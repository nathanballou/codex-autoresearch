# Parallel Candidates

Read this before claiming work. You are the coordinator. You spawn the workers; the
control plane never does.

## The loop

```text
claim --count N   ->  N worker packets
spawn N subagents ->  one per packet, concurrently, using your host's own primitive
bind              ->  record each agent id
(worker finishes) ->  slot enters reporting
(worker reports)  ->  evidence persists; slot frees
claim --count 1   ->  refill immediately; do not wait for the others
```

Refill as each worker returns. Waiting for all of them wastes the slots that already
freed.

## Host adapter

Concurrent subagent spawning is a tool inside your session, not a CLI subcommand, so
the control plane cannot do it for you. Tool names and parameters change between
releases — use whatever your own tool list exposes.

| Need | Codex | Claude Code | Prime Agent |
|---|---|---|---|
| concurrent workers | the native multi-agent primitive | several agent calls in one message | `await rlm(packet, name=...)`, one call per packet |
| notice a worker finished | subagent completion | task completion notification | `status`; `rlm()` returns an admission handle, never an answer |
| read-only helper | native, read-only | the read-only explore agent type | none; a child inherits the parent's tools |
| continuity across turns | an official Goal | the conversation, plus task tracking | a persistent goal, plus an autonomous gate |
| concurrency ceiling | `max_concurrent_threads_per_session` in `config.toml` | measured at 16; declare it in the bank | 8 per root session; declare it in the bank |

Sixteen concurrent Claude Code subagents were measured working: all sixteen held a
60-second task at once, none rejected. Dispatch ramps at roughly 2 seconds per start,
so the last of sixteen begins about 31 seconds after the first. Candidates run for
minutes, so that ramp does not matter, but do not expect an instant fleet.

Spawn each worker with the `model` and `thinking_tokens` its packet was assigned.
`claim` rates every candidate: deepening a win is close to mechanical and goes cheap,
a new mechanism gets the standard budget, and a plateau escape or a discard streak gets
the largest, which is where the reasoning is actually hard.

If your host cannot spawn concurrent subagents, run `claim --count 1` in a loop. The
state model is identical; only the concurrency is lost.

## On Prime Agent

Every command runs in the IPython kernel, so control-script calls go through `%%bash`
cells and workers are RLM children. Three differences matter while the loop runs.

`rlm()` admits a child and returns immediately; a worker's result never comes back
through that call. Nothing here needs it to. The worker calls `finish` itself, and you
read the outcome from `status` like any other host.

`rlm()` accepts only `name` and `model`, and rejects anything else rather than ignoring
it. A packet's `thinking_tokens` therefore cannot be set at spawn — children inherit the
session's thinking level. Pass the tier `model` through as an exact `provider/model`
selector from `await rlm.find_models()`; treat the token budget as the intent the packet
records.

Continuity is two mechanisms, not one. The persistent goal stores what the run is:

```python
await goal.create("autoresearch <run8>: drive <metric> from <baseline> to <target>")
```

Create it right after `init` returns the run id, and call `await goal.complete()` only
once `status` reports `complete`. The `goal` skill creates a goal only when instructed
explicitly; this skill's `Start` step is that instruction. The autonomous gate decides
whether another continuation is injected, and the user configures it at launch.

When you learn something that outlives this run — a tactic, a repeated failure, a worker
role that keeps paying off — also push it into the harness:

```python
await refine.run("workers that rewrite the parser before reading its tests always discard")
```

Refinement is scheduled and applies when the turn ends; one request per turn is enough.
It supplements `decisions.md`, which is still curated with `decide --add` and still dies
with the run; it never replaces it. `refine` exists only in a persisted session, so treat
its absence as a run started with `--no-session`, not as an error to work around.

## What you own, and what you must not do

You curate two documents every worker receives:

- `autoresearch/goal.md` — the overarching process goal. Not one run's target.
- `autoresearch/decisions.md` — accumulated notes. Append with `decide --add`.

Both are capped at 16 KB because they go into every packet. Edit them **only** through
`decide`; an unrecorded edit fails the next `finish` with an actionable error.

Do not write the worker prompt yourself. `claim` returns a complete packet carrying
the goal, the decisions, the individual target, the role instruction, the grant, the
worktree, the lease, and the exact commands. Pass it through verbatim.

Do not commit, measure, or judge on a worker's behalf. The control plane commits in
the worker's worktree, measures there, and decides admission.

## Roles

Every candidate branches from the current frontier. `claim` assigns a role from
recent admission rates:

- **exploit** — deepen the direction that produced the current best result.
- **explore** — try a materially different mechanism.

Override with `--role`, which requires `--role-reason` so the deviation is auditable.
After `plateau_k` consecutive exploits admit nothing, the policy forces an explore.

## Compute

`autoresearch/compute.json` declares the bank. `cores` entries are fungible; a `node`
entry is a whole machine held by one candidate. Capacity is
`floor(cores / cores_per_candidate)` plus each node's capacity.

Grants are **advisory**. The bank says what a worker has; `decisions.md` says how to
use it. Nothing enforces the share — macOS has no `taskset` — so the accounting is
the contract, not a sandbox.

Run `compute detect` to see observed local capacity with the provenance of each
number. It never writes. You decide, then write the bank explicitly.

## Admission

A worker calls `finish --candidate <id>`. The control plane commits and measures in
that worker's worktree, then takes the admission lock. Two candidates that both
improve will serialize on the lock; the loser finds a moved frontier and is rebased
onto it and re-measured, because a candidate must improve against the frontier it
will actually land on.

`finish` returns the adjudicated commit and leaves the slot in `reporting`. The worker
must profile that commit and call `report --candidate <id> --analysis-file <path>` with
the packet's exact schema. The validated event records before/after component values,
the outcome analysis, diagnostic confidence, an ordered causal chain tied to those
measurements, and the measured next focus; only then does the grant return to the bank.
Reports derive execution versus frontier outcome, improvements, regressions, preserved
state, the remaining bottleneck, and the next experiment. Version-1 analysis remains
valid. The file is UTF-8 JSON capped at 16 KB.

A discarded candidate keeps its commit on `autoresearch/<run8>/c<NNNN>`. The frontier
history contains only admitted work.

## When something goes wrong

- `status` reports every slot, its lease, and whether that lease lapsed.
- `reconcile` reports reapable candidates and broken slots, and clears an admission
  lock whose holder is provably gone. It repairs nothing else.
- `reap --candidate <id>` resolves an expired-lease candidate and frees its slot.
  Always explicit: a slow worker and a dead worker look identical from here.
- Reaping a `reporting` slot records a terminal missing-report error. Any admitted
  improvement remains on the frontier; missing analysis never rolls it back.
- A worker returning after its candidate was reaped or abandoned is refused. Stale
  work can never be admitted.

Never resolve a candidate on a worker's behalf just to free a slot. Use `abandon`
when the worker itself reports it has nothing, and `reap` only after a lease lapses.
