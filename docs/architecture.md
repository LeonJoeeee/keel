# Keel Architecture & Extension Guide

Keel is structured around orthogonal, loosely coupled layers so the entire plugin remains under ~1,000 lines of Python 3 stdlib code with zero external package dependencies.

## 1. Component Map

- **`rules/AGENTS.md`:** ~40-line universal spine merged natively into Antigravity `<user_rules>` (persisted across compactions without `SessionStart` or `PreInvocation` hooks).
- **`skills/keel-{dispatch,integrate,repo-ops}/SKILL.md`:** Progressive-disclosure operational guides loaded strictly when multi-agent dispatch, PR verification/integration, or repo founding/release workflows are triggered.
- **`config/`:**
  - `defaults.json`: Executor model rankings, reasoning effort defaults, preset routing, and review convergence cap (`2`).
  - `presets/{general,implement,verify}.md`: Thin context lenses assembled with project `AGENTS.md` and per-run `Why` + `Task` into `.agents/runs/<run_id>/brief.md`.
  - `schemas/verdict.schema.json`: Strict JSON Schema governing independent verification verdicts.
- **`hooks/pre-tool-use` + `scripts/keel_lib/guard_rules.py`:** Dual-protocol (`Antigravity` camelCase + `Codex` snake_case) command-segment semantic guard. Strips quotes and heredocs while inspecting `$(...)`/backtick substitutions, preventing unverified `gh pr merge` and direct pushes to `main`/`master` without false positives on filenames or `&&` command chains.
- **`scripts/keel` + `scripts/keel_lib/`:**
  - `env_gh.py`: Non-login `PATH` normalization, macOS/symlink-safe `same_path` (`ELOOP`-guarded), process-group signal forwarding, multi-page `gh api --paginate` JSON stream decoding, and bounded 3-attempt transient retry strictly on idempotent read-only queries.
  - `worktree.py`: Unified `.agents/worktrees/<slug>` creation (using `.git/info/exclude` on adopted repos lacking `.gitignore` entries), safe syncing of gitignored root `AGENTS.md`/`GEMINI.md`, `--no-local` disposable verification clones, and cache-tolerant safe teardown.
  - `executors/`: Pluggable `ExecutorAdapter` registry (`codex.py` and `agy.py`).
  - `pr_gate.py`: True git ancestry check (`merge-base --is-ancestor`), `skipped`/`neutral`-aware CI check evaluation, sanitized `merge-tree` / rebase-replay mathematical `tree SHA` equivalence proof, stateless human-readable PR review parsing, 2-round convergence cap, and SHA-pinned squash merge.

## 2. Adding a 3rd or 4th Side Executor (`1..N`)

To add a new side-agent backend (e.g., `claude` CLI, `gemini` CLI, or a remote SDK runner):

1. Create `scripts/keel_lib/executors/<name>.py` subclassing `ExecutorAdapter`:
   - Set `name = "<name>"`.
   - Set `native_agent_docs = ("AGENTS.md",)` (or `("AGENTS.md", "GEMINI.md")` / `()`) declaring which root instruction filenames the runtime natively loads from `<workdir>`; `context.py` automatically prepends the repository's doc whenever it is not natively loaded.
   - Set `inject_schema_in_brief = False` if the runtime enforces `--output-schema` natively, or `True` to instruct the agent in `brief.md` to write validated JSON directly to `req.output_path`.
   - Implement `execute(req, defaults)` and `resume(run_dir, record, followup_path, output_path, defaults)`.
2. Register the class in `EXECUTORS` inside `scripts/keel_lib/executors/__init__.py` and add its default model mapping to `config/defaults.json`.

No changes are required in `worktree.py`, `context.py`, `pr_gate.py`, `presets/`, or `skills/`.
