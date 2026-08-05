# Parallel Autoresearch — Phase 1 Implementation Plan (Teardown & Extraction)

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete background mode, collapse `run.json` to the schema-2 shape, and extract state validation into its own module — leaving a smaller, green, sequential autoresearch that Phase 2 can build parallelism on.

**Architecture:** Pure subtraction and one extraction. No new behavior. Background mode (`launch`, `stop`, the detached controller, `runtime.json`, `runtime.log`) is removed because Phase 2 replaces it with coordinator-spawned subagents. `run.json` loses `mode` and `background` and renames `max_iterations` → `max_candidates`. `derive_state` and the run/event schemas move from `autoresearch_core.py` into a new `autoresearch_state.py`, so Phase 2 can grow the event model without pushing core past readable size.

**Tech Stack:** Python 3.11 (CI-pinned; do not use 3.12+ APIs), stdlib only, `unittest` (not pytest), `Decimal` for all metric arithmetic, real `git` subprocesses in `tempfile` dirs for tests.

**Spec:** [2026-08-05-parallel-autoresearch-design.md](../specs/2026-08-05-parallel-autoresearch-design.md)

---

## Before You Start

**Commits:** Nathan makes all commits. The commit steps below are written for a human to run, or for an agent that has been explicitly authorized to commit on a feature branch. Never commit to `main` — it is a protected branch.

**Schema version:** This phase sets `SCHEMA_VERSION = 2`. Phase 2 continues evolving the v2 shape on the same feature branch. Only one bump reaches `main`, so intermediate v2 shapes inside the branch are expected and fine.

**Baseline to preserve:** `python3 -m unittest discover -s tests -q` currently reports `Ran 36 tests ... OK` in roughly 38 seconds. After this phase it will report **26 tests**, because 10 background tests are deleted. Every task must leave the suite green.

**Verification command used throughout:**

```bash
python3 -m unittest discover -s tests -q
```

---

## File Structure

| File | Responsibility after Phase 1 |
|---|---|
| `scripts/autoresearch_core.py` | Atomic IO, JSON strictness, git primitives, command execution, metric parsing. **Loses** all schema/replay/runtime code. |
| `scripts/autoresearch_state.py` | **New.** `SCHEMA_VERSION`, `RUN_KEYS`, `EVENT_FIELDS`, `validate_run`, `validate_event`, `load_run`, `load_events`, `derive_state`, `load_context`, `append_event`, `status_payload`. |
| `scripts/autoresearch.py` | CLI dispatch only. **Loses** ~600 lines of controller and background code. |
| `scripts/autoresearch_report.py` | History table, TSV, HTML. Updated for the schema-2 field names. |
| `references/parallel.md` | Created in Phase 3. Not in this phase. |
| `deprecated/background_2026_08_05.md` | **New.** The retired `references/background.md`. |

---

## Task 1: Create the feature branch

**Files:** none

- [ ] **Step 1: Confirm a clean tree on `main`**

```bash
git -C /Users/nathanballou/Documents/codex-autoresearch status --porcelain
```

Expected: only the two untracked doc files from the design phase (`docs/agent-workflows/`). If anything else appears, stop and resolve it first.

- [ ] **Step 2: Create and switch to the feature branch**

```bash
git -C /Users/nathanballou/Documents/codex-autoresearch checkout -b feat/parallel-autoresearch
```

Expected: `Switched to a new branch 'feat/parallel-autoresearch'`

- [ ] **Step 3: Verify you are off the protected branch**

```bash
git -C /Users/nathanballou/Documents/codex-autoresearch rev-parse --abbrev-ref HEAD
```

Expected: `feat/parallel-autoresearch`

- [ ] **Step 4: Commit the design and plan documents**

```bash
git add docs/agent-workflows
git commit -m "docs: add parallel autoresearch design and phase 1 plan

Co-authored-by: Claude <noreply@anthropic.com>"
```

---

## Task 2: Delete the background tests

Deleting the tests first means every later task in this phase is verified by a suite that no longer expects background behavior.

**Files:**
- Modify: `tests/test_autoresearch.py`

- [ ] **Step 1: Delete these ten test methods**

Delete each method in full, from its `def` line through the line before the next `def`:

| Line (before edits) | Method |
|---|---|
| 364 | `test_foreground_config_omits_background_only_options` |
| 617 | `test_background_controller_runs_multiple_real_helper_iterations` |
| 642 | `test_background_worker_without_event_fails_fast` |
| 655 | `test_background_launch_surfaces_missing_codex_binary` |
| 746 | `test_background_stop_terminates_sleeping_worker` |
| 773 | `test_controller_error_terminates_active_worker` |
| 805 | `test_controller_start_failure_terminates_before_state_diagnostics` |
| 837 | `test_controller_spawn_failure_records_terminal_error` |
| 875 | `test_stop_accepts_run_that_completes_during_stop_race` |
| 936 | `test_live_orphaned_worker_blocks_control_transitions` |

