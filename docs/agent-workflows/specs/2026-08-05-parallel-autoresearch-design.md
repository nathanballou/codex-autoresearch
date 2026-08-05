# Parallel Autoresearch — Design

Date: 2026-08-05
Status: awaiting review
Scope: parallel candidate engine, compute bank accounting, dual-host coordination

## 1. Problem

Autoresearch runs one experiment at a time. `derive_state` replays a strictly
linear chain in which every iteration asserts `previous_metric == retained_metric`,
the run is pinned to one branch and one HEAD in one working tree, and the
background controller starts exactly one worker per iteration.

Sequential search wastes available compute and explores one hypothesis class at a
time. A plateau costs a full measurement cycle per attempt to escape.

## 2. Goals

1. Run multiple candidate experiments concurrently, by default.
2. Track available compute in a bank so each subagent knows what it may use, and
   let the coordinator add outside capacity — cores or whole nodes — to that bank.
3. Split concurrency adaptively between improving the current best and trying
   genuinely different ideas.
4. Track every parallel agent durably enough to survive a crash.
5. Give every worker the overarching process goal and the accumulated decisions,
   curated by the main thread, distinct from the individual optimization target.
6. Work on both Codex and Claude Code with one control plane.

## 3. Non-goals

- **Executing anything on a remote machine.** The bank accounts for remote capacity;
  the subagent uses it, guided by `decisions.md`. The script never ssh's or syncs.
- **Enforcing a grant.** Grants are advisory; the accounting is authoritative.
- Auto-detecting or defaulting any configuration value (§4, D9).
- Cross-run allocator priors.
- A third candidate role (for example, revisiting discarded ideas).
- Any migration path from schema 1. Existing runs are archived, not converted.

## 4. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Continuous frontier with stale rebase | No barrier; a candidate whose base went stale is rebased and re-measured rather than discarded |
| D2 | Script computes allocation; coordinator may override with a recorded reason | Deterministic and replayable, without blocking coordinator judgment |
| D3 | `goal.md`, `decisions.md`, `compute.json` are repo-tracked, snapshotted per run, hashed per candidate | Overarching knowledge outlives a run; provenance stays exact |
| D4 | One long-lived git worktree per slot | Shared object store; setup and dependency install amortized per slot |
| D5 | Compute is a tracked bank. The script accounts, the coordinator allocates, the agent honors an advisory grant | Enforcement is not portable; accounting is, and it is what subagents actually need |
| D6 | `measurement` mode is explicit | Contention silently destroys timing metrics |
| D7 | Foreground only; background mode deleted | Coordinator-driven refill keeps the main thread in the loop to curate docs; removes the orphaned-controller failure class entirely |
| D8 | Workers are host-native subagents; the script never spawns a worker | The only portable seam between Codex and Claude Code |
| D9 | **No defaults.** Every value this feature introduces is required and explicit | Matches the repo's no-fallback rule; the confirmation block surfaces every knob to the human once |

## 5. Architecture

```
main thread (coordinator)                   control plane (python)
  |                                            |
  |-- claim --count N ----------------------->  | allocate role + compute grant,
  |                                            | prepare worktrees, write
  |                                            | candidate_started, emit packets
  |<-- N worker packets ------------------------|
  |
  |-- spawn N subagents concurrently (host-native primitive)
  |       |                                    |
  |       worker i (own worktree, own grant) -- finish -->
  |       |                                    | commit, measure, [rebase, re-measure],
  |       |                                    | guard, fast-forward, candidate_resolved,
  |       |                                    | return grant to the bank
  |<------ completion notification              |
  |
  |-- claim --count 1 (refill freed slot) --->  |
```

**Slot** — long-lived. Owns one worktree for the whole run.
**Candidate** — ephemeral. One experiment in one slot. Monotonic id, never reused.
**Grant** — the compute a candidate holds while live, returned on resolution.

Between candidates a slot resets its worktree to the current frontier head. Worktree
creation and `--prepare` are paid once per slot, so a 40-candidate run over 4 slots
performs 4 dependency installs, not 40.

### 5.1 Why the script never spawns workers

