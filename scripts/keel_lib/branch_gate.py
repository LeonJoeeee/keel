"""Pure-Git branch verification, `.git/keel-state/` ledger, pure-rebase proof, and guarded landing."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Iterator, Sequence

from . import KeelError, SCHEMAS_DIR
from .context import assemble_brief, generate_run_id, runs_root_dir, save_run_record
from .env import check_interrupted, forward_signals_to_child_groups, run_cmd, safe_resolve, same_path, tool_env
from .executors import RunRequest, get_executor
from .executors.base import (
    KeelConfigError,
    KeelExecutionError,
    VALID_LEVELS,
    escalate_level,
    level_rank,
    max_level,
    normalize_level,
)
from .worktree import (
    cleanup_worktree,
    create_disposable_clone,
    ensure_agents_dir_ignored,
    git_dirs,
    list_worktrees,
    load_branch_state,
    locked_branch_state,
    locked_repo_land,
    primary_checkout_root,
    resolve_base_ref,
)

ALLOWED_VERDICT_KEYS = frozenset({"verdict", "summary", "blocking", "notes", "commands_run"})
SAFE_MERGE_ATTRS = frozenset({"set", "unset", "unspecified", "text", "binary", "union"})
_HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def validate_verdict_payload(payload: Any) -> dict[str, Any]:
    """Validate structured verifier JSON strictly against `verdict.schema.json` (`additionalProperties: false`)."""
    if not isinstance(payload, dict):
        raise KeelError("Verdict payload must be a JSON object.")

    unknown_keys = sorted(set(payload.keys()) - ALLOWED_VERDICT_KEYS)
    if unknown_keys:
        raise KeelError(f"Verdict payload contains unexpected properties: {', '.join(unknown_keys)}")

    missing_keys = sorted(ALLOWED_VERDICT_KEYS - set(payload.keys()))
    if missing_keys:
        raise KeelError(f"Verdict payload is missing required properties: {', '.join(missing_keys)}")

    verdict = payload.get("verdict")
    if verdict not in ("approve", "request_changes"):
        raise KeelError(f"Invalid verdict '{verdict}': expected 'approve' or 'request_changes'.")

    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise KeelError("Verdict 'summary' must be a non-empty string.")

    out: dict[str, Any] = {"verdict": verdict, "summary": summary.strip()}
    for field in ("blocking", "notes", "commands_run"):
        raw_list = payload.get(field)
        if not isinstance(raw_list, list) or not all(isinstance(x, str) for x in raw_list):
            raise KeelError(f"Verdict '{field}' must be an array of strings.")
        out[field] = [x.strip() for x in raw_list if x.strip()]

    if verdict == "approve" and out["blocking"]:
        raise KeelError("Verdict cannot be 'approve' when 'blocking' items are present.")
    if verdict == "request_changes" and not out["blocking"]:
        raise KeelError(
            "Verdict 'request_changes' requires at least one 'blocking' defect ('notes' never block approval)."
        )
    return out


def _sanitized_git_env() -> dict[str, str]:
    env = {k: v for k, v in tool_env().items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_EDITOR"] = "true"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_ATTR_NOSYSTEM"] = "1"
    return env


def _sanitized_git(repo_dir: Path, args: list[str], *, check: bool = True) -> str:
    proc = run_cmd(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "rerere.enabled=false", "-c", "core.quotePath=false", *args],
        cwd=repo_dir,
        env=_sanitized_git_env(),
        check=check,
    )
    return proc.stdout.strip()


def _sanitized_git_nul(repo_dir: Path, args: list[str]) -> list[str]:
    proc = run_cmd(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "rerere.enabled=false", "-c", "core.quotePath=false", *args],
        cwd=repo_dir,
        env=_sanitized_git_env(),
    )
    return [item for item in proc.stdout.split("\0") if item]


def _prove_rebase_in_clean_repo(
    clean_repo: Path,
    old_base: str,
    old_head: str,
    new_base: str,
    new_head: str,
) -> dict[str, Any]:
    for anc, desc, desc_label in (
        (old_base, old_head, "old-base <= old-head"),
        (old_base, new_base, "old-base <= new-base"),
        (new_base, new_head, "new-base <= new-head"),
    ):
        chk = run_cmd(
            ["git", "merge-base", "--is-ancestor", anc, desc],
            cwd=clean_repo,
            env=_sanitized_git_env(),
            check=False,
        )
        if chk.returncode != 0:
            raise KeelError(f"Pure-rebase ancestry precondition failed: {desc_label}.")

    if _sanitized_git(clean_repo, ["rev-list", "--merges", f"{old_base}..{old_head}"]) or _sanitized_git(
        clean_repo, ["rev-list", "--merges", f"{new_base}..{new_head}"]
    ):
        raise KeelError("Pure-rebase proof rejects ranges containing merge commits.")

    old_diff_files = _sanitized_git_nul(clean_repo, ["diff", "--name-only", "-z", old_base, old_head])
    if not old_diff_files:
        raise KeelError("Old range has an empty diff.")

    touched = set(old_diff_files)
    touched.update(_sanitized_git_nul(clean_repo, ["diff", "--name-only", "-z", old_base, new_base]))

    lit_paths = [f":(literal){p}" for p in sorted(touched)]
    for rev in (old_base, old_head, new_base, new_head):
        ls_entries = _sanitized_git_nul(clean_repo, ["ls-tree", "-z", rev, "--", *lit_paths])
        if any(entry.startswith("160000 ") for entry in ls_entries):
            raise KeelError("Pure-rebase proof does not admit submodule gitlink changes.")
        attr_tokens = _sanitized_git_nul(
            clean_repo,
            ["check-attr", "-z", "--source", rev, "merge", "--", *sorted(touched)],
        )
        for idx in range(0, len(attr_tokens) - 2, 3):
            merge_attr = attr_tokens[idx + 2].strip()
            if merge_attr not in SAFE_MERGE_ATTRS:
                raise KeelError(
                    f"Pure-rebase proof rejects custom .gitattributes merge driver '{merge_attr}'."
                )

    target_tree = _sanitized_git(clean_repo, ["rev-parse", f"{new_head}^{{tree}}"])

    mt = run_cmd(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "rerere.enabled=false",
            "merge-tree",
            "--write-tree",
            f"--merge-base={old_base}",
            new_base,
            old_head,
        ],
        cwd=clean_repo,
        env=_sanitized_git_env(),
        check=False,
    )
    if mt.returncode == 0 and mt.stdout.strip().splitlines()[0].strip() == target_tree:
        return {"ok": True, "method": "merge-tree", "tree_sha": target_tree}

    run_cmd(["git", "checkout", "--quiet", "--detach", old_head], cwd=clean_repo, env=_sanitized_git_env())
    rb = run_cmd(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "rerere.enabled=false",
            "-c",
            "user.name=Keel Rebase Proof",
            "-c",
            "user.email=keel@localhost",
            "rebase",
            "--no-autostash",
            "--no-gpg-sign",
            "--reapply-cherry-picks",
            "--empty=keep",
            "--onto",
            new_base,
            old_base,
        ],
        cwd=clean_repo,
        env=_sanitized_git_env(),
        check=False,
    )
    if rb.returncode != 0:
        raise KeelError("Rebase replay encountered conflicts; fresh independent verification is required.")
    replay_tree = _sanitized_git(clean_repo, ["rev-parse", "HEAD^{tree}"])
    if replay_tree != target_tree:
        raise KeelError(
            f"Rebased head tree ({target_tree[:12]}) differs from pure replay tree ({replay_tree[:12]}); "
            "fresh independent verification is required."
        )
    return {"ok": True, "method": "rebase-replay", "tree_sha": target_tree}


def compare_rebase(
    project: Path,
    old_base: str,
    old_head: str,
    new_base: str,
    new_head: str,
) -> dict[str, Any]:
    """Mathematically prove `new_head` on `new_base` is a conflict-free pure rebase of `old_head` on `old_base`."""
    for label, sha in (("old-base", old_base), ("old-head", old_head), ("new-base", new_base), ("new-head", new_head)):
        if not _HEX_SHA_RE.fullmatch(sha):
            raise KeelError(f"--{label} must be a full 40/64-character commit SHA (got '{sha}').")
        chk = run_cmd(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=project, check=False)
        if chk.returncode != 0:
            raise KeelError(f"Commit for --{label} ({sha}) is not present in {project}.")

    common_dir, _ = git_dirs(project)
    objects_dir = common_dir / "objects"

    with tempfile.TemporaryDirectory(prefix="keel-rebase-proof-") as tmp:
        clean_repo = Path(tmp) / "clean"
        clean_repo.mkdir()
        run_cmd(["git", "init", "--quiet", "-b", "main", str(clean_repo)], env=_sanitized_git_env())
        alt_file = clean_repo / ".git" / "objects" / "info" / "alternates"
        alt_file.parent.mkdir(parents=True, exist_ok=True)
        alt_file.write_text(f"{objects_dir}\n", encoding="utf-8")
        return _prove_rebase_in_clean_repo(clean_repo, old_base, old_head, new_base, new_head)


def resolve_branch_and_slug(project: Path, branch_or_slug: str, *, must_exist: bool = True) -> tuple[str, str]:
    raw = branch_or_slug.strip()
    if not raw:
        raise KeelError("Branch or worktree slug cannot be empty.")
    if raw.startswith("refs/heads/"):
        raw = raw[len("refs/heads/") :]

    task_candidate = f"task/{raw}"
    if not raw.startswith("task/") and (
        run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{task_candidate}"], cwd=project, check=False).returncode
        == 0
    ):
        branch = task_candidate
    else:
        branch = raw

    if must_exist and (
        run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=project, check=False).returncode != 0
    ):
        raise KeelError(f"Local branch '{branch}' (resolved from '{branch_or_slug}') does not exist in {project}.")

    slug = branch[5:] if branch.startswith("task/") else branch.split("/")[-1]
    return branch, slug


def _list_tree_md_files(project: Path, rev: str, subdir: str) -> list[str]:
    proc = run_cmd(
        ["git", "-c", "core.quotePath=false", "ls-tree", "--name-only", "-z", rev, "--", f"{subdir.rstrip('/')}/"],
        cwd=project,
        check=False,
    )
    if proc.returncode != 0:
        return []
    return sorted(
        item
        for item in proc.stdout.split("\0")
        if item.endswith(".md") and Path(item).name != ".gitkeep"
    )


def _read_blob_sha(project: Path, rev: str, rel_path: str) -> str | None:
    kind = run_cmd(["git", "cat-file", "-t", f"{rev}:{rel_path}"], cwd=project, check=False)
    if kind.returncode != 0 or kind.stdout.strip() != "blob":
        return None
    return run_cmd(["git", "rev-parse", f"{rev}:{rel_path}"], cwd=project).stdout.strip().lower()


def _read_blob_text(project: Path, rev: str, rel_path: str) -> str | None:
    if _read_blob_sha(project, rev, rel_path) is None:
        return None
    return run_cmd(["git", "show", f"{rev}:{rel_path}"], cwd=project).stdout.strip()


def _effective_task_blob_sha(project: Path, base_tip_sha: str, head_sha: str, task_file: str | None) -> str | None:
    if not task_file:
        return None
    sha = _read_blob_sha(project, base_tip_sha, task_file)
    if sha is None and task_file.startswith("docs/tasks/active/"):
        sha = _read_blob_sha(project, base_tip_sha, "docs/tasks/todo/" + Path(task_file).name)
    if sha is None:
        sha = _read_blob_sha(project, head_sha, task_file)
    return sha


def normalize_task_rel_path(project: Path, raw_task_file: str) -> str:
    primary_root = primary_checkout_root(project)
    p = Path(raw_task_file)
    candidate = p if p.is_absolute() else (primary_root / p)
    try:
        rel = candidate.resolve(strict=False).relative_to(primary_root.resolve(strict=False)).as_posix()
    except ValueError as exc:
        raise KeelError(f"--task-file '{raw_task_file}' is outside repository {primary_root}.") from exc
    if not rel or rel == ".":
        raise KeelError(f"--task-file '{raw_task_file}' must point to a file inside {primary_root}.")
    return rel


def _task_exists_in_git(project: Path, base_ref: str, head_sha: str | None, rel: str) -> bool:
    todo_equiv = ("docs/tasks/todo/" + Path(rel).name) if rel.startswith("docs/tasks/active/") else None
    return (
        _read_blob_text(project, base_ref, rel) is not None
        or (head_sha is not None and _read_blob_text(project, head_sha, rel) is not None)
        or (todo_equiv is not None and _read_blob_text(project, base_ref, todo_equiv) is not None)
    )


def discover_task_file(
    project: Path,
    branch: str,
    slug: str,
    *,
    base_ref: str,
    head_sha: str | None = None,
    explicit_task_file: str | None = None,
    state_task_file: str | None = None,
) -> str | None:
    if explicit_task_file:
        rel = normalize_task_rel_path(project, explicit_task_file)
        if not _task_exists_in_git(project, base_ref, head_sha, rel):
            raise KeelError(f"--task-file '{rel}' is not committed on '{base_ref}' or '{branch}'.")
        return rel

    valid_state_task = (
        state_task_file
        if state_task_file and _task_exists_in_git(project, base_ref, head_sha, state_task_file)
        else None
    )
    if valid_state_task:
        return valid_state_task

    # Never steal `task/<slug>`'s active task file when operating on a different branch (e.g. `feature/<slug>`)
    if branch != f"task/{slug}" and (
        run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/task/{slug}"], cwd=project, check=False).returncode
        == 0
    ):
        return valid_state_task

    candidates: list[str] = []
    for rev in ([head_sha, base_ref] if head_sha else [base_ref]):
        if rev:
            for item in _list_tree_md_files(project, rev, "docs/tasks/active"):
                if item not in candidates:
                    candidates.append(item)

    exact = [p for p in candidates if Path(p).stem == slug or Path(p).stem.endswith(f"-{slug}")]
    if len(exact) == 1:
        return exact[0]
    fuzzy = [p for p in candidates if slug in Path(p).stem]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        raise KeelError(
            f"Multiple active task files match '{slug}': {', '.join(fuzzy)}. Pass explicit --task-file <path>."
        )
    return valid_state_task


def _find_branch_worktree(project: Path, branch: str) -> Path | None:
    target_ref = f"refs/heads/{branch}"
    primary_root = primary_checkout_root(project)
    for entry in list_worktrees(project):
        wt_str = entry.get("worktree")
        if not wt_str or entry.get("branch") != target_ref:
            continue
        wt_path = Path(wt_str)
        if wt_path.is_dir():
            c_dir, g_dir = git_dirs(wt_path)
            if not same_path(c_dir, g_dir):
                return safe_resolve(wt_path)
        elif not same_path(wt_path, primary_root):
            return wt_path
    return None


def run_deterministic_checks(project: Path, head_sha: str, commands: Sequence[str]) -> dict[str, Any]:
    scratch_dir = Path(tempfile.mkdtemp(prefix="keel-verify-checks-"))
    results: list[dict[str, Any]] = []
    shell_bin = "/bin/bash" if Path("/bin/bash").is_file() else "/bin/sh"
    try:
        checkout_dir, _ = create_disposable_clone(project, head_sha, scratch_dir)
        all_ok = True
        for cmd in commands:
            proc = run_cmd(
                [shell_bin, "-c", cmd],
                cwd=checkout_dir,
                env=tool_env({"KEEL_AGENT_MODE": "read"}),
                check=False,
            )
            combined = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
            results.append(
                {
                    "command": cmd,
                    "returncode": proc.returncode,
                    "ok": proc.returncode == 0,
                    "output_tail": combined[-2000:] if combined else "",
                }
            )
            if proc.returncode != 0:
                all_ok = False
                break
        return {
            "configured": True,
            "ok": all_ok,
            "skipped": False,
            "skip_reason": None,
            "commands": results,
            "recorded_at": _now_iso(),
        }
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def _review_rounds(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Fold the last detailed verdict per vendor/SHA into its historical round on upgrade."""
    rounds = [dict(entry) for entry in state.get("rounds", [])]
    for sha, by_executor in state.get("verdicts_by_sha", {}).items():
        remaining = by_executor.copy()
        for entry in reversed(rounds):
            if not remaining:
                break
            if entry["head_sha"] == sha:
                for name in remaining.keys() & entry["verdicts"].keys():
                    entry["verdicts"][name] = remaining.pop(name)
    return rounds


