## Lens: General Subagent Execution

1. **Global alignment before action:** Check the project's `## Core need` (`AGENTS.md` / `GEMINI.md`) and the `Why` section above before forming conclusions or making edits. If solving this slice locally would undermine the broader project goal, stop and report the conflict clearly.
2. **Respect physical & scope boundaries:** Operate strictly within the stated `Mode` (`read` vs `write`) and `Scope`. In `read` mode, never modify repository files, git refs, or external remote state; use `/tmp` for any disposable probes.
3. **Evidence-backed output:** Verify claims directly against the codebase, tests, or primary sources, and return a concise, actionable result matching the `Done` criteria.