Verified on `codex-cli 0.144.6`: `multi_agent` is `stable`/`true`, `goals` is
`stable`/`true`, while `enable_fanout` and `multi_agent_v2` are under development
and off. On Claude Code the equivalent is the `Agent` tool.

On both hosts, concurrent subagent spawning is a **model-facing tool inside the
session**, not a CLI subcommand. A Python supervisor cannot invoke it. Therefore
the control plane hands out slots, grants, and packets, and the coordinator spawns.
This is the only seam portable across both hosts, and it deletes all worker process
management: no PIDs, no process groups, no orphan detection.

### 5.2 Host adapter

The adapter deliberately names no tool parameters — those change between releases,
and the coordinator sees its own tool list at runtime.

| Need | Codex | Claude Code |
|---|---|---|
| concurrent workers | native multi-agent primitive (`multi_agent`) | multiple `Agent` calls in one message |
| per-completion refill | subagent completion | background agent task notification |
| read-only helper | native, read-only | `subagent_type: "Explore"` |
| cross-turn continuation | official Goal (`goals`) | conversation plus `TaskCreate`/`TaskUpdate` |

If a host cannot spawn concurrent subagents, `claim --count 1` in a loop degrades to
sequential execution against the identical state model. Nothing else changes.

### 5.3 Worktrees live outside the repository

`--worktree-root` is required — there is no default path (D9). The absolute path is
recorded in `run.json`.

Worktrees must not live under `autoresearch-results/`. This is not cosmetic: a
root-level `pytest -q` verify command would recurse into `slot-1/tests/` and collect
every test N+1 times, silently corrupting the metric. Init rejects a
`--worktree-root` inside the repository.

Slot reset between candidates:

```bash
git -C <worktree> checkout -B autoresearch/<run8>/c<NNNN> <frontier-head>
git -C <worktree> reset --hard <frontier-head>
git -C <worktree> clean -df        # NOT -x: ignored build artifacts survive
```

`-x` is omitted deliberately. Cleaning ignored files would delete `node_modules`
and `.venv`, destroying the per-slot amortization that D4 exists to buy.

## 6. Curated documents

| Path | Owner | Content | Lifetime |
|---|---|---|---|
| `autoresearch/goal.md` | main thread | "We are building a dynamic pricing engine." | tracked, spans runs |
| `autoresearch/decisions.md` | main thread | "Training runs go to ec2-b over ssh." | tracked, accumulates |
| `autoresearch/compute.json` | main thread | the compute bank (§8) | tracked |
| `run.json.goal` | script, immutable | "Reduce demand-model RMSE." | one run |

- Cap: **4 KB** per file. Over cap is a hard error naming the file and its size.
  This is what keeps "small" enforced rather than aspirational.
- `PROTECTED_PREFIXES` gains `"autoresearch"`. A candidate editing a doc trips the
  existing out-of-scope check, and `normalize_scopes` refuses `autoresearch` as a
  scope. Write authority is enforced by existing machinery.
- Snapshots are content-addressed at `autoresearch-results/docs/<sha256>.<name>`.
  Every candidate records all three hashes, so an archived run can always state
  exactly what its agents were told.
- `goal.md` is required and non-empty at init. `decisions.md` may be absent, treated
  as empty, created on first `decide`.
- `decide --add "<note>"` appends a bullet, re-snapshots, and appends a `decision`
  event carrying the new hash.

`compute.json` is the machine-readable bank; `decisions.md` is the prose that tells
an agent *how* to use what it was granted. Keeping them separate is deliberate:
parsing prose for infrastructure would be exactly the fragile fallback behavior the
coding standards forbid.

## 7. State model

`SCHEMA_VERSION` 1 → 2. Schema 1 runs are refused with the existing
"archive and start a new one" error. No migration code (invariant 8).

### 7.1 `run.json` v2

Removed: `background`. Renamed: `max_iterations` → `max_candidates` (counts
candidates started). Added — every field required:

