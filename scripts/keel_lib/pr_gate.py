"""Stateless PR verification, skipped-aware CI evaluation, pure-rebase proof, and guarded squash merge."""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable

from . import KeelError
from .env_gh import gh_api, run_cmd, tool_env
from .worktree import git_dirs

ALLOWED_VERDICT_KEYS = frozenset({"verdict", "summary", "blocking", "notes", "commands_run"})
TRUSTED_AUTHOR_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
SAFE_MERGE_ATTRS = frozenset({"set", "unset", "unspecified", "text", "binary", "union"})

_HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$")
_REVIEW_HEADER_LINE_RE = re.compile(
    r"^#{1,4}\s*Keel Review\s*[:\-—]\s*(Approve|Request changes)\s*$",
    re.IGNORECASE,
)
_REVIEW_COMMIT_LINE_RE = re.compile(
    r"^Reviewed commit:\s*`?([0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?)`?(?:\s|$|·)",
    re.IGNORECASE,
)
_PROTECTION_UNAVAILABLE_RE = re.compile(
    r"(\bHTTP\s+404\b"
    r"|Branch not protected"
    r"|Upgrade to GitHub Pro or make this repository public"
    r"|Resource not accessible by integration)",
    re.IGNORECASE,
)


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


def format_review_comment(
    verdict_data: dict[str, Any],
    *,
    head_sha: str,
    executor: str,
    model: str | None = None,
) -> str:
    validated = validate_verdict_payload(verdict_data)
    label = "Approve" if validated["verdict"] == "approve" else "Request changes"
    exec_label = f"`{executor}` (`{model}`)" if model else f"`{executor}`"

    lines = [
        f"### Keel Review: {label}",
        f"Reviewed commit: `{head_sha}` · Verifier: {exec_label}",
        "",
        f"**Summary:** {validated['summary']}",
    ]
    if validated["blocking"]:
        lines.append("")
        lines.append("**Blocking (must fix before merge):**")
        for item in validated["blocking"]:
            lines.append(f"- {item}")
    if validated["notes"]:
        lines.append("")
        lines.append("**Notes (non-blocking):**")
        for item in validated["notes"]:
            lines.append(f"- {item}")
    if validated["commands_run"]:
        lines.append("")
        lines.append("**Verification run:**")
        for cmd in validated["commands_run"]:
            lines.append(f"- `{cmd}`")
    return "\n".join(lines) + "\n"


def _is_trusted_commenter(comment: dict[str, Any], repo_owner: str | None) -> bool:
    assoc = str(comment.get("author_association") or "").upper()
    if assoc in TRUSTED_AUTHOR_ASSOCIATIONS:
        return True
    user = comment.get("user")
    login = str(user.get("login") or "") if isinstance(user, dict) else ""
    if repo_owner and login and login.lower() == repo_owner.lower():
        return True
    return False


def parse_review_comments(
    comments: list[dict[str, Any]],
    *,
    repo_owner: str | None = None,
    trusted_Logins: set[str] | None = None,
) -> dict[str, Any]:
    """Extract clean, authenticated top-level Keel Review verdicts (rejecting code fences & untrusted actors)."""
    reviews: list[dict[str, Any]] = []
    extra_trusted = {x.lower() for x in (trusted_Logins or set())}

    for c in comments:
        body = c.get("body")
        if not isinstance(body, str):
            continue

        user = c.get("user")
        login = str(user.get("login") or "").lower() if isinstance(user, dict) else ""
        if not (_is_trusted_commenter(c, repo_owner) or (login and login in extra_trusted)):
            continue

        # Require top-level line 1 header + line 2 `Reviewed commit:` (never inside code fences or blockquotes)
        non_empty = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
        if len(non_empty) < 2:
            continue
        if non_empty[0].startswith(("```", "~~~", ">")):
            continue
        hm = _REVIEW_HEADER_LINE_RE.match(non_empty[0])
        cm = _REVIEW_COMMIT_LINE_RE.match(non_empty[1])
        if not hm or not cm:
            continue

        verdict = "approve" if hm.group(1).lower() == "approve" else "request_changes"
        commit_sha = cm.group(1).lower()
        needs_human = "[needs human decision]" in body.lower()
        reviews.append(
            {
                "comment_id": c.get("id"),
                "verdict": verdict,
                "commit_sha": commit_sha,
                "needs_human_decision": needs_human,
                "created_at": c.get("created_at"),
            }
        )

    latest_by_sha: dict[str, dict[str, Any]] = {}
    request_changes_rounds = 0
    for r in reviews:
        latest_by_sha[r["commit_sha"]] = r
        if r["verdict"] == "request_changes":
            request_changes_rounds += 1

    return {
        "reviews": reviews,
        "total_rounds": len(reviews),
        "request_changes_rounds": request_changes_rounds,
        "latest": reviews[-1] if reviews else None,
        "latest_by_sha": latest_by_sha,
    }


