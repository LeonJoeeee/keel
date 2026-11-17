"""Git worktree lifecycle, state helpers, gitignored root doc syncing, and disposable verification clones."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Iterator
import urllib.parse

from . import KeelError
from .env import is_subpath, run_cmd, safe_resolve, same_path

ROOT_AGENT_DOCS = ("AGENTS.md", "GEMINI.md")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def validate_slug(slug: str) -> str:
    s = slug.strip()
    if not _SLUG_RE.fullmatch(s) or ".." in s:
        raise KeelError(f"Invalid worktree slug '{slug}' (use letters, digits, '.', '_', '-').")
    return s


def git_toplevel(path: str | os.PathLike[str]) -> Path:
    proc = run_cmd(["git", "rev-parse", "--path-format=absolute", "--show-toplevel"], cwd=path)
    return safe_resolve(proc.stdout.strip())


def git_dirs(path: str | os.PathLike[str]) -> tuple[Path, Path]:
    """Return `(git_common_dir, git_dir)` as resolved absolute Paths."""
    common = run_cmd(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=path).stdout.strip()
    git_dir = run_cmd(["git", "rev-parse", "--path-format=absolute", "--git-dir"], cwd=path).stdout.strip()
    return safe_resolve(common), safe_resolve(git_dir)


def primary_checkout_root(path: str | os.PathLike[str]) -> Path:
    """Resolve the repository's primary checkout root even when `path` points at a linked worktree."""
    root = git_toplevel(path)
    common_dir, git_dir = git_dirs(root)
    if same_path(common_dir, git_dir):
        return root
    if common_dir.name == ".git" and common_dir.parent.is_dir():
        return safe_resolve(common_dir.parent)
    raw = run_cmd(["git", "worktree", "list", "--porcelain"], cwd=root).stdout
    for line in raw.splitlines():
        if line.startswith("worktree "):
            wt_candidate = Path(line[9:].strip())
            if wt_candidate.is_dir():
                c_dir, g_dir = git_dirs(wt_candidate)
                if same_path(c_dir, g_dir):
                    return safe_resolve(wt_candidate)
    return root


def branch_state_path(common_dir: Path, branch_name: str) -> Path:
    encoded = urllib.parse.quote(branch_name, safe="")
    return common_dir / "keel-state" / "branches" / f"{encoded}.json"


def load_branch_state(common_dir: Path, branch_name: str) -> dict[str, Any]:
    path = branch_state_path(common_dir, branch_name)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "branch": branch_name,
        "dual_required_by_sha": {},
        "checks_by_sha": {},
        "verdicts_by_sha": {},
        "pending_verifiers": {},
        "rounds": [],
        "request_changes_rounds": 0,
        "landed_head_sha": None,
        "landed_sha": None,
        "landed_method": None,
        "discarded": False,
        "discard_reason": None,
    }


def save_branch_state(common_dir: Path, branch_name: str, state: dict[str, Any]) -> Path:
    path = branch_state_path(common_dir, branch_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)
    return path


@contextlib.contextmanager
def locked_branch_state(common_dir: Path, branch_name: str) -> Iterator[dict[str, Any]]:
    """Acquire an exclusive flock on `<encoded_branch>.lock`, reload state, yield for mutation, and save atomically."""
    state_path = branch_state_path(common_dir, branch_name)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            state = load_branch_state(common_dir, branch_name)
            yield state
            save_branch_state(common_dir, branch_name, state)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def locked_repo_land(common_dir: Path) -> Iterator[None]:
    """Acquire an exclusive repo-wide flock on `<common_dir>/keel-state/land.lock` to serialize primary-checkout mutations."""
    lock_path = common_dir / "keel-state" / "land.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def resolve_base_ref(
    root: Path,
    explicit_base: str | None = None,
    *,
    allow_head_fallback: bool = False,
    default_branches: tuple[str, ...] = ("main", "master"),
) -> str:
    if explicit_base:
        if run_cmd(["git", "rev-parse", "--verify", f"{explicit_base}^{{commit}}"], cwd=root, check=False).returncode == 0:
            return explicit_base
        raise KeelError(f"Base revision '{explicit_base}' not found in {root}")
    for candidate in default_branches:
        if run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"], cwd=root, check=False).returncode == 0:
            return candidate
    if allow_head_fallback and run_cmd(["git", "rev-parse", "--verify", "HEAD"], cwd=root, check=False).returncode == 0:
        return "HEAD"
    raise KeelError(f"Could not find local default branch ({', '.join(default_branches)}) in {root}; pass --base <ref>.")


def ensure_agents_dir_ignored(project_root: Path) -> None:
    """Ensure `.agents/` is gitignored; use `.git/info/exclude` on adopted repos without dirtying `.gitignore`."""
    probe = ".agents/worktrees/.probe"
    chk = run_cmd(["git", "check-ignore", "-q", "--", probe], cwd=project_root, check=False)
    if chk.returncode == 0:
        return
    common_dir, _ = git_dirs(project_root)
    exclude = common_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if "/.agents/" not in existing and ".agents/" not in existing:
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        exclude.write_text(f"{existing}{prefix}/.agents/\n", encoding="utf-8")


