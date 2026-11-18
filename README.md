# Keel

Keel lets one developer direct parallel agents across Google Antigravity and OpenAI Codex CLI. Workers get the project's Core need, a bounded task, and an isolated Git worktree. Before a branch lands, an independent verifier exercises the change in a disposable clone. Task history and verification state stay local to the repository.

The main session uses `rules/AGENTS.md` §4 to size work, choose an intensity level, and triage review findings. Standalone worker briefs receive §§1–3. The on-demand `keel-repo-ops` skill covers repository setup, task templates, ADRs, releases, and deployments.

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
keel agent run --level L2 --executor agy --mode write --preset implement \
  --worktree parser-fix --why "Keep existing project files readable" \
  --task "Fix the parser's handling of empty sections and run its regression tests."
```

`keel worktree list` shows active worktrees. Use `--task-file <file>` for a written assignment. Continue a resumable run with `keel agent resume <run_id> --message-file <file>` and optionally select a new `--level`.

## Verify and land

Verify the clean, committed branch with its deterministic checks. With a recorded write, Keel selects the other vendor at the write level or L2, whichever is higher. Use `--dual` to run both vendors concurrently. Each verifier gets a separate clone and exercises a realistic workflow, including a one-line Canary mutation to check that the tests detect a break. The gate records the verdict on return.

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
