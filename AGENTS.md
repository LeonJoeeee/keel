# Keel — Repository Context

## Core need

One developer or researcher directing complex, multi-hour engineering work wants to set direction once and walk away—without babysitting coding agents turn by turn, losing the project's purpose across context compactions and isolated worktrees, having broken or fake-green code silently merged onto `main`, or getting dragged back into endless agent nitpicking, deadlocks, and process friction.

## Design Philosophy

- **User value over developer self-indulgence:** Anchor every slice in the human user's concrete problem and pain points rather than internal feature checklists, premature abstractions, or architecture for its own sake.
- **Root-cause verification with evidence-backed rebuttal over fake green or blind obedience:** Independent clean-context verifiers test realistic caller workflows and Canary breaks to catch symptom masking; when a review finding is wrong or out of scope, authors and Orchestrators push back with concrete evidence rather than blindly contorting code.
- **Autonomous Orchestrator recovery before interrupting the human:** Let clear, reversible work run end to end—matching capability tiers to slice difficulty, stepping up on rework, and stopping at bounded convergence caps—while reserving human attention for genuine tradeoffs, direction-changing ambiguity, and irreversible actions.
- **Thin local-Git & Python stdlib rebuildability over platform lock-in:** Keep timeless requirements separate from thin Python 3 stdlib glue, local Git state, and public CLI/hook contracts so the harness stays inspectable, offline-capable, and cheap to rebuild as executors evolve.

## Architecture & Boundaries

- Antigravity loads `rules/AGENTS.md` as plugin rules. Sections 1–3 also travel in standalone worker briefs. Section 4 keeps sizing, dispatch, verification, and review triage resident in the main session. The single on-demand skill, `keel-repo-ops`, covers repository setup and maintenance.
- `scripts/keel` (Python 3 stdlib + local Git) manages worktrees, synchronous agent runs across Google Antigravity (`agy -p` reusing its CLI Project), OpenAI Codex CLI, and future executors, and the local branch gate. `config/defaults.json` maps L1–L4 to executor models and effort.
- Task specifications live in Git under `docs/tasks/` as the single zero-context handoff contract (`scripts/keel` and `branch_gate.py` extract `## Goal & Why` / `## Why` automatically when `--why` is omitted). The gate records verifier results directly in untracked `<git_common_dir>/keel-state/branches/`.
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

- When checking Keel's detailed design philosophy and first-principles invariants (`## Design Philosophy`, `R1`–`R5`, `D1`–`D3`, `NR1`–`NR4`) -> read `docs/requirements.md`.
- When adding a 3rd/4th executor adapter or inspecting module boundaries -> read `docs/architecture.md` and `scripts/keel_lib/executors/base.py`.
- When modifying branch verification, rebase tree-equivalence proofs, or landing/archiving -> read `scripts/keel_lib/branch_gate.py`.
- When modifying `PreToolUse` command guards or subagent mode binding -> read `hooks/pre-tool-use` and `scripts/keel_lib/guard_rules.py`.
