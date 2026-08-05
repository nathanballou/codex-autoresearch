#!/usr/bin/env python3
"""Command line control plane for autoresearch."""

from __future__ import annotations

import argparse
import shutil
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from autoresearch_core import (
    AutoresearchError,
    Paths,
    RunState,
    commit_trial,
    decimal_json,
    git_branch,
    git_head,
    improved,
    json_text,
    next_command_log,
    normalize_scopes,
    parse_decimal,
    parse_json,
    parse_metric_output,
    paths_for,
    process_alive,
    relative_log_path,
    require_clean_repo,
    require_artifacts_untracked,
    require_git_repo,
    require_git_identity,
    require_no_staged_artifacts,
    require_exact_keys,
    require_paths_in_scope,
    run_command,
    run_git,
    target_reached,
    utc_now,
    working_paths,
    write_json_atomic,
    write_text_atomic,
)
from autoresearch_allocator import choose_role
from autoresearch_bank import (
    allocate_grant,
    grant_environment,
    bank_capacity,
    compute_path,
    detect_local_capacity,
    exhausted_entries,
    load_bank,
)
from autoresearch_packet import build_packet
from autoresearch_slots import (
    acquire_admission_lock,
    free_slot,
    held_grants,
    lease_expired,
    live_roles,
    load_slots,
    prepare_worktree,
    read_admission_lock,
    release_admission_lock,
    save_slots,
    slots_path,
    unresolved_from,
    update_slot,
)
from autoresearch_docs import (
    DECISIONS_FILE,
    GOAL_FILE,
    append_decision,
    load_and_snapshot,
    read_doc,
    require_docs_match,
)
from autoresearch_state import (
    SCHEMA_VERSION,
    append_event,
    load_context,
    status_payload,
    validate_run,
)
from autoresearch_report import render_history_table, render_history_tsv, render_html_report


def print_json(payload: dict[str, Any]) -> None:
    print(json_text(payload, pretty=True))


def add_repo_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, help="Absolute or relative Git repository root.")


def add_run_arguments(parser: argparse.ArgumentParser) -> None:
    add_repo_argument(parser)
    parser.add_argument("--goal", required=True)
    parser.add_argument(
        "--scope",
        action="append",
        required=True,
        help="Repository-relative file or directory. Repeat for multiple paths; globs are rejected.",
    )
    parser.add_argument("--metric-name", required=True)
    parser.add_argument("--direction", choices=["lower", "higher"], required=True)
    parser.add_argument("--verify", required=True, help="Command whose final stdout line is the metric.")
    parser.add_argument("--metric-key", help="Read the metric from this key in a final-line JSON object.")
    parser.add_argument("--target", required=True)
    parser.add_argument("--guard", help="Regression command; exit code 0 means pass.")
    parser.add_argument("--max-candidates", type=int)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument(
        "--max-parallel",
        required=True,
        help="Concurrent candidates: a positive integer, or 'bank' for the whole compute bank.",
    )
    parser.add_argument(
        "--worktree-root",
        required=True,
        help="Absolute directory for slot worktrees. Must be outside the repository.",
    )
    parser.add_argument(
        "--prepare", help="One-time setup command run in each new slot worktree."
    )
    parser.add_argument("--lease-seconds", type=int, required=True)
    parser.add_argument("--window", type=int, required=True)
    parser.add_argument("--min-per-role", type=int, required=True)
    parser.add_argument("--plateau-k", type=int, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run measurable, Git-backed autoresearch experiments with strict state validation."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Validate and initialize a run.")
    add_run_arguments(init_parser)

    finish_parser = subparsers.add_parser(
        "finish", help="Commit, verify, and keep or revert one focused experiment."
    )
    add_repo_argument(finish_parser)
    finish_parser.add_argument("--description", required=True)
    finish_parser.add_argument(
        "--candidate", type=int, help="Candidate to finish. Required once slots are in use."
    )

    block_parser = subparsers.add_parser("block", help="Stop on a genuine external blocker.")
    add_repo_argument(block_parser)
    block_parser.add_argument("--reason", required=True)

    status_parser = subparsers.add_parser("status", help="Print validated run status.")
    add_repo_argument(status_parser)

    history_parser = subparsers.add_parser(
        "history", help="Print a validated human-readable or TSV experiment history."
    )
    add_repo_argument(history_parser)
    history_parser.add_argument("--format", choices=["table", "tsv"], default="table")

    report_parser = subparsers.add_parser(
        "report", help="Generate a self-contained static HTML report."
    )
    add_repo_argument(report_parser)

    resume_parser = subparsers.add_parser("resume", help="Resume a stopped or blocked run.")
    add_repo_argument(resume_parser)
    resume_parser.add_argument("--note", required=True)

    claim_parser = subparsers.add_parser(
        "claim", help="Claim slots and emit one worker packet per claimed candidate."
    )
    add_repo_argument(claim_parser)
    claim_parser.add_argument("--count", type=int, default=1)
    claim_parser.add_argument("--role", choices=["exploit", "explore"])
    claim_parser.add_argument("--role-reason", help="Required when overriding the policy role.")

    bind_parser = subparsers.add_parser(
        "bind", help="Record the host-assigned agent id for a claimed candidate."
    )
    add_repo_argument(bind_parser)
    bind_parser.add_argument("--candidate", type=int, required=True)
    bind_parser.add_argument("--agent-ref", required=True)

    heartbeat_parser = subparsers.add_parser(
        "heartbeat", help="Extend a candidate's lease before a long operation."
    )
    add_repo_argument(heartbeat_parser)
    heartbeat_parser.add_argument("--candidate", type=int, required=True)

    abandon_parser = subparsers.add_parser(
        "abandon", help="Give up a claimed candidate without admitting anything."
    )
    add_repo_argument(abandon_parser)
    abandon_parser.add_argument("--candidate", type=int, required=True)
    abandon_parser.add_argument("--reason", required=True)

    reap_parser = subparsers.add_parser(
        "reap", help="Resolve a candidate whose lease expired and free its slot."
    )
    add_repo_argument(reap_parser)
    reap_parser.add_argument("--candidate", type=int, required=True)

    reconcile_parser = subparsers.add_parser(
        "reconcile", help="Report recoverable problems and clear a stale admission lock."
    )
    add_repo_argument(reconcile_parser)

    compute_parser = subparsers.add_parser(
        "compute", help="Inspect compute capacity. Reports only; never writes."
    )
    compute_sub = compute_parser.add_subparsers(dest="compute_command", required=True)
    detect_parser = compute_sub.add_parser(
        "detect", help="Report observed local capacity with the provenance of each number."
    )
    add_repo_argument(detect_parser)

    decide_parser = subparsers.add_parser(
        "decide", help="Record one curated decision every future candidate will receive."
    )
    add_repo_argument(decide_parser)
    decide_parser.add_argument("--add", required=True, help="Decision or note to append.")

    archive_parser = subparsers.add_parser(
        "archive", help="Archive the current run before starting a different one."
    )
    add_repo_argument(archive_parser)

    return parser


