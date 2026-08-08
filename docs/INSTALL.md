# Installation

## Requirements

- a current Codex CLI release with Skills and Goals;
- Python 3.11 or newer;
- Git;
- a configured Git author and committer identity;
- a clean, named branch for each new run.

## Optional V2 Luna Workers

Use this setup when the terminal session running autoresearch will delegate supporting work to V2 subagents pinned to Luna at maximum reasoning. Runtime `0.147.0-alpha.10` is the minimum version verified to start this custom Luna role; `0.147.0-alpha.1.2` rejects it.

Install and verify the tested terminal runtime:

```bash
npm install --global @openai/codex@0.147.0-alpha.10
hash -r
command -v codex
codex --version
```

Add the following to `~/.codex/config.toml`:

```toml
[features]
multi_agent = true
multi_agent_v2 = true

[multi_agent_v2]
max_concurrent_threads_per_session = 18
expose_spawn_agent_model_overrides = true

[agents]
max_concurrent_threads_per_session = 8
default_subagent_model = "gpt-5.6-luna"
default_subagent_reasoning_effort = "max"

[agents.luna]
description = "General-purpose worker pinned to Luna at maximum reasoning."
config_file = "agents/luna.toml"
```

Create `~/.codex/agents/luna.toml`:

```toml
name = "luna"
description = "General-purpose worker pinned to Luna at maximum reasoning."
model = "gpt-5.6-luna"
model_reasoning_effort = "max"
developer_instructions = """
Complete the delegated task precisely and return concise, evidence-backed results to the parent agent.
Do not substitute another model.
"""
```

Start a fresh terminal process. Spawn the custom role with `agent_type = "luna"` and `fork_turns = "none"`, without explicit model or reasoning overrides. Verify the child session reports `model=gpt-5.6-luna`, `effort=max`, and `multi_agent_version=v2`. The 18-thread V2 cap is session-wide, not per worker thread.

This setup is manual and optional. Installing the autoresearch skill itself does not modify global configuration or install a runtime.

Full Access is recommended because autoresearch creates and reverts Git commits:

```bash
codex --dangerously-bypass-approvals-and-sandbox
```

## Claude Code

The skill is host-neutral. Claude Code reads the same `SKILL.md`, runs the same control
script, and shares the same run state, so a run started in Codex continues in Claude
Code and back again.

Install for one project:

```bash
mkdir -p .claude/skills
cp -R /path/to/autoresearch .claude/skills/autoresearch
```

Or for every project:

```bash
mkdir -p ~/.claude/skills
cp -R /path/to/autoresearch ~/.claude/skills/autoresearch
```

Invoke it by name, or let Claude select it from the description.

### Concurrency

Measured on this machine: **16 concurrent subagents run successfully.** Sixteen were
dispatched at once, all sixteen held a 60-second task simultaneously, and none were
rejected or queued.

Dispatch is ramped rather than instantaneous: starts are roughly 2 seconds apart, so
all sixteen are running about 31 seconds after the first. That matters only for very
short candidates. Autoresearch candidates run for minutes, so the ramp is noise.

Declare that pool as an `agents` bank entry, where capacity is a subagent count rather
than a core count:

```json
{
  "cores_per_candidate": 1,
  "measurement": "parallel",
  "bank": [
    {
      "id": "claude-subagents",
      "kind": "agents",
      "slots": 16,
      "label": "Claude Code subagent pool"
    }
  ],
  "workers": {
    "simple": { "model": "haiku", "thinking_tokens": 4000 },
    "standard": { "model": "sonnet", "thinking_tokens": 16000 },
    "complex": { "model": "sonnet", "thinking_tokens": 32000 }
  }
}
```

`claim` assigns each candidate a tier and returns its `model` and `thinking_tokens`.
Pass them through when you spawn the subagent. Deepening a result that just paid off is
close to mechanical and gets the cheap tier; escaping a plateau gets the largest budget,
because that is where the hard reasoning actually is.

