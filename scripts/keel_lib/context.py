"""Context engineering assembler and local run record management."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
from typing import Any

from . import KeelError, PRESETS_DIR
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


def find_repo_agent_doc(project_root: Path | None) -> tuple[str, str] | None:
    if project_root is None:
        return None
    for name in ROOT_AGENT_DOCS:
        candidate = project_root / name
        if candidate.is_file():
            return name, candidate.read_text(encoding="utf-8").strip()
    return None


def assemble_brief(
    *,
    why: str,
    task: str,
    preset: str,
    mode: str,
    workdir: Path,
    project_root: Path | None,
    native_agent_docs: tuple[str, ...] = (),
    schema_path: Path | None = None,
    output_path: Path | None = None,
    inject_schema_in_brief: bool = False,
) -> str:
    if not why.strip():
        raise KeelError("`--why` is required: state in 1-3 sentences how this task serves the project's Core need.")
    if not task.strip():
        raise KeelError("`--task` (or `--task-file`) is required.")

    sections: list[str] = ["# Subagent Assignment"]

    # Inject repo agent doc iff the executor does NOT natively load a doc already present in `workdir`
    natively_loaded = any((workdir / name).is_file() for name in native_agent_docs)
    if not natively_loaded:
        found = find_repo_agent_doc(project_root)
        if found is not None:
            doc_name, doc_body = found
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

    return "\n\n".join(sections) + "\n"


def runs_root_dir(project_root: Path | None, fallback_dir: Path) -> Path:
    if project_root is not None:
        return project_root / ".agents" / "runs"
    return fallback_dir / "keel-runs"


def save_run_record(run_dir: Path, record: dict[str, Any]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    meta_path = run_dir / "run.json"
    meta_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return meta_path


def load_run_record(project_root: Path | None, run_id: str, fallback_dir: Path) -> tuple[Path, dict[str, Any]]:
    run_dir = runs_root_dir(project_root, fallback_dir) / run_id
    meta_path = run_dir / "run.json"
    if not meta_path.is_file():
        raise KeelError(f"Run record not found: {meta_path}")
    return run_dir, json.loads(meta_path.read_text(encoding="utf-8"))