def parallel_config(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    """
    Build the parallelism block from explicit flags and the declared compute bank.
    Args:
    repo: Repository root.
    args: Parsed init arguments.
    Return: The parallel configuration recorded in run.json.

    Nothing here is defaulted. A worktree root inside the repository is rejected,
    because a verify command run from the repository root would recurse into every
    slot worktree and silently multiply the metric.
    """
    bank = load_bank(repo)
    capacity = bank_capacity(bank)
    if capacity <= 0:
        raise AutoresearchError(
            f"The compute bank at {compute_path(repo)} supports no candidates. "
            f"Lower cores_per_candidate or declare more capacity."
        )
    if args.max_parallel == "bank":
        resolved = capacity
    else:
        try:
            resolved = int(args.max_parallel)
        except ValueError:
            raise AutoresearchError(
                "--max-parallel must be a positive integer or the string bank"
            ) from None
        if resolved <= 0:
            raise AutoresearchError("--max-parallel must be a positive integer or bank")
        resolved = min(resolved, capacity)
    root = Path(args.worktree_root).expanduser().resolve()
    try:
        root.relative_to(repo)
    except ValueError:
        pass
    else:
        raise AutoresearchError(
            f"--worktree-root must be outside the repository. {root} is inside {repo}, "
            f"so a verify command run from the root would collect every slot copy."
        )
    for name in ("lease_seconds", "window", "min_per_role", "plateau_k"):
        if getattr(args, name) <= 0:
            raise AutoresearchError(f"--{name.replace('_', '-')} must be a positive integer")
    return {
        "max_parallel": args.max_parallel if args.max_parallel == "bank" else resolved,
        "max_parallel_resolved": resolved,
        "worktree_root": str(root),
        "prepare": args.prepare,
        "lease_seconds": args.lease_seconds,
        "allocation": {
            "window": args.window,
            "min_per_role": args.min_per_role,
            "plateau_k": args.plateau_k,
        },
    }


def ensure_new_results_root(repo: Path) -> None:
    paths = paths_for(repo)
    existing = []
    if paths.root.exists():
        existing = sorted(child.name for child in paths.root.iterdir() if child.name != "archive")
    if existing:
        raise AutoresearchError(
            f"Autoresearch state already exists at {paths.root}: {', '.join(existing)}. "
            "Use status, resume, or archive before starting a new run."
        )
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.logs.mkdir(parents=True, exist_ok=True)


def write_init_error(repo: Path, error: Exception) -> None:
    paths = paths_for(repo)
    if not paths.root.exists():
        return
    write_json_atomic(
        paths.root / "init-error.json",
        {
            "time": utc_now(),
            "error_type": type(error).__name__,
            "message": str(error),
            "traceback": traceback.format_exc(),
        },
    )


def require_no_initialized_state(paths: Paths, *, command_name: str, log_path: Path) -> None:
    created = [
        path
        for path in paths.root.iterdir()
        if path.name not in {"archive", paths.logs.name}
    ]
    if created:
        raise AutoresearchError(
            f"{command_name} created autoresearch control files: "
            + ", ".join(str(path) for path in created)
            + f". Full output: {log_path}"
        )


def control_state_snapshot(paths: Paths) -> tuple[bytes, bytes]:
    try:
        return paths.run.read_bytes(), paths.events.read_bytes()
    except OSError as exc:
        raise AutoresearchError(f"Cannot snapshot autoresearch control state: {exc}") from exc


def require_control_state_unchanged(
    paths: Paths,
    snapshot: tuple[bytes, bytes],
    *,
    command_name: str,
    log_path: Path,
) -> None:
    try:
        current = (paths.run.read_bytes(), paths.events.read_bytes())
    except OSError as exc:
        raise AutoresearchError(
            f"{command_name} removed or replaced autoresearch control state: {exc}. "
            f"Full output: {log_path}"
        ) from exc
    if current != snapshot:
        raise AutoresearchError(
            f"{command_name} modified run.json or events.jsonl. Full output: {log_path}"
        )


def require_command_preserved_repository(
    repo: Path,
    *,
    expected_head: str,
    expected_branch: str,
    command_name: str,
    log_path: Path,
) -> None:
    problems: list[str] = []
    dirty = working_paths(repo)
    if dirty:
        problems.append("changed paths " + ", ".join(dirty))
    current_head = git_head(repo)
    if current_head != expected_head:
        problems.append(f"moved HEAD from {expected_head} to {current_head}")
    try:
        current_branch = git_branch(repo)
    except AutoresearchError as exc:
        problems.append(str(exc))
    else:
        if current_branch != expected_branch:
            problems.append(f"switched branch from {expected_branch} to {current_branch}")
    if problems:
        raise AutoresearchError(
            f"{command_name} modified the repository: {'; '.join(problems)}. "
            f"Full output: {log_path}"
        )


def initialize_run(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo).expanduser().resolve()
    require_git_repo(repo)
    require_git_identity(repo)
    require_artifacts_untracked(repo)
    require_clean_repo(repo)
    require_no_staged_artifacts(repo)
    goal = " ".join(args.goal.split())
    metric_name = " ".join(args.metric_name.split())
    verify = args.verify.strip()
    metric_key = None if args.metric_key is None else args.metric_key.strip()
    guard = None if args.guard is None else args.guard.strip()
    if not goal:
        raise AutoresearchError("--goal cannot be empty")
    if not metric_name:
        raise AutoresearchError("--metric-name cannot be empty")
    if not verify:
        raise AutoresearchError("--verify cannot be empty")
    if args.metric_key is not None and not metric_key:
        raise AutoresearchError("--metric-key cannot be empty")
    if args.guard is not None and not guard:
        raise AutoresearchError("--guard cannot be empty")
    if args.max_candidates is not None and args.max_candidates <= 0:
        raise AutoresearchError("--max-candidates must be a positive integer")
    if args.timeout_seconds <= 0:
        raise AutoresearchError("--timeout-seconds must be a positive integer")
    target = parse_decimal(args.target, field="--target")
    target_json = decimal_json(target)
    scopes = normalize_scopes(repo, args.scope)
    branch = git_branch(repo)
    head = git_head(repo)
    ensure_new_results_root(repo)
    paths = paths_for(repo)

    try:
        verify_result = run_command(
            command=verify,
            cwd=repo,
            timeout_seconds=args.timeout_seconds,
            log_path=next_command_log(paths, 0, "baseline-verify"),
        )
        require_no_initialized_state(
            paths,
            command_name="Baseline metric command",
            log_path=verify_result.log_path,
        )
        require_command_preserved_repository(
            repo,
            expected_head=head,
            expected_branch=branch,
            command_name="Baseline metric command",
            log_path=verify_result.log_path,
        )
        baseline = parse_metric_output(verify_result, json_key=metric_key)
        guard_log = None
        if guard:
            guard_result = run_command(
                command=guard,
                cwd=repo,
                timeout_seconds=args.timeout_seconds,
                log_path=next_command_log(paths, 0, "baseline-guard"),
            )
            guard_log = guard_result.log_path
            require_no_initialized_state(
                paths,
                command_name="Baseline guard command",
                log_path=guard_result.log_path,
            )
            require_command_preserved_repository(
                repo,
                expected_head=head,
                expected_branch=branch,
                command_name="Baseline guard command",
                log_path=guard_result.log_path,
            )
            if guard_result.returncode != 0:
                raise AutoresearchError(
                    f"Baseline guard exited {guard_result.returncode}: {guard}. "
                    f"Full output: {guard_result.log_path}"
                )
        run_id = uuid.uuid4().hex
        run = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": utc_now(),
            "repo": str(repo),
            "branch": branch,
            "goal": goal,
            "scope": scopes,
            "metric": {
                "name": metric_name,
                "direction": args.direction,
                "command": verify,
                "json_key": metric_key,
            },
            "guard": guard,
            "target": target_json,
            "max_candidates": args.max_candidates,
            "timeout_seconds": args.timeout_seconds,
            "docs": load_and_snapshot(repo, paths),
            "parallel": parallel_config(repo, args),
        }
        validate_run(run, source="new run")
        write_json_atomic(paths.run, run)
        events: list[dict[str, Any]] = []
        baseline_event = append_event(
            paths,
            run,
            events,
            event="baseline",
            head=head,
            metric=decimal_json(baseline),
            verify_log=relative_log_path(paths, verify_result.log_path),
            guard_log=relative_log_path(paths, guard_log),
        )
        events.append(baseline_event)
        status = "active"
        if target_reached(baseline, target, args.direction):
            complete_event = append_event(
                paths,
                run,
                events,
                event="complete",
                reason="baseline already satisfies the target",
                head=head,
                metric=decimal_json(baseline),
                unresolved_candidates=[],
            )
            events.append(complete_event)
            status = "complete"
        return {
            "run_id": run_id,
            "status": status,
            "baseline": decimal_json(baseline),
            "target": target_json,
            "results": str(paths.root),
        }
    except Exception as exc:
        try:
            write_init_error(repo, exc)
        except Exception as log_error:
            raise AutoresearchError(
                f"Initialization failed: {exc}; writing init-error.json also failed: {log_error}"
            ) from exc
        if isinstance(exc, AutoresearchError):
            diagnostic = paths_for(repo).root / "init-error.json"
            raise AutoresearchError(
                f"{exc}. Initialization diagnostics: {diagnostic}. "
                "Archive the failed initialization before retrying."
            ) from exc
        raise


