"""Pure-Git branch verification, `.git/keel-state/` ledger, pure-rebase proof, and guarded landing."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
from typing import Any, Sequence

from . import KeelError, SCHEMAS_DIR
from .context import assemble_brief, generate_run_id, runs_root_dir, save_run_record
from .env import run_cmd, safe_resolve, same_path, tool_env
from .executors import RunRequest, get_executor
from .executors.agy import VALID_AGY_MODELS
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
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _read_blob_text(project: Path, rev: str, rel_path: str) -> str | None:
    if run_cmd(["git", "cat-file", "-e", f"{rev}:{rel_path}"], cwd=project, check=False).returncode != 0:
        return None
    return run_cmd(["git", "show", f"{rev}:{rel_path}"], cwd=project).stdout.strip()


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
        return normalize_task_rel_path(project, explicit_task_file)

    # Never steal `task/<slug>`'s active task file when operating on a different branch (e.g. `feature/<slug>`)
    if branch != f"task/{slug}" and (
        run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/task/{slug}"], cwd=project, check=False).returncode
        == 0
    ):
        return state_task_file

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
    return state_task_file


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


def _add_pending_verifier(state: dict[str, Any], head_sha: str, exec_name: str) -> None:
    pending_map = state.setdefault("pending_verifiers", {})
    cur = list(pending_map.get(head_sha) or [])
    if exec_name not in cur:
        cur.append(exec_name)
    pending_map[head_sha] = cur


def _remove_pending_verifier(state: dict[str, Any], head_sha: str, exec_name: str) -> None:
    pending_map = state.setdefault("pending_verifiers", {})
    if head_sha in pending_map and isinstance(pending_map[head_sha], list):
        pending_map[head_sha] = [x for x in pending_map[head_sha] if x != exec_name]
        if not pending_map[head_sha]:
            pending_map.pop(head_sha, None)


def _join_or_create_round(
    state: dict[str, Any],
    *,
    head_sha: str,
    dual: bool,
    exec_name: str,
    slug: str,
    branch: str = "",
    max_rounds: int | None = None,
    force_round: bool = False,
) -> dict[str, Any]:
    rounds: list[dict[str, Any]] = state.setdefault("rounds", [])
    if dual:
        state.setdefault("dual_required_by_sha", {})[head_sha] = True
        for r in reversed(rounds):
            if (
                r.get("head_sha") == head_sha
                and r.get("dual") is True
                and not r.get("completed")
                and exec_name not in (r.get("verdicts") or {})
            ):
                r["expected_executors"] = ["codex", "agy"]
                return r
        if max_rounds is not None and not force_round and int(state.get("request_changes_rounds", 0)) >= max_rounds:
            raise KeelError(
                f"Branch '{branch or slug}' already has {state['request_changes_rounds']} 'request_changes' rounds "
                f"(>= {max_rounds}-round convergence cap). Stop looping and escalate to the human (or pass --force-round if authorized)."
            )
        round_entry = {
            "round_id": generate_run_id(f"round-{slug}"),
            "head_sha": head_sha,
            "dual": True,
            "expected_executors": ["codex", "agy"],
            "verdicts": {},
            "completed": False,
            "counted_request_changes": False,
            "outcome": "pending",
        }
        rounds.append(round_entry)
        return round_entry

    if max_rounds is not None and not force_round and int(state.get("request_changes_rounds", 0)) >= max_rounds:
        raise KeelError(
            f"Branch '{branch or slug}' already has {state['request_changes_rounds']} 'request_changes' rounds "
            f"(>= {max_rounds}-round convergence cap). Stop looping and escalate to the human (or pass --force-round if authorized)."
        )
    # Non-dual single-executor verify resets stale dual requirement & clears stale pending verifiers on `head_sha`
    state.setdefault("dual_required_by_sha", {})[head_sha] = False
    state.setdefault("pending_verifiers", {}).pop(head_sha, None)
    round_entry = {
        "round_id": generate_run_id(f"round-{slug}"),
        "head_sha": head_sha,
        "dual": False,
        "expected_executors": [exec_name],
        "verdicts": {},
        "completed": False,
        "counted_request_changes": False,
        "outcome": "pending",
    }
    rounds.append(round_entry)
    return round_entry


def _has_joinable_open_dual_round(state: dict[str, Any], head_sha: str, dual: bool, executor: str | None) -> bool:
    if not dual or executor not in ("codex", "agy"):
        return False
    for r in reversed(state.get("rounds") or []):
        if (
            r.get("head_sha") == head_sha
            and r.get("dual") is True
            and not r.get("completed")
            and executor in (r.get("expected_executors") or [])
            and executor not in (r.get("verdicts") or {})
        ):
            return True
    return False


def record_verdict_in_state(
    common_dir: Path,
    branch: str,
    *,
    head_sha: str,
    executor: str,
    model: str | None,
    validated_verdict: dict[str, Any],
) -> dict[str, Any]:
    norm_sha = head_sha.strip().lower()
    if not _HEX_SHA_RE.fullmatch(norm_sha):
        raise KeelError(f"--head-sha must be a full 40/64-character commit SHA (got '{head_sha}').")

    exec_key = executor.strip().lower()
    with locked_branch_state(common_dir, branch) as state:
        state["branch"] = branch

        verdicts_by_sha = state.setdefault("verdicts_by_sha", {})
        sha_verdicts = verdicts_by_sha.setdefault(norm_sha, {})
        sha_verdicts[exec_key] = {
            "executor": exec_key,
            "model": model,
            **validated_verdict,
            "recorded_at": _now_iso(),
        }
        _remove_pending_verifier(state, norm_sha, exec_key)

        rounds: list[dict[str, Any]] = state.setdefault("rounds", [])
        target_round: dict[str, Any] | None = None
        for r in reversed(rounds):
            if r.get("head_sha") == norm_sha and not r.get("completed") and exec_key in (r.get("expected_executors") or []):
                target_round = r
                break
        if target_round is None:
            latest_for_sha = next((r for r in reversed(rounds) if r.get("head_sha") == norm_sha), None)
            if latest_for_sha is not None and (
                exec_key in (latest_for_sha.get("expected_executors") or [])
                or exec_key in (latest_for_sha.get("verdicts") or {})
            ):
                target_round = latest_for_sha
        if target_round is None:
            is_dual = bool((state.get("dual_required_by_sha") or {}).get(norm_sha, False))
            target_round = {
                "round_id": generate_run_id("round"),
                "head_sha": norm_sha,
                "dual": is_dual,
                "expected_executors": ["codex", "agy"] if is_dual else [exec_key],
                "verdicts": {},
                "completed": False,
                "counted_request_changes": False,
                "outcome": "pending",
            }
            rounds.append(target_round)

        round_verdicts = target_round.setdefault("verdicts", {})
        round_verdicts[exec_key] = validated_verdict["verdict"]

        has_rc = any(v == "request_changes" for v in round_verdicts.values())
        target_round["counted_request_changes"] = has_rc
        expected_set = set(target_round.get("expected_executors") or [exec_key])
        target_round["completed"] = expected_set <= set(round_verdicts.keys())
        target_round["outcome"] = "request_changes" if has_rc else ("approve" if target_round["completed"] else "pending")
        state["request_changes_rounds"] = sum(1 for r in rounds if r.get("counted_request_changes"))

        return dict(state)


def verify_branch(
    *,
    project: Path,
    branch_or_slug: str,
    defaults: dict[str, Any],
    base: str | None = None,
    executor: str | None = None,
    dual: bool = False,
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

    project = primary_checkout_root(project)
    ensure_agents_dir_ignored(project)
    common_dir, _ = git_dirs(project)
    branch, slug = resolve_branch_and_slug(project, branch_or_slug, must_exist=True)
    default_branches = tuple(defaults.get("default_branches", ["main", "master"]))
    base_ref = resolve_base_ref(project, base, default_branches=default_branches)

    head_sha = run_cmd(["git", "rev-parse", f"refs/heads/{branch}"], cwd=project).stdout.strip().lower()
    base_tip_sha = run_cmd(["git", "rev-parse", f"{base_ref}^{{commit}}"], cwd=project).stdout.strip().lower()
    merge_base_sha = run_cmd(["git", "merge-base", base_tip_sha, head_sha], cwd=project).stdout.strip().lower()

    linked_wt = _find_branch_worktree(project, branch)
    if linked_wt is not None and linked_wt.is_dir():
        porcelain = run_cmd(["git", "status", "--porcelain", "-uall"], cwd=linked_wt).stdout.strip()
        if porcelain:
            raise KeelError(f"Worktree {linked_wt} has uncommitted or untracked changes:\n{porcelain}")
        wt_head = run_cmd(["git", "rev-parse", "HEAD"], cwd=linked_wt).stdout.strip().lower()
        if wt_head != head_sha:
            raise KeelError(f"Worktree {linked_wt} HEAD ({wt_head[:12]}) differs from '{branch}' tip ({head_sha[:12]}).")

    initial_state = load_branch_state(common_dir, branch)
    max_rounds = int(defaults.get("max_review_rounds", 2))
    joining_open_dual = _has_joinable_open_dual_round(initial_state, head_sha, dual, executor)
    if int(initial_state.get("request_changes_rounds", 0)) >= max_rounds and not force_round and not joining_open_dual:
        raise KeelError(
            f"Branch '{branch}' already has {initial_state['request_changes_rounds']} 'request_changes' rounds "
            f"(>= {max_rounds}-round convergence cap). Stop looping and escalate to the human (or pass --force-round if authorized)."
        )

    task_file = discover_task_file(
        project,
        branch,
        slug,
        base_ref=base_ref,
        head_sha=head_sha,
        explicit_task_file=task_file_arg,
        state_task_file=initial_state.get("task_file"),
    )

    with locked_branch_state(common_dir, branch) as state:
        state["branch"] = branch
        state["slug"] = slug
        state["base_ref"] = base_ref
        if task_file:
            state["task_file"] = task_file

    # 1. Deterministic checks run unlocked, then merge into latest state under `flock`
    if checks:
        chk_res = run_deterministic_checks(project, head_sha, checks)
        with locked_branch_state(common_dir, branch) as state:
            state.setdefault("checks_by_sha", {})[head_sha] = chk_res
        if not chk_res["ok"]:
            failed = next((c for c in chk_res["commands"] if not c["ok"]), chk_res["commands"][-1])
            raise KeelError(
                f"Deterministic check failed ({failed['returncode']}): {failed['command']}\n{failed['output_tail']}".strip()
            )
    elif skip_checks:
        with locked_branch_state(common_dir, branch) as state:
            state.setdefault("checks_by_sha", {})[head_sha] = {
                "configured": True,
                "ok": True,
                "skipped": True,
                "skip_reason": skip_checks.strip(),
                "commands": [],
                "recorded_at": _now_iso(),
            }

    # 2. Build locked task spec & verification context
    locked_spec: str | None = None
    if task_file:
        locked_spec = _read_blob_text(project, base_ref, task_file)
        if locked_spec is None and task_file.startswith("docs/tasks/active/"):
            todo_equiv = "docs/tasks/todo/" + Path(task_file).name
            locked_spec = _read_blob_text(project, base_ref, todo_equiv)
    branch_spec = _read_blob_text(project, head_sha, task_file) if task_file else None

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

    if dual:
        if executor:
            if executor not in ("codex", "agy"):
                raise KeelError("--dual supports 'codex' and 'agy' executors.")
            executors_to_run = [executor]
        else:
            # Prepare `agy` descriptor first, then run `codex` synchronously
            executors_to_run = ["agy", "codex"]
    else:
        executors_to_run = [executor or defaults.get("routing", {}).get("verify", "codex")]

    schema_path = SCHEMAS_DIR / "verdict.schema.json"
    runs_out: dict[str, Any] = {}
    prepared_agy_scratch_in_this_call: Path | None = None

    for exec_name in executors_to_run:
        adapter = get_executor(exec_name)
        run_id = generate_run_id(f"verify-{slug}-{exec_name}")
        if output_dir:
            scratch_root = (Path(output_dir) / exec_name) if (dual and not executor) else Path(output_dir)
        else:
            scratch_root = Path(tempfile.mkdtemp(prefix=f"keel-verify-{slug}-{exec_name}-"))

        eff_model = model
        if dual and not executor and model is not None:
            if exec_name == "agy" and model not in VALID_AGY_MODELS:
                eff_model = None
            elif exec_name == "codex" and model in VALID_AGY_MODELS and model != "inherit":
                eff_model = None

        registered_sync_pending = False
        try:
            checkout_dir, sep_git_dir = create_disposable_clone(project, head_sha, scratch_root)
            run_dir = runs_root_dir(project, Path(tempfile.gettempdir())) / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            verdict_path = scratch_root / "verdict.json"
            brief_path = run_dir / "brief.md"

            brief_path.write_text(
                assemble_brief(
                    why=why_text,
                    task="\n".join(task_lines),
                    preset="verify",
                    mode="read",
                    workdir=checkout_dir,
                    project_root=project,
                    native_agent_docs=adapter.native_agent_docs,
                    loads_plugin_spine=adapter.loads_plugin_spine,
                    caller_cwd=caller_cwd or Path.cwd(),
                    schema_path=schema_path,
                    output_path=verdict_path,
                    inject_schema_in_brief=adapter.inject_schema_in_brief,
                ),
                encoding="utf-8",
            )

            req = RunRequest(
                run_id=run_id,
                run_dir=run_dir,
                brief_path=brief_path,
                workdir=checkout_dir,
                project_root=project,
                mode="read",
                preset="verify",
                model=eff_model,
                effort=effort,
                schema_path=schema_path,
                output_path=verdict_path,
                extra_git_dirs=(sep_git_dir,),
                scratch_verify=True,
            )

            if adapter.name != "agy":
                # Join/create round under `flock` BEFORE blocking on synchronous CLI execution
                with locked_branch_state(common_dir, branch) as state:
                    r = _join_or_create_round(
                        state,
                        head_sha=head_sha,
                        dual=dual,
                        exec_name=adapter.name,
                        slug=slug,
                        branch=branch,
                        max_rounds=max_rounds,
                        force_round=force_round,
                    )
                    if dual and adapter.name not in (r.get("verdicts") or {}):
                        _add_pending_verifier(state, head_sha, adapter.name)
                        registered_sync_pending = True

            exec_res = adapter.execute(req, defaults)
            if exec_res.get("status") != "completed":
                record_cmd = (
                    f"keel branch record-verdict {shlex.quote(branch)} --project {shlex.quote(str(project))} "
                    f"--verdict-file {shlex.quote(str(verdict_path))} --head-sha {shlex.quote(head_sha)} "
                    f"--executor {shlex.quote(adapter.name)} --cleanup-scratch {shlex.quote(str(scratch_root))}"
                )
                rec = {
                    "run_id": run_id,
                    "executor": adapter.name,
                    "preset": "verify",
                    "mode": "read",
                    "scratch_verify": True,
                    "workdir": str(checkout_dir),
                    "project_root": str(project),
                    "brief_path": str(brief_path),
                    "schema_path": str(schema_path),
                    "branch": branch,
                    "head_sha": head_sha,
                    "scratch_root": str(scratch_root),
                    "verdict_path": str(verdict_path),
                    "record_verdict_command": record_cmd,
                    **exec_res,
                }
                save_run_record(run_dir, rec)
                # Persist `pending_verifiers` ONLY after clone + brief + run.json succeeded!
                with locked_branch_state(common_dir, branch) as state:
                    r = _join_or_create_round(
                        state,
                        head_sha=head_sha,
                        dual=dual,
                        exec_name=adapter.name,
                        slug=slug,
                        branch=branch,
                        max_rounds=max_rounds,
                        force_round=force_round,
                    )
                    if adapter.name not in (r.get("verdicts") or {}):
                        _add_pending_verifier(state, head_sha, adapter.name)
                prepared_agy_scratch_in_this_call = scratch_root
                runs_out[adapter.name] = rec
                continue

            verdict_raw = json.loads(verdict_path.read_text(encoding="utf-8"))
            validated = validate_verdict_payload(verdict_raw)
            updated_state = record_verdict_in_state(
                common_dir,
                branch,
                head_sha=head_sha,
                executor=adapter.name,
                model=exec_res.get("model"),
                validated_verdict=validated,
            )
            if not output_dir and scratch_root.exists():
                shutil.rmtree(scratch_root, ignore_errors=True)
            runs_out[adapter.name] = {
                "run_id": run_id,
                "status": "completed",
                "executor": adapter.name,
                "model": exec_res.get("model"),
                "verdict": validated,
                "request_changes_rounds": updated_state.get("request_changes_rounds", 0),
            }
        except Exception:
            if registered_sync_pending or (prepared_agy_scratch_in_this_call is not None and not executor):
                with locked_branch_state(common_dir, branch) as state:
                    if registered_sync_pending:
                        _remove_pending_verifier(state, head_sha, adapter.name)
                    if prepared_agy_scratch_in_this_call is not None and not executor:
                        _remove_pending_verifier(state, head_sha, "agy")
            if not output_dir:
                if scratch_root.exists():
                    shutil.rmtree(scratch_root, ignore_errors=True)
                if prepared_agy_scratch_in_this_call is not None and not executor and prepared_agy_scratch_in_this_call.exists():
                    shutil.rmtree(prepared_agy_scratch_in_this_call, ignore_errors=True)
            raise

    final_state = load_branch_state(common_dir, branch)
    if not dual:
        single = runs_out[executors_to_run[0]]
        return {
            "branch": branch,
            "slug": slug,
            "base_ref": base_ref,
            "head_sha": head_sha,
            "dual": False,
            "task_file": task_file,
            "request_changes_rounds": final_state.get("request_changes_rounds", 0),
            **single,
        }
    base_payload: dict[str, Any] = {
        "branch": branch,
        "slug": slug,
        "base_ref": base_ref,
        "head_sha": head_sha,
        "dual": True,
        "task_file": task_file,
        "verifiers": runs_out,
        "pending_verifiers": (final_state.get("pending_verifiers") or {}).get(head_sha, []),
        "request_changes_rounds": final_state.get("request_changes_rounds", 0),
    }
    if executor and executor in runs_out:
        return {**runs_out[executor], **base_payload}
    return base_payload


def _validate_old_rebase_pair(old_base: str | None, old_head: str | None) -> None:
    has_base = bool(old_base and old_base.strip())
    has_head = bool(old_head and old_head.strip())
    if (old_base is not None or old_head is not None) and (not has_base or not has_head):
        raise KeelError("--old-base and --old-head must both be provided together.")


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
    default_branches: Sequence[str] = ("main", "master"),
    require_clean_checkouts: bool = False,
    pinned_head_sha: str | None = None,
    pinned_base_tip_sha: str | None = None,
    state_override: dict[str, Any] | None = None,
    precomputed_rebase_proof: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_old_rebase_pair(old_base, old_head)
    project = primary_checkout_root(project)
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
    verdicts_map = state.get("verdicts_by_sha") or {}
    verdicts_for_head: dict[str, Any] = verdicts_map.get(head_sha) or {}
    dual_required = bool((state.get("dual_required_by_sha") or {}).get(head_sha, False))
    pending_for_head: list[str] = list((state.get("pending_verifiers") or {}).get(head_sha, []))
    rc_rounds = int(state.get("request_changes_rounds", 0))

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
        blockers.append(f"Deterministic checks failed on commit {head_sha[:12]}.")

    # Current-head rejection ALWAYS takes precedence over older pre-rebase approvals!
    has_current_rejection = any(v.get("verdict") == "request_changes" for v in verdicts_for_head.values())
    if has_current_rejection:
        if human_override and human_override.strip():
            approval_source = f"human-override:{human_override.strip()}"
        else:
            blockers.append(f"Commit {head_sha[:12]} carries an active 'request_changes' verdict.")
    elif dual_required:
        codex_ok = (verdicts_for_head.get("codex") or {}).get("verdict") == "approve"
        agy_ok = (verdicts_for_head.get("agy") or {}).get("verdict") == "approve"
        if codex_ok and agy_ok and not pending_for_head:
            approval_source = f"dual-approve:{head_sha[:12]}"
        elif human_override and human_override.strip():
            approval_source = f"human-override:{human_override.strip()}"
        else:
            missing = [ex for ex, ok in (("codex", codex_ok), ("agy", agy_ok)) if not ok]
            blockers.append(
                f"Dual verification on {head_sha[:12]} requires unanimous 'approve' from both codex and agy "
                f"(missing/pending: {', '.join(missing or pending_for_head)})."
            )
    elif verdicts_for_head and all(v.get("verdict") == "approve" for v in verdicts_for_head.values()) and not pending_for_head:
        approval_source = f"direct-approve:{head_sha[:12]}"
    elif pending_for_head:
        if human_override and human_override.strip():
            approval_source = f"human-override:{human_override.strip()}"
        else:
            blockers.append(f"Pending independent verifier(s) on {head_sha[:12]}: {', '.join(pending_for_head)}.")
    elif old_base and old_head:
        old_head_lc = old_head.strip().lower()
        old_verdicts: dict[str, Any] = verdicts_map.get(old_head_lc) or {}
        old_dual = bool((state.get("dual_required_by_sha") or {}).get(old_head_lc, False))
        old_approved = bool(old_verdicts) and all(v.get("verdict") == "approve" for v in old_verdicts.values())
        if old_dual:
            old_approved = old_approved and ("codex" in old_verdicts and "agy" in old_verdicts)
        if not old_approved:
            raise KeelError(f"--old-head {old_head_lc[:12]} does not carry complete 'approve' verification for '{branch}'.")
        rebase_proof = (
            precomputed_rebase_proof
            if precomputed_rebase_proof is not None
            else compare_rebase(project, old_base.strip().lower(), old_head_lc, base_tip_sha, head_sha)
        )
        old_checks = checks_map.get(old_head_lc) or {}
        if old_checks.get("configured") and not old_checks.get("skipped") and not checks_for_head.get("configured"):
            blockers.append(
                f"Pure rebase from {old_head_lc[:12]} requires re-running deterministic checks on {head_sha[:12]} "
                "(pass --check '<cmd>' or --skip-checks '<reason>')."
            )
        else:
            approval_source = f"pure-rebase-from:{old_head_lc[:12]}"
    elif human_override and human_override.strip():
        approval_source = f"human-override:{human_override.strip()}"
    else:
        if rc_rounds >= max_review_rounds:
            blockers.append(
                f"Reached {rc_rounds} 'request_changes' rounds (>= {max_review_rounds}-round cap): "
                "stop looping and escalate to the human."
            )
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
        "verdicts": verdicts_for_head,
        "pending_verifiers": pending_for_head,
        "dual_required": dual_required,
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
    _validate_old_rebase_pair(old_base, old_head)

    project = primary_checkout_root(project)
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

            with locked_branch_state(common_dir, branch) as state:
                state["branch"] = branch
                state["discarded"] = True
                state["discard_reason"] = clean_reason

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
        chk_res = run_deterministic_checks(project, pinned_head_sha, checks)
        with locked_branch_state(common_dir, branch) as state:
            state.setdefault("checks_by_sha", {})[pinned_head_sha] = chk_res
        _assert_pinned_refs(project, branch, base_ref, pinned_head_sha, pinned_base_tip_sha, require_clean_worktree=False)
    elif skip_checks is not None:
        if not skip_checks.strip():
            raise KeelError("--skip-checks requires a non-empty reason.")
        with locked_branch_state(common_dir, branch) as state:
            state.setdefault("checks_by_sha", {})[pinned_head_sha] = {
                "configured": True,
                "ok": True,
                "skipped": True,
                "skip_reason": skip_checks.strip(),
                "commands": [],
                "recorded_at": _now_iso(),
            }

    max_review_rounds = int(defaults.get("max_review_rounds", 2))
    gate = evaluate_branch_gate(
        project,
        branch,
        base=base_ref,
        old_base=old_base,
        old_head=old_head,
        human_override=human_override,
        task_file_arg=task_file_arg,
        max_review_rounds=max_review_rounds,
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
        with locked_branch_state(common_dir, branch) as state:
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
