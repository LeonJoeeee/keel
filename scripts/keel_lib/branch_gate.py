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
from .executors.base import KeelConfigError, KeelExecutionError, escalate_level, level_rank, max_level, normalize_level
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


def _rework_floor(rounds: list[dict[str, Any]]) -> str | None:
    if not rounds or rounds[-1]["outcome"] != "request_changes":
        return None
    latest = rounds[-1]
    # Reviews recorded before the tier ladder use the manual-review baseline.
    return escalate_level(latest.get("implement_level") or latest.get("level") or "L3")


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
    rounds = state.get("rounds", [])
    floor = _rework_floor(rounds)
    last_write = state.get("last_write") or {}
    if floor and last_write.get("reworked_round") != len(rounds) and level_rank(level) < level_rank(floor):
        raise KeelConfigError(f"Rework on '{branch}' requires --level {floor} or higher.")
    return common_dir, branch


def record_branch_write(target: tuple[Path, str] | None, executor: str, result: dict[str, Any]) -> None:
    if target is None or result.get("status") != "completed":
        return
    level = result["level"]
    with edit_gate_state(*target) as state:
        write = {"executor": executor, "level": level, "recorded_at": _now_iso()}
        rounds = state["rounds"]
        floor = _rework_floor(rounds)
        if floor:
            previous = state.get("last_write") or {}
            if level_rank(level) >= level_rank(floor) or previous.get("reworked_round") == len(rounds):
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

    def check_round_limit(state: dict[str, Any]) -> None:
        rejected = _request_changes_count(state.get("rounds", []))
        if rejected >= max_rounds and not force_round:
            raise KeelError(f"Branch '{branch}' reached its {max_rounds}-round convergence cap. "
                            "Escalate to the human or pass --force-round if authorized.")

    check_round_limit(initial_state)
    last_write = initial_state.get("last_write") or {}
    implement_level = last_write.get("level")
    minimum = normalize_level(defaults["verify_min_level"])
    if implement_level:
        minimum = max_level(minimum, implement_level)
    if level is None:
        level = minimum if implement_level else "L3"
    level = normalize_level(level)
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
            "implement_level": implement_level, "outcome": outcome, "verdicts": verdicts,
            "recorded_at": _now_iso(),
        })
        if task_file:
            state["task_file"] = task_file
        rejected = _request_changes_count(state["rounds"])
    return {
        "branch": branch, "slug": slug, "base_ref": base_ref, "head_sha": head_sha,
        "task_file": task_file, "level": level, "dual": dual, "outcome": outcome,
        "request_changes_rounds": rejected, **({"verifiers": runs} if dual else single_run),
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
    rc_rounds = _request_changes_count(rounds)

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
    elif current:
        if human_override and human_override.strip():
            approval_source = f"human-override:{human_override.strip()}"
        else:
            blockers.append(f"Commit {head_sha[:12]} carries an active '{current['outcome']}' verdict.")
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
        elif not missing_rebase_checks:
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
