"""Executor registry (`1..N` pluggable side-agent adapters)."""

from __future__ import annotations

from .. import KeelError
from .agy import AgyExecutor
from .base import ExecutorAdapter, RunRequest
from .codex import CodexExecutor

EXECUTORS: dict[str, type[ExecutorAdapter]] = {
    CodexExecutor.name: CodexExecutor,
    AgyExecutor.name: AgyExecutor,
}


def get_executor(name: str) -> ExecutorAdapter:
    cls = EXECUTORS.get(name.strip().lower())
    if cls is None:
        raise KeelError(f"Unknown executor '{name}'. Registered executors: {', '.join(sorted(EXECUTORS))}")
    return cls()


__all__ = ["EXECUTORS", "ExecutorAdapter", "RunRequest", "get_executor"]
