# Task: Clarify Orchestrator Writer-Dispatch Default and Tighten Convergence-Cap Guard

## Goal & Why

In practice, main-session agents repeatedly bypassed `keel agent run` and directly implemented entire multi-file features and large documentation rewrites in the main session—bloat-polluting context, defeating `R1` model-cost matching, skipping `last_write` diagonal tracking, and losing Orchestrator detachment during review triage. One session also self-authorized `--force-round` 33 times in a row because the 2-round convergence cap error message directly suggested passing `--force-round`.

This slice restores Keel's Core need (`R1`, `R3`, `R4`, `R5`, `D1`) by:
1. Making the main session's **Orchestrator-by-default** boundary explicit: reserve direct main-session edits strictly for trivial, few-line single-point changes (where writing a brief outweighs the edit), and default all substantive slices (new files/modules, cross-file implementation, test suites, large doc rewrites, and review rework) to clean-context writer subagents via `keel agent run --mode write --preset implement`.
2. Explicitly clarifying that standing execution/landing delegation never waives the 2-round convergence cap and that `--force-round` requires explicit human authorization for that specific escalation.
3. Removing the self-unlock `--force-round` hint from `branch_gate.py`'s convergence-cap error message while recording `forced_round` transparently in recorded verification rounds and output (keeping the existing `--force-round` CLI flag backward-compatible for human-authorized escalations).

## Bounds

- **In scope:**
  - `rules/AGENTS.md` (§4 only; keep §§1–3 and the `## 1.`..`## 4.` section structure intact so `context.py` spine extraction is unaffected, and preserve all existing stacked-branch and `docs/tasks/todo/` locking rules).
  - `docs/requirements.md` (`D1` wording alignment; keep offline/existing-repo/no-ticket-template guarantees intact).
  - `skills/keel-repo-ops/SKILL.md` (Section 2 `D1` sentence alignment).
  - `scripts/keel_lib/branch_gate.py`:
    - Update the `max_rounds` convergence-cap `KeelError` message so it tells the caller to stop and escalate unresolved blockers to the human without suggesting `--force-round` (keep the `"convergence cap"` substring).
    - Record `"forced_round": bool(force_round)` in each appended round entry in `state["rounds"]` and return `"forced_round": bool(force_round)` in `verify_branch`'s result dict.
  - `scripts/keel`: add clear `--help` text on `branch verify --force-round` noting it is only for human-authorized escalations after hitting the convergence cap.
  - `tests/test_branch_gate.py` (and `tests/test_worktree_and_dispatch.py` if helpful): add/update tests verifying (a) the convergence-cap error includes `"convergence cap"` and does **not** suggest `"--force-round"`, (b) normal verify rounds persist and return `forced_round: False`, (c) `--force-round` remains backward-compatible and persists/returns `forced_round: True`, and (d) `rules/AGENTS.md` §§1–3 extraction into Codex briefs remains unaffected.
- **Out of scope:**
  - No breaking changes to `--force-round` CLI flag syntax (must stay a boolean flag).
  - No heuristic diff-size warnings or hard blocking of direct edits inside worktrees (`NR1`, `NR2`).
  - No version bump in `plugin.json` (release-only versioning).

## Done-check

1. `rules/AGENTS.md` §4 clearly establishes:
   - Main session = Orchestrator by default; direct edits are limited to trivial, few-line changes where writing a brief outweighs the edit.
   - Substantive slices and review rework default to `keel agent run` / `keel agent resume` at the matched `--level`.
   - Two-round convergence cap stops and escalates to the human; standing execution/landing delegation never authorizes `--force-round`, which requires explicit per-escalation human authorization.
   - All existing stacked-branch (`--base`, `docs/tasks/todo/`, `--ff-only` landing) guidance remains intact.
2. `docs/requirements.md` (`D1`) and `skills/keel-repo-ops/SKILL.md` align with the trivial-direct vs substantive-dispatched distinction.
3. `scripts/keel_lib/branch_gate.py` convergence-cap error omits any `--force-round` hint, keeps `"convergence cap"`, and records/returns `"forced_round": bool(force_round)`.
4. Full test suite passes with zero warnings and zero bytecode pollution:
   `PYTHONDONTWRITEBYTECODE=1 python3 -W default -m unittest discover -s tests -v`
5. `git status` is clean inside the worktree with all changes committed on `task/orchestrator-dispatch-and-cap-guard`.
