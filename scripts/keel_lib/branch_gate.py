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
import subprocess
import tempfile
from typing import Any, Iterator, Sequence

from . import KeelError, SCHEMAS_DIR
from .context import (
    WHY_HEADINGS,
    _strip_markdown_sections,
    assemble_brief,
    extract_changed_markdown_sections,
    extract_markdown_section,
    generate_run_id,
    read_utf8_file,
    runs_root_dir,
    save_run_record,
    strip_review_history_sections,
    trim_markdown_block,
)
from .env import (
    check_interrupted,
    forward_signals_to_child_groups,
    is_subpath,
    run_cmd,
    safe_resolve,
    same_path,
    tool_env,
)
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
    ROOT_AGENT_DOCS,
    apply_lineage_to_branch_state,
    cleanup_worktree,
    cli_base_flag,
    create_disposable_clone,
    dedup_strings,
    ensure_agents_dir_ignored,
    git_dirs,
    list_worktrees,
    load_branch_state,
    load_lineage_for_branch,
    locked_branch_state,
    locked_repo_land,
    primary_checkout_root,
    resolve_base_ref,
    resolve_supersedes_lineage,
    save_lineage_state,
    unlink_lineage_for_branch,
    validate_supersedes_target,
)

ALLOWED_VERDICT_KEYS = frozenset({"verdict", "summary", "blocking", "notes", "commands_run"})
ALLOWED_ARBITER_KEYS = frozenset(
    {"action", "summary", "directives", "avoid_dead_ends", "waived_findings", "commands_run"}
)
VALID_ARBITER_ACTIONS = ("approve", "rework_once", "replan", "pivot", "park")
GATE_STATE_FIELDS = (
    "branch",
    "task_file",
    "rounds",
    "last_write",
    "checks_by_sha",
    "rebuttals",
    "arbitration",
    "arbiter_rework_used",
    "arbiter_rework_after_round",
    "arbiter_closed_round",
    "arbiter_closed_head_sha",
    "pending_supersede_cleanup",
    "parked",
    "park_reason",
    "recovery_count",
    "avoid_dead_ends",
    "prior_directives",
    "superseded_from",
    "landed_head_sha",
    "landed_sha",
    "landed_method",
    "landed_history",
    "discarded",
    "discard_reason",
)
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


def validate_arbiter_payload(payload: Any) -> dict[str, Any]:
    """Validate structured Arbiter JSON strictly against `arbiter.schema.json` (`additionalProperties: false`)."""
    if not isinstance(payload, dict):
        raise KeelError("Arbiter payload must be a JSON object.")

    unknown_keys = sorted(set(payload.keys()) - ALLOWED_ARBITER_KEYS)
    if unknown_keys:
        raise KeelError(f"Arbiter payload contains unexpected properties: {', '.join(unknown_keys)}")

    missing_keys = sorted(ALLOWED_ARBITER_KEYS - set(payload.keys()))
    if missing_keys:
        raise KeelError(f"Arbiter payload is missing required properties: {', '.join(missing_keys)}")

    action = payload.get("action")
    if action not in VALID_ARBITER_ACTIONS:
        raise KeelError(f"Invalid arbiter action '{action}': expected one of {', '.join(VALID_ARBITER_ACTIONS)}.")

    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise KeelError("Arbiter 'summary' must be a non-empty string.")

    out: dict[str, Any] = {"action": action, "summary": summary.strip()}
    for field in ("directives", "avoid_dead_ends", "waived_findings", "commands_run"):
        raw_list = payload.get(field)
        if not isinstance(raw_list, list) or not all(isinstance(x, str) and bool(x.strip()) for x in raw_list):
            raise KeelError(f"Arbiter '{field}' must be an array of non-empty strings.")
        out[field] = [x.strip() for x in raw_list]

    if action in ("rework_once", "replan", "pivot", "park") and not out["directives"]:
        raise KeelError(f"Arbiter action '{action}' requires at least one non-empty directive in 'directives'.")
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
    *,
    require_base_ancestry: bool = True,
) -> dict[str, Any]:
    ancestry_checks = [
        (old_base, old_head, "old-base <= old-head"),
        (new_base, new_head, "new-base <= new-head"),
    ]
    if require_base_ancestry:
        ancestry_checks.insert(1, (old_base, new_base, "old-base <= new-base"))
    for anc, desc, desc_label in ancestry_checks:
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
    explicit_heads = raw.startswith("refs/heads/")
    if explicit_heads:
        raw = raw[len("refs/heads/") :]
        if not raw:
            raise KeelError("Branch or worktree slug cannot be empty.")

    task_candidate = f"task/{raw}"
    if not explicit_heads and not raw.startswith("task/") and (
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


def _cli_branch_target(branch: str, slug: str) -> str:
    if branch == f"task/{slug}":
        return slug
    if "/" not in branch:
        return f"refs/heads/{branch}"
    return branch


def _cli_base_flag(
    project: Path,
    base_ref: str,
    default_branches: Sequence[str] = ("main", "master"),
) -> str:
    return cli_base_flag(project, base_ref, default_branches=default_branches)


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
    proc = subprocess.run(
        ["git", "show", f"{rev}:{rel_path}"],
        cwd=str(project),
        env=tool_env(),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise KeelError(f"Unable to read '{rel_path}' at '{rev}': {detail}")
    try:
        decoded = proc.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KeelError(f"Task file '{rel_path}' at '{rev}' is not valid UTF-8: {exc}") from exc
    if decoded.startswith("\ufeff"):
        decoded = decoded[1:]
    return trim_markdown_block(decoded)


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
        _read_blob_sha(project, base_ref, rel) is not None
        or (head_sha is not None and _read_blob_sha(project, head_sha, rel) is not None)
        or (todo_equiv is not None and _read_blob_sha(project, base_ref, todo_equiv) is not None)
    )


def _is_archivable_task_path(task_file: str | None, *, allow_todo: bool = True) -> bool:
    if not task_file:
        return False
    if task_file.startswith("docs/tasks/active/"):
        return True
    return allow_todo and task_file.startswith("docs/tasks/todo/")


def _archivable_task_source(project: Path, task_file: str | None, *, allow_todo: bool = True) -> str | None:
    if not task_file:
        return None
    if task_file.startswith("docs/tasks/active/"):
        if (project / task_file).is_file():
            return task_file
        if allow_todo:
            todo_rel = "docs/tasks/todo/" + Path(task_file).name
            if (project / todo_rel).is_file():
                return todo_rel
        return None
    if allow_todo and task_file.startswith("docs/tasks/todo/"):
        if (project / task_file).is_file():
            return task_file
        return None
    return None


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
    if branch != f"task/{slug}":
        task_branch = f"task/{slug}"
        if (
            run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{task_branch}"], cwd=project, check=False).returncode
            == 0
        ):
            return valid_state_task
        common_dir, _ = git_dirs(project)
        owner_task = load_branch_state(common_dir, task_branch).get("task_file")
        if isinstance(owner_task, str) and owner_task and _task_exists_in_git(project, base_ref, head_sha, owner_task):
            return valid_state_task
        owner_lineage = load_lineage_for_branch(common_dir, task_branch)
        if isinstance(owner_lineage, dict):
            lin_task = owner_lineage.get("task_file")
            if (
                isinstance(lin_task, str)
                and lin_task
                and not owner_lineage.get("archived_task")
                and _task_exists_in_git(project, base_ref, head_sha, lin_task)
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
    with locked_branch_state(common_dir, branch) as stored:
        state = {key: stored[key] for key in GATE_STATE_FIELDS if key in stored}
        state["rounds"] = _review_rounds(stored)
        state.setdefault("checks_by_sha", {})
        state.setdefault("last_write", None)
        state.setdefault("arbiter_closed_round", 0)
        state.setdefault("arbiter_closed_head_sha", None)
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


def _record_skipped_checks(common_dir: Path, branch: str, head_sha: str, reason: str) -> dict[str, Any]:
    with edit_gate_state(common_dir, branch) as state:
        checks_by_sha = state["checks_by_sha"]
        previous = checks_by_sha.get(head_sha) or {}
        prev_commands = list(previous.get("commands") or [])
        all_prev_ok = all(bool(c.get("ok")) for c in prev_commands)
        entry = {
            "configured": True,
            "ok": all_prev_ok,
            "skipped": True,
            "skip_reason": reason.strip(),
            "commands": prev_commands,
            "recorded_at": _now_iso(),
        }
        checks_by_sha[head_sha] = entry
        return entry


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


def _effective_arbiter_closed_round(
    rounds: Sequence[dict[str, Any]],
    arbiter_closed_round: Any = 0,
    arbitration: dict[str, Any] | None = None,
) -> int:
    closed = (
        arbiter_closed_round
        if isinstance(arbiter_closed_round, int) and not isinstance(arbiter_closed_round, bool)
        else 0
    )
    closed = max(0, min(closed, len(rounds)))
    if isinstance(arbitration, dict) and arbitration.get("action") == "approve":
        raw_after = arbitration.get("after_round")
        if isinstance(raw_after, int) and not isinstance(raw_after, bool) and 0 < raw_after <= len(rounds):
            closed = max(closed, raw_after)
    return closed


def _latest_scoped_approve_cut(
    rounds: Sequence[dict[str, Any]],
    *,
    project: Path | None = None,
    common_dir: Path | None = None,
    expected_merge_base: str | None = None,
    current_head_sha: str | None = None,
    task_file: str | None = None,
    expected_task_blob: str | None = None,
) -> int:
    for idx in range(len(rounds) - 1, -1, -1):
        entry = rounds[idx]
        if entry.get("outcome") != "approve":
            continue
        if expected_merge_base is None:
            return idx + 1
        if "task_file" in entry or "task_blob_sha" in entry:
            if entry.get("task_file") != task_file or entry.get("task_blob_sha") != expected_task_blob:
                continue
        elif task_file is not None:
            continue
        if "merge_base_sha" not in entry:
            return idx + 1
        entry_mb = str(entry.get("merge_base_sha") or "").strip().lower()
        mb = expected_merge_base.strip().lower()
        if bool(mb) and entry_mb == mb:
            return idx + 1
        if project is not None and _HEX_SHA_RE.fullmatch(entry_mb) and _HEX_SHA_RE.fullmatch(mb):
            if run_cmd(["git", "merge-base", "--is-ancestor", entry_mb, mb], cwd=project, check=False).returncode == 0:
                return idx + 1
            if common_dir is not None and _is_proven_parent_replacement(
                project,
                common_dir,
                entry_mb,
                str(entry.get("head_sha") or "").strip().lower(),
                mb,
                (current_head_sha or mb).strip().lower(),
            ):
                return idx + 1
    return 0


def _active_cycle_start(
    rounds: Sequence[dict[str, Any]],
    arbitration: dict[str, Any] | None = None,
    *,
    arbiter_closed_round: Any = 0,
) -> int:
    start = _effective_arbiter_closed_round(rounds, arbiter_closed_round, arbitration)
    for idx in range(len(rounds) - 1, -1, -1):
        if rounds[idx].get("outcome") == "approve":
            start = max(start, idx + 1)
            break
    return start


def _has_pending_arbiter_rework(rounds: Sequence[dict[str, Any]], arbiter_rework_after_round: Any) -> bool:
    if isinstance(arbiter_rework_after_round, bool) or not isinstance(arbiter_rework_after_round, int):
        return False
    if arbiter_rework_after_round <= 0 or arbiter_rework_after_round > len(rounds):
        return False
    return not any(r.get("outcome") in ("request_changes", "approve") for r in rounds[arbiter_rework_after_round:])


def _is_arbiter_rework_consumed(state: dict[str, Any], rounds: Sequence[dict[str, Any]]) -> bool:
    if not bool(state.get("arbiter_rework_used")):
        return False
    after_round = state.get("arbiter_rework_after_round")
    if isinstance(after_round, bool) or not isinstance(after_round, int):
        return False
    arb_cut = _effective_arbiter_closed_round(
        rounds,
        state.get("arbiter_closed_round"),
        state.get("arbitration") if isinstance(state.get("arbitration"), dict) else None,
    )
    if after_round <= 0 or after_round < arb_cut or after_round > len(rounds):
        return False
    return any(r.get("outcome") == "request_changes" for r in rounds[after_round:])


def _convergence_cap_status(
    rounds: Sequence[dict[str, Any]],
    *,
    max_review_rounds: int = 2,
    levels: Sequence[str] | None = None,
    verify_min_level: str | None = None,
    arbiter_rework_after_round: Any = None,
    arbitration: dict[str, Any] | None = None,
    arbiter_closed_round: Any = 0,
    project: Path | None = None,
    common_dir: Path | None = None,
    expected_merge_base: str | None = None,
    current_head_sha: str | None = None,
    task_file: str | None = None,
    expected_task_blob: str | None = None,
) -> dict[str, Any]:
    top_level, verify_tiers = _resolve_ladder(levels, verify_min_level)
    baseline = _manual_baseline_level(verify_tiers)
    max_top_rounds = int(max_review_rounds)
    max_total_rounds = len(verify_tiers[:-1]) + max_top_rounds

    arb_cut = max(
        _effective_arbiter_closed_round(rounds, arbiter_closed_round, arbitration),
        _latest_scoped_approve_cut(
            rounds,
            project=project,
            common_dir=common_dir,
            expected_merge_base=expected_merge_base,
            current_head_sha=current_head_sha,
            task_file=task_file,
            expected_task_blob=expected_task_blob,
        ),
    )
    active_rounds = rounds[arb_cut:]
    eff_rework_after = (
        (arbiter_rework_after_round - arb_cut)
        if isinstance(arbiter_rework_after_round, int)
        and not isinstance(arbiter_rework_after_round, bool)
        and arbiter_rework_after_round > arb_cut
        else None
    )
    rc_rounds = [r for r in active_rounds if r.get("outcome") == "request_changes"]
    total_rc = len(rc_rounds)
    top_rank = level_rank(top_level)
    top_rc = sum(1 for r in rc_rounds if level_rank(_round_level(r, default_level=baseline)) >= top_rank)
    raw_cap = top_rc >= max_top_rounds or total_rc >= max_total_rounds
    rework_pending = _has_pending_arbiter_rework(active_rounds, eff_rework_after)
    rework_exhausted = (
        isinstance(eff_rework_after, int)
        and 0 < eff_rework_after <= len(active_rounds)
        and any(r.get("outcome") == "request_changes" for r in active_rounds[eff_rework_after:])
        and not any(r.get("outcome") == "approve" for r in active_rounds[eff_rework_after:])
    )
    return {
        "cap_reached": False if rework_pending else (raw_cap or rework_exhausted),
        "raw_cap_reached": raw_cap or rework_exhausted,
        "top_level": top_level,
        "top_request_changes": top_rc,
        "max_top_rounds": max_top_rounds,
        "total_request_changes": total_rc,
        "max_total_rounds": max_total_rounds,
    }


def _convergence_cap_blocker(
    cap: dict[str, Any],
    *,
    target: str | None = None,
    slug: str | None = None,
    base_flag: str = "",
) -> str:
    cli_target = target or slug or "<slug>"
    return (
        f"Reached convergence cap ({cap['top_request_changes']}/{cap['max_top_rounds']} rejected rounds "
        f"at {cap['top_level']}, {cap['total_request_changes']}/{cap['max_total_rounds']} total): "
        f"run `keel branch arbitrate {cli_target}{base_flag}` for impartial self-recovery or escalate unresolved blockers to the human."
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
    arbiter_closed_round: Any = 0,
    arbitration: dict[str, Any] | None = None,
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

    eff_closed = _effective_arbiter_closed_round(rounds, arbiter_closed_round, arbitration)
    if rounds[-1].get("outcome") == "request_changes" and eff_closed < len(rounds):
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
    elif "implement_level" in anchor or _last_request_changes_round(rounds[eff_closed:]) != 0:
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


def _init_clean_proof_repo(repo_dir: Path, objects_dir: Path) -> Path:
    repo_dir.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "init", "--quiet", "-b", "main", str(repo_dir)], env=_sanitized_git_env())
    alt_file = repo_dir / ".git" / "objects" / "info" / "alternates"
    alt_file.parent.mkdir(parents=True, exist_ok=True)
    alt_file.write_text(f"{objects_dir}\n", encoding="utf-8")
    return repo_dir


def _normalize_prior_head_shas(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return dedup_strings(
        [
            s.strip().lower()
            for s in raw
            if isinstance(s, str) and _HEX_SHA_RE.fullmatch(s.strip())
        ]
    )


def _collect_landed_records(common_dir: Path) -> list[dict[str, Any]]:
    branches_dir = common_dir / "keel-state" / "branches"
    if not branches_dir.is_dir():
        return []
    records: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def _add(entry: Any) -> None:
        if not isinstance(entry, dict):
            return
        landed_head_sha = str(entry.get("landed_head_sha") or "").strip().lower()
        landed_sha = str(entry.get("landed_sha") or "").strip().lower()
        if not (_HEX_SHA_RE.fullmatch(landed_head_sha) and _HEX_SHA_RE.fullmatch(landed_sha)):
            return
        landed_method = str(entry.get("landed_method") or "").strip()
        old_head_sha = str(entry.get("old_head_sha") or "").strip().lower()
        if old_head_sha and not _HEX_SHA_RE.fullmatch(old_head_sha):
            old_head_sha = ""
        prior_head_shas = _normalize_prior_head_shas(entry.get("prior_head_shas"))
        key = (landed_head_sha, landed_sha, landed_method, old_head_sha)
        existing = seen.get(key)
        if existing is not None:
            if prior_head_shas:
                existing["prior_head_shas"] = dedup_strings(
                    [*(existing.get("prior_head_shas") or []), *prior_head_shas]
                )
            return
        rec: dict[str, Any] = {
            "landed_head_sha": landed_head_sha,
            "landed_sha": landed_sha,
        }
        if landed_method:
            rec["landed_method"] = landed_method
        if old_head_sha:
            rec["old_head_sha"] = old_head_sha
        if prior_head_shas:
            rec["prior_head_shas"] = prior_head_shas
        seen[key] = rec
        records.append(rec)

    for state_path in sorted(branches_dir.glob("*.json")):
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        raw_history = data.get("landed_history")
        if isinstance(raw_history, list):
            for item in raw_history:
                _add(item)
        if not data.get("discarded"):
            _add(data)
    return records


def _is_proven_parent_replacement(
    project: Path,
    common_dir: Path,
    old_mb: str,
    old_head: str,
    mb: str,
    head: str,
) -> bool:
    for sha in (old_mb, old_head, mb, head):
        if not _HEX_SHA_RE.fullmatch(sha):
            return False
        if run_cmd(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=project, check=False).returncode != 0:
            return False

    if run_cmd(["git", "merge-base", "--is-ancestor", mb, head], cwd=project, check=False).returncode != 0:
        return False

    common_proc = run_cmd(["git", "merge-base", old_mb, mb], cwd=project, check=False)
    if common_proc.returncode != 0 or not common_proc.stdout.strip():
        return False
    common_anc = common_proc.stdout.strip().lower()
    if common_anc == old_mb or common_anc == mb:
        return False

    old_parent_proc = run_cmd(
        ["git", "rev-list", "--first-parent", "--reverse", f"{common_anc}..{old_mb}"],
        cwd=project,
        check=False,
    )
    if old_parent_proc.returncode != 0:
        return False
    old_parent_revs = [ln.strip().lower() for ln in old_parent_proc.stdout.splitlines() if ln.strip()]
    k = len(old_parent_revs)
    if k == 0:
        return False

    mb_revs_proc = run_cmd(
        ["git", "rev-list", "--first-parent", "--reverse", f"{common_anc}..{mb}"],
        cwd=project,
        check=False,
    )
    if mb_revs_proc.returncode != 0:
        return False
    mb_chain = [common_anc, *[ln.strip().lower() for ln in mb_revs_proc.stdout.splitlines() if ln.strip()]]
    if len(mb_chain) < 2:
        return False

    old_parent_has_diff = (
        run_cmd(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "rerere.enabled=false",
                "diff",
                "--quiet",
                common_anc,
                old_mb,
            ],
            cwd=project,
            env=_sanitized_git_env(),
            check=False,
        ).returncode
        == 1
    )
    objects_dir = common_dir / "objects"

    with tempfile.TemporaryDirectory(prefix="keel-parent-proof-") as tmp:
        tmp_path = Path(tmp)

        attempt_idx = 0
        for i in range(len(mb_chain) - 2, -1, -1):
            b_new = mb_chain[i]
            candidates: list[str] = []
            if i + k < len(mb_chain):
                candidates.append(mb_chain[i + k])
            for cand in (mb_chain[i + 1], mb_chain[-1]):
                if cand not in candidates:
                    candidates.append(cand)

            for p_new in candidates:
                attempt_idx += 1
                clean_parent_repo = _init_clean_proof_repo(tmp_path / f"parent-{attempt_idx}", objects_dir)
                try:
                    _prove_rebase_in_clean_repo(
                        clean_parent_repo,
                        common_anc,
                        old_mb,
                        b_new,
                        p_new,
                        require_base_ancestry=True,
                    )
                    return True
                except KeelError:
                    pass

        if old_parent_has_diff:
            checked_cand_heads: set[str] = set()
            for rec in _collect_landed_records(common_dir):
                landed_sha = rec["landed_sha"]
                landed_head_sha = rec["landed_head_sha"]
                if (
                    run_cmd(["git", "cat-file", "-e", f"{landed_sha}^{{commit}}"], cwd=project, check=False).returncode != 0
                    or run_cmd(["git", "cat-file", "-e", f"{landed_head_sha}^{{commit}}"], cwd=project, check=False).returncode != 0
                ):
                    continue
                if run_cmd(["git", "merge-base", "--is-ancestor", landed_sha, mb], cwd=project, check=False).returncode != 0:
                    continue
                if run_cmd(["git", "merge-base", "--is-ancestor", landed_sha, common_anc], cwd=project, check=False).returncode == 0:
                    continue

                cand_heads = dedup_strings(
                    [
                        landed_head_sha,
                        *([rec["old_head_sha"]] if rec.get("old_head_sha") else []),
                        *(rec.get("prior_head_shas") or []),
                    ]
                )

                for cand_head in cand_heads:
                    if cand_head in checked_cand_heads:
                        continue
                    if (
                        cand_head != landed_head_sha
                        and run_cmd(["git", "cat-file", "-e", f"{cand_head}^{{commit}}"], cwd=project, check=False).returncode != 0
                    ):
                        continue
                    checked_cand_heads.add(cand_head)

                    if run_cmd(["git", "merge-base", "--is-ancestor", old_mb, cand_head], cwd=project, check=False).returncode == 0:
                        return True

                    lh_anc_proc = run_cmd(["git", "merge-base", old_mb, cand_head], cwd=project, check=False)
                    if lh_anc_proc.returncode != 0 or not lh_anc_proc.stdout.strip():
                        continue
                    lh_anc = lh_anc_proc.stdout.strip().lower()
                    if lh_anc == old_mb or lh_anc == cand_head:
                        continue

                    lh_old_proc = run_cmd(
                        ["git", "rev-list", "--first-parent", "--reverse", f"{lh_anc}..{old_mb}"],
                        cwd=project,
                        check=False,
                    )
                    lh_chain_proc = run_cmd(
                        ["git", "rev-list", "--first-parent", "--reverse", f"{lh_anc}..{cand_head}"],
                        cwd=project,
                        check=False,
                    )
                    if lh_old_proc.returncode != 0 or lh_chain_proc.returncode != 0:
                        continue
                    lh_old_revs = [ln.strip().lower() for ln in lh_old_proc.stdout.splitlines() if ln.strip()]
                    k_lh = len(lh_old_revs)
                    lh_chain = [
                        lh_anc,
                        *[ln.strip().lower() for ln in lh_chain_proc.stdout.splitlines() if ln.strip()],
                    ]
                    if k_lh == 0 or len(lh_chain) < 2:
                        continue

                    for j in range(len(lh_chain) - 2, -1, -1):
                        b_lh = lh_chain[j]
                        lh_candidates: list[str] = []
                        if j + k_lh < len(lh_chain):
                            lh_candidates.append(lh_chain[j + k_lh])
                        for cand_p in (lh_chain[j + 1], lh_chain[-1]):
                            if cand_p not in lh_candidates:
                                lh_candidates.append(cand_p)

                        for p_lh in lh_candidates:
                            attempt_idx += 1
                            clean_lh_repo = _init_clean_proof_repo(tmp_path / f"lh-{attempt_idx}", objects_dir)
                            try:
                                _prove_rebase_in_clean_repo(
                                    clean_lh_repo,
                                    lh_anc,
                                    old_mb,
                                    b_lh,
                                    p_lh,
                                    require_base_ancestry=True,
                                )
                                return True
                            except KeelError:
                                pass
    return False


def _is_capped_base_advance_refresh(
    project: Path,
    rounds: Sequence[dict[str, Any]],
    *,
    cap: dict[str, Any],
    arbiter_closed_round: Any = 0,
    arbitration: dict[str, Any] | None = None,
    current_merge_base: str,
    current_head_sha: str,
    current_task_file: str | None = None,
    current_task_blob_sha: str | None = None,
    common_dir: Path | None = None,
) -> bool:
    if not cap.get("cap_reached") or not current_merge_base or not current_head_sha:
        return False
    eff_common_dir = common_dir if common_dir is not None else git_dirs(project)[0]
    arb_cut = max(
        _effective_arbiter_closed_round(rounds, arbiter_closed_round, arbitration),
        _latest_scoped_approve_cut(
            rounds,
            project=project,
            common_dir=eff_common_dir,
            expected_merge_base=current_merge_base,
            current_head_sha=current_head_sha,
            task_file=current_task_file,
            expected_task_blob=current_task_blob_sha,
        ),
    )
    active_rounds = rounds[arb_cut:]
    if not active_rounds:
        return False
    last_round = active_rounds[-1]
    if last_round.get("outcome") != "request_changes":
        return False
    mb = current_merge_base.strip().lower()
    head = current_head_sha.strip().lower()
    old_mb = str(last_round.get("merge_base_sha") or "").strip().lower()
    if not old_mb or not mb or not head or old_mb == mb:
        return False
    latest_for_head = next(
        (r for r in reversed(active_rounds) if str(r.get("head_sha") or "").strip().lower() == head),
        None,
    )
    if latest_for_head is not None and _round_entry_matches_scope(
        latest_for_head,
        expected_merge_base=mb,
        task_file=current_task_file,
        expected_task_blob=current_task_blob_sha,
    ):
        return False
    if any(
        bool(r.get("rebase_refresh")) and str(r.get("merge_base_sha") or "").strip().lower() == mb
        for r in active_rounds
    ):
        return False
    if not (_HEX_SHA_RE.fullmatch(old_mb) and _HEX_SHA_RE.fullmatch(mb)):
        return False
    if run_cmd(["git", "merge-base", "--is-ancestor", old_mb, mb], cwd=project, check=False).returncode == 0:
        return True
    for cand_round in reversed(active_rounds):
        if cand_round.get("outcome") != "request_changes":
            continue
        cand_mb = str(cand_round.get("merge_base_sha") or "").strip().lower()
        cand_head = str(cand_round.get("head_sha") or "").strip().lower()
        if _is_proven_parent_replacement(project, eff_common_dir, cand_mb, cand_head, mb, head):
            return True
    return False


def _rework_floor(
    rounds: Sequence[dict[str, Any]],
    *,
    levels: Sequence[str] | None = None,
    verify_min_level: str | None = None,
    arbiter_rework_after_round: Any = None,
    arbitration: dict[str, Any] | None = None,
    arbiter_closed_round: Any = 0,
) -> str | None:
    arb_cut = _effective_arbiter_closed_round(rounds, arbiter_closed_round, arbitration)
    active_rounds = rounds[arb_cut:]
    if not active_rounds or active_rounds[-1].get("outcome") != "request_changes":
        return None
    top_level, verify_tiers = _resolve_ladder(levels, verify_min_level)
    eff_rework_after = (
        (arbiter_rework_after_round - arb_cut)
        if isinstance(arbiter_rework_after_round, int)
        and not isinstance(arbiter_rework_after_round, bool)
        and arbiter_rework_after_round > arb_cut
        else None
    )
    if _has_pending_arbiter_rework(active_rounds, eff_rework_after):
        return top_level
    start_idx = _active_cycle_start(active_rounds, None)
    if start_idx >= len(active_rounds):
        return None
    min_verify = verify_tiers[0]
    baseline = _manual_baseline_level(verify_tiers)
    active_floor: str | None = None
    for entry in active_rounds[start_idx:]:
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
    arbiter_rework_after_round: Any = None,
    arbitration: dict[str, Any] | None = None,
    arbiter_closed_round: Any = 0,
) -> str | None:
    floor = _rework_floor(
        rounds,
        levels=levels,
        verify_min_level=verify_min_level,
        arbiter_rework_after_round=arbiter_rework_after_round,
        arbitration=arbitration,
        arbiter_closed_round=arbiter_closed_round,
    )
    if floor is None or fresh_write:
        return floor
    latest_level = rounds[-1].get("level")
    if latest_level:
        return max_level(floor, _escalate_in_ladder(latest_level, levels))
    return floor