@contextmanager
def edit_gate_state(common_dir: Path, branch: str) -> Iterator[dict[str, Any]]:
    """Use the shared Git lock and persist only the flat gate's fields, including on upgrade."""
    fields = ("branch", "task_file", "rounds", "last_write", "checks_by_sha", "landed_head_sha",
              "landed_sha", "landed_method", "discarded", "discard_reason")
    with locked_branch_state(common_dir, branch) as stored:
        state = {key: stored[key] for key in fields if key in stored}
        state["rounds"] = _review_rounds(stored)
        state.setdefault("checks_by_sha", {})
        state.setdefault("last_write", None)
        yield state
        stored.clear()
        stored.update(state)


def _run_and_record_checks(
    project: Path, common_dir: Path, branch: str, head_sha: str, commands: Sequence[str],
) -> dict[str, Any]:
    batch = run_deterministic_checks(project, head_sha, commands)
    with edit_gate_state(common_dir, branch) as state:
        checks_by_sha = state["checks_by_sha"]
        previous = checks_by_sha.get(head_sha, {})
        by_command = {c["command"]: c for c in previous.get("commands", [])}
        by_command.update((c["command"], c) for c in batch["commands"])
        # A fail-fast batch can declare new checks that it never reached.
        for command in commands:
            if command not in by_command:
                by_command[command] = {
                    "command": command, "ok": False, "returncode": None,
                    "output_tail": "Not run after an earlier check failed.",
                }
        results = list(by_command.values())
        merged = {**batch, "commands": results, "ok": all(c["ok"] for c in results)}
        checks_by_sha[head_sha] = merged
    return merged


def _record_skipped_checks(common_dir: Path, branch: str, head_sha: str, reason: str) -> None:
    with edit_gate_state(common_dir, branch) as state:
        checks_by_sha = state["checks_by_sha"]
        previous = checks_by_sha.get(head_sha) or {}
        checks_by_sha[head_sha] = {
            "configured": True,
            "ok": True,
            "skipped": True,
            "skip_reason": reason.strip(),
            "commands": list(previous.get("commands") or []),
            "recorded_at": _now_iso(),
        }


def _resolve_ladder(
    levels: Sequence[str] | None = None,
    verify_min_level: str | None = None,
) -> tuple[str, list[str]]:
    raw_levels = tuple(levels) if levels else VALID_LEVELS
    ordered = sorted({normalize_level(lvl) for lvl in raw_levels}, key=level_rank)
    if not ordered:
        ordered = list(VALID_LEVELS)
    min_lvl = normalize_level(verify_min_level or "L2")
    verify_tiers = [lvl for lvl in ordered if level_rank(lvl) >= level_rank(min_lvl)]
    if not verify_tiers:
        verify_tiers = [ordered[-1]]
    return verify_tiers[-1], verify_tiers


def _escalate_in_ladder(level: str, levels: Sequence[str] | None = None) -> str:
    norm = normalize_level(level)
    raw_levels = tuple(levels) if levels else VALID_LEVELS
    ordered = sorted({normalize_level(lvl) for lvl in raw_levels}, key=level_rank)
    if not ordered:
        return escalate_level(norm)
    cur_rank = level_rank(norm)
    if level_rank(ordered[-1]) < cur_rank:
        raise KeelConfigError(
            f"Required tier {norm} exceeds configured levels (max {ordered[-1]})."
        )
    for lvl in ordered:
        if level_rank(lvl) > cur_rank:
            return lvl
    return ordered[-1]


def _manual_baseline_level(verify_tiers: Sequence[str]) -> str:
    return "L3" if "L3" in verify_tiers else verify_tiers[-1]


def _ceil_in_tiers(level: str, tiers: Sequence[str]) -> str:
    norm = normalize_level(level)
    target = level_rank(norm)
    for tier in tiers:
        if level_rank(tier) >= target:
            return tier
    raise KeelConfigError(
        f"Verification requires --level {norm} or higher, "
        f"but configured verification tiers end at {tiers[-1]}."
    )