Work bottom-up (line 936 first) so earlier line numbers stay valid.

- [ ] **Step 2: Drop the `mode` parameter from the `init` helper**

Replace the helper at lines 68-87 exactly:

```python
    def init(self, *extra: str) -> dict:
        completed = self.cli(
            "init",
            "--repo",
            str(self.repo),
            "--goal",
            "Reduce the value to zero",
            "--scope",
            "src",
            "--metric-name",
            "value",
            "--direction",
            "lower",
            "--verify",
            "python3 score.py",
            "--target",
            "0",
            *extra,
        )
        return json.loads(completed.stdout)
```

Then delete the `write_test_worker` helper method in full — it existed only to build fake `codex exec` workers for the deleted controller tests.

- [ ] **Step 3: Rename the iteration-limit test flag**

At line 602, change:

```python
        self.init("--max-iterations", "1")
```

to:

```python
        self.init("--max-candidates", "1")
```

- [ ] **Step 4: Remove unused imports**

`threading` and `time` were used only by the deleted controller tests. Check whether any remaining test uses them:

```bash
grep -n "threading\.\|time\." tests/test_autoresearch.py
```

Delete the `import threading` and/or `import time` lines for any that returns no hits.

- [ ] **Step 5: Confirm the suite fails for the right reason**

```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -20
```

Expected: FAIL. The remaining 26 tests still pass, but `test_iteration_limit_stops_without_claiming_completion` fails with a `--max-candidates` argparse error, because the CLI flag is not renamed until Task 5. This is the expected red state.

- [ ] **Step 6: Commit**

```bash
git add tests/test_autoresearch.py
git commit -m "test: remove background mode test coverage

Co-authored-by: Claude <noreply@anthropic.com>"
```

---

## Task 3: Remove the runtime smoke test and its CI job

**Files:**
- Modify: `scripts/run_skill_e2e.sh`
- Modify: `.github/workflows/ci.yml:30-48`

- [ ] **Step 1: Delete the runtime-smoke shell functions**

In `scripts/run_skill_e2e.sh`, delete these functions in full: `write_test_worker`, `wait_for_terminal_status`, `run_runtime_smoke`, and `run_real_background`.

- [ ] **Step 2: Delete the runtime-smoke dispatch cases**

In the `case` statement near line 387, delete the `runtime-smoke)` branch and any `real-background)` branch, keeping `foreground-smoke)` and `real-foreground)`.

- [ ] **Step 3: Update the usage text**

Delete these two lines from `usage()`:

```
  bash scripts/run_skill_e2e.sh runtime-smoke [--clean]
```

```
  runtime-smoke     Deterministic two-worker detached controller run with a local test worker.
```

- [ ] **Step 4: Delete the CI smoke job's runtime step**

In `.github/workflows/ci.yml`, delete these two lines (46-48):

```yaml
      - name: Run background runtime smoke
        run: bash scripts/run_skill_e2e.sh runtime-smoke --clean
```

- [ ] **Step 5: Delete the runtime-smoke call from the contributor gate**

In `scripts/run_contributor_gate.sh`, delete line 16 from the `skill)` branch:

```bash
    bash "$ROOT/scripts/run_skill_e2e.sh" runtime-smoke --clean
```

The `skill)` branch then ends with the `foreground-smoke` call.

- [ ] **Step 6: Verify the foreground smoke still passes**

```bash
bash scripts/run_skill_e2e.sh foreground-smoke --clean
```

Expected: exits 0. This still exercises the pre-rename CLI, so it must pass before any script edits land.

- [ ] **Step 7: Commit**

```bash
git add scripts/run_skill_e2e.sh scripts/run_contributor_gate.sh .github/workflows/ci.yml
git commit -m "ci: remove background runtime smoke test

Co-authored-by: Claude <noreply@anthropic.com>"
```

---

## Task 4: Retire `references/background.md`

**Files:**
- Create: `deprecated/background_2026_08_05.md`
- Delete: `references/background.md`
- Modify: `SKILL.md:20`
- Modify: `CONTRIBUTING.md:13`
- Modify: `tests/test_structure.py:18`
- Modify: `scripts/validate_skill_structure.sh:14,35-39`

- [ ] **Step 1: Write the failing structure test**

In `tests/test_structure.py`, change the reference assertion at line 18 to the two-file set this phase leaves behind:

```python
    def test_reference_surface_is_intentionally_small(self) -> None:
        references = sorted(path.name for path in (ROOT / "references").glob("*.md"))
        self.assertEqual(["experiment.md", "workflow.md"], references)
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for reference in references:
            self.assertIn(f"references/{reference}", skill)
```

- [ ] **Step 2: Run it to verify it fails**

```bash
python3 -m unittest discover -s tests -p 'test_structure.py' -q
```

Expected: FAIL with `Lists differ: ['experiment.md', 'workflow.md'] != ['background.md', 'experiment.md', 'workflow.md']`

- [ ] **Step 3: Move the file to `deprecated/`**

