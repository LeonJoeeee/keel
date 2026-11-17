from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from keel_lib import KeelError  # noqa: E402
from keel_lib.branch_gate import compare_rebase, validate_verdict_payload  # noqa: E402
from keel_lib.executors.codex import CodexExecutor  # noqa: E402


def _load_cli_module() -> Any:
    loader = importlib.machinery.SourceFileLoader("keel_cli_test", str(ROOT / "scripts" / "keel"))
    spec = importlib.util.spec_from_loader("keel_cli_test", loader)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


KEEL_CLI = _load_cli_module()


def _run_cli(argv: list[str], *, agent_mode: str = "main") -> dict[str, Any]:
    buf = io.StringIO()
    err_buf = io.StringIO()
    with (
        mock.patch.dict(os.environ, {"KEEL_AGENT_MODE": agent_mode}),
        contextlib.redirect_stdout(buf),
        contextlib.redirect_stderr(err_buf),
    ):
        rc = KEEL_CLI.main(argv)
    if rc != 0:
        raise KeelError(err_buf.getvalue().strip())
    return json.loads(buf.getvalue())


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo_with_task(repo: Path, slug: str = "alpha") -> tuple[str, str]:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("## Core need\nExact arithmetic.\n", encoding="utf-8")
    todo_file = repo / "docs" / "tasks" / "todo" / "20261117-backlog.md"
    active_file = repo / "docs" / "tasks" / "active" / f"20261117-{slug}.md"
    todo_file.parent.mkdir(parents=True, exist_ok=True)
    active_file.parent.mkdir(parents=True, exist_ok=True)
    todo_file.write_text("# Backlog item\n\n## Goal & Why\nLater.\n", encoding="utf-8")
    active_file.write_text(
        f"# Feature {slug}\n\n## Goal & Why\nAdd mul() cleanly.\n\n## Bounds\ncalc.py only.\n\n## Done-check\npython3 -c 'import calc'\n\n## Progress\n- [ ] Implement\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init repo and baseline task")
    return _git(repo, "rev-parse", "HEAD"), f"docs/tasks/active/20261117-{slug}.md"


