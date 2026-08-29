---
name: autoresearch
description: "Run autonomous, measurable experiments in a Git repository: change one hypothesis, verify a numeric metric, keep improvements, and revert failures. Use when the user wants an agent to keep iterating toward a numeric target. Do not use for ordinary one-shot coding, open-ended work without a mechanical metric, or non-Git directories."
metadata:
  short-description: "Run measurable autonomous experiments"
---

# Autoresearch

Turn a repo-level goal into a controlled loop:

`inspect -> change one thing -> verify -> keep or revert -> repeat`

The coordinating model supplies the engineering judgment. The bundled control script supplies strict Git boundaries, measurement, rollback, state, and logs.

## Load

- Read `references/workflow.md` for every invocation, including status, history, report, and resume.
- Read `references/experiment.md` before starting or continuing an active run.
- Read `references/parallel.md` before claiming work, since candidates run in parallel by default.

Resolve commands from this skill's own directory as `<skill-root>/scripts/autoresearch.py`. Never assume the target repository contains the script.

## Before Starting

1. Require one Git repository root. If the task spans repositories, ask the user to choose one run per repository.
2. Check for `autoresearch-results/run.json` with:

   ```bash
   python3 <skill-root>/scripts/autoresearch.py status --repo <repo>
   ```

   `not_initialized` is fresh. Any other status or schema error must be surfaced; do not infer state from other files.
3. For a fresh run, inspect the repo and propose:
   - one plain-language goal,
   - repository-relative file or directory scopes (no globs),
   - one numeric metric and whether lower or higher is better,
   - a command whose final non-empty stdout line is that number, or a JSON object plus one explicit key,
   - a numeric target,
   - an optional baseline-passing guard command,
   - an optional candidate limit.
4. Run candidate measurement commands read-only if needed, then show one concise confirmation. Include the baseline, target, scope, commands, and the fact that each trial is committed and failed trials keep their commit on a candidate branch while the frontier stays put.
5. Do not write project files, initialize artifacts, or create a Goal before clear user approval such as `go`.

## Start

After approval, use the exact confirmed values.

Initialize once:

```bash
python3 <skill-root>/scripts/autoresearch.py init \
  --repo <repo> --goal <goal> --scope <path> \
  --metric-name <name> --direction <lower|higher> \
  --verify <command> [--metric-key <key>] --target <number> \
  --max-parallel <n|bank> --worktree-root <dir-outside-repo> \
  --lease-seconds <n> --window <n> --min-per-role <n> --plateau-k <n> \
  [--guard <command>] [--prepare <command>] [--max-candidates <n>]
```

Every parallelism value is explicit; nothing is defaulted. Run `compute detect` first
and write `autoresearch/compute.json` from what it reports. `autoresearch/goal.md`
must exist and state the overarching goal.

Then establish continuity with whatever your host provides, so the run survives across
turns. `references/parallel.md` names the mechanism per host. Whatever it is, it must
identify this as autoresearch and carry the returned run id, metric, and target.

If your host has no continuation mechanism, say so plainly rather than implying the
run will continue on its own. The run itself is never lost: all state lives in
`autoresearch-results/`, so any later session on any host resumes it with `status`.

## Experiment Loop

Candidates run in parallel. Read `references/parallel.md` before your first claim.

1. Claim slots and receive one worker packet each:

   ```bash
   python3 <skill-root>/scripts/autoresearch.py claim --repo <repo> --count <n>
   ```

2. Spawn one subagent per packet, concurrently, using your host's own primitive. Pass
   each packet through verbatim; you do not write the worker prompt. Record each agent
   id with `bind`.
3. As each worker returns, claim again to refill that slot immediately. Do not wait
   for the whole batch.
4. Curate `decisions.md` with `decide --add` when you learn something every future
   worker needs. Never edit it by hand.

Each worker resolves and reports its own candidate:

```bash
python3 <skill-root>/scripts/autoresearch.py finish \
  --repo <repo> --candidate <id> --description <short-description>
```

`finish` checks scope and Git provenance, commits and measures inside that
candidate's worktree, rebases and re-measures if the frontier moved underneath it,
runs the guard, and admits a genuine improvement. The slot then stays in `reporting`
until the worker submits the packet's measured JSON analysis with
`report --candidate`; only the validated report frees the slot and allows terminal
completion. A discarded candidate keeps its commit on its own branch and leaves the
frontier alone. New reports separate completed execution from frontier outcome, label
diagnostic confidence, and link an ordered causal chain to measured observations.
History derives the measured improvements, regressions, preserved frontier/trial
state, remaining bottleneck, and next experiment from that evidence. Version-1
reports remain readable.

Without `--candidate` the run degrades to one sequential candidate in the primary
checkout, for hosts that cannot spawn concurrent subagents.

Continue immediately while status is `active`. On `complete`, verify status, close out
your host's continuation, and summarize the baseline, final metric, candidate count,
and retained commits.

Use `block` only when progress truly requires external input or an environment change, and only after the same blocker has prevented progress on three consecutive Goal turns:

```bash
python3 <skill-root>/scripts/autoresearch.py block --repo <repo> --reason <reason>
```

Then mark your host's continuation blocked. A failed hypothesis, difficult bug, or lack of immediate improvement is not a blocker.

## Existing Runs

- History request: run `history --repo <repo>`; use `--format tsv` only for tabular export.
- HTML report request: run `report --repo <repo>` and return its generated path. Both views validate the complete event history; neither is runtime state.
- Same foreground goal: validate `status`, resume the matching official Goal, and continue.
- Different goal: show the current run. Ask the user to clear the previous continuation on their host. Then ask before `archive` and initialize the fresh run.
- `complete`: never resume it. Archive before a new goal.
- Invalid JSON, unknown schema, event gap, Git mismatch, out-of-scope change, or malformed metric output: stop and report the exact error and log path. Never reconstruct, guess, or silently repair state.
- A failed initialization may leave `init-error.json` and command logs but no `run.json`. Report the diagnostic and use explicit `archive` before retrying; do not treat it as a fresh run.

## Invariants

1. Ask before the first write.
2. Require a clean named Git branch at initialization.
3. Keep one authoritative configuration in `run.json` and one append-only state history in `events.jsonl`.
4. Use one numeric metric and one target. A guard is pass/fail and must pass at baseline.
5. One focused experiment per `finish`; one repository per run.
6. Never stage autoresearch artifacts or touch paths outside confirmed scope.
7. Verification commands must exit zero, emit UTF-8, and use an explicit scalar or JSON-key parser. Parsing or command errors stop the run.
8. Never hide failures with fallback parsing, old-layout recovery, or synthetic success.
9. Never ask "should I continue?" once the run is initialized. Continue until target, user stop, candidate limit, or a verified external blocker.
10. Preserve command output under `autoresearch-results/` for diagnosis.