def _round_level(entry: dict[str, Any], *, default_level: str = "L3") -> str:
    # Reviews recorded before the tier ladder use the manual-review baseline.
    return normalize_level(entry.get("level") or entry.get("implement_level") or default_level)


def _convergence_cap_status(
    rounds: Sequence[dict[str, Any]],
    *,
    max_review_rounds: int = 2,
    levels: Sequence[str] | None = None,
    verify_min_level: str | None = None,
) -> dict[str, Any]:
    top_level, verify_tiers = _resolve_ladder(levels, verify_min_level)
    baseline = _manual_baseline_level(verify_tiers)
    max_top_rounds = int(max_review_rounds)
    max_total_rounds = len(verify_tiers[:-1]) + max_top_rounds
    rc_rounds = [r for r in rounds if r.get("outcome") == "request_changes"]
    total_rc = len(rc_rounds)
    top_rank = level_rank(top_level)
    top_rc = sum(1 for r in rc_rounds if level_rank(_round_level(r, default_level=baseline)) >= top_rank)
    return {
        "cap_reached": top_rc >= max_top_rounds or total_rc >= max_total_rounds,
        "top_level": top_level,
        "top_request_changes": top_rc,
        "max_top_rounds": max_top_rounds,
        "total_request_changes": total_rc,
        "max_total_rounds": max_total_rounds,
    }


def _convergence_cap_blocker(cap: dict[str, Any]) -> str:
    return (
        f"Reached convergence cap ({cap['top_request_changes']}/{cap['max_top_rounds']} rejected rounds "
        f"at {cap['top_level']}, {cap['total_request_changes']}/{cap['max_total_rounds']} total): "
        "stop looping and escalate unresolved blockers to the human."
    )


def _last_request_changes_round(rounds: Sequence[dict[str, Any]]) -> int:
    for idx in range(len(rounds), 0, -1):
        if rounds[idx - 1].get("outcome") == "request_changes":
            return idx
    return 0


def _matches_pure_rebase(
    project: Path | None,
    candidate_sha: str | None,
    current_merge_base: str | None,
    current_head_sha: str | None,
) -> bool:
    if (
        project is None
        or not candidate_sha
        or not current_merge_base
        or not current_head_sha
    ):
        return False
    cand = candidate_sha.strip().lower()
    mb = current_merge_base.strip().lower()
    head = current_head_sha.strip().lower()
    if not cand or not mb or not head or cand == head:
        return False
    old_base_proc = run_cmd(
        ["git", "merge-base", mb, cand],
        cwd=project,
        check=False,
    )
    if old_base_proc.returncode != 0 or not old_base_proc.stdout.strip():
        return False
    old_base = old_base_proc.stdout.strip().lower()
    if old_base == mb:
        return False
    try:
        compare_rebase(project, old_base, cand, mb, head)
    except KeelError:
        return False
    return True


def _pending_trees_match_pure_rebase(
    project: Path | None,
    base_commit_sha: str | None,
    pending_trees: Sequence[str],
    current_merge_base: str | None,
    current_head_sha: str | None,
) -> bool:
    if (
        project is None
        or not base_commit_sha
        or not pending_trees
        or not current_merge_base
        or not current_head_sha
    ):
        return False
    base_commit = base_commit_sha.strip().lower()
    mb = current_merge_base.strip().lower()
    head = current_head_sha.strip().lower()
    for sha in (base_commit, mb, head):
        if not _HEX_SHA_RE.fullmatch(sha):
            return False
        if run_cmd(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=project, check=False).returncode != 0:
            return False
    old_base_proc = run_cmd(
        ["git", "merge-base", mb, base_commit],
        cwd=project,
        check=False,
    )
    if old_base_proc.returncode != 0 or not old_base_proc.stdout.strip():
        return False
    old_base = old_base_proc.stdout.strip().lower()
    if old_base == mb:
        return False

    valid_trees = [
        t.strip().lower()
        for t in pending_trees
        if isinstance(t, str)
        and _HEX_SHA_RE.fullmatch(t.strip())
        and run_cmd(["git", "cat-file", "-e", f"{t.strip().lower()}^{{tree}}"], cwd=project, check=False).returncode == 0
    ]
    if not valid_trees:
        return False

    common_dir, _ = git_dirs(project)
    objects_dir = common_dir / "objects"
    with tempfile.TemporaryDirectory(prefix="keel-rebase-proof-") as tmp:
        for idx, ptree in enumerate(valid_trees):
            clean_repo = Path(tmp) / f"clean-{idx}"
            clean_repo.mkdir()
            run_cmd(["git", "init", "--quiet", "-b", "main", str(clean_repo)], env=_sanitized_git_env())
            alt_file = clean_repo / ".git" / "objects" / "info" / "alternates"
            alt_file.parent.mkdir(parents=True, exist_ok=True)
            alt_file.write_text(f"{objects_dir}\n", encoding="utf-8")
            ct_proc = run_cmd(
                [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "user.name=Keel Rebase Proof",
                    "-c",
                    "user.email=keel@localhost",
                    "commit-tree",
                    ptree,
                    "-p",
                    base_commit,
                    "-m",
                    "keel-pending",
                ],
                cwd=clean_repo,
                env=_sanitized_git_env(),
                check=False,
            )
            if ct_proc.returncode != 0 or not ct_proc.stdout.strip():
                continue
            cand_sha = ct_proc.stdout.strip().lower()
            try:
                _prove_rebase_in_clean_repo(clean_repo, old_base, cand_sha, mb, head)
                return True
            except KeelError:
                continue
    return False


def _last_write_is_fresh(
    last_write: dict[str, Any] | None,
    rounds: Sequence[dict[str, Any]],
    *,
    current_head_sha: str | None = None,
    current_tree_sha: str | None = None,
    common_dir: Path | None = None,
    project: Path | None = None,
    current_merge_base: str | None = None,
) -> bool:
    if not last_write or not last_write.get("level"):
        return False
    write_sha = last_write.get("head_sha")
    head_tree_sha = last_write.get("head_tree_sha")
    raw_pending = last_write.get("pending_tree_shas")
    pending_trees = raw_pending if isinstance(raw_pending, list) else []
    has_recorded_target = bool(write_sha or head_tree_sha or pending_trees)
    sha_matches = bool(write_sha and current_head_sha and write_sha == current_head_sha)
    tree_matches = bool(current_tree_sha and current_tree_sha in pending_trees)
    head_tree_matches = bool(
        head_tree_sha and current_tree_sha and current_tree_sha == head_tree_sha
    )
    direct_matches = sha_matches or tree_matches or head_tree_matches
    rebase_matches = (
        False
        if direct_matches
        else (
            _matches_pure_rebase(project, write_sha, current_merge_base, current_head_sha)
            or _pending_trees_match_pure_rebase(
                project, write_sha, pending_trees, current_merge_base, current_head_sha
            )
        )
    )
    target_matches = direct_matches or rebase_matches
    if has_recorded_target and current_head_sha and not target_matches:
        return False
    if not rounds:
        return True

    if "after_round" in last_write:
        after_round = last_write.get("after_round")
        written_after_latest = isinstance(after_round, int) and after_round >= len(rounds)
    else:
        reworked_round = last_write.get("reworked_round")
        write_ts = last_write.get("recorded_at")
        latest_ts = rounds[-1].get("recorded_at")
        written_after_latest = (
            (isinstance(reworked_round, int) and reworked_round >= len(rounds))
            or (isinstance(write_ts, str) and isinstance(latest_ts, str) and write_ts > latest_ts)
        )

    if rounds[-1].get("outcome") == "request_changes":
        return written_after_latest

    if written_after_latest:
        return True

    if has_recorded_target:
        return target_matches

    reworked_round = last_write.get("reworked_round")
    write_ts = last_write.get("recorded_at")
    if isinstance(reworked_round, int) and 0 <= reworked_round < len(rounds):
        anchor = rounds[reworked_round]
    elif isinstance(write_ts, str):
        anchor = next(
            (
                entry
                for entry in rounds
                if isinstance(entry.get("recorded_at"), str) and entry["recorded_at"] >= write_ts
            ),
            rounds[0],
        )
    else:
        anchor = rounds[0]

    anchor_impl = anchor.get("implement_level")
    if anchor_impl:
        if normalize_level(anchor_impl) != normalize_level(last_write["level"]):
            return False
    elif "implement_level" in anchor or _last_request_changes_round(rounds) != 0:
        return False

    anchor_sha = anchor.get("head_sha")
    if not (current_head_sha and isinstance(anchor_sha, str) and anchor_sha):
        return False
    if current_head_sha == anchor_sha:
        return True
    if common_dir is not None and current_tree_sha:
        at_proc = run_cmd(
            ["git", "--git-dir", str(common_dir), "rev-parse", f"{anchor_sha}^{{tree}}"],
            check=False,
        )
        if (
            at_proc.returncode == 0
            and at_proc.stdout.strip()
            and at_proc.stdout.strip().lower() == current_tree_sha
        ):
            return True
    return _matches_pure_rebase(project, anchor_sha, current_merge_base, current_head_sha)


def _rework_floor(
    rounds: Sequence[dict[str, Any]],
    *,
    levels: Sequence[str] | None = None,
    verify_min_level: str | None = None,
) -> str | None:
    if not rounds or rounds[-1].get("outcome") != "request_changes":
        return None
    start_idx = 0
    for idx in range(len(rounds) - 1, -1, -1):
        if rounds[idx].get("outcome") == "approve":
            start_idx = idx + 1
            break
    _, verify_tiers = _resolve_ladder(levels, verify_min_level)
    min_verify = verify_tiers[0]
    baseline = _manual_baseline_level(verify_tiers)
    active_floor: str | None = None
    for entry in rounds[start_idx:]:
        if entry.get("outcome") != "request_changes":
            continue
        raw_impl = entry.get("implement_level")
        impl = normalize_level(raw_impl) if raw_impl else None
        if impl and (active_floor is None or level_rank(impl) >= level_rank(active_floor)):
            base_level = max_level(impl, min_verify)
        else:
            # Missing `implement_level` (direct edit / legacy manual review) or stale legacy
            # `implement_level` below the already-active floor uses the review tier / active floor.
            review_lvl = normalize_level(entry.get("level") or baseline)
            base_level = max_level(review_lvl, active_floor) if active_floor else review_lvl
        active_floor = _escalate_in_ladder(base_level, levels)
    return active_floor