```json
"parallel": {
  "max_parallel": "bank",            // "bank" = full bank capacity; int = explicit cap
  "worktree_root": "/abs/path/outside/the/repo",
  "prepare": "uv sync",              // or null, stated explicitly
  "lease_seconds": 1800,
  "allocation": { "window": 8, "min_per_role": 1, "plateau_k": 3 }
},
"docs": {
  "goal_path": "autoresearch/goal.md",
  "decisions_path": "autoresearch/decisions.md",
  "compute_path": "autoresearch/compute.json",
  "goal_sha256": "...", "decisions_sha256": "...", "compute_sha256": "..."
}
```

The values shown are illustrative, not defaults. Init fails if any is absent.
`--max-parallel <n|bank>` is required; `bank` is an explicit choice meaning "the whole
bank", never an omission. When capped below bank capacity, `claim` stops at the cap
even though the bank still has room.

`run.json` holds user configuration only. Derived and validated facts go in the event
log, where replay can see them.

### 7.2 Events

Removed: `iteration`. Retained: `baseline`, `blocked`, `complete`, `error`,
`resumed`, `stopped`.

| Event | Fields added or new |
|---|---|
| `baseline` | `bank` (snapshot), `max_parallel`, `measurement` |
| `candidate_started` | `candidate`, `slot`, `role`, `role_source`, `role_override_reason`, `grant`, `base_commit`, `base_metric`, `branch`, `lease_expires_at`, `goal_doc_sha256`, `decisions_doc_sha256`, `compute_doc_sha256` |
| `candidate_resolved` | `candidate`, `outcome`, `reason`, `trial_commit`, `trial_metric`, `rebased_commit`, `rebased_metric`, `guard`, `head`, `retained_metric`, `verify_log`, `rebase_verify_log`, `guard_log` |
| `decision` | `decisions_doc_sha256`, `note` |
| `bank_changed` | `compute_doc_sha256`, `bank`, `max_parallel` |
| terminal events | `unresolved_candidates` (list of ints) |

`outcome` ∈ `admitted` | `discarded` | `failed`.

| `reason` | `outcome` | Counts toward allocation rate |
|---|---|---|
| `no_improvement` | discarded | yes |
| `stale_no_improvement` | discarded | yes |
| `rebase_conflict` | discarded | yes |
| `guard_failed` | discarded | yes |
| `no_change` | failed | no |
| `abandoned` | failed | no |
| `lease_expired` | failed | no |

`failed` outcomes are excluded from the allocation window: an agent dying says
nothing about whether its role was a good bet.

### 7.3 Replay invariants

`candidate_resolved` events are appended **while holding the admission lock**, so
their order in the log is a true serialization of frontier moves. `candidate_started`
interleaves freely.

1. The frontier (`head`, `retained_metric`) changes **only** on `outcome: admitted`.
2. `admitted` requires `guard == "pass"`, `head ∈ {trial_commit, rebased_commit}`,
   and a genuine improvement over the prior frontier metric.
3. `discarded` and `failed` must leave `head` and `retained_metric` identical to the
   prior frontier. **No revert commits.** A loser's commit stays on its own candidate
   branch, which is the audit trail.
4. `candidate_started.base_metric` must equal the frontier metric at that event's
   position in the log.
5. Every `candidate_resolved` requires a matching earlier, still-unresolved
   `candidate_started` with the same id.
6. Candidate ids are monotonic from 1 and never reused.
7. A terminal event's `unresolved_candidates` must exactly equal the
   started-but-unresolved set. A crash mid-flight is stated, never dropped.
8. `max_parallel` and `bank` change only on `baseline` or `bank_changed`.
9. Outstanding grants never exceed bank capacity at any point in the replay.

Invariant 3 removes the revert-commit cost that `docs/GUIDE.md:160` currently
apologizes for. Invariant 9 makes over-allocation a replay error, not a runtime hope.

## 8. Compute bank

The script is an **accountant**, not a broker. It tracks what compute exists, grants
a share to each candidate, records the grant durably, refuses to over-allocate, and
returns the grant when the candidate resolves.

It does **not** wrap commands, ssh, sync worktrees, probe hosts, or enforce a grant.
Enforcement was never portable — macOS has no `taskset` and no `sched_setaffinity`.
Rather than enforce on one platform and pretend on another, the grant is advisory
everywhere and the accounting is authoritative.

### 8.1 The bank