```bash
mkdir -p deprecated
git mv references/background.md deprecated/background_2026_08_05.md
```

- [ ] **Step 4: Remove the SKILL.md and CONTRIBUTING.md references**

Delete line 20 of `SKILL.md`:

```
- Read `references/background.md` only for a background run.
```

Then delete line 13 of `CONTRIBUTING.md` from its architecture tree, and change the `└──` connector on the preceding line so the tree still renders correctly:

```text
SKILL.md
├── references/workflow.md
└── references/experiment.md
```

- [ ] **Step 5: Update the shell validator**

In `scripts/validate_skill_structure.sh`, delete this line from the `required` array:

```
  "$ROOT/references/background.md"
```

and change the reference count check from 3 to 2:

```bash
reference_count="$(find "$ROOT/references" -maxdepth 1 -type f -name '*.md' | wc -l | tr -d ' ')"
if [[ "$reference_count" -ne 2 ]]; then
  echo "Expected exactly 2 model references, found $reference_count" >&2
  exit 1
fi
```

Also update the final echo so its text matches reality:

```bash
echo "Skill structure valid: $skill_bytes-byte SKILL.md, 2 references, 3 runtime modules."
```

- [ ] **Step 6: Run both validators to verify they pass**

```bash
python3 -m unittest discover -s tests -p 'test_structure.py' -q && bash scripts/validate_skill_structure.sh
```

Expected: `OK`, then `Skill structure valid: ...`

- [ ] **Step 7: Commit**

```bash
git add SKILL.md references deprecated tests/test_structure.py scripts/validate_skill_structure.sh
git commit -m "docs: retire background runtime reference

Co-authored-by: Claude <noreply@anthropic.com>"
```

---

## Task 5: Collapse `run.json` to the schema-2 shape

Drops `mode` and `background`, renames `max_iterations` → `max_candidates`, bumps `SCHEMA_VERSION` to 2.

**Files:**
- Modify: `scripts/autoresearch_core.py:19,218-304,576`
- Modify: `scripts/autoresearch.py:73-98,258-380,713,734`
- Modify: `scripts/autoresearch_report.py:189,421,482`
- Test: `tests/test_autoresearch.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_autoresearch.py`:

```python
    def test_schema_two_run_has_no_mode_or_background(self) -> None:
        self.init()
        run = json.loads(
            (self.repo / "autoresearch-results" / "run.json").read_text(encoding="utf-8")
        )
        self.assertEqual(2, run["schema_version"])
        self.assertNotIn("mode", run)
        self.assertNotIn("background", run)
        self.assertIn("max_candidates", run)
        self.assertNotIn("max_iterations", run)
```

- [ ] **Step 2: Run it to verify it fails**

```bash
python3 -m unittest tests.test_autoresearch.AutoresearchTest.test_schema_two_run_has_no_mode_or_background -v
```

Expected: FAIL with `1 != 2` on the schema version assertion.

- [ ] **Step 3: Update the core schema**

In `scripts/autoresearch_core.py`, set line 19:

```python
SCHEMA_VERSION = 2
```

Replace `RUN_KEYS` (lines 218-233) and delete `BACKGROUND_KEYS` (line 235):

```python
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
```

- [ ] **Step 4: Update `validate_run`**

In the same file, change the string-field loop at line 247 to drop `mode`:

```python
    for key in ("run_id", "created_at", "repo", "branch", "goal"):
```

Delete the mode check (lines 250-251) and the entire `background` block (lines 286-303). Rename the iteration-limit check (lines 274-279):

```python
    if payload["max_candidates"] is not None and (
        not isinstance(payload["max_candidates"], int)
        or isinstance(payload["max_candidates"], bool)
        or payload["max_candidates"] <= 0
    ):
        raise AutoresearchError(f"{source}.max_candidates must be null or a positive integer")
```

- [ ] **Step 5: Update `derive_state` and `status_payload`**

At line 576, change `run["max_iterations"]` to `run["max_candidates"]` in both places:

```python
        if run["max_candidates"] is not None and iterations >= run["max_candidates"]:
```

In `status_payload`, delete the `"mode": run["mode"],` entry.

- [ ] **Step 6: Update the CLI argument parser**

In `scripts/autoresearch.py`, change `add_run_arguments` to drop the `background` parameter entirely:

```python
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
```

In `build_parser`, change the `init` registration and delete the `set_defaults(mode=...)` line:

```python
    init_parser = subparsers.add_parser("init", help="Validate and initialize a run.")
    add_run_arguments(init_parser)
```

- [ ] **Step 7: Update `initialize_run`**

In `scripts/autoresearch.py`, delete the `codex_bin`/`model` locals (lines 270-271) and the entire `if args.mode == "background":` block (lines 282-292). Change the limit validation:

```python
    if args.max_candidates is not None and args.max_candidates <= 0:
        raise AutoresearchError("--max-candidates must be a positive integer")
```

Replace the run dictionary construction so it has no `mode` or `background`:

