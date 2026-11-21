"""Abstract pluggable executor contract (`1..N` side-agent backends)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import KeelError
from ..env import run_cmd, same_path
from ..worktree import git_dirs


class KeelConfigError(KeelError):
    """The requested routing or its configured model mapping is invalid."""


class KeelExecutionError(KeelError):
    """An executor failed after producing metadata that can be saved for a retry."""

    def __init__(self, message: str, partial: dict[str, Any]) -> None:
        super().__init__(message)
        self.partial = {**partial, "status": "failed"}


VALID_LEVELS = ("L1", "L2", "L3", "L4")


def normalize_level(level: str | None, *, defaults: dict[str, Any] | None = None) -> str:
    normalized = level.strip().upper() if isinstance(level, str) else ""
    if normalized not in VALID_LEVELS:
        raise KeelConfigError(f"Expected --level L1, L2, L3, or L4, got {level!r}.")
    if defaults is not None:
        levels = defaults.get("levels")
        if not isinstance(levels, list) or normalized not in levels:
            raise KeelConfigError(f"--level {normalized} is missing from the levels list in defaults.json.")
    return normalized


def level_rank(level: str) -> int:
    return VALID_LEVELS.index(normalize_level(level)) + 1


def max_level(first: str, *others: str) -> str:
    return normalize_level(max((first, *others), key=level_rank))


def escalate_level(level: str) -> str:
    return VALID_LEVELS[min(level_rank(level), len(VALID_LEVELS) - 1)]


def enforce_verify_min_level(level: str, defaults: dict[str, Any], *, preset: str = "verify") -> None:
    minimum = normalize_level(defaults.get("verify_min_level"))
    if level_rank(level) < level_rank(minimum):
        raise KeelConfigError(f"The {preset} preset requires --level {minimum} or higher, got {level}.")


def resolve_executor_level_config(
    executor_name: str, level: str | None, defaults: dict[str, Any]
) -> tuple[str, str, str | None]:
    level = normalize_level(level)
    path = f"executors.{executor_name}.levels.{level}"
    try:
        entry = defaults["executors"][executor_name]["levels"][level]
    except (KeyError, TypeError) as exc:
        raise KeelConfigError(f"Missing level mapping at {path} in defaults.json.") from exc
    if not isinstance(entry, dict):
        raise KeelConfigError(f"Expected a level mapping at {path}.")
    model, effort = entry.get("model"), entry.get("effort")
    if not isinstance(model, str) or not model.strip():
        raise KeelConfigError(f"Expected a model at {path}.model.")
    if effort is not None or executor_name == "codex":
        if not isinstance(effort, str) or not effort.strip():
            raise KeelConfigError(f"Expected a reasoning effort at {path}.effort.")
        effort = effort.strip()
    return level, model.strip(), effort


def resolve_git_add_dirs(workdir: Path, extra_git_dirs: tuple[Path, ...] = ()) -> list[str]:
    """Build deduplicated --add-dir flags for write access to Git metadata directories."""
    dirs: list[Path] = list(extra_git_dirs)
    chk = run_cmd(["git", "rev-parse", "--is-inside-work-tree"], cwd=workdir, check=False)
    if chk.returncode == 0:
        common_dir, git_dir = git_dirs(workdir)
        dirs.extend([common_dir, git_dir])

    flags: list[str] = []
    seen: list[Path] = []
    for d in dirs:
        if any(same_path(d, prev) for prev in seen):
            continue
        seen.append(d)
        flags.extend(["--add-dir", str(d)])
    return flags


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
    scratch_verify: bool = False
    level: str | None = None
    why: str | None = None

    def run_record(self, executor: str) -> dict[str, Any]:
        """Metadata shared by the turn-one hook, persisted run and warm resume."""
        rec: dict[str, Any] = {
            "run_id": self.run_id,
            "executor": executor,
            "preset": self.preset,
            "mode": self.mode,
            "level": self.level,
            "workdir": str(self.workdir),
            "project_root": str(self.project_root) if self.project_root else None,
            "brief_path": str(self.brief_path),
            "output_path": str(self.output_path),
            "schema_path": str(self.schema_path) if self.schema_path else None,
            "extra_git_dirs": [str(path) for path in self.extra_git_dirs],
            "scratch_verify": self.scratch_verify,
        }
        if self.why is not None:
            rec["why"] = self.why
        return rec


class ExecutorAdapter(ABC):
    """Minimal interface to add any new side-agent executor (Codex, Antigravity, Claude CLI, SDK, etc.)."""

    name: str = ""
    # Root instruction filenames this runtime natively discovers and loads without prompt injection
    native_agent_docs: tuple[str, ...] = ()
    # True when host runtime natively injects `rules/AGENTS.md` into `<user_rules>` (e.g., Antigravity plugin)
    loads_plugin_spine: bool = False
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
        *,
        level: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        """Continue an existing subagent session with a follow-up prompt file."""
