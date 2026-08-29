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
    tier: str,
    profile: dict[str, Any],
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
    tier: Difficulty tier this candidate was assigned.
    profile: Model and thinking budget for that tier.
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

## Effort

This candidate is rated **{tier}**: model `{profile["model"]}`, thinking budget
{profile["thinking_tokens"]} tokens. Spend that budget on the hypothesis, not on
re-reading context you already have here.

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

## Measured analysis

Profile the current frontier before changing code. Use a profiler, analyzer,
benchmark breakdown, or focused instrumentation appropriate to the goal, and record
the command or data source, measured values, and units. The scalar metric alone is
not a profile. Keep profiling-only artifacts out of the candidate diff.

After making your focused change, rerun the same profiling after your change under
the same conditions before calling `finish`. If existing tools do not expose a
breakdown, create and run the smallest diagnostic measurement needed; do not guess.

## Finishing

Make one focused change, then hand the candidate back. The control plane commits,
measures, and decides whether to admit it; you do not commit and you do not judge
your own result.

    python3 {control} finish --repo {run["repo"]} --candidate {candidate} \\
      --description "<what you changed>"

`finish` leaves your slot in `reporting`. Before replying to the main thread, write
a UTF-8 JSON analysis file no larger than 16,384 bytes. The analysis must describe
the exact `trial_commit` returned by `finish`. If `finish` rebased your change onto a
new frontier, rerun the same profiling on that returned commit and use those results.
Use this exact shape:

    {{
      "schema_version": 2,
      "profiled_commit": "<trial_commit from finish>",
      "measurement_source": "<profiling command or data source>",
      "observations": [
        {{"area": "<component>", "before": 0, "after": 0,
          "unit": "<unit>", "effect": "improvement|regression|unchanged"}}
      ],
      "outcome_analysis": "<what changed and why the goal was or was not reached>",
      "diagnostic_confidence": "observed|inferred|hypothesis|unknown",
      "cause_chain": [
        {{"area": "<area from observations>",
          "role": "improvement|regression|remaining_bottleneck|context",
          "why": "<how this measured area affected the outcome>"}}
      ],
      "next_focus": {{"area": "<largest remaining issue>", "current_value": 0,
        "unit": "<unit>", "why": "<why hard data makes it next>",
        "experiment": "<specific next experiment>"}},
      "limitations": "<measurement limitations>"
    }}

Order `cause_chain` from the change's useful effect through any offsetting regression
to the remaining bottleneck. Every area must name an `observations` entry. Use
`observed` only when the measurements directly establish the explanation; otherwise
label it `inferred`, `hypothesis`, or `unknown` rather than presenting it as fact.

Submit it to persist the evidence and release your slot:

    python3 {control} report --repo {run["repo"]} --candidate {candidate} \
      --analysis-file <analysis.json>

Use the `finish` receipt and persisted analysis in your final report to the main
thread:

- State the outcome, trial and retained metrics, target, and remaining gap.
- Separate execution status from frontier outcome: an experiment that ran correctly
  but was discarded is a completed experiment, not an execution failure.
- Show the profiling command or data source and the before/after breakdown with each
  measured value and unit.
- State which improvements and regressions were measured, what frontier and trial
  state was preserved, and the confidence level of the causal explanation.
- If the candidate is admitted but misses the target, identify the largest remaining
  measured contributor and recommend the next experiment that directly targets it.
- If the candidate is discarded, analyze this run rather than the retained frontier:
  quantify its improvements and regressions, explain what outweighed what, and say
  where a retry of this direction should focus.
- Give one recommended main-thread focus, the hard data that makes it the priority,
  and a concrete next experiment.

If you cannot make a meaningful change, say so instead of guessing:

    python3 {control} abandon --repo {run["repo"]} --candidate {candidate} \\
      --reason "<why>"
"""