```python
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
```

Delete `"mode": args.mode,` from both return dictionaries (lines 358 and 410).

- [ ] **Step 8: Update `finish_iteration`**

At line 713, rename the limit check:

```python
    elif run["max_candidates"] is not None and iteration >= run["max_candidates"]:
```

At line 734, replace the mode-conditional instruction with the unconditional one:

```python
        "instruction": "Continue with the next distinct experiment unless complete.",
```

- [ ] **Step 9: Update the report module**

In `scripts/autoresearch_report.py`, line 189, drop the mode field:

```python
        f"Run: {run['run_id'][:8]}  Status: {state.status}",
```

Line 421:

```python
    iteration_limit = run["max_candidates"] if run["max_candidates"] is not None else "Unlimited"
```

Line 482: delete the `<span>{_escape(run['mode'])}</span>` element and its surrounding label row in the HTML metadata table.

- [ ] **Step 10: Run the new test to verify it passes**

```bash
python3 -m unittest tests.test_autoresearch.AutoresearchTest.test_schema_two_run_has_no_mode_or_background -v
```

Expected: PASS

- [ ] **Step 11: Commit**

```bash
git add scripts/autoresearch_core.py scripts/autoresearch.py scripts/autoresearch_report.py tests/test_autoresearch.py
git commit -m "feat!: collapse run.json to schema 2 without background mode

Drops mode and background, renames max_iterations to max_candidates.
Schema 1 runs are refused and must be archived.

Co-authored-by: Claude <noreply@anthropic.com>"
```

---

## Task 6: Delete the controller and background commands from the CLI

**Files:**
- Modify: `scripts/autoresearch.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_autoresearch.py`:

```python
    def test_background_subcommands_are_gone(self) -> None:
        for command in ("launch", "stop", "_controller"):
            result = self.cli(command, "--repo", str(self.repo), check=False)
            self.assertNotEqual(0, result.returncode, f"{command} should not exist")
            self.assertIn("invalid choice", result.stderr)
```

- [ ] **Step 2: Run it to verify it fails**

```bash
python3 -m unittest tests.test_autoresearch.AutoresearchTest.test_background_subcommands_are_gone -v
```

Expected: FAIL — `launch` currently exists, so `invalid choice` is absent from stderr.

- [ ] **Step 3: Delete the subparser registrations**

In `build_parser`, delete the `launch_parser`, `stop_parser`, and `controller_parser` blocks in full.

- [ ] **Step 4: Delete the controller and background functions**

Delete these functions from `scripts/autoresearch.py` in full:

`runtime_event`, `write_stop_request`, `consume_stop_request`, `worker_prompt`, `controller_command`, `record_controller_error`, `run_controller`, `controller_entry`, `fail_controller_start`, `fail_controller_spawn`, `spawn_controller`, `launch_background`, `stop_background`.

- [ ] **Step 5: Simplify `resume_run`**

Delete the background branch (lines 1479-1491) so the active-run guard reads:

```python
    if state.status == "active":
        raise AutoresearchError("Run is already active")
```

Delete the stop-request cleanup at lines 1495-1496 and the background return at lines 1506-1507, leaving one return:

```python
    return {
        "status": "active",
        "note": event["note"],
        "instruction": "Reuse or create the matching Goal, then continue the experiment loop.",
    }
```

- [ ] **Step 6: Simplify `archive_run`**

Delete the `if paths.runtime.exists():` block (lines 1521-1528) in full. Archiving no longer has a controller to check for.

- [ ] **Step 7: Update `main` dispatch**

Delete the `launch`, `stop`, and `_controller` branches from `main()`.

- [ ] **Step 8: Prune the now-unused imports**

```bash
grep -n "^import \|^from \|shlex\.\|signal\.\|shutil\.\|process_alive\|terminate_process_tree\|load_runtime\|write_runtime\|append_json_line\|NoReturn" scripts/autoresearch.py
```

Delete any import whose only remaining hits are the import line itself. `shlex` and `signal` become unused; `shutil` is still used by `archive_run`.

- [ ] **Step 9: Run the full suite to verify green**

```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
```

Expected: `Ran 28 tests ... OK` (26 surviving plus the two added in Tasks 5 and 6).

- [ ] **Step 10: Commit**

```bash
git add scripts/autoresearch.py tests/test_autoresearch.py
git commit -m "feat!: delete detached controller and background commands

Co-authored-by: Claude <noreply@anthropic.com>"
```

---

## Task 7: Delete runtime state tracking from core

**Files:**
- Modify: `scripts/autoresearch_core.py:23-24,37-47,74-86,1093-1183,1186-1213`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_autoresearch.py`:

```python
    def test_status_has_no_runtime_section(self) -> None:
        self.init()
        status = json.loads(
            self.cli("status", "--repo", str(self.repo)).stdout
        )
        self.assertNotIn("runtime", status)
        self.assertNotIn("runtime_log", status)
        self.assertFalse((self.repo / "autoresearch-results" / "runtime.json").exists())
