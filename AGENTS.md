# Keel — Repository Context

## Core need

Keel lets one developer direct parallel workers across Google Antigravity, OpenAI Codex CLI, and future executors. Match model capability to each slice (`R1`) and carry the project's purpose across context compactions and isolated worktrees (`R2`). Verify root-cause fixes independently in a clean disposable clone (`R3`). Reserve human attention for the Core need, genuine tradeoffs, and authorization of irreversible actions (`R4`).

Separate durable requirements in `docs/requirements.md` from Python 3 stdlib glue. Use local Git state and public CLI and hook contracts so the harness stays rebuildable (`D3`) and branches can be verified and landed locally.

## Architecture & Boundaries

- Antigravity loads `rules/AGENTS.md` as plugin rules. Sections 1–3 also travel in standalone worker briefs. Section 4 keeps sizing, dispatch, verification, and review triage resident in the main session. The single on-demand skill, `keel-repo-ops`, covers repository setup and maintenance.
- `scripts/keel` manages worktrees, synchronous agent runs, and the local branch gate. `config/defaults.json` maps L1–L4 to executor models and effort. The Antigravity adapter uses `agy -p` in the selected checkout and reuses its CLI Project.
- Task specifications live in Git under `docs/tasks/`. The gate records verifier results directly in untracked `<git_common_dir>/keel-state/branches/`.
- `hooks/pre-tool-use` handles both Antigravity and Codex events. It guards against subagent pushes and lands, destructive pushes, and direct commits outside `docs/tasks/` on `main` or `master`.

## Commands

```sh
# Run full unit and real-git integration suite (zero bytecode pollution, warnings enabled)
PYTHONDONTWRITEBYTECODE=1 python3 -W default -m unittest discover -s tests -v

# Verify per-entry plugin symlinks and strict Antigravity plugin validation
./scripts/install-plugin --check
~/.local/bin/agy plugin validate --strict ~/.gemini/config/plugins/keel
```

## Gotchas

- **Root `AGENTS.md` vs `rules/AGENTS.md`:** Root `AGENTS.md` is repo context for maintaining `keel`; `rules/AGENTS.md` is the universal spine loaded across all projects. Always install `~/.gemini/config/plugins/keel` via `./scripts/install-plugin` (per-entry symlinks excluding root `AGENTS.md`, `docs/`, `tests/`) so `agy plugin validate --strict` never flags root `AGENTS.md` as `never loaded`.
- Keep shell guards focused on parsed commands and their targets. Filenames such as `src/merge.py`, `git pull --ff-only`, and ordinary pushes to `main` from the main session must pass.
- **Release-only versioning:** Never bump `plugin.json` on ordinary feature/fix branches; bump versions and cut tags only on explicitly authorized releases.

## Where to look

- When checking first-principles invariants (`R1`–`R5`, `D1`–`D3`, `NR1`–`NR4`) -> read `docs/requirements.md`.
- When adding a 3rd/4th executor adapter or inspecting module boundaries -> read `docs/architecture.md` and `scripts/keel_lib/executors/base.py`.
- When modifying branch verification, rebase tree-equivalence proofs, or landing/archiving -> read `scripts/keel_lib/branch_gate.py`.
- When modifying `PreToolUse` command guards or subagent mode binding -> read `hooks/pre-tool-use` and `scripts/keel_lib/guard_rules.py`.
