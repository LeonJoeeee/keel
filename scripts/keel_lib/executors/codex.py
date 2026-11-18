"""OpenAI Codex CLI (`codex-cli >= 0.169`) non-interactive executor adapter."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
from typing import Any

from .. import KeelError, PLUGIN_ROOT
from ..env import run_cmd, run_process_group, tool_env
from .base import (
    ExecutorAdapter,
    KeelConfigError,
    KeelExecutionError,
    RunRequest,
    enforce_verify_min_level,
    normalize_level,
    resolve_executor_level_config,
    resolve_git_add_dirs,
)

_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
VALID_CODEX_SANDBOX_MODES = frozenset({"read-only", "workspace-write", "danger-full-access"})


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


def _override(value: str | None, default: str) -> str:
    clean = (value or "").strip()
    return default if clean in ("", "inherit") else clean


class CodexExecutor(ExecutorAdapter):
    name = "codex"
    native_agent_docs = ("AGENTS.md",)
    loads_plugin_spine = False
    inject_schema_in_brief = False  # Enforced natively via `codex exec --output-schema`

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
        sandbox_mode: str | None = None,
        allow_disposable_write: bool = False,
    ) -> list[str]:
        if sandbox_mode is not None:
            effective_sandbox = sandbox_mode
        elif mode == "read" and not allow_disposable_write:
            effective_sandbox = "read-only"
        else:
            effective_sandbox = "workspace-write"
        if effective_sandbox not in VALID_CODEX_SANDBOX_MODES:
            raise KeelError(
                f"Invalid Codex sandbox mode '{effective_sandbox}'. Valid choices: {sorted(VALID_CODEX_SANDBOX_MODES)}"
            )

        sub_cfg = ((defaults.get("executors") or {}).get("codex") or {}).get("subagent_default") or {}
        raw_sub_model = str(sub_cfg.get("model") or "inherit").strip()
        raw_sub_effort = str(sub_cfg.get("effort") or "inherit").strip()
        sub_model = model if raw_sub_model in ("", "inherit") else raw_sub_model
        sub_effort = effort if raw_sub_effort in ("", "inherit") else raw_sub_effort

        # Top-level options MUST precede `resume` in codex-cli 0.169+
        argv: list[str] = [
            "codex",
            "exec",
            "-s",
            effective_sandbox,
            "-C",
            str(workdir),
        ]
        if effective_sandbox == "workspace-write":
            argv.extend(resolve_git_add_dirs(workdir, extra_git_dirs))
            argv.extend(["-c", "sandbox_workspace_write.network_access=true"])

        argv.extend(
            [
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
        )

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
        requested_level = "L3" if req.is_legacy_scratch_verify else req.level
        level, model, effort = resolve_executor_level_config(self.name, requested_level, defaults)
        if req.preset == "verify":
            enforce_verify_min_level(level, defaults)
        assert effort is not None  # Required by the Codex level mapping
        model = _override(req.model, model)
        effort = _override(req.effort, effort)
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
            allow_disposable_write=req.scratch_verify,
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

        metadata = {
            "executor": self.name,
            "level": level,
            "model": model,
            "effort": effort,
            "session_id": session_id,
            "ephemeral": ephemeral,
            "output_path": str(req.output_path),
            "events_path": str(events_path),
            "extra_git_dirs": [str(p) for p in req.extra_git_dirs],
            "scratch_verify": req.scratch_verify,
        }
        if rc != 0:
            raise KeelExecutionError(f"Codex run '{req.run_id}' exited with code {rc}:\n{stderr_text.strip()}", metadata)
        final_text = req.output_path.read_text(encoding="utf-8") if req.output_path.is_file() else ""
        return {**metadata, "status": "completed", "output_preview": final_text[:1000]}

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
        if record.get("ephemeral") or (
            "ephemeral" not in record and record.get("mode") == "read" and record.get("preset") == "verify"
        ):
            raise KeelConfigError("Ephemeral Codex verification sessions cannot be resumed.")
        session_id = record.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise KeelError(f"Run '{record.get('run_id')}' has no persisted Codex session_id to resume.")

        workdir = Path(record["workdir"])
        if not workdir.is_dir():
            raise KeelError(f"Run workdir no longer exists: {workdir}")

        if level is None:
            # A warm session keeps the actual settings, including overrides and pre-ladder records.
            level = record.get("level")
            if level is not None:
                level = normalize_level(level)
            level_model, level_effort = record["model"], record["effort"]
        else:
            level, level_model, level_effort = resolve_executor_level_config(self.name, level, defaults)
        if level is not None and record.get("preset") == "verify":
            enforce_verify_min_level(level, defaults)
        assert level_effort is not None
        model = _override(model, level_model)
        effort = _override(effort, level_effort)
        extra_dirs = tuple(Path(p) for p in record.get("extra_git_dirs", []))
        schema_str = record.get("schema_path")
        schema_path = Path(schema_str) if isinstance(schema_str, str) and schema_str else None
        argv = self.build_exec_argv(
            workdir=workdir,
            mode=record["mode"],
            model=model,
            effort=effort,
            output_path=output_path,
            defaults=defaults,
            schema_path=schema_path,
            extra_git_dirs=extra_dirs,
            resume_session_id=session_id,
            allow_disposable_write=bool(record.get("scratch_verify")),
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

        metadata = {
            "executor": self.name,
            "model": model,
            "effort": effort,
            "session_id": session_id,
            "output_path": str(output_path),
        }
        if level is not None:
            metadata["level"] = level
        if rc != 0:
            raise KeelExecutionError(
                f"Codex resume for '{record.get('run_id')}' exited with code {rc}:\n{stderr_text.strip()}", metadata
            )
        final_text = output_path.read_text(encoding="utf-8") if output_path.is_file() else ""
        return {**metadata, "status": "completed", "output_preview": final_text[:1000]}
