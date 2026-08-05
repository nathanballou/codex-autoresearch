# Parallel Autoresearch — Phase 2 Implementation Plan (Parallel Engine)

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single linear `iteration` chain with concurrent candidates that branch from a shared frontier, are allocated adaptively between exploiting the best result and exploring new ideas, run in isolated worktrees owned by host-native subagents, and admit through a serialized stale-rebase protocol.

**Architecture:** The Python control plane never spawns a worker. It hands out slots, compute grants, and worker packets; the coordinating model spawns subagents with its host's own primitive (Codex `multi_agent`, Claude Code `Agent`). Durability is the event log plus lease-based liveness, because the script does not own worker processes and cannot use PIDs to judge them.

**Tech Stack:** Python 3.11 (CI-pinned — no 3.12+ APIs), stdlib only, `unittest`, `Decimal` for all metric arithmetic, real `git` worktrees.

**Spec:** [2026-08-05-parallel-autoresearch-design.md](../specs/2026-08-05-parallel-autoresearch-design.md)
**Builds on:** [Phase 1](2026-08-05-parallel-autoresearch-phase1.md) — complete, 29 tests green at `f933a9d`.

---

## Before You Start

**Commits:** Nathan makes all commits, or authorizes a subagent to commit on the feature branch. Never commit to `main`.

**Schema:** Phase 1 shipped `SCHEMA_VERSION = 2` as "schema 2 minus parallelism." Phase 2 completes schema 2 on the same unreleased branch. Do **not** bump to 3 — nothing outside this branch has ever seen a v2 run.

**Baseline:** `python3 -m unittest discover -s tests -q` → `Ran 29 tests ... OK`. Every task must end green.

---

## Sequencing rationale

Three constraints fix the order:

1. **The event model is the foundation.** Slots, grants, roles, and packets all record into `candidate_started` / `candidate_resolved`. Building them before the events exist means writing every call site twice.
2. **`claim` is the convergence point.** It needs a role (allocator), a grant (bank), a worktree (slots), and a packet (docs + packet module). Everything it depends on must exist first, so `claim` lands late.
3. **D9's "no defaults" applies at the point of use.** Requiring `compute.json` at `init` before anything can allocate from it is backwards, and would break all 29 tests for no benefit. The bank becomes required in T7, when `claim` can actually consume it.

```
T1 events ──┬── T2 config ── T3 docs ──┐
            └── T4 bank ── T5 allocator ┼── T7 claim/packet ── T8 admission ── T9 recovery
                           T6 slots ────┘                                          │
                                                            T10 views ── T11 integration ── T12 surfaces
```

---

## File Structure

| File | Responsibility |
|---|---|
| `scripts/autoresearch_state.py` | Gains the candidate event pair and replay invariants 1-9. Loses `iteration`. |
| `scripts/autoresearch_docs.py` | **New.** Load, 4 KB cap, sha256, content-addressed snapshot of the curated docs. |
| `scripts/autoresearch_bank.py` | **New.** Bank parsing, capacity math, deterministic grant allocation and release, measurement lease. |
| `scripts/autoresearch_allocator.py` | **New.** Pure exploit/explore policy. No IO. |
| `scripts/autoresearch_slots.py` | **New.** Worktree lifecycle, `slots.json`, leases, admission lock. |
| `scripts/autoresearch_packet.py` | **New.** Host-agnostic worker packet generation. |
| `scripts/autoresearch.py` | Gains `claim`, `bind`, `heartbeat`, `abandon`, `reap`, `reconcile`, `decide`, `compute`, `rebank`. `finish` takes `--candidate`. |
| `autoresearch/goal.md`, `decisions.md`, `compute.json` | **New, repo-tracked.** Curated by the main thread. |
| `references/parallel.md` | **New.** Replaces the retired `background.md`, keeping the reference count at 3. |

---

## Task 1: Candidate event model and replay invariants

Replaces the `iteration` event with `candidate_started` / `candidate_resolved`. Still strictly sequential — one candidate at a time, no worktrees, no concurrency. Behavior must be indistinguishable from today; the existing tests are the proof.

