"""Synchronous Google Antigravity print-mode executor."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from ..context import save_run_record
from ..env import run_process_group, tool_env
from .base import (
    ExecutorAdapter,
    KeelConfigError,
    KeelExecutionError,
    RunRequest,
    enforce_verify_min_level,
    resolve_executor_level_config,
    resolve_git_add_dirs,
)

_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def _validate_settings(model: str, effort: str | None) -> None:
    if model.lower() in {"inherit", "flash", "flash_lite", "pro"} or not re.fullmatch(r"[A-Za-z0-9._:-]+", model):
        raise KeelConfigError(f"Expected an Antigravity CLI model slug for --model, got {model!r}.")
    if effort is not None:
        if effort not in _EFFORTS:
            raise KeelConfigError(f"Invalid Antigravity --effort {effort!r}. Expected one of {sorted(_EFFORTS)}.")
        _, separator, suffix = model.rpartition("-")
        if separator and suffix in _EFFORTS and effort != suffix:
            raise KeelConfigError(f"--effort {effort} conflicts with the effort embedded in --model {model}.")


class AgyExecutor(ExecutorAdapter):
    name = "agy"
    native_agent_docs = ("AGENTS.md", "GEMINI.md")
    loads_plugin_spine = True
    inject_schema_in_brief = False

    def execute(self, req: RunRequest, defaults: dict[str, Any]) -> dict[str, Any]:
        level, model, effort = resolve_executor_level_config(self.name, req.level, defaults)
        if req.preset == "verify":
            enforce_verify_min_level(level, defaults)
        if req.agy_workspace != "inherit":
            raise KeelConfigError("Antigravity print mode uses the current workspace. Select a checkout with --worktree.")
        return self._run(req, level, model, effort)

    def _run(
        self, req: RunRequest, level: str, model: str, effort: str | None, *, session_id: str | None = None
    ) -> dict[str, Any]:
        if req.model is not None:
            model = req.model
            if model.rpartition("-")[2] in _EFFORTS:
                effort = None
        if req.effort is not None:
            effort = req.effort
        _validate_settings(model, effort)
        result_path = req.run_dir / (f"agy-resume-{uuid4().hex}.json" if session_id else "agy-result.json")
        record = {
            "executor": self.name, "level": level, "model": model, "effort": effort,
            "session_id": session_id, "ephemeral": req.mode == "read" and req.preset == "verify",
            "output_path": str(req.output_path), "result_path": str(result_path),
        }
        argv = ["agy"]
        if session_id:
            argv.extend(["--conversation", session_id])
        argv.extend(["--dangerously-skip-permissions", "--add-dir", str(req.run_dir)])
        if req.mode == "write" or req.scratch_verify:
            argv.extend(resolve_git_add_dirs(req.workdir, req.extra_git_dirs))
        argv.extend(["--model", model])
        if effort is not None:
            argv.extend(["--effort", effort])
        argv.extend(["--output-format", "json"])
        if req.schema_path is not None:
            argv.extend(["--json-schema", str(req.schema_path)])
        argv.extend(["-p", f"Read `{req.brief_path}` in full and execute that assignment in `{req.workdir}`."])

        # The first tool call can precede the CLI's first output (including its conversation ID).
        if session_id is None:
            save_run_record(req.run_dir, {**req.run_record(self.name), **record, "status": "running"})
        with result_path.open("x" if session_id else "w", encoding="utf-8") as result_file:
            rc, stderr = run_process_group(
                argv, cwd=req.workdir, env=tool_env({"KEEL_AGENT_MODE": req.mode}),
                on_stdout_line=result_file.write,
            )
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise KeelExecutionError(f"Antigravity returned invalid JSON (exit code {rc}): {stderr or exc}", record) from exc
        if not isinstance(result, dict):
            raise KeelExecutionError("Expected an Antigravity JSON result object.", record)
        conversation = result.get("conversation_id") or session_id
        record.update(session_id=conversation, usage=result.get("usage"))
        if rc or result.get("status") != "SUCCESS":
            raise KeelExecutionError(
                f"Antigravity run {req.run_id} failed (exit code {rc}): {stderr or result.get('error') or result.get('status')}",
                record,
            )
        if not isinstance(conversation, str) or not conversation.strip():
            raise KeelExecutionError("Antigravity result is missing its conversation_id.", record)

        structured = "structured_output" in result and req.schema_path is not None
        output = result["structured_output"] if structured else result.get("response")
        if req.schema_path is not None:
            if isinstance(output, str) and not structured:
                try:
                    output = json.loads(output)
                except json.JSONDecodeError as exc:
                    raise KeelExecutionError(f"Invalid JSON in Antigravity response: {exc}", record) from exc
            if output is None and not (structured or "response" in result):
                raise KeelExecutionError("Antigravity result is missing structured output.", record)
            text = json.dumps(output, indent=2, ensure_ascii=False) + "\n"
        elif isinstance(output, str):
            text = output
        else:
            raise KeelExecutionError("Expected a text response from Antigravity.", record)
        try:
            req.output_path.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise KeelExecutionError(f"Cannot write Antigravity output to {req.output_path}: {exc}", record) from exc
        return {**record, "status": "completed", "output_preview": text[:1000]}

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
        if record.get("ephemeral") or (record["mode"] == "read" and record["preset"] == "verify"):
            raise KeelConfigError("Ephemeral Antigravity verification sessions cannot be resumed.")
        session_id = record.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise KeelConfigError(f"Run {record['run_id']} has no persisted Antigravity session_id to resume.")
        if level is None:
            level, previous_model, previous_effort = record["level"], record["model"], record.get("effort")
        else:
            level, previous_model, previous_effort = resolve_executor_level_config(self.name, level, defaults)
        if record["preset"] == "verify":
            enforce_verify_min_level(level, defaults)
        req = RunRequest(
            run_id=record["run_id"], run_dir=run_dir, brief_path=followup_path, workdir=Path(record["workdir"]),
            project_root=None, mode=record["mode"], preset=record["preset"], level=level,
            model=model, effort=effort, schema_path=Path(record["schema_path"]) if record.get("schema_path") else None,
            output_path=output_path, extra_git_dirs=tuple(Path(p) for p in record.get("extra_git_dirs", [])),
            scratch_verify=bool(record.get("scratch_verify")),
        )
        return self._run(req, level, previous_model, previous_effort, session_id=session_id)
