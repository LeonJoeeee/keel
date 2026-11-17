# Pure-Git Multi-Executor Spine, Repo-Native Task Ledger & Active Branch Gate

## Goal & Why

Refactor `keel` from GitHub Web PR / `gh api` coupling into a pure-local-Git, multi-executor (`agy` + `codex`, extensible `1..N`) engineering spine grounded in `docs/requirements.md` (`R1`–`R5`, `D1`–`D3`, `NR1`–`NR4`) and late-2026 frontier harness practices:

1. **Preserve peak model intelligence (`R1`) & global alignment (`R2`):** Upgrade the two-layer `AGENTS.md` architecture (`rules/AGENTS.md` universal spine + `<repo>/AGENTS.md` 5-section project map `<100` lines with conditional routing) and replace external GitHub Issues with a zero-conflict, `main`-anchored `docs/tasks/{todo,active,archive}/` task ledger.
2. **Eliminate context/registration blind spots (`R2`, `R5`):** Fix the 3 empirically discovered context-registration gaps across `agy` and `codex` subagents while upgrading Codex preset defaults to `gpt-6-astra` at `max` (all overridable via CLI flags).
3. **Deliver trustworthy local verification & guarded integration (`R3`, `R4`):** Replace `keel pr` / `pr_gate.py` / `env_gh.py` with a pure-Git `keel branch` gate (`verify`, `record-verdict`, `status`, `compare`, `land`) backed by external `.git/keel-state/` records, remote-stripped disposable `/tmp` clones, 1-line Canary mutation checks against fake-green tests, a 2-round convergence circuit breaker, and a slim, zero-false-positive `PreToolUse` seatbelt.

## Bounds

- **In scope:**
  - `config/defaults.json` (upgrade Codex presets `general`, `implement`, `verify` to `model: "gpt-6-astra"`, `effort: "max"`)
  - `config/presets/{general,implement,verify}.md` (pure-Git wording; `verify.md` checks against `main`-locked task spec + 1-line Canary break in disposable `/tmp` clone)
  - `rules/AGENTS.md`, `AGENTS.md`, `README.md`, `docs/requirements.md`, `docs/architecture.md`, `.github/workflows/ci.yml`
  - `skills/keel-{repo-ops,dispatch,integrate}/SKILL.md`
  - `hooks/pre-tool-use` and `scripts/keel_lib/guard_rules.py` (slim pure-Git seatbelt; delete all `gh` / `gh api` / `keel pr` checks)
  - `scripts/keel_lib/env.py` (replaces `scripts/keel_lib/env_gh.py`, removing all `gh api` / GitHub slug helpers while preserving `tool_env`, `safe_resolve`, `same_path`, `is_subpath`, `run_cmd` with `cwd` `FileNotFoundError`/`NotADirectoryError` diagnostics, and `run_process_group`)
  - `scripts/keel_lib/worktree.py` (default `base="main"`, `fetch=False` for local refs; `cleanup_worktree` verifies local ancestry or recorded squash-land SHA instead of `--pr`; `create_disposable_clone` strips all git remotes inside `/tmp` checkout)
  - `scripts/keel_lib/context.py` and `scripts/keel_lib/executors/{base,agy,codex}.py` (fix all 3 live-probe context leaks + Codex `-s read-only` when `mode == "read"` outside disposable verify clones)
  - `scripts/keel_lib/branch_gate.py` (replaces `scripts/keel_lib/pr_gate.py`) and `scripts/keel` (`keel branch` replaces `keel pr`)
  - `tests/` (comprehensive unit + real-git integration coverage for all pure-Git flows, guard rules, context registration, `--dual` verification, `--squash` / `--ff-only` / `--discard` landing, and `install-plugin`)
