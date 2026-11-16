"""Environment normalization, safe path identity, process groups, and resilient GitHub REST helpers."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Sequence

from . import KeelError

FALLBACK_BINS = (
    Path.home() / ".local" / "bin",
    Path.home() / ".npm-global" / "bin",
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
)

API_RETRY_DELAYS = (0.25, 0.5)

_PERMANENT_ERR_RE = re.compile(
    r"(\bHTTP\s+4\d\d\b"
    r"|\bHTTP\s+50[01]\b"
    r"|Upgrade to GitHub Pro or make this repository public"
    r"|authentication|bad credentials|certificate verify failed|x509:)",
    re.IGNORECASE,
)

_TRANSIENT_ERR_RE = re.compile(
    r"(\bEOF\b"
    r"|\bHTTP\s+50[234]\b"
    r"|connection reset"
    r"|TLS handshake timeout"
    r"|i/o timeout"
    r"|dial tcp"
    r"|stream error"
    r"|context deadline exceeded"
    r"|error connecting to"
    r"|net/http:)",
    re.IGNORECASE,
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
    env.setdefault("GH_PROMPT_DISABLED", "1")
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


def run_cmd(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        env=env if env is not None else tool_env(),
        input=input_text,
        text=True,
        capture_output=True,
    )
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


def decode_json_stream(raw: str) -> Any:
    """Decode single JSON value or concatenated `gh api --paginate` pages cleanly."""
    text = raw.strip()
    if not text:
        return None
    decoder = json.JSONDecoder()
    idx = 0
    n = len(text)
    docs: list[Any] = []
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        val, idx = decoder.raw_decode(text, idx)
        docs.append(val)
    if len(docs) == 1:
        return docs[0]
    if all(isinstance(d, list) for d in docs):
        merged: list[Any] = []
        for d in docs:
            merged.extend(d)
        return merged
    return docs


def is_transient_api_error(stderr: str) -> bool:
    if not stderr or _PERMANENT_ERR_RE.search(stderr):
        return False
    return _TRANSIENT_ERR_RE.search(stderr) is not None


def gh_api(
    endpoint: str,
    extra_args: Sequence[str] = (),
    *,
    cwd: str | os.PathLike[str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = run_cmd,
) -> Any:
    """Call `gh api`, retrying transient transport/502-504 failures ONLY on plain idempotent reads."""
    idempotent_read = set(extra_args) <= {"--paginate"}
    argv = ["gh", "api", endpoint, *extra_args]
    max_attempts = (1 + len(API_RETRY_DELAYS)) if idempotent_read else 1

    for attempt in range(max_attempts):
        proc = runner(argv, cwd=cwd, check=False)
        if proc.returncode == 0:
            return decode_json_stream(proc.stdout)
        err = (proc.stderr or proc.stdout or "").strip()
        if attempt + 1 < max_attempts and is_transient_api_error(err):
            time.sleep(API_RETRY_DELAYS[attempt])
            continue
        raise KeelError(f"gh api {endpoint} failed ({proc.returncode}): {err}")
    raise AssertionError("unreachable")


_GITHUB_REMOTE_RE = re.compile(
    r"(?:github\.com[:/])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def detect_repo_slug(project: str | os.PathLike[str]) -> str:
    proc = run_cmd(["git", "remote", "get-url", "origin"], cwd=project)
    url = proc.stdout.strip()
    m = _GITHUB_REMOTE_RE.search(url)
    if not m:
        raise KeelError(f"Could not parse GitHub OWNER/REPO from origin URL: {url}")
    return m.group(1)
