#!/usr/bin/env python3
"""Slot worktrees, lease-based liveness, and the admission lock."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from autoresearch_core import (
    AutoresearchError,
    Paths,
    json_text,
    parse_json,
    require_exact_keys,
    run_command,
    run_git,
    utc_now,
    write_json_atomic,
)

SLOTS_FILE = "slots.json"
LOCK_FILE = "admission.lock"
SLOTS_KEYS = {"run_id", "updated_at", "max_parallel", "worktree_root", "slots"}
SLOT_KEYS = {
    "slot",
    "worktree",
    "branch",
    "state",
    "candidate",
    "role",
    "grant",
    "agent_ref",
    "claimed_at",
    "lease_expires_at",
}
SLOT_STATES = {"idle", "preparing", "live", "measuring", "admitting", "broken"}


def slots_path(paths: Paths) -> Path:
    """
    Resolve the slot liveness file.
    Args:
    paths: Resolved run paths.
    Return: Path to slots.json.
    """
    return paths.root / SLOTS_FILE


def lock_path(paths: Paths) -> Path:
    """
    Resolve the admission lock file.
    Args:
    paths: Resolved run paths.
    Return: Path to admission.lock.
    """
    return paths.root / LOCK_FILE


def empty_slots(run_id: str, *, max_parallel: int, worktree_root: str) -> dict[str, Any]:
    """
    Build the initial slot table.
    Args:
    run_id: Active run identifier.
    max_parallel: Concurrency the run is allowed.
    worktree_root: Absolute directory holding every slot worktree.
    Return: A slot table with one idle slot per allowed concurrent candidate.
    """
    return {
        "run_id": run_id,
        "updated_at": utc_now(),
        "max_parallel": max_parallel,
        "worktree_root": worktree_root,
        "slots": [
            {
                "slot": index,
                "worktree": str(Path(worktree_root) / f"slot-{index}"),
                "branch": None,
                "state": "idle",
                "candidate": None,
                "role": None,
                "grant": None,
                "agent_ref": None,
                "claimed_at": None,
                "lease_expires_at": None,
            }
            for index in range(1, max_parallel + 1)
        ],
    }


def load_slots(paths: Paths, run: dict[str, Any]) -> dict[str, Any]:
    """
    Read and validate the slot table, creating it on first use.
    Args:
    paths: Resolved run paths.
    run: Validated run configuration.
    Return: Validated slot table.
    """
    path = slots_path(paths)
    if not path.exists():
        table = empty_slots(
            run["run_id"],
            max_parallel=run["parallel"]["max_parallel_resolved"],
            worktree_root=run["parallel"]["worktree_root"],
        )
        write_json_atomic(path, table)
        return table
    payload = parse_json(path.read_text(encoding="utf-8"), source=str(path))
    if not isinstance(payload, dict):
        raise AutoresearchError(f"{path} must contain an object")
    require_exact_keys(payload, required=SLOTS_KEYS, source=str(path))
    if payload["run_id"] != run["run_id"]:
        raise AutoresearchError(f"{path}.run_id does not match the active run")
    if not isinstance(payload["slots"], list) or not payload["slots"]:
        raise AutoresearchError(f"{path}.slots must be a non-empty array")
    for index, slot in enumerate(payload["slots"]):
        source = f"{path}.slots[{index}]"
        if not isinstance(slot, dict):
            raise AutoresearchError(f"{source} must be an object")
        require_exact_keys(slot, required=SLOT_KEYS, source=source)
        if slot["state"] not in SLOT_STATES:
            raise AutoresearchError(f"{source}.state is invalid: {slot['state']!r}")
    return payload


def save_slots(paths: Paths, table: dict[str, Any]) -> None:
    """
    Persist the slot table.
    Args:
    paths: Resolved run paths.
    table: Slot table to write.
    Return: None.
    """
    table["updated_at"] = utc_now()
    write_json_atomic(slots_path(paths), table)


def held_grants(table: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Collect every grant currently outstanding.
    Args:
    table: Slot table.
    Return: Grants held by live slots.
    """
    return [slot["grant"] for slot in table["slots"] if slot["grant"] is not None]


def live_roles(table: dict[str, Any]) -> dict[str, int]:
    """
    Count started-but-unresolved candidates by role.
    Args:
    table: Slot table.
    Return: Mapping of role to live count.
    """
    counts = {"exploit": 0, "explore": 0}
    for slot in table["slots"]:
        if slot["role"] in counts and slot["candidate"] is not None:
            counts[slot["role"]] += 1
    return counts


