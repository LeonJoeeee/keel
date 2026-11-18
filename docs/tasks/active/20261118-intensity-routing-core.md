# Task: Single-Source `L1`–`L4` Intensity Routing, Thread-Safe Process Groups, & `keel agent --level`

## Goal & Why

Implement the single-source `L1`–`L4` capability ladder (`P3`, `R1`, `R5`) across `config/defaults.json`, `env.py`, `context.py`, `executors/{base,codex}.py`, and `keel agent run/resume`:
1. Keep concrete model names and reasoning efforts exclusively in `config/defaults.json` (no duplicated fallback model tables hardcoded in Python) so future model upgrades edit one file.
2. Require explicit `--level L1|L2|L3|L4` on `keel agent run` (rejecting `L1` for `preset=verify`) and support `--level` / `--model` / `--effort` step-up overrides on `keel agent resume`.
3. Make `run_process_group` thread-safe across concurrent worker threads (`forward_signals_to_child_groups()` installing `signal.signal` only on the main thread) and make `save_run_record` atomic via temp-file `os.replace`, laying the clean foundation for Slice 3's synchronous `agy -p` adapter and flat `branch_gate.py` rewrite.

## Bounds

- **Allowed paths:**
  - `config/defaults.json`
  - `scripts/keel`
  - `scripts/keel_lib/env.py`
  - `scripts/keel_lib/context.py`
  - `scripts/keel_lib/executors/{base.py,codex.py,agy.py}`
  - `tests/{test_guard_and_env.py,test_worktree_and_dispatch.py}`
  - `docs/tasks/active/20261118-intensity-routing-core.md`
- **Non-goals:**
  - Keep `AgyExecutor`'s synchronous `agy -p` conversion and the flat `branch_gate.py` rewrite for Slice 3 (`AgyExecutor` in this slice only updates its `resume` signature to accept `*, level=None, model=None, effort=None` and retains `VALID_AGY_MODELS` compatibility with `branch_gate.py`).
  - Do not edit `hooks/pre-tool-use`, `scripts/keel_lib/guard_rules.py`, `scripts/keel_lib/branch_gate.py`, or `plugin.json`.

## Exact Specification

1. **`config/defaults.json` (Single Source of Truth):**
   - `"levels": ["L1", "L2", "L3", "L4"]`, `"verify_min_level": "L2"`, `"routing": {"general": "agy", "implement": "agy", "verify": "codex"}`.
   - `executors.codex`:
     - `models_ranked`: `["gpt-6.2-astra", "gpt-6.2-sol", "gpt-6.1-terra"]`
     - `levels`: `L1` (`gpt-6.1-terra` @ `high`), `L2` (`gpt-6.2-sol` @ `high`), `L3` (`gpt-6.2-astra` @ `high`), `L4` (`gpt-6.2-astra` @ `xhigh`)
     - `subagent_default`: `{"model": "inherit", "effort": "inherit"}`
   - `executors.agy`:
     - `models_ranked`: `["gemini-4-carbon-high", "gemini-4-hydrogen-xhigh", "gemini-4-hydrogen-high", "gemini-4-hydrogen-medium"]`
     - `levels`: `L1` (`gemini-4-hydrogen-medium`), `L2` (`gemini-4-hydrogen-high`), `L3` (`gemini-4-hydrogen-xhigh`), `L4` (`gemini-4-carbon-high`), while retaining `"presets"` compatibility ONLY if needed by pre-Slice-3 `AgyExecutor` (or letting pre-Slice-3 `AgyExecutor` default cleanly to `"inherit"`).
2. **`scripts/keel_lib/env.py` & `scripts/keel_lib/context.py`:**
   - `env.py`: Add main-thread-safe `forward_signals_to_child_groups()` + lock-free `_ACTIVE_PGIDS_SNAPSHOT` signal forwarding + `check_interrupted()` so `run_process_group()` works safely inside `ThreadPoolExecutor` worker threads without raising `ValueError: signal only works in main thread`.
   - `context.py`: Make `save_run_record()` write atomically via a temp file in `run_dir` + `os.replace`, and generalize the non-schema output contract sentence to `"captured automatically via CLI output"`.
3. **`scripts/keel_lib/executors/{base.py,codex.py,agy.py}` & `scripts/keel`:**
   - `base.py`: Define `KeelConfigError`, `KeelExecutionError`, `VALID_LEVELS`, `normalize_level`, `level_rank`, `max_level`, `escalate_level`, `enforce_verify_min_level`, `resolve_executor_level_config` (reading strictly from `defaults["executors"][executor_name]["levels"]` — zero hardcoded fallback model names in Python), `resolve_git_add_dirs`, `RunRequest(..., level: str | None = None)`, and `ExecutorAdapter.resume(..., *, level=None, model=None, effort=None)`.
   - `codex.py`: Resolve `(level, model, effort)` via `resolve_executor_level_config("codex", effective_level, defaults)` with explicit `--model`/`--effort` overrides (allowing `req.level is None` to fall back to `"L3"` strictly for internal pre-Slice-3 `branch_gate.py` calls while `keel agent run` CLI requires `--level`), enforce `enforce_verify_min_level` on `preset == "verify"`, and support `resume(..., *, level=None, model=None, effort=None)`.
   - `scripts/keel`: Require `--level` on `keel agent run` (validating via `normalize_level` and `enforce_verify_min_level` when `preset == "verify"`); add `--level`, `--model`, and `--effort` to `keel agent resume`.

## Done-check

- `PYTHONDONTWRITEBYTECODE=1 python3 -W default -m unittest discover -s tests -v` passes with zero warnings, covering:
  - `L1`–`L4` single-source resolution from `config/defaults.json` (and clean `KeelConfigError` when `defaults` lacks mappings or `--level` is missing/invalid or `L1` is passed for `preset=verify`);
  - `CodexExecutor` argv generation across `read`/`write`/disposable-verify modes, explicit `--model`/`--effort` overrides, and `resume --level` escalation;
  - `run_process_group` concurrent execution inside `ThreadPoolExecutor` worker threads without `signal.signal` errors, plus atomic `save_run_record`.
- Temporary plugin install + `./scripts/install-plugin --plugin-dir <tmp_dir> --no-bin --check` and `~/.local/bin/agy plugin validate --strict <tmp_dir>` pass.
