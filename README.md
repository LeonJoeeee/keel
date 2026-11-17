# Keel

Pure-Git, multi-executor engineering spine for **Google Antigravity** and **OpenAI Codex CLI** (`gpt-6-astra @ max`, with a pluggable `1..N` executor interface for future backends).

Keel keeps human attention focused on **first-principles core needs**, **neutral product/architectural tradeoffs**, and **explicit authorization of irreversible actions**, while giving parallel subagents global context registration, isolated local `.agents/worktrees/<slug>` branches, `main`-anchored `docs/tasks/` tracking, clean-context active verification (with 1-line Canary mutation checks), and guarded atomic landing — with zero external dependencies or GitHub Web PR coupling.

## Install (Antigravity Plugin)

Install per-entry plugin symlinks into `~/.gemini/config/plugins/keel` (excluding repo-only files like root `AGENTS.md`, `docs/`, and `tests/`) and link `keel` onto your `PATH` (`~/.local/bin/keel`):

```sh
./scripts/install-plugin
~/.local/bin/agy plugin validate --strict ~/.gemini/config/plugins/keel
```

## CLI Overview

```sh
# 1. Isolated local git worktrees (.agents/worktrees/<slug>, shared across all executors)
keel worktree create <slug> [--base main]
keel worktree list
keel worktree cleanup <slug> [--discard]

# 2. Universal multi-side subagent dispatch (general | implement | verify)
keel agent run \
  --executor codex|agy \
  --mode read|write \
  --preset general|implement|verify \
  --worktree <slug> \
  --why "How this task serves the project's Core need" \
  --task "Goal, scope boundaries, and deterministic done-checks"

keel agent resume <run_id> --message-file <followup.md>

# 3. Pure-Git branch verification, pure-rebase proof, and guarded atomic landing
keel branch verify <slug_or_branch> [--executor codex|agy] [--dual] [--check "<cmd>"]
keel branch record-verdict <slug_or_branch> --verdict-file <path> --head-sha <sha> --executor agy
keel branch status [<slug_or_branch>]
keel branch compare --old-base <SHA> --old-head <SHA> --base <SHA> --head <SHA>
keel branch land <slug_or_branch> [--squash|--ff-only] [--execute | --discard --reason "<why>"]
```

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -W default -m unittest discover -s tests -v
```

See [docs/requirements.md](docs/requirements.md) for the first-principles baseline and [docs/architecture.md](docs/architecture.md) for adapter extension details.
