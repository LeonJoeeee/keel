# Task: Resident Orchestrator Spine, Single Skill Consolidation, & Documentation Alignment

## Goal & Why

Complete the Keel architecture alignment (`P1`, `D3`, `NR1`) now that the `L1`–`L4` model ladder, synchronous `agy -p` adapter, and flat local branch gate are live on `main`:
1. Move the Orchestrator's daily responsibilities (proportional slicing, `L1`–`L4` intensity selection, worktree/subagent dispatch, cross-vendor diagonal verification, and root-cause review triage) directly into `rules/AGENTS.md` `## 4.` (explicitly scoped to the main-session Orchestrator while keeping `## 1.`–`## 3.` universal for all agents), and delete the fragmented `skills/keel-dispatch/` and `skills/keel-integrate/` skills so only low-frequency `skills/keel-repo-ops/SKILL.md` remains on-demand.
2. Align `AGENTS.md`, `docs/architecture.md`, `README.md`, `skills/keel-repo-ops/SKILL.md`, and `tests/test_worktree_and_dispatch.py` with the finalized CLI and requirements: remove stale references to `record-verdict` / `pending_verifiers`, remove hardcoded concrete model names outside `config/defaults.json`, and replace arbitrary numerical line-count quotas with responsibility-based conciseness.

## Bounds

- **Allowed paths:**
  - `rules/AGENTS.md`
  - `skills/keel-repo-ops/SKILL.md`
  - `skills/keel-dispatch/SKILL.md` (delete)
  - `skills/keel-integrate/SKILL.md` (delete)
  - `AGENTS.md`
  - `docs/architecture.md`
  - `README.md`
  - `tests/test_worktree_and_dispatch.py`
  - `docs/tasks/active/20261118-resident-orchestrator-spine-and-docs.md`
- **Non-goals:**
  - Do not modify Python runtime modules (`scripts/`), `hooks/`, `config/defaults.json`, or `plugin.json`.

## Done-check

- `rules/AGENTS.md` preserves universal `## 1.`–`## 3.` (`load_universal_spine()` continues to extract `## 1.`–`## 3.` cleanly and exclude `## 4.`) and defines `## 4.` for the main-session Orchestrator only (proportional slicing, `L1`–`L4` dispatch backed by `config/defaults.json`, same-level diagonal verification, root-cause review triage with rework step-up and 2-round circuit breaker, core `keel` CLI syntax, and `keel-repo-ops` pointer).
- `skills/keel-dispatch/` and `skills/keel-integrate/` are removed; `skills/keel-repo-ops/SKILL.md`, `AGENTS.md`, `docs/architecture.md`, and `README.md` contain zero stale `record-verdict` / `--agy-workspace` / hardcoded model-tier names or arbitrary numerical LOC quotas.
- `PYTHONDONTWRITEBYTECODE=1 python3 -W default -m unittest discover -s tests -v` passes with zero warnings.
- Temporary plugin install + `./scripts/install-plugin --plugin-dir <tmp_dir> --no-bin --check` and `~/.local/bin/agy plugin validate --strict <tmp_dir>` pass cleanly with zero warnings.
