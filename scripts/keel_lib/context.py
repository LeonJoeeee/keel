"""Context engineering assembler and local run record management."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from typing import Any, Sequence

from . import KeelError, PLUGIN_ROOT, PRESETS_DIR
from .env import is_subpath
from .worktree import ROOT_AGENT_DOCS

WHY_HEADINGS: tuple[str, ...] = ("Goal & Why", "Why")
REVIEW_HISTORY_HEADING_SUBSTRINGS: tuple[str, ...] = (
    "prior review",
    "review carry-forward",
    "review carry forward",
    "review ledger",
    "review rebuttal",
    "author rebuttal",
    "technical rebuttal",
    "disputed finding",
)
_BQ_PREFIX_RE = re.compile(r"^ {0,3}> ?")
_THEMATIC_BREAK_RE = re.compile(r"^ {0,3}(?:(?:- *){3,}|(?:\* *){3,}|(?:_ *){3,})$")
_SETEXT_UNDERLINE_RE = re.compile(r"^ {0,3}(?:=+|-+) *$")
_LEADING_BLANK_LINES_RE = re.compile(r"^(?:[ \t]*\r?\n)+")
_LIST_MARKER_RE = re.compile(r"^(?P<lead> {0,3})(?P<marker>[-*+]|\d{1,9}[.)])(?:(?P<sep> +)(?P<after>.*)|$)")
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,}) *$")
_ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?: +(.*?))? *$")
_HTML_BLOCK_NAMES = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|"
    "dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|"
    "frameset|h1|h2|h3|h4|h5|h6|head|header|hr|html|iframe|legend|li|link|main|menu|"
    "menuitem|nav|noframes|ol|optgroup|option|p|param|section|source|summary|table|"
    "tbody|td|tfoot|th|thead|title|tr|track|ul"
)
_HTML_OPEN_CLOSE_TAG_RE = re.compile(
    r"^(?:<[A-Za-z][A-Za-z0-9\-]*(?:\s+[a-zA-Z_:][a-zA-Z0-9:._-]*"
    r"(?:\s*=\s*(?:[^\"'=<>`\x00-\x20]+|'[^']*'|\"[^\"]*\"))?)*\s*/?>|"
    r"</[A-Za-z][A-Za-z0-9\-]*\s*>) *$"
)
# CommonMark §4.6 HTML block sequences: (open_re, close_re_or_None_for_blank_line, can_interrupt_paragraph)
_HTML_BLOCK_SEQUENCES: tuple[tuple[re.Pattern[str], re.Pattern[str] | None, bool], ...] = (
    (
        re.compile(r"^<(?:script|pre|style|textarea)(?: |>|$)", re.IGNORECASE),
        re.compile(r"</(?:script|pre|style|textarea)>", re.IGNORECASE),
        True,
    ),
    (re.compile(r"^<!--"), re.compile(r"-->"), True),
    (re.compile(r"^<\?"), re.compile(r"\?>"), True),
    (re.compile(r"^<![A-Z]"), re.compile(r">"), True),
    (re.compile(r"^<!\[CDATA\["), re.compile(r"\]\]>"), True),
    (re.compile(rf"^</?(?:{_HTML_BLOCK_NAMES})(?: |/?>|$)", re.IGNORECASE), None, True),
    (_HTML_OPEN_CLOSE_TAG_RE, None, False),
)


def _normalize_heading(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _expand_tabs(line: str) -> str:
    if "\t" not in line:
        return line
    out: list[str] = []
    col = 0
    for ch in line:
        if ch == "\t":
            pad = 4 - (col % 4)
            out.append(" " * pad)
            col += pad
        else:
            out.append(ch)
            col += 1
    return "".join(out)


def trim_markdown_block(text: str) -> str:
    """Strip leading blank lines and trailing whitespace while preserving first-line horizontal indentation."""
    if not text or not text.strip():
        return ""
    return _LEADING_BLANK_LINES_RE.sub("", text).rstrip()


def read_utf8_file(path_arg: str | Path, flag_name: str) -> str:
    """Read a UTF-8 text file for a CLI flag, converting all filesystem/decode errors into `KeelError`."""
    path = Path(path_arg)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise KeelError(f"{flag_name} not found: {path_arg}") from exc
    except UnicodeDecodeError as exc:
        raise KeelError(f"{flag_name} '{path_arg}' must be valid UTF-8 text: {exc}") from exc
    except OSError as exc:
        raise KeelError(f"Unable to read {flag_name} '{path_arg}': {exc}") from exc
    return raw[1:] if raw.startswith("\ufeff") else raw


def _match_fence_open(rest: str) -> str | None:
    open_m = _FENCE_OPEN_RE.match(rest)
    if not open_m:
        return None
    marker, info = open_m.group(1), open_m.group(2)
    if marker[0] == "~" or "`" not in info:
        return marker
    return None


def _match_html_block_open(
    rest: str,
    *,
    interrupt_paragraph: bool,
) -> tuple[bool, re.Pattern[str] | None] | None:
    lead = len(rest) - len(rest.lstrip(" "))
    if lead >= 4:
        return None
    stripped = rest[lead:]
    if not stripped.startswith("<"):
        return None
    for open_re, close_re, can_interrupt in _HTML_BLOCK_SEQUENCES:
        if open_re.search(stripped):
            if interrupt_paragraph and not can_interrupt:
                return None
            return (True, close_re)
    return None


def _list_marker_interrupts_paragraph(lm: re.Match[str]) -> bool:
    after_s = lm.group("after")
    if not after_s or not after_s.strip(" "):
        return False
    marker_s = lm.group("marker")
    if marker_s in ("-", "*", "+"):
        return True
    return int(marker_s[:-1]) == 1


def _parse_markdown_sections(
    markdown: str,
    *,
    top_level_only: bool = True,
) -> list[tuple[int, str, str, int, int]]:
    """Return `(level, title, body, start_line, end_line)` for ATX headings outside code and HTML blocks."""
    if markdown.startswith("\ufeff"):
        markdown = markdown[1:]
    lines = markdown.splitlines()
    headings_found: list[tuple[int, int, str, int]] = []
    container_end_by_pos: dict[int, int] = {}
    # Active CommonMark containers: ("bq", 0, False) or ("list", indent_delta, started_empty)
    containers: list[tuple[str, int, bool]] = []
    # Open code fence: (container_depth, fence_char, fence_len)
    fence: tuple[int, str, int] | None = None
    indented_code_depth: int | None = None
    # Open CommonMark §4.6 HTML block: (container_depth, close_re_or_None_for_blank_line)
    html_block: tuple[int, re.Pattern[str] | None] | None = None
    in_paragraph = False

    for idx, line in enumerate(lines):
        rest = _expand_tabs(line)
        matched_depth = 0
        raw_blank_in_list_html = False
        for c_idx, (kind, indent, started_empty) in enumerate(containers):
            if kind == "bq":
                bq_m = _BQ_PREFIX_RE.match(rest)
                if not bq_m:
                    break
                rest = rest[bq_m.end() :]
                matched_depth += 1
            else:
                if not rest.strip(" "):
                    if started_empty:
                        break
                    if html_block is not None and len(rest) < indent:
                        raw_blank_in_list_html = True
                    rest = ""
                    matched_depth += 1
                else:
                    lead = len(rest) - len(rest.lstrip(" "))
                    if lead < indent:
                        break
                    if started_empty:
                        containers[c_idx] = ("list", indent, False)
                    rest = rest[indent:]
                    matched_depth += 1

        if html_block is not None:
            html_depth, close_re = html_block
            if matched_depth < html_depth or raw_blank_in_list_html:
                html_block = None
            elif close_re is None:
                if not rest.strip(" "):
                    html_block = None
                in_paragraph = False
                continue
            else:
                if close_re.search(rest):
                    html_block = None
                in_paragraph = False
                continue

        if indented_code_depth is not None:
            if matched_depth < indented_code_depth:
                indented_code_depth = None
            elif not rest.strip(" ") or (len(rest) - len(rest.lstrip(" "))) >= 4:
                in_paragraph = False
                continue
            else:
                indented_code_depth = None

        if fence is None and matched_depth < len(containers) and in_paragraph:
            is_interrupter = (
                not rest.strip(" ")
                or _BQ_PREFIX_RE.match(rest) is not None
                or _THEMATIC_BREAK_RE.match(rest) is not None
                or _ATX_HEADING_RE.match(rest) is not None
                or _match_fence_open(rest) is not None
                or _match_html_block_open(rest, interrupt_paragraph=True) is not None
                or _LIST_MARKER_RE.match(rest) is not None
            )
            if not is_interrupter:
                in_paragraph = True
                continue

        if matched_depth < len(containers):
            for h_pos, (_, _, _, h_depth) in enumerate(headings_found):
                if h_depth > matched_depth and h_pos not in container_end_by_pos:
                    container_end_by_pos[h_pos] = idx
            containers = containers[:matched_depth]
            in_paragraph = False
            if fence is not None and matched_depth < fence[0]:
                fence = None

        if fence is not None:
            _, fence_char, fence_len = fence
            close_m = _FENCE_CLOSE_RE.match(rest)
            if close_m and close_m.group(1)[0] == fence_char and len(close_m.group(1)) >= fence_len:
                fence = None
            in_paragraph = False
            continue

        if not rest.strip(" "):
            in_paragraph = False
            continue

        if in_paragraph and _SETEXT_UNDERLINE_RE.match(rest):
            in_paragraph = False
            continue

        entered_thematic_break = False
        started_empty_list = False
        while rest:
            if not in_paragraph and (len(rest) - len(rest.lstrip(" "))) >= 4:
                break
            bq_m = _BQ_PREFIX_RE.match(rest)
            if bq_m:
                containers.append(("bq", 0, False))
                in_paragraph = False
                rest = rest[bq_m.end() :]
                continue
            if _THEMATIC_BREAK_RE.match(rest):
                entered_thematic_break = True
                break
            lm = _LIST_MARKER_RE.match(rest)
            if lm:
                if in_paragraph and not _list_marker_interrupts_paragraph(lm):
                    break
                in_paragraph = False
                lead_s = lm.group("lead")
                marker_s = lm.group("marker")
                sep_s = lm.group("sep")
                after_s = lm.group("after")
                if not after_s or not after_s.strip(" "):
                    delta = len(lead_s) + len(marker_s) + 1
                    containers.append(("list", delta, True))
                    started_empty_list = True
                    rest = ""
                    break
                if len(sep_s) >= 5:
                    delta = len(lead_s) + len(marker_s) + 1
                    containers.append(("list", delta, False))
                    rest = " " * (len(sep_s) - 1) + after_s
                    break
                delta = len(lead_s) + len(marker_s) + len(sep_s)
                containers.append(("list", delta, False))
                rest = after_s
                continue
            break

        if entered_thematic_break or started_empty_list or not rest.strip(" "):
            in_paragraph = False
            continue

        if not in_paragraph and (len(rest) - len(rest.lstrip(" "))) >= 4:
            indented_code_depth = len(containers)
            in_paragraph = False
            continue

        fence_marker = _match_fence_open(rest)
        if fence_marker is not None:
            fence = (len(containers), fence_marker[0], len(fence_marker))
            in_paragraph = False
            continue

        html_open = _match_html_block_open(rest, interrupt_paragraph=in_paragraph)
        if html_open is not None:
            _, close_re = html_open
            in_paragraph = False
            if close_re is None or not close_re.search(rest.lstrip(" ")):
                html_block = (len(containers), close_re)
            continue

        head_m = _ATX_HEADING_RE.match(rest)
        if head_m:
            in_paragraph = False
            if not (top_level_only and containers):
                level = len(head_m.group(1))
                raw_title = re.sub(r"(?:^| +)#+$", "", head_m.group(2) or "").strip()
                headings_found.append((idx, level, raw_title, len(containers)))
            continue

        in_paragraph = True

    sections: list[tuple[int, str, str, int, int]] = []
    for pos, (start_idx, level, title, depth) in enumerate(headings_found):
        end_idx = container_end_by_pos.get(pos, len(lines))
        for next_idx, next_level, _, next_depth in headings_found[pos + 1 :]:
            if next_idx >= end_idx:
                break
            if next_depth < depth or (next_depth == depth and next_level <= level):
                end_idx = next_idx
                break
        body = trim_markdown_block("\n".join(lines[start_idx + 1 : end_idx]))
        sections.append((level, title, body, start_idx, end_idx))
    return sections


def extract_markdown_section(
    markdown: str,
    headings: Sequence[str],
    *,
    levels: Sequence[int] | None = (2,),
) -> str | None:
    """Extract the non-empty body under the first matching ATX heading outside fenced code blocks."""
    targets = {_normalize_heading(h) for h in headings if h and h.strip()}
    if not markdown or not targets:
        return None
    allowed_levels = set(levels) if levels is not None else None
    for level, title, body, _, _ in _parse_markdown_sections(markdown):
        if allowed_levels is not None and level not in allowed_levels:
            continue
        if _normalize_heading(title) in targets and body:
            return body
    return None


def _strip_markdown_sections(markdown: str, exclude_headings: Sequence[str]) -> str:
    """Strip ATX heading sections (and any nested subsections) whose normalized title matches `exclude_headings`."""
    excluded = {_normalize_heading(h) for h in exclude_headings if h and h.strip()}
    if not markdown or not excluded:
        return markdown
    clean_md = markdown[1:] if markdown.startswith("\ufeff") else markdown
    spans = [
        (start_idx, end_idx)
        for _, title, _, start_idx, end_idx in _parse_markdown_sections(clean_md)
        if _normalize_heading(title) in excluded
    ]
    if not spans:
        return markdown
    lines = clean_md.splitlines()
    kept = [line for idx, line in enumerate(lines) if not any(s <= idx < e for s, e in spans)]
    return "\n".join(kept)


def strip_review_history_sections(markdown: str) -> str:
    """Strip ATX sections whose heading title names prior review, carry-forward, or rebuttal history."""
    if not markdown or not markdown.strip():
        return ""
    clean_md = markdown[1:] if markdown.startswith("\ufeff") else markdown
    spans = [
        (start_idx, end_idx)
        for _, title, _, start_idx, end_idx in _parse_markdown_sections(clean_md, top_level_only=False)
        if any(tok in _normalize_heading(title) for tok in REVIEW_HISTORY_HEADING_SUBSTRINGS)
    ]
    if not spans:
        return trim_markdown_block(clean_md)
    lines = clean_md.splitlines()
    kept = [line for idx, line in enumerate(lines) if not any(s <= idx < e for s, e in spans)]
    return trim_markdown_block("\n".join(kept))


def extract_changed_markdown_sections(
    base_markdown: str,
    head_markdown: str,
    headings: Sequence[str],
    *,
    levels: Sequence[int] | None = (2, 3),
    exclude_headings: Sequence[str] = (),
) -> str | None:
    """Extract non-empty matching sections from `head_markdown` whose bodies differ from `base_markdown`."""
    excluded = {_normalize_heading(h) for h in exclude_headings if h and h.strip()}
    targets = {_normalize_heading(h) for h in headings if h and h.strip()} - excluded
    if not head_markdown or not targets:
        return None
    if excluded:
        if base_markdown:
            base_markdown = _strip_markdown_sections(base_markdown, exclude_headings)
        head_markdown = _strip_markdown_sections(head_markdown, exclude_headings)
        if not head_markdown:
            return None
    allowed_levels = set(levels) if levels is not None else None

    base_bodies: dict[str, set[str]] = {}
    for level, title, body, _, _ in _parse_markdown_sections(base_markdown):
        if allowed_levels is not None and level not in allowed_levels:
            continue
        key = _normalize_heading(title)
        if key in targets and body:
            base_bodies.setdefault(key, set()).add(body)

    kept_blocks: list[str] = []
    kept_spans: list[tuple[int, int]] = []
    for level, title, body, start_idx, end_idx in _parse_markdown_sections(head_markdown):
        if allowed_levels is not None and level not in allowed_levels:
            continue
        key = _normalize_heading(title)
        if key not in targets or not body:
            continue
        if any(k_start <= start_idx and end_idx <= k_end for k_start, k_end in kept_spans):
            continue
        if body in base_bodies.get(key, ()):
            continue
        kept_blocks.append(f"{'#' * level} {title}\n\n{body}")
        kept_spans.append((start_idx, end_idx))

    return "\n\n".join(kept_blocks) if kept_blocks else None


def resolve_why(why: str | None, task: str) -> str | None:
    """Return explicit `why` if non-empty, else extract level-2 `## Goal & Why` or `## Why` from `task`."""
    if why:
        raw_why = why[1:] if why.startswith("\ufeff") else why
        trimmed = trim_markdown_block(raw_why)
        if trimmed:
            return trimmed
    return extract_markdown_section(task, WHY_HEADINGS, levels=(2,))


def generate_run_id(prefix: str = "run") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{secrets.token_hex(3)}"


def load_preset_text(preset: str) -> str:
    path = PRESETS_DIR / f"{preset}.md"
    if not path.is_file():
        available = sorted(p.stem for p in PRESETS_DIR.glob("*.md"))
        raise KeelError(f"Unknown preset '{preset}'. Available presets: {', '.join(available)}")
    return path.read_text(encoding="utf-8").strip()


def load_universal_spine() -> str:
    """Extract universal sections 1–3 of `rules/AGENTS.md` for non-plugin executors (e.g., Codex CLI)."""
    spine_path = PLUGIN_ROOT / "rules" / "AGENTS.md"
    if not spine_path.is_file():
        return ""
    raw = spine_path.read_text(encoding="utf-8")
    start_idx = raw.find("## 1.")
    if start_idx == -1:
        return raw.strip()
    end_idx = raw.find("\n## 4.", start_idx)
    sliced = raw[start_idx:end_idx] if end_idx != -1 else raw[start_idx:]
    return sliced.strip()


def find_repo_agent_doc(project_root: Path | None, workdir: Path | None = None) -> tuple[str, str] | None:
    """Prefer reading agent doc (`AGENTS.md` / `GEMINI.md`) from `workdir` if present, else `project_root`."""
    for base in (workdir, project_root):
        if base is None:
            continue
        for name in ROOT_AGENT_DOCS:
            candidate = base / name
            if candidate.is_file():
                raw = candidate.read_text(encoding="utf-8")
                if raw.startswith("\ufeff"):
                    raw = raw[1:]
                return name, trim_markdown_block(raw)
    return None


def _is_agent_doc_natively_loaded(
    doc_name: str,
    *,
    workdir: Path,
    project_root: Path | None,
    caller_cwd: Path | None,
    native_agent_docs: tuple[str, ...],
    loads_plugin_spine: bool,
) -> bool:
    if doc_name not in native_agent_docs:
        return False
    if not loads_plugin_spine:
        # Standalone CLI executor (e.g. `codex -C <workdir>`) reads `<workdir>/<doc_name>` directly.
        return (workdir / doc_name).is_file()
    # Host-inherited executor (`agy`): `<user_rules>` only loads the parent session's active workspace (`caller_cwd`).
    eff_caller = caller_cwd if caller_cwd is not None else Path.cwd()
    if project_root is None:
        return False
    if not (is_subpath(eff_caller, project_root) and is_subpath(workdir, project_root)):
        return False
    proj_doc = project_root / doc_name
    work_doc = workdir / doc_name
    if not (proj_doc.is_file() and work_doc.is_file()):
        return False
    return work_doc.read_bytes() == proj_doc.read_bytes()


def assemble_brief(
    *,
    why: str | None = None,
    task: str,
    preset: str,
    mode: str,
    workdir: Path,
    project_root: Path | None,
    native_agent_docs: tuple[str, ...] = (),
    loads_plugin_spine: bool = False,
    caller_cwd: Path | None = None,
    extra_context: str | None = None,
    schema_path: Path | None = None,
    output_path: Path | None = None,
    inject_schema_in_brief: bool = False,
) -> str:
    if task.startswith("\ufeff"):
        task = task[1:]
    if not task.strip():
        raise KeelError("`--task` (or `--task-file`) is required.")
    resolved_why = resolve_why(why, task)
    if not resolved_why:
        raise KeelError(
            "Provide `--why` or include a non-empty `## Goal & Why` (or `## Why`) section in `--task` / `--task-file`."
        )

    sections: list[str] = ["# Subagent Assignment"]

    # Leak ② fix: include universal sections 1–3 of `rules/AGENTS.md` when executor does not load plugin spine
    if not loads_plugin_spine:
        spine = load_universal_spine()
        if spine:
            sections.append(f"## Engineering Spine\n\n{spine}")

    # Leak ① fix: inject repo agent doc whenever not genuinely natively loaded with identical bytes
    found = find_repo_agent_doc(project_root, workdir=workdir)
    if found is not None:
        doc_name, doc_body = found
        natively_loaded = _is_agent_doc_natively_loaded(
            doc_name,
            workdir=workdir,
            project_root=project_root,
            caller_cwd=caller_cwd,
            native_agent_docs=native_agent_docs,
            loads_plugin_spine=loads_plugin_spine,
        )
        if not natively_loaded:
            sections.append(f"## Project Context (`{doc_name}`)\n\n{doc_body}")

    sections.append(
        "## Execution Environment\n"
        f"- **Mode:** `{mode}`\n"
        f"- **Working directory:** `{workdir}` (perform all file reads, edits, and shell commands relative to or inside this path)"
    )
    sections.append(f"## Why (Global Alignment)\n\n{resolved_why}")
    clean_task = trim_markdown_block(task)
    sections.append(f"## Task, Scope & Done Criteria\n\n{clean_task}")
    if extra_context:
        raw_extra = extra_context[1:] if extra_context.startswith("\ufeff") else extra_context
        clean_extra = trim_markdown_block(raw_extra)
        if clean_extra:
            sections.append(f"## Additional Context\n\n{clean_extra}")
    sections.append(load_preset_text(preset))

    if schema_path is not None:
        schema_text = read_utf8_file(schema_path, "--schema").strip()
        if inject_schema_in_brief and output_path is not None:
            sections.append(
                "## Required Structured Output\n\n"
                f"Write valid JSON matching the schema below to `{output_path}` before completing your turn:\n\n"
                f"```json\n{schema_text}\n```"
            )
        elif output_path is not None:
            sections.append(
                "## Output Contract\n\n"
                "Return valid JSON conforming strictly to the attached output schema."
            )
    elif not inject_schema_in_brief:
        sections.append(
            "## Output Contract\n\n"
            "Emit your concise final report directly as your final assistant message "
            "(it is captured automatically via CLI output). Create extra report files only when explicitly requested."
        )

    return "\n\n".join(sections) + "\n"


def runs_root_dir(project_root: Path | None, fallback_dir: Path) -> Path:
    if project_root is not None:
        return project_root / ".agents" / "runs"
    return fallback_dir / "keel-runs"


def save_run_record(run_dir: Path, record: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    meta_path = run_dir / "run.json"
    tmp = tempfile.NamedTemporaryFile(dir=run_dir, mode="w", encoding="utf-8", delete=False)
    try:
        with tmp:
            json.dump(record, tmp, indent=2, ensure_ascii=False)
            tmp.write("\n")
        os.replace(tmp.name, meta_path)
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    return meta_path


def load_run_record(project_root: Path | None, run_id: str, fallback_dir: Path) -> tuple[Path, dict[str, Any]]:
    run_dir = runs_root_dir(project_root, fallback_dir) / run_id
    meta_path = run_dir / "run.json"
    if not meta_path.is_file():
        raise KeelError(f"Run record not found: {meta_path}")
    return run_dir, json.loads(meta_path.read_text(encoding="utf-8"))
