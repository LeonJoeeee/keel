---
name: keel-dispatch
description: >-
  Use when splitting engineering tasks, isolating parallel branches in git worktrees, or dispatching side-agent tasks (general research/prototyping, implementation, or local verification) across built-in Antigravity subagents and OpenAI Codex CLI.
---

# Keel Dispatch — Multi-Side Subagent Execution

Resolve `<keel>` as this plugin's root directory (two levels above this `SKILL.md`, i.e., `../../scripts/keel`).

## 1. Proportional task sizing & model-tier routing
- **Single-point, clear, atomic tasks** (1–5 line fixes, quick read-only checks): handle directly or in one lightweight pass — never chain pre-code challenge + worker + reviewer subagents on trivial work (`D1`, `NR1`).
- **Stage multi-subsystem or large (`>500–800` LOC) refactors as sequential small slices:** break cross-cutting overhauls into vertical `docs/tasks/` slices and verify + land each slice cleanly onto `main` before branching the next, rather than stacking multiple subsystems onto one giant branch that triggers multi-round "peeling the onion" verification loops.
- **Parallel or context-heavy slices** (independent modules, deep reconnaissance, prototype comparisons, clean-context verification): commit any non-trivial task spec in `docs/tasks/active/<YYYYMMDD>-<slug>.md` on `main` first, then split along non-overlapping file boundaries so two writers never touch the same file concurrently.
- **Executor & model-tier routing** (`../../config/defaults.json`, explicit human instructions and CLI flags `--model` / `--effort` always win):
  - **Research, synthesis, specs/ADRs, and architecture (`general`):** `--executor agy` (`TypeName="self"`, `Model="inherit"`; never `TypeName="research"`, which strips network and MCP access) or `--executor codex` (`gpt-6-astra` at `max`).
  - **Implementation (`implement`) & independent verification (`verify`):** `--executor codex` (defaults to `gpt-6-astra` at `max`, with child subagents inheriting `(model, effort)`) or `--executor agy` (`Model="inherit"`).
  - **Purely mechanical checks / narrow execution helpers:** `--model gpt-6.1-terra --effort high`, `--model gpt-6-luna`, or Antigravity `Model="flash"` — never downgrading judgment, research, implementation, or verification tasks.

## 2. Unified pure-Git worktree lifecycle (`mode=write`)
All executors share `.agents/worktrees/<slug>` on local branch `task/<slug>` so Antigravity, Codex CLI, and future backends can collaborate without touching the primary checkout:

```sh
# Create an isolated worktree on branch task/<slug> from local main (auto-syncs gitignored root AGENTS.md/GEMINI.md)
<keel>/scripts/keel worktree create <slug> --project <repo_dir> [--base main]

# Inspect active worktrees
<keel>/scripts/keel worktree list --project <repo_dir>

# Safe teardown after local branch landing (or --discard when human authorizes dropping unintegrated work)
<keel>/scripts/keel worktree cleanup <slug> --project <repo_dir>
```

## 3. Universal subagent dispatch (`keel agent`)
`keel agent run` automatically handles cross-executor context registration (injecting `rules/AGENTS.md` sections 1–3 for non-plugin executors like Codex, injecting `<repo>/AGENTS.md` whenever not natively loaded with identical bytes, and setting Codex `-s read-only` in `mode=read`):

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

# Continue an existing run (e.g., addressing verification feedback)
<keel>/scripts/keel agent resume <run_id> --project <repo_dir> --message-file <followup.md>
```

### Handling `--executor agy` output (zero prompt echoing)
`keel agent run --executor agy` writes `.agents/runs/<run_id>/brief.md` and outputs a compact JSON descriptor (`invoke_subagent` with `TypeName="self"`, `Model`, `Workspace`, and a short file-pointer `Prompt`). Pass those exact lightweight fields to `invoke_subagent` (or `send_message` on resume) — **never** read `brief.md` just to copy-paste its full text through your own output tokens.
