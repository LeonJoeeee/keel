"""Built-in Google Antigravity (`invoke_subagent` / `send_message`) executor adapter.

Emits a minimal file-pointer descriptor so the main session never copies large prompts
through its own output token stream (`P1`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import KeelError
from .base import ExecutorAdapter, RunRequest

VALID_AGY_MODELS = frozenset({"inherit", "flash_lite", "flash", "pro"})
VALID_AGY_WORKSPACES = frozenset({"inherit", "share", "branch"})


class AgyExecutor(ExecutorAdapter):
    name = "agy"
    native_agent_docs = ("AGENTS.md", "GEMINI.md")
    loads_plugin_spine = True
    inject_schema_in_brief = True  # Native subagent writes structured JSON directly to `output_path`

    def execute(self, req: RunRequest, defaults: dict[str, Any]) -> dict[str, Any]:
        preset_cfg = (
            defaults.get("executors", {})
            .get("agy", {})
            .get("presets", {})
            .get(req.preset, {"model": "inherit"})
        )
        model = req.model or preset_cfg.get("model", "inherit")
        if model not in VALID_AGY_MODELS:
            raise KeelError(f"Invalid Antigravity model '{model}'. Valid choices: {sorted(VALID_AGY_MODELS)}")
        if req.agy_workspace not in VALID_AGY_WORKSPACES:
            raise KeelError(
                f"Invalid Antigravity workspace '{req.agy_workspace}'. Valid choices: {sorted(VALID_AGY_WORKSPACES)}"
            )

        pointer_lines = [
            f"Read `{req.brief_path}` in full first and execute that assignment.",
            f"Operate in `{req.workdir}` (`mode={req.mode}`).",
        ]
        if req.schema_path is not None:
            pointer_lines.append(
                f"Before finishing, write your validated JSON output to `{req.output_path}`."
            )
        else:
            pointer_lines.append(
                f"Write your concise final report to `{req.output_path}` (and summarize it in your completion reply)."
            )

        invoke_args = {
            "TypeName": "self",
            "Role": f"Keel {req.preset} ({req.run_id})",
            "Model": model,
            "Workspace": req.agy_workspace,
            "Prompt": " ".join(pointer_lines),
        }
        return {
            "status": "awaiting_host_invoke",
            "executor": self.name,
            "model": model,
            "effort": "inherited",
            "invoke_subagent": invoke_args,
            "output_path": str(req.output_path),
            "extra_git_dirs": [str(p) for p in req.extra_git_dirs],
            "scratch_verify": req.scratch_verify,
        }

    def resume(
        self,
        run_dir: Path,
        record: dict[str, Any],
        followup_path: Path,
        output_path: Path,
        defaults: dict[str, Any],
    ) -> dict[str, Any]:
        pointer = (
            f"Read `{followup_path}` for follow-up instructions on `{record['workdir']}`, "
            f"and write updated output to `{output_path}`."
        )
        return {
            "status": "awaiting_host_send_message",
            "executor": self.name,
            "send_message": {
                "Message": pointer,
            },
            "output_path": str(output_path),
        }