def sync_gitignored_root_docs(source_root: Path, target_root: Path) -> list[str]:
    """Copy gitignored non-symlink root AGENTS.md/GEMINI.md into a fresh worktree or review clone."""
    copied: list[str] = []
    for name in ROOT_AGENT_DOCS:
        src = source_root / name
        dst = target_root / name
        if not src.exists() or src.is_symlink() or not src.is_file():
            continue
        if dst.exists() or dst.is_symlink():
            continue
        # Copy only when untracked & ignored in both source and target
        if run_cmd(["git", "ls-files", "--", name], cwd=source_root).stdout.strip():
            continue
        if run_cmd(["git", "ls-files", "--", name], cwd=target_root).stdout.strip():
            continue
        if run_cmd(["git", "check-ignore", "-q", "--", name], cwd=source_root, check=False).returncode != 0:
            continue
        if run_cmd(["git", "check-ignore", "-q", "--", name], cwd=target_root, check=False).returncode != 0:
            continue
        shutil.copy2(src, dst)
        copied.append(name)
    return copied


def create_worktree(
    project: str | os.PathLike[str],
    slug: str,
    *,
    base: str = "main",
    branch: str | None = None,
    fetch: bool = False,
) -> dict[str, Any]:
    clean_slug = validate_slug(slug)
    root = primary_checkout_root(project)
    ensure_agents_dir_ignored(root)

    wt_path = root / ".agents" / "worktrees" / clean_slug
    branch_name = branch or f"task/{clean_slug}"

    if wt_path.exists():
        raise KeelError(f"Worktree path already exists: {wt_path}")

    if fetch and "/" in base:
        remote, ref = base.split("/", 1)
        remotes = run_cmd(["git", "remote"], cwd=root).stdout.splitlines()
        if remote in remotes:
            run_cmd(["git", "fetch", "--no-tags", remote, ref], cwd=root)

    if run_cmd(["git", "rev-parse", "--verify", f"{base}^{{commit}}"], cwd=root, check=False).returncode != 0:
        if base == "main":
            base = resolve_base_ref(root, None, allow_head_fallback=True)
        else:
            raise KeelError(f"Base revision '{base}' not found in {root}")

    wt_path.parent.mkdir(parents=True, exist_ok=True)
    branch_exists = (
        run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"], cwd=root, check=False).returncode
        == 0
    )
    if branch_exists:
        run_cmd(["git", "worktree", "add", str(wt_path), branch_name], cwd=root)
    else:
        run_cmd(["git", "worktree", "add", "-b", branch_name, str(wt_path), base], cwd=root)

    synced = sync_gitignored_root_docs(root, wt_path)
    head_sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=wt_path).stdout.strip()
    return {
        "slug": clean_slug,
        "path": str(wt_path),
        "branch": branch_name,
        "base": base,
        "head_sha": head_sha,
        "synced_docs": synced,
    }


def list_worktrees(project: str | os.PathLike[str]) -> list[dict[str, str]]:
    root = git_toplevel(project)
    raw = run_cmd(["git", "worktree", "list", "--porcelain"], cwd=root).stdout
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, val = line.partition(" ")
        current[key] = val.strip()
    if current:
        entries.append(current)
    return entries


