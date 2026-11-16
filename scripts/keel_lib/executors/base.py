"""Abstract pluggable executor contract (`1..N` side-agent backends)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class RunRequest:
    run_id: str
    run_dir: Path
    brief_path: Path
    workdir: Path
    project_root: Path | None
    mode: str  # "read" | "write"
    preset: str
    model: str | None
    effort: str | None
    schema_path: Path | None
    output_path: Path
    agy_workspace: str = "inherit"
    extra_git_dirs: tuple[Path, ...] = ()


class ExecutorAdapter(ABC):
    """Minimal interface to add any new side-agent executor (Codex, Antigravity, Claude CLI, SDK, etc.)."""

    name: str = ""
    # Root instruction filenames this runtime natively discovers and loads from `<workdir>` without prompt injection
    native_agent_docs: tuple[str, ...] = ()
    # True when structured output schema must be written to file by the agent itself (vs enforced by CLI flag)
    inject_schema_in_brief: bool = True

    @abstractmethod
    def execute(self, req: RunRequest, defaults: dict[str, Any]) -> dict[str, Any]:
        """Start a fresh subagent run and return structured run metadata/output."""

    @abstractmethod
    def resume(
        self,
        run_dir: Path,
        record: dict[str, Any],
        followup_path: Path,
        output_path: Path,
        defaults: dict[str, Any],
    ) -> dict[str, Any]:
        """Continue an existing subagent session with a follow-up prompt file."""
