"""Command-segment semantic guard for PreToolUse hooks (Antigravity + Codex CLI).

Tokenizes shell command segments accurately without regex false positives:
- Strips heredocs before line-continuation scanning so quoted heredoc terminators and `#` comments
  never swallow following lines via trailing backslashes; inspects `$(...)` and backticks inside unquoted heredocs.
- Supports standard `'...'`, `"..."`, ANSI-C `$'...'`, and locale `$"..."` quoting.
- Handles unspaced, noclobber, fd-dup, and `{fd}>` redirections (`merge>/tmp/out`, `pr&>/dev/null`, `>|`, `{fd}>`).
- Splits on shell compound keywords (`if/then/elif/else/fi`, `while/until/do/done`, `{`, `}`, `!`).
- Inspects explicit, DWIM (`heads/main`), wildcard, matching (`:` / `push.default=matching`), `-c` CLI overrides,
  and configured remote/upstream `git push` targets via fast `.git/HEAD` + config reads.
- Supports read-only `gh api -X GET -f k=v` and `gh api graphql` with query variables.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Sequence

DEFAULT_BRANCHES = frozenset({"main", "master"})
TRANSPARENT_WRAPPERS = frozenset({"command", "env", "nohup", "sudo", "time", "nice"})
SHELL_CONTROL_KEYWORDS = frozenset(
    {"if", "then", "elif", "else", "fi", "while", "until", "do", "done", "case", "esac", "{", "}", "!"}
)

GH_MUTATING_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "pr": frozenset({"create", "merge", "close", "reopen", "edit", "comment", "review", "ready", "lock", "unlock"}),
    "issue": frozenset({"create", "close", "reopen", "edit", "comment", "delete", "transfer", "pin", "unpin"}),
    "release": frozenset({"create", "delete", "edit", "upload"}),
    "repo": frozenset({"create", "delete", "edit", "archive", "rename", "fork"}),
}

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_BRACE_FD_RE = re.compile(r"^\{[A-Za-z_][A-Za-z0-9_]*\}$")


def _extract_substitutions_from_text(text: str) -> list[str]:
    """Extract `$(...)` and backtick command substitutions from unquoted heredoc bodies."""
    subs: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "$" and i + 1 < n and text[i + 1] == "(":
            depth = 1
            j = i + 2
            in_sq = False
            in_dq = False
            while j < n and depth > 0:
                c = text[j]
                if in_sq:
                    if c == "'":
                        in_sq = False
                elif in_dq:
                    if c == "\\" and j + 1 < n:
                        j += 2
                        continue
                    if c == '"':
                        in_dq = False
                else:
                    if c == "\\" and j + 1 < n:
                        j += 2
                        continue
                    if c == "'":
                        in_sq = True
                    elif c == '"':
                        in_dq = True
                    elif c == "(":
                        depth += 1
                    elif c == ")":
                        depth -= 1
                        if depth == 0:
                            subs.append(text[i + 2 : j])
                            j += 1
                            break
                j += 1
            else:
                subs.append(text[i + 2 : j])
            i = j
            continue
        if ch == "`":
            j = i + 1
            buf: list[str] = []
            while j < n:
                c = text[j]
                if c == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                if c == "`":
                    subs.append("".join(buf))
                    j += 1
                    break
                buf.append(c)
                j += 1
            else:
                subs.append("".join(buf))
            i = j
            continue
        i += 1
    return subs


def _find_unquoted_heredocs_on_line(line: str) -> list[tuple[str, bool, bool]]:
    """Return `(delimiter, strip_tabs, quoted_delim)` for real unquoted, uncommented heredoc operators."""
    specs: list[tuple[str, bool, bool]] = []
    in_sq = False
    in_ansi_sq = False
    in_dq = False
    at_word_start = True
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_sq:
            if ch == "'":
                in_sq = False
            i += 1
            continue
        if in_ansi_sq:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == "'":
                in_ansi_sq = False
            i += 1
            continue
        if in_dq:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_dq = False
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            at_word_start = False
            i += 2
            continue
        if ch == "$" and i + 1 < n and line[i + 1] == "'":
            in_ansi_sq = True
            at_word_start = False
            i += 2
            continue
        if ch == "$" and i + 1 < n and line[i + 1] == '"':
            in_dq = True
            at_word_start = False
            i += 2
            continue
        if ch == "'":
            in_sq = True
            at_word_start = False
            i += 1
            continue
        if ch == '"':
            in_dq = True
            at_word_start = False
            i += 1
            continue
        if ch == "#" and at_word_start:
            break
        if ch == "<" and i + 1 < n and line[i + 1] == "<":
            if i + 2 < n and line[i + 2] == "<":
                i += 3
                at_word_start = True
                continue
            j = i + 2
            strip_tabs = False
            if j < n and line[j] == "-":
                strip_tabs = True
                j += 1
            while j < n and line[j] in (" ", "\t"):
                j += 1
            delim_chars: list[str] = []
            quoted_delim = False
            while j < n and line[j] not in (" ", "\t", "\r", "\n", ";", "&", "|", "<", ">", ")"):
                dc = line[j]
                if dc == "'":
                    quoted_delim = True
                    end_q = line.find("'", j + 1)
                    if end_q == -1:
                        delim_chars.append(line[j + 1 :])
                        j = n
                    else:
                        delim_chars.append(line[j + 1 : end_q])
                        j = end_q + 1
                elif dc == '"':
                    quoted_delim = True
                    j += 1
                    while j < n and line[j] != '"':
                        if line[j] == "\\" and j + 1 < n:
                            delim_chars.append(line[j + 1])
                            j += 2
                        else:
                            delim_chars.append(line[j])
                            j += 1
                    if j < n:
                        j += 1
                elif dc == "\\" and j + 1 < n:
                    quoted_delim = True
                    delim_chars.append(line[j + 1])
                    j += 2
                else:
                    delim_chars.append(dc)
                    j += 1
            delim = "".join(delim_chars)
            if delim:
                specs.append((delim, strip_tabs, quoted_delim))
            i = j
            at_word_start = True
            continue
        at_word_start = ch in (" ", "\t", ";", "|", "&", "(")
        i += 1
    return specs


def _strip_heredocs(command: str) -> tuple[str, list[str]]:
    """Strip heredoc body lines before line-continuation joining so EOF & comments stay intact."""
    if "\n" not in command or "<<" not in command:
        return command, []
    lines = command.splitlines(keepends=True)
    out: list[str] = []
    heredoc_subs: list[str] = []
    pending: list[tuple[str, bool, bool, list[str]]] = []
    for line in lines:
        if pending:
            delim, strip_tabs, quoted_delim, body_buf = pending[0]
            candidate = line.rstrip("\r\n")
            if strip_tabs:
                candidate = candidate.lstrip("\t")
            if candidate == delim:
                if not quoted_delim:
                    heredoc_subs.extend(_extract_substitutions_from_text("".join(body_buf)))
                pending.pop(0)
            else:
                body_buf.append(line)
            continue
        out.append(line)
        for delim, strip_tabs, quoted_delim in _find_unquoted_heredocs_on_line(line):
            pending.append((delim, strip_tabs, quoted_delim, []))
    for _delim, _strip_tabs, quoted_delim, body_buf in pending:
        if not quoted_delim and body_buf:
            heredoc_subs.extend(_extract_substitutions_from_text("".join(body_buf)))
    return "".join(out), heredoc_subs


def extract_command_segments(command: str) -> list[list[str]]:
    """Tokenize shell command into argv segments across operators, keywords, and substitutions."""
    cleaned, heredoc_subs = _strip_heredocs(command)
    segments: list[list[str]] = []
    substitutions: list[str] = list(heredoc_subs)
    cur_seg: list[str] = []
    cur_tok: list[str] = []
    token_started = False
    tok_was_quoted = False
    skip_redirect_target = False

    i = 0
    n = len(cleaned)

    def _flush_raw_segment(tokens: list[str]) -> None:
        unwrapped = _unwrap_command_tokens(tokens)
        if unwrapped:
            segments.append(unwrapped)

    def _flush_tok() -> None:
        nonlocal token_started, tok_was_quoted, skip_redirect_target
        if token_started or cur_tok:
            word = "".join(cur_tok)
            was_q = tok_was_quoted
            cur_tok.clear()
            token_started = False
            tok_was_quoted = False
            if skip_redirect_target:
                skip_redirect_target = False
                return
            if not was_q and not cur_seg and word in SHELL_CONTROL_KEYWORDS:
                return
            if not was_q and word == "}":
                if cur_seg:
                    _flush_raw_segment(cur_seg)
                    cur_seg.clear()
                return
            cur_seg.append(word)

    def _flush_seg() -> None:
        nonlocal skip_redirect_target
        _flush_tok()
        skip_redirect_target = False
        if cur_seg:
            _flush_raw_segment(cur_seg)
            cur_seg.clear()

    def _consume_dollar_paren(start: int) -> int:
        depth = 1
        j = start
        in_sq = False
        in_ansi_sq = False
        in_dq = False
        while j < n and depth > 0:
            ch = cleaned[j]
            if in_sq:
                if ch == "'":
                    in_sq = False
            elif in_ansi_sq:
                if ch == "\\" and j + 1 < n:
                    j += 2
                    continue
                if ch == "'":
                    in_ansi_sq = False
            elif in_dq:
                if ch == "\\" and j + 1 < n:
                    j += 2
                    continue
                if ch == '"':
                    in_dq = False
            else:
                if ch == "\\" and j + 1 < n:
                    j += 2
                    continue
                if ch == "$" and j + 1 < n and cleaned[j + 1] == "'":
                    in_ansi_sq = True
                    j += 2
                    continue
                if ch == "$" and j + 1 < n and cleaned[j + 1] == '"':
                    in_dq = True
                    j += 2
                    continue
                if ch == "'":
                    in_sq = True
                elif ch == '"':
                    in_dq = True
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        substitutions.append(cleaned[start:j])
                        return j + 1
            j += 1
        substitutions.append(cleaned[start:j])
        return j

    def _consume_backtick(start: int) -> int:
        j = start
        buf: list[str] = []
        while j < n:
            ch = cleaned[j]
            if ch == "\\" and j + 1 < n:
                buf.append(cleaned[j + 1])
                j += 2
                continue
            if ch == "`":
                substitutions.append("".join(buf))
                return j + 1
            buf.append(ch)
            j += 1
        substitutions.append("".join(buf))
        return j

    def _consume_double_quoted_into_cur_tok(start: int) -> int:
        j = start
        while j < n:
            c = cleaned[j]
            if c == "\\" and j + 1 < n:
                if cleaned[j + 1] == "\n":
                    j += 2
                    continue
                if cleaned[j + 1] == "\r" and j + 2 < n and cleaned[j + 2] == "\n":
                    j += 3
                    continue
                cur_tok.append(cleaned[j + 1])
                j += 2
                continue
            if c == '"':
                return j + 1
            if c == "$" and j + 1 < n and cleaned[j + 1] == "(":
                j = _consume_dollar_paren(j + 2)
                cur_tok.append("SUBST")
                continue
            if c == "`":
                j = _consume_backtick(j + 1)
                cur_tok.append("SUBST")
                continue
            cur_tok.append(c)
            j += 1
        return j

    while i < n:
        ch = cleaned[i]

        # Unquoted backslash-newline line continuation (comments already stop at `\n` below!)
        if ch == "\\" and i + 1 < n:
            if cleaned[i + 1] == "\n":
                i += 2
                continue
            if cleaned[i + 1] == "\r" and i + 2 < n and cleaned[i + 2] == "\n":
                i += 3
                continue
            token_started = True
            tok_was_quoted = True
            cur_tok.append(cleaned[i + 1])
            i += 2
            continue

        if ch in (" ", "\t", "\r"):
            _flush_tok()
            i += 1
            continue

        if ch in (";", "\n", "(", ")"):
            _flush_seg()
            i += 1
            continue

        if ch in ("&", "|"):
            # `&>` or `&>>` is a redirection (even when unspaced after a word like `gh pr&>/dev/null merge 7`!)
            if ch == "&" and i + 1 < n and cleaned[i + 1] == ">":
                if cur_tok:
                    _flush_tok()
                i += 2
                if i < n and cleaned[i] == ">":
                    i += 1
                skip_redirect_target = True
                continue
            _flush_seg()
            if i + 1 < n and cleaned[i + 1] == ch:
                i += 2
            else:
                i += 1
            continue

        if ch == "#" and not token_started and not cur_tok:
            # POSIX/Bash `#` comments NEVER continue across `\n` even when ending in `\`
            nl = cleaned.find("\n", i)
            i = nl if nl != -1 else n
            continue

        # Handle redirections: `<f`, `>f`, `>|f`, `2>&1`, `>&1tmp`, `{fd}>/dev/null`, `<<<word`
        if ch in ("<", ">"):
            cur_word = "".join(cur_tok)
            is_fd_prefix = not tok_was_quoted and (cur_word.isdigit() or bool(_BRACE_FD_RE.fullmatch(cur_word)))
            if cur_tok and not is_fd_prefix:
                _flush_tok()
            else:
                cur_tok.clear()
                token_started = False
                tok_was_quoted = False
            i += 1
            # Consume remaining characters of this single redirection operator (`>>`, `>|`, `>&`, `<&`, `<>`, `<<<`, `<<-`)
            if i < n:
                if ch == ">" and cleaned[i] in (">", "|", "&"):
                    i += 1
                elif ch == "<" and cleaned[i] == "<":
                    i += 1
                    if i < n and cleaned[i] in ("<", "-"):
                        i += 1
                elif ch == "<" and cleaned[i] in (">", "&"):
                    i += 1
            # Every redirection operator (`2>&1`, `2>&-`, `>&1tmp`, `>/dev/null`, `>|/dev/null`) takes 1 target word!
            skip_redirect_target = True
            continue

        # ANSI-C `$'...'` and Bash locale `$"..."` quoting
        if ch == "$" and i + 1 < n and cleaned[i + 1] == "'":
            token_started = True
            tok_was_quoted = True
            i += 2
            while i < n:
                c = cleaned[i]
                if c == "\\" and i + 1 < n:
                    esc = cleaned[i + 1]
                    cur_tok.append("\n" if esc == "n" else ("\t" if esc == "t" else esc))
                    i += 2
                    continue
                if c == "'":
                    i += 1
                    break
                cur_tok.append(c)
                i += 1
            continue

        if ch == "$" and i + 1 < n and cleaned[i + 1] == '"':
            token_started = True
            tok_was_quoted = True
            i = _consume_double_quoted_into_cur_tok(i + 2)
            continue

        if ch == "'":
            token_started = True
            tok_was_quoted = True
            j = cleaned.find("'", i + 1)
            if j == -1:
                cur_tok.append(cleaned[i + 1 :])
                i = n
            else:
                cur_tok.append(cleaned[i + 1 : j])
                i = j + 1
            continue

        if ch == '"':
            token_started = True
            tok_was_quoted = True
            i = _consume_double_quoted_into_cur_tok(i + 1)
            continue

        if ch == "$" and i + 1 < n and cleaned[i + 1] == "(":
            token_started = True
            i = _consume_dollar_paren(i + 2)
            cur_tok.append("SUBST")
            continue

        if ch == "`":
            token_started = True
            i = _consume_backtick(i + 1)
            cur_tok.append("SUBST")
            continue

        token_started = True
        cur_tok.append(ch)
        i += 1

    _flush_seg()

    for sub in substitutions:
        segments.extend(extract_command_segments(sub))
    return segments


def _unwrap_command_tokens(tokens: list[str]) -> list[str]:
    idx = 0
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        if tok in SHELL_CONTROL_KEYWORDS:
            idx += 1
            continue
        if _ENV_ASSIGN_RE.match(tok):
            idx += 1
            continue
        base = Path(tok).name
        if base in TRANSPARENT_WRAPPERS:
            idx += 1
            while idx < n and tokens[idx].startswith("-"):
                flag = tokens[idx]
                idx += 1
                if flag in ("-u", "-C", "-S") and idx < n:
                    idx += 1
            continue
        break
    return tokens[idx:]


def detect_agent_mode(cwd: str | None = None, env: dict[str, str] | None = None) -> str:
    """Determine execution mode ('main', 'write', or 'read') strictly from public env & cwd."""
    e = env if env is not None else os.environ
    explicit = (e.get("KEEL_AGENT_MODE") or e.get("DEVSTANDARD_AGENT_MODE") or "").strip().lower()
    if explicit in ("read", "write", "main"):
        return explicit
    role = (e.get("DEVSTANDARD_ROLE") or "").strip().lower()
    if role == "reviewer":
        return "read"
    if role == "worker":
        return "write"
    if cwd:
        norm = cwd.replace("\\", "/")
        if "/keel-verify-" in norm or ("/.agents/runs/" in norm and "/readonly" in norm):
            return "read"
        if "/.agents/worktrees/" in norm:
            return "write"
    return "main"


def _normalize_branch_ref(ref: str) -> str:
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/") :]
    if ref.startswith("heads/"):
        return ref[len("heads/") :]
    return ref


def _read_git_push_config(
    repo_dir: Path | None,
    explicit_git_dir: str | None,
    cli_overrides: Sequence[tuple[str, str]],
) -> dict[str, Any]:
    """Pure-Python fast lookup of current branch, upstream, push.default, default remote, and remote push specs."""
    info: dict[str, Any] = {
        "current_branch": None,
        "upstream_branch": None,
        "branch_remote": None,
        "branch_push_remote": None,
        "push_default_remote": None,
        "push_default_mode": "simple",
        "remote_push_specs": {},
    }

    git_dir: Path | None = None
    if explicit_git_dir:
        base = repo_dir or Path.cwd()
        cand = Path(explicit_git_dir)
        git_dir = cand if cand.is_absolute() else (base / cand)
    elif repo_dir is not None:
        cur = repo_dir
        for candidate in (cur, *cur.parents):
            dot_git = candidate / ".git"
            if dot_git.is_dir():
                git_dir = dot_git
                break
            if dot_git.is_file():
                try:
                    first = dot_git.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
                    if first.startswith("gitdir:"):
                        raw_ptr = first[len("gitdir:") :].strip()
                        ptr = Path(raw_ptr)
                        git_dir = ptr if ptr.is_absolute() else (candidate / ptr)
                        break
                except OSError:
                    break

    if git_dir is not None and git_dir.is_dir():
        head_file = git_dir / "HEAD"
        if head_file.is_file():
            try:
                head_text = head_file.read_text(encoding="utf-8", errors="replace").strip()
                if head_text.startswith("ref:"):
                    info["current_branch"] = _normalize_branch_ref(head_text[len("ref:") :].strip())
            except OSError:
                pass

        common_dir = git_dir
        commondir_file = git_dir / "commondir"
        if commondir_file.is_file():
            try:
                rel = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
                if rel:
                    p = Path(rel)
                    common_dir = p if p.is_absolute() else (git_dir / p)
            except OSError:
                pass

        cfg_file = common_dir / "config"
        if cfg_file.is_file():
            try:
                raw_cfg = cfg_file.read_text(encoding="utf-8", errors="replace")
                active_section = ""
                cur_b = info["current_branch"]
                branch_sec = f'branch "{cur_b}"' if cur_b else ""
                for raw_line in raw_cfg.splitlines():
                    stripped = raw_line.strip()
                    if not stripped or stripped.startswith(("#", ";")):
                        continue
                    if stripped.startswith("[") and stripped.endswith("]"):
                        active_section = stripped[1:-1].strip()
                        continue
                    if "=" not in stripped:
                        continue
                    k, v = stripped.split("=", 1)
                    key = k.strip().lower()
                    val = v.strip().strip('"')
                    if active_section.lower() == "push" and key == "default" and val:
                        info["push_default_mode"] = val.lower()
                    elif active_section.lower() == "remote" and key == "pushdefault" and val:
                        info["push_default_remote"] = val
                    elif branch_sec and active_section == branch_sec:
                        if key == "merge" and val:
                            info["upstream_branch"] = _normalize_branch_ref(val)
                        elif key == "remote" and val:
                            info["branch_remote"] = val
                        elif key == "pushremote" and val:
                            info["branch_push_remote"] = val
                    elif active_section.lower().startswith('remote "') and active_section.endswith('"') and key == "push" and val:
                        rname = active_section[len('remote "') : -1]
                        info["remote_push_specs"].setdefault(rname, []).append(val)
            except OSError:
                pass

    # Apply `git -c key=val` CLI overrides last (highest precedence!)
    cur_b = info["current_branch"]
    for raw_k, raw_v in cli_overrides:
        parts = raw_k.strip().split(".")
        val = raw_v.strip()
        if len(parts) == 2 and parts[0].lower() == "push" and parts[1].lower() == "default":
            info["push_default_mode"] = val.lower()
        elif len(parts) == 2 and parts[0].lower() == "remote" and parts[1].lower() == "pushdefault":
            info["push_default_remote"] = val
        elif len(parts) >= 3 and parts[0].lower() == "remote" and parts[-1].lower() == "push":
            r_key = ".".join(parts[1:-1])
            if not val:
                info["remote_push_specs"][r_key] = []
            else:
                info["remote_push_specs"].setdefault(r_key, []).append(val)
        elif len(parts) >= 3 and parts[0].lower() == "branch" and cur_b and ".".join(parts[1:-1]) == cur_b:
            subk = parts[-1].lower()
            if subk == "merge":
                info["upstream_branch"] = _normalize_branch_ref(val)
            elif subk == "remote":
                info["branch_remote"] = val
            elif subk == "pushremote":
                info["branch_push_remote"] = val

    return info


def _git_context_and_subcommand(
    tokens: Sequence[str], base_cwd: Path | None
) -> tuple[str | None, list[str], Path | None, str | None, list[tuple[str, str]]]:
    idx = 1
    n = len(tokens)
    effective_cwd = base_cwd
    explicit_git_dir: str | None = None
    cli_overrides: list[tuple[str, str]] = []
    flags_with_arg = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}

    while idx < n:
        tok = tokens[idx]
        if tok == "--":
            idx += 1
            break
        if tok == "-C" and idx + 1 < n:
            arg_dir = Path(tokens[idx + 1])
            effective_cwd = arg_dir if arg_dir.is_absolute() else ((effective_cwd or Path.cwd()) / arg_dir)
            idx += 2
            continue
        if tok.startswith("-C") and len(tok) > 2:
            arg_dir = Path(tok[2:])
            effective_cwd = arg_dir if arg_dir.is_absolute() else ((effective_cwd or Path.cwd()) / arg_dir)
            idx += 1
            continue
        if tok == "-c" and idx + 1 < n:
            kv = tokens[idx + 1]
            k, _, v = kv.partition("=")
            cli_overrides.append((k, v))
            idx += 2
            continue
        if tok.startswith("-c") and len(tok) > 2:
            kv = tok[2:]
            k, _, v = kv.partition("=")
            cli_overrides.append((k, v))
            idx += 1
            continue
        if tok == "--git-dir" and idx + 1 < n:
            explicit_git_dir = tokens[idx + 1]
            idx += 2
            continue
        if tok.startswith("--git-dir="):
            explicit_git_dir = tok.split("=", 1)[1]
            idx += 1
            continue
        if tok in flags_with_arg:
            idx += 2
            continue
        if any(tok.startswith(f + "=") for f in flags_with_arg):
            idx += 1
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        return tok, list(tokens[idx + 1 :]), effective_cwd, explicit_git_dir, cli_overrides
    return None, [], effective_cwd, explicit_git_dir, cli_overrides


def _git_push_protected_reason(
    args: Sequence[str],
    default_branches: frozenset[str],
    repo_dir: Path | None,
    explicit_git_dir: str | None,
    cli_overrides: Sequence[tuple[str, str]],
) -> str | None:
    positionals: list[str] = []
    explicit_repo_flag: str | None = None
    is_delete_flag = False
    idx = 0
    n = len(args)
    other_value_flags = {"--receive-pack", "--exec", "-o", "--push-option"}

    while idx < n:
        tok = args[idx]
        if tok == "--":
            positionals.extend(args[idx + 1 :])
            break
        if tok in ("--all", "--branches", "--mirror"):
            return f"Bulk 'git push {tok}' is blocked because it updates protected default branches."
        if tok in ("-d", "--delete"):
            is_delete_flag = True
            idx += 1
            continue
        if tok == "--repo" and idx + 1 < n:
            explicit_repo_flag = args[idx + 1]
            idx += 2
            continue
        if tok.startswith("--repo="):
            explicit_repo_flag = tok.split("=", 1)[1]
            idx += 1
            continue
        if tok in other_value_flags:
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        positionals.append(tok)
        idx += 1

    cfg = _read_git_push_config(repo_dir, explicit_git_dir, cli_overrides)
    cur_branch = cfg["current_branch"]
    upstream_branch = cfg["upstream_branch"]
    push_default = cfg["push_default_mode"]

    remote_name = (
        (positionals[0] if positionals else None)
        or explicit_repo_flag
        or cfg["branch_push_remote"]
        or cfg["push_default_remote"]
        or cfg["branch_remote"]
        or "origin"
    )
    remote_push_specs: list[str] = cfg["remote_push_specs"].get(remote_name, [])

    def _check_refspec(spec: str) -> str | None:
        clean = spec.lstrip("+")
        if clean == ":":
            return "Matching 'git push ... :' is blocked because it updates matching protected branches."
        if ":" in clean:
            _src, dst = clean.split(":", 1)
            target = dst
        elif is_delete_flag:
            target = clean
        elif clean in ("HEAD", "@"):
            if cur_branch in default_branches:
                return f"'git push ... {clean}' while checked out on '{cur_branch}' targets protected '{cur_branch}'."
            if upstream_branch in default_branches:
                return f"'git push ... {clean}' tracking protected upstream '{upstream_branch}' is blocked."
            return None
        else:
            target = clean

        if "*" in target:
            return f"Wildcard push refspec '{spec}' is blocked because it can update protected branches."
        norm_target = _normalize_branch_ref(target)
        if norm_target in default_branches:
            return (
                f"Direct 'git push' targeting protected branch '{norm_target}' is blocked. "
                "Push a task branch and integrate through a verified pull request."
            )
        return None

    # Implicit push (`git push` or `git push <remote>` with zero explicit CLI refspecs)
    if len(positionals) < 2:
        for cfg_spec in remote_push_specs:
            reason = _check_refspec(cfg_spec)
            if reason:
                return reason
        if not remote_push_specs:
            if push_default == "matching":
                return "Implicit 'git push' with push.default=matching is blocked because it updates protected branches."
            if cur_branch in default_branches:
                return f"Implicit 'git push' while checked out on protected branch '{cur_branch}' is blocked."
            if upstream_branch in default_branches:
                return f"Implicit 'git push' tracking protected upstream branch '{upstream_branch}' is blocked."
        return None

    for spec in positionals[1:]:
        reason = _check_refspec(spec)
        if reason:
            return reason
    return None


def _gh_group_and_action(tokens: Sequence[str]) -> tuple[str, str, list[str]]:
    """Extract `(group, action, remaining_args)` while preserving `gh api` flags intact."""
    non_global: list[str] = []
    idx = 1
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        if len(non_global) < 2:
            if tok in ("-R", "--repo", "--hostname") and idx + 1 < n:
                idx += 2
                continue
            if any(tok.startswith(p) for p in ("-R", "--repo=", "--hostname=")) and tok not in ("-R",):
                idx += 1
                continue
            if tok.startswith("-"):
                idx += 1
                continue
        non_global.append(tok)
        idx += 1
        # As soon as `gh api` is identified, return all subsequent arguments untouched!
        if len(non_global) == 1 and non_global[0] == "api":
            return "api", "", list(tokens[idx:])
    group = non_global[0] if len(non_global) >= 1 else ""
    action = non_global[1] if len(non_global) >= 2 else ""
    rest = non_global[2:] if len(non_global) > 2 else []
    return group, action, rest


def _gh_api_is_mutating(args: Sequence[str]) -> bool:
    idx = 0
    n = len(args)
    explicit_method: str | None = None
    has_fields = False
    has_readonly_graphql_query = False
    has_graphql_mutation = False
    endpoint: str | None = None
    read_value_flags = {"-H", "--header", "-q", "--jq", "-t", "--template", "--hostname", "--cache", "-p", "--preview"}

    def _inspect_field(val: str) -> None:
        nonlocal has_fields, has_readonly_graphql_query, has_graphql_mutation
        has_fields = True
        if val.startswith("query="):
            q = val[len("query=") :]
            if re.search(r"\bmutation\b", q, re.IGNORECASE):
                has_graphql_mutation = True
            else:
                has_readonly_graphql_query = True

    while idx < n:
        tok = args[idx]
        if tok in ("-X", "--method") and idx + 1 < n:
            explicit_method = args[idx + 1].upper()
            idx += 2
            continue
        if tok.startswith("-X") and len(tok) > 2:
            explicit_method = tok[2:].upper()
            idx += 1
            continue
        if tok.startswith("--method="):
            explicit_method = tok.split("=", 1)[1].upper()
            idx += 1
            continue
        if tok == "--input" or tok.startswith("--input="):
            return True
        if tok in ("-f", "-F", "--field", "--raw-field"):
            val = args[idx + 1] if idx + 1 < n else ""
            _inspect_field(val)
            idx += 2
            continue
        if any(tok.startswith(p) for p in ("-f", "-F", "--field=", "--raw-field=")):
            val = tok.split("=", 1)[1] if tok.startswith("--") else tok[2:]
            _inspect_field(val)
            idx += 1
            continue
        if tok in read_value_flags and idx + 1 < n:
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        if endpoint is None:
            endpoint = tok.lstrip("/")
        idx += 1

    if explicit_method is not None:
        if explicit_method not in ("GET", "HEAD"):
            return True
        return endpoint == "graphql" and has_graphql_mutation

    if not has_fields:
        return False
    if endpoint == "graphql" and has_readonly_graphql_query and not has_graphql_mutation:
        return False
    return True


def _unwrap_python_keel_invocation(seg: list[str]) -> list[str]:
    """Unwrap `python3 [-B -u -W warn ...] /path/to/keel ...` to `['keel', ...]`."""
    prog = Path(seg[0]).name
    if not prog.startswith("python"):
        return seg
    idx = 1
    n = len(seg)
    py_arg_flags = {"-W", "-X", "--check-hash-based-pycs"}
    while idx < n:
        tok = seg[idx]
        if tok in py_arg_flags and idx + 1 < n:
            idx += 2
            continue
        if tok in ("-c", "-m"):
            return seg
        if tok.startswith("-"):
            idx += 1
            continue
        if Path(tok).name == "keel":
            return seg[idx:]
        break
    return seg


def evaluate_command(
    command: str,
    *,
    mode: str = "main",
    default_branches: frozenset[str] = DEFAULT_BRANCHES,
    cwd: str | os.PathLike[str] | None = None,
) -> str | None:
    """Return a concrete refusal reason if `command` violates safety boundaries, else None."""
    if not command or not command.strip():
        return None

    tracked_cwd: Path | None = Path(cwd) if cwd else Path.cwd()

    for seg in extract_command_segments(command):
        if not seg:
            continue
        prog = Path(seg[0]).name

        if prog == "cd" and len(seg) >= 2 and not seg[1].startswith("-"):
            dest = Path(seg[1])
            tracked_cwd = dest if dest.is_absolute() else ((tracked_cwd or Path.cwd()) / dest)
            continue

        # 1. `gh` checks
        if prog == "gh" and len(seg) >= 2:
            group, action, gh_rest = _gh_group_and_action(seg)
            if group == "pr" and action == "merge":
                return (
                    "Raw 'gh pr merge' is blocked across all sessions. "
                    "Use 'keel pr merge <pr>' so ancestry, CI checks, and independent review are verified."
                )
            if mode == "read":
                if group in GH_MUTATING_SUBCOMMANDS and action in GH_MUTATING_SUBCOMMANDS[group]:
                    return f"Read-only subagent mode blocks mutating GitHub CLI command 'gh {group} {action}'."
                if group == "api":
                    api_args = ([action] if action else []) + gh_rest
                    if _gh_api_is_mutating(api_args):
                        return "Read-only subagent mode blocks mutating 'gh api' requests."

        # 2. `git` checks
        if prog == "git":
            subcmd, git_args, git_cwd, explicit_git_dir, cli_overrides = _git_context_and_subcommand(seg, tracked_cwd)
            if subcmd == "push":
                if mode == "read":
                    return "Read-only subagent mode blocks 'git push'."
                push_reason = _git_push_protected_reason(
                    git_args, default_branches, git_cwd, explicit_git_dir, cli_overrides
                )
                if push_reason:
                    return push_reason

        # 3. Subagent `keel pr` mutation checks (`allow_abbrev=False` enforced on CLI too)
        keel_seg = _unwrap_python_keel_invocation(seg)
        if mode in ("read", "write") and Path(keel_seg[0]).name == "keel" and len(keel_seg) >= 3 and keel_seg[1] == "pr":
            sub_action = keel_seg[2]
            rest = keel_seg[3:]
            if sub_action == "merge" and any(r == "--execute" or r.startswith("--exec") for r in rest):
                return "Subagents cannot execute 'keel pr merge --execute'; return your result to the main session."
            if mode == "read" and (
                sub_action == "publish-verdict"
                or (sub_action == "verify" and any(r == "--publish" or r.startswith("--pub") for r in rest))
            ):
                return "Read-only subagent mode blocks publishing PR reviews via 'keel pr'."

    return None