- **Explicit non-goals / forbidden:**
  - Do **not** bump `plugin.json` version (`Release-only versioning`).
  - Do **not** add any third-party Python dependencies (Python 3 stdlib only).
  - Do **not** modify `~/.codex/` or `~/.gemini/config/plugins/keel` from inside the implementation branch.
  - Do **not** build a 2,000-line regex Git CLI flag emulator in `guard_rules.py` — keep `guard_rules.py` concise (~450–650 lines max) around `extract_command_segments` + direct subcommand/branch checks.

## Technical Specification

### 1. Stage 1 & 2 — Two-Layer `AGENTS.md` & `docs/tasks/` Ledger
- `rules/AGENTS.md` (~25 lines): keep the 4 universal rules (`Core need` first, Real solutions over fake green + honest conflict escalation, Protect human attention with neutral-first tradeoffs & explicit authorization for irreversible actions, On-demand skills), updating item 4 from PR wording to branch verification/landing (`keel-integrate`).
- `skills/keel-repo-ops/SKILL.md`:
  - Define the 5-section `<repo>/AGENTS.md` standard (`<100` lines ceiling; prune/merge stale items or move details to `docs/` when near 100 lines; never copy code-searchable lists; 90% of routine tasks do not touch `AGENTS.md`):
    1. `## Core need`
    2. `## Architecture & Boundaries`
    3. `## Commands`
    4. `## Gotchas`
    5. `## Where to look` (strictly **conditional routing**: `"When doing X -> read Y"`, never mandatory upfront pre-reads).
  - Define the `docs/tasks/{todo,active,archive}/` ledger convention (`<YYYYMMDD>-<slug>.md`):
    - Orchestrator opens/commits task specs in `docs/tasks/todo/` or `docs/tasks/active/` directly on `main` **before** branching worktrees (`D1`: skip task files for trivial 2–5 line fixes).
    - Short spec sections: `## Goal & Why`, `## Bounds`, `## Done-check`. Long multi-step tasks add `## Progress` and `## Decisions & Findings` (workers may update `Progress` / `Decisions & Findings` on their branch, never weaken `Goal & Why` / `Bounds` / `Done-check` locked on `main`).
    - Out-of-scope bugs discovered on a branch are reported in completion `notes` to Orchestrator to open in `docs/tasks/todo/` on `main`.
- Update `keel`'s own root `AGENTS.md` to exemplify this exact 5-section `<100`-line map.

### 2. Stage 3 — Context Registration Fixes & Codex `gpt-6-astra @ max`
- `config/defaults.json`: set `executors.codex.presets.{general,implement,verify}` to `{"model": "gpt-6-astra", "effort": "max"}` (fallback in `codex.py` `_resolve_model_effort` also defaults to `"gpt-6-astra"` and `"max"` when unset).
- `ExecutorAdapter` (`base.py`): add `loads_plugin_spine: bool = False` (`True` on `AgyExecutor`, `False` on `CodexExecutor`).
- `assemble_brief` (`context.py`):
  1. **Leak ② fix (Codex global spine):** When `not loads_plugin_spine`, include universal sections 1–3 of `PLUGIN_ROOT / "rules" / "AGENTS.md"` (`Core need`, `Real solutions over fake green`, `Protect human attention`) at the top of `brief.md`.
  2. **Leak ① fix (`agy` cross-dir / `/tmp` clone `AGENTS.md`):**
     - Prefer reading agent doc (`AGENTS.md` / `GEMINI.md`) from `workdir` if present, else `project_root`.
     - For `codex` (`native_agent_docs = ("AGENTS.md",)`): `natively_loaded` is `True` iff `(workdir / "AGENTS.md").is_file()`.
     - For `agy`: `<user_rules>` only loads the parent session's active workspace root (`caller_cwd`). Therefore `natively_loaded` is `True` **only** when `project_root is not None`, `is_subpath(caller_cwd, project_root)`, `is_subpath(workdir, project_root)`, `(project_root / doc_name).is_file()`, and `(workdir / doc_name)` has identical bytes to `(project_root / doc_name)`. In all other cases (`/tmp` disposable clone, child/sibling project, or branch that edited `AGENTS.md`), inject the doc into `brief.md`!
  3. **Leak ③ fix (`codex -o` output contract):** When `not inject_schema_in_brief` and `schema_path is None`, append an `## Output Contract` telling the agent to emit its final report directly as its final assistant message (captured automatically by `-o`) without creating extra report files unless requested.
