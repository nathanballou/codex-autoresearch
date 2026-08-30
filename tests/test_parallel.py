from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import time
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

    def analysis_file(self, finish: dict, *, area: str = "knob b") -> Path:
        path = Path(self.temp.name) / f"analysis-{finish['candidate']}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "profiled_commit": finish["trial_commit"],
                    "measurement_source": "python3 profile.py",
                    "observations": [
                        {
                            "area": area,
                            "before": 10,
                            "after": 10,
                            "unit": "points",
                            "effect": "unchanged",
                        }
                    ],
                    "outcome_analysis": "Knob a improved by 6 points; knob b remained at 10 points.",
                    "diagnostic_confidence": "observed",
                    "cause_chain": [
                        {
                            "area": area,
                            "role": "remaining_bottleneck",
                            "why": "The measured value did not move and remains the next target.",
                        }
                    ],
                    "next_focus": {
                        "area": area,
                        "current_value": 10,
                        "unit": "points",
                        "why": "It is the largest measured remaining contributor.",
                        "experiment": "Reduce knob b while holding knobs a and c fixed.",
                    },
                    "limitations": "The profile covers the three score components only.",
                }
            ),
            encoding="utf-8",
        )
        return path

    def submit_analysis(self, finish: dict, *, area: str = "knob b") -> dict:
        return json.loads(
            self.cli(
                "report",
                "--candidate", str(finish["candidate"]),
                "--analysis-file", str(self.analysis_file(finish, area=area)),
            ).stdout
        )

    def finish_and_report(
        self, packet: dict, description: str, *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        completed = self.cli(
            "finish",
            "--candidate", str(packet["candidate"]),
            "--description", description,
            check=check,
        )
        if completed.returncode == 0:
            self.submit_analysis(json.loads(completed.stdout))
        return completed

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

    def test_worker_packet_requires_measured_outcome_analysis(self) -> None:
        self.init()
        packet = self.claim(1)[0]["packet"]

        self.assertIn("Profile the current frontier before changing code", packet)
        self.assertIn("rerun the same profiling after your change", packet)
        self.assertIn("If the candidate is admitted", packet)
        self.assertIn("If the candidate is discarded", packet)
        self.assertIn("what outweighed what", packet)
        self.assertIn("measured value and unit", packet)
        self.assertIn("leaves your slot in `reporting`", packet)
        self.assertIn("If `finish` rebased your change", packet)
        self.assertIn("--analysis-file <analysis.json>", packet)
        self.assertIn('"schema_version": 2', packet)
        self.assertIn('"diagnostic_confidence"', packet)
        self.assertIn('"cause_chain"', packet)

    def test_finish_holds_slot_until_measured_report_is_persisted(self) -> None:
        self.init()
        packet = self.claim(1)[0]
        self.set_knob(packet, "a", 4)

        finish = json.loads(
            self.cli(
                "finish",
                "--candidate", str(packet["candidate"]),
                "--description", "lower a",
            ).stdout
        )

        self.assertEqual("reporting", finish["status"])
        status = self.status()
        self.assertEqual([packet["candidate"]], status["parallel"]["reporting_candidates"])
        self.assertEqual("reporting", status["parallel"]["slots"][0]["state"])
        self.assertEqual(1, status["parallel"]["grants_held"])
        blocked = self.cli("block", "--reason", "pause", check=False)
        self.assertNotEqual(0, blocked.returncode)
        self.assertIn("owe measured reports", blocked.stderr)

        receipt = self.submit_analysis(finish)

        self.assertEqual("active", receipt["status"])
        status = self.status()
        self.assertEqual([], status["parallel"]["reporting_candidates"])
        self.assertEqual("idle", status["parallel"]["slots"][0]["state"])
        self.assertEqual(0, status["parallel"]["grants_held"])
        self.assertEqual("knob b", status["candidate_reports"][0]["analysis"]["next_focus"]["area"])
        history = self.cli("history").stdout
        self.assertIn("Next: knob b at 10 points", history)
        report = Path(json.loads(self.cli("report").stdout)["report"]).read_text(encoding="utf-8")
        self.assertIn("largest measured remaining contributor", report)
        self.assertIn("knob b: 10 -&gt; 10 points (unchanged)", report)
        self.assertIn("python3 profile.py", report)
        self.assertIn("Execution: completed. Frontier outcome: admitted.", report)
        self.assertIn("Diagnostic confidence: observed.", report)
        self.assertIn("Preserved state: frontier retained", report)

    def test_version_two_report_renders_causal_evidence_and_discarded_state(self) -> None:
        self.init()
        packet = self.claim(1)[0]
        self.set_knob(packet, "a", 11)
        finish = json.loads(
            self.cli(
                "finish",
                "--candidate", str(packet["candidate"]),
                "--description", "trade parser speed for allocation cost",
            ).stdout
        )
        source = self.analysis_file(finish)
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["diagnostic_confidence"] = "inferred"
        payload["observations"] = [
            {
                "area": "parser",
                "before": 20,
                "after": 12,
                "unit": "ms",
                "effect": "improvement",
            },
            {
                "area": "allocation",
                "before": 3,
                "after": 5,
                "unit": "MB",
                "effect": "regression",
            },
        ]
        payload["cause_chain"] = [
            {
                "area": "parser",
                "role": "improvement",
                "why": "Parsing became 8 ms faster.",
            },
            {
                "area": "allocation",
                "role": "regression",
                "why": "The 2 MB increase outweighed the parser gain in the objective.",
            },
            {
                "area": "allocation",
                "role": "remaining_bottleneck",
                "why": "It is the largest measured regression in this trial.",
            },
        ]
        payload["next_focus"] = {
            "area": "allocation",
            "current_value": 5,
            "unit": "MB",
            "why": "It is the largest measured regression in this trial.",
            "experiment": "Retain the parser change while restoring the old allocator.",
        }
        payload["outcome_analysis"] = "Parser improved.\n\x1b[31mAllocation regressed."
        source.write_text(json.dumps(payload), encoding="utf-8")

        self.cli(
            "report",
            "--candidate", str(packet["candidate"]),
            "--analysis-file", str(source),
        )

        report = Path(json.loads(self.cli("report").stdout)["report"]).read_text(
            encoding="utf-8"
        )
        self.assertIn("Execution: completed. Frontier outcome: discarded.", report)
        self.assertIn("Diagnostic confidence: inferred.", report)
        self.assertIn("Improvements: parser: 20 -&gt; 12 ms.", report)
        self.assertIn("Regressions: allocation: 3 -&gt; 5 MB.", report)
        self.assertIn("Preserved state: frontier remained", report)
        self.assertIn("Causal chain: improvement — parser", report)
        self.assertIn("remaining_bottleneck — allocation", report)
        history = self.cli("history").stdout
        self.assertNotIn("\x1b", history)
        self.assertIn("Parser improved. Allocation regressed.", history)
        self.assertIn("Diagnostic confidence: inferred.", history)
        self.assertIn("Causal chain: improvement — parser", history)
        self.assertIn("Preserved state: frontier remained", history)
        tsv = self.cli("history", "--format", "tsv").stdout
        reported = list(csv.DictReader(io.StringIO(tsv), delimiter="\t"))[-1]
        self.assertNotIn("\x1b", reported["description"])
        self.assertIn("Next: allocation at 5 MB.", reported["description"])
        self.assertIn("Improvements: parser: 20 -> 12 ms.", reported["description"])
        self.assertIn("Regressions: allocation: 3 -> 5 MB.", reported["description"])
        self.assertIn("Diagnostic confidence: inferred.", reported["description"])
        self.assertIn("Causal chain: improvement — parser", reported["description"])
        self.assertIn("Preserved state: frontier remained", reported["description"])

    def test_version_one_analysis_remains_accepted(self) -> None:
        self.init()
        packet = self.claim(1)[0]
        self.set_knob(packet, "a", 4)
        finish = json.loads(
            self.cli(
                "finish",
                "--candidate", str(packet["candidate"]),
                "--description", "lower a",
            ).stdout
        )
        source = self.analysis_file(finish)
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["schema_version"] = 1
        del payload["diagnostic_confidence"]
        del payload["cause_chain"]
        source.write_text(json.dumps(payload), encoding="utf-8")

        receipt = json.loads(
            self.cli(
                "report",
                "--candidate", str(packet["candidate"]),
                "--analysis-file", str(source),
            ).stdout
        )

        self.assertTrue(receipt["reported"])
        self.assertEqual(1, self.status()["candidate_reports"][0]["analysis"]["schema_version"])

    def test_version_two_cause_chain_must_reference_measured_observations(self) -> None:
        self.init()
        packet = self.claim(1)[0]
        self.set_knob(packet, "a", 4)
        finish = json.loads(
            self.cli(
                "finish",
                "--candidate", str(packet["candidate"]),
                "--description", "lower a",
            ).stdout
        )
        source = self.analysis_file(finish)
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["cause_chain"][0]["area"] = "unmeasured guess"
        source.write_text(json.dumps(payload), encoding="utf-8")

        rejected = self.cli(
            "report",
            "--candidate", str(packet["candidate"]),
            "--analysis-file", str(source),
            check=False,
        )

        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("must reference a measured observation", rejected.stderr)

    def test_version_two_rejects_ambiguous_or_unordered_causes(self) -> None:
        self.init()
        packet = self.claim(1)[0]
        self.set_knob(packet, "a", 4)
        finish = json.loads(
            self.cli(
                "finish",
                "--candidate", str(packet["candidate"]),
                "--description", "lower a",
            ).stdout
        )
        source = self.analysis_file(finish)
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["cause_chain"].append(
            {"area": "knob b", "role": "context", "why": "Appended after the bottleneck."}
        )
        source.write_text(json.dumps(payload), encoding="utf-8")

        unordered = self.cli(
            "report",
            "--candidate", str(packet["candidate"]),
            "--analysis-file", str(source),
            check=False,
        )
        self.assertNotEqual(0, unordered.returncode)
        self.assertIn("must end with next_focus", unordered.stderr)

        payload = json.loads(self.analysis_file(finish).read_text(encoding="utf-8"))
        payload["observations"].append(dict(payload["observations"][0]))
        source.write_text(json.dumps(payload), encoding="utf-8")
        ambiguous = self.cli(
            "report",
            "--candidate", str(packet["candidate"]),
            "--analysis-file", str(source),
            check=False,
        )
        self.assertNotEqual(0, ambiguous.returncode)
        self.assertIn("observation areas must be unique", ambiguous.stderr)

    def test_malformed_analysis_types_return_validation_errors_without_tracebacks(self) -> None:
        self.init()
        packet = self.claim(1)[0]
        self.set_knob(packet, "a", 4)
        finish = json.loads(
            self.cli(
                "finish",
                "--candidate", str(packet["candidate"]),
                "--description", "lower a",
            ).stdout
        )
        source = self.analysis_file(finish)
        payload = json.loads(source.read_text(encoding="utf-8"))

        for field, value in (("schema_version", []), ("effect", [])):
            malformed = dict(payload)
            if field == "effect":
                malformed["observations"] = [dict(payload["observations"][0])]
                malformed["observations"][0]["effect"] = value
            else:
                malformed[field] = value
            source.write_text(json.dumps(malformed), encoding="utf-8")
            rejected = self.cli(
                "report",
                "--candidate", str(packet["candidate"]),
                "--analysis-file", str(source),
                check=False,
            )
            self.assertNotEqual(0, rejected.returncode)
            self.assertNotIn("Traceback", rejected.stderr)
            self.assertIn("error:", rejected.stderr)

    def test_invalid_report_keeps_the_slot_and_evidence_limit_is_enforced(self) -> None:
        self.init()
        packet = self.claim(1)[0]
        self.set_knob(packet, "a", 4)
        finish = json.loads(
            self.cli(
                "finish",
                "--candidate", str(packet["candidate"]),
                "--description", "lower a",
            ).stdout
        )
        invalid = self.analysis_file(finish)
        payload = json.loads(invalid.read_text(encoding="utf-8"))
        payload["profiled_commit"] = "wrong-commit"
        invalid.write_text(json.dumps(payload), encoding="utf-8")

        mismatch = self.cli(
            "report",
            "--candidate", str(packet["candidate"]),
            "--analysis-file", str(invalid),
            check=False,
        )

        self.assertNotEqual(0, mismatch.returncode)
        self.assertIn("profiled_commit", mismatch.stderr)
        self.assertEqual("reporting", self.status()["parallel"]["slots"][0]["state"])

        payload["profiled_commit"] = finish["trial_commit"]
        payload["next_focus"]["current_value"] = 9
        invalid.write_text(json.dumps(payload), encoding="utf-8")
        unmeasured = self.cli(
            "report",
            "--candidate", str(packet["candidate"]),
            "--analysis-file", str(invalid),
            check=False,
        )
        self.assertNotEqual(0, unmeasured.returncode)
        self.assertIn("must match one observation", unmeasured.stderr)
        self.assertEqual("reporting", self.status()["parallel"]["slots"][0]["state"])

        invalid.write_text(" " * 16385, encoding="utf-8")
        oversized = self.cli(
            "report",
            "--candidate", str(packet["candidate"]),
            "--analysis-file", str(invalid),
            check=False,
        )
        self.assertNotEqual(0, oversized.returncode)
        self.assertIn("16,384 bytes", oversized.stderr)
        self.assertEqual("reporting", self.status()["parallel"]["slots"][0]["state"])

        payload = json.loads(self.analysis_file(finish).read_text(encoding="utf-8"))
        encoded = json.dumps(payload).encode("utf-8")
        payload["limitations"] += "x" * (16_384 - len(encoded))
        invalid.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(16_384, invalid.stat().st_size)
        accepted = json.loads(
            self.cli(
                "report",
                "--candidate", str(packet["candidate"]),
                "--analysis-file", str(invalid),
            ).stdout
        )
        self.assertTrue(accepted["reported"])

    def test_discarded_candidate_also_requires_and_persists_its_run_analysis(self) -> None:
        self.init()
        packet = self.claim(1)[0]
        self.set_knob(packet, "a", 11)

        finish = json.loads(
            self.cli(
                "finish",
                "--candidate", str(packet["candidate"]),
                "--description", "raise a",
            ).stdout
        )

        self.assertEqual("discarded", finish["outcome"])
        self.assertEqual("reporting", finish["status"])
        self.submit_analysis(finish, area="knob a regression")
        status = self.status()
        self.assertEqual(30, status["metric"]["current"])
        self.assertEqual(
            "knob a regression",
            status["candidate_reports"][0]["analysis"]["next_focus"]["area"],
        )
        self.assertEqual("idle", status["parallel"]["slots"][0]["state"])

    def test_concurrent_discarded_finishes_keep_the_event_log_valid(self) -> None:
        self.init()
        packets = self.claim(3)
        for packet, knob in zip(packets, ("a", "b", "c")):
            self.set_knob(packet, knob, 11)

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(
                pool.map(
                    lambda packet: self.cli(
                        "finish",
                        "--candidate", str(packet["candidate"]),
                        "--description", f"raise candidate {packet['candidate']}",
                        check=False,
                    ),
                    packets,
                )
            )

        self.assertTrue(all(result.returncode == 0 for result in results), results)
        status = self.status()
        self.assertEqual([1, 2, 3], status["parallel"]["reporting_candidates"])
        self.assertEqual(30, status["metric"]["current"])

    def test_finish_accepts_concurrent_controller_event(self) -> None:
        (self.repo / "score.py").write_text(
            "import time\n"
            "from pathlib import Path\n"
            "time.sleep(1.5)\n"
            "print(sum(int(Path(f'src/{n}.txt').read_text().strip()) for n in 'abc'))\n",
            encoding="utf-8",
        )
        self.git("add", "score.py")
        self.git("commit", "-m", "slow scorer")
        self.init()
        packet = self.claim(1)[0]
        self.set_knob(packet, "a", 4)

        with ThreadPoolExecutor(max_workers=2) as pool:
            finish = pool.submit(
                self.cli,
                "finish",
                "--candidate",
                str(packet["candidate"]),
                "--description",
                "lower a during concurrent decision",
                check=False,
            )
            for _ in range(100):
                if self.status()["parallel"]["slots"][0]["state"] == "measuring":
                    break
                time.sleep(0.01)
            else:
                self.fail("candidate did not enter measuring state")
            decision = pool.submit(
                self.cli, "decide", "--add", "concurrent controller event"
            )
            decision.result(timeout=0.5)
            self.assertFalse(finish.done())
            result = finish.result()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("admitted", json.loads(result.stdout)["outcome"])

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS sandbox_init")
    def test_finish_allows_scorer_to_sandbox_its_candidate_child(self) -> None:
        child = (
            "import ctypes; library=ctypes.CDLL('/usr/lib/libsandbox.1.dylib'); "
            "library.sandbox_init.argtypes=[ctypes.c_char_p,ctypes.c_uint64,"
            "ctypes.POINTER(ctypes.c_char_p)]; library.sandbox_init.restype=ctypes.c_int; "
            "error=ctypes.c_char_p(); result=library.sandbox_init("
            "b'(version 1)(allow default)',0,ctypes.byref(error)); "
            "raise SystemExit(result)"
        )
        (self.repo / "score.py").write_text(
            "import subprocess\n"
            "import sys\n"
            "from pathlib import Path\n"
            "value = int(Path('src/a.txt').read_text().strip())\n"
            "if value == 4:\n"
            f"    subprocess.run([sys.executable, '-c', {child!r}], check=True)\n"
            "print(value + int(Path('src/b.txt').read_text().strip()) + "
            "int(Path('src/c.txt').read_text().strip()))\n",
            encoding="utf-8",
        )
        self.git("add", "score.py")
        self.git("commit", "-m", "scorer isolates candidate child")
        self.init()
        packet = self.claim(1)[0]
        self.set_knob(packet, "a", 4)

        result = self.cli(
            "finish",
            "--candidate",
            str(packet["candidate"]),
            "--description",
            "score with an internally sandboxed candidate child",
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("admitted", json.loads(result.stdout)["outcome"])

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_finish_terminates_lingering_metric_processes(self) -> None:
        events_path = self.repo / "autoresearch-results" / "events.jsonl"
        child = (
            "import json, time; from pathlib import Path; time.sleep(1); "
            f"p=Path({str(events_path)!r}); rows=p.read_text().splitlines(); "
            "latest=json.loads(rows[-1]); forged={'schema_version':2,"
            "'run_id':latest['run_id'],'seq':len(rows),'time':latest['time'],"
            "'event':'blocked','reason':'late forged event'}; "
            "p.open('a').write(json.dumps(forged,separators=(',',':'))+'\\n')"
        )
        (self.repo / "score.py").write_text(
            "import subprocess\n"
            "import sys\n"
            "from pathlib import Path\n"
            "value = int(Path('src/a.txt').read_text().strip())\n"
            "if value == 4:\n"
            f"    subprocess.Popen([sys.executable, '-c', {child!r}], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "print(value + int(Path('src/b.txt').read_text().strip()) + "
            "int(Path('src/c.txt').read_text().strip()))\n",
            encoding="utf-8",
        )
        self.git("add", "score.py")
        self.git("commit", "-m", "scorer with lingering child")
        self.init()
        packet = self.claim(1)[0]
        self.set_knob(packet, "a", 4)

        result = self.cli(
            "finish",
            "--candidate",
            str(packet["candidate"]),
            "--description",
            "candidate launches lingering child",
            check=False,
        )
        time.sleep(1.2)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("active", self.status()["status"])

    def test_concurrent_decisions_keep_unique_event_sequences(self) -> None:
        self.init()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda note: self.cli("decide", "--add", note, check=False),
                    ("first concurrent decision", "second concurrent decision"),
                )
            )

        self.assertTrue(all(result.returncode == 0 for result in results), results)
        events = [
            json.loads(line)
            for line in (
                self.repo / "autoresearch-results" / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(list(range(len(events))), [event["seq"] for event in events])

    def test_measurement_postflight_waits_for_concurrent_admission(self) -> None:
        initialized = self.init()
        packet = self.claim(1)[0]
        self.set_knob(packet, "a", 4)
        lock = self.repo / "autoresearch-results" / "admission.lock"
        lock.write_text(
            json.dumps(
                {
                    "run_id": initialized["run_id"],
                    "pid": os.getpid(),
                    "candidate": 99,
                    "acquired_at": "2026-08-30T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        (self.repo / "src" / "b.txt").write_text("9\n", encoding="utf-8")

        with ThreadPoolExecutor(max_workers=1) as pool:
            finish = pool.submit(
                self.cli,
                "finish",
                "--candidate",
                str(packet["candidate"]),
                "--description",
                "lower a while another admission completes",
                check=False,
            )
            for _ in range(100):
                if self.status()["parallel"]["slots"][0]["state"] == "measuring":
                    break
                time.sleep(0.01)
            else:
                self.fail("candidate did not enter measuring state")
            time.sleep(0.1)
            (self.repo / "src" / "b.txt").write_text("10\n", encoding="utf-8")
            lock.unlink()
            result = finish.result()

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("admitted", json.loads(result.stdout)["outcome"])

    def test_in_flight_candidate_can_resolve_after_target_is_reached(self) -> None:
        self.init("--target", "20")
        first, second = self.claim(2)
        self.set_knob(first, "a", 0)
        self.set_knob(second, "b", 9)

        first_finish = json.loads(
            self.cli(
                "finish",
                "--candidate", str(first["candidate"]),
                "--description", "reach target",
            ).stdout
        )
        second_finish = json.loads(
            self.cli(
                "finish",
                "--candidate", str(second["candidate"]),
                "--description", "finish already claimed work",
            ).stdout
        )
        first_report = self.submit_analysis(first_finish)
        self.assertEqual("active", first_report["status"])
        second_report = self.submit_analysis(second_finish)

        self.assertEqual("complete", second_report["status"])
        self.assertEqual([], self.status()["parallel"]["unresolved_candidates"])

    def test_foreground_finish_is_rejected_while_candidate_reports_are_pending(self) -> None:
        self.init()
        packet = self.claim(1)[0]
        self.set_knob(packet, "a", 4)
        finish = json.loads(
            self.cli(
                "finish",
                "--candidate", str(packet["candidate"]),
                "--description", "lower a",
            ).stdout
        )
        (self.repo / "src" / "c.txt").write_text("9\n", encoding="utf-8")

        foreground = self.cli("finish", "--description", "foreground", check=False)

        self.assertNotEqual(0, foreground.returncode)
        self.assertIn("in-flight candidates", foreground.stderr)
        (self.repo / "src" / "c.txt").write_text("10\n", encoding="utf-8")
        self.assertTrue(self.submit_analysis(finish)["reported"])

    def test_reaping_one_missing_report_closes_all_reporting_slots(self) -> None:
        self.init(lease="1")
        first, second = self.claim(2)
        self.set_knob(first, "a", 11)
        self.set_knob(second, "b", 11)
        for packet in (first, second):
            self.cli(
                "finish",
                "--candidate", str(packet["candidate"]),
                "--description", "discard",
            )
        subprocess.run([sys.executable, "-c", "import time; time.sleep(2)"], check=True)

        self.cli("reap", "--candidate", str(first["candidate"]))

        status = self.status()
        self.assertEqual("error", status["status"])
        self.assertEqual(0, status["parallel"]["grants_held"])
        self.assertTrue(all(slot["state"] == "idle" for slot in status["parallel"]["slots"]))
        later = self.cli("reap", "--candidate", str(second["candidate"]), check=False)
        self.assertNotEqual(0, later.returncode)
        self.assertIn("run status is error", later.stderr)

    def test_report_rejects_candidate_zero_instead_of_generating_html(self) -> None:
        self.init()
        source = Path(self.temp.name) / "analysis.json"
        source.write_text("{}", encoding="utf-8")

        result = self.cli(
            "report",
            "--candidate", "0",
            "--analysis-file", str(source),
            check=False,
        )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("positive integer", result.stderr)

    def test_report_and_claim_do_not_overwrite_each_others_slot_updates(self) -> None:
        self.init()
        first = self.claim(1)[0]
        self.set_knob(first, "a", 4)
        finish = json.loads(
            self.cli(
                "finish",
                "--candidate", str(first["candidate"]),
                "--description", "lower a",
            ).stdout
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            report_future = pool.submit(self.submit_analysis, finish)
            claim_future = pool.submit(self.claim, 1)
            report_future.result()
            claimed = claim_future.result()

        self.assertEqual(1, len(claimed))
        status = self.status()
        self.assertEqual(1, status["parallel"]["grants_held"])
        self.assertEqual([], status["parallel"]["reporting_candidates"])
        self.assertEqual([claimed[0]["candidate"]], status["parallel"]["unresolved_candidates"])

    def test_replay_rejects_completion_before_required_report(self) -> None:
        initialized = self.init("--target", "24")
        packet = self.claim(1)[0]
        self.set_knob(packet, "a", 4)
        finish = json.loads(
            self.cli(
                "finish",
                "--candidate", str(packet["candidate"]),
                "--description", "reach target",
            ).stdout
        )
        events_path = self.repo / "autoresearch-results" / "events.jsonl"
        lines = events_path.read_text(encoding="utf-8").splitlines()
        lines.append(
            json.dumps(
                {
                    "schema_version": 2,
                    "run_id": initialized["run_id"],
                    "seq": len(lines),
                    "time": "2026-08-29T00:00:00Z",
                    "event": "complete",
                    "reason": "tampered premature completion",
                    "head": finish["head"],
                    "metric": 24,
                    "unresolved_candidates": [],
                }
            )
        )
        events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        status = self.cli("status", check=False)

        self.assertNotEqual(0, status.returncode)
        self.assertIn("cannot precede all candidate resolutions and reports", status.stderr)

    def test_missing_report_expires_to_error_without_rolling_back_admission(self) -> None:
        self.init(lease="1")
        packet = self.claim(1)[0]
        self.set_knob(packet, "a", 4)
        finish = json.loads(
            self.cli(
                "finish",
                "--candidate", str(packet["candidate"]),
                "--description", "lower a",
            ).stdout
        )
        admitted_head = finish["head"]
        subprocess.run([sys.executable, "-c", "import time; time.sleep(2)"], check=True)

        reaped = json.loads(
            self.cli("reap", "--candidate", str(packet["candidate"])).stdout
        )

        self.assertEqual("missing_report", reaped["reason"])
        status = self.status()
        self.assertEqual("error", status["status"])
        self.assertEqual(admitted_head, status["head"])
        self.assertEqual(24, status["metric"]["current"])
        self.assertEqual("idle", status["parallel"]["slots"][0]["state"])

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

        self.finish_and_report(first, "lower a")
        self.assertEqual(24, self.status()["metric"]["current"])

        # The second candidate measured 23 in isolation. After rebasing onto the moved
        # frontier it must be re-measured as 17, which is what it actually lands on.
        result = json.loads(
            self.finish_and_report(second, "lower b").stdout
        )
        self.assertEqual("admitted", result["outcome"])
        self.assertEqual(17, result["trial_metric"])
        self.assertEqual(17, self.status()["metric"]["current"])
        self.assertNotIn("Revert", self.git("log", "--format=%s").stdout)

    def test_decision_waits_for_stale_candidate_remeasurement(self) -> None:
        rebase_started = Path(self.temp.name) / "rebase-started"
        (self.repo / "score.py").write_text(
            "import time\n"
            "from pathlib import Path\n"
            "total = sum(int(Path(f'src/{n}.txt').read_text().strip()) for n in 'abc')\n"
            f"marker = Path({str(rebase_started)!r})\n"
            "if total == 17:\n"
            "    marker.write_text('started')\n"
            "time.sleep(0.5)\n"
            "print(total)\n",
            encoding="utf-8",
        )
        self.git("add", "score.py")
        self.git("commit", "-m", "slow scorer")
        self.init()
        first, second = self.claim(2)
        self.set_knob(first, "a", 4)
        self.set_knob(second, "b", 3)
        self.finish_and_report(first, "lower a")

        with ThreadPoolExecutor(max_workers=2) as pool:
            finish = pool.submit(
                self.cli,
                "finish",
                "--candidate",
                str(second["candidate"]),
                "--description",
                "lower b during concurrent decision",
                check=False,
            )
            for _ in range(200):
                if rebase_started.exists():
                    break
                time.sleep(0.01)
            else:
                self.fail("candidate did not enter stale remeasurement")
            decision = pool.submit(
                self.cli, "decide", "--add", "decision during stale remeasurement"
            )
            time.sleep(0.1)
            self.assertFalse(decision.done())
            result = finish.result()
            decision.result()

        self.assertEqual(0, result.returncode, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual("admitted", receipt["outcome"])

    def test_parallel_finishes_serialize_on_the_admission_lock(self) -> None:
        self.init()
        packets = self.claim(3)
        for packet, knob in zip(packets, ("a", "b", "c")):
            self.set_knob(packet, knob, 1)

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(
                pool.map(
                    lambda packet: self.finish_and_report(
                        packet,
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
            self.finish_and_report(packet, f"zero {knob}")
        status = self.status()
        self.assertEqual("complete", status["status"])
        self.assertEqual(0, status["metric"]["current"])


if __name__ == "__main__":
    unittest.main()
