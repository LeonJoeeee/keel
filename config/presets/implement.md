## Lens: Implementation (`implement`)

1. **Global alignment before coding:** Check `AGENTS.md` / `GEMINI.md` (`## Core need`, commands, gotchas) and `Why` above before designing your change. If satisfying the local task within `Scope` conflicts with the global `Core need` or requires out-of-bounds edits, stop and escalate rather than shipping a local-optimum workaround.
2. **No symptom-masking or fake green:**
   - Trace what existing behavior and tests protect before changing them.
   - Never weaken, delete, or skip valid assertions, swallow errors, or silently degrade functionality to make checks pass.
   - Use public APIs of third-party packages; never monkey-patch private library internals without explicit human consent.
3. **Clean worktree & task-ledger hygiene:**
   - Work strictly inside the assigned `.agents/worktrees/<slug>` workspace and allowed `Scope`; never edit files in the primary checkout or push to git remotes.
   - You may update `## Progress` and `## Decisions & Findings` in the branch's active task file (`docs/tasks/active/`), but never weaken `## Goal & Why`, `## Bounds`, or `## Done-check` locked on `main`. Report out-of-scope bugs in your completion notes for the orchestrator to open in `docs/tasks/todo/`.
   - Do not bump release version manifests on ordinary feature/fix branches unless explicitly instructed, and never commit scratch files (`SUMMARY.md`, `HANDOFF.md`, temporary scripts) or secrets.
4. **Final-state verification:** Run the `Done` verification commands on the final committed state, confirm `git status --porcelain -uall` is completely clean, and report commit SHA(s) plus exact verification results.
