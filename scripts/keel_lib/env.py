"""Environment normalization, safe path identity, command execution, and process groups."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
from typing import Any, Callable, Sequence

from . import KeelError

FALLBACK_BINS = (
    Path.home() / ".local" / "bin",
    Path.home() / ".npm-global" / "bin",
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
)


def tool_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a non-interactive environment with fallback tool PATH entries appended safely."""
    env = dict(os.environ)
    raw_path = env.get("PATH", "")
    parts = [p for p in raw_path.split(os.pathsep) if p]
    existing = set(parts)
    for candidate in FALLBACK_BINS:
        s = str(candidate)
        if s not in existing and candidate.is_dir():
            parts.append(s)
            existing.add(s)
    env["PATH"] = os.pathsep.join(parts)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_EDITOR", "true")
    env.setdefault("GCM_INTERACTIVE", "never")
    if extra:
        env.update(extra)
    return env


def safe_resolve(path: str | os.PathLike[str]) -> Path:
    """Resolve path while guarding Python <=3.12 Path.resolve() RuntimeError on ELOOP."""
    p = Path(path)
    try:
        os.stat(p)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise KeelError(f"Symlink loop detected at path: {p}") from exc
    return p.resolve()


def same_path(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    """Filesystem-accurate identity check (handles macOS case-insensitive APFS & symlinks)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return safe_resolve(a) == safe_resolve(b)


def is_subpath(child: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """Return True iff child is root or nested inside root using filesystem identity."""
    c = safe_resolve(child)
    r = safe_resolve(root)
    return any(same_path(candidate, r) for candidate in (c, *c.parents))


def is_disposable_verify_path(path: str | os.PathLike[str] | None) -> bool:
    """Return True iff `path` resolves inside system temp dir under a `keel-verify-*` scratch dir."""
    if path is None:
        return False
    raw = str(path).strip()
    if not raw:
        return False
    resolved = Path(raw).resolve(strict=False)
    tmp_roots = (Path(tempfile.gettempdir()).resolve(strict=False), Path("/tmp").resolve(strict=False))
    rel_parts: tuple[str, ...] = ()
    for tmp_root in tmp_roots:
        if is_subpath(resolved, tmp_root):
            try:
                rel_parts = resolved.relative_to(tmp_root).parts
                break
            except ValueError:
                continue
    if not rel_parts:
        return False
    for idx, part in enumerate(rel_parts):
        if part == ".agents" and idx + 1 < len(rel_parts) and rel_parts[idx + 1] == "worktrees":
            return False
    return any(part.startswith("keel-verify-") for part in rel_parts)


def run_cmd(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            env=env if env is not None else tool_env(),
            input=input_text,
            text=True,
            capture_output=True,
        )
    except OSError as exc:
        if cwd is not None and not os.path.isdir(cwd):
            problem = "not found" if isinstance(exc, FileNotFoundError) else "invalid"
            message = f"Working directory (cwd) {problem} (127): {cwd} ({exc})"
            if not check:
                return subprocess.CompletedProcess(list(argv), 127, "", message)
            raise KeelError(message) from exc
        if not isinstance(exc, FileNotFoundError):
            raise
        if not check:
            return subprocess.CompletedProcess(list(argv), 127, "", str(exc))
        raise KeelError(f"Executable not found (127): {argv[0]}") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise KeelError(f"Command failed ({proc.returncode}): {' '.join(argv)}\n{detail}".strip())
    return proc


def run_process_group(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    stdin_path: Path | None = None,
    on_stdout_line: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """Run CLI child in its own session, forwarding SIGINT/SIGTERM to the entire process group."""
    child: subprocess.Popen[str] | None = None
    prev_int = signal.getsignal(signal.SIGINT)
    prev_term = signal.getsignal(signal.SIGTERM)

    def _forward(signum: int, _frame: Any) -> None:
        if child is not None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGINT, _forward)
    signal.signal(signal.SIGTERM, _forward)
    stderr_text = ""
    try:
        stdin_fh = stdin_path.open("r", encoding="utf-8") if stdin_path else subprocess.DEVNULL
        try:
            try:
                child = subprocess.Popen(
                    list(argv),
                    cwd=str(cwd) if cwd is not None else None,
                    env=env if env is not None else tool_env(),
                    stdin=stdin_fh,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                if cwd is not None and not os.path.isdir(cwd):
                    problem = "not found" if isinstance(exc, FileNotFoundError) else "invalid"
                    return 127, f"Working directory (cwd) {problem}: {cwd} ({exc})"
                if not isinstance(exc, FileNotFoundError):
                    raise
                return 127, f"Executable not found: {argv[0]} ({exc})"
            assert child.stdout is not None and child.stderr is not None
            stderr_chunks: list[str] = []

            def _drain_stderr(stream: Any) -> None:
                stderr_chunks.append(stream.read())

            err_thread = threading.Thread(target=_drain_stderr, args=(child.stderr,), daemon=True)
            err_thread.start()
            for line in child.stdout:
                if on_stdout_line is not None:
                    on_stdout_line(line)
            err_thread.join()
            child.stdout.close()
            child.stderr.close()
            stderr_text = "".join(stderr_chunks)
            rc = child.wait()
        finally:
            if isinstance(stdin_fh, object) and hasattr(stdin_fh, "close"):
                stdin_fh.close()  # type: ignore[union-attr]
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)

    if rc < 0:
        return 128 + abs(rc), stderr_text
    return rc, stderr_text
