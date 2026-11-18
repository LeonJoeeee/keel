# Task: Synchronous `agy -p` Adapter & Flat Local Branch Gate

## Goal & Why

Complete the runtime core (`P3`, `P4`, `P5`) by replacing the legacy two-step Antigravity host-descriptor workflow and asynchronous pending-verifier state machine with synchronous `agy -p` execution and a flat, symmetric local branch gate:
1. Upgrade `AgyExecutor` (`scripts/keel_lib/executors/agy.py`) to run `agy -p --output-format json [--json-schema]` synchronously **without `--new-project`** (so headless runs reuse `CLI Project` without polluting the Antigravity UI sidebar), resolving `L1`–`L4` strictly from `config/defaults.json` with zero hardcoded shipped model lists in Python.
2. Replace `scripts/keel_lib/branch_gate.py` with a clean, flat local branch gate that removes `record-verdict` and asynchronous pending-verifier claim tables, enforces same-level cross-vendor diagonal verification (`agy <-> codex`), runs `--dual` (`codex + agy`) concurrently via `ThreadPoolExecutor`, enforces rework step-up escalation after `request_changes`, and preserves pure-rebase `tree SHA` equivalence and atomic `--squash` / `--ff-only` / `--discard` landing.

## Bounds

- **Allowed paths:**
  - `config/defaults.json`
  - `scripts/keel`
  - `scripts/keel_lib/executors/{base.py,codex.py,agy.py}`
  - `scripts/keel_lib/branch_gate.py`
  - `tests/{test_worktree_and_dispatch.py,test_branch_gate.py}`
  - `docs/tasks/active/20261118-sync-agy-and-flat-branch-gate.md`
- **Non-goals:**
  - Do not edit `hooks/pre-tool-use`, `scripts/keel_lib/guard_rules.py`, or `plugin.json`.
  - Keep `rules/AGENTS.md`, `skills/`, `AGENTS.md`, `docs/architecture.md`, and `README.md` updates for Slice 4 once the CLI surface is finalized.
  - Unit and integration tests must never invoke the real host `agy` binary (always mock `run_process_group` or provide a local temp stub `agy` on `PATH`).

## Behavioral Specification

1. **Synchronous `AgyExecutor` (`scripts/keel_lib/executors/agy.py`) & Executor Cleanup:**
   - Remove the pre-Slice-3 `is_legacy_scratch_verify` compatibility fallback from `base.py` / `codex.py` and remove obsolete `"presets"` from `config/defaults.json` if any remain, since `branch_gate.py` now always passes an explicit `level`.
   - Set `loads_plugin_spine = True`, `inject_schema_in_brief = False` (enforced natively via `agy --json-schema`).
   - Resolve `(level, model, effort)` from `resolve_executor_level_config("agy", req.level, defaults)` with explicit `--model` / `--effort` overrides (enforcing `enforce_verify_min_level` when `preset == "verify"`). Keep `config/defaults.json` as the sole shipped model table: reject legacy `invoke_subagent` short aliases (`inherit`, `flash`, `flash_lite`, `pro`), validate slug syntax (`^[A-Za-z0-9._:-]+$`), validate explicit effort (`low|medium|high|xhigh|max`), and reject conflicting embedded suffix + explicit `--effort` (e.g., `claude-sonnet-5-5-high` combined with `--effort medium`).
   - Build CLI argv **omitting `--new-project`** (`["agy", *(["--conversation", session_id] if session_id else []), "--dangerously-skip-permissions", *add_dir_flags, "--model", model, *(["--effort", effort] if effort else []), "--output-format", "json", *(["--json-schema", str(schema_path)] if schema_path else []), "-p", pointer_prompt]`), granting `--add-dir <run_dir>` always plus `resolve_git_add_dirs(workdir, extra_git_dirs)` only when `mode == "write"` or `scratch_verify=True`. Reject non-`"inherit"` `agy_workspace` (or remove `--agy-workspace` from `scripts/keel` in favor of `--worktree`).
   - Write preliminary `run.json` (`status: "running"`) before invoking `agy -p` so `hooks/pre-tool-use` binds Turn 1; parse JSON stdout (`conversation_id`, `status == "SUCCESS"`, `structured_output` / `response`, `usage`), write `output_path` and `agy-result.json` (`agy-resume.json`), support `resume(..., *, level=None, model=None, effort=None)` via `--conversation <session_id>`, and reject resuming ephemeral `preset=verify, mode=read` runs (preserving completed `run.json` on resume failure).