def append_error(
    *,
    paths: Paths,
    run: dict[str, Any],
    events: list[dict[str, Any]],
    state: RunState,
    reason: str,
    head: str | None = None,
    trial_commit: str | None = None,
    unresolved_candidates: list[int] | None = None,
    log: Path | None = None,
) -> None:
    append_event(
        paths,
        run,
        events,
        event="error",
        reason=reason,
        head=head or state.head,
        metric=decimal_json(state.metric),
        trial_commit=trial_commit,
        unresolved_candidates=(
            list(state.unresolved) if unresolved_candidates is None else unresolved_candidates
        ),
        log=relative_log_path(paths, log),
    )


def candidate_branch(run: dict[str, Any], candidate: int) -> str:
    """
    Build the audit branch name that preserves one candidate's trial commit.
    Args:
    run: Validated run configuration supplying the run id.
    candidate: Monotonic candidate identifier.
    Return: Branch name of the form autoresearch/<run8>/c<NNNN>.
    """
    return f"autoresearch/{run['run_id'][:8]}/c{candidate:04d}"


def preserve_and_reset(repo: Path, *, branch: str, trial_commit: str, frontier: str) -> None:
    """
    Keep a rejected trial commit reachable, then return the run branch to the frontier.
    Args:
    repo: Repository root.
    branch: Audit branch to create at the trial commit.
    trial_commit: Commit produced by the rejected candidate.
    frontier: Commit the run branch must return to.
    Return: None.
    """
    run_git(repo, "branch", "--force", branch, trial_commit)
    run_git(repo, "reset", "--hard", frontier)


def safely_restore_after_error(
    *,
    repo: Path,
    paths: Paths,
    run: dict[str, Any],
    events: list[dict[str, Any]],
    state: RunState,
    candidate: int,
    trial_commit: str,
    reason: str,
    log: Path | None,
) -> None:
    current_head = git_head(repo)
    try:
        current_branch = git_branch(repo)
    except AutoresearchError as branch_error:
        current_branch = None
        reason += f"; {branch_error}"
    if current_head != trial_commit or current_branch != run["branch"]:
        append_error(
            paths=paths,
            run=run,
            events=events,
            state=state,
            reason=(
                reason
                + "; automatic rollback was not attempted because Git moved away from "
                + f"trial {trial_commit} on branch {run['branch']} "
                + f"(current HEAD {current_head}, branch {current_branch or 'detached'})"
            ),
            head=trial_commit,
            trial_commit=trial_commit,
            unresolved_candidates=[candidate],
            log=log,
        )
        return
    dirty = working_paths(repo)
    if dirty:
        append_error(
            paths=paths,
            run=run,
            events=events,
            state=state,
            reason=reason + "; automatic rollback was not attempted because commands left changes: " + ", ".join(dirty),
            head=trial_commit,
            trial_commit=trial_commit,
            unresolved_candidates=[candidate],
            log=log,
        )
        return
    try:
        preserve_and_reset(
            repo,
            branch=candidate_branch(run, candidate),
            trial_commit=trial_commit,
            frontier=state.head,
        )
    except AutoresearchError as restore_error:
        current_head = git_head(repo)
        append_error(
            paths=paths,
            run=run,
            events=events,
            state=state,
            reason=(
                f"{reason}; rollback failed: {restore_error}; "
                f"current HEAD is {current_head}"
            ),
            head=trial_commit,
            trial_commit=trial_commit,
            unresolved_candidates=[candidate],
            log=log,
        )
        return
    append_error(
        paths=paths,
        run=run,
        events=events,
        state=state,
        reason=reason,
        head=state.head,
        trial_commit=trial_commit,
        unresolved_candidates=[candidate],
        log=log,
    )


def recorded_doc_digests(run: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, str]:
    """
    Resolve the curated document digests currently in force.
    Args:
    run: Validated run configuration carrying the digests recorded at init.
    events: Full validated event list, which may contain later decisions.
    Return: Mapping of goal_sha256 and decisions_sha256 as last recorded.
    """
    digests = {
        "goal_sha256": run["docs"]["goal_sha256"],
        "decisions_sha256": run["docs"]["decisions_sha256"],
    }
    for event in events:
        if event["event"] == "decision":
            digests["decisions_sha256"] = event["decisions_sha256"]
    return digests


def measure_in(
    *,
    worktree: Path,
    run: dict[str, Any],
    paths: Paths,
    expected_head: str,
    branch: str,
    log_path: Path,
    label: str,
    grant: dict[str, Any],
) -> Any:
    """
    Run the verify command inside one worktree and parse its metric.
    Args:
    worktree: Directory the command runs in.
    run: Validated run configuration.
    paths: Resolved run paths.
    expected_head: Commit the worktree must still be on afterwards.
    branch: Branch the worktree must still be on afterwards.
    log_path: Where to record command output.
    label: Human label used in error messages.
    grant: Compute grant exported to the command environment.
    Return: The parsed metric.

    The command must leave both the worktree and the primary checkout untouched. A
    verify command that wanders into the primary repository is an error, not a surprise.
    """
    primary_head = git_head(paths.repo)
    control_snapshot = control_state_snapshot(paths)
    result = run_command(
        command=run["metric"]["command"],
        cwd=worktree,
        timeout_seconds=run["timeout_seconds"],
        log_path=log_path,
        environment=grant_environment(grant),
    )
    require_control_state_unchanged(
        paths, control_snapshot, command_name=label, log_path=log_path
    )
    require_command_preserved_repository(
        worktree,
        expected_head=expected_head,
        expected_branch=branch,
        command_name=label,
        log_path=log_path,
    )
    if git_head(paths.repo) != primary_head:
        raise AutoresearchError(
            f"{label} moved the primary repository from {primary_head} to "
            f"{git_head(paths.repo)}. Full output: {log_path}"
        )
    return parse_metric_output(result, json_key=run["metric"]["json_key"])