**Files:** `scripts/autoresearch_state.py`, `scripts/autoresearch.py`, `scripts/autoresearch_report.py`, `tests/test_autoresearch.py`

- [ ] Add `candidate_started` and `candidate_resolved` to `EVENT_FIELDS` per spec §7.2, and remove `iteration`.
- [ ] Rewrite the `derive_state` replay loop to pair started/resolved events, enforcing invariants 1-7 (spec §7.3). Frontier moves **only** on `outcome: admitted`.
- [ ] **Drop the revert commit.** A discarded candidate leaves its commit on its own branch; `head` and `retained_metric` must be unchanged. This removes `safely_revert_after_error`'s revert-on-discard path and the `revert_commit` field.
- [ ] Rewrite `finish_iteration` as `finish_candidate`, emitting the started/resolved pair around one experiment.
- [ ] Add `unresolved_candidates` to every terminal event.
- [ ] Update `autoresearch_report.py` for the new event names and the removed `revert_commit` column.
- [ ] Update every affected test. The two that assert revert behavior (`test_non_improving_trial_is_reverted_and_recorded`, and the TSV assertions in `test_history_table_and_tsv_render_discard_without_changing_events`) now assert the candidate branch retains the commit and the frontier did not move.

**Verify:** `Ran 29 tests ... OK`, and a real init → discard → keep → complete loop where `git log` shows no revert commit and the discarded work survives on its candidate branch.

---

## Task 2: Run configuration for parallelism

**Files:** `scripts/autoresearch_state.py`, `scripts/autoresearch.py`, `tests/test_autoresearch.py`

- [ ] Add the `parallel` and `docs` blocks to `RUN_KEYS` and `validate_run` per spec §7.1.
- [ ] `init` gains `--max-parallel <n|bank>`, `--worktree-root`, `--prepare`, `--lease-seconds`, and the three allocation knobs. All required (D9) except where the spec marks a value nullable-but-explicit.
- [ ] Reject a `--worktree-root` inside the repository. A worktree under `autoresearch-results/` makes a root-level `pytest -q` collect every test N+1 times and silently corrupts the metric (spec §5.3).
- [ ] Update the test `init` helper to pass the new required flags.

**Verify:** init fails with an actionable message when any required flag is absent; `Ran 30 tests ... OK`.

---

## Task 3: Curated documents

**Files:** `scripts/autoresearch_docs.py` (new), `scripts/autoresearch_core.py`, `scripts/autoresearch.py`, `autoresearch/` (new), `tests/test_autoresearch.py`

- [ ] Create `autoresearch_docs.py`: load, enforce the **4 KB cap** per file as a hard error naming the file and its size, sha256, and snapshot to `autoresearch-results/docs/<sha256>.<name>`.
- [ ] Add `"autoresearch"` to `PROTECTED_PREFIXES` in `autoresearch_core.py:22`. This makes `normalize_scopes` refuse it as a scope and makes a candidate editing a doc trip the existing out-of-scope check — write authority enforced by machinery already present.
- [ ] Scaffold `autoresearch/goal.md` and `autoresearch/decisions.md` with the repo's own overarching goal, as a worked example.
- [ ] `init` requires a non-empty `goal.md`, treats a missing `decisions.md` as empty, snapshots all three, and records their hashes in `run.json.docs`.
- [ ] Add `decide --add "<note>"`: append a bullet to `decisions.md`, re-snapshot, append a `decision` event carrying the new hash. `derive_state` must accept `decision` events without moving the frontier.

**Verify:** a 5 KB `goal.md` fails init with its size named; `decide` twice produces two distinct snapshot files and two events; a candidate editing `autoresearch/decisions.md` is rejected as out-of-scope.

---

## Task 4: Compute bank

**Files:** `scripts/autoresearch_bank.py` (new), `scripts/autoresearch.py`, `scripts/autoresearch_state.py`, `tests/test_autoresearch.py`