def _collect_paged_key(raw: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        items = raw.get(key, [])
        return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
    if isinstance(raw, list):
        out: list[dict[str, Any]] = []
        for entry in raw:
            if isinstance(entry, dict) and key in entry and isinstance(entry[key], list):
                out.extend(x for x in entry[key] if isinstance(x, dict))
            elif isinstance(entry, dict):
                out.append(entry)
        return out
    return []


def evaluate_ci_checks(
    repo: str,
    commit_sha: str,
    *,
    base_branch: str = "main",
    project_root: Path | None = None,
    api_fn: Callable[..., Any] = gh_api,
) -> dict[str, Any]:
    """Evaluate GitHub check-runs & commit statuses, treating non-required `skipped`/`neutral` as green."""
    raw_runs = api_fn(f"repos/{repo}/commits/{commit_sha}/check-runs", ("--paginate",))
    raw_status = api_fn(f"repos/{repo}/commits/{commit_sha}/statuses", ("--paginate",))

    check_runs = _collect_paged_key(raw_runs, "check_runs")
    statuses = raw_status if isinstance(raw_status, list) else _collect_paged_key(raw_status, "statuses")

    check_runs_sorted = sorted(check_runs, key=lambda r: int(r.get("id") or 0), reverse=True)
    latest_runs: dict[tuple[Any, str], dict[str, Any]] = {}
    for run in check_runs_sorted:
        name = str(run.get("name") or "")
        app_id = (run.get("app") or {}).get("id") if isinstance(run.get("app"), dict) else None
        key = (app_id, name)
        if name and key not in latest_runs:
            latest_runs[key] = run

    statuses_sorted = sorted(
        [s for s in statuses if isinstance(s, dict)],
        key=lambda s: int(s.get("id") or 0),
        reverse=True,
    )
    latest_statuses: dict[str, dict[str, Any]] = {}
    for st in statuses_sorted:
        ctx = str(st.get("context") or "")
        if ctx and ctx not in latest_statuses:
            latest_statuses[ctx] = st

    # Distinguish genuinely unconfigured branch protection (404 / Free-tier 403) from real 5xx/transport errors!
    # Preserve optional `app_id` pinning (`None` or `-1` means any source).
    required_specs: list[tuple[str, int | None]] = []
    try:
        prot = api_fn(f"repos/{repo}/branches/{base_branch}/protection/required_status_checks")
        if isinstance(prot, dict):
            checks_arr = prot.get("checks")
            if isinstance(checks_arr, list) and checks_arr:
                for chk in checks_arr:
                    if isinstance(chk, dict) and isinstance(chk.get("context"), str):
                        raw_app = chk.get("app_id")
                        app_pin = raw_app if isinstance(raw_app, int) and raw_app >= 0 else None
                        required_specs.append((chk["context"], app_pin))
            else:
                for c in prot.get("contexts") or []:
                    if isinstance(c, str):
                        required_specs.append((c, None))
    except KeelError as exc:
        if not _PROTECTION_UNAVAILABLE_RE.search(str(exc)):
            raise

    failing: list[str] = []
    pending: list[str] = []
    passing_names: set[str] = set()
    passing_typed: set[tuple[int | None, str]] = set()

    for (app_id, name), run in latest_runs.items():
        status = str(run.get("status") or "").lower()
        conclusion = str(run.get("conclusion") or "").lower()
        if status != "completed":
            pending.append(f"{name} ({status or 'pending'})")
            continue
        # GitHub branch protection accepts `success`, `skipped`, and `neutral` conclusions on check-runs
        if conclusion in ("success", "skipped", "neutral"):
            passing_names.add(name)
            norm_app = app_id if isinstance(app_id, int) else None
            passing_typed.add((norm_app, name))
        else:
            failing.append(f"{name} ({conclusion or 'unknown'})")

    for ctx, st in latest_statuses.items():
        state = str(st.get("state") or "").lower()
        if state == "success":
            passing_names.add(ctx)
            passing_typed.add((None, ctx))
        elif state == "pending":
            pending.append(f"{ctx} (pending)")
        else:
            failing.append(f"{ctx} ({state or 'unknown'})")

    missing_required: list[str] = []
    for req_ctx, req_app in required_specs:
        if req_app is None:
            if req_ctx not in passing_names:
                missing_required.append(req_ctx)
        else:
            if (req_app, req_ctx) not in passing_typed:
                missing_required.append(f"{req_ctx} (app_id={req_app})")
    missing_required.sort()
    total_observed = len(latest_runs) + len(latest_statuses)

    has_local_workflows = False
    if project_root is not None:
        wf_dir = project_root / ".github" / "workflows"
        if wf_dir.is_dir():
            has_local_workflows = any(wf_dir.glob("*.yml")) or any(wf_dir.glob("*.yaml"))

    silence_blocked = total_observed == 0 and (bool(required_specs) or has_local_workflows)
    ok = not failing and not pending and not missing_required and not silence_blocked

    return {
        "ok": ok,
        "total_observed": total_observed,
        "passing": sorted(passing_names),
        "pending": sorted(pending),
        "failing": sorted(failing),
        "missing_required": missing_required,
        "silence_blocked": silence_blocked,
    }


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
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "rerere.enabled=false", *args],
        cwd=repo_dir,
        env=_sanitized_git_env(),
        check=check,
    )
    return proc.stdout.strip()


