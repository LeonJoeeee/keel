---
name: keel-integrate
description: >-
  Use when verifying local task branches in a clean disposable clone, handling review convergence, proving pure-rebase git-tree equivalence, or landing/discarding branches on a protected default branch.
---

# Keel Integrate — Pure-Git Branch Verification & Guarded Landing

Resolve `<keel>` as this plugin's root directory (`../../scripts/keel`).

## 1. Upstream checks before verification
Ensure `git status --porcelain -uall` is completely clean in the author worktree and local test/lint suites pass without weakened assertions or masked symptoms.

## 2. Clean-context active verification (`keel branch verify`)
Every branch landing onto `main`/`master` requires independent verification in a remote-stripped disposable `/tmp` clone (`verdict.schema.json` enforced, recorded in `.git/keel-state/branches/`, checking against `main`'s locked task spec + a 1-line Canary mutation check against fake-green tests):

```sh
# Default cross-model Codex verifier (gpt-6-astra @ max, runs synchronously in disposable /tmp clone)
<keel>/scripts/keel branch verify <slug_or_branch> --project <repo_dir> [--check "<test_cmd>"]

# High-stakes dual verification (both Codex gpt-6-astra @ max AND built-in Antigravity must approve)
# Option A: launch both verifiers concurrently in parallel (recommended for speed — joins the same locked dual round):
<keel>/scripts/keel branch verify <slug_or_branch> --project <repo_dir> --dual --executor agy
<keel>/scripts/keel branch verify <slug_or_branch> --project <repo_dir> --dual --executor codex [--check "<test_cmd>"]
# Option B: single combined command (prepares agy descriptor first, then runs codex synchronously):
<keel>/scripts/keel branch verify <slug_or_branch> --project <repo_dir> --dual [--check "<test_cmd>"]

# Built-in Antigravity verifier standalone (returns invoke_subagent descriptor + disposable /tmp clone)
<keel>/scripts/keel branch verify <slug_or_branch> --project <repo_dir> --executor agy
# After the agy subagent writes verdict.json, record it and clean up the disposable clone:
<keel>/scripts/keel branch record-verdict <slug_or_branch> \
  --project <repo_dir> --verdict-file <path> --head-sha <sha> --executor agy --cleanup-scratch <dir>
```

### Convergence rules (`blocking` vs `notes`)
- **`blocking` items:** Real defects, Canary-surviving fake-green tests, weakened `main`-locked task bounds, regressions, or `Core need` drift. Fix in the author worktree and re-verify. Items prefixed `[Needs human decision]` escalate immediately to the human.
- **`notes` items:** Non-blocking observations or out-of-scope follow-ups (open in `docs/tasks/todo/` on `main` if warranted). Never block approval or trigger another review loop.
- **2-round circuit breaker:** After 2 `request_changes` rounds (`keel branch status <slug_or_branch>`), stop looping and escalate to the human (in `--dual`, a dual pass on one commit counts as 1 round).

## 3. Guarded local landing & discard (`keel branch land`)
Direct `git merge` / `git rebase` / non-`docs/tasks/` `git commit` on `main` is blocked by `hooks/pre-tool-use`. Inspect readiness with `keel branch status` or dry-run `keel branch land`; pass `--execute` or `--discard` **only** with explicit human authorization (or standing delegation):

```sh
# Inspect repo-wide todo/active tasks & worktrees, or a specific branch's gate status
<keel>/scripts/keel branch status [<slug_or_branch>] --project <repo_dir>

# Dry-run landing check (default strategy: atomic --squash bundling active -> archive task move)
<keel>/scripts/keel branch land <slug_or_branch> --project <repo_dir>

# Human-authorized atomic squash land (or pass --ff-only for fast-forward + archive commit)
<keel>/scripts/keel branch land <slug_or_branch> --project <repo_dir> [--squash|--ff-only] --execute

# Pure rebase onto advanced main without re-running LLM verification (re-run --check if prior head had checks)
<keel>/scripts/keel branch land <slug_or_branch> \
  --project <repo_dir> --old-base <OLD_BASE> --old-head <OLD_HEAD> [--check "<test_cmd>"] --execute

# Human-authorized discard (archives task with Outcome: [DISCARDED] on main, removes worktree + branch)
<keel>/scripts/keel branch land <slug_or_branch> --project <repo_dir> --discard --reason "<why>"
```
