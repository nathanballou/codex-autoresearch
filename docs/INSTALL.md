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

## Skill Installer

In Codex, run:

```text
$skill-installer install https://github.com/leo-lilinxiao/autoresearch
```

Then open the target repository and invoke `$autoresearch`.

## Manual Repository Install

Use this when the skill should travel with one project:

```bash
git clone https://github.com/leo-lilinxiao/autoresearch.git
mkdir -p your-project/.agents/skills
cp -R autoresearch your-project/.agents/skills/autoresearch
```

## Manual User Install

Use this for all projects owned by the current user:

```bash
git clone https://github.com/leo-lilinxiao/autoresearch.git
mkdir -p ~/.agents/skills
cp -R autoresearch ~/.agents/skills/autoresearch
```

Do not install both a repository copy and a user copy unless you intentionally want two independently discovered versions.

## Development Symlink

```bash
git clone https://github.com/leo-lilinxiao/autoresearch.git
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