def finish_claimed_candidate(args: argparse.Namespace) -> dict[str, Any]:
    """
    Commit, measure, and admit or discard one claimed candidate.
    Args:
    args: Parsed CLI arguments carrying the repository, candidate, and description.
    Return: The resolution receipt.

    Measurement happens before the admission lock is taken, because measurement is the
    slow step and holding the lock across it would serialize the feature away. Two
    candidates may both measure an improvement and race for the lock; they serialize,
    and the loser finds a moved frontier and takes the stale path.
    """
    repo = Path(args.repo).expanduser().resolve()
    paths, run, events, state = load_context(repo)
    if state.status != "active":
        raise AutoresearchError(f"Cannot finish a candidate while run status is {state.status}")
    require_docs_match(repo, recorded_doc_digests(run, events))
    if args.candidate in {
        event["candidate"] for event in events if event["event"] == "candidate_resolved"
    }:
        raise AutoresearchError(
            f"Candidate {args.candidate} is already resolved. A late finish from a "
            f"reaped or abandoned worker can never admit stale work."
        )
    table = load_slots(paths, run)
    slot = slot_for_candidate(table, args.candidate)
    started = next(
        event
        for event in events
        if event["event"] == "candidate_started" and event["candidate"] == args.candidate
    )
    worktree = Path(slot["worktree"])
    branch = slot["branch"]
    grant = slot["grant"]
    base_commit = started["base_commit"]
    base_metric = parse_decimal(started["base_metric"], field="candidate.base_metric")

    changed = working_paths(worktree)
    if not changed:
        raise AutoresearchError(
            f"Candidate {args.candidate} made no changes in {worktree}. Use abandon "
            f"if there is nothing to try."
        )
    require_paths_in_scope(changed, run["scope"])

    update_slot(paths, run, args.candidate, state="measuring")
    trial_commit = commit_trial(worktree, paths=changed, description=args.description)
    verify_log = next_command_log(paths, args.candidate, "verify")
    trial_metric = measure_in(
        worktree=worktree,
        run=run,
        paths=paths,
        expected_head=trial_commit,
        branch=branch,
        log_path=verify_log,
        label="Metric command",
        grant=grant,
    )

    def resolve(outcome: str, reason: str, *, metric: Any, head: str, guard: str,
                guard_log: Path | None, rebased: str | None = None) -> dict[str, Any]:
        event = append_event(
            paths,
            run,
            events,
            event="candidate_resolved",
            candidate=args.candidate,
            outcome=outcome,
            reason=reason,
            description=" ".join(args.description.split()),
            trial_metric=decimal_json(metric) if metric is not None else None,
            retained_metric=decimal_json(
                trial_metric if outcome == "admitted" else state.metric
            ),
            trial_commit=rebased or trial_commit,
            trial_branch=branch,
            head=head,
            guard=guard,
            verify_log=relative_log_path(paths, verify_log),
            guard_log=relative_log_path(paths, guard_log),
        )
        # Keep the in-memory log in step, or a following terminal event computes its
        # sequence number from a list that is already one event behind the file.
        events.append(event)
        update_slot(
            paths,
            run,
            args.candidate,
            state="idle",
            candidate=None,
            role=None,
            grant=None,
            agent_ref=None,
            claimed_at=None,
            lease_expires_at=None,
        )
        return event

    if not improved(trial_metric, base_metric, run["metric"]["direction"]):
        resolve(
            "discarded",
            "no_improvement",
            metric=trial_metric,
            head=state.head,
            guard="not_run",
            guard_log=None,
        )
        return {
            "status": "active",
            "candidate": args.candidate,
            "outcome": "discarded",
            "reason": "no_improvement",
            "trial_metric": decimal_json(trial_metric),
            "retained_metric": decimal_json(state.metric),
        }

    update_slot(paths, run, args.candidate, state="admitting")
    acquire_admission_lock(paths, run_id=run["run_id"], candidate=args.candidate)
    try:
        paths, run, events, state = load_context(repo)
        frontier, frontier_metric = state.head, state.metric
        admitted_commit = trial_commit
        measured = trial_metric
        rebase_log: Path | None = None

        if base_commit != frontier:
            rebase = run_git(worktree, "rebase", "--onto", frontier, base_commit, branch,
                             check=False)
            if rebase.returncode != 0:
                run_git(worktree, "rebase", "--abort", check=False)
                resolve(
                    "discarded",
                    "rebase_conflict",
                    metric=trial_metric,
                    head=frontier,
                    guard="not_run",
                    guard_log=None,
                )
                return {
                    "status": "active",
                    "candidate": args.candidate,
                    "outcome": "discarded",
                    "reason": "rebase_conflict",
                }
            admitted_commit = git_head(worktree)
            rebase_log = next_command_log(paths, args.candidate, "rebase-verify")
            measured = measure_in(
                worktree=worktree,
                run=run,
                paths=paths,
                expected_head=admitted_commit,
                branch=branch,
                log_path=rebase_log,
                label="Rebase metric command",
                grant=grant,
            )
            if not improved(measured, frontier_metric, run["metric"]["direction"]):
                resolve(
                    "discarded",
                    "no_improvement",
                    metric=measured,
                    head=frontier,
                    guard="not_run",
                    guard_log=None,
                )
                return {
                    "status": "active",
                    "candidate": args.candidate,
                    "outcome": "discarded",
                    "reason": "stale_no_improvement",
                    "trial_metric": decimal_json(measured),
                }

        guard_status = "pass"
        guard_log: Path | None = None
        if run["guard"]:
            guard_log = next_command_log(paths, args.candidate, "guard")
            guard_result = run_command(
                command=run["guard"],
                cwd=worktree,
                timeout_seconds=run["timeout_seconds"],
                log_path=guard_log,
                environment=grant_environment(grant),
            )
            guard_status = "pass" if guard_result.returncode == 0 else "fail"
            if guard_status == "fail":
                resolve(
                    "discarded",
                    "guard_failed",
                    metric=measured,
                    head=frontier,
                    guard="fail",
                    guard_log=guard_log,
                )
                return {
                    "status": "active",
                    "candidate": args.candidate,
                    "outcome": "discarded",
                    "reason": "guard_failed",
                }

        run_git(repo, "merge", "--ff-only", admitted_commit)
        trial_metric = measured
        resolve(
            "admitted",
            "improved",
            metric=measured,
            head=admitted_commit,
            guard=guard_status,
            guard_log=guard_log,
            rebased=admitted_commit,
        )
        status = "active"
        target = parse_decimal(run["target"], field="run.target")
        if target_reached(measured, target, run["metric"]["direction"]):
            events.append(
                append_event(
                    paths, run, events,
                    event="complete",
                    reason="retained metric satisfies the target",
                    head=admitted_commit,
                    metric=decimal_json(measured),
                    unresolved_candidates=unresolved_from(events),
                )
            )
            status = "complete"
        return {
            "status": status,
            "candidate": args.candidate,
            "outcome": "admitted",
            "trial_metric": decimal_json(measured),
            "retained_metric": decimal_json(measured),
            "head": admitted_commit,
        }
    finally:
        release_admission_lock(paths)