```

- [ ] **Step 2: Run it to verify it fails**

```bash
python3 -m unittest tests.test_autoresearch.AutoresearchTest.test_status_has_no_runtime_section -v
```

Expected: FAIL — `status` still emits a `runtime` key.

- [ ] **Step 3: Delete the runtime constants and path fields**

In `scripts/autoresearch_core.py`, delete lines 23-24:

```python
RUNTIME_FILE = "runtime.json"
RUNTIME_LOG = "runtime.log"
```

Trim the `Paths` dataclass to:

```python
@dataclass(frozen=True)
class Paths:
    repo: Path
    root: Path
    run: Path
    events: Path
    logs: Path
```

Trim `paths_for` to match:

```python
def paths_for(repo: Path | str) -> Paths:
    resolved = Path(repo).expanduser().resolve()
    root = resolved / RESULTS_DIR
    return Paths(
        repo=resolved,
        root=root,
        run=root / RUN_FILE,
        events=root / EVENTS_FILE,
        logs=root / "logs",
    )
```

- [ ] **Step 4: Delete the runtime functions**

Delete `load_runtime`, `write_runtime`, and `runtime_snapshot` in full — a contiguous range, lines 1070-1133 as of commit `033d2e0`.

Also delete `process_alive` (lines 977-988). Task 6 removed its last caller when it deleted `archive_run`'s controller-liveness guard, so this change orphaned it. Per the repo convention — remove what your change orphaned, report pre-existing dead code rather than deleting it — it goes now. A later phase that needs PID liveness checking can reintroduce it at that point rather than carrying it unused.

Keep `process_group_alive` and `terminate_process_tree`. Both are still live: `terminate_process_tree` is called from `run_command`'s timeout path at line 853, and it calls `process_group_alive` five times. Keep `os` too — core uses it 19 times for atomic writes.

- [ ] **Step 4a: Fix the last stale "iteration limit" string**

`autoresearch_core.py:555` raises `"Active event history reached the iteration limit but lacks a stopped event"`. Task 6 renamed the other three user-facing occurrences to "candidate limit" to match `--max-candidates`, but could not touch this file. Change `iteration limit` to `candidate limit` here.

This is an error message, not an event `reason` value, and `derive_state` never compares reason strings — it compares `max_candidates` numerically — so no replay behavior depends on the wording.

Leave the `ITER` column header in `autoresearch_report.py` and the `iteration` event type alone. The event is still literally named `iteration` until a later phase, so renaming those now would trade one inconsistency for a worse one.

- [ ] **Step 5: Trim `status_payload`**

Delete the `"runtime"` and `"runtime_log"` entries. The function ends:

```python
        "iterations": state.iterations,
        "head": state.head,
        "last_event": state.last_event,
        "events_path": str(paths.events),
        "event_count": len(events),
    }
```

- [ ] **Step 6: Remove the stale imports in the CLI**

`load_runtime` and `write_runtime` are imported at `scripts/autoresearch.py:36,59`. Delete both names from the import list.

- [ ] **Step 7: Run the full suite to verify green**

```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
```

Expected: `Ran 29 tests ... OK`

- [ ] **Step 8: Commit**

```bash
git add scripts/autoresearch_core.py scripts/autoresearch.py tests/test_autoresearch.py
git commit -m "refactor!: remove runtime.json process tracking

Co-authored-by: Claude <noreply@anthropic.com>"
```

---

## Task 8: Extract `autoresearch_state.py`

Moves schema and replay out of core so Phase 2 can grow the event model without pushing `autoresearch_core.py` past readable size.

**Files:**
- Create: `scripts/autoresearch_state.py`
- Modify: `scripts/autoresearch_core.py`
- Modify: `scripts/autoresearch.py:19-61`
- Modify: `scripts/autoresearch_report.py`
- Modify: `tests/test_structure.py:23-28`
- Modify: `scripts/validate_skill_structure.sh:14-19,41-45,66-69`
- Modify: `scripts/run_skill_e2e.sh:56-60`
- Modify: `CONTRIBUTING.md:15-17`

- [ ] **Step 1: Write the failing structure test**

Replace `test_runtime_surface_is_intentionally_small` in `tests/test_structure.py` with an exact-name assertion, so adding a module stays a conscious edit:

```python
    def test_runtime_surface_is_intentionally_small(self) -> None:
        scripts = {path.name for path in (ROOT / "scripts").glob("autoresearch*.py")}
        self.assertEqual(
            {
                "autoresearch.py",
                "autoresearch_core.py",
                "autoresearch_report.py",
                "autoresearch_state.py",
            },
            scripts,
        )
