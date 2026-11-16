# Keel

Lightweight multi-side engineering spine for **Google Antigravity** and **OpenAI Codex CLI** (with a pluggable executor interface for future backends).

Keel keeps human attention on **first-principles core needs**, **genuine product/architectural tradeoffs**, and **explicit authorization of irreversible actions**, while giving parallel subagents global context alignment, isolated workspaces, clean-context active verification, and deterministic merge guardrails — without bloated prompts or custom GitHub comment state machines.

## Install (Antigravity Plugin)

Symlink `keel` directly into `~/.gemini/config/plugins/`:

```sh
ln -s /path/to/keel ~/.gemini/config/plugins/keel
agy plugin validate --strict ~/.gemini/config/plugins/keel
```

Optionally symlink the CLI onto your `PATH`:

```sh
ln -sf /path/to/keel/scripts/keel ~/.local/bin/keel
```

## CLI Overview

```sh
# 1. Isolated git worktrees (.agents/worktrees/<slug>, shared across all executors)
keel worktree create <slug> [--base origin/main]
keel worktree list
keel worktree cleanup <slug> --pr <merged_pr_number>

# 2. Universal multi-side subagent dispatch (general | implement | verify)
keel agent run \
  --executor codex|agy \
  --mode read|write \
  --preset general|implement|verify \
  --worktree <slug> \
  --why "How this task serves the project's Core need" \
  --task "Goal, scope boundaries, and done check"

keel agent resume <run_id> --message-file <followup.md>

# 3. Stateless PR verification, pure-rebase proof, and guarded squash merge
keel pr verify <pr_number> [--executor codex|agy] [--publish]
keel pr status <pr_number>
keel pr compare --old-base <SHA> --old-head <SHA> --base <SHA> --head <SHA>
keel pr merge <pr_number> [--old-base <SHA> --old-head <SHA>] [--execute]
```

## Tests

```sh
python3 -m unittest discover -s tests -v
```

See [docs/requirements.md](docs/requirements.md) for the first-principles baseline and [docs/architecture.md](docs/architecture.md) for adapter extension details.
