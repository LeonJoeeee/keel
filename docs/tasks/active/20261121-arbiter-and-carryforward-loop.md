# Task: Carry-Forward Re-Review, Writer Rebuttals, and 5-Action Arbiter Self-Recovery Loop

## Goal & Why

Keel's core user need is letting a single developer set direction for complex, multi-hour sequential engineering work and walk away—without babysitting agents, having broken/fake-green code merged onto `main`, or coming back the next morning to find the entire sequential pipeline stalled at 1:00 AM over a Writer ↔ Verifier deadlock.

Previously, multi-round verification had three structural deadlock causes:
1. **Stateless amnesia on Round 2+ re-review:** Subsequent verifiers received zero structured record of prior `blocking` items or why changes were made, causing ping-pong reversals and repeated false positives—yet blinding Round 2+ to only incremental lines would risk missing genuine `P0` bugs overlooked in Round 1.
2. **Forced code compliance without structured pushback:** Writers had no CLI/gate channel to dispute false-positive or out-of-bounds findings on task-file-free or unchanged-tree branches, pressuring them into patching clean code ("fix-induced regression cycle").
3. **No autonomous deadlock resolution:** Reaching a rebuttal stalemate or the convergence cap halted the sequential chain waiting for a human (`R4`), even when an independent judge could either waive non-blocking pedantry (`approve`), issue one surgical fix (`rework_once`), or hand an orchestration-level recovery (`replan` / `pivot`) back to the Orchestrator while reserving `park` strictly for genuine human-authority boundaries or exhausted recovery budgets.

This slice implements the complete root-cause anti-deadlock loop while keeping full `merge_base..HEAD` verification 100% unblinded and preserving all existing `branch verify` / `branch status` contracts.

## Bounds

