# Keel Architecture & Extension Guide

Keel uses Python 3's standard library, local Git, and the public interfaces of its executors.

## Component map

- **Rules and repository context.** Antigravity loads `rules/AGENTS.md` into its plugin rules. Sections 1–3 apply to every agent. Section 4 is the main session's guide to sizing, dispatch, verification, and review triage. The sole on-demand skill, `skills/keel-repo-ops/SKILL.md`, covers repository maps, task templates, ADRs, releases, and deployments.
- **Configuration and briefs.** `config/defaults.json` maps L1–L4 to models and effort per executor and defines preset routing and review limits. `context.py` assembles the slice's purpose, task, working directory, and lens from `config/presets/` into the run brief. It injects the project instructions when the adapter cannot rely on native discovery and includes sections 1–3 for standalone workers. In a repository, briefs, results, and session records live under `.agents/runs/<run_id>/`.
- **Worktrees.** `worktree.py` creates isolated task branches under `.agents/worktrees/<slug>` from local `main` by default, copies gitignored root instruction files when needed, and checks integration before cleanup. It also creates remote-stripped disposable clones for verification.
- **Executors.** `scripts/keel agent run` waits for the selected adapter. Codex reads the brief on stdin and captures the final response with `-o`. Antigravity runs `agy -p` in the selected checkout, omits `--new-project` to reuse the CLI Project, and collects JSON on exit. Resumable runs retain the native session ID. A resume can select a new level with `--level`. `env.py` supplies command lookup, path handling, and process-group signal forwarding.
- **Command guards.** `hooks/pre-tool-use` reads public Antigravity and Codex hook events. It can bind an Antigravity conversation to its worker mode from the brief path. `guard_rules.py` checks parsed shell command segments and Git targets for prohibited operations, including worker pushes and lands and direct commits outside the task ledger on protected branches.

## Local branch gate

`branch_gate.py` owns verification and landing. It runs deterministic `--check` commands against the pinned head, gives each verifier a clean disposable clone and the base branch's task specification when available, and records the validated verdict directly in `<git_common_dir>/keel-state/branches/<encoded_branch>.json`. The verdict contract lives in `config/schemas/verdict.schema.json`.

By default, the other vendor reviews the last recorded write at its implementation level, with an L2 minimum. A branch without a recorded write defaults to L3 and the configured verifier. With `--dual`, both vendors run concurrently and must approve in the same round. After a `request_changes` round, both rework writes and the next verification step up one tier up to L4. The tier-aware convergence cap (`L2(1) -> L3(1) -> L4(2)`) stops and escalates unresolved blockers to the human after two rejected L4 rounds (at most 4 rounds from L2, 3 from L3, or 2 from L4).

`branch status` reports the gate for one branch or the task and worktree overview. After a pure rebase, `branch compare` checks Git tree equivalence. To carry approval forward, `branch land` checks that proof, the locked task scope, and any prior deterministic checks on the new head. The default squash land includes task archival in the same commit. Fast-forward landing archives an active task in a subsequent commit. An authorized discard archives the task with its reason.

## Adding an executor

1. Implement `ExecutorAdapter` in `scripts/keel_lib/executors/<name>.py`. Declare the root instruction files it reads (`native_agent_docs`), whether it loads the plugin rules (`loads_plugin_spine`), and whether the brief must supply the schema and output path (`inject_schema_in_brief`).
2. Implement synchronous `execute` and `resume` using the signatures in `executors/base.py`. Resolve the requested level from configuration and return run metadata, including the selected level and the session ID for a resumable run.
3. Register the adapter in `executors/__init__.py` and configure each level under `executors.<name>.levels` in `config/defaults.json`. The CLI derives its executor choices from this registry. The built-in diagonal and dual pair are defined in `branch_gate.py`.
