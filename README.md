<p align="center">
  <img src="image/banner.png" width="700" alt="Autoresearch">
</p>

<h2 align="center"><b>Aim. Iterate. Arrive.</b></h2>

<p align="center">
  <i>Autonomous, measurable experimentation for Codex.</i>
</p>

<p align="center">
  <a href="https://developers.openai.com/codex/skills"><img src="https://img.shields.io/badge/Codex-Skill-blue?logo=openai&logoColor=white" alt="Codex Skill"></a>
  <a href="https://github.com/leo-lilinxiao/codex-autoresearch"><img src="https://img.shields.io/github/stars/leo-lilinxiao/codex-autoresearch?style=social" alt="GitHub Stars"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="MIT License"></a>
</p>

<p align="center">
  <b>English</b> ·
  <a href="docs/i18n/README_ZH.md">中文</a> ·
  <a href="docs/i18n/README_JA.md">日本語</a> ·
  <a href="docs/i18n/README_KO.md">한국어</a> ·
  <a href="docs/i18n/README_FR.md">Français</a> ·
  <a href="docs/i18n/README_DE.md">Deutsch</a> ·
  <a href="docs/i18n/README_ES.md">Español</a> ·
  <a href="docs/i18n/README_PT.md">Português</a> ·
  <a href="docs/i18n/README_RU.md">Русский</a>
</p>

---

Tell Codex what measurable result you want. Codex inspects the repository, confirms the experiment with you, changes one thing, verifies it, keeps improvements, reverts failures, and repeats until the target is reached.

Autoresearch works for test failures, coverage, type errors, warnings, latency, binary size, reproducible security findings, and any other outcome a command can measure.

## Quick Start

Install from Codex:

```text
$skill-installer install https://github.com/leo-lilinxiao/codex-autoresearch
```

Open a clean Git repository with Full Access:

```bash
codex --dangerously-bypass-approvals-and-sandbox
```