def compare_rebase(
    project: Path,
    old_base: str,
    old_head: str,
    new_base: str,
    new_head: str,
) -> dict[str, Any]:
    """Mathematically prove `new_head` on `new_base` is a conflict-free pure rebase of `old_head` on `old_base`.

    Executes inside a clean empty-config repository pointing at `objects` via `alternates` so
    repository-local `.git/config` merge drivers or `.git/info/attributes` can never influence the proof.
    """
    for label, sha in (("old-base", old_base), ("old-head", old_head), ("new-base", new_base), ("new-head", new_head)):
        if not _HEX_SHA_RE.fullmatch(sha):
            raise KeelError(f"--{label} must be a full 40/64-character commit SHA (got '{sha}').")
        chk = run_cmd(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=project, check=False)
        if chk.returncode != 0:
            raise KeelError(f"Commit for --{label} ({sha}) is not present in {project}; fetch first.")

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

        touched = set(_sanitized_git(clean_repo, ["diff", "--name-only", old_base, old_head]).splitlines())
        touched.update(_sanitized_git(clean_repo, ["diff", "--name-only", old_base, new_base]).splitlines())
        if not _sanitized_git(clean_repo, ["diff", "--name-only", old_base, old_head]).splitlines():
            raise KeelError("Old range has an empty diff.")

        # Reject submodule gitlinks (mode 160000) and custom `.gitattributes` merge drivers in any rev
        lit_paths = [f":(literal){p}" for p in sorted(touched)]
        for rev in (old_base, old_head, new_base, new_head):
            ls_out = _sanitized_git(clean_repo, ["ls-tree", rev, "--", *lit_paths])
            if any(line.startswith("160000 ") for line in ls_out.splitlines()):
                raise KeelError("Pure-rebase proof does not admit submodule gitlink changes.")
            attr_out = _sanitized_git(
                clean_repo,
                ["check-attr", "--source", rev, "merge", "--", *sorted(touched)],
            )
            for line in attr_out.splitlines():
                # `<path>: merge: <value>`
                parts = line.rsplit(": merge: ", 1)
                if len(parts) == 2 and parts[1].strip() not in SAFE_MERGE_ATTRS:
                    raise KeelError(
                        f"Pure-rebase proof rejects custom .gitattributes merge driver '{parts[1].strip()}'."
                    )

        target_tree = _sanitized_git(clean_repo, ["rev-parse", f"{new_head}^{{tree}}"])

        # 1. Fast in-memory 3-way merge tree check in clean_repo (zero inherited .git/config drivers!)
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

        # 2. Multi-commit linear replay fallback inside clean_repo
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


