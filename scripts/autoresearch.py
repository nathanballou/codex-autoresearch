#!/usr/bin/env python3
"""Command line control plane for codex-autoresearch."""

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
    SCHEMA_VERSION,
    append_event,
    commit_trial,
    decimal_json,
    git_branch,
    git_head,
    improved,
    json_text,
    load_context,
    next_command_log,
    normalize_scopes,
    parse_decimal,
    parse_json,
    parse_metric_output,
    paths_for,
    relative_log_path,
    require_clean_repo,
    require_artifacts_untracked,
    require_git_repo,
    require_git_identity,
    require_no_staged_artifacts,
    require_exact_keys,
    require_paths_in_scope,
    revert_trial,
    run_command,
    status_payload,
    target_reached,
    utc_now,
    validate_run,
    working_paths,
    write_json_atomic,
    write_text_atomic,
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

    archive_parser = subparsers.add_parser(
        "archive", help="Archive the current run before starting a different one."
    )
    add_repo_argument(archive_parser)

    return parser


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
    revert_commit: str | None = None,
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
        revert_commit=revert_commit,
        log=relative_log_path(paths, log),
    )


def safely_revert_after_error(
    *,
    repo: Path,
    paths: Paths,
    run: dict[str, Any],
    events: list[dict[str, Any]],
    state: RunState,
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
            log=log,
        )
        return
    try:
        revert_commit = revert_trial(repo, trial_commit)
    except AutoresearchError as revert_error:
        current_head = git_head(repo)
        append_error(
            paths=paths,
            run=run,
            events=events,
            state=state,
            reason=(
                f"{reason}; rollback failed: {revert_error}; "
                f"current HEAD is {current_head}"
            ),
            head=trial_commit,
            trial_commit=trial_commit,
            log=log,
        )
        return
    append_error(
        paths=paths,
        run=run,
        events=events,
        state=state,
        reason=reason,
        head=revert_commit,
        trial_commit=trial_commit,
        revert_commit=revert_commit,
        log=log,
    )


def finish_iteration(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo).expanduser().resolve()
    paths, run, events, state = load_context(repo)
    if state.status != "active":
        raise AutoresearchError(f"Cannot finish an iteration while run status is {state.status}")
    if git_branch(repo) != run["branch"]:
        raise AutoresearchError(
            f"Run is pinned to branch {run['branch']}, current branch is {git_branch(repo)}"
        )
    if git_head(repo) != state.head:
        raise AutoresearchError(
            f"Git HEAD changed outside autoresearch: expected {state.head}, got {git_head(repo)}"
        )
    require_no_staged_artifacts(repo)
    changed = working_paths(repo)
    if not changed:
        raise AutoresearchError("No experiment changes found; make one focused change before finish")
    require_paths_in_scope(changed, run["scope"])

    trial_commit = commit_trial(repo, paths=changed, description=args.description)
    iteration = state.iterations + 1
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
        safely_revert_after_error(
            repo=repo,
            paths=paths,
            run=run,
            events=events,
            state=state,
            trial_commit=trial_commit,
            reason=str(exc),
            log=verify_log,
        )
        raise

    outcome = "discard"
    guard_status = "not_run"
    retained_metric = state.metric
    revert_commit: str | None = None
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
                safely_revert_after_error(
                    repo=repo,
                    paths=paths,
                    run=run,
                    events=events,
                    state=state,
                    trial_commit=trial_commit,
                    reason=str(exc),
                    log=guard_log,
                )
                raise
            guard_status = "pass" if guard_result.returncode == 0 else "fail"
        else:
            guard_status = "pass"

        if guard_status == "pass":
            outcome = "keep"
            retained_metric = trial_metric

    if outcome == "discard":
        try:
            revert_commit = revert_trial(repo, trial_commit)
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
                log=guard_log or verify_log,
            )
            raise
        head = revert_commit

    event = append_event(
        paths,
        run,
        events,
        event="iteration",
        iteration=iteration,
        outcome=outcome,
        description=" ".join(args.description.split()),
        previous_metric=decimal_json(state.metric),
        trial_metric=decimal_json(trial_metric),
        retained_metric=decimal_json(retained_metric),
        trial_commit=trial_commit,
        head=head,
        revert_commit=revert_commit,
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
        )
        events.append(stopped)
        status = "stopped"

    return {
        "status": status,
        "iteration": iteration,
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
    )
    return {"status": "blocked", "reason": event["reason"]}


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
        and state.last_event.get("revert_commit") is None
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
        output = finish_iteration(args)
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
