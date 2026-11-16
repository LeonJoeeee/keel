# Keel — First-Principles Requirements & Engineering Principles

> **Purpose of this document:** Keep durable **requirements (What & Why)** cleanly separated from **model-era engineering practices (How)**. When frontier models leap forward every 6–12 months, rebuild the thin implementation glue from this baseline rather than accreting patches.

---

## Layer 1: Pure Requirements (Timeless Human & Outcome Needs)

### Core Must-Haves (`R1`–`R5`)

1. **`R1` — Zero Model Capability Degradation (Peak Intelligence First)**
   - **Outcome:** Agents operating with Keel must solve complex engineering problems at the full native ceiling of the chosen frontier model — never slower, duller, or more rigid than running bare-metal.
   - **Rationale:** Context attention is finite. Verbose process rules, artificial step-by-step thinking scripts, or false-positive tool blocks tax model IQ directly and turn a productivity tool into a net negative.

2. **`R2` — Persistent Global Perspective (`Core need` over Local Optima)**
   - **Outcome:** Across long multi-compaction main sessions and isolated subagent slices alike, every agent knows *what fundamental problem the project solves* (`## Core need`) and *how the current slice serves it* before proposing or writing solutions — speaking up or halting when a surface ask or local fix harms the global goal.
   - **Rationale:** Context compaction and task slicing naturally narrow an agent's horizon into local-optimum fixes that pass narrow checks while eroding overall architecture.

3. **`R3` — Trustworthy Delivery: Zero Fake Green + Independent Verification**
   - **Outcome:**
     - Never mask symptoms, weaken/delete valid assertions, swallow errors, silently degrade behavior, or monkey-patch private third-party internals to force green checks; escalate genuine conflicts honestly.
     - Changes integrating into the default branch pass both deterministic automated checks and an independent, clean-context active verification (`blocking` defects vs non-blocking `notes`), bounded by a 2-round convergence circuit breaker.
   - **Rationale:** One human directing parallel agents cannot manually audit every intermediate line. Parallel delegation works only if "green + approved" reliably means the real need is met without endless review nitpick loops.

4. **`R4` — Protect Scarce Human Attention (`Clarity × Blast Radius`)**
   - **Outcome:**
     - Clear, reversible tasks proceed end-to-end without prompt parroting or repetitive permission gates.
     - Interrupt the human strictly on: (a) direction-changing ambiguity, (b) genuine product/architecture tradeoffs (presenting viable options neutrally first, recommendation strictly last), and (c) irreversible or external-blast-radius actions (merging to default branch, releasing/deploying, deleting unintegrated work, mutating external state — unless explicitly delegated).
   - **Rationale:** Routine confirmation prompts train humans into 90%+ reflexive rubber-stamping (approval fatigue), eroding vigilance where irreversible mistakes actually hurt, while leading leans anchor human judgment prematurely.

5. **`R5` — Universal, Extensible Multi-Side Subagent Dispatch (`1..N` Executors)**
   - **Outcome:** Dispatch any independent task (research, prototype comparison, debugging, implementation, or verification) across pluggable executors — currently **built-in Google Antigravity** and **OpenAI Codex CLI**, extensible to future 3rd/4th backends — with physical workspace isolation for concurrent writers.
   - **Rationale:** Real engineering spans far more than rigid ticket-coding pipelines, and model/runtime leadership shifts continuously.

### Detailed Needs (`D1`–`D3`)

- **`D1` — Proportional Friction & Zero-Edit Repo Adoption:** 2-line fixes, Dependabot PRs, issue-less branches, and existing standard-CI repositories work out of the box without mandatory ticket templates or custom CI job names.
- **`D2` — Zero Casual-Chat Interference:** Non-engineering conversations incur near-zero token overhead and zero workflow nagging.
- **`D3` — Rebuildable Thinness:** Keep requirements (`docs/requirements.md`), configuration (`config/`), and thin stdlib glue (`scripts/`) cleanly separated so periodic clean-slate rebuilds stay effortless.

### Explicit Outcome Red Lines (`NR1`–`NR4`)

- **`NR1` — No Process Bureaucracy:** No ceremonial restatement templates, no stop-for-permission gates on already-clear reversible tasks, and no multi-agent committees on atomic edits.
- **`NR2` — No False-Positive Developer Friction:** Safety guards block genuine unauthorized merges/pushes to default branches without ever misfiring on filenames (`src/merge.py`), test runs, or chained branch pushes.
- **`NR3` — No Repo/PR Pollution or Single-Machine Lock-In:** Never commit scratch handoff files (`SUMMARY.md`/`HANDOFF.md`), never spam hidden machine-JSON state comments onto Issues/PRs, and never lock PR workflows to one machine's local filesystem paths.
- **`NR4` — No Brittle Couplings:** Never regex-parse prose tables as config, reject valid reviews over punctuation differences, or scrape undocumented host private directories.

---

## Layer 2: Late-2026 Frontier Engineering Principles (`P1`–`P5`)

1. **`P1` — Lean Resident Spine + Progressive Disclosure + File-Pointer Handoff:** Keep always-on rules (`rules/AGENTS.md`) under ~50 lines; load procedural runbooks via on-demand `skills/`; pass subagent briefs via disk files / `stdin` rather than main-session prompt echoing.
2. **`P2` — Internalize Global Purpose, Bound Local Scope, Drop Cognitive Scripts:** Reason against `Core need` internally and surface discrepancies conditionally; pair global `Why` with explicit local `Scope`; let native reasoning models think without rigid step checklists.
3. **`P3` — Orthogonal Dispatch (`Executor × Mode × Context Preset`):** Separate pluggable `ExecutorAdapter` implementations (`codex`, `agy`, ...) from `read`/`write` workspace isolation (`.agents/worktrees/<slug>` and disposable `--no-local` clones) and thin Markdown presets (`general`, `implement`, `verify`).
4. **`P4` — Schema-Validated Clean-Context Verification & Mathematical Rebase Proof:** Enforce structured `verdict.schema.json` output on independent verifiers, post at most one concise human-readable PR review summary per round, cap fix loops at 2 rounds, and prove pure rebases via git `tree SHA` equivalence.
5. **`P5` — Native Git/GitHub Truth & Command-Segment Guardrails:** Read branch/worktree/check state directly from Git and GitHub APIs (tolerating path-filtered `skipped`/`neutral` checks), and enforce `PreToolUse` boundaries per parsed command segment using public interfaces only.
