---
name: keel-repo-ops
description: >-
  Use when founding or adopting a repository, authoring or updating a repo's root AGENTS.md (six-section project map with user-oriented Core need and Design Philosophy), documenting detailed Design Philosophy in docs/requirements.md or docs/prd.md, managing the zero-context docs/tasks/ ledger on main, recording Architecture Decision Records (ADRs), or running safe releases and deployments.
---

# Keel Repo Ops — Repository Map, Design Philosophy, Task Ledger, ADRs & Releases

## 1. Root `AGENTS.md` & Dual-Tier Design Philosophy

Keep the project map concise enough for every main session and worker to use (`R1`, `R2`, `NR1`). Keep entries that change how someone works in the repository. Remove stale guidance, move detailed procedures to `docs/`, and omit inventories that code search already provides.

Use these **six sections in order** in root `AGENTS.md`:

1. **`## Core need` (strictly user-oriented):** Describe **who the human user is, what they want to accomplish, and where their real pain points lie**. Never write feature checklists, framework/architecture bragging, or developer self-indulgence ("技术自嗨").
2. **`## Design Philosophy` (concise in `AGENTS.md`, detailed in `docs/requirements.md` / `docs/prd.md`):** State 3–5 crisp tradeoff beliefs bridging `Core need` to concrete engineering choices—what the project prioritizes and what temptations it explicitly rejects (zero generic platitudes such as "write clean code"). Expand the full rationale in a dedicated `## Design Philosophy` section in `docs/requirements.md` (or `docs/prd.md`).
3. **`## Architecture & Boundaries`:** High-level module ownership, runtime boundaries, and hard invariants.
4. **`## Commands`:** Verified build, lint, test, and validation commands.
5. **`## Gotchas`:** Focus on real, non-obvious traps hit in practice rather than speculative boilerplate.
6. **`## Where to look`:** Prefer **conditional routing** (`"When doing X -> read Y"`) over mandatory upfront pre-reads on every turn (including routing philosophy/requirement lookups to `docs/requirements.md` or `docs/prd.md`).

### Bad vs Good `## Core need`

- **Bad (architecture bragging / developer self-indulgence):**
  > A modular Python 3.12 async ingestion pipeline using Pydantic v2, Redis streams, and pluggable worker adapters with 100% typed interfaces and zero external ORM overhead.
- **Good (human user, goal, and concrete pain points):**
  > Small-team analysts reconcile bank and invoice CSVs every Monday morning and cannot afford silent row drops, misparsed dates, or duplicate ledger entries; they need one command that turns messy vendor exports into an auditable reconciliation report and fails loudly whenever totals do not balance.

## 2. Repo-native `docs/tasks/` ledger (`main`-anchored, single zero-context handoff)

Replace external issue trackers with `docs/tasks/{todo,active,archive}/<YYYYMMDD>-<slug>.md` for non-trivial work. Reserve direct main-session edits for trivial, few-line changes, and dispatch substantive slices to a clean-context writer (`D1`).

- **Single authoritative handoff contract:** Workers and verifiers start in fresh processes with zero parent chat history. Write **thorough, self-contained specifications** proportional to slice complexity—with **no sentence or line ceiling**—including exact file bounds, edge cases, invariants, caller workflows, and concrete done-checks.
- **No duplicated `--why`:** When `--task-file` supplies a non-empty `## Goal & Why` (or `## Why`) section, `--why` is optional on both `keel agent run` and `keel branch verify` (Keel extracts it automatically). Pass supplementary rework or pivot notes via `--context-file` instead of mutating the `main`-locked task spec.
- **Orchestrator owns opening on `main`:** Open and commit task specs in `docs/tasks/todo/` or `docs/tasks/active/` directly on `main` **before** branching `.agents/worktrees/<slug>` (`hooks/pre-tool-use` permits `git commit` on `main` iff every touched path is under `docs/tasks/`).
- **Spec structure:**
  - Core locked sections (immutable on the branch): `## Goal & Why`, `## Bounds`, `## Done-check`.
  - Branch-updated sections: `## Progress` and `## Decisions & Findings`.
  - **Evidence-backed review pushback (`### Review Rebuttals`):** Inside `## Decisions & Findings`, add a `### Review Rebuttals` subsection when pushing back on a disputed, false-positive, or out-of-scope review finding—citing concrete file lines, test outputs, or locked bounds. `keel branch verify` surfaces changed `Progress`, `Decisions & Findings`, and `Review Rebuttals` sections in a clearly labeled advisory block while keeping `main`'s locked `Goal & Why`, `Bounds`, and `Done-check` authoritative.