```json
{
  "cores_per_candidate": 4,
  "measurement": "parallel",
  "bank": [
    { "id": "local", "kind": "cores", "cores": 12, "label": "workstation" },
    { "id": "ec2-a", "kind": "cores", "cores": 32, "label": "ubuntu@10.0.1.15" },
    { "id": "ec2-b", "kind": "node",  "capacity": 1, "label": "ubuntu@10.0.1.16" }
  ]
}
```

Every field is required. Nothing is inferred, defaulted, or auto-detected.

| `kind` | Meaning | Capacity contributed |
|---|---|---|
| `cores` | a fungible pool of N cores | `floor(cores / cores_per_candidate)` |
| `node` | a whole machine, held by one candidate at a time | `capacity` |

```
bank_capacity = Σ capacity(entry)
max_parallel  = bank_capacity, or parallel.max_parallel when explicitly capped
```

Twelve local cores at four per candidate gives 3 slots; a 32-core EC2 entry adds 8;
a single node entry adds 1. Twelve slots total.

This is how outside capacity enters: the coordinator adds an entry. Cores from an
EC2 box join the fungible pool; a box treated as a unit joins as a `node`.

### 8.2 Grants

A grant is allocated at `claim`, recorded in `candidate_started` and `slots.json`,
and released on `candidate_resolved`:

```json
{ "source_id": "ec2-a", "kind": "cores", "cores": 4, "label": "ubuntu@10.0.1.15" }
{ "source_id": "ec2-b", "kind": "node",              "label": "ubuntu@10.0.1.16" }
```

The worker packet states the grant in prose and it is exported to the command
environment:

```
AUTORESEARCH_GRANT_SOURCE=ec2-a
AUTORESEARCH_GRANT_KIND=cores
AUTORESEARCH_GRANT_LABEL=ubuntu@10.0.1.15
AUTORESEARCH_CORES=4
```

So a subagent knows how many cores it may use, and `decisions.md` tells it what to do
with a node grant. That closes the loop: the bank says *what you have*, the decisions
doc says *how to use it*, and neither tries to be the other.

Allocation order is deterministic for replay: bank entries in declared order, cores
before nodes, first entry with free capacity wins. A `claim` that cannot be satisfied
returns no packet for that slot and says which entries are exhausted — it never
grants beyond capacity, and never silently shrinks a grant.

### 8.3 `compute detect` reports, never decides

Reports observed local capacity with the provenance of each number:
`len(os.sched_getaffinity(0))` where present, cgroup v2 `/sys/fs/cgroup/cpu.max`, and
`os.cpu_count()`. CI pins Python 3.11, so `os.process_cpu_count()` is unavailable.

`os.cpu_count()` returning `None` is reported as unavailable. It is never substituted
with 1, and no observation is ever written into the bank automatically. The
coordinator reads the report, decides, and writes explicit numbers.

### 8.4 Measurement contention

N concurrent measurements contend, and **any timing-based metric silently becomes
noise**. `references/experiment.md:37` already requires fixing a noisy benchmark
before launch; parallelism manufactures that noise.

`measurement` is required:

- `"parallel"` — count-like metrics (failing tests, error count, coverage, binary
  size). Contention does not move the number.
- `"exclusive"` — at most one measurement at a time **per bank source**. Agents still
  edit and reason concurrently, which is most of the wall clock.

If the metric name or verify command matches a timing shape (`latency`, `p95`,
`bench`, `ms`, `seconds`, `throughput`), init requires explicit confirmation of the
choice. The heuristic drives a prompt, never a silent default.

Consequence to state plainly: under `exclusive`, effective parallelism against a
single bank source collapses toward 1. Multiple sources are what restore it.

## 9. Admission protocol

The lock is acquired **after** the first measurement. Measurement is the slow step;
holding a lock across it would serialize the feature away.

