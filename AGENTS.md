# Keel — Repository Context

## Core need

Keel enables one developer to direct parallel subagents across pluggable executors (Google Antigravity, OpenAI Codex CLI at `gpt-6-astra @ max`, and future `1..N` backends) at full native frontier-model intelligence (`R1`) without losing global project alignment (`R2`) across context compactions or isolated worktrees. Every workflow keeps human attention strictly on first-principles core needs, neutral product/architectural tradeoff decisions, and explicit authorization of irreversible actions (`R4`), while ensuring integrated changes reflect honest root-cause solutions verified actively in a clean disposable clone (`R3`).

To preserve zero-drag model reasoning and effortless rebuildability (`D3`), Keel separates durable requirements (`docs/requirements.md`) from thin Python 3 stdlib glue (`scripts/`, `hooks/`, `config/`), relying on pure local Git truth (`docs/tasks/` ledger + `.git/keel-state/`) and public CLI/hook contracts rather than external packages, GitHub Web PR coupling, or undocumented host internals.

## Architecture & Boundaries

- **`rules/AGENTS.md` + `skills/`:** ~24-line always-on universal spine (`<user_rules>`) paired with progressive-disclosure skills (`keel-repo-ops`, `keel-dispatch`, `keel-integrate`).
- **`scripts/keel` + `scripts/keel_lib/`:** Pure-stdlib CLI (`worktree`, `agent`, `branch`) backed by `env.py`, `worktree.py`, `context.py`, `executors/{base,agy,codex}.py`, `guard_rules.py`, and `branch_gate.py` (storing verification/land state in untracked `<git_common_dir>/keel-state/branches/`).
- **`hooks/pre-tool-use`:** Dual-protocol (`Antigravity` camelCase + `Codex` snake_case) pure-Git command-segment seatbelt blocking subagent pushes/lands, destructive force pushes, and non-`docs/tasks/` direct mutations on `main`/`master`.

## Commands

```sh
# Run full unit and real-git integration suite (zero bytecode pollution, warnings enabled)
PYTHONDONTWRITEBYTECODE=1 python3 -W default -m unittest discover -s tests -v

# Verify per-entry plugin symlinks and strict Antigravity plugin validation
./scripts/install-plugin --check
~/.local/bin/agy plugin validate --strict ~/.gemini/config/plugins/keel
```

## Gotchas

- **Root `AGENTS.md` vs `rules/AGENTS.md`:** Root `AGENTS.md` is repo context for maintaining `keel`; `rules/AGENTS.md` is the universal spine loaded across all projects. Always install `~/.gemini/config/plugins/keel` via `./scripts/install-plugin` (per-entry symlinks excluding root `AGENTS.md`, `docs/`, `tests/`) so `agy plugin validate --strict` never flags root `AGENTS.md` as `never loaded`.
- **Slim `guard_rules.py` ceiling:** Keep `guard_rules.py` concise (`~450–650` lines) around `extract_command_segments` and direct subcommand/branch checks, free of false positives on filenames or `git pull --ff-only` / normal `main` pushes.
- **Release-only versioning:** Never bump `plugin.json` on ordinary feature/fix branches; bump versions and cut tags only on explicitly authorized releases.

## Where to look

- When checking first-principles invariants (`R1`–`R5`, `D1`–`D3`, `NR1`–`NR4`) -> read `docs/requirements.md`.
- When adding a 3rd/4th executor adapter or inspecting module boundaries -> read `docs/architecture.md` and `scripts/keel_lib/executors/base.py`.
- When modifying branch verification, rebase tree-equivalence proofs, or landing/archiving -> read `scripts/keel_lib/branch_gate.py`.
- When modifying `PreToolUse` command guards or subagent mode binding -> read `hooks/pre-tool-use` and `scripts/keel_lib/guard_rules.py`.