- `CodexExecutor.build_exec_argv` (`codex.py`):
  - Add `sandbox_mode: str | None = None` parameter: use `"read-only"` when `mode == "read"` unless `allow_Disposable_write` (i.e. `keel branch verify` running inside a `/tmp/keel-verify-*` disposable clone, which uses `"workspace-write"` scoped solely to that `/tmp` clone + its separated git dir so tests, `.venv`, and 1-line Canary breaks work while the real repo remains kernel-unwritable!).

### 3. Stage 4 — Slim Pure-Git `PreToolUse` Seatbelt (`hooks/pre-tool-use` + `guard_rules.py`)
- `hooks/pre-tool-use`:
  - Keep dual-protocol handling (`Antigravity` camelCase + `Codex` snake_case) and fail-open safety.
  - Improve `agy` subagent mode binding: if `conv_id` is not yet in `conv_modes.json`, check `read_file` target for `brief.md` AND also check `event.get("lastUserInput")` for any `/.agents/runs/<id>/brief.md` path (`run.json` -> `mode` + `scratch_verify`) before falling back to `detect_agent_mode(cwd)`.
- `guard_rules.py`:
  - Keep `extract_command_segments` (quotes, heredocs, substitutions, `cd` tracking) and `detect_agent_mode` (`.agents/worktrees/` -> `"write"`, `/tmp/keel-verify-` -> `"read"`, `KEEL_AGENT_MODE` env override, else `"main"`).
  - Delete all `gh` / `gh api` / `keel pr` logic.
  - Enforce clean, low-friction rules:
    1. Non-Git `git_cwd` (and no `--git-dir`): allow.
    2. `keel` CLI guard: in `mode in ("read", "write")`, block `keel branch land` when `--execute` or `--discard` is passed (and block `keel worktree cleanup --discard`).
    3. `git push` guard:
       - Block all `git push` when `mode in ("read", "write")`.
       - In `mode == "main"`: **allow normal non-force `git push`** (including `git push`, `git push origin main`, `git push th main`), and **block** destructive pushes: `--force`, `-f` (including short flag clusters like `-uf`), `--force-with-lease`, `--force-if-includes`, `--delete`, `-d`, `--mirror`, leading `+` refspecs (`+main`, `+HEAD:main`), or deletion refspecs (`:branch`).
    4. `mode == "read"` mutation guard:
       - Inside disposable `/tmp/keel-verify-*` clones: allow local working-tree commands needed for test/Canary verification (while still blocking `git push`).
       - Outside `/tmp/keel-verify-*`: block mutating `git` subcommands (`commit`, `merge`, `rebase`, `cherry-pick`, `reset`, `checkout`, `switch`, `restore`, `clean`, `stash`, `tag` creation/deletion, `branch` deletion/rename/force, `worktree` add/remove/prune, `pull`, `am`, `apply`).
    5. Protected default-branch (`main` / `master`) guard:
       - In `mode == "write"`: block mutating `git` commands executed inside the primary checkout or targeting `refs/heads/main` / `refs/heads/master`.
       - In `mode == "main"` when checked out on `main` / `master`:
         - Block direct `git merge`, `git rebase`, `git cherry-pick`, `git reset --hard`, and `git pull` unless `--ff-only` is present (so `git pull --ff-only` multi-machine sync works!).
         - On `git commit` while on `main` / `master`: inspect staged paths (`git diff --cached --name-only`; and reject `-a`/`--all` if any tracked non-`docs/tasks/` file is modified, or explicit non-`docs/tasks/` pathspecs). **Allow** iff the commit touches at least one path and **every** path is under `docs/tasks/` (`p.startswith("docs/tasks/")`); otherwise **block** with a clear message directing code changes to a worktree + `keel branch verify/land`.

