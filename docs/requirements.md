# Keel — First-Principles Requirements & Engineering Principles

Layer 1 defines the outcomes a rebuild must preserve. Layer 2 records how Keel should deliver them with the current generation of executors.

## Layer 1: Pure Requirements (Timeless Human & Outcome Needs)

### Core Must-Haves (`R1`–`R5`)

1. **`R1` — Preserve native model capability.** Match reasoning capability and effort to the difficulty of the slice. A difficult change must have access to the model's full native capability. Underpowered assignments cost rework rounds, while maximum effort on routine tasks wastes time and capacity. Keep process instructions and tool restrictions light enough for the selected model to exercise its own judgment.

2. **`R2` — Preserve the project's Core need across contexts.** Both implementers and independent verifiers receive the project's `## Core need`, the slice's purpose, and its `main`-locked goal, bounds, and done criteria when a task specification exists. This context must survive compaction and workspace isolation. Exclude the author's chat and private reasoning from verification while preserving the project purpose and task bounds. Every agent should flag a local fix that would undermine the global goal.

3. **`R3` — Deliver root-cause fixes that withstand independent verification.** Before integration, require passing deterministic checks and an independent, clean-context review that exercises realistic caller workflows against the Core need and agreed task bounds. Verify that the checks detect a meaningful break. Masking symptoms, weakening valid assertions, swallowing errors, silently degrading behavior, or patching private third-party internals to force a pass requires correction. Avoid contrived OS or kernel fault injections outside the slice's realistic fault model. On rejection, the Orchestrator checks the finding against the caller's workflow and task criteria, diagnoses the root cause, then directs a fix for a confirmed defect or explains why the finding falls outside the slice. Bound review rounds and escalate unresolved conflicts.

4. **`R4` — Protect scarce human attention.** Clear, reversible tasks proceed end to end without prompt restatement or repeated permission gates. Interrupt the human for direction-changing ambiguity, genuine product or architectural tradeoffs, or irreversible actions and actions with external impact. Present viable tradeoffs neutrally and give the recommendation last. Landing, releasing, deploying, discarding unintegrated work, force-pushing shared refs, and mutating external state require explicit authorization unless standing delegation covers the action and target.

5. **`R5` — Support task-matched dispatch across pluggable executors.** Route research, prototype comparisons, debugging, implementation, and verification to a capable model on any suitable executor (`1..N`). Match each assignment to the slice's actual difficulty as models and runtimes evolve. Concurrent writers need isolated workspaces, and every dispatched agent needs the project and task context required for its assignment.

### Detailed Needs (`D1`–`D3`)

- **`D1` — Size the work proportionally.** Execute a small, single-slice change directly. Before dispatching a multi-module overhaul, divide it into small, sequential slices with independently checkable outcomes. Existing repositories, branches without task files, and offline or SSH-remote workflows must work without mandatory ticket templates, hosted pull requests, or custom CI job names.
- **`D2` — Keep casual chat free of workflow overhead.** Non-engineering conversations incur near-zero token overhead and zero workflow prompts.
- **`D3` — Keep the harness thin and rebuildable.** Separate durable requirements, configuration, and execution glue by responsibility so each can be changed or rebuilt independently. Maintaining the glue must never eclipse the project work it supports.

### Explicit Outcome Red Lines (`NR1`–`NR4`)

- **`NR1` — No process bureaucracy.** Avoid ceremonial templates, permission gates on clear reversible tasks, and multi-agent committees on atomic edits. Judge simplicity by how clearly responsibilities are divided, and avoid arbitrary upfront line-count quotas or edits made solely to hit a line count.
- **`NR2` — No false-positive developer friction.** Guards must block unauthorized default-branch changes and destructive force-pushes while allowing routine operations such as tests, filenames like `src/merge.py`, `git pull --ff-only`, and normal `main` pushes.
- **`NR3` — No repo pollution or platform lock-in.** Keep scratch handoff files and verification state out of version control. Local branch workflows must work independently of hosted platform APIs.
- **`NR4` — No brittle couplings.** Use public host contracts and structured configuration. Never parse prose tables as configuration or scrape undocumented host directories.

## Layer 2: Late-2026 Frontier Engineering Principles (`P1`–`P5`)

1. **`P1` — Keep the daily Orchestrator loop resident.** In `rules/AGENTS.md`, §§1–3 apply to every agent. Section 4 supplies the daily Orchestrator loop for the main session only. Of these sections, `context.py` injects only §§1–3 into worker briefs delivered by file or stdin. Keep infrequent procedures in a single on-demand `keel-repo-ops` skill. Each project's `AGENTS.md` is a concise map to its Core need, commands, gotchas, and task-specific reading. Select entries by their value to someone working in the repo, without a fixed line ceiling.

2. **`P2` — Anchor the task ledger on `main`.** Open non-trivial tasks in `docs/tasks/{todo,active,archive}/<YYYYMMDD>-<slug>.md` on `main` before branching. The `main` versions of `Goal & Why`, `Bounds`, and `Done-check` are immutable for the slice. Workers may update `Progress` and `Decisions & Findings` on the branch. Verify against the locked specification.

3. **`P3` — Configure model levels once and dispatch synchronously.** Define the L1–L4 capability ladder and each executor's model and effort mapping in `config/defaults.json`. Choose a level for the slice independently of executor, read/write mode, and context preset. Run both `codex` and `agy -p` synchronously and collect the result on exit. Omit `--new-project` from `agy -p` so headless runs reuse the CLI Project without adding projects to the UI sidebar. Isolate concurrent writers in task worktrees and supply the project and task context even when an executor starts in a disposable clone.

4. **`P4` — Verify independently at the implementation level.** Default to a same-level cross-vendor diagonal. An `agy` implementation at a given level goes to a `codex` verifier at that level, and a `codex` implementation goes to `agy` at the same level. In a clean disposable clone, verify the locked criteria through realistic caller workflows proportional to the change. Make a one-line Canary mutation, confirm the relevant check fails, and restore the clone. Validate the verdict against `config/schemas/verdict.schema.json`, keeping must-fix findings in `blocking` and optional findings in `notes`. After a rejection, diagnose the cause and step up the model level for rework when a higher level is available. Escalate unresolved blockers after two review rounds. Carry verification across a pure rebase only when the rebased Git tree SHA matches the expected tree from applying the verified change to the new base.

5. **`P5` — Keep the branch gate local and flat.** Store each branch's verification and landing state in `<git_common_dir>/keel-state/branches/`. The gate records the synchronous verifier result directly, without asynchronous pending-verifier claim tables or a manual record-verdict step. Bundle task archival into the atomic squash land. Keep `PreToolUse` guards slim by checking parsed command segments for prohibited operations, so legitimate commands and filenames pass.
