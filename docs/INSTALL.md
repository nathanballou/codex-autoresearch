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
cp -R autoresearch your-project/.agents/skills/autoresearch
```

## Manual User Install

Use this for all projects owned by the current user:

```bash
git clone https://github.com/leo-lilinxiao/codex-autoresearch.git
mkdir -p ~/.agents/skills
cp -R autoresearch ~/.agents/skills/autoresearch
```

Do not install both a repository copy and a user copy unless you intentionally want two independently discovered versions.

## Development Symlink

```bash
git clone https://github.com/leo-lilinxiao/codex-autoresearch.git
mkdir -p your-project/.agents/skills
ln -s "$(pwd)/autoresearch" your-project/.agents/skills/autoresearch
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
