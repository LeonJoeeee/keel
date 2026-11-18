# Task: Task-Intensity Model Routing (`L1`–`L4`), Guarded Escalation, and Synchronous `agy -p` Adapter

## Goal & Why

Align subagent dispatch and branch verification with the first-principles core need of model routing: matching each task's real complexity to an appropriate model intensity (`L1`–`L4`) across both OpenAI Codex CLI (`gpt-6.1-terra`, `gpt-6.2-sol`, `gpt-6.2-astra`) and Google Antigravity CLI (`gemini-4-hydrogen-{medium,high,xhigh}`, `gemini-4-carbon-high`), preventing both wasteful over-provisioning on trivial slices and repeated low-tier rework loops (`R1`, `R3`, `R5`).

Switching `AgyExecutor` from two-step host `invoke_subagent` descriptors to synchronous `agy -p --output-format json [--json-schema]` unlocks the full `Gemini 4 Hydrogen` / `Carbon` ladder, unifies `agy` and `codex` execution semantics, and makes `--dual` verification 100% concurrent and self-recording in one command.

## Bounds

- **Allowed paths:**
  - `config/defaults.json`
  - `config/presets/verify.md`
  - `rules/AGENTS.md`
  - `scripts/keel`
  - `scripts/keel_lib/env.py`
  - `scripts/keel_lib/context.py`
  - `scripts/keel_lib/branch_gate.py`
  - `scripts/keel_lib/executors/{base.py,agy.py,codex.py}`
  - `skills/keel-{dispatch,integrate}/SKILL.md`
  - `AGENTS.md`, `README.md`, `docs/requirements.md`, `docs/architecture.md`
  - `tests/{test_guard_and_env.py,test_worktree_and_dispatch.py,test_branch_gate.py}`
  - `docs/tasks/active/20261117-intensity-model-routing.md`
- **Hard non-goals & constraints:**
  - Do **not** add a `helper` preset (keep presets strictly `general`, `implement`, `verify`).
  - Do **not** edit `hooks/pre-tool-use`, `scripts/keel_lib/guard_rules.py` (keep `<= 650` lines untouched), or bump `plugin.json` version (`0.1.0`).
  - Eliminate low-floor `<35` IQ tiers (`hydrogen-low`, `terra-low`, `6-luna`) and obsolete 6.0/6.1 flagship defaults (`gpt-6-astra`, `gpt-6.1-sol`) from `config/defaults.json`.

## Architecture & Exact Behavioral Spec

### 1. Four-Level Intensity Ladder (`1A` in `config/defaults.json`)
- Define `"levels": ["L1", "L2", "L3", "L4"]` and `"verify_min_level": "L2"`.
- Default executor `routing`: `{"general": "agy", "implement": "agy", "verify": "codex"}`.
- `executors.codex`:
  - `models_ranked`: `["gpt-6.2-astra", "gpt-6.2-sol", "gpt-6.1-terra"]`
  - `levels`:
    - `L1`: `{"model": "gpt-6.1-terra", "effort": "high"}`
    - `L2`: `{"model": "gpt-6.2-sol", "effort": "high"}`
    - `L3`: `{"model": "gpt-6.2-astra", "effort": "high"}`
    - `L4`: `{"model": "gpt-6.2-astra", "effort": "xhigh"}`
  - `subagent_default`: `{"model": "inherit", "effort": "inherit"}`
- `executors.agy`:
  - `models_ranked`: `["gemini-4-carbon-high", "gemini-4-hydrogen-xhigh", "gemini-4-hydrogen-high", "gemini-4-hydrogen-medium"]`
  - `levels`:
    - `L1`: `{"model": "gemini-4-hydrogen-medium"}`
    - `L2`: `{"model": "gemini-4-hydrogen-high"}`
    - `L3`: `{"model": "gemini-4-hydrogen-xhigh"}`
    - `L4`: `{"model": "gemini-4-carbon-high"}`

### 2. Explicit `--level`, Same-Level Cross-Vendor Diagonal `verify`, & Rework Escalation (`2A`)
- **Explicit `--level` (`L1`–`L4`, case-insensitive normalized to uppercase):**
  - `keel agent run` requires `--level L1|L2|L3|L4` (if omitted or invalid, raise a clear `KeelError` naming `L1`–`L4` and their scope criteria; `preset == "verify"` still rejects `L1`). Explicit `--model` / `--effort` flags still override the resolved model/effort when passed.
  - **Same-level, cross-vendor diagonal jump on `keel branch verify`:**
    - **Level resolution:** If `--level` is omitted on `keel branch verify` AND `state.get("last_implement_level")` is recorded on the branch, automatically inherit `level = max(state["last_implement_level"], verify_min_level)` (so `L2 -> L2`, `L3 -> L3`, `L4 -> L4`, and `L1 -> L2` floor). If `--level` is omitted AND `last_implement_level` is not recorded (e.g., manual commits without `keel agent run`), raise `KeelError` requiring explicit `--level L2|L3|L4`. If `--level` IS explicitly passed, enforce `level >= max(verify_min_level, state.get("last_implement_level"))`.
    - **Executor resolution (diagonal jump across vendors):** When `dual=False` and `--executor` is omitted on `keel branch verify`, inspect `state.get("last_implement_executor")`: if `"agy"` -> route `verify` to `"codex"`; if `"codex"` -> route `verify` to `"agy"`; otherwise fall back to `defaults["routing"]["verify"]` (`"codex"`).
  - **Align `rules/AGENTS.md` §4:** Update `rules/AGENTS.md` §4 so subagent execution routes through `keel-dispatch` (`keel agent run --level L1..L4`) rather than encouraging direct `invoke_subagent(Model="inherit")` on repo tasks (retaining the ban on `TypeName="research"`).