- [ ] Create `autoresearch_bank.py`: parse `compute.json`, compute capacity (`floor(cores / cores_per_candidate)` for `cores` entries, `capacity` for `node` entries), and allocate/release grants in declared order, cores before nodes, first entry with free capacity wins.
- [ ] Every field required. An unknown `kind` is a hard error, never a silent skip.
- [ ] Add `compute detect`: report observed local capacity with the provenance of each number — `len(os.sched_getaffinity(0))` where present, cgroup v2 `/sys/fs/cgroup/cpu.max`, `os.cpu_count()`. **Writes nothing.** `os.cpu_count()` returning `None` is reported as unavailable, never substituted with 1. CI pins 3.11, so `os.process_cpu_count()` is unavailable.
- [ ] Add the `bank_changed` event and the `rebank` command. Shrinking the bank below outstanding grants is a hard error.
- [ ] Add replay invariant 9: outstanding grants never exceed capacity at any point in the log.

**Verify:** capacity math for both kinds; exhaustion returns fewer grants rather than over-allocating; `compute detect` writes nothing; `rebank` under live grants fails.

---

## Task 5: Adaptive allocator

**Files:** `scripts/autoresearch_allocator.py` (new), `tests/test_allocator.py` (new)

- [ ] Implement spec §10 as a pure function — no IO, all arithmetic in `Decimal`.
- [ ] Unit tests: window boundary, optimistic prior for an unseen role, `min_per_role` floors, plateau escape after `plateau_k` unadmitted exploits, tie-break, override recording, and `max_parallel == 1` reducing to policy alternation.

**Verify:** `Ran 30+ tests ... OK`. This task adds a new test file; update the structure gate's module-name set for `autoresearch_allocator.py`.

---

## Task 6: Slots, worktrees, and leases

**Files:** `scripts/autoresearch_slots.py` (new), `tests/test_autoresearch.py`

- [ ] `slots.json` per spec §11, validated with `require_exact_keys` like every other artifact.
- [ ] Long-lived worktree per slot, created outside the repo, `--prepare` run once per slot.
- [ ] Slot reset between candidates: `checkout -B`, `reset --hard`, `clean -df`. **Not `-x`** — cleaning ignored files would delete `node_modules`/`.venv` and destroy the per-slot amortization worktrees exist to buy.
- [ ] Lease set on claim, extended by `heartbeat`, never auto-reaped.
- [ ] Admission lock via `O_CREAT|O_EXCL` carrying `{run_id, pid, candidate, acquired_at}`. The lock holder is a real CLI process, so PID-based stale detection is valid here even though it is not for workers.

**Verify:** two concurrent `finish` processes serialize on the lock; a worktree is reused across candidates without re-running `--prepare`; `clean -df` preserves an ignored `node_modules/`.

---

## Task 7: Claim, bind, heartbeat, and the worker packet

**Files:** `scripts/autoresearch_packet.py` (new), `scripts/autoresearch.py`

- [ ] `claim --count N`: allocate role and grant, prepare worktrees, write `candidate_started`, return N packets.
- [ ] The packet states the goal, decisions, individual optimization target, role instruction, grant, worktree path, candidate id, lease deadline, and the exact `finish` command. Generating it in code is what makes the worker contract mechanical — the coordinator cannot spawn a thin prompt because it does not write the prompt.
- [ ] Export `AUTORESEARCH_CORES`, `AUTORESEARCH_GRANT_KIND`, `AUTORESEARCH_GRANT_SOURCE`, `AUTORESEARCH_GRANT_LABEL`.
- [ ] `bind --candidate <id> --agent-ref <id>` records the host-assigned id in `slots.json` only — advisory, since the script cannot verify it.
- [ ] `heartbeat --candidate <id>` extends the lease.
- [ ] **`compute.json` becomes required at init here** (D9), now that something consumes it.

**Verify:** `claim --count 3` returns three packets with distinct slots, worktrees, and grants; a fourth claim against a 3-capacity bank returns nothing and names the exhausted entries.

---

## Task 8: Admission with stale rebase

The most intricate logic in the phase. Implement spec §9 as a decision table and unit-test every branch.

**Files:** `scripts/autoresearch.py`, `tests/test_autoresearch.py`