class BranchGateTests(unittest.TestCase):
    def test_verdict_schema_strictness(self) -> None:
        with self.assertRaises(KeelError):
            validate_verdict_payload(
                {
                    "verdict": "approve",
                    "summary": "ok",
                    "blocking": [],
                    "notes": [],
                    "commands_run": [],
                    "extra_key": 1,
                }
            )
        with self.assertRaises(KeelError):
            validate_verdict_payload(
                {
                    "verdict": "request_changes",
                    "summary": "Only style notes",
                    "blocking": [],
                    "notes": ["rename helper"],
                    "commands_run": [],
                }
            )
        with self.assertRaises(KeelError):
            validate_verdict_payload(
                {
                    "verdict": "approve",
                    "summary": "contradiction",
                    "blocking": ["broken edge case"],
                    "notes": [],
                    "commands_run": [],
                }
            )

    def test_verify_locked_spec_dual_convergence_and_atomic_squash_land(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="mul")

            wt_res = _run_cli(["worktree", "create", "mul", "--project", str(repo)])
            wt_path = Path(wt_res["path"])

            # Worker implements feature + updates Progress (and suppose worker mischievously edited Bounds on branch!)
            (wt_path / "calc.py").write_text(
                "def add(a: int, b: int) -> int:\n    return a + b\n\ndef mul(a: int, b: int) -> int:\n    return a * b\n",
                encoding="utf-8",
            )
            (wt_path / "docs/tasks/active/20261117-mul.md").write_text(
                "# Feature mul\n\n## Goal & Why\nAdd mul() cleanly.\n\n## Bounds\nWEAKENED ON BRANCH!\n\n## Done-check\npython3 -c 'import calc'\n\n## Progress\n- [x] Implement\n",
                encoding="utf-8",
            )
            _git(wt_path, "add", ".")
            _git(wt_path, "commit", "-m", "feat(calc): add mul\n\nCo-authored-by: Helper <h@example.com>")
            head_sha = _git(wt_path, "rev-parse", "HEAD")

            # Repo-wide `keel branch status` overview
            overview = _run_cli(["branch", "status", "--project", str(repo)])
            self.assertEqual(overview["todo_tasks"], ["docs/tasks/todo/20261117-backlog.md"])
            self.assertEqual(overview["active_tasks_on_base"], ["docs/tasks/active/20261117-mul.md"])
            self.assertEqual(len(overview["worktrees"]), 1)
            self.assertFalse(overview["worktrees"][0]["gate"]["ready_to_land"])

            # Failing deterministic `--check` aborts BEFORE invoking LLM verifier!
            codex_called = False

            def fake_codex_never(self_obj: Any, req: Any, defaults: Any) -> dict[str, Any]:
                nonlocal codex_called
                codex_called = True
                return {}

            with mock.patch.object(CodexExecutor, "execute", fake_codex_never):
                with self.assertRaisesRegex(KeelError, "Deterministic check failed"):
                    _run_cli(["branch", "verify", "mul", "--project", str(repo), "--check", "exit 1"])
            self.assertFalse(codex_called)

            # `--dual` verification: round 1 where BOTH `codex` and `agy` return `request_changes` -> counts as 1 round!
            captured_briefs: list[str] = []

            def fake_codex_rc(self_obj: Any, req: Any, defaults: Any) -> dict[str, Any]:
                captured_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                req.output_path.write_text(
                    json.dumps(
                        {
                            "verdict": "request_changes",
                            "summary": "Bounds weakened on branch.",
                            "blocking": ["Restore original Bounds in 20261117-mul.md"],
                            "notes": [],
                            "commands_run": ["python3 -c 'import calc'"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": "codex", "model": "gpt-6-astra", "effort": "max"}

            with mock.patch.object(CodexExecutor, "execute", fake_codex_rc):
                dual_r1 = _run_cli(
                    [
                        "branch",
                        "verify",
                        "mul",
                        "--project",
                        str(repo),
                        "--dual",
                        "--check",
                        "python3 -c 'import calc; assert calc.mul(3, 4) == 12'",
                    ]
                )

            # Verify `main`'s IMMUTABLE locked spec ("calc.py only.") was injected into the verifier's brief!
            self.assertIn("calc.py only.", captured_briefs[0])
            self.assertEqual(dual_r1["request_changes_rounds"], 1)
            agy_desc = dual_r1["verifiers"]["agy"]
            agy_scratch = Path(agy_desc["scratch_root"])
            self.assertTrue(agy_scratch.is_dir())
            agy_verdict_file = Path(agy_desc["verdict_path"])
            agy_verdict_file.write_text(
                json.dumps(
                    {
                        "verdict": "request_changes",
                        "summary": "Bounds modified.",
                        "blocking": ["Restore Bounds section."],
                        "notes": [],
                        "commands_run": ["git diff"],
                    }
                ),
                encoding="utf-8",
            )
            rec_r1 = _run_cli(
                [
                    "branch",
                    "record-verdict",
                    "mul",
                    "--project",
                    str(repo),
                    "--verdict-file",
                    str(agy_verdict_file),
                    "--head-sha",
                    head_sha,
                    "--executor",
                    "agy",
                    "--cleanup-scratch",
                    str(agy_scratch),
                ]
            )
            # Dual pass on same SHA still counts as 1 round (NOT 2), and scratch clone was cleaned up!
            self.assertEqual(rec_r1["request_changes_rounds"], 1)
            self.assertFalse(agy_scratch.exists())

            # Author fixes the defect on a new commit
            (wt_path / "docs/tasks/active/20261117-mul.md").write_text(
                "# Feature mul\n\n## Goal & Why\nAdd mul() cleanly.\n\n## Bounds\ncalc.py only.\n\n## Done-check\npython3 -c 'import calc'\n\n## Progress\n- [x] Implement\n",
                encoding="utf-8",
            )
            _git(wt_path, "add", ".")
            _git(wt_path, "commit", "-m", "fix(tasks): restore locked Bounds")
            head_sha_2 = _git(wt_path, "rev-parse", "HEAD")

            def fake_codex_approve(self_obj: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "verdict": "approve",
                            "summary": "Verified mul() and Canary break.",
                            "blocking": [],
                            "notes": ["Clean implementation."],
                            "commands_run": ["python3 -c 'import calc; assert calc.mul(3, 4) == 12'"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": "codex", "model": "gpt-6-astra", "effort": "max"}

            with mock.patch.object(CodexExecutor, "execute", fake_codex_approve):
                dual_r2 = _run_cli(
                    [
                        "branch",
                        "verify",
                        "mul",
                        "--project",
                        str(repo),
                        "--dual",
                        "--check",
                        "python3 -c 'import calc; assert calc.mul(3, 4) == 12'",
                    ]
                )

            # With only `codex` approved so far in `--dual`, branch MUST NOT be ready to land yet!
            st_half = _run_cli(["branch", "status", "mul", "--project", str(repo)])
            self.assertFalse(st_half["ready_to_land"])
            self.assertTrue(st_half["dual_required"])

            # Now `agy` also approves via `record-verdict`
            agy_desc_2 = dual_r2["verifiers"]["agy"]
            Path(agy_desc_2["verdict_path"]).write_text(
                json.dumps(
                    {
                        "verdict": "approve",
                        "summary": "Approved independently.",
                        "blocking": [],
                        "notes": [],
                        "commands_run": ["python3 -c 'import calc'"],
                    }
                ),
                encoding="utf-8",
            )
            _run_cli(
                [
                    "branch",
                    "record-verdict",
                    "mul",
                    "--project",
                    str(repo),
                    "--verdict-file",
                    agy_desc_2["verdict_path"],
                    "--head-sha",
                    head_sha_2,
                    "--executor",
                    "agy",
                    "--cleanup-scratch",
                    agy_desc_2["scratch_root"],
                ]
            )

            # Dry-run `keel branch land` reports `ready_to_land: True` without mutating `main`
            dry = _run_cli(["branch", "land", "mul", "--project", str(repo)])
            self.assertTrue(dry["ready_to_land"])
            self.assertFalse(dry["landed"])
            self.assertEqual(dry["approval_source"], f"dual-approve:{head_sha_2[:12]}")

            # Subagent modes (`write` and `read`) CANNOT execute or discard via `keel branch land`!
            for sub_mode in ("write", "read"):
                with self.assertRaisesRegex(KeelError, "Subagents cannot execute or discard"):
                    _run_cli(["branch", "land", "mul", "--project", str(repo), "--execute"], agent_mode=sub_mode)
                with self.assertRaisesRegex(KeelError, "Subagents cannot execute or discard"):
                    _run_cli(
                        ["branch", "land", "mul", "--project", str(repo), "--discard", "--reason", "nope"],
                        agent_mode=sub_mode,
                    )
            with self.assertRaisesRegex(KeelError, "Read-only subagent mode"):
                _run_cli(["branch", "verify", "mul", "--project", str(repo)], agent_mode="read")

            # Execute default `--squash` land: 1 atomic commit bundling code + `active -> archive` + trailers!
            base_before = _git(repo, "rev-parse", "HEAD")
            landed = _run_cli(["branch", "land", "mul", "--project", str(repo), "--execute"])
            self.assertTrue(landed["landed"])
            self.assertEqual(landed["archived_task"], "docs/tasks/archive/20261117-mul.md")
            self.assertFalse(wt_path.exists())
            self.assertEqual(_git(repo, "status", "--porcelain", "-uall"), "")
            # Exactly 1 commit added on `main`!
            rev_count = _git(repo, "rev-list", "--count", f"{base_before}..HEAD")
            self.assertEqual(rev_count, "1")
            self.assertFalse((repo / "docs/tasks/active/20261117-mul.md").exists())
            self.assertTrue((repo / "docs/tasks/archive/20261117-mul.md").is_file())
            self.assertIn("Co-authored-by: Helper <h@example.com>", _git(repo, "log", "-1", "--format=%B"))

    def test_two_round_circuit_breaker_and_pure_rebase_mandatory_check_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="sub")
            wt_res = _run_cli(["worktree", "create", "sub", "--project", str(repo)])
            wt_path = Path(wt_res["path"])
            (wt_path / "calc.py").write_text("def sub(a: int, b: int) -> int:\n    return a - b\n", encoding="utf-8")
            _git(wt_path, "add", "calc.py")
            _git(wt_path, "commit", "-m", "feat: sub")

            def fake_rc(self_obj: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "verdict": "request_changes",
                            "summary": "Missing add()",
                            "blocking": ["Keep add() intact"],
                            "notes": [],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": "codex", "model": "gpt-6-astra", "effort": "max"}

            with mock.patch.object(CodexExecutor, "execute", fake_rc):
                _run_cli(["branch", "verify", "sub", "--project", str(repo)])
                _run_cli(["branch", "verify", "sub", "--project", str(repo)])
                # 3rd round blocked by 2-round circuit breaker unless `--force-round`!
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "sub", "--project", str(repo)])

            # Fix code, run authorized `--force-round` with `--check` -> approves `old_head`
            (wt_path / "calc.py").write_text(
                "def add(a: int, b: int) -> int:\n    return a + b\n\ndef sub(a: int, b: int) -> int:\n    return a - b\n",
                encoding="utf-8",
            )
            _git(wt_path, "add", "calc.py")
            _git(wt_path, "commit", "-m", "fix: restore add()")
            old_base = _git(repo, "rev-parse", "main")
            old_head = _git(wt_path, "rev-parse", "HEAD")

            def fake_ok(self_obj: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "verdict": "approve",
                            "summary": "All good",
                            "blocking": [],
                            "notes": [],
                            "commands_run": ["python3 -c 'import calc'"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": "codex", "model": "gpt-6-astra", "effort": "max"}

            with mock.patch.object(CodexExecutor, "execute", fake_ok):
                _run_cli(
                    [
                        "branch",
                        "verify",
                        "sub",
                        "--project",
                        str(repo),
                        "--force-round",
                        "--check",
                        "python3 -c 'import calc; assert calc.sub(5, 2) == 3'",
                    ]
                )

            # Advance `main` with an unrelated file and rebase `task/sub` onto `main`
            (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-m", "docs: add readme")
            new_base = _git(repo, "rev-parse", "main")
            _git(wt_path, "rebase", "main")
            new_head = _git(wt_path, "rev-parse", "HEAD")

            proof = compare_rebase(repo, old_base, old_head, new_base, new_head)
            self.assertTrue(proof["ok"])

            # Custom `.gitattributes` merge driver is rejected by `compare_rebase`
            _git(repo, "branch", "task/attr", old_head)
            wt_attr = Path(_run_cli(["worktree", "create", "attr", "--project", str(repo)])["path"])
            (wt_attr / ".gitattributes").write_text("calc.py merge=ours\n", encoding="utf-8")
            _git(wt_attr, "add", ".gitattributes")
            _git(wt_attr, "commit", "-m", "custom merge attr")
            attr_old = _git(wt_attr, "rev-parse", "HEAD")
            _git(wt_attr, "rebase", "main")
            attr_new = _git(wt_attr, "rev-parse", "HEAD")
            with self.assertRaises(KeelError):
                compare_rebase(repo, old_base, attr_old, new_base, attr_new)

            # Pure-rebase land WITHOUT `--check` MUST be blocked because `old_head` had deterministic checks!
            dry_missing_chk = _run_cli(
                [
                    "branch",
                    "land",
                    "sub",
                    "--project",
                    str(repo),
                    "--old-base",
                    old_base,
                    "--old-head",
                    old_head,
                ]
            )
            self.assertFalse(dry_missing_chk["ready_to_land"])
            self.assertTrue(any("requires re-running deterministic checks" in b for b in dry_missing_chk["blockers"]))

            # Current-head `request_changes` MUST strictly block landing even when `--old-base`/`--old-head` was approved!
            rej_file = Path(tmp) / "rej_new_head.json"
            rej_file.write_text(
                json.dumps(
                    {
                        "verdict": "request_changes",
                        "summary": "Rebased head broke runtime interaction.",
                        "blocking": ["Fix interaction on rebased head."],
                        "notes": [],
                        "commands_run": ["pytest"],
                    }
                ),
                encoding="utf-8",
            )
            _run_cli(
                [
                    "branch",
                    "record-verdict",
                    "sub",
                    "--project",
                    str(repo),
                    "--verdict-file",
                    str(rej_file),
                    "--head-sha",
                    new_head,
                    "--executor",
                    "codex",
                ]
            )
            rej_gate = _run_cli(
                [
                    "branch",
                    "land",
                    "sub",
                    "--project",
                    str(repo),
                    "--old-base",
                    old_base,
                    "--old-head",
                    old_head,
                    "--check",
                    "python3 -c 'import calc; assert calc.sub(5, 2) == 3'",
                ]
            )
            self.assertFalse(rej_gate["ready_to_land"])
            self.assertTrue(any("active 'request_changes' verdict" in b for b in rej_gate["blockers"]))
            with self.assertRaisesRegex(KeelError, "request_changes"):
                _run_cli(
                    [
                        "branch",
                        "land",
                        "sub",
                        "--project",
                        str(repo),
                        "--old-base",
                        old_base,
                        "--old-head",
                        old_head,
                        "--check",
                        "python3 -c 'import calc; assert calc.sub(5, 2) == 3'",
                        "--execute",
                    ]
                )

            # Re-rebase a clean commit (`new_head_2`) without current-head verdicts to test pure-rebase carry-over land!
            _git(wt_path, "commit", "--amend", "--no-edit", "--date", "2026-11-17T12:00:00Z")
            new_head_2 = _git(wt_path, "rev-parse", "HEAD")
            self.assertNotEqual(new_head_2, new_head)
            cmp_cli = _run_cli(
                [
                    "branch",
                    "compare",
                    "--project",
                    str(repo),
                    "--old-base",
                    old_base,
                    "--old-head",
                    old_head,
                    "--base",
                    new_base,
                    "--head",
                    new_head_2,
                ]
            )
            self.assertTrue(cmp_cli["ok"])

            # Re-running `--check` with `--ff-only --execute` lands cleanly + commits `docs(tasks): archive ...`!
            ff_land = _run_cli(
                [
                    "branch",
                    "land",
                    "sub",
                    "--project",
                    str(repo),
                    "--ff-only",
                    "--old-base",
                    old_base,
                    "--old-head",
                    old_head,
                    "--check",
                    "python3 -c 'import calc; assert calc.sub(5, 2) == 3'",
                    "--execute",
                ]
            )
            self.assertTrue(ff_land["landed"])
            self.assertEqual(ff_land["approval_source"], f"pure-rebase-from:{old_head[:12]}")
            self.assertEqual(ff_land["archived_task"], "docs/tasks/archive/20261117-sub.md")
            self.assertEqual(_git(repo, "log", "-1", "--format=%s"), "docs(tasks): archive 20261117-sub")
            self.assertFalse(wt_path.exists())

    def test_discard_branch_archives_task_with_outcome_without_merging_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="abandoned")
            wt_res = _run_cli(["worktree", "create", "abandoned", "--project", str(repo)])
            wt_path = Path(wt_res["path"])
            (wt_path / "calc.py").write_text("BROKEN = True\n", encoding="utf-8")
            _git(wt_path, "add", "calc.py")
            _git(wt_path, "commit", "-m", "wip: dead-end prototype")
            # Even leave untracked scratch file inside the discarded worktree!
            (wt_path / "scratch.tmp").write_text("dirty\n", encoding="utf-8")

            # `--discard` without `--reason` MUST fail
            with self.assertRaisesRegex(KeelError, "--reason"):
                _run_cli(["branch", "land", "abandoned", "--project", str(repo), "--discard"])

            res = _run_cli(
                [
                    "branch",
                    "land",
                    "abandoned",
                    "--project",
                    str(repo),
                    "--discard",
                    "--reason",
                    "Superseded by simpler pure-Git design",
                ]
            )
            self.assertTrue(res["discarded"])
            self.assertFalse(wt_path.exists())
            # `calc.py` on `main` was NEVER touched by the discarded branch!
            self.assertNotIn("BROKEN", (repo / "calc.py").read_text(encoding="utf-8"))
            self.assertFalse((repo / "docs/tasks/active/20261117-abandoned.md").exists())
            archived = (repo / "docs/tasks/archive/20261117-abandoned.md").read_text(encoding="utf-8")
            self.assertIn("## Outcome: [DISCARDED]", archived)
            self.assertIn("- **Reason:** Superseded by simpler pure-Git design", archived)
            self.assertEqual(_git(repo, "log", "-1", "--format=%s"), "docs(tasks): discard 20261117-abandoned")
            self.assertEqual(_git(repo, "status", "--porcelain", "-uall"), "")

    def test_taskless_two_line_fix_and_human_override_land(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="other")
            wt_res = _run_cli(["worktree", "create", "tiny-fix", "--project", str(repo)])
            wt_path = Path(wt_res["path"])
            (wt_path / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return int(a) + int(b)\n", encoding="utf-8")
            _git(wt_path, "add", "calc.py")
            _git(wt_path, "commit", "-m", "fix(calc): cast operands")

            landed = _run_cli(
                [
                    "branch",
                    "land",
                    "tiny-fix",
                    "--project",
                    str(repo),
                    "--human-override",
                    "Owner authorized hotfix",
                    "--execute",
                ]
            )
            self.assertTrue(landed["landed"])
            self.assertIsNone(landed["archived_task"])
            self.assertIn("Keel-Human-Override: Owner authorized hotfix", _git(repo, "log", "-1", "--format=%B"))

    def test_cross_branch_teardown_isolation_and_custom_branch_worktree_land(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="alpha")

            # 1. `.agents/worktrees/alpha` (`task/alpha`) has uncommitted work; standalone `feature/alpha` is discarded!
            wt_alpha = Path(_run_cli(["worktree", "create", "alpha", "--project", str(repo)])["path"])
            (wt_alpha / "calc.py").write_text("# precious uncommitted work in task/alpha\n", encoding="utf-8")

            _git(repo, "branch", "feature/alpha", "main")
            discard_feat = _run_cli(
                [
                    "branch",
                    "land",
                    "feature/alpha",
                    "--project",
                    str(repo),
                    "--discard",
                    "--reason",
                    "Drop standalone feature/alpha without touching task/alpha",
                ]
            )
            self.assertTrue(discard_feat["discarded"])
            self.assertIsNone(discard_feat["removed_worktree"])
            self.assertEqual(discard_feat["deleted_branch"], "feature/alpha")
            self.assertIsNone(discard_feat["archived_task"])
            # `task/alpha` worktree, uncommitted edits, branch ref, AND `20261117-alpha.md` remain 100% intact!
            self.assertTrue(wt_alpha.is_dir())
            self.assertIn("precious uncommitted", (wt_alpha / "calc.py").read_text(encoding="utf-8"))
            self.assertTrue((repo / "docs/tasks/active/20261117-alpha.md").is_file())
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", "refs/heads/feature/alpha"],
                    check=False,
                ).returncode,
                1,
            )

            # 2. Worktree created with custom `--branch feat/my-fix` lands and tears down cleanly without crashing!
            wt_custom = Path(
                _run_cli(["worktree", "create", "my-fix", "--branch", "feat/my-fix", "--project", str(repo)])["path"]
            )
            (wt_custom / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b + 0\n", encoding="utf-8")
            _git(wt_custom, "add", "calc.py")
            _git(wt_custom, "commit", "-m", "fix: custom branch")
            custom_sha = _git(wt_custom, "rev-parse", "HEAD")

            v_file = Path(tmp) / "custom_ok.json"
            v_file.write_text(
                json.dumps(
                    {
                        "verdict": "approve",
                        "summary": "ok",
                        "blocking": [],
                        "notes": [],
                        "commands_run": ["git diff"],
                    }
                ),
                encoding="utf-8",
            )
            _run_cli(
                [
                    "branch",
                    "record-verdict",
                    "feat/my-fix",
                    "--project",
                    str(repo),
                    "--verdict-file",
                    str(v_file),
                    "--head-sha",
                    custom_sha,
                    "--executor",
                    "agy",
                ]
            )
            landed_custom = _run_cli(["branch", "land", "feat/my-fix", "--project", str(repo), "--execute"])
            self.assertTrue(landed_custom["landed"])
            self.assertFalse(wt_custom.exists())
            self.assertEqual(landed_custom["deleted_branch"], "feat/my-fix")

    def test_concurrent_flock_state_safety_pending_hygiene_and_parallel_dual(self) -> None:
        import keel_lib.branch_gate as bg

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "project with spaces"
            _init_repo_with_task(repo, slug="para")
            wt_path = Path(_run_cli(["worktree", "create", "para", "--project", str(repo)])["path"])
            (wt_path / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n# v2\n", encoding="utf-8")
            _git(wt_path, "add", "calc.py")
            _git(wt_path, "commit", "-m", "feat: v2")
            sha1 = _git(wt_path, "rev-parse", "HEAD")

            # 1. Failed `agy` setup (e.g. invalid model) MUST NOT leave `"agy"` in `pending_verifiers`!
            with self.assertRaises(KeelError):
                _run_cli(["branch", "verify", "para", "--project", str(repo), "--executor", "agy", "--model", "bad-model"])
            st_clean = _run_cli(["branch", "status", "para", "--project", str(repo)])
            self.assertEqual(st_clean["pending_verifiers"], [])

            # 2. True parallel `--dual --executor agy` + `--dual --executor codex` joining the SAME dual round!
            agy_prep = _run_cli(["branch", "verify", "para", "--project", str(repo), "--dual", "--executor", "agy"])
            self.assertTrue(agy_prep["dual"])
            # `record_verdict_command` properly shell-quotes `project with spaces`
            import shlex

            self.assertIn(shlex.quote(str(repo)), agy_prep["record_verdict_command"])
            parsed_cmd = shlex.split(agy_prep["record_verdict_command"])
            self.assertEqual(parsed_cmd[parsed_cmd.index("--project") + 1], str(repo))
            agy_scratch = Path(agy_prep["scratch_root"])
            agy_vpath = Path(agy_prep["verdict_path"])

            # Simulate `agy` recording `"request_changes"` CONCURRENTLY *while* `codex` `--dual --executor codex` is running!
            def fake_codex_concurrent_rc(self_obj: Any, req: Any, defaults: Any) -> dict[str, Any]:
                agy_vpath.write_text(
                    json.dumps(
                        {
                            "verdict": "request_changes",
                            "summary": "AGY found defect mid-flight",
                            "blocking": ["Fix edge case"],
                            "notes": [],
                            "commands_run": ["pytest"],
                        }
                    ),
                    encoding="utf-8",
                )
                _run_cli(
                    [
                        "branch",
                        "record-verdict",
                        "para",
                        "--project",
                        str(repo),
                        "--verdict-file",
                        str(agy_vpath),
                        "--head-sha",
                        sha1,
                        "--executor",
                        "agy",
                        "--cleanup-scratch",
                        str(agy_scratch),
                    ]
                )
                req.output_path.write_text(
                    json.dumps(
                        {
                            "verdict": "request_changes",
                            "summary": "Codex also found defect",
                            "blocking": ["Fix edge case"],
                            "notes": [],
                            "commands_run": ["pytest"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": "codex", "model": "gpt-6-astra", "effort": "max"}

            with mock.patch.object(CodexExecutor, "execute", fake_codex_concurrent_rc):
                codex_dual = _run_cli(
                    ["branch", "verify", "para", "--project", str(repo), "--dual", "--executor", "codex"]
                )

            # Both verdicts preserved (neither clobbered!) AND joined 1 single dual round (`request_changes_rounds == 1`)!
            st_after_para = _run_cli(["branch", "status", "para", "--project", str(repo)])
            self.assertEqual(set(st_after_para["verdicts"].keys()), {"agy", "codex"})
            self.assertEqual(codex_dual["request_changes_rounds"], 1)
            self.assertEqual(st_after_para["request_changes_rounds"], 1)
            self.assertEqual(st_after_para["pending_verifiers"], [])

            # 3. Switching from `--dual` to single `--executor codex` clears stale `pending_verifiers` and `dual_required`!
            (wt_path / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n# v3\n", encoding="utf-8")
            _git(wt_path, "add", "calc.py")
            _git(wt_path, "commit", "-m", "feat: v3")
            sha2 = _git(wt_path, "rev-parse", "HEAD")

            stale_prep = _run_cli(["branch", "verify", "para", "--project", str(repo), "--dual", "--executor", "agy"])
            shutil.rmtree(stale_prep["scratch_root"], ignore_errors=True)
            self.assertEqual(_run_cli(["branch", "status", "para", "--project", str(repo)])["pending_verifiers"], ["agy"])

            def fake_codex_ok(self_obj: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "verdict": "approve",
                            "summary": "Single codex approved",
                            "blocking": [],
                            "notes": [],
                            "commands_run": ["pytest"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": "codex", "model": "gpt-6-astra", "effort": "max"}

            with mock.patch.object(CodexExecutor, "execute", fake_codex_ok):
                _run_cli(["branch", "verify", "para", "--project", str(repo), "--executor", "codex"])

            st_single = _run_cli(["branch", "status", "para", "--project", str(repo)])
            self.assertFalse(st_single["dual_required"])
            self.assertEqual(st_single["pending_verifiers"], [])
            self.assertTrue(st_single["ready_to_land"])

            # 4. Concurrent `record-verdict` (`request_changes`) arriving *while* `land --check` runs `run_deterministic_checks`
            # MUST NOT be overwritten by `land_branch`'s check-state update, and MUST block `--execute`!
            orig_run_checks = bg.run_deterministic_checks

            def hooked_checks(proj_arg: Path, head_arg: str, cmds: Any) -> dict[str, Any]:
                res_chk = orig_run_checks(proj_arg, head_arg, cmds)
                late_rej = Path(tmp) / "late_rej.json"
                late_rej.write_text(
                    json.dumps(
                        {
                            "verdict": "request_changes",
                            "summary": "Late verifier caught race bug",
                            "blocking": ["Race bug"],
                            "notes": [],
                            "commands_run": ["pytest"],
                        }
                    ),
                    encoding="utf-8",
                )
                _run_cli(
                    [
                        "branch",
                        "record-verdict",
                        "para",
                        "--project",
                        str(repo),
                        "--verdict-file",
                        str(late_rej),
                        "--head-sha",
                        sha2,
                        "--executor",
                        "agy",
                    ]
                )
                return res_chk

            with mock.patch.object(bg, "run_deterministic_checks", hooked_checks):
                with self.assertRaisesRegex(KeelError, "request_changes"):
                    _run_cli(
                        [
                            "branch",
                            "land",
                            "para",
                            "--project",
                            str(repo),
                            "--check",
                            "python3 -c 'import calc'",
                            "--execute",
                        ]
                    )

    def test_round2_pinned_sha_idempotent_verdict_dual_cap_join_custom_base_and_utf8_tasks(self) -> None:
        import keel_lib.branch_gate as bg
        from keel_lib.guard_rules import evaluate_command

        with tempfile.TemporaryDirectory() as tmp:
            # A. End-to-end UTF-8 task filename (`docs/tasks/active/20261117-修复-mul.md`) + `guard_rules.py` allow on `main`
            repo = Path(tmp) / "utf8-repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.com")
            (repo / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
            _git(repo, "add", "calc.py")
            _git(repo, "commit", "-m", "init")

            utf8_rel = "docs/tasks/active/20261117-修复-mul.md"
            utf8_abs = repo / utf8_rel
            utf8_abs.parent.mkdir(parents=True, exist_ok=True)
            utf8_abs.write_text(
                "# 修复乘法\n\n## Goal & Why\n支持 UTF-8 任务文件归档。\n\n## Bounds\ncalc.py only.\n\n## Done-check\npython3 -c 'import calc'\n",
                encoding="utf-8",
            )
            self.assertIsNone(
                evaluate_command(
                    f"git add '{utf8_rel}' && git commit -m 'docs(tasks): open utf8 spec'",
                    mode="main",
                    cwd=repo,
                )
            )
            _git(repo, "add", utf8_rel)
            self.assertIsNone(evaluate_command("git commit -m 'docs(tasks): staged utf8'", mode="main", cwd=repo))
            _git(repo, "commit", "-m", "docs(tasks): open utf8 spec")

            wt_mul = Path(_run_cli(["worktree", "create", "mul", "--project", str(repo)])["path"])
            (wt_mul / "calc.py").write_text(
                "def add(a: int, b: int) -> int:\n    return a + b\n\ndef mul(a: int, b: int) -> int:\n    return a * b\n",
                encoding="utf-8",
            )
            _git(wt_mul, "add", "calc.py")
            _git(wt_mul, "commit", "-m", "feat: add mul")

            seen_briefs: list[str] = []

            def fake_codex_utf8(self_obj: Any, req: Any, defaults: Any) -> dict[str, Any]:
                seen_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                req.output_path.write_text(
                    json.dumps(
                        {
                            "verdict": "approve",
                            "summary": "UTF-8 spec verified",
                            "blocking": [],
                            "notes": [],
                            "commands_run": ["python3 -c 'import calc'"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": "codex", "model": "gpt-6-astra", "effort": "max"}

            with mock.patch.object(CodexExecutor, "execute", fake_codex_utf8):
                v_utf8 = _run_cli(["branch", "verify", "mul", "--project", str(repo)])
            self.assertEqual(v_utf8["task_file"], utf8_rel)
            self.assertIn("支持 UTF-8 任务文件归档。", seen_briefs[0])

            land_utf8 = _run_cli(["branch", "land", "mul", "--project", str(repo), "--execute"])
            self.assertTrue(land_utf8["landed"])
            self.assertEqual(land_utf8["archived_task"], "docs/tasks/archive/20261117-修复-mul.md")
            self.assertTrue((repo / "docs/tasks/archive/20261117-修复-mul.md").is_file())
            self.assertFalse((repo / utf8_rel).exists())

            # B. Pinning `land_branch` to verified `H1`: advancing branch `H1 -> H2` during `--check` aborts cleanly!
            wt_race = Path(_run_cli(["worktree", "create", "race", "--project", str(repo)])["path"])
            (wt_race / "calc.py").write_text("# H1 verified\n", encoding="utf-8")
            _git(wt_race, "add", "calc.py")
            _git(wt_race, "commit", "-m", "feat: H1")
            h1_sha = _git(wt_race, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", fake_codex_utf8):
                _run_cli(["branch", "verify", "race", "--project", str(repo)])

            orig_checks = bg.run_deterministic_checks
            h2_sha_holder: list[str] = []

            def checks_advancing_branch(proj_arg: Path, head_arg: str, cmds: Any) -> dict[str, Any]:
                res_c = orig_checks(proj_arg, head_arg, cmds)
                (wt_race / "calc.py").write_text("# H2 UNVERIFIED!\n", encoding="utf-8")
                _git(wt_race, "add", "calc.py")
                _git(wt_race, "commit", "-m", "wip: H2 unverified")
                h2_sha_holder.append(_git(wt_race, "rev-parse", "HEAD"))
                return res_c

            main_before_race = _git(repo, "rev-parse", "main")
            with mock.patch.object(bg, "run_deterministic_checks", checks_advancing_branch):
                with self.assertRaisesRegex(KeelError, "moved during land"):
                    _run_cli(
                        [
                            "branch",
                            "land",
                            "race",
                            "--project",
                            str(repo),
                            "--check",
                            "true",
                            "--execute",
                        ]
                    )
            # Neither H1 nor H2 merged into `main`, and `wt_race` + H2 remain completely intact!
            self.assertEqual(_git(repo, "rev-parse", "main"), main_before_race)
            self.assertTrue(wt_race.is_dir())
            self.assertEqual(_git(wt_race, "rev-parse", "HEAD"), h2_sha_holder[0])
            self.assertNotEqual(h1_sha, h2_sha_holder[0])

            # Also verify advancing `main` (`base_ref`) during `land --check` aborts cleanly without merging or deleting `wt_race`!
            with mock.patch.object(CodexExecutor, "execute", fake_codex_utf8):
                _run_cli(["branch", "verify", "race", "--project", str(repo)])

            main_adv_holder: list[str] = []

            def checks_advancing_base(proj_arg: Path, head_arg: str, cmds: Any) -> dict[str, Any]:
                res_c = orig_checks(proj_arg, head_arg, cmds)
                (repo / "docs" / "tasks" / "todo" / "concurrent.md").parent.mkdir(parents=True, exist_ok=True)
                (repo / "docs" / "tasks" / "todo" / "concurrent.md").write_text("# Concurrent\n", encoding="utf-8")
                _git(repo, "add", "docs/tasks/todo/concurrent.md")
                _git(repo, "commit", "-m", "docs(tasks): concurrent base commit")
                main_adv_holder.append(_git(repo, "rev-parse", "main"))
                return res_c

            with mock.patch.object(bg, "run_deterministic_checks", checks_advancing_base):
                with self.assertRaisesRegex(KeelError, "Base 'main' moved during land"):
                    _run_cli(["branch", "land", "race", "--project", str(repo), "--check", "true", "--execute"])
            self.assertEqual(_git(repo, "rev-parse", "main"), main_adv_holder[0])
            self.assertTrue(wt_race.is_dir())
            self.assertEqual(_git(wt_race, "rev-parse", "HEAD"), h2_sha_holder[0])

            # C. Idempotent `record-verdict` + joining an open incomplete Round-2 `--dual` round at the cap!
            wt_idem = Path(_run_cli(["worktree", "create", "idem", "--project", str(repo)])["path"])
            (wt_idem / "calc.py").write_text("# idem r1\n", encoding="utf-8")
            _git(wt_idem, "add", "calc.py")
            _git(wt_idem, "commit", "-m", "feat: r1")
            idem_sha1 = _git(wt_idem, "rev-parse", "HEAD")

            agy_r1 = _run_cli(["branch", "verify", "idem", "--project", str(repo), "--executor", "agy"])
            rc_file = Path(tmp) / "idem_rc.json"
            rc_file.write_text(
                json.dumps(
                    {
                        "verdict": "request_changes",
                        "summary": "Round 1 defect",
                        "blocking": ["Fix r1"],
                        "notes": [],
                        "commands_run": ["git diff"],
                    }
                ),
                encoding="utf-8",
            )
            # Recording the EXACT same `(idem_sha1, agy)` verdict TWICE keeps `request_changes_rounds == 1`!
            for _ in range(2):
                rec_idem = _run_cli(
                    [
                        "branch",
                        "record-verdict",
                        "idem",
                        "--project",
                        str(repo),
                        "--verdict-file",
                        str(rc_file),
                        "--head-sha",
                        idem_sha1,
                        "--executor",
                        "agy",
                        "--cleanup-scratch",
                        agy_r1["scratch_root"],
                    ]
                )
            self.assertEqual(rec_idem["request_changes_rounds"], 1)
            ok_file = Path(tmp) / "idem_ok.json"
            ok_file.write_text(
                json.dumps(
                    {
                        "verdict": "approve",
                        "summary": "Corrected in place",
                        "blocking": [],
                        "notes": [],
                        "commands_run": ["git diff"],
                    }
                ),
                encoding="utf-8",
            )
            rec_flip_ok = _run_cli(
                [
                    "branch",
                    "record-verdict",
                    "idem",
                    "--project",
                    str(repo),
                    "--verdict-file",
                    str(ok_file),
                    "--head-sha",
                    idem_sha1,
                    "--executor",
                    "agy",
                ]
            )
            self.assertEqual(rec_flip_ok["request_changes_rounds"], 0)
            rec_flip_rc = _run_cli(
                [
                    "branch",
                    "record-verdict",
                    "idem",
                    "--project",
                    str(repo),
                    "--verdict-file",
                    str(rc_file),
                    "--head-sha",
                    idem_sha1,
                    "--executor",
                    "agy",
                ]
            )
            self.assertEqual(rec_flip_rc["request_changes_rounds"], 1)

            # Commit `idem_sha2` for Round 2 (`--dual`): `agy` rejects first (pushing `request_changes_rounds` to 2 == cap),
            # then `codex` joins Round 2 WITHOUT `--force-round` and completes Round 2 cleanly!
            (wt_idem / "calc.py").write_text("# idem r2\n", encoding="utf-8")
            _git(wt_idem, "add", "calc.py")
            _git(wt_idem, "commit", "-m", "feat: r2")
            idem_sha2 = _git(wt_idem, "rev-parse", "HEAD")

            agy_r2 = _run_cli(["branch", "verify", "idem", "--project", str(repo), "--dual", "--executor", "agy"])
            rec_r2_agy = _run_cli(
                [
                    "branch",
                    "record-verdict",
                    "idem",
                    "--project",
                    str(repo),
                    "--verdict-file",
                    str(rc_file),
                    "--head-sha",
                    idem_sha2,
                    "--executor",
                    "agy",
                    "--cleanup-scratch",
                    agy_r2["scratch_root"],
                ]
            )
            self.assertEqual(rec_r2_agy["request_changes_rounds"], 2)

            def fake_codex_r2_rc(self_obj: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(rc_file.read_text(encoding="utf-8"), encoding="utf-8")
                return {"status": "completed", "executor": "codex", "model": "gpt-6-astra", "effort": "max"}

            with mock.patch.object(CodexExecutor, "execute", fake_codex_r2_rc):
                codex_r2 = _run_cli(
                    ["branch", "verify", "idem", "--project", str(repo), "--dual", "--executor", "codex"]
                )
            self.assertEqual(codex_r2["request_changes_rounds"], 2)
            # Now that Round 2 IS complete, starting a brand-new Round 3 without `--force-round` IS blocked!
            with self.assertRaisesRegex(KeelError, "convergence cap"):
                _run_cli(["branch", "verify", "idem", "--project", str(repo), "--dual", "--executor", "agy"])

            # D. Landing onto custom `--base develop` when `main` also exists in the repository!
            _git(repo, "checkout", "-b", "develop", "main")
            main_tip = _git(repo, "rev-parse", "main")
            wt_dev = Path(_run_cli(["worktree", "create", "dev-feat", "--base", "develop", "--project", str(repo)])["path"])
            (wt_dev / "dev.py").write_text("DEV = 1\n", encoding="utf-8")
            _git(wt_dev, "add", "dev.py")
            _git(wt_dev, "commit", "-m", "feat: dev")

            with mock.patch.object(CodexExecutor, "execute", fake_codex_utf8):
                _run_cli(["branch", "verify", "dev-feat", "--base", "develop", "--project", str(repo)])

            land_dev = _run_cli(
                ["branch", "land", "dev-feat", "--base", "develop", "--project", str(repo), "--execute"]
            )
            self.assertTrue(land_dev["landed"])
            self.assertFalse(wt_dev.exists())
            self.assertEqual(land_dev["deleted_branch"], "task/dev-feat")
            self.assertEqual(_git(repo, "rev-parse", "main"), main_tip)
            self.assertTrue((repo / "dev.py").is_file())

    def test_round3_land_lock_gate_revalidation_linked_project_cleanliness_and_bounds(self) -> None:
        import keel_lib.branch_gate as bg
        from keel_lib.worktree import git_dirs, locked_branch_state

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "r3-repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            _git(repo, "config", "user.name", "Test")
            _git(repo, "config", "user.email", "test@example.com")
            (repo / "app.py").write_text("X = 1\n", encoding="utf-8")
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-m", "init")
            base_sha = _git(repo, "rev-parse", "main")

            wt_link = Path(_run_cli(["worktree", "create", "linked", "--project", str(repo)])["path"])
            (wt_link / "app.py").write_text("X = 2\n", encoding="utf-8")
            _git(wt_link, "add", "app.py")
            _git(wt_link, "commit", "-m", "feat: linked")
            head_sha = _git(wt_link, "rev-parse", "HEAD")

            # 1. `--task-file` path traversal (`..` outside repo) and partial `--old-base`/`--old-head` rejected!
            with self.assertRaisesRegex(KeelError, "outside repository"):
                _run_cli(["branch", "verify", "linked", "--project", str(repo), "--task-file", "../../outside.md"])
            with self.assertRaisesRegex(KeelError, "--old-base and --old-head must both be provided"):
                _run_cli(["branch", "land", "linked", "--project", str(repo), "--old-base", base_sha])
            with self.assertRaisesRegex(KeelError, "--old-base and --old-head must both be provided"):
                _run_cli(["branch", "land", "linked", "--project", str(repo), "--old-head", head_sha])

            # 2. Passing `--project <wt_link>` (pointing directly at the linked worktree!) MUST still enforce
            # clean `git status --porcelain -uall` on `<wt_link>` for BOTH `verify` and `land`!
            dirty_file = wt_link / "untracked_dirty.txt"
            dirty_file.write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(KeelError, "uncommitted or untracked changes"):
                _run_cli(["branch", "verify", "linked", "--project", str(wt_link)])
            dirty_file.unlink()

            def fake_codex_approve(self_obj: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "verdict": "approve",
                            "summary": "Clean!",
                            "blocking": [],
                            "notes": [],
                            "commands_run": ["git status"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": "codex", "model": "gpt-6-astra", "effort": "max"}

            with mock.patch.object(CodexExecutor, "execute", fake_codex_approve):
                v_link = _run_cli(["branch", "verify", "linked", "--project", str(wt_link)])
            self.assertEqual(v_link["status"], "completed")

            dirty_file.write_text("dirty before land\n", encoding="utf-8")
            with self.assertRaisesRegex(KeelError, "uncommitted or untracked changes"):
                _run_cli(["branch", "land", "linked", "--project", str(wt_link), "--execute"])
            dirty_file.unlink()

            # 3. Full gate revalidation inside `land.lock` critical section: if a concurrent failed `checks_by_sha`
            # or `pending_verifiers` entry arrives after initial `evaluate_branch_gate`, `land --execute` aborts!
            common_dir, _ = git_dirs(repo)
            orig_extract = bg.extract_commit_trailers

            def inject_concurrent_failed_check(proj_arg: Path, rev_range: str) -> list[str]:
                res_t = orig_extract(proj_arg, rev_range)
                with locked_branch_state(common_dir, "task/linked") as st:
                    st.setdefault("checks_by_sha", {})[head_sha] = {
                        "configured": True,
                        "ok": False,
                        "skipped": False,
                        "commands": [{"command": "false", "returncode": 1, "ok": False, "output_tail": "failed"}],
                    }
                return res_t

            with mock.patch.object(bg, "extract_commit_trailers", inject_concurrent_failed_check):
                with self.assertRaisesRegex(KeelError, "Deterministic checks failed"):
                    _run_cli(["branch", "land", "linked", "--project", str(wt_link), "--execute"])
            self.assertEqual(_git(repo, "rev-parse", "main"), base_sha)
            self.assertTrue(wt_link.is_dir())

            # Clear failed check and verify concurrent `pending_verifiers` ALSO aborts inside critical section!
            with locked_branch_state(common_dir, "task/linked") as st:
                st["checks_by_sha"].pop(head_sha, None)

            def inject_concurrent_pending(proj_arg: Path, rev_range: str) -> list[str]:
                res_t = orig_extract(proj_arg, rev_range)
                with locked_branch_state(common_dir, "task/linked") as st:
                    st.setdefault("pending_verifiers", {})[head_sha] = ["agy"]
                return res_t

            with mock.patch.object(bg, "extract_commit_trailers", inject_concurrent_pending):
                with self.assertRaisesRegex(KeelError, "Pending independent verifier"):
                    _run_cli(["branch", "land", "linked", "--project", str(wt_link), "--execute"])
            with locked_branch_state(common_dir, "task/linked") as st:
                st["pending_verifiers"].pop(head_sha, None)

            # 4. Clean `land --project <wt_link> --execute` resolves `primary_root` (`repo`), acquires `land.lock`,
            # merges onto `main`, and removes `<wt_link>` cleanly!
            land_ok = _run_cli(["branch", "land", "linked", "--project", str(wt_link), "--execute"])
            self.assertTrue(land_ok["landed"])
            self.assertFalse(wt_link.exists())
            self.assertTrue((common_dir / "keel-state" / "land.lock").is_file())
            self.assertEqual((repo / "app.py").read_text(encoding="utf-8"), "X = 2\n")


if __name__ == "__main__":
    unittest.main()