def finish_candidate(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo).expanduser().resolve()
    paths, run, events, state = load_context(repo)
    if state.status != "active":
        raise AutoresearchError(f"Cannot finish a candidate while run status is {state.status}")
    if git_branch(repo) != run["branch"]:
        raise AutoresearchError(
            f"Run is pinned to branch {run['branch']}, current branch is {git_branch(repo)}"
        )
    if git_head(repo) != state.head:
        raise AutoresearchError(
            f"Git HEAD changed outside autoresearch: expected {state.head}, got {git_head(repo)}"
        )
    require_no_staged_artifacts(repo)
    require_docs_match(repo, recorded_doc_digests(run, events))
    changed = working_paths(repo)
    if not changed:
        raise AutoresearchError("No experiment changes found; make one focused change before finish")
    require_paths_in_scope(changed, run["scope"])

    candidate = state.iterations + 1
    # Slot 0 means the primary checkout. A finish without a prior claim runs the
    # candidate in place, which is how a host with no concurrent subagent primitive
    # degrades to sequential execution against the identical state model.
    bank = load_bank(repo)
    table = load_slots(paths, run)
    grant = allocate_grant(bank, held_grants(table))
    if grant is None:
        raise AutoresearchError(
            "Compute bank exhausted: " + ", ".join(exhausted_entries(bank, held_grants(table)))
        )
    allocation = run["parallel"]["allocation"]
    role, role_source = choose_role(
        events=events,
        roles=candidate_roles(events),
        live=live_roles(table),
        max_parallel=run["parallel"]["max_parallel_resolved"],
        window=allocation["window"],
        min_per_role=allocation["min_per_role"],
        plateau_k=allocation["plateau_k"],
    )
    digests = recorded_doc_digests(run, events)
    started = append_event(
        paths,
        run,
        events,
        event="candidate_started",
        candidate=candidate,
        base_commit=state.head,
        base_metric=decimal_json(state.metric),
        slot=0,
        role=role,
        role_source=role_source,
        grant=grant,
        branch=candidate_branch(run, candidate),
        lease_expires_at=time.time() + run["parallel"]["lease_seconds"],
        goal_sha256=digests["goal_sha256"],
        decisions_sha256=digests["decisions_sha256"],
    )
    events.append(started)

    trial_commit = commit_trial(repo, paths=changed, description=args.description)
    iteration = candidate
    verify_log = next_command_log(paths, iteration, "verify")
    guard_log: Path | None = None
    control_snapshot = control_state_snapshot(paths)
    try:
        verify_result = run_command(
            command=run["metric"]["command"],
            cwd=repo,
            timeout_seconds=run["timeout_seconds"],
            log_path=verify_log,
        )
        require_control_state_unchanged(
            paths,
            control_snapshot,
            command_name="Metric command",
            log_path=verify_log,
        )
        require_command_preserved_repository(
            repo,
            expected_head=trial_commit,
            expected_branch=run["branch"],
            command_name="Metric command",
            log_path=verify_log,
        )
        trial_metric = parse_metric_output(
            verify_result,
            json_key=run["metric"]["json_key"],
        )
    except AutoresearchError as exc:
        require_control_state_unchanged(
            paths,
            control_snapshot,
            command_name="Metric command",
            log_path=verify_log,
        )
        safely_restore_after_error(
            repo=repo,
            paths=paths,
            run=run,
            events=events,
            state=state,
            candidate=candidate,
            trial_commit=trial_commit,
            reason=str(exc),
            log=verify_log,
        )
        raise

    outcome = "discarded"
    reason = "no_improvement"
    guard_status = "not_run"
    retained_metric = state.metric
    head = trial_commit
    if improved(trial_metric, state.metric, run["metric"]["direction"]):
        if run["guard"]:
            guard_log = next_command_log(paths, iteration, "guard")
            try:
                control_snapshot = control_state_snapshot(paths)
                guard_result = run_command(
                    command=run["guard"],
                    cwd=repo,
                    timeout_seconds=run["timeout_seconds"],
                    log_path=guard_log,
                )
                require_control_state_unchanged(
                    paths,
                    control_snapshot,
                    command_name="Guard command",
                    log_path=guard_log,
                )
                require_command_preserved_repository(
                    repo,
                    expected_head=trial_commit,
                    expected_branch=run["branch"],
                    command_name="Guard command",
                    log_path=guard_log,
                )
            except AutoresearchError as exc:
                require_control_state_unchanged(
                    paths,
                    control_snapshot,
                    command_name="Guard command",
                    log_path=guard_log,
                )
                safely_restore_after_error(
                    repo=repo,
                    paths=paths,
                    run=run,
                    events=events,
                    state=state,
                    candidate=candidate,
                    trial_commit=trial_commit,
                    reason=str(exc),
                    log=guard_log,
                )
                raise
            guard_status = "pass" if guard_result.returncode == 0 else "fail"
        else:
            guard_status = "pass"

        if guard_status == "pass":
            outcome = "admitted"
            reason = "improved"
            retained_metric = trial_metric
        else:
            reason = "guard_failed"

    trial_branch = candidate_branch(run, candidate)
    if outcome == "admitted":
        run_git(repo, "branch", "--force", trial_branch, trial_commit)
    else:
        try:
            preserve_and_reset(
                repo,
                branch=trial_branch,
                trial_commit=trial_commit,
                frontier=state.head,
            )
        except AutoresearchError as exc:
            current_head = git_head(repo)
            append_error(
                paths=paths,
                run=run,
                events=events,
                state=state,
                reason=(
                    f"Experiment should be discarded but rollback failed: {exc}; "
                    f"current HEAD is {current_head}"
                ),
                head=trial_commit,
                trial_commit=trial_commit,
                unresolved_candidates=[candidate],
                log=guard_log or verify_log,
            )
            raise
        head = state.head

    event = append_event(
        paths,
        run,
        events,
        event="candidate_resolved",
        candidate=candidate,
        outcome=outcome,
        reason=reason,
        description=" ".join(args.description.split()),
        trial_metric=decimal_json(trial_metric),
        retained_metric=decimal_json(retained_metric),
        trial_commit=trial_commit,
        trial_branch=trial_branch,
        head=head,
        guard=guard_status,
        verify_log=relative_log_path(paths, verify_log),
        guard_log=relative_log_path(paths, guard_log),
    )
    events.append(event)

    status = "active"
    target = parse_decimal(run["target"], field="run.target")
    if target_reached(retained_metric, target, run["metric"]["direction"]):
        complete = append_event(
            paths,
            run,
            events,
            event="complete",
            reason="retained metric satisfies the target",
            head=head,
            metric=decimal_json(retained_metric),
            unresolved_candidates=[],
        )
        events.append(complete)
        status = "complete"
    elif run["max_candidates"] is not None and iteration >= run["max_candidates"]:
        stopped = append_event(
            paths,
            run,
            events,
            event="stopped",
            reason="configured candidate limit reached",
            head=head,
            metric=decimal_json(retained_metric),
            unresolved_candidates=[],
        )
        events.append(stopped)
        status = "stopped"

    return {
        "status": status,
        "candidate": candidate,
        "outcome": outcome,
        "trial_metric": decimal_json(trial_metric),
        "retained_metric": decimal_json(retained_metric),
        "target": run["target"],
        "head": head,
        "instruction": "Continue with the next distinct experiment unless complete.",
    }


