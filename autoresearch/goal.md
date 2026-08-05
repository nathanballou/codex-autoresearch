# Overarching Goal

Ship `codex-autoresearch`: a skill that runs autonomous, measurable experiments in a
Git repository. Change one hypothesis at a time, verify a numeric metric, keep
improvements, and discard failures — with every decision reconstructible from an
append-only event log.

This is the *process* goal. It outlives any single run. The individual optimization
target for a run lives in `run.json.goal`, not here.

## What good looks like

- Every retained change is backed by a measured improvement, never a claim.
- State is always reconstructible from `events.jsonl` alone.
- Failures stop the run with an exact error and a log path, never a silent repair.
