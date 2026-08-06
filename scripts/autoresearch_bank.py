#!/usr/bin/env python3
"""Compute bank accounting: what capacity exists, and who currently holds it."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from autoresearch_core import (
    DOCS_DIR,
    AutoresearchError,
    parse_json,
    require_exact_keys,
)

COMPUTE_FILE = "compute.json"
BANK_KEYS = {"cores_per_candidate", "measurement", "bank", "workers"}
CORES_ENTRY_KEYS = {"id", "kind", "cores", "label"}
NODE_ENTRY_KEYS = {"id", "kind", "capacity", "label"}
AGENTS_ENTRY_KEYS = {"id", "kind", "slots", "label"}
WORKER_TIERS = ("simple", "standard", "complex")
TIER_KEYS = {"model", "thinking_tokens"}
MEASUREMENT_MODES = {"parallel", "exclusive"}


def compute_path(repo: Path) -> Path:
    """
    Resolve the compute bank document.
    Args:
    repo: Repository root.
    Return: Absolute path to compute.json.
    """
    return repo / DOCS_DIR / COMPUTE_FILE


def load_bank(repo: Path) -> dict[str, Any]:
    """
    Read and validate the compute bank.
    Args:
    repo: Repository root.
    Return: Validated bank document.

    Every field is required. Nothing is inferred, defaulted, or auto-detected, so a
    bank that does not describe real capacity fails here rather than mid-run.
    """
    path = compute_path(repo)
    if not path.exists():
        raise AutoresearchError(
            f"Compute bank is missing: {path}. Run 'compute detect' to see local "
            f"capacity, then write the bank explicitly."
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AutoresearchError(f"Cannot read {path}: {exc}") from exc
    payload = parse_json(text, source=str(path))
    if not isinstance(payload, dict):
        raise AutoresearchError(f"{path} must contain an object")
    require_exact_keys(payload, required=BANK_KEYS, source=str(path))

    per_candidate = payload["cores_per_candidate"]
    if not isinstance(per_candidate, int) or isinstance(per_candidate, bool) or per_candidate <= 0:
        raise AutoresearchError(f"{path}.cores_per_candidate must be a positive integer")
    if payload["measurement"] not in MEASUREMENT_MODES:
        raise AutoresearchError(
            f"{path}.measurement must be one of {', '.join(sorted(MEASUREMENT_MODES))}"
        )
    entries = payload["bank"]
    if not isinstance(entries, list) or not entries:
        raise AutoresearchError(f"{path}.bank must be a non-empty array")

    seen: set[str] = set()
    for index, entry in enumerate(entries):
        source = f"{path}.bank[{index}]"
        if not isinstance(entry, dict):
            raise AutoresearchError(f"{source} must be an object")
        kind = entry.get("kind")
        if kind == "cores":
            require_exact_keys(entry, required=CORES_ENTRY_KEYS, source=source)
            if (
                not isinstance(entry["cores"], int)
                or isinstance(entry["cores"], bool)
                or entry["cores"] <= 0
            ):
                raise AutoresearchError(f"{source}.cores must be a positive integer")
        elif kind == "agents":
            require_exact_keys(entry, required=AGENTS_ENTRY_KEYS, source=source)
            if (
                not isinstance(entry["slots"], int)
                or isinstance(entry["slots"], bool)
                or entry["slots"] <= 0
            ):
                raise AutoresearchError(f"{source}.slots must be a positive integer")
        elif kind == "node":
            require_exact_keys(entry, required=NODE_ENTRY_KEYS, source=source)
            if (
                not isinstance(entry["capacity"], int)
                or isinstance(entry["capacity"], bool)
                or entry["capacity"] <= 0
            ):
                raise AutoresearchError(f"{source}.capacity must be a positive integer")
        else:
            raise AutoresearchError(
                f"{source}.kind must be cores, agents, or node, got {kind!r}"
            )
        for key in ("id", "label"):
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise AutoresearchError(f"{source}.{key} must be a non-empty string")
        if entry["id"] in seen:
            raise AutoresearchError(f"{source}.id duplicates an earlier entry: {entry['id']}")
        seen.add(entry["id"])

    workers = payload["workers"]
    if not isinstance(workers, dict):
        raise AutoresearchError(f"{path}.workers must be an object")
    require_exact_keys(workers, required=set(WORKER_TIERS), source=f"{path}.workers")
    for tier in WORKER_TIERS:
        entry = workers[tier]
        if not isinstance(entry, dict):
            raise AutoresearchError(f"{path}.workers.{tier} must be an object")
        require_exact_keys(entry, required=TIER_KEYS, source=f"{path}.workers.{tier}")
        if not isinstance(entry["model"], str) or not entry["model"].strip():
            raise AutoresearchError(f"{path}.workers.{tier}.model must be a non-empty string")
        if (
            not isinstance(entry["thinking_tokens"], int)
            or isinstance(entry["thinking_tokens"], bool)
            or entry["thinking_tokens"] < 0
        ):
            raise AutoresearchError(
                f"{path}.workers.{tier}.thinking_tokens must be a non-negative integer"
            )
    return payload


def entry_capacity(entry: dict[str, Any], cores_per_candidate: int) -> int:
    """
    Count how many candidates one bank entry can hold at once.
    Args:
    entry: Validated bank entry.
    cores_per_candidate: Cores each candidate is granted.
    Return: Number of concurrent candidates this entry supports.
    """
    if entry["kind"] == "cores":
        return entry["cores"] // cores_per_candidate
    if entry["kind"] == "agents":
        return entry["slots"]
    return entry["capacity"]


def bank_capacity(bank: dict[str, Any]) -> int:
    """
    Total concurrent candidates the whole bank supports.
    Args:
    bank: Validated bank document.
    Return: Sum of every entry's capacity.
    """
    return sum(entry_capacity(entry, bank["cores_per_candidate"]) for entry in bank["bank"])


def allocate_grant(bank: dict[str, Any], held: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    Grant compute to one candidate, or report exhaustion.
    Args:
    bank: Validated bank document.
    held: Grants currently outstanding.
    Return: A grant, or None when every entry is at capacity.

    Allocation order is deterministic so a replayed log reproduces the same grants:
    entries in declared order, first entry with free capacity wins.
    """
    per_candidate = bank["cores_per_candidate"]
    used: dict[str, int] = {}
    for grant in held:
        used[grant["source_id"]] = used.get(grant["source_id"], 0) + 1
    for entry in bank["bank"]:
        if used.get(entry["id"], 0) >= entry_capacity(entry, per_candidate):
            continue
        if entry["kind"] == "cores":
            return {
                "source_id": entry["id"],
                "kind": "cores",
                "cores": per_candidate,
                "label": entry["label"],
            }
        if entry["kind"] == "agents":
            # A subagent slot, not CPU. The limiting resource on a host like Claude Code
            # is how many workers may run at once, which cores cannot express.
            return {
                "source_id": entry["id"],
                "kind": "agents",
                "cores": None,
                "label": entry["label"],
            }
        return {
            "source_id": entry["id"],
            "kind": "node",
            "cores": None,
            "label": entry["label"],
        }
    return None


