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
     - Changes integrating into the default branch pass both deterministic automated checks and an independent, clean-context active verification (`blocking` defects vs non-blocking `notes`, checking against `main`-locked task bounds and Canary test-sensitivity probes), bounded by a 2-round convergence circuit breaker.
   - **Rationale:** One human directing parallel agents cannot manually audit every intermediate line. Parallel delegation works only if "green + approved" reliably means the real need is met without endless review nitpick loops.

4. **`R4` — Protect Scarce Human Attention (`Clarity × Blast Radius`)**
   - **Outcome:**
     - Clear, reversible tasks proceed end-to-end without prompt parroting or repetitive permission gates.
     - Interrupt the human strictly on: (a) direction-changing ambiguity, (b) genuine product/architecture tradeoffs (presenting viable options neutrally first, recommendation strictly last), and (c) irreversible or external-blast-radius actions (landing onto the default branch, releasing/deploying, discarding unintegrated work, force-pushing shared refs, mutating external state — unless explicitly delegated).
   - **Rationale:** Routine confirmation prompts train humans into 90%+ reflexive rubber-stamping (approval fatigue), eroding vigilance where irreversible mistakes actually hurt, while leading leans anchor human judgment prematurely.

5. **`R5` — Universal, Extensible Multi-Side Subagent Dispatch (`1..N` Executors)**
   - **Outcome:** Dispatch any independent task (research, prototype comparison, debugging, implementation, or verification) across pluggable executors — currently **built-in Google Antigravity** and **OpenAI Codex CLI**, extensible to future 3rd/4th backends — with physical workspace isolation for concurrent writers and gap-free context registration.
   - **Rationale:** Real engineering spans far more than rigid ticket-coding pipelines, and model/runtime leadership shifts continuously.

### Detailed Needs (`D1`–`D3`)

- **`D1` — Proportional Friction & Zero-Edit Repo Adoption:** 2-line fixes, task-file-less branches, offline/SSH-remote repos (`origin`, `th`), and existing repositories work out of the box without mandatory ticket templates, GitHub Web PRs, or custom CI job names.
- **`D2` — Zero Casual-Chat Interference:** Non-engineering conversations incur near-zero token overhead and zero workflow nagging.
- **`D3` — Rebuildable Thinness:** Keep requirements (`docs/requirements.md`), configuration (`config/`), and thin stdlib glue (`scripts/`) cleanly separated so periodic clean-slate rebuilds stay effortless.

### Explicit Outcome Red Lines (`NR1`–`NR4`)

- **`NR1` — No Process Bureaucracy:** No ceremonial restatement templates, no stop-for-permission gates on already-clear reversible tasks, and no multi-agent committees on atomic edits.
- **`NR2` — No False-Positive Developer Friction:** Safety guards block genuine unauthorized merges/commits on default branches and destructive force-pushes without ever misfiring on filenames (`src/merge.py`), test runs, `git pull --ff-only`, or normal `main` pushes.
- **`NR3` — No Repo Pollution or Platform Lock-In:** Never commit scratch handoff files (`SUMMARY.md`/`HANDOFF.md`), never couple local branch workflows to GitHub Web APIs, and keep verification state untracked inside `.git/keel-state/`.
- **`NR4` — No Brittle Couplings:** Never regex-parse prose tables as config or scrape undocumented host private directories.

---

## Layer 2: Late-2026 Frontier Engineering Principles (`P1`–`P5`)

1. **`P1` — Two-Layer `AGENTS.md` + Progressive Disclosure + File-Pointer Handoff:** Pair a ~25-line universal plugin spine (`rules/AGENTS.md`) with a `<100`-line 5-section project map (`<repo>/AGENTS.md` using strictly conditional `"When doing X -> read Y"` routing); load procedural runbooks via on-demand `skills/`; pass subagent briefs via disk files / `stdin` rather than main-session prompt echoing.
2. **`P2` — `main`-Anchored Task Ledger & Immutable Bounds:** Track non-trivial tasks in `docs/tasks/{todo,active,archive}/<YYYYMMDD>-<slug>.md` opened on `main` before branching, allowing branch workers to update `Progress` / `Decisions & Findings` while locking `Goal & Why`, `Bounds`, and `Done-check` on `main`.
3. **`P3` — Orthogonal Dispatch (`Executor × Mode × Context Preset`) & Gap-Free Registration:** Separate pluggable `ExecutorAdapter` implementations (`codex` at `gpt-6-astra @ max`, `agy`, ...) from `read`/`write` workspace isolation (`.agents/worktrees/<slug>`, Codex `-s read-only` vs remote-stripped `/tmp` verify clones) and auto-inject missing spine/project context across cross-directory and `/tmp` runs.
4. **`P4` — Schema-Validated Clean-Context Verification, Canary Probes & Mathematical Rebase Proof:** Enforce structured `verdict.schema.json` output on independent verifiers (single cross-model `codex` or `--dual` `codex + agy`), require 1-line Canary mutation checks against fake-green tests, cap `request_changes` loops at 2 rounds, and prove pure rebases via git `tree SHA` equivalence.
5. **`P5` — Pure Local Git Truth & Slim Command-Segment Guardrails:** Store branch verification/land state in `<git_common_dir>/keel-state/branches/`, bundle `active -> archive` task closure into atomic `--squash` lands, and enforce low-friction `PreToolUse` boundaries per parsed command segment.