Re-measure before trusting 16 on a different machine or plan. The number belongs in the
bank precisely so it is a declared fact you can change, not an assumption in the code.

### Continuing a run from another chat

Run state lives in `autoresearch-results/` inside the repository, not in any chat. Any
session on either host picks up an existing run:

```bash
python3 <skill-root>/scripts/autoresearch.py status --repo <repo>
```

Whatever the previous session was, `status` replays the event log and reports the exact
frontier, the live slots, and any expired leases. There is no shared service to
configure and nothing to export: the repository is the backend.

If the previous session died mid-flight, `reconcile` reports what it left behind and
`reap` clears it.

## Prime Agent

Prime Agent reads the same `SKILL.md`, runs the same control script, and shares the same
run state, so a run started in Codex or Claude Code continues here and back again. What
differs is the host adapter: workers are RLM child sessions, and the loop is driven by
Prime Agent's own continuation machinery rather than by a chat turn.

Install for one project:

```bash
mkdir -p .prime/agent/skills
cp -R /path/to/autoresearch .prime/agent/skills/autoresearch
```

Or for every project:

```bash
mkdir -p ~/.prime/agent/skills
cp -R /path/to/autoresearch ~/.prime/agent/skills/autoresearch
```

Prime Agent also scans `~/.agents/skills/` and `.agents/skills/`, so a Manual User or
Manual Repository install below is already discoverable and needs no second copy. Install
one or the other: a duplicate name warns and the first copy found wins.

The directory must be named `autoresearch` to match the skill's frontmatter. Run
`/reload` to pick up a new skill without restarting, and invoke it by name or with
`/skill:autoresearch`.

### Concurrency

Workers are RLM children spawned from the IPython kernel, one call per claimed packet:

```python
handle = await rlm(packet, name="c0007")
```

The call admits the child and returns immediately; it never returns the child's answer.
That costs nothing here, because an autoresearch worker calls `finish` itself and the
coordinator reads the outcome from `status`. The default `RLM_MAX_DEPTH` of 1 is enough:
the root spawns workers and workers delegate to no one.

**Eight concurrent workers per root session** is the declared ceiling. Prime Agent
publishes no per-session child cap, so this is a policy number rather than a measured
limit — which is exactly why it belongs in the bank, where it is a declared fact you can
change:

```json
{
  "cores_per_candidate": 1,
  "measurement": "parallel",
  "bank": [
    {
      "id": "prime-agent-rlm",
      "kind": "agents",
      "slots": 8,
      "label": "Prime Agent RLM child sessions"
    }
  ],
  "workers": {
    "simple": { "model": "openai-codex/gpt-5.6-sol", "thinking_tokens": 4000 },
    "standard": { "model": "openai-codex/gpt-5.6-sol", "thinking_tokens": 16000 },
    "complex": { "model": "openai-codex/gpt-5.6-luna", "thinking_tokens": 32000 }
  }
}
```

Take the tier selectors from `await rlm.find_models()`. A model that is not an exact
`provider/model` match fails the spawn instead of quietly falling back to another one.

`rlm()` accepts only `name` and `model`, and rejects anything else rather than ignoring
it, so `thinking_tokens` cannot be set per worker: children inherit the session's
thinking level. Set it once at launch with `--thinking`, and read the tier budget as the
intent the packet records.

### The Improvement Loop

Autoresearch does not drive itself. On Codex an official Goal re-enters the run each
turn. Prime Agent splits that job across two mechanisms, and a third carries what the run
learned into the next one.

**Persistent goal — what the run is.** After `init` returns a run id, the model records
the objective so it survives compaction, detach, and restart:

```python
await goal.create("autoresearch <run8>: drive <metric> from <baseline> to <target>")
```

Goal state is host-owned and outlives the terminal client. A run can also be seeded at
launch:

