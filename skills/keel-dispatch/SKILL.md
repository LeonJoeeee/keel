---
name: keel-dispatch
description: >-
  Use when splitting engineering tasks, isolating parallel branches in git worktrees, or dispatching side-agent tasks (general research/prototyping, implementation, or local verification) across built-in Antigravity subagents and OpenAI Codex CLI.
---

# Keel Dispatch — Multi-Side Subagent Execution

Resolve `<keel>` as this plugin's root directory (two levels above this `SKILL.md`, i.e., `../../scripts/keel`).

## 1. Proportional task sizing & model-tier routing
- **Single-point, clear, atomic tasks** (1–5 line fixes, quick read-only checks): handle directly or in one lightweight pass — never chain pre-code challenge + worker + reviewer subagents on trivial work.
- **Parallel or context-heavy slices** (independent modules, deep reconnaissance, prototype comparisons, clean-context verification): split along non-overlapping file boundaries so two writers never touch the same file concurrently.
- **Executor & model-tier routing** (`../../config/defaults.json`, explicit human instructions always win):
  - **Research, synthesis, specs/ADRs, and architecture (`general`):** `--executor agy` (`TypeName="self"`, `Model="inherit"`; never `TypeName="research"`, which strips network and MCP access).
  - **Implementation (`implement`) & independent verification (`verify`):** `--executor codex` (`gpt-6.1-sol` at `xhigh`) or `--executor agy` (`Model="inherit"`), with Codex child subagents inheriting the parent run's `(model, effort)` (Codex `general` defaults to `gpt-6.1-sol` at `high`).
  - **Highest-difficulty cross-system arbitration / design fallback on Codex:** `--executor codex --model gpt-6-astra --effort max` (child subagents inherit `gpt-6-astra` at `max`).
  - **Purely mechanical checks / narrow execution helpers:** Codex `gpt-6.1-terra` (`high`) or `gpt-6-luna` (quick single-point checks), or Antigravity `Model="flash"` — never downgrading judgment, research, implementation, or verification tasks.

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