def _verify_floor(
    rounds: Sequence[dict[str, Any]],
    *,
    fresh_write: bool,
    levels: Sequence[str] | None = None,
    verify_min_level: str | None = None,
) -> str | None:
    floor = _rework_floor(rounds, levels=levels, verify_min_level=verify_min_level)
    if floor is None or fresh_write:
        return floor
    latest_level = rounds[-1].get("level")
    if latest_level:
        return max_level(floor, _escalate_in_ladder(latest_level, levels))
    return floor


def prepare_branch_write(
    workdir: Path, mode: str, level: str | None, defaults: dict[str, Any],
) -> tuple[Path, str] | None:
    if mode != "write":
        return None
    branch = run_cmd(["git", "branch", "--show-current"], cwd=workdir, check=False).stdout.strip()
    if not branch or branch in defaults.get("default_branches", ("main", "master")):
        return None
    if level is None:
        raise KeelConfigError(f"Resume with --level to record the implementation tier on '{branch}'.")
    common_dir, _ = git_dirs(workdir)
    state = load_branch_state(common_dir, branch)
    rounds = _review_rounds(state)
    floor = _rework_floor(
        rounds,
        levels=defaults.get("levels"),
        verify_min_level=defaults.get("verify_min_level"),
    )
    if floor and level_rank(level) < level_rank(floor):
        raise KeelConfigError(f"Rework on '{branch}' requires --level {floor} or higher.")
    return common_dir, branch


def _resolve_write_worktree(common_dir: Path, branch: str, result: dict[str, Any]) -> Path | None:
    raw_workdir = result.get("workdir")
    if isinstance(raw_workdir, str) and raw_workdir.strip():
        candidate = Path(raw_workdir)
        if candidate.is_dir():
            cur = run_cmd(["git", "branch", "--show-current"], cwd=candidate, check=False).stdout.strip()
            top = run_cmd(
                ["git", "rev-parse", "--path-format=absolute", "--show-toplevel"],
                cwd=candidate,
                check=False,
            )
            if cur == branch and top.returncode == 0 and top.stdout.strip():
                return safe_resolve(top.stdout.strip())
    target_ref = f"refs/heads/{branch}"
    wt_proc = run_cmd(
        ["git", "--git-dir", str(common_dir), "worktree", "list", "--porcelain"],
        check=False,
    )
    if wt_proc.returncode == 0:
        cur_wt: Path | None = None
        for line in wt_proc.stdout.splitlines():
            if line.startswith("worktree "):
                cur_wt = Path(line[len("worktree ") :].strip())
            elif line.startswith("branch ") and line[len("branch ") :].strip() == target_ref and cur_wt and cur_wt.is_dir():
                return safe_resolve(cur_wt)
            elif not line.strip():
                cur_wt = None
    return None


def _snapshot_pending_tree_shas(
    common_dir: Path,
    wt_path: Path,
    head_sha: str | None,
    *,
    head_tree_sha: str | None = None,
) -> list[str]:
    porcelain = run_cmd(["git", "status", "--porcelain", "-uall"], cwd=wt_path, check=False)
    if porcelain.returncode != 0 or not porcelain.stdout.strip():
        return []
    if head_tree_sha is None and head_sha:
        ht = run_cmd(["git", "--git-dir", str(common_dir), "rev-parse", f"{head_sha}^{{tree}}"], check=False)
        if ht.returncode == 0 and ht.stdout.strip():
            head_tree_sha = ht.stdout.strip().lower()

    idx_proc = run_cmd(
        ["git", "rev-parse", "--path-format=absolute", "--git-path", "index"],
        cwd=wt_path,
        check=False,
    )
    real_index = Path(idx_proc.stdout.strip()) if idx_proc.returncode == 0 and idx_proc.stdout.strip() else None

    with tempfile.TemporaryDirectory(prefix="keel-write-tree-") as tmp:
        tmp_dir = Path(tmp)
        tmp_objects = tmp_dir / "objects"
        (tmp_objects / "info").mkdir(parents=True, exist_ok=True)
        (tmp_objects / "pack").mkdir(parents=True, exist_ok=True)
        base_env = {
            k: v
            for k, v in tool_env().items()
            if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
        }
        base_env["GIT_TERMINAL_PROMPT"] = "0"
        base_env["GIT_OBJECT_DIRECTORY"] = str(tmp_objects)
        real_objects = str(common_dir / "objects")
        existing_alts = base_env.get("GIT_ALTERNATE_OBJECT_DIRECTORIES", "")
        base_env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = (
            f"{real_objects}{os.pathsep}{existing_alts}" if existing_alts else real_objects
        )
        git_base = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.quotePath=false"]

        staged_tree_sha: str | None = None
        if real_index and real_index.is_file():
            idx_staged = tmp_dir / "index_staged"
            shutil.copy2(real_index, idx_staged)
            st_proc = run_cmd(
                [*git_base, "write-tree"],
                cwd=wt_path,
                env={**base_env, "GIT_INDEX_FILE": str(idx_staged)},
                check=False,
            )
            if st_proc.returncode == 0 and st_proc.stdout.strip():
                staged_tree_sha = st_proc.stdout.strip().lower()

        tracked_tree_sha: str | None = None
        if (real_index and real_index.is_file()) or head_sha:
            idx_tracked = tmp_dir / "index_tracked"
            tr_env = {**base_env, "GIT_INDEX_FILE": str(idx_tracked)}
            if real_index and real_index.is_file():
                shutil.copy2(real_index, idx_tracked)
            elif head_sha:
                run_cmd([*git_base, "read-tree", head_sha], cwd=wt_path, env=tr_env, check=False)
            upd_proc = run_cmd([*git_base, "add", "-u"], cwd=wt_path, env=tr_env, check=False)
            if upd_proc.returncode == 0:
                tr_proc = run_cmd([*git_base, "write-tree"], cwd=wt_path, env=tr_env, check=False)
                if tr_proc.returncode == 0 and tr_proc.stdout.strip():
                    tracked_tree_sha = tr_proc.stdout.strip().lower()

        worktree_tree_sha: str | None = None
        idx_wt = tmp_dir / "index_wt"
        wt_env = {**base_env, "GIT_INDEX_FILE": str(idx_wt)}
        if real_index and real_index.is_file():
            shutil.copy2(real_index, idx_wt)
        elif head_sha:
            run_cmd([*git_base, "read-tree", head_sha], cwd=wt_path, env=wt_env, check=False)
        add_proc = run_cmd([*git_base, "add", "-A"], cwd=wt_path, env=wt_env, check=False)
        if add_proc.returncode == 0:
            wt_proc = run_cmd([*git_base, "write-tree"], cwd=wt_path, env=wt_env, check=False)
            if wt_proc.returncode == 0 and wt_proc.stdout.strip():
                worktree_tree_sha = wt_proc.stdout.strip().lower()

    return sorted(
        {t for t in (staged_tree_sha, tracked_tree_sha, worktree_tree_sha) if t and t != head_tree_sha}
    )


def record_branch_write(
    target: tuple[Path, str] | None,
    executor: str,
    result: dict[str, Any],
    *,
    defaults: dict[str, Any] | None = None,
) -> None:
    if target is None or result.get("status") != "completed":
        return
    common_dir, branch = target
    head_proc = run_cmd(
        ["git", "--git-dir", str(common_dir), "rev-parse", f"refs/heads/{branch}"],
        check=False,
    )
    head_sha = head_proc.stdout.strip().lower() if head_proc.returncode == 0 and head_proc.stdout.strip() else None
    head_tree_sha: str | None = None
    if head_sha:
        ht_proc = run_cmd(
            ["git", "--git-dir", str(common_dir), "rev-parse", f"{head_sha}^{{tree}}"],
            check=False,
        )
        if ht_proc.returncode == 0 and ht_proc.stdout.strip():
            head_tree_sha = ht_proc.stdout.strip().lower()
    wt_path = _resolve_write_worktree(common_dir, branch, result)
    pending_tree_shas = (
        _snapshot_pending_tree_shas(common_dir, wt_path, head_sha, head_tree_sha=head_tree_sha)
        if wt_path
        else []
    )
    level = result["level"]
    levels = defaults.get("levels") if defaults else None
    verify_min_level = defaults.get("verify_min_level") if defaults else None
    with edit_gate_state(common_dir, branch) as state:
        rounds = state["rounds"]
        write: dict[str, Any] = {
            "executor": executor,
            "level": level,
            "after_round": len(rounds),
            "recorded_at": _now_iso(),
        }
        if head_sha:
            write["head_sha"] = head_sha
        if head_tree_sha:
            write["head_tree_sha"] = head_tree_sha
        if pending_tree_shas:
            write["pending_tree_shas"] = pending_tree_shas
        floor = _rework_floor(rounds, levels=levels, verify_min_level=verify_min_level)
        if floor and level_rank(level) >= level_rank(floor):
            write["reworked_round"] = len(rounds)
        state["last_write"] = write


def _request_changes_count(rounds: Sequence[dict[str, Any]]) -> int:
    return sum(r["outcome"] == "request_changes" for r in rounds)