def block_run(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo).expanduser().resolve()
    paths, run, events, state = load_context(repo)
    reason = args.reason.strip()
    if not reason:
        raise AutoresearchError("--reason cannot be empty")
    if state.status != "active":
        raise AutoresearchError(f"Cannot block a run whose status is {state.status}")
    require_clean_repo(repo, expected_head=state.head, expected_branch=run["branch"])
    event = append_event(
        paths,
        run,
        events,
        event="blocked",
        reason=reason,
        head=state.head,
        metric=decimal_json(state.metric),
        unresolved_candidates=list(state.unresolved),
    )
    return {"status": "blocked", "reason": event["reason"]}


def parallel_status(repo: Path, paths: Paths, run: dict[str, Any], state: RunState) -> dict[str, Any]:
    """
    Summarize slots, leases, and bank utilization for status output.
    Args:
    repo: Repository root.
    paths: Resolved run paths.
    run: Validated run configuration.
    state: Replayed run state.
    Return: The parallel section of the status payload.
    """
    table = load_slots(paths, run)
    bank = load_bank(repo)
    now = time.time()
    held = held_grants(table)
    return {
        "max_parallel": run["parallel"]["max_parallel_resolved"],
        "bank_capacity": bank_capacity(bank),
        "grants_held": len(held),
        "live_by_role": live_roles(table),
        "unresolved_candidates": list(state.unresolved),
        "slots": [
            {
                "slot": slot["slot"],
                "state": slot["state"],
                "candidate": slot["candidate"],
                "role": slot["role"],
                "grant": slot["grant"],
                "agent_ref": slot["agent_ref"],
                "lease_expired": lease_expired(slot, now=now),
            }
            for slot in table["slots"]
        ],
    }


def show_status(args: argparse.Namespace) -> dict[str, Any]:
    paths = paths_for(args.repo)
    require_git_repo(paths.repo)
    if not paths.run.is_file():
        if not paths.root.exists():
            return {"status": "not_initialized", "repo": str(paths.repo)}
        current = sorted(
            child.name for child in paths.root.iterdir() if child.name != "archive"
        )
        if not current:
            return {"status": "not_initialized", "repo": str(paths.repo)}
        diagnostic_path = paths.root / "init-error.json"
        allowed = {"init-error.json", "logs"}
        unexpected = sorted(set(current) - allowed)
        if unexpected:
            raise AutoresearchError(
                f"Incomplete autoresearch initialization has unexpected files at {paths.root}: "
                + ", ".join(unexpected)
            )
        if not diagnostic_path.is_file():
            raise AutoresearchError(
                f"Incomplete autoresearch initialization has no run.json or init-error.json at {paths.root}"
            )
        try:
            diagnostic = parse_json(
                diagnostic_path.read_text(encoding="utf-8"),
                source=str(diagnostic_path),
            )
        except OSError as exc:
            raise AutoresearchError(f"Cannot read {diagnostic_path}: {exc}") from exc
        if not isinstance(diagnostic, dict):
            raise AutoresearchError(f"{diagnostic_path} must contain a JSON object")
        require_exact_keys(
            diagnostic,
            required={"time", "error_type", "message", "traceback"},
            source=str(diagnostic_path),
        )
        if any(not isinstance(diagnostic[key], str) for key in diagnostic):
            raise AutoresearchError(f"{diagnostic_path} fields must all be strings")
        return {
            "status": "initialization_failed",
            "repo": str(paths.repo),
            "error_type": diagnostic["error_type"],
            "message": diagnostic["message"],
            "diagnostic": str(diagnostic_path),
            "logs": str(paths.logs),
        }
    paths, run, events, state = load_context(args.repo)
    payload = status_payload(paths, run, events, state)
    payload["parallel"] = parallel_status(paths.repo, paths, run, state)
    payload["docs"] = recorded_doc_digests(run, events)
    current_head = git_head(paths.repo)
    branch_error: str | None = None
    try:
        current_branch = git_branch(paths.repo)
    except AutoresearchError as exc:
        current_branch = None
        branch_error = str(exc)
    dirty = working_paths(paths.repo)
    payload["repository"] = {
        "expected_head": state.head,
        "current_head": current_head,
        "expected_branch": run["branch"],
        "current_branch": current_branch,
        "branch_error": branch_error,
        "dirty_paths": dirty,
        "consistent": (
            current_head == state.head
            and current_branch == run["branch"]
            and not dirty
        ),
    }
    return payload


def show_history(args: argparse.Namespace) -> str:
    _, run, events, state = load_context(Path(args.repo).expanduser().resolve())
    if args.format == "tsv":
        return render_history_tsv(events)
    return render_history_table(run, state, events)


def generate_report(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo).expanduser().resolve()
    paths, run, events, state = load_context(repo)
    output = paths.root / "report.html"
    try:
        write_text_atomic(output, render_html_report(run, state, events))
    except OSError as exc:
        raise AutoresearchError(f"Cannot write HTML report {output}: {exc}") from exc
    return {
        "status": state.status,
        "report": str(output),
        "event_count": len(events),
        "iterations": state.iterations,
    }


def resume_run(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo).expanduser().resolve()
    paths, run, events, state = load_context(repo)
    note = args.note.strip()
    if not note:
        raise AutoresearchError("--note cannot be empty")
    if state.status == "complete":
        raise AutoresearchError("A completed run cannot be resumed; archive it and start a new goal")
    if run["max_candidates"] is not None and state.iterations >= run["max_candidates"]:
        raise AutoresearchError(
            "The configured candidate limit has been reached; archive this run and confirm a new limit"
        )
    if (
        state.status == "error"
        and state.last_event.get("trial_commit") is not None
        and state.last_event.get("head") == state.last_event.get("trial_commit")
    ):
        raise AutoresearchError(
            "The failed trial commit was not rolled back. Inspect the recorded command log and Git state, "
            "then archive this run after manual recovery; it cannot be resumed from an unverified commit."
        )
    if state.status == "active":
        raise AutoresearchError("Foreground run is already active")
    require_clean_repo(repo, expected_head=state.head, expected_branch=run["branch"])
    event = append_event(
        paths,
        run,
        events,
        event="resumed",
        note=note,
        head=state.head,
        metric=decimal_json(state.metric),
    )
    return {
        "status": "active",
        "note": event["note"],
        "instruction": "Reuse or create the matching Codex Goal, then continue the experiment loop.",
    }