```

- [ ] **Step 2: Run it to verify it fails**

```bash
python3 -m unittest discover -s tests -p 'test_structure.py' -q
```

Expected: FAIL — `autoresearch_state.py` is missing from the actual set.

- [ ] **Step 3: Create the new module**

Create `scripts/autoresearch_state.py`. Move these names out of `autoresearch_core.py` verbatim, in this order:

```python
#!/usr/bin/env python3
"""Run and event schema validation, and authoritative state replay."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from autoresearch_core import (
    AutoresearchError,
    EVENTS_FILE,
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
```

Then move these eight functions from `autoresearch_core.py` into this module, in this
order, **copying each body without modification** — this is a relocation, not a
rewrite: `validate_run`, `validate_event`, `load_run`, `load_events`, `derive_state`,
`load_context`, `append_event`, `status_payload`.

The `iteration` event and its replay logic are retained here unchanged. Phase 2
replaces them with `candidate_started` / `candidate_resolved`; keeping them intact
through Phase 1 is what lets the existing 29 tests prove the extraction was faithful.

- [ ] **Step 4: Delete the moved names from core**

Remove from `scripts/autoresearch_core.py`: `SCHEMA_VERSION`, `TERMINAL_EVENTS`, `RUN_KEYS`, `METRIC_KEYS`, `EVENT_COMMON`, `EVENT_FIELDS`, `validate_run`, `validate_event`, `load_run`, `load_events`, `derive_state`, `load_context`, `append_event`, `status_payload`.

Keep `RunState` in core — it is a data container that both modules use.

- [ ] **Step 5: Repoint the CLI imports**

In `scripts/autoresearch.py`, split the single `from autoresearch_core import (...)` block into two. Move these names to a new `from autoresearch_state import (...)` block: `SCHEMA_VERSION`, `append_event`, `load_context`, `load_run`, `status_payload`, `validate_run`.

- [ ] **Step 6: Repoint the report imports**

```bash
grep -n "^from autoresearch_core import\|^import autoresearch_core" scripts/autoresearch_report.py
```

Move any of the six names above into a `from autoresearch_state import (...)` block.

- [ ] **Step 7: Update the shell validator**

In `scripts/validate_skill_structure.sh`, change the module count check from 3 to 4:

```bash
runtime_script_count="$(find "$ROOT/scripts" -maxdepth 1 -type f -name 'autoresearch*.py' | wc -l | tr -d ' ')"
if [[ "$runtime_script_count" -ne 4 ]]; then
  echo "Expected exactly 4 autoresearch Python modules, found $runtime_script_count" >&2
  exit 1
fi
```

Add the new module to the `py_compile` call:

```bash
python3 -m py_compile \
  "$ROOT/scripts/autoresearch.py" \
  "$ROOT/scripts/autoresearch_core.py" \
  "$ROOT/scripts/autoresearch_report.py" \
  "$ROOT/scripts/autoresearch_state.py"
```

Add it to the `required` array too, after the `autoresearch_report.py` entry near line 19:

```bash
  "$ROOT/scripts/autoresearch_state.py"
```

Update the closing echo to say `4 runtime modules`.

- [ ] **Step 7a: Add the new module to the e2e skill copier — this one is load-bearing**

`scripts/run_skill_e2e.sh` builds a disposable copy of the skill to test against, and `copy_skill()` hardcodes the script list at lines 56-60. Without this edit the copied skill is missing `autoresearch_state.py` and `foreground-smoke` fails with `ModuleNotFoundError: No module named 'autoresearch_state'`.

```bash
  cp \
    "$ROOT/scripts/autoresearch.py" \
    "$ROOT/scripts/autoresearch_core.py" \
    "$ROOT/scripts/autoresearch_report.py" \
    "$ROOT/scripts/autoresearch_state.py" \
    "$destination/scripts/"
```

- [ ] **Step 7b: Update the CONTRIBUTING.md module list**

Replace lines 15-17 so the architecture section names four modules and no longer claims the CLI owns a detached controller:

```text
scripts/autoresearch.py         CLI entry point
scripts/autoresearch_core.py    atomic IO, Git, and command primitives
scripts/autoresearch_state.py   strict run and event schema, and state replay
scripts/autoresearch_report.py  read-only terminal, TSV, and HTML views
```

- [ ] **Step 8: Verify no circular import**

```bash
python3 -c "import sys; sys.path.insert(0, 'scripts'); import autoresearch; print('import ok')"
```

Expected: `import ok`. `autoresearch_state` imports from `autoresearch_core`; core must import nothing from state. If Python raises `ImportError: cannot import name`, a moved function still references a core-only helper — pass it as an argument rather than importing backward.

- [ ] **Step 9: Run everything to verify green**

```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5 && bash scripts/validate_skill_structure.sh
```

Expected: `Ran 29 tests ... OK`, then `Skill structure valid: <n>-byte SKILL.md, 2 references, 4 runtime modules.`

- [ ] **Step 10: Commit**

```bash
git add scripts/autoresearch_state.py scripts/autoresearch_core.py scripts/autoresearch.py scripts/autoresearch_report.py tests/test_structure.py scripts/validate_skill_structure.sh
git commit -m "refactor: extract schema validation and replay into autoresearch_state

Co-authored-by: Claude <noreply@anthropic.com>"
```

---

## Task 9: Update the skill and user documentation

**Files:**
- Modify: `SKILL.md`
- Modify: `docs/GUIDE.md`
- Modify: `docs/EXAMPLES.md`
- Modify: `README.md`
- Modify: `CONTRIBUTING.md:39-40`

- [ ] **Step 1: Write the failing size check**

`SKILL.md` must stay at or under 8000 bytes. Check the current size:

```bash
wc -c < SKILL.md
```

Record the number. It must not exceed 8000 after edits.

- [ ] **Step 2: Remove background from SKILL.md**

Delete the entire `### Background` section (lines 66-81), the `- Read references/background.md ...` load line if Task 4 missed it, and the `foreground or background` bullet from the Before Starting list. Rename `--max-iterations` to `--max-candidates` at line 59. Delete the `### Foreground` heading, promoting its content, since there is now only one mode.

In the Existing Runs section, delete the `Background status, stop, or resume` bullet and the "Stop a live background run first" clause.

In Invariants, delete "Never ask 'should I continue?' after launch" if it references launch; reword to "after the run is initialized".

- [ ] **Step 3: Remove background from `docs/GUIDE.md`**

Delete the `## Background` section (lines 75-95), the `runtime.json` and `runtime.log` lines from the Artifacts tree (lines 120-121), the two `orphaned` sentences in Run States (line 109), and the `Worker produces no event` and `Controller disappears` bullets in Errors And Recovery (lines 171-172). Rename the `## Foreground` heading to `## The Experiment Loop`, since there is no longer a second mode to contrast it with.

Three further sites outside those sections:

| Line | Current | Change to |
|---|---|---|
| 25 | "You also choose foreground or background and may set an iteration limit." | Drop the mode choice; keep the limit, renamed — "You may set a candidate limit." |
| 105 | Run-states table: "User stop or iteration limit" | "User stop or candidate limit" |
| 129 | "If the current run is still active, stop a background run first. For foreground, clear the old Codex Goal with `/goal clear`, then..." | Drop the background clause, keep the Goal-clearing instruction as the unconditional path |

The `iteration limit` → `candidate limit` rewording on lines 25 and 105 follows the `max_iterations` → `max_candidates` rename from Task 5; leaving the prose saying "iteration" would contradict the flag users actually type.

- [ ] **Step 4: Remove background from `docs/EXAMPLES.md` and `README.md`**

Surveyed inventory of every hit, so this is not a discovery exercise. Line numbers are as of commit `fc39447`; re-grep to confirm before editing, since earlier steps in this task shift them.

`README.md`:

| Lines | Content |
|---|---|
| 60-65 | Example dialogue asking "Run in foreground or background?" and answering "Background. Go." |
| 89-98 | The whole `## Foreground And Background` section, including the comparison table and the mode-continuation paragraph |
| 108 | "foreground or background mode;" in the confirmation-block list |
| 120-123 | Artifacts table rows for `runtime.json`, `runtime.log`, and the `logs/` row mentioning "background worker output" |
| 205 | Sandbox paragraph explaining Full Access defaults for background runs |
| 207-209 | FAQ "Can I stop and resume?" answer describing background status/stop/resume |

`docs/EXAMPLES.md`:

| Lines | Content |
|---|---|
| 74 | "before launch" phrasing — reword, the noisy-benchmark point itself stays |
| 138-143 | The whole `## Background Overnight` example section |

`docs/INSTALL.md`: no hits. Leave it alone.

`CONTRIBUTING.md:56`: says the skill gate "runs strict unit tests plus deterministic foreground and background smoke tests." The background smoke test was deleted in Task 3, so this is now factually wrong. Change it to name only the foreground smoke test.

Re-grep to catch anything this inventory missed:

```bash
grep -n "background\|Background\|launch\|runtime\|max-iterations\|orphan" docs/EXAMPLES.md README.md docs/INSTALL.md CONTRIBUTING.md
```

Apply one rule per hit:

| Hit describes | Action |
|---|---|
| detached execution, `launch`, `stop`, a controller, or a worker process | delete the sentence, bullet, or code block |
| `runtime.json` / `runtime.log` artifacts | delete the line |
| `--max-iterations` | rename to `--max-candidates` |
| foreground contrasted against background | rewrite as an unqualified statement |

Leave `docs/i18n/README_*.md` untouched — the eight translations are updated in Phase 3. `test_local_markdown_links_resolve` does scan them via `docs.rglob("*.md")`, but their only local links are to `../../README.md`, `../EXAMPLES.md`, `../GUIDE.md`, and `../INSTALL.md`, all of which survive this phase. Their prose will describe a background mode that no longer exists until Phase 3 corrects it; that is a known, accepted gap for the duration of the branch.

- [ ] **Step 4a: Remove the background design rules from `CONTRIBUTING.md`**

Line 39 currently reads:

```
- Do not add custom Codex hooks. Foreground continuity belongs to official Goals; background continuity belongs to the controller.
```

Replace it with:

```
- Do not add custom Codex hooks. Continuity belongs to official Goals.
```

Delete line 40 entirely — it describes a controller that no longer exists:

```
- The background controller owns each worker process tree and must terminate it before stopping or failing.
```

Then check the rest of the file for surviving background language:

```bash
grep -n "background\|controller\|worker" CONTRIBUTING.md
```

Apply the same four-row rule table from Step 4 to each hit.

- [ ] **Step 5: Verify the size limit and link integrity**

```bash
wc -c < SKILL.md && python3 -m unittest discover -s tests -p 'test_structure.py' -q
```

Expected: a number at or below 8000, then `OK`. The structure test's `test_local_markdown_links_resolve` catches any link left pointing at the moved `references/background.md`.

- [ ] **Step 6: Run the full gate**

```bash
bash scripts/run_contributor_gate.sh
```

Expected: exits 0. This runs the structure validator, the full suite, and the foreground smoke in sequence — the same three gates CI runs.

- [ ] **Step 7: Commit**

```bash
git add SKILL.md docs README.md
git commit -m "docs: remove background mode from skill and user documentation

Co-authored-by: Claude <noreply@anthropic.com>"
```

---

## Task 10: Verify the phase end-to-end

**Files:** none

- [ ] **Step 1: Run the whole suite**

```bash
python3 -m unittest discover -s tests -q 2>&1 | tail -5
```

Expected: `Ran 29 tests ... OK`

- [ ] **Step 2: Run the structure validator**

```bash
bash scripts/validate_skill_structure.sh
```

Expected: `Skill structure valid: <n>-byte SKILL.md, 2 references, 4 runtime modules.`

- [ ] **Step 3: Run the foreground smoke against the renamed CLI**

```bash
bash scripts/run_skill_e2e.sh foreground-smoke --clean
```

Expected: exits 0. This is the real integration evidence that a sequential run still initializes, iterates, and completes on schema 2.

- [ ] **Step 4: Confirm no background surface survives**

```bash
grep -rn "runtime\.json\|_controller\|launch_background\|spawn_controller\|max_iterations" scripts/ SKILL.md references/ docs/GUIDE.md
```

Expected: no output. Any hit is leftover dead code or stale documentation.

- [ ] **Step 5: Confirm the line count dropped**

```bash
wc -l scripts/autoresearch*.py
```

Expected: `autoresearch.py` well under its original 1584 lines, and `autoresearch_core.py` under its original 1213.

Final measurements against the phase baseline at `d65f7c6`:

| Module | Before | After |
|---|---|---|
| `autoresearch.py` | 1584 | 894 |
| `autoresearch_core.py` | 1213 | 671 |
| `autoresearch_report.py` | 524 | 523 |
| `autoresearch_state.py` | — | 428 |
| **Total** | **3321** | **2516** |

An earlier revision of this plan claimed the 1584 baseline was wrong and should read 1527. That correction was itself mistaken: 1584 is the count at the phase baseline, while 1527 was the count Task 6 measured after Task 5 had already deleted from the file.

---

## Phase 2 and Phase 3 (separate plan documents)

Phase 1 is deliberately the whole of this document. Phases 2 and 3 get their own plans, written once Phase 1 lands — because bite-sized steps written now against `autoresearch_state.py` interfaces that do not exist yet would be wrong by the time anyone reached them.

**Phase 2 — Parallel engine.** Task inventory, in dependency order:

1. `autoresearch_docs.py` — load, 4 KB cap, sha256, content-addressed snapshot
2. `autoresearch/` doc scaffold, `PROTECTED_PREFIXES` gains `"autoresearch"`, `decide` command
3. `autoresearch_bank.py` — bank parsing, capacity for `cores` and `node` kinds, deterministic grant allocation and release
4. `compute detect` — reports observed capacity with provenance, writes nothing
5. `autoresearch_allocator.py` — the pure exploit/explore policy (spec §10)
6. Event model v2 — `candidate_started`, `candidate_resolved`, `decision`, `bank_changed`, `unresolved_candidates` on terminal events
7. Replay invariants 1-9 in `derive_state`, including grants never exceeding capacity
8. `autoresearch_slots.py` — worktree lifecycle, `slots.json`, leases, admission lock
9. Admission protocol including the stale rebase decision table (spec §9)
10. `autoresearch_packet.py` — host-agnostic worker packet
11. CLI: `claim`, `bind`, `heartbeat`, `finish --candidate`, `abandon`, `reap`, `reconcile`, `rebank`
12. Integration test: `max_parallel=3` on the existing fixture with a forced stale rebase and a reaped lease

**Phase 3 — Surfaces.** `references/parallel.md` (references back to 3), SKILL.md rewrite within 8000 bytes, candidate-level `history` and `report` rendering, README plus eight translations, and the `codex-autoresearch` → `autoresearch` rename if approved (spec §17).
