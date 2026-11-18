"""Environment normalization, safe path identity, command execution, and process groups."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
import errno
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
from typing import Any, Callable, Iterator, Sequence

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


_PGIDS_LOCK = threading.Lock()
_ACTIVE_PGIDS_SNAPSHOT: frozenset[int] = frozenset()
_INTERRUPTED_BY: int | None = None


def _signal_group(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        pass  # The group may have exited between snapshot and delivery.


def _forward_signal(signum: int, _frame: Any) -> None:
    global _INTERRUPTED_BY
    _INTERRUPTED_BY = signum
    for pgid in _ACTIVE_PGIDS_SNAPSHOT:
        _signal_group(pgid, signum)


@contextmanager
def forward_signals_to_child_groups() -> Iterator[None]:
    """Wrap the worker pool on the main thread. Nested/worker runs share its handlers."""
    global _INTERRUPTED_BY
    if threading.current_thread() is not threading.main_thread() or signal.getsignal(signal.SIGINT) is _forward_signal:
        yield
        return

    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, _forward_signal)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        _INTERRUPTED_BY = None


def check_interrupted() -> None:
    """Stop scheduling new work after an interrupt in the enclosing forwarding scope."""
    signum = _INTERRUPTED_BY
    if signum is not None:
        raise KeyboardInterrupt(f"Interrupted by {signal.Signals(signum).name}")


def _close_group(pgid: int) -> None:
    global _ACTIVE_PGIDS_SNAPSHOT
    with _PGIDS_LOCK:
        if pgid in _ACTIVE_PGIDS_SNAPSHOT:
            _ACTIVE_PGIDS_SNAPSHOT -= {pgid}
            _signal_group(pgid, signal.SIGKILL)


def _wait_for_group_leader(child: subprocess.Popen[str]) -> int:
    """Reap the leader and close any descendant-held stdout handles when it exits."""
    try:
        while _INTERRUPTED_BY is None:
            try:
                return child.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                continue
        # Give an interrupted CLI a chance to flush output before forcing teardown.
        try:
            return child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _signal_group(child.pid, signal.SIGKILL)
            return child.wait()
    finally:
        _close_group(child.pid)


def run_process_group(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    stdin_path: Path | None = None,
    on_stdout_line: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """Stream CLI output in the calling thread while supervising its whole process group."""
    global _ACTIVE_PGIDS_SNAPSHOT
    stdin = stdin_path.open("r", encoding="utf-8") if stdin_path else nullcontext(subprocess.DEVNULL)
    with stdin as src, forward_signals_to_child_groups(), tempfile.TemporaryFile(mode="w+") as stderr:
        check_interrupted()
        try:
            child = subprocess.Popen(
                list(argv), cwd=cwd, env=env if env is not None else tool_env(), stdin=src,
                stdout=subprocess.PIPE, stderr=stderr, text=True, start_new_session=True,
            )
        except OSError as exc:
            if cwd is not None and not os.path.isdir(cwd):
                problem = "not found" if isinstance(exc, FileNotFoundError) else "invalid"
                return 127, f"Working directory (cwd) {problem}: {cwd} ({exc})"
            if not isinstance(exc, FileNotFoundError):
                raise
            return 127, f"Executable not found: {argv[0]} ({exc})"

        with child, ThreadPoolExecutor(max_workers=1) as waiter:
            with _PGIDS_LOCK:
                _ACTIVE_PGIDS_SNAPSHOT |= {child.pid}
            try:
                # A signal arriving during Popen may have missed this group.
                signum = _INTERRUPTED_BY
                if signum is not None:
                    _signal_group(child.pid, signum)
                done = waiter.submit(_wait_for_group_leader, child)
                assert child.stdout is not None
                for line in child.stdout:
                    if on_stdout_line is not None:
                        on_stdout_line(line)
                rc = done.result()
            finally:
                if child.returncode is None:
                    _close_group(child.pid)
        stderr.seek(0)
        error_text = stderr.read()
        signum = _INTERRUPTED_BY
        if rc == 0 and signum is not None:
            rc = -signum
        return (128 - rc if rc < 0 else rc), error_text