- **Immutability boundary:** Subagent workers on a branch may update `## Progress` and `## Decisions & Findings`, but must **never** weaken `## Goal & Why`, `## Bounds`, or `## Done-check` locked on `main` (`keel branch verify` injects `main`'s locked version directly into the verifier brief).
- **Out-of-scope discoveries:** Workers report unrelated bugs in their completion `notes` so the orchestrator can open a separate `docs/tasks/todo/<YYYYMMDD>-<slug>.md` on `main`.
- **Lifecycle closure:** The default squash land (`keel branch land <slug> --execute`) atomically moves `docs/tasks/active/<task>.md` into `docs/tasks/archive/<task>.md` (`--discard --reason "..."` appends `## Outcome: [DISCARDED]` and archives on `main`).

### Bad vs Good Task Spec (`docs/tasks/active/<YYYYMMDD>-<slug>.md`)

- **Bad (terse, relies on hidden chat memory):**
  > ## Goal & Why
  > Fix CSV dates.
  > ## Bounds
  > parser.py
  > ## Done-check
  > Tests pass.
- **Good (self-contained zero-context handoff):**
  > ## Goal & Why
  > Analysts importing European vendor exports (`DD/MM/YYYY`) currently get silent month/day swaps when day <= 12, corrupting monthly close totals. Make date parsing explicit about format selection and fail loudly on ambiguous dates instead of guessing.
  > ## Bounds
  > - In scope: `src/csv_parser.py` (explicit `--date-format` / per-vendor config lookup; raise `AmbiguousDateError` on mixed or unrecognized formats), `tests/test_csv_parser.py` (unit + fixture regression tests for ISO, US, EU, and mixed-format rejection).
  > - Out of scope: Currency conversion and downstream PDF export formatting.
  > ## Done-check
  > 1. `python3 -m unittest tests/test_csv_parser.py -v` passes with regression coverage for `05/03/2026` under explicit `DMY` vs `MDY` and loud failure when format is unspecified and ambiguous.
  > 2. `git status --porcelain -uall` is clean on the final committed worktree state.

## 3. Requirements, Design Philosophy & costly-to-reverse decisions (`docs/`)
- `docs/requirements.md` (or `docs/prd.md`): Home for the **detailed `## Design Philosophy`** (expanding root `AGENTS.md`'s concise bullets and showing how they bridge the user's `Core need` to concrete tradeoffs) plus durable user/outcome requirements.
- Optional `docs/architecture.md`: Concise component map and extension guide when a codebase grows beyond a few modules.
- Optional `docs/adr/NNNN-<slug>.md`: Reserve strictly for costly-to-reverse architectural choices (context, neutral comparison of viable options with pros/cons/risks/carry costs, and chosen decision). Never commit `SUMMARY.md` or `HANDOFF.md` scratch files.

## 4. Release-time versioning & safe deployments
- Keep ordinary feature/fix branches free of global version bumps (`plugin.json`, `pyproject.toml`, `package.json`) so parallel worktrees never collide.
- Cut releases and run live deployments **only** with explicit human authorization (or documented standing delegation), preferring immutable versioned artifacts, atomic cutover, post-cutover health checks, and immediate rollback readiness.