- **Branch intensity state & invariants (`<git_common_dir>/keel-state/branches/<branch>.json`):**
  - When `keel agent run` or `keel agent resume` runs with `mode == "write"` inside a non-default Git branch `B`:
    1. Compute required rework floor before execution:
       - Start with `min_level = "L1"`.
       - If `state.get("last_failed_implement")` has `"level": prev_fail_lvl`, `min_level = max(min_level, escalate_level(prev_fail_lvl))` (`L1->L2`, `L2->L3`, `L3->L4`, `L4->L4`).
       - Inspect the latest completed round in `state.get("rounds", [])`: if `outcome == "request_changes"` and `impl_lvl = round.get("implement_level_at_verify")`, then `min_level = max(min_level, escalate_level(impl_lvl))`.
       - Reject with `KeelError` if requested `level < min_level` (preventing same-tier rework loops after failure or rejection).
    2. If `adapter.execute` / `adapter.resume` raises an error on `mode == "write"`, record `state["last_failed_implement"] = {"level": level, "executor": adapter.name, "recorded_at": ...}` under `locked_branch_state` before re-raising.
    3. On completion of `mode == "write"`, update `state["last_implement_level"] = level`, `state["last_implement_executor"] = adapter.name`, and clear `state.pop("last_failed_implement", None)`.
  - `keel agent resume` accepts optional `--level`, `--model`, and `--effort` (defaulting `level` to `record.get("level")`), allowing in-place model escalation on warm sessions when rework requires stepping up a tier.
  - In `verify_branch`:
    - Enforce `level >= verify_min_level` (`L2`) AND if `state.get("last_implement_level")` is set, enforce `level >= state["last_implement_level"]`.
    - Snapshot `"verify_level": level` and `"implement_level_at_verify": state.get("last_implement_level")` onto the round entry.

### 3. Synchronous `agy -p` CLI Executor & Concurrent `--dual` Verification (`3B`)
- **`AgyExecutor` (`scripts/keel_lib/executors/agy.py`):**
  - `loads_plugin_spine = True`, `inject_schema_in_brief = False` (structured output enforced natively via `agy --json-schema`).
  - Build CLI argv: `["agy", "--new-project" | ("--conversation", session_id), "--dangerously-skip-permissions", *add_dir_flags, "--model", model, *(["--effort", effort] if effort else []), "--output-format", "json", *(["--json-schema", str(schema_path)] if schema_path else []), "-p", pointer_prompt]`, where `add_dir_flags` grants `--add-dir <run_dir>` plus git dirs (`resolve_git_add_dirs(workdir, extra_git_dirs)`) when in `mode == "write"` or `allow_disposable_write` (`scratch_verify`).
  - Execute synchronously via `run_process_group` with `env=tool_env({"KEEL_AGENT_MODE": mode})`, parse JSON stdout (`conversation_id`, `status == "SUCCESS"`, `structured_output` / `response`, `usage`), write structured JSON or text to `output_path`, persist `agy-result.json` (or `agy-resume.json`), and return `{"status": "completed", "executor": "agy", "level": level, "model": model, "effort": effort or "embedded", "session_id": conversation_id, ...}`.
- **True concurrent `--dual` in `verify_branch` (`scripts/keel_lib/branch_gate.py`):**
  - When `dual=True` and `executor is None`, run `codex` and `agy` concurrently via `ThreadPoolExecutor(max_workers=2)`, each in its own isolated `/tmp/keel-verify-<slug>-<exec>-*` clone, recording both verdicts and cleaning up both scratch clones automatically (`--dual --executor agy|codex` and `record-verdict` remain supported).

## Done-check

- `PYTHONDONTWRITEBYTECODE=1 python3 -W default -m unittest discover -s tests -v` passes with zero warnings, covering:
  - 4-level (`L1`–`L4`) resolution across `codex` and `agy`, explicit `--model`/`--effort` overrides, and missing/invalid `--level` rejection;
  - Same-level inheritance + cross-vendor diagonal jump (`agy <-> codex`) when `--level`/`--executor` are omitted on `keel branch verify` after an `implement` run, plus `verify >= max(L2, last_implement_level)` enforcement;
  - Automatic rework escalation (`L2 -> L3 -> L4`) after both failed `implement` runs and `request_changes` rounds (on both `agent run` and `agent resume`, without over-escalating consecutive rework passes within the same round);
  - `AgyExecutor` synchronous `agy -p` argv/JSON parsing/schema handling/resume `--conversation` and concurrent `--dual` verification.
- `./scripts/install-plugin --check` and `~/.local/bin/agy plugin validate --strict ~/.gemini/config/plugins/keel` pass.

## Progress

- [ ] Open baseline task spec on `main` and branch `.agents/worktrees/intensity-model-routing`
- [ ] Implement `config/defaults.json`, `executors/{base,agy,codex}.py`, `context.py`, `branch_gate.py`, `scripts/keel`, docs/skills, and unit/integration tests
- [ ] Run clean-clone independent verification and report to owner for landing authorization

## Decisions & Findings

- **Owner decisions locked (2026-11-17):** `1A` (4-level `L1`–`L4` ladder matching `Terra high / 6.2 Sol high / 6.2 Astra high / 6.2 Astra xhigh` and `Hydrogen medium / high / xhigh / Carbon high`) + `2A` (mandatory `--level`, `verify >= implement` lock, rework step-up escalation) + `3B` (pure synchronous `agy -p` CLI execution and concurrent `--dual` verification).