def exhausted_entries(bank: dict[str, Any], held: list[dict[str, Any]]) -> list[str]:
    """
    Name the bank entries with no free capacity.
    Args:
    bank: Validated bank document.
    held: Grants currently outstanding.
    Return: Identifiers of entries that are full.
    """
    per_candidate = bank["cores_per_candidate"]
    used: dict[str, int] = {}
    for grant in held:
        used[grant["source_id"]] = used.get(grant["source_id"], 0) + 1
    return [
        entry["id"]
        for entry in bank["bank"]
        if used.get(entry["id"], 0) >= entry_capacity(entry, per_candidate)
    ]


def grant_environment(grant: dict[str, Any]) -> dict[str, str]:
    """
    Express a grant as environment variables a worker command can honor.
    Args:
    grant: Allocated grant.
    Return: Environment mapping describing the granted compute.
    """
    environment = {
        "AUTORESEARCH_GRANT_SOURCE": grant["source_id"],
        "AUTORESEARCH_GRANT_KIND": grant["kind"],
        "AUTORESEARCH_GRANT_LABEL": grant["label"],
    }
    if grant["cores"] is not None:
        environment["AUTORESEARCH_CORES"] = str(grant["cores"])
    return environment


def detect_local_capacity() -> list[dict[str, Any]]:
    """
    Observe local CPU capacity, recording where each number came from.
    Args:
    none
    Return: Observations, each naming its source and value.

    This reports; it never decides. CI pins Python 3.11, so os.process_cpu_count is
    unavailable. An unavailable observation is reported as such and never replaced
    with a guess.
    """
    observations: list[dict[str, Any]] = []

    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        observations.append(
            {
                "source": "sched_getaffinity",
                "cores": len(affinity(0)),
                "note": "cores this process is allowed to run on",
            }
        )
    else:
        observations.append(
            {
                "source": "sched_getaffinity",
                "cores": None,
                "note": "unavailable on this platform",
            }
        )

    quota_path = Path("/sys/fs/cgroup/cpu.max")
    if quota_path.exists():
        try:
            quota, period = quota_path.read_text(encoding="utf-8").split()
            cores = None if quota == "max" else max(1, int(quota) // int(period))
            note = "cgroup v2 quota" if cores is not None else "cgroup v2 quota is unlimited"
        except (OSError, ValueError) as exc:
            cores, note = None, f"cgroup v2 quota unreadable: {exc}"
        observations.append({"source": "cgroup_v2_cpu_max", "cores": cores, "note": note})
    else:
        observations.append(
            {
                "source": "cgroup_v2_cpu_max",
                "cores": None,
                "note": "no cgroup v2 cpu.max on this host",
            }
        )

    count = os.cpu_count()
    observations.append(
        {
            "source": "os.cpu_count",
            "cores": count,
            "note": "unavailable" if count is None else "total logical cores",
        }
    )
    return observations
