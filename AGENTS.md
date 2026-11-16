# Keel — Repository Context

## Core need

Keel enables one developer to direct parallel subagents across pluggable executors (Google Antigravity, OpenAI Codex CLI, and future `1..N` backends) at full native frontier-model intelligence (`R1`) without losing global project alignment (`R2`) across context compactions or isolated task slices. Every workflow exists to keep human attention focused strictly on first-principles core needs, neutral product/architectural tradeoff decisions, and explicit authorization of irreversible actions (`R4`), while ensuring integrated changes reflect honest root-cause solutions verified in an independent clean context rather than masked symptoms or fake-green checks (`R3`).

To preserve zero-drag model reasoning and effortless rebuildability as frontier models evolve (`D3`), Keel keeps durable requirements (`docs/requirements.md`) separated from radically thin Python 3 stdlib implementation glue (`scripts/`, `hooks/`, `config/`), relying on native Git/GitHub truth and public CLI/hook contracts rather than external dependencies, custom GitHub comment state machines, or undocumented host internals.

## Commands

```sh
# Run full unit and git-integration suite (zero bytecode pollution, warnings enabled)
PYTHONDONTWRITEBYTECODE=1 python3 -W default -m unittest discover -s tests -v

# Verify per-entry plugin symlinks and strict Antigravity plugin validation
./scripts/install-plugin --check
agy plugin validate --strict ~/.gemini/config/plugins/keel
```

## Gotchas

- **Root `AGENTS.md` vs `rules/AGENTS.md`:** Root `AGENTS.md` is repository context for maintaining `keel` itself; `rules/AGENTS.md` is the ~24-line universal engineering spine injected into `<user_rules>` across all projects when the plugin is enabled. Because `agy plugin validate --strict` flags a root `AGENTS.md` inside an installed plugin directory as `never loaded`, always install `~/.gemini/config/plugins/keel` via `./scripts/install-plugin` (which creates per-entry symlinks excluding root `AGENTS.md`, `docs/`, and `tests/`) rather than a top-level directory symlink.
- **Dual-protocol `PreToolUse` hook:** `hooks/pre-tool-use` handles both Antigravity (`toolCall` camelCase) and Codex CLI (`tool_name` / `tool_input` snake_case) events on `stdin`; keep it fast, stdlib-only, and free of false positives on ordinary commands or filenames.
- **Release-only versioning:** Keep ordinary feature and fix PRs free of `plugin.json` version bumps so parallel worktrees never collide; bump versions and cut tags only on explicitly authorized releases.