def _verify_one(
    executor: str, *, project: Path, branch: str, head_sha: str, slug: str, task: str,
    why: str, level: str, model: str | None, effort: str | None, defaults: dict[str, Any],
    caller_cwd: Path | None, output_dir: Path | None,
) -> dict[str, Any]:
    adapter = get_executor(executor)
    run_id = generate_run_id(f"verify-{slug}-{executor}")
    run_dir = runs_root_dir(project, Path(tempfile.gettempdir())) / run_id
    run_dir.mkdir(parents=True)
    scratch = output_dir or Path(tempfile.mkdtemp(prefix=f"keel-verify-{slug}-{executor}-"))
    try:
        checkout, git_dir = create_disposable_clone(project, head_sha, scratch)
        schema = SCHEMAS_DIR / "verdict.schema.json"
        verdict_file = (output_dir or run_dir) / "verdict.json"
        if verdict_file.exists():
            raise KeelError(f"Verdict output already exists: {verdict_file}")
        req = RunRequest(
            run_id=run_id, run_dir=run_dir, brief_path=run_dir / "brief.md", project_root=project,
            workdir=checkout, mode="read", preset="verify", level=level, model=model, effort=effort,
            schema_path=schema, output_path=verdict_file, extra_git_dirs=(git_dir,), scratch_verify=True,
        )
        req.brief_path.write_text(assemble_brief(
            why=why, task=task, preset=req.preset, mode=req.mode, workdir=checkout, project_root=project,
            native_agent_docs=adapter.native_agent_docs, loads_plugin_spine=adapter.loads_plugin_spine,
            caller_cwd=caller_cwd, schema_path=schema, output_path=verdict_file,
            inject_schema_in_brief=adapter.inject_schema_in_brief,
        ), encoding="utf-8")
        record = {
            **req.run_record(executor), "branch": branch, "head_sha": head_sha, "ephemeral": True,
        }
        try:
            record.update(adapter.execute(req, defaults))
            if record.get("status") != "completed":
                raise KeelError(f"Verifier {executor} did not complete.")
            verdict = validate_verdict_payload(json.loads(verdict_file.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeelError) as exc:
            if isinstance(exc, KeelExecutionError):
                record.update(exc.partial)
            save_run_record(run_dir, {**record, "status": "failed"})
            if isinstance(exc, KeelError):
                raise
            raise KeelError(f"Invalid verifier output from {executor}: {exc}") from exc
        save_run_record(run_dir, record)
        return {"executor": executor, "run_id": run_id, "status": "completed",
                "model": record.get("model"), "level": level, "verdict": verdict}
    finally:
        if output_dir is None:
            shutil.rmtree(scratch)


def verify_branch(
    *,
    project: Path,
    branch_or_slug: str,
    defaults: dict[str, Any],
    base: str | None = None,
    executor: str | None = None,
    dual: bool = False,
    level: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    checks: Sequence[str] = (),
    skip_checks: str | None = None,
    task_file_arg: str | None = None,
    why: str | None = None,
    context_file: str | None = None,
    output_dir: str | None = None,
    force_round: bool = False,
    caller_cwd: Path | None = None,
) -> dict[str, Any]:
    if checks and skip_checks:
        raise KeelError("Cannot combine --check and --skip-checks.")
    if skip_checks is not None and not skip_checks.strip():
        raise KeelError("--skip-checks requires a non-empty reason.")

    if dual and any(value is not None for value in (executor, model, effort)):
        raise KeelConfigError("--dual selects both executors. Use --level to select their tier.")

    project = primary_checkout_root(project)
    ensure_agents_dir_ignored(project)
    common_dir, _ = git_dirs(project)
    branch, slug = resolve_branch_and_slug(project, branch_or_slug, must_exist=True)
    default_branches = tuple(defaults.get("default_branches", ["main", "master"]))
    base_ref = resolve_base_ref(project, base, default_branches=default_branches)

    head_sha = run_cmd(["git", "rev-parse", f"refs/heads/{branch}"], cwd=project).stdout.strip().lower()
    head_tree_sha = run_cmd(["git", "rev-parse", f"{head_sha}^{{tree}}"], cwd=project).stdout.strip().lower()
    base_tip_sha = run_cmd(["git", "rev-parse", f"{base_ref}^{{commit}}"], cwd=project).stdout.strip().lower()
    merge_base_sha = run_cmd(["git", "merge-base", base_tip_sha, head_sha], cwd=project).stdout.strip().lower()

    linked_wt = _find_branch_worktree(project, branch)
    if linked_wt is None and run_cmd(["git", "branch", "--show-current"], cwd=project).stdout.strip() == branch:
        linked_wt = project
    if linked_wt is not None and linked_wt.is_dir():
        porcelain = run_cmd(["git", "status", "--porcelain", "-uall"], cwd=linked_wt).stdout.strip()
        if porcelain:
            raise KeelError(f"Worktree {linked_wt} has uncommitted or untracked changes:\n{porcelain}")
        wt_head = run_cmd(["git", "rev-parse", "HEAD"], cwd=linked_wt).stdout.strip().lower()
        if wt_head != head_sha:
            raise KeelError(f"Worktree {linked_wt} HEAD ({wt_head[:12]}) differs from '{branch}' tip ({head_sha[:12]}).")

    initial_state = load_branch_state(common_dir, branch)
    max_rounds = int(defaults.get("max_review_rounds", 2))
    raw_cfg_levels = defaults.get("levels")
    if raw_cfg_levels is not None:
        if not isinstance(raw_cfg_levels, (list, tuple)) or not raw_cfg_levels:
            raise KeelConfigError("Expected a non-empty levels list in defaults.json.")
        configured_levels = [normalize_level(lvl) for lvl in raw_cfg_levels]
    else:
        configured_levels = list(VALID_LEVELS)
    verify_min_level = normalize_level(defaults.get("verify_min_level") or "L2")

    def check_round_limit(state: dict[str, Any]) -> None:
        cap = _convergence_cap_status(
            state.get("rounds", []),
            max_review_rounds=max_rounds,
            levels=configured_levels,
            verify_min_level=verify_min_level,
        )
        if cap["cap_reached"] and not force_round:
            raise KeelError(
                f"Branch '{branch}' reached its convergence cap "
                f"({cap['top_request_changes']}/{cap['max_top_rounds']} rejected rounds at {cap['top_level']}, "
                f"{cap['total_request_changes']}/{cap['max_total_rounds']} total). "
                "Stop looping and escalate unresolved blockers to the human."
            )

    check_round_limit(initial_state)
    _, verify_tiers = _resolve_ladder(configured_levels, verify_min_level)
    default_manual_level = _manual_baseline_level(verify_tiers)
    initial_rounds = _review_rounds(initial_state)
    last_write = initial_state.get("last_write") or {}
    fresh_write = _last_write_is_fresh(
        last_write,
        initial_rounds,
        current_head_sha=head_sha,
        current_tree_sha=head_tree_sha,
        common_dir=common_dir,
        project=project,
        current_merge_base=merge_base_sha,
    )
    implement_level = normalize_level(last_write["level"]) if fresh_write else None
    floor = _verify_floor(
        initial_rounds,
        fresh_write=fresh_write,
        levels=configured_levels,
        verify_min_level=verify_min_level,
    )
    minimum = verify_min_level
    if implement_level:
        minimum = max_level(minimum, implement_level)
    if floor:
        minimum = max_level(minimum, floor)
    minimum = _ceil_in_tiers(minimum, verify_tiers)
    if level is None:
        level = minimum if implement_level else max_level(minimum, default_manual_level)
    level = normalize_level(level, defaults={"levels": configured_levels})
    if level_rank(level) < level_rank(minimum):
        raise KeelConfigError(f"Verification requires --level {minimum} or higher, got {level}.")

    task_file = discover_task_file(
        project,
        branch,
        slug,
        base_ref=base_ref,
        head_sha=head_sha,
        explicit_task_file=task_file_arg,
        state_task_file=initial_state.get("task_file"),
    )
    if task_file and task_file != initial_state.get("task_file"):
        with edit_gate_state(common_dir, branch) as state:
            state["task_file"] = task_file

    # 1. Deterministic checks run unlocked, then merge into latest state under `flock`
    if checks:
        chk_res = _run_and_record_checks(project, common_dir, branch, head_sha, checks)
        if not chk_res["ok"]:
            failed = next(c for c in chk_res["commands"] if not c["ok"])
            if failed["returncode"] is None:
                raise KeelError(f"Required deterministic check has not run: {failed['command']}")
            raise KeelError(
                f"Deterministic check failed ({failed['returncode']}): {failed['command']}\n{failed['output_tail']}".strip()
            )
    elif skip_checks:
        _record_skipped_checks(common_dir, branch, head_sha, skip_checks)

    # 2. Build locked task spec & verification context
    locked_spec: str | None = None
    if task_file:
        locked_spec = _read_blob_text(project, base_tip_sha, task_file)
        if locked_spec is None and task_file.startswith("docs/tasks/active/"):
            todo_equiv = "docs/tasks/todo/" + Path(task_file).name
            locked_spec = _read_blob_text(project, base_tip_sha, todo_equiv)
    branch_spec = _read_blob_text(project, head_sha, task_file) if task_file else None
    task_blob_sha = _effective_task_blob_sha(project, base_tip_sha, head_sha, task_file)

    commit_log = run_cmd(["git", "log", "--oneline", f"{merge_base_sha}..{head_sha}"], cwd=project).stdout.strip()
    extra_ctx = Path(context_file).read_text(encoding="utf-8").strip() if context_file else ""
    why_text = why or (
        f"Independently verify branch '{branch}' against the repository's Core need and locked task bounds before landing onto '{base_ref}'."
    )

    task_lines = [
        f"- **Branch:** `{branch}`",
        f"- **Target base:** `{base_ref}` (`{base_tip_sha}`)",
        f"- **Pinned head commit:** `{head_sha}`",
        f"- **Merge-base commit:** `{merge_base_sha}` (inspect full diff via `git diff {merge_base_sha}..{head_sha}`)",
    ]
    if task_file:
        task_lines.append(f"- **Task ledger file:** `{task_file}`")
    if commit_log:
        task_lines.extend(["", "### Commits Under Review", f"```\n{commit_log}\n```"])
    if locked_spec:
        task_lines.extend(
            [
                "",
                f"### Locked Task Specification on `{base_ref}` (verify `Goal & Why`, `Bounds`, and `Done-check` were NOT weakened)",
                locked_spec,
            ]
        )
    elif branch_spec:
        task_lines.extend(["", f"### Branch Task Specification (`{task_file}`)", branch_spec])
    if extra_ctx:
        task_lines.extend(["", "### Additional Context", extra_ctx])

    def review(name: str) -> dict[str, Any]:
        dest = safe_resolve(output_dir) if output_dir else None
        return _verify_one(
            name, project=project, branch=branch, head_sha=head_sha, slug=slug.replace("/", "-"),
            task="\n".join(task_lines), why=why_text, level=level, model=model, effort=effort,
            defaults=defaults, caller_cwd=caller_cwd, output_dir=dest / name if dual and dest else dest,
        )

    if dual:
        with forward_signals_to_child_groups():
            with ThreadPoolExecutor(max_workers=2) as pool:
                runs = {run["executor"]: run for run in pool.map(review, ("codex", "agy"))}
            check_interrupted()
    else:
        diagonal = {"agy": "codex", "codex": "agy"}
        name = executor or diagonal.get(last_write.get("executor")) or defaults["routing"]["verify"]
        single_run = review(name)
        runs = {name: single_run}

    verdicts = {name: run["verdict"] for name, run in runs.items()}
    approved = all(verdict["verdict"] == "approve" for verdict in verdicts.values())
    outcome = "approve" if approved else "request_changes"
    with edit_gate_state(common_dir, branch) as state:
        check_round_limit(state)
        state["rounds"].append({
            "head_sha": head_sha, "merge_base_sha": merge_base_sha, "task_file": task_file,
            "task_blob_sha": task_blob_sha, "dual": dual, "level": level,
            "implement_level": implement_level, "forced_round": bool(force_round),
            "outcome": outcome, "verdicts": verdicts,
            "recorded_at": _now_iso(),
        })
        if task_file:
            state["task_file"] = task_file
        rejected = _request_changes_count(state["rounds"])
    return {
        "branch": branch, "slug": slug, "base_ref": base_ref, "head_sha": head_sha,
        "task_file": task_file, "level": level, "dual": dual, "forced_round": bool(force_round),
        "outcome": outcome, "request_changes_rounds": rejected,
        **({"verifiers": runs} if dual else single_run),
    }


def _validate_old_rebase_pair(project: Path | None, old_base: str | None, old_head: str | None) -> None:
    has_base = bool(old_base and old_base.strip())
    has_head = bool(old_head and old_head.strip())
    if (old_base is not None or old_head is not None) and (not has_base or not has_head):
        raise KeelError("--old-base and --old-head must both be provided together.")
    if has_base and has_head:
        assert old_base is not None and old_head is not None
        for label, raw_sha in (("old-base", old_base.strip()), ("old-head", old_head.strip())):
            if not _HEX_SHA_RE.fullmatch(raw_sha):
                raise KeelError(f"--{label} must be a full 40/64-character commit SHA (got '{raw_sha}').")
            if project is not None:
                chk = run_cmd(["git", "cat-file", "-e", f"{raw_sha}^{{commit}}"], cwd=project, check=False)
                if chk.returncode != 0:
                    raise KeelError(f"Commit for --{label} ({raw_sha}) is not present in {project}.")


def evaluate_branch_gate(
    project: Path,
    branch_or_slug: str,
    *,
    base: str | None = None,
    old_base: str | None = None,
    old_head: str | None = None,
    human_override: str | None = None,
    task_file_arg: str | None = None,
    max_review_rounds: int = 2,
    levels: Sequence[str] = VALID_LEVELS,
    verify_min_level: str = "L2",
    default_branches: Sequence[str] = ("main", "master"),
    require_clean_checkouts: bool = False,
    pinned_head_sha: str | None = None,
    pinned_base_tip_sha: str | None = None,
    state_override: dict[str, Any] | None = None,
    precomputed_rebase_proof: dict[str, Any] | None = None,
) -> dict[str, Any]:
    project = primary_checkout_root(project)
    _validate_old_rebase_pair(project, old_base, old_head)
    common_dir, _ = git_dirs(project)
    branch, slug = resolve_branch_and_slug(project, branch_or_slug, must_exist=True)
    base_ref = resolve_base_ref(project, base, default_branches=tuple(default_branches))

    live_head_sha = run_cmd(["git", "rev-parse", f"refs/heads/{branch}"], cwd=project).stdout.strip().lower()
    live_base_tip_sha = _resolve_ref_tip_sha(project, base_ref)
    head_sha = pinned_head_sha or live_head_sha
    base_tip_sha = pinned_base_tip_sha or live_base_tip_sha

    has_ahead_commits = head_sha != base_tip_sha
    anc = run_cmd(["git", "merge-base", "--is-ancestor", base_tip_sha, head_sha], cwd=project, check=False)
    ancestry_ok = anc.returncode == 0 and has_ahead_commits
    mb_proc = run_cmd(["git", "merge-base", base_tip_sha, head_sha], cwd=project, check=False)
    merge_base_sha = mb_proc.stdout.strip().lower() if mb_proc.returncode == 0 else ""
    has_tree_diff = (
        has_ahead_commits
        and run_cmd(["git", "diff", "--quiet", base_tip_sha, head_sha], cwd=project, check=False).returncode != 0
    )

    state = state_override if state_override is not None else load_branch_state(common_dir, branch)
    task_file = discover_task_file(
        project,
        branch,
        slug,
        base_ref=base_ref,
        head_sha=head_sha,
        explicit_task_file=task_file_arg,
        state_task_file=state.get("task_file"),
    )

    checks_map = state.get("checks_by_sha") or {}
    checks_for_head = checks_map.get(head_sha) or {"configured": False, "ok": True, "skipped": False, "commands": []}
    rounds = _review_rounds(state)

    def latest_for(sha: str) -> dict[str, Any]:
        return next((entry for entry in reversed(rounds) if entry["head_sha"] == sha), {})

    current = latest_for(head_sha)
    cap = _convergence_cap_status(
        rounds,
        max_review_rounds=max_review_rounds,
        levels=levels,
        verify_min_level=verify_min_level,
    )
    rc_rounds = cap["total_request_changes"]

    approval_source: str | None = None
    rebase_proof: dict[str, Any] | None = None
    blockers: list[str] = []

    if pinned_head_sha is not None and live_head_sha != pinned_head_sha:
        blockers.append(
            f"Branch '{branch}' tip moved during execution ({pinned_head_sha[:12]} -> {live_head_sha[:12]})."
        )
    if pinned_base_tip_sha is not None and live_base_tip_sha != pinned_base_tip_sha:
        blockers.append(
            f"Base '{base_ref}' tip moved during execution ({pinned_base_tip_sha[:12]} -> {live_base_tip_sha[:12]})."
        )

    if not has_ahead_commits:
        blockers.append(f"Branch '{branch}' ({head_sha[:12]}) has no commits ahead of '{base_ref}'.")
    elif not ancestry_ok:
        blockers.append(
            f"Branch '{branch}' ({head_sha[:12]}) is behind '{base_ref}' tip ({base_tip_sha[:12]}); rebase onto '{base_ref}'."
        )
    elif not has_tree_diff:
        blockers.append(f"Branch '{branch}' ({head_sha[:12]}) has an empty net diff against '{base_ref}'.")

    if checks_for_head.get("configured") and not checks_for_head.get("ok"):
        outstanding = [c["command"] for c in checks_for_head.get("commands", []) if not c["ok"]]
        blockers.append("\n".join([f"Deterministic checks failed on commit {head_sha[:12]}.", *outstanding]))

    missing_rebase_checks: set[str] = set()
    if old_base and old_head:
        old_head_lc = old_head.strip().lower()
        old_checks = checks_map.get(old_head_lc) or {}
        previous_commands = {item["command"] for item in old_checks.get("commands", [])}
        rerun_commands = {item["command"] for item in checks_for_head.get("commands", []) if item["ok"]}
        missing_rebase_checks = previous_commands - rerun_commands
        if missing_rebase_checks:
            missing = ", ".join(sorted(missing_rebase_checks))
            blockers.append(
                f"Pure rebase from {old_head_lc[:12]} requires re-running deterministic checks "
                f"on {head_sha[:12]}: {missing}"
            )

    expected_task_blob = _effective_task_blob_sha(project, base_tip_sha, head_sha, task_file)

    def _round_matches_scope(entry: dict[str, Any], *, expected_merge_base: str) -> bool:
        return (
            "task_file" in entry
            and entry["task_file"] == task_file
            and "task_blob_sha" in entry
            and entry["task_blob_sha"] == expected_task_blob
            and bool(expected_merge_base)
            and entry.get("merge_base_sha") == expected_merge_base
        )

    if current.get("outcome") == "approve":
        if _round_matches_scope(current, expected_merge_base=merge_base_sha):
            source = "dual-approve" if current.get("dual") else "direct-approve"
            approval_source = f"{source}:{head_sha[:12]}"
        elif human_override and human_override.strip():
            approval_source = f"human-override:{human_override.strip()}"
        else:
            blockers.append(
                f"Latest approval on commit {head_sha[:12]} does not match target merge-base "
                f"({merge_base_sha[:12] or 'none'}) and locked task criteria ({task_file!r}); run fresh verification."
            )
            if cap["cap_reached"]:
                blockers.append(_convergence_cap_blocker(cap))
    elif current:
        if human_override and human_override.strip():
            approval_source = f"human-override:{human_override.strip()}"
        else:
            blockers.append(f"Commit {head_sha[:12]} carries an active '{current['outcome']}' verdict.")
            if cap["cap_reached"]:
                blockers.append(_convergence_cap_blocker(cap))
    elif old_base and old_head:
        old_base_lc = old_base.strip().lower()
        old_head_lc = old_head.strip().lower()
        old_round = latest_for(old_head_lc)
        if old_round.get("outcome") != "approve":
            raise KeelError(f"--old-head {old_head_lc[:12]} does not carry complete 'approve' verification for '{branch}'.")
        rebase_proof = (
            precomputed_rebase_proof
            if precomputed_rebase_proof is not None
            else compare_rebase(project, old_base_lc, old_head_lc, base_tip_sha, head_sha)
        )
        if not _round_matches_scope(old_round, expected_merge_base=old_base_lc):
            blockers.append(
                f"Approval on {old_head_lc[:12]} does not match target base ({old_base_lc[:12]}) or "
                f"revised task criteria ({task_file!r}); run fresh verification."
            )
            if cap["cap_reached"]:
                blockers.append(_convergence_cap_blocker(cap))
        elif not missing_rebase_checks:
            approval_source = f"pure-rebase-from:{old_head_lc[:12]}"
    elif human_override and human_override.strip():
        approval_source = f"human-override:{human_override.strip()}"
    else:
        if cap["cap_reached"]:
            blockers.append(_convergence_cap_blocker(cap))
        else:
            blockers.append(f"No independent 'approve' verdict recorded for commit {head_sha[:12]}.")

    if require_clean_checkouts:
        primary_branch = run_cmd(["git", "branch", "--show-current"], cwd=project).stdout.strip()
        if primary_branch != base_ref:
            blockers.append(f"Primary checkout {project} is on '{primary_branch or 'detached HEAD'}', expected '{base_ref}'.")
        primary_porcelain = run_cmd(["git", "status", "--porcelain", "-uall"], cwd=project).stdout.strip()
        if primary_porcelain:
            blockers.append(f"Primary checkout {project} has uncommitted or untracked changes.")
        linked_wt = _find_branch_worktree(project, branch)
        if linked_wt is not None and linked_wt.is_dir():
            wt_porcelain = run_cmd(["git", "status", "--porcelain", "-uall"], cwd=linked_wt).stdout.strip()
            if wt_porcelain:
                blockers.append(f"Worktree {linked_wt} has uncommitted or untracked changes.")
            wt_head = run_cmd(["git", "rev-parse", "HEAD"], cwd=linked_wt).stdout.strip().lower()
            if wt_head != head_sha:
                blockers.append(f"Worktree {linked_wt} HEAD ({wt_head[:12]}) differs from '{branch}' tip ({head_sha[:12]}).")

    return {
        "branch": branch,
        "slug": slug,
        "head_sha": head_sha,
        "base_ref": base_ref,
        "base_tip_sha": base_tip_sha,
        "ancestry_ok": ancestry_ok,
        "task_file": task_file,
        "checks": checks_for_head,
        "verdicts": current.get("verdicts", {}),
        "approval_source": approval_source,
        "rebase_proof": rebase_proof,
        "request_changes_rounds": rc_rounds,
        "ready_to_land": len(blockers) == 0,
        "blockers": blockers,
    }


def branch_repo_overview(
    project: Path,
    *,
    base: str | None = None,
    max_review_rounds: int = 2,
    levels: Sequence[str] = VALID_LEVELS,
    verify_min_level: str = "L2",
    default_branches: Sequence[str] = ("main", "master"),
) -> dict[str, Any]:
    project = primary_checkout_root(project)
    base_ref = resolve_base_ref(project, base, default_branches=tuple(default_branches))
    base_tip_sha = run_cmd(["git", "rev-parse", f"{base_ref}^{{commit}}"], cwd=project).stdout.strip().lower()
    todo_tasks = _list_tree_md_files(project, base_ref, "docs/tasks/todo")
    active_tasks = _list_tree_md_files(project, base_ref, "docs/tasks/active")

    wt_summaries: list[dict[str, Any]] = []
    for entry in list_worktrees(project):
        wt_str = entry.get("worktree")
        if not wt_str or same_path(wt_str, project):
            continue
        wt_path = Path(wt_str)
        raw_branch = entry.get("branch") or ""
        bname = raw_branch[11:] if raw_branch.startswith("refs/heads/") else ""
        clean = not bool(run_cmd(["git", "status", "--porcelain", "-uall"], cwd=wt_path).stdout.strip()) if wt_path.is_dir() else False
        gate_summary: dict[str, Any] | None = None
        active_task: str | None = None
        if bname:
            gate = evaluate_branch_gate(
                project,
                bname,
                base=base_ref,
                max_review_rounds=max_review_rounds,
                levels=levels,
                verify_min_level=verify_min_level,
                default_branches=default_branches,
            )
            active_task = gate["task_file"]
            gate_summary = {
                "ready_to_land": gate["ready_to_land"],
                "approval_source": gate["approval_source"],
                "request_changes_rounds": gate["request_changes_rounds"],
                "blockers": gate["blockers"],
            }
        wt_summaries.append(
            {
                "path": str(wt_path),
                "branch": bname or None,
                "head_sha": entry.get("HEAD"),
                "clean": clean,
                "active_task_file": active_task,
                "gate": gate_summary,
            }
        )

    return {
        "base_ref": base_ref,
        "base_tip_sha": base_tip_sha,
        "todo_tasks": todo_tasks,
        "active_tasks_on_base": active_tasks,
        "worktrees": wt_summaries,
    }


def extract_commit_trailers(project: Path, rev_range: str) -> list[str]:
    raw = run_cmd(["git", "log", "--reverse", "--format=%B%x00", rev_range], cwd=project).stdout
    seen: list[str] = []
    prefixes = ("co-authored-by:", "antigravity-session:", "codex-session:")
    for msg in raw.split("\x00"):
        for raw_line in msg.splitlines():
            line = raw_line.strip()
            if line.lower().startswith(prefixes) and line not in seen:
                seen.append(line)
    return seen


def _resolve_ref_tip_sha(project: Path, ref_name: str) -> str:
    if run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{ref_name}"], cwd=project, check=False).returncode == 0:
        return run_cmd(["git", "rev-parse", f"refs/heads/{ref_name}"], cwd=project).stdout.strip().lower()
    return run_cmd(["git", "rev-parse", f"{ref_name}^{{commit}}"], cwd=project).stdout.strip().lower()


def _assert_pinned_refs(
    project: Path,
    branch: str,
    base_ref: str,
    pinned_head_sha: str,
    pinned_base_tip_sha: str | None = None,
    *,
    require_clean_worktree: bool = True,
) -> None:
    live_head = run_cmd(["git", "rev-parse", f"refs/heads/{branch}"], cwd=project).stdout.strip().lower()
    if live_head != pinned_head_sha:
        raise KeelError(
            f"Branch '{branch}' moved during land ({pinned_head_sha[:12]} -> {live_head[:12]}); "
            "aborting without merging or deleting unverified commits."
        )
    if pinned_base_tip_sha is not None:
        live_base = _resolve_ref_tip_sha(project, base_ref)
        if live_base != pinned_base_tip_sha:
            raise KeelError(
                f"Base '{base_ref}' moved during land ({pinned_base_tip_sha[:12]} -> {live_base[:12]}); "
                "re-verify ancestry before landing."
            )
    if require_clean_worktree:
        linked_wt = _find_branch_worktree(project, branch)
        if linked_wt is not None and linked_wt.is_dir():
            wt_porcelain = run_cmd(["git", "status", "--porcelain", "-uall"], cwd=linked_wt).stdout.strip()
            if wt_porcelain:
                raise KeelError(f"Worktree {linked_wt} has uncommitted or untracked changes:\n{wt_porcelain}")
            wt_head = run_cmd(["git", "rev-parse", "HEAD"], cwd=linked_wt).stdout.strip().lower()
            if wt_head != pinned_head_sha:
                raise KeelError(
                    f"Worktree {linked_wt} HEAD ({wt_head[:12]}) differs from verified commit ({pinned_head_sha[:12]})."
                )


def _remove_worktree_and_branch_after_land(
    project: Path,
    branch: str,
    *,
    base_ref: str,
    pinned_head_sha: str | None = None,
    pinned_base_tip_sha: str | None = None,
    keep_branch: bool,
    discard: bool = False,
) -> dict[str, Any]:
    """Tear down ONLY a worktree actually checked out on `branch`, and delete `refs/heads/{branch}`."""
    if not discard and pinned_head_sha is not None:
        _assert_pinned_refs(
            project,
            branch,
            base_ref,
            pinned_head_sha,
            pinned_base_tip_sha,
            require_clean_worktree=True,
        )

    linked_wt = _find_branch_worktree(project, branch)
    removed_wt: str | None = None
    deleted_branch: str | None = None

    if linked_wt is not None and linked_wt.exists():
        wt_root = (project / ".agents" / "worktrees").resolve(strict=False)
        if linked_wt.parent.resolve(strict=False) == wt_root:
            res = cleanup_worktree(
                project,
                linked_wt.name,
                base_ref=base_ref,
                expected_branch=branch,
                merged_head_sha=pinned_head_sha if not discard else None,
                keep_branch=keep_branch,
                discard=discard,
            )
            removed_wt = res["removed_worktree"]
            deleted_branch = res["deleted_branch"]
        else:
            if not discard:
                porcelain = run_cmd(["git", "status", "--porcelain", "-uall"], cwd=linked_wt).stdout.strip()
                if porcelain:
                    raise KeelError(f"Worktree {linked_wt} has uncommitted or untracked changes:\n{porcelain}")
            run_cmd(["git", "worktree", "remove", "--force", str(linked_wt)], cwd=project)
            removed_wt = str(linked_wt)

    if (
        deleted_branch is None
        and not keep_branch
        and run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=project, check=False).returncode
        == 0
    ):
        if not discard and pinned_head_sha is not None:
            live_tip = run_cmd(["git", "rev-parse", f"refs/heads/{branch}"], cwd=project).stdout.strip().lower()
            if live_tip != pinned_head_sha:
                raise KeelError(
                    f"Refusing to delete branch '{branch}': tip moved ({pinned_head_sha[:12]} -> {live_tip[:12]})."
                )
        run_cmd(["git", "branch", "-D", branch], cwd=project)
        deleted_branch = branch
    return {"removed_worktree": removed_wt, "deleted_branch": deleted_branch}


