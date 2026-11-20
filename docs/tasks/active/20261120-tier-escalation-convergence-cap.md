# Task: Tier-Aware Escalation Convergence Cap (`L2(1) -> L3(1) -> L4(2)`) and Dual-Side Step-Up

## Goal & Why

Keel's core routing model (`R1`, `R5`, `P4`) matches each slice to `L1`–`L4` and escalates one tier on rework (`L2 -> L3 -> L4`). Previously, a flat `max_review_rounds = 2` circuit breaker tripped after just 2 total `request_changes` rounds regardless of tier—meaning any slice starting honestly at `L2` hit the breaker after `L2 -> L3` without ever reaching `L4`, creating a perverse incentive to over-allocate `L3`/`L4` upfront (undermining `R1`) or interrupt the human prematurely before `L4` took a swing (undermining `R4`). Meanwhile, `verify_branch` did not enforce monotonic step-up when edits happened without a new `keel agent run` write, which would let a direct-edit loop spin at `L2`/`L3` indefinitely if the breaker only counted `L4` rejections.

This slice resolves both issues cleanly while preserving strict bounded convergence (`R3`, `R4`) and zero self-unlock hints on `--force-round`:
1. **Tier-aware convergence cap (`L2(1) -> L3(1) -> L4(2)`):** Keep `max_review_rounds: 2` in `config/defaults.json` as the top-tier (`L4`) rejection cap, and derive the total cap from the configured ladder (`len(lower verify tiers) + max_review_rounds`, i.e., `4` for `L2/L3/L4` with `max_review_rounds=2`). A branch trips the convergence cap once it reaches `>= max_review_rounds` (2) `request_changes` rounds at the top tier (`L4`) **or** `>= max_total_rounds` (4) total `request_changes` rounds:
   - Starting at `L2`: max 4 rounds (`L2 -> L3 -> L4 -> L4`)
   - Starting at `L3`: max 3 rounds (`L3 -> L4 -> L4`)
   - Starting at `L4`: max 2 rounds (`L4 -> L4`)
2. **Dual-side monotonic step-up (`prepare_branch_write` AND `verify_branch`):**
   - After a `request_changes` round, enforce the stepped-up floor (`escalate_level(...)`) on **both** rework writes (`keel agent run` / `keel agent resume`) **and** the next verification (`keel branch verify`).
   - Track whether `last_write` occurred after the latest round (e.g., via `after_round: len(rounds)` in `record_branch_write`, with backward compatibility for legacy state lacking `after_round`) so stale pre-rejection `last_write` metadata cannot hold the escalation floor back if a direct edit happens between rounds, while preserving the existing behavior when a fresh `L2` write is explicitly verified at `L4` (rework still steps up from `L2` to `L3`).
   - When `verify_branch` is invoked without `--level`, auto-select at least the computed floor (`max_level(minimum, "L3")` when there is no fresh `implement_level`, or `minimum` when there is) so callers don't crash on default level selection after escalation.
3. **Single source of truth across `verify_branch` and `evaluate_branch_gate`:** Both `verify_branch` (`check_round_limit`) and `evaluate_branch_gate` (`branch status` / `branch land` blockers) share the same convergence-cap helper. Keep the error message containing `"convergence cap"` and strictly free of any `"--force-round"` hint.

## Bounds

- **In scope:**
  - `scripts/keel_lib/branch_gate.py`:
    - Implement a shared tier-aware convergence-cap helper (handling `levels`, `verify_min_level`, and `max_review_rounds`, with sensible defaults `("L1", "L2", "L3", "L4")`, `"L2"`, `2` when callers pass only `max_review_rounds`).
    - Enforce monotonic step-up floor in both `prepare_branch_write` and `verify_branch` (including when no `keel agent run` write occurred after the prior rejection, and when `last_write` predates the prior rejection).
    - Update `verify_branch` default level selection when `--level` is omitted so it respects the stepped-up `minimum`.
    - Update `evaluate_branch_gate` and `branch_repo_overview` / `land_branch` to pass config cleanly and report the tier-aware cap accurately in blockers.
    - Keep `--force-round` backward-compatible, persist/return `forced_round: bool(force_round)`, and never mention `--force-round` in the convergence-cap `KeelError`.
  - `scripts/keel`: pass configured `levels` / `verify_min_level` (or `defaults`) if needed for `branch status` / `branch land`.
  - `rules/AGENTS.md` (§4 only; keep §§1–3 and stacked-branch guidance intact): update the convergence-cap wording in **Triage the verdict** to reflect the tier-aware ladder (step up on rework/verify up to `L4`, stop and escalate to the human after two rejected `L4` rounds—at most 4 rounds from `L2`, 3 from `L3`, 2 from `L4`—and never pass `--force-round` without explicit per-escalation human authorization).
  - `docs/requirements.md` (`P4`), `docs/architecture.md`, and `README.md` (brief alignment where the old flat "two review rounds" cap is mentioned).
  - `tests/test_branch_gate.py` & `tests/test_worktree_and_dispatch.py`:
    - Update existing convergence-cap tests to reflect the tier-aware cap (`L3 -> L4 -> L4` then blocked on 4th verify; `L4 -> L4` then blocked on 3rd verify).
    - Add explicit real-Git tests covering:
      1. `L2` start full ladder (`L2 -> L3 -> L4 -> L4`, then 5th blocked with `"convergence cap"` and without `"--force-round"`), plus `--force-round` recovery.
      2. `L3` start (`L3 -> L4 -> L4`, then 4th blocked) and `L4` start (`L4 -> L4`, then 3rd blocked).
      3. Direct-edit (no `keel agent run`) `verify --level L2` rejection -> next `verify --level L2` is refused (`KeelConfigError`), next omitted `--level` auto-escalates to `L3`, then after `L3` rejection next `verify --level L3` is refused and auto-escalates to `L4`.
      4. Stale `last_write` (`L2`) followed by R1 rejection and direct edit (no fresh `keel agent run`) still escalates R2 verify to `L3` and R3 verify to `L4` (cannot spin at `L3`).
      5. Fresh `L2` write verified explicitly at `--level L4` (`request_changes`) still allows `L3` rework and `L3` verify (preserving existing `test_write_routing_review_inheritance_and_rework_on_run_and_resume` contract).
      6. `branch status` (`evaluate_branch_gate`) blocker reflects the tier-aware cap consistently with `verify_branch`.
- **Out of scope:**
  - Do NOT change `--why` / `--task-file` handoff rules or CLI flags in this slice (that design is being aligned separately).
  - Do NOT bump `plugin.json` version (release-only versioning).
  - Do NOT alter `config/defaults.json` model routing or `executors` entries (`max_review_rounds: 2` stays `2`).

## Done-check

1. Full tier-aware escalation and dual-side step-up works across `L2` (max 4), `L3` (max 3), and `L4` (max 2) starts, both with `keel agent run` writers and with direct edits, with zero spinning at lower tiers.
2. Convergence-cap error contains `"convergence cap"` and never suggests `"--force-round"`; authorized `--force-round` still works and records `forced_round: True`.
3. `rules/AGENTS.md` §4, `docs/requirements.md`, `docs/architecture.md`, and `README.md` consistently describe the `L2(1) -> L3(1) -> L4(2)` convergence cap while keeping `rules/AGENTS.md` §§1–3 untouched.
4. Full test suite passes with zero warnings and zero bytecode pollution:
   `PYTHONDONTWRITEBYTECODE=1 python3 -W default -m unittest discover -s tests -v`
5. `git status` inside the worktree is clean with all changes committed on `task/tier-escalation-convergence-cap`.
