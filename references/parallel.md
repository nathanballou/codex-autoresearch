# Parallel Candidates

Read this before claiming work. You are the coordinator. You spawn the workers; the
control plane never does.

## The loop

```text
claim --count N   ->  N worker packets
spawn N subagents ->  one per packet, concurrently, using your host's own primitive
bind              ->  record each agent id
(worker finishes) ->  slot frees
claim --count 1   ->  refill immediately; do not wait for the others
```

Refill as each worker returns. Waiting for all of them wastes the slots that already
freed.

## Host adapter

Concurrent subagent spawning is a tool inside your session, not a CLI subcommand, so
the control plane cannot do it for you. Tool names and parameters change between
releases — use whatever your own tool list exposes.

| Need | Codex | Claude Code |
|---|---|---|
| concurrent workers | the native multi-agent primitive | several agent calls in one message |
| notice a worker finished | subagent completion | task completion notification |
| read-only helper | native, read-only | the read-only explore agent type |
| continuity across turns | an official Goal | the conversation, plus task tracking |

If your host cannot spawn concurrent subagents, run `claim --count 1` in a loop. The
state model is identical; only the concurrency is lost.

## What you own, and what you must not do

You curate two documents every worker receives:

- `autoresearch/goal.md` — the overarching process goal. Not one run's target.
- `autoresearch/decisions.md` — accumulated notes. Append with `decide --add`.

Both are capped at 4 KB because they go into every packet. Edit them **only** through
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

A discarded candidate keeps its commit on `autoresearch/<run8>/c<NNNN>`. The frontier
history contains only admitted work.

## When something goes wrong

- `status` reports every slot, its lease, and whether that lease lapsed.
- `reconcile` reports reapable candidates and broken slots, and clears an admission
  lock whose holder is provably gone. It repairs nothing else.
- `reap --candidate <id>` resolves an expired-lease candidate and frees its slot.
  Always explicit: a slow worker and a dead worker look identical from here.
- A worker returning after its candidate was reaped or abandoned is refused. Stale
  work can never be admitted.

Never resolve a candidate on a worker's behalf just to free a slot. Use `abandon`
when the worker itself reports it has nothing, and `reap` only after a lease lapses.
