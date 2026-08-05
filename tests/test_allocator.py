from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from autoresearch_allocator import admission_rate, choose_role, resolved_window


def resolved(candidate: int, outcome: str) -> dict[str, object]:
    return {"event": "candidate_resolved", "candidate": candidate, "outcome": outcome}


class AllocatorTest(unittest.TestCase):
    def choose(self, events, roles, live, **overrides):
        settings = {
            "max_parallel": 4,
            "window": 8,
            "min_per_role": 1,
            "plateau_k": 3,
        }
        settings.update(overrides)
        return choose_role(events=events, roles=roles, live=live, **settings)

    def test_unseen_role_scores_optimistically(self) -> None:
        self.assertEqual(Decimal(1), admission_rate([], "explore"))
        self.assertEqual(Decimal(1), admission_rate([("exploit", "admitted")], "explore"))

    def test_admission_rate_counts_only_that_role(self) -> None:
        judged = [
            ("exploit", "admitted"),
            ("exploit", "discarded"),
            ("explore", "discarded"),
        ]
        self.assertEqual(Decimal("0.5"), admission_rate(judged, "exploit"))
        self.assertEqual(Decimal(0), admission_rate(judged, "explore"))

    def test_window_keeps_only_the_most_recent_judged_candidates(self) -> None:
        events = [resolved(index, "discarded") for index in range(1, 11)]
        roles = {index: "exploit" for index in range(1, 11)}
        self.assertEqual(3, len(resolved_window(events, roles, 3)))
        self.assertEqual(10, len(resolved_window(events, roles, 50)))

    def test_failed_candidates_never_vote(self) -> None:
        events = [resolved(1, "failed"), resolved(2, "failed")]
        roles = {1: "exploit", 2: "exploit"}
        self.assertEqual([], resolved_window(events, roles, 8))

    def test_plateau_forces_exploration(self) -> None:
        events = [resolved(index, "discarded") for index in (1, 2, 3)]
        roles = {1: "exploit", 2: "exploit", 3: "exploit"}
        role, reason = self.choose(events, roles, {"exploit": 0, "explore": 0})
        self.assertEqual(("explore", "plateau_escape"), (role, reason))

    def test_one_admitted_exploit_breaks_the_plateau(self) -> None:
        events = [resolved(1, "discarded"), resolved(2, "admitted"), resolved(3, "discarded")]
        roles = {1: "exploit", 2: "exploit", 3: "exploit"}
        _, reason = self.choose(events, roles, {"exploit": 0, "explore": 0})
        self.assertNotEqual("plateau_escape", reason)

    def test_a_winning_role_earns_more_slots(self) -> None:
        events = [
            resolved(1, "admitted"),
            resolved(2, "admitted"),
            resolved(3, "discarded"),
            resolved(4, "discarded"),
        ]
        roles = {1: "exploit", 2: "exploit", 3: "explore", 4: "explore"}
        role, reason = self.choose(events, roles, {"exploit": 0, "explore": 0})
        self.assertEqual("exploit", role)
        self.assertEqual("policy_share", reason)

    def test_floors_keep_both_roles_alive_when_one_dominates(self) -> None:
        events = [resolved(index, "admitted") for index in (1, 2, 3, 4)]
        roles = {index: "exploit" for index in (1, 2, 3, 4)}
        # exploit wins every judged candidate, but the floor reserves a slot for explore
        role, _ = self.choose(events, roles, {"exploit": 3, "explore": 0}, max_parallel=4)
        self.assertEqual("explore", role)

    def test_single_slot_run_skips_the_floors(self) -> None:
        events = [resolved(1, "admitted")]
        roles = {1: "exploit"}
        role, _ = self.choose(events, roles, {"exploit": 0, "explore": 0}, max_parallel=1)
        self.assertEqual("exploit", role)

    def test_tie_breaks_toward_the_role_with_fewer_live_candidates(self) -> None:
        role, reason = self.choose([], {}, {"exploit": 2, "explore": 1}, max_parallel=4)
        self.assertEqual(("explore", "policy_tiebreak"), (role, reason))

    def test_cold_start_is_deterministic(self) -> None:
        role, reason = self.choose([], {}, {"exploit": 0, "explore": 0})
        self.assertEqual(("exploit", "policy_tiebreak"), (role, reason))


if __name__ == "__main__":
    unittest.main()
