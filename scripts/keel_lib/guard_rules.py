"""Pure-Git command-segment semantic seatbelt for PreToolUse hooks (Antigravity + Codex CLI)."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Any, Sequence

from .env import is_disposable_verify_path

DEFAULT_BRANCHES = frozenset({"main", "master"})
TRANSPARENT_WRAPPERS = frozenset({"command", "env", "nohup", "sudo", "time", "nice"})
SHELL_CONTROL_KEYWORDS = frozenset(
    {"if", "then", "elif", "else", "fi", "while", "until", "do", "done", "case", "esac", "{", "}", "!"}
)
ALWAYS_MUTATING_GIT = frozenset(
    {"add", "am", "apply", "cherry-pick", "checkout", "clean", "commit", "merge", "mv", "pull", "rebase", "reset", "restore", "revert", "rm", "switch", "update-ref"}
)
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_BRACE_FD_RE = re.compile(r"^\{[A-Za-z_][A-Za-z0-9_]*\}$")

def _scan_dollar_paren(text: str, start: int) -> tuple[str, int]:
    depth, j, n, in_sq, in_ansi, in_dq = 1, start, len(text), False, False, False
    while j < n and depth > 0:
        ch = text[j]
        if in_sq:
            in_sq = ch != "'"
        elif in_ansi or in_dq:
            if ch == "\\" and j + 1 < n:
                j += 2
                continue
            in_ansi, in_dq = (False if ch == "'" else in_ansi), (False if ch == '"' else in_dq)
        elif ch == "\\" and j + 1 < n:
            j += 2
            continue
        elif ch == "$" and j + 1 < n and text[j + 1] in ("'", '"'):
            in_ansi, in_dq, j = (text[j + 1] == "'"), (text[j + 1] == '"'), j + 2
            continue
        elif ch in ("'", '"'):
            in_sq, in_dq = (ch == "'"), (ch == '"')
        elif ch in ("(", ")"):
            depth += 1 if ch == "(" else -1
            if depth == 0:
                return text[start:j], j + 1
        j += 1
    return text[start:j], j

def _scan_backtick(text: str, start: int) -> tuple[str, int]:
    j, n, buf = start, len(text), []
    while j < n:
        if text[j] == "\\" and j + 1 < n:
            buf.append(text[j + 1])
            j += 2
        elif text[j] == "`":
            return "".join(buf), j + 1
        else:
            buf.append(text[j])
            j += 1
    return "".join(buf), j

def _extract_substitutions_from_text(text: str) -> list[str]:
    subs, i, n = [], 0, len(text)
    while i < n:
        if text[i] == "\\" and i + 1 < n:
            i += 2
        elif text[i] == "$" and i + 1 < n and text[i + 1] == "(":
            sub, i = _scan_dollar_paren(text, i + 2)
            subs.append(sub)
        elif text[i] == "`":
            sub, i = _scan_backtick(text, i + 1)
            subs.append(sub)
        else:
            i += 1
    return subs

def _find_unquoted_heredocs_on_line(line: str) -> list[tuple[str, bool, bool]]:
    specs, in_sq, in_ansi, in_dq, at_word_start, i, n = [], False, False, False, True, 0, len(line)
    while i < n:
        ch = line[i]
        if in_sq:
            in_sq, i = (ch != "'"), i + 1
        elif in_ansi or in_dq:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            in_ansi = False if (in_ansi and ch == "'") else in_ansi
            in_dq = False if (in_dq and ch == '"') else in_dq
            i += 1
        elif ch == "\\" and i + 1 < n:
            at_word_start, i = False, i + 2
        elif ch == "$" and i + 1 < n and line[i + 1] in ("'", '"'):
            in_ansi, in_dq, at_word_start, i = (line[i + 1] == "'"), (line[i + 1] == '"'), False, i + 2
        elif ch in ("'", '"'):
            in_sq, in_dq, at_word_start, i = (ch == "'"), (ch == '"'), False, i + 1
        elif ch == "#" and at_word_start:
            break
        elif ch == "<" and i + 1 < n and line[i + 1] == "<":
            if i + 2 < n and line[i + 2] == "<":
                i, at_word_start = i + 3, True
                continue
            j = i + 2
            strip_tabs = j < n and line[j] == "-"
            j += 1 if strip_tabs else 0
            while j < n and line[j] in (" ", "\t"):
                j += 1
            delim_chars, quoted_delim = [], False
            while j < n and line[j] not in (" ", "\t", "\r", "\n", ";", "&", "|", "<", ">", ")"):
                dc = line[j]
                if dc == "'":
                    quoted_delim, end_q = True, line.find("'", j + 1)
                    delim_chars.append(line[j + 1 :] if end_q == -1 else line[j + 1 : end_q])
                    j = n if end_q == -1 else end_q + 1
                elif dc == '"':
                    quoted_delim, j = True, j + 1
                    while j < n and line[j] != '"':
                        step = 2 if (line[j] == "\\" and j + 1 < n) else 1
                        delim_chars.append(line[j + step - 1])
                        j += step
                    j += 1 if j < n else 0
                elif dc == "\\" and j + 1 < n:
                    quoted_delim = True
                    delim_chars.append(line[j + 1])
                    j += 2
                else:
                    delim_chars.append(dc)
                    j += 1
            if delim_chars:
                specs.append(("".join(delim_chars), strip_tabs, quoted_delim))
            i, at_word_start = j, True
        else:
            at_word_start, i = (ch in (" ", "\t", ";", "|", "&", "(")), i + 1
    return specs

def _strip_heredocs(command: str) -> tuple[str, list[str]]:
    if "\n" not in command or "<<" not in command:
        return command, []
    out, heredoc_subs, pending = [], [], []
    for line in command.splitlines(keepends=True):
        if pending:
            delim, strip_tabs, quoted_delim, body_buf = pending[0]
            cand = line.rstrip("\r\n")
            if (cand.lstrip("\t") if strip_tabs else cand) == delim:
                if not quoted_delim:
                    heredoc_subs.extend(_extract_substitutions_from_text("".join(body_buf)))
                pending.pop(0)
            else:
                body_buf.append(line)
            continue
        out.append(line)
        for spec in _find_unquoted_heredocs_on_line(line):
            pending.append((*spec, []))
    for _d, _s, quoted_delim, body_buf in pending:
        if not quoted_delim and body_buf:
            heredoc_subs.extend(_extract_substitutions_from_text("".join(body_buf)))
    return "".join(out), heredoc_subs

def _unwrap_command_tokens(tokens: list[str]) -> list[str]:
    idx, n = 0, len(tokens)
    while idx < n:
        if tokens[idx] in SHELL_CONTROL_KEYWORDS or _ENV_ASSIGN_RE.match(tokens[idx]):
            idx += 1
        elif Path(tokens[idx]).name in TRANSPARENT_WRAPPERS:
            idx += 1
            while idx < n and tokens[idx].startswith("-"):
                idx += 2 if (tokens[idx] in ("-u", "-C", "-S") and idx + 1 < n) else 1
        else:
            break
    return tokens[idx:]


def extract_command_segments(command: str) -> list[list[str]]:
    """Tokenize shell command into argv segments across operators, keywords, and substitutions."""
    cleaned, substitutions = _strip_heredocs(command)
    segments, cur_seg, cur_tok = [], [], []
    token_started = tok_was_quoted = skip_redirect_target = False
    i, n = 0, len(cleaned)

    def _flush_raw(tokens: list[str]) -> None:
        unwrapped = _unwrap_command_tokens(tokens)
        if unwrapped:
            segments.append(unwrapped)

    def _flush_tok() -> None:
        nonlocal token_started, tok_was_quoted, skip_redirect_target
        if not (token_started or cur_tok):
            return
        word, was_q = "".join(cur_tok), tok_was_quoted
        cur_tok.clear()
        token_started = tok_was_quoted = False
        if skip_redirect_target:
            skip_redirect_target = False
        elif not was_q and not cur_seg and word in SHELL_CONTROL_KEYWORDS:
            return
        elif not was_q and word == "}":
            if cur_seg:
                _flush_raw(cur_seg)
                cur_seg.clear()
        else:
            cur_seg.append(word)

    def _flush_seg() -> None:
        nonlocal skip_redirect_target
        _flush_tok()
        skip_redirect_target = False
        if cur_seg:
            _flush_raw(cur_seg)
            cur_seg.clear()

    def _consume_subst(pos: int) -> int:
        sub, nxt = _scan_dollar_paren(cleaned, pos + 2) if cleaned[pos] == "$" else _scan_backtick(cleaned, pos + 1)
        substitutions.append(sub)
        cur_tok.append("SUBST")
        return nxt

    def _consume_dq(start: int) -> int:
        j = start
        while j < n:
            c = cleaned[j]
            if c == "\\" and j + 1 < n:
                nl_step = 2 if cleaned[j + 1] == "\n" else (3 if (cleaned[j + 1] == "\r" and j + 2 < n and cleaned[j + 2] == "\n") else 0)
                if nl_step:
                    j += nl_step
                else:
                    cur_tok.append(cleaned[j + 1])
                    j += 2
            elif c == '"':
                return j + 1
            elif (c == "$" and j + 1 < n and cleaned[j + 1] == "(") or c == "`":
                j = _consume_subst(j)
            else:
                cur_tok.append(c)
                j += 1
        return j

    while i < n:
        ch = cleaned[i]
        if ch == "\\" and i + 1 < n:
            nl_step = 2 if cleaned[i + 1] == "\n" else (3 if (cleaned[i + 1] == "\r" and i + 2 < n and cleaned[i + 2] == "\n") else 0)
            if nl_step:
                i += nl_step
            else:
                token_started, tok_was_quoted = True, True
                cur_tok.append(cleaned[i + 1])
                i += 2
        elif ch in (" ", "\t", "\r"):
            _flush_tok()
            i += 1
        elif ch in (";", "\n", "(", ")"):
            _flush_seg()
            i += 1
        elif ch in ("&", "|"):
            if ch == "&" and i + 1 < n and cleaned[i + 1] == ">":
                if cur_tok:
                    _flush_tok()
                i += 3 if (i + 2 < n and cleaned[i + 2] == ">") else 2
                skip_redirect_target = True
            else:
                _flush_seg()
                i += 2 if (i + 1 < n and cleaned[i + 1] == ch) else 1
        elif ch == "#" and not token_started and not cur_tok:
            nl = cleaned.find("\n", i)
            i = nl if nl != -1 else n
        elif ch in ("<", ">"):
            cur_word = "".join(cur_tok)
            if cur_tok and not (not tok_was_quoted and (cur_word.isdigit() or bool(_BRACE_FD_RE.fullmatch(cur_word)))):
                _flush_tok()
            else:
                cur_tok.clear()
                token_started = tok_was_quoted = False
            i += 1
            if i < n:
                if (ch == ">" and cleaned[i] in (">", "|", "&")) or (ch == "<" and cleaned[i] in (">", "&")):
                    i += 1
                elif ch == "<" and cleaned[i] == "<":
                    i += 2 if (i + 1 < n and cleaned[i + 1] in ("<", "-")) else 1
            skip_redirect_target = True
        elif ch == "$" and i + 1 < n and cleaned[i + 1] == "'":
            token_started, tok_was_quoted, i = True, True, i + 2
            while i < n and cleaned[i] != "'":
                if cleaned[i] == "\\" and i + 1 < n:
                    esc = cleaned[i + 1]
                    cur_tok.append("\n" if esc == "n" else ("\t" if esc == "t" else esc))
                    i += 2
                else:
                    cur_tok.append(cleaned[i])
                    i += 1
            i += 1 if i < n else 0
        elif ch == "$" and i + 1 < n and cleaned[i + 1] == '"':
            token_started, tok_was_quoted, i = True, True, _consume_dq(i + 2)
        elif ch == "'":
            token_started, tok_was_quoted, j = True, True, cleaned.find("'", i + 1)
            cur_tok.append(cleaned[i + 1 :] if j == -1 else cleaned[i + 1 : j])
            i = n if j == -1 else j + 1
        elif ch == '"':
            token_started, tok_was_quoted, i = True, True, _consume_dq(i + 1)
        elif (ch == "$" and i + 1 < n and cleaned[i + 1] == "(") or ch == "`":
            token_started, i = True, _consume_subst(i)
        else:
            token_started = True
            cur_tok.append(ch)
            i += 1
    _flush_seg()
    for sub in substitutions:
        segments.extend(extract_command_segments(sub))
    return segments


def detect_agent_mode(cwd: str | None = None, env: dict[str, str] | None = None) -> str:
    """Determine execution mode ('main', 'write', or 'read') strictly from public env & cwd."""
    e = env if env is not None else os.environ
    explicit = (e.get("KEEL_AGENT_MODE") or e.get("DEVSTANDARD_AGENT_MODE") or "").strip().lower()
    if explicit in ("read", "write", "main"):
        return explicit
    role = (e.get("DEVSTANDARD_ROLE") or "").strip().lower()
    if role in ("reviewer", "worker"):
        return "read" if role == "reviewer" else "write"
    if cwd:
        norm = cwd.replace("\\", "/")
        if "/.agents/worktrees/" in norm:
            return "write"
        if is_disposable_verify_path(cwd) or ("/.agents/runs/" in norm and "/readonly" in norm):
            return "read"
    return "main"

def _normalize_branch_ref(ref: str) -> str:
    return ref[11:] if ref.startswith("refs/heads/") else (ref[6:] if ref.startswith("heads/") else ref)

def _git_context_and_subcommand(
    tokens: Sequence[str], base_cwd: Path | None
) -> tuple[str | None, list[str], Path | None, str | None, str | None, list[tuple[str, str]]]:
    idx, n, eff_cwd, git_dir, work_tree, overrides = 1, len(tokens), base_cwd, None, None, []
    while idx < n:
        tok = tokens[idx]
        if tok == "--":
            return (tokens[idx + 1], list(tokens[idx + 2 :]), eff_cwd, git_dir, work_tree, overrides) if idx + 1 < n else (
                None, [], eff_cwd, git_dir, work_tree, overrides
            )
        if (tok == "-C" and idx + 1 < n) or (tok.startswith("-C") and len(tok) > 2):
            d = Path(tokens[idx + 1] if tok == "-C" else tok[2:])
            eff_cwd, idx = (d if d.is_absolute() else ((eff_cwd or Path.cwd()) / d)), idx + (2 if tok == "-C" else 1)
        elif (tok == "-c" and idx + 1 < n) or (tok.startswith("-c") and len(tok) > 2):
            overrides.append((tokens[idx + 1] if tok == "-c" else tok[2:]).partition("=")[::2])  # type: ignore[arg-type]
            idx += 2 if tok == "-c" else 1
        elif (tok == "--git-dir" and idx + 1 < n) or tok.startswith("--git-dir="):
            git_dir, idx = (tokens[idx + 1] if tok == "--git-dir" else tok.split("=", 1)[1]), idx + (2 if tok == "--git-dir" else 1)
        elif (tok == "--work-tree" and idx + 1 < n) or tok.startswith("--work-tree="):
            work_tree, idx = (tokens[idx + 1] if tok == "--work-tree" else tok.split("=", 1)[1]), idx + (2 if tok == "--work-tree" else 1)
        elif tok in ("--namespace", "--config-env"):
            idx += 2
        elif tok.startswith("-"):
            idx += 1
        else:
            return tok, list(tokens[idx + 1 :]), eff_cwd, git_dir, work_tree, overrides
    return None, [], eff_cwd, git_dir, work_tree, overrides

def _inspect_git_repo(git_cwd: Path | None, explicit_git_dir: str | None, explicit_work_tree: str | None) -> dict[str, Any] | None:
    base = git_cwd or Path.cwd()
    if not explicit_git_dir:
        if git_cwd is None or not git_cwd.exists():
            return None
        resolved = git_cwd.resolve(strict=False)
        if not any((c / ".git").exists() or ((c / "HEAD").is_file() and (c / "objects").is_dir()) for c in (resolved, *resolved.parents)):
            return None
    cmd = ["git", *([f"--git-dir={explicit_git_dir}"] if explicit_git_dir else []), *([f"--work-tree={explicit_work_tree}"] if explicit_work_tree else [])]
    proc = subprocess.run(
        [*cmd, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--show-toplevel"],
        cwd=str(base) if base.is_dir() else None, text=True, capture_output=True, check=False,
    )
    lines = [ln.strip() for ln in proc.stdout.splitlines()] if proc.returncode == 0 else []
    if len(lines) < 2:
        return None
    git_dir, common_dir, toplevel = Path(lines[0]), Path(lines[1]), (Path(lines[2]) if len(lines) >= 3 and lines[2] else None)
    head_text = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip() if (git_dir / "HEAD").is_file() else ""
    return {
        "git_dir": git_dir, "common_dir": common_dir, "toplevel": toplevel,
        "current_branch": _normalize_branch_ref(head_text[4:].strip()) if head_text.startswith("ref:") else None,
        "is_linked_worktree": git_dir.resolve(strict=False) != common_dir.resolve(strict=False),
        "is_disposable_verify": is_disposable_verify_path(git_dir) and is_disposable_verify_path(toplevel or git_cwd),
    }

def _git_push_reason(args: Sequence[str], mode: str, cli_overrides: Sequence[tuple[str, str]]) -> str | None:
    if mode in ("read", "write"):
        return f"Subagent '{mode}' mode blocks all 'git push' commands; return to the main session."
    if any(k.strip().lower().endswith(".push") and v.startswith(("+", ":")) for k, v in cli_overrides):
        return "Destructive 'git -c ... push' refspec is blocked."
    pos, idx, n = [], 0, len(args)
    bad_long = {"--force", "--force-with-lease", "--force-if-includes", "--delete", "--mirror"}
    val_long = ("--repo", "--receive-pack", "--exec", "--push-option")
    while idx < n:
        tok = args[idx]
        if tok == "--":
            pos.extend(args[idx + 1 :])
            break
        if tok in bad_long or tok.startswith("--force-with-lease="):
            return f"Destructive 'git push {tok}' is blocked without explicit human authorization."
        if tok in (*val_long, "-o"):
            idx += 2
        elif any(tok.startswith(f + "=") for f in val_long):
            idx += 1
        elif tok.startswith("-") and not tok.startswith("--") and len(tok) > 1:
            o_pos = tok.find("o", 1)
            flags_part = tok[1:] if o_pos == -1 else tok[1:o_pos]
            if any(c in flags_part for c in ("f", "d")):
                return "Destructive 'git push' short flag ('-f'/'-d') is blocked without explicit human authorization."
            idx += 2 if (o_pos != -1 and o_pos + 1 == len(tok)) else 1
        else:
            pos.append(tok)
            idx += 1
    bad_spec = next((s for s in pos[1:] if s.startswith(("+", ":"))), None)
    return f"Destructive refspec '{bad_spec}' in 'git push' is blocked without explicit human authorization." if bad_spec else None

def _non_flag_positionals(args: Sequence[str], value_flags: set[str]) -> list[str]:
    pos, idx = [], 0
    while idx < len(args):
        if args[idx] == "--":
            pos.extend(args[idx + 1 :])
            break
        if not args[idx].startswith("-"):
            pos.append(args[idx])
        idx += 2 if args[idx] in value_flags else 1
    return pos

def _is_mutating_git(subcmd: str, args: Sequence[str]) -> bool:
    if subcmd in ALWAYS_MUTATING_GIT:
        return True
    if subcmd == "stash":
        return next((a for a in args if not a.startswith("-")), "push") not in ("list", "show")
    if subcmd == "tag":
        if any(a in {"-d", "--delete", "-f", "--force", "-a", "--annotate", "-s", "--sign", "-u", "-m", "-F"} or a.startswith(("-m", "-F", "-u")) for a in args):
            return True
        if any(a in ("-l", "--list", "-v", "--verify") for a in args):
            return False
        return bool(_non_flag_positionals(args, {"--contains", "--no-contains", "--points-at", "--merged", "--no-merged", "--sort", "--format"}))
    if subcmd == "branch":
        if any(
            a in {"--delete", "--move", "--copy", "--force", "--set-upstream-to", "--unset-upstream", "--edit-description"}
            or a.startswith("--set-upstream-to=")
            or (a.startswith("-") and not a.startswith("--") and any(c in a[1:] for c in "dDmMcCfu"))
            for a in args
        ):
            return True
        q_opts = ("--contains", "--no-contains", "--merged", "--no-merged", "--points-at")
        has_q = any(
            a in {"-a", "--all", "-r", "--remotes", "-l", "--list", "--show-current", *q_opts}
            or any(a.startswith(f + "=") for f in q_opts)
            or (a.startswith("-") and not a.startswith("--") and any(c in a[1:] for c in "arl"))
            for a in args
        )
        return bool(_non_flag_positionals(args, {"--contains", "--no-contains", "--points-at", "--sort", "--format"})) and not has_q
    if subcmd == "worktree":
        return next((a for a in args if not a.startswith("-")), "list") in ("add", "remove", "prune", "move", "lock", "unlock", "repair")
    if subcmd == "remote":
        return next((a for a in args if not a.startswith("-")), "") in ("add", "remove", "rm", "set-url", "rename", "prune")
    return False

def _targets_protected_branch(subcmd: str, args: Sequence[str], default_branches: frozenset[str]) -> bool:
    if subcmd == "update-ref":
        return any(_normalize_branch_ref(a) in default_branches for a in args if not a.startswith("-"))
    if subcmd == "branch" and _is_mutating_git("branch", args):
        pos = [a for a in args if a != "--" and not a.startswith("-")]
        if not pos:
            return False
        has_dm = any(a in ("-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy") or (a.startswith("-") and not a.startswith("--") and any(c in a[1:] for c in "dDmMcC")) for a in args)
        return any(_normalize_branch_ref(t) in default_branches for t in (pos if has_dm else [pos[0]]))
    if subcmd == "fetch":
        return any(_normalize_branch_ref(a.lstrip("+").split(":", 1)[1]) in default_branches for a in args if not a.startswith("-") and ":" in a)
    if subcmd in ("checkout", "switch") and "--" not in args:
        for idx, a in enumerate(args):
            if a in ("-b", "-B", "-c", "-C") and idx + 1 < len(args):
                return _normalize_branch_ref(args[idx + 1]) in default_branches
        first_pos = next((a for a in args if not a.startswith("-")), None)
        return first_pos is not None and _normalize_branch_ref(first_pos) in default_branches
    return False

def _git_z(toplevel: Path, git_args: list[str]) -> list[str]:
    proc = subprocess.run(["git", "-c", "core.quotePath=false", "-C", str(toplevel), *git_args], text=True, capture_output=True, check=False)
    return [item for item in proc.stdout.split("\0") if item] if proc.returncode == 0 else []

def _to_repo_rel(raw_path: str, base_cwd: Path, toplevel: Path) -> str | None:
    if raw_path.startswith(":"):
        return None
    try:
        rel_str = (Path(raw_path) if Path(raw_path).is_absolute() else (base_cwd / raw_path)).resolve(strict=False).relative_to(toplevel.resolve(strict=False)).as_posix()
        return "" if rel_str == "." else rel_str
    except ValueError:
        return None

def _record_pending_staging(subcmd: str, args: Sequence[str], base_cwd: Path, toplevel: Path, pending: set[str]) -> None:
    if subcmd == "add":
        has_all, has_upd = any(a in ("-A", "--all", "--no-ignore-removal") for a in args), any(a in ("-u", "--update") for a in args)
        specs = [a for a in args if a != "--" and not a.startswith("-")]
        if has_all or has_upd or not specs:
            for ln in _git_z(toplevel, ["status", "--porcelain", "--no-renames", "-z", "-uall"]):
                if not (has_upd and ln.startswith("?? ")):
                    pending.add(ln[3:])
        for raw_p in specs:
            rel_p = _to_repo_rel(raw_p, base_cwd, toplevel)
            if rel_p is None:
                pending.add(raw_p)
            elif rel_p == "" or (toplevel / rel_p).is_dir():
                for ln in _git_z(toplevel, ["status", "--porcelain", "--no-renames", "-z", "-uall", "--", *([rel_p] if rel_p else [])]):
                    pending.add(ln[3:])
            elif (toplevel / rel_p).exists() or (toplevel / rel_p).is_symlink() or _git_z(toplevel, ["ls-files", "-z", "--", rel_p]) or not rel_p.startswith("docs/tasks/"):
                pending.add(rel_p)
    elif subcmd in ("mv", "rm"):
        for raw_p in (a for a in args if a != "--" and not a.startswith("-")):
            rel_p = _to_repo_rel(raw_p, base_cwd, toplevel)
            pending.add(rel_p if rel_p is not None else raw_p)

def _validate_main_commit(args: Sequence[str], repo_info: dict[str, Any], git_cwd: Path | None, pending_staged: set[str]) -> str | None:
    toplevel: Path | None = repo_info.get("toplevel")
    branch = repo_info.get("current_branch") or "main"
    block_msg = (
        f"Direct 'git commit' on protected branch '{branch}' is restricted to non-empty 'docs/tasks/' ledger updates. "
        "Make code/doc changes in a worktree ('keel worktree create <slug>') and integrate via 'keel branch verify/land'."
    )
    if toplevel is None or any(a == "--pathspec-from-file" or a.startswith("--pathspec-from-file=") for a in args):
        return block_msg
    has_all, has_include, has_amend, pathspecs, idx, n = False, False, False, [], 0, len(args)
    val_long = ("--message", "--file", "--reedit-message", "--reuse-message", "--fixup", "--squash", "--author", "--date", "--cleanup", "--trailer")
    while idx < n:
        tok = args[idx]
        if tok == "--":
            pathspecs.extend(args[idx + 1 :])
            break
        if tok in ("-a", "--all", "-i", "--include", "--amend"):
            has_all, has_include, has_amend = (has_all or tok in ("-a", "--all")), (has_include or tok in ("-i", "--include")), (has_amend or tok == "--amend")
            idx += 1
        elif tok in (*val_long, "-m", "-F", "-c", "-C"):
            idx += 2
        elif tok.startswith("--"):
            idx += 1
        elif tok.startswith("-") and len(tok) > 1:
            cut = min((tok.find(c, 1) for c in "mFcCSu" if tok.find(c, 1) != -1), default=len(tok))
            flags_part = tok[1:cut]
            has_all, has_include = (has_all or "a" in flags_part), (has_include or "i" in flags_part)
            idx += 2 if (cut < len(tok) and tok[cut] in "mFcC" and cut + 1 == len(tok)) else 1
        else:
            pathspecs.append(tok)
            idx += 1
    eff_cwd = git_cwd or toplevel
    staged = set(_git_z(toplevel, ["diff", "--no-renames", "--cached", "--name-only", "-z"])) | {p for p in pending_staged if p}
    if pathspecs:
        rel_specs = [_to_repo_rel(p, eff_cwd, toplevel) for p in pathspecs]
        if any(r is None or not (r == "docs/tasks" or r.startswith("docs/tasks/")) for r in rel_specs):
            return block_msg
        specs_ok = [r for r in rel_specs if r is not None]
        touched = set(_git_z(toplevel, ["diff", "--no-renames", "HEAD", "--name-only", "-z", "--", *specs_ok])) | set(_git_z(toplevel, ["diff", "--no-renames", "--cached", "--name-only", "-z", "--", *specs_ok]))
        touched.update(p for p in pending_staged if p and any(p == sp or p.startswith(sp.rstrip("/") + "/") for sp in specs_ok))
        touched.update(staged if has_include else set())
    else:
        touched = (staged | set(_git_z(toplevel, ["diff", "--no-renames", "--name-only", "-z"]))) if has_all else staged
    if has_amend:
        touched.update(_git_z(toplevel, ["diff-tree", "--root", "--no-renames", "--no-commit-id", "--name-only", "-r", "-z", "HEAD"]))
    return None if (touched and all(p.startswith("docs/tasks/") for p in touched)) else block_msg

def _unwrap_python_keel_invocation(seg: list[str]) -> list[str]:
    if not Path(seg[0]).name.startswith("python"):
        return seg
    idx, n = 1, len(seg)
    while idx < n:
        if seg[idx] in ("-W", "-X", "--check-hash-based-pycs") and idx + 1 < n:
            idx += 2
        elif seg[idx] in ("-c", "-m"):
            return seg
        elif seg[idx].startswith("-"):
            idx += 1
        elif Path(seg[idx]).name == "keel":
            return seg[idx:]
        else:
            break
    return seg


def evaluate_command(
    command: str,
    *,
    mode: str = "main",
    default_branches: frozenset[str] = DEFAULT_BRANCHES,
    cwd: str | os.PathLike[str] | None = None,
) -> str | None:
    """Return a concrete refusal reason if `command` violates pure-Git safety boundaries, else None."""
    if not command or not command.strip():
        return None
    tracked_cwd: Path = Path(cwd) if cwd else Path.cwd()
    pending_staged_by_repo: dict[Path, set[str]] = {}

    for seg in extract_command_segments(command):
        if not seg:
            continue
        prog = Path(seg[0]).name
        if prog == "cd" and len(seg) >= 2 and not seg[1].startswith("-"):
            tracked_cwd = Path(seg[1]) if Path(seg[1]).is_absolute() else (tracked_cwd / seg[1])
            continue
        keel_seg = _unwrap_python_keel_invocation(seg)
        if mode in ("read", "write") and Path(keel_seg[0]).name == "keel" and len(keel_seg) >= 3:
            domain, action, rest = keel_seg[1], keel_seg[2], keel_seg[3:]
            if domain == "branch" and action == "land" and any(r in ("--execute", "--discard") or r.startswith(("--execute=", "--discard=")) for r in rest):
                return "Subagents cannot execute or discard branches via 'keel branch land'; return to the main session."
            if domain == "worktree" and action == "cleanup" and any(r == "--discard" or r.startswith("--discard=") for r in rest):
                return "Subagents cannot run 'keel worktree cleanup --discard'; return to the main session."
            if mode == "read" and ((domain == "branch" and action in ("record-verdict", "verify")) or (domain == "worktree" and action in ("create", "cleanup"))):
                return f"Read-only subagent mode blocks 'keel {domain} {action}'."
        if prog != "git":
            continue
        subcmd, git_args, git_cwd, explicit_git_dir, explicit_work_tree, cli_overrides = _git_context_and_subcommand(seg, tracked_cwd)
        if subcmd is None:
            continue
        if subcmd == "push":
            push_reason = _git_push_reason(git_args, mode, cli_overrides)
            if push_reason:
                return push_reason
            continue
        repo_info = _inspect_git_repo(git_cwd, explicit_git_dir, explicit_work_tree)
        if repo_info is None:
            continue
        if mode == "read":
            if not repo_info["is_disposable_verify"] and _is_mutating_git(subcmd, git_args):
                return f"Read-only subagent mode blocks mutating Git command 'git {subcmd}' outside disposable verify clones."
            continue
        if mode == "write":
            if _targets_protected_branch(subcmd, git_args, default_branches):
                return "Write-mode subagents cannot target or mutate protected default branches ('main'/'master')."
            if _is_mutating_git(subcmd, git_args):
                if not repo_info["is_linked_worktree"]:
                    return "Write-mode subagents cannot run mutating Git commands in the primary checkout; operate strictly inside '.agents/worktrees/<slug>'."
                if repo_info["current_branch"] in default_branches:
                    return f"Write-mode subagents cannot mutate protected default branch '{repo_info['current_branch']}'."
            continue
        if mode == "main" and repo_info["current_branch"] in default_branches:
            cur_b, top = repo_info["current_branch"], repo_info.get("toplevel")
            if top is not None and subcmd in ("add", "mv", "rm"):
                _record_pending_staging(subcmd, git_args, git_cwd or tracked_cwd, top.resolve(strict=False), pending_staged_by_repo.setdefault(top.resolve(strict=False), set()))
                continue
            if subcmd in ("merge", "rebase", "cherry-pick"):
                return f"Direct 'git {subcmd}' on protected branch '{cur_b}' is blocked. Integrate verified worktree branches via 'keel branch land'."
            if subcmd == "reset" and "--hard" in git_args:
                return f"Destructive 'git reset --hard' on protected branch '{cur_b}' is blocked."
            if subcmd == "pull" and "--ff-only" not in git_args:
                return f"Direct 'git pull' on '{cur_b}' requires '--ff-only' (e.g., 'git pull --ff-only')."
            if subcmd == "commit":
                pending = pending_staged_by_repo.get(top.resolve(strict=False), set()) if top is not None else set()
                commit_reason = _validate_main_commit(git_args, repo_info, git_cwd, pending)
                if commit_reason:
                    return commit_reason
    return None
