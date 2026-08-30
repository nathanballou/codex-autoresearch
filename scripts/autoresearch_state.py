#!/usr/bin/env python3
"""Strict run and event schema validation, and authoritative state replay."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
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
EVENT_LOCK_FILE = "events.lock"


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
    "docs",
    "parallel",
}
PARALLEL_KEYS = {
    "max_parallel",
    "max_parallel_resolved",
    "worktree_root",
    "prepare",
    "lease_seconds",
    "allocation",
}
ALLOCATION_KEYS = {"window", "min_per_role", "plateau_k"}
DOCS_KEYS = {"goal_path", "decisions_path", "goal_sha256", "decisions_sha256"}
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
    docs = payload["docs"]
    if not isinstance(docs, dict):
        raise AutoresearchError(f"{source}.docs must be an object")
    require_exact_keys(docs, required=DOCS_KEYS, source=f"{source}.docs")
    for key in sorted(DOCS_KEYS):
        if not isinstance(docs[key], str) or not docs[key]:
            raise AutoresearchError(f"{source}.docs.{key} must be a non-empty string")
    parallel = payload["parallel"]
    if not isinstance(parallel, dict):
        raise AutoresearchError(f"{source}.parallel must be an object")
    require_exact_keys(parallel, required=PARALLEL_KEYS, source=f"{source}.parallel")
    if parallel["max_parallel"] != "bank" and (
        not isinstance(parallel["max_parallel"], int)
        or isinstance(parallel["max_parallel"], bool)
        or parallel["max_parallel"] <= 0
    ):
        raise AutoresearchError(
            f"{source}.parallel.max_parallel must be the string bank or a positive integer"
        )
    for key in ("max_parallel_resolved", "lease_seconds"):
        if (
            not isinstance(parallel[key], int)
            or isinstance(parallel[key], bool)
            or parallel[key] <= 0
        ):
            raise AutoresearchError(f"{source}.parallel.{key} must be a positive integer")
    if not isinstance(parallel["worktree_root"], str) or not parallel["worktree_root"]:
        raise AutoresearchError(f"{source}.parallel.worktree_root must be a non-empty string")
    if parallel["prepare"] is not None and (
        not isinstance(parallel["prepare"], str) or not parallel["prepare"].strip()
    ):
        raise AutoresearchError(f"{source}.parallel.prepare must be null or a non-empty string")
    allocation = parallel["allocation"]
    if not isinstance(allocation, dict):
        raise AutoresearchError(f"{source}.parallel.allocation must be an object")
    require_exact_keys(
        allocation, required=ALLOCATION_KEYS, source=f"{source}.parallel.allocation"
    )
    for key in sorted(ALLOCATION_KEYS):
        if (
            not isinstance(allocation[key], int)
            or isinstance(allocation[key], bool)
            or allocation[key] <= 0
        ):
            raise AutoresearchError(
                f"{source}.parallel.allocation.{key} must be a positive integer"
            )
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
CANDIDATE_REASONS = {
    "improved": "admitted",
    "no_improvement": "discarded",
    "stale_no_improvement": "discarded",
    "rebase_conflict": "discarded",
    "guard_failed": "discarded",
    "no_change": "failed",
    "abandoned": "failed",
    "lease_expired": "failed",
}

EVENT_FIELDS = {
    "baseline": {"head", "metric", "verify_log", "guard_log"},
    "candidate_started": {
        "candidate",
        "base_commit",
        "base_metric",
        "slot",
        "role",
        "role_source",
        "grant",
        "branch",
        "lease_expires_at",
        "goal_sha256",
        "decisions_sha256",
    },
    "candidate_resolved": {
        "candidate",
        "outcome",
        "reason",
        "description",
        "trial_metric",
        "retained_metric",
        "trial_commit",
        "trial_branch",
        "head",
        "guard",
        "verify_log",
        "guard_log",
    },
    "candidate_reported": {"candidate", "analysis"},
    "blocked": {"reason", "head", "metric", "unresolved_candidates"},
    "complete": {"reason", "head", "metric", "unresolved_candidates"},
    "error": {
        "reason",
        "head",
        "metric",
        "trial_commit",
        "log",
        "unresolved_candidates",
    },
    "decision": {"note", "decisions_sha256"},
    "resumed": {"note", "head", "metric"},
    "stopped": {"reason", "head", "metric", "unresolved_candidates"},
}
EVENT_OPTIONAL_FIELDS = {"candidate_resolved": {"report_required"}}

ANALYSIS_KEYS = {
    "schema_version",
    "profiled_commit",
    "measurement_source",
    "observations",
    "outcome_analysis",
    "next_focus",
    "limitations",
}
ANALYSIS_V2_KEYS = ANALYSIS_KEYS | {"diagnostic_confidence", "cause_chain"}
OBSERVATION_KEYS = {"area", "before", "after", "unit", "effect"}
NEXT_FOCUS_KEYS = {"area", "current_value", "unit", "why", "experiment"}
CAUSE_KEYS = {"area", "role", "why"}


def validate_analysis(payload: Any, *, source: str) -> dict[str, Any]:
    """
    Validate one measured candidate analysis.
    Args:
    payload: Parsed JSON value to validate.
    source: Human-readable source used in errors.
    Return: The validated analysis object.
    """
    if not isinstance(payload, dict):
        raise AutoresearchError(f"{source} must contain a JSON object")
    schema_version = payload.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in {1, 2}
    ):
        raise AutoresearchError(f"{source}.schema_version must be 1 or 2")
    require_exact_keys(
        payload,
        required=ANALYSIS_V2_KEYS if schema_version == 2 else ANALYSIS_KEYS,
        source=source,
    )
    for key in (
        "profiled_commit",
        "measurement_source",
        "outcome_analysis",
        "limitations",
    ):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise AutoresearchError(f"{source}.{key} must be a non-empty string")
    observations = payload["observations"]
    if not isinstance(observations, list) or not observations:
        raise AutoresearchError(f"{source}.observations must be a non-empty array")
    for index, observation in enumerate(observations):
        item_source = f"{source}.observations[{index}]"
        if not isinstance(observation, dict):
            raise AutoresearchError(f"{item_source} must be an object")
        require_exact_keys(observation, required=OBSERVATION_KEYS, source=item_source)
        for key in ("area", "unit"):
            if not isinstance(observation[key], str) or not observation[key].strip():
                raise AutoresearchError(f"{item_source}.{key} must be a non-empty string")
        for key in ("before", "after"):
            if not isinstance(observation[key], (int, float)) or isinstance(
                observation[key], bool
            ):
                raise AutoresearchError(f"{item_source}.{key} must be a number")
            parse_decimal(observation[key], field=f"{item_source}.{key}")
        if not isinstance(observation["effect"], str) or observation["effect"] not in {
            "improvement",
            "regression",
            "unchanged",
        }:
            raise AutoresearchError(
                f"{item_source}.effect must be improvement, regression, or unchanged"
            )
    next_focus = payload["next_focus"]
    if not isinstance(next_focus, dict):
        raise AutoresearchError(f"{source}.next_focus must be an object")
    require_exact_keys(next_focus, required=NEXT_FOCUS_KEYS, source=f"{source}.next_focus")
    for key in ("area", "unit", "why", "experiment"):
        if not isinstance(next_focus[key], str) or not next_focus[key].strip():
            raise AutoresearchError(f"{source}.next_focus.{key} must be a non-empty string")
    if not isinstance(next_focus["current_value"], (int, float)) or isinstance(
        next_focus["current_value"], bool
    ):
        raise AutoresearchError(f"{source}.next_focus.current_value must be a number")
    parse_decimal(
        next_focus["current_value"], field=f"{source}.next_focus.current_value"
    )
    if not any(
        observation["area"] == next_focus["area"]
        and observation["unit"] == next_focus["unit"]
        and parse_decimal(observation["after"], field="observation.after")
        == parse_decimal(next_focus["current_value"], field="next_focus.current_value")
        for observation in observations
    ):
        raise AutoresearchError(
            f"{source}.next_focus must match one observation's area, after value, and unit"
        )
    if schema_version == 2:
        confidence = payload["diagnostic_confidence"]
        if not isinstance(confidence, str) or confidence not in {
            "observed",
            "inferred",
            "hypothesis",
            "unknown",
        }:
            raise AutoresearchError(
                f"{source}.diagnostic_confidence must be observed, inferred, hypothesis, or unknown"
            )
        cause_chain = payload["cause_chain"]
        if not isinstance(cause_chain, list) or not cause_chain:
            raise AutoresearchError(f"{source}.cause_chain must be a non-empty array")
        l_areas = [observation["area"] for observation in observations]
        if len(l_areas) != len(set(l_areas)):
            raise AutoresearchError(f"{source}.observation areas must be unique")
        d_observations = {observation["area"]: observation for observation in observations}
        for index, cause in enumerate(cause_chain):
            item_source = f"{source}.cause_chain[{index}]"
            if not isinstance(cause, dict):
                raise AutoresearchError(f"{item_source} must be an object")
            require_exact_keys(cause, required=CAUSE_KEYS, source=item_source)
            for key in ("area", "why"):
                if not isinstance(cause[key], str) or not cause[key].strip():
                    raise AutoresearchError(f"{item_source}.{key} must be a non-empty string")
            if cause["area"] not in d_observations:
                raise AutoresearchError(
                    f"{item_source}.area must reference a measured observation"
                )
            if not isinstance(cause["role"], str) or cause["role"] not in {
                "improvement",
                "regression",
                "remaining_bottleneck",
                "context",
            }:
                raise AutoresearchError(
                    f"{item_source}.role must be improvement, regression, "
                    "remaining_bottleneck, or context"
                )
            if cause["role"] in {"improvement", "regression"} and (
                d_observations[cause["area"]]["effect"] != cause["role"]
            ):
                raise AutoresearchError(
                    f"{item_source}.role must match the measured observation effect"
                )
        if (
            cause_chain[-1]["role"] != "remaining_bottleneck"
            or cause_chain[-1]["area"] != next_focus["area"]
            or any(cause["role"] == "remaining_bottleneck" for cause in cause_chain[:-1])
        ):
            raise AutoresearchError(
                f"{source}.cause_chain must end with next_focus as its only remaining_bottleneck"
            )
    return payload


def validate_event(payload: Any, *, run_id: str, expected_seq: int, source: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AutoresearchError(f"{source} must contain a JSON object")
    event_type = payload.get("event")
    if event_type not in EVENT_FIELDS:
        raise AutoresearchError(f"{source}.event has unsupported value {event_type!r}")
    require_exact_keys(
        payload,
        required=EVENT_COMMON | EVENT_FIELDS[event_type],
        optional=EVENT_OPTIONAL_FIELDS.get(event_type),
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
        if key in payload and payload[key] is not None:
            parse_decimal(payload[key], field=f"{source}.{key}")
    if event_type == "baseline":
        if not isinstance(payload["verify_log"], str) or not payload["verify_log"]:
            raise AutoresearchError(f"{source}.verify_log must be a non-empty string")
        if payload["guard_log"] is not None and (
            not isinstance(payload["guard_log"], str) or not payload["guard_log"]
        ):
            raise AutoresearchError(f"{source}.guard_log must be null or a non-empty string")
    if event_type == "candidate_started":
        if not isinstance(payload["candidate"], int) or payload["candidate"] <= 0:
            raise AutoresearchError(f"{source}.candidate must be a positive integer")
        if not isinstance(payload["base_commit"], str) or not payload["base_commit"]:
            raise AutoresearchError(f"{source}.base_commit must be a non-empty string")
    if event_type == "candidate_resolved":
        if payload["reason"] not in CANDIDATE_REASONS:
            raise AutoresearchError(
                f"{source}.reason must be one of {', '.join(sorted(CANDIDATE_REASONS))}"
            )
        if payload["outcome"] != CANDIDATE_REASONS[payload["reason"]]:
            raise AutoresearchError(
                f"{source}.outcome must be {CANDIDATE_REASONS[payload['reason']]} "
                f"for reason {payload['reason']}"
            )
        if payload["guard"] not in {"pass", "fail", "not_run"}:
            raise AutoresearchError(f"{source}.guard is invalid")
        if not isinstance(payload["candidate"], int) or payload["candidate"] <= 0:
            raise AutoresearchError(f"{source}.candidate must be a positive integer")
        for key in ("description", "trial_branch"):
            if not isinstance(payload[key], str) or not payload[key]:
                raise AutoresearchError(f"{source}.{key} must be a non-empty string")
        # An abandoned candidate never produced a commit or a measurement, so these
        # stay null. Every other outcome must carry both.
        for key in ("trial_commit", "verify_log"):
            if payload["outcome"] == "failed":
                if payload[key] is not None and not isinstance(payload[key], str):
                    raise AutoresearchError(f"{source}.{key} must be null or a string")
            elif not isinstance(payload[key], str) or not payload[key]:
                raise AutoresearchError(f"{source}.{key} must be a non-empty string")
        if payload["outcome"] == "failed" and payload["trial_metric"] is not None:
            raise AutoresearchError(f"{source}.trial_metric must be null for a failed candidate")
        if payload["guard_log"] is not None and (
            not isinstance(payload["guard_log"], str) or not payload["guard_log"]
        ):
            raise AutoresearchError(f"{source}.guard_log must be null or a non-empty string")
        if "report_required" in payload and not isinstance(payload["report_required"], bool):
            raise AutoresearchError(f"{source}.report_required must be a boolean")
    if event_type == "candidate_reported":
        if not isinstance(payload["candidate"], int) or payload["candidate"] <= 0:
            raise AutoresearchError(f"{source}.candidate must be a positive integer")
        validate_analysis(payload["analysis"], source=f"{source}.analysis")
    if event_type in {"blocked", "complete", "error", "stopped"}:
        if not isinstance(payload["reason"], str) or not payload["reason"].strip():
            raise AutoresearchError(f"{source}.reason must be a non-empty string")
        unresolved = payload["unresolved_candidates"]
        if not isinstance(unresolved, list) or any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in unresolved
        ):
            raise AutoresearchError(
                f"{source}.unresolved_candidates must be a list of positive integers"
            )
    if event_type == "error":
        for key in ("trial_commit", "log"):
            if payload[key] is not None and (not isinstance(payload[key], str) or not payload[key]):
                raise AutoresearchError(f"{source}.{key} must be null or a non-empty string")
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
    started = 0
    unresolved: dict[int, dict[str, Any]] = {}
    reporting: dict[int, dict[str, Any]] = {}
    status = "active"
    last_terminal: str | None = None
    unrolled_back_error = False

    for event in events[1:]:
        event_type = event["event"]
        if last_terminal is not None:
            if event_type != "resumed" or last_terminal == "complete":
                raise AutoresearchError(
                    f"Invalid event transition: {event_type} follows terminal event {last_terminal}"
                )
            if unrolled_back_error:
                raise AutoresearchError("Cannot resume an error whose trial commit was not rolled back")
            status = "active"
            last_terminal = None
        elif event_type == "resumed":
            raise AutoresearchError("resumed event requires a preceding blocked, error, or stopped event")

        if event_type == "candidate_started":
            started += 1
            if event["candidate"] != started:
                raise AutoresearchError(
                    f"Candidate id must be {started}, got {event['candidate']}"
                )
            if event["base_commit"] != head:
                raise AutoresearchError(
                    f"Candidate {started} base_commit does not match the frontier commit"
                )
            base_metric = parse_decimal(
                event["base_metric"], field="candidate_started.base_metric"
            )
            if base_metric != metric:
                raise AutoresearchError(
                    f"Candidate {started} base_metric does not match the frontier metric"
                )
            unresolved[started] = event
        elif event_type == "candidate_resolved":
            candidate = event["candidate"]
            if candidate not in unresolved:
                raise AutoresearchError(
                    f"Candidate {candidate} resolved without a matching unresolved start"
                )
            del unresolved[candidate]
            iterations += 1
            outcome = event["outcome"]
            reason = event["reason"]
            if event.get("report_required", False):
                if event["outcome"] == "failed":
                    raise AutoresearchError(
                        f"Candidate {candidate} failed and cannot require a report"
                    )
                reporting[candidate] = event
            if CANDIDATE_REASONS.get(reason) != outcome:
                raise AutoresearchError(
                    f"Candidate {candidate} reason {reason!r} does not match outcome {outcome!r}"
                )
            trial_metric = (
                parse_decimal(event["trial_metric"], field="candidate_resolved.trial_metric")
                if event["trial_metric"] is not None
                else None
            )
            retained_metric = parse_decimal(
                event["retained_metric"], field="candidate_resolved.retained_metric"
            )
            if outcome == "admitted":
                if trial_metric is None:
                    raise AutoresearchError(f"Candidate {candidate} admitted without a trial metric")
                if not improved(trial_metric, metric, run["metric"]["direction"]):
                    raise AutoresearchError(
                        f"Candidate {candidate} admitted a metric that did not improve"
                    )
                if retained_metric != trial_metric:
                    raise AutoresearchError(
                        f"Candidate {candidate} admitted must retain the trial metric"
                    )
                if event["guard"] != "pass":
                    raise AutoresearchError(f"Candidate {candidate} admitted requires a passing guard")
                if event["head"] != event["trial_commit"]:
                    raise AutoresearchError(
                        f"Candidate {candidate} admitted has invalid commit provenance"
                    )
                if run["guard"] is None and event["guard_log"] is not None:
                    raise AutoresearchError(
                        f"Candidate {candidate} has a guard log but no configured guard"
                    )
                if run["guard"] is not None and event["guard_log"] is None:
                    raise AutoresearchError(
                        f"Candidate {candidate} is missing its configured guard log"
                    )
                metric = retained_metric
                head = event["head"]
            else:
                if retained_metric != metric or event["head"] != head:
                    raise AutoresearchError(
                        f"Candidate {candidate} {outcome} moved the frontier"
                    )
                if reason == "guard_failed" and (
                    run["guard"] is None or event["guard"] != "fail" or event["guard_log"] is None
                ):
                    raise AutoresearchError(
                        f"Candidate {candidate} recorded guard_failed without a failed guard"
                    )
                if reason == "no_improvement":
                    if trial_metric is None:
                        raise AutoresearchError(
                            f"Candidate {candidate} no_improvement requires a trial metric"
                        )
                    if improved(trial_metric, metric, run["metric"]["direction"]):
                        raise AutoresearchError(
                            f"Candidate {candidate} discarded an improvement as no_improvement"
                        )
                    if event["guard"] != "not_run" or event["guard_log"] is not None:
                        raise AutoresearchError(
                            f"Candidate {candidate} ran a guard for a non-improving trial"
                        )
        elif event_type == "candidate_reported":
            candidate = event["candidate"]
            if candidate not in reporting:
                raise AutoresearchError(
                    f"Candidate {candidate} reported without a report-required resolution"
                )
            if event["analysis"]["profiled_commit"] != reporting[candidate]["trial_commit"]:
                raise AutoresearchError(
                    f"Candidate {candidate} analysis profiled_commit does not match its trial commit"
                )
            del reporting[candidate]
        elif event_type in TERMINAL_EVENTS:
            if event_type != "error" and reporting:
                raise AutoresearchError(
                    f"{event_type} event cannot precede all candidate resolutions and reports"
                )
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
            if sorted(event["unresolved_candidates"]) != sorted(unresolved):
                raise AutoresearchError(
                    f"{event_type} event reports unresolved candidates "
                    f"{event['unresolved_candidates']}, replay has {sorted(unresolved)}"
                )
            unrolled_back_error = False
            if event_type == "error":
                if event["trial_commit"] is None and event["head"] != head:
                    raise AutoresearchError(
                        "error without a trial commit changed the retained HEAD"
                    )
                if event["trial_commit"] is not None and event["head"] != head:
                    # Rollback could not be completed, so the branch still carries the trial
                    # commit. The run is recoverable only by manual Git repair, never by resume.
                    if event["head"] != event["trial_commit"]:
                        raise AutoresearchError(
                            "error that left the trial in place must point at its trial commit"
                        )
                    unrolled_back_error = True
            head = event["head"]
            status = event_type
            last_terminal = event_type
        elif event_type == "resumed":
            if parse_decimal(event["metric"], field="resumed.metric") != metric or event["head"] != head:
                raise AutoresearchError("resumed event does not match retained state")

    if status == "active":
        target = parse_decimal(run["target"], field="run.target")
        if target_reached(metric, target, run["metric"]["direction"]) and not reporting:
            raise AutoresearchError("Active event history reached the target but lacks a complete event")
        if (
            run["max_candidates"] is not None
            and iterations >= run["max_candidates"]
            and not reporting
        ):
            raise AutoresearchError(
                "Active event history reached the candidate limit but lacks a stopped event"
            )

    return RunState(
        status=status,
        metric=metric,
        head=head,
        iterations=iterations,
        last_event=events[-1],
        unresolved=tuple(sorted(unresolved)),
        reporting=tuple(sorted(reporting)),
    )


def load_context(repo: Path | str) -> tuple[Paths, dict[str, Any], list[dict[str, Any]], RunState]:
    paths, run = load_run(repo)
    events = load_events(paths, run)
    state = derive_state(run, events)
    return paths, run, events, state


@contextmanager
def event_read_lock(paths: Paths) -> Iterator[None]:
    """
    Prevent controller event writes while a metric command is running.
    Args:
    paths: Resolved run paths.
    Return: Context manager yielding while a shared event lock is held.
    """
    with (paths.root / EVENT_LOCK_FILE).open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def event_write_lock(paths: Paths) -> Iterator[None]:
    """
    Give one controller process exclusive access to the event log.
    Args:
    paths: Resolved run paths.
    Return: Context manager yielding while an exclusive event lock is held.
    """
    with (paths.root / EVENT_LOCK_FILE).open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def append_event(paths: Paths, run: dict[str, Any], events: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    with event_write_lock(paths):
        current_events = load_events(paths, run) if paths.events.exists() else []
        if current_events != events:
            raise AutoresearchError(
                "Event log changed after controller state was loaded; retry the command"
            )
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
        "reporting_candidates": list(state.reporting),
        "candidate_reports": [
            {"candidate": event["candidate"], "analysis": event["analysis"]}
            for event in events
            if event["event"] == "candidate_reported"
        ],
    }
