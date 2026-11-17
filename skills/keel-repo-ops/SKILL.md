---
name: keel-repo-ops
description: >-
  Use when founding or adopting a repository, authoring or updating a repo's root AGENTS.md (5-section <100-line map), managing the docs/tasks/ ledger on main, recording Architecture Decision Records (ADRs), or running safe releases and deployments.
---

# Keel Repo Ops — Repository Map, Task Ledger, ADRs & Releases

## 1. Root `AGENTS.md` (5-section project map, `<100` lines ceiling)
Keep `<repo>/AGENTS.md` strictly under **100 lines** so every main session and subagent absorbs global direction at zero reasoning drag (`R1`, `R2`). Notice that **90%+ of routine tasks do not touch `AGENTS.md`**; when approaching 100 lines, prune/merge stale items or move details into `docs/` — never copy code-searchable file/symbol inventories into `AGENTS.md`.

Use these exact 5 sections:
1. **`## Core need`:** 1–2 short paragraphs stating the real first-principles problem the project solves and what user/architectural value must not be traded away for local shortcuts.
2. **`## Architecture & Boundaries`:** High-level module ownership, runtime boundaries, and hard invariants.
3. **`## Commands`:** Verified build, lint, test, and validation commands.
4. **`## Gotchas`:** Only real, non-obvious traps hit in practice — never speculative boilerplate.
5. **`## Where to look`:** Strictly **conditional routing** (`"When doing X -> read Y"`), never mandatory upfront pre-reads that bloat every turn.

## 2. Repo-native `docs/tasks/` ledger (`main`-anchored, zero-conflict)
Replace external issue trackers with `docs/tasks/{todo,active,archive}/<YYYYMMDD>-<slug>.md` (`D1`: skip task files for trivial 2–5 line fixes):
- **Orchestrator owns opening on `main`:** Open and commit task specs in `docs/tasks/todo/` or `docs/tasks/active/` directly on `main` **before** branching `.agents/worktrees/<slug>` (`hooks/pre-tool-use` permits `git commit` on `main` iff every touched path is under `docs/tasks/`).
- **Spec structure:**
  - Short tasks: `## Goal & Why`, `## Bounds`, `## Done-check`.
  - Multi-step tasks add: `## Progress` and `## Decisions & Findings`.
- **Immutability boundary:** Subagent workers on a branch may update `## Progress` and `## Decisions & Findings`, but must **never** weaken `## Goal & Why`, `## Bounds`, or `## Done-check` locked on `main` (`keel branch verify` injects `main`'s locked version directly into the verifier brief).
- **Out-of-scope discoveries:** Workers report unrelated bugs in their completion `notes` so the orchestrator can open a separate `docs/tasks/todo/<YYYYMMDD>-<slug>.md` on `main`.
- **Lifecycle closure:** `keel branch land --execute` atomically moves `docs/tasks/active/<task>.md` into `docs/tasks/archive/<task>.md` (`--discard --reason "..."` appends `## Outcome: [DISCARDED]` and archives on `main`).

## 3. Architecture & costly-to-reverse decisions (`docs/`)
- Optional `docs/architecture.md`: Concise component map and extension guide when a codebase grows beyond a few modules.
- Optional `docs/adr/NNNN-<slug>.md`: Reserve strictly for costly-to-reverse architectural choices (context, neutral comparison of viable options with pros/cons/risks/carry costs, and chosen decision). Never commit `SUMMARY.md` or `HANDOFF.md` scratch files.

## 4. Release-time versioning & safe deployments
- Keep ordinary feature/fix branches free of global version bumps (`plugin.json`, `pyproject.toml`, `package.json`) so parallel worktrees never collide.
- Cut releases and run live deployments **only** with explicit human authorization (or documented standing delegation), preferring immutable versioned artifacts, atomic cutover, post-cutover health checks, and immediate rollback readiness.