For V2 supporting workers pinned to Luna at maximum reasoning, complete the [optional terminal runtime and custom-agent setup](docs/INSTALL.md#optional-v2-luna-workers) first.

Then invoke the skill:

```text
You:   $autoresearch
       Reduce `python3 scripts/score.py` error_count to 0.

Codex: Baseline: 5
       Target: 0 (lower is better)
       Scope: src/
       Verify: python3 scripts/score.py, JSON key error_count
       Guard: python3 -m pytest -q
       Parallel: 3 candidates from the compute bank

You:   Go.
```

Codex launches the confirmed run. No Codex configuration changes or special prompt syntax are required.

See [Installation](docs/INSTALL.md) for manual and development installs.

## The Loop

```text
inspect evidence
      |
change one focused thing
      |
commit and measure
      |
      +-- improved + guard passes --> keep
      |
      +-- otherwise ---------------> revert
      |
append an audit event
      |
repeat until target
```

The control script owns commits, verification, rollback, and state. Codex owns the hypotheses and code changes.

## Parallel Candidates

Candidates run in parallel by default. The coordinator claims slots, spawns one
subagent per slot using its own host's primitive, and refills each slot as its worker
returns.

| | |
|---|---|
| Isolation | One long-lived Git worktree per slot |
| Allocation | Adaptive split between deepening the best result and trying new ideas |
| Compute | A declared bank of cores and whole machines; each candidate gets a grant |
| Admission | Serialized; a candidate whose base went stale is rebased and re-measured |
| Liveness | Leases, because the control plane does not own worker processes |

Every worker receives the same curated overarching goal and decisions, capped at 16 KB
each, plus its own individual target. It profiles the frontier before its change and the
trial afterward, then reports the measured remaining bottleneck for an admitted run or
the measured improvements and regressions for a discarded run. `finish` adjudicates the
trial, but the slot remains in `reporting` until the worker submits that structured
evidence (up to 16 KB). Status, history, and HTML retain the measured next focus. A host
that cannot spawn concurrent subagents claims one slot at a time and degrades to
sequential execution against the identical state model. Version-2 evidence also labels
diagnostic confidence and orders measured causes, so reports distinguish execution
success from frontier outcome and show improvements, regressions, preserved state,
the remaining bottleneck, and the next experiment. Version-1 evidence stays readable.

## What Gets Confirmed

Before the first write, Codex shows:

- the goal and numeric target;
- repository-relative paths it may change;
- the metric command and explicit parser;
- an optional regression guard;
- the concurrency, worktree root, and lease;
- an optional candidate limit.

Initialization requires a clean named Git branch. One run manages one repository.

## Results

Run artifacts live in `autoresearch-results/` and stay uncommitted:

| Path | Purpose |
|---|---|
| `run.json` | Immutable confirmed configuration |
| `events.jsonl` | Append-only baseline, candidate, stop, and completion history |
| `logs/` | Full metric and guard output |
| `slots.json` | Slot liveness, leases, and outstanding compute grants |
| `docs/` | Content-addressed snapshots of the curated documents |
| `report.html` | Optional, regenerated visual snapshot |

`events.jsonl` is the state history. Missing, malformed, contradictory, or partial state is an error; the skill never guesses a result from old files or conversational memory.

## Review Results

Ask the skill to show the validated experiment history:

```text
$autoresearch show experiment history
```

```text
Autoresearch
Run: 0a516883  Status: complete  Mode: foreground
Metric: error_count  2 -> 0  Target: 0 (lower is better)

SEQ  ITER  EVENT     PREVIOUS  TRIAL  RETAINED  DESCRIPTION
---  ----  --------  --------  -----  --------  ------------------------------------
  0     0  baseline         -      -         2  Initial measurement
  1     1  discard          2      3         2  Broaden parser fallback
  2     2  keep             2      1         1  Fix nested parser branch
  3     3  keep             1      0         0  Remove final parser error
  4     3  complete         -      -         0  retained metric satisfies the target
```

The same validated events can be exported as TSV or rendered as a self-contained static report:

```text
$autoresearch export experiment history as TSV
$autoresearch generate an HTML report
```

The report is written to `autoresearch-results/report.html`. It is a replaceable snapshot, not runtime state.

<p align="center">
  <img src="image/autoresearch-report.png" width="900" alt="Autoresearch HTML report showing metric trajectory and experiment history">
</p>

## Safety Model

- Every trial is a Git commit.
- A non-improving trial or failed guard is reverted with `git revert`.
- Out-of-scope edits, branch changes, HEAD drift, malformed metrics, command failures, timeouts, and generated byproducts stop the run with an exact error and log path.
- Autoresearch artifacts are never staged.
- A run reports `complete` only when the retained metric reaches the confirmed target.
- A genuine external blocker is reported explicitly; a difficult or unsuccessful hypothesis is not treated as blocked.

This strictness is intentional. Silent recovery makes long autonomous runs impossible to trust.

## Good Metrics

The verify command must exit successfully and place one finite number on its final non-empty stdout line. It may instead print a JSON object on that line when Codex names one numeric key explicitly.

```text
7
```

```json
{"error_count": 7, "passed": 12}
```

Use a guard for behavior the metric does not protect, such as a test suite around a latency benchmark. The guard must pass at baseline.

## Documentation

| Guide | Contents |
|---|---|
| [Installation](docs/INSTALL.md) | Install, update, and verify the skill |
| [User Guide](docs/GUIDE.md) | Configuration, lifecycle, state, and troubleshooting |
| [Examples](docs/EXAMPLES.md) | Practical prompts and metric patterns |
| [Contributing](CONTRIBUTING.md) | Architecture and validation for contributors |

## FAQ

**Does installation change my Codex settings?**

No. Installation copies the skill files. Use a current Codex release so foreground runs can use the built-in Goal capability. The optional [V2 Luna worker setup](docs/INSTALL.md#optional-v2-luna-workers) is manual and separate from skill installation.

**Why Full Access?**

Each candidate creates a Git commit and the control plane manages worktrees. Restricted sandboxes may block writes under `.git`, so Full Access is the reliable choice; `workspace-write` remains an explicit option when its limitations are acceptable.

**Can I stop and resume?**

Yes. Interrupt or pause the Goal, then ask `$autoresearch` for status or resume with a new direction. A worker that dies mid-flight is reported by `reconcile` and cleared with `reap`.

**Can it run without Git or across several repos?**

No. Git is the experiment memory and rollback boundary. Use one run per repository so commit ownership and metrics remain unambiguous.

**Is this only for small changes?**

No. One experiment should test one coherent hypothesis. Its size should match the hypothesis, while still being independently measurable and reversible.

## Acknowledgments

Inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch), generalized for Codex and software repositories.

## Citation

```bibtex
@misc{autoresearch,
  author = {Li, Linxiao},
  title = {Autoresearch: Autonomous Goal-Driven Experimentation for Codex},
  year = {2026},
  publisher = {GitHub},
  url = {https://github.com/leo-lilinxiao/codex-autoresearch}
}
```

GitHub also reads [CITATION.cff](CITATION.cff) for its **Cite this repository** menu.

## Star History

<a href="https://www.star-history.com/?repos=leo-lilinxiao%2Fautoresearch&type=timeline&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/image?repos=leo-lilinxiao/codex-autoresearch&type=timeline&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/image?repos=leo-lilinxiao/codex-autoresearch&type=timeline&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/image?repos=leo-lilinxiao/codex-autoresearch&type=timeline&legend=top-left" />
 </picture>
</a>

## License

MIT, see [LICENSE](LICENSE).
