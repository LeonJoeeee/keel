# Keel Architecture & Extension Guide

Keel is structured around orthogonal, loosely coupled layers using Python 3 stdlib only, pure local Git truth, and zero external package or GitHub Web API dependencies.

## 1. Component Map

- **`rules/AGENTS.md`:** ~25-line universal engineering spine merged natively into Antigravity `<user_rules>` (and injected automatically into `brief.md` sections 1–3 for standalone CLI executors like Codex).
- **`skills/keel-{repo-ops,dispatch,integrate}/SKILL.md`:** Progressive-disclosure operational guides defining the 5-section `<100`-line `<repo>/AGENTS.md` standard, the `main`-anchored `docs/tasks/{todo,active,archive}/` ledger, multi-executor worktree dispatch, and pure-Git branch verification/landing.
- **`config/`:**
  - `defaults.json`: Executor model rankings, default `gpt-6-astra @ max` Codex presets (`general`, `implement`, `verify`), preset routing, and review convergence cap (`2`).
  - `presets/{general,implement,verify}.md`: Thin context lenses assembled with `AGENTS.md`, `main`-locked task specs, and per-run `Why` + `Task` into `.agents/runs/<run_id>/brief.md`.
  - `schemas/verdict.schema.json`: Strict JSON Schema governing independent verification verdicts.
- **`hooks/pre-tool-use` + `scripts/keel_lib/guard_rules.py`:** Slim dual-protocol (`Antigravity` camelCase + `Codex` snake_case) pure-Git command-segment seatbelt. Binds Antigravity subagent conversations via `read_file` targets and `lastUserInput` `brief.md` paths; blocks subagent `git push` and `keel branch land --execute/--discard`, blocks destructive pushes on `main`, and restricts direct `git commit` on `main`/`master` to `docs/tasks/` ledger updates.
- **`scripts/keel` + `scripts/keel_lib/`:**
  - `env.py`: Non-interactive `PATH` normalization, symlink-loop-safe `safe_resolve` / `same_path` / `is_subpath`, diagnostic `run_cmd`, and process-group signal forwarding (`run_process_group`).
  - `worktree.py`: Unified `.agents/worktrees/<slug>` lifecycle (defaulting to local `main`), `.git/info/exclude` hygiene, gitignored root `AGENTS.md`/`GEMINI.md` syncing, remote-stripped `--no-local` disposable verification clones, and ancestry/squash-land-aware safe teardown.
  - `context.py` + `executors/`: Pluggable `ExecutorAdapter` registry (`codex.py` and `agy.py`) handling cross-dir/`/tmp` `AGENTS.md` injection, `rules/AGENTS.md` spine injection, `-o` output contracts, and Codex `-s read-only` vs disposable `workspace-write` sandboxing.
  - `branch_gate.py`: Untracked `<git_common_dir>/keel-state/branches/<encoded_branch>.json` ledger, deterministic `--check` runner, single (`codex`) and `--dual` (`codex + agy`) verification orchestration, 2-round convergence circuit breaker, sanitized `merge-tree` / rebase-replay mathematical `tree SHA` equivalence proof, and atomic `--squash` / `--ff-only` / `--discard` landing with `docs/tasks/active -> archive` closure.

## 2. Adding a 3rd or 4th Side Executor (`1..N`)

To add a new side-agent backend (e.g., `claude` CLI, `gemini` CLI, or a remote SDK runner):

1. Create `scripts/keel_lib/executors/<name>.py` subclassing `ExecutorAdapter`:
   - Set `name = "<name>"`.
   - Set `native_agent_docs = ("AGENTS.md",)` (or `("AGENTS.md", "GEMINI.md")` / `()`) declaring which root instruction filenames the runtime natively loads.
   - Set `loads_plugin_spine = False` (or `True` if the host already injects `rules/AGENTS.md` into `<user_rules>`).
   - Set `inject_schema_in_brief = False` if the runtime enforces `--output-schema` natively, or `True` to instruct the agent in `brief.md` to write validated JSON directly to `req.output_path`.
   - Implement `execute(req, defaults)` and `resume(run_dir, record, followup_path, output_path, defaults)`.
2. Register the class in `EXECUTORS` inside `scripts/keel_lib/executors/__init__.py` and add its default model mapping to `config/defaults.json`.

No changes are required in `worktree.py`, `context.py`, `branch_gate.py`, `presets/`, or `skills/`.