### 4. Stage 5 — Pure-Git Branch Verification & Guarded Landing (`branch_gate.py` + `scripts/keel`)
- State storage: `<git_common_dir>/keel-state/branches/<encoded_branch>.json` (outside worktrees, automatically untracked inside `.git/`).
- `create_disposable_clone` (`worktree.py`): `--no-local` detached checkout at `head_sha`, sync gitignored root docs, and **remove all configured git remotes** inside the disposable clone.
- CLI subcommands (`keel branch ...`, replacing `keel pr ...`; `keel worktree cleanup <slug> [--discard]` without `--pr`):
  1. `keel branch verify <branch_or_slug>`:
     - Resolves `<slug>` -> `task/<slug>` if `refs/heads/task/<slug>` exists, else `<branch_or_slug>`.
     - Requires clean worktree if a linked worktree is checked out on that branch.
     - Options: `--project`, `--base` (default `main`/`master`), `--executor codex|agy` (default `codex`), `--dual` (runs both `codex` and `agy`), `--model`, `--effort`, `--check <cmd>` (repeatable deterministic shell check run inside disposable clone; must exit `0`), `--skip-checks <reason>`, `--task-file` (auto-discovers matching `docs/tasks/active/*<slug>*.md` on `base` or branch if omitted), `--why`, `--context-file`, `--output-dir`, `--force-round`.
     - Enforces 2-round `request_changes` circuit breaker on the branch (in `--dual`, a dual pass on the same `head_sha` counts as 1 round; requires **both** `codex` and `agy` to return `"approve"`).
     - Copies `base`'s locked version of the task file (if present on `base`) into the brief context so the independent verifier checks that `Goal` / `Bounds` / `Done-check` were not weakened on the branch.
     - For `codex`: executes synchronously in its own disposable `/tmp` clone, validates `verdict.json` (`validate_verdict_payload`), records verdict in `.git/keel-state/`, cleans up its `/tmp` clone.
     - For `agy` (standalone or `--dual`): creates a dedicated disposable `/tmp` clone + `brief.md` + `invoke_subagent` descriptor (`pending_verifiers` recorded in state) for completion via `record-verdict`.
  2. `keel branch record-verdict <branch_or_slug> --verdict-file <path> --head-sha <sha> [--executor agy] [--model <m>] [--cleanup-scratch <dir>] [--project <dir>]`:
     - Blocked in `mode == "read"`. Validates `verdict.json`, records verdict for `(head_sha, executor)` in `.git/keel-state/`, updates `request_changes` round count once all expected verifiers for that round report, and removes `--cleanup-scratch` if prefixed `keel-verify-`.
  3. `keel branch status [branch_or_slug] [--project <dir>] [--base <ref>]`:
     - With no positional arg: repo-wide summary (`todo_tasks`, `active_tasks_on_base`, `worktrees` with each branch's active task file and gate status).
     - With `<branch_or_slug>`: full gate evaluation (`head_sha`, `base_ref`, `base_tip_sha`, `ancestry_ok`, `checks`, `verdicts`, `dual_required`, `approval_source`, `request_changes_rounds`, `ready_to_land`, `blockers`).
  4. `keel branch compare --old-base <SHA> --old-head <SHA> --base <SHA> --head <SHA> [--project <dir>]`:
     - Pure-rebase mathematical `tree SHA` equivalence proof (`compare_rebase`).
  5. `keel branch land <branch_or_slug>`:
     - Blocked in `mode in ("read", "write")` when `--execute` or `--discard` is passed.
     - Options: `--project`, `--base`, `--squash` (default `True`) / `--ff-only` (overrides to fast-forward), `--old-base`, `--old-head`, `--check <cmd>` (mandatory if `--old-head` pure-rebase carry-over had prior deterministic checks unless `--skip-checks <reason>`), `--human-override <reason>`, `--task-file`, `--title`, `--keep-branch`, `--execute`, `--discard`, `--reason <why_discarded>`.
     - Dry-run (neither `--execute` nor `--discard`): returns gate readiness & blockers.
     - `--execute`:
       - Verifies gate is ready (ancestry `base <= head`, deterministic checks green if configured/rebased, `approve` verdict on `head_sha` — or unanimous `codex + agy` `approve` if verified with `--dual` — or valid `--old-base`/`--old-head` pure-rebase proof, or `--human-override`), primary checkout is on `base_ref` with clean `git status --porcelain -uall`, and any linked worktree for the branch is clean at `head_sha`.
       - **Default `--squash`:** runs `git merge --squash <head_sha>` on `base_ref`, moves matching `docs/tasks/active/<task>.md` -> `docs/tasks/archive/<task>.md` inside the same index if present, preserves trailers (`Co-authored-by`, `Keel-Human-Override`), commits as **1 clean atomic commit** on `base_ref`, records `landed_sha` in `.git/keel-state/`, and removes the worktree + local branch (unless `--keep-branch`).
       - **`--ff-only`:** runs `git merge --ff-only <head_sha>` on `base_ref`; if matching `docs/tasks/active/<task>.md` remains in `active/`, `git mv`s it to `docs/tasks/archive/<task>.md` and commits `docs(tasks): archive <task>`; removes worktree + local branch.
     - `--discard --reason "<why>"`:
       - Requires non-empty `--reason`. Does **not** merge branch code onto `base_ref`.
       - Ensures primary checkout is on `base_ref` and clean; if a matching `docs/tasks/active/<task>.md` exists on `base_ref` (or rescues it from the branch if only there), appends `\n## Outcome: [DISCARDED]\n\n- **Reason:** <why>\n`, moves it into `docs/tasks/archive/<task>.md`, and commits `docs(tasks): discard <task>` on `base_ref`.
       - Removes the worktree (`discard=True`) and deletes the local branch.

## Done-check

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -W default -m unittest discover -s tests -v
./scripts/install-plugin --plugin-dir /tmp/keel-plugin-test --no-bin
./scripts/install-plugin --plugin-dir /tmp/keel-plugin-test --no-bin --check
~/.local/bin/agy plugin validate --strict /tmp/keel-plugin-test
rm -rf /tmp/keel-plugin-test
```

## Progress

- [x] Align all 5 architecture stages (`AGENTS.md`, `docs/tasks/`, context registration, slim `PreToolUse` seatbelt, `keel branch` local gate) and open baseline task spec on `main`.
- [ ] Implement Milestone 1: Core `env.py`, `worktree.py`, `context.py` + `executors/` fixes & `gpt-6-astra @ max` defaults.
- [ ] Implement Milestone 2: Slim pure-Git `guard_rules.py` & `hooks/pre-tool-use`.
- [ ] Implement Milestone 3: `branch_gate.py`, `scripts/keel` (`keel branch`), updated `rules/`, `skills/`, `config/presets/`, `AGENTS.md`, `README.md`, `docs/`, and full unit/integration test suite.
- [ ] Run `--dual` independent clean-context verification (`agy` + `codex` `gpt-6-astra @ max`) and prepare human landing summary.

## Decisions & Findings

- **Live probe finding (2026-11-17):** Empirical registration probe caught 3 context gaps (`agy` missing `AGENTS.md` when `workdir` is `/tmp` or outside parent workspace; `codex` missing `rules/AGENTS.md` global spine; `codex -o` auto-capture clarification). All 3 are explicitly specified in Stage 3 above.
- **Owner preferences locked (2026-11-17):** Default landing strategy is `--squash` (bundling `active -> archive` into 1 atomic commit, overridable via `--ff-only`); default verification is single cross-model `codex` (`gpt-6-astra @ max`), overridable via `--dual`, `--executor`, `--model`, and `--effort`.
