---
name: keel-repo-ops
description: >-
  Use when founding or adopting a repository, authoring or updating a repo's root AGENTS.md (Core need, commands, gotchas), recording Architecture Decision Records (ADRs), or running safe releases and deployments.
---

# Keel Repo Ops — Repository Context, ADRs & Releases

## 1. Root `AGENTS.md` (high-signal project map, <100 lines)
Keep target-repo `AGENTS.md` concise so every main session and subagent absorbs global direction without context bloat:
- **`## Core need`:** 1–2 short paragraphs stating the real first-principles problem the project solves and what user/architectural value must not be traded away for local shortcuts. Write constructively (what the project *is* for, not long lists of dogmatic product bans).
- **`## Commands`:** Verified build, lint, test, and validation commands.
- **`## Gotchas`:** Only real, non-obvious traps hit in practice — never speculative boilerplate.

## 2. Architecture & costly-to-reverse decisions (`docs/`)
- Optional `docs/architecture.md`: Concise component map and invariants when a codebase grows beyond a few modules.
- Optional `docs/adr/NNNN-<slug>.md`: Reserve strictly for costly-to-reverse architectural choices. Record context, neutral comparison of viable options (pros, cons, risks, carry costs), and the chosen decision. Never create standalone `PRD.md` bureaucracy or commit `SUMMARY.md` / `HANDOFF.md` process files.

## 3. Release-time versioning & safe deployments
- Keep ordinary feature/fix PRs free of global version bumps so parallel worktrees never collide on `plugin.json` / `pyproject.toml` / `package.json`.
- Cut releases and run live deployments **only** with explicit human authorization (or documented standing delegation in that repo's `AGENTS.md`), preferring immutable versioned artifacts, atomic symlink/traffic cutover, post-cutover health verification, and immediate rollback readiness.