- [ ] `finish --candidate <id>`: commit, measure, and only then acquire the lock — measurement is the slow step and holding a lock across it would serialize the feature away.
- [ ] Fast path when `base_commit == frontier`; stale path rebases, re-measures under the lock, and re-checks improvement against the moved frontier.
- [ ] Both `require_command_preserved_repository` checks per candidate: the worktree's own HEAD and branch unchanged, **and** the primary repository untouched.
- [ ] Release the grant on every resolution path, including failures.

**Verify:** unit tests for fast path, stale-improves, stale-no-improve, rebase conflict, and guard failure at both `T` and `R`. A forced-stale integration case where candidate B admits after candidate A moved the frontier.

---

## Task 9: Recovery

**Files:** `scripts/autoresearch.py`, `scripts/autoresearch_slots.py`, `tests/test_autoresearch.py`

- [ ] `abandon`, `reap`, `reconcile` per spec §11.2.
- [ ] An expired lease is reported as reapable, never auto-reaped.
- [ ] `finish` on an already-resolved candidate fails, so a zombie agent can never admit stale work.
- [ ] No silent repair; no ambiguous slot is ever reused.

**Verify:** an expired lease is reported but not acted on until `reap`; a zombie `finish` after `reap` is rejected; a stale lock held by a dead PID is cleared only by explicit `reconcile`.

---

## Task 10: Candidate-level views

**Files:** `scripts/autoresearch_report.py`, `scripts/autoresearch.py`

- [ ] `status`: per-slot table, live candidates, lease state, bank utilization, allocation state, doc hashes.
- [ ] `history` and `report`: candidate rows with role, outcome, reason, and grant.
- [ ] Rename the `ITER` column to `CAND` — Phase 1 deliberately deferred this until the underlying event stopped being called `iteration`, which happens in Task 1.

---

## Task 11: Parallel integration test

**Files:** `tests/test_parallel.py` (new)

- [ ] A Python test coordinator plays the main thread — claim, edit, finish — across real concurrent processes against `tests/e2e-fixtures/counter_reduction` with `max_parallel=3`.
- [ ] Cover: concurrent claims, a **forced** stale rebase, an expired lease reaped, a broken worktree, and bank exhaustion.
- [ ] Because the contract is CLI-level, this exercises genuine concurrency with no model in the loop — stronger coverage than the deleted `runtime-smoke` job provided.
- [ ] Regression: `--max-parallel 1` still reaches target on the existing fixture.

---

## Task 12: Model-facing surfaces

**Files:** `references/parallel.md` (new), `SKILL.md`, `tests/test_structure.py`, `scripts/validate_skill_structure.sh`, `scripts/run_skill_e2e.sh`, `CONTRIBUTING.md`, `docs/`

- [ ] Write `references/parallel.md`: the coordinator loop, the host adapter table (spec §5.2, naming no tool parameters — those change between releases), doc curation duties, and recovery.
- [ ] Rewrite `SKILL.md` for the parallel loop, staying at or under **8000 bytes**. It is 6335 now.
- [ ] Update both structure gates: references back to 3, modules to the new exact name set.
- [ ] **Add every new module to `copy_skill()` in `run_skill_e2e.sh`.** It hardcodes the script list; a missed module fails `foreground-smoke` with `ModuleNotFoundError`. This bit Phase 1 and was only caught by advance review.
- [ ] Sync `docs/i18n/` — eight translated READMEs still describe background mode, deferred from Phase 1.

---

## Risks

| Risk | Mitigation |
|---|---|
| Task 1 changes replay for every existing test at once | It is deliberately first and behavior-preserving; the 29 existing tests are the oracle |
| Stale rebase is the most intricate logic | Decision table in spec §9, unit-tested per branch before the integration test |
| Worktrees inside the repo corrupt the metric silently | Init rejects it (Task 2) |
| Grants are advisory, so an agent can oversubscribe | Accounting is authoritative and auditable; enforcement is explicitly out of scope |
| `copy_skill()` drift | Called out explicitly in Task 12 |

## Open items

1. `cores_per_candidate`, `lease_seconds`, and the allocation knobs have no defaults by D9 — every run states them. Confirm the resulting `init` command length is acceptable in practice, or revisit D9 for the allocation knobs specifically.
2. The `codex-autoresearch` → `autoresearch` rename (spec §17) is still unscheduled.
