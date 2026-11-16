"""OpenAI Codex CLI (`codex-cli >= 0.169`) non-interactive executor adapter."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
from typing import Any

from .. import KeelError, PLUGIN_ROOT
from ..env_gh import run_cmd, run_process_group, same_path, tool_env
from ..worktree import git_dirs
from .base import ExecutorAdapter, RunRequest

_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def discover_mcp_approval_flags(workdir: Path) -> list[str]:
    """Build validated `-c mcp_servers.<name>.default_tools_approval_mode="approve"` flags."""
    proc = run_cmd(["codex", "mcp", "list", "--json"], cwd=workdir, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        items = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []

    names = [
        item["name"]
        for item in items
        if isinstance(item, dict)
        and item.get("enabled") is True
        and isinstance(item.get("name"), str)
        and _MCP_NAME_RE.fullmatch(item["name"])
    ]
    if not names:
        return []

    def _flag_pair(server_name: str) -> list[str]:
        return ["-c", f'mcp_servers.{server_name}.default_tools_approval_mode="approve"']

    batch: list[str] = []
    for n in names:
        batch.extend(_flag_pair(n))

    # Fast batch check; fall back to individual checks if a plugin-managed server rejects overrides
    probe = run_cmd(["codex", "mcp", "get", names[0], "--json", *batch], cwd=workdir, check=False)
    if probe.returncode == 0:
        return batch

    accepted: list[str] = []
    for n in names:
        pair = _flag_pair(n)
        single = run_cmd(["codex", "mcp", "get", n, "--json", *pair], cwd=workdir, check=False)
        if single.returncode == 0:
            accepted.extend(pair)
    return accepted


def resolve_git_add_dirs(workdir: Path, extra_git_dirs: tuple[Path, ...] = ()) -> list[str]:
    """Grant Codex `workspace-write` access to split worktree / separate-git-dir metadata directories."""
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


def inline_hook_flags() -> list[str]:
    hook_cmd = shlex.quote(str(PLUGIN_ROOT / "hooks" / "pre-tool-use"))
    hook_toml = f'hooks.PreToolUse=[{{matcher=".*",hooks=[{{type="command",command="{hook_cmd}",timeout=10}}]}}]'
    return [
        "-c",
        "features.hooks=true",
        "-c",
        hook_toml,
        "--dangerously-bypass-hook-trust",
    ]


def extract_session_id_from_event(line: str) -> str | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    for key in ("thread_id", "session_id", "conversation_id"):
        val = obj.get(key)
        if isinstance(val, str) and _UUID_RE.fullmatch(val):
            return val
    return None


class CodexExecutor(ExecutorAdapter):
    name = "codex"
    native_agent_docs = ("AGENTS.md",)
    inject_schema_in_brief = False  # Enforced natively via `codex exec --output-schema`

    def _resolve_model_effort(
        self,
        preset: str,
        model: str | None,
        effort: str | None,
        defaults: dict[str, Any],
    ) -> tuple[str, str]:
        cfg = defaults.get("executors", {}).get("codex", {})
        preset_cfg = cfg.get("presets", {}).get(preset, cfg.get("presets", {}).get("general", {}))
        resolved_model = model or preset_cfg.get("model", "gpt-6.1-sol")
        resolved_effort = effort or preset_cfg.get("effort", "high")
        return resolved_model, resolved_effort

    def build_exec_argv(
        self,
        *,
        workdir: Path,
        mode: str,
        model: str,
        effort: str,
        output_path: Path,
        defaults: dict[str, Any],
        schema_path: Path | None = None,
        extra_git_dirs: tuple[Path, ...] = (),
        ephemeral: bool = False,
        resume_session_id: str | None = None,
    ) -> list[str]:
        sub_cfg = defaults.get("executors", {}).get("codex", {}).get("subagent_default", {})
        sub_model = sub_cfg.get("model", "gpt-6.1-terra")
        sub_effort = sub_cfg.get("effort", "high")

        # Top-level options MUST precede `resume` in codex-cli 0.169+
        argv: list[str] = [
            "codex",
            "exec",
            "-s",
            "workspace-write",
            "-C",
            str(workdir),
            *resolve_git_add_dirs(workdir, extra_git_dirs),
            "-c",
            "sandbox_workspace_write.network_access=true",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            "-c",
            f'agents.default_subagent_model="{sub_model}"',
            "-c",
            f'agents.default_subagent_reasoning_effort="{sub_effort}"',
            *inline_hook_flags(),
            *discover_mcp_approval_flags(workdir),
        ]

        in_git = run_cmd(["git", "rev-parse", "--is-inside-work-tree"], cwd=workdir, check=False).returncode == 0
        if not in_git:
            argv.append("--skip-git-repo-check")
        if ephemeral:
            argv.append("--ephemeral")

        if resume_session_id:
            argv.extend(["resume", resume_session_id])

        if schema_path is not None:
            argv.extend(["--output-schema", str(schema_path)])

        argv.extend(["-o", str(output_path), "--json", "-"])
        return argv

    def execute(self, req: RunRequest, defaults: dict[str, Any]) -> dict[str, Any]:
        model, effort = self._resolve_model_effort(req.preset, req.model, req.effort, defaults)
        ephemeral = req.mode == "read" and req.preset == "verify"
        argv = self.build_exec_argv(
            workdir=req.workdir,
            mode=req.mode,
            model=model,
            effort=effort,
            output_path=req.output_path,
            defaults=defaults,
            schema_path=req.schema_path,
            extra_git_dirs=req.extra_git_dirs,
            ephemeral=ephemeral,
        )
        events_path = req.run_dir / "events.jsonl"
        session_id: str | None = None

        with events_path.open("w", encoding="utf-8") as events_fh:
            def _handle_line(line: str) -> None:
                nonlocal session_id
                events_fh.write(line)
                if session_id is None:
                    session_id = extract_session_id_from_event(line)

            env = tool_env({"KEEL_AGENT_MODE": req.mode})
            rc, stderr_text = run_process_group(
                argv,
                cwd=req.workdir,
                env=env,
                stdin_path=req.brief_path,
                on_stdout_line=_handle_line,
            )

        if rc != 0:
            raise KeelError(f"Codex run '{req.run_id}' exited with code {rc}:\n{stderr_text.strip()}".strip())

        final_text = req.output_path.read_text(encoding="utf-8") if req.output_path.is_file() else ""
        return {
            "status": "completed",
            "executor": self.name,
            "model": model,
            "effort": effort,
            "session_id": session_id,
            "output_path": str(req.output_path),
            "events_path": str(events_path),
            "extra_git_dirs": [str(p) for p in req.extra_git_dirs],
            "output_preview": final_text[:1000],
        }

    def resume(
        self,
        run_dir: Path,
        record: dict[str, Any],
        followup_path: Path,
        output_path: Path,
        defaults: dict[str, Any],
    ) -> dict[str, Any]:
        session_id = record.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise KeelError(f"Run '{record.get('run_id')}' has no persisted Codex session_id to resume.")

        workdir = Path(record["workdir"])
        if not workdir.is_dir():
            raise KeelError(f"Run workdir no longer exists: {workdir}")

        extra_dirs = tuple(Path(p) for p in record.get("extra_git_dirs", []))
        schema_str = record.get("schema_path")
        schema_path = Path(schema_str) if isinstance(schema_str, str) and schema_str else None
        argv = self.build_exec_argv(
            workdir=workdir,
            mode=record["mode"],
            model=record["model"],
            effort=record["effort"],
            output_path=output_path,
            defaults=defaults,
            schema_path=schema_path,
            extra_git_dirs=extra_dirs,
            resume_session_id=session_id,
        )

        events_path = run_dir / "events-resume.jsonl"
        with events_path.open("a", encoding="utf-8") as events_fh:
            env = tool_env({"KEEL_AGENT_MODE": record["mode"]})
            rc, stderr_text = run_process_group(
                argv,
                cwd=workdir,
                env=env,
                stdin_path=followup_path,
                on_stdout_line=events_fh.write,
            )

        if rc != 0:
            raise KeelError(f"Codex resume for '{record.get('run_id')}' exited with code {rc}:\n{stderr_text.strip()}".strip())

        final_text = output_path.read_text(encoding="utf-8") if output_path.is_file() else ""
        return {
            "status": "completed",
            "executor": self.name,
            "session_id": session_id,
            "output_path": str(output_path),
            "output_preview": final_text[:1000],
        }