- **In scope:**
  - `config/defaults.json`:
    - Add top-level `"max_structural_recoveries": 2`. Keep `defaults["routing"]` unchanged (`{"general": "agy", "implement": "agy", "verify": "codex"}`) so existing routing assertions remain intact.
  - `config/schemas/arbiter.schema.json`:
    - Strict JSON Schema (`additionalProperties: false`) requiring `action` (`"approve" | "rework_once" | "replan" | "pivot" | "park"`), `summary` (non-empty string), `directives` (array of strings), `avoid_dead_ends` (array of strings), `waived_findings` (array of strings), and `commands_run` (array of strings).
  - `config/presets/verify.md` & `config/presets/arbitrate.md`:
    - Update `config/presets/verify.md` to explain Round 2+ Carry-Forward re-adjudication: re-evaluate prior `blocking` items against new commits and evidence-backed author rebuttals (retracting fixed or validly refuted items into `notes` or dropping them), **while keeping full `merge_base..HEAD` inspection completely open for genuine newly discovered defects**. Ground proportionality in the user's `Core need` and `Design Philosophy` rather than developer self-indulgence.
    - Add `config/presets/arbitrate.md` defining the impartial clean-context Arbiter lens in a disposable `/tmp` clone (`mode="read"`): weigh the user's `## Core need`, `## Design Philosophy`, `main`-locked task spec, passing deterministic checks, committed diff, and the Writer ↔ Verifier dispute/rejection history to choose:
      - `approve`: disputed findings are false positives, out-of-scope, or non-blocking polish that do not harm real user workflows or violate locked criteria;
      - `rework_once`: a genuine defect exists (including a missed Round 1 bug caught in Round 2+) with a clear, localized fix on the current branch;
      - `replan`: the slice is oversized/coupled or blocked by an upstream prerequisite / tight `Bounds` that the Orchestrator should split or sequence first;
      - `pivot`: the task size is fine, but the implementation took a flawed technical path and became tangled across patches, warranting a clean rewrite from `base` avoiding the recorded dead ends;
      - `park`: strictly reserved for genuine product/public-contract tradeoffs requiring human authority, external environment/credential blockers, or exhausted structural recoveries.
  - `scripts/keel_lib/executors/agy.py`, `scripts/keel_lib/executors/codex.py`, `scripts/keel`:
    - Treat `preset == "arbitrate"` identically to `preset == "verify"` for `enforce_verify_min_level` and `ephemeral = req.mode == "read" and req.preset in ("verify", "arbitrate")` (non-resumable ephemeral sessions). Use `keel-verify-arbiter-` as the disposable clone prefix so `is_disposable_verify_path` works seamlessly.
  - `scripts/keel_lib/branch_gate.py`:
    - Persist new optional branch-state fields in `edit_gate_state`: `"rebuttals"`, `"arbitration"`, `"arbiter_rework_used"`, `"arbiter_rework_after_round"`, `"parked"`, `"park_reason"`, `"recovery_count"`, `"avoid_dead_ends"`, `"prior_directives"`, `"superseded_from"`.
    - **1. Structured Writer Rebuttals (`dispute_branch`):**
      - `dispute_branch(project, branch_or_slug, *, reason, base=None, defaults=...)`: require at least one active `request_changes` round in the current cycle (`rounds` after the last `approve`, with `rounds[-1]["outcome"] == "request_changes"`); require non-empty `reason`; append `{ "round": len(rounds), "reviewed_head_sha": rounds[-1]["head_sha"], "head_sha": head_sha, "reason": _trim_markdown_block(reason), "recorded_at": _now_iso() }` to `state["rebuttals"]` (and clear `"parked"` only if not parked; reject disputing an already `parked` branch).
    - **2. Unblinded Carry-Forward Re-Review (`verify_branch` Round 2+):**
      - Identify active-cycle rounds (`cycle_rounds`: entries in `rounds` after the most recent `approve`).
      - When `cycle_rounds` is non-empty (Round 2+ of the active cycle): build `### Prior Review Carry-Forward Ledger (re-adjudicate prior items; full diff remains open for genuine missed defects)` containing:
        - Prior round's `head_sha`, `prev_sha..head_sha` commit log (or explicit note if `prev_sha == head_sha`), and prior unresolved `blocking` items;
        - Any `state["rebuttals"]` recorded in the current cycle (never writer chat logs/`output.md` per `R2`).
      - On single-verifier Round 2+: include the Carry-Forward ledger in `task_lines`.
      - On `--dual` Round 2+: include the Carry-Forward ledger ONLY for the primary cross-vendor verifier (`diagonal.get(last_write.get("executor")) or defaults["routing"]["verify"]`) and run the second verifier without the Carry-Forward ledger (clean-slate blind).
      - Support optional `auto_arbitrate: bool = False` (`--arbitrate` CLI flag on `keel branch verify`): when `auto_arbitrate=True` and `verify_branch` records `outcome == "request_changes"` AND the branch now satisfies arbitration eligibility (convergence cap reached OR rebuttal deadlock where a rebuttal was filed via `dispute_branch` or branch `Review Rebuttals` before this round and the verifier still rejected), automatically invoke `arbitrate_branch` and attach `"arbitration"` to the returned dict (updating `"outcome": "approve"` if Arbiter approved!). Keeping `auto_arbitrate=False` by default when `--arbitrate` is omitted preserves 100% backward compatibility with all existing `verify_branch` callers and unit tests while letting Orchestrators either pass `--arbitrate` or invoke `keel branch arbitrate <slug>` explicitly!
      - Update `_convergence_cap_blocker` and `verify_branch`'s cap error message to still contain `"convergence cap"` and NEVER mention `"--force-round"`, while noting `keel branch arbitrate` when eligible (unless already `parked`, in which case refuse further non-forced `verify_branch` / `arbitrate_branch` runs with a clear `"parked"` `KeelError`).
    - **3. 5-Action Arbiter (`arbitrate_branch` + `validate_arbiter_payload`):**
      - Validate Arbiter JSON strictly against `arbiter.schema.json`; require non-empty `directives` whenever `action in ("rework_once", "replan", "pivot", "park")`.
      - Preconditions before launching Arbiter:
        1. Branch is not currently `parked` (unless `force_round=True`).
        2. Linked worktree (if present) is clean (`git status --porcelain -uall` empty and `wt_head == head_sha`).
        3. Optional `--check` commands run and pass; existing `checks_by_sha[head_sha]` is not failing; and if the latest `request_changes` round ran deterministic checks on an earlier SHA that haven't been run on `head_sha`, wait—require `head_sha` ITSELF to already carry a scope-matching `request_changes` round (`latest_for(head_sha)` has `outcome == "request_changes"` and `_round_matches_scope` for current `merge_base_sha` and `expected_task_blob`), which guarantees `head_sha` was already verified!
        4. Eligibility: require EITHER `cap["cap_reached"]` OR a recorded dispute/rebuttal in the current cycle (`state["rebuttals"]` OR non-empty branch `Review Rebuttals` section differing from `locked_spec`).
      - Level & executor enforcement: compute `min_arb_level = max_level(top_level, _round_level(current_round, default_level=default_manual_level))` (i.e. `L4` on the standard ladder). Default `level` to `min_arb_level`, and raise `KeelConfigError` if explicit `--level` is below `min_arb_level` (never allow an `L2`/`L3` Arbiter to overrule an `L4` rejection!). Default `executor` to cross-vendor from `last_write.get("executor")` (else opposite of single verifier in `current_round["verdicts"]`, else `defaults["routing"]["verify"]`).
      - Deterministic post-validation safety clamps:
        1. **Single-use `rework_once`:** If `action == "rework_once"` and `state.get("arbiter_rework_used")` is already `True`, clamp `action` to `"pivot"` (if `recovery_count < max_structural_recoveries`) else `"park"`, recording `"clamped_from": "rework_once"` and appending explanation to `summary` and `directives`.
        2. **Macro-recovery safety fuse (`max_structural_recoveries`, default `2`):** If `action in ("replan", "pivot")` and `int(state.get("recovery_count") or 0) >= max_structural_recoveries`, deterministically clamp `action` to `"park"`, recording `"clamped_from": raw_action` and appending fuse explanation to `summary` and `directives`.
      - State transitions per effective `action`:
        - `approve`: store `state["arbitration"]` (pinned to `head_sha`, `merge_base_sha`, `task_file`, `task_blob_sha`, `after_round: len(rounds)`). `evaluate_branch_gate` recognizes an active scope-matching `arbitration["action"] == "approve"` on `head_sha` (with `after_round == len(rounds)`) as `approval_source = f"arbiter-approve:{head_sha[:12]}"` (`ready_to_land = True`), AND carries `arbiter-approve` across a proven pure rebase (`--old-base` / `--old-head`) identically to `direct-approve`! On `land_branch --execute` (both `squash` and `ff-only`), include trailer `Keel-Arbitrated: approve (<run_id>)` and, when archiving `docs/tasks/active/<task>.md` to `docs/tasks/archive/<task>.md`, append `## Arbitration Record` (run ID, executor/level, summary, and `waived_findings`) into the archived task file!
        - `rework_once`: set `state["arbiter_rework_used"] = True`, `state["arbiter_rework_after_round"] = len(rounds)`, `state["prior_directives"] = directives`, and store `state["arbitration"]`. While `arbiter_rework_after_round` is active and NO new `request_changes` round has been recorded in `rounds[arbiter_rework_after_round:]`, `_convergence_cap_status` reports `cap_reached = False` (unlocking 1 bonus rework + verification round without `--force-round`!) and `_rework_floor` / `_verify_floor` enforce `top_level` (`L4`). Once a post-grant `request_changes` round is recorded, the bonus allowance is consumed (`cap_reached = True` again).
        - `replan` | `pivot`: store `state["arbitration"]` (`action`, `directives`, `avoid_dead_ends`, etc.). `evaluate_branch_gate` reports blocker guiding `keel branch supersede <slug>`.
        - `park`: set `state["parked"] = True`, `state["park_reason"] = summary`, and store `state["arbitration"]`. `evaluate_branch_gate` includes `"parked": True` and blocker `"Branch '<branch>' is parked for human decision: <summary>"` (only explicit `--force-round` or `--human-override` can clear/override `parked`).
    - **4. Safe Branch Retirement & Lineage Propagation (`supersede_branch` + `--supersedes`):**
      - `supersede_branch(project, branch_or_slug, *, defaults, base=None, reason=None, archive_task=False)`:
        - Require `main` session mode (blocked for `read`/`write` subagents).
        - Under `locked_repo_land(common_dir)`: require primary checkout on `base_ref` and clean (`porcelain` empty); require linked worktree (if present) clean (`porcelain` empty and `wt_head == pinned_head_sha`) so uncommitted worktree edits are NEVER destroyed!
        - Require active `state["arbitration"]` with `action in ("replan", "pivot")` matching `pinned_head_sha` and `after_round == len(rounds)` (arbitrary unarbitrated branches still require human `--discard`).
        - Preserve exact Git commit at `refs/keel/superseded/<slug>/<utc_stamp>-<pinned_head_sha[:12]>` via `git update-ref` (verify ref exists!).
        - Compute `next_recovery_count = int(state.get("recovery_count") or 0) + 1`, merge `avoid_dead_ends` (accumulating prior + new deduplicated items), and persist lineage JSON at `<git_common_dir>/keel-state/lineage/<slug>.json`.
        - Optionally when `archive_task=True` and `task_file` exists, archive `task_file` to `docs/tasks/archive/<name>` with `## Outcome: [SUPERSEDED BY ARBITER: <ACTION>]`, preserved ref, summary, directives, and dead ends committed on `base_ref` (when `archive_task=False`, leave `docs/tasks/active/` on `base_ref` intact so same-slug `pivot` or Orchestrator task-splitting can proceed immediately).
        - Remove worktree and `refs/heads/<branch>`; reset branch gate state `rounds`/`checks_by_sha`/`last_write`/`rebuttals`/`arbitration`/`arbiter_rework_*`/`parked` while retaining `recovery_count = next_recovery_count`, `avoid_dead_ends`, and `prior_directives` on `<branch>`'s state in case the exact same `<slug>` is recreated!
      - Lineage inheritance (`--supersedes <old_slug>` on `keel worktree create` and `keel agent run`, PLUS automatic lookup when `<slug>` itself has a lineage file `<git_common_dir>/keel-state/lineage/<slug>.json`):
        - Propagate `recovery_count`, `avoid_dead_ends`, and `prior_directives` into the successor branch's gate state (`keel-state/branches/<new_branch>.json`).
        - In `keel agent run` (when `mode == "write"` on a branch carrying `avoid_dead_ends` or `prior_directives`, or via `--supersedes <old_slug>`): automatically append a `### Prior Attempt Dead-Ends & Arbiter Directives` block to `extra_context` so the fresh writer avoids the failed path!
  - `scripts/keel` & `scripts/keel_lib/guard_rules.py`:
    - Add CLI subcommands:
      - `keel branch dispute <branch> (--reason <text> | --reason-file <file>) [--base <ref>] [--project <dir>]`
      - `keel branch arbitrate <branch> [--base <ref>] [--executor <executor>] [--level <level>] [--model <model>] [--effort <effort>] [--check <cmd>]... [--skip-checks <reason>] [--task-file <file>] [--why <why>] [--context-file <file>] [--output-dir <dir>] [--force-round] [--project <dir>]`
      - `keel branch supersede <branch> [--base <ref>] [--reason <text>] [--archive-task] [--keep-branch] [--project <dir>]`
      - Add `--arbitrate` flag to `keel branch verify`, and `--supersedes <old_slug>` flag to `keel worktree create` and `keel agent run`.
    - Enforce subagent guards in BOTH `scripts/keel` and `scripts/keel_lib/guard_rules.py`:
      - Block `keel branch arbitrate` and `keel branch supersede` in both `read` and `write` modes.
      - Block `keel branch dispute` in `read` mode (allow in `main` and `write` modes).
      - **Hard invariant:** Keep `scripts/keel_lib/guard_rules.py` at **`<= 650` lines** (`tests/test_guard_and_env.py:311` asserts `line_count <= 650`!).
  - `rules/AGENTS.md` (§4 only; keep §§1–3 intact and preserve required clauses in `tests/test_worktree_and_dispatch.py`, updating only if assertions are updated in lockstep), `docs/requirements.md` (`R3`, `P4`), `docs/architecture.md`, `README.md`:
    - Document Carry-Forward re-review, `keel branch dispute` / `### Review Rebuttals`, and the 5-action `keel branch arbitrate` + `keel branch supersede` Orchestrator self-recovery loop cleanly.
  - `tests/test_branch_gate.py`, `tests/test_worktree_and_dispatch.py`, `tests/test_guard_and_env.py`:
    - Comprehensive unit & real-Git CLI tests covering:
      1. Round 2+ Carry-Forward ledger injection (prior `blocking` + commit delta + `branch dispute` rebuttals, zero writer chat leakage) on single verify, AND `--dual` Round 2+ split (primary verifier receives Carry-Forward ledger while second verifier runs clean-slate blind); plus post-`approve` new commit starting a clean cycle without stale carry-forward.
      2. `keel branch dispute` validation (`--reason` & `--reason-file` UTF-8 handling, rejected when no active `request_changes` round, allowed in `write`/`main`, blocked in `read`).
      3. `keel branch arbitrate` preconditions (rejects unreviewed `head_sha`, dirty worktree, stale scope/base, failing checks, non-deadlocked round without cap/rebuttal, or `--level` below `L4`).
      4. All 5 Arbiter actions end-to-end:
         - `approve`: unlocks `branch status` (`arbiter-approve:<sha12>`) and `branch land --execute` (both `--squash` and `--ff-only`), writes `Keel-Arbitrated:` commit trailer and `## Arbitration Record` (with `waived_findings`) into `docs/tasks/archive/<task>.md`, and carries across `compare_rebase` pure rebase (`--old-base` / `--old-head`)! Also test `branch verify --arbitrate` inline auto-trigger!
         - `rework_once`: unlocks 1 bonus `L4` write + `branch verify` round without `--force-round`; if that bonus round fails again, a second `rework_once` from the Arbiter is deterministically clamped to `pivot` (or `park` at fuse).
         - `replan` & `pivot` + `keel branch supersede`: refuses dirty worktrees or unarbitrated branches; preserves `refs/keel/superseded/<slug>/...` in Git; supports both `--archive-task` (`replan` splitting into `slice-a` / `slice-b` with `--supersedes`) and same-slug `pivot` (keeping `docs/tasks/active/` on `main`), automatically injecting `### Prior Attempt Dead-Ends & Arbiter Directives` into the successor `agent run` brief!
         - `park` & `max_structural_recoveries` (`2`) fuse: after 2 `replan`/`pivot` recoveries (`attempt 1 -> supersede -> attempt 2 -> supersede -> attempt 3`), a 3rd `replan` or `pivot` Arbiter output is deterministically clamped to `park` (`parked: True`), blocking further non-forced loops.
      5. `guard_rules.py` (`<= 650` lines) & CLI subagent mode blocks for `branch arbitrate`, `branch supersede`, and `read`-mode `branch dispute`.
- **Out of scope:**
  - Do NOT modify `rules/AGENTS.md` §§1–3 or bump `plugin.json`.

## Done-check

1. All 48 existing tests plus all new Carry-Forward, Rebuttal, 5-Action Arbiter, `supersede` lineage, macro-fuse, and subagent guard tests pass with zero warnings under `KEEL_AGENT_MODE=read`:
   `KEEL_AGENT_MODE=read PYTHONDONTWRITEBYTECODE=1 python3 -W default -m unittest discover -s tests -v`
2. `scripts/keel_lib/guard_rules.py` stays `<= 650` lines.
3. `rules/AGENTS.md` §4, `docs/requirements.md`, `docs/architecture.md`, `README.md`, `config/presets/verify.md`, `config/presets/arbitrate.md`, and `config/schemas/arbiter.schema.json` are aligned and clean.
4. `git status --porcelain -uall` in the worktree is clean with zero `__pycache__` artifacts.