```
 1. commit worktree changes on the candidate branch      -> T   (script commits, never the agent)
 2. measure at T in the worktree                         -> trial_metric
 3. improved(trial_metric, base_metric)?          no     -> discard  no_improvement
--- acquire admission.lock; re-read frontier F, metric M ---
 4. base_commit == F:
      guard at T (if configured);            fail        -> discard  guard_failed
      git -C <repo> merge --ff-only T                    -> ADMIT   head=T
 5. base_commit != F  (stale):
      rebase T onto F;                       conflict    -> discard  rebase_conflict
                                                         -> R
      re-measure at R                                    -> rebased_metric
      improved(rebased_metric, M)?           no          -> discard  stale_no_improvement
      guard at R (if configured);            fail        -> discard  guard_failed
      git -C <repo> merge --ff-only R                    -> ADMIT   head=R
--- append candidate_resolved; release grant; release admission.lock ---
```

With no guard configured, `guard` records `"pass"` and `guard_log` is null, matching
the existing behavior in `finish_iteration`. A guard runs only on a candidate that
already improved, so a non-improving trial records `"not_run"`.

Two candidates may both clear step 3 and race for the lock. That is correct: they
serialize, and the loser finds a moved frontier and takes the stale path.

The frontier branch stays in the **primary checkout**, fast-forwarded by the script
only. Today's invariant — primary clean, on the run branch, `HEAD == retained
commit` — survives unchanged. `R` is built on `F`, so the fast-forward is always
valid.

`require_command_preserved_repository` applies twice per candidate: the worktree's own
HEAD and branch must be unchanged by the command, **and** the primary repository must
be untouched. A verify command that `cd`s into the primary and mutates it is an error,
not a surprise.

**Known throughput ceiling:** the rebase re-measure in step 5 runs under the lock,
because it needs a frontier that cannot move underneath it. Under heavy staleness,
admissions serialize on that re-measurement.

## 10. Adaptive allocation

Pure function in `autoresearch_allocator.py`, called once per refill. All arithmetic
in `Decimal`, consistent with the existing metric discipline.

```
W = last `window` resolved candidates with outcome in {admitted, discarded}

# plateau escape
tail = most recent `plateau_k` exploit candidates in W
if len(tail) == plateau_k and none were admitted:
    return ("explore", "plateau_escape")

# admission rate, optimistic prior for an unseen role
rate(role) = Decimal(1) if role unseen in W
             else admitted(role) / resolved(role)

share_exploit = Decimal("0.5") if rate sum == 0
                else rate(exploit) / (rate(exploit) + rate(explore))

desired_exploit = quantize(share_exploit * max_parallel, to integer, ROUND_HALF_EVEN)
if max_parallel >= 2:
    desired_exploit = clamp(desired_exploit, min_per_role, max_parallel - min_per_role)

# live_<role> = candidates currently started-but-unresolved with that role,
# excluding the slot being filled by this call
deficit_exploit = desired_exploit - live_exploit
deficit_explore = (max_parallel - desired_exploit) - live_explore

return role with the larger deficit, "policy_share"
       tie -> role with fewer live candidates, "policy_tiebreak"
```

`window`, `min_per_role`, and `plateau_k` are required configuration (D9). With
`max_parallel == 1` the floors are skipped, so a single-slot run still alternates by
policy and remains a faithful reduction of the parallel case.

Role semantics in the packet. **Both roles branch from the frontier head** — wins are
never discarded. They differ in hypothesis class:

- **exploit** — receives the last admitted candidate's hypothesis and metric delta;
  instructed to deepen that direction.
- **explore** — receives the hypotheses of the last `window` candidates; instructed
  toward a materially different mechanism, explicitly not a variation of any listed.

Every decision records `role_source: "policy"`. `claim --role explore --role-reason
"<why>"` records `role_source: "override"` and the reason, so replay reproduces every
policy decision and shows every override with its justification.

## 11. Durable tracking and recovery

`slots.json` carries liveness and correlation, never truth. Events remain the only
authority for metric, frontier, and grant accounting.

```json
{ "run_id": "...", "updated_at": "...", "max_parallel": 3,
  "worktree_root": "/abs/...",
  "slots": [ { "slot": 1, "worktree": "/abs/...",
               "branch": "autoresearch/ab12cd34/c0007", "state": "live",
               "candidate": 7, "agent_ref": "agent-3f9a",
               "grant": { "source_id": "local", "kind": "cores", "cores": 4,
                          "label": "workstation" },
               "claimed_at": "...", "lease_expires_at": "..." } ] }
