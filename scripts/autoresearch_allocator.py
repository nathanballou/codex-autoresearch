#!/usr/bin/env python3
"""Adaptive split of concurrency between deepening the best result and trying new ideas."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any

ROLES = ("exploit", "explore")

# Outcomes that say something about whether a role is paying off. A failed candidate
# says nothing about its role, only that an agent died, so it never votes.
JUDGED_OUTCOMES = {"admitted", "discarded"}


def resolved_window(
    events: list[dict[str, Any]], roles: dict[int, str], window: int
) -> list[tuple[str, str]]:
    """
    Take the most recent judged candidates as (role, outcome) pairs.
    Args:
    events: Full validated event list.
    roles: Candidate id to role, taken from the start events.
    window: How many recent judged candidates to consider.
    Return: Up to window pairs, oldest first.
    """
    judged = [
        (roles[event["candidate"]], event["outcome"])
        for event in events
        if event["event"] == "candidate_resolved"
        and event["outcome"] in JUDGED_OUTCOMES
        and event["candidate"] in roles
    ]
    return judged[-window:]


def admission_rate(judged: list[tuple[str, str]], role: str) -> Decimal:
    """
    Fraction of a role's judged candidates that were admitted.
    Args:
    judged: Recent (role, outcome) pairs.
    role: Role to score.
    Return: Admission rate, or 1 for a role with no history yet.

    An unseen role scores 1 so the policy tries it before believing anything about it.
    """
    attempts = [outcome for candidate_role, outcome in judged if candidate_role == role]
    if not attempts:
        return Decimal(1)
    admitted = sum(1 for outcome in attempts if outcome == "admitted")
    return Decimal(admitted) / Decimal(len(attempts))


def choose_role(
    *,
    events: list[dict[str, Any]],
    roles: dict[int, str],
    live: dict[str, int],
    max_parallel: int,
    window: int,
    min_per_role: int,
    plateau_k: int,
) -> tuple[str, str]:
    """
    Pick the role for the next candidate.
    Args:
    events: Full validated event list.
    roles: Candidate id to role for every started candidate.
    live: Count of started-but-unresolved candidates per role.
    max_parallel: Concurrency the run is allowed.
    window: How many recent judged candidates inform the split.
    min_per_role: Floor each role keeps when max_parallel allows it.
    plateau_k: Consecutive unadmitted exploits that force an explore.
    Return: The chosen role and the reason it was chosen.
    """
    judged = resolved_window(events, roles, window)

    exploit_tail = [outcome for role, outcome in judged if role == "exploit"][-plateau_k:]
    if len(exploit_tail) == plateau_k and not any(
        outcome == "admitted" for outcome in exploit_tail
    ):
        return "explore", "plateau_escape"

    exploit_rate = admission_rate(judged, "exploit")
    explore_rate = admission_rate(judged, "explore")

    # With no evidence separating the roles, balance the live counts instead of letting
    # a rounding mode decide a tied share. An unseen role rates 1, so a perfect exploit
    # streak still ties until explore has actually been tried.
    if exploit_rate != explore_rate:
        share = exploit_rate / (exploit_rate + explore_rate)
        desired_exploit = int(
            (share * Decimal(max_parallel)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
        )
        if max_parallel >= 2:
            desired_exploit = max(min_per_role, min(desired_exploit, max_parallel - min_per_role))
        deficit = {
            "exploit": desired_exploit - live.get("exploit", 0),
            "explore": (max_parallel - desired_exploit) - live.get("explore", 0),
        }
        if deficit["exploit"] != deficit["explore"]:
            chosen = "exploit" if deficit["exploit"] > deficit["explore"] else "explore"
            return chosen, "policy_share"

    if live.get("exploit", 0) != live.get("explore", 0):
        chosen = "exploit" if live.get("exploit", 0) < live.get("explore", 0) else "explore"
        return chosen, "policy_tiebreak"
    return "exploit", "policy_tiebreak"