def free_slot(table: dict[str, Any]) -> dict[str, Any] | None:
    """
    Find the lowest-numbered slot available for a new candidate.
    Args:
    table: Slot table.
    Return: An idle slot, or None when every slot is busy or broken.
    """
    for slot in table["slots"]:
        if slot["state"] == "idle":
            return slot
    return None


def lease_expired(slot: dict[str, Any], *, now: float) -> bool:
    """
    Report whether a slot's lease has lapsed.
    Args:
    slot: Slot record.
    now: Current epoch seconds.
    Return: True when the slot holds a candidate whose lease deadline has passed.
    """
    if slot["candidate"] is None or slot["lease_expires_at"] is None:
        return False
    return now > float(slot["lease_expires_at"])


def prepare_worktree(
    repo: Path,
    slot: dict[str, Any],
    *,
    branch: str,
    frontier: str,
    prepare: str | None,
    timeout_seconds: int,
    log_path: Path,
) -> None:
    """
    Create or reset one slot's worktree onto the frontier.
    Args:
    repo: Primary repository root.
    slot: Slot record to prepare.
    branch: Candidate branch to check out in the worktree.
    frontier: Commit the worktree must start from.
    prepare: One-time setup command for a newly created worktree, or None.
    timeout_seconds: Timeout for the setup command.
    log_path: Where to record setup command output.
    Return: None.

    A reused worktree is cleaned with -d but never -x. Removing ignored files would
    delete node_modules and .venv, destroying the per-slot amortization that
    long-lived worktrees exist to provide.
    """
    worktree = Path(slot["worktree"])
    if not worktree.exists():
        worktree.parent.mkdir(parents=True, exist_ok=True)
        run_git(repo, "worktree", "add", "--force", "-B", branch, str(worktree), frontier)
        if prepare:
            result = run_command(
                command=prepare,
                cwd=worktree,
                timeout_seconds=timeout_seconds,
                log_path=log_path,
            )
            if result.returncode != 0:
                raise AutoresearchError(
                    f"Slot {slot['slot']} preparation command failed: {prepare}. "
                    f"Full output: {log_path}"
                )
        return
    run_git(repo, "worktree", "repair", str(worktree), check=False)
    run_git(worktree, "checkout", "-B", branch, frontier)
    run_git(worktree, "reset", "--hard", frontier)
    run_git(worktree, "clean", "-df")


def remove_worktree(repo: Path, slot: dict[str, Any]) -> None:
    """
    Detach one slot's worktree from the repository.
    Args:
    repo: Primary repository root.
    slot: Slot record whose worktree should be removed.
    Return: None.
    """
    worktree = Path(slot["worktree"])
    if worktree.exists():
        run_git(repo, "worktree", "remove", "--force", str(worktree), check=False)
    run_git(repo, "worktree", "prune", check=False)


def acquire_admission_lock(
    paths: Paths, *, run_id: str, candidate: int, wait_seconds: float = 120.0
) -> None:
    """
    Take the exclusive right to move the frontier.
    Args:
    paths: Resolved run paths.
    run_id: Active run identifier.
    candidate: Candidate attempting admission.
    wait_seconds: How long to wait for a competing admission to finish.
    Return: None.

    The holder is a real CLI process, so a PID recorded here can be checked for
    liveness. That is not true of workers, whose liveness is tracked by lease instead.
    """
    path = lock_path(paths)
    deadline = time.monotonic() + wait_seconds
    payload = {
        "run_id": run_id,
        "pid": os.getpid(),
        "candidate": candidate,
        "acquired_at": utc_now(),
    }
    while True:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise AutoresearchError(
                    f"Timed out waiting {wait_seconds:.0f}s for the admission lock at {path}. "
                    f"If no candidate is admitting, run reconcile."
                ) from None
            time.sleep(0.05)
            continue
        try:
            os.write(descriptor, (json_text(payload, pretty=True) + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return


def release_admission_lock(paths: Paths) -> None:
    """
    Give up the frontier lock.
    Args:
    paths: Resolved run paths.
    Return: None.
    """
    path = lock_path(paths)
    if path.exists():
        path.unlink()


def read_admission_lock(paths: Paths) -> dict[str, Any] | None:
    """
    Inspect the current lock holder.
    Args:
    paths: Resolved run paths.
    Return: The lock payload, or None when the lock is free.
    """
    path = lock_path(paths)
    if not path.exists():
        return None
    return parse_json(path.read_text(encoding="utf-8"), source=str(path))
