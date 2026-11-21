# Keel

Keel lets one developer or researcher direct parallel coding agents across Google Antigravity and OpenAI Codex CLI without babysitting turns, losing the project's purpose across compactions and worktrees, or merging fake-green code onto `main`. Every worker and independent verifier receives the repository's user-oriented Core need, Design Philosophy, and a single zero-context task specification in an isolated Git worktree or disposable clone. Task history and verification state stay local to the repository.

The main session uses `rules/AGENTS.md` §4 to size work, choose an intensity level, and triage review findings. Standalone worker briefs receive §§1–3. The on-demand `keel-repo-ops` skill covers the six-section repository map, Design Philosophy, zero-context task specs, ADRs, releases, and deployments.

## Install the Antigravity plugin

The installer links plugin entries into `~/.gemini/config/plugins/keel` and the CLI into `~/.local/bin`.

```sh
./scripts/install-plugin
./scripts/install-plugin --check
~/.local/bin/agy plugin validate --strict ~/.gemini/config/plugins/keel
```

## Run a task

From the target repository, choose a level from L1 through L4 for the difficulty of the slice. Each executor's model and effort mapping lives in `config/defaults.json`. Both adapters return results on exit. Antigravity uses `agy -p` in the selected checkout and reuses its CLI Project.

```sh
keel worktree create parser-fix

# Single-document zero-context handoff (--why is extracted from `## Goal & Why` in the task file)
keel agent run --level L2 --executor agy --mode write --preset implement \
  --worktree parser-fix --task-file docs/tasks/active/20261121-parser-fix.md

# Lightweight task-file-free run
keel agent run --level L2 --executor agy --mode write --preset implement \
  --worktree parser-fix --why "Keep existing project files readable" \
  --task "Fix the parser's handling of empty sections and run its regression tests."
```

`keel worktree list` shows active worktrees. Use `--task-file <file>` for a thorough written specification (no `--why` needed when the spec contains `## Goal & Why` or `## Why`), and pass `--context-file <file>` for supplementary rework or pivot notes without mutating the `main`-locked spec. Continue a resumable run with `keel agent resume <run_id> --message-file <file>` and optionally select a new `--level`.

## Verify and land

Verify the clean, committed branch with its deterministic checks. With a recorded write, Keel selects the other vendor at the write level or L2, whichever is higher. Use `--dual` to run both vendors concurrently. Each verifier gets a separate clone and exercises a realistic workflow, including a one-line Canary mutation to check that the tests detect a break. The gate records the verdict on return, steps up rework and verification tiers after a rejection, and enforces a tier-aware convergence cap (`L2(1) -> L3(1) -> L4(2)`—at most 4 rounds from L2, 3 from L3, or 2 from L4).

```sh
keel branch verify parser-fix --check "python3 -m unittest discover -s tests"
keel branch status parser-fix
keel branch land parser-fix
```

The last command previews the default squash land. With authorization for the target branch, add `--execute` to land or use `--discard --reason "<why>"` to discard. After a pure rebase, use `branch compare` to inspect tree equivalence and pass the verified `--old-base` and `--old-head` to `branch land` with any prior checks. See the [core command reference](rules/AGENTS.md) for syntax.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -W default -m unittest discover -s tests -v
```

See the [requirements](docs/requirements.md) for project invariants and the [architecture guide](docs/architecture.md) for executor extension details.
