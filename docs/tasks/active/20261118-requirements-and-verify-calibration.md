# Task: Unified Requirements Baseline & Realistic Verifier Calibration

## Goal & Why

Update `docs/requirements.md` and `config/presets/verify.md` to capture the real engineering lessons from dogfooding Keel before modifying runtime code:
1. Reinforce Layer 1 (`What & Why`) with proportional slicing (`D1`), realistic-scenario verification convergence + Orchestrator root-cause triage (`R3`), task-intensity-matched intelligence (`R1`/`R5`), and responsibility-based simplicity free of arbitrary hard line-count targets (`D3`/`NR1`).
2. Align Layer 2 (`How`) around resident Orchestrator spine injection (`rules/AGENTS.md` vs single low-frequency `keel-repo-ops` skill), single-source `L1`–`L4` model configuration (`config/defaults.json`), synchronous `codex` + `agy -p` CLI execution (omitting `--new-project`), same-level cross-vendor diagonal verification, and a flat local branch gate without async pending-verifier state machines.
3. Calibrate `config/presets/verify.md` immediately on `main` so all subsequent slice verifications focus on realistic caller workflows, `Core need` alignment, and 1-line Canary checks rather than contrived fault injections or arbitrary line-count policing.

## Bounds

- **Allowed paths:**
  - `docs/requirements.md`
  - `config/presets/verify.md`
  - `docs/tasks/active/20261118-requirements-and-verify-calibration.md`
- **Non-goals:**
  - Do not modify Python code, `config/defaults.json`, `rules/AGENTS.md`, or `skills/` in this slice (those land cleanly in subsequent slices once runtime flags are in place).

## Done-check

- `docs/requirements.md` cleanly separates Layer 1 (timeless requirements & outcome red lines) from Layer 2 (current engineering practices), explicitly forbidding arbitrary upfront line-count constraints while encoding proportional slicing, realistic-scenario review convergence, Orchestrator root-cause triage, single-source `L1`–`L4` model routing, resident Orchestrator instructions, synchronous `agy -p` without `--new-project`, same-level cross-vendor diagonal verification, and a flat local branch gate.
- `config/presets/verify.md` preserves clean-clone isolation, `main`-locked spec checks, and the 1-line Canary mutation check while explicitly scoping `blocking` to realistic usage scenarios and prohibiting contrived OS/kernel fault injections or arbitrary line-count nitpicks.
- `PYTHONDONTWRITEBYTECODE=1 python3 -W default -m unittest discover -s tests -v` passes.
- `./scripts/install-plugin --check` and `~/.local/bin/agy plugin validate --strict ~/.gemini/config/plugins/keel` pass.