```bash
prime-agent --goal "autoresearch: drive failure_count to 0" --thinking high
```

**Autonomous mode — what keeps it moving.** The goal stores the objective; autonomous
mode decides whether to inject another continuation. Point its completion gate at the
run's own status, so the session may not finish while the metric is short of target:

```bash
prime-agent --autonomous \
  --autonomous-gate 'python3 <skill-root>/scripts/autoresearch.py status --repo <repo> | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin)[\"status\"] == \"complete\" else 1)"' \
  --autonomous-max-continuations 50 \
  --autonomous-max-turns 200 \
  --autonomous-timeout-ms 21600000 \
  "Resume the active autoresearch run"
```

The gate reads validated state rather than a claim: `status` replays `events.jsonl` and
exits non-zero until the retained metric reaches the confirmed target. A run that is not
initialized, or whose state fails validation, also fails the gate, so a broken run
continues rather than being reported finished.

Raise every limit you intend to allow. The defaults — 3 continuations, 12 turns, 80,000
tokens, 30 minutes — are far below a real run, and the run stops at whichever binds
first.

**Refinement — what survives the run.** `decisions.md` is per-run memory and dies with
the run. When a decision is a lesson about how to work rather than a fact about this
repository, the model pushes it into the continual harness:

```python
await refine.run("workers that rewrite the parser before reading its tests always discard")
```

Refinement is scheduled, not immediate: it applies when the turn ends, then the harness
rebuilds the system prompt and the run resumes. Keep it session-local unless the lesson
is repository-independent, in which case pass `global_=True` to write it to
`~/.prime/agent/harness/`.

Harness state lives in the session artifact directory, so `refine` is registered only for
a persisted session. Verified on 0.7.1: with `--no-session` the skill is absent from the
session entirely and the run keeps only `decisions.md`. Do not disable the session for a
run you want to learn from.

### Verify

From the target repository:

```bash
prime-agent -p "List your available skills by name."
```

`autoresearch` should appear. A valid installation then behaves exactly as it does on any
other host: it inspects the repository without editing it, proposes a metric, target,
scope, guard, and run mode, and waits for approval before creating
`autoresearch-results/`.

## Skill Installer

In Codex, run:

```text
$skill-installer install https://github.com/leo-lilinxiao/codex-autoresearch
```

Then open the target repository and invoke `$autoresearch`.

## Manual Repository Install

Use this when the skill should travel with one project:

```bash
git clone https://github.com/leo-lilinxiao/codex-autoresearch.git
mkdir -p your-project/.agents/skills
cp -R codex-autoresearch your-project/.agents/skills/autoresearch
```

## Manual User Install

Use this for all projects owned by the current user:

```bash
git clone https://github.com/leo-lilinxiao/codex-autoresearch.git
mkdir -p ~/.agents/skills
cp -R codex-autoresearch ~/.agents/skills/autoresearch
```

Do not install both a repository copy and a user copy unless you intentionally want two independently discovered versions.

## Development Symlink

```bash
git clone https://github.com/leo-lilinxiao/codex-autoresearch.git
mkdir -p your-project/.agents/skills
ln -s "$(pwd)/codex-autoresearch" your-project/.agents/skills/autoresearch
```

Edits to the source checkout are then visible through the symlink.

## Verify

Open Codex in the target repository, type `$`, and select `autoresearch`. A valid installation should:

1. inspect the repository without editing it;
2. propose a metric, target, scope, guard, and run mode;
3. wait for approval before creating `autoresearch-results/`.

The skill does not modify Codex configuration. The optional V2 Luna setup above is a separate manual prerequisite for workflows that delegate supporting work.

## Update

- Skill installer: run the installer again with the same repository URL.
- Copied install: replace the installed `autoresearch` directory with a fresh checkout.
- Symlink: run `git pull` in the source checkout.

Keep only one discovered copy for a given scope to avoid duplicate skill entries.
