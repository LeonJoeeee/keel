---
name: keel-integrate
description: >-
  Use when verifying pull requests or branches in a clean context, handling review convergence, checking pure-rebase tree equivalence, or merging changes into a protected default branch.
---

# Keel Integrate — Independent Verification & Guarded Merge

Resolve `<keel>` as this plugin's root directory (`../../scripts/keel`).

## 1. Upstream checks before review
Before commissioning independent verification, ensure `git status --porcelain -uall` is clean in the author worktree and required CI / local test suites pass without weakened assertions or masked symptoms.

## 2. Clean-context active verification (`keel pr verify`)
Every PR to the default branch requires independent verification from a fresh session that did not author the code (works with or without a GitHub Issue, including small fixes and Dependabot PRs):

```sh
# Codex verifier (runs in disposable /tmp clone, enforces verdict.schema.json, optionally posts clean PR comment)
<keel>/scripts/keel pr verify <pr_number> --project <repo_dir> [--executor codex] [--why "..."] --publish

# Built-in Antigravity verifier (prepares disposable /tmp clone + file-pointer spawn spec)
<keel>/scripts/keel pr verify <pr_number> --project <repo_dir> --executor agy [--why "..."]
# After the native subagent writes verdict.json, publish and clean up:
<keel>/scripts/keel pr publish-verdict <pr_number> --project <repo_dir> --verdict-file <path> --head-sha <sha> --executor agy
```

### Convergence rules (`Blocking` vs `Notes`)
- **`blocking` items:** Real defects, regressions, scope/safety violations, or `Core need` drift. Fix in the author lane and re-verify. Items prefixed `[Needs human decision]` escalate immediately to the human.
- **`notes` items:** Non-blocking observations. Never block approval or trigger another fix/review loop.
- **2-round circuit breaker:** If 2 fix rounds fail to converge (`keel pr status <pr_number>`), stop looping and escalate to the human to reshape direction or authorize `--human-override`.

## 3. Guarded integration (`keel pr merge`)
Raw `gh pr merge` is blocked by `hooks/pre-tool-use`. Always check gate readiness via `keel pr merge` (add `--execute` **only** when the human has authorized merging this PR or granted `delegated` merge authority):

```sh
# Dry-run gate check (ancestry, skipped-aware CI, and matching Approve verdict)
<keel>/scripts/keel pr merge <pr_number> --project <repo_dir>

# Pure rebase onto advanced main without re-running LLM review (proves exact git tree equality)
<keel>/scripts/keel pr merge <pr_number> --project <repo_dir> --old-base <OLD_BASE> --old-head <OLD_HEAD> [--execute]

# Human-authorized direct merge override (only with explicit human words)
<keel>/scripts/keel pr merge <pr_number> --project <repo_dir> --human-override "<exact authorization & reason>" --execute
```

After merge, clean up any local worktree (`keel worktree cleanup <slug> --project <repo_dir> --pr <pr_number>`).