def candidate_roles(events: list[dict[str, Any]]) -> dict[int, str]:
    """
    Map every started candidate to the role it was claimed under.
    Args:
    events: Full validated event list.
    Return: Candidate id to role.
    """
    return {
        event["candidate"]: event["role"]
        for event in events
        if event["event"] == "candidate_started"
    }


def slot_for_candidate(table: dict[str, Any], candidate: int) -> dict[str, Any]:
    """
    Find the slot holding one live candidate.
    Args:
    table: Slot table.
    candidate: Candidate identifier.
    Return: The owning slot record.
    """
    for slot in table["slots"]:
        if slot["candidate"] == candidate:
            return slot
    raise AutoresearchError(
        f"Candidate {candidate} does not hold a slot. It was never claimed, or it "
        f"was already resolved."
    )


def release_slot(table: dict[str, Any], slot: dict[str, Any]) -> None:
    """
    Return a slot to the idle pool and release its grant.
    Args:
    table: Slot table.
    slot: Slot to release.
    Return: None.
    """
    slot.update(
        state="idle",
        candidate=None,
        role=None,
        grant=None,
        agent_ref=None,
        claimed_at=None,
        lease_expires_at=None,
    )


def claim_candidates(args: argparse.Namespace) -> dict[str, Any]:
    """
    Claim up to --count slots and emit one worker packet for each.
    Args:
    args: Parsed CLI arguments carrying the repository, count, and optional role override.
    Return: Packets for the claimed candidates, and why any request went unfilled.
    """
    if args.role and not args.role_reason:
        raise AutoresearchError("--role requires --role-reason so the override is auditable")
    if args.count <= 0:
        raise AutoresearchError("--count must be a positive integer")
    repo = Path(args.repo).expanduser().resolve()
    paths, run, events, state = load_context(repo)
    if state.status != "active":
        raise AutoresearchError(f"Cannot claim while run status is {state.status}")
    require_docs_match(repo, recorded_doc_digests(run, events))

    bank = load_bank(repo)
    table = load_slots(paths, run)
    parallel = run["parallel"]
    allocation = parallel["allocation"]
    goal_text = read_doc(repo, GOAL_FILE, required=True)
    decisions_text = read_doc(repo, DECISIONS_FILE, required=False)
    digests = recorded_doc_digests(run, events)

    packets: list[dict[str, Any]] = []
    unfilled: str | None = None
    for _ in range(args.count):
        slot = free_slot(table)
        if slot is None:
            unfilled = "every slot is busy or broken"
            break
        grant = allocate_grant(bank, held_grants(table))
        if grant is None:
            unfilled = "compute bank exhausted: " + ", ".join(
                exhausted_entries(bank, held_grants(table))
            )
            break
        if args.role:
            role, role_source = args.role, "override"
        else:
            role, role_source = choose_role(
                events=events,
                roles=candidate_roles(events),
                live=live_roles(table),
                max_parallel=parallel["max_parallel_resolved"],
                window=allocation["window"],
                min_per_role=allocation["min_per_role"],
                plateau_k=allocation["plateau_k"],
            )
        # events already grew for each candidate claimed in this loop, so the highest
        # started id is authoritative on its own. Adding len(packets) double-counts.
        candidate = max([0, *candidate_roles(events)]) + 1
        branch = candidate_branch(run, candidate)
        slot["state"] = "preparing"
        save_slots(paths, table)
        prepare_worktree(
            repo,
            slot,
            branch=branch,
            frontier=state.head,
            prepare=parallel["prepare"],
            timeout_seconds=run["timeout_seconds"],
            log_path=next_command_log(paths, candidate, "prepare"),
        )
        expires = time.time() + parallel["lease_seconds"]
        started = append_event(
            paths,
            run,
            events,
            event="candidate_started",
            candidate=candidate,
            base_commit=state.head,
            base_metric=decimal_json(state.metric),
            slot=slot["slot"],
            role=role,
            role_source=role_source,
            grant=grant,
            branch=branch,
            lease_expires_at=expires,
            goal_sha256=digests["goal_sha256"],
            decisions_sha256=digests["decisions_sha256"],
        )
        events.append(started)
        slot.update(
            state="live",
            branch=branch,
            candidate=candidate,
            role=role,
            grant=grant,
            claimed_at=utc_now(),
            lease_expires_at=expires,
        )
        save_slots(paths, table)
        packets.append(
            {
                "candidate": candidate,
                "slot": slot["slot"],
                "role": role,
                "role_source": role_source,
                "role_reason": args.role_reason,
                "grant": grant,
                "worktree": slot["worktree"],
                "branch": branch,
                "lease_expires_at": expires,
                "packet": build_packet(
                    control=Path(__file__).resolve(),
                    run=run,
                    events=events,
                    candidate=candidate,
                    slot=slot,
                    role=role,
                    grant=grant,
                    goal_text=goal_text,
                    decisions_text=decisions_text,
                    base_metric=str(decimal_json(state.metric)),
                    window=allocation["window"],
                ),
            }
        )
    return {
        "status": state.status,
        "claimed": len(packets),
        "requested": args.count,
        "unfilled_reason": unfilled,
        "candidates": packets,
        "instruction": (
            "Spawn one subagent per packet using your host's concurrent subagent "
            "primitive, then record each agent id with bind."
        ),
    }


def bind_agent(args: argparse.Namespace) -> dict[str, Any]:
    """
    Record the host-assigned agent id for a claimed candidate.
    Args:
    args: Parsed CLI arguments carrying the repository, candidate, and agent reference.
    Return: Confirmation of the recorded reference.
    """
    repo = Path(args.repo).expanduser().resolve()
    paths, run, events, state = load_context(repo)
    table = load_slots(paths, run)
    slot = slot_for_candidate(table, args.candidate)
    slot["agent_ref"] = args.agent_ref
    save_slots(paths, table)
    return {
        "candidate": args.candidate,
        "agent_ref": args.agent_ref,
        "note": (
            "Advisory only. The control plane cannot verify a host-assigned id, so "
            "the lease remains the authority on liveness."
        ),
    }


def extend_lease(args: argparse.Namespace) -> dict[str, Any]:
    """
    Push out a candidate's lease deadline.
    Args:
    args: Parsed CLI arguments carrying the repository and candidate.
    Return: The new deadline.
    """
    repo = Path(args.repo).expanduser().resolve()
    paths, run, events, state = load_context(repo)
    table = load_slots(paths, run)
    slot = slot_for_candidate(table, args.candidate)
    expires = time.time() + run["parallel"]["lease_seconds"]
    slot["lease_expires_at"] = expires
    save_slots(paths, table)
    return {"candidate": args.candidate, "lease_expires_at": expires}