def build_branch_lineage_context(
    workdir: Path,
    mode: str,
    defaults: dict[str, Any],
    *,
    supersedes: str | None = None,
) -> str | None:
    in_git = run_cmd(["git", "rev-parse", "--is-inside-work-tree"], cwd=workdir, check=False).returncode == 0
    if not in_git:
        if supersedes is not None:
            validate_supersedes_target(supersedes)
            raise KeelError("--supersedes requires running inside or pointing --project at a Git repository.")
        return None
    common_dir, _ = git_dirs(workdir)
    branch = run_cmd(["git", "branch", "--show-current"], cwd=workdir, check=False).stdout.strip()
    is_task_branch = bool(branch and branch not in defaults.get("default_branches", ("main", "master")))

    lin: dict[str, Any] | None = None
    if supersedes is not None:
        lin = resolve_supersedes_lineage(common_dir, supersedes)
        old_slug = str(lin.get("slug") or "") or None
        if is_task_branch:
            apply_lineage_to_branch_state(common_dir, branch, lin, source_slug=old_slug)
    elif mode == "write" and is_task_branch:
        cand_lin = load_lineage_for_branch(common_dir, branch)
        if cand_lin is not None:
            lin = cand_lin
            slug = str(lin.get("slug") or (branch[5:] if branch.startswith("task/") else branch.split("/")[-1]))
            cur_st = load_branch_state(common_dir, branch)
            if int(lin.get("recovery_count") or 0) > int(cur_st.get("recovery_count") or 0) or not cur_st.get(
                "superseded_from"
            ):
                apply_lineage_to_branch_state(common_dir, branch, lin, source_slug=slug)

    if mode != "write" and supersedes is None:
        return None

    if is_task_branch:
        st = load_branch_state(common_dir, branch)
        rec_count = int(st.get("recovery_count") or 0)
        dead_ends = dedup_strings(st.get("avoid_dead_ends") or [])
        directives = dedup_strings(st.get("prior_directives") or [])
    elif lin is not None:
        rec_count = int(lin.get("recovery_count") or 0)
        dead_ends = dedup_strings(lin.get("avoid_dead_ends") or [])
        directives = dedup_strings(lin.get("prior_directives") or [])
    else:
        return None

    if not dead_ends and not directives:
        return None

    lines = ["### Prior Attempt Dead-Ends & Arbiter Directives"]
    if rec_count > 0:
        lines.append(f"- **Structural Recovery Attempt:** {rec_count}")
    if directives:
        lines.append("- **Arbiter Directives (follow strictly):**")
        for item in directives:
            lines.append(f"  - {item}")
    if dead_ends:
        lines.append("- **Failed Dead Ends to Avoid (do NOT repeat):**")
        for item in dead_ends:
            lines.append(f"  - {item}")
    return "\n".join(lines)


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
        arbiter_rework_after_round=state.get("arbiter_rework_after_round"),
        arbitration=state.get("arbitration"),
        arbiter_closed_round=state.get("arbiter_closed_round"),
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
    why: str | None = None,
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
    raw_why = (why[1:] if why.startswith("\ufeff") else why) if isinstance(why, str) else None
    clean_why = (trim_markdown_block(raw_why) or None) if raw_why is not None else None
    with edit_gate_state(common_dir, branch) as state:
        rounds = state["rounds"]
        prev_write = state.get("last_write") or {}
        if clean_why is None:
            prev_why = prev_write.get("why")
            if isinstance(prev_why, str):
                raw_prev = prev_why[1:] if prev_why.startswith("\ufeff") else prev_why
                clean_why = trim_markdown_block(raw_prev) or None
        write: dict[str, Any] = {
            "executor": executor,
            "level": level,
            "after_round": len(rounds),
            "recorded_at": _now_iso(),
        }
        if clean_why:
            write["why"] = clean_why
        if head_sha:
            write["head_sha"] = head_sha
        if head_tree_sha:
            write["head_tree_sha"] = head_tree_sha
        if pending_tree_shas:
            write["pending_tree_shas"] = pending_tree_shas
        floor = _rework_floor(
            rounds,
            levels=levels,
            verify_min_level=verify_min_level,
            arbiter_rework_after_round=state.get("arbiter_rework_after_round"),
            arbitration=state.get("arbitration"),
            arbiter_closed_round=state.get("arbiter_closed_round"),
        )
        if floor and level_rank(level) >= level_rank(floor):
            write["reworked_round"] = len(rounds)
        state["last_write"] = write