def extract_trailers(commits: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    prefixes = ("co-authored-by:", "antigravity-session:", "codex-session:")
    for item in commits:
        msg = ((item.get("commit") or {}).get("message") or "") if isinstance(item, dict) else ""
        for raw_line in msg.splitlines():
            line = raw_line.strip()
            if line.lower().startswith(prefixes) and line not in seen:
                seen.append(line)
    return seen


def evaluate_and_merge_pr(
    *,
    repo: str,
    pr_number: int,
    project_root: Path,
    old_base: str | None = None,
    old_head: str | None = None,
    human_override: str | None = None,
    execute: bool = False,
    max_review_rounds: int = 2,
    api_fn: Callable[..., Any] = gh_api,
) -> dict[str, Any]:
    pr = api_fn(f"repos/{repo}/pulls/{pr_number}")
    if not isinstance(pr, dict):
        raise KeelError(f"Unexpected PR response for #{pr_number}.")

    merged = bool(pr.get("merged_at"))
    state = str(pr.get("state") or "").lower()
    if merged:
        raise KeelError(f"PR #{pr_number} is already merged.")
    if state != "open":
        raise KeelError(f"PR #{pr_number} is not open (state={state}).")
    if pr.get("draft"):
        raise KeelError(f"PR #{pr_number} is still a draft.")

    head_sha = str(((pr.get("head") or {}).get("sha") or "")).lower()
    base_ref = str(((pr.get("base") or {}).get("ref") or "main"))
    repo_owner = repo.split("/", 1)[0] if "/" in repo else None

    branch_info = api_fn(f"repos/{repo}/branches/{base_ref}")
    base_tip_sha = str((((branch_info or {}).get("commit") or {}).get("sha") or "")).lower()
    if not head_sha or not base_tip_sha:
        raise KeelError(f"Could not determine head/base SHAs for PR #{pr_number}.")

    remotes = run_cmd(["git", "remote"], cwd=project_root).stdout.splitlines()
    if "origin" in remotes:
        run_cmd(["git", "fetch", "--quiet", "--no-tags", "origin", base_ref, head_sha], cwd=project_root, check=False)

    # 1. True ancestry check
    anc = run_cmd(["git", "merge-base", "--is-ancestor", base_tip_sha, head_sha], cwd=project_root, check=False)
    ancestry_ok = anc.returncode == 0

    # 2. Skipped-aware CI check evaluation
    ci = evaluate_ci_checks(repo, head_sha, base_branch=base_ref, project_root=project_root, api_fn=api_fn)

    # 3. Authenticated review check across PR comments
    raw_comments = api_fn(f"repos/{repo}/issues/{pr_number}/comments", ("--paginate",))
    comments = raw_comments if isinstance(raw_comments, list) else []
    review_state = parse_review_comments(comments, repo_owner=repo_owner)
    latest_for_head = review_state["latest_by_sha"].get(head_sha)

    approval_source: str | None = None
    rebase_proof: dict[str, Any] | None = None

    # Current-head verdict ALWAYS takes precedence over older pre-rebase approvals!
    if latest_for_head is not None:
        if latest_for_head["verdict"] == "approve":
            approval_source = f"direct-approve:{head_sha[:12]}"
        elif human_override and human_override.strip():
            approval_source = f"human-override:{human_override.strip()}"
    elif old_base and old_head:
        old_verdict = review_state["latest_by_sha"].get(old_head.lower())
        if not old_verdict or old_verdict["verdict"] != "approve":
            raise KeelError(f"--old-head {old_head[:12]} does not carry an Approve review on PR #{pr_number}.")
        rebase_proof = compare_rebase(project_root, old_base.lower(), old_head.lower(), base_tip_sha, head_sha)
        approval_source = f"pure-rebase-from:{old_head[:12]}"
    elif human_override and human_override.strip():
        approval_source = f"human-override:{human_override.strip()}"

    blockers: list[str] = []
    if not ancestry_ok:
        blockers.append(
            f"PR head ({head_sha[:12]}) is behind '{base_ref}' tip ({base_tip_sha[:12]}); rebase onto latest '{base_ref}'."
        )
    if not ci["ok"]:
        if ci["failing"]:
            blockers.append(f"Failing CI checks: {', '.join(ci['failing'])}")
        if ci["pending"]:
            blockers.append(f"Pending CI checks: {', '.join(ci['pending'])}")
        if ci["missing_required"]:
            blockers.append(f"Missing required checks: {', '.join(ci['missing_required'])}")
        if ci["silence_blocked"]:
            blockers.append("Zero CI checks have reported on a repository with workflows.")
    if not approval_source:
        if latest_for_head is not None and latest_for_head["verdict"] == "request_changes":
            blockers.append(f"Commit {head_sha[:12]} carries an active 'Request changes' verdict.")
        elif review_state["request_changes_rounds"] >= max_review_rounds:
            blockers.append(
                f"Reached {review_state['request_changes_rounds']} 'Request changes' rounds (>= {max_review_rounds}-round cap): "
                "stop looping and escalate to the human."
            )
        else:
            blockers.append(f"No independent 'Approve' review found for commit {head_sha[:12]}.")

    ready = len(blockers) == 0
    result: dict[str, Any] = {
        "pr": pr_number,
        "repo": repo,
        "head_sha": head_sha,
        "base_ref": base_ref,
        "base_tip_sha": base_tip_sha,
        "ancestry_ok": ancestry_ok,
        "ci": ci,
        "approval_source": approval_source,
        "review_rounds": review_state["total_rounds"],
        "request_changes_rounds": review_state["request_changes_rounds"],
        "rebase_proof": rebase_proof,
        "ready_to_merge": ready,
        "blockers": blockers,
        "merged": False,
    }

    if not execute:
        return result

    if not ready:
        raise KeelError("PR is not ready to merge:\n- " + "\n- ".join(blockers))

    raw_commits = api_fn(f"repos/{repo}/pulls/{pr_number}/commits", ("--paginate",))
    commits = raw_commits if isinstance(raw_commits, list) else []
    trailers = extract_trailers(commits)
    if human_override and human_override.strip():
        trailers.append(f"Keel-Human-Override: {human_override.strip()}")

    title = f"{pr.get('title', '').strip()} (#{pr_number})"
    body_parts = [str(pr.get("body") or "").strip()]
    if trailers:
        body_parts.append("\n".join(trailers))
    commit_msg = "\n\n".join(p for p in body_parts if p)

    merge_resp = api_fn(
        f"repos/{repo}/pulls/{pr_number}/merge",
        (
            "-X",
            "PUT",
            "-f",
            "merge_method=squash",
            "-f",
            f"sha={head_sha}",
            "-f",
            f"commit_title={title}",
            "-f",
            f"commit_message={commit_msg}",
        ),
    )
    result["merged"] = True
    result["merge_sha"] = (merge_resp or {}).get("sha") if isinstance(merge_resp, dict) else None
    return result