def abandon_candidate(args: argparse.Namespace) -> dict[str, Any]:
    """
    Resolve a claimed candidate as failed without touching the frontier.
    Args:
    args: Parsed CLI arguments carrying the repository, candidate, and reason.
    Return: The resolution receipt.
    """
    repo = Path(args.repo).expanduser().resolve()
    paths, run, events, state = load_context(repo)
    table = load_slots(paths, run)
    slot = slot_for_candidate(table, args.candidate)
    event = append_event(
        paths,
        run,
        events,
        event="candidate_resolved",
        candidate=args.candidate,
        outcome="failed",
        reason="abandoned",
        description=" ".join(args.reason.split()),
        trial_metric=None,
        retained_metric=decimal_json(state.metric),
        trial_commit=None,
        trial_branch=slot["branch"],
        head=state.head,
        guard="not_run",
        verify_log=None,
        guard_log=None,
    )
    release_slot(table, slot)
    save_slots(paths, table)
    return {"candidate": args.candidate, "outcome": "failed", "reason": event["reason"]}


def reap_candidate(args: argparse.Namespace) -> dict[str, Any]:
    """
    Resolve a candidate whose lease lapsed, freeing its slot.
    Args:
    args: Parsed CLI arguments carrying the repository and candidate.
    Return: The resolution receipt.

    Reaping is always explicit. An expired lease is reported by status but never acted
    on automatically, because the control plane cannot see worker processes and a slow
    worker is indistinguishable from a dead one.
    """
    repo = Path(args.repo).expanduser().resolve()
    paths, run, events, state = load_context(repo)
    table = load_slots(paths, run)
    slot = slot_for_candidate(table, args.candidate)
    if not lease_expired(slot, now=time.time()):
        raise AutoresearchError(
            f"Candidate {args.candidate} still holds a valid lease until "
            f"{slot['lease_expires_at']}. Wait for it, or have the worker abandon it."
        )
    append_event(
        paths,
        run,
        events,
        event="candidate_resolved",
        candidate=args.candidate,
        outcome="failed",
        reason="lease_expired",
        description=f"lease expired while held by {slot['agent_ref'] or 'an unbound agent'}",
        trial_metric=None,
        retained_metric=decimal_json(state.metric),
        trial_commit=None,
        trial_branch=slot["branch"],
        head=state.head,
        guard="not_run",
        verify_log=None,
        guard_log=None,
    )
    release_slot(table, slot)
    save_slots(paths, table)
    return {"candidate": args.candidate, "outcome": "failed", "reason": "lease_expired"}


def reconcile_run(args: argparse.Namespace) -> dict[str, Any]:
    """
    Report recoverable problems and clear a stale admission lock.
    Args:
    args: Parsed CLI arguments carrying the repository.
    Return: What was found and what was cleared.

    Nothing is silently repaired. Reapable candidates and broken slots are reported for
    an explicit decision; only a lock whose holder is provably gone is cleared here.
    """
    repo = Path(args.repo).expanduser().resolve()
    paths, run, events, state = load_context(repo)
    table = load_slots(paths, run)
    now = time.time()

    reapable: list[int] = []
    broken: list[int] = []
    for slot in table["slots"]:
        if lease_expired(slot, now=now):
            reapable.append(slot["candidate"])
        worktree = Path(slot["worktree"])
        if slot["candidate"] is not None and not worktree.exists():
            slot["state"] = "broken"
            broken.append(slot["slot"])

    cleared_lock = None
    lock = read_admission_lock(paths)
    if lock is not None and not process_alive(lock["pid"]):
        release_admission_lock(paths)
        cleared_lock = lock

    divergence = sorted(
        set(state.unresolved)
        ^ {slot["candidate"] for slot in table["slots"] if slot["candidate"] is not None}
    )
    save_slots(paths, table)
    return {
        "status": state.status,
        "reapable_candidates": reapable,
        "broken_slots": broken,
        "cleared_stale_lock": cleared_lock,
        "slots_events_divergence": divergence,
        "instruction": (
            "Events are authoritative. Reap expired candidates explicitly; a broken "
            "slot is never reused."
        ),
    }


def report_compute(args: argparse.Namespace) -> dict[str, Any]:
    """
    Report observed compute capacity and the declared bank, writing nothing.
    Args:
    args: Parsed CLI arguments carrying the repository.
    Return: Observations, plus the declared bank when one exists.
    """
    repo = Path(args.repo).expanduser().resolve()
    payload: dict[str, Any] = {
        "observed": detect_local_capacity(),
        "bank_path": str(compute_path(repo)),
        "instruction": (
            "Write the bank explicitly from these observations. Nothing is inferred, "
            "and this command never writes."
        ),
    }
    if compute_path(repo).exists():
        bank = load_bank(repo)
        payload["declared"] = bank
        payload["declared_capacity"] = bank_capacity(bank)
    else:
        payload["declared"] = None
        payload["declared_capacity"] = None
    return payload


def record_decision(args: argparse.Namespace) -> dict[str, Any]:
    """
    Append one curated decision and record it in the event log.
    Args:
    args: Parsed CLI arguments carrying the repository and the note to add.
    Return: Receipt naming the note and the new decisions digest.
    """
    repo = Path(args.repo).expanduser().resolve()
    paths, run, events, state = load_context(repo)
    if state.status not in {"active", "blocked", "stopped"}:
        raise AutoresearchError(f"Cannot record a decision while run status is {state.status}")
    note, digest = append_decision(repo, paths, args.add)
    event = append_event(
        paths,
        run,
        events,
        event="decision",
        note=note,
        decisions_sha256=digest,
    )
    return {
        "status": state.status,
        "note": event["note"],
        "decisions_sha256": digest,
        "instruction": "Future candidates receive this note in their worker packet.",
    }


def archive_run(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo).expanduser().resolve()
    paths = paths_for(repo)
    if not paths.root.exists():
        raise AutoresearchError(f"No autoresearch results directory at {paths.root}")
    archive_root = paths.root / "archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    archive_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    destination = archive_root / archive_id
    if destination.exists():
        raise AutoresearchError(f"Archive destination already exists: {destination}")
    destination.mkdir()
    moved: list[str] = []
    for child in sorted(paths.root.iterdir(), key=lambda item: item.name):
        if child == archive_root:
            continue
        shutil.move(str(child), destination / child.name)
        moved.append(child.name)
    if not moved:
        destination.rmdir()
        raise AutoresearchError(f"No current run files to archive in {paths.root}")
    return {"status": "archived", "destination": str(destination), "moved": moved}


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "init":
        output = initialize_run(args)
    elif args.command == "finish":
        output = (
            finish_claimed_candidate(args) if args.candidate else finish_candidate(args)
        )
    elif args.command == "block":
        output = block_run(args)
    elif args.command == "status":
        output = show_status(args)
    elif args.command == "history":
        print(show_history(args), end="")
        return 0
    elif args.command == "report":
        output = generate_report(args)
    elif args.command == "resume":
        output = resume_run(args)
    elif args.command == "claim":
        output = claim_candidates(args)
    elif args.command == "bind":
        output = bind_agent(args)
    elif args.command == "heartbeat":
        output = extend_lease(args)
    elif args.command == "abandon":
        output = abandon_candidate(args)
    elif args.command == "reap":
        output = reap_candidate(args)
    elif args.command == "reconcile":
        output = reconcile_run(args)
    elif args.command == "compute":
        output = report_compute(args)
    elif args.command == "decide":
        output = record_decision(args)
    elif args.command == "archive":
        output = archive_run(args)
    else:
        raise AutoresearchError(f"Unsupported command: {args.command}")
    print_json(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AutoresearchError as exc:
        raise SystemExit(f"error: {exc}")
