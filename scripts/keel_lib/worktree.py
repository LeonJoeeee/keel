"""Git worktree lifecycle, gitignored root doc syncing, and disposable verification clones."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
from typing import Any

from . import KeelError
from .env_gh import is_subpath, run_cmd, safe_resolve, same_path

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
    base: str = "origin/main",
    branch: str | None = None,
    fetch: bool = True,
) -> dict[str, Any]:
    clean_slug = validate_slug(slug)
    root = git_toplevel(project)
    ensure_agents_dir_ignored(root)

    wt_path = root / ".agents" / "worktrees" / clean_slug
    branch_name = branch or f"task/{clean_slug}"

    if wt_path.exists():
        raise KeelError(f"Worktree path already exists: {wt_path}")

    if fetch and "/" in base:
        remote, ref = base.split("/", 1)
        # Best-effort fetch when remote exists
        remotes = run_cmd(["git", "remote"], cwd=root).stdout.splitlines()
        if remote in remotes:
            run_cmd(["git", "fetch", "--no-tags", remote, ref], cwd=root)

    # Check if base exists; fall back to HEAD if origin/main does not exist in a fresh local repo
    if run_cmd(["git", "rev-parse", "--verify", f"{base}^{{commit}}"], cwd=root, check=False).returncode != 0:
        if base == "origin/main" and run_cmd(["git", "rev-parse", "--verify", "HEAD"], cwd=root, check=False).returncode == 0:
            base = "HEAD"
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
    merged_head_sha: str | None = None,
    discard: bool = False,
    caller_cwd: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Safely remove `.agents/worktrees/<slug>` without losing unpublished edits or modified root docs."""
    clean_slug = validate_slug(slug)
    root = git_toplevel(project)
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

    expected_branch = f"task/{clean_slug}"
    actual_branch = run_cmd(["git", "branch", "--show-current"], cwd=wt_path).stdout.strip()
    if not discard and actual_branch != expected_branch:
        raise KeelError(
            f"Worktree {wt_path} is on branch '{actual_branch}' rather than '{expected_branch}'. "
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
        if not merged_head_sha:
            raise KeelError("Provide --pr <merged-pr-number> (or --discard with human authorization) to clean up worktree.")
        if wt_head != merged_head_sha:
            raise KeelError(
                f"Worktree HEAD ({wt_head[:12]}) does not match merged PR head ({merged_head_sha[:12]}); "
                "unintegrated local commits may exist."
            )
        if actual_branch:
            branch_tip = run_cmd(["git", "rev-parse", f"refs/heads/{actual_branch}"], cwd=root).stdout.strip()
            if branch_tip != merged_head_sha:
                raise KeelError(
                    f"Branch '{actual_branch}' tip ({branch_tip[:12]}) differs from merged PR head ({merged_head_sha[:12]})."
                )

    for wt_doc in docs_to_unlink:
        wt_doc.unlink(missing_ok=True)

    # `--force` removes gitignored build/test caches (__pycache__, .pytest_cache) AFTER porcelain & SHA checks pass
    run_cmd(["git", "worktree", "remove", "--force", str(wt_path)], cwd=root)
    deleted_branch = None
    if actual_branch and (
        run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{actual_branch}"], cwd=root, check=False).returncode
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
    """Create a physically isolated `--no-local` detached checkout at `commit_sha` for clean verification."""
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
    # Ensure commit_sha is present even if not at a branch tip
    if run_cmd(["git", "cat-file", "-e", f"{commit_sha}^{{commit}}"], cwd=checkout_dir, check=False).returncode != 0:
        origin_proc = run_cmd(["git", "remote", "get-url", "origin"], cwd=root, check=False)
        fetch_src = origin_proc.stdout.strip() if origin_proc.returncode == 0 and origin_proc.stdout.strip() else str(root)
        run_cmd(["git", "fetch", "--quiet", "--no-tags", fetch_src, commit_sha], cwd=checkout_dir)

    origin_proc = run_cmd(["git", "remote", "get-url", "origin"], cwd=root, check=False)
    if origin_proc.returncode == 0 and origin_proc.stdout.strip():
        run_cmd(["git", "remote", "set-url", "origin", origin_proc.stdout.strip()], cwd=checkout_dir)

    run_cmd(["git", "checkout", "--quiet", "--detach", commit_sha], cwd=checkout_dir)
    sync_gitignored_root_docs(root, checkout_dir)
    return checkout_dir, sep_git_dir
