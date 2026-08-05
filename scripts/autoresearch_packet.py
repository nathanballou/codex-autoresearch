#!/usr/bin/env python3
"""Host-agnostic worker packets handed to each spawned subagent."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROLE_INSTRUCTIONS = {
    "exploit": (
        "Deepen the direction that produced the current best result. Read the last "
        "admitted candidate's hypothesis below and push further along it. Do not "
        "restart from an unrelated idea."
    ),
    "explore": (
        "Try a materially different mechanism. Read the recent hypotheses below and "
        "pick something that is NOT a variation of any of them. A small tweak to an "
        "already-tried idea is a wasted candidate."
    ),
}


def readable_deadline(epoch_seconds: float) -> str:
    """
    Render a lease deadline for a human reader.
    Args:
    epoch_seconds: Deadline as epoch seconds.
    Return: UTC timestamp to the second.
    """
    return (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def recent_hypotheses(events: list[dict[str, Any]], limit: int) -> list[str]:
    """
    Collect recent candidate descriptions so a worker can avoid repeating them.
    Args:
    events: Full validated event list.
    limit: How many recent descriptions to include.
    Return: Descriptions with their outcome, oldest first.
    """
    described = [
        f"{event['description']} -> {event['outcome']}"
        for event in events
        if event["event"] == "candidate_resolved"
    ]
    return described[-limit:]


def last_admitted(events: list[dict[str, Any]]) -> str | None:
    """
    Find the hypothesis behind the current frontier.
    Args:
    events: Full validated event list.
    Return: The most recent admitted description, or None before the first admission.
    """
    for event in reversed(events):
        if event["event"] == "candidate_resolved" and event["outcome"] == "admitted":
            return event["description"]
    return None


def build_packet(
    *,
    control: Path,
    run: dict[str, Any],
    events: list[dict[str, Any]],
    candidate: int,
    slot: dict[str, Any],
    role: str,
    grant: dict[str, Any],
    goal_text: str,
    decisions_text: str,
    base_metric: str,
    window: int,
) -> str:
    """
    Render the complete instruction text for one candidate worker.
    Args:
    control: Path to the control script the worker must call.
    run: Validated run configuration.
    events: Full validated event list.
    candidate: Candidate identifier this worker owns.
    slot: Slot record carrying the worktree and lease.
    role: Either exploit or explore.
    grant: Compute granted to this candidate.
    goal_text: Overarching process goal shared by every worker.
    decisions_text: Accumulated curated decisions.
    base_metric: Frontier metric this candidate must beat.
    window: How many recent hypotheses to show.
    Return: The worker packet.

    Generating this in code is what makes the worker contract mechanical. The
    coordinator cannot spawn a thin prompt, because it does not write the prompt.
    """
    metric = run["metric"]
    better = "lower" if metric["direction"] == "lower" else "higher"
    grant_line = (
        f"{grant['cores']} cores on {grant['label']}"
        if grant["cores"] is not None
        else f"the whole machine {grant['label']}"
    )
    history = recent_hypotheses(events, window)
    history_block = (
        "\n".join(f"  - {item}" for item in history) if history else "  (none yet)"
    )
    admitted = last_admitted(events)
    frontier_line = (
        f"Current best hypothesis: {admitted}"
        if admitted
        else "No candidate has been admitted yet; the baseline is the frontier."
    )

    return f"""You own autoresearch candidate {candidate}. Work only in your own worktree.

## Overarching goal (shared by every worker, curated by the main thread)

{goal_text.strip()}

## Decisions and notes you must honor

{decisions_text.strip() or "(none recorded yet)"}

## This candidate's individual target

{run["goal"]}

Metric `{metric["name"]}`: currently {base_metric}, target {run["target"]} ({better} is better).
Verify command: {metric["command"]}
{frontier_line}

## Your role: {role}

{ROLE_INSTRUCTIONS[role]}

Recently tried, do not repeat:
{history_block}

## Your workspace and compute

Worktree: {slot["worktree"]}
Branch: {slot["branch"]}
Granted compute: {grant_line}
Editable scope: {", ".join(run["scope"])}

Everything you change must live inside your worktree and inside that scope. Do not
touch the primary checkout, and do not edit the curated documents above.

Your lease expires at {readable_deadline(slot["lease_expires_at"])}. If your work will run past that,
extend it first:

    python3 {control} heartbeat --repo {run["repo"]} --candidate {candidate}

## Finishing

Make one focused change, then hand the candidate back. The control plane commits,
measures, and decides whether to admit it; you do not commit and you do not judge
your own result.

    python3 {control} finish --repo {run["repo"]} --candidate {candidate} \\
      --description "<what you changed>"

If you cannot make a meaningful change, say so instead of guessing:

    python3 {control} abandon --repo {run["repo"]} --candidate {candidate} \\
      --reason "<why>"
"""