2. **Flat Local Branch Gate (`scripts/keel_lib/branch_gate.py` & `scripts/keel`):**
   - Remove `keel branch record-verdict` and all asynchronous `pending_verifiers` / `dual_required_by_sha` state machinery.
   - Add `--level` to `keel branch verify` in `scripts/keel`.
   - Track branch write runs & rework step-up:
     - When `keel agent run` or `keel agent resume` runs in `mode == "write"` inside a non-default Git branch, enforce rework step-up before execution: if the branch's latest verification round in `state["rounds"]` has `outcome == "request_changes"` and no write at the stepped-up level has completed since that round, require `level >= escalate_level(latest_round.get("implement_level") or latest_round["level"])` (`KeelConfigError` otherwise). On successful `mode == "write"` completion, record `state["last_write"] = {"executor": adapter.name, "level": resolved_level, "recorded_at": ...}` under `locked_branch_state`.
   - Same-level, cross-vendor diagonal `verify_branch`:
     - Enforce clean author worktree and the 2-round `request_changes` circuit breaker (`count(r["outcome"] == "request_changes") >= max_review_rounds` blocks unless `force_round=True`).
     - **Level:** Let `min_level = max_level(verify_min_level, last_write["level"])` if `last_write` exists, else `verify_min_level`. If `--level` is omitted, inherit `min_level` when `last_write` exists, else default to `"L3"` (supporting manual commits cleanly). If `--level` is passed, require `level_rank(level) >= level_rank(min_level)` via `KeelConfigError`.
     - **Executors:** When `dual=True`, reject `--executor` / `--model` / `--effort` overrides with `KeelConfigError` (dual runs span heterogeneous vendors; select tier via `--level`) and run `codex` and `agy` concurrently via `ThreadPoolExecutor(max_workers=2)` inside `forward_signals_to_child_groups()`, each in its own isolated `/tmp/keel-verify-<slug>-<exec>-*` disposable clone cleaned up in `finally` (unless `output_dir` is set). When `dual=False`, use explicit `--executor` if passed; else jump diagonally across vendors from `last_write.get("executor")` (`"agy" -> "codex"`, `"codex" -> "agy"`, otherwise `defaults["routing"]["verify"]`).
     - Validate every verdict against `verdict.schema.json` and append one completed round entry (`head_sha`, `dual`, `level`, `implement_level`, `outcome`, `verdicts`, `recorded_at`) to `state["rounds"]`.
   - Gate evaluation, rebase proof, & landing:
     - Inspect the latest round in `state["rounds"]` matching `head_sha` (or `old_head` proven equivalent via `compare_rebase`). When that latest round has `outcome == "approve"` (and deterministic checks pass when configured, including mandatory rerun after rebase if `old_head` had checks), `ready_to_land` is `True` without requiring `--human-override` even if earlier rounds exceeded `max_review_rounds` via `--force-round`.
     - Preserve `compare_rebase`, `discover_task_file`, `branch_repo_overview`, and `land_branch` (`--squash` default bundling `docs/tasks/active -> archive`, `--ff-only`, `--discard --reason`, `--human-override`).

## Done-check

- `PYTHONDONTWRITEBYTECODE=1 python3 -W default -m unittest discover -s tests -v` passes with zero warnings and zero invocations of real host `agy`, covering:
  - Synchronous `AgyExecutor` argv (confirming `--new-project` is never passed, `--add-dir` scoping, `--json-schema`, `--conversation` resume, suffix/`--effort` conflict check, and single-source model resolution so changing `config/defaults.json` never breaks tests);
  - Same-level cross-vendor diagonal `verify` inheritance (`agy@L2 -> codex@L2`, `codex@L3 -> agy@L3`, `L1 -> L2` floor, manual commit `-> L3`), `verify >= implement` enforcement, concurrent `--dual` execution, and rework step-up escalation (`L2 -> L3 -> L4`) after `request_changes`;
  - 2-round circuit breaker + `--force-round` approval landing cleanly without double `--human-override`, pure-rebase `compare_rebase` tree-SHA equivalence, taskless branch landing, and `--squash` / `--ff-only` / `--discard` task archival.
- Temporary plugin install + `./scripts/install-plugin --plugin-dir <tmp_dir> --no-bin --check` and `~/.local/bin/agy plugin validate --strict <tmp_dir>` pass.
