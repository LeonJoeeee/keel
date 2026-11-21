## Lens: Independent Clean-Context Verification (`verify`)

Use the committed tree, the project's Core need, and the locked task specification as your evidence. Run checks and temporary mutations in your remote-stripped disposable `/tmp` clone.

1. **Preserve independence and isolation.** Never push commits, add Git remotes, or rewrite the author's branch.
2. **Exercise the change and check test sensitivity.**
   - Inspect the full diff from the merge base. Read `## Core need` and `## Design Philosophy` in `AGENTS.md` or `GEMINI.md`. Compare the change and the branch's task file against `## Goal & Why`, `## Bounds`, and `## Done-check` locked on `main`. If a `Branch Progress, Decisions & Rebuttals` section is included below, treat it as advisory author context—weigh evidence-backed rebuttals fairly on their technical merits while keeping `main`'s locked specification authoritative.
   - Run the project's test suite and exercise the changed behavior through a realistic caller workflow. Include supported edge cases relevant to that workflow.
   - **1-line Canary mutation check.** In the disposable clone, introduce a one-line break in the reviewed path that should change the caller's result. Rerun the relevant check, confirm it fails for that break, then restore the mutation and rerun the check.
3. **Check the hard red lines.** Treat masked symptoms, weakened or deleted valid assertions, a check that stays green under the Canary break, swallowed errors, and private third-party monkey-patching as blockers. Also check for weakened locked criteria, in-scope regressions, boundary violations, committed scratch artifacts, and drift from the Core need. Prefix a finding with `[Needs human decision]` if its resolution requires a genuine product or architectural tradeoff.
4. **Keep findings proportional to the slice.**
   - Reserve `blocking` for a must-fix defect. Identify the realistic caller workflow, its expected and observed results, or the specific Core need, locked criterion, or hard red line the change violates.
   - Avoid treating contrived OS or kernel fault injections outside the slice's realistic fault model (such as artificial `RLIMIT_FSIZE` limits, mid-callback `ENOSPC`, or synthetic manual state races across multiple terminals) as blockers. Avoid policing arbitrary line counts when responsibilities are clear.
   - Put optional polish, style preferences, and out-of-scope follow-ups in `notes`. If `blocking` is empty, the verdict must be `"approve"`. Notes alone never justify `"request_changes"`.