def cleanup_worktree(
    project: str | os.PathLike[str],
    slug: str,
    *,
    base: str | None = None,
    base_ref: str | None = None,
    expected_branch: str | None = None,
    merged_head_sha: str | None = None,
    keep_branch: bool = False,
    discard: bool = False,
    caller_cwd: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Safely remove `.agents/worktrees/<slug>` without losing unintegrated edits or modified root docs."""
    clean_slug = validate_slug(slug)
    root = primary_checkout_root(project)
    wt_path = root / ".agents" / "worktrees" / clean_slug
    if not wt_path.exists():
        raise KeelError(f"Worktree does not exist: {wt_path}")

    active_cwd = safe_resolve(caller_cwd or Path.cwd())
    if is_subpath(active_cwd, wt_path):
        raise KeelError(f"Cannot remove worktree {wt_path} while current directory ({active_cwd}) is inside it.")

    proj_common, _ = git_dirs(root)
    wt_common, wt_git = git_dirs(wt_path)
    if not same_path(proj_common, wt_common) or same_path(wt_common, wt_git):
        raise KeelError(f"Refusing to remove {wt_path}: not a linked worktree of {root}.")

    target_branch = expected_branch or f"task/{clean_slug}"
    actual_branch = run_cmd(["git", "branch", "--show-current"], cwd=wt_path).stdout.strip()
    if not discard and actual_branch != target_branch:
        raise KeelError(
            f"Worktree {wt_path} is on branch '{actual_branch}' rather than '{target_branch}'. "
            "Inspect before discarding."
        )

    porcelain = run_cmd(["git", "status", "--porcelain", "-uall"], cwd=wt_path).stdout.strip()
    if porcelain and not discard:
        raise KeelError(f"Worktree {wt_path} has uncommitted or untracked changes:\n{porcelain}")

    # Validate gitignored root docs BEFORE mutating anything on disk
    docs_to_unlink: list[Path] = []
    for name in ROOT_AGENT_DOCS:
        wt_doc = wt_path / name
        src_doc = root / name
        if wt_doc.exists() and not wt_doc.is_symlink():
            tracked = bool(run_cmd(["git", "ls-files", "--", name], cwd=wt_path).stdout.strip())
            if not tracked:
                if src_doc.exists() and not src_doc.is_symlink() and wt_doc.read_bytes() == src_doc.read_bytes():
                    docs_to_unlink.append(wt_doc)
                elif not discard:
                    raise KeelError(
                        f"Gitignored {wt_doc} differs from {src_doc}; reconcile it before cleanup or pass --discard."
                    )

    wt_head = run_cmd(["git", "rev-parse", "HEAD"], cwd=wt_path).stdout.strip()
    if not discard:
        if actual_branch:
            branch_tip = run_cmd(["git", "rev-parse", f"refs/heads/{actual_branch}"], cwd=root).stdout.strip()
            if branch_tip != wt_head:
                raise KeelError(
                    f"Branch '{actual_branch}' tip ({branch_tip[:12]}) differs from worktree HEAD ({wt_head[:12]})."
                )
        if merged_head_sha is not None and wt_head != merged_head_sha:
            raise KeelError(
                f"Worktree HEAD ({wt_head[:12]}) does not match expected integrated head ({merged_head_sha[:12]}); "
                "unintegrated local commits may exist."
            )
        effective_base = base_ref if base_ref is not None else base
        resolved_base = resolve_base_ref(root, effective_base, allow_head_fallback=True)
        is_ancestor = (
            run_cmd(["git", "merge-base", "--is-ancestor", wt_head, resolved_base], cwd=root, check=False).returncode == 0
        )
        squash_landed = False
        if not is_ancestor:
            state = load_branch_state(proj_common, actual_branch or target_branch)
            landed_head = state.get("landed_head_sha")
            landed_sha = state.get("landed_sha")
            if (
                isinstance(landed_head, str)
                and landed_head == wt_head
                and isinstance(landed_sha, str)
                and landed_sha
                and run_cmd(
                    ["git", "merge-base", "--is-ancestor", landed_sha, resolved_base], cwd=root, check=False
                ).returncode
                == 0
            ):
                squash_landed = True
        if not is_ancestor and not squash_landed:
            raise KeelError(
                f"Worktree HEAD ({wt_head[:12]}) on '{actual_branch or target_branch}' is not integrated into "
                f"'{resolved_base}'. Land via 'keel branch land {actual_branch or clean_slug}' first or pass --discard with human authorization."
            )

    for wt_doc in docs_to_unlink:
        wt_doc.unlink(missing_ok=True)

    # `--force` removes gitignored build/test caches (__pycache__, .pytest_cache) AFTER porcelain & integration checks pass
    run_cmd(["git", "worktree", "remove", "--force", str(wt_path)], cwd=root)
    deleted_branch = None
    if (
        not keep_branch
        and actual_branch
        and run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{actual_branch}"], cwd=root, check=False).returncode
        == 0
    ):
        run_cmd(["git", "branch", "-D", actual_branch], cwd=root)
        deleted_branch = actual_branch

    return {"removed_worktree": str(wt_path), "deleted_branch": deleted_branch, "discarded": discard}


def create_disposable_clone(
    project: str | os.PathLike[str],
    commit_sha: str,
    scratch_root: Path,
) -> tuple[Path, Path]:
    """Create a physically isolated, remote-stripped `--no-local` detached checkout at `commit_sha`."""
    root = git_toplevel(project)
    scratch_root.mkdir(parents=True, exist_ok=True)
    sep_git_dir = scratch_root / "git"
    checkout_dir = scratch_root / "checkout"

    run_cmd(
        [
            "git",
            "clone",
            "--quiet",
            "--no-local",
            "--no-checkout",
            "--separate-git-dir",
            str(sep_git_dir),
            str(root),
            str(checkout_dir),
        ]
    )
    # Ensure commit_sha is present locally even if not at a default branch tip
    if run_cmd(["git", "cat-file", "-e", f"{commit_sha}^{{commit}}"], cwd=checkout_dir, check=False).returncode != 0:
        run_cmd(["git", "fetch", "--quiet", "--no-tags", str(root), commit_sha], cwd=checkout_dir)

    # Strip ALL configured git remotes inside the disposable clone so no remote/source URL remains reachable
    for remote_name in run_cmd(["git", "remote"], cwd=checkout_dir).stdout.splitlines():
        clean_remote = remote_name.strip()
        if clean_remote:
            run_cmd(["git", "remote", "remove", clean_remote], cwd=checkout_dir)

    run_cmd(["git", "checkout", "--quiet", "--detach", commit_sha], cwd=checkout_dir)
    sync_gitignored_root_docs(root, checkout_dir)
    return checkout_dir, sep_git_dir
