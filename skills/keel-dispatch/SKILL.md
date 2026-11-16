---
name: keel-dispatch
description: >-
  Use when splitting engineering tasks, isolating parallel branches in git worktrees, or dispatching side-agent tasks (general research/prototyping, implementation, or local verification) across built-in Antigravity subagents and OpenAI Codex CLI.
---

# Keel Dispatch — Multi-Side Subagent Execution

Resolve `<keel>` as this plugin's root directory (two levels above this `SKILL.md`, i.e., `../../scripts/keel`).

## 1. Proportional task sizing (do not over-spawn)
- **Single-point, clear, atomic tasks** (1–5 line fixes, quick read-only checks): handle directly or in one lightweight pass — never chain pre-code challenge + worker + reviewer subagents on trivial work.
- **Parallel or context-heavy slices** (independent modules, deep reconnaissance, prototype comparisons, clean-context verification): split along non-overlapping file boundaries so two writers never touch the same file concurrently.
- **Executor routing defaults** (`../../config/defaults.json`, explicit human instructions always win):
  - Architecture, specs/ADRs, open-ended research & synthesis → `--executor agy` (`Model="inherit"`, `TypeName="self"`, never `TypeName="research"` which strips network and MCP).
  - Bounded coding, bug fixes, tests, pipelines, or cross-model verification → `--executor codex` (`gpt-6.1-sol`).

## 2. Unified worktree lifecycle (`mode=write`)
All executors share `.agents/worktrees/<slug>` so Antigravity, Codex CLI, and future executors can seamlessly hand off the same isolated branch without polluting the main checkout:

```sh
# Create an isolated worktree on branch task/<slug> (auto-syncs gitignored root AGENTS.md/GEMINI.md)
<keel>/scripts/keel worktree create <slug> --project <repo_dir> [--base origin/main]

# Inspect active worktrees
<keel>/scripts/keel worktree list --project <repo_dir>

# Safe teardown after PR merges (or --discard when human authorizes dropping unmerged work)
<keel>/scripts/keel worktree cleanup <slug> --project <repo_dir> --pr <number>
```

## 3. Universal subagent dispatch (`keel agent`)
Every subagent run automatically receives the repository's `AGENTS.md` (`## Core need`) plus a concise local `brief.md` bridging global purpose (`--why`) to local execution (`--task`) and a thin preset (`general`, `implement`, or `verify`):

```sh
# Dispatch any task (run Codex in background when non-trivial so you stay responsive)
<keel>/scripts/keel agent run \
  --project <repo_dir> \
  --executor codex|agy \
  --mode read|write \
  --preset general|implement|verify \
  --worktree <slug> \
  --why "How this slice serves the project's Core need and what global invariants must hold" \
  --task "Concrete goal, allowed file scope, and deterministic done-check commands"

# Continue an existing run (e.g., addressing review feedback)
<keel>/scripts/keel agent resume <run_id> --project <repo_dir> --message-file <followup.md>
```

### Handling `--executor agy` output (zero prompt echoing)
`keel agent run --executor agy` writes `.agents/runs/<run_id>/brief.md` and outputs a compact JSON descriptor (`type_name`, `model`, `workspace`, and a short file-pointer `prompt`). Pass those exact lightweight fields to `invoke_subagent` (or `send_message` on resume) — **never** read `brief.md` just to copy-paste its full text through your own output tokens.
