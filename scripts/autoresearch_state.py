#!/usr/bin/env python3
"""Strict run and event schema validation, and authoritative state replay."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autoresearch_core import (
    AutoresearchError,
    Paths,
    RunState,
    append_json_line,
    decimal_json,
    improved,
    normalize_scopes,
    parse_decimal,
    parse_json,
    paths_for,
    require_exact_keys,
    target_reached,
    utc_now,
)


SCHEMA_VERSION = 2
TERMINAL_EVENTS = {"blocked", "complete", "error", "stopped"}


RUN_KEYS = {
    "schema_version",
    "run_id",
    "created_at",
    "repo",
    "branch",
    "goal",
    "scope",
    "metric",
    "guard",
    "target",
    "max_candidates",
    "timeout_seconds",
}
METRIC_KEYS = {"name", "direction", "command", "json_key"}


def validate_run(payload: Any, *, source: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AutoresearchError(f"{source} must contain a JSON object")
    require_exact_keys(payload, required=RUN_KEYS, source=source)
    if payload["schema_version"] != SCHEMA_VERSION:
        raise AutoresearchError(
            f"Unsupported run schema {payload['schema_version']!r} in {source}; "
            f"expected {SCHEMA_VERSION}. Archive the old run and start a new one."
        )
    for key in ("run_id", "created_at", "repo", "branch", "goal"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise AutoresearchError(f"{source}.{key} must be a non-empty string")
    if not isinstance(payload["scope"], list) or not payload["scope"]:
        raise AutoresearchError(f"{source}.scope must be a non-empty string array")
    if any(not isinstance(item, str) or not item for item in payload["scope"]):
        raise AutoresearchError(f"{source}.scope contains an invalid path")
    metric = payload["metric"]
    if not isinstance(metric, dict):
        raise AutoresearchError(f"{source}.metric must be an object")
    require_exact_keys(metric, required=METRIC_KEYS, source=f"{source}.metric")
    if metric["direction"] not in {"lower", "higher"}:
        raise AutoresearchError(f"{source}.metric.direction must be lower or higher")
    for key in ("name", "command"):
        if not isinstance(metric[key], str) or not metric[key].strip():
            raise AutoresearchError(f"{source}.metric.{key} must be a non-empty string")
    if metric["json_key"] is not None and (
        not isinstance(metric["json_key"], str) or not metric["json_key"].strip()
    ):
        raise AutoresearchError(f"{source}.metric.json_key must be null or a non-empty string")
    if payload["guard"] is not None and (
        not isinstance(payload["guard"], str) or not payload["guard"].strip()
    ):
        raise AutoresearchError(f"{source}.guard must be null or a non-empty string")
    parse_decimal(payload["target"], field=f"{source}.target")
    if payload["max_candidates"] is not None and (
        not isinstance(payload["max_candidates"], int)
        or isinstance(payload["max_candidates"], bool)
        or payload["max_candidates"] <= 0
    ):
        raise AutoresearchError(f"{source}.max_candidates must be null or a positive integer")
    if (
        not isinstance(payload["timeout_seconds"], int)
        or isinstance(payload["timeout_seconds"], bool)
        or payload["timeout_seconds"] <= 0
    ):
        raise AutoresearchError(f"{source}.timeout_seconds must be a positive integer")
    return payload


EVENT_COMMON = {"schema_version", "run_id", "seq", "time", "event"}
EVENT_FIELDS = {
    "baseline": {"head", "metric", "verify_log", "guard_log"},
    "iteration": {
        "iteration",
        "outcome",
        "description",
        "previous_metric",
        "trial_metric",
        "retained_metric",
        "trial_commit",
        "head",
        "revert_commit",
        "guard",
        "verify_log",
        "guard_log",
    },
    "blocked": {"reason", "head", "metric"},
    "complete": {"reason", "head", "metric"},
    "error": {"reason", "head", "metric", "trial_commit", "revert_commit", "log"},
    "resumed": {"note", "head", "metric"},
    "stopped": {"reason", "head", "metric"},
}


def validate_event(payload: Any, *, run_id: str, expected_seq: int, source: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AutoresearchError(f"{source} must contain a JSON object")
    event_type = payload.get("event")
    if event_type not in EVENT_FIELDS:
        raise AutoresearchError(f"{source}.event has unsupported value {event_type!r}")
    require_exact_keys(
        payload,
        required=EVENT_COMMON | EVENT_FIELDS[event_type],
        source=source,
    )
    if payload["schema_version"] != SCHEMA_VERSION:
        raise AutoresearchError(f"{source}.schema_version must be {SCHEMA_VERSION}")
    if payload["run_id"] != run_id:
        raise AutoresearchError(f"{source}.run_id does not match run.json")
    if payload["seq"] != expected_seq:
        raise AutoresearchError(f"{source}.seq must be {expected_seq}, got {payload['seq']!r}")
    if not isinstance(payload["time"], str) or not payload["time"]:
        raise AutoresearchError(f"{source}.time must be a non-empty string")
    for key in ("head",):
        if key in payload and (not isinstance(payload[key], str) or not payload[key]):
            raise AutoresearchError(f"{source}.{key} must be a non-empty string")
    for key in ("metric", "previous_metric", "trial_metric", "retained_metric"):
        if key in payload:
            parse_decimal(payload[key], field=f"{source}.{key}")
    if event_type == "baseline":
        if not isinstance(payload["verify_log"], str) or not payload["verify_log"]:
            raise AutoresearchError(f"{source}.verify_log must be a non-empty string")
        if payload["guard_log"] is not None and (
            not isinstance(payload["guard_log"], str) or not payload["guard_log"]
        ):
            raise AutoresearchError(f"{source}.guard_log must be null or a non-empty string")
    if event_type == "iteration":
        if payload["outcome"] not in {"keep", "discard"}:
            raise AutoresearchError(f"{source}.outcome must be keep or discard")
        if payload["guard"] not in {"pass", "fail", "not_run"}:
            raise AutoresearchError(f"{source}.guard is invalid")
        if not isinstance(payload["iteration"], int) or payload["iteration"] <= 0:
            raise AutoresearchError(f"{source}.iteration must be a positive integer")
        for key in ("description", "trial_commit", "verify_log"):
            if not isinstance(payload[key], str) or not payload[key]:
                raise AutoresearchError(f"{source}.{key} must be a non-empty string")
        for key in ("revert_commit", "guard_log"):
            if payload[key] is not None and (not isinstance(payload[key], str) or not payload[key]):
                raise AutoresearchError(f"{source}.{key} must be null or a non-empty string")
    if event_type in {"blocked", "complete", "error", "stopped"}:
        if not isinstance(payload["reason"], str) or not payload["reason"].strip():
            raise AutoresearchError(f"{source}.reason must be a non-empty string")
    if event_type == "error":
        for key in ("trial_commit", "revert_commit", "log"):
            if payload[key] is not None and (not isinstance(payload[key], str) or not payload[key]):
                raise AutoresearchError(f"{source}.{key} must be null or a non-empty string")
        if payload["trial_commit"] is None and payload["revert_commit"] is not None:
            raise AutoresearchError(f"{source}.revert_commit requires trial_commit")
        if payload["revert_commit"] is not None and payload["head"] != payload["revert_commit"]:
            raise AutoresearchError(f"{source}.head must equal revert_commit after rollback")
    if event_type == "resumed" and (
        not isinstance(payload["note"], str) or not payload["note"].strip()
    ):
        raise AutoresearchError(f"{source}.note must be a non-empty string")
    return payload


def load_run(repo: Path | str) -> tuple[Paths, dict[str, Any]]:
    paths = paths_for(repo)
    if not paths.run.is_file():
        raise AutoresearchError(f"No autoresearch run at {paths.run}")
    try:
        text = paths.run.read_text(encoding="utf-8")
    except OSError as exc:
        raise AutoresearchError(f"Cannot read {paths.run}: {exc}") from exc
    payload = validate_run(parse_json(text, source=str(paths.run)), source=str(paths.run))
    if Path(payload["repo"]).resolve() != paths.repo:
        raise AutoresearchError(
            f"run.json belongs to {payload['repo']}, not requested repo {paths.repo}"
        )
    if normalize_scopes(paths.repo, payload["scope"]) != payload["scope"]:
        raise AutoresearchError(f"{paths.run}.scope is not in canonical repository-relative form")
    return paths, payload


def load_events(paths: Paths, run: dict[str, Any]) -> list[dict[str, Any]]:
    if not paths.events.is_file():
        raise AutoresearchError(f"Missing event source of truth: {paths.events}")
    try:
        text = paths.events.read_text(encoding="utf-8")
    except OSError as exc:
        raise AutoresearchError(f"Cannot read {paths.events}: {exc}") from exc
    if not text:
        raise AutoresearchError(f"Event log is empty: {paths.events}")
    if not text.endswith("\n"):
        raise AutoresearchError(f"Event log has a partial final record: {paths.events}")
    events: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            raise AutoresearchError(f"Blank event record at {paths.events}:{index + 1}")
        payload = parse_json(line, source=f"{paths.events}:{index + 1}")
        events.append(
            validate_event(
                payload,
                run_id=run["run_id"],
                expected_seq=index,
                source=f"{paths.events}:{index + 1}",
            )
        )
    return events


def derive_state(run: dict[str, Any], events: list[dict[str, Any]]) -> RunState:
    if not events or events[0]["event"] != "baseline":
        raise AutoresearchError("events.jsonl must begin with exactly one baseline event")
    metric = parse_decimal(events[0]["metric"], field="baseline.metric")
    head = events[0]["head"]
    if run["guard"] is None and events[0]["guard_log"] is not None:
        raise AutoresearchError("Baseline has a guard log but no configured guard")
    if run["guard"] is not None and events[0]["guard_log"] is None:
        raise AutoresearchError("Baseline is missing its configured guard log")
    iterations = 0
    status = "active"
    last_terminal: str | None = None
    last_terminal_event: dict[str, Any] | None = None

    for event in events[1:]:
        event_type = event["event"]
        if last_terminal is not None:
            if event_type != "resumed" or last_terminal == "complete":
                raise AutoresearchError(
                    f"Invalid event transition: {event_type} follows terminal event {last_terminal}"
                )
            if (
                last_terminal == "error"
                and last_terminal_event is not None
                and last_terminal_event["trial_commit"] is not None
                and last_terminal_event["revert_commit"] is None
            ):
                raise AutoresearchError("Cannot resume an error with an unreverted trial commit")
            status = "active"
            last_terminal = None
            last_terminal_event = None
        elif event_type == "resumed":
            raise AutoresearchError("resumed event requires a preceding blocked, error, or stopped event")

        if event_type == "iteration":
            iterations += 1
            if event["iteration"] != iterations:
                raise AutoresearchError(
                    f"Iteration number must be {iterations}, got {event['iteration']}"
                )
            previous_metric = parse_decimal(
                event["previous_metric"], field="iteration.previous_metric"
            )
            trial_metric = parse_decimal(event["trial_metric"], field="iteration.trial_metric")
            retained_metric = parse_decimal(
                event["retained_metric"], field="iteration.retained_metric"
            )
            if previous_metric != metric:
                raise AutoresearchError(
                    f"Iteration {iterations} previous_metric does not match retained state"
                )
            trial_improved = improved(trial_metric, metric, run["metric"]["direction"])
            if event["outcome"] == "keep":
                if not trial_improved:
                    raise AutoresearchError(
                        f"Iteration {iterations} keeps a metric that did not improve"
                    )
                if retained_metric != trial_metric:
                    raise AutoresearchError(
                        f"Iteration {iterations} keep must retain the trial metric"
                    )
                if event["guard"] != "pass":
                    raise AutoresearchError(f"Iteration {iterations} keep requires a passing guard")
                if event["revert_commit"] is not None or event["head"] != event["trial_commit"]:
                    raise AutoresearchError(
                        f"Iteration {iterations} keep has invalid commit provenance"
                    )
                if run["guard"] is None and event["guard_log"] is not None:
                    raise AutoresearchError(
                        f"Iteration {iterations} has a guard log but no configured guard"
                    )
                if run["guard"] is not None and event["guard_log"] is None:
                    raise AutoresearchError(
                        f"Iteration {iterations} is missing its configured guard log"
                    )
            else:
                if retained_metric != metric:
                    raise AutoresearchError(
                        f"Iteration {iterations} discard changed the retained metric"
                    )
                if event["revert_commit"] is None or event["head"] != event["revert_commit"]:
                    raise AutoresearchError(
                        f"Iteration {iterations} discard must point at its revert commit"
                    )
                if trial_improved:
                    if run["guard"] is None or event["guard"] != "fail" or event["guard_log"] is None:
                        raise AutoresearchError(
                            f"Iteration {iterations} discarded an improvement without a failed guard"
                        )
                elif event["guard"] != "not_run" or event["guard_log"] is not None:
                    raise AutoresearchError(
                        f"Iteration {iterations} ran a guard for a non-improving trial"
                    )
            metric = retained_metric
            head = event["head"]
        elif event_type in TERMINAL_EVENTS:
            event_metric = parse_decimal(event["metric"], field=f"{event_type}.metric")
            if event_metric != metric:
                raise AutoresearchError(
                    f"{event_type} event does not match the last retained metric"
                )
            if event_type != "error" and event["head"] != head:
                raise AutoresearchError(
                    f"{event_type} event does not match the last retained commit"
                )
            if event_type == "complete" and not target_reached(
                event_metric,
                parse_decimal(run["target"], field="run.target"),
                run["metric"]["direction"],
            ):
                raise AutoresearchError("complete event does not satisfy the configured target")
            if event_type == "error":
                if event["trial_commit"] is None and event["head"] != head:
                    raise AutoresearchError(
                        "error without a trial commit changed the retained HEAD"
                    )
                if (
                    event["trial_commit"] is not None
                    and event["revert_commit"] is None
                    and event["head"] != event["trial_commit"]
                ):
                    raise AutoresearchError(
                        "unreverted error HEAD must equal its trial commit"
                    )
            head = event["head"]
            status = event_type
            last_terminal = event_type
            last_terminal_event = event
        elif event_type == "resumed":
            if parse_decimal(event["metric"], field="resumed.metric") != metric or event["head"] != head:
                raise AutoresearchError("resumed event does not match retained state")

    if status == "active":
        target = parse_decimal(run["target"], field="run.target")
        if target_reached(metric, target, run["metric"]["direction"]):
            raise AutoresearchError("Active event history reached the target but lacks a complete event")
        if run["max_candidates"] is not None and iterations >= run["max_candidates"]:
            raise AutoresearchError(
                "Active event history reached the candidate limit but lacks a stopped event"
            )

    return RunState(
        status=status,
        metric=metric,
        head=head,
        iterations=iterations,
        last_event=events[-1],
    )


def load_context(repo: Path | str) -> tuple[Paths, dict[str, Any], list[dict[str, Any]], RunState]:
    paths, run = load_run(repo)
    events = load_events(paths, run)
    state = derive_state(run, events)
    return paths, run, events, state


def append_event(paths: Paths, run: dict[str, Any], events: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    event = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run["run_id"],
        "seq": len(events),
        "time": utc_now(),
        **fields,
    }
    validate_event(
        event,
        run_id=run["run_id"],
        expected_seq=len(events),
        source="new event",
    )
    append_json_line(paths.events, event)
    return event


def status_payload(
    paths: Paths,
    run: dict[str, Any],
    events: list[dict[str, Any]],
    state: RunState,
) -> dict[str, Any]:
    return {
        "run_id": run["run_id"],
        "status": state.status,
        "goal": run["goal"],
        "repo": run["repo"],
        "branch": run["branch"],
        "scope": run["scope"],
        "metric": {
            "name": run["metric"]["name"],
            "direction": run["metric"]["direction"],
            "current": decimal_json(state.metric),
            "target": run["target"],
        },
        "iterations": state.iterations,
        "head": state.head,
        "last_event": state.last_event,
        "events_path": str(paths.events),
        "event_count": len(events),
    }