```

`state` ∈ `idle` | `preparing` | `live` | `measuring` | `admitting` | `broken`.
Validated with `require_exact_keys` like every other artifact.

### 11.1 Leases replace PIDs

The script does not own worker processes, so `process_alive` cannot judge a worker.
Liveness is a lease instead:

- `claim` sets `lease_expires_at = now + lease_seconds`, recorded in both
  `slots.json` and `candidate_started`. `lease_seconds` is required (D9).
- The packet instructs the worker to run `heartbeat --candidate <id>` before any long
  operation.
- An expired lease is **never auto-reaped.** `status` reports it as reapable;
  `reap --candidate <id>` resolves it `failed`/`lease_expired`, releases its grant,
  resets the worktree, and frees the slot.
- A zombie agent that later calls `finish` fails, because `finish` requires the
  candidate to be unresolved. Stale work can never be admitted.

`agent_ref` is recorded by `bind --candidate <id> --agent-ref <id>` after spawn. It is
advisory: the script cannot verify a host-assigned id, so it lives in `slots.json`
rather than the event log, and the lease remains the authority.

The admission lock still uses a PID, because its holder is a real CLI process:
`O_CREAT|O_EXCL` with `{run_id, pid, candidate, acquired_at}`, and a holder whose pid
is dead marks the lock stale for explicit `reconcile`.

### 11.2 Recovery

`status` reports, and `reconcile` acts, only on explicit invocation:

| Condition | Action |
|---|---|
| unresolved candidate, lease valid | report live; leave alone |
| unresolved candidate, lease expired | report reapable; `reap` resolves `lease_expired` |
| worktree missing or unexpectedly dirty | slot `broken`, never reused, reported |
| admission lock held by a dead pid | reported stale; `reconcile` clears it |
| `slots.json` disagrees with replayed events | events win; report the divergence |
| outstanding grants exceed bank capacity | hard error; the bank shrank under live candidates |

No silent repair, and no ambiguous slot is ever reused.

## 12. CLI surface

| Command | Change | Purpose |
|---|---|---|
| `init` | extended | requires `--max-parallel`, `--worktree-root`, `--prepare`, `--lease-seconds`, `--max-candidates`, and the allocation knobs; validates docs and bank |
| `claim` | **new** | `--count N`; allocates role and grant, prepares worktrees, writes `candidate_started`, returns N worker packets |
| `bind` | **new** | records `agent_ref` for a claimed candidate |
| `heartbeat` | **new** | extends a candidate's lease |
| `finish` | changed | `--candidate <id> --description <d> --hypothesis <h>`; runs §9 |
| `abandon` | **new** | resolves `failed`/`no_change` or `abandoned`; releases grant, frees the slot |
| `reap` | **new** | resolves an expired-lease candidate |
| `reconcile` | **new** | clears stale locks, marks broken slots |
| `decide` | **new** | appends to `decisions.md`, re-snapshots, logs a `decision` event |
| `compute` | **new** | `detect`; reports observed local capacity, writes nothing |
| `rebank` | **new** | re-reads `compute.json`, appends `bank_changed`; slots above the new capacity drain rather than being killed |
| `status` | extended | per-slot table, live candidates, lease state, bank utilization, allocation state, doc hashes |
| `history`, `report` | extended | candidate-level rows with role, outcome, reason, grant |
| `archive` | extended | `git worktree prune`, optional `--delete-branches` |
| `block`, `resume` | unchanged semantics | |
| `launch`, `stop`, `_controller` | **deleted** | D7 |

## 13. Module layout

`autoresearch.py` is 1584 lines today; adding a scheduler would make it unreadable.
Deleting the controller frees roughly 250 of those lines.

| Module | Purpose |
|---|---|
| `autoresearch.py` | CLI dispatch |
| `autoresearch_core.py` | atomic IO, git primitives, command execution, metric parsing |
| `autoresearch_state.py` | **new** — run and event schemas, `derive_state` replay |
| `autoresearch_allocator.py` | **new** — pure allocation policy |
| `autoresearch_slots.py` | **new** — worktree lifecycle, `slots.json`, leases, admission lock |
| `autoresearch_docs.py` | **new** — doc load, cap, hash, snapshot |
| `autoresearch_bank.py` | **new** — bank parsing, capacity, grant allocation and release, measurement lease |
| `autoresearch_packet.py` | **new** — host-agnostic worker packet generation |
| `autoresearch_report.py` | history, TSV, HTML |

The packet generator is what makes the worker contract mechanical rather than
aspirational: the coordinator cannot spawn a thin prompt, because it does not write
the prompt.

## 14. Structure gates

| Gate | Now | After | Change |
|---|---|---|---|
| `SKILL.md` ≤ 8000 bytes | 7507 | ≤ 8000 | unchanged; SKILL.md rewritten tighter |
| reference `.md` files | `background`, `experiment`, `workflow` | `experiment`, `parallel`, `workflow` | **count stays 3**; `background.md` deleted, `parallel.md` added |
| `autoresearch*.py` modules | 3 | 9 | assert an **exact name set**, as references already do, so adding a module stays a conscious edit |
| CI `runtime-smoke` job | present | deleted | background mode is gone |

`references/background.md` moves to `deprecated/background_2026_08_05.md` before
deletion, per the replacement convention.

The dual-host rename (`name: codex-autoresearch` → `autoresearch`) touches
`SKILL.md`, `tests/test_structure.py:32`, `validate_skill_structure.sh:53`,
`agents/openai.yaml`, `PROTECTED_PREFIXES`, `README.md`, `docs/`, and the eight
translated READMEs. `PROTECTED_PREFIXES` becomes a prefix match on `.agents/skills/`
rather than one hardcoded skill directory.

## 15. Testing

| Level | Coverage |
|---|---|
| unit — allocator | window, optimistic prior, floors, plateau escape, tie-break, override, `max_parallel == 1` |
| unit — replay | `base_metric` mismatch, admit without improvement, discard that moved the frontier, resolved-without-started, reused candidate id, terminal with wrong `unresolved_candidates`, grants exceeding capacity |
| unit — admission table | fast path, stale-improves, stale-no-improve, rebase conflict, guard fail at both T and R |
| unit — bank | capacity math for both kinds, deterministic allocation order, exhaustion returns fewer packets, release on every resolution path, `rebank` shrinking under live grants |
| unit — detect | provenance of each observation, `cpu_count() is None` reported not substituted, nothing written |
| unit — docs | 4 KB cap, hash stability, snapshot deduplication, protected-path rejection |
| unit — config | every required field missing fails init; no code path supplies a default |
| integration — real git | `max_parallel=3` on `tests/e2e-fixtures/counter_reduction`; concurrent claims, a **forced** stale rebase, an expired lease reaped, a broken worktree |
| regression | `--max-parallel 1` still reaches target on the existing fixture |

The integration driver is a Python test coordinator that plays the main thread —
claim, edit, finish — across real concurrent processes. Because the contract is
CLI-level, genuine concurrency is exercised with no model in the loop, which is
stronger coverage than the deleted `runtime-smoke` job provided.

## 16. Risks

| Risk | Mitigation |
|---|---|
| Stale-rebase path is the most intricate logic here | Specified as a decision table in §9, unit-tested per branch |
| Grants are advisory, so an agent can oversubscribe | Accounting is authoritative and auditable; enforcement is explicitly out of scope (§3) |
| `exclusive` measurement collapses parallelism against one bank source | Stated at init; more sources restore it |
| Lease expiry could reap a slow but healthy agent | `lease_seconds` is explicit per run; `heartbeat` in the packet; reaping is always explicit |
| Candidate branches accumulate | Retained deliberately as the audit trail; `archive --delete-branches` |
| Codex multi-agent API may change | Adapter names no tool parameters; degrades to sequential `claim --count 1` |
| Schema 2 breaks in-flight runs | Refused with the existing archive-and-restart error; no migration by design |
| No defaults means a long init command | The confirmation block already surfaces values for approval; a model composes the flags |

## 17. Open item for review

Whether the `codex-autoresearch` → `autoresearch` rename lands with this work or is
deferred to a follow-up. It is the only change here with a wide, mostly mechanical
blast radius (§14).