def _request_changes_count(rounds: Sequence[dict[str, Any]]) -> int:
    return sum(r["outcome"] == "request_changes" for r in rounds)


def dispute_branch(
    project: Path,
    branch_or_slug: str,
    *,
    reason: str,
    base: str | None = None,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_reason = reason[1:] if isinstance(reason, str) and reason.startswith("\ufeff") else (reason or "")
    clean_reason = trim_markdown_block(raw_reason)
    if not clean_reason:
        raise KeelError("Provide a non-empty --reason (or --reason-file) for `keel branch dispute`.")

    project = primary_checkout_root(project)
    common_dir, _ = git_dirs(project)
    branch, slug = resolve_branch_and_slug(project, branch_or_slug, must_exist=True)
    cli_target = _cli_branch_target(branch, slug)
    default_branches = tuple((defaults or {}).get("default_branches", ("main", "master")))
    base_ref = resolve_base_ref(project, base, default_branches=default_branches)
    base_flag = _cli_base_flag(project, base_ref, default_branches=default_branches)
    head_sha = run_cmd(["git", "rev-parse", f"refs/heads/{branch}"], cwd=project).stdout.strip().lower()
    base_tip_sha = _resolve_ref_tip_sha(project, base_ref)
    mb_proc = run_cmd(["git", "merge-base", base_tip_sha, head_sha], cwd=project, check=False)
    merge_base_sha = mb_proc.stdout.strip().lower() if mb_proc.returncode == 0 else ""

    with edit_gate_state(common_dir, branch) as state:
        state["branch"] = branch
        if state.get("parked"):
            raise KeelError(
                f"Branch '{branch}' is parked for human decision: {state.get('park_reason') or 'awaiting human decision'}"
            )
        task_file = discover_task_file(
            project,
            branch,
            slug,
            base_ref=base_ref,
            head_sha=head_sha,
            state_task_file=state.get("task_file"),
        )
        expected_task_blob = _effective_task_blob_sha(project, base_tip_sha, head_sha, task_file)
        rounds = state["rounds"]
        arb = state.get("arbitration")
        if _arbiter_replan_pivot_matches(
            arb,
            head_sha=head_sha,
            merge_base_sha=merge_base_sha,
            task_file=task_file,
            expected_task_blob=expected_task_blob,
            round_count=len(rounds),
        ):
            assert isinstance(arb, dict)
            raise KeelError(
                f"Branch '{branch}' was already adjudicated as '{arb['action']}' by the Arbiter; run `keel branch supersede {cli_target}{base_flag}`."
            )
        cycle_start = _active_cycle_start(
            rounds,
            arb if isinstance(arb, dict) else None,
            arbiter_closed_round=state.get("arbiter_closed_round"),
        )
        cycle_rounds = rounds[cycle_start:]
        if not cycle_rounds or rounds[-1].get("outcome") != "request_changes":
            raise KeelError(
                f"Branch '{branch}' has no active 'request_changes' round to dispute."
            )
        entry = {
            "round": len(rounds),
            "reviewed_head_sha": rounds[-1]["head_sha"],
            "head_sha": head_sha,
            "reason": clean_reason,
            "recorded_at": _now_iso(),
        }
        rebuttals = list(state.get("rebuttals") or [])
        rebuttals.append(entry)
        state["rebuttals"] = rebuttals

    return {
        "branch": branch,
        "slug": slug,
        "base_ref": base_ref,
        "head_sha": head_sha,
        "rebuttal": entry,
        "rebuttal_count": len(rebuttals),
    }


def _active_cycle_rebuttals(state: dict[str, Any], cycle_start: int) -> list[dict[str, Any]]:
    return [
        r
        for r in (state.get("rebuttals") or [])
        if isinstance(r, dict)
        and isinstance(r.get("round"), int)
        and r["round"] > cycle_start
        and bool(str(r.get("reason") or "").strip())
    ]


def _cycle_baseline_spec(
    project: Path,
    rounds: Sequence[dict[str, Any]],
    arbitration: dict[str, Any] | None,
    cycle_start: int,
    task_file: str | None,
    locked_spec: str | None,
    *,
    arbiter_closed_round: Any = 0,
    arbiter_closed_head_sha: str | None = None,
) -> str | None:
    if not task_file or cycle_start <= 0:
        return locked_spec
    last_closed_sha: str | None = None
    if cycle_start - 1 < len(rounds) and rounds[cycle_start - 1].get("outcome") == "approve":
        raw_sha = rounds[cycle_start - 1].get("head_sha")
        if isinstance(raw_sha, str) and raw_sha:
            last_closed_sha = raw_sha
    if (
        last_closed_sha is None
        and isinstance(arbiter_closed_round, int)
        and not isinstance(arbiter_closed_round, bool)
        and arbiter_closed_round == cycle_start
        and isinstance(arbiter_closed_head_sha, str)
        and arbiter_closed_head_sha
    ):
        last_closed_sha = arbiter_closed_head_sha
    if (
        last_closed_sha is None
        and isinstance(arbitration, dict)
        and arbitration.get("action") == "approve"
        and arbitration.get("after_round") == cycle_start
    ):
        raw_sha = arbitration.get("head_sha")
        if isinstance(raw_sha, str) and raw_sha:
            last_closed_sha = raw_sha
    if last_closed_sha is None and cycle_start - 1 < len(rounds):
        raw_sha = rounds[cycle_start - 1].get("head_sha")
        if isinstance(raw_sha, str) and raw_sha:
            last_closed_sha = raw_sha
    if last_closed_sha:
        prior_spec = _read_blob_text(project, last_closed_sha, task_file)
        if prior_spec is not None:
            return prior_spec
    return locked_spec


def _has_branch_rebuttal_section(locked_spec: str | None, branch_spec: str | None) -> bool:
    if not branch_spec:
        return False
    if locked_spec is not None:
        if branch_spec == locked_spec:
            return False
        return bool(extract_changed_markdown_sections(locked_spec, branch_spec, ("Review Rebuttals",), levels=(2, 3)))
    return bool(extract_markdown_section(branch_spec, ("Review Rebuttals",), levels=(2, 3)))


def _build_advisory_task_sections(
    locked_spec: str,
    branch_spec: str | None,
    cycle_base_spec: str | None,
    *,
    include_cycle_rebuttals: bool = True,
) -> tuple[str | None, bool]:
    """Return `(advisory_markdown, has_cycle_rebuttals)` with non-rebuttal notes relative to `locked_spec`
    and active-cycle `Review Rebuttals` relative to `cycle_base_spec`."""
    if not branch_spec:
        return None, False
    shared_advisory = (
        extract_changed_markdown_sections(
            locked_spec,
            branch_spec,
            ("Progress", "Decisions & Findings"),
            levels=(2, 3),
            exclude_headings=("Review Rebuttals",),
        )
        if branch_spec != locked_spec
        else None
    )
    cycle_rebuttals: str | None = None
    if include_cycle_rebuttals:
        rebuttal_base = cycle_base_spec if cycle_base_spec is not None else locked_spec
        if branch_spec != rebuttal_base:
            cycle_rebuttals = extract_changed_markdown_sections(
                rebuttal_base,
                branch_spec,
                ("Review Rebuttals",),
                levels=(2, 3),
            )
    parts = [part for part in (shared_advisory, cycle_rebuttals) if part]
    if not parts:
        return None, False
    return "\n\n".join(parts), bool(cycle_rebuttals)


def _filter_branch_spec_for_brief(
    branch_spec: str,
    cycle_base_spec: str | None,
    *,
    include_cycle_rebuttals: bool = True,
) -> tuple[str, bool]:
    """Filter branch-only task specs (`locked_spec is None`) so closed prior-cycle rebuttals do not leak."""
    if not include_cycle_rebuttals:
        return trim_markdown_block(_strip_markdown_sections(branch_spec, ("Review Rebuttals",))), False
    if cycle_base_spec is None:
        has_reb = bool(extract_markdown_section(branch_spec, ("Review Rebuttals",), levels=(2, 3)))
        return branch_spec, has_reb
    stripped = trim_markdown_block(_strip_markdown_sections(branch_spec, ("Review Rebuttals",)))
    cycle_rebuttals = (
        extract_changed_markdown_sections(
            cycle_base_spec,
            branch_spec,
            ("Review Rebuttals",),
            levels=(2, 3),
        )
        if branch_spec != cycle_base_spec
        else None
    )
    parts = [part for part in (stripped, cycle_rebuttals) if part]
    return "\n\n".join(parts), bool(cycle_rebuttals)


def _build_carry_forward_ledger(
    project: Path,
    head_sha: str,
    cycle_rounds: Sequence[dict[str, Any]],
    cycle_start: int,
    active_rebuttals: Sequence[dict[str, Any]],
    arbitration: dict[str, Any] | None,
) -> str:
    prev_round = cycle_rounds[-1]
    prev_sha = str(prev_round.get("head_sha") or "")
    prev_level = str(prev_round.get("level") or "unknown")
    prev_round_num = cycle_start + len(cycle_rounds)
    lines = [
        "### Prior Review Carry-Forward Ledger (re-adjudicate prior items; full diff remains open for genuine missed defects)",
        f"- **Prior round:** Round {prev_round_num} at `{prev_level}` on commit `{prev_sha}`",
    ]
    if prev_sha == head_sha:
        lines.append(
            "- **Commit delta since prior review:** None (`prev_sha == head_sha`; tree unchanged—evaluate author rebuttals below alongside full diff)."
        )
    else:
        delta = run_cmd(["git", "log", "--oneline", f"{prev_sha}..{head_sha}"], cwd=project, check=False)
        if delta.returncode == 0 and delta.stdout.strip():
            lines.extend(
                [
                    f"- **Commit delta since prior review (`{prev_sha[:12]}..{head_sha[:12]}`):**",
                    f"```\n{delta.stdout.strip()}\n```",
                ]
            )
        else:
            lines.append(f"- **Commit delta since prior review:** Branch history updated (`{prev_sha[:12]}` -> `{head_sha[:12]}`).")

    lines.append("- **Prior unresolved `blocking` items to re-adjudicate:**")
    has_blocking = False
    for v_exec, v_payload in (prev_round.get("verdicts") or {}).items():
        if isinstance(v_payload, dict):
            for item in v_payload.get("blocking") or []:
                lines.append(f"  - [`{v_exec}`] {item}")
                has_blocking = True
    if not has_blocking:
        lines.append("  - (none)")

    if active_rebuttals:
        lines.append("- **Structured Author Rebuttals (`keel branch dispute`):**")
        for reb in active_rebuttals:
            r_sha = str(reb.get("head_sha") or "")[:12]
            lines.append(f"  - **After Round {reb['round']} (commit `{r_sha}`):** {reb['reason']}")

    if (
        isinstance(arbitration, dict)
        and arbitration.get("action") == "rework_once"
        and isinstance(arbitration.get("after_round"), int)
        and arbitration["after_round"] > cycle_start
        and arbitration.get("directives")
    ):
        lines.append("- **Arbiter `rework_once` Directives:**")
        for directive in arbitration["directives"]:
            lines.append(f"  - {directive}")
    return "\n".join(lines)


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
            why=why,
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


def _round_entry_matches_scope(
    entry: dict[str, Any],
    *,
    expected_merge_base: str,
    task_file: str | None,
    expected_task_blob: str | None,
) -> bool:
    return (
        "task_file" in entry
        and entry["task_file"] == task_file
        and "task_blob_sha" in entry
        and entry["task_blob_sha"] == expected_task_blob
        and bool(expected_merge_base)
        and entry.get("merge_base_sha") == expected_merge_base
    )


def _arbiter_replan_pivot_matches(
    arb: Any,
    *,
    head_sha: str,
    merge_base_sha: str,
    task_file: str | None,
    expected_task_blob: str | None,
    round_count: int,
) -> bool:
    return (
        isinstance(arb, dict)
        and arb.get("action") in ("replan", "pivot")
        and arb.get("after_round") == round_count
        and arb.get("head_sha") == head_sha
        and _round_entry_matches_scope(
            arb,
            expected_merge_base=merge_base_sha,
            task_file=task_file,
            expected_task_blob=expected_task_blob,
        )
    )


def _worktree_lock_reason(project: Path, wt_path: Path) -> str | None:
    for entry in list_worktrees(project):
        wt_str = entry.get("worktree")
        if wt_str and same_path(wt_str, wt_path) and "locked" in entry:
            return entry["locked"] or "locked"
    return None


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
    auto_arbitrate: bool = False,
    caller_cwd: Path | None = None,
) -> dict[str, Any]:
    if checks and skip_checks:
        raise KeelError("Cannot combine --check and --skip-checks.")
    if skip_checks is not None and not skip_checks.strip():
        raise KeelError("--skip-checks requires a non-empty reason.")

    if dual and any(value is not None for value in (executor, model, effort)):
        raise KeelConfigError("--dual selects both executors. Use --level to select their tier.")

    extra_ctx = trim_markdown_block(read_utf8_file(context_file, "--context-file")) if context_file else ""

    project = primary_checkout_root(project)
    ensure_agents_dir_ignored(project)
    common_dir, _ = git_dirs(project)
    branch, slug = resolve_branch_and_slug(project, branch_or_slug, must_exist=True)
    cli_target = _cli_branch_target(branch, slug)
    default_branches = tuple(defaults.get("default_branches", ["main", "master"]))
    base_ref = resolve_base_ref(project, base, default_branches=default_branches)
    base_flag = _cli_base_flag(project, base_ref, default_branches=default_branches)
    base_branch_name = base_ref[len("refs/heads/") :] if base_ref.startswith("refs/heads/") else base_ref

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

    task_file = discover_task_file(
        project,
        branch,
        slug,
        base_ref=base_ref,
        head_sha=head_sha,
        explicit_task_file=task_file_arg,
        state_task_file=initial_state.get("task_file"),
    )
    task_blob_sha = _effective_task_blob_sha(project, base_tip_sha, head_sha, task_file)

    def check_round_limit(state: dict[str, Any]) -> bool:
        if state.get("parked") and not force_round:
            raise KeelError(
                f"Branch '{branch}' is parked for human decision: {state.get('park_reason') or 'awaiting human decision'}"
            )
        arb = state.get("arbitration")
        arb_dict = arb if isinstance(arb, dict) else None
        state_rounds = _review_rounds(state)
        if not force_round and _arbiter_replan_pivot_matches(
            arb_dict,
            head_sha=head_sha,
            merge_base_sha=merge_base_sha,
            task_file=task_file,
            expected_task_blob=task_blob_sha,
            round_count=len(state_rounds),
        ):
            assert arb_dict is not None
            raise KeelError(
                f"Arbiter directed '{arb_dict['action']}' on branch '{branch}'; "
                f"run `keel branch supersede {cli_target}{base_flag}` instead of starting another verification round."
            )
        cap = _convergence_cap_status(
            state_rounds,
            max_review_rounds=max_rounds,
            levels=configured_levels,
            verify_min_level=verify_min_level,
            arbiter_rework_after_round=state.get("arbiter_rework_after_round"),
            arbitration=arb_dict,
            arbiter_closed_round=state.get("arbiter_closed_round"),
            project=project,
            common_dir=common_dir,
            expected_merge_base=merge_base_sha,
            current_head_sha=head_sha,
            task_file=task_file,
            expected_task_blob=task_blob_sha,
        )
        if cap["cap_reached"] and not force_round:
            allow_equal_head = head_sha == base_tip_sha and branch != base_branch_name and branch not in default_branches
            if (head_sha != merge_base_sha or allow_equal_head) and _is_capped_base_advance_refresh(
                project,
                state_rounds,
                cap=cap,
                arbiter_closed_round=state.get("arbiter_closed_round"),
                arbitration=arb_dict,
                current_merge_base=merge_base_sha,
                current_head_sha=head_sha,
                current_task_file=task_file,
                current_task_blob_sha=task_blob_sha,
                common_dir=common_dir,
            ):
                return True
            raise KeelError(
                f"Branch '{branch}' reached its convergence cap "
                f"({cap['top_request_changes']}/{cap['max_top_rounds']} rejected rounds at {cap['top_level']}, "
                f"{cap['total_request_changes']}/{cap['max_total_rounds']} total). "
                f"Run `keel branch arbitrate {cli_target}{base_flag}` to adjudicate the deadlock, or stop looping and escalate unresolved blockers to the human."
            )
        return False

    capped_rebase_refresh = check_round_limit(initial_state)
    top_level, verify_tiers = _resolve_ladder(configured_levels, verify_min_level)
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
        arbiter_closed_round=initial_state.get("arbiter_closed_round"),
        arbitration=initial_state.get("arbitration") if isinstance(initial_state.get("arbitration"), dict) else None,
    )
    implement_level = normalize_level(last_write["level"]) if fresh_write else None
    floor = _verify_floor(
        initial_rounds,
        fresh_write=fresh_write,
        levels=configured_levels,
        verify_min_level=verify_min_level,
        arbiter_rework_after_round=initial_state.get("arbiter_rework_after_round"),
        arbitration=initial_state.get("arbitration"),
        arbiter_closed_round=initial_state.get("arbiter_closed_round"),
    )
    minimum = verify_min_level
    if implement_level:
        minimum = max_level(minimum, implement_level)
    elif initial_rounds and (
        initial_rounds[-1].get("outcome") == "approve"
        or _effective_arbiter_closed_round(
            initial_rounds,
            initial_state.get("arbiter_closed_round"),
            initial_state.get("arbitration") if isinstance(initial_state.get("arbitration"), dict) else None,
        )
        >= len(initial_rounds)
    ):
        prior_impl_entry = next(
            (r for r in reversed(initial_rounds) if r.get("head_sha") == head_sha and r.get("implement_level")),
            None,
        )
        if prior_impl_entry is not None:
            minimum = max_level(minimum, normalize_level(prior_impl_entry["implement_level"]))
    if floor:
        minimum = max_level(minimum, floor)
    if capped_rebase_refresh:
        minimum = max_level(minimum, top_level)
    minimum = _ceil_in_tiers(minimum, verify_tiers)
    if level is None:
        level = minimum if implement_level else max_level(minimum, default_manual_level)
    level = normalize_level(level, defaults={"levels": configured_levels})
    if level_rank(level) < level_rank(minimum):
        raise KeelConfigError(f"Verification requires --level {minimum} or higher, got {level}.")

    if (
        task_file_arg
        and task_file
        and initial_state.get("task_file") is None
        and not initial_rounds
        and initial_state.get("arbitration") is None
    ):
        with edit_gate_state(common_dir, branch) as state:
            if state.get("task_file") is None and not state["rounds"] and state.get("arbitration") is None:
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
        prev_checks = (load_branch_state(common_dir, branch).get("checks_by_sha") or {}).get(head_sha) or {}
        failed_prev = [c["command"] for c in (prev_checks.get("commands") or []) if not c.get("ok")]
        if failed_prev:
            raise KeelError(
                f"--skip-checks cannot clear failed deterministic checks on commit {head_sha[:12]} "
                f"({', '.join(failed_prev)}); re-run passing --check command(s) first."
            )
        _record_skipped_checks(common_dir, branch, head_sha, skip_checks)

    initial_arb = initial_state.get("arbitration")
    has_active_replan_pivot = (
        isinstance(initial_arb, dict)
        and initial_arb.get("action") in ("replan", "pivot")
        and initial_arb.get("head_sha") == head_sha
        and initial_arb.get("after_round") == len(initial_rounds)
    )
    if task_file and task_file != initial_state.get("task_file") and not has_active_replan_pivot:
        with edit_gate_state(common_dir, branch) as state:
            live_arb = state.get("arbitration")
            if not (
                isinstance(live_arb, dict)
                and live_arb.get("action") in ("replan", "pivot")
                and live_arb.get("head_sha") == head_sha
                and live_arb.get("after_round") == len(state["rounds"])
            ):
                state["task_file"] = task_file

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
    spec_why = extract_markdown_section(locked_spec or branch_spec or "", WHY_HEADINGS, levels=(2,))
    raw_recorded_why = last_write.get("why")
    if isinstance(raw_recorded_why, str) and raw_recorded_why.startswith("\ufeff"):
        raw_recorded_why = raw_recorded_why[1:]
    recorded_why = (trim_markdown_block(raw_recorded_why) or None) if isinstance(raw_recorded_why, str) else None
    raw_explicit_why = (why[1:] if why.startswith("\ufeff") else why) if isinstance(why, str) else None
    explicit_why = (trim_markdown_block(raw_explicit_why) or None) if raw_explicit_why is not None else None
    why_text = (
        explicit_why
        or spec_why
        or recorded_why
        or (
            f"Independently verify branch '{branch}' against the repository's Core need and locked task bounds before landing onto '{base_ref}'."
        )
    )

    base_task_lines = [
        f"- **Branch:** `{branch}`",
        f"- **Target base:** `{base_ref}` (`{base_tip_sha}`)",
        f"- **Pinned head commit:** `{head_sha}`",
        f"- **Merge-base commit:** `{merge_base_sha}` (inspect full diff via `git diff {merge_base_sha}..{head_sha}`)",
    ]
    if task_file:
        base_task_lines.append(f"- **Task ledger file:** `{task_file}`")
    if commit_log:
        base_task_lines.extend(["", "### Commits Under Review", f"```\n{commit_log}\n```"])

    cycle_start = _active_cycle_start(
        initial_rounds,
        initial_state.get("arbitration") if isinstance(initial_state.get("arbitration"), dict) else None,
        arbiter_closed_round=initial_state.get("arbiter_closed_round"),
    )
    cycle_rounds = initial_rounds[cycle_start:]
    active_rebuttals = _active_cycle_rebuttals(initial_state, cycle_start)
    cycle_base_spec = _cycle_baseline_spec(
        project,
        initial_rounds,
        initial_state.get("arbitration") if isinstance(initial_state.get("arbitration"), dict) else None,
        cycle_start,
        task_file,
        locked_spec,
        arbiter_closed_round=initial_state.get("arbiter_closed_round"),
        arbiter_closed_head_sha=initial_state.get("arbiter_closed_head_sha"),
    )
    had_rebuttal_before_round = len(cycle_rounds) >= 1 and (
        bool(active_rebuttals) or _has_branch_rebuttal_section(cycle_base_spec, branch_spec)
    )

    primary_task_lines = list(base_task_lines)
    primary_has_rebuttals = False
    if locked_spec:
        primary_task_lines.extend(
            [
                "",
                f"### Locked Task Specification on `{base_ref}` (verify `Goal & Why`, `Bounds`, and `Done-check` were NOT weakened)",
                locked_spec,
            ]
        )
        advisory, primary_has_rebuttals = _build_advisory_task_sections(
            locked_spec,
            branch_spec,
            cycle_base_spec,
            include_cycle_rebuttals=True,
        )
        if advisory:
            advisory_heading = (
                "### Branch Progress, Decisions & Rebuttals (advisory author context; Locked Task Specification above remains authoritative for Goal & Why, Bounds, and Done-check)"
                if primary_has_rebuttals
                else "### Branch Progress & Decisions (advisory author context; Locked Task Specification above remains authoritative for Goal & Why, Bounds, and Done-check)"
            )
            primary_task_lines.extend(["", advisory_heading, advisory])
    elif branch_spec:
        filtered_branch_spec, primary_has_rebuttals = _filter_branch_spec_for_brief(
            branch_spec,
            cycle_base_spec,
            include_cycle_rebuttals=True,
        )
        if filtered_branch_spec:
            primary_task_lines.extend(["", f"### Branch Task Specification (`{task_file}`)", filtered_branch_spec])

    cf_ledger = (
        _build_carry_forward_ledger(
            project,
            head_sha,
            cycle_rounds,
            cycle_start,
            active_rebuttals,
            initial_state.get("arbitration"),
        )
        if cycle_rounds
        else None
    )
    if cf_ledger:
        primary_task_lines.extend(["", cf_ledger])
    if extra_ctx:
        primary_task_lines.extend(["", "### Additional Context", extra_ctx])

    blind_task_lines: list[str] | None = None
    if dual and (cycle_rounds or primary_has_rebuttals):
        blind_task_lines = list(base_task_lines)
        if locked_spec:
            blind_task_lines.extend(
                [
                    "",
                    f"### Locked Task Specification on `{base_ref}` (verify `Goal & Why`, `Bounds`, and `Done-check` were NOT weakened)",
                    locked_spec,
                ]
            )
            blind_advisory, _ = _build_advisory_task_sections(
                locked_spec,
                branch_spec,
                cycle_base_spec,
                include_cycle_rebuttals=False,
            )
            if blind_advisory:
                blind_task_lines.extend(
                    [
                        "",
                        "### Branch Progress & Decisions (advisory author context; Locked Task Specification above remains authoritative for Goal & Why, Bounds, and Done-check)",
                        blind_advisory,
                    ]
                )
        elif branch_spec:
            stripped_branch_spec, _ = _filter_branch_spec_for_brief(
                branch_spec,
                cycle_base_spec,
                include_cycle_rebuttals=False,
            )
            if stripped_branch_spec:
                blind_task_lines.extend(["", f"### Branch Task Specification (`{task_file}`)", stripped_branch_spec])
        blind_extra_ctx = strip_review_history_sections(extra_ctx) if extra_ctx else ""
        if blind_extra_ctx:
            blind_task_lines.extend(["", "### Additional Context", blind_extra_ctx])

    diagonal = {"agy": "codex", "codex": "agy"}
    primary_verifier = diagonal.get(last_write.get("executor")) or defaults["routing"]["verify"]

    def _task_for_verifier(name: str) -> str:
        if blind_task_lines is not None and name != primary_verifier:
            return "\n".join(blind_task_lines)
        return "\n".join(primary_task_lines)

    def review(name: str) -> dict[str, Any]:
        dest = safe_resolve(output_dir) if output_dir else None
        return _verify_one(
            name, project=project, branch=branch, head_sha=head_sha, slug=slug.replace("/", "-"),
            task=_task_for_verifier(name), why=why_text, level=level, model=model, effort=effort,
            defaults=defaults, caller_cwd=caller_cwd, output_dir=dest / name if dual and dest else dest,
        )

    if dual:
        with forward_signals_to_child_groups():
            with ThreadPoolExecutor(max_workers=2) as pool:
                runs = {run["executor"]: run for run in pool.map(review, ("codex", "agy"))}
            check_interrupted()
    else:
        name = executor or primary_verifier
        single_run = review(name)
        runs = {name: single_run}

    verdicts = {name: run["verdict"] for name, run in runs.items()}
    approved = all(verdict["verdict"] == "approve" for verdict in verdicts.values())
    outcome = "approve" if approved else "request_changes"
    with edit_gate_state(common_dir, branch) as state:
        live_rebase_refresh = check_round_limit(state)
        was_parked = bool(state.get("parked"))
        arb_dict = state.get("arbitration") if isinstance(state.get("arbitration"), dict) else None
        active_arb_unresolved = (
            arb_dict is not None
            and arb_dict.get("action") in ("park", "replan", "pivot")
            and isinstance(arb_dict.get("after_round"), int)
            and not isinstance(arb_dict.get("after_round"), bool)
            and arb_dict["after_round"] > int(state.get("arbiter_closed_round") or 0)
        )
        if force_round:
            state.pop("parked", None)
            state.pop("park_reason", None)
        state["rounds"].append({
            "head_sha": head_sha, "merge_base_sha": merge_base_sha, "task_file": task_file,
            "task_blob_sha": task_blob_sha, "dual": dual, "level": level,
            "implement_level": implement_level, "forced_round": bool(force_round),
            "rebase_refresh": bool(live_rebase_refresh),
            "outcome": outcome, "verdicts": verdicts,
            "recorded_at": _now_iso(),
        })
        if task_file:
            state["task_file"] = task_file
        if outcome == "approve" and (
            live_rebase_refresh
            or was_parked
            or (force_round and active_arb_unresolved)
            or bool(state.get("arbiter_rework_used"))
            or state.get("arbiter_rework_after_round") is not None
        ):
            state["arbiter_closed_round"] = max(int(state.get("arbiter_closed_round") or 0), len(state["rounds"]))
            state["arbiter_closed_head_sha"] = head_sha
            state["arbiter_rework_used"] = False
            state["arbiter_rework_after_round"] = None
        post_cap = _convergence_cap_status(
            state["rounds"],
            max_review_rounds=max_rounds,
            levels=configured_levels,
            verify_min_level=verify_min_level,
            arbiter_rework_after_round=state.get("arbiter_rework_after_round"),
            arbitration=state.get("arbitration") if isinstance(state.get("arbitration"), dict) else None,
            arbiter_closed_round=state.get("arbiter_closed_round"),
            project=project,
            common_dir=common_dir,
            expected_merge_base=merge_base_sha,
            current_head_sha=head_sha,
            task_file=task_file,
            expected_task_blob=task_blob_sha,
        )
        rejected = post_cap["total_request_changes"]

    result: dict[str, Any] = {
        "branch": branch, "slug": slug, "base_ref": base_ref, "head_sha": head_sha,
        "task_file": task_file, "level": level, "dual": dual, "forced_round": bool(force_round),
        "outcome": outcome, "request_changes_rounds": rejected,
        **({"verifiers": runs} if dual else single_run),
    }

    if auto_arbitrate and outcome == "request_changes" and (post_cap["cap_reached"] or had_rebuttal_before_round):
        arb_out_dir = str(safe_resolve(output_dir) / "arbiter") if output_dir else None
        arb_res = arbitrate_branch(
            project=project,
            branch_or_slug=f"refs/heads/{branch}",
            defaults=defaults,
            base=base_ref,
            executor=None,
            level=None,
            model=None,
            effort=None,
            task_file_arg=task_file_arg,
            why=why,
            context_file=context_file,
            output_dir=arb_out_dir,
            force_round=force_round,
            caller_cwd=caller_cwd,
        )
        if arb_res["branch"] != branch or arb_res["head_sha"] != head_sha:
            raise KeelError(
                f"Arbitration target mismatch: verified '{branch}' ({head_sha[:12]}), "
                f"but arbitrated '{arb_res['branch']}' ({str(arb_res.get('head_sha') or '')[:12]})."
            )
        result["arbitration"] = arb_res["arbitration"]
        if arb_res["action"] == "approve":
            result["outcome"] = "approve"
    return result


