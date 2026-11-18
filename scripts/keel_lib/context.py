"""Context engineering assembler and local run record management."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import tempfile
from typing import Any

from . import KeelError, PLUGIN_ROOT, PRESETS_DIR
from .env import is_subpath
from .worktree import ROOT_AGENT_DOCS


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
                return name, candidate.read_text(encoding="utf-8").strip()
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
    why: str,
    task: str,
    preset: str,
    mode: str,
    workdir: Path,
    project_root: Path | None,
    native_agent_docs: tuple[str, ...] = (),
    loads_plugin_spine: bool = False,
    caller_cwd: Path | None = None,
    schema_path: Path | None = None,
    output_path: Path | None = None,
    inject_schema_in_brief: bool = False,
) -> str:
    if not why.strip():
        raise KeelError("`--why` is required: state in 1-3 sentences how this task serves the project's Core need.")
    if not task.strip():
        raise KeelError("`--task` (or `--task-file`) is required.")

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
    sections.append(f"## Why (Global Alignment)\n\n{why.strip()}")
    sections.append(f"## Task, Scope & Done Criteria\n\n{task.strip()}")
    sections.append(load_preset_text(preset))

    if schema_path is not None:
        schema_text = schema_path.read_text(encoding="utf-8").strip()
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