def land_branch(
    *,
    project: Path,
    branch_or_slug: str,
    defaults: dict[str, Any],
    base: str | None = None,
    strategy: str = "squash",
    old_base: str | None = None,
    old_head: str | None = None,
    checks: Sequence[str] = (),
    skip_checks: str | None = None,
    human_override: str | None = None,
    task_file_arg: str | None = None,
    title: str | None = None,
    keep_branch: bool = False,
    execute: bool = False,
    discard: bool = False,
    discard_reason: str | None = None,
) -> dict[str, Any]:
    if execute and discard:
        raise KeelError("Cannot combine --execute and --discard.")
    if checks and skip_checks:
        raise KeelError("Cannot combine --check and --skip-checks.")

    project = primary_checkout_root(project)
    _validate_old_rebase_pair(project, old_base, old_head)
    common_dir, _ = git_dirs(project)
    branch, slug = resolve_branch_and_slug(project, branch_or_slug, must_exist=True)
    default_branches = tuple(defaults.get("default_branches", ["main", "master"]))
    base_ref = resolve_base_ref(project, base, default_branches=default_branches)
    pinned_head_sha = run_cmd(["git", "rev-parse", f"refs/heads/{branch}"], cwd=project).stdout.strip().lower()
    pinned_base_tip_sha = _resolve_ref_tip_sha(project, base_ref)

    # 1. Discard path (`--discard --reason "<why>"`), serialized under repo-wide `land.lock`
    if discard:
        if not discard_reason or not discard_reason.strip():
            raise KeelError("--discard requires a non-empty --reason '<why>'.")
        clean_reason = discard_reason.strip()

        with locked_repo_land(common_dir):
            initial_state = load_branch_state(common_dir, branch)
            primary_branch = run_cmd(["git", "branch", "--show-current"], cwd=project).stdout.strip()
            if primary_branch != base_ref:
                raise KeelError(
                    f"Primary checkout {project} must be on '{base_ref}' (currently '{primary_branch or 'detached'}')."
                )
            porcelain = run_cmd(["git", "status", "--porcelain", "-uall"], cwd=project).stdout.strip()
            if porcelain:
                raise KeelError(f"Primary checkout {project} has uncommitted or untracked changes:\n{porcelain}")

            task_file = discover_task_file(
                project,
                branch,
                slug,
                base_ref=base_ref,
                head_sha=pinned_head_sha,
                explicit_task_file=task_file_arg,
                state_task_file=initial_state.get("task_file"),
            )
            archived_task: str | None = None
            discard_commit_sha: str | None = None
            if task_file:
                base_task_path = project / task_file
                task_name = Path(task_file).name
                task_stem = Path(task_file).stem
                archive_rel = f"docs/tasks/archive/{task_name}"
                archive_abs = project / archive_rel
                archive_abs.parent.mkdir(parents=True, exist_ok=True)

                source_text: str | None = None
                if base_task_path.is_file():
                    source_text = base_task_path.read_text(encoding="utf-8")
                    run_cmd(["git", "rm", "-f", "--", task_file], cwd=project)
                else:
                    source_text = _read_blob_text(project, pinned_head_sha, task_file)
                    todo_rel = f"docs/tasks/todo/{task_name}"
                    if (project / todo_rel).is_file():
                        if source_text is None:
                            source_text = (project / todo_rel).read_text(encoding="utf-8")
                        run_cmd(["git", "rm", "-f", "--", todo_rel], cwd=project)

                if source_text is not None:
                    updated_text = source_text.rstrip() + f"\n\n## Outcome: [DISCARDED]\n\n- **Reason:** {clean_reason}\n"
                    archive_abs.write_text(updated_text, encoding="utf-8")
                    run_cmd(["git", "add", "--", archive_rel], cwd=project)
                    run_cmd(["git", "commit", "-m", f"docs(tasks): discard {task_stem}"], cwd=project)
                    discard_commit_sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=project).stdout.strip().lower()
                    archived_task = archive_rel

            with edit_gate_state(common_dir, branch) as state:
                state["branch"] = branch
                state["discarded"] = True
                state["discard_reason"] = clean_reason
                if not keep_branch:
                    state["rounds"] = []
                    state["checks_by_sha"] = {}
                    state["last_write"] = None
                    state.pop("task_file", None)

            teardown = _remove_worktree_and_branch_after_land(
                project, branch, base_ref=base_ref, keep_branch=keep_branch, discard=True
            )
            return {
                "branch": branch,
                "slug": slug,
                "base_ref": base_ref,
                "discarded": True,
                "reason": clean_reason,
                "archived_task": archived_task,
                "discard_commit_sha": discard_commit_sha,
                **teardown,
            }

    # 2. Optional deterministic checks run unlocked against `pinned_head_sha`, then merge into state under `flock`
    if checks:
        _run_and_record_checks(project, common_dir, branch, pinned_head_sha, checks)
        _assert_pinned_refs(project, branch, base_ref, pinned_head_sha, pinned_base_tip_sha, require_clean_worktree=False)
    elif skip_checks is not None:
        if not skip_checks.strip():
            raise KeelError("--skip-checks requires a non-empty reason.")
        _record_skipped_checks(common_dir, branch, pinned_head_sha, skip_checks)

    max_review_rounds = int(defaults.get("max_review_rounds", 2))
    configured_levels = tuple(defaults.get("levels", VALID_LEVELS))
    verify_min_level = str(defaults.get("verify_min_level", "L2"))
    gate = evaluate_branch_gate(
        project,
        branch,
        base=base_ref,
        old_base=old_base,
        old_head=old_head,
        human_override=human_override,
        task_file_arg=task_file_arg,
        max_review_rounds=max_review_rounds,
        levels=configured_levels,
        verify_min_level=verify_min_level,
        default_branches=default_branches,
        require_clean_checkouts=True,
        pinned_head_sha=pinned_head_sha,
        pinned_base_tip_sha=pinned_base_tip_sha,
    )
    gate["strategy"] = strategy
    gate["landed"] = False

    if not execute:
        return gate

    if not gate["ready_to_land"]:
        raise KeelError("Branch is not ready to land:\n- " + "\n- ".join(gate["blockers"]))

    # Critical section: serialize under repo-wide `land.lock` + per-branch `locked_branch_state`
    base_tip_sha = pinned_base_tip_sha
    trailers = extract_commit_trailers(project, f"{base_tip_sha}..{pinned_head_sha}")
    if human_override and human_override.strip():
        trailers.append(f"Keel-Human-Override: {human_override.strip()}")

    archived_task = None

    with locked_repo_land(common_dir):
        with edit_gate_state(common_dir, branch) as state:
            _assert_pinned_refs(
                project, branch, base_ref, pinned_head_sha, pinned_base_tip_sha, require_clean_worktree=True
            )
            live_gate = evaluate_branch_gate(
                project,
                branch,
                base=base_ref,
                old_base=old_base,
                old_head=old_head,
                human_override=human_override,
                task_file_arg=task_file_arg,
                max_review_rounds=max_review_rounds,
                levels=configured_levels,
                verify_min_level=verify_min_level,
                default_branches=default_branches,
                require_clean_checkouts=True,
                pinned_head_sha=pinned_head_sha,
                pinned_base_tip_sha=pinned_base_tip_sha,
                state_override=state,
                precomputed_rebase_proof=gate.get("rebase_proof"),
            )
            if not live_gate["ready_to_land"]:
                raise KeelError("Branch is no longer ready to land:\n- " + "\n- ".join(live_gate["blockers"]))

            gate = live_gate
            gate["strategy"] = strategy
            task_file = gate["task_file"]

            primary_branch = run_cmd(["git", "branch", "--show-current"], cwd=project).stdout.strip()
            if primary_branch != base_ref:
                raise KeelError(
                    f"Primary checkout {project} must be on '{base_ref}' (currently '{primary_branch or 'detached'}')."
                )
            primary_porcelain = run_cmd(["git", "status", "--porcelain", "-uall"], cwd=project).stdout.strip()
            if primary_porcelain:
                raise KeelError(f"Primary checkout {project} has uncommitted or untracked changes:\n{primary_porcelain}")

            if strategy == "squash":
                subjects = [
                    ln.strip()
                    for ln in run_cmd(
                        ["git", "log", "--reverse", "--format=%s", f"{base_tip_sha}..{pinned_head_sha}"], cwd=project
                    ).stdout.splitlines()
                    if ln.strip()
                ]
                commit_title = (
                    (title or "").strip() or (subjects[0] if len(subjects) == 1 else f"feat({slug}): land {branch}")
                )
                body_parts: list[str] = []
                if len(subjects) > 1:
                    body_parts.append("\n".join(f"- {s}" for s in subjects))
                if trailers:
                    body_parts.append("\n".join(trailers))
                full_msg = "\n\n".join([commit_title, *body_parts])

                run_cmd(["git", "merge", "--squash", pinned_head_sha], cwd=project)
                if task_file and task_file.startswith("docs/tasks/active/") and (project / task_file).is_file():
                    archive_rel = f"docs/tasks/archive/{Path(task_file).name}"
                    (project / archive_rel).parent.mkdir(parents=True, exist_ok=True)
                    run_cmd(["git", "mv", "-f", "--", task_file, archive_rel], cwd=project)
                    archived_task = archive_rel

                run_cmd(["git", "commit", "-m", full_msg], cwd=project)
                landed_sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=project).stdout.strip().lower()
            elif strategy == "ff-only":
                run_cmd(["git", "merge", "--ff-only", pinned_head_sha], cwd=project)
                if task_file and task_file.startswith("docs/tasks/active/") and (project / task_file).is_file():
                    archive_rel = f"docs/tasks/archive/{Path(task_file).name}"
                    (project / archive_rel).parent.mkdir(parents=True, exist_ok=True)
                    run_cmd(["git", "mv", "-f", "--", task_file, archive_rel], cwd=project)
                    archive_msg = f"docs(tasks): archive {Path(task_file).stem}"
                    if human_override and human_override.strip():
                        archive_msg += f"\n\nKeel-Human-Override: {human_override.strip()}"
                    run_cmd(["git", "commit", "-m", archive_msg], cwd=project)
                    archived_task = archive_rel
                landed_sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=project).stdout.strip().lower()
            else:
                raise KeelError(f"Unsupported landing strategy '{strategy}'.")

            state["branch"] = branch
            state["landed_head_sha"] = pinned_head_sha
            state["landed_sha"] = landed_sha
            state["landed_method"] = strategy
            if not keep_branch:
                state["rounds"] = []
                state["checks_by_sha"] = {}
                state["last_write"] = None
                state.pop("task_file", None)

        teardown = _remove_worktree_and_branch_after_land(
            project,
            branch,
            base_ref=base_ref,
            pinned_head_sha=pinned_head_sha,
            pinned_base_tip_sha=landed_sha,
            keep_branch=keep_branch,
            discard=False,
        )
    gate["landed"] = True
    gate["landed_sha"] = landed_sha
    gate["archived_task"] = archived_task
    gate.update(teardown)
    return gate
