from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "autoresearch.py"


class ParallelTest(unittest.TestCase):
    """End-to-end coverage of concurrent candidates against a real Git repository."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.worktrees = Path(self.temp.name) / "worktrees"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "autoresearch").mkdir()
        # Three independent knobs so candidates can improve without conflicting.
        for name in ("a", "b", "c"):
            (self.repo / "src" / f"{name}.txt").write_text("10\n", encoding="utf-8")
        (self.repo / "score.py").write_text(
            "from pathlib import Path\n"
            "print(sum(int(Path(f'src/{n}.txt').read_text().strip()) for n in 'abc'))\n",
            encoding="utf-8",
        )
        (self.repo / "autoresearch" / "goal.md").write_text(
            "# Goal\n\nMinimize the sum of three independent knobs.\n", encoding="utf-8"
        )
        (self.repo / "autoresearch" / "compute.json").write_text(
            json.dumps(
                {
                    "cores_per_candidate": 1,
                    "measurement": "parallel",
                    "bank": [{"id": "local", "kind": "cores", "cores": 3, "label": "test"}],
                    "workers": {
                        "simple": {"model": "haiku", "thinking_tokens": 1},
                        "standard": {"model": "sonnet", "thinking_tokens": 2},
                        "complex": {"model": "sonnet", "thinking_tokens": 3},
                    },
                }
            ),
            encoding="utf-8",
        )
        self.git("init", "-b", "main")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.com")
        self.git("add", ".")
        self.git("commit", "-m", "baseline")

    def tearDown(self) -> None:
        self.git("worktree", "prune", check=False)
        self.temp.cleanup()

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=check,
        )

    def cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args, "--repo", str(self.repo)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=check,
        )

    def init(self, *extra: str, lease: str = "1800") -> dict:
        return json.loads(
            self.cli(
                "init",
                "--goal", "minimize the sum",
                "--scope", "src",
                "--metric-name", "sum",
                "--direction", "lower",
                "--verify", "python3 score.py",
                "--target", "0",
                "--max-parallel", "bank",
                "--worktree-root", str(self.worktrees),
                "--lease-seconds", lease,
                "--window", "8",
                "--min-per-role", "1",
                "--plateau-k", "3",
                *extra,
            ).stdout
        )

    def claim(self, count: int) -> list[dict]:
        return json.loads(self.cli("claim", "--count", str(count)).stdout)["candidates"]

    def status(self) -> dict:
        return json.loads(self.cli("status").stdout)

    def set_knob(self, packet: dict, knob: str, value: int) -> None:
        (Path(packet["worktree"]) / "src" / f"{knob}.txt").write_text(
            f"{value}\n", encoding="utf-8"
        )

    def test_concurrent_candidates_each_get_an_isolated_worktree(self) -> None:
        self.init()
        packets = self.claim(3)
        self.assertEqual([1, 2, 3], [packet["candidate"] for packet in packets])
        self.assertEqual(3, len({packet["worktree"] for packet in packets}))
        self.assertEqual(3, len({packet["branch"] for packet in packets}))

        self.set_knob(packets[0], "a", 1)
        values = [
            (Path(packet["worktree"]) / "src" / "a.txt").read_text(encoding="utf-8").strip()
            for packet in packets
        ]
        self.assertEqual(["1", "10", "10"], values)
        self.assertEqual("10", (self.repo / "src" / "a.txt").read_text(encoding="utf-8").strip())

    def test_bank_exhaustion_reports_rather_than_over_allocating(self) -> None:
        self.init()
        self.claim(3)
        payload = json.loads(self.cli("claim", "--count", "1").stdout)
        self.assertEqual(0, payload["claimed"])
        self.assertIsNotNone(payload["unfilled_reason"])

    def test_stale_candidate_rebases_and_remeasures_against_the_moved_frontier(self) -> None:
        self.init()
        first, second = self.claim(2)
        self.set_knob(first, "a", 4)
        self.set_knob(second, "b", 3)

        self.cli("finish", "--candidate", str(first["candidate"]), "--description", "lower a")
        self.assertEqual(24, self.status()["metric"]["current"])

        # The second candidate measured 23 in isolation. After rebasing onto the moved
        # frontier it must be re-measured as 17, which is what it actually lands on.
        result = json.loads(
            self.cli(
                "finish", "--candidate", str(second["candidate"]), "--description", "lower b"
            ).stdout
        )
        self.assertEqual("admitted", result["outcome"])
        self.assertEqual(17, result["trial_metric"])
        self.assertEqual(17, self.status()["metric"]["current"])
        self.assertNotIn("Revert", self.git("log", "--format=%s").stdout)

    def test_parallel_finishes_serialize_on_the_admission_lock(self) -> None:
        self.init()
        packets = self.claim(3)
        for packet, knob in zip(packets, ("a", "b", "c")):
            self.set_knob(packet, knob, 1)

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(
                pool.map(
                    lambda packet: self.cli(
                        "finish",
                        "--candidate",
                        str(packet["candidate"]),
                        "--description",
                        f"lower knob for candidate {packet['candidate']}",
                        check=False,
                    ),
                    packets,
                )
            )

        self.assertTrue(all(result.returncode == 0 for result in results), results)
        status = self.status()
        # All three improvements combined: 1 + 1 + 1.
        self.assertEqual(3, status["metric"]["current"])
        self.assertEqual([], status["parallel"]["unresolved_candidates"])
        self.assertEqual(0, status["parallel"]["grants_held"])

    def test_expired_lease_is_reported_then_reaped_explicitly(self) -> None:
        self.init(lease="1")
        packet = self.claim(1)[0]
        subprocess.run([sys.executable, "-c", "import time; time.sleep(2)"], check=True)

        reconcile = json.loads(self.cli("reconcile").stdout)
        self.assertEqual([packet["candidate"]], reconcile["reapable_candidates"])
        self.assertEqual("live", self.status()["parallel"]["slots"][0]["state"])

        reaped = json.loads(self.cli("reap", "--candidate", str(packet["candidate"])).stdout)
        self.assertEqual("lease_expired", reaped["reason"])
        self.assertEqual("idle", self.status()["parallel"]["slots"][0]["state"])

    def test_reaped_candidate_cannot_admit_late_work(self) -> None:
        self.init(lease="1")
        packet = self.claim(1)[0]
        subprocess.run([sys.executable, "-c", "import time; time.sleep(2)"], check=True)
        self.cli("reap", "--candidate", str(packet["candidate"]))

        self.set_knob(packet, "a", 1)
        late = self.cli(
            "finish", "--candidate", str(packet["candidate"]), "--description", "late", check=False
        )
        self.assertNotEqual(0, late.returncode)
        self.assertIn("already resolved", late.stderr)
        self.assertEqual(30, self.status()["metric"]["current"])

    def test_abandoned_candidate_frees_its_slot_without_moving_the_frontier(self) -> None:
        self.init()
        packet = self.claim(1)[0]
        result = json.loads(
            self.cli(
                "abandon", "--candidate", str(packet["candidate"]), "--reason", "no idea left"
            ).stdout
        )
        self.assertEqual("failed", result["outcome"])
        status = self.status()
        self.assertEqual(30, status["metric"]["current"])
        self.assertEqual([], status["parallel"]["unresolved_candidates"])

    def test_single_slot_run_still_reaches_the_target(self) -> None:
        self.init("--max-parallel", "1")
        for knob in ("a", "b", "c"):
            packet = self.claim(1)[0]
            self.set_knob(packet, knob, 0)
            self.cli(
                "finish", "--candidate", str(packet["candidate"]), "--description", f"zero {knob}"
            )
        status = self.status()
        self.assertEqual("complete", status["status"])
        self.assertEqual(0, status["metric"]["current"])


if __name__ == "__main__":
    unittest.main()