def _arbitrate_one(
    executor: str,
    *,
    project: Path,
    branch: str,
    head_sha: str,
    slug: str,
    task: str,
    why: str,
    level: str,
    model: str | None,
    effort: str | None,
    defaults: dict[str, Any],
    caller_cwd: Path | None,
    output_dir: Path | None,
) -> dict[str, Any]:
    adapter = get_executor(executor)
    target_arbiter_file = (output_dir / "arbiter.json") if output_dir is not None else None
    if target_arbiter_file is not None and target_arbiter_file.exists():
        raise KeelError(f"Arbiter output already exists: {target_arbiter_file}")
    run_id = generate_run_id(f"arbitrate-{slug}-{executor}")
    run_dir = runs_root_dir(project, Path(tempfile.gettempdir())) / run_id
    run_dir.mkdir(parents=True)
    scratch = Path(tempfile.mkdtemp(prefix=f"keel-verify-arbiter-{slug}-{executor}-"))
    try:
        checkout, git_dir = create_disposable_clone(project, head_sha, scratch)
        schema = SCHEMAS_DIR / "arbiter.schema.json"
        run_arbiter_file = run_dir / "arbiter.json"
        if run_arbiter_file.exists():
            raise KeelError(f"Arbiter output already exists: {run_arbiter_file}")
        req = RunRequest(
            run_id=run_id,
            run_dir=run_dir,
            brief_path=run_dir / "brief.md",
            project_root=project,
            workdir=checkout,
            mode="read",
            preset="arbitrate",
            level=level,
            model=model,
            effort=effort,
            schema_path=schema,
            output_path=run_arbiter_file,
            extra_git_dirs=(git_dir,),
            scratch_verify=True,
            why=why,
        )
        req.brief_path.write_text(
            assemble_brief(
                why=why,
                task=task,
                preset=req.preset,
                mode=req.mode,
                workdir=checkout,
                project_root=project,
                native_agent_docs=adapter.native_agent_docs,
                loads_plugin_spine=adapter.loads_plugin_spine,
                caller_cwd=caller_cwd,
                schema_path=schema,
                output_path=run_arbiter_file,
                inject_schema_in_brief=adapter.inject_schema_in_brief,
            ),
            encoding="utf-8",
        )
        record = {
            **req.run_record(executor),
            "branch": branch,
            "head_sha": head_sha,
            "ephemeral": True,
        }
        try:
            record.update(adapter.execute(req, defaults))
            if record.get("status") != "completed":
                raise KeelError(f"Arbiter {executor} did not complete.")
            decision = validate_arbiter_payload(json.loads(run_arbiter_file.read_text(encoding="utf-8")))
            if output_dir is not None and target_arbiter_file is not None:
                output_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(run_arbiter_file, target_arbiter_file)
                shutil.copy2(req.brief_path, output_dir / "brief.md")
                record["output_path"] = str(target_arbiter_file)
        except (OSError, ValueError, KeelError) as exc:
            if isinstance(exc, KeelExecutionError):
                record.update(exc.partial)
            save_run_record(run_dir, {**record, "status": "failed"})
            if isinstance(exc, KeelError):
                raise
            raise KeelError(f"Invalid arbiter output from {executor}: {exc}") from exc
        save_run_record(run_dir, record)
        return {
            "executor": executor,
            "run_id": run_id,
            "status": "completed",
            "model": record.get("model"),
            "level": level,
            "decision": decision,
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def arbitrate_branch(
    *,
    project: Path,
    branch_or_slug: str,
    defaults: dict[str, Any],
    base: str | None = None,
    executor: str | None = None,
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

    extra_ctx = trim_markdown_block(read_utf8_file(context_file, "--context-file")) if context_file else ""

    project = primary_checkout_root(project)
    ensure_agents_dir_ignored(project)
    common_dir, _ = git_dirs(project)
    branch, slug = resolve_branch_and_slug(project, branch_or_slug, must_exist=True)
    cli_target = _cli_branch_target(branch, slug)
    default_branches = tuple(defaults.get("default_branches", ["main", "master"]))
    base_ref = resolve_base_ref(project, base, default_branches=default_branches)
    base_flag = _cli_base_flag(project, base_ref, default_branches=default_branches)
    base_branch_name = base_ref[len("refs/heads/") :] if base_ref.startswith("refs/heads/") else base_ref
    if branch == base_branch_name or branch in default_branches:
        raise KeelError(f"Cannot arbitrate base or default branch '{branch}' against '{base_ref}'.")

    head_sha = run_cmd(["git", "rev-parse", f"refs/heads/{branch}"], cwd=project).stdout.strip().lower()
    base_tip_sha = _resolve_ref_tip_sha(project, base_ref)
    has_ahead_commits = head_sha != base_tip_sha
    ancestry_ok = (
        run_cmd(["git", "merge-base", "--is-ancestor", base_tip_sha, head_sha], cwd=project, check=False).returncode == 0
    )
    mb_proc = run_cmd(["git", "merge-base", base_tip_sha, head_sha], cwd=project, check=False)
    merge_base_sha = mb_proc.stdout.strip().lower() if mb_proc.returncode == 0 else ""
    has_tree_diff = (
        has_ahead_commits
        and run_cmd(["git", "diff", "--quiet", base_tip_sha, head_sha], cwd=project, check=False).returncode != 0
    )

    initial_state = load_branch_state(common_dir, branch)
    if initial_state.get("parked") and not force_round:
        raise KeelError(
            f"Branch '{branch}' is parked for human decision: {initial_state.get('park_reason') or 'awaiting human decision'}"
        )

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

    if not ancestry_ok:
        raise KeelError(
            f"Branch '{branch}' ({head_sha[:12]}) is behind '{base_ref}' tip ({base_tip_sha[:12]}); rebase and re-verify before arbitration."
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
    expected_task_blob = _effective_task_blob_sha(project, base_tip_sha, head_sha, task_file)

    initial_rounds = _review_rounds(initial_state)
    cycle_start = _active_cycle_start(
        initial_rounds,
        initial_state.get("arbitration") if isinstance(initial_state.get("arbitration"), dict) else None,
        arbiter_closed_round=initial_state.get("arbiter_closed_round"),
    )
    cycle_rounds = initial_rounds[cycle_start:]
    current_round = next((entry for entry in reversed(cycle_rounds) if entry.get("head_sha") == head_sha), {})
    if not current_round or not initial_rounds:
        prior_head_round = next((entry for entry in reversed(initial_rounds) if entry.get("head_sha") == head_sha), {})
        if prior_head_round.get("outcome") == "approve":
            raise KeelError(
                f"Commit {head_sha[:12]} on '{branch}' does not carry an active 'request_changes' verdict (latest outcome: 'approve')."
            )
        raise KeelError(
            f"Commit {head_sha[:12]} on '{branch}' has not been verified yet; run `keel branch verify {cli_target}{base_flag}` before arbitration."
        )
    pinned_round_count = len(initial_rounds)
    pinned_latest_round = initial_rounds[-1]
    pinned_tail_sha = pinned_latest_round.get("head_sha")
    if current_round.get("outcome") != "request_changes":
        raise KeelError(
            f"Commit {head_sha[:12]} on '{branch}' does not carry an active 'request_changes' verdict (latest outcome: {current_round.get('outcome')!r})."
        )
    if not _round_entry_matches_scope(
        current_round,
        expected_merge_base=merge_base_sha,
        task_file=task_file,
        expected_task_blob=expected_task_blob,
    ):
        raise KeelError(
            f"Latest 'request_changes' round on {head_sha[:12]} does not match target base '{base_ref}' ({merge_base_sha[:12]}) or locked task criteria ({task_file!r}); run fresh `keel branch verify {cli_target}{base_flag}` first."
        )

    existing_arb = initial_state.get("arbitration")
    if (
        not force_round
        and isinstance(existing_arb, dict)
        and existing_arb.get("after_round") == pinned_round_count
        and existing_arb.get("head_sha") == head_sha
        and _round_entry_matches_scope(
            existing_arb,
            expected_merge_base=merge_base_sha,
            task_file=task_file,
            expected_task_blob=expected_task_blob,
        )
    ):
        raise KeelError(
            f"Round {pinned_round_count} on '{branch}' ({head_sha[:12]}) was already arbitrated "
            f"(action: '{existing_arb.get('action')}'); execute that recovery step instead of re-arbitrating the same rejection round."
        )

    raw_cfg_levels = defaults.get("levels")
    if raw_cfg_levels is not None:
        if not isinstance(raw_cfg_levels, (list, tuple)) or not raw_cfg_levels:
            raise KeelConfigError("Expected a non-empty levels list in defaults.json.")
        configured_levels = [normalize_level(lvl) for lvl in raw_cfg_levels]
    else:
        configured_levels = list(VALID_LEVELS)
    verify_min_level = normalize_level(defaults.get("verify_min_level") or "L2")
    top_level, verify_tiers = _resolve_ladder(configured_levels, verify_min_level)
    default_manual_level = _manual_baseline_level(verify_tiers)
    min_arb_level = _ceil_in_tiers(
        max_level(top_level, _round_level(current_round, default_level=default_manual_level)),
        verify_tiers,
    )
    if level is None:
        eff_level = min_arb_level
    else:
        eff_level = normalize_level(level, defaults={"levels": configured_levels})
        if level_rank(eff_level) < level_rank(min_arb_level):
            raise KeelConfigError(
                f"Arbitration requires --level {min_arb_level} or higher, got {eff_level}."
            )

    locked_spec: str | None = None
    if task_file:
        locked_spec = _read_blob_text(project, base_tip_sha, task_file)
        if locked_spec is None and task_file.startswith("docs/tasks/active/"):
            locked_spec = _read_blob_text(project, base_tip_sha, "docs/tasks/todo/" + Path(task_file).name)
    branch_spec = _read_blob_text(project, head_sha, task_file) if task_file else None

    max_rounds = int(defaults.get("max_review_rounds", 2))
    cap = _convergence_cap_status(
        initial_rounds,
        max_review_rounds=max_rounds,
        levels=configured_levels,
        verify_min_level=verify_min_level,
        arbiter_rework_after_round=initial_state.get("arbiter_rework_after_round"),
        arbitration=initial_state.get("arbitration") if isinstance(initial_state.get("arbitration"), dict) else None,
        arbiter_closed_round=initial_state.get("arbiter_closed_round"),
        project=project,
        common_dir=common_dir,
        expected_merge_base=merge_base_sha,
        current_head_sha=head_sha,
        task_file=task_file,
        expected_task_blob=expected_task_blob,
    )
    active_rebuttals = _active_cycle_rebuttals(initial_state, cycle_start)
    cycle_base_spec = _cycle_baseline_spec(
        project,
        initial_rounds,
        initial_state.get("arbitration") if isinstance(initial_state.get("arbitration"), dict) else None,
        cycle_start,
        task_file,
        locked_spec,
        arbiter_closed_round=initial_state.get("arbiter_closed_round"),
        arbiter_closed_head_sha=initial_state.get("arbiter_closed_head_sha"),
    )
    has_rebuttal = bool(active_rebuttals) or _has_branch_rebuttal_section(cycle_base_spec, branch_spec)
    if not cap["cap_reached"] and not cap.get("raw_cap_reached") and not has_rebuttal:
        raise KeelError(
            f"Branch '{branch}' is not eligible for arbitration yet: neither convergence cap nor an author rebuttal/dispute is recorded in the active cycle."
        )

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
        prev_checks = (load_branch_state(common_dir, branch).get("checks_by_sha") or {}).get(head_sha) or {}
        failed_prev = [c["command"] for c in (prev_checks.get("commands") or []) if not c.get("ok")]
        if failed_prev:
            raise KeelError(
                f"--skip-checks cannot clear failed deterministic checks on commit {head_sha[:12]} "
                f"({', '.join(failed_prev)}); re-run passing --check command(s) first."
            )
        _record_skipped_checks(common_dir, branch, head_sha, skip_checks)

    latest_checks = (load_branch_state(common_dir, branch).get("checks_by_sha") or {}).get(head_sha) or {}
    if latest_checks.get("configured") and not latest_checks.get("ok"):
        outstanding = [c["command"] for c in latest_checks.get("commands", []) if not c["ok"]]
        raise KeelError(f"Deterministic checks failed on commit {head_sha[:12]}: {', '.join(outstanding)}")

    last_write = initial_state.get("last_write") or {}
    diagonal = {"agy": "codex", "codex": "agy"}
    if executor is not None:
        arb_executor = executor
    elif last_write.get("executor") in diagonal:
        arb_executor = diagonal[last_write["executor"]]
    elif len(current_round.get("verdicts") or {}) == 1:
        sole_verifier = next(iter(current_round["verdicts"]))
        arb_executor = diagonal.get(sole_verifier) or defaults["routing"]["verify"]
    else:
        arb_executor = defaults["routing"]["verify"]

    commit_log = run_cmd(["git", "log", "--oneline", f"{merge_base_sha}..{head_sha}"], cwd=project).stdout.strip()
    spec_why = extract_markdown_section(locked_spec or branch_spec or "", WHY_HEADINGS, levels=(2,))
    raw_recorded_why = last_write.get("why")
    if isinstance(raw_recorded_why, str) and raw_recorded_why.startswith("\ufeff"):
        raw_recorded_why = raw_recorded_why[1:]
    recorded_why = (trim_markdown_block(raw_recorded_why) or None) if isinstance(raw_recorded_why, str) else None
    raw_explicit_why = (why[1:] if why.startswith("\ufeff") else why) if isinstance(why, str) else None
    explicit_why = (trim_markdown_block(raw_explicit_why) or None) if raw_explicit_why is not None else None
    why_text = (
        explicit_why
        or spec_why
        or recorded_why
        or f"Adjudicate the verification deadlock on branch '{branch}' against the repository's Core need and locked task bounds."
    )

    max_recoveries = int(defaults.get("max_structural_recoveries", 2))
    branch_lineage = load_lineage_for_branch(common_dir, branch)
    lineage_rec_count = int((branch_lineage or {}).get("recovery_count") or 0)
    cur_recovery_count = max(int(initial_state.get("recovery_count") or 0), lineage_rec_count)
    rework_already_used = _is_arbiter_rework_consumed(initial_state, initial_rounds)

    arb_lines = [
        f"- **Branch:** `{branch}`",
        f"- **Target base:** `{base_ref}` (`{base_tip_sha}`)",
        f"- **Pinned head commit:** `{head_sha}`",
        f"- **Merge-base commit:** `{merge_base_sha}` (inspect full diff via `git diff {merge_base_sha}..{head_sha}`)",
        f"- **Structural recoveries used:** `{cur_recovery_count}/{max_recoveries}`",
        f"- **Single-branch `rework_once` already used:** `{rework_already_used}`",
    ]
    if not has_tree_diff:
        diff_note = (
            f"Empty (no commits ahead of `{base_ref}`; `head_sha == base_tip_sha`—cannot land as-is)."
            if not has_ahead_commits
            else f"Empty (`git diff {base_tip_sha[:12]}..{head_sha[:12]}` is empty; e.g. reverted commit—cannot land as-is)."
        )
        arb_lines.append(f"- **Net tree diff vs `{base_ref}`:** {diff_note}")
    if task_file:
        arb_lines.append(f"- **Task ledger file:** `{task_file}`")
    if commit_log:
        arb_lines.extend(["", "### Commits Under Review", f"```\n{commit_log}\n```"])
    if latest_checks.get("configured"):
        arb_lines.append("")
        arb_lines.append("### Deterministic Checks on Head Commit")
        if latest_checks.get("skipped"):
            arb_lines.append(f"- Skipped (`{latest_checks.get('skip_reason')}`)")
        for c in latest_checks.get("commands", []):
            arb_lines.append(f"- `{'PASS' if c.get('ok') else 'FAIL'}` `{c.get('command')}`")
    if locked_spec:
        arb_lines.extend(
            [
                "",
                f"### Locked Task Specification on `{base_ref}`",
                locked_spec,
            ]
        )
        advisory, arb_has_rebuttals = _build_advisory_task_sections(
            locked_spec,
            branch_spec,
            cycle_base_spec,
            include_cycle_rebuttals=True,
        )
        if advisory:
            arb_advisory_heading = (
                "### Branch Progress, Decisions & Rebuttals (advisory author context)"
                if arb_has_rebuttals
                else "### Branch Progress & Decisions (advisory author context)"
            )
            arb_lines.extend(["", arb_advisory_heading, advisory])
    elif branch_spec:
        filtered_branch_spec, _ = _filter_branch_spec_for_brief(
            branch_spec,
            cycle_base_spec,
            include_cycle_rebuttals=True,
        )
        if filtered_branch_spec:
            arb_lines.extend(["", f"### Branch Task Specification (`{task_file}`)", filtered_branch_spec])

    arb_lines.extend(["", "### Active-Cycle Verification & Dispute History"])
    for offset, rnd in enumerate(cycle_rounds, start=cycle_start + 1):
        arb_lines.append(
            f"- **Round {offset}** (`{rnd.get('level') or 'unknown'}` on `{str(rnd.get('head_sha') or '')[:12]}` -> `{rnd.get('outcome')}`):"
        )
        for v_exec, v_payload in (rnd.get("verdicts") or {}).items():
            if isinstance(v_payload, dict):
                arb_lines.append(f"  - Verifier `{v_exec}` (`{v_payload.get('verdict')}`): {v_payload.get('summary')}")
                for blk in v_payload.get("blocking") or []:
                    arb_lines.append(f"    - `blocking`: {blk}")
                for nt in v_payload.get("notes") or []:
                    arb_lines.append(f"    - `note`: {nt}")
    if active_rebuttals:
        arb_lines.append("- **Structured Author Rebuttals (`keel branch dispute`):**")
        for reb in active_rebuttals:
            arb_lines.append(
                f"  - After Round {reb['round']} (commit `{str(reb.get('head_sha') or '')[:12]}`): {reb['reason']}"
            )

    prior_dead_ends = dedup_strings(initial_state.get("avoid_dead_ends") or [])
    if prior_dead_ends:
        arb_lines.extend(["", "### Prior Attempt Dead Ends Already Recorded"])
        for de in prior_dead_ends:
            arb_lines.append(f"- {de}")
    if extra_ctx:
        arb_lines.extend(["", "### Additional Context", extra_ctx])

    dest_dir = safe_resolve(output_dir) if output_dir else None
    arb_run = _arbitrate_one(
        arb_executor,
        project=project,
        branch=branch,
        head_sha=head_sha,
        slug=slug.replace("/", "-"),
        task="\n".join(arb_lines),
        why=why_text,
        level=eff_level,
        model=model,
        effort=effort,
        defaults=defaults,
        caller_cwd=caller_cwd,
        output_dir=dest_dir,
    )

    raw_decision = arb_run["decision"]
    with edit_gate_state(common_dir, branch) as state:
        state["branch"] = branch
        if state.get("parked") and not force_round:
            raise KeelError(
                f"Branch '{branch}' is parked for human decision: {state.get('park_reason') or 'awaiting human decision'}"
            )
        live_head_sha = run_cmd(["git", "rev-parse", f"refs/heads/{branch}"], cwd=project).stdout.strip().lower()
        if live_head_sha != head_sha:
            raise KeelError(
                f"Branch '{branch}' tip moved concurrently during arbitration "
                f"({head_sha[:12]} -> {live_head_sha[:12]}); aborting without overwriting state."
            )
        live_rounds = state["rounds"]
        if (
            len(live_rounds) != pinned_round_count
            or not live_rounds
            or live_rounds[-1].get("head_sha") != pinned_tail_sha
            or live_rounds[-1].get("recorded_at") != pinned_latest_round.get("recorded_at")
        ):
            raise KeelError(
                f"Branch '{branch}' verification rounds changed concurrently during arbitration "
                f"(expected {pinned_round_count} round(s) ending on {str(pinned_tail_sha or '')[:12]}, got {len(live_rounds)}); "
                "aborting without overwriting state."
            )

        live_arb = state.get("arbitration")
        if (
            not force_round
            and isinstance(live_arb, dict)
            and live_arb.get("after_round") == pinned_round_count
            and live_arb.get("head_sha") == head_sha
            and _round_entry_matches_scope(
                live_arb,
                expected_merge_base=merge_base_sha,
                task_file=task_file,
                expected_task_blob=expected_task_blob,
            )
        ):
            raise KeelError(
                f"Round {pinned_round_count} on '{branch}' ({head_sha[:12]}) was already arbitrated "
                f"(action: '{live_arb.get('action')}'); execute that recovery step instead of re-arbitrating the same rejection round."
            )

        raw_action = raw_decision["action"]
        eff_action = raw_action
        clamped_from: str | None = None
        summary = raw_decision["summary"]
        directives = list(raw_decision["directives"])
        dead_ends = list(raw_decision["avoid_dead_ends"])
        waived_findings = list(raw_decision["waived_findings"])
        commands_run = list(raw_decision["commands_run"])

        rec_count = max(int(state.get("recovery_count") or 0), lineage_rec_count)
        if rec_count > int(state.get("recovery_count") or 0):
            state["recovery_count"] = rec_count
        if not has_tree_diff and eff_action == "approve":
            clamped_from = "approve"
            if rec_count < max_recoveries:
                eff_action = "pivot"
                clamp_msg = (
                    f"Clamped 'approve' to 'pivot': branch '{branch}' ({head_sha[:12]}) has an empty net diff "
                    f"against '{base_ref}' and cannot land."
                )
            else:
                eff_action = "park"
                clamp_msg = (
                    f"Clamped 'approve' to 'park': branch '{branch}' ({head_sha[:12]}) has an empty net diff "
                    f"against '{base_ref}' and structural recovery fuse ({rec_count}/{max_recoveries}) is exhausted."
                )
            summary = f"{summary} [{clamp_msg}]"
            directives.append(clamp_msg)

        if eff_action == "rework_once" and _is_arbiter_rework_consumed(state, live_rounds):
            clamped_from = "rework_once"
            if rec_count < max_recoveries:
                eff_action = "pivot"
                clamp_msg = "Clamped repeated 'rework_once' to 'pivot': single-branch arbiter rework allowance was already used."
            else:
                eff_action = "park"
                clamp_msg = (
                    f"Clamped repeated 'rework_once' to 'park': single-branch arbiter rework allowance was already used "
                    f"and structural recovery fuse ({rec_count}/{max_recoveries}) is exhausted."
                )
            summary = f"{summary} [{clamp_msg}]"
            directives.append(clamp_msg)

        if eff_action in ("replan", "pivot") and rec_count >= max_recoveries:
            if clamped_from is None:
                clamped_from = raw_action
            eff_action = "park"
            fuse_msg = (
                f"Clamped '{raw_action}' to 'park': structural recovery fuse reached "
                f"({rec_count}/{max_recoveries} recoveries used)."
            )
            summary = f"{summary} [{fuse_msg}]"
            directives.append(fuse_msg)

        arb_record: dict[str, Any] = {
            "run_id": arb_run["run_id"],
            "executor": arb_executor,
            "model": arb_run.get("model"),
            "level": eff_level,
            "head_sha": head_sha,
            "merge_base_sha": merge_base_sha,
            "task_file": task_file,
            "task_blob_sha": expected_task_blob,
            "after_round": pinned_round_count,
            "action": eff_action,
            "clamped_from": clamped_from,
            "summary": summary,
            "directives": directives,
            "avoid_dead_ends": dead_ends,
            "waived_findings": waived_findings,
            "commands_run": commands_run,
            "recorded_at": _now_iso(),
        }
        state["arbitration"] = arb_record
        if task_file:
            state["task_file"] = task_file

        if eff_action != "park" and force_round:
            state.pop("parked", None)
            state.pop("park_reason", None)

        if eff_action == "approve":
            state["arbiter_closed_round"] = max(int(state.get("arbiter_closed_round") or 0), pinned_round_count)
            state["arbiter_closed_head_sha"] = head_sha
            state["arbiter_rework_used"] = False
            state["arbiter_rework_after_round"] = None
        elif eff_action == "rework_once":
            state["arbiter_rework_used"] = True
            state["arbiter_rework_after_round"] = pinned_round_count
            state["prior_directives"] = list(directives)
            if dead_ends:
                state["avoid_dead_ends"] = dedup_strings([*(state.get("avoid_dead_ends") or []), *dead_ends])
        elif eff_action in ("replan", "pivot"):
            state["prior_directives"] = list(directives)
            state["avoid_dead_ends"] = dedup_strings([*(state.get("avoid_dead_ends") or []), *dead_ends])
        elif eff_action == "park":
            state["parked"] = True
            state["park_reason"] = summary
            state["prior_directives"] = list(directives)
            if dead_ends:
                state["avoid_dead_ends"] = dedup_strings([*(state.get("avoid_dead_ends") or []), *dead_ends])

    return {
        "branch": branch,
        "slug": slug,
        "base_ref": base_ref,
        "head_sha": head_sha,
        "task_file": task_file,
        "executor": arb_executor,
        "run_id": arb_run["run_id"],
        "model": arb_run.get("model"),
        "level": eff_level,
        "action": eff_action,
        "clamped_from": clamped_from,
        "arbitration": arb_record,
    }


def supersede_branch(
    *,
    project: Path,
    branch_or_slug: str,
    defaults: dict[str, Any],
    base: str | None = None,
    reason: str | None = None,
    archive_task: bool = False,
    keep_branch: bool = False,
    task_file_arg: str | None = None,
    caller_cwd: Path | None = None,
) -> dict[str, Any]:
    project = primary_checkout_root(project)
    common_dir, _ = git_dirs(project)
    branch, slug = resolve_branch_and_slug(project, branch_or_slug, must_exist=True)
    cli_target = _cli_branch_target(branch, slug)
    default_branches = tuple(defaults.get("default_branches", ["main", "master"]))
    base_ref = resolve_base_ref(project, base, default_branches=default_branches)
    base_flag = _cli_base_flag(project, base_ref, default_branches=default_branches)
    base_branch_name = base_ref[len("refs/heads/") :] if base_ref.startswith("refs/heads/") else base_ref
    if branch == base_branch_name or branch in default_branches:
        raise KeelError(f"Cannot supersede base or default branch '{branch}' against '{base_ref}'.")
    pinned_head_sha = run_cmd(["git", "rev-parse", f"refs/heads/{branch}"], cwd=project).stdout.strip().lower()

    active_cwd = safe_resolve(caller_cwd or Path.cwd())
    pre_wt = _find_branch_worktree(project, branch)
    if pre_wt is not None and pre_wt.exists() and is_subpath(active_cwd, pre_wt):
        raise KeelError(f"Cannot supersede worktree {pre_wt} while current directory ({active_cwd}) is inside it.")

    with locked_repo_land(common_dir):
        live_head = run_cmd(["git", "rev-parse", f"refs/heads/{branch}"], cwd=project).stdout.strip().lower()
        if live_head != pinned_head_sha:
            raise KeelError(
                f"Branch '{branch}' moved during supersede ({pinned_head_sha[:12]} -> {live_head[:12]}); aborting."
            )
        primary_branch = run_cmd(["git", "branch", "--show-current"], cwd=project).stdout.strip()
        if primary_branch == branch:
            raise KeelError(
                f"Cannot supersede branch '{branch}' while it is checked out in primary checkout {project}; "
                f"switch {project} to '{base_ref}' or a default branch first."
            )
        primary_porcelain = run_cmd(["git", "status", "--porcelain", "-uall"], cwd=project).stdout.strip()
        if primary_porcelain:
            raise KeelError(f"Primary checkout {project} has uncommitted or untracked changes:\n{primary_porcelain}")

        is_default_base = base_branch_name in default_branches
        target_checkout: Path | None = None

        def _validate_target_checkout(expected_tip_sha: str) -> Path:
            checkout = project if primary_branch == base_branch_name else _find_branch_worktree(project, base_branch_name)
            if checkout is None or not checkout.is_dir():
                raise KeelError(
                    f"Base branch '{base_ref}' must be checked out in the primary checkout or a linked worktree "
                    "before superseding."
                )
            checkout_branch = run_cmd(["git", "branch", "--show-current"], cwd=checkout).stdout.strip()
            if checkout_branch != base_branch_name:
                raise KeelError(
                    f"Checkout {checkout} must be on '{base_ref}' (currently '{checkout_branch or 'detached'}')."
                )
            checkout_porcelain = run_cmd(["git", "status", "--porcelain", "-uall"], cwd=checkout).stdout.strip()
            if checkout_porcelain:
                raise KeelError(f"Checkout {checkout} has uncommitted or untracked changes:\n{checkout_porcelain}")
            checkout_head = run_cmd(["git", "rev-parse", "HEAD"], cwd=checkout).stdout.strip().lower()
            if checkout_head != expected_tip_sha:
                raise KeelError(
                    f"Checkout {checkout} HEAD ({checkout_head[:12]}) differs from '{base_ref}' tip ({expected_tip_sha[:12]})."
                )
            return checkout

        pinned_base_tip_sha = _resolve_ref_tip_sha(project, base_ref)
        if is_default_base:
            if primary_branch != base_branch_name:
                raise KeelError(
                    f"Primary checkout {project} must be on '{base_ref}' (currently '{primary_branch or 'detached'}')."
                )
            target_checkout = project
        else:
            if primary_branch != base_branch_name and primary_branch not in default_branches:
                raise KeelError(
                    f"Primary checkout {project} must be on '{base_ref}' or a default branch "
                    f"({', '.join(default_branches)}), currently '{primary_branch or 'detached'}'."
                )
            target_checkout = _validate_target_checkout(pinned_base_tip_sha)

        linked_wt = _find_branch_worktree(project, branch)
        docs_to_unlink: list[Path] = []
        if linked_wt is not None and linked_wt.exists():
            lock_reason = _worktree_lock_reason(project, linked_wt)
            if lock_reason is not None:
                raise KeelError(
                    f"Worktree {linked_wt} is locked ({lock_reason}); unlock it via `git worktree unlock` before superseding."
                )
        if linked_wt is not None and linked_wt.is_dir():
            if is_subpath(active_cwd, linked_wt):
                raise KeelError(
                    f"Cannot supersede worktree {linked_wt} while current directory ({active_cwd}) is inside it."
                )
            actual_branch = run_cmd(["git", "branch", "--show-current"], cwd=linked_wt).stdout.strip()
            if actual_branch != branch:
                raise KeelError(
                    f"Worktree {linked_wt} is on branch '{actual_branch}' rather than '{branch}'."
                )
            wt_porcelain = run_cmd(["git", "status", "--porcelain", "-uall"], cwd=linked_wt).stdout.strip()
            if wt_porcelain:
                raise KeelError(f"Worktree {linked_wt} has uncommitted or untracked changes:\n{wt_porcelain}")
            wt_head = run_cmd(["git", "rev-parse", "HEAD"], cwd=linked_wt).stdout.strip().lower()
            if wt_head != pinned_head_sha:
                raise KeelError(
                    f"Worktree {linked_wt} HEAD ({wt_head[:12]}) differs from '{branch}' tip ({pinned_head_sha[:12]})."
                )
            for doc_name in ROOT_AGENT_DOCS:
                wt_doc = linked_wt / doc_name
                src_doc = project / doc_name
                if wt_doc.exists() and not wt_doc.is_symlink():
                    tracked = bool(run_cmd(["git", "ls-files", "--", doc_name], cwd=linked_wt).stdout.strip())
                    if not tracked:
                        if src_doc.exists() and not src_doc.is_symlink() and wt_doc.read_bytes() == src_doc.read_bytes():
                            docs_to_unlink.append(wt_doc)
                        else:
                            raise KeelError(
                                f"Gitignored {wt_doc} differs from {src_doc}; reconcile it before superseding."
                            )

        initial_state = load_branch_state(common_dir, branch)
        rounds = _review_rounds(initial_state)
        arb = initial_state.get("arbitration")
        if (
            not isinstance(arb, dict)
            or arb.get("action") not in ("replan", "pivot")
            or arb.get("head_sha") != pinned_head_sha
            or arb.get("after_round") != len(rounds)
        ):
            raise KeelError(
                f"Branch '{branch}' does not carry an active Arbiter 'replan' or 'pivot' decision on commit "
                f"{pinned_head_sha[:12]}; run `keel branch arbitrate {cli_target}{base_flag}` first or use human-authorized `keel branch land {cli_target}{base_flag} --discard --reason ...`."
            )

        arb_run_id = str(arb.get("run_id") or "")
        raw_arb_task = arb.get("task_file")
        arb_task_file = raw_arb_task if isinstance(raw_arb_task, str) and raw_arb_task else None
        pending_cleanup = initial_state.get("pending_supersede_cleanup")
        committed_retry = (
            isinstance(pending_cleanup, dict)
            and bool(arb_run_id)
            and pending_cleanup.get("run_id") == arb_run_id
            and pending_cleanup.get("head_sha") == pinned_head_sha
            and bool(pending_cleanup.get("supersede_commit_sha"))
            and run_cmd(
                [
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    str(pending_cleanup["supersede_commit_sha"]),
                    pinned_base_tip_sha,
                ],
                cwd=project,
                check=False,
            ).returncode
            == 0
        )

        if committed_retry:
            assert isinstance(pending_cleanup, dict)
            if task_file_arg is not None:
                explicit_rel = normalize_task_rel_path(project, task_file_arg)
                if explicit_rel != arb_task_file:
                    raise KeelError(
                        f"Arbiter '{arb['action']}' decision on '{branch}' was recorded for task "
                        f"{arb_task_file!r}, not --task-file {explicit_rel!r}; refusing to supersede."
                    )
            scope_base_tip = str(pending_cleanup.get("pre_commit_base_sha") or pinned_base_tip_sha)
            mb_proc = run_cmd(["git", "merge-base", scope_base_tip, pinned_head_sha], cwd=project, check=False)
            merge_base_sha = mb_proc.stdout.strip().lower() if mb_proc.returncode == 0 else ""
            expected_task_blob = _effective_task_blob_sha(project, scope_base_tip, pinned_head_sha, arb_task_file)
            if not _round_entry_matches_scope(
                arb,
                expected_merge_base=merge_base_sha,
                task_file=arb_task_file,
                expected_task_blob=expected_task_blob,
            ):
                raise KeelError(
                    f"Arbiter '{arb['action']}' decision on '{branch}' ({pinned_head_sha[:12]}) no longer matches "
                    f"recorded scope {arb_task_file!r}; refusing to supersede."
                )
        else:
            mb_proc = run_cmd(["git", "merge-base", pinned_base_tip_sha, pinned_head_sha], cwd=project, check=False)
            merge_base_sha = mb_proc.stdout.strip().lower() if mb_proc.returncode == 0 else ""
            task_file = discover_task_file(
                project,
                branch,
                slug,
                base_ref=base_ref,
                head_sha=pinned_head_sha,
                explicit_task_file=task_file_arg,
                state_task_file=initial_state.get("task_file"),
            )
            expected_task_blob = _effective_task_blob_sha(project, pinned_base_tip_sha, pinned_head_sha, task_file)
            if not _round_entry_matches_scope(
                arb,
                expected_merge_base=merge_base_sha,
                task_file=task_file,
                expected_task_blob=expected_task_blob,
            ):
                raise KeelError(
                    f"Arbiter '{arb['action']}' decision on '{branch}' ({pinned_head_sha[:12]}) was recorded for task "
                    f"{arb_task_file!r} (merge-base {str(arb.get('merge_base_sha') or '')[:12]}), which does not match "
                    f"current scope {task_file!r} on '{base_ref}' (merge-base {merge_base_sha[:12]}); refusing to supersede."
                )

        existing_lineage = load_lineage_for_branch(common_dir, branch) or {}
        can_archive_task = _is_archivable_task_path(arb_task_file, allow_todo=True)
        preloaded_branch_task = (
            _read_blob_text(project, pinned_head_sha, arb_task_file)
            if (archive_task and can_archive_task and arb_task_file and not committed_retry)
            else None
        )

        already_recorded = bool(arb_run_id and existing_lineage.get("last_arbitration_run_id") == arb_run_id)
        if already_recorded and existing_lineage.get("last_preserved_ref"):
            next_recovery_count = int(existing_lineage["recovery_count"])
            preserved_ref = str(existing_lineage["last_preserved_ref"])
            created_new_ref = False
        elif committed_retry and isinstance(pending_cleanup, dict) and pending_cleanup.get("preserved_ref"):
            prev_rec = max(
                int(initial_state.get("recovery_count") or 0),
                int(existing_lineage.get("recovery_count") or 0),
            )
            next_recovery_count = prev_rec + 1
            preserved_ref = str(pending_cleanup["preserved_ref"])
            created_new_ref = False
        else:
            prev_rec = max(
                int(initial_state.get("recovery_count") or 0),
                int(existing_lineage.get("recovery_count") or 0),
            )
            next_recovery_count = prev_rec + 1
            clean_ref_slug = re.sub(r"[^A-Za-z0-9._-]", "-", slug).strip("-") or "branch"
            utc_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            preserved_ref = f"refs/keel/superseded/{clean_ref_slug}/{utc_stamp}-{pinned_head_sha[:12]}"
            created_new_ref = True

        run_cmd(["git", "update-ref", preserved_ref, pinned_head_sha], cwd=project)
        verified_sha = run_cmd(["git", "rev-parse", "--verify", f"{preserved_ref}^{{commit}}"], cwd=project).stdout.strip().lower()
        if verified_sha != pinned_head_sha:
            raise KeelError(f"Failed to verify preserved ref '{preserved_ref}' -> {pinned_head_sha[:12]}.")

        merged_dead_ends = dedup_strings(
            [
                *(existing_lineage.get("avoid_dead_ends") or []),
                *(initial_state.get("avoid_dead_ends") or []),
                *(arb.get("avoid_dead_ends") or []),
            ]
        )
        directives = dedup_strings(arb.get("directives") or initial_state.get("prior_directives") or [])
        arb_summary = str(arb.get("summary") or "").strip()
        explicit_reason = (reason or "").strip()
        clean_reason = explicit_reason or arb_summary

        archived_task: str | None = None
        supersede_commit_sha: str | None = None
        if committed_retry:
            assert isinstance(pending_cleanup, dict)
            raw_arch = pending_cleanup.get("archived_task")
            raw_csha = pending_cleanup.get("supersede_commit_sha")
            archived_task = str(raw_arch) if isinstance(raw_arch, str) and raw_arch else None
            supersede_commit_sha = str(raw_csha) if isinstance(raw_csha, str) and raw_csha else None
        elif archive_task and can_archive_task and arb_task_file:
            assert target_checkout is not None
            task_name = Path(arb_task_file).name
            task_stem = Path(arb_task_file).stem
            archive_rel = f"docs/tasks/archive/{task_name}"
            archive_abs = target_checkout / archive_rel
            archive_tracked_at_base = _read_blob_sha(target_checkout, pinned_base_tip_sha, archive_rel) is not None
            archive_dir = archive_abs.parent
            archive_dir_existed = archive_dir.exists()
            try:
                src_task = _archivable_task_source(target_checkout, arb_task_file, allow_todo=True)
                source_text: str | None = None
                if src_task == arb_task_file:
                    source_text = (target_checkout / src_task).read_text(encoding="utf-8")
                elif preloaded_branch_task is not None:
                    source_text = preloaded_branch_task
                elif src_task is not None:
                    source_text = (target_checkout / src_task).read_text(encoding="utf-8")

                if source_text is not None:
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    if src_task is not None:
                        run_cmd(["git", "rm", "-f", "--", src_task], cwd=target_checkout)
                    action_upper = str(arb["action"]).upper()
                    outcome_lines = [
                        f"## Outcome: [SUPERSEDED BY ARBITER: {action_upper}]",
                        "",
                        f"- **Arbiter Run ID:** `{arb['run_id']}`",
                        f"- **Preserved Ref:** `{preserved_ref}` (`{pinned_head_sha}`)",
                        f"- **Recovery Count:** {next_recovery_count}",
                        f"- **Summary:** {arb_summary}",
                    ]
                    if explicit_reason:
                        outcome_lines.append(f"- **Reason:** {explicit_reason}")
                    if directives:
                        outcome_lines.append("- **Directives:**")
                        for item in directives:
                            outcome_lines.append(f"  - {item}")
                    if merged_dead_ends:
                        outcome_lines.append("- **Avoid Dead Ends:**")
                        for item in merged_dead_ends:
                            outcome_lines.append(f"  - {item}")
                    updated_text = source_text.rstrip() + "\n\n" + "\n".join(outcome_lines) + "\n"
                    archive_abs.write_text(updated_text, encoding="utf-8")
                    run_cmd(["git", "add", "--", archive_rel], cwd=target_checkout)
                    commit_msg = (
                        f"docs(tasks): supersede {task_stem} ({arb['action']})\n\n"
                        f"Keel-Arbitrated: {arb['action']} ({arb['run_id']})"
                    )
                    run_cmd(["git", "commit", "-m", commit_msg], cwd=target_checkout)
                    supersede_commit_sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=target_checkout).stdout.strip().lower()
                    archived_task = archive_rel
            except Exception:
                run_cmd(
                    ["git", "-c", "core.hooksPath=/dev/null", "reset", "--hard", pinned_base_tip_sha],
                    cwd=target_checkout,
                    check=False,
                )
                if not archive_tracked_at_base:
                    archive_abs.unlink(missing_ok=True)
                if not archive_dir_existed and archive_dir.is_dir():
                    try:
                        archive_dir.rmdir()
                    except OSError:
                        pass
                if created_new_ref:
                    run_cmd(["git", "update-ref", "-d", preserved_ref], cwd=project, check=False)
                raise

        history = list(existing_lineage.get("history") or [])
        if not already_recorded:
            history.append(
                {
                    "attempt": next_recovery_count,
                    "branch": branch,
                    "head_sha": pinned_head_sha,
                    "preserved_ref": preserved_ref,
                    "action": arb["action"],
                    "run_id": arb["run_id"],
                    "summary": arb["summary"],
                    "reason": clean_reason,
                    "superseded_at": _now_iso(),
                }
            )
        lineage_payload = {
            "slug": slug,
            "branch": branch,
            "task_file": arb_task_file,
            "archived_task": archived_task,
            "recovery_count": next_recovery_count,
            "last_arbitration_run_id": arb_run_id,
            "last_action": arb["action"],
            "last_preserved_ref": preserved_ref,
            "last_head_sha": pinned_head_sha,
            "last_summary": arb["summary"],
            "avoid_dead_ends": merged_dead_ends,
            "prior_directives": directives,
            "history": history,
            "updated_at": _now_iso(),
        }

        lineage_file: Path | None = None
        if supersede_commit_sha is not None:
            lineage_file = save_lineage_state(common_dir, branch, lineage_payload)
            if not committed_retry:
                with edit_gate_state(common_dir, branch) as state:
                    state["pending_supersede_cleanup"] = {
                        "run_id": arb_run_id,
                        "head_sha": pinned_head_sha,
                        "preserved_ref": preserved_ref,
                        "archived_task": archived_task,
                        "supersede_commit_sha": supersede_commit_sha,
                        "pre_commit_base_sha": pinned_base_tip_sha,
                    }

        for wt_doc in docs_to_unlink:
            wt_doc.unlink(missing_ok=True)

        try:
            teardown = _remove_worktree_and_branch_after_land(
                project,
                branch,
                base_ref=base_ref,
                pinned_head_sha=pinned_head_sha,
                keep_branch=keep_branch,
                discard=True,
            )
        except Exception:
            if supersede_commit_sha is None and created_new_ref:
                run_cmd(["git", "update-ref", "-d", preserved_ref], cwd=project, check=False)
            raise

        if lineage_file is None:
            lineage_file = save_lineage_state(common_dir, branch, lineage_payload)

        with edit_gate_state(common_dir, branch) as state:
            prev_landed_history = state.get("landed_history")
            state.clear()
            state["branch"] = branch
            if isinstance(prev_landed_history, list) and prev_landed_history:
                state["landed_history"] = prev_landed_history
            if not archive_task and arb_task_file and not archived_task:
                state["task_file"] = arb_task_file
            state["rounds"] = []
            state["checks_by_sha"] = {}
            state["last_write"] = None
            state["recovery_count"] = next_recovery_count
            state["avoid_dead_ends"] = merged_dead_ends
            state["prior_directives"] = directives
            state["superseded_from"] = {
                "slug": slug,
                "branch": branch,
                "head_sha": pinned_head_sha,
                "preserved_ref": preserved_ref,
                "action": arb["action"],
            }

        return {
            "branch": branch,
            "slug": slug,
            "base_ref": base_ref,
            "superseded": True,
            "action": arb["action"],
            "head_sha": pinned_head_sha,
            "preserved_ref": preserved_ref,
            "summary": arb_summary,
            "reason": clean_reason,
            "recovery_count": next_recovery_count,
            "avoid_dead_ends": merged_dead_ends,
            "prior_directives": directives,
            "lineage_path": str(lineage_file),
            "archived_task": archived_task,
            "supersede_commit_sha": supersede_commit_sha,
            **teardown,
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
    cli_target = _cli_branch_target(branch, slug)
    base_ref = resolve_base_ref(project, base, default_branches=tuple(default_branches))
    base_flag = _cli_base_flag(project, base_ref, default_branches=default_branches)

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
    arbitration = state.get("arbitration") if isinstance(state.get("arbitration"), dict) else None
    parked = bool(state.get("parked"))
    expected_task_blob = _effective_task_blob_sha(project, base_tip_sha, head_sha, task_file)
    cap = _convergence_cap_status(
        rounds,
        max_review_rounds=max_review_rounds,
        levels=levels,
        verify_min_level=verify_min_level,
        arbiter_rework_after_round=state.get("arbiter_rework_after_round"),
        arbitration=arbitration,
        arbiter_closed_round=state.get("arbiter_closed_round"),
        project=project,
        common_dir=common_dir,
        expected_merge_base=merge_base_sha,
        current_head_sha=head_sha,
        task_file=task_file,
        expected_task_blob=expected_task_blob,
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

    def _round_matches_scope(entry: dict[str, Any], *, expected_merge_base: str) -> bool:
        return _round_entry_matches_scope(
            entry,
            expected_merge_base=expected_merge_base,
            task_file=task_file,
            expected_task_blob=expected_task_blob,
        )

    active_arb_for_head = (
        arbitration
        if arbitration is not None
        and arbitration.get("head_sha") == head_sha
        and arbitration.get("after_round") == len(rounds)
        else None
    )

    if parked:
        if human_override and human_override.strip():
            approval_source = f"human-override:{human_override.strip()}"
        else:
            park_reason = str(state.get("park_reason") or "awaiting human decision")
            blockers.append(f"Branch '{branch}' is parked for human decision: {park_reason}")
    elif current.get("outcome") == "approve":
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
                blockers.append(_convergence_cap_blocker(cap, target=cli_target, base_flag=base_flag))
    elif active_arb_for_head is not None and active_arb_for_head.get("action") == "approve":
        if _round_matches_scope(active_arb_for_head, expected_merge_base=merge_base_sha):
            approval_source = f"arbiter-approve:{head_sha[:12]}"
        elif human_override and human_override.strip():
            approval_source = f"human-override:{human_override.strip()}"
        else:
            blockers.append(
                f"Latest approval on commit {head_sha[:12]} does not match target merge-base "
                f"({merge_base_sha[:12] or 'none'}) and locked task criteria ({task_file!r}); run fresh verification."
            )
            if cap["cap_reached"]:
                blockers.append(_convergence_cap_blocker(cap, target=cli_target, base_flag=base_flag))
    elif current:
        if human_override and human_override.strip():
            approval_source = f"human-override:{human_override.strip()}"
        elif (
            active_arb_for_head is not None
            and active_arb_for_head.get("action") in ("replan", "pivot")
            and _round_matches_scope(active_arb_for_head, expected_merge_base=merge_base_sha)
        ):
            blockers.append(
                f"Arbiter directed '{active_arb_for_head['action']}' on branch '{branch}': "
                f"run `keel branch supersede {cli_target}{base_flag}` to retire this attempt safely and propagate dead-end lineage."
            )
        else:
            blockers.append(f"Commit {head_sha[:12]} carries an active '{current['outcome']}' verdict.")
            if cap["cap_reached"]:
                blockers.append(_convergence_cap_blocker(cap, target=cli_target, base_flag=base_flag))
    elif old_base and old_head:
        old_base_lc = old_base.strip().lower()
        old_head_lc = old_head.strip().lower()
        old_round = latest_for(old_head_lc)
        old_arb = (
            arbitration
            if arbitration is not None
            and arbitration.get("action") == "approve"
            and arbitration.get("head_sha") == old_head_lc
            and arbitration.get("after_round") == len(rounds)
            else None
        )
        approved_entry = old_round if old_round.get("outcome") == "approve" else old_arb
        if approved_entry is None:
            raise KeelError(f"--old-head {old_head_lc[:12]} does not carry complete 'approve' verification for '{branch}'.")
        rebase_proof = (
            precomputed_rebase_proof
            if precomputed_rebase_proof is not None
            else compare_rebase(project, old_base_lc, old_head_lc, base_tip_sha, head_sha)
        )
        if not _round_matches_scope(approved_entry, expected_merge_base=old_base_lc):
            blockers.append(
                f"Approval on {old_head_lc[:12]} does not match target base ({old_base_lc[:12]}) or "
                f"revised task criteria ({task_file!r}); run fresh verification."
            )
            if cap["cap_reached"]:
                blockers.append(_convergence_cap_blocker(cap, target=cli_target, base_flag=base_flag))
        elif not missing_rebase_checks:
            approval_source = f"pure-rebase-from:{old_head_lc[:12]}"
    elif human_override and human_override.strip():
        approval_source = f"human-override:{human_override.strip()}"
    else:
        if cap["cap_reached"]:
            blockers.append(_convergence_cap_blocker(cap, target=cli_target, base_flag=base_flag))
        else:
            blockers.append(f"No independent 'approve' verdict recorded for commit {head_sha[:12]}.")

    if require_clean_checkouts:
        base_branch_name = base_ref[len("refs/heads/") :] if base_ref.startswith("refs/heads/") else base_ref
        primary_branch = run_cmd(["git", "branch", "--show-current"], cwd=project).stdout.strip()
        if primary_branch != base_branch_name:
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
        "arbitration": arbitration,
        "parked": parked,
        "recovery_count": int(state.get("recovery_count") or 0),
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
                f"refs/heads/{bname}",
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


def _append_arbitration_record(archive_path: Path, arb: dict[str, Any]) -> None:
    lines = [
        "## Arbitration Record",
        "",
        f"- **Action:** `{arb['action']}`",
        f"- **Run ID:** `{arb['run_id']}`",
        f"- **Arbiter:** `{arb['executor']}` (`{arb['level']}`)",
        f"- **Reviewed Commit:** `{arb['head_sha']}`",
        f"- **Summary:** {arb['summary']}",
    ]
    waived = arb.get("waived_findings") or []
    if waived:
        lines.append("- **Waived Findings:**")
        for item in waived:
            lines.append(f"  - {item}")
    existing = archive_path.read_text(encoding="utf-8")
    archive_path.write_text(existing.rstrip() + "\n\n" + "\n".join(lines) + "\n", encoding="utf-8")


def _unlink_lineage_if_owned(common_dir: Path, branch: str, slug: str, state: dict[str, Any]) -> None:
    del slug, state
    unlink_lineage_for_branch(common_dir, branch)


def _reset_transient_gate_state(state: dict[str, Any]) -> None:
    state["rounds"] = []
    state["checks_by_sha"] = {}
    state["last_write"] = None
    for key in (
        "task_file",
        "rebuttals",
        "arbitration",
        "arbiter_rework_used",
        "arbiter_rework_after_round",
        "arbiter_closed_round",
        "arbiter_closed_head_sha",
        "pending_supersede_cleanup",
        "parked",
        "park_reason",
        "recovery_count",
        "avoid_dead_ends",
        "prior_directives",
        "superseded_from",
    ):
        state.pop(key, None)
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
    base_branch_name = base_ref[len("refs/heads/") :] if base_ref.startswith("refs/heads/") else base_ref
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
            if primary_branch != base_branch_name:
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
            if task_file and _is_archivable_task_path(task_file, allow_todo=True):
                task_name = Path(task_file).name
                task_stem = Path(task_file).stem
                archive_rel = f"docs/tasks/archive/{task_name}"
                archive_abs = project / archive_rel
                archive_tracked_at_base = _read_blob_sha(project, pinned_base_tip_sha, archive_rel) is not None
                archive_dir = archive_abs.parent
                archive_dir_existed = archive_dir.exists()
                try:
                    src_task = _archivable_task_source(project, task_file, allow_todo=True)
                    source_text: str | None = None
                    if src_task == task_file:
                        source_text = (project / src_task).read_text(encoding="utf-8")
                    else:
                        branch_task_text = _read_blob_text(project, pinned_head_sha, task_file)
                        if branch_task_text is not None:
                            source_text = branch_task_text
                        elif src_task is not None:
                            source_text = (project / src_task).read_text(encoding="utf-8")

                    if source_text is not None:
                        archive_dir.mkdir(parents=True, exist_ok=True)
                        if src_task is not None:
                            run_cmd(["git", "rm", "-f", "--", src_task], cwd=project)
                        updated_text = source_text.rstrip() + f"\n\n## Outcome: [DISCARDED]\n\n- **Reason:** {clean_reason}\n"
                        archive_abs.write_text(updated_text, encoding="utf-8")
                        run_cmd(["git", "add", "--", archive_rel], cwd=project)
                        run_cmd(["git", "commit", "-m", f"docs(tasks): discard {task_stem}"], cwd=project)
                        discard_commit_sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=project).stdout.strip().lower()
                        archived_task = archive_rel
                except Exception:
                    run_cmd(
                        ["git", "-c", "core.hooksPath=/dev/null", "reset", "--hard", pinned_base_tip_sha],
                        cwd=project,
                        check=False,
                    )
                    if not archive_tracked_at_base:
                        archive_abs.unlink(missing_ok=True)
                    if not archive_dir_existed and archive_dir.is_dir():
                        try:
                            archive_dir.rmdir()
                        except OSError:
                            pass
                    raise

            with edit_gate_state(common_dir, branch) as state:
                state["branch"] = branch
                state["discarded"] = True
                state["discard_reason"] = clean_reason
                state["landed_head_sha"] = None
                state["landed_sha"] = None
                state["landed_method"] = None
                if not keep_branch:
                    _unlink_lineage_if_owned(common_dir, branch, slug, state)
                    _reset_transient_gate_state(state)

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
        f"refs/heads/{branch}",
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

    archived_task = None

    with locked_repo_land(common_dir):
        with edit_gate_state(common_dir, branch) as state:
            _assert_pinned_refs(
                project, branch, base_ref, pinned_head_sha, pinned_base_tip_sha, require_clean_worktree=True
            )
            live_gate = evaluate_branch_gate(
                project,
                f"refs/heads/{branch}",
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
            app_src = str(gate.get("approval_source") or "")
            used_human_override = app_src.startswith("human-override:")
            override_reason = app_src[len("human-override:") :] if used_human_override else ""

            approved_arbitration: dict[str, Any] | None = None
            state_arb = state.get("arbitration")
            if isinstance(state_arb, dict) and state_arb.get("action") == "approve":
                if app_src.startswith("arbiter-approve:"):
                    approved_arbitration = state_arb
                elif app_src.startswith("pure-rebase-from:") and old_head:
                    old_head_lc = old_head.strip().lower()
                    old_direct = next((entry for entry in reversed(state["rounds"]) if entry["head_sha"] == old_head_lc), {})
                    if old_direct.get("outcome") != "approve" and state_arb.get("head_sha") == old_head_lc:
                        approved_arbitration = state_arb

            commit_trailers = list(trailers)
            if approved_arbitration is not None:
                arb_trailer = f"Keel-Arbitrated: approve ({approved_arbitration['run_id']})"
                if arb_trailer not in commit_trailers:
                    commit_trailers.append(arb_trailer)
            if used_human_override:
                ovr_trailer = f"Keel-Human-Override: {override_reason}"
                if ovr_trailer not in commit_trailers:
                    commit_trailers.append(ovr_trailer)

            primary_branch = run_cmd(["git", "branch", "--show-current"], cwd=project).stdout.strip()
            if primary_branch != base_branch_name:
                raise KeelError(
                    f"Primary checkout {project} must be on '{base_ref}' (currently '{primary_branch or 'detached'}')."
                )
            primary_porcelain = run_cmd(["git", "status", "--porcelain", "-uall"], cwd=project).stdout.strip()
            if primary_porcelain:
                raise KeelError(f"Primary checkout {project} has uncommitted or untracked changes:\n{primary_porcelain}")

            if strategy not in ("squash", "ff-only"):
                raise KeelError(f"Unsupported landing strategy '{strategy}'.")

            created_archive_rel: str | None = None
            created_archive_dir: Path | None = None
            try:
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
                    if commit_trailers:
                        body_parts.append("\n".join(commit_trailers))
                    full_msg = "\n\n".join([commit_title, *body_parts])

                    run_cmd(["git", "merge", "--squash", pinned_head_sha], cwd=project)
                    squash_src_task = _archivable_task_source(
                        project,
                        task_file,
                        allow_todo=approved_arbitration is not None,
                    )
                    if squash_src_task is not None:
                        archive_rel = f"docs/tasks/archive/{Path(squash_src_task).name}"
                        if _read_blob_sha(project, base_tip_sha, archive_rel) is None:
                            created_archive_rel = archive_rel
                        archive_parent = (project / archive_rel).parent
                        if not archive_parent.exists():
                            created_archive_dir = archive_parent
                        archive_parent.mkdir(parents=True, exist_ok=True)
                        run_cmd(["git", "mv", "-f", "--", squash_src_task, archive_rel], cwd=project)
                        if approved_arbitration is not None:
                            _append_arbitration_record(project / archive_rel, approved_arbitration)
                            run_cmd(["git", "add", "--", archive_rel], cwd=project)
                        archived_task = archive_rel

                    run_cmd(["git", "commit", "-m", full_msg], cwd=project)
                    landed_sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=project).stdout.strip().lower()
                else:
                    run_cmd(["git", "merge", "--ff-only", pinned_head_sha], cwd=project)
                    ff_src_task = _archivable_task_source(
                        project,
                        task_file,
                        allow_todo=(approved_arbitration is not None or used_human_override),
                    )

                    archive_trailers: list[str] = []
                    if approved_arbitration is not None:
                        archive_trailers.append(f"Keel-Arbitrated: approve ({approved_arbitration['run_id']})")
                    if used_human_override:
                        archive_trailers.append(f"Keel-Human-Override: {override_reason}")

                    if ff_src_task is not None:
                        archive_rel = f"docs/tasks/archive/{Path(ff_src_task).name}"
                        if _read_blob_sha(project, base_tip_sha, archive_rel) is None:
                            created_archive_rel = archive_rel
                        archive_parent = (project / archive_rel).parent
                        if not archive_parent.exists():
                            created_archive_dir = archive_parent
                        archive_parent.mkdir(parents=True, exist_ok=True)
                        run_cmd(["git", "mv", "-f", "--", ff_src_task, archive_rel], cwd=project)
                        if approved_arbitration is not None:
                            _append_arbitration_record(project / archive_rel, approved_arbitration)
                            run_cmd(["git", "add", "--", archive_rel], cwd=project)
                        archive_msg = f"docs(tasks): archive {Path(ff_src_task).stem}"
                        if archive_trailers:
                            archive_msg += "\n\n" + "\n".join(archive_trailers)
                        run_cmd(["git", "commit", "-m", archive_msg], cwd=project)
                        archived_task = archive_rel
                    elif archive_trailers:
                        head_msg = run_cmd(["git", "log", "-1", "--format=%B", pinned_head_sha], cwd=project).stdout
                        missing_trailers = [t for t in archive_trailers if t not in head_msg]
                        if missing_trailers:
                            audit_title = (
                                f"chore(gate): record arbitration for {branch}"
                                if approved_arbitration is not None
                                else f"chore(gate): record human override for {branch}"
                            )
                            run_cmd(
                                ["git", "commit", "--allow-empty", "-m", f"{audit_title}\n\n" + "\n".join(missing_trailers)],
                                cwd=project,
                            )
                    landed_sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=project).stdout.strip().lower()
            except Exception:
                run_cmd(
                    ["git", "-c", "core.hooksPath=/dev/null", "reset", "--hard", base_tip_sha],
                    cwd=project,
                    check=False,
                )
                if created_archive_rel is not None:
                    (project / created_archive_rel).unlink(missing_ok=True)
                if created_archive_dir is not None and created_archive_dir.is_dir():
                    try:
                        created_archive_dir.rmdir()
                    except OSError:
                        pass
                raise

            state["branch"] = branch
            state["discarded"] = False
            state["discard_reason"] = None
            state["landed_head_sha"] = pinned_head_sha
            state["landed_sha"] = landed_sha
            state["landed_method"] = strategy
            prior_head_shas = dedup_strings(
                [
                    r["head_sha"].strip().lower()
                    for r in (state.get("rounds") or [])
                    if isinstance(r, dict)
                    and isinstance(r.get("head_sha"), str)
                    and _HEX_SHA_RE.fullmatch(r["head_sha"].strip())
                ]
            )
            history_entry: dict[str, Any] = {
                "landed_head_sha": pinned_head_sha,
                "landed_sha": landed_sha,
                "landed_method": strategy,
            }
            if old_head and old_head.strip():
                history_entry["old_head_sha"] = old_head.strip().lower()
            if prior_head_shas:
                history_entry["prior_head_shas"] = prior_head_shas
            raw_history = state.get("landed_history")
            cleaned_history: list[dict[str, Any]] = []
            seen_history: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            if isinstance(raw_history, list):
                for item in raw_history:
                    if not isinstance(item, dict):
                        continue
                    lh = str(item.get("landed_head_sha") or "").strip().lower()
                    ls = str(item.get("landed_sha") or "").strip().lower()
                    if not (_HEX_SHA_RE.fullmatch(lh) and _HEX_SHA_RE.fullmatch(ls)):
                        continue
                    lm = str(item.get("landed_method") or "").strip()
                    oh = str(item.get("old_head_sha") or "").strip().lower()
                    if oh and not _HEX_SHA_RE.fullmatch(oh):
                        oh = ""
                    ph = _normalize_prior_head_shas(item.get("prior_head_shas"))
                    key = (lh, ls, lm, oh)
                    existing = seen_history.get(key)
                    if existing is not None:
                        if ph:
                            existing["prior_head_shas"] = dedup_strings(
                                [*(existing.get("prior_head_shas") or []), *ph]
                            )
                        continue
                    norm_item: dict[str, Any] = {"landed_head_sha": lh, "landed_sha": ls}
                    if lm:
                        norm_item["landed_method"] = lm
                    if oh:
                        norm_item["old_head_sha"] = oh
                    if ph:
                        norm_item["prior_head_shas"] = ph
                    seen_history[key] = norm_item
                    cleaned_history.append(norm_item)
            new_key = (
                history_entry["landed_head_sha"],
                history_entry["landed_sha"],
                history_entry["landed_method"],
                history_entry.get("old_head_sha", ""),
            )
            existing_new = seen_history.get(new_key)
            if existing_new is not None:
                if prior_head_shas:
                    existing_new["prior_head_shas"] = dedup_strings(
                        [*(existing_new.get("prior_head_shas") or []), *prior_head_shas]
                    )
            else:
                cleaned_history.append(history_entry)
            state["landed_history"] = cleaned_history
            if not keep_branch:
                _unlink_lineage_if_owned(common_dir, branch, slug, state)
                _reset_transient_gate_state(state)

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
