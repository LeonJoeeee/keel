from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from typing import Any
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from keel_lib import KeelError  # noqa: E402
from keel_lib.branch_gate import compare_rebase, validate_verdict_payload  # noqa: E402
from keel_lib.executors.agy import AgyExecutor
from keel_lib.executors.base import KeelConfigError
from keel_lib.worktree import load_branch_state
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


def _create_feature_worktree(repo: Path) -> Path:
    _init_repo_with_task(repo)
    wt = Path(_run_cli(["worktree", "create", "feature", "--project", str(repo)])["path"])
    (wt / "calc.py").write_text("value = 7\n")
    _git(wt, "commit", "-am", "Update value")
    return wt


def verifier(outcome="approve"):
    def execute(adapter, req, defaults):
        payload = {"verdict": outcome, "summary": "Checked", "blocking": [], "notes": [], "commands_run": []}
        if outcome == "request_changes":
            payload["blocking"] = ["Fix the implementation"]
        req.output_path.write_text(json.dumps(payload))
        return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-model"}
    return execute


class BranchGateTests(unittest.TestCase):
    def setUp(self) -> None:
        # A routing regression must never fall through to a real vendor process.
        for adapter in ("agy", "codex"):
            self.enterContext(mock.patch(
                f"keel_lib.executors.{adapter}.run_process_group", side_effect=AssertionError("unexpected CLI launch"),
            ))

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

            # Repo-wide `keel branch status` overview
            overview = _run_cli(["branch", "status", "--project", str(repo)])
            self.assertEqual(overview["todo_tasks"], ["docs/tasks/todo/20261117-backlog.md"])
            self.assertEqual(overview["active_tasks_on_base"], ["docs/tasks/active/20261117-mul.md"])
            self.assertEqual(len(overview["worktrees"]), 1)
            self.assertFalse(overview["worktrees"][0]["gate"]["ready_to_land"])

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
                return {"status": "completed", "executor": "codex", "model": "test-codex", "effort": "max"}

            with (mock.patch.object(CodexExecutor, "execute", fake_codex_rc),
                  mock.patch.object(AgyExecutor, "execute", verifier("request_changes"))):
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
            self.assertIs(dual_r1["forced_round"], False)
            self.assertEqual(set(dual_r1["verifiers"]), {"agy", "codex"})
            self.assertIs(load_branch_state(repo / ".git", "task/mul")["rounds"][-1]["forced_round"], False)
            self.assertFalse(_run_cli(["branch", "status", "mul", "--project", str(repo)])["ready_to_land"])

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
                return {"status": "completed", "executor": "codex", "model": "test-codex", "effort": "max"}

            with (mock.patch.object(CodexExecutor, "execute", fake_codex_approve),
                  mock.patch.object(AgyExecutor, "execute", verifier())):
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

            self.assertEqual(dual_r2["outcome"], "approve")

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

    def test_partial_check_reruns_preserve_requirements_across_rebase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            wt = _create_feature_worktree(repo)
            args = ["feature", "--project", str(repo)]
            first, second = "test -f calc.py", "test -f AGENTS.md"
            old_base = _git(repo, "rev-parse", "main")
            old_head = _git(wt, "rev-parse", "HEAD")
            with mock.patch.object(CodexExecutor, "execute", verifier()):
                _run_cli(["branch", "verify", *args, "--check", first, "--check", second])
                _run_cli(["branch", "verify", *args, "--check", first])
            status = _run_cli(["branch", "status", *args])
            self.assertCountEqual([c["command"] for c in status["checks"]["commands"]], [first, second])
            gate = _run_cli(["branch", "land", *args, "--check", second])
            self.assertTrue(gate["ready_to_land"])
            self.assertCountEqual([c["command"] for c in gate["checks"]["commands"]], [first, second])

            (repo / "README.md").write_text("New base\n")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-m", "Document project")
            _git(wt, "rebase", "main")
            land = ["branch", "land", *args, "--old-base", old_base, "--old-head", old_head]
            partial = _run_cli([*land, "--check", second])
            self.assertFalse(partial["ready_to_land"])
            self.assertIn(first, " ".join(partial["blockers"]))
            complete = _run_cli([*land, "--check", first])
            self.assertTrue(complete["ready_to_land"], complete["blockers"])
            self.assertCountEqual([c["command"] for c in complete["checks"]["commands"]], [first, second])

    def test_partial_rerun_cannot_clear_a_failed_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_feature_worktree(repo)
            args = ["feature", "--project", str(repo)]
            verify = ["branch", "verify", *args]
            land = ["branch", "land", *args]
            check = 'test "$KEEL_TEST_RESULT" = pass'
            with mock.patch.object(CodexExecutor, "execute", verifier()):
                _run_cli(verify)
            with (mock.patch.dict(os.environ, {"KEEL_TEST_RESULT": "fail"}),
                  mock.patch.object(CodexExecutor, "execute", side_effect=AssertionError("unexpected review"))):
                with self.assertRaisesRegex(KeelError, "Deterministic check failed"):
                    _run_cli([*verify, "--check", check, "--check", "test -f calc.py"])
                with self.assertRaisesRegex(KeelError, "Deterministic check failed"):
                    _run_cli([*verify, "--check", "true"])
            gate = _run_cli([*land, "--check", "true"])
            self.assertFalse(gate["ready_to_land"])
            self.assertIn(check, " ".join(gate["blockers"]))
            self.assertFalse(gate["checks"]["ok"])
            self.assertEqual({c["command"]: c["ok"] for c in gate["checks"]["commands"]},
                             {check: False, "test -f calc.py": False, "true": True})
            with mock.patch.dict(os.environ, {"KEEL_TEST_RESULT": "pass"}):
                partial = _run_cli([*land, "--check", check])
            # The fail-fast batch never ran its second required command.
            self.assertFalse(partial["checks"]["ok"])
            fixed = _run_cli([*land, "--check", "test -f calc.py"])
            self.assertTrue(fixed["ready_to_land"], fixed["blockers"])
            self.assertTrue(fixed["checks"]["ok"])
            self.assertCountEqual([c["command"] for c in fixed["checks"]["commands"]], [check, "test -f calc.py", "true"])
            skipped = _run_cli([*land, "--skip-checks", "Owner accepted"])
            self.assertTrue(skipped["checks"]["skipped"])
            self.assertEqual(skipped["checks"]["skip_reason"], "Owner accepted")

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
                return {"status": "completed", "executor": "codex", "model": "test-codex", "effort": "max"}

            with mock.patch.object(CodexExecutor, "execute", fake_rc):
                r1 = _run_cli(["branch", "verify", "sub", "--project", str(repo)])
                r2 = _run_cli(["branch", "verify", "sub", "--project", str(repo)])
                r3 = _run_cli(["branch", "verify", "sub", "--project", str(repo)])
                self.assertEqual([r1["level"], r2["level"], r3["level"]], ["L3", "L4", "L4"])
                self.assertIs(r1["forced_round"], False)
                self.assertIs(r2["forced_round"], False)
                self.assertIs(r3["forced_round"], False)
                saved_rounds = load_branch_state(repo / ".git", "task/sub")["rounds"]
                self.assertEqual([entry["forced_round"] for entry in saved_rounds], [False, False, False])
                self.assertEqual([entry["level"] for entry in saved_rounds], ["L3", "L4", "L4"])
                # 4th round (after L3 -> L4 -> L4) blocked by tier-aware circuit breaker and must NOT suggest `--force-round`!
                with self.assertRaises(KeelError) as cap_ctx:
                    _run_cli(["branch", "verify", "sub", "--project", str(repo)])
                cap_msg = str(cap_ctx.exception)
                self.assertIn("convergence cap", cap_msg)
                self.assertNotIn("--force-round", cap_msg)
                self.assertNotIn("force-round", cap_msg.lower())

            # CLI `--help` on `branch verify` documents `--force-round` as human-authorized escalation only
            help_buf = io.StringIO()
            with contextlib.redirect_stdout(help_buf):
                with self.assertRaises(SystemExit) as help_exit:
                    KEEL_CLI.build_parser().parse_args(["branch", "verify", "--help"])
            self.assertEqual(help_exit.exception.code, 0)
            help_text = help_buf.getvalue()
            self.assertIn("--force-round", help_text)
            self.assertIn("human-authorized", help_text.lower())

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
                return {"status": "completed", "executor": "codex", "model": "test-codex", "effort": "max"}

            with mock.patch.object(CodexExecutor, "execute", fake_ok):
                forced_res = _run_cli(
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
            self.assertIs(forced_res["forced_round"], True)
            self.assertIs(load_branch_state(repo / ".git", "task/sub")["rounds"][-1]["forced_round"], True)

            self.assertTrue(_run_cli(["branch", "land", "sub", "--project", str(repo)])["ready_to_land"])

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

            for args in (["--skip-checks", "other checks passed"], ["--check", "true"]):
                with self.subTest(args=args):
                    gate = _run_cli(["branch", "land", "sub", "--project", str(repo), "--old-base", old_base,
                                     "--old-head", old_head, *args])
                    self.assertFalse(gate["ready_to_land"])
                    self.assertTrue(any("requires re-running" in reason for reason in gate["blockers"]))

            # Current-head `request_changes` MUST strictly block landing even when `--old-base`/`--old-head` was approved!
            with mock.patch.object(CodexExecutor, "execute", fake_rc):
                _run_cli(["branch", "verify", "sub", "--project", str(repo), "--force-round"])
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
            with mock.patch.object(AgyExecutor, "execute", verifier()):
                _run_cli(["branch", "verify", "feat/my-fix", "--executor", "agy", "--project", str(repo)])
            landed_custom = _run_cli(["branch", "land", "feat/my-fix", "--project", str(repo), "--execute"])
            self.assertTrue(landed_custom["landed"])
            self.assertFalse(wt_custom.exists())
            self.assertEqual(landed_custom["deleted_branch"], "feat/my-fix")

    def test_split_dual_verdict_blocks_landing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_feature_worktree(repo)
            args = ["feature", "--project", str(repo)]
            for codex, agy in (("approve", "request_changes"), ("request_changes", "approve")):
                with (self.subTest(codex=codex, agy=agy),
                      mock.patch.object(CodexExecutor, "execute", verifier(codex)),
                      mock.patch.object(AgyExecutor, "execute", verifier(agy))):
                    result = _run_cli(["branch", "verify", *args, "--dual"])
                    self.assertEqual(result["outcome"], "request_changes")
                    gate = _run_cli(["branch", "land", *args])
                    self.assertEqual({k: v["verdict"] for k, v in gate["verdicts"].items()},
                                     {"codex": codex, "agy": agy})
                    self.assertFalse(gate["ready_to_land"])

    def test_dual_workers_overlap_and_failed_round_preserves_state(self) -> None:
        import keel_lib.branch_gate as bg
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "space repo"
            head, _ = _init_repo_with_task(repo)
            _git(repo, "branch", "task/parallel")
            barrier = threading.Barrier(2, timeout=5)
            checkouts = {}
            invalid = False

            def check(adapter, req, defaults):
                checkouts[adapter.name] = req.workdir
                self.assertEqual(_git(req.workdir, "rev-parse", "HEAD"), head)
                self.assertEqual(_git(req.workdir, "remote"), "")
                self.assertTrue(req.scratch_verify)
                self.assertEqual(req.level, "L3")
                barrier.wait()
                result = verifier()(adapter, req, defaults)
                if invalid and adapter.name == "agy":
                    req.output_path.write_text("{}")
                return result

            with (mock.patch.object(CodexExecutor, "execute", check),
                  mock.patch.object(AgyExecutor, "execute", check)):
                args = dict(project=repo, branch_or_slug="parallel", defaults=KEEL_CLI.load_defaults(), dual=True)
                for key in ("executor", "model", "effort"):
                    with self.subTest(key=key), self.assertRaises(KeelConfigError):
                        bg.verify_branch(**args, **{key: "codex"})
                result = bg.verify_branch(**args)
                self.assertEqual(result["outcome"], "approve")
                self.assertNotEqual(checkouts["agy"], checkouts["codex"])
                self.assertFalse(any(path.parent.exists() for path in checkouts.values()))
                before = load_branch_state(repo / ".git", "task/parallel")["rounds"]
                self.assertEqual(len(before), 1)
                self.assertEqual(set(before[0]["verdicts"]), {"agy", "codex"})
                retained = Path(tmp) / "evidence"
                bg.verify_branch(**args, output_dir=str(retained))
                for name in ("codex", "agy"):
                    self.assertTrue((retained / name / "checkout").is_dir())
                    self.assertEqual(json.loads((retained / name / "verdict.json").read_text())["verdict"], "approve")
                before = load_branch_state(repo / ".git", "task/parallel")["rounds"]
                invalid = True
                with self.assertRaisesRegex(KeelError, "required properties"):
                    bg.verify_branch(**args)
                self.assertEqual(load_branch_state(repo / ".git", "task/parallel")["rounds"], before)
                self.assertFalse(any(path.parent.exists() for path in checkouts.values()))

    def test_flat_ledger_retains_legacy_review_evidence(self) -> None:
        from keel_lib.worktree import save_branch_state

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            head, _ = _init_repo_with_task(repo)
            _git(repo, "branch", "topic")
            detail = {"verdict": "approve", "summary": "Original review", "blocking": [],
                      "notes": ["Original note"], "commands_run": ["git diff"]}
            save_branch_state(repo / ".git", "topic", {
                "branch": "topic", "checks_by_sha": {}, "verdicts_by_sha": {head: {"agy": detail}},
                "rounds": [{"head_sha": head, "outcome": "approve", "dual": False, "verdicts": {"agy": "approve"}}],
            })
            status = _run_cli(["branch", "status", "topic", "--project", str(repo)])
            self.assertEqual(status["verdicts"]["agy"], detail)
            with mock.patch.object(CodexExecutor, "execute", verifier()):
                _run_cli(["branch", "verify", "topic", "--project", str(repo)])
            saved = load_branch_state(repo / ".git", "topic")
            self.assertEqual(saved["rounds"][0]["verdicts"]["agy"], detail)
            self.assertNotIn("verdicts_by_sha", saved)

    def test_new_verdict_during_land_checks_is_preserved(self) -> None:
        import keel_lib.branch_gate as bg

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo_with_task(repo)
            wt = Path(_run_cli(["worktree", "create", "quick", "--project", str(repo)])["path"])
            (wt / "calc.py").write_text("value = 7\n")
            _git(wt, "commit", "-am", "Update value")
            verify = ["branch", "verify", "quick", "--project", str(repo)]
            land = ["branch", "land", "quick", "--project", str(repo), "--execute"]
            with mock.patch.object(CodexExecutor, "execute", verifier()):
                _run_cli(verify)
            real_checks = bg.run_deterministic_checks

            def checks(project, sha, commands):
                result = real_checks(project, sha, commands)
                _run_cli(verify)
                return result

            with (mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                  mock.patch.object(bg, "run_deterministic_checks", checks)):
                with self.assertRaisesRegex(KeelError, "request_changes"):
                    _run_cli([*land, "--check", "true"])
                r2 = _run_cli(verify)
                r3 = _run_cli(verify)
                self.assertEqual((r2["level"], r3["level"]), ("L4", "L4"))
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(verify)
            with mock.patch.object(CodexExecutor, "execute", verifier()):
                _run_cli([*verify, "--force-round"])
            landed = _run_cli(land)
            self.assertTrue(landed["landed"])
            self.assertIsNone(landed["archived_task"])
            self.assertFalse(wt.exists())

    def test_write_routing_review_inheritance_and_rework_on_run_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo)
            wt = Path(_run_cli(["worktree", "create", "alpha", "--project", str(repo)])["path"])
            project = ["--project", str(repo)]
            run = ["agent", "run", *project, "--cwd", str(wt), "--why", "Test", "--task", "Implement",
                   "--mode", "write", "--run-id", "writer"]
            resume = ["agent", "resume", "writer", *project, "--message", "Rework"]
            verify = ["branch", "verify", "alpha", *project]
            def state():
                return load_branch_state(repo / ".git", "task/alpha")

            outcome = "approve"

            def execute(adapter, req, defaults):
                if req.preset == "verify":
                    return verifier(outcome)(adapter, req, defaults)
                return {"status": "completed", "output_path": str(req.output_path), "level": req.level}
            def continue_write(adapter, run_dir, record, instruction, output, defaults, **kwargs):
                return {"status": "completed", "level": kwargs["level"] or record["level"]}

            with (mock.patch.object(AgyExecutor, "execute", execute), mock.patch.object(CodexExecutor, "execute", execute),
                  mock.patch.object(AgyExecutor, "resume", continue_write)):
                manual = _run_cli(verify)
                self.assertEqual((manual["executor"], manual["level"]), ("codex", "L3"))
                for writer, tier, reviewer, expected in (
                    ("agy", "L1", "codex", "L2"), ("codex", "L3", "agy", "L3"), ("agy", "L2", "codex", "L2"),
                ):
                    _run_cli([*run, "--executor", writer, "--level", tier])
                    review = _run_cli(verify)
                    self.assertEqual((review["executor"], review["level"]), (reviewer, expected))
                    self.assertEqual(state()["rounds"][-1]["implement_level"], tier)
                    with self.assertRaisesRegex(KeelError, "level"):
                        _run_cli([*verify, "--level", "L1" if tier != "L3" else "L2"])
                outcome = "request_changes"
                _run_cli([*verify, "--level", "L4"])
                for cmd in (run, resume):
                    with self.assertRaisesRegex(KeelError, "L3"):
                        _run_cli([*cmd, "--level", "L2"])
                previous_write = state()["last_write"]
                with mock.patch.object(AgyExecutor, "resume", side_effect=KeelError("executor failed")):
                    with self.assertRaisesRegex(KeelError, "executor failed"):
                        _run_cli([*resume, "--level", "L4"])
                self.assertEqual(state()["last_write"], previous_write)
                _run_cli([*resume, "--level", "L3"])
                _run_cli([*run, "--level", "L3"])
                self.assertEqual(state()["last_write"]["level"], "L3")
                _run_cli(verify)
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli(resume)
                _run_cli([*resume, "--level", "L4"])
                self.assertEqual(state()["last_write"]["level"], "L4")
                for obsolete in ("pending_verifiers", "dual_required_by_sha", "verdicts_by_sha", "request_changes_rounds"):
                    self.assertNotIn(obsolete, state())

    def test_pinned_sha_custom_base_and_utf8_tasks(self) -> None:
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
                return {"status": "completed", "executor": "codex", "model": "test-codex", "effort": "max"}

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
                return {"status": "completed", "executor": "codex", "model": "test-codex", "effort": "max"}

            with mock.patch.object(CodexExecutor, "execute", fake_codex_approve):
                v_link = _run_cli(["branch", "verify", "linked", "--project", str(wt_link)])
            self.assertEqual(v_link["status"], "completed")

            dirty_file.write_text("dirty before land\n", encoding="utf-8")
            with self.assertRaisesRegex(KeelError, "uncommitted or untracked changes"):
                _run_cli(["branch", "land", "linked", "--project", str(wt_link), "--execute"])
            dirty_file.unlink()

            # 3. Full gate revalidation inside `land.lock` critical section: if a concurrent failed `checks_by_sha`
            # or rejecting round arrives after initial `evaluate_branch_gate`, `land --execute` aborts!
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

            # A completed rejection arriving after evaluation also blocks the merge.
            with locked_branch_state(common_dir, "task/linked") as st:
                st["checks_by_sha"].pop(head_sha)

            def inject_review(project, rev_range):
                with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                    _run_cli(["branch", "verify", "linked", "--project", str(repo)])
                return orig_extract(project, rev_range)

            with mock.patch.object(bg, "extract_commit_trailers", inject_review):
                with self.assertRaisesRegex(KeelError, "request_changes"):
                    _run_cli(["branch", "land", "linked", "--project", str(wt_link), "--execute"])
            with mock.patch.object(CodexExecutor, "execute", verifier()):
                _run_cli(["branch", "verify", "linked", "--project", str(repo)])

            # 4. Clean `land --project <wt_link> --execute` resolves `primary_root` (`repo`), acquires `land.lock`,
            # merges onto `main`, and removes `<wt_link>` cleanly!
            land_ok = _run_cli(["branch", "land", "linked", "--project", str(wt_link), "--execute"])
            self.assertTrue(land_ok["landed"])
            self.assertFalse(wt_link.exists())
            self.assertTrue((common_dir / "keel-state" / "land.lock").is_file())
            self.assertEqual((repo / "app.py").read_text(encoding="utf-8"), "X = 2\n")

    def test_skip_checks_preserves_rebase_rerun_and_failed_verify_retains_explicit_task_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            # Competing slug-matching task `20261117-worker-lane.md` MUST NOT override explicit `--task-file`!
            _init_repo_with_task(repo, slug="worker-lane")
            custom_task = repo / "docs" / "tasks" / "active" / "20261118-custom-spec.md"
            custom_task.write_text("# Task: Custom\n\n## Bounds\n- LOCKED_CUSTOM_BOUND_TOKEN\n", encoding="utf-8")

            wt_info = _run_cli(["worktree", "create", "worker-lane", "--project", str(repo)])
            wt = Path(wt_info["path"])
            (wt / "app.py").write_text("VALUE = 9\n", encoding="utf-8")
            _git(wt, "add", "app.py")
            _git(wt, "commit", "-m", "feat: initial draft")

            # Uncommitted working-tree --task-file (exists on disk, not in Git) MUST be rejected!
            with self.assertRaisesRegex(KeelError, "not committed"):
                _run_cli(
                    [
                        "branch",
                        "verify",
                        "worker-lane",
                        "--project",
                        str(repo),
                        "--task-file",
                        "docs/tasks/active/20261118-custom-spec.md",
                    ]
                )
            _git(repo, "add", "docs/tasks/active/20261118-custom-spec.md")
            _git(repo, "commit", "-m", "docs(tasks): open custom spec")
            old_base = _git(repo, "rev-parse", "main")
            _git(wt, "rebase", "main")

            # Missing or directory --task-file rejected without poisoning branch state
            for bad_task in ("docs/tasks/active/nonexistent.md", "docs/tasks/active"):
                with self.assertRaisesRegex(KeelError, "not committed"):
                    _run_cli(
                        [
                            "branch",
                            "verify",
                            "worker-lane",
                            "--project",
                            str(repo),
                            "--task-file",
                            bad_task,
                        ]
                    )

            check_cmd = "grep -q 'VALUE = 10' app.py"

            # First verify with explicit --task-file fails on --check, yet persists task_file association!
            with self.assertRaisesRegex(KeelError, "Deterministic check failed"):
                _run_cli(
                    [
                        "branch",
                        "verify",
                        "worker-lane",
                        "--project",
                        str(repo),
                        "--task-file",
                        "docs/tasks/active/20261118-custom-spec.md",
                        "--check",
                        check_cmd,
                    ]
                )

            # Author commits the fix on the branch and retries WITHOUT --task-file
            (wt / "app.py").write_text("VALUE = 10\n", encoding="utf-8")
            _git(wt, "add", "app.py")
            _git(wt, "commit", "-m", "fix: set VALUE = 10")
            old_head = _git(wt, "rev-parse", "HEAD")

            captured_briefs: list[str] = []

            def fake_codex(self_obj: Any, req: Any, defaults: Any) -> dict[str, Any]:
                captured_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                req.output_path.write_text(
                    json.dumps(
                        {
                            "verdict": "approve",
                            "summary": "Approved",
                            "blocking": [],
                            "notes": [],
                            "commands_run": ["true"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": "codex", "model": "test-codex", "effort": "high"}

            # Retry WITHOUT --task-file retains custom task spec in brief and passes check_cmd
            with mock.patch.object(CodexExecutor, "execute", fake_codex):
                v_ok = _run_cli(
                    [
                        "branch",
                        "verify",
                        "worker-lane",
                        "--project",
                        str(repo),
                        "--check",
                        check_cmd,
                    ]
                )
            self.assertEqual(v_ok["task_file"], "docs/tasks/active/20261118-custom-spec.md")
            self.assertIn("LOCKED_CUSTOM_BOUND_TOKEN", captured_briefs[0])

            # Dry-run land with --skip-checks on old_head MUST NOT erase check_cmd from old_head's command history
            skip_dry = _run_cli(
                [
                    "branch",
                    "land",
                    "worker-lane",
                    "--project",
                    str(repo),
                    "--skip-checks",
                    "Already passed on old_head",
                ]
            )
            self.assertTrue(skip_dry["ready_to_land"])

            # Advance `main` and pure-rebase `task/worker-lane`
            (repo / "README.txt").write_text("main advance\n", encoding="utf-8")
            _git(repo, "add", "README.txt")
            _git(repo, "commit", "-m", "docs: readme")
            _git(wt, "rebase", "main")

            # Without rerunning check_cmd on the rebased SHA, land dry-run MUST block!
            rebased_dry = _run_cli(
                [
                    "branch",
                    "land",
                    "worker-lane",
                    "--project",
                    str(repo),
                    "--old-base",
                    old_base,
                    "--old-head",
                    old_head,
                ]
            )
            self.assertFalse(rebased_dry["ready_to_land"])
            self.assertTrue(any(check_cmd in b for b in rebased_dry["blockers"]))

            # Even if a fresh verify (without --check) approves the rebased SHA, passing --old-base/--old-head
            # STILL enforces rerunning old_head's deterministic checks (and rejects abbreviated --old-head)!
            with mock.patch.object(CodexExecutor, "execute", fake_codex):
                _run_cli(["branch", "verify", "worker-lane", "--project", str(repo)])
            with self.assertRaisesRegex(KeelError, "full 40/64-character commit SHA"):
                _run_cli(
                    [
                        "branch",
                        "land",
                        "worker-lane",
                        "--project",
                        str(repo),
                        "--old-base",
                        old_base,
                        "--old-head",
                        old_head[:12],
                    ]
                )
            rebased_after_verify = _run_cli(
                [
                    "branch",
                    "land",
                    "worker-lane",
                    "--project",
                    str(repo),
                    "--old-base",
                    old_base,
                    "--old-head",
                    old_head,
                ]
            )
            self.assertFalse(rebased_after_verify["ready_to_land"])
            self.assertTrue(any(check_cmd in b for b in rebased_after_verify["blockers"]))

            # Switching --task-file to another task (`20261117-worker-lane.md`) and failing in the verifier
            # MUST NOT allow landing that other task under `20261118-custom-spec.md`'s approval!
            def fail_verifier(self_obj: Any, req: Any, defaults: Any) -> dict[str, Any]:
                raise KeelError("simulated verifier outage")

            with mock.patch.object(CodexExecutor, "execute", fail_verifier):
                with self.assertRaisesRegex(KeelError, "simulated verifier outage"):
                    _run_cli(
                        [
                            "branch",
                            "verify",
                            "worker-lane",
                            "--project",
                            str(repo),
                            "--task-file",
                            "docs/tasks/active/20261117-worker-lane.md",
                        ]
                    )
            unverified_other = _run_cli(["branch", "land", "worker-lane", "--project", str(repo)])
            self.assertEqual(unverified_other["task_file"], "docs/tasks/active/20261117-worker-lane.md")
            self.assertFalse(unverified_other["ready_to_land"])

            # Rerunning check_cmd with --task-file pointing back to the approved custom spec lands and archives it!
            landed = _run_cli(
                [
                    "branch",
                    "land",
                    "worker-lane",
                    "--project",
                    str(repo),
                    "--task-file",
                    "docs/tasks/active/20261118-custom-spec.md",
                    "--old-base",
                    old_base,
                    "--old-head",
                    old_head,
                    "--check",
                    check_cmd,
                    "--execute",
                ]
            )
            self.assertTrue(landed["landed"])
            self.assertEqual(landed["archived_task"], "docs/tasks/archive/20261118-custom-spec.md")

            # Reusing the same branch slug `worker-lane` after squash-landing picks up the remaining/new active task
            # (`20261117-worker-lane.md`) rather than the archived `20261118-custom-spec.md`!
            wt2_info = _run_cli(["worktree", "create", "worker-lane", "--project", str(repo)])
            wt2 = Path(wt2_info["path"])
            (wt2 / "app.py").write_text("VALUE = 20\n", encoding="utf-8")
            _git(wt2, "add", "app.py")
            _git(wt2, "commit", "-m", "feat: second worker-lane slice")
            sha2 = _git(wt2, "rev-parse", "HEAD")

            captured_briefs.clear()
            with mock.patch.object(CodexExecutor, "execute", fake_codex):
                v_reused = _run_cli(["branch", "verify", "worker-lane", "--project", str(repo)])
            self.assertEqual(v_reused["task_file"], "docs/tasks/active/20261117-worker-lane.md")
            self.assertEqual(v_reused["request_changes_rounds"], 0)

            # Open a second active task B (`20261118-other.md`) on `main` and rebase `worker-lane`
            other_task = repo / "docs" / "tasks" / "active" / "20261118-other.md"
            other_task.write_text("# Task: Other\n\n## Bounds\n- OTHER_TOKEN\n", encoding="utf-8")
            _git(repo, "add", "docs/tasks/active/20261118-other.md")
            _git(repo, "commit", "-m", "docs(tasks): open other spec")
            _git(wt2, "rebase", "main")
            sha3 = _git(wt2, "rev-parse", "HEAD")

            # 1) Legacy pre-upgrade round lacking `"task_file"` retains its original `state["task_file"]` provenance
            # even when a subsequent verify under `--task-file .../20261118-other.md` fails!
            from keel_lib.worktree import save_branch_state

            save_branch_state(
                repo / ".git",
                "task/worker-lane",
                {
                    "branch": "task/worker-lane",
                    "task_file": "docs/tasks/active/20261117-worker-lane.md",
                    "checks_by_sha": {},
                    "rounds": [
                        {
                            "head_sha": sha3,
                            "outcome": "approve",
                            "dual": False,
                            "verdicts": {
                                "codex": {
                                    "verdict": "approve",
                                    "summary": "Legacy ok",
                                    "blocking": [],
                                    "notes": [],
                                    "commands_run": ["true"],
                                }
                            },
                        }
                    ],
                },
            )
            with mock.patch.object(CodexExecutor, "execute", fail_verifier):
                with self.assertRaisesRegex(KeelError, "simulated verifier outage"):
                    _run_cli(
                        [
                            "branch",
                            "verify",
                            "worker-lane",
                            "--project",
                            str(repo),
                            "--task-file",
                            "docs/tasks/active/20261118-other.md",
                        ]
                    )
            legacy_switch_dry = _run_cli(["branch", "land", "worker-lane", "--project", str(repo)])
            self.assertEqual(legacy_switch_dry["task_file"], "docs/tasks/active/20261118-other.md")
            self.assertFalse(legacy_switch_dry["ready_to_land"])

            # 2) A later `request_changes` round on the same SHA overrides an earlier `approve` round even if
            # `--task-file` points back to the earlier round's task!
            def fake_reject(self_obj: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "verdict": "request_changes",
                            "summary": "Found bug",
                            "blocking": ["Real regression"],
                            "notes": [],
                            "commands_run": ["pytest"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": "codex", "model": "test-codex", "effort": "high"}

            with mock.patch.object(CodexExecutor, "execute", fake_reject):
                _run_cli(
                    [
                        "branch",
                        "verify",
                        "worker-lane",
                        "--project",
                        str(repo),
                        "--task-file",
                        "docs/tasks/active/20261118-other.md",
                    ]
                )
            override_attempt = _run_cli(
                [
                    "branch",
                    "land",
                    "worker-lane",
                    "--project",
                    str(repo),
                    "--task-file",
                    "docs/tasks/active/20261117-worker-lane.md",
                ]
            )
            self.assertFalse(override_attempt["ready_to_land"])
            self.assertTrue(any("request_changes" in b for b in override_attempt["blockers"]))

            # 3) Approving `narrow-topic` against `--base develop` MUST NOT authorize landing onto `--base main`
            # when `main..develop` contains unreviewed changes (`merge_base_sha` mismatch)!
            _git(repo, "checkout", "-b", "develop", "main")
            (repo / "develop_only.txt").write_text("unreviewed\n", encoding="utf-8")
            _git(repo, "add", "develop_only.txt")
            _git(repo, "commit", "-m", "feat: develop only")
            _git(repo, "checkout", "main")

            wt_dev = Path(_run_cli(["worktree", "create", "narrow-topic", "--project", str(repo), "--base", "develop"])["path"])
            (wt_dev / "topic.txt").write_text("reviewed\n", encoding="utf-8")
            _git(wt_dev, "add", "topic.txt")
            _git(wt_dev, "commit", "-m", "feat: topic")
            with mock.patch.object(CodexExecutor, "execute", fake_codex):
                _run_cli(["branch", "verify", "narrow-topic", "--project", str(repo), "--base", "develop"])
            dev_status = _run_cli(["branch", "status", "narrow-topic", "--project", str(repo), "--base", "develop"])
            self.assertTrue(dev_status["ready_to_land"])
            land_onto_main = _run_cli(["branch", "land", "narrow-topic", "--project", str(repo), "--base", "main"])
            self.assertFalse(land_onto_main["ready_to_land"])
            self.assertTrue(any("merge-base" in b for b in land_onto_main["blockers"]))

            # 4) Task criteria blob binding (`task_blob_sha`):
            # (a) Same merge-base (`fork_point`), but `release` loosened `docs/tasks/active/20261118-other.md` while
            #     `main` has stricter bounds -> verifying `--base release` MUST NOT approve `--base main`!
            fork_point = _git(repo, "rev-parse", "main")
            _git(repo, "checkout", "-b", "release", fork_point)
            (repo / "docs/tasks/active/20261118-other.md").write_text("# Task: Other\n\n## Bounds\n- LOOSE_BOUNDS\n", encoding="utf-8")
            _git(repo, "add", "docs/tasks/active/20261118-other.md")
            _git(repo, "commit", "-m", "docs(tasks): loosen on release")
            _git(repo, "checkout", "main")

            wt_math = Path(_run_cli(["worktree", "create", "math-lane", "--project", str(repo), "--base", "main"])["path"])
            (wt_math / "probe.py").write_text("P = 1\n", encoding="utf-8")
            _git(wt_math, "add", "probe.py")
            _git(wt_math, "commit", "-m", "feat: probe")
            math_old_head = _git(wt_math, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", fake_codex):
                _run_cli(
                    [
                        "branch",
                        "verify",
                        "math-lane",
                        "--project",
                        str(repo),
                        "--base",
                        "release",
                        "--task-file",
                        "docs/tasks/active/20261118-other.md",
                    ]
                )
            # Notice `merge-base(release, math-lane) == fork_point == merge-base(main, math-lane)`,
            # yet `task_blob_sha` differs between `release` and `main`!
            cross_base_land = _run_cli(["branch", "land", "math-lane", "--project", str(repo), "--base", "main"])
            self.assertFalse(cross_base_land["ready_to_land"])
            self.assertTrue(any("task criteria" in b for b in cross_base_land["blockers"]))

            # (b) Approve against `main`, then revise `docs/tasks/active/20261118-other.md` on `main` and pure-rebase:
            #     rebase carry-over MUST block until fresh verification runs against the revised criteria!
            with mock.patch.object(CodexExecutor, "execute", fake_codex):
                _run_cli(
                    [
                        "branch",
                        "verify",
                        "math-lane",
                        "--project",
                        str(repo),
                        "--base",
                        "main",
                        "--task-file",
                        "docs/tasks/active/20261118-other.md",
                    ]
                )
            (repo / "docs/tasks/active/20261118-other.md").write_text("# Task: Other\n\n## Bounds\n- REVISED_STRICT_BOUNDS\n", encoding="utf-8")
            _git(repo, "add", "docs/tasks/active/20261118-other.md")
            _git(repo, "commit", "-m", "docs(tasks): tighten bounds on main")
            _git(wt_math, "rebase", "main")
            revised_rebase_land = _run_cli(
                [
                    "branch",
                    "land",
                    "math-lane",
                    "--project",
                    str(repo),
                    "--old-base",
                    fork_point,
                    "--old-head",
                    math_old_head,
                ]
            )
            self.assertFalse(revised_rebase_land["ready_to_land"])
            self.assertTrue(any("revised task criteria" in b for b in revised_rebase_land["blockers"]))

    def test_stacked_slices_incremental_verify_and_ordered_ff_only_land(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            m0_sha, base_task = _init_repo_with_task(repo, slug="unrelated")
            _git(repo, "rm", "-f", base_task)
            _git(repo, "commit", "-m", "docs(tasks): clear initial active task")
            m0_sha = _git(repo, "rev-parse", "HEAD")

            seen_briefs: list[str] = []

            def fake_codex(self_exec: Any, req: Any, defaults: dict[str, Any]) -> dict[str, Any]:
                seen_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                req.output_path.write_text(
                    json.dumps(
                        {
                            "verdict": "approve",
                            "summary": "Incremental slice verified",
                            "blocking": [],
                            "notes": [],
                            "commands_run": ["python3 -c 'import calc'"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": "codex", "level": req.level, "model": "test-codex", "effort": "high"}

            # 1) Three-slice stack (`slice-1 -> slice-2 -> slice-3`) while `main` stays untouched at `m0_sha`
            wt1 = Path(_run_cli(["worktree", "create", "slice-1", "--project", str(repo)])["path"])
            (wt1 / "calc.py").write_text(
                "def add(a: int, b: int) -> int:\n    return a + b\n\ndef sub(a: int, b: int) -> int:\n    return a - b\n",
                encoding="utf-8",
            )
            _git(wt1, "add", "calc.py")
            _git(wt1, "commit", "-m", "feat: slice 1 sub")
            s1_sha = _git(wt1, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", fake_codex):
                v1 = _run_cli(
                    [
                        "branch",
                        "verify",
                        "slice-1",
                        "--why",
                        "Verify sub() slice.",
                        "--project",
                        str(repo),
                        "--check",
                        "python3 -c 'import calc; assert calc.sub(5, 2) == 3'",
                    ]
                )
            self.assertEqual(v1["outcome"], "approve")
            self.assertEqual(_git(repo, "rev-parse", "main"), m0_sha)

            wt2 = Path(_run_cli(["worktree", "create", "slice-2", "--base", "task/slice-1", "--project", str(repo)])["path"])
            (wt2 / "mult.py").write_text("def mul(a: int, b: int) -> int:\n    return a * b\n", encoding="utf-8")
            _git(wt2, "add", "mult.py")
            _git(wt2, "commit", "-m", "feat: slice 2 mul")
            s2_sha = _git(wt2, "rev-parse", "HEAD")

            ctx2 = Path(tmp) / "slice2-ctx.md"
            ctx2.write_text("Bounds: mult.py only.\n", encoding="utf-8")
            with mock.patch.object(CodexExecutor, "execute", fake_codex):
                v2 = _run_cli(
                    [
                        "branch",
                        "verify",
                        "slice-2",
                        "--base",
                        "task/slice-1",
                        "--why",
                        "Verify mul() slice.",
                        "--context-file",
                        str(ctx2),
                        "--project",
                        str(repo),
                        "--check",
                        "python3 -c 'import mult; assert mult.mul(3, 4) == 12'",
                    ]
                )
            self.assertEqual(v2["outcome"], "approve")
            self.assertIn(f"git diff {s1_sha}..{s2_sha}", seen_briefs[-1])
            self.assertIn("Bounds: mult.py only.", seen_briefs[-1])
            self.assertNotIn("feat: slice 1 sub", seen_briefs[-1])

            # Leaf slice `slice-3` can even carry its own active task file on the top of the stack
            wt3 = Path(_run_cli(["worktree", "create", "slice-3", "--base", "task/slice-2", "--project", str(repo)])["path"])
            task3 = wt3 / "docs/tasks/active/20261119-slice-3.md"
            task3.parent.mkdir(parents=True, exist_ok=True)
            task3.write_text("# Slice 3\n\n## Goal & Why\nAdd div().\n\n## Bounds\ndiv.py\n\n## Done-check\npython3 -c 'import div'\n", encoding="utf-8")
            (wt3 / "div.py").write_text("def div(a: int, b: int) -> float:\n    return a / b\n", encoding="utf-8")
            _git(wt3, "add", ".")
            _git(wt3, "commit", "-m", "feat: slice 3 div")
            s3_sha = _git(wt3, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", fake_codex):
                v3 = _run_cli(
                    [
                        "branch",
                        "verify",
                        "slice-3",
                        "--base",
                        "task/slice-2",
                        "--project",
                        str(repo),
                        "--check",
                        "python3 -c 'import div; assert div.div(6, 2) == 3.0'",
                    ]
                )
            self.assertEqual(v3["outcome"], "approve")
            self.assertIn(f"git diff {s2_sha}..{s3_sha}", seen_briefs[-1])
            self.assertNotIn("feat: slice 2 mul", seen_briefs[-1])

            # Out-of-order land of `slice-2` or `slice-3` directly onto `main` is blocked (`merge-base` mismatch)
            for premature in ("slice-2", "slice-3"):
                blocked = _run_cli(["branch", "land", premature, "--project", str(repo)])
                self.assertFalse(blocked["ready_to_land"])
                self.assertTrue(any("merge-base" in b for b in blocked["blockers"]))

            # Mutating verified parent `task/slice-1` invalidates its approval until restored
            (wt1 / "extra.txt").write_text("unverified\n", encoding="utf-8")
            _git(wt1, "add", "extra.txt")
            _git(wt1, "commit", "-m", "chore: mutate parent")
            mutated_gate = _run_cli(["branch", "land", "slice-1", "--project", str(repo)])
            self.assertFalse(mutated_gate["ready_to_land"])
            _git(wt1, "reset", "--hard", s1_sha)

            # Ordered `--ff-only` on parent slices advances `main` cleanly (`M0 -> S1 -> S2`), then `slice-3` squash-lands & archives!
            land1 = _run_cli(["branch", "land", "slice-1", "--ff-only", "--project", str(repo), "--execute"])
            self.assertTrue(land1["landed"])
            self.assertEqual(_git(repo, "rev-parse", "main"), s1_sha)

            land2 = _run_cli(["branch", "land", "slice-2", "--ff-only", "--project", str(repo), "--execute"])
            self.assertTrue(land2["landed"])
            self.assertEqual(_git(repo, "rev-parse", "main"), s2_sha)

            land3 = _run_cli(["branch", "land", "slice-3", "--project", str(repo), "--execute"])
            self.assertTrue(land3["landed"])
            self.assertTrue(land3["approval_source"].startswith("direct-approve:"))
            self.assertEqual(land3["archived_task"], "docs/tasks/archive/20261119-slice-3.md")

            # 2) Recovery path when a stacked parent inherited an active task file from `main`:
            #    `--ff-only` adds an archival commit on `main`, so the child rebases `--onto main <parent_sha>`
            #    and lands via `--old-base <parent_sha> --old-head <child_old_sha>` + rerun `--check`!
            inherited_task = repo / "docs/tasks/active/20261119-parent-task.md"
            inherited_task.parent.mkdir(parents=True, exist_ok=True)
            inherited_task.write_text("# Parent\n\n## Goal & Why\nP.\n\n## Bounds\np.py\n\n## Done-check\ntrue\n", encoding="utf-8")
            _git(repo, "add", str(inherited_task.relative_to(repo)))
            _git(repo, "commit", "-m", "docs(tasks): open parent-task on main")

            wt_p = Path(_run_cli(["worktree", "create", "parent-task", "--project", str(repo)])["path"])
            (wt_p / "p.py").write_text("P = 1\n", encoding="utf-8")
            _git(wt_p, "add", "p.py")
            _git(wt_p, "commit", "-m", "feat: p")
            p_sha = _git(wt_p, "rev-parse", "HEAD")
            with mock.patch.object(CodexExecutor, "execute", fake_codex):
                _run_cli(["branch", "verify", "parent-task", "--project", str(repo), "--check", "python3 -c 'import p'"])

            wt_c = Path(_run_cli(["worktree", "create", "child-slice", "--base", "task/parent-task", "--project", str(repo)])["path"])
            (wt_c / "c.py").write_text("C = 2\n", encoding="utf-8")
            _git(wt_c, "add", "c.py")
            _git(wt_c, "commit", "-m", "feat: c")
            c_old_sha = _git(wt_c, "rev-parse", "HEAD")
            with mock.patch.object(CodexExecutor, "execute", fake_codex):
                _run_cli(
                    [
                        "branch",
                        "verify",
                        "child-slice",
                        "--base",
                        "task/parent-task",
                        "--why",
                        "Verify child slice.",
                        "--project",
                        str(repo),
                        "--check",
                        "python3 -c 'import c'",
                    ]
                )

            land_p = _run_cli(["branch", "land", "parent-task", "--ff-only", "--project", str(repo), "--execute"])
            self.assertTrue(land_p["landed"])
            self.assertEqual(land_p["archived_task"], "docs/tasks/archive/20261119-parent-task.md")
            self.assertNotEqual(_git(repo, "rev-parse", "main"), p_sha)

            preview_c = _run_cli(["branch", "land", "child-slice", "--project", str(repo)])
            self.assertFalse(preview_c["ready_to_land"])
            self.assertTrue(any("behind 'main' tip" in b for b in preview_c["blockers"]))

            _git(wt_c, "rebase", "--onto", "main", p_sha)
            land_c = _run_cli(
                [
                    "branch",
                    "land",
                    "child-slice",
                    "--project",
                    str(repo),
                    "--old-base",
                    p_sha,
                    "--old-head",
                    c_old_sha,
                    "--check",
                    "python3 -c 'import c'",
                    "--execute",
                ]
            )
            self.assertTrue(land_c["landed"])
            self.assertTrue(land_c["approval_source"].startswith("pure-rebase-from:"))

            # 3) Non-trivial stacked specs locked in `docs/tasks/todo/` on `main` + `--task-file`:
            #    preserves `Locked Task Specification` (`P2`/`R2`) AND fast-forwards `main` to exact parent SHA without rebase!
            todo1_rel = "docs/tasks/todo/20261119-todo-parent.md"
            todo2_rel = "docs/tasks/todo/20261119-todo-child.md"
            (repo / todo1_rel).write_text("# Todo 1\n\n## Goal & Why\nT1.\n\n## Bounds\nt1.py only.\n\n## Done-check\npython3 -c 'import t1'\n", encoding="utf-8")
            (repo / todo2_rel).write_text("# Todo 2\n\n## Goal & Why\nT2.\n\n## Bounds\nt2.py only.\n\n## Done-check\npython3 -c 'import t2'\n", encoding="utf-8")
            _git(repo, "add", todo1_rel, todo2_rel)
            _git(repo, "commit", "-m", "docs(tasks): lock stacked todo specs on main")

            wt_t1 = Path(_run_cli(["worktree", "create", "todo-parent", "--project", str(repo)])["path"])
            (wt_t1 / "t1.py").write_text("T1 = 1\n", encoding="utf-8")
            _git(wt_t1, "add", "t1.py")
            _git(wt_t1, "commit", "-m", "feat: t1")
            t1_sha = _git(wt_t1, "rev-parse", "HEAD")
            with mock.patch.object(CodexExecutor, "execute", fake_codex):
                _run_cli(["branch", "verify", "todo-parent", "--task-file", todo1_rel, "--project", str(repo), "--check", "python3 -c 'import t1'"])
            self.assertIn("### Locked Task Specification on `main`", seen_briefs[-1])
            self.assertIn("t1.py only.", seen_briefs[-1])

            wt_t2 = Path(_run_cli(["worktree", "create", "todo-child", "--base", "task/todo-parent", "--project", str(repo)])["path"])
            (wt_t2 / "t2.py").write_text("T2 = 2\n", encoding="utf-8")
            _git(wt_t2, "add", "t2.py")
            _git(wt_t2, "commit", "-m", "feat: t2")
            with mock.patch.object(CodexExecutor, "execute", fake_codex):
                _run_cli(
                    [
                        "branch",
                        "verify",
                        "todo-child",
                        "--base",
                        "task/todo-parent",
                        "--task-file",
                        todo2_rel,
                        "--project",
                        str(repo),
                        "--check",
                        "python3 -c 'import t2'",
                    ]
                )
            self.assertIn("### Locked Task Specification on `task/todo-parent`", seen_briefs[-1])
            self.assertIn("t2.py only.", seen_briefs[-1])

            land_t1 = _run_cli(["branch", "land", "todo-parent", "--ff-only", "--project", str(repo), "--execute"])
            self.assertTrue(land_t1["landed"])
            self.assertIsNone(land_t1["archived_task"])
            self.assertEqual(_git(repo, "rev-parse", "main"), t1_sha)

            land_t2 = _run_cli(["branch", "land", "todo-child", "--ff-only", "--project", str(repo), "--execute"])
            self.assertTrue(land_t2["landed"])
            self.assertTrue(land_t2["approval_source"].startswith("direct-approve:"))

    def test_tier_aware_convergence_cap_l2_l3_l4_and_status_consistency(self) -> None:
        from keel_lib.branch_gate import evaluate_branch_gate

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="ladder")
            project = ["--project", str(repo)]

            def write_exec(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                if req.preset == "verify":
                    return verifier("request_changes")(adapter, req, defaults)
                return {"status": "completed", "output_path": str(req.output_path), "level": req.level}

            # 1) L2 start full ladder: L2 -> L3 -> L4 -> L4 (4 rounds), 5th blocked, --force-round recovers
            wt_l2 = Path(_run_cli(["worktree", "create", "l2-start", *project])["path"])
            (wt_l2 / "calc.py").write_text("V = 1\n", encoding="utf-8")
            _git(wt_l2, "commit", "-am", "feat: l2 start")
            run_l2 = ["agent", "run", *project, "--cwd", str(wt_l2), "--why", "Ladder", "--task", "Fix", "--mode", "write"]
            verify_l2 = ["branch", "verify", "l2-start", *project]

            with (mock.patch.object(AgyExecutor, "execute", write_exec),
                  mock.patch.object(CodexExecutor, "execute", write_exec)):
                _run_cli([*run_l2, "--executor", "agy", "--level", "L2"])
                r1 = _run_cli(verify_l2)
                self.assertEqual(r1["level"], "L2")
                _run_cli([*run_l2, "--executor", "agy", "--level", "L3"])
                r2 = _run_cli(verify_l2)
                self.assertEqual(r2["level"], "L3")

                # After 2 lower-tier rejections (L2, L3), status on a fresh commit must NOT report convergence cap!
                (wt_l2 / "calc.py").write_text("V = 2\n", encoding="utf-8")
                _git(wt_l2, "commit", "-am", "fix: prep for L4")
                mid_status = _run_cli(["branch", "status", "l2-start", *project])
                self.assertFalse(mid_status["ready_to_land"])
                self.assertFalse(any("convergence cap" in b.lower() for b in mid_status["blockers"]))
                self.assertTrue(any("No independent 'approve' verdict" in b for b in mid_status["blockers"]))

                _run_cli([*run_l2, "--executor", "agy", "--level", "L4"])
                r3 = _run_cli(verify_l2)
                self.assertEqual(r3["level"], "L4")
                _run_cli([*run_l2, "--executor", "agy", "--level", "L4"])
                r4 = _run_cli(verify_l2)
                self.assertEqual(r4["level"], "L4")

                with self.assertRaises(KeelError) as cap_l2:
                    _run_cli(verify_l2)
                self.assertIn("convergence cap", str(cap_l2.exception))
                self.assertNotIn("force-round", str(cap_l2.exception).lower())

                # `branch status` reports convergence cap consistently both on rejected commit and new commit
                st_same = _run_cli(["branch", "status", "l2-start", *project])
                self.assertTrue(any("convergence cap" in b.lower() for b in st_same["blockers"]))
                self.assertFalse(any("force-round" in b.lower() for b in st_same["blockers"]))

                (wt_l2 / "calc.py").write_text("V = 3\n", encoding="utf-8")
                _git(wt_l2, "commit", "-am", "fix: post-cap commit")
                st_new = _run_cli(["branch", "status", "l2-start", *project])
                self.assertTrue(any("convergence cap" in b.lower() for b in st_new["blockers"]))
                self.assertFalse(any("force-round" in b.lower() for b in st_new["blockers"]))

                # Direct `evaluate_branch_gate` with only `max_review_rounds=2` matches CLI behavior
                direct_gate = evaluate_branch_gate(repo, "l2-start", max_review_rounds=2)
                self.assertTrue(any("convergence cap" in b.lower() for b in direct_gate["blockers"]))
                # Custom 3-tier ladder (`L1..L3`, `verify_min_level="L2"`) derives top_level=L3 and max_total=3
                custom_gate = evaluate_branch_gate(
                    repo, "l2-start", max_review_rounds=2, levels=("L1", "L2", "L3"), verify_min_level="L2",
                )
                self.assertTrue(any("at L3" in b and "4/3 total" in b for b in custom_gate["blockers"]))

            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                recovered = _run_cli([*verify_l2, "--force-round"])
            self.assertEqual(recovered["outcome"], "approve")
            self.assertIs(recovered["forced_round"], True)
            self.assertIs(load_branch_state(repo / ".git", "task/l2-start")["rounds"][-1]["forced_round"], True)
            self.assertTrue(_run_cli(["branch", "status", "l2-start", *project])["ready_to_land"])

            # 2) L3 start: L3 -> L4 -> L4 (3 rounds), 4th blocked
            wt_l3 = Path(_run_cli(["worktree", "create", "l3-start", *project])["path"])
            (wt_l3 / "calc.py").write_text("V = 30\n", encoding="utf-8")
            _git(wt_l3, "commit", "-am", "feat: l3 start")
            verify_l3 = ["branch", "verify", "l3-start", *project]
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                levels_l3 = [_run_cli(verify_l3)["level"] for _ in range(3)]
                self.assertEqual(levels_l3, ["L3", "L4", "L4"])
                with self.assertRaises(KeelError) as cap_l3:
                    _run_cli(verify_l3)
                self.assertIn("convergence cap", str(cap_l3.exception))
                self.assertNotIn("force-round", str(cap_l3.exception).lower())
            st_l3 = _run_cli(["branch", "status", "l3-start", *project])
            self.assertTrue(any("convergence cap" in b.lower() for b in st_l3["blockers"]))

            # 3) L4 start: L4 -> L4 (2 rounds), 3rd blocked
            wt_l4 = Path(_run_cli(["worktree", "create", "l4-start", *project])["path"])
            (wt_l4 / "calc.py").write_text("V = 40\n", encoding="utf-8")
            _git(wt_l4, "commit", "-am", "feat: l4 start")
            verify_l4 = ["branch", "verify", "l4-start", *project]
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                r4_1 = _run_cli([*verify_l4, "--level", "L4"])
                r4_2 = _run_cli(verify_l4)
                self.assertEqual((r4_1["level"], r4_2["level"]), ("L4", "L4"))
                with self.assertRaises(KeelError) as cap_l4:
                    _run_cli(verify_l4)
                self.assertIn("convergence cap", str(cap_l4.exception))
                self.assertNotIn("force-round", str(cap_l4.exception).lower())
            st_l4 = _run_cli(["branch", "status", "l4-start", *project])
            self.assertTrue(any("convergence cap" in b.lower() for b in st_l4["blockers"]))

    def test_dual_side_step_up_direct_edits_stale_write_and_explicit_l4_verify(self) -> None:
        import keel_lib.branch_gate as bg
        from keel_lib.worktree import save_branch_state

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="stepup")
            project = ["--project", str(repo)]
            defaults = KEEL_CLI.load_defaults()

            # A) Direct-edit (no `keel agent run`): verify --level L2 rejection -> L2 refused (KeelConfigError),
            #    omitted --level auto-escalates to L3 -> after L3 rejection, L3 refused and auto-escalates to L4.
            wt_direct = Path(_run_cli(["worktree", "create", "direct-edit", *project])["path"])
            (wt_direct / "calc.py").write_text("D = 1\n", encoding="utf-8")
            _git(wt_direct, "commit", "-am", "feat: direct edit")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                d1 = bg.verify_branch(project=repo, branch_or_slug="direct-edit", defaults=defaults, level="L2")
                self.assertEqual(d1["level"], "L2")
                with self.assertRaisesRegex(KeelConfigError, "L3"):
                    bg.prepare_branch_write(wt_direct, "write", "L2", defaults)
                with self.assertRaisesRegex(KeelConfigError, "L3"):
                    bg.verify_branch(project=repo, branch_or_slug="direct-edit", defaults=defaults, level="L2")
                d2 = bg.verify_branch(project=repo, branch_or_slug="direct-edit", defaults=defaults)
                self.assertEqual(d2["level"], "L3")
                with self.assertRaisesRegex(KeelConfigError, "L4"):
                    bg.verify_branch(project=repo, branch_or_slug="direct-edit", defaults=defaults, level="L3")
                d3 = bg.verify_branch(project=repo, branch_or_slug="direct-edit", defaults=defaults)
                self.assertEqual(d3["level"], "L4")
                d4 = bg.verify_branch(project=repo, branch_or_slug="direct-edit", defaults=defaults)
                self.assertEqual(d4["level"], "L4")
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    bg.verify_branch(project=repo, branch_or_slug="direct-edit", defaults=defaults)

            # B) Stale `last_write` (L2) followed by R1 rejection and direct edit (no fresh `keel agent run`)
            #    still escalates R2 verify to L3 and R3 verify to L4 (cannot spin at L3).
            wt_stale = Path(_run_cli(["worktree", "create", "stale-write", *project])["path"])
            (wt_stale / "calc.py").write_text("S = 1\n", encoding="utf-8")
            _git(wt_stale, "commit", "-am", "feat: stale write")

            def write_exec(adapter: Any, req: Any, cfg: Any) -> dict[str, Any]:
                if req.preset == "verify":
                    return verifier("request_changes")(adapter, req, cfg)
                return {"status": "completed", "output_path": str(req.output_path), "level": req.level}

            with (mock.patch.object(AgyExecutor, "execute", write_exec),
                  mock.patch.object(CodexExecutor, "execute", write_exec)):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_stale), "--executor", "agy",
                    "--mode", "write", "--level", "L2", "--why", "Stale test", "--task", "Init",
                ])
                s1 = bg.verify_branch(project=repo, branch_or_slug="stale-write", defaults=defaults)
                self.assertEqual(s1["level"], "L2")
                # Direct edit without calling `keel agent run`
                (wt_stale / "calc.py").write_text("S = 2\n", encoding="utf-8")
                _git(wt_stale, "commit", "-am", "fix: direct edit after R1")
                with self.assertRaisesRegex(KeelConfigError, "L3"):
                    bg.verify_branch(project=repo, branch_or_slug="stale-write", defaults=defaults, level="L2")
                s2 = bg.verify_branch(project=repo, branch_or_slug="stale-write", defaults=defaults)
                self.assertEqual(s2["level"], "L3")
                # Second direct edit without calling `keel agent run` MUST escalate R3 to L4 (never spin at L3!)
                (wt_stale / "calc.py").write_text("S = 3\n", encoding="utf-8")
                _git(wt_stale, "commit", "-am", "fix: direct edit after R2")
                with self.assertRaisesRegex(KeelConfigError, "L4"):
                    bg.verify_branch(project=repo, branch_or_slug="stale-write", defaults=defaults, level="L3")
                s3 = bg.verify_branch(project=repo, branch_or_slug="stale-write", defaults=defaults)
                self.assertEqual(s3["level"], "L4")

            # B2) Legacy state lacking `after_round` on `last_write` still treats pre-rejection write as stale
            wt_legacy = Path(_run_cli(["worktree", "create", "legacy-stale", *project])["path"])
            (wt_legacy / "calc.py").write_text("L = 1\n", encoding="utf-8")
            _git(wt_legacy, "commit", "-am", "feat: legacy")
            legacy_head = _git(wt_legacy, "rev-parse", "HEAD")
            save_branch_state(repo / ".git", "task/legacy-stale", {
                "branch": "task/legacy-stale",
                "last_write": {"executor": "agy", "level": "L2", "recorded_at": "2026-11-20T10:00:00.000000+00:00"},
                "rounds": [{
                    "head_sha": legacy_head, "outcome": "request_changes", "dual": False,
                    "level": "L2", "implement_level": "L2",
                    "recorded_at": "2026-11-20T10:01:00.000000+00:00",
                    "verdicts": {"codex": {"verdict": "request_changes", "summary": "Fix",
                                           "blocking": ["Fix"], "notes": [], "commands_run": []}},
                }],
                "checks_by_sha": {},
            })
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                l2_res = bg.verify_branch(project=repo, branch_or_slug="legacy-stale", defaults=defaults)
                self.assertEqual(l2_res["level"], "L3")
                l3_res = bg.verify_branch(project=repo, branch_or_slug="legacy-stale", defaults=defaults)
                self.assertEqual(l3_res["level"], "L4")

            # C) Fresh L2 write verified explicitly at --level L4 (request_changes) still allows L3 rework and L3 verify
            wt_l4v = Path(_run_cli(["worktree", "create", "l2-write-l4-verify", *project])["path"])
            (wt_l4v / "calc.py").write_text("E = 1\n", encoding="utf-8")
            _git(wt_l4v, "commit", "-am", "feat: l2 write")
            run_l4v = ["agent", "run", *project, "--cwd", str(wt_l4v), "--executor", "agy",
                       "--mode", "write", "--why", "Explicit L4 verify", "--task", "Impl"]
            verify_l4v = ["branch", "verify", "l2-write-l4-verify", *project]
            with (mock.patch.object(AgyExecutor, "execute", write_exec),
                  mock.patch.object(CodexExecutor, "execute", write_exec)):
                _run_cli([*run_l4v, "--level", "L2"])
                v1 = _run_cli([*verify_l4v, "--level", "L4"])
                self.assertEqual(v1["level"], "L4")
                with self.assertRaisesRegex(KeelError, "L3"):
                    _run_cli([*run_l4v, "--level", "L2"])
                _run_cli([*run_l4v, "--level", "L3"])
                v2 = _run_cli([*verify_l4v, "--level", "L3"])
                self.assertEqual(v2["level"], "L3")
                self.assertEqual(
                    load_branch_state(repo / ".git", "task/l2-write-l4-verify")["rounds"][-1]["implement_level"],
                    "L3",
                )

            # D) Consecutive rework writes before the next verdict CANNOT downgrade below the active rework floor,
            #    and `verify_branch` records the truthful `implement_level` of the actual writer tier.
            wt_consec = Path(_run_cli(["worktree", "create", "consec-rework", *project])["path"])
            (wt_consec / "calc.py").write_text("C = 1\n", encoding="utf-8")
            _git(wt_consec, "commit", "-am", "feat: consec rework")
            run_consec = [
                "agent", "run", *project, "--cwd", str(wt_consec), "--executor", "agy",
                "--mode", "write", "--why", "Consecutive rework floor", "--task", "Impl",
                "--run-id", "consec-writer",
            ]
            resume_consec = ["agent", "resume", "consec-writer", *project, "--message", "Follow-up"]
            verify_consec = ["branch", "verify", "consec-rework", *project]

            def resume_exec(adapter: Any, run_dir: Any, record: Any, followup: Any, out: Any, cfg: Any, **kw: Any) -> dict[str, Any]:
                return {"status": "completed", "level": kw.get("level") or record["level"]}

            with (mock.patch.object(AgyExecutor, "execute", write_exec),
                  mock.patch.object(AgyExecutor, "resume", resume_exec),
                  mock.patch.object(CodexExecutor, "execute", write_exec)):
                _run_cli([*run_consec, "--level", "L2"])
                c_r1 = _run_cli(verify_consec)
                self.assertEqual(c_r1["level"], "L2")
                # First rework write at L3 succeeds and sets `reworked_round`
                _run_cli([*run_consec, "--level", "L3"])
                self.assertEqual(load_branch_state(repo / ".git", "task/consec-rework")["last_write"]["reworked_round"], 1)
                # Subsequent `agent run --level L2` and `agent resume --level L2` MUST STILL be refused with L3!
                with self.assertRaisesRegex(KeelConfigError, "L3"):
                    bg.prepare_branch_write(wt_consec, "write", "L2", defaults)
                with self.assertRaisesRegex(KeelError, "L3"):
                    _run_cli([*run_consec, "--level", "L2"])
                with self.assertRaisesRegex(KeelError, "L3"):
                    _run_cli([*resume_consec, "--level", "L2"])
                self.assertEqual(load_branch_state(repo / ".git", "task/consec-rework")["last_write"]["level"], "L3")
                c_r2 = _run_cli(verify_consec)
                self.assertEqual(c_r2["level"], "L3")
                self.assertEqual(
                    load_branch_state(repo / ".git", "task/consec-rework")["rounds"][-1]["implement_level"],
                    "L3",
                )
                # After L3 rejection, an L4 rework followed by L3 downgrade attempt is also refused, and L4 is recorded truthfully
                _run_cli([*resume_consec, "--level", "L4"])
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*run_consec, "--level", "L3"])
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*resume_consec, "--level", "L3"])
                c_r3 = _run_cli(verify_consec)
                self.assertEqual(c_r3["level"], "L4")
                self.assertEqual(
                    load_branch_state(repo / ".git", "task/consec-rework")["rounds"][-1]["implement_level"],
                    "L4",
                )

            # E) Legacy in-progress branch upgrade: old `main` copied stale `implement_level="L2"` onto a direct-edit
            #    R2 (`level="L3"`, `request_changes`). Inductive `_rework_floor` detects `implement_level < active_floor`
            #    and requires L4 for both `agent run` / `agent resume` and `branch verify` (cannot repeat L3!).
            wt_upg = Path(_run_cli(["worktree", "create", "legacy-upgrade", *project])["path"])
            (wt_upg / "calc.py").write_text("U = 1\n", encoding="utf-8")
            _git(wt_upg, "commit", "-am", "feat: legacy upgrade")
            upg_head = _git(wt_upg, "rev-parse", "HEAD")
            rc_verdict = {
                "codex": {
                    "verdict": "request_changes",
                    "summary": "Still broken",
                    "blocking": ["Fix edge case"],
                    "notes": [],
                    "commands_run": [],
                }
            }
            save_branch_state(repo / ".git", "task/legacy-upgrade", {
                "branch": "task/legacy-upgrade",
                "last_write": {
                    "executor": "agy",
                    "level": "L2",
                    "recorded_at": "2026-11-20T10:00:00.000000+00:00",
                },
                "rounds": [
                    {
                        "head_sha": upg_head,
                        "outcome": "request_changes",
                        "dual": False,
                        "level": "L2",
                        "implement_level": "L2",
                        "recorded_at": "2026-11-20T10:01:00.000000+00:00",
                        "verdicts": rc_verdict,
                    },
                    {
                        "head_sha": upg_head,
                        "outcome": "request_changes",
                        "dual": False,
                        "level": "L3",
                        "implement_level": "L2",  # Stale `implement_level` copied by old `main` on direct edit!
                        "recorded_at": "2026-11-20T10:02:00.000000+00:00",
                        "verdicts": rc_verdict,
                    },
                ],
                "checks_by_sha": {},
            })
            run_upg = [
                "agent", "run", *project, "--cwd", str(wt_upg), "--executor", "agy",
                "--mode", "write", "--why", "Legacy upgrade rework", "--task", "Fix",
            ]
            verify_upg = ["branch", "verify", "legacy-upgrade", *project]
            with (mock.patch.object(AgyExecutor, "execute", write_exec),
                  mock.patch.object(CodexExecutor, "execute", write_exec)):
                with self.assertRaisesRegex(KeelConfigError, "L4"):
                    bg.prepare_branch_write(wt_upg, "write", "L3", defaults)
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*run_upg, "--level", "L3"])
                with self.assertRaisesRegex(KeelConfigError, "L4"):
                    bg.verify_branch(project=repo, branch_or_slug="legacy-upgrade", defaults=defaults, level="L3")
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*verify_upg, "--level", "L3"])
                u_r3 = _run_cli(verify_upg)
                self.assertEqual(u_r3["level"], "L4")
                self.assertIsNone(
                    load_branch_state(repo / ".git", "task/legacy-upgrade")["rounds"][-1]["implement_level"]
                )

            # F) L1 write -> R1 L2 `request_changes` escalates rework & verify floor to L3 (no duplicate L2 review!),
            #    preserving both L4 attempts (`L2 -> L3 -> L4 -> L4`, then 5th blocked at 2/2 L4 and 4/4 total).
            wt_l1 = Path(_run_cli(["worktree", "create", "l1-start", *project])["path"])
            (wt_l1 / "calc.py").write_text("L1_VAL = 1\n", encoding="utf-8")
            _git(wt_l1, "commit", "-am", "feat: l1 start")
            run_l1 = [
                "agent", "run", *project, "--cwd", str(wt_l1), "--executor", "agy",
                "--mode", "write", "--why", "L1 start ladder", "--task", "Impl",
            ]
            verify_l1 = ["branch", "verify", "l1-start", *project]
            with (mock.patch.object(AgyExecutor, "execute", write_exec),
                  mock.patch.object(CodexExecutor, "execute", write_exec)):
                _run_cli([*run_l1, "--level", "L1"])
                l1_r1 = _run_cli(verify_l1)
                self.assertEqual(l1_r1["level"], "L2")
                self.assertEqual(load_branch_state(repo / ".git", "task/l1-start")["rounds"][-1]["implement_level"], "L1")
                # After L1 write fails L2 review, L2 rework AND L2 verify MUST be refused (floor is L3)
                with self.assertRaisesRegex(KeelError, "L3"):
                    _run_cli([*run_l1, "--level", "L2"])
                with self.assertRaisesRegex(KeelError, "L3"):
                    _run_cli([*verify_l1, "--level", "L2"])
                _run_cli([*run_l1, "--level", "L3"])
                l1_r2 = _run_cli(verify_l1)
                self.assertEqual(l1_r2["level"], "L3")
                _run_cli([*run_l1, "--level", "L4"])
                l1_r3 = _run_cli(verify_l1)
                self.assertEqual(l1_r3["level"], "L4")
                _run_cli([*run_l1, "--level", "L4"])
                l1_r4 = _run_cli(verify_l1)
                self.assertEqual(l1_r4["level"], "L4")
                with self.assertRaises(KeelError) as cap_l1:
                    _run_cli(verify_l1)
                self.assertIn("2/2 rejected rounds at L4", str(cap_l1.exception))
                self.assertIn("4/4 total", str(cap_l1.exception))

            # G) Direct-edit commit B made AFTER an `approve` round on commit A does NOT inherit A's `L2` `last_write`:
            #    same-commit re-verify on A keeps `implement_level == "L2"`, whereas commit B gets `implement_level is None`
            #    so an explicit `--level L4` rejection on B requires L4 for subsequent rework and verification.
            wt_post_app = Path(_run_cli(["worktree", "create", "post-approve-edit", *project])["path"])
            (wt_post_app / "calc.py").write_text("PA = 1\n", encoding="utf-8")
            _git(wt_post_app, "commit", "-am", "feat: commit A")
            sha_a = _git(wt_post_app, "rev-parse", "HEAD")
            run_pa = [
                "agent", "run", *project, "--cwd", str(wt_post_app), "--executor", "agy",
                "--mode", "write", "--why", "Post-approve direct edit", "--task", "Impl",
            ]
            verify_pa = ["branch", "verify", "post-approve-edit", *project]
            pa_outcome = "approve"

            def pa_exec(adapter: Any, req: Any, cfg: Any) -> dict[str, Any]:
                if req.preset == "verify":
                    return verifier(pa_outcome)(adapter, req, cfg)
                return {"status": "completed", "output_path": str(req.output_path), "level": req.level}

            with (mock.patch.object(AgyExecutor, "execute", pa_exec),
                  mock.patch.object(CodexExecutor, "execute", pa_exec)):
                _run_cli([*run_pa, "--level", "L2"])
                self.assertEqual(load_branch_state(repo / ".git", "task/post-approve-edit")["last_write"]["head_sha"], sha_a)
                pa_r1 = _run_cli(verify_pa)
                self.assertEqual((pa_r1["outcome"], pa_r1["level"]), ("approve", "L2"))
                # Re-verifying the SAME approved commit A preserves `implement_level == "L2"`
                pa_same = _run_cli([*verify_pa, "--level", "L3"])
                self.assertEqual(pa_same["outcome"], "approve")
                self.assertEqual(
                    load_branch_state(repo / ".git", "task/post-approve-edit")["rounds"][-1]["implement_level"],
                    "L2",
                )
                # Direct edit commit B (without `keel agent run`) invalidates stale `last_write` from commit A!
                (wt_post_app / "calc.py").write_text("PA = 2\n", encoding="utf-8")
                _git(wt_post_app, "commit", "-am", "fix: direct edit commit B")
                pa_outcome = "request_changes"
                pa_b_r1 = _run_cli([*verify_pa, "--level", "L4"])
                self.assertEqual(pa_b_r1["level"], "L4")
                self.assertIsNone(
                    load_branch_state(repo / ".git", "task/post-approve-edit")["rounds"][-1]["implement_level"]
                )
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*run_pa, "--level", "L3"])
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*verify_pa, "--level", "L3"])
                pa_b_r2 = _run_cli(verify_pa)
                self.assertEqual(pa_b_r2["level"], "L4")

            # H) Reduced ladder (`levels: ["L1", "L2"]`, `verify_min_level: "L2"`, `max_review_rounds: 2`,
            #    with executor mappings ONLY for L1/L2): omitted `--level` selects L2 (never missing L3!) and caps at 2/2.
            from keel_lib.executors.base import resolve_executor_level_config

            wt_red = Path(_run_cli(["worktree", "create", "reduced-ladder", *project])["path"])
            (wt_red / "calc.py").write_text("RED = 1\n", encoding="utf-8")
            _git(wt_red, "commit", "-am", "feat: reduced ladder")
            reduced_defaults = {
                "default_branches": ["main", "master"],
                "max_review_rounds": 2,
                "levels": ["L1", "L2"],
                "verify_min_level": "L2",
                "routing": {"general": "agy", "implement": "agy", "verify": "codex"},
                "executors": {
                    "codex": {
                        "levels": {
                            "L1": {"model": "test-sol", "effort": "medium"},
                            "L2": {"model": "test-sol", "effort": "max"},
                        }
                    },
                    "agy": {
                        "levels": {
                            "L1": {"model": "test-h2"},
                            "L2": {"model": "test-neon"},
                        }
                    },
                },
            }

            def strict_reduced_verify(adapter: Any, req: Any, cfg: Any) -> dict[str, Any]:
                lvl, model, _ = resolve_executor_level_config(adapter.name, req.level, cfg)
                payload = {
                    "verdict": "request_changes",
                    "summary": "Needs fix",
                    "blocking": ["Fix bug"],
                    "notes": [],
                    "commands_run": [],
                }
                req.output_path.write_text(json.dumps(payload), encoding="utf-8")
                return {"status": "completed", "executor": adapter.name, "level": lvl, "model": model}

            with mock.patch.object(CodexExecutor, "execute", strict_reduced_verify):
                red_r1 = bg.verify_branch(project=repo, branch_or_slug="reduced-ladder", defaults=reduced_defaults)
                self.assertEqual(red_r1["level"], "L2")
                red_r2 = bg.verify_branch(project=repo, branch_or_slug="reduced-ladder", defaults=reduced_defaults)
                self.assertEqual(red_r2["level"], "L2")
                with self.assertRaises(KeelError) as cap_red:
                    bg.verify_branch(project=repo, branch_or_slug="reduced-ladder", defaults=reduced_defaults)
                self.assertIn("2/2 rejected rounds at L2", str(cap_red.exception))
                self.assertIn("2/2 total", str(cap_red.exception))
                # Legacy round lacking `"level"` on reduced ladder uses `L2` baseline (never unconfigured `L3`)
                legacy_no_lvl = [{"head_sha": sha_a, "outcome": "request_changes", "verdicts": rc_verdict}]
                self.assertEqual(bg._rework_floor(legacy_no_lvl, levels=["L1", "L2"], verify_min_level="L2"), "L2")
                self.assertEqual(
                    bg._convergence_cap_status(legacy_no_lvl, max_review_rounds=2, levels=["L1", "L2"], verify_min_level="L2")["top_request_changes"],
                    1,
                )

            # I) Direct edit BEFORE first review (or after rework write): L2 agent commits A, direct edit commits B != A,
            #    R1 verify on B rejects at L3 -> `implement_level` is None, L3 rework is refused, and R2 runs at L4.
            wt_pre = Path(_run_cli(["worktree", "create", "pre-review-direct-edit", *project])["path"])
            (wt_pre / "calc.py").write_text("PRE = 1\n", encoding="utf-8")
            _git(wt_pre, "commit", "-am", "feat: agent commit A")
            run_pre = [
                "agent", "run", *project, "--cwd", str(wt_pre), "--executor", "agy",
                "--mode", "write", "--why", "Pre-review direct edit", "--task", "Impl",
            ]
            verify_pre = ["branch", "verify", "pre-review-direct-edit", *project]
            with (mock.patch.object(AgyExecutor, "execute", write_exec),
                  mock.patch.object(CodexExecutor, "execute", write_exec)):
                _run_cli([*run_pre, "--level", "L2"])
                # Direct edit commit B BEFORE any verification round runs
                (wt_pre / "calc.py").write_text("PRE = 2\n", encoding="utf-8")
                _git(wt_pre, "commit", "-am", "fix: direct edit commit B before R1")
                pre_r1 = _run_cli(verify_pre)
                self.assertEqual(pre_r1["level"], "L3")
                self.assertIsNone(
                    load_branch_state(repo / ".git", "task/pre-review-direct-edit")["rounds"][-1]["implement_level"]
                )
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*run_pre, "--level", "L3"])
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*verify_pre, "--level", "L3"])
                pre_r2 = _run_cli(verify_pre)
                self.assertEqual(pre_r2["level"], "L4")

            # J) Non-contiguous / altered ladders snap `minimum` to the lowest configured tier in `verify_tiers`:
            #    1) `levels: ["L1", "L3", "L4"]`, `verify_min_level: "L2"` + `L1` write -> implicit verify selects `L3`
            wt_gap = Path(_run_cli(["worktree", "create", "gap-ladder", *project])["path"])
            (wt_gap / "calc.py").write_text("GAP = 1\n", encoding="utf-8")
            _git(wt_gap, "commit", "-am", "feat: gap ladder")
            gap_l1_l3_l4 = {
                "default_branches": ["main", "master"],
                "max_review_rounds": 2,
                "levels": ["L1", "L3", "L4"],
                "verify_min_level": "L2",
                "routing": {"general": "agy", "implement": "agy", "verify": "codex"},
                "executors": {
                    "codex": {
                        "levels": {
                            "L1": {"model": "test-sol", "effort": "medium"},
                            "L3": {"model": "test-astra", "effort": "high"},
                            "L4": {"model": "test-astra", "effort": "xhigh"},
                        }
                    },
                    "agy": {
                        "levels": {
                            "L1": {"model": "test-h2"},
                            "L3": {"model": "test-neon-hi"},
                            "L4": {"model": "test-carbon"},
                        }
                    },
                },
            }
            with (mock.patch.object(AgyExecutor, "execute", write_exec),
                  mock.patch.object(CodexExecutor, "execute", strict_reduced_verify)):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_gap), "--executor", "agy",
                    "--mode", "write", "--level", "L1", "--why", "Gap ladder", "--task", "Impl",
                ])
                gap_r1 = bg.verify_branch(project=repo, branch_or_slug="gap-ladder", defaults=gap_l1_l3_l4)
                self.assertEqual(gap_r1["level"], "L3")

            #    2) Existing `L3` write on branch after `"L3"` is removed (`levels: ["L1", "L2", "L4"]`) -> selects `L4`
            wt_alt = Path(_run_cli(["worktree", "create", "altered-ladder", *project])["path"])
            (wt_alt / "calc.py").write_text("ALT = 1\n", encoding="utf-8")
            _git(wt_alt, "commit", "-am", "feat: altered ladder")
            altered_l1_l2_l4 = {
                "default_branches": ["main", "master"],
                "max_review_rounds": 2,
                "levels": ["L1", "L2", "L4"],
                "verify_min_level": "L2",
                "routing": {"general": "agy", "implement": "agy", "verify": "codex"},
                "executors": {
                    "codex": {
                        "levels": {
                            "L1": {"model": "test-sol", "effort": "medium"},
                            "L2": {"model": "test-sol", "effort": "max"},
                            "L4": {"model": "test-astra", "effort": "xhigh"},
                        }
                    },
                    "agy": {
                        "levels": {
                            "L1": {"model": "test-h2"},
                            "L2": {"model": "test-neon"},
                            "L4": {"model": "test-carbon"},
                        }
                    },
                },
            }
            with (mock.patch.object(AgyExecutor, "execute", write_exec),
                  mock.patch.object(CodexExecutor, "execute", strict_reduced_verify)):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_alt), "--executor", "agy",
                    "--mode", "write", "--level", "L3", "--why", "Altered ladder", "--task", "Impl",
                ])
                alt_r1 = bg.verify_branch(project=repo, branch_or_slug="altered-ladder", defaults=altered_l1_l2_l4)
                self.assertEqual(alt_r1["level"], "L4")

            # K) Scope-mismatched approval when convergence cap is reached surfaces BOTH blockers:
            #    stacked child hits 2 L4 rejections + `--force-round` approve against `task/stack-parent`;
            #    `branch status --base task/stack-parent` is `ready_to_land: True`, whereas `--base main`
            #    reports BOTH scope mismatch AND convergence cap (including on `--old-base`/`--old-head`).
            wt_sp = Path(_run_cli(["worktree", "create", "stack-parent", *project])["path"])
            (wt_sp / "parent_mod.py").write_text("P = 1\n", encoding="utf-8")
            _git(wt_sp, "add", "parent_mod.py")
            _git(wt_sp, "commit", "-m", "feat: stack parent")
            sp_sha = _git(wt_sp, "rev-parse", "HEAD")

            wt_sc = Path(_run_cli(["worktree", "create", "stack-child", "--base", "task/stack-parent", *project])["path"])
            (wt_sc / "child_mod.py").write_text("C = 1\n", encoding="utf-8")
            _git(wt_sc, "add", "child_mod.py")
            _git(wt_sc, "commit", "-m", "feat: stack child")
            sc_old_sha = _git(wt_sc, "rev-parse", "HEAD")
            verify_sc = ["branch", "verify", "stack-child", "--base", "task/stack-parent", *project]
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([*verify_sc, "--level", "L4"])
                _run_cli([*verify_sc, "--level", "L4"])
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                _run_cli([*verify_sc, "--level", "L4", "--force-round"])

            st_parent = _run_cli(["branch", "status", "stack-child", "--base", "task/stack-parent", *project])
            self.assertTrue(st_parent["ready_to_land"], st_parent["blockers"])

            st_main = _run_cli(["branch", "status", "stack-child", "--base", "main", *project])
            self.assertFalse(st_main["ready_to_land"])
            self.assertTrue(any("merge-base" in b for b in st_main["blockers"]))
            self.assertTrue(any("convergence cap" in b.lower() for b in st_main["blockers"]))
            self.assertFalse(any("force-round" in b.lower() for b in st_main["blockers"]))

            # Also check `--old-base`/`--old-head` scope mismatch when task criteria changed after cap + forced approve
            (wt_sp / "docs/tasks/active/20261117-stepup.md").write_text(
                "# Feature stepup\n\n## Goal & Why\nTightened.\n\n## Bounds\nstrict.\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(wt_sp, "add", "docs/tasks/active/20261117-stepup.md")
            _git(wt_sp, "commit", "-m", "docs(tasks): tighten stepup spec on parent")
            _git(wt_sc, "rebase", "task/stack-parent")
            land_mismatch = _run_cli([
                "branch", "land", "stack-child", "--base", "task/stack-parent", *project,
                "--task-file", "docs/tasks/active/20261117-stepup.md",
                "--old-base", sp_sha, "--old-head", sc_old_sha,
            ])
            self.assertFalse(land_mismatch["ready_to_land"])
            self.assertTrue(any("revised task criteria" in b for b in land_mismatch["blockers"]))
            self.assertTrue(any("convergence cap" in b.lower() for b in land_mismatch["blockers"]))

            # L) Same-SHA re-verification after rejection + subsequent approval preserves known `last_write` floor:
            #    L4 writer commits A -> L4 rejection on A -> L4 approval on same A -> `verify --dual` on A selects L4
            #    and `verify --level L2` on A is refused requiring L4.
            wt_same = Path(_run_cli(["worktree", "create", "same-sha-rc-approve", *project])["path"])
            (wt_same / "calc.py").write_text("SAME = 1\n", encoding="utf-8")
            _git(wt_same, "commit", "-am", "feat: L4 commit A")
            run_same = [
                "agent", "run", *project, "--cwd", str(wt_same), "--executor", "agy",
                "--mode", "write", "--why", "Same SHA floor", "--task", "Impl",
            ]
            verify_same = ["branch", "verify", "same-sha-rc-approve", *project]
            same_outcome = "request_changes"

            def same_exec(adapter: Any, req: Any, cfg: Any) -> dict[str, Any]:
                if req.preset == "verify":
                    return verifier(same_outcome)(adapter, req, cfg)
                return {"status": "completed", "output_path": str(req.output_path), "level": req.level}

            with (mock.patch.object(AgyExecutor, "execute", same_exec),
                  mock.patch.object(CodexExecutor, "execute", same_exec)):
                _run_cli([*run_same, "--level", "L4"])
                sm_r1 = _run_cli(verify_same)
                self.assertEqual((sm_r1["outcome"], sm_r1["level"]), ("request_changes", "L4"))
                same_outcome = "approve"
                sm_r2 = _run_cli(verify_same)
                self.assertEqual((sm_r2["outcome"], sm_r2["level"]), ("approve", "L4"))
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*verify_same, "--level", "L2"])
                sm_dual = _run_cli([*verify_same, "--dual"])
                self.assertEqual((sm_dual["outcome"], sm_dual["level"]), ("approve", "L4"))
                self.assertEqual(
                    load_branch_state(repo / ".git", "task/same-sha-rc-approve")["rounds"][-1]["implement_level"],
                    "L4",
                )
                # Message-only `--amend` (`A -> A'`) after `request_changes -> approve` STILL preserves `L4` floor!
                _git(wt_same, "commit", "--amend", "-m", "feat: L4 commit A reworded after rc+approve")
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*verify_same, "--level", "L2"])
                sm_reword = _run_cli(verify_same)
                self.assertEqual((sm_reword["outcome"], sm_reword["level"]), ("approve", "L4"))
                self.assertEqual(
                    load_branch_state(repo / ".git", "task/same-sha-rc-approve")["rounds"][-1]["implement_level"],
                    "L4",
                )

            # M) Pending L4 write when L4 is removed from configured `levels` (`["L1", "L2", "L3"]`) raises
            #    `KeelConfigError` instead of silently downgrading verification to L3!
            wt_ceil = Path(_run_cli(["worktree", "create", "ceil-overflow", *project])["path"])
            (wt_ceil / "calc.py").write_text("CEIL = 1\n", encoding="utf-8")
            _git(wt_ceil, "commit", "-am", "feat: L4 write before cap")
            capped_l1_l2_l3 = {
                "default_branches": ["main", "master"],
                "max_review_rounds": 2,
                "levels": ["L1", "L2", "L3"],
                "verify_min_level": "L2",
                "routing": {"general": "agy", "implement": "agy", "verify": "codex"},
                "executors": {
                    "codex": {
                        "levels": {
                            "L1": {"model": "test-sol", "effort": "medium"},
                            "L2": {"model": "test-sol", "effort": "max"},
                            "L3": {"model": "test-astra", "effort": "high"},
                        }
                    },
                    "agy": {
                        "levels": {
                            "L1": {"model": "test-h2"},
                            "L2": {"model": "test-neon"},
                            "L3": {"model": "test-neon-hi"},
                        }
                    },
                },
            }
            with (mock.patch.object(AgyExecutor, "execute", write_exec),
                  mock.patch.object(CodexExecutor, "execute", strict_reduced_verify)):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_ceil), "--executor", "agy",
                    "--mode", "write", "--level", "L4", "--why", "L4 write", "--task", "Impl",
                ])
                with self.assertRaisesRegex(KeelConfigError, "L4"):
                    bg.verify_branch(project=repo, branch_or_slug="ceil-overflow", defaults=capped_l1_l2_l3)

            # N) Independent `total_rc >= max_total_rounds` Canary test (`1/2` at L4, `4/4` total):
            #    Active cycle carries 3 pre-step-up lower-tier rejections (`L2`, `L2`, `L3`) plus 1 live CLI `L4`
            #    rejection (only 1 L4 rejection!). Both `branch status` and `branch verify` report convergence cap
            #    (`1/2` at L4, `4/4` total) and block 5th verify; `--force-round` recovers cleanly.
            wt_tot = Path(_run_cli(["worktree", "create", "total-cap-only", *project])["path"])
            (wt_tot / "calc.py").write_text("TOT = 1\n", encoding="utf-8")
            _git(wt_tot, "commit", "-am", "feat: total cap commit 1")
            tot_head = _git(wt_tot, "rev-parse", "HEAD")
            tot_mb = _git(repo, "merge-base", "main", "task/total-cap-only")
            save_branch_state(repo / ".git", "task/total-cap-only", {
                "branch": "task/total-cap-only",
                "rounds": [
                    {
                        "head_sha": tot_head,
                        "merge_base_sha": tot_mb,
                        "task_file": None,
                        "task_blob_sha": None,
                        "outcome": "request_changes",
                        "dual": False,
                        "level": lvl,
                        "implement_level": lvl,
                        "recorded_at": f"2026-11-20T10:0{idx}:00.000000+00:00",
                        "verdicts": rc_verdict,
                    }
                    for idx, lvl in enumerate(("L2", "L2", "L3"), start=1)
                ],
                "checks_by_sha": {},
            })
            run_tot = [
                "agent", "run", *project, "--cwd", str(wt_tot), "--executor", "agy",
                "--mode", "write", "--why", "Total cap independent", "--task", "Impl",
            ]
            verify_tot = ["branch", "verify", "total-cap-only", *project]
            tot_outcome = "request_changes"

            def tot_exec(adapter: Any, req: Any, cfg: Any) -> dict[str, Any]:
                if req.preset == "verify":
                    return verifier(tot_outcome)(adapter, req, cfg)
                return {"status": "completed", "output_path": str(req.output_path), "level": req.level}

            with (mock.patch.object(AgyExecutor, "execute", tot_exec),
                  mock.patch.object(CodexExecutor, "execute", tot_exec)):
                _run_cli([*run_tot, "--level", "L4"])
                self.assertEqual(_run_cli(verify_tot)["level"], "L4")  # rc #4 (L4: 1/2 top, 4/4 total!)

                st_tot = _run_cli(["branch", "status", "total-cap-only", *project])
                self.assertFalse(st_tot["ready_to_land"])
                self.assertTrue(any("1/2 rejected rounds at L4" in b and "4/4 total" in b for b in st_tot["blockers"]))
                with self.assertRaises(KeelError) as cap_tot:
                    _run_cli(verify_tot)
                self.assertIn("1/2 rejected rounds at L4", str(cap_tot.exception))
                self.assertIn("4/4 total", str(cap_tot.exception))
                self.assertNotIn("force-round", str(cap_tot.exception).lower())
                tot_outcome = "approve"
                forced_tot = _run_cli([*verify_tot, "--force-round"])
                self.assertEqual((forced_tot["outcome"], forced_tot["forced_round"]), ("approve", True))
                self.assertTrue(_run_cli(["branch", "status", "total-cap-only", *project])["ready_to_land"])

            # O) Staged / uncommitted writer output (`pending_tree_shas`) preserves writer tier when committed untouched,
            #    without mutating `.git/index` or `.git/objects`, and drops attribution if modified before commit!
            wt_stg = Path(_run_cli(["worktree", "create", "staged-writer", *project])["path"])
            verify_stg = ["branch", "verify", "staged-writer", *project]

            def uncommitted_l4_writer(adapter: Any, req: Any, cfg: Any) -> dict[str, Any]:
                if req.preset == "verify":
                    return verifier("approve")(adapter, req, cfg)
                (req.workdir / "calc.py").write_text("STAGED_L4 = 42\n", encoding="utf-8")
                (req.workdir / "helper_l4.py").write_text("H = 1\n", encoding="utf-8")
                _git(req.workdir, "add", "calc.py")  # `calc.py` staged, `helper_l4.py` untracked!
                return {"status": "completed", "output_path": str(req.output_path), "level": req.level}

            with (mock.patch.object(AgyExecutor, "execute", uncommitted_l4_writer),
                  mock.patch.object(CodexExecutor, "execute", uncommitted_l4_writer)):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_stg), "--executor", "agy",
                    "--preset", "general", "--mode", "write", "--level", "L4",
                    "--why", "Staged L4 output", "--task", "Write code without committing",
                ])
                # Confirm index was NOT mutated (`helper_l4.py` is still untracked `??`)
                porcelain_stg = _git(wt_stg, "status", "--porcelain", "-uall")
                self.assertIn("?? helper_l4.py", porcelain_stg)
                saved_stg = load_branch_state(repo / ".git", "task/staged-writer")["last_write"]
                self.assertEqual(len(saved_stg["pending_tree_shas"]), 2)
                # Caller stages all and commits the exact writer output untouched -> L4 floor preserved!
                _git(wt_stg, "add", "-A")
                _git(wt_stg, "commit", "-m", "feat: commit writer output untouched")
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*verify_stg, "--level", "L2"])
                stg_v1 = _run_cli(verify_stg)
                self.assertEqual(stg_v1["level"], "L4")
                self.assertEqual(
                    load_branch_state(repo / ".git", "task/staged-writer")["rounds"][-1]["implement_level"],
                    "L4",
                )

                # Conversely, if writer leaves uncommitted output and caller modifies it before committing -> drops L4!
                wt_mod = Path(_run_cli(["worktree", "create", "modified-writer", *project])["path"])
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_mod), "--executor", "agy",
                    "--preset", "general", "--mode", "write", "--level", "L4",
                    "--why", "Modified output", "--task", "Write code",
                ])
                (wt_mod / "calc.py").write_text("MODIFIED_BY_CALLER = 99\n", encoding="utf-8")
                _git(wt_mod, "add", "-A")
                _git(wt_mod, "commit", "-m", "feat: caller edited writer output")
                mod_v1 = _run_cli(["branch", "verify", "modified-writer", *project])
                self.assertEqual(mod_v1["level"], "L3")
                self.assertIsNone(
                    load_branch_state(repo / ".git", "task/modified-writer")["rounds"][-1]["implement_level"]
                )

            # P) Legacy pre-upgrade `last_write` (no `head_sha`) after prior rejection, approved post-upgrade with
            #    `implement_level="L4"` on commit A -> repeat `verify --dual` on A selects L4 and refuses `--level L2`.
            wt_leg_app = Path(_run_cli(["worktree", "create", "legacy-post-approve", *project])["path"])
            (wt_leg_app / "calc.py").write_text("LA = 1\n", encoding="utf-8")
            _git(wt_leg_app, "commit", "-am", "feat: legacy L4 rework")
            leg_sha = _git(wt_leg_app, "rev-parse", "HEAD")
            save_branch_state(repo / ".git", "task/legacy-post-approve", {
                "branch": "task/legacy-post-approve",
                "last_write": {
                    "executor": "agy",
                    "level": "L4",
                    "reworked_round": 1,
                    "recorded_at": "2026-11-20T10:02:00.000000+00:00",
                },
                "rounds": [{
                    "head_sha": leg_sha,
                    "outcome": "request_changes",
                    "dual": False,
                    "level": "L3",
                    "implement_level": "L3",
                    "recorded_at": "2026-11-20T10:01:00.000000+00:00",
                    "verdicts": rc_verdict,
                }],
                "checks_by_sha": {},
            })
            verify_leg_app = ["branch", "verify", "legacy-post-approve", *project]
            with (mock.patch.object(AgyExecutor, "execute", verifier("approve")),
                  mock.patch.object(CodexExecutor, "execute", verifier("approve"))):
                la_r2 = _run_cli(verify_leg_app)
                self.assertEqual((la_r2["outcome"], la_r2["level"]), ("approve", "L4"))
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*verify_leg_app, "--level", "L2"])
                la_dual = _run_cli([*verify_leg_app, "--dual"])
                self.assertEqual((la_dual["outcome"], la_dual["level"]), ("approve", "L4"))

            # Q) Removing L4 from `levels` after an L4 rejection -> both `prepare_branch_write` and `verify_branch`
            #    raise `KeelConfigError`; and explicit `verify --level L3` when `levels=["L1", "L2", "L4"]` raises `KeelConfigError`.
            wt_drop_l4 = Path(_run_cli(["worktree", "create", "drop-l4-after-rc", *project])["path"])
            (wt_drop_l4 / "calc.py").write_text("DL4 = 1\n", encoding="utf-8")
            _git(wt_drop_l4, "commit", "-am", "feat: L4 rejected")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "drop-l4-after-rc", "--level", "L4", *project])
            with self.assertRaisesRegex(KeelConfigError, "L4"):
                bg.prepare_branch_write(wt_drop_l4, "write", "L3", capped_l1_l2_l3)
            with self.assertRaisesRegex(KeelConfigError, "L4"):
                bg.verify_branch(project=repo, branch_or_slug="drop-l4-after-rc", defaults=capped_l1_l2_l3)
            with self.assertRaisesRegex(KeelConfigError, "L3"):
                bg.verify_branch(project=repo, branch_or_slug="altered-ladder", defaults=altered_l1_l2_l4, level="L3")

            # R) Unstaged CRLF file written by L4 writer under temporary HOME with `core.autocrlf = input`,
            #    committed unchanged by caller -> `_snapshot_pending_tree_shas` respects caller normalization and preserves L4!
            fake_home = Path(tmp) / "crlf-home"
            fake_home.mkdir()
            subprocess.run(
                ["git", "config", "--global", "core.autocrlf", "input"],
                check=True,
                capture_output=True,
                env={**os.environ, "HOME": str(fake_home)},
            )
            wt_crlf = Path(_run_cli(["worktree", "create", "crlf-writer", *project])["path"])

            def crlf_l4_writer(adapter: Any, req: Any, cfg: Any) -> dict[str, Any]:
                if req.preset == "verify":
                    return verifier("approve")(adapter, req, cfg)
                (req.workdir / "crlf.py").write_bytes(b"LINE1 = 1\r\nLINE2 = 2\r\n")
                return {"status": "completed", "output_path": str(req.output_path), "level": req.level}

            with (
                mock.patch.dict(os.environ, {"HOME": str(fake_home)}),
                mock.patch.object(AgyExecutor, "execute", crlf_l4_writer),
                mock.patch.object(CodexExecutor, "execute", crlf_l4_writer),
            ):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_crlf), "--executor", "agy",
                    "--preset", "general", "--mode", "write", "--level", "L4",
                    "--why", "CRLF normalization", "--task", "Write CRLF file",
                ])
                subprocess.run(
                    ["git", "-C", str(wt_crlf), "add", "-A"],
                    check=True,
                    capture_output=True,
                    env={**os.environ, "HOME": str(fake_home)},
                )
                subprocess.run(
                    ["git", "-C", str(wt_crlf), "commit", "-m", "feat: commit normalized CRLF"],
                    check=True,
                    capture_output=True,
                    env={**os.environ, "HOME": str(fake_home)},
                )
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli(["branch", "verify", "crlf-writer", "--level", "L2", *project])
                crlf_v = _run_cli(["branch", "verify", "crlf-writer", *project])
                self.assertEqual(crlf_v["level"], "L4")

            # S) Message-only `git commit --amend -m "reword"` on L4 writer commit preserves L4 floor
            #    (including across subsequent `--dual` verify), while content-modifying `--amend` drops L4!
            wt_amd = Path(_run_cli(["worktree", "create", "amend-reword", *project])["path"])
            (wt_amd / "calc.py").write_text("AMD = 1\n", encoding="utf-8")
            _git(wt_amd, "commit", "-am", "feat: L4 writer commit")
            run_amd = [
                "agent", "run", *project, "--cwd", str(wt_amd), "--executor", "agy",
                "--mode", "write", "--why", "Amend reword", "--task", "Impl",
            ]
            verify_amd = ["branch", "verify", "amend-reword", *project]
            with (mock.patch.object(AgyExecutor, "execute", same_exec),
                  mock.patch.object(CodexExecutor, "execute", same_exec)):
                same_outcome = "approve"
                _run_cli([*run_amd, "--level", "L4"])
                _git(wt_amd, "commit", "--amend", "-m", "feat: reworded message only")
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*verify_amd, "--level", "L2"])
                amd_v1 = _run_cli(verify_amd)
                self.assertEqual((amd_v1["outcome"], amd_v1["level"]), ("approve", "L4"))
                amd_dual = _run_cli([*verify_amd, "--dual"])
                self.assertEqual((amd_dual["outcome"], amd_dual["level"]), ("approve", "L4"))

                # Rewording AGAIN *after* `approve` (`A' -> A''` with unchanged tree) requires fresh approval in
                # `branch status`, STILL refuses `--level L2`, and selects `L4` on `branch verify`!
                _git(wt_amd, "commit", "--amend", "-m", "feat: reworded after approval")
                self.assertFalse(_run_cli(["branch", "status", "amend-reword", *project])["ready_to_land"])
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*verify_amd, "--level", "L2"])
                amd_post_app = _run_cli(verify_amd)
                self.assertEqual((amd_post_app["outcome"], amd_post_app["level"]), ("approve", "L4"))
                self.assertEqual(
                    load_branch_state(repo / ".git", "task/amend-reword")["rounds"][-1]["implement_level"],
                    "L4",
                )

                # Amending with modified file content *after* `approve` drops `L4` attribution!
                (wt_amd / "calc.py").write_text("AMD = 2\n", encoding="utf-8")
                _git(wt_amd, "commit", "-a", "--amend", "-m", "feat: content edit after approval")
                amd_post_edit = _run_cli(verify_amd)
                self.assertEqual(amd_post_edit["level"], "L3")
                self.assertIsNone(
                    load_branch_state(repo / ".git", "task/amend-reword")["rounds"][-1]["implement_level"]
                )

                # Amending with modified file content drops L4 attribution!
                wt_amd_mod = Path(_run_cli(["worktree", "create", "amend-modified", *project])["path"])
                (wt_amd_mod / "calc.py").write_text("AMD_MOD = 1\n", encoding="utf-8")
                _git(wt_amd_mod, "commit", "-am", "feat: initial L4")
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_amd_mod), "--executor", "agy",
                    "--mode", "write", "--level", "L4", "--why", "Amend edit", "--task", "Impl",
                ])
                (wt_amd_mod / "calc.py").write_text("AMD_MOD = 2\n", encoding="utf-8")
                _git(wt_amd_mod, "commit", "-a", "--amend", "-m", "feat: amended content")
                amd_mod_v = _run_cli(["branch", "verify", "amend-modified", *project])
                self.assertEqual(amd_mod_v["level"], "L3")
                self.assertIsNone(
                    load_branch_state(repo / ".git", "task/amend-modified")["rounds"][-1]["implement_level"]
                )

            # T) Historical L4 rejection resolved by L4 approval, followed by removing L4 from `levels` (`["L1", "L2", "L3"]`):
            #    new commit rejected at L2 escalates cleanly to L3 rework & L3 verify without replaying resolved L4 round!
            wt_hist = Path(_run_cli(["worktree", "create", "resolved-hist-l4", *project])["path"])
            (wt_hist / "calc.py").write_text("HIST = 1\n", encoding="utf-8")
            _git(wt_hist, "commit", "-am", "feat: hist 1")
            verify_hist = ["branch", "verify", "resolved-hist-l4", *project]
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([*verify_hist, "--level", "L4"])
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                _run_cli([*verify_hist, "--level", "L4"])
            (wt_hist / "calc.py").write_text("HIST = 2\n", encoding="utf-8")
            _git(wt_hist, "commit", "-am", "feat: hist 2 after L4 removed")
            with mock.patch.object(CodexExecutor, "execute", strict_reduced_verify):
                hist_r3 = bg.verify_branch(
                    project=repo, branch_or_slug="resolved-hist-l4", defaults=capped_l1_l2_l3, level="L2",
                )
                self.assertEqual(hist_r3["level"], "L2")
                self.assertIsNotNone(bg.prepare_branch_write(wt_hist, "write", "L3", capped_l1_l2_l3))
                hist_r4 = bg.verify_branch(project=repo, branch_or_slug="resolved-hist-l4", defaults=capped_l1_l2_l3)
                self.assertEqual(hist_r4["level"], "L3")

            # U) Staged L4 writer output committed unchanged as A -> L4 rejection on A -> L4 approval on A
            #    (where R2 records `implement_level: None`) -> follow-up `verify --dual` STILL selects L4 and refuses L2!
            wt_stg_rc = Path(_run_cli(["worktree", "create", "staged-rc-approve", *project])["path"])
            verify_stg_rc = ["branch", "verify", "staged-rc-approve", *project]
            stg_rc_outcome = "request_changes"

            def staged_rc_exec(adapter: Any, req: Any, cfg: Any) -> dict[str, Any]:
                if req.preset == "verify":
                    return verifier(stg_rc_outcome)(adapter, req, cfg)
                (req.workdir / "calc.py").write_text("STAGED_RC_L4 = 7\n", encoding="utf-8")
                _git(req.workdir, "add", "calc.py")
                return {"status": "completed", "output_path": str(req.output_path), "level": req.level}

            with (mock.patch.object(AgyExecutor, "execute", staged_rc_exec),
                  mock.patch.object(CodexExecutor, "execute", staged_rc_exec)):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_stg_rc), "--executor", "agy",
                    "--preset", "general", "--mode", "write", "--level", "L4",
                    "--why", "Staged rc->approve", "--task", "Stage changes",
                ])
                _git(wt_stg_rc, "commit", "-m", "feat: commit staged L4 output")
                sr_r1 = _run_cli(verify_stg_rc)
                self.assertEqual((sr_r1["outcome"], sr_r1["level"]), ("request_changes", "L4"))
                stg_rc_outcome = "approve"
                sr_r2 = _run_cli(verify_stg_rc)
                self.assertEqual((sr_r2["outcome"], sr_r2["level"]), ("approve", "L4"))
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*verify_stg_rc, "--level", "L2"])
                sr_dual = _run_cli([*verify_stg_rc, "--dual"])
                self.assertEqual((sr_dual["outcome"], sr_dual["level"]), ("approve", "L4"))
                self.assertEqual(
                    load_branch_state(repo / ".git", "task/staged-rc-approve")["rounds"][-1]["implement_level"],
                    "L4",
                )

            # V) Legacy `main` ledger upgrade: L4 write (no `head_sha`) + L4 rejection on commit A recorded on `main`,
            #    then upgraded -> L4 approve on same commit A (which records `implement_level: None`) ->
            #    (a) `verify --dual` stays L4 and `verify --level L2` is rejected;
            #    (b) message-only `git commit --amend -m` on approved legacy L4 commit still rejects `--level L2` and runs L4,
            #        whereas a content-changing `--amend` drops L4 attribution back to manual baseline L3.
            wt_leg_l4 = Path(_run_cli(["worktree", "create", "legacy-l4-rc-upgrade", *project])["path"])
            (wt_leg_l4 / "calc.py").write_text("LEG_L4 = 1\n", encoding="utf-8")
            _git(wt_leg_l4, "commit", "-am", "feat: legacy main L4 commit A")
            leg_l4_sha = _git(wt_leg_l4, "rev-parse", "HEAD")
            save_branch_state(repo / ".git", "task/legacy-l4-rc-upgrade", {
                "branch": "task/legacy-l4-rc-upgrade",
                "last_write": {
                    "executor": "agy",
                    "level": "L4",
                    "recorded_at": "2026-11-20T09:00:00.000000+00:00",
                },
                "rounds": [{
                    "head_sha": leg_l4_sha,
                    "outcome": "request_changes",
                    "dual": False,
                    "level": "L4",
                    "implement_level": "L4",
                    "recorded_at": "2026-11-20T09:01:00.000000+00:00",
                    "verdicts": rc_verdict,
                }],
                "checks_by_sha": {},
            })
            verify_leg_l4 = ["branch", "verify", "legacy-l4-rc-upgrade", *project]
            with (mock.patch.object(AgyExecutor, "execute", verifier("approve")),
                  mock.patch.object(CodexExecutor, "execute", verifier("approve"))):
                # Post-upgrade approval on same commit A
                leg_up_r2 = _run_cli(verify_leg_l4)
                self.assertEqual((leg_up_r2["outcome"], leg_up_r2["level"]), ("approve", "L4"))
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*verify_leg_l4, "--level", "L2"])
                leg_up_dual = _run_cli([*verify_leg_l4, "--dual"])
                self.assertEqual((leg_up_dual["outcome"], leg_up_dual["level"]), ("approve", "L4"))
                self.assertEqual(
                    load_branch_state(repo / ".git", "task/legacy-l4-rc-upgrade")["rounds"][-1]["implement_level"],
                    "L4",
                )
                # Message-only `--amend` on approved legacy L4 commit still rejects `--level L2` and runs L4!
                _git(wt_leg_l4, "commit", "--amend", "-m", "feat: reworded legacy L4 commit")
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*verify_leg_l4, "--level", "L2"])
                leg_up_amd = _run_cli(verify_leg_l4)
                self.assertEqual((leg_up_amd["outcome"], leg_up_amd["level"]), ("approve", "L4"))
                # Content-changing `--amend` drops legacy L4 attribution to manual baseline L3!
                (wt_leg_l4 / "calc.py").write_text("LEG_L4 = 2\n", encoding="utf-8")
                _git(wt_leg_l4, "commit", "-a", "--amend", "-m", "feat: edited legacy L4 content")
                leg_up_edit = _run_cli(verify_leg_l4)
                self.assertEqual(leg_up_edit["level"], "L3")
                self.assertIsNone(
                    load_branch_state(repo / ".git", "task/legacy-l4-rc-upgrade")["rounds"][-1]["implement_level"]
                )

            # W) L4 writer commits its change AND leaves an untracked log file (`pending_tree_shas` non-empty);
            #    after moving the untracked log aside, L4 `request_changes` -> L4 `approve` -> message-only
            #    `git commit --amend -m "reword"` STILL preserves L4 (`verify --level L2` rejected, `verify --dual` L4),
            #    whereas a content-changing `--amend` drops to L3!
            wt_untrk = Path(_run_cli(["worktree", "create", "committed-plus-untracked", *project])["path"])
            verify_untrk = ["branch", "verify", "committed-plus-untracked", *project]
            untrk_outcome = "request_changes"

            def committed_plus_untracked_l4(adapter: Any, req: Any, cfg: Any) -> dict[str, Any]:
                if req.preset == "verify":
                    return verifier(untrk_outcome)(adapter, req, cfg)
                (req.workdir / "calc.py").write_text("COMMITTED_L4 = 100\n", encoding="utf-8")
                _git(req.workdir, "commit", "-am", "feat: L4 writer committed")
                (req.workdir / "test.log").write_text("leftover log\n", encoding="utf-8")
                return {"status": "completed", "output_path": str(req.output_path), "level": req.level}

            with (mock.patch.object(AgyExecutor, "execute", committed_plus_untracked_l4),
                  mock.patch.object(CodexExecutor, "execute", committed_plus_untracked_l4)):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_untrk), "--executor", "agy",
                    "--mode", "write", "--level", "L4", "--why", "Committed + untracked log", "--task", "Impl",
                ])
                self.assertTrue(
                    bool(load_branch_state(repo / ".git", "task/committed-plus-untracked")["last_write"]["pending_tree_shas"])
                )
                (wt_untrk / "test.log").unlink()
                un_r1 = _run_cli(verify_untrk)
                self.assertEqual((un_r1["outcome"], un_r1["level"]), ("request_changes", "L4"))
                untrk_outcome = "approve"
                un_r2 = _run_cli(verify_untrk)
                self.assertEqual((un_r2["outcome"], un_r2["level"]), ("approve", "L4"))
                _git(wt_untrk, "commit", "--amend", "-m", "feat: L4 writer reworded")
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*verify_untrk, "--level", "L2"])
                un_dual = _run_cli([*verify_untrk, "--dual"])
                self.assertEqual((un_dual["outcome"], un_dual["level"]), ("approve", "L4"))
                self.assertEqual(
                    load_branch_state(repo / ".git", "task/committed-plus-untracked")["rounds"][-1]["implement_level"],
                    "L4",
                )
                # Content-changing `--amend` drops to manual baseline L3
                (wt_untrk / "calc.py").write_text("COMMITTED_L4 = 200\n", encoding="utf-8")
                _git(wt_untrk, "commit", "-a", "--amend", "-m", "feat: content changed")
                un_edit = _run_cli(verify_untrk)
                self.assertEqual(un_edit["level"], "L3")
                self.assertIsNone(
                    load_branch_state(repo / ".git", "task/committed-plus-untracked")["rounds"][-1]["implement_level"]
                )

            # X) Tracked-only commit (`git commit -am` / `git add -u`) when writer modified tracked file AND left an
            #    untracked log file (`tracked_tree_sha` in `pending_tree_shas`): removing the untracked log preserves L4!
            wt_trk = Path(_run_cli(["worktree", "create", "tracked-only-commit", *project])["path"])
            verify_trk = ["branch", "verify", "tracked-only-commit", *project]

            def tracked_plus_untracked_l4(adapter: Any, req: Any, cfg: Any) -> dict[str, Any]:
                if req.preset == "verify":
                    return verifier("approve")(adapter, req, cfg)
                (req.workdir / "calc.py").write_text("TRACKED_ONLY_L4 = 77\n", encoding="utf-8")
                (req.workdir / "scratch.log").write_text("untracked debug output\n", encoding="utf-8")
                return {"status": "completed", "output_path": str(req.output_path), "level": req.level}

            with (mock.patch.object(AgyExecutor, "execute", tracked_plus_untracked_l4),
                  mock.patch.object(CodexExecutor, "execute", tracked_plus_untracked_l4)):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_trk), "--executor", "agy",
                    "--mode", "write", "--level", "L4", "--why", "Tracked-only commit", "--task", "Impl",
                ])
                # Confirm real index was NOT mutated (`calc.py` unstaged, `scratch.log` untracked `??`)
                self.assertEqual(_git(wt_trk, "diff", "--cached", "--name-only"), "")
                self.assertEqual(_git(wt_trk, "diff", "--name-only"), "calc.py")
                self.assertIn("?? scratch.log", _git(wt_trk, "status", "--porcelain", "-uall"))
                saved_trk = load_branch_state(repo / ".git", "task/tracked-only-commit")["last_write"]
                self.assertEqual(len(saved_trk["pending_tree_shas"]), 2)
                _git(wt_trk, "commit", "-am", "feat: commit tracked writer changes only")
                (wt_trk / "scratch.log").unlink()
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*verify_trk, "--level", "L2"])
                trk_v1 = _run_cli(verify_trk)
                self.assertEqual((trk_v1["outcome"], trk_v1["level"]), ("approve", "L4"))
                self.assertEqual(
                    load_branch_state(repo / ".git", "task/tracked-only-commit")["rounds"][-1]["implement_level"],
                    "L4",
                )

                # Modifying tracked file before `git commit -am` + removing untracked log drops to manual baseline L3
                wt_trk_mod = Path(_run_cli(["worktree", "create", "tracked-only-modified", *project])["path"])
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_trk_mod), "--executor", "agy",
                    "--mode", "write", "--level", "L4", "--why", "Tracked-only modified", "--task", "Impl",
                ])
                (wt_trk_mod / "calc.py").write_text("TRACKED_EDITED_BY_CALLER = 88\n", encoding="utf-8")
                _git(wt_trk_mod, "commit", "-am", "feat: caller edited tracked file")
                (wt_trk_mod / "scratch.log").unlink()
                trk_mod_v = _run_cli(["branch", "verify", "tracked-only-modified", *project])
                self.assertEqual(trk_mod_v["level"], "L3")
                self.assertIsNone(
                    load_branch_state(repo / ".git", "task/tracked-only-modified")["rounds"][-1]["implement_level"]
                )

            # Y) Pure rebase of writer commit onto advanced `main` before (and after) review preserves L4 floor,
            #    including tracked-only committed writer output AND legacy anchor commits, whereas modifying content
            #    during/after rebase drops attribution to manual baseline L3!
            wt_reb = Path(_run_cli(["worktree", "create", "rebased-writer", *project])["path"])
            wt_reb_mod = Path(_run_cli(["worktree", "create", "rebased-modified", *project])["path"])
            wt_reb_trk = Path(_run_cli(["worktree", "create", "rebased-tracked-pending", *project])["path"])
            wt_reb_leg = Path(_run_cli(["worktree", "create", "rebased-legacy-anchor", *project])["path"])
            old_main_sha = _git(repo, "rev-parse", "HEAD")

            def committed_l4_writer(adapter: Any, req: Any, cfg: Any) -> dict[str, Any]:
                if req.preset == "verify":
                    return verifier("approve")(adapter, req, cfg)
                (req.workdir / "calc.py").write_text("REBASED_L4 = 314\n", encoding="utf-8")
                _git(req.workdir, "commit", "-am", "feat: L4 writer commit before rebase")
                return {"status": "completed", "output_path": str(req.output_path), "level": req.level}

            with (mock.patch.object(AgyExecutor, "execute", committed_l4_writer),
                  mock.patch.object(CodexExecutor, "execute", committed_l4_writer)):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_reb), "--executor", "agy",
                    "--mode", "write", "--level", "L4", "--why", "Pure rebase writer", "--task", "Impl",
                ])
                old_reb_head = _git(wt_reb, "rev-parse", "HEAD")
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_reb_mod), "--executor", "agy",
                    "--mode", "write", "--level", "L4", "--why", "Modified rebase writer", "--task", "Impl",
                ])

            with (mock.patch.object(AgyExecutor, "execute", tracked_plus_untracked_l4),
                  mock.patch.object(CodexExecutor, "execute", tracked_plus_untracked_l4)):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_reb_trk), "--executor", "agy",
                    "--mode", "write", "--level", "L4", "--why", "Tracked pending + rebase", "--task", "Impl",
                ])
                _git(wt_reb_trk, "commit", "-am", "feat: commit tracked writer changes before rebase")
                (wt_reb_trk / "scratch.log").unlink()

            (wt_reb_leg / "calc.py").write_text("LEGACY_REB = 1\n", encoding="utf-8")
            _git(wt_reb_leg, "commit", "-am", "feat: legacy L4 anchor before rebase")
            leg_reb_sha = _git(wt_reb_leg, "rev-parse", "HEAD")
            save_branch_state(repo / ".git", "task/rebased-legacy-anchor", {
                "branch": "task/rebased-legacy-anchor",
                "last_write": {
                    "executor": "agy",
                    "level": "L4",
                    "recorded_at": "2026-11-20T09:00:00.000000+00:00",
                },
                "rounds": [{
                    "head_sha": leg_reb_sha,
                    "outcome": "approve",
                    "dual": False,
                    "level": "L4",
                    "implement_level": "L4",
                    "recorded_at": "2026-11-20T09:01:00.000000+00:00",
                    "verdicts": {"codex": {"verdict": "approve", "summary": "ok", "blocking": [], "notes": [], "commands_run": []}},
                }],
                "checks_by_sha": {},
            })

            # Advance `main` with an unrelated file commit and rebase all four task branches onto `main`
            (repo / "unrelated_main.txt").write_text("main advanced\n", encoding="utf-8")
            _git(repo, "add", "unrelated_main.txt")
            _git(repo, "commit", "-m", "docs: advance main with unrelated file")
            new_main_sha = _git(repo, "rev-parse", "HEAD")

            _git(wt_reb, "rebase", "main")
            new_reb_head = _git(wt_reb, "rev-parse", "HEAD")
            cmp_reb = _run_cli([
                "branch", "compare", *project,
                "--old-base", old_main_sha, "--old-head", old_reb_head,
                "--base", new_main_sha, "--head", new_reb_head,
            ])
            self.assertTrue(cmp_reb["ok"])

            _git(wt_reb_mod, "rebase", "main")
            (wt_reb_mod / "calc.py").write_text("REBASED_EDITED_BY_CALLER = 999\n", encoding="utf-8")
            _git(wt_reb_mod, "commit", "-a", "--amend", "-m", "feat: caller modified content after rebase")

            _git(wt_reb_trk, "rebase", "main")
            _git(wt_reb_leg, "rebase", "main")

            verify_reb = ["branch", "verify", "rebased-writer", *project]
            with (mock.patch.object(AgyExecutor, "execute", verifier("approve")),
                  mock.patch.object(CodexExecutor, "execute", verifier("approve"))):
                # (a) Pure rebase of committed L4 writer -> `--level L2` rejected, defaults to `L4`, and `--dual` stays `L4`
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*verify_reb, "--level", "L2"])
                reb_v1 = _run_cli(verify_reb)
                self.assertEqual((reb_v1["outcome"], reb_v1["level"]), ("approve", "L4"))
                self.assertEqual(
                    load_branch_state(repo / ".git", "task/rebased-writer")["rounds"][-1]["implement_level"],
                    "L4",
                )
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli([*verify_reb, "--level", "L2"])
                reb_dual = _run_cli([*verify_reb, "--dual"])
                self.assertEqual((reb_dual["outcome"], reb_dual["level"]), ("approve", "L4"))

                # (b) Modifying tracked content during/after rebase drops attribution to manual baseline `L3`
                reb_mod_v = _run_cli(["branch", "verify", "rebased-modified", *project])
                self.assertEqual(reb_mod_v["level"], "L3")
                self.assertIsNone(
                    load_branch_state(repo / ".git", "task/rebased-modified")["rounds"][-1]["implement_level"]
                )

                # (c) Tracked-only commit (`git commit -am` + clean untracked) + pure rebase onto `main` -> stays `L4`
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli(["branch", "verify", "rebased-tracked-pending", "--level", "L2", *project])
                reb_trk_v = _run_cli(["branch", "verify", "rebased-tracked-pending", *project])
                self.assertEqual((reb_trk_v["outcome"], reb_trk_v["level"]), ("approve", "L4"))

                # (d) Legacy anchor commit purely rebased onto `main` -> `--level L2` rejected, defaults to `L4`
                with self.assertRaisesRegex(KeelError, "L4"):
                    _run_cli(["branch", "verify", "rebased-legacy-anchor", "--level", "L2", *project])
                reb_leg_v = _run_cli(["branch", "verify", "rebased-legacy-anchor", *project])
                self.assertEqual((reb_leg_v["outcome"], reb_leg_v["level"]), ("approve", "L4"))

    def test_verify_inherits_why_and_surfaces_advisory_branch_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="handoff")
            project = ["--project", str(repo)]

            # 1. Task-file branch: `verify_branch` inherits locked `## Goal & Why` and surfaces branch advisory sections
            wt_task = Path(_run_cli(["worktree", "create", "handoff", *project])["path"])
            (wt_task / "calc.py").write_text(
                "def add(a: int, b: int) -> int:\n    return a + b\n\ndef mul(a: int, b: int) -> int:\n    return a * b\n",
                encoding="utf-8",
            )
            task_path = wt_task / "docs/tasks/active/20261117-handoff.md"
            # Branch modifies Goal & Why + Bounds (must NOT weaken locked spec!) AND adds Progress / Decisions / Review Rebuttals
            task_path.write_text(
                "# Feature handoff\n\n"
                "## Goal & Why\nTAMPERED BRANCH GOAL\n\n"
                "## Bounds\nTAMPERED BRANCH BOUNDS\n\n"
                "## Done-check\npython3 -c 'import calc'\n\n"
                "## Progress\n- [x] Implemented mul()\n\n"
                "## Decisions & Findings\n"
                "Kept integer arithmetic exact.\n\n"
                "### Review Rebuttals\n"
                "Finding 1 (float division) is outside Bounds (`calc.py` integer mul only).\n",
                encoding="utf-8",
            )
            _git(wt_task, "add", ".")
            _git(wt_task, "commit", "-m", "feat: implement mul with progress and rebuttal")

            captured_briefs: list[str] = []

            def capture_verifier(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                captured_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                return verifier("approve")(adapter, req, defaults)

            with mock.patch.object(CodexExecutor, "execute", capture_verifier):
                _run_cli(["branch", "verify", "handoff", *project])
                _run_cli(["branch", "verify", "handoff", *project, "--why", "Explicit CLI alignment override."])

            brief_inherited = captured_briefs[0]
            self.assertIn("## Why (Global Alignment)\n\nAdd mul() cleanly.", brief_inherited)
            self.assertNotIn("## Why (Global Alignment)\n\nTAMPERED BRANCH GOAL", brief_inherited)
            self.assertIn("### Locked Task Specification on `main`", brief_inherited)
            self.assertIn("calc.py only.", brief_inherited)
            advisory_heading = (
                "### Branch Progress, Decisions & Rebuttals "
                "(advisory author context; Locked Task Specification above remains authoritative for Goal & Why, Bounds, and Done-check)"
            )
            self.assertIn(advisory_heading, brief_inherited)
            advisory_block = brief_inherited[brief_inherited.index(advisory_heading) :]
            self.assertIn("- [x] Implemented mul()", advisory_block)
            self.assertIn("Kept integer arithmetic exact.", advisory_block)
            self.assertIn("Finding 1 (float division) is outside Bounds", advisory_block)
            self.assertEqual(advisory_block.count("### Review Rebuttals"), 1)
            self.assertNotIn("TAMPERED BRANCH BOUNDS", advisory_block)
            self.assertNotIn("TAMPERED BRANCH GOAL", advisory_block)

            # Explicit `--why` overrides locked spec `## Goal & Why`
            self.assertIn("## Why (Global Alignment)\n\nExplicit CLI alignment override.", captured_briefs[1])

            # CommonMark lazy list continuation & 4-space indented code blocks in locked spec do not hijack `verify_branch` Why
            lazy_spec_rel = "docs/tasks/todo/20261117-lazy-handoff.md"
            (repo / lazy_spec_rel).write_text(
                "# Feature lazy-handoff\n\n"
                "- The previous ticket used this example\n"
                "as its reference template:\n\n"
                "    ```markdown\n"
                "  ## Why\n"
                "  Old ticket objective.\n"
                "    ```\n\n"
                "## Goal & Why\n"
                "Real objective.\n\n"
                "## Bounds\ncalc.py only.\n",
                encoding="utf-8",
            )
            indented_spec_rel = "docs/tasks/todo/20261117-indented-handoff.md"
            (repo / indented_spec_rel).write_text(
                "# Feature indented-handoff\n\n"
                "    ```markdown\n"
                "    ## Why\n"
                "    Fake.\n\n"
                "## Goal & Why\n"
                "Real after indented code.\n\n"
                "## Bounds\ncalc.py only.\n",
                encoding="utf-8",
            )
            _git(repo, "add", lazy_spec_rel, indented_spec_rel)
            _git(repo, "commit", "-m", "docs: add lazy and indented-code locked specs")

            wt_lazy = Path(_run_cli(["worktree", "create", "lazy-handoff", *project])["path"])
            (wt_lazy / "calc.py").write_text("LAZY_OK = 1\n", encoding="utf-8")
            _git(wt_lazy, "commit", "-am", "feat: lazy handoff impl")
            wt_indented = Path(_run_cli(["worktree", "create", "indented-handoff", *project])["path"])
            (wt_indented / "calc.py").write_text("INDENTED_OK = 1\n", encoding="utf-8")
            _git(wt_indented, "commit", "-am", "feat: indented handoff impl")

            with mock.patch.object(CodexExecutor, "execute", capture_verifier):
                _run_cli(["branch", "verify", "lazy-handoff", "--task-file", lazy_spec_rel, *project])
                _run_cli(["branch", "verify", "indented-handoff", "--task-file", indented_spec_rel, *project])
            self.assertIn("## Why (Global Alignment)\n\nReal objective.", captured_briefs[-2])
            self.assertNotIn("## Why (Global Alignment)\n\nOld ticket objective.", captured_briefs[-2])
            self.assertIn("## Why (Global Alignment)\n\nReal after indented code.", captured_briefs[-1])
            self.assertNotIn("## Why (Global Alignment)\n\nFake.", captured_briefs[-1])

            # Round 5: committed task files starting with line-1 4-space indented `    ## Why` or `<!-- ... -->` comments
            line1_indented_rel = "docs/tasks/todo/20261117-line1-indented.md"
            (repo / line1_indented_rel).write_text(
                "    ## Why\n    Example only.\n\n## Goal & Why\nPrevent data loss.\n\n## Bounds\ncalc.py only.\n",
                encoding="utf-8",
            )
            html_comment_rel = "docs/tasks/todo/20261117-html-comment.md"
            (repo / html_comment_rel).write_text(
                "<!--\n## Why\nCommented placeholder.\n-->\n\n## Goal & Why\nActual user goal.\n\n## Bounds\ncalc.py only.\n",
                encoding="utf-8",
            )
            _git(repo, "add", line1_indented_rel, html_comment_rel)
            _git(repo, "commit", "-m", "docs: add line-1 indented code and html-comment locked specs")

            wt_line1 = Path(_run_cli(["worktree", "create", "line1-indented", *project])["path"])
            (wt_line1 / "calc.py").write_text("LINE1_INDENTED_OK = 1\n", encoding="utf-8")
            _git(wt_line1, "commit", "-am", "feat: line1 indented impl")
            wt_comment = Path(_run_cli(["worktree", "create", "html-comment", *project])["path"])
            (wt_comment / "calc.py").write_text("HTML_COMMENT_OK = 1\n", encoding="utf-8")
            _git(wt_comment, "commit", "-am", "feat: html comment impl")

            def noop_agy_run(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                return {"status": "completed", "executor": "agy", "level": req.level, "model": "test-agy"}

            with mock.patch.object(AgyExecutor, "execute", noop_agy_run):
                run_line1 = _run_cli([
                    "agent", "run", *project, "--executor", "agy", "--mode", "read", "--level", "L2",
                    "--task-file", str(repo / line1_indented_rel), "--run-id", "line1-indented-agent-check",
                ])
                run_comment = _run_cli([
                    "agent", "run", *project, "--executor", "agy", "--mode", "read", "--level", "L2",
                    "--task-file", str(repo / html_comment_rel), "--run-id", "html-comment-agent-check",
                ])
            self.assertEqual(run_line1["why"], "Prevent data loss.")
            self.assertEqual(run_comment["why"], "Actual user goal.")

            with mock.patch.object(CodexExecutor, "execute", capture_verifier):
                _run_cli(["branch", "verify", "line1-indented", "--task-file", line1_indented_rel, *project])
                _run_cli(["branch", "verify", "html-comment", "--task-file", html_comment_rel, *project])
            brief_line1 = captured_briefs[-2]
            brief_comment = captured_briefs[-1]
            self.assertIn("## Why (Global Alignment)\n\nPrevent data loss.", brief_line1)
            self.assertNotIn("## Why (Global Alignment)\n\nExample only.", brief_line1)
            self.assertIn("\n    ## Why\n    Example only.", brief_line1)
            comment_why_header = brief_comment.split("## Task, Scope & Done Criteria")[0]
            self.assertIn("## Why (Global Alignment)\n\nActual user goal.", comment_why_header)
            self.assertNotIn("Commented placeholder", comment_why_header)
            self.assertNotIn("-->", comment_why_header)

            # Round 6: UTF-8 BOM (`\ufeff`) before `~~~md` fence / directly before `## Why`, and 4-space indented
            # code block at the start of `## Decisions & Findings` preserved in verifier advisory output
            bom_fence_rel = "docs/tasks/todo/20261117-bom-fence.md"
            (repo / bom_fence_rel).write_bytes(
                "\ufeff~~~md\n## Why\nFake BOM fenced why.\n~~~\n\n## Goal & Why\nReal goal after BOM fence.\n\n## Bounds\ncalc.py only.\n".encode("utf-8")
            )
            bom_why_rel = "docs/tasks/todo/20261117-bom-why.md"
            (repo / bom_why_rel).write_bytes(
                "\ufeff## Why\nDirect BOM goal.\n\n## Bounds\ncalc.py only.\n".encode("utf-8")
            )
            _git(repo, "add", bom_fence_rel, bom_why_rel)
            _git(repo, "commit", "-m", "docs: add UTF-8 BOM fence and BOM heading locked specs")

            wt_bom_fence = Path(_run_cli(["worktree", "create", "bom-fence", *project])["path"])
            (wt_bom_fence / "calc.py").write_text("BOM_FENCE_OK = 1\n", encoding="utf-8")
            _git(wt_bom_fence, "commit", "-am", "feat: bom fence impl")

            wt_bom_why = Path(_run_cli(["worktree", "create", "bom-why", *project])["path"])
            (wt_bom_why / "calc.py").write_text("BOM_WHY_OK = 1\n", encoding="utf-8")
            # Branch updates `## Decisions & Findings` starting directly with a 4-space indented `    ## Done-check`
            (wt_bom_why / bom_why_rel).write_text(
                "## Why\nDirect BOM goal.\n\n## Bounds\ncalc.py only.\n\n"
                "## Decisions & Findings\n\n"
                "    ## Done-check\n"
                "    Example code block\n",
                encoding="utf-8",
            )
            _git(wt_bom_why, "add", "calc.py", bom_why_rel)
            _git(wt_bom_why, "commit", "-m", "feat: bom why impl with indented advisory block")

            with mock.patch.object(AgyExecutor, "execute", noop_agy_run):
                run_bom_fence = _run_cli([
                    "agent", "run", *project, "--executor", "agy", "--mode", "read", "--level", "L2",
                    "--task-file", str(repo / bom_fence_rel), "--run-id", "bom-fence-agent-check",
                ])
                run_bom_why = _run_cli([
                    "agent", "run", *project, "--executor", "agy", "--mode", "read", "--level", "L2",
                    "--task-file", str(repo / bom_why_rel), "--run-id", "bom-why-agent-check",
                ])
            self.assertEqual(run_bom_fence["why"], "Real goal after BOM fence.")
            self.assertEqual(run_bom_why["why"], "Direct BOM goal.")

            with mock.patch.object(CodexExecutor, "execute", capture_verifier):
                _run_cli(["branch", "verify", "bom-fence", "--task-file", bom_fence_rel, *project])
                _run_cli(["branch", "verify", "bom-why", "--task-file", bom_why_rel, *project])
            brief_bom_fence_v = captured_briefs[-2]
            brief_bom_why_v = captured_briefs[-1]
            self.assertNotIn("\ufeff", brief_bom_fence_v)
            self.assertIn("## Why (Global Alignment)\n\nReal goal after BOM fence.", brief_bom_fence_v)
            self.assertNotIn("Fake BOM fenced why.", brief_bom_fence_v.split("## Task, Scope & Done Criteria")[0])
            self.assertNotIn("\ufeff", brief_bom_why_v)
            self.assertIn("## Why (Global Alignment)\n\nDirect BOM goal.", brief_bom_why_v)
            self.assertIn(
                "## Decisions & Findings\n\n    ## Done-check\n    Example code block",
                brief_bom_why_v,
            )

            # When branch ONLY tampers with `## Bounds` (no Progress/Decisions/Rebuttals change), NO advisory block is added
            task_path.write_text(
                "# Feature handoff\n\n"
                "## Goal & Why\nAdd mul() cleanly.\n\n"
                "## Bounds\nONLY BOUNDS CHANGED\n\n"
                "## Done-check\npython3 -c 'import calc'\n\n"
                "## Progress\n- [ ] Implement\n",
                encoding="utf-8",
            )
            _git(wt_task, "commit", "-am", "chore: touch bounds only")
            with mock.patch.object(CodexExecutor, "execute", capture_verifier):
                _run_cli(["branch", "verify", "handoff", *project])
            self.assertNotIn(advisory_heading, captured_briefs[-1])

            # Missing, untraversable (`chmod 000`), or UTF-16 `--context-file` on `branch verify` fails cleanly with exit 2
            with self.assertRaisesRegex(KeelError, "--context-file"):
                _run_cli(["branch", "verify", "handoff", *project, "--context-file", str(repo / "nope.md")])
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("KEEL_AGENT_MODE", "DEVSTANDARD_AGENT_MODE", "DEVSTANDARD_ROLE")
            }
            if os.geteuid() != 0:
                locked_v_dir = repo / "locked-verify-dir"
                locked_v_dir.mkdir()
                locked_v_ctx = locked_v_dir / "ctx.md"
                locked_v_ctx.write_text("hidden\n", encoding="utf-8")
                locked_v_dir.chmod(0o000)
                try:
                    proc_perm_v = subprocess.run(
                        [str(ROOT / "scripts" / "keel"), "branch", "verify", "handoff", *project, "--context-file", str(locked_v_ctx)],
                        cwd=repo, capture_output=True, text=True, timeout=10, env=clean_env,
                    )
                    self.assertEqual(proc_perm_v.returncode, 2)
                    self.assertIn("keel error:", proc_perm_v.stderr)
                    self.assertIn("--context-file", proc_perm_v.stderr)
                    self.assertNotIn("Traceback", proc_perm_v.stderr)
                finally:
                    locked_v_dir.chmod(0o755)
                    locked_v_ctx.unlink()
                    locked_v_dir.rmdir()

            utf16_verify_ctx = repo / "utf16-verify-context.md"
            utf16_verify_ctx.write_bytes("UTF-16 verify note".encode("utf-16"))
            proc_utf16_v = subprocess.run(
                [str(ROOT / "scripts" / "keel"), "branch", "verify", "handoff", *project, "--context-file", str(utf16_verify_ctx)],
                cwd=repo, capture_output=True, text=True, timeout=10, env=clean_env,
            )
            self.assertEqual(proc_utf16_v.returncode, 2)
            self.assertIn("keel error:", proc_utf16_v.stderr)
            self.assertIn("UTF-8", proc_utf16_v.stderr)
            self.assertNotIn("Traceback", proc_utf16_v.stderr)
            utf16_verify_ctx.unlink()

            # Committed non-UTF-8 (UTF-16) task blob on a branch exits 2 cleanly naming the path and revision
            wt_utf16_blob = Path(_run_cli(["worktree", "create", "utf16-blob", *project])["path"])
            (wt_utf16_blob / "docs/tasks/active/20261117-handoff.md").write_bytes(
                "# UTF-16 task spec\n\n## Goal & Why\nEncoded in UTF-16.\n".encode("utf-16")
            )
            _git(wt_utf16_blob, "add", "docs/tasks/active/20261117-handoff.md")
            _git(wt_utf16_blob, "commit", "-m", "chore: commit utf16 task blob")
            utf16_blob_head = _git(wt_utf16_blob, "rev-parse", "HEAD")
            proc_utf16_blob = subprocess.run(
                [
                    str(ROOT / "scripts" / "keel"), "branch", "verify", "utf16-blob", *project,
                    "--task-file", "docs/tasks/active/20261117-handoff.md",
                ],
                cwd=repo, capture_output=True, text=True, timeout=10, env=clean_env,
            )
            self.assertEqual(proc_utf16_blob.returncode, 2)
            self.assertIn("keel error:", proc_utf16_blob.stderr)
            self.assertIn("docs/tasks/active/20261117-handoff.md", proc_utf16_blob.stderr)
            self.assertIn(utf16_blob_head, proc_utf16_blob.stderr)
            self.assertIn("UTF-8", proc_utf16_blob.stderr)
            self.assertNotIn("Traceback", proc_utf16_blob.stderr)

            # 2. Task-file-free branch: `verify_branch` inherits recorded Orchestrator `last_write["why"]`
            #    both on normal run->resume AND when initial `agent run` fails recoverably before `agent resume` commits!
            wt_fail_rec = Path(_run_cli(["worktree", "create", "taskless-recovered", *project])["path"])

            def agy_recoverable_fail(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                from keel_lib.executors.base import KeelExecutionError

                raise KeelExecutionError(
                    "transient timeout after session start",
                    {"status": "failed", "session_id": "sess-recoverable", "model": "test-agy"},
                )

            def agy_recovered_resume(
                adapter: Any,
                run_dir: Path,
                record: dict[str, Any],
                followup_path: Path,
                output_path: Path,
                defaults: dict[str, Any],
                *,
                level: str | None = None,
                model: str | None = None,
                effort: str | None = None,
            ) -> dict[str, Any]:
                (Path(record["workdir"]) / "calc.py").write_text(
                    "def add(a: int, b: int) -> int:\n    return int(a) + int(b)\n", encoding="utf-8"
                )
                _git(Path(record["workdir"]), "commit", "-am", "feat: commit on resumed session after initial failure")
                return {"status": "completed", "executor": "agy", "model": "test-agy"}

            with (
                mock.patch.object(AgyExecutor, "execute", agy_recoverable_fail),
                mock.patch.object(AgyExecutor, "resume", agy_recovered_resume),
            ):
                with self.assertRaisesRegex(KeelError, "transient timeout"):
                    _run_cli([
                        "agent", "run", *project, "--worktree", "taskless-recovered",
                        "--executor", "agy", "--mode", "write", "--preset", "implement", "--level", "L2",
                        "--run-id", "recoverable-run",
                        "--why", "Preserve Orchestrator Why across failed run into resume.",
                        "--task", "Coerce operands in add().",
                    ])
                failed_run_json = json.loads((repo / ".agents/runs/recoverable-run/run.json").read_text(encoding="utf-8"))
                self.assertEqual(failed_run_json["why"], "Preserve Orchestrator Why across failed run into resume.")
                self.assertIsNone(load_branch_state(repo / ".git", "task/taskless-recovered").get("last_write"))

                _run_cli([
                    "agent", "resume", "recoverable-run", *project, "--level", "L2",
                    "--message", "Finish and commit.",
                ])
                recovered_write = load_branch_state(repo / ".git", "task/taskless-recovered")["last_write"]
                self.assertEqual(recovered_write["why"], "Preserve Orchestrator Why across failed run into resume.")

            with mock.patch.object(CodexExecutor, "execute", capture_verifier):
                _run_cli(["branch", "verify", "taskless-recovered", *project])
            self.assertIn(
                "## Why (Global Alignment)\n\nPreserve Orchestrator Why across failed run into resume.",
                captured_briefs[-1],
            )

            wt_free = Path(_run_cli(["worktree", "create", "taskless-why", *project])["path"])

            def agy_writer_exec(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                (req.workdir / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b + 0\n", encoding="utf-8")
                _git(req.workdir, "commit", "-am", "feat: taskless edit")
                return {
                    "status": "completed",
                    "executor": "agy",
                    "level": req.level,
                    "model": "test-agy",
                    "session_id": "sess-taskless",
                    "output_path": str(req.output_path),
                }

            def agy_writer_resume(
                adapter: Any,
                run_dir: Path,
                record: dict[str, Any],
                followup_path: Path,
                output_path: Path,
                defaults: dict[str, Any],
                *,
                level: str | None = None,
                model: str | None = None,
                effort: str | None = None,
            ) -> dict[str, Any]:
                (Path(record["workdir"]) / "calc.py").write_text(
                    "def add(a: int, b: int) -> int:\n    return int(a) + int(b)\n", encoding="utf-8"
                )
                _git(Path(record["workdir"]), "commit", "-am", "feat: resumed taskless edit")
                return {"status": "completed", "executor": "agy", "model": "test-agy"}

            with (
                mock.patch.object(AgyExecutor, "execute", agy_writer_exec),
                mock.patch.object(AgyExecutor, "resume", agy_writer_resume),
            ):
                _run_cli([
                    "agent", "run", *project, "--worktree", "taskless-why",
                    "--executor", "agy", "--mode", "write", "--preset", "implement", "--level", "L2",
                    "--run-id", "taskless-run",
                    "--why", "Keep operand coercion explicit for taskless branch.",
                    "--task", "Coerce operands in add().",
                ])
                saved_after_run = load_branch_state(repo / ".git", "task/taskless-why")["last_write"]
                self.assertEqual(saved_after_run["why"], "Keep operand coercion explicit for taskless branch.")

                _run_cli([
                    "agent", "resume", "taskless-run", *project, "--level", "L2",
                    "--message", "Also cast both operands to int.",
                ])
                saved_after_resume = load_branch_state(repo / ".git", "task/taskless-why")["last_write"]
                self.assertEqual(saved_after_resume["why"], "Keep operand coercion explicit for taskless branch.")

            with mock.patch.object(CodexExecutor, "execute", capture_verifier):
                _run_cli(["branch", "verify", "taskless-why", *project])
            self.assertIn(
                "## Why (Global Alignment)\n\nKeep operand coercion explicit for taskless branch.",
                captured_briefs[-1],
            )

            # 3. Task-file-free branch with NO recorded write falls back to default alignment sentence
            wt_manual = Path(_run_cli(["worktree", "create", "manual-taskless", *project])["path"])
            (wt_manual / "calc.py").write_text("VALUE = 42\n", encoding="utf-8")
            _git(wt_manual, "commit", "-am", "feat: manual edit")
            with mock.patch.object(CodexExecutor, "execute", capture_verifier):
                _run_cli(["branch", "verify", "manual-taskless", *project])
            self.assertIn(
                "## Why (Global Alignment)\n\nIndependently verify branch 'task/manual-taskless' against the repository's Core need",
                captured_briefs[-1],
            )

    def test_arbiter_schema_strictness_and_validation(self) -> None:
        from keel_lib.branch_gate import validate_arbiter_payload

        valid_approve = {
            "action": "approve",
            "summary": "Disputed finding is out of scope.",
            "directives": [],
            "avoid_dead_ends": [],
            "waived_findings": ["Helper naming preference"],
            "commands_run": ["python3 -m unittest"],
        }
        self.assertEqual(validate_arbiter_payload(valid_approve)["action"], "approve")

        schema_json = json.loads(
            (Path(__file__).resolve().parents[1] / "config/schemas/arbiter.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(schema_json["properties"]["summary"]["minLength"], 1)
        self.assertEqual(schema_json["properties"]["summary"]["pattern"], r"\S")
        for arr_field in ("directives", "avoid_dead_ends", "waived_findings", "commands_run"):
            self.assertEqual(schema_json["properties"][arr_field]["items"]["minLength"], 1)
            self.assertEqual(schema_json["properties"][arr_field]["items"]["pattern"], r"\S")
        try:
            import jsonschema  # type: ignore[import-untyped]
        except ImportError:
            jsonschema = None
        blank_item_payloads = [
            {**valid_approve, "directives": [" "]},
            {**valid_approve, "directives": ["", "valid directive"]},
            {**valid_approve, "action": "rework_once", "directives": ["", "valid directive"]},
            {**valid_approve, "avoid_dead_ends": [" "]},
            {**valid_approve, "avoid_dead_ends": ["", "valid dead end"]},
            {**valid_approve, "waived_findings": [" "]},
            {**valid_approve, "waived_findings": ["", "valid waived finding"]},
            {**valid_approve, "commands_run": [" "]},
            {**valid_approve, "commands_run": ["", "pytest"]},
        ]
        if jsonschema is not None:
            validator = jsonschema.Draft7Validator(schema_json)
            validator.validate(valid_approve)
            for ws_bad in (
                {**valid_approve, "summary": ""},
                {**valid_approve, "summary": "   \t\n"},
                *blank_item_payloads,
            ):
                self.assertFalse(validator.is_valid(ws_bad), ws_bad)

        for bad_payload in (
            "not-a-dict",
            {**valid_approve, "extra": 1},
            {k: v for k, v in valid_approve.items() if k != "waived_findings"},
            {**valid_approve, "action": "merge_now"},
            {**valid_approve, "summary": ""},
            {**valid_approve, "summary": "   "},
            {**valid_approve, "commands_run": [123]},
            {**valid_approve, "action": "rework_once", "directives": []},
            {**valid_approve, "action": "replan", "directives": ["   "]},
            {**valid_approve, "action": "pivot", "directives": []},
            {**valid_approve, "action": "park", "directives": []},
            *blank_item_payloads,
        ):
            with self.subTest(bad_payload=bad_payload), self.assertRaises(KeelError):
                validate_arbiter_payload(bad_payload)

    def test_carry_forward_ledger_single_and_dual_split_and_clean_post_approve_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="cf")
            project = ["--project", str(repo)]
            wt = Path(_run_cli(["worktree", "create", "cf", *project])["path"])

            def agy_writer(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text("PRIVATE_WRITER_CHAT_SECRET_DO_NOT_LEAK\n", encoding="utf-8")
                (req.workdir / "calc.py").write_text("def mul(a: int, b: int) -> int:\n    return a * b\n", encoding="utf-8")
                _git(req.workdir, "commit", "-am", "feat: initial mul")
                return {
                    "status": "completed",
                    "executor": "agy",
                    "level": req.level,
                    "model": "test-agy",
                    "session_id": "sess-cf",
                    "output_path": str(req.output_path),
                }

            with mock.patch.object(AgyExecutor, "execute", agy_writer):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt), "--executor", "agy",
                    "--mode", "write", "--preset", "implement", "--level", "L2",
                    "--task-file", str(repo / "docs/tasks/active/20261117-cf.md"),
                ])
            h1_sha = _git(wt, "rev-parse", "HEAD")

            single_briefs: list[str] = []

            def r1_reject(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                single_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                req.output_path.write_text(
                    json.dumps(
                        {
                            "verdict": "request_changes",
                            "summary": "Round 1 concern.",
                            "blocking": ["PRIOR_BLOCKING_ITEM_1: keep add() intact"],
                            "notes": [],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-model"}

            # Round 1: clean initial verify has NO Carry-Forward ledger
            with mock.patch.object(CodexExecutor, "execute", r1_reject):
                r1 = _run_cli(["branch", "verify", "cf", *project])
            self.assertEqual((r1["outcome"], r1["level"]), ("request_changes", "L2"))
            self.assertNotIn("### Prior Review Carry-Forward Ledger", single_briefs[-1])

            # Writer files structured dispute on unchanged tree (`prev_sha == head_sha`)
            disp = _run_cli(
                ["branch", "dispute", "cf", *project, "--reason", "REBUTTAL_TOKEN_1: add() moved to base module"],
                agent_mode="write",
            )
            self.assertEqual(disp["rebuttal"]["round"], 1)
            self.assertEqual(disp["rebuttal_count"], 1)

            # Round 2a (unchanged tree, single verify): receives Carry-Forward ledger noting `prev_sha == head_sha`
            with mock.patch.object(CodexExecutor, "execute", r1_reject):
                r2 = _run_cli(["branch", "verify", "cf", *project])
            self.assertEqual(r2["level"], "L3")
            r2_brief = single_briefs[-1]
            self.assertIn(
                "### Prior Review Carry-Forward Ledger (re-adjudicate prior items; full diff remains open for genuine missed defects)",
                r2_brief,
            )
            self.assertIn("prev_sha == head_sha", r2_brief)
            self.assertIn("PRIOR_BLOCKING_ITEM_1: keep add() intact", r2_brief)
            self.assertIn("REBUTTAL_TOKEN_1: add() moved to base module", r2_brief)
            self.assertIn("inspect full diff via `git diff", r2_brief)
            self.assertNotIn("PRIVATE_WRITER_CHAT_SECRET_DO_NOT_LEAK", r2_brief)

            # Writer commits actual fix `H2` at L4 -> `--dual` Round 3:
            # Primary cross-vendor (`codex`, opposite of `agy` writer) gets Carry-Forward ledger;
            # Second verifier (`agy`) runs clean-slate BLIND without Carry-Forward ledger!
            def agy_l4_fix(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                (req.workdir / "calc.py").write_text(
                    "def add(a: int, b: int) -> int:\n    return a + b\n\ndef mul(a: int, b: int) -> int:\n    return a * b\n",
                    encoding="utf-8",
                )
                _git(req.workdir, "commit", "-am", "fix: restore add() alongside mul()")
                return {"status": "completed", "executor": "agy", "level": req.level, "model": "test-agy"}

            with mock.patch.object(AgyExecutor, "execute", agy_l4_fix):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt), "--executor", "agy",
                    "--mode", "write", "--preset", "implement", "--level", "L4",
                    "--task-file", str(repo / "docs/tasks/active/20261117-cf.md"),
                ])
            h2_sha = _git(wt, "rev-parse", "HEAD")

            dual_briefs: dict[str, str] = {}

            def dual_approve_capture(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                dual_briefs[adapter.name] = req.brief_path.read_text(encoding="utf-8")
                return verifier("approve")(adapter, req, defaults)

            with (
                mock.patch.object(CodexExecutor, "execute", dual_approve_capture),
                mock.patch.object(AgyExecutor, "execute", dual_approve_capture),
            ):
                r3_dual = _run_cli(["branch", "verify", "cf", "--dual", *project])
            self.assertEqual((r3_dual["outcome"], r3_dual["level"]), ("approve", "L4"))

            # Primary (`codex`) received Carry-Forward + commit delta + rebuttal; second (`agy`) ran clean-slate blind!
            self.assertIn("### Prior Review Carry-Forward Ledger", dual_briefs["codex"])
            self.assertIn(f"{h1_sha[:12]}..{h2_sha[:12]}", dual_briefs["codex"])
            self.assertIn("fix: restore add() alongside mul()", dual_briefs["codex"])
            self.assertIn("PRIOR_BLOCKING_ITEM_1", dual_briefs["codex"])
            self.assertIn("REBUTTAL_TOKEN_1", dual_briefs["codex"])
            self.assertNotIn("PRIVATE_WRITER_CHAT_SECRET_DO_NOT_LEAK", dual_briefs["codex"])

            self.assertNotIn("### Prior Review Carry-Forward Ledger", dual_briefs["agy"])
            self.assertNotIn("PRIOR_BLOCKING_ITEM_1", dual_briefs["agy"])
            self.assertNotIn("REBUTTAL_TOKEN_1", dual_briefs["agy"])

            # Post-`approve` new commit `H3` starts a fresh review cycle WITHOUT stale carry-forward or old rebuttals!
            (wt / "calc.py").write_text(
                "def add(a: int, b: int) -> int:\n    return a + b\n\ndef mul(a: int, b: int) -> int:\n    return int(a) * int(b)\n",
                encoding="utf-8",
            )
            _git(wt, "commit", "-am", "feat: coerce mul operands in new cycle")
            single_briefs.clear()
            with mock.patch.object(CodexExecutor, "execute", capture := lambda a, req, d: (single_briefs.append(req.brief_path.read_text(encoding="utf-8")) or verifier("approve")(a, req, d))):
                r4_fresh = _run_cli(["branch", "verify", "cf", *project])
            self.assertEqual(r4_fresh["outcome"], "approve")
            self.assertNotIn("### Prior Review Carry-Forward Ledger", single_briefs[-1])
            self.assertNotIn("PRIOR_BLOCKING_ITEM_1", single_briefs[-1])
            self.assertNotIn("REBUTTAL_TOKEN_1", single_briefs[-1])

    def test_branch_dispute_validation_utf8_and_subagent_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="disp")
            project = ["--project", str(repo)]
            wt = Path(_run_cli(["worktree", "create", "disp", *project])["path"])
            (wt / "calc.py").write_text("D = 1\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: disp")

            # Rejected when no active `request_changes` round exists
            with self.assertRaisesRegex(KeelError, "no active 'request_changes' round"):
                _run_cli(["branch", "dispute", "disp", *project, "--reason", "too early"])

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "disp", *project])

            # Blocked in `read` mode; empty/conflicting/UTF-16 reasons rejected cleanly
            with self.assertRaisesRegex(KeelError, "Read-only subagent mode"):
                _run_cli(["branch", "dispute", "disp", *project, "--reason", "nope"], agent_mode="read")
            with self.assertRaisesRegex(KeelError, "non-empty --reason"):
                _run_cli(["branch", "dispute", "disp", *project, "--reason", "   "])
            with self.assertRaisesRegex(KeelError, "Cannot combine"):
                _run_cli(["branch", "dispute", "disp", *project, "--reason", "a", "--reason-file", "b"])

            utf16_reason = Path(tmp) / "utf16-reason.md"
            utf16_reason.write_bytes("bad utf16".encode("utf-16"))
            with self.assertRaisesRegex(KeelError, "UTF-8"):
                _run_cli(["branch", "dispute", "disp", *project, "--reason-file", str(utf16_reason)])

            # UTF-8 `--reason-file` with BOM works in `write` and `main` modes
            utf8_reason = Path(tmp) / "utf8-reason.md"
            utf8_reason.write_bytes("\ufeff证据反驳：该边界已在调用方校验。\n".encode("utf-8"))
            res_w = _run_cli(["branch", "dispute", "disp", *project, "--reason-file", str(utf8_reason)], agent_mode="write")
            self.assertEqual(res_w["rebuttal"]["reason"], "证据反驳：该边界已在调用方校验。")
            res_m = _run_cli(["branch", "dispute", "disp", *project, "--reason", "\ufeffMain session note"], agent_mode="main")
            self.assertEqual(res_m["rebuttal_count"], 2)
            self.assertEqual(res_m["rebuttal"]["reason"], "Main session note")

            # Once approved, disputing is rejected even after a new unreviewed commit
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                _run_cli(["branch", "verify", "disp", *project])
            with self.assertRaisesRegex(KeelError, "no active 'request_changes' round"):
                _run_cli(["branch", "dispute", "disp", *project, "--reason", "already approved"])
            (wt / "calc.py").write_text("D = 2\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: post-approve unreviewed commit")
            with self.assertRaisesRegex(KeelError, "no active 'request_changes' round"):
                _run_cli(["branch", "dispute", "disp", *project, "--reason", "new commit not yet rejected"])

    def test_branch_arbitrate_preconditions_and_subagent_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="arb-pre")
            project = ["--project", str(repo)]
            wt = Path(_run_cli(["worktree", "create", "arb-pre", *project])["path"])
            (wt / "calc.py").write_text("A = 1\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: arb-pre")

            # 1. Subagent mode blocks (`read` & `write`) for `arbitrate`, `supersede`, and `verify --arbitrate`
            for sub_mode in ("read", "write"):
                with self.assertRaisesRegex(KeelError, "Subagents cannot run `keel branch arbitrate`"):
                    _run_cli(["branch", "arbitrate", "arb-pre", *project], agent_mode=sub_mode)
                with self.assertRaisesRegex(KeelError, "Subagents cannot run `keel branch supersede`"):
                    _run_cli(["branch", "supersede", "arb-pre", *project], agent_mode=sub_mode)
            with self.assertRaisesRegex(KeelError, "Subagents cannot run arbitration"):
                _run_cli(["branch", "verify", "arb-pre", "--arbitrate", *project], agent_mode="write")

            # 2. Unreviewed `head_sha` rejected
            with self.assertRaisesRegex(KeelError, "has not been verified yet"):
                _run_cli(["branch", "arbitrate", "arb-pre", *project])

            # 3. Single non-deadlocked rejection (no cap, no dispute/rebuttal) rejected!
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "arb-pre", *project])
            with self.assertRaisesRegex(KeelError, "not eligible for arbitration"):
                _run_cli(["branch", "arbitrate", "arb-pre", *project])

            # 4. `--level` below L4 rejected (`KeelConfigError` -> exit 2)
            _run_cli(["branch", "dispute", "arb-pre", *project, "--reason", "Out of bounds finding"])
            for low_lvl in ("L1", "L2", "L3"):
                with self.assertRaisesRegex(KeelError, "requires --level L4"):
                    _run_cli(["branch", "arbitrate", "arb-pre", "--level", low_lvl, *project])

            # 5. Dirty worktree rejected
            dirty = wt / "dirty.tmp"
            dirty.write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(KeelError, "uncommitted or untracked changes"):
                _run_cli(["branch", "arbitrate", "arb-pre", *project])
            dirty.unlink()

            # 6. Failing `--check` (and persisted failing check) rejected before launching Arbiter,
            #    and `--skip-checks` CANNOT erase a recorded failed command on `arbitrate`, `verify`, or `land`!
            with self.assertRaisesRegex(KeelError, "Deterministic check failed"):
                _run_cli(["branch", "arbitrate", "arb-pre", "--check", "false", *project])
            with self.assertRaisesRegex(KeelError, "Deterministic checks failed"):
                _run_cli(["branch", "arbitrate", "arb-pre", *project])
            with self.assertRaisesRegex(KeelError, "--skip-checks cannot clear failed deterministic checks"):
                _run_cli(["branch", "arbitrate", "arb-pre", "--skip-checks", "waived", *project])
            with self.assertRaisesRegex(KeelError, "--skip-checks cannot clear failed deterministic checks"):
                _run_cli(["branch", "verify", "arb-pre", "--skip-checks", "waived", *project])
            land_failed_skip = _run_cli(["branch", "land", "arb-pre", "--skip-checks", "waived", *project])
            self.assertFalse(land_failed_skip["checks"]["ok"])
            self.assertFalse(land_failed_skip["ready_to_land"])
            self.assertTrue(any("Deterministic checks failed" in b for b in land_failed_skip["blockers"]))
            # 7. Stale scope / base: advancing `main` makes branch behind `main` -> rejected!
            old_main = _git(repo, "rev-parse", "main")
            (repo / "README.md").write_text("main moved\n", encoding="utf-8")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-m", "docs: advance main")
            with self.assertRaisesRegex(KeelError, "behind 'main' tip"):
                _run_cli(["branch", "arbitrate", "arb-pre", "--skip-checks", "waived", *project])

            # Rebase branch onto `main` -> new `head_sha` is unverified -> rejected!
            _git(wt, "rebase", "main")
            with self.assertRaisesRegex(KeelError, "has not been verified yet"):
                _run_cli(["branch", "arbitrate", "arb-pre", *project])

            # Verify on new `head_sha`, then revise locked task criteria on `main` (same merge-base? No, commit on main advances main;
            # test revising task criteria via `--base` branch with same merge-base!)
            fork_sha = _git(repo, "rev-parse", "main")
            _git(repo, "branch", "alt-base", fork_sha)
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "arb-pre", "--base", "alt-base", *project])
            _git(repo, "checkout", "alt-base")
            (repo / "docs/tasks/active/20261117-arb-pre.md").write_text(
                "# Revised\n\n## Goal & Why\nChanged.\n\n## Bounds\nStrict.\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(repo, "commit", "-am", "docs(tasks): revise spec on alt-base")
            _git(repo, "checkout", "main")
            # Even if branch fast-forwards/rebases or compares against revised task_file, stale `task_blob_sha` is rejected:
            _git(repo, "branch", "-f", "alt-same-mb", fork_sha)
            # Switch `--task-file` to `docs/tasks/todo/20261117-backlog.md` (mismatched task_file against round) -> rejected!
            with self.assertRaisesRegex(KeelError, "does not match target base"):
                _run_cli([
                    "branch", "arbitrate", "arb-pre", "--base", "alt-same-mb",
                    "--task-file", "docs/tasks/todo/20261117-backlog.md", *project,
                ])

    def test_arbiter_approve_squash_ff_only_rebase_and_inline_verify_arbitrate(self) -> None:
        from keel_lib.env import is_disposable_verify_path

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="arb-ok")
            project = ["--project", str(repo)]

            # Part A: Explicit `keel branch arbitrate` -> `approve` -> `--squash` land with trailer & `## Arbitration Record`
            wt1 = Path(_run_cli(["worktree", "create", "arb-ok", *project])["path"])
            (wt1 / "calc.py").write_text(
                "def add(a: int, b: int) -> int:\n    return a + b\n\ndef mul(a: int, b: int) -> int:\n    return a * b\n",
                encoding="utf-8",
            )
            _git(wt1, "commit", "-am", "feat: clean mul")
            h1_sha = _git(wt1, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "arb-ok", "--check", "python3 -c 'import calc; assert calc.mul(2, 3) == 6'", *project])
            _run_cli(["branch", "dispute", "arb-ok", *project, "--reason", "add() and mul() both pass Done-check"])

            seen_arb_clone_paths: list[Path] = []
            seen_arb_briefs: list[str] = []

            def fake_arbiter_approve(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                seen_arb_clone_paths.append(req.workdir)
                self.assertTrue(is_disposable_verify_path(req.workdir))
                self.assertEqual(_git(req.workdir, "remote"), "")
                self.assertEqual(req.preset, "arbitrate")
                self.assertEqual(req.level, "L4")
                seen_arb_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "approve",
                            "summary": "Implementation satisfies Core need and locked Done-check; finding is non-blocking polish.",
                            "directives": [],
                            "avoid_dead_ends": [],
                            "waived_findings": ["Stylistic docstring preference in calc.py"],
                            "commands_run": ["python3 -c 'import calc; assert calc.mul(2, 3) == 6'"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arbiter"}

            # Note: manual edit had single `codex` verifier -> default Arbiter crosses to opposite vendor `agy` at `L4`!
            with mock.patch.object(AgyExecutor, "execute", fake_arbiter_approve):
                arb_res = _run_cli(["branch", "arbitrate", "arb-ok", *project])
            self.assertEqual((arb_res["action"], arb_res["executor"], arb_res["level"]), ("approve", "agy", "L4"))
            self.assertIn("add() and mul() both pass Done-check", seen_arb_briefs[0])
            self.assertFalse(seen_arb_clone_paths[0].parent.exists())

            st_ok = _run_cli(["branch", "status", "arb-ok", *project])
            self.assertTrue(st_ok["ready_to_land"], st_ok["blockers"])
            self.assertEqual(st_ok["approval_source"], f"arbiter-approve:{h1_sha[:12]}")

            land_sq = _run_cli(["branch", "land", "arb-ok", "--execute", *project])
            self.assertTrue(land_sq["landed"])
            self.assertEqual(land_sq["archived_task"], "docs/tasks/archive/20261117-arb-ok.md")
            sq_msg = _git(repo, "log", "-1", "--format=%B")
            self.assertIn(f"Keel-Arbitrated: approve ({arb_res['run_id']})", sq_msg)
            archived_sq = (repo / "docs/tasks/archive/20261117-arb-ok.md").read_text(encoding="utf-8")
            self.assertIn("## Arbitration Record", archived_sq)
            self.assertIn(arb_res["run_id"], archived_sq)
            self.assertIn("Stylistic docstring preference in calc.py", archived_sq)

            # Part B: `branch verify --arbitrate` inline trigger + pure rebase (`--old-base`/`--old-head`) + `--ff-only` land!
            task2_rel = "docs/tasks/active/20261117-inline-ff.md"
            (repo / task2_rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / task2_rel).write_text(
                "# Inline FF\n\n## Goal & Why\nAdd cube().\n\n## Bounds\ncalc.py\n\n## Done-check\npython3 -c 'import calc'\n",
                encoding="utf-8",
            )
            _git(repo, "add", task2_rel)
            _git(repo, "commit", "-m", "docs(tasks): open inline-ff")
            old_base = _git(repo, "rev-parse", "main")

            wt2 = Path(_run_cli(["worktree", "create", "inline-ff", *project])["path"])
            (wt2 / "calc.py").write_text(
                (wt2 / "calc.py").read_text(encoding="utf-8") + "\ndef cube(x: int) -> int:\n    return x * x * x\n",
                encoding="utf-8",
            )
            _git(wt2, "commit", "-am", "feat: add cube")
            old_head = _git(wt2, "rev-parse", "HEAD")
            chk_cube = "python3 -c 'import calc; assert calc.cube(3) == 27'"

            # Round 1 with `--arbitrate` BEFORE any rebuttal/cap does NOT invoke Arbiter!
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                v_r1 = _run_cli(["branch", "verify", "inline-ff", "--arbitrate", "--check", chk_cube, *project])
            self.assertEqual(v_r1["outcome"], "request_changes")
            self.assertNotIn("arbitration", v_r1)

            # File dispute -> Round 2 `branch verify --arbitrate` rejects in Codex, then auto-triggers Agy Arbiter -> `approve`!
            _run_cli(["branch", "dispute", "inline-ff", *project, "--reason", "cube() is exact for ints"])
            with (
                mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                mock.patch.object(AgyExecutor, "execute", fake_arbiter_approve),
            ):
                v_r2 = _run_cli(["branch", "verify", "inline-ff", "--arbitrate", *project])
            self.assertEqual(v_r2["outcome"], "approve")
            self.assertEqual(v_r2["arbitration"]["action"], "approve")
            self.assertIn("cube() is exact for ints", seen_arb_briefs[1])
            inline_arb_id = v_r2["arbitration"]["run_id"]

            # Advance `main` with an unrelated file and pure-rebase `task/inline-ff` onto `main`
            (repo / "NOTES.txt").write_text("unrelated main commit\n", encoding="utf-8")
            _git(repo, "add", "NOTES.txt")
            _git(repo, "commit", "-m", "docs: add notes")
            _git(wt2, "rebase", "main")

            # Missing check rerun blocks pure-rebase land; rerunning `chk_cube` with `--ff-only --execute` lands & records trailer + archive!
            blocked_reb = _run_cli([
                "branch", "land", "inline-ff", "--ff-only",
                "--old-base", old_base, "--old-head", old_head, *project,
            ])
            self.assertFalse(blocked_reb["ready_to_land"])

            land_ff = _run_cli([
                "branch", "land", "inline-ff", "--ff-only",
                "--old-base", old_base, "--old-head", old_head,
                "--check", chk_cube, "--execute", *project,
            ])
            self.assertTrue(land_ff["landed"])
            self.assertEqual(land_ff["approval_source"], f"pure-rebase-from:{old_head[:12]}")
            ff_msg = _git(repo, "log", "-1", "--format=%B")
            self.assertIn(f"Keel-Arbitrated: approve ({inline_arb_id})", ff_msg)
            archived_ff = (repo / "docs/tasks/archive/20261117-inline-ff.md").read_text(encoding="utf-8")
            self.assertIn("## Arbitration Record", archived_ff)
            self.assertIn(inline_arb_id, archived_ff)

    def test_arbiter_rework_once_bonus_l4_round_and_single_use_clamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="rw-once")
            project = ["--project", str(repo)]
            wt = Path(_run_cli(["worktree", "create", "rw-once", *project])["path"])
            (wt / "calc.py").write_text("R = 1\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: buggy R")

            # Hit convergence cap (`L3 -> L4 -> L4`)
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                for _ in range(3):
                    _run_cli(["branch", "verify", "rw-once", *project])
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "rw-once", *project])

            def fake_arb_rework_once(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "rework_once",
                            "summary": "Real bug in calc.py with a 1-line surgical fix.",
                            "directives": ["SURGICAL_DIRECTIVE_TOKEN: set R = 42 in calc.py"],
                            "avoid_dead_ends": ["DEAD_END_TOKEN: mutating global state"],
                            "waived_findings": [],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with mock.patch.object(AgyExecutor, "execute", fake_arb_rework_once):
                arb1 = _run_cli(["branch", "arbitrate", "rw-once", *project])
            self.assertEqual((arb1["action"], arb1["clamped_from"]), ("rework_once", None))

            # `rework_once` enforces L4 floor on both `agent run` and `branch verify`, and unlocks 1 bonus round without `--force-round`!
            with self.assertRaisesRegex(KeelError, "L4"):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt), "--executor", "agy",
                    "--mode", "write", "--level", "L3", "--why", "Too low", "--task", "Fix",
                ])
            with self.assertRaisesRegex(KeelError, "L4"):
                _run_cli(["branch", "verify", "rw-once", "--level", "L3", *project])

            writer_briefs: list[str] = []

            def agy_l4_rework(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                writer_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                (req.workdir / "calc.py").write_text("R = 42\n", encoding="utf-8")
                _git(req.workdir, "commit", "-am", "fix: apply surgical R = 42")
                return {"status": "completed", "executor": "agy", "level": req.level, "model": "test-agy"}

            with mock.patch.object(AgyExecutor, "execute", agy_l4_rework):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt), "--executor", "agy",
                    "--mode", "write", "--preset", "implement", "--level", "L4",
                    "--task-file", str(repo / "docs/tasks/active/20261117-rw-once.md"),
                ])
            self.assertIn("### Prior Attempt Dead-Ends & Arbiter Directives", writer_briefs[-1])
            self.assertIn("SURGICAL_DIRECTIVE_TOKEN: set R = 42 in calc.py", writer_briefs[-1])
            self.assertIn("DEAD_END_TOKEN: mutating global state", writer_briefs[-1])

            # Bonus L4 `branch verify` runs WITHOUT `--force-round`! Suppose it STILL rejects:
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                bonus_v = _run_cli(["branch", "verify", "rw-once", *project])
            self.assertEqual((bonus_v["outcome"], bonus_v["level"], bonus_v["forced_round"]), ("request_changes", "L4", False))

            # Bonus allowance is now consumed -> further non-forced `branch verify` is blocked again!
            with self.assertRaisesRegex(KeelError, "convergence cap"):
                _run_cli(["branch", "verify", "rw-once", *project])

            # If Arbiter tries to return `rework_once` a SECOND time, it is deterministically clamped to `pivot`!
            with mock.patch.object(CodexExecutor, "execute", fake_arb_rework_once):
                arb2 = _run_cli(["branch", "arbitrate", "rw-once", *project])
            self.assertEqual(arb2["action"], "pivot")
            self.assertEqual(arb2["clamped_from"], "rework_once")
            self.assertIn("Clamped repeated 'rework_once' to 'pivot'", arb2["arbitration"]["summary"])

            # Positive `rework_once` path + branch `### Review Rebuttals` markdown eligibility (without `branch dispute`)!
            task_md_rel = "docs/tasks/active/20261117-rw-win.md"
            (repo / task_md_rel).write_text(
                "# RW Win\n\n## Goal & Why\nWin.\n\n## Bounds\ncalc.py\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(repo, "add", task_md_rel)
            _git(repo, "commit", "-m", "docs(tasks): open rw-win")
            wt_win = Path(_run_cli(["worktree", "create", "rw-win", *project])["path"])
            (wt_win / "calc.py").write_text("WIN = 0\n", encoding="utf-8")
            (wt_win / task_md_rel).write_text(
                "# RW Win\n\n## Goal & Why\nWin.\n\n## Bounds\ncalc.py\n\n## Done-check\ntrue\n\n"
                "## Decisions & Findings\n\n### Review Rebuttals\nBranch markdown rebuttal unlocks arbitration.\n",
                encoding="utf-8",
            )
            _git(wt_win, "add", ".")
            _git(wt_win, "commit", "-m", "feat: rw-win with markdown Review Rebuttals")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "rw-win", *project])
            with mock.patch.object(AgyExecutor, "execute", fake_arb_rework_once):
                arb_win = _run_cli(["branch", "arbitrate", "rw-win", *project])
            self.assertEqual(arb_win["action"], "rework_once")
            with mock.patch.object(AgyExecutor, "execute", agy_l4_rework):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_win), "--executor", "agy",
                    "--mode", "write", "--preset", "implement", "--level", "L4",
                    "--task-file", str(repo / task_md_rel),
                ])
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                bonus_ok = _run_cli(["branch", "verify", "rw-win", *project])
            self.assertEqual((bonus_ok["outcome"], bonus_ok["level"]), ("approve", "L4"))
            self.assertTrue(_run_cli(["branch", "land", "rw-win", "--execute", *project])["landed"])

    def test_arbiter_replan_pivot_supersede_and_macro_recovery_fuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="big-slice")
            project = ["--project", str(repo)]

            # 1. `supersede` rejects unarbitrated branch!
            wt_big = Path(_run_cli(["worktree", "create", "big-slice", *project])["path"])
            (wt_big / "calc.py").write_text("BIG = 1\n", encoding="utf-8")
            _git(wt_big, "commit", "-am", "wip: tangled monolithic slice")
            big_sha = _git(wt_big, "rev-parse", "HEAD")

            with self.assertRaisesRegex(KeelError, "does not carry an active Arbiter 'replan' or 'pivot'"):
                _run_cli(["branch", "supersede", "big-slice", *project])

            # Trigger Arbiter `replan`
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "big-slice", *project])
            _run_cli(["branch", "dispute", "big-slice", *project, "--reason", "Scope requires prerequisite"])

            def fake_arb_replan(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "replan",
                            "summary": "Slice mixes parser and CLI; split into two sequential slices.",
                            "directives": ["SPLIT_DIRECTIVE: implement parser-core before cli-glue"],
                            "avoid_dead_ends": ["MONOLITH_DEAD_END: coupling CLI flags inside parser"],
                            "waived_findings": [],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with mock.patch.object(AgyExecutor, "execute", fake_arb_replan):
                arb_replan = _run_cli(["branch", "arbitrate", "big-slice", *project])
            self.assertEqual(arb_replan["action"], "replan")

            st_replan = _run_cli(["branch", "status", "big-slice", *project])
            self.assertFalse(st_replan["ready_to_land"])
            self.assertTrue(any("keel branch supersede big-slice" in b for b in st_replan["blockers"]))

            # Dirty worktree MUST be refused by `branch supersede` without losing uncommitted work!
            untracked_precious = wt_big / "precious.txt"
            untracked_precious.write_text("do not delete\n", encoding="utf-8")
            with self.assertRaisesRegex(KeelError, "uncommitted or untracked changes"):
                _run_cli(["branch", "supersede", "big-slice", *project])
            self.assertTrue(untracked_precious.is_file())
            untracked_precious.unlink()

            # Clean `branch supersede --archive-task`: preserves Git ref, archives task, writes lineage JSON!
            sup_replan = _run_cli(["branch", "supersede", "big-slice", "--archive-task", *project])
            self.assertTrue(sup_replan["superseded"])
            self.assertEqual(sup_replan["action"], "replan")
            self.assertEqual(sup_replan["recovery_count"], 1)
            self.assertTrue(sup_replan["preserved_ref"].startswith("refs/keel/superseded/big-slice/"))
            self.assertEqual(_git(repo, "rev-parse", sup_replan["preserved_ref"]), big_sha)
            self.assertFalse(wt_big.exists())
            self.assertEqual(sup_replan["archived_task"], "docs/tasks/archive/20261117-big-slice.md")
            archived_replan = (repo / "docs/tasks/archive/20261117-big-slice.md").read_text(encoding="utf-8")
            self.assertIn("## Outcome: [SUPERSEDED BY ARBITER: REPLAN]", archived_replan)
            self.assertIn("MONOLITH_DEAD_END", archived_replan)

            # Missing `--supersedes` fails cleanly without creating an orphan worktree or run directory!
            with self.assertRaisesRegex(KeelError, "No superseded lineage record"):
                _run_cli(["worktree", "create", "orphan-wt", "--supersedes", "nonexistent", *project])
            self.assertFalse((repo / ".agents/worktrees/orphan-wt").exists())

            # Successor slices `slice-a` & `slice-b` inherit lineage via `--supersedes big-slice`!
            wt_a_res = _run_cli(["worktree", "create", "slice-a", "--supersedes", "big-slice", *project])
            self.assertEqual(wt_a_res["recovery_count"], 1)

            succ_briefs: list[str] = []

            def noop_writer_capture(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                succ_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                return {"status": "completed", "executor": "agy", "level": req.level, "model": "test-agy"}

            with mock.patch.object(AgyExecutor, "execute", noop_writer_capture):
                _run_cli([
                    "agent", "run", *project, "--worktree", "slice-b", "--supersedes", "big-slice",
                    "--executor", "agy", "--mode", "write", "--preset", "implement", "--level", "L2",
                    "--why", "Implement split slice B.", "--task", "Build CLI glue.",
                ])
            self.assertIn("### Prior Attempt Dead-Ends & Arbiter Directives", succ_briefs[-1])
            self.assertIn("SPLIT_DIRECTIVE: implement parser-core before cli-glue", succ_briefs[-1])
            self.assertIn("MONOLITH_DEAD_END: coupling CLI flags inside parser", succ_briefs[-1])

            # 2. Same-slug `pivot` (keeping `docs/tasks/active/` on `main`) + `max_structural_recoveries = 2` fuse -> `park`!
            pivot_task_rel = "docs/tasks/active/20261117-pivot-loop.md"
            (repo / pivot_task_rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / pivot_task_rel).write_text(
                "# Pivot Loop\n\n## Goal & Why\nSolve cleanly.\n\n## Bounds\ncalc.py\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(repo, "add", pivot_task_rel)
            _git(repo, "commit", "-m", "docs(tasks): open pivot-loop")

            for attempt_num in (1, 2, 3):
                succ_briefs.clear()

                def attempt_writer(adapter: Any, req: Any, defaults: Any, n: int = attempt_num) -> dict[str, Any]:
                    succ_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                    (req.workdir / "calc.py").write_text(f"ATTEMPT = {n}\n", encoding="utf-8")
                    _git(req.workdir, "commit", "-am", f"feat: attempt {n}")
                    return {"status": "completed", "executor": "agy", "level": req.level, "model": "test-agy"}

                with mock.patch.object(AgyExecutor, "execute", attempt_writer):
                    # Notice NO `--supersedes` needed on same-slug `pivot-loop`!
                    _run_cli([
                        "agent", "run", *project, "--worktree", "pivot-loop",
                        "--executor", "agy", "--mode", "write", "--preset", "implement", "--level", "L4",
                        "--task-file", str(repo / pivot_task_rel),
                    ])
                if attempt_num >= 2:
                    self.assertIn("### Prior Attempt Dead-Ends & Arbiter Directives", succ_briefs[-1])
                    self.assertIn("PIVOT_DEAD_END_1", succ_briefs[-1])
                if attempt_num == 3:
                    self.assertIn("PIVOT_DEAD_END_2", succ_briefs[-1])
                    self.assertIn("Structural Recovery Attempt:** 2", succ_briefs[-1])

                with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                    _run_cli(["branch", "verify", "pivot-loop", *project])
                    _run_cli(["branch", "verify", "pivot-loop", *project])

                def fake_arb_pivot(adapter: Any, req: Any, defaults: Any, n: int = attempt_num) -> dict[str, Any]:
                    req.output_path.write_text(
                        json.dumps(
                            {
                                "action": "pivot",
                                "summary": f"Attempt {n} tangled; rewrite from base.",
                                "directives": [f"PIVOT_DIRECTIVE_{n}: rewrite cleanly"],
                                "avoid_dead_ends": [f"PIVOT_DEAD_END_{n}"],
                                "waived_findings": [],
                                "commands_run": ["git diff"],
                            }
                        ),
                        encoding="utf-8",
                    )
                    return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

                with mock.patch.object(CodexExecutor, "execute", fake_arb_pivot):
                    arb_out = _run_cli(["branch", "arbitrate", "pivot-loop", *project])

                if attempt_num < 3:
                    self.assertEqual((arb_out["action"], arb_out["clamped_from"]), ("pivot", None))
                    sup_out = _run_cli(["branch", "supersede", "pivot-loop", *project])
                    self.assertEqual(sup_out["recovery_count"], attempt_num)
                    self.assertIsNone(sup_out["archived_task"])
                    # `docs/tasks/active/20261117-pivot-loop.md` remains intact on `main`!
                    self.assertTrue((repo / pivot_task_rel).is_file())
                else:
                    # 3rd structural recovery (`recovery_count == 2 >= max_structural_recoveries`) is CLAMPED to `park`!
                    self.assertEqual(arb_out["action"], "park")
                    self.assertEqual(arb_out["clamped_from"], "pivot")
                    self.assertIn("structural recovery fuse reached (2/2 recoveries used)", arb_out["arbitration"]["summary"])

            # Parked branch blocks non-forced verify, arbitrate, dispute, supersede, and land!
            st_parked = _run_cli(["branch", "status", "pivot-loop", *project])
            self.assertTrue(st_parked["parked"])
            self.assertFalse(st_parked["ready_to_land"])
            self.assertTrue(any("parked for human decision" in b for b in st_parked["blockers"]))

            for blocked_cmd in (
                ["branch", "verify", "pivot-loop", *project],
                ["branch", "arbitrate", "pivot-loop", *project],
                ["branch", "dispute", "pivot-loop", "--reason", "try again", *project],
                ["branch", "supersede", "pivot-loop", *project],
            ):
                with self.subTest(blocked_cmd=blocked_cmd), self.assertRaises(KeelError):
                    _run_cli(blocked_cmd)

            # `--human-override` previews ready_to_land on a parked branch, and `--force-round` clears `parked` and lands cleanly!
            override_preview = _run_cli([
                "branch", "land", "pivot-loop", "--human-override", "Owner resolved tradeoff", *project,
            ])
            self.assertTrue(override_preview["ready_to_land"])

            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                forced_unpark = _run_cli(["branch", "verify", "pivot-loop", "--force-round", *project])
            self.assertEqual(forced_unpark["outcome"], "approve")
            st_unparked = _run_cli(["branch", "status", "pivot-loop", *project])
            self.assertFalse(st_unparked["parked"])
            self.assertTrue(st_unparked["ready_to_land"])
            landed_final = _run_cli(["branch", "land", "pivot-loop", "--execute", *project])
            self.assertTrue(landed_final["landed"])

    def test_round1_arbiter_concurrency_pin_and_inline_vendor_isolation(self) -> None:
        from keel_lib.branch_gate import edit_gate_state

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="race-pin")
            project = ["--project", str(repo)]
            wt = Path(_run_cli(["worktree", "create", "race-pin", *project])["path"])

            # 1. Record a `codex` L2 write so cross-vendor inline Arbiter selects `agy` at L4
            def codex_l2_writer(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                (req.workdir / "calc.py").write_text("PIN = 1\n", encoding="utf-8")
                _git(req.workdir, "commit", "-am", "feat: pin 1")
                return {"status": "completed", "executor": "codex", "level": req.level, "model": "gpt-6.2-sol"}

            with mock.patch.object(CodexExecutor, "execute", codex_l2_writer):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt), "--executor", "codex",
                    "--mode", "write", "--preset", "implement", "--level", "L2",
                    "--task-file", str(repo / "docs/tasks/active/20261117-race-pin.md"),
                ])

            with mock.patch.object(AgyExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "race-pin", *project])
            _run_cli(["branch", "dispute", "race-pin", *project, "--reason", "Valid design"])

            # 2. Simulate concurrent verification round arriving while Arbiter subprocess is running (#2)
            def racing_arbiter(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                with edit_gate_state(repo / ".git", "task/race-pin") as st:
                    st["rounds"].append({
                        **st["rounds"][-1],
                        "level": "L4",
                        "recorded_at": "2026-11-21T99:99:99Z",
                    })
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "approve",
                            "summary": "Stale arbiter approval that never saw Round 2.",
                            "directives": [],
                            "avoid_dead_ends": [],
                            "waived_findings": ["Unseen finding"],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with (
                mock.patch.object(AgyExecutor, "execute", racing_arbiter),
                self.assertRaisesRegex(KeelError, "verification rounds changed concurrently during arbitration"),
            ):
                _run_cli(["branch", "arbitrate", "race-pin", *project])
            self.assertIsNone(load_branch_state(repo / ".git", "task/race-pin").get("arbitration"))

            # 3. Inline `verify --arbitrate` with vendor-specific `--executor codex --level L3 --model ... --effort xhigh`
            #    MUST pass `executor=None, level=None, model=None, effort=None` to `arbitrate_branch` (#5)!
            seen_codex_verify_req: list[tuple[str | None, str | None, str | None]] = []
            seen_agy_arb_req: list[tuple[str | None, str | None, str | None]] = []

            def strict_codex_verify(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                seen_codex_verify_req.append((req.level, req.model, req.effort))
                return verifier("request_changes")(adapter, req, defaults)

            def strict_agy_arbiter(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                seen_agy_arb_req.append((req.level, req.model, req.effort))
                # Real AgyExecutor rejects `--effort` for `gemini-4-carbon-high` unless `effort is None`!
                self.assertIsNone(req.model)
                self.assertIsNone(req.effort)
                self.assertEqual(req.level, "L4")
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "approve",
                            "summary": "Clean cross-vendor inline arbitration without effort bleed.",
                            "directives": [],
                            "avoid_dead_ends": [],
                            "waived_findings": ["Pedantic nit"],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": "agy", "level": req.level, "model": "gemini-4-carbon-high"}

            # Trim simulated race round so we run L3 verify cleanly
            with edit_gate_state(repo / ".git", "task/race-pin") as st:
                st["rounds"] = st["rounds"][:1]

            with (
                mock.patch.object(CodexExecutor, "execute", strict_codex_verify),
                mock.patch.object(AgyExecutor, "execute", strict_agy_arbiter),
            ):
                v_inline = _run_cli([
                    "branch", "verify", "race-pin",
                    "--executor", "codex", "--level", "L3",
                    "--model", "gpt-6.2-astra", "--effort", "xhigh",
                    "--arbitrate", *project,
                ])
            self.assertEqual(seen_codex_verify_req, [("L3", "gpt-6.2-astra", "xhigh")])
            self.assertEqual(seen_agy_arb_req, [("L4", None, None)])
            self.assertEqual(v_inline["outcome"], "approve")
            self.assertEqual(v_inline["arbitration"]["executor"], "agy")

    def test_round1_post_arbiter_approve_new_commit_and_post_pivot_moved_sha_no_deadlock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="post-arb")
            project = ["--project", str(repo)]
            wt = Path(_run_cli(["worktree", "create", "post-arb", *project])["path"])
            (wt / "calc.py").write_text("X = 1\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: X = 1")

            # 3(a): Hit convergence cap (`L3 -> L4 -> L4`), get `arbiter-approve`, then commit `H2`!
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                for _ in range(3):
                    _run_cli(["branch", "verify", "post-arb", *project])

            def fake_arb_ok(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "approve",
                            "summary": "Approved at cap.",
                            "directives": [],
                            "avoid_dead_ends": [],
                            "waived_findings": ["Nit"],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with mock.patch.object(AgyExecutor, "execute", fake_arb_ok):
                _run_cli(["branch", "arbitrate", "post-arb", *project])
            self.assertTrue(_run_cli(["branch", "status", "post-arb", *project])["ready_to_land"])

            # Add follow-up commit `H2`: MUST start a fresh cycle (no cap deadlock between `verify` and `arbitrate`!)
            (wt / "calc.py").write_text("X = 2\n", encoding="utf-8")
            _git(wt, "commit", "-am", "fix: follow-up X = 2 after arbiter approve")
            st_h2 = _run_cli(["branch", "status", "post-arb", *project])
            self.assertFalse(st_h2["ready_to_land"])
            self.assertEqual(st_h2["request_changes_rounds"], 0)
            self.assertFalse(any("convergence cap" in b.lower() for b in st_h2["blockers"]))

            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                v_h2 = _run_cli(["branch", "verify", "post-arb", *project])
            self.assertEqual((v_h2["outcome"], v_h2["level"]), ("approve", "L3"))
            self.assertTrue(_run_cli(["branch", "status", "post-arb", *project])["ready_to_land"])

            # 3(b): Scoped `pivot` blocker on `H1` vs moved commit `H2`!
            pivot_rel = "docs/tasks/active/20261117-piv-move.md"
            (repo / pivot_rel).write_text(
                "# Piv Move\n\n## Goal & Why\nTest moved SHA.\n\n## Bounds\ncalc.py\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(repo, "add", pivot_rel)
            _git(repo, "commit", "-m", "docs(tasks): open piv-move")
            wt_pm = Path(_run_cli(["worktree", "create", "piv-move", *project])["path"])
            (wt_pm / "calc.py").write_text("PM = 1\n", encoding="utf-8")
            _git(wt_pm, "commit", "-am", "feat: pm 1")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "piv-move", *project])
            _run_cli(["branch", "dispute", "piv-move", *project, "--reason", "Try dispute"])

            def fake_arb_piv(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "pivot",
                            "summary": "Pivot needed on H1.",
                            "directives": ["Rewrite cleanly"],
                            "avoid_dead_ends": ["Bad H1"],
                            "waived_findings": [],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with mock.patch.object(AgyExecutor, "execute", fake_arb_piv):
                _run_cli(["branch", "arbitrate", "piv-move", *project])

            # On `H1`, `verify` STILL demands `keel branch supersede piv-move`:
            with self.assertRaisesRegex(KeelError, "keel branch supersede piv-move"):
                _run_cli(["branch", "verify", "piv-move", *project])

            # Once branch moves to `H2` (e.g. rebased or new commit), `verify` does NOT demand `supersede`!
            (wt_pm / "calc.py").write_text("PM = 2\n", encoding="utf-8")
            _git(wt_pm, "commit", "-am", "feat: pm 2 moved sha")
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                v_pm2 = _run_cli(["branch", "verify", "piv-move", *project])
            self.assertEqual(v_pm2["outcome"], "approve")

    def test_round1_rebuttal_cycle_baseline_and_round1_auto_arbitrate_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="reb-cycle")
            project = ["--project", str(repo)]
            task_rel = "docs/tasks/active/20261117-reb-cycle.md"
            wt = Path(_run_cli(["worktree", "create", "reb-cycle", *project])["path"])

            # Commit `H1` already containing `### Review Rebuttals` before Round 1 verify!
            (wt / "calc.py").write_text("C = 1\n", encoding="utf-8")
            cycle1_spec = (
                (wt / task_rel).read_text(encoding="utf-8").rstrip()
                + "\n\n## Decisions & Findings\n\n### Review Rebuttals\nCycle 1 rebuttal note.\n"
            )
            (wt / task_rel).write_text(cycle1_spec, encoding="utf-8")
            _git(wt, "add", ".")
            _git(wt, "commit", "-m", "feat: H1 with Review Rebuttals")

            def fake_arb_approve(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "approve",
                            "summary": "Approved Cycle 1 rebuttal.",
                            "directives": [],
                            "avoid_dead_ends": [],
                            "waived_findings": ["Cycle 1 nit"],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            # 4(ii): Round 1 `verify --arbitrate` MUST NOT auto-trigger arbitration on the very first rejection of the cycle!
            with (
                mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                mock.patch.object(AgyExecutor, "execute", side_effect=AssertionError("Arbiter must not run on Round 1!")),
            ):
                c1_r1 = _run_cli(["branch", "verify", "reb-cycle", "--arbitrate", *project])
            self.assertEqual(c1_r1["outcome"], "request_changes")
            self.assertNotIn("arbitration", c1_r1)

            # Round 2 `verify --arbitrate`: verifier saw Cycle 1 rebuttal and still rejected -> inline Arbiter fires!
            with (
                mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                mock.patch.object(AgyExecutor, "execute", fake_arb_approve),
            ):
                c1_r2 = _run_cli(["branch", "verify", "reb-cycle", "--arbitrate", *project])
            self.assertEqual(c1_r2["outcome"], "approve")

            # 4(i): Cycle 2 commit `H2` touches ONLY `calc.py`, leaving Cycle 1 `Review Rebuttals` untouched!
            (wt / "calc.py").write_text("C = 2\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: Cycle 2 code change without new rebuttal")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "reb-cycle", *project])
            # Untouched Cycle 1 `Review Rebuttals` MUST NOT unlock arbitration in Cycle 2!
            with self.assertRaisesRegex(KeelError, "not eligible for arbitration"):
                _run_cli(["branch", "arbitrate", "reb-cycle", *project])

            # Even on Cycle 2 Round 2, `verify --arbitrate` MUST NOT treat untouched Cycle 1 `Review Rebuttals` as active!
            with (
                mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                mock.patch.object(AgyExecutor, "execute", side_effect=AssertionError("Stale Cycle 1 rebuttal leaked!")),
            ):
                c2_r2 = _run_cli(["branch", "verify", "reb-cycle", "--arbitrate", *project])
            self.assertEqual(c2_r2["outcome"], "request_changes")
            self.assertNotIn("arbitration", c2_r2)

            # Updating `### Review Rebuttals` in Cycle 2 (`H3`) unlocks arbitration cleanly!
            (wt / task_rel).write_text(cycle1_spec + "- Cycle 2 fresh rebuttal.\n", encoding="utf-8")
            _git(wt, "commit", "-am", "docs(tasks): add Cycle 2 rebuttal")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "reb-cycle", *project])
            with mock.patch.object(AgyExecutor, "execute", fake_arb_approve):
                c2_arb = _run_cli(["branch", "arbitrate", "reb-cycle", *project])
            self.assertEqual(c2_arb["action"], "approve")

    def test_round1_supersede_atomicity_idempotency_directive_precedence_and_corrupt_lineage(self) -> None:
        import keel_lib.branch_gate as bg
        from keel_lib.worktree import lineage_state_path, load_lineage_state, save_lineage_state

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="sup-atom")
            project = ["--project", str(repo)]
            wt = Path(_run_cli(["worktree", "create", "sup-atom", *project])["path"])
            (wt / "calc.py").write_text("ATOM = 1\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: atom 1")
            atom_sha = _git(wt, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "sup-atom", *project])
            _run_cli(["branch", "dispute", "sup-atom", *project, "--reason", "Need split"])

            def fake_arb_replan(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "replan",
                            "summary": "Split sup-atom.",
                            "directives": ["PARENT_SPLIT_DIRECTIVE: split into two"],
                            "avoid_dead_ends": ["PARENT_DEAD_END: monolithic helper"],
                            "waived_findings": [],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with mock.patch.object(AgyExecutor, "execute", fake_arb_replan):
                arb = _run_cli(["branch", "arbitrate", "sup-atom", *project])

            # 6(a): Locked worktree fails upfront before creating any `refs/keel/superseded/...` ref or lineage file!
            _git(repo, "worktree", "lock", "--reason", "investigating", str(wt))
            with self.assertRaisesRegex(KeelError, "locked"):
                _run_cli(["branch", "supersede", "sup-atom", *project])
            self.assertEqual(_git(repo, "for-each-ref", "refs/keel/superseded/sup-atom"), "")
            self.assertIsNone(load_lineage_state(repo / ".git", "sup-atom"))
            _git(repo, "worktree", "unlock", str(wt))

            # 6(b): Teardown failure rolls back newly created `refs/keel/superseded/...` ref and does NOT increment lineage!
            with (
                mock.patch.object(bg, "_remove_worktree_and_branch_after_land", side_effect=KeelError("simulated teardown crash")),
                self.assertRaisesRegex(KeelError, "simulated teardown crash"),
            ):
                _run_cli(["branch", "supersede", "sup-atom", *project])
            self.assertEqual(_git(repo, "for-each-ref", "refs/keel/superseded/sup-atom"), "")
            self.assertIsNone(load_lineage_state(repo / ".git", "sup-atom"))

            # 6(c): Idempotency on `last_arbitration_run_id` prevents double-counting `recovery_count` on retry!
            seeded_ref = f"refs/keel/superseded/sup-atom/20261121T000000Z-{atom_sha[:12]}"
            _git(repo, "update-ref", seeded_ref, atom_sha)
            save_lineage_state(
                repo / ".git",
                "sup-atom",
                {
                    "slug": "sup-atom",
                    "branch": "task/sup-atom",
                    "recovery_count": 1,
                    "last_arbitration_run_id": arb["run_id"],
                    "last_action": "replan",
                    "last_preserved_ref": seeded_ref,
                    "last_head_sha": atom_sha,
                    "avoid_dead_ends": ["PARENT_DEAD_END: monolithic helper"],
                    "prior_directives": ["PARENT_SPLIT_DIRECTIVE: split into two"],
                    "history": [{"attempt": 1, "run_id": arb["run_id"]}],
                },
            )
            sup_retry = _run_cli(["branch", "supersede", "sup-atom", *project])
            self.assertEqual(sup_retry["recovery_count"], 1)
            self.assertEqual(sup_retry["preserved_ref"], seeded_ref)

            # 7: Repeated `--supersedes sup-atom` on `child-a` MUST NOT clobber newer branch-local `rework_once` directives!
            wt_child = Path(_run_cli(["worktree", "create", "child-a", "--supersedes", "sup-atom", *project])["path"])
            (wt_child / "calc.py").write_text("CHILD = 1\n", encoding="utf-8")
            _git(wt_child, "commit", "-am", "feat: child-a initial")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "child-a", *project])
            _run_cli(["branch", "dispute", "child-a", *project, "--reason", "Small surgical issue"])

            def fake_child_rework(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "rework_once",
                            "summary": "Surgical fix on child-a.",
                            "directives": ["LOCAL_SURGICAL_DIRECTIVE_CHILD_A: set CHILD = 2"],
                            "avoid_dead_ends": ["CHILD_LOCAL_DEAD_END"],
                            "waived_findings": [],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with mock.patch.object(AgyExecutor, "execute", fake_child_rework):
                _run_cli(["branch", "arbitrate", "child-a", *project])

            captured_rework_briefs: list[str] = []

            def capture_rework_writer(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                captured_rework_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                return {"status": "completed", "executor": "agy", "level": req.level, "model": "test-agy"}

            with mock.patch.object(AgyExecutor, "execute", capture_rework_writer):
                _run_cli([
                    "agent", "run", *project, "--worktree", "child-a", "--supersedes", "sup-atom",
                    "--executor", "agy", "--mode", "write", "--preset", "implement", "--level", "L4",
                    "--why", "Apply child-a surgical fix.", "--task", "Update calc.py.",
                ])
            self.assertIn("LOCAL_SURGICAL_DIRECTIVE_CHILD_A: set CHILD = 2", captured_rework_briefs[-1])
            self.assertIn("PARENT_DEAD_END: monolithic helper", captured_rework_briefs[-1])
            self.assertIn("CHILD_LOCAL_DEAD_END", captured_rework_briefs[-1])
            self.assertNotIn("PARENT_SPLIT_DIRECTIVE: split into two", captured_rework_briefs[-1])

            # 8: Corrupt / non-UTF-8 / invalid-schema lineage files fail fast with `KeelError` naming the file!
            bad_path = lineage_state_path(repo / ".git", "corrupt-lin")
            bad_path.parent.mkdir(parents=True, exist_ok=True)
            bad_cases: list[bytes] = [
                b"\xff\xfe\x00\x00invalid-utf8",
                b"{not valid json",
                b"[]",
                json.dumps({"recovery_count": True}).encode("utf-8"),
                json.dumps({"recovery_count": -1}).encode("utf-8"),
                json.dumps({"recovery_count": 1, "avoid_dead_ends": [123]}).encode("utf-8"),
                json.dumps({"recovery_count": 1, "prior_directives": "not-a-list"}).encode("utf-8"),
            ]
            for raw_bytes in bad_cases:
                bad_path.write_bytes(raw_bytes)
                with self.subTest(raw_bytes=raw_bytes):
                    with self.assertRaisesRegex(KeelError, "corrupt-lin\\.json"):
                        load_lineage_state(repo / ".git", "corrupt-lin")
                    with self.assertRaisesRegex(KeelError, "corrupt-lin\\.json"):
                        _run_cli(["worktree", "create", "from-corrupt", "--supersedes", "corrupt-lin", *project])
                    with self.assertRaisesRegex(KeelError, "corrupt-lin\\.json"):
                        _run_cli([
                            "agent", "run", *project, "--supersedes", "corrupt-lin",
                            "--executor", "agy", "--mode", "read", "--level", "L2",
                            "--why", "Check corrupt lineage.", "--task", "Noop.",
                        ])

    def test_round1_ff_only_audit_trail_for_todo_and_taskless_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="unused-active")
            project = ["--project", str(repo)]

            def fake_arb_approve(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "approve",
                            "summary": "Approved cleanly.",
                            "directives": [],
                            "avoid_dead_ends": [],
                            "waived_findings": ["Minor style preference"],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            # 9(a): `todo`-backed branch landed via `--ff-only --execute` archives `todo -> archive` with `Keel-Arbitrated:`!
            wt_todo = Path(_run_cli(["worktree", "create", "backlog", *project])["path"])
            (wt_todo / "calc.py").write_text("TODO_OK = 1\n", encoding="utf-8")
            _git(wt_todo, "commit", "-am", "feat: implement backlog task")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "backlog",
                    "--task-file", "docs/tasks/todo/20261117-backlog.md", *project,
                ])
            _run_cli(["branch", "dispute", "backlog", *project, "--reason", "Matches backlog spec"])
            with mock.patch.object(AgyExecutor, "execute", fake_arb_approve):
                arb_todo = _run_cli([
                    "branch", "arbitrate", "backlog",
                    "--task-file", "docs/tasks/todo/20261117-backlog.md", *project,
                ])
            land_todo = _run_cli([
                "branch", "land", "backlog", "--ff-only",
                "--task-file", "docs/tasks/todo/20261117-backlog.md", "--execute", *project,
            ])
            self.assertTrue(land_todo["landed"])
            self.assertEqual(land_todo["archived_task"], "docs/tasks/archive/20261117-backlog.md")
            self.assertFalse((repo / "docs/tasks/todo/20261117-backlog.md").exists())
            archived_todo_txt = (repo / "docs/tasks/archive/20261117-backlog.md").read_text(encoding="utf-8")
            self.assertIn("## Arbitration Record", archived_todo_txt)
            self.assertIn(arb_todo["run_id"], archived_todo_txt)
            self.assertIn(f"Keel-Arbitrated: approve ({arb_todo['run_id']})", _git(repo, "log", "-1", "--format=%B"))

            # 9(b): Taskless branch landed via `--ff-only --execute` records `--allow-empty` audit commit with `Keel-Arbitrated:`!
            wt_notask = Path(_run_cli(["worktree", "create", "no-task-ff", *project])["path"])
            (wt_notask / "calc.py").write_text("NOTASK_OK = 2\n", encoding="utf-8")
            _git(wt_notask, "commit", "-am", "fix: two-line taskless fix")
            notask_head = _git(wt_notask, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "no-task-ff", "--why", "Two-line hotfix.", *project])
            _run_cli(["branch", "dispute", "no-task-ff", *project, "--reason", "Hotfix is minimal"])
            with mock.patch.object(AgyExecutor, "execute", fake_arb_approve):
                arb_notask = _run_cli(["branch", "arbitrate", "no-task-ff", "--why", "Two-line hotfix.", *project])

            land_notask = _run_cli(["branch", "land", "no-task-ff", "--ff-only", "--execute", *project])
            self.assertTrue(land_notask["landed"])
            self.assertIsNone(land_notask["archived_task"])
            self.assertNotEqual(land_notask["landed_sha"], notask_head)
            self.assertEqual(land_notask["landed_sha"], _git(repo, "rev-parse", "main"))
            audit_msg = _git(repo, "log", "-1", "--format=%B")
            self.assertIn("chore(gate): record arbitration for task/no-task-ff", audit_msg)
            self.assertIn(f"Keel-Arbitrated: approve ({arb_notask['run_id']})", audit_msg)
            self.assertEqual(_git(repo, "rev-parse", "main~1"), notask_head)

    def test_round2_supersede_scope_revalidation_and_failed_check_preserves_task_file(self) -> None:
        from keel_lib.worktree import load_lineage_state, save_branch_state

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _, task_a_rel = _init_repo_with_task(repo, slug="task-a")
            task_b_rel = "docs/tasks/active/20261117-task-b.md"
            (repo / task_b_rel).write_text(
                "# Task B\n\n## Goal & Why\nUnrelated active task B.\n\n## Bounds\ncalc.py\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(repo, "add", task_b_rel)
            _git(repo, "commit", "-m", "docs(tasks): open task-b")
            project = ["--project", str(repo)]

            wt = Path(_run_cli(["worktree", "create", "task-a", *project])["path"])
            (wt / "calc.py").write_text("PIV = 1\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: draft task-a")
            head_sha = _git(wt, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "task-a", *project])
            _run_cli(["branch", "dispute", "task-a", *project, "--reason", "Need clean rewrite"])

            def fake_arb_pivot(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "pivot",
                            "summary": "Pivot task-a cleanly.",
                            "directives": ["Rewrite task-a from base"],
                            "avoid_dead_ends": ["Tangled draft"],
                            "waived_findings": [],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with mock.patch.object(AgyExecutor, "execute", fake_arb_pivot):
                _run_cli(["branch", "arbitrate", "task-a", *project])
            self.assertEqual(load_branch_state(repo / ".git", "task/task-a")["task_file"], task_a_rel)

            # 1) Failed `--check` verify with `--task-file` B MUST NOT overwrite `state["task_file"]`!
            with self.assertRaisesRegex(KeelError, "Deterministic check failed"):
                _run_cli(["branch", "verify", "task-a", "--task-file", task_b_rel, "--check", "false", *project])
            self.assertEqual(load_branch_state(repo / ".git", "task/task-a")["task_file"], task_a_rel)

            # Failed `arbitrate` with `--task-file` B also leaves `state["task_file"]` untouched!
            with self.assertRaisesRegex(KeelError, "does not match target base"):
                _run_cli(["branch", "arbitrate", "task-a", "--task-file", task_b_rel, *project])
            self.assertEqual(load_branch_state(repo / ".git", "task/task-a")["task_file"], task_a_rel)

            # 2) Passing `--task-file` B to `supersede --archive-task` refuses task B without deleting anything!
            with self.assertRaisesRegex(KeelError, "refusing to supersede"):
                _run_cli(["branch", "supersede", "task-a", "--archive-task", "--task-file", task_b_rel, *project])
            self.assertTrue(wt.is_dir())
            self.assertEqual(_git(repo, "rev-parse", "refs/heads/task/task-a"), head_sha)
            self.assertTrue((repo / task_a_rel).is_file())
            self.assertTrue((repo / task_b_rel).is_file())
            self.assertIsNone(load_lineage_state(repo / ".git", "task-a"))

            # Even if `state["task_file"]` were manually pointed at task B, `supersede` refuses!
            corrupted_state = load_branch_state(repo / ".git", "task/task-a")
            corrupted_state["task_file"] = task_b_rel
            save_branch_state(repo / ".git", "task/task-a", corrupted_state)
            with self.assertRaisesRegex(KeelError, "refusing to supersede"):
                _run_cli(["branch", "supersede", "task-a", "--archive-task", *project])
            self.assertTrue(wt.is_dir())
            self.assertTrue((repo / task_b_rel).is_file())
            corrupted_state["task_file"] = task_a_rel
            save_branch_state(repo / ".git", "task/task-a", corrupted_state)

            # Changing task A's blob on an alternate base ref (same merge-base) also refuses `supersede`!
            main_sha = _git(repo, "rev-parse", "main")
            _git(repo, "checkout", "-b", "alt-base", main_sha)
            (repo / task_a_rel).write_text(
                "# Altered Task A\n\n## Goal & Why\nChanged after arbiter.\n\n## Bounds\ncalc.py\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(repo, "commit", "-am", "docs(tasks): alter task-a on alt-base")
            with self.assertRaisesRegex(KeelError, "refusing to supersede"):
                _run_cli(["branch", "supersede", "task-a", "--base", "alt-base", "--archive-task", *project])
            _git(repo, "checkout", "main")

            # 3) Valid `supersede --archive-task` retires ONLY task A and leaves task B completely intact!
            sup_ok = _run_cli(["branch", "supersede", "task-a", "--archive-task", *project])
            self.assertTrue(sup_ok["superseded"])
            self.assertEqual(sup_ok["archived_task"], "docs/tasks/archive/20261117-task-a.md")
            self.assertFalse((repo / task_a_rel).exists())
            self.assertTrue((repo / task_b_rel).is_file())
            self.assertFalse(wt.exists())

    def test_round2_supersede_archive_task_hook_failure_rolls_back_cleanly_and_recovers(self) -> None:
        import keel_lib.branch_gate as bg
        from keel_lib.worktree import load_lineage_state

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _, task_rel = _init_repo_with_task(repo, slug="hook-sup")
            project = ["--project", str(repo)]
            wt = Path(_run_cli(["worktree", "create", "hook-sup", *project])["path"])
            (wt / "calc.py").write_text("HOOK_TEST = 1\n", encoding="utf-8")
            # Also update advisory `## Progress` on branch to verify post-commit retry handles `branch_spec != locked_spec`
            (wt / task_rel).write_text(
                (wt / task_rel).read_text(encoding="utf-8").rstrip() + "\n- [x] Tried draft\n",
                encoding="utf-8",
            )
            _git(wt, "add", ".")
            _git(wt, "commit", "-m", "feat: hook-sup draft")
            head_sha = _git(wt, "rev-parse", "HEAD")
            base_before = _git(repo, "rev-parse", "main")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "hook-sup", *project])
            _run_cli(["branch", "dispute", "hook-sup", *project, "--reason", "Needs replan"])

            def fake_arb_replan(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "replan",
                            "summary": "Split hook-sup into smaller slices.",
                            "directives": ["Split parser and runner"],
                            "avoid_dead_ends": ["Coupled runner"],
                            "waived_findings": [],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with mock.patch.object(AgyExecutor, "execute", fake_arb_replan):
                _run_cli(["branch", "arbitrate", "hook-sup", *project])

            # 1) Install failing `.git/hooks/commit-msg`!
            hook_path = repo / ".git" / "hooks" / "commit-msg"
            hook_path.parent.mkdir(parents=True, exist_ok=True)
            hook_path.write_text("#!/bin/sh\necho 'commit-msg rejected' >&2\nexit 1\n", encoding="utf-8")
            hook_path.chmod(0o755)

            with self.assertRaisesRegex(KeelError, "commit-msg rejected"):
                _run_cli(["branch", "supersede", "hook-sup", "--archive-task", *project])

            # `main` rolled back cleanly, branch/worktree/state intact, no ref or lineage written!
            self.assertEqual(_git(repo, "rev-parse", "main"), base_before)
            self.assertEqual(_git(repo, "status", "--porcelain", "-uall"), "")
            self.assertTrue((repo / task_rel).is_file())
            self.assertFalse((repo / "docs/tasks/archive/20261117-hook-sup.md").exists())
            self.assertTrue(wt.is_dir())
            self.assertEqual(_git(wt, "status", "--porcelain", "-uall"), "")
            self.assertEqual(_git(repo, "rev-parse", "refs/heads/task/hook-sup"), head_sha)
            self.assertEqual(_git(repo, "for-each-ref", "refs/keel/superseded/hook-sup"), "")
            self.assertIsNone(load_lineage_state(repo / ".git", "hook-sup"))
            self.assertEqual(load_branch_state(repo / ".git", "task/hook-sup")["arbitration"]["action"], "replan")

            # 2) Fix the hook; also verify that if teardown fails AFTER the archive commit succeeds, retry finishes cleanly with `recovery_count == 1`!
            hook_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            hook_path.chmod(0o755)

            with (
                mock.patch.object(bg, "_remove_worktree_and_branch_after_land", side_effect=KeelError("transient teardown error")),
                self.assertRaisesRegex(KeelError, "transient teardown error"),
            ):
                _run_cli(["branch", "supersede", "hook-sup", "--archive-task", *project])
            commit_after_first = _git(repo, "rev-parse", "main")
            self.assertNotEqual(commit_after_first, base_before)
            self.assertEqual(load_lineage_state(repo / ".git", "hook-sup")["recovery_count"], 1)

            sup_ok = _run_cli(["branch", "supersede", "hook-sup", "--archive-task", *project])
            self.assertTrue(sup_ok["superseded"])
            self.assertEqual(sup_ok["recovery_count"], 1)
            self.assertEqual(sup_ok["archived_task"], "docs/tasks/archive/20261117-hook-sup.md")
            self.assertEqual(sup_ok["supersede_commit_sha"], commit_after_first)
            self.assertEqual(_git(repo, "rev-parse", "main"), commit_after_first)
            self.assertEqual(_git(repo, "status", "--porcelain", "-uall"), "")
            self.assertFalse(wt.exists())
            self.assertEqual(load_lineage_state(repo / ".git", "hook-sup")["recovery_count"], 1)

    def test_round2_arbiter_closed_round_across_rework_once_approve_and_multi_cycle_rebuttals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="cyc-rw")
            project = ["--project", str(repo)]

            # Part A: `2 L4 rejections -> rework_once -> bonus round approve -> new commit` verifies cleanly without hitting old cap!
            wt_rw = Path(_run_cli(["worktree", "create", "cyc-rw", *project])["path"])
            (wt_rw / "calc.py").write_text("RW = 1\n", encoding="utf-8")
            _git(wt_rw, "commit", "-am", "feat: rw 1")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "cyc-rw", "--level", "L4", *project])
                _run_cli(["branch", "verify", "cyc-rw", *project])

            def fake_arb_rework_once(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "rework_once",
                            "summary": "One surgical L4 fix.",
                            "directives": ["CYCLE_SURGICAL_FIX: update calc.py"],
                            "avoid_dead_ends": [],
                            "waived_findings": [],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with mock.patch.object(AgyExecutor, "execute", fake_arb_rework_once):
                _run_cli(["branch", "arbitrate", "cyc-rw", *project])

            (wt_rw / "calc.py").write_text("RW = 2\n", encoding="utf-8")
            _git(wt_rw, "commit", "-am", "fix: surgical rw 2")
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                bonus_app = _run_cli(["branch", "verify", "cyc-rw", *project])
            self.assertEqual((bonus_app["outcome"], bonus_app["level"]), ("approve", "L4"))
            st_after_bonus = load_branch_state(repo / ".git", "task/cyc-rw")
            self.assertEqual(st_after_bonus["arbiter_closed_round"], 3)
            self.assertFalse(st_after_bonus["arbiter_rework_used"])
            self.assertIsNone(st_after_bonus["arbiter_rework_after_round"])

            # Follow-up commit after approved bonus round starts a clean cycle without hitting the old cap!
            (wt_rw / "calc.py").write_text("RW = 3\n", encoding="utf-8")
            _git(wt_rw, "commit", "-am", "feat: post-bonus follow-up commit")
            st_next = _run_cli(["branch", "status", "cyc-rw", *project])
            self.assertEqual(st_next["request_changes_rounds"], 0)
            self.assertFalse(any("convergence cap" in b.lower() for b in st_next["blockers"]))
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                next_v = _run_cli(["branch", "verify", "cyc-rw", *project])
            self.assertEqual((next_v["outcome"], next_v["level"]), ("approve", "L3"))

            # Part B: `arbiter-approve -> new commit -> rework_once -> verify` does NOT resurrect closed cycle's rebuttal or blockers!
            iso_task_rel = "docs/tasks/active/20261117-cyc-iso.md"
            (repo / iso_task_rel).write_text(
                "# Cyc Iso\n\n## Goal & Why\nIsolate cycles.\n\n## Bounds\ncalc.py\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(repo, "add", iso_task_rel)
            _git(repo, "commit", "-m", "docs(tasks): open cyc-iso")
            wt_iso = Path(_run_cli(["worktree", "create", "cyc-iso", *project])["path"])
            (wt_iso / "calc.py").write_text("ISO = 1\n", encoding="utf-8")
            (wt_iso / iso_task_rel).write_text(
                (wt_iso / iso_task_rel).read_text(encoding="utf-8").rstrip()
                + "\n\n## Decisions & Findings\n\n### Review Rebuttals\nCLOSED_CYCLE_1_MD_REBUTTAL\n",
                encoding="utf-8",
            )
            _git(wt_iso, "add", ".")
            _git(wt_iso, "commit", "-m", "feat: cycle 1 commit with markdown rebuttal")
            h1_sha = _git(wt_iso, "rev-parse", "HEAD")

            def reject_with(blocker_token: str, capture_list: list[str] | None = None) -> Any:
                def _exec(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                    if capture_list is not None:
                        capture_list.append(req.brief_path.read_text(encoding="utf-8"))
                    req.output_path.write_text(
                        json.dumps(
                            {
                                "verdict": "request_changes",
                                "summary": f"Rejected: {blocker_token}",
                                "blocking": [blocker_token],
                                "notes": [],
                                "commands_run": ["git diff"],
                            }
                        ),
                        encoding="utf-8",
                    )
                    return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-v"}
                return _exec

            with mock.patch.object(CodexExecutor, "execute", reject_with("CLOSED_CYCLE_1_BLOCKER")):
                _run_cli(["branch", "verify", "cyc-iso", *project])
            _run_cli(["branch", "dispute", "cyc-iso", *project, "--reason", "CLOSED_CYCLE_1_DISPUTE"])

            def fake_arb_approve(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "approve",
                            "summary": "Approved Cycle 1.",
                            "directives": [],
                            "avoid_dead_ends": [],
                            "waived_findings": ["CLOSED_CYCLE_1_BLOCKER"],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with mock.patch.object(AgyExecutor, "execute", fake_arb_approve):
                _run_cli(["branch", "arbitrate", "cyc-iso", *project])
            st_c1 = load_branch_state(repo / ".git", "task/cyc-iso")
            self.assertEqual((st_c1["arbiter_closed_round"], st_c1["arbiter_closed_head_sha"]), (1, h1_sha))

            # Cycle 2: commit `H2` without touching `Review Rebuttals`, hit 2 L4 rejections, get `rework_once` (replacing `state["arbitration"]`!)
            (wt_iso / "calc.py").write_text("ISO = 2\n", encoding="utf-8")
            _git(wt_iso, "commit", "-am", "feat: cycle 2 code change")
            with mock.patch.object(CodexExecutor, "execute", reject_with("ACTIVE_CYCLE_2_BLOCKER_1")):
                _run_cli(["branch", "verify", "cyc-iso", "--level", "L4", *project])
            with mock.patch.object(CodexExecutor, "execute", reject_with("ACTIVE_CYCLE_2_BLOCKER_2")):
                _run_cli(["branch", "verify", "cyc-iso", *project])

            arb_c2_briefs: list[str] = []

            def fake_arb_c2_rework(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                arb_c2_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                return fake_arb_rework_once(adapter, req, defaults)

            with mock.patch.object(AgyExecutor, "execute", fake_arb_c2_rework):
                _run_cli(["branch", "arbitrate", "cyc-iso", *project])
            self.assertNotIn("CLOSED_CYCLE_1_BLOCKER", arb_c2_briefs[-1])
            self.assertNotIn("CLOSED_CYCLE_1_DISPUTE", arb_c2_briefs[-1])

            # Bonus verify in Cycle 2 AFTER `rework_once` replaced `arbitration`:
            # MUST NOT resurrect Cycle 1's blockers or Cycle 1's `CLOSED_CYCLE_1_DISPUTE` or treat `CLOSED_CYCLE_1_MD_REBUTTAL` as active!
            (wt_iso / "calc.py").write_text("ISO = 3\n", encoding="utf-8")
            _git(wt_iso, "commit", "-am", "fix: cycle 2 surgical fix")
            c2_bonus_briefs: list[str] = []

            def capture_approve(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                c2_bonus_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                return verifier("approve")(adapter, req, defaults)

            with mock.patch.object(CodexExecutor, "execute", capture_approve):
                c2_ok = _run_cli(["branch", "verify", "cyc-iso", *project])
            self.assertEqual(c2_ok["outcome"], "approve")
            self.assertIn("ACTIVE_CYCLE_2_BLOCKER_2", c2_bonus_briefs[-1])
            self.assertIn("CYCLE_SURGICAL_FIX: update calc.py", c2_bonus_briefs[-1])
            self.assertNotIn("CLOSED_CYCLE_1_BLOCKER", c2_bonus_briefs[-1])
            self.assertNotIn("CLOSED_CYCLE_1_DISPUTE", c2_bonus_briefs[-1])

            # And starting Cycle 3 after Cycle 2's bonus approve still keeps `CLOSED_CYCLE_1_MD_REBUTTAL` closed!
            (wt_iso / "calc.py").write_text("ISO = 4\n", encoding="utf-8")
            _git(wt_iso, "commit", "-am", "feat: cycle 3 commit without new rebuttal")
            with (
                mock.patch.object(CodexExecutor, "execute", reject_with("CYCLE_3_BLOCKER")),
                mock.patch.object(AgyExecutor, "execute", side_effect=AssertionError("Closed Cycle 1 rebuttal resurrected!")),
            ):
                _run_cli(["branch", "verify", "cyc-iso", "--arbitrate", *project])
                c3_r2 = _run_cli(["branch", "verify", "cyc-iso", "--arbitrate", *project])
            self.assertEqual(c3_r2["outcome"], "request_changes")
            self.assertNotIn("arbitration", c3_r2)

    def test_round2_capped_pure_rebase_refresh_and_inline_arbitrate_without_deadlock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="cap-reb")
            project = ["--project", str(repo)]
            wt = Path(_run_cli(["worktree", "create", "cap-reb", *project])["path"])
            (wt / "calc.py").write_text("REB = 1\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: reb 1")

            # Hit convergence cap (`2 L4 rejections`); sneaking a code edit on the same `merge_base` stays blocked!
            pre_rebase_sha = _git(wt, "rev-parse", "HEAD")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "cap-reb", "--level", "L4", *project])
                _run_cli(["branch", "verify", "cap-reb", *project])
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "cap-reb", *project])
                (wt / "calc.py").write_text("REB = 999\n", encoding="utf-8")
                _git(wt, "commit", "--amend", "-am", "feat: sneaked edit on same merge-base")
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "cap-reb", *project])
            _git(wt, "reset", "--hard", pre_rebase_sha)

            # Unrelated commit lands on `main` -> `arbitrate` requires rebasing onto `main`
            (repo / "UNRELATED.txt").write_text("main moved\n", encoding="utf-8")
            _git(repo, "add", "UNRELATED.txt")
            _git(repo, "commit", "-m", "docs: advance main while branch is at cap")
            with self.assertRaisesRegex(KeelError, "behind 'main' tip"):
                _run_cli(["branch", "arbitrate", "cap-reb", *project])

            # Rebasing onto `main`: below-L4 level is refused on the capped base-advance refresh!
            _git(wt, "rebase", "main")
            pure_rebased_sha = _git(wt, "rev-parse", "HEAD")
            with self.assertRaisesRegex(KeelError, "L4"):
                _run_cli(["branch", "verify", "cap-reb", "--level", "L3", *project])

            def fake_arb_approve(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "approve",
                            "summary": "Approved rebased commit cleanly.",
                            "directives": [],
                            "avoid_dead_ends": [],
                            "waived_findings": ["Pedantic nit"],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            # `verify --arbitrate` refreshes verification on `pure_rebased_sha` at L4 and arbitrates without deadlock!
            with (
                mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                mock.patch.object(AgyExecutor, "execute", fake_arb_approve),
            ):
                refreshed = _run_cli(["branch", "verify", "cap-reb", "--arbitrate", *project])
            self.assertEqual((refreshed["outcome"], refreshed["level"], refreshed["head_sha"]), ("approve", "L4", pure_rebased_sha))
            self.assertEqual(refreshed["arbitration"]["action"], "approve")
            self.assertTrue(_run_cli(["branch", "status", "cap-reb", *project])["ready_to_land"])

            # Also verify two-step `verify` -> second `verify` blocked (even if commit message amended) -> `arbitrate`!
            (repo / "docs/tasks/active/20261117-cap-reb-2.md").write_text(
                "# Cap Reb 2\n\n## Goal & Why\nTwo-step.\n\n## Bounds\ncalc.py\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(repo, "add", "docs/tasks/active/20261117-cap-reb-2.md")
            _git(repo, "commit", "-m", "docs(tasks): open cap-reb-2")
            wt2 = Path(_run_cli(["worktree", "create", "cap-reb-2", *project])["path"])
            (wt2 / "calc.py").write_text("REB2 = 1\n", encoding="utf-8")
            _git(wt2, "commit", "-am", "feat: reb2")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "cap-reb-2", "--level", "L4", *project])
                _run_cli(["branch", "verify", "cap-reb-2", *project])
            (repo / "UNRELATED2.txt").write_text("main moved 2\n", encoding="utf-8")
            _git(repo, "add", "UNRELATED2.txt")
            _git(repo, "commit", "-m", "docs: advance main again")
            _git(wt2, "rebase", "main")
            reb2_sha = _git(wt2, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                v_once = _run_cli(["branch", "verify", "cap-reb-2", *project])
                self.assertEqual((v_once["outcome"], v_once["level"]), ("request_changes", "L4"))
                # Cannot loop a second time on the same `merge_base_sha`, even if rewording the commit!
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "cap-reb-2", *project])
                _git(wt2, "commit", "--amend", "-m", "feat: reb2 reworded")
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "cap-reb-2", *project])
                _git(wt2, "reset", "--hard", reb2_sha)

            with mock.patch.object(AgyExecutor, "execute", fake_arb_approve):
                arb2 = _run_cli(["branch", "arbitrate", "cap-reb-2", *project])
            self.assertEqual(arb2["action"], "approve")

    def test_round2_duplicate_arbitration_rejected_and_unused_rework_once_not_clamped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="dup-arb")
            project = ["--project", str(repo)]
            wt = Path(_run_cli(["worktree", "create", "dup-arb", *project])["path"])
            (wt / "calc.py").write_text("DUP = 1\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: dup 1")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "dup-arb", "--level", "L4", *project])
                _run_cli(["branch", "verify", "dup-arb", *project])

            def fake_arb_rework_once(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "rework_once",
                            "summary": "Surgical fix.",
                            "directives": ["Set DUP = 2"],
                            "avoid_dead_ends": [],
                            "waived_findings": [],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with mock.patch.object(AgyExecutor, "execute", fake_arb_rework_once):
                arb1 = _run_cli(["branch", "arbitrate", "dup-arb", *project])
            self.assertEqual((arb1["action"], arb1["clamped_from"]), ("rework_once", None))

            # 1) Calling `branch arbitrate` a SECOND time on the same rejection round raises `KeelError`
            #    instead of clamping the unused `rework_once` to `pivot` or verdict shopping for `approve`!
            with (
                mock.patch.object(AgyExecutor, "execute", side_effect=AssertionError("Duplicate arbitrate must not launch!")),
                self.assertRaisesRegex(KeelError, "already arbitrated \\(action: 'rework_once'\\)"),
            ):
                _run_cli(["branch", "arbitrate", "dup-arb", *project])
            self.assertEqual(load_branch_state(repo / ".git", "task/dup-arb")["arbitration"]["action"], "rework_once")

            # 2) Even with explicit `--force-round` BEFORE any subsequent `request_changes` round ran,
            #    `rework_once` is NOT treated as consumed (not clamped to `pivot`)!
            with mock.patch.object(AgyExecutor, "execute", fake_arb_rework_once):
                arb_forced = _run_cli(["branch", "arbitrate", "dup-arb", "--force-round", *project])
            self.assertEqual((arb_forced["action"], arb_forced["clamped_from"]), ("rework_once", None))

            # 3) Once the bonus `request_changes` round actually runs, `rework_once` IS consumed -> clamped to `pivot`!
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "dup-arb", *project])
            with mock.patch.object(AgyExecutor, "execute", fake_arb_rework_once):
                arb_clamped = _run_cli(["branch", "arbitrate", "dup-arb", *project])
            self.assertEqual((arb_clamped["action"], arb_clamped["clamped_from"]), ("pivot", "rework_once"))

            # Calling `branch arbitrate` again on that `pivot`-adjudicated round is ALSO refused!
            with (
                mock.patch.object(AgyExecutor, "execute", side_effect=AssertionError("Duplicate arbitrate must not launch!")),
                self.assertRaisesRegex(KeelError, "already arbitrated \\(action: 'pivot'\\)"),
            ):
                _run_cli(["branch", "arbitrate", "dup-arb", *project])

    def test_round3_land_squash_and_ff_only_hook_failure_rolls_back_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _, task_rel = _init_repo_with_task(repo, slug="land-hook")
            project = ["--project", str(repo)]
            hook_path = repo / ".git" / "hooks" / "commit-msg"
            hook_path.parent.mkdir(parents=True, exist_ok=True)

            def fake_arb_approve(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "approve",
                            "summary": "Approved cleanly.",
                            "directives": [],
                            "avoid_dead_ends": [],
                            "waived_findings": ["Minor style nit"],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            # 1) `--ff-only --execute` with active task archive under failing `commit-msg` hook:
            wt_ff = Path(_run_cli(["worktree", "create", "land-hook", *project])["path"])
            (wt_ff / "calc.py").write_text("FF_HOOK = 1\n", encoding="utf-8")
            _git(wt_ff, "commit", "-am", "feat: ff hook test")
            ff_head = _git(wt_ff, "rev-parse", "HEAD")
            base_before_ff = _git(repo, "rev-parse", "main")
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                _run_cli(["branch", "verify", "land-hook", *project])

            hook_path.write_text("#!/bin/sh\necho 'land hook failed' >&2\nexit 1\n", encoding="utf-8")
            hook_path.chmod(0o755)
            with self.assertRaisesRegex(KeelError, "land hook failed"):
                _run_cli(["branch", "land", "land-hook", "--ff-only", "--execute", *project])
            self.assertEqual(_git(repo, "rev-parse", "main"), base_before_ff)
            self.assertEqual(_git(repo, "status", "--porcelain", "-uall"), "")
            self.assertTrue((repo / task_rel).is_file())
            self.assertFalse((repo / "docs/tasks/archive/20261117-land-hook.md").exists())
            self.assertTrue(wt_ff.is_dir())
            self.assertEqual(_git(repo, "rev-parse", "refs/heads/task/land-hook"), ff_head)

            hook_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            hook_path.chmod(0o755)
            ff_ok = _run_cli(["branch", "land", "land-hook", "--ff-only", "--execute", *project])
            self.assertTrue(ff_ok["landed"])
            self.assertEqual(ff_ok["archived_task"], "docs/tasks/archive/20261117-land-hook.md")

            # 2) Taskless `--ff-only --execute` with `Keel-Arbitrated:` `--allow-empty` audit commit under failing hook:
            wt_audit = Path(_run_cli(["worktree", "create", "audit-hook", *project])["path"])
            (wt_audit / "calc.py").write_text("AUDIT_HOOK = 2\n", encoding="utf-8")
            _git(wt_audit, "commit", "-am", "fix: taskless audit hook")
            base_before_audit = _git(repo, "rev-parse", "main")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "audit-hook", "--why", "Taskless fix.", *project])
            _run_cli(["branch", "dispute", "audit-hook", *project, "--reason", "Clean fix"])
            with mock.patch.object(AgyExecutor, "execute", fake_arb_approve):
                arb_audit = _run_cli(["branch", "arbitrate", "audit-hook", "--why", "Taskless fix.", *project])

            hook_path.write_text("#!/bin/sh\necho 'audit hook failed' >&2\nexit 1\n", encoding="utf-8")
            with self.assertRaisesRegex(KeelError, "audit hook failed"):
                _run_cli(["branch", "land", "audit-hook", "--ff-only", "--execute", *project])
            self.assertEqual(_git(repo, "rev-parse", "main"), base_before_audit)
            self.assertEqual(_git(repo, "status", "--porcelain", "-uall"), "")
            self.assertTrue(wt_audit.is_dir())

            hook_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            audit_ok = _run_cli(["branch", "land", "audit-hook", "--ff-only", "--execute", *project])
            self.assertTrue(audit_ok["landed"])
            self.assertIn(f"Keel-Arbitrated: approve ({arb_audit['run_id']})", _git(repo, "log", "-1", "--format=%B"))

            # 3) `--squash --execute` under failing `commit-msg` hook rolls back staged squash & new files cleanly:
            sq_task_rel = "docs/tasks/active/20261117-sq-hook.md"
            (repo / sq_task_rel).write_text(
                "# Sq Hook\n\n## Goal & Why\nSquash rollback.\n\n## Bounds\ncalc.py\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(repo, "add", sq_task_rel)
            _git(repo, "commit", "-m", "docs(tasks): open sq-hook")
            base_before_sq = _git(repo, "rev-parse", "main")
            wt_sq = Path(_run_cli(["worktree", "create", "sq-hook", *project])["path"])
            (wt_sq / "new_mod.py").write_text("NEW_MOD = 1\n", encoding="utf-8")
            _git(wt_sq, "add", "new_mod.py")
            _git(wt_sq, "commit", "-m", "feat: add new_mod")
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                _run_cli(["branch", "verify", "sq-hook", *project])

            hook_path.write_text("#!/bin/sh\necho 'squash hook failed' >&2\nexit 1\n", encoding="utf-8")
            with self.assertRaisesRegex(KeelError, "squash hook failed"):
                _run_cli(["branch", "land", "sq-hook", "--squash", "--execute", *project])
            self.assertEqual(_git(repo, "rev-parse", "main"), base_before_sq)
            self.assertEqual(_git(repo, "status", "--porcelain", "-uall"), "")
            self.assertFalse((repo / "new_mod.py").exists())
            self.assertTrue((repo / sq_task_rel).is_file())
            self.assertTrue(wt_sq.is_dir())

            hook_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            sq_ok = _run_cli(["branch", "land", "sq-hook", "--squash", "--execute", *project])
            self.assertTrue(sq_ok["landed"])
            self.assertTrue((repo / "new_mod.py").is_file())

    def test_round3_capped_pure_rebase_approve_closes_cycle_for_followup_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="reb-close")
            project = ["--project", str(repo)]
            wt = Path(_run_cli(["worktree", "create", "reb-close", *project])["path"])
            (wt / "calc.py").write_text("RC = 1\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: rc 1")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "reb-close", "--level", "L4", *project])
                _run_cli(["branch", "verify", "reb-close", *project])

            (repo / "MAIN_NOTE.md").write_text("advance main\n", encoding="utf-8")
            _git(repo, "add", "MAIN_NOTE.md")
            _git(repo, "commit", "-m", "docs: advance main")
            _git(wt, "rebase", "main")
            rebased_sha = _git(wt, "rev-parse", "HEAD")

            # Capped pure-rebase refresh approves directly -> closes the pre-rebase capped cycle!
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                reb_app = _run_cli(["branch", "verify", "reb-close", *project])
            self.assertEqual((reb_app["outcome"], reb_app["level"]), ("approve", "L4"))
            st_closed = load_branch_state(repo / ".git", "task/reb-close")
            self.assertEqual((st_closed["arbiter_closed_round"], st_closed["arbiter_closed_head_sha"]), (3, rebased_sha))

            # Follow-up commit on the branch starts a clean cycle instead of deadlocking at the old cap!
            (wt / "calc.py").write_text("RC = 2\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: follow-up after rebased approve")
            st_follow = _run_cli(["branch", "status", "reb-close", *project])
            self.assertEqual(st_follow["request_changes_rounds"], 0)
            self.assertFalse(any("convergence cap" in b.lower() for b in st_follow["blockers"]))
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                follow_v = _run_cli(["branch", "verify", "reb-close", *project])
            self.assertEqual((follow_v["outcome"], follow_v["level"]), ("approve", "L3"))

    def test_round3_arbiter_approve_preserves_l4_last_write_floor_on_reverify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="l4-floor")
            project = ["--project", str(repo)]
            wt = Path(_run_cli(["worktree", "create", "l4-floor", *project])["path"])

            def agy_l4_writer(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                (req.workdir / "calc.py").write_text("L4_VAL = 42\n", encoding="utf-8")
                _git(req.workdir, "commit", "-am", "feat: L4 writer commit")
                return {"status": "completed", "executor": "agy", "level": req.level, "model": "test-agy"}

            with mock.patch.object(AgyExecutor, "execute", agy_l4_writer):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt), "--executor", "agy",
                    "--mode", "write", "--preset", "implement", "--level", "L4",
                    "--task-file", str(repo / "docs/tasks/active/20261117-l4-floor.md"),
                ])

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "l4-floor", *project])
            _run_cli(["branch", "dispute", "l4-floor", *project, "--reason", "Valid L4 implementation"])

            def fake_arb_approve(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "approve",
                            "summary": "Waive false-positive finding.",
                            "directives": [],
                            "avoid_dead_ends": [],
                            "waived_findings": ["False positive"],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with mock.patch.object(CodexExecutor, "execute", fake_arb_approve):
                _run_cli(["branch", "arbitrate", "l4-floor", *project])

            # `arbiter-approve` closed `rounds[-1]` on the unchanged L4 commit: `--level L2`/`L3` MUST be rejected!
            for low_lvl in ("L2", "L3"):
                with self.assertRaisesRegex(KeelError, "requires --level L4"):
                    _run_cli(["branch", "verify", "l4-floor", "--level", low_lvl, *project])

            with (
                mock.patch.object(CodexExecutor, "execute", verifier("approve")),
                mock.patch.object(AgyExecutor, "execute", verifier("approve")),
            ):
                reverified = _run_cli(["branch", "verify", "l4-floor", "--dual", *project])
            self.assertEqual((reverified["outcome"], reverified["level"]), ("approve", "L4"))
            self.assertEqual(load_branch_state(repo / ".git", "task/l4-floor")["rounds"][-1]["implement_level"], "L4")

    def test_round3_lineage_preserved_across_colliding_slug_land_and_discard(self) -> None:
        from keel_lib.worktree import load_lineage_state

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="feature")
            project = ["--project", str(repo)]
            wt_feat = Path(_run_cli(["worktree", "create", "feature", *project])["path"])
            (wt_feat / "calc.py").write_text("FEAT = 1\n", encoding="utf-8")
            _git(wt_feat, "commit", "-am", "feat: attempt 1")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "feature", *project])
            _run_cli(["branch", "dispute", "feature", *project, "--reason", "Need split"])

            def fake_arb_replan(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "replan",
                            "summary": "Split feature.",
                            "directives": ["FEATURE_SPLIT_DIRECTIVE"],
                            "avoid_dead_ends": ["FEATURE_DEAD_END"],
                            "waived_findings": [],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with mock.patch.object(AgyExecutor, "execute", fake_arb_replan):
                _run_cli(["branch", "arbitrate", "feature", *project])
            _run_cli(["branch", "supersede", "feature", *project])
            self.assertEqual(load_lineage_state(repo / ".git", "feature")["branch"], "task/feature")

            # 1) Unrelated `hotfix/feature` (derived slug `"feature"`, different branch) discarded -> `lineage/feature.json` stays intact!
            wt_hotfix = Path(_run_cli(["worktree", "create", "hotfix-wt", "--branch", "hotfix/feature", *project])["path"])
            self.assertNotIn("superseded_from", load_branch_state(repo / ".git", "hotfix/feature"))
            (wt_hotfix / "calc.py").write_text("HOTFIX = 1\n", encoding="utf-8")
            _git(wt_hotfix, "commit", "-am", "fix: hotfix")
            _run_cli(["branch", "land", "hotfix/feature", "--discard", "--reason", "Obsolete hotfix", *project])
            self.assertIsNotNone(load_lineage_state(repo / ".git", "feature"))

            # 2) Unrelated `fix/feature` (derived slug `"feature"`, different branch) landed -> `lineage/feature.json` STILL stays intact!
            wt_fix = Path(_run_cli(["worktree", "create", "fix-wt", "--branch", "fix/feature", *project])["path"])
            (wt_fix / "calc.py").write_text("FIX = 2\n", encoding="utf-8")
            _git(wt_fix, "commit", "-am", "fix: unrelated fix/feature")
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                _run_cli(["branch", "verify", "fix/feature", "--why", "Unrelated bugfix.", *project])
            _run_cli(["branch", "land", "fix/feature", "--execute", *project])
            self.assertIsNotNone(load_lineage_state(repo / ".git", "feature"))

            # 3) `worktree create child --supersedes feature` continues to work cleanly!
            child_res = _run_cli(["worktree", "create", "child", "--supersedes", "feature", *project])
            self.assertEqual(child_res["recovery_count"], 1)
            self.assertEqual(load_branch_state(repo / ".git", "task/child")["avoid_dead_ends"], ["FEATURE_DEAD_END"])

            # 4) Landing the direct same-slug successor `task/feature` itself DOES clean up `lineage/feature.json`!
            wt_succ = Path(_run_cli(["worktree", "create", "feature", *project])["path"])
            (wt_succ / "calc.py").write_text("FEAT_CLEAN = 3\n", encoding="utf-8")
            _git(wt_succ, "commit", "-am", "feat: clean rewrite of task/feature")
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                _run_cli(["branch", "verify", "feature", *project])
            _run_cli(["branch", "land", "feature", "--execute", *project])
            self.assertIsNone(load_lineage_state(repo / ".git", "feature"))

    def test_round3_arbitrate_with_output_dir_uses_disposable_clone_and_allows_canary_restore(self) -> None:
        from keel_lib.env import is_disposable_verify_path
        from keel_lib.guard_rules import evaluate_command

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="arb-outdir")
            project = ["--project", str(repo)]
            wt = Path(_run_cli(["worktree", "create", "arb-outdir", *project])["path"])
            (wt / "calc.py").write_text("OUT = 1\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: out 1")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "arb-outdir", *project])
            _run_cli(["branch", "dispute", "arb-outdir", *project, "--reason", "Valid implementation"])

            ordinary_out = Path(tmp) / "evidence" / "arb-run"
            preexisting_out = Path(tmp) / "evidence" / "already-populated"
            preexisting_out.mkdir(parents=True, exist_ok=True)
            (preexisting_out / "arbiter.json").write_text("{}\n", encoding="utf-8")
            with (
                mock.patch.object(AgyExecutor, "execute", side_effect=AssertionError("Must fail before launching Arbiter!")),
                self.assertRaisesRegex(KeelError, "Arbiter output already exists"),
            ):
                _run_cli(["branch", "arbitrate", "arb-outdir", "--output-dir", str(preexisting_out), *project])

            captured_arb_cwds: list[Path] = []

            def probing_arbiter(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                captured_arb_cwds.append(req.workdir)
                self.assertTrue(is_disposable_verify_path(req.workdir), req.workdir)
                self.assertIsNone(
                    evaluate_command("git restore calc.py", mode="read", cwd=str(req.workdir)),
                    f"guard_rules blocked Canary restore inside {req.workdir}",
                )
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "approve",
                            "summary": "Canary probe succeeded inside disposable clone.",
                            "directives": [],
                            "avoid_dead_ends": [],
                            "waived_findings": ["False positive"],
                            "commands_run": ["git restore calc.py"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with mock.patch.object(AgyExecutor, "execute", probing_arbiter):
                res = _run_cli(["branch", "arbitrate", "arb-outdir", "--output-dir", str(ordinary_out), *project])
            self.assertEqual(res["action"], "approve")
            self.assertEqual(len(captured_arb_cwds), 1)
            self.assertFalse(captured_arb_cwds[0].exists())
            self.assertTrue((ordinary_out / "arbiter.json").is_file())
            self.assertTrue((ordinary_out / "brief.md").is_file())
            self.assertEqual(json.loads((ordinary_out / "arbiter.json").read_text(encoding="utf-8"))["action"], "approve")

            # Also verify inline `branch verify --arbitrate --output-dir <ordinary_dir>` preserves disposable Arbiter clone!
            (wt / "calc.py").write_text("OUT = 2\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: out 2")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "arb-outdir", *project])
            _run_cli(["branch", "dispute", "arb-outdir", *project, "--reason", "Valid out 2"])
            inline_out = Path(tmp) / "evidence" / "inline-verify-arb"
            with (
                mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                mock.patch.object(AgyExecutor, "execute", probing_arbiter),
            ):
                inline_res = _run_cli(["branch", "verify", "arb-outdir", "--arbitrate", "--output-dir", str(inline_out), *project])
            self.assertEqual(inline_res["outcome"], "approve")
            self.assertEqual(len(captured_arb_cwds), 2)
            self.assertFalse(captured_arb_cwds[1].exists())
            self.assertTrue((inline_out / "arbiter" / "arbiter.json").is_file())
            self.assertTrue((inline_out / "arbiter" / "brief.md").is_file())

    def test_round4_superseded_task_ownership_protects_active_spec_from_unrelated_branch(self) -> None:
        from keel_lib.worktree import branch_state_path, load_lineage_for_branch

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _, task_rel = _init_repo_with_task(repo, slug="parser")
            project = ["--project", str(repo)]
            wt_task = Path(_run_cli(["worktree", "create", "parser", *project])["path"])
            (wt_task / "calc.py").write_text("PARSER = 1\n", encoding="utf-8")
            _git(wt_task, "commit", "-am", "feat: parser attempt 1")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "parser", *project])
            _run_cli(["branch", "dispute", "parser", *project, "--reason", "Need clean pivot"])

            def fake_arb_pivot(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "pivot",
                            "summary": "Pivot parser implementation.",
                            "directives": ["Use iterative tokenizer"],
                            "avoid_dead_ends": ["Recursive regex parser"],
                            "waived_findings": [],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with mock.patch.object(AgyExecutor, "execute", fake_arb_pivot):
                _run_cli(["branch", "arbitrate", "parser", *project])

            # Supersede `task/parser` WITHOUT `--archive-task`: spec remains active on `main` and owned by `task/parser`
            sup_res = _run_cli(["branch", "supersede", "parser", *project])
            self.assertIsNone(sup_res["archived_task"])
            self.assertTrue((repo / task_rel).is_file())
            self.assertEqual(load_branch_state(repo / ".git", "task/parser")["task_file"], task_rel)
            lin_task = load_lineage_for_branch(repo / ".git", "task/parser")
            assert lin_task is not None
            self.assertEqual((lin_task["task_file"], lin_task["archived_task"]), (task_rel, None))

            # Unrelated `fix/parser` cannot steal or discard `task/parser`'s active spec while `task/parser` is retired!
            wt_fix = Path(_run_cli(["worktree", "create", "fix-parser", "--branch", "fix/parser", *project])["path"])
            (wt_fix / "calc.py").write_text("FIX_PARSER = 2\n", encoding="utf-8")
            _git(wt_fix, "commit", "-am", "fix: unrelated fix/parser")
            st_fix = _run_cli(["branch", "status", "fix/parser", *project])
            self.assertIsNone(st_fix["task_file"])

            disc_fix = _run_cli(["branch", "land", "fix/parser", "--discard", "--reason", "Drop unrelated fix", *project])
            self.assertTrue(disc_fix["discarded"])
            self.assertIsNone(disc_fix["archived_task"])
            self.assertIsNone(disc_fix["discard_commit_sha"])
            self.assertTrue((repo / task_rel).is_file())

            # Even if `branches/task%2Fparser.json` is absent, `lineage/parser.json` alone still protects ownership!
            branch_state_path(repo / ".git", "task/parser").unlink()
            wt_hotfix = Path(_run_cli(["worktree", "create", "hotfix-parser", "--branch", "hotfix/parser", *project])["path"])
            (wt_hotfix / "calc.py").write_text("HOTFIX_PARSER = 3\n", encoding="utf-8")
            _git(wt_hotfix, "commit", "-am", "fix: hotfix/parser")
            self.assertIsNone(_run_cli(["branch", "status", "hotfix/parser", *project])["task_file"])
            disc_hotfix = _run_cli(["branch", "land", "hotfix/parser", "--discard", "--reason", "Drop hotfix", *project])
            self.assertIsNone(disc_hotfix["archived_task"])
            self.assertTrue((repo / task_rel).is_file())

            # Recreating `task/parser` cleanly discovers its active spec and inherits `recovery_count == 1`!
            wt_recreated = _run_cli(["worktree", "create", "parser", *project])
            self.assertEqual(wt_recreated["recovery_count"], 1)
            st_recreated = _run_cli(["branch", "status", "parser", *project])
            self.assertEqual(st_recreated["task_file"], task_rel)
            self.assertEqual(st_recreated["recovery_count"], 1)

    def test_round4_branch_keyed_lineage_isolates_same_slug_branches_and_explicit_supersedes(self) -> None:
        from keel_lib.worktree import lineage_state_path, load_lineage_for_branch

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="parser")
            project = ["--project", str(repo)]

            def make_arb(action: str, directive: str, dead_end: str) -> Any:
                def _arb(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                    req.output_path.write_text(
                        json.dumps(
                            {
                                "action": action,
                                "summary": f"{action}: {directive}",
                                "directives": [directive],
                                "avoid_dead_ends": [dead_end],
                                "waived_findings": [],
                                "commands_run": ["git diff"],
                            }
                        ),
                        encoding="utf-8",
                    )
                    return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}
                return _arb

            # 1) Supersede `task/parser` -> `recovery_count == 1`, stored at `lineage/parser.json`
            wt_task = Path(_run_cli(["worktree", "create", "parser", *project])["path"])
            (wt_task / "calc.py").write_text("TASK_P = 1\n", encoding="utf-8")
            _git(wt_task, "commit", "-am", "feat: task/parser")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "parser", *project])
            _run_cli(["branch", "dispute", "parser", *project, "--reason", "Split parser"])
            with mock.patch.object(AgyExecutor, "execute", make_arb("replan", "TASK_PARSER_DIR", "TASK_PARSER_DEAD")):
                _run_cli(["branch", "arbitrate", "parser", *project])
            sup_task = _run_cli(["branch", "supersede", "parser", *project])
            self.assertEqual(sup_task["recovery_count"], 1)
            self.assertTrue(lineage_state_path(repo / ".git", "task/parser").is_file())
            self.assertEqual(lineage_state_path(repo / ".git", "task/parser").name, "parser.json")

            # 2) Create and supersede `fix/parser` -> starts fresh (`recovery_count == 1`, NOT `2`!) in `custom+fix%2Fparser.json`
            wt_fix = Path(_run_cli(["worktree", "create", "fix-p", "--branch", "fix/parser", *project])["path"])
            self.assertEqual(load_branch_state(repo / ".git", "fix/parser").get("recovery_count", 0), 0)
            (wt_fix / "calc.py").write_text("FIX_P = 1\n", encoding="utf-8")
            _git(wt_fix, "commit", "-am", "fix: fix/parser")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "fix/parser", "--why", "Unrelated parser hotfix.", *project])
            _run_cli(["branch", "dispute", "fix/parser", *project, "--reason", "Pivot hotfix"])
            with mock.patch.object(AgyExecutor, "execute", make_arb("pivot", "FIX_PARSER_DIR", "FIX_PARSER_DEAD")):
                _run_cli(["branch", "arbitrate", "fix/parser", "--why", "Unrelated parser hotfix.", *project])
            sup_fix = _run_cli(["branch", "supersede", "fix/parser", *project])
            self.assertEqual(sup_fix["recovery_count"], 1)
            self.assertEqual(lineage_state_path(repo / ".git", "fix/parser").name, "custom+fix%2Fparser.json")
            self.assertEqual(load_lineage_for_branch(repo / ".git", "task/parser")["recovery_count"], 1)
            self.assertEqual(load_lineage_for_branch(repo / ".git", "fix/parser")["recovery_count"], 1)

            # 3) Bare `--supersedes parser` deterministically selects `task/parser`, while `--supersedes fix/parser` selects `fix/parser`!
            core_res = _run_cli(["worktree", "create", "parser-core", "--supersedes", "parser", *project])
            self.assertEqual(core_res["recovery_count"], 1)
            self.assertEqual(core_res["superseded_from"]["branch"], "task/parser")
            core_state = load_branch_state(repo / ".git", "task/parser-core")
            self.assertEqual(core_state["prior_directives"], ["TASK_PARSER_DIR"])
            self.assertEqual(core_state["avoid_dead_ends"], ["TASK_PARSER_DEAD"])

            fix_v2_res = _run_cli(
                ["worktree", "create", "fix-p2", "--branch", "fix/parser-v2", "--supersedes", "fix/parser", *project]
            )
            self.assertEqual(fix_v2_res["recovery_count"], 1)
            self.assertEqual(fix_v2_res["superseded_from"]["branch"], "fix/parser")
            fix_v2_state = load_branch_state(repo / ".git", "fix/parser-v2")
            self.assertEqual(fix_v2_state["prior_directives"], ["FIX_PARSER_DIR"])
            self.assertEqual(fix_v2_state["avoid_dead_ends"], ["FIX_PARSER_DEAD"])

            captured_briefs: list[str] = []

            def capture_agent(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                captured_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                return {"status": "completed", "executor": "agy", "level": req.level, "model": "test-agy"}

            with mock.patch.object(AgyExecutor, "execute", capture_agent):
                _run_cli([
                    "agent", "run", *project, "--supersedes", "refs/heads/fix/parser",
                    "--executor", "agy", "--mode", "read", "--level", "L2",
                    "--why", "Inspect fix/parser lineage.", "--task", "Check context.",
                ])
            self.assertIn("FIX_PARSER_DIR", captured_briefs[-1])
            self.assertNotIn("TASK_PARSER_DIR", captured_briefs[-1])

            # 4) Multiple non-`task/` branches sharing a bare slug without `task/<slug>` require explicit branch name!
            for prefix in ("fix", "chore"):
                wt_a = Path(_run_cli(["worktree", "create", f"{prefix}-amb", "--branch", f"{prefix}/ambig", *project])["path"])
                (wt_a / "calc.py").write_text(f"{prefix.upper()} = 1\n", encoding="utf-8")
                _git(wt_a, "commit", "-am", f"{prefix}: ambig")
                with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                    _run_cli(["branch", "verify", f"{prefix}/ambig", "--why", "Ambig test.", *project])
                _run_cli(["branch", "dispute", f"{prefix}/ambig", *project, "--reason", "Pivot"])
                with mock.patch.object(AgyExecutor, "execute", make_arb("pivot", f"{prefix.upper()}_DIR", f"{prefix.upper()}_DEAD")):
                    _run_cli(["branch", "arbitrate", f"{prefix}/ambig", "--why", "Ambig test.", *project])
                _run_cli(["branch", "supersede", f"{prefix}/ambig", *project])

            with self.assertRaisesRegex(KeelError, "Multiple superseded branches match --supersedes 'ambig'"):
                _run_cli(["worktree", "create", "ambig-child", "--supersedes", "ambig", *project])
            explicit_ambig = _run_cli(["worktree", "create", "ambig-child", "--supersedes", "chore/ambig", *project])
            self.assertEqual(explicit_ambig["superseded_from"]["branch"], "chore/ambig")

    def test_round4_dual_round2_blind_verifier_strips_review_rebuttals_and_carryforward(self) -> None:
        from keel_lib.context import extract_changed_markdown_sections

        # Direct unit checks for `exclude_headings=("Review Rebuttals",)`
        base_doc = "# Spec\n\n## Progress\n- [ ] Init\n\n## Decisions & Findings\nBaseline decision.\n"
        only_rebuttal_doc = (
            "# Spec\n\n## Progress\n- [ ] Init\n\n## Decisions & Findings\nBaseline decision.\n\n"
            "### Review Rebuttals\nDisputed Round 1 nit.\n"
        )
        self.assertIsNone(
            extract_changed_markdown_sections(
                base_doc,
                only_rebuttal_doc,
                ("Progress", "Decisions & Findings", "Review Rebuttals"),
                levels=(2, 3),
                exclude_headings=("Review Rebuttals",),
            )
        )
        mixed_doc = (
            "# Spec\n\n## Progress\n- [x] Done\n\n## Decisions & Findings\nBaseline decision.\nAdded non-rebuttal note.\n\n"
            "### Review Rebuttals\nNested rebuttal body.\n\n#### Evidence\nLine 12.\n\n"
            "### Complexity\nO(1) lookup.\n\n## Review Rebuttals\nTop-level rebuttal body.\n"
        )
        stripped_mixed = extract_changed_markdown_sections(
            base_doc,
            mixed_doc,
            ("Progress", "Decisions & Findings", "Review Rebuttals"),
            levels=(2, 3),
            exclude_headings=("Review Rebuttals",),
        )
        assert stripped_mixed is not None
        self.assertIn("## Progress\n\n- [x] Done", stripped_mixed)
        self.assertIn("Added non-rebuttal note.", stripped_mixed)
        self.assertIn("### Complexity\nO(1) lookup.", stripped_mixed)
        self.assertNotIn("Review Rebuttals", stripped_mixed)
        self.assertNotIn("Nested rebuttal body", stripped_mixed)
        self.assertNotIn("Top-level rebuttal body", stripped_mixed)

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _, task_rel = _init_repo_with_task(repo, slug="dual-blind")
            project = ["--project", str(repo)]
            wt = Path(_run_cli(["worktree", "create", "dual-blind", *project])["path"])

            def agy_writer(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                (req.workdir / "calc.py").write_text("BLIND = 1\n", encoding="utf-8")
                (req.workdir / task_rel).write_text(
                    (req.workdir / task_rel).read_text(encoding="utf-8").rstrip()
                    + "\n\n## Progress\n- [x] Implemented calc\n\n"
                    + "## Decisions & Findings\nNON_REBUTTAL_DECISION_NOTE\n\n"
                    + "### Review Rebuttals\nSECRET_COMMITTED_MD_REBUTTAL\n\n"
                    + "#### Rebuttal Detail\nSECRET_SUB_DETAIL\n\n"
                    + "### Performance Notes\nKEEP_PERF_SUBSECTION\n",
                    encoding="utf-8",
                )
                _git(req.workdir, "add", ".")
                _git(req.workdir, "commit", "-m", "feat: implement calc")
                return {"status": "completed", "executor": "agy", "level": req.level, "model": "test-agy"}

            with mock.patch.object(AgyExecutor, "execute", agy_writer):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt), "--executor", "agy",
                    "--mode", "write", "--preset", "implement", "--level", "L3",
                    "--task-file", str(repo / task_rel),
                ])

            def r1_reject(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "verdict": "request_changes",
                            "summary": "Round 1 issue.",
                            "blocking": ["ROUND1_PRIOR_BLOCKER_TOKEN"],
                            "notes": [],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-v"}

            with mock.patch.object(CodexExecutor, "execute", r1_reject):
                _run_cli(["branch", "verify", "dual-blind", *project])
            _run_cli(["branch", "dispute", "dual-blind", *project, "--reason", "STRUCTURED_DISPUTE_TOKEN"])

            dual_briefs: dict[str, str] = {}

            def capture_dual_approve(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                dual_briefs[adapter.name] = req.brief_path.read_text(encoding="utf-8")
                return verifier("approve")(adapter, req, defaults)

            with (
                mock.patch.object(CodexExecutor, "execute", capture_dual_approve),
                mock.patch.object(AgyExecutor, "execute", capture_dual_approve),
            ):
                r2_dual = _run_cli(["branch", "verify", "dual-blind", "--dual", *project])
            self.assertEqual((r2_dual["outcome"], r2_dual["level"]), ("approve", "L4"))

            primary_brief = dual_briefs["codex"]
            blind_brief = dual_briefs["agy"]
            blind_task_section = blind_brief.split("## Lens:")[0]

            # Primary diagonal verifier (`codex`) receives BOTH carry-forward ledger AND committed `### Review Rebuttals`
            self.assertIn("### Prior Review Carry-Forward Ledger", primary_brief)
            self.assertIn("ROUND1_PRIOR_BLOCKER_TOKEN", primary_brief)
            self.assertIn("STRUCTURED_DISPUTE_TOKEN", primary_brief)
            self.assertIn("### Review Rebuttals", primary_brief)
            self.assertIn("SECRET_COMMITTED_MD_REBUTTAL", primary_brief)
            self.assertIn("SECRET_SUB_DETAIL", primary_brief)
            self.assertIn("NON_REBUTTAL_DECISION_NOTE", primary_brief)
            self.assertIn("KEEP_PERF_SUBSECTION", primary_brief)

            # Secondary clean-slate blind verifier (`agy`) receives non-rebuttal context, ZERO carry-forward, ZERO rebuttals!
            self.assertIn("- [x] Implemented calc", blind_brief)
            self.assertIn("NON_REBUTTAL_DECISION_NOTE", blind_brief)
            self.assertIn("KEEP_PERF_SUBSECTION", blind_brief)
            self.assertNotIn("### Prior Review Carry-Forward Ledger", blind_brief)
            self.assertNotIn("Prior Review Carry-Forward Ledger", blind_task_section)
            self.assertNotIn("ROUND1_PRIOR_BLOCKER_TOKEN", blind_brief)
            self.assertNotIn("STRUCTURED_DISPUTE_TOKEN", blind_brief)
            self.assertNotIn("### Review Rebuttals", blind_brief)
            self.assertNotIn("Review Rebuttals", blind_task_section)
            self.assertNotIn("SECRET_COMMITTED_MD_REBUTTAL", blind_brief)
            self.assertNotIn("SECRET_SUB_DETAIL", blind_brief)

    def test_round4_land_audit_trail_matches_approval_source_when_redundant_human_override_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _, sq_task_rel = _init_repo_with_task(repo, slug="arb-sq-ovr")
            project = ["--project", str(repo)]

            def fake_arb_approve(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "approve",
                            "summary": "Arbiter approved cleanly.",
                            "directives": [],
                            "avoid_dead_ends": [],
                            "waived_findings": ["False positive blocker"],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            # 1) `--squash --execute` on `arbiter-approve` branch with redundant `--human-override`
            wt_sq = Path(_run_cli(["worktree", "create", "arb-sq-ovr", *project])["path"])
            (wt_sq / "calc.py").write_text("ARB_SQ = 1\n", encoding="utf-8")
            _git(wt_sq, "commit", "-am", "feat: arb squash with redundant human-override")
            sq_head = _git(wt_sq, "rev-parse", "HEAD")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "arb-sq-ovr", *project])
            _run_cli(["branch", "dispute", "arb-sq-ovr", *project, "--reason", "Clean squash"])
            with mock.patch.object(AgyExecutor, "execute", fake_arb_approve):
                arb_sq = _run_cli(["branch", "arbitrate", "arb-sq-ovr", *project])

            land_sq = _run_cli([
                "branch", "land", "arb-sq-ovr", "--squash",
                "--human-override", "Redundant CLI override text", "--execute", *project,
            ])
            self.assertTrue(land_sq["landed"])
            self.assertEqual(land_sq["approval_source"], f"arbiter-approve:{sq_head[:12]}")
            sq_msg = _git(repo, "log", "-1", "--format=%B")
            self.assertIn(f"Keel-Arbitrated: approve ({arb_sq['run_id']})", sq_msg)
            self.assertNotIn("Keel-Human-Override:", sq_msg)
            sq_arch_txt = (repo / "docs/tasks/archive" / Path(sq_task_rel).name).read_text(encoding="utf-8")
            self.assertIn("## Arbitration Record", sq_arch_txt)
            self.assertIn(arb_sq["run_id"], sq_arch_txt)

            # 2) `--ff-only --execute` on `arbiter-approve` branch with redundant `--human-override`
            ff_task_rel = "docs/tasks/active/20261117-arb-ff-ovr.md"
            (repo / ff_task_rel).write_text(
                "# Arb FF Ovr\n\n## Goal & Why\nFF audit consistency.\n\n## Bounds\ncalc.py\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(repo, "add", ff_task_rel)
            _git(repo, "commit", "-m", "docs(tasks): open arb-ff-ovr")
            wt_ff = Path(_run_cli(["worktree", "create", "arb-ff-ovr", *project])["path"])
            (wt_ff / "calc.py").write_text("ARB_FF = 2\n", encoding="utf-8")
            _git(wt_ff, "commit", "-am", "feat: arb ff-only with redundant human-override")
            ff_head = _git(wt_ff, "rev-parse", "HEAD")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "arb-ff-ovr", *project])
            _run_cli(["branch", "dispute", "arb-ff-ovr", *project, "--reason", "Clean ff-only"])
            with mock.patch.object(AgyExecutor, "execute", fake_arb_approve):
                arb_ff = _run_cli(["branch", "arbitrate", "arb-ff-ovr", *project])

            land_ff = _run_cli([
                "branch", "land", "arb-ff-ovr", "--ff-only",
                "--human-override", "Redundant CLI override text", "--execute", *project,
            ])
            self.assertTrue(land_ff["landed"])
            self.assertEqual(land_ff["approval_source"], f"arbiter-approve:{ff_head[:12]}")
            ff_msg = _git(repo, "log", "-1", "--format=%B")
            self.assertIn(f"Keel-Arbitrated: approve ({arb_ff['run_id']})", ff_msg)
            self.assertNotIn("Keel-Human-Override:", ff_msg)
            ff_arch_txt = (repo / "docs/tasks/archive/20261117-arb-ff-ovr.md").read_text(encoding="utf-8")
            self.assertIn("## Arbitration Record", ff_arch_txt)
            self.assertIn(arb_ff["run_id"], ff_arch_txt)

            # 3) Taskless `direct-approve` `--ff-only --execute` with redundant `--human-override` creates NO bogus override commit!
            wt_dir = Path(_run_cli(["worktree", "create", "dir-ff-ovr", *project])["path"])
            (wt_dir / "calc.py").write_text("DIR_FF = 3\n", encoding="utf-8")
            _git(wt_dir, "commit", "-am", "fix: direct-approve taskless ff")
            dir_head = _git(wt_dir, "rev-parse", "HEAD")
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                _run_cli(["branch", "verify", "dir-ff-ovr", "--why", "Direct fix.", *project])
            land_dir = _run_cli([
                "branch", "land", "dir-ff-ovr", "--ff-only",
                "--human-override", "Redundant CLI override text", "--execute", *project,
            ])
            self.assertEqual(land_dir["approval_source"], f"direct-approve:{dir_head[:12]}")
            self.assertEqual(land_dir["landed_sha"], dir_head)
            self.assertNotIn("Keel-Human-Override:", _git(repo, "log", "-1", "--format=%B"))

    def test_round5_cli_guidance_uses_unambiguous_branch_target_when_task_slug_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="parser")
            project = ["--project", str(repo)]

            def fake_arb_pivot(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "pivot",
                            "summary": "Pivot cleanly.",
                            "directives": ["Rewrite cleanly"],
                            "avoid_dead_ends": ["Dead end"],
                            "waived_findings": [],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            wt_task = Path(_run_cli(["worktree", "create", "parser", *project])["path"])
            (wt_task / "calc.py").write_text("TASK_P = 1\n", encoding="utf-8")
            _git(wt_task, "commit", "-am", "feat: task/parser")
            task_head = _git(wt_task, "rev-parse", "HEAD")

            wt_fix = Path(_run_cli(["worktree", "create", "fix-p", "--branch", "fix/parser", *project])["path"])
            (wt_fix / "calc.py").write_text("FIX_P = 2\n", encoding="utf-8")
            _git(wt_fix, "commit", "-am", "fix: fix/parser")

            # Check unverified `arbitrate` & unarbitrated `supersede` CLI guidance on `fix/parser`
            with self.assertRaisesRegex(KeelError, "keel branch verify fix/parser"):
                _run_cli(["branch", "arbitrate", "fix/parser", "--why", "Hotfix.", *project])
            with self.assertRaisesRegex(
                KeelError, "keel branch arbitrate fix/parser.*keel branch land fix/parser --discard"
            ):
                _run_cli(["branch", "supersede", "fix/parser", *project])

            # Drive BOTH `task/parser` and `fix/parser` to active Arbiter `pivot`
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "parser", *project])
                _run_cli(["branch", "verify", "fix/parser", "--level", "L4", "--why", "Hotfix.", *project])
                _run_cli(["branch", "verify", "fix/parser", "--why", "Hotfix.", *project])
                # At cap on `fix/parser`, verify error prints `keel branch arbitrate fix/parser`
                with self.assertRaisesRegex(KeelError, "keel branch arbitrate fix/parser"):
                    _run_cli(["branch", "verify", "fix/parser", "--why", "Hotfix.", *project])

            st_fix_cap = _run_cli(["branch", "status", "fix/parser", *project])
            self.assertTrue(any("keel branch arbitrate fix/parser" in b for b in st_fix_cap["blockers"]))

            _run_cli(["branch", "dispute", "parser", *project, "--reason", "Pivot task/parser"])
            with mock.patch.object(AgyExecutor, "execute", fake_arb_pivot):
                _run_cli(["branch", "arbitrate", "parser", *project])
                _run_cli(["branch", "arbitrate", "fix/parser", "--why", "Hotfix.", *project])

            # `branch status fix/parser` (and `dispute` / `verify`) prints `keel branch supersede fix/parser`!
            st_fix_piv = _run_cli(["branch", "status", "fix/parser", *project])
            self.assertTrue(any("keel branch supersede fix/parser" in b for b in st_fix_piv["blockers"]))
            self.assertFalse(any("keel branch supersede parser`" in b for b in st_fix_piv["blockers"]))
            with self.assertRaisesRegex(KeelError, "keel branch supersede fix/parser"):
                _run_cli(["branch", "dispute", "fix/parser", *project, "--reason", "Already pivoted"])
            with self.assertRaisesRegex(KeelError, "keel branch supersede fix/parser"):
                _run_cli(["branch", "verify", "fix/parser", "--why", "Hotfix.", *project])

            # `task/parser` still prints concise `keel branch supersede parser`
            st_task_piv = _run_cli(["branch", "status", "parser", *project])
            self.assertTrue(any("keel branch supersede parser`" in b for b in st_task_piv["blockers"]))

            # Running the exact suggested command retires `fix/parser` while leaving `task/parser` completely active!
            sup_fix = _run_cli(["branch", "supersede", "fix/parser", *project])
            self.assertEqual((sup_fix["branch"], sup_fix["superseded"]), ("fix/parser", True))
            self.assertFalse(wt_fix.exists())
            self.assertTrue(wt_task.is_dir())
            self.assertEqual(_git(repo, "rev-parse", "refs/heads/task/parser"), task_head)

    def test_round5_capped_conflict_resolved_rebase_verifies_once_and_arbitrates_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="conf-reb")
            project = ["--project", str(repo)]

            def fake_arb_approve(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "approve",
                            "summary": "Approved conflict-resolved rebase cleanly.",
                            "directives": [],
                            "avoid_dead_ends": [],
                            "waived_findings": ["Minor nit"],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            # Scenario A: 2 L4 rejections -> `main` modifies SAME line of `calc.py` -> conflict resolved during `git rebase main` -> `verify --arbitrate`
            wt1 = Path(_run_cli(["worktree", "create", "conf-reb", *project])["path"])
            (wt1 / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b + 0  # branch version\n", encoding="utf-8")
            _git(wt1, "commit", "-am", "feat: branch calc edit")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "conf-reb", "--level", "L4", *project])
                _run_cli(["branch", "verify", "conf-reb", *project])

            # Advance `main` on the exact same line of `calc.py` so `git rebase main` produces a real merge conflict!
            (repo / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return int(a) + int(b)  # main hotfix\n", encoding="utf-8")
            _git(repo, "commit", "-am", "fix(main): conflicting calc.py hotfix")

            rb1 = subprocess.run(
                ["git", "rebase", "main"], cwd=wt1, env={**os.environ, "GIT_EDITOR": "true"}, capture_output=True, text=True
            )
            self.assertNotEqual(rb1.returncode, 0, "Expected textual rebase conflict on calc.py")
            (wt1 / "calc.py").write_text(
                "def add(a: int, b: int) -> int:\n    return int(a) + int(b)  # resolved conflict\n\n\ndef mul(a: int, b: int) -> int:\n    return a * b\n",
                encoding="utf-8",
            )
            _git(wt1, "add", "calc.py")
            subprocess.run(
                ["git", "rebase", "--continue"],
                cwd=wt1,
                env={**os.environ, "GIT_EDITOR": "true"},
                check=True,
                capture_output=True,
                text=True,
            )
            resolved_sha_1 = _git(wt1, "rev-parse", "HEAD")

            with (
                mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                mock.patch.object(AgyExecutor, "execute", fake_arb_approve),
            ):
                inline_res = _run_cli(["branch", "verify", "conf-reb", "--arbitrate", *project])
            self.assertEqual((inline_res["outcome"], inline_res["level"], inline_res["head_sha"]), ("approve", "L4", resolved_sha_1))
            self.assertEqual(inline_res["arbitration"]["action"], "approve")
            self.assertTrue(_run_cli(["branch", "status", "conf-reb", *project])["ready_to_land"])

            # Scenario B: Verified without `--arbitrate` and rejected -> 2nd commit on same `merge_base` stays blocked by cap while `branch arbitrate` succeeds!
            task2_rel = "docs/tasks/active/20261117-conf-reb-2.md"
            (repo / task2_rel).write_text(
                "# Conf Reb 2\n\n## Goal & Why\nTwo-step conflict rebase.\n\n## Bounds\ncalc.py\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(repo, "add", task2_rel)
            _git(repo, "commit", "-m", "docs(tasks): open conf-reb-2")
            wt2 = Path(_run_cli(["worktree", "create", "conf-reb-2", *project])["path"])
            (wt2 / "calc.py").write_text("def sub(a: int, b: int) -> int:\n    return a - b  # branch sub\n", encoding="utf-8")
            _git(wt2, "commit", "-am", "feat: sub on branch")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "conf-reb-2", "--level", "L4", *project])
                _run_cli(["branch", "verify", "conf-reb-2", *project])

            (repo / "calc.py").write_text("def sub(a: int, b: int) -> int:\n    return int(a) - int(b)  # main sub\n", encoding="utf-8")
            _git(repo, "commit", "-am", "fix(main): conflicting sub on main")
            rb2 = subprocess.run(
                ["git", "rebase", "main"], cwd=wt2, env={**os.environ, "GIT_EDITOR": "true"}, capture_output=True, text=True
            )
            self.assertNotEqual(rb2.returncode, 0)
            (wt2 / "calc.py").write_text(
                "def sub(a: int, b: int) -> int:\n    return int(a) - int(b)  # resolved sub\n", encoding="utf-8"
            )
            _git(wt2, "add", "calc.py")
            subprocess.run(
                ["git", "rebase", "--continue"],
                cwd=wt2,
                env={**os.environ, "GIT_EDITOR": "true"},
                check=True,
                capture_output=True,
                text=True,
            )
            resolved_sha_2 = _git(wt2, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                v_once = _run_cli(["branch", "verify", "conf-reb-2", *project])
                self.assertEqual((v_once["outcome"], v_once["level"]), ("request_changes", "L4"))
                # A 2nd commit on the same `merge_base` is blocked by the convergence cap!
                (wt2 / "calc.py").write_text("EXTRA = 2\n", encoding="utf-8")
                _git(wt2, "commit", "-am", "feat: extra commit on same merge-base")
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "conf-reb-2", *project])
                _git(wt2, "reset", "--hard", resolved_sha_2)

            with mock.patch.object(AgyExecutor, "execute", fake_arb_approve):
                arb2 = _run_cli(["branch", "arbitrate", "conf-reb-2", *project])
            self.assertEqual(arb2["action"], "approve")

    def test_round5_dual_round2_strips_review_history_sections_from_context_file_while_keeping_shared_criteria(self) -> None:
        from keel_lib.context import strip_review_history_sections

        # Direct unit check including fenced example preservation and all review-history heading tokens
        raw_ctx = (
            "### Shared Caller Criteria\n"
            "- Enforce zero-copy buffer invariant.\n\n"
            "```markdown\n"
            "### Prior Review Carry-Forward Ledger\n"
            "Fenced example heading stays intact.\n"
            "```\n\n"
            "### Prior Review Carry-Forward Ledger\n"
            "-Prior blocker 1\n\n"
            "#### Nested Detail\n"
            "Detail under prior review.\n\n"
            "### Technical Rebuttal\n"
            "Author pushback.\n\n"
            "### Post-Section Shared Note\n"
            "Check memory bounds.\n"
        )
        stripped_ctx = strip_review_history_sections(raw_ctx)
        self.assertIn("### Shared Caller Criteria", stripped_ctx)
        self.assertIn("Enforce zero-copy buffer invariant.", stripped_ctx)
        self.assertIn("Fenced example heading stays intact.", stripped_ctx)
        self.assertIn("### Post-Section Shared Note\nCheck memory bounds.", stripped_ctx)
        self.assertNotIn("Prior blocker 1", stripped_ctx)
        self.assertNotIn("Detail under prior review.", stripped_ctx)
        self.assertNotIn("Author pushback.", stripped_ctx)

        # History-only context strips down to empty string
        self.assertEqual(
            strip_review_history_sections("### Disputed Findings\nItem A.\n\n### Author Rebuttal\nItem B.\n"),
            "",
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _, task_rel = _init_repo_with_task(repo, slug="ctx-blind")
            project = ["--project", str(repo)]
            wt = Path(_run_cli(["worktree", "create", "ctx-blind", *project])["path"])

            def agy_writer(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                (req.workdir / "calc.py").write_text("CTX = 1\n", encoding="utf-8")
                _git(req.workdir, "commit", "-am", "feat: ctx-blind")
                return {"status": "completed", "executor": "agy", "level": req.level, "model": "test-agy"}

            with mock.patch.object(AgyExecutor, "execute", agy_writer):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt), "--executor", "agy",
                    "--mode", "write", "--preset", "implement", "--level", "L3",
                    "--task-file", str(repo / task_rel),
                ])

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "ctx-blind", *project])

            ctx_path = Path(tmp) / "caller-ctx.md"
            ctx_path.write_text(
                "### Shared Caller Criteria\n"
                "SHARED_CALLER_INVARIANT_TOKEN\n\n"
                "### Prior Review Carry-Forward Ledger\n"
                "CTX_FILE_SECRET_PRIOR_BLOCKER_TOKEN\n",
                encoding="utf-8",
            )

            dual_briefs: dict[str, str] = {}

            def capture_dual(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                dual_briefs[adapter.name] = req.brief_path.read_text(encoding="utf-8")
                return verifier("approve")(adapter, req, defaults)

            with (
                mock.patch.object(CodexExecutor, "execute", capture_dual),
                mock.patch.object(AgyExecutor, "execute", capture_dual),
            ):
                res = _run_cli(["branch", "verify", "ctx-blind", "--dual", "--context-file", str(ctx_path), *project])
            self.assertEqual((res["outcome"], res["level"]), ("approve", "L4"))

            primary_brief = dual_briefs["codex"]
            blind_brief = dual_briefs["agy"]
            blind_task_part = blind_brief.split("## Lens:")[0]

            # Both verifiers receive the shared caller criteria!
            self.assertIn("SHARED_CALLER_INVARIANT_TOKEN", primary_brief)
            self.assertIn("SHARED_CALLER_INVARIANT_TOKEN", blind_brief)

            # ONLY the primary verifier receives the `Prior Review Carry-Forward Ledger` section from `--context-file`!
            self.assertIn("CTX_FILE_SECRET_PRIOR_BLOCKER_TOKEN", primary_brief)
            self.assertNotIn("CTX_FILE_SECRET_PRIOR_BLOCKER_TOKEN", blind_brief)
            self.assertNotIn("Prior Review Carry-Forward Ledger", blind_task_part)

    def test_round6_stacked_child_fixed_head_base_advance_refresh_and_arbitrate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="unused-base")
            project = ["--project", str(repo)]

            def fake_arb_approve(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "approve",
                            "summary": "Approved stacked child on advanced merge-base.",
                            "directives": [],
                            "avoid_dead_ends": [],
                            "waived_findings": ["Pedantic nit"],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            # Scenario A: inline `branch verify child-a --arbitrate` after parent fast-forwards onto `main`
            wt_p1 = Path(_run_cli(["worktree", "create", "parent-a", *project])["path"])
            (wt_p1 / "p1.py").write_text("P1 = 1\n", encoding="utf-8")
            _git(wt_p1, "add", "p1.py")
            _git(wt_p1, "commit", "-m", "feat: parent-a")
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                _run_cli(["branch", "verify", "parent-a", "--why", "Parent slice A.", *project])

            wt_c1 = Path(_run_cli(["worktree", "create", "child-a", "--base", "task/parent-a", *project])["path"])
            (wt_c1 / "c1.py").write_text("C1 = 2\n", encoding="utf-8")
            _git(wt_c1, "add", "c1.py")
            _git(wt_c1, "commit", "-m", "feat: child-a")
            child_a_sha = _git(wt_c1, "rev-parse", "HEAD")

            # Child-a verified twice at L4 against `main` (`merge_base == M0`), hitting the convergence cap!
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "child-a", "--level", "L4", "--why", "Child slice A.", *project])
                _run_cli(["branch", "verify", "child-a", "--why", "Child slice A.", *project])
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "child-a", "--why", "Child slice A.", *project])

            # Parent-a lands onto `main` via `--ff-only --execute`: `main` advances to `parent-a`, while `child-a` `head_sha` stays identical!
            land_p1 = _run_cli(["branch", "land", "parent-a", "--ff-only", "--execute", *project])
            self.assertTrue(land_p1["landed"])
            self.assertEqual(_git(wt_c1, "rev-parse", "HEAD"), child_a_sha)

            # Before refreshing verify on new `merge_base`, `arbitrate` rejects stale `merge_base_sha`
            with self.assertRaisesRegex(KeelError, "does not match target base"):
                _run_cli(["branch", "arbitrate", "child-a", "--why", "Child slice A.", *project])

            # `verify --arbitrate` refreshes once at L4 on `(child_a_sha, new_merge_base)` and arbitrates cleanly!
            with (
                mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                mock.patch.object(AgyExecutor, "execute", fake_arb_approve),
            ):
                inline_c1 = _run_cli(["branch", "verify", "child-a", "--arbitrate", "--why", "Child slice A.", *project])
            self.assertEqual((inline_c1["outcome"], inline_c1["level"], inline_c1["head_sha"]), ("approve", "L4", child_a_sha))
            self.assertTrue(_run_cli(["branch", "status", "child-a", *project])["ready_to_land"])

            # Scenario B: two-step `branch verify child-b` -> second `verify` blocked by cap -> `branch arbitrate child-b` succeeds!
            wt_p2 = Path(_run_cli(["worktree", "create", "parent-b", *project])["path"])
            (wt_p2 / "p2.py").write_text("P2 = 1\n", encoding="utf-8")
            _git(wt_p2, "add", "p2.py")
            _git(wt_p2, "commit", "-m", "feat: parent-b")
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                _run_cli(["branch", "verify", "parent-b", "--why", "Parent slice B.", *project])

            wt_c2 = Path(_run_cli(["worktree", "create", "child-b", "--base", "task/parent-b", *project])["path"])
            (wt_c2 / "c2.py").write_text("C2 = 2\n", encoding="utf-8")
            _git(wt_c2, "add", "c2.py")
            _git(wt_c2, "commit", "-m", "feat: child-b")
            child_b_sha = _git(wt_c2, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "child-b", "--level", "L4", "--why", "Child slice B.", *project])
                _run_cli(["branch", "verify", "child-b", "--why", "Child slice B.", *project])

            _run_cli(["branch", "land", "parent-b", "--ff-only", "--execute", *project])
            parent_b_sha = _git(repo, "rev-parse", "main")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                v_c2 = _run_cli(["branch", "verify", "child-b", "--why", "Child slice B.", *project])
                self.assertEqual((v_c2["outcome"], v_c2["level"], v_c2["head_sha"]), ("request_changes", "L4", child_b_sha))
                # A second verify on the same `(child_b_sha, parent_b_sha)` is blocked by the cap!
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "child-b", "--why", "Child slice B.", *project])

            with mock.patch.object(AgyExecutor, "execute", fake_arb_approve):
                arb_c2 = _run_cli(["branch", "arbitrate", "child-b", "--why", "Child slice B.", *project])
            self.assertEqual(arb_c2["action"], "approve")
            self.assertEqual(arb_c2["arbitration"]["merge_base_sha"], parent_b_sha)
            self.assertTrue(_run_cli(["branch", "status", "child-b", *project])["ready_to_land"])

    def test_round6_supersede_archive_task_preserves_arbiter_summary_alongside_explicit_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _, task_rel = _init_repo_with_task(repo, slug="sup-reason")
            project = ["--project", str(repo)]
            wt = Path(_run_cli(["worktree", "create", "sup-reason", *project])["path"])
            (wt / "calc.py").write_text("SUP_R = 1\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: sup-reason draft")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "sup-reason", *project])
            _run_cli(["branch", "dispute", "sup-reason", *project, "--reason", "Need prerequisite split"])

            arb_summary_text = "Missing parser tokenizer prerequisite; split before landing."

            def fake_arb_replan(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "replan",
                            "summary": arb_summary_text,
                            "directives": ["Land tokenizer slice first"],
                            "avoid_dead_ends": ["Bundled parser + evaluator"],
                            "waived_findings": [],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            with mock.patch.object(AgyExecutor, "execute", fake_arb_replan):
                _run_cli(["branch", "arbitrate", "sup-reason", *project])

            caller_reason = "Split into two tracked tasks"
            sup_res = _run_cli([
                "branch", "supersede", "sup-reason", "--archive-task", "--reason", caller_reason, *project,
            ])
            self.assertTrue(sup_res["superseded"])
            self.assertEqual(sup_res["summary"], arb_summary_text)
            self.assertEqual(sup_res["reason"], caller_reason)

            archived_path = repo / "docs/tasks/archive" / Path(task_rel).name
            self.assertTrue(archived_path.is_file())
            archived_txt = archived_path.read_text(encoding="utf-8")
            self.assertIn(f"- **Summary:** {arb_summary_text}", archived_txt)
            self.assertIn(f"- **Reason:** {caller_reason}", archived_txt)

    def test_round7_mixed_base_stacked_child_post_cap_refresh_and_arbitrate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="unused-r7-base")
            project = ["--project", str(repo)]

            def fake_arb_approve(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "approve",
                            "summary": "Approved mixed-base stacked child after parent fast-forward.",
                            "directives": [],
                            "avoid_dead_ends": [],
                            "waived_findings": ["Non-blocking nit"],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            # Scenario A: C1 rejected at L4 against `--base task/parent-mix-a`, C2 rejected at L4 against `main`,
            # then `branch land parent-mix-a --ff-only --execute` -> `branch verify child-mix-a --arbitrate`!
            old_main_a = _git(repo, "rev-parse", "main")
            wt_pa = Path(_run_cli(["worktree", "create", "parent-mix-a", *project])["path"])
            (wt_pa / "pa.py").write_text("PA = 1\n", encoding="utf-8")
            _git(wt_pa, "add", "pa.py")
            _git(wt_pa, "commit", "-m", "feat: parent-mix-a")
            parent_a_sha = _git(wt_pa, "rev-parse", "HEAD")
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                _run_cli(["branch", "verify", "parent-mix-a", "--why", "Parent mix A.", *project])

            wt_ca = Path(_run_cli(["worktree", "create", "child-mix-a", "--base", "task/parent-mix-a", *project])["path"])
            (wt_ca / "ca.py").write_text("CA = 1\n", encoding="utf-8")
            _git(wt_ca, "add", "ca.py")
            _git(wt_ca, "commit", "-m", "feat: child-mix-a C1")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "child-mix-a",
                    "--base", "task/parent-mix-a", "--level", "L4", "--why", "Child mix A.", *project,
                ])

            (wt_ca / "ca.py").write_text("CA = 2\n", encoding="utf-8")
            _git(wt_ca, "commit", "-am", "feat: child-mix-a C2")
            c2_a_sha = _git(wt_ca, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "child-mix-a", "--level", "L4", "--why", "Child mix A.", *project])
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "child-mix-a", "--why", "Child mix A.", *project])

            st_pre_land = load_branch_state(repo / ".git", "task/child-mix-a")
            self.assertEqual(st_pre_land["rounds"][0]["merge_base_sha"], parent_a_sha)
            self.assertFalse(st_pre_land["rounds"][0]["rebase_refresh"])
            self.assertEqual(st_pre_land["rounds"][1]["merge_base_sha"], old_main_a)
            self.assertFalse(st_pre_land["rounds"][1]["rebase_refresh"])

            # Fast-forward parent onto `main` (`main` is now `parent_a_sha`)
            _run_cli(["branch", "land", "parent-mix-a", "--ff-only", "--execute", *project])
            self.assertEqual(_git(repo, "rev-parse", "main"), parent_a_sha)

            # Before refreshing `(C2, parent_a_sha)`, `arbitrate` refuses stale `old_main_a` round on `C2`
            with self.assertRaisesRegex(KeelError, "does not match target base"):
                _run_cli(["branch", "arbitrate", "child-mix-a", "--why", "Child mix A.", *project])

            # `verify --arbitrate` runs 1 post-cap L4 refresh on `(C2, parent_a_sha)` and arbitrates without deadlock!
            with (
                mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                mock.patch.object(AgyExecutor, "execute", fake_arb_approve),
            ):
                inline_a = _run_cli(["branch", "verify", "child-mix-a", "--arbitrate", "--why", "Child mix A.", *project])
            self.assertEqual((inline_a["outcome"], inline_a["level"], inline_a["head_sha"]), ("approve", "L4", c2_a_sha))
            self.assertTrue(_run_cli(["branch", "status", "child-mix-a", *project])["ready_to_land"])
            st_post_a = load_branch_state(repo / ".git", "task/child-mix-a")
            self.assertTrue(st_post_a["rounds"][2]["rebase_refresh"])
            self.assertEqual(st_post_a["rounds"][2]["merge_base_sha"], parent_a_sha)

            # Scenario B: two-step `branch verify child-mix-b` -> second refresh blocked -> `branch arbitrate child-mix-b`!
            wt_pb = Path(_run_cli(["worktree", "create", "parent-mix-b", *project])["path"])
            (wt_pb / "pb.py").write_text("PB = 1\n", encoding="utf-8")
            _git(wt_pb, "add", "pb.py")
            _git(wt_pb, "commit", "-m", "feat: parent-mix-b")
            parent_b_sha = _git(wt_pb, "rev-parse", "HEAD")
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                _run_cli(["branch", "verify", "parent-mix-b", "--why", "Parent mix B.", *project])

            wt_cb = Path(_run_cli(["worktree", "create", "child-mix-b", "--base", "task/parent-mix-b", *project])["path"])
            (wt_cb / "cb.py").write_text("CB = 1\n", encoding="utf-8")
            _git(wt_cb, "add", "cb.py")
            _git(wt_cb, "commit", "-m", "feat: child-mix-b C1")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "child-mix-b",
                    "--base", "task/parent-mix-b", "--level", "L4", "--why", "Child mix B.", *project,
                ])

            (wt_cb / "cb.py").write_text("CB = 2\n", encoding="utf-8")
            _git(wt_cb, "commit", "-am", "feat: child-mix-b C2")
            c2_b_sha = _git(wt_cb, "rev-parse", "HEAD")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "child-mix-b", "--level", "L4", "--why", "Child mix B.", *project])

            _run_cli(["branch", "land", "parent-mix-b", "--ff-only", "--execute", *project])
            self.assertEqual(_git(repo, "rev-parse", "main"), parent_b_sha)

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                ref_b = _run_cli(["branch", "verify", "child-mix-b", "--why", "Child mix B.", *project])
                self.assertEqual((ref_b["outcome"], ref_b["level"], ref_b["head_sha"]), ("request_changes", "L4", c2_b_sha))
                # A second refresh attempt on `parent_b_sha` (same head OR new commit C3) is still blocked at the cap!
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "child-mix-b", "--why", "Child mix B.", *project])
                (wt_cb / "cb.py").write_text("CB = 3\n", encoding="utf-8")
                _git(wt_cb, "commit", "-am", "feat: extra C3 on parent_b_sha")
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "child-mix-b", "--why", "Child mix B.", *project])
                _git(wt_cb, "reset", "--hard", c2_b_sha)

            with mock.patch.object(AgyExecutor, "execute", fake_arb_approve):
                arb_b = _run_cli(["branch", "arbitrate", "child-mix-b", "--why", "Child mix B.", *project])
            self.assertEqual(arb_b["action"], "approve")
            self.assertEqual(arb_b["arbitration"]["merge_base_sha"], parent_b_sha)
            self.assertTrue(_run_cli(["branch", "status", "child-mix-b", *project])["ready_to_land"])

            # Scenario C: unchanged head `C1` disputed between `--base task/parent` and `--base main` also refreshes without deadlock!
            wt_pc = Path(_run_cli(["worktree", "create", "parent-mix-c", *project])["path"])
            (wt_pc / "pc.py").write_text("PC = 1\n", encoding="utf-8")
            _git(wt_pc, "add", "pc.py")
            _git(wt_pc, "commit", "-m", "feat: parent-mix-c")
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                _run_cli(["branch", "verify", "parent-mix-c", "--why", "Parent mix C.", *project])

            wt_cc = Path(_run_cli(["worktree", "create", "child-mix-c", "--base", "task/parent-mix-c", *project])["path"])
            (wt_cc / "cc.py").write_text("CC = 1\n", encoding="utf-8")
            _git(wt_cc, "add", "cc.py")
            _git(wt_cc, "commit", "-m", "feat: child-mix-c C1")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "child-mix-c",
                    "--base", "task/parent-mix-c", "--level", "L4", "--why", "Child mix C.", *project,
                ])
            _run_cli(["branch", "dispute", "child-mix-c", *project, "--reason", "Valid C1"])
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "child-mix-c", "--level", "L4", "--why", "Child mix C.", *project])
            _run_cli(["branch", "land", "parent-mix-c", "--ff-only", "--execute", *project])
            with (
                mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                mock.patch.object(AgyExecutor, "execute", fake_arb_approve),
            ):
                inline_c = _run_cli(["branch", "verify", "child-mix-c", "--arbitrate", "--why", "Child mix C.", *project])
            self.assertEqual(inline_c["outcome"], "approve")

    def test_round7_task_slug_lineage_path_contract_auto_inheritance_and_fuse(self) -> None:
        from keel_lib.worktree import branch_state_path, lineage_state_path, load_lineage_for_branch

        # Also check `--reason` help text on `keel branch supersede --help`
        help_proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "keel"), "branch", "supersede", "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn(
            "Optional Orchestrator note recorded alongside the Arbiter summary.",
            " ".join(help_proc.stdout.split()),
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _, task_rel = _init_repo_with_task(repo, slug="alpha")
            project = ["--project", str(repo)]
            lineage_dir = repo / ".git" / "keel-state" / "lineage"

            def make_arb(action: str, directive: str, dead_end: str) -> Any:
                def _arb(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                    req.output_path.write_text(
                        json.dumps(
                            {
                                "action": action,
                                "summary": f"{action}: {directive}",
                                "directives": [directive],
                                "avoid_dead_ends": [dead_end],
                                "waived_findings": [],
                                "commands_run": ["git diff"],
                            }
                        ),
                        encoding="utf-8",
                    )
                    return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}
                return _arb

            # 1) `supersede` on `task/alpha` writes `<git_common_dir>/keel-state/lineage/alpha.json`,
            #    while `fix/alpha` writes `<git_common_dir>/keel-state/lineage/fix%2Falpha.json`!
            wt_alpha1 = Path(_run_cli(["worktree", "create", "alpha", *project])["path"])
            (wt_alpha1 / "calc.py").write_text("ALPHA = 1\n", encoding="utf-8")
            _git(wt_alpha1, "commit", "-am", "feat: alpha attempt 1")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "alpha", *project])
            _run_cli(["branch", "dispute", "alpha", *project, "--reason", "Need rewrite"])
            with mock.patch.object(AgyExecutor, "execute", make_arb("pivot", "ALPHA_DIR_1", "ALPHA_DEAD_1")):
                _run_cli(["branch", "arbitrate", "alpha", *project])
            sup_alpha1 = _run_cli(["branch", "supersede", "alpha", *project])
            alpha_lin_file = lineage_dir / "alpha.json"
            self.assertEqual(Path(sup_alpha1["lineage_path"]), alpha_lin_file)
            self.assertTrue(alpha_lin_file.is_file())
            self.assertFalse((lineage_dir / "task%2Falpha.json").exists())

            wt_fix1 = Path(_run_cli(["worktree", "create", "fix-alpha-1", "--branch", "fix/alpha", *project])["path"])
            self.assertEqual(load_branch_state(repo / ".git", "fix/alpha").get("recovery_count", 0), 0)
            (wt_fix1 / "calc.py").write_text("FIX_ALPHA = 1\n", encoding="utf-8")
            _git(wt_fix1, "commit", "-am", "fix: fix/alpha 1")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "fix/alpha", "--why", "Hotfix alpha.", *project])
            _run_cli(["branch", "dispute", "fix/alpha", *project, "--reason", "Pivot hotfix"])
            with mock.patch.object(AgyExecutor, "execute", make_arb("pivot", "FIX_ALPHA_DIR", "FIX_ALPHA_DEAD")):
                _run_cli(["branch", "arbitrate", "fix/alpha", "--why", "Hotfix alpha.", *project])
            sup_fix1 = _run_cli(["branch", "supersede", "fix/alpha", *project])
            fix_lin_file = lineage_dir / "custom+fix%2Falpha.json"
            self.assertEqual(Path(sup_fix1["lineage_path"]), fix_lin_file)
            self.assertTrue(fix_lin_file.is_file())
            self.assertEqual(json.loads(alpha_lin_file.read_text(encoding="utf-8"))["recovery_count"], 1)

            # 2) With `lineage/alpha.json` recording `recovery_count: 2` (owned by `task/alpha`, even when
            #    `branches/task%2Falpha.json` is absent), ordinary `keel worktree create alpha` (without `--supersedes`)
            #    inherits `recovery_count == 2`, injects directives/dead-ends into `agent run`, and clamps 3rd `pivot` to `park`!
            branch_state_path(repo / ".git", "task/alpha").unlink(missing_ok=True)
            alpha_lin_file.write_text(
                json.dumps(
                    {
                        "slug": "alpha",
                        "branch": "task/alpha",
                        "task_file": task_rel,
                        "recovery_count": 2,
                        "prior_directives": ["ALPHA_DIR_2: clean iterative pass"],
                        "avoid_dead_ends": ["ALPHA_DEAD_1", "ALPHA_DEAD_2: global cache"],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            wt_alpha3_res = _run_cli(["worktree", "create", "alpha", *project])
            self.assertEqual(wt_alpha3_res["recovery_count"], 2)
            wt_alpha3 = Path(wt_alpha3_res["path"])

            captured_briefs: list[str] = []

            def alpha3_writer(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                captured_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                (req.workdir / "calc.py").write_text("ALPHA = 3\n", encoding="utf-8")
                _git(req.workdir, "commit", "-am", "feat: alpha attempt 3")
                return {"status": "completed", "executor": "agy", "level": req.level, "model": "test-agy"}

            with mock.patch.object(AgyExecutor, "execute", alpha3_writer):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_alpha3),
                    "--executor", "agy", "--mode", "write", "--preset", "implement", "--level", "L4",
                    "--task-file", str(repo / task_rel),
                ])
            self.assertIn("### Prior Attempt Dead-Ends & Arbiter Directives", captured_briefs[-1])
            self.assertIn("Structural Recovery Attempt:** 2", captured_briefs[-1])
            self.assertIn("ALPHA_DIR_2: clean iterative pass", captured_briefs[-1])
            self.assertIn("ALPHA_DEAD_2: global cache", captured_briefs[-1])
            self.assertNotIn("FIX_ALPHA_DIR", captured_briefs[-1])

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "alpha", *project])
                _run_cli(["branch", "verify", "alpha", *project])
            with mock.patch.object(CodexExecutor, "execute", make_arb("pivot", "ALPHA_DIR_3", "ALPHA_DEAD_3")):
                arb_alpha3 = _run_cli(["branch", "arbitrate", "alpha", *project])
            self.assertEqual((arb_alpha3["action"], arb_alpha3["clamped_from"]), ("park", "pivot"))
            st_alpha3 = _run_cli(["branch", "status", "alpha", *project])
            self.assertTrue(st_alpha3["parked"])
            self.assertEqual(st_alpha3["recovery_count"], 2)

            # `fix/alpha` remains isolated in `lineage/fix%2Falpha.json` (`recovery_count == 1`)!
            self.assertEqual(load_lineage_for_branch(repo / ".git", "fix/alpha")["recovery_count"], 1)
            wt_fix2_res = _run_cli(["worktree", "create", "fix-alpha-2", "--branch", "fix/alpha", *project])
            self.assertEqual(wt_fix2_res["recovery_count"], 1)
            self.assertEqual(load_branch_state(repo / ".git", "fix/alpha")["prior_directives"], ["FIX_ALPHA_DIR"])

            # 3) External/pre-seeded `lineage/beta.json` omitting `"branch"` defaults to `"task/beta"`,
            #    while `lineage/gamma.json` explicitly naming `"fix/gamma"` is ignored for `task/gamma`,
            #    and legacy `lineage/task%2Flegacy.json` fallback still loads cleanly!
            (lineage_dir / "beta.json").write_text(
                json.dumps({"recovery_count": 2, "prior_directives": ["BETA_OMITTED_BRANCH_DIR"]}) + "\n",
                encoding="utf-8",
            )
            wt_beta = _run_cli(["worktree", "create", "beta", *project])
            self.assertEqual(wt_beta["recovery_count"], 2)
            self.assertEqual(wt_beta["superseded_from"]["branch"], "task/beta")
            wt_beta_sup = _run_cli(["worktree", "create", "beta-child", "--supersedes", "beta", *project])
            self.assertEqual((wt_beta_sup["recovery_count"], wt_beta_sup["superseded_from"]["branch"]), (2, "task/beta"))
            wt_fix_beta = _run_cli(["worktree", "create", "fix-beta", "--branch", "fix/beta", *project])
            self.assertNotIn("recovery_count", wt_fix_beta)

            (lineage_dir / "gamma.json").write_text(
                json.dumps({"branch": "fix/gamma", "slug": "gamma", "recovery_count": 2}) + "\n",
                encoding="utf-8",
            )
            wt_gamma = _run_cli(["worktree", "create", "gamma", *project])
            self.assertNotIn("recovery_count", wt_gamma)

            (lineage_dir / "task%2Flegacy.json").write_text(
                json.dumps({"branch": "task/legacy", "slug": "legacy", "recovery_count": 1, "prior_directives": ["LEGACY_DIR"]})
                + "\n",
                encoding="utf-8",
            )
            self.assertEqual(lineage_state_path(repo / ".git", "task/legacy").name, "legacy.json")
            wt_legacy = _run_cli(["worktree", "create", "legacy", *project])
            self.assertEqual(wt_legacy["recovery_count"], 1)

    def test_round8_reverted_empty_diff_arbitration_flat_custom_lineage_and_domain_carryforward(self) -> None:
        from keel_lib.worktree import lineage_state_path

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="rev-cap")
            project = ["--project", str(repo)]

            def make_arb(action: str, directive: str | None = None, dead_end: str | None = None) -> Any:
                def _arb(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                    req.output_path.write_text(
                        json.dumps(
                            {
                                "action": action,
                                "summary": f"Arbiter selected {action}.",
                                "directives": [directive] if directive else [],
                                "avoid_dead_ends": [dead_end] if dead_end else [],
                                "waived_findings": ["Minor item"] if action == "approve" else [],
                                "commands_run": ["git log -n 2"],
                            }
                        ),
                        encoding="utf-8",
                    )
                    return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}
                return _arb

            # 1(a): Reverted commit (`git revert`, ahead of `main` with empty net diff) hitting L4 cap
            #       via `branch verify --arbitrate` -> `pivot` -> `evaluate_branch_gate` still blocks land -> `supersede` works!
            wt_rev = Path(_run_cli(["worktree", "create", "rev-cap", *project])["path"])
            (wt_rev / "calc.py").write_text("BROKEN = 1\n", encoding="utf-8")
            _git(wt_rev, "commit", "-am", "feat: broken attempt")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "rev-cap", "--level", "L4", *project])

            _git(wt_rev, "revert", "--no-edit", "HEAD")
            reverted_sha = _git(wt_rev, "rev-parse", "HEAD")
            self.assertEqual(_git(repo, "diff", "main", reverted_sha), "")

            with (
                mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                mock.patch.object(AgyExecutor, "execute", make_arb("pivot", "REVERT_PIVOT_DIR", "REVERT_DEAD_END")),
            ):
                v_rev = _run_cli(["branch", "verify", "rev-cap", "--arbitrate", *project])
            self.assertEqual(v_rev["outcome"], "request_changes")
            self.assertEqual(v_rev["arbitration"]["action"], "pivot")

            st_rev = _run_cli(["branch", "status", "rev-cap", *project])
            self.assertFalse(st_rev["ready_to_land"])
            self.assertTrue(any("empty net diff" in b for b in st_rev["blockers"]))
            self.assertTrue(any("keel branch supersede rev-cap" in b for b in st_rev["blockers"]))

            sup_rev = _run_cli(["branch", "supersede", "rev-cap", *project])
            self.assertTrue(sup_rev["superseded"])
            self.assertEqual(sup_rev["recovery_count"], 1)

            # 1(b): Explicit `branch arbitrate` on empty net diff where Arbiter returns `approve`
            #       deterministically clamps `approve` -> `pivot` (and `evaluate_branch_gate` never marks ready_to_land)!
            wt_rev2 = Path(_run_cli(["worktree", "create", "rev-cap", *project])["path"])
            (wt_rev2 / "calc.py").write_text("BROKEN = 2\n", encoding="utf-8")
            _git(wt_rev2, "commit", "-am", "feat: broken attempt 2")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "rev-cap", "--level", "L4", *project])
            _git(wt_rev2, "revert", "--no-edit", "HEAD")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "rev-cap", *project])
            with mock.patch.object(AgyExecutor, "execute", make_arb("approve")):
                arb_clamped = _run_cli(["branch", "arbitrate", "rev-cap", *project])
            self.assertEqual((arb_clamped["action"], arb_clamped["clamped_from"]), ("pivot", "approve"))
            self.assertFalse(_run_cli(["branch", "status", "rev-cap", *project])["ready_to_land"])
            sup_rev2 = _run_cli(["branch", "supersede", "rev-cap", *project])
            self.assertEqual(sup_rev2["recovery_count"], 2)

            # At `recovery_count == 2`, clamping `approve` on empty net diff escalates to `park`!
            wt_rev3 = Path(_run_cli(["worktree", "create", "rev-cap", *project])["path"])
            (wt_rev3 / "calc.py").write_text("BROKEN = 3\n", encoding="utf-8")
            _git(wt_rev3, "commit", "-am", "feat: broken attempt 3")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "rev-cap", "--level", "L4", *project])
            _git(wt_rev3, "revert", "--no-edit", "HEAD")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "rev-cap", *project])
            with mock.patch.object(AgyExecutor, "execute", make_arb("approve")):
                arb_parked = _run_cli(["branch", "arbitrate", "rev-cap", *project])
            self.assertEqual((arb_parked["action"], arb_parked["clamped_from"]), ("park", "approve"))
            self.assertTrue(_run_cli(["branch", "status", "rev-cap", *project])["parked"])

            # 2: Superseding `task/parser` (`lineage/parser.json`) and flat custom branch `parser` (`lineage/custom+parser.json`)
            #    keeps BOTH lineage files disjoint and intact!
            parser_task_rel = "docs/tasks/active/20261117-parser.md"
            (repo / parser_task_rel).write_text(
                "# Parser\n\n## Goal & Why\nBuild parser.\n\n## Bounds\ncalc.py\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(repo, "add", parser_task_rel)
            _git(repo, "commit", "-m", "docs(tasks): open parser")

            wt_tp = Path(_run_cli(["worktree", "create", "parser", *project])["path"])
            (wt_tp / "calc.py").write_text("TP = 1\n", encoding="utf-8")
            _git(wt_tp, "commit", "-am", "feat: task/parser")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "parser", *project])
            _run_cli(["branch", "dispute", "parser", *project, "--reason", "Split task/parser"])
            with mock.patch.object(AgyExecutor, "execute", make_arb("pivot", "TASK_PARSER_DIR", "TASK_PARSER_DEAD")):
                _run_cli(["branch", "arbitrate", "parser", *project])
            sup_tp = _run_cli(["branch", "supersede", "parser", *project])
            task_lin_path = repo / ".git" / "keel-state" / "lineage" / "parser.json"
            self.assertEqual(Path(sup_tp["lineage_path"]), task_lin_path)
            self.assertTrue(task_lin_path.is_file())

            # Now create & supersede flat custom branch `parser` (`refs/heads/parser`)
            wt_fp = Path(_run_cli(["worktree", "create", "flat-parser-wt", "--branch", "parser", *project])["path"])
            self.assertEqual(load_branch_state(repo / ".git", "parser").get("recovery_count", 0), 0)
            (wt_fp / "calc.py").write_text("FP = 1\n", encoding="utf-8")
            _git(wt_fp, "commit", "-am", "fix: flat parser")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "refs/heads/parser", "--why", "Flat parser experiment.", *project])
            _run_cli(["branch", "dispute", "refs/heads/parser", *project, "--reason", "Pivot flat parser"])
            with mock.patch.object(AgyExecutor, "execute", make_arb("pivot", "FLAT_PARSER_DIR", "FLAT_PARSER_DEAD")):
                _run_cli(["branch", "arbitrate", "refs/heads/parser", "--why", "Flat parser experiment.", *project])
            sup_fp = _run_cli(["branch", "supersede", "refs/heads/parser", *project])
            flat_lin_path = repo / ".git" / "keel-state" / "lineage" / "custom+parser.json"
            self.assertEqual(lineage_state_path(repo / ".git", "parser"), flat_lin_path)
            self.assertEqual(Path(sup_fp["lineage_path"]), flat_lin_path)
            self.assertEqual(sup_fp["recovery_count"], 1)
            self.assertTrue(task_lin_path.is_file())
            self.assertTrue(flat_lin_path.is_file())
            self.assertEqual(json.loads(task_lin_path.read_text(encoding="utf-8"))["prior_directives"], ["TASK_PARSER_DIR"])
            self.assertEqual(json.loads(flat_lin_path.read_text(encoding="utf-8"))["prior_directives"], ["FLAT_PARSER_DIR"])

            # `--supersedes task/parser` and `--supersedes parser` inherit `task/parser`!
            c_explicit_task = _run_cli(["worktree", "create", "from-task-p1", "--supersedes", "task/parser", *project])
            self.assertEqual(c_explicit_task["superseded_from"]["branch"], "task/parser")
            self.assertEqual(load_branch_state(repo / ".git", "task/from-task-p1")["prior_directives"], ["TASK_PARSER_DIR"])

            c_bare_slug = _run_cli(["worktree", "create", "from-task-p2", "--supersedes", "parser", *project])
            self.assertEqual(c_bare_slug["superseded_from"]["branch"], "task/parser")
            self.assertEqual(load_branch_state(repo / ".git", "task/from-task-p2")["prior_directives"], ["TASK_PARSER_DIR"])

            # `--supersedes refs/heads/parser` AND automatic recreation of `--branch parser` inherit flat `parser`!
            c_explicit_flat = _run_cli(["worktree", "create", "from-flat-p1", "--supersedes", "refs/heads/parser", *project])
            self.assertEqual(c_explicit_flat["superseded_from"]["branch"], "parser")
            self.assertEqual(load_branch_state(repo / ".git", "task/from-flat-p1")["prior_directives"], ["FLAT_PARSER_DIR"])

            c_auto_flat = _run_cli(["worktree", "create", "flat-parser-wt2", "--branch", "parser", *project])
            self.assertEqual(c_auto_flat["superseded_from"]["branch"], "parser")
            self.assertEqual(load_branch_state(repo / ".git", "parser")["prior_directives"], ["FLAT_PARSER_DIR"])

            # 3: Task-file-free Round 2 `--dual` with `--context-file` headed `# Tax loss carry-forward`
            #    preserves domain criteria in BOTH verifiers while stripping only `### Prior Review Carry-Forward Ledger`!
            wt_tax = Path(_run_cli(["worktree", "create", "tax-loss", *project])["path"])

            def tax_writer(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                (req.workdir / "tax.py").write_text("MAX_OFFSET = 3000\n", encoding="utf-8")
                _git(req.workdir, "add", "tax.py")
                _git(req.workdir, "commit", "-m", "feat: tax loss offset")
                return {"status": "completed", "executor": "agy", "level": req.level, "model": "test-agy"}

            with mock.patch.object(AgyExecutor, "execute", tax_writer):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_tax), "--executor", "agy",
                    "--mode", "write", "--preset", "implement", "--level", "L3",
                    "--why", "Enforce IRS capital loss rules.", "--task", "Implement tax.py.",
                ])

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "tax-loss", "--why", "Enforce IRS capital loss rules.", *project])

            tax_ctx_file = Path(tmp) / "tax-criteria.md"
            tax_ctx_file.write_text(
                "# Tax loss carry-forward\n\n"
                "- Annual ordinary-income offset cap MUST equal exactly `$3,000` (`NUMERIC_CAP_3000_TOKEN`).\n\n"
                "## Carry-forward accounting\n"
                "- Unused net capital loss carries forward indefinitely (`INDEFINITE_CARRYFORWARD_TOKEN`).\n\n"
                "### Prior Review Carry-Forward Ledger\n"
                "- `SECRET_ROUND1_REVIEW_NOTE_TOKEN`: disputed Round 1 nit.\n\n"
                "## Post-Ledger Domain Invariant\n"
                "- Short-term losses offset short-term gains first (`SHORT_TERM_FIRST_TOKEN`).\n",
                encoding="utf-8",
            )

            dual_tax_briefs: dict[str, str] = {}

            def capture_tax_dual(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                dual_tax_briefs[adapter.name] = req.brief_path.read_text(encoding="utf-8")
                return verifier("approve")(adapter, req, defaults)

            with (
                mock.patch.object(CodexExecutor, "execute", capture_tax_dual),
                mock.patch.object(AgyExecutor, "execute", capture_tax_dual),
            ):
                tax_res = _run_cli([
                    "branch", "verify", "tax-loss", "--dual",
                    "--why", "Enforce IRS capital loss rules.",
                    "--context-file", str(tax_ctx_file), *project,
                ])
            self.assertEqual((tax_res["outcome"], tax_res["level"]), ("approve", "L4"))

            for v_name in ("codex", "agy"):
                self.assertIn("# Tax loss carry-forward", dual_tax_briefs[v_name])
                self.assertIn("NUMERIC_CAP_3000_TOKEN", dual_tax_briefs[v_name])
                self.assertIn("## Carry-forward accounting", dual_tax_briefs[v_name])
                self.assertIn("INDEFINITE_CARRYFORWARD_TOKEN", dual_tax_briefs[v_name])
                self.assertIn("SHORT_TERM_FIRST_TOKEN", dual_tax_briefs[v_name])

            self.assertIn("SECRET_ROUND1_REVIEW_NOTE_TOKEN", dual_tax_briefs["codex"])
            self.assertNotIn("SECRET_ROUND1_REVIEW_NOTE_TOKEN", dual_tax_briefs["agy"])
            self.assertNotIn("Prior Review Carry-Forward Ledger", dual_tax_briefs["agy"].split("## Lens:")[0])

    def test_round9_coexisting_flat_and_task_branches_and_restored_head_arbitration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _, task_rel = _init_repo_with_task(repo, slug="parser")
            project = ["--project", str(repo)]

            def fake_arb_approve(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": "approve",
                            "summary": "Approved cleanly by Arbiter.",
                            "directives": [],
                            "avoid_dead_ends": [],
                            "waived_findings": ["False positive nit"],
                            "commands_run": ["git diff"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}

            # 1: Coexisting flat `refs/heads/parser` AND `refs/heads/task/parser` both carrying disputed rejections!
            wt_task = Path(_run_cli(["worktree", "create", "parser", *project])["path"])
            (wt_task / "calc.py").write_text("TASK_PARSER_VAL = 100\n", encoding="utf-8")
            _git(wt_task, "commit", "-am", "feat: task/parser commit")
            task_head = _git(wt_task, "rev-parse", "HEAD")

            wt_flat = Path(_run_cli(["worktree", "create", "flat-parser", "--branch", "parser", *project])["path"])
            (wt_flat / "flat_only.py").write_text("FLAT_PARSER_VAL = 200\n", encoding="utf-8")
            _git(wt_flat, "add", "flat_only.py")
            _git(wt_flat, "commit", "-m", "feat: flat refs/heads/parser commit")
            flat_head = _git(wt_flat, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "parser", *project])
                _run_cli(["branch", "verify", "refs/heads/parser", "--why", "Flat parser slice.", *project])
            _run_cli(["branch", "dispute", "parser", *project, "--reason", "Task dispute"])
            _run_cli(["branch", "dispute", "refs/heads/parser", *project, "--reason", "Flat dispute"])

            # `branch verify refs/heads/parser --arbitrate` MUST arbitrate and approve `refs/heads/parser`, NOT `task/parser`!
            with (
                mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                mock.patch.object(AgyExecutor, "execute", fake_arb_approve),
            ):
                v_flat = _run_cli([
                    "branch", "verify", "refs/heads/parser", "--arbitrate",
                    "--why", "Flat parser slice.", *project,
                ])
            self.assertEqual((v_flat["branch"], v_flat["head_sha"], v_flat["outcome"]), ("parser", flat_head, "approve"))
            self.assertEqual(v_flat["arbitration"]["head_sha"], flat_head)

            # `branch status refs/heads/parser` AND repo-wide `branch status` report `parser` ready_to_land while `task/parser` stays unapproved!
            st_flat = _run_cli(["branch", "status", "refs/heads/parser", *project])
            st_task = _run_cli(["branch", "status", "parser", *project])
            self.assertEqual((st_flat["branch"], st_flat["ready_to_land"]), ("parser", True))
            self.assertEqual(st_flat["approval_source"], f"arbiter-approve:{flat_head[:12]}")
            self.assertEqual((st_task["branch"], st_task["ready_to_land"]), ("task/parser", False))
            self.assertIsNone(st_task["arbitration"])

            overview = _run_cli(["branch", "status", *project])
            by_branch = {w["branch"]: w["gate"] for w in overview["worktrees"]}
            self.assertTrue(by_branch["parser"]["ready_to_land"])
            self.assertFalse(by_branch["task/parser"]["ready_to_land"])

            # `branch land refs/heads/parser` (preview & `--execute`) lands ONLY flat `parser`, leaving `task/parser` and its active spec intact!
            land_preview = _run_cli(["branch", "land", "refs/heads/parser", *project])
            self.assertEqual((land_preview["branch"], land_preview["ready_to_land"], land_preview["landed"]), ("parser", True, False))
            land_exec = _run_cli(["branch", "land", "refs/heads/parser", "--execute", *project])
            self.assertEqual((land_exec["branch"], land_exec["landed"]), ("parser", True))
            self.assertTrue((repo / "flat_only.py").is_file())
            self.assertTrue((repo / task_rel).is_file())
            self.assertTrue(wt_task.is_dir())
            self.assertEqual(_git(repo, "rev-parse", "refs/heads/task/parser"), task_head)

            # 2: Two L4 rejections (`C1` with passing `--check`, then `C2` hitting cap) -> restore `C1` (`git reset --keep C1`)
            #    -> `branch arbitrate` approves `C1`, `branch status` marks `ready_to_land: True`, and `branch land --execute` lands `C1`!
            rest_task_rel = "docs/tasks/active/20261117-restore-c1.md"
            (repo / rest_task_rel).write_text(
                "# Restore C1\n\n## Goal & Why\nLand clean C1.\n\n## Bounds\nrest.py\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(repo, "add", rest_task_rel)
            _git(repo, "commit", "-m", "docs(tasks): open restore-c1")

            wt_rest = Path(_run_cli(["worktree", "create", "restore-c1", *project])["path"])
            (wt_rest / "rest.py").write_text("VALUE = 1\n", encoding="utf-8")
            _git(wt_rest, "add", "rest.py")
            _git(wt_rest, "commit", "-m", "feat: C1 clean implementation")
            c1_sha = _git(wt_rest, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "restore-c1", "--level", "L4",
                    "--check", "python3 -c 'import rest; assert rest.VALUE == 1'", *project,
                ])

            (wt_rest / "rest.py").write_text("VALUE = 2  # regressed C2\n", encoding="utf-8")
            _git(wt_rest, "commit", "-am", "wip: C2 regression")
            c2_sha = _git(wt_rest, "rev-parse", "HEAD")
            self.assertNotEqual(c1_sha, c2_sha)

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "restore-c1", "--level", "L4", *project])

            # Restore branch back to verified `C1`!
            _git(wt_rest, "reset", "--keep", c1_sha)
            self.assertEqual(_git(repo, "rev-parse", "refs/heads/task/restore-c1"), c1_sha)

            with mock.patch.object(AgyExecutor, "execute", fake_arb_approve):
                arb_c1 = _run_cli(["branch", "arbitrate", "restore-c1", *project])
            self.assertEqual((arb_c1["action"], arb_c1["head_sha"]), ("approve", c1_sha))

            st_c1 = _run_cli(["branch", "status", "restore-c1", *project])
            self.assertTrue(st_c1["ready_to_land"])
            self.assertEqual(st_c1["approval_source"], f"arbiter-approve:{c1_sha[:12]}")
            self.assertTrue(st_c1["checks"]["ok"])

            land_c1 = _run_cli(["branch", "land", "restore-c1", "--execute", *project])
            self.assertTrue(land_c1["landed"])
            self.assertEqual((repo / "rest.py").read_text(encoding="utf-8"), "VALUE = 1\n")
            self.assertIn(f"Keel-Arbitrated: approve ({arb_c1['run_id']})", _git(repo, "log", "-1", "--format=%B"))

    def test_round10_equal_head_arbitration_and_cycle_scoped_advisory_rebuttals(self) -> None:
        from keel_lib.worktree import lineage_state_path

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _, inline_task_rel = _init_repo_with_task(repo, slug="prereq-inline")
            project = ["--project", str(repo)]

            arb_captured_briefs: list[str] = []

            def make_arb(action: str, directive: str | None = None, dead_end: str | None = None) -> Any:
                def _arb(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                    arb_captured_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                    req.output_path.write_text(
                        json.dumps(
                            {
                                "action": action,
                                "summary": f"Arbiter selected {action}.",
                                "directives": [directive] if directive else [],
                                "avoid_dead_ends": [dead_end] if dead_end else [],
                                "waived_findings": ["Minor item"] if action == "approve" else [],
                                "commands_run": ["git status"],
                            }
                        ),
                        encoding="utf-8",
                    )
                    return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}
                return _arb

            # 1(a): Distinct task branch at `head_sha == base_tip_sha` (no commits ahead) with L4 rejection +
            #       `--check true` + `branch dispute` -> `branch verify --arbitrate` records `replan`,
            #       `branch status` directs to `supersede` while keeping `ready_to_land: False`,
            #       and `branch supersede` retires the branch and records lineage cleanly!
            wt_inline = Path(_run_cli(["worktree", "create", "prereq-inline", *project])["path"])
            main_sha = _git(repo, "rev-parse", "main")
            self.assertEqual(_git(wt_inline, "rev-parse", "HEAD"), main_sha)

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                v1 = _run_cli(["branch", "verify", "prereq-inline", "--level", "L4", "--check", "true", *project])
            self.assertEqual((v1["outcome"], v1["head_sha"]), ("request_changes", main_sha))

            _run_cli([
                "branch", "dispute", "prereq-inline", *project,
                "--reason", "Upstream prerequisite outside Bounds blocks the first implementation commit.",
            ])

            with (
                mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                mock.patch.object(AgyExecutor, "execute", make_arb("replan", "PREREQ_INLINE_DIR", "PREREQ_INLINE_DEAD")),
            ):
                v2 = _run_cli(["branch", "verify", "prereq-inline", "--arbitrate", *project])
            self.assertEqual((v2["outcome"], v2["head_sha"]), ("request_changes", main_sha))
            self.assertEqual(v2["arbitration"]["action"], "replan")
            self.assertIn("no commits ahead", arb_captured_briefs[-1])

            st_inline = _run_cli(["branch", "status", "prereq-inline", *project])
            self.assertFalse(st_inline["ready_to_land"])
            self.assertTrue(any("has no commits ahead" in b for b in st_inline["blockers"]))
            self.assertTrue(any("keel branch supersede prereq-inline" in b for b in st_inline["blockers"]))

            with self.assertRaisesRegex(KeelError, "has no commits ahead"):
                _run_cli(["branch", "land", "prereq-inline", "--execute", *project])

            sup_inline = _run_cli(["branch", "supersede", "prereq-inline", *project])
            self.assertTrue(sup_inline["superseded"])
            self.assertEqual((sup_inline["action"], sup_inline["recovery_count"]), ("replan", 1))
            self.assertEqual(_git(repo, "rev-parse", sup_inline["preserved_ref"]), main_sha)
            lin_inline = lineage_state_path(repo / ".git", "task/prereq-inline")
            self.assertTrue(lin_inline.is_file())
            self.assertEqual(json.loads(lin_inline.read_text(encoding="utf-8"))["prior_directives"], ["PREREQ_INLINE_DIR"])
            self.assertFalse(wt_inline.exists())
            self.assertEqual(
                subprocess.run(["git", "show-ref", "--verify", "--quiet", "refs/heads/task/prereq-inline"], cwd=repo).returncode,
                1,
            )

            # 1(b): Standalone `branch arbitrate` on `head_sha == base_tip_sha` + `--archive-task` supersede,
            #       plus `approve` clamping on empty-head branch and default/self/behind guards!
            direct_task_rel = "docs/tasks/active/20261117-prereq-direct.md"
            (repo / direct_task_rel).write_text(
                "# Prereq Direct\n\n## Goal & Why\nSplit prerequisite.\n\n## Bounds\ncalc.py only.\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(repo, "add", direct_task_rel)
            _git(repo, "commit", "-m", "docs(tasks): open prereq-direct")
            direct_main_sha = _git(repo, "rev-parse", "main")

            wt_direct = Path(_run_cli(["worktree", "create", "prereq-direct", *project])["path"])
            self.assertEqual(_git(wt_direct, "rev-parse", "HEAD"), direct_main_sha)
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "prereq-direct", "--level", "L4", "--check", "true", *project])
            _run_cli(["branch", "dispute", "prereq-direct", *project, "--reason", "Missing prerequisite API"])

            with mock.patch.object(AgyExecutor, "execute", make_arb("replan", "PREREQ_DIRECT_DIR", "PREREQ_DIRECT_DEAD")):
                arb_direct = _run_cli(["branch", "arbitrate", "prereq-direct", *project])
            self.assertEqual((arb_direct["action"], arb_direct["head_sha"]), ("replan", direct_main_sha))

            st_direct = _run_cli(["branch", "status", "prereq-direct", *project])
            self.assertFalse(st_direct["ready_to_land"])
            self.assertTrue(any("keel branch supersede prereq-direct" in b for b in st_direct["blockers"]))

            sup_direct = _run_cli(["branch", "supersede", "prereq-direct", "--archive-task", *project])
            self.assertTrue(sup_direct["superseded"])
            self.assertEqual(sup_direct["archived_task"], "docs/tasks/archive/20261117-prereq-direct.md")
            self.assertIn(
                "[SUPERSEDED BY ARBITER: REPLAN]",
                (repo / sup_direct["archived_task"]).read_text(encoding="utf-8"),
            )

            # Empty-head `approve` clamps to `pivot`; default/self-base and behind-base remain blocked!
            wt_clamp = Path(_run_cli(["worktree", "create", "prereq-clamp", *project])["path"])
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "prereq-clamp", "--level", "L4", "--check", "true", "--why", "Clamp check.", *project])
            _run_cli(["branch", "dispute", "prereq-clamp", *project, "--reason", "Blocked before first commit"])
            with mock.patch.object(AgyExecutor, "execute", make_arb("approve")):
                arb_clamp = _run_cli(["branch", "arbitrate", "prereq-clamp", "--why", "Clamp check.", *project])
            self.assertEqual((arb_clamp["action"], arb_clamp["clamped_from"]), ("pivot", "approve"))
            self.assertFalse(_run_cli(["branch", "status", "prereq-clamp", *project])["ready_to_land"])

            with self.assertRaisesRegex(KeelError, "Cannot arbitrate base or default branch"):
                _run_cli(["branch", "arbitrate", "main", *project])
            with self.assertRaisesRegex(KeelError, "Cannot arbitrate base or default branch"):
                _run_cli(["branch", "arbitrate", "prereq-clamp", "--base", "task/prereq-clamp", *project])
            with self.assertRaisesRegex(KeelError, "Cannot supersede base or default branch"):
                _run_cli(["branch", "supersede", "main", *project])

            # Advancing `main` makes `prereq-clamp` strictly behind `main` -> `arbitrate` rejects!
            (repo / "README.md").write_text("main advanced\n", encoding="utf-8")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-m", "docs: advance main past prereq-clamp")
            with self.assertRaisesRegex(KeelError, "is behind 'main' tip"):
                _run_cli(["branch", "arbitrate", "prereq-clamp", "--why", "Clamp check.", *project])
            _run_cli(["worktree", "cleanup", "prereq-clamp", "--discard", *project])

            # 2: Cycle 1 rejection -> commit `### Review Rebuttals` + architectural note under `## Decisions & Findings`
            #    -> Cycle 1 `approve` -> Cycle 2 code-only commit -> `branch verify --dual` AND single `branch verify`:
            #    NEITHER Cycle 2 verifier brief contains closed Cycle 1 `### Review Rebuttals`, while BOTH retain
            #    the non-rebuttal `## Decisions & Findings` note and full-diff access!
            adv_task_rel = "docs/tasks/active/20261117-cycle-adv.md"
            (repo / adv_task_rel).write_text(
                "# Cycle Advisory\n\n## Goal & Why\nScope rebuttals per cycle.\n\n"
                "## Bounds\ncalc.py only.\n\n## Done-check\npython3 -c 'import calc'\n",
                encoding="utf-8",
            )
            _git(repo, "add", adv_task_rel)
            _git(repo, "commit", "-m", "docs(tasks): open cycle-adv")

            wt_adv = Path(_run_cli(["worktree", "create", "cycle-adv", *project])["path"])

            def agy_v1(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                (req.workdir / "calc.py").write_text("CYCLE_ADV = 1\n", encoding="utf-8")
                _git(req.workdir, "commit", "-am", "feat: cycle 1 v1")
                return {"status": "completed", "executor": "agy", "level": req.level, "model": "test-agy"}

            with mock.patch.object(AgyExecutor, "execute", agy_v1):
                _run_cli([
                    "agent", "run", *project, "--cwd", str(wt_adv), "--executor", "agy",
                    "--mode", "write", "--preset", "implement", "--level", "L3",
                    "--task-file", str(repo / adv_task_rel),
                ])

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "cycle-adv", *project])

            # Commit Progress + non-rebuttal Decisions & Findings + Cycle 1 `### Review Rebuttals` -> Cycle 1 `approve`
            c1_spec_text = (
                (wt_adv / adv_task_rel).read_text(encoding="utf-8").rstrip()
                + "\n\n## Progress\n- [x] Implemented v1\n\n"
                + "## Decisions & Findings\nARCH_NOTE_KEEP_ACROSS_CYCLES\n\n"
                + "### Review Rebuttals\nCLOSED_CYCLE1_REBUTTAL_TOKEN\n\n"
                + "#### Evidence\nCLOSED_CYCLE1_EVIDENCE_TOKEN\n\n"
                + "### Complexity\nCOMPLEXITY_NOTE_KEEP\n"
            )
            (wt_adv / adv_task_rel).write_text(c1_spec_text, encoding="utf-8")
            _git(wt_adv, "commit", "-am", "docs(tasks): record cycle 1 decisions and rebuttal")

            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                c1_app = _run_cli(["branch", "verify", "cycle-adv", *project])
            self.assertEqual(c1_app["outcome"], "approve")

            # Cycle 2: code-only follow-up commit (leaving Cycle 1 `### Review Rebuttals` untouched in the task file!)
            (wt_adv / "calc.py").write_text("CYCLE_ADV = 2\n", encoding="utf-8")
            _git(wt_adv, "commit", "-am", "feat: cycle 2 code-only follow-up")
            c2_head = _git(wt_adv, "rev-parse", "HEAD")
            c2_mb = _git(repo, "merge-base", "main", c2_head)

            c2_dual_briefs: dict[str, str] = {}

            def capture_c2_dual(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                c2_dual_briefs[adapter.name] = req.brief_path.read_text(encoding="utf-8")
                return verifier("request_changes")(adapter, req, defaults)

            with (
                mock.patch.object(CodexExecutor, "execute", capture_c2_dual),
                mock.patch.object(AgyExecutor, "execute", capture_c2_dual),
            ):
                c2_r1_dual = _run_cli(["branch", "verify", "cycle-adv", "--dual", "--level", "L4", *project])
            self.assertEqual(c2_r1_dual["outcome"], "request_changes")
            self.assertEqual(set(c2_dual_briefs.keys()), {"codex", "agy"})
            for v_name in ("codex", "agy"):
                brief_text = c2_dual_briefs[v_name]
                task_section = brief_text.split("## Lens:")[0]
                self.assertIn("ARCH_NOTE_KEEP_ACROSS_CYCLES", brief_text)
                self.assertIn("COMPLEXITY_NOTE_KEEP", brief_text)
                self.assertIn("- [x] Implemented v1", brief_text)
                self.assertIn("### Branch Progress & Decisions", brief_text)
                self.assertIn(f"git diff {c2_mb}..{c2_head}", brief_text)
                self.assertNotIn("CLOSED_CYCLE1_REBUTTAL_TOKEN", brief_text)
                self.assertNotIn("CLOSED_CYCLE1_EVIDENCE_TOKEN", brief_text)
                self.assertNotIn("### Review Rebuttals", brief_text)
                self.assertNotIn("Review Rebuttals", task_section)

            # Cycle 2 Round 2 single `branch verify` (still untouched Cycle 1 rebuttal): omits closed Cycle 1 rebuttal!
            c2_single_briefs: list[str] = []

            def capture_c2_single_rc(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                c2_single_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                return verifier("request_changes")(adapter, req, defaults)

            with mock.patch.object(CodexExecutor, "execute", capture_c2_single_rc):
                c2_r2_single = _run_cli(["branch", "verify", "cycle-adv", *project])
            self.assertEqual(c2_r2_single["outcome"], "request_changes")
            single_task_section = c2_single_briefs[-1].split("## Lens:")[0]
            self.assertIn("ARCH_NOTE_KEEP_ACROSS_CYCLES", c2_single_briefs[-1])
            self.assertIn("COMPLEXITY_NOTE_KEEP", c2_single_briefs[-1])
            self.assertIn("### Prior Review Carry-Forward Ledger", c2_single_briefs[-1])
            self.assertIn(f"git diff {c2_mb}..{c2_head}", c2_single_briefs[-1])
            self.assertNotIn("CLOSED_CYCLE1_REBUTTAL_TOKEN", c2_single_briefs[-1])
            self.assertNotIn("CLOSED_CYCLE1_EVIDENCE_TOKEN", c2_single_briefs[-1])
            self.assertNotIn("### Review Rebuttals", c2_single_briefs[-1])
            self.assertNotIn("Review Rebuttals", single_task_section)

            # Cycle 2 Arbiter brief at L4 cap also omits closed Cycle 1 rebuttal while keeping non-rebuttal decisions!
            with mock.patch.object(CodexExecutor, "execute", make_arb("rework_once", "SURGICAL_C2_FIX")):
                arb_c2 = _run_cli(["branch", "arbitrate", "cycle-adv", *project])
            self.assertEqual(arb_c2["action"], "rework_once")
            arb_c2_task_section = arb_captured_briefs[-1].split("## Lens:")[0]
            self.assertIn("ARCH_NOTE_KEEP_ACROSS_CYCLES", arb_captured_briefs[-1])
            self.assertNotIn("CLOSED_CYCLE1_REBUTTAL_TOKEN", arb_captured_briefs[-1])
            self.assertNotIn("Review Rebuttals", arb_c2_task_section)

            # Updating `### Review Rebuttals` in Cycle 2 surfaces ONLY to primary `--dual` verifier, NOT blind secondary!
            c2_updated_spec = c1_spec_text.replace(
                "CLOSED_CYCLE1_REBUTTAL_TOKEN",
                "ACTIVE_CYCLE2_REBUTTAL_TOKEN",
            )
            (wt_adv / adv_task_rel).write_text(c2_updated_spec, encoding="utf-8")
            _git(wt_adv, "commit", "-am", "docs(tasks): update rebuttal for cycle 2")

            c2_reb_dual_briefs: dict[str, str] = {}

            def capture_c2_reb_dual(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                c2_reb_dual_briefs[adapter.name] = req.brief_path.read_text(encoding="utf-8")
                return verifier("approve")(adapter, req, defaults)

            with (
                mock.patch.object(CodexExecutor, "execute", capture_c2_reb_dual),
                mock.patch.object(AgyExecutor, "execute", capture_c2_reb_dual),
            ):
                c2_app = _run_cli(["branch", "verify", "cycle-adv", "--dual", *project])
            self.assertEqual(c2_app["outcome"], "approve")
            self.assertIn("ACTIVE_CYCLE2_REBUTTAL_TOKEN", c2_reb_dual_briefs["codex"])
            self.assertIn("### Review Rebuttals", c2_reb_dual_briefs["codex"])
            self.assertIn("ARCH_NOTE_KEEP_ACROSS_CYCLES", c2_reb_dual_briefs["codex"])
            self.assertIn("ARCH_NOTE_KEEP_ACROSS_CYCLES", c2_reb_dual_briefs["agy"])
            self.assertNotIn("ACTIVE_CYCLE2_REBUTTAL_TOKEN", c2_reb_dual_briefs["agy"])
            self.assertNotIn("Review Rebuttals", c2_reb_dual_briefs["agy"].split("## Lens:")[0])

            # Cycle 3 Round 1 single `branch verify` (code-only commit after Cycle 2 `approve`):
            # closed Cycle 2 rebuttal does NOT leak into Cycle 3 single verifier!
            (wt_adv / "calc.py").write_text("CYCLE_ADV = 3\n", encoding="utf-8")
            _git(wt_adv, "commit", "-am", "feat: cycle 3 code-only follow-up")
            c3_head = _git(wt_adv, "rev-parse", "HEAD")
            c3_single_briefs: list[str] = []

            def capture_c3_single(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                c3_single_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                return verifier("approve")(adapter, req, defaults)

            with mock.patch.object(CodexExecutor, "execute", capture_c3_single):
                c3_r1 = _run_cli(["branch", "verify", "cycle-adv", *project])
            self.assertEqual(c3_r1["outcome"], "approve")
            c3_task_section = c3_single_briefs[-1].split("## Lens:")[0]
            self.assertIn("ARCH_NOTE_KEEP_ACROSS_CYCLES", c3_single_briefs[-1])
            self.assertIn("COMPLEXITY_NOTE_KEEP", c3_single_briefs[-1])
            self.assertIn(f"git diff {c2_mb}..{c3_head}", c3_single_briefs[-1])
            self.assertNotIn("ACTIVE_CYCLE2_REBUTTAL_TOKEN", c3_single_briefs[-1])
            self.assertNotIn("CLOSED_CYCLE1_EVIDENCE_TOKEN", c3_single_briefs[-1])
            self.assertNotIn("Review Rebuttals", c3_task_section)

    def test_round11_equal_head_base_advance_force_round_close_and_qualified_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="eq-rebase")
            project = ["--project", str(repo)]

            def make_arb(action: str, directive: str | None = None, dead_end: str | None = None) -> Any:
                def _arb(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                    req.output_path.write_text(
                        json.dumps(
                            {
                                "action": action,
                                "summary": f"Arbiter selected {action}.",
                                "directives": [directive] if directive else [],
                                "avoid_dead_ends": [dead_end] if dead_end else [],
                                "waived_findings": ["Minor item"] if action == "approve" else [],
                                "commands_run": ["git status"],
                            }
                        ),
                        encoding="utf-8",
                    )
                    return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}
                return _arb

            # 1: Distinct task branch with zero implementation commits (`head == main`) hits 2 L4 rejections
            #    -> unrelated commit lands on `main` -> `git rebase main` (`head == new_main`)
            #    -> `branch verify --arbitrate` refreshes once at L4 and arbitrates cleanly (`replan` -> `supersede`),
            #    while keeping `ready_to_land: False`!
            wt_eq = Path(_run_cli(["worktree", "create", "eq-rebase", *project])["path"])
            old_main = _git(repo, "rev-parse", "main")
            self.assertEqual(_git(wt_eq, "rev-parse", "HEAD"), old_main)

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "eq-rebase", "--level", "L4", "--check", "true", *project])
                _run_cli(["branch", "verify", "eq-rebase", *project])
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "eq-rebase", *project])

            (repo / "README.md").write_text("unrelated main advance\n", encoding="utf-8")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-m", "docs: unrelated main commit")
            new_main = _git(repo, "rev-parse", "main")
            self.assertNotEqual(new_main, old_main)

            _git(wt_eq, "rebase", "main")
            self.assertEqual(_git(wt_eq, "rev-parse", "HEAD"), new_main)

            with (
                mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                mock.patch.object(AgyExecutor, "execute", make_arb("replan", "EQ_REBASE_DIR", "EQ_REBASE_DEAD")),
            ):
                v_eq = _run_cli(["branch", "verify", "eq-rebase", "--arbitrate", "--check", "true", *project])
            self.assertEqual((v_eq["outcome"], v_eq["level"], v_eq["head_sha"]), ("request_changes", "L4", new_main))
            self.assertEqual(v_eq["arbitration"]["action"], "replan")
            self.assertEqual(v_eq["arbitration"]["merge_base_sha"], new_main)

            st_eq = _run_cli(["branch", "status", "eq-rebase", *project])
            self.assertFalse(st_eq["ready_to_land"])
            self.assertTrue(any("has no commits ahead" in b for b in st_eq["blockers"]))
            self.assertTrue(any("keel branch supersede eq-rebase" in b for b in st_eq["blockers"]))

            # 3(a): Superseding with explicit qualified `--base refs/heads/main` succeeds when primary checkout is on `main`!
            sup_eq = _run_cli(["branch", "supersede", "eq-rebase", "--base", "refs/heads/main", *project])
            self.assertTrue(sup_eq["superseded"])
            self.assertEqual((sup_eq["action"], sup_eq["recovery_count"]), ("replan", 1))
            self.assertEqual(_git(repo, "rev-parse", sup_eq["preserved_ref"]), new_main)

            # Also verify two-step equal-head base-advance refresh blocks a second refresh on the same `new_main`!
            wt_eq2 = Path(_run_cli(["worktree", "create", "eq-rebase-2", *project])["path"])
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "eq-rebase-2", "--level", "L4", "--why", "Eq2.", *project])
                _run_cli(["branch", "verify", "eq-rebase-2", "--why", "Eq2.", *project])
            (repo / "README.md").write_text("unrelated main advance 2\n", encoding="utf-8")
            _git(repo, "commit", "-am", "docs: unrelated main commit 2")
            new_main_2 = _git(repo, "rev-parse", "main")
            _git(wt_eq2, "rebase", "main")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                ref_eq2 = _run_cli(["branch", "verify", "eq-rebase-2", "--why", "Eq2.", *project])
                self.assertEqual((ref_eq2["outcome"], ref_eq2["level"], ref_eq2["head_sha"]), ("request_changes", "L4", new_main_2))
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "eq-rebase-2", "--why", "Eq2.", *project])
            with mock.patch.object(AgyExecutor, "execute", make_arb("replan", "EQ2_DIR", "EQ2_DEAD")):
                arb_eq2 = _run_cli(["branch", "arbitrate", "eq-rebase-2", "--why", "Eq2.", *project])
            self.assertEqual((arb_eq2["action"], arb_eq2["head_sha"]), ("replan", new_main_2))
            _run_cli(["branch", "supersede", "eq-rebase-2", "--base", "refs/heads/main", *project])

            # 2: Two L4 rejections -> Arbiter `park` -> `branch verify --force-round` approves
            #    -> follow-up commit verifies cleanly without hitting the old pre-park cap!
            park_task_rel = "docs/tasks/active/20261117-park-recover.md"
            (repo / park_task_rel).write_text(
                "# Park Recover\n\n## Goal & Why\nRecover from park.\n\n## Bounds\npark.py only.\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(repo, "add", park_task_rel)
            _git(repo, "commit", "-m", "docs(tasks): open park-recover")

            wt_park = Path(_run_cli(["worktree", "create", "park-recover", *project])["path"])
            (wt_park / "park.py").write_text("PARK = 1\n", encoding="utf-8")
            _git(wt_park, "add", "park.py")
            _git(wt_park, "commit", "-m", "feat: park attempt 1")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli(["branch", "verify", "park-recover", "--level", "L4", *project])
                _run_cli(["branch", "verify", "park-recover", *project])

            with mock.patch.object(AgyExecutor, "execute", make_arb("park", "HUMAN_REVIEW_NEEDED")):
                arb_park = _run_cli(["branch", "arbitrate", "park-recover", *project])
            self.assertEqual(arb_park["action"], "park")
            self.assertTrue(_run_cli(["branch", "status", "park-recover", *project])["parked"])

            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                forced_app = _run_cli(["branch", "verify", "park-recover", "--force-round", *project])
            self.assertEqual((forced_app["outcome"], forced_app["forced_round"], forced_app["request_changes_rounds"]), ("approve", True, 0))
            st_after_force = _run_cli(["branch", "status", "park-recover", *project])
            self.assertFalse(st_after_force["parked"])
            self.assertTrue(st_after_force["ready_to_land"])

            # Follow-up commit on the same branch verifies cleanly WITHOUT `--force-round`!
            (wt_park / "park.py").write_text("PARK = 2\n", encoding="utf-8")
            _git(wt_park, "commit", "-am", "feat: follow-up after forced approve")
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                followup_v = _run_cli(["branch", "verify", "park-recover", "--base", "refs/heads/main", *project])
            self.assertEqual((followup_v["outcome"], followup_v["request_changes_rounds"]), ("approve", 0))

            # 3(b): Qualified `--base refs/heads/main` also works on `branch land` preview, `--execute`, and `--discard`!
            land_preview = _run_cli(["branch", "land", "park-recover", "--base", "refs/heads/main", *project])
            self.assertTrue(land_preview["ready_to_land"])
            land_exec = _run_cli(["branch", "land", "park-recover", "--base", "refs/heads/main", "--execute", *project])
            self.assertTrue(land_exec["landed"])
            self.assertEqual((repo / "park.py").read_text(encoding="utf-8"), "PARK = 2\n")

            wt_disc = Path(_run_cli(["worktree", "create", "qual-discard", *project])["path"])
            (wt_disc / "tmp_d.py").write_text("D = 1\n", encoding="utf-8")
            _git(wt_disc, "add", "tmp_d.py")
            _git(wt_disc, "commit", "-m", "wip: discardable")
            disc_res = _run_cli([
                "branch", "land", "qual-discard", "--base", "refs/heads/main",
                "--discard", "--reason", "Qualified base discard check", *project,
            ])
            self.assertTrue(disc_res["discarded"])

    def test_round12_custom_task_file_outside_ledger_never_archives_todo_basename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="base-r12")
            project = ["--project", str(repo)]

            todo_readme_rel = "docs/tasks/todo/README.md"
            todo_readme_content = "# Backlog README\n\nUNRELATED_TODO_README_SENTINEL\n"
            (repo / todo_readme_rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / todo_readme_rel).write_text(todo_readme_content, encoding="utf-8")
            _git(repo, "add", todo_readme_rel)
            _git(repo, "commit", "-m", "docs(tasks): add unrelated todo README")

            custom_spec_rel = "specs/README.md"
            custom_spec_content = (
                "# Custom Spec\n\n## Goal & Why\nCustom spec outside docs/tasks ledger.\n\n"
                "## Bounds\nOnly touched module files.\n\n## Done-check\ntrue\n"
            )

            def make_arb(action: str) -> Any:
                def _arb(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                    req.output_path.write_text(
                        json.dumps(
                            {
                                "action": action,
                                "summary": f"Arbiter chose {action} on custom spec.",
                                "directives": [] if action == "approve" else [f"DIR_{action.upper()}"],
                                "avoid_dead_ends": [] if action == "approve" else [f"DEAD_{action.upper()}"],
                                "waived_findings": ["Non-blocking nit"] if action == "approve" else [],
                                "commands_run": ["git diff"],
                            }
                        ),
                        encoding="utf-8",
                    )
                    return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}
                return _arb

            # 1) `supersede --archive-task` with branch-only `specs/README.md`:
            #    must leave `docs/tasks/todo/README.md` untouched and create no archive file.
            main_before_sup_branch = _git(repo, "rev-parse", "main")
            wt_sup_b = Path(_run_cli(["worktree", "create", "custom-sup-branch", *project])["path"])
            (wt_sup_b / custom_spec_rel).parent.mkdir(parents=True, exist_ok=True)
            (wt_sup_b / custom_spec_rel).write_text(custom_spec_content, encoding="utf-8")
            (wt_sup_b / "sup_b.py").write_text("SUP_B = 1\n", encoding="utf-8")
            _git(wt_sup_b, "add", custom_spec_rel, "sup_b.py")
            _git(wt_sup_b, "commit", "-m", "feat: branch-only custom spec attempt")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "custom-sup-branch",
                    "--task-file", custom_spec_rel, "--check", "true", *project,
                ])
            _run_cli(["branch", "dispute", "custom-sup-branch", "--reason", "Need replan", *project])
            with mock.patch.object(AgyExecutor, "execute", make_arb("replan")):
                _run_cli([
                    "branch", "arbitrate", "custom-sup-branch",
                    "--task-file", custom_spec_rel, *project,
                ])
            sup_b_res = _run_cli(["branch", "supersede", "custom-sup-branch", "--archive-task", *project])
            self.assertTrue(sup_b_res["superseded"])
            self.assertIsNone(sup_b_res["archived_task"])
            self.assertIsNone(sup_b_res["supersede_commit_sha"])
            self.assertEqual(_git(repo, "rev-parse", "main"), main_before_sup_branch)
            self.assertEqual((repo / todo_readme_rel).read_text(encoding="utf-8"), todo_readme_content)
            self.assertFalse((repo / "docs/tasks/archive/README.md").exists())

            # 2) `land --discard` with branch-only `specs/README.md`:
            #    must leave `docs/tasks/todo/README.md` untouched and create no archive file.
            wt_disc_b = Path(_run_cli(["worktree", "create", "custom-disc-branch", *project])["path"])
            (wt_disc_b / custom_spec_rel).parent.mkdir(parents=True, exist_ok=True)
            (wt_disc_b / custom_spec_rel).write_text(custom_spec_content, encoding="utf-8")
            (wt_disc_b / "disc_b.py").write_text("DISC_B = 1\n", encoding="utf-8")
            _git(wt_disc_b, "add", custom_spec_rel, "disc_b.py")
            _git(wt_disc_b, "commit", "-m", "wip: branch-only custom spec discard")
            disc_b_res = _run_cli([
                "branch", "land", "custom-disc-branch",
                "--task-file", custom_spec_rel,
                "--discard", "--reason", "Discard branch-only custom spec", *project,
            ])
            self.assertTrue(disc_b_res["discarded"])
            self.assertIsNone(disc_b_res["archived_task"])
            self.assertIsNone(disc_b_res["discard_commit_sha"])
            self.assertEqual(_git(repo, "rev-parse", "main"), main_before_sup_branch)
            self.assertEqual((repo / todo_readme_rel).read_text(encoding="utf-8"), todo_readme_content)
            self.assertFalse((repo / "docs/tasks/archive/README.md").exists())

            # Now commit `specs/README.md` on `main` as well and verify all four operations leave BOTH files intact!
            (repo / custom_spec_rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / custom_spec_rel).write_text(custom_spec_content, encoding="utf-8")
            _git(repo, "add", custom_spec_rel)
            _git(repo, "commit", "-m", "docs: add custom specs/README.md on main")
            main_with_spec = _git(repo, "rev-parse", "main")

            # 3) `supersede --archive-task` with `main`-committed `specs/README.md`:
            wt_sup_m = Path(_run_cli(["worktree", "create", "custom-sup-main", *project])["path"])
            (wt_sup_m / "sup_m.py").write_text("SUP_M = 1\n", encoding="utf-8")
            _git(wt_sup_m, "add", "sup_m.py")
            _git(wt_sup_m, "commit", "-m", "feat: main custom spec attempt")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "custom-sup-main",
                    "--task-file", custom_spec_rel, "--check", "true", *project,
                ])
            _run_cli(["branch", "dispute", "custom-sup-main", "--reason", "Need pivot", *project])
            with mock.patch.object(AgyExecutor, "execute", make_arb("pivot")):
                _run_cli([
                    "branch", "arbitrate", "custom-sup-main",
                    "--task-file", custom_spec_rel, *project,
                ])
            sup_m_res = _run_cli(["branch", "supersede", "custom-sup-main", "--archive-task", *project])
            self.assertTrue(sup_m_res["superseded"])
            self.assertIsNone(sup_m_res["archived_task"])
            self.assertIsNone(sup_m_res["supersede_commit_sha"])
            self.assertEqual(_git(repo, "rev-parse", "main"), main_with_spec)
            self.assertEqual((repo / custom_spec_rel).read_text(encoding="utf-8"), custom_spec_content)
            self.assertEqual((repo / todo_readme_rel).read_text(encoding="utf-8"), todo_readme_content)
            self.assertFalse((repo / "docs/tasks/archive/README.md").exists())

            # 4) `land --discard` with `main`-committed `specs/README.md`:
            wt_disc_m = Path(_run_cli(["worktree", "create", "custom-disc-main", *project])["path"])
            (wt_disc_m / "disc_m.py").write_text("DISC_M = 1\n", encoding="utf-8")
            _git(wt_disc_m, "add", "disc_m.py")
            _git(wt_disc_m, "commit", "-m", "wip: main custom spec discard")
            disc_m_res = _run_cli([
                "branch", "land", "custom-disc-main",
                "--task-file", custom_spec_rel,
                "--discard", "--reason", "Discard main custom spec", *project,
            ])
            self.assertTrue(disc_m_res["discarded"])
            self.assertIsNone(disc_m_res["archived_task"])
            self.assertIsNone(disc_m_res["discard_commit_sha"])
            self.assertEqual(_git(repo, "rev-parse", "main"), main_with_spec)
            self.assertEqual((repo / custom_spec_rel).read_text(encoding="utf-8"), custom_spec_content)
            self.assertEqual((repo / todo_readme_rel).read_text(encoding="utf-8"), todo_readme_content)
            self.assertFalse((repo / "docs/tasks/archive/README.md").exists())

            # 5) `land --squash --execute` on Arbiter-approved branch with `--task-file specs/README.md`:
            wt_sq = Path(_run_cli(["worktree", "create", "custom-sq", *project])["path"])
            (wt_sq / "sq_mod.py").write_text("SQ = 1\n", encoding="utf-8")
            _git(wt_sq, "add", "sq_mod.py")
            _git(wt_sq, "commit", "-m", "feat: squash custom spec slice")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "custom-sq",
                    "--task-file", custom_spec_rel, "--check", "true", *project,
                ])
            _run_cli(["branch", "dispute", "custom-sq", "--reason", "Matches custom spec", *project])
            with mock.patch.object(AgyExecutor, "execute", make_arb("approve")):
                arb_sq = _run_cli([
                    "branch", "arbitrate", "custom-sq",
                    "--task-file", custom_spec_rel, *project,
                ])
            land_sq = _run_cli([
                "branch", "land", "custom-sq",
                "--task-file", custom_spec_rel, "--execute", *project,
            ])
            self.assertTrue(land_sq["landed"])
            self.assertIsNone(land_sq["archived_task"])
            self.assertEqual((repo / "sq_mod.py").read_text(encoding="utf-8"), "SQ = 1\n")
            self.assertEqual((repo / custom_spec_rel).read_text(encoding="utf-8"), custom_spec_content)
            self.assertEqual((repo / todo_readme_rel).read_text(encoding="utf-8"), todo_readme_content)
            self.assertFalse((repo / "docs/tasks/archive/README.md").exists())
            sq_msg = _git(repo, "log", "-1", "--format=%B")
            self.assertIn(f"Keel-Arbitrated: approve ({arb_sq['run_id']})", sq_msg)

            # 6) `land --ff-only --execute` on Arbiter-approved branch with `--task-file specs/README.md`:
            wt_ff = Path(_run_cli(["worktree", "create", "custom-ff", *project])["path"])
            (wt_ff / "ff_mod.py").write_text("FF = 1\n", encoding="utf-8")
            _git(wt_ff, "add", "ff_mod.py")
            _git(wt_ff, "commit", "-m", "feat: ff-only custom spec slice")
            ff_head = _git(wt_ff, "rev-parse", "HEAD")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "custom-ff",
                    "--task-file", custom_spec_rel, "--check", "true", *project,
                ])
            _run_cli(["branch", "dispute", "custom-ff", "--reason", "Matches custom spec for ff", *project])
            with mock.patch.object(AgyExecutor, "execute", make_arb("approve")):
                arb_ff = _run_cli([
                    "branch", "arbitrate", "custom-ff",
                    "--task-file", custom_spec_rel, *project,
                ])
            land_ff = _run_cli([
                "branch", "land", "custom-ff", "--ff-only",
                "--task-file", custom_spec_rel, "--execute", *project,
            ])
            self.assertTrue(land_ff["landed"])
            self.assertIsNone(land_ff["archived_task"])
            self.assertEqual((repo / "ff_mod.py").read_text(encoding="utf-8"), "FF = 1\n")
            self.assertEqual((repo / custom_spec_rel).read_text(encoding="utf-8"), custom_spec_content)
            self.assertEqual((repo / todo_readme_rel).read_text(encoding="utf-8"), todo_readme_content)
            self.assertFalse((repo / "docs/tasks/archive/README.md").exists())
            self.assertEqual(_git(repo, "rev-parse", "main~1"), ff_head)
            self.assertEqual(land_ff["landed_sha"], _git(repo, "rev-parse", "main"))
            ff_msg = _git(repo, "log", "-1", "--format=%B")
            self.assertIn("chore(gate): record arbitration for task/custom-ff", ff_msg)
            self.assertIn(f"Keel-Arbitrated: approve ({arb_ff['run_id']})", ff_msg)

    def test_round13_stacked_non_default_base_guidance_and_supersede(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="parent-r13")
            project = ["--project", str(repo)]

            def make_arb(action: str, directive: str, dead_end: str) -> Any:
                def _arb(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                    req.output_path.write_text(
                        json.dumps(
                            {
                                "action": action,
                                "summary": f"Stacked arbiter selected {action}.",
                                "directives": [directive],
                                "avoid_dead_ends": [dead_end],
                                "waived_findings": [],
                                "commands_run": ["git status"],
                            }
                        ),
                        encoding="utf-8",
                    )
                    return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}
                return _arb

            # Create stacked `task/parent` in `.agents/worktrees/parent` while primary checkout stays on `main`
            wt_parent = Path(_run_cli(["worktree", "create", "parent", *project])["path"])
            (wt_parent / "parent.py").write_text("PARENT = 1\n", encoding="utf-8")
            _git(wt_parent, "add", "parent.py")
            _git(wt_parent, "commit", "-m", "feat: parent slice")

            # Create `task/child` stacked on `--base task/parent`
            wt_child = Path(_run_cli(["worktree", "create", "child", "--base", "task/parent", *project])["path"])
            (wt_child / "child.py").write_text("CHILD = 1\n", encoding="utf-8")
            _git(wt_child, "add", "child.py")
            _git(wt_child, "commit", "-m", "feat: child slice")
            child_head = _git(wt_child, "rev-parse", "HEAD")

            # Before verification: `arbitrate`, `supersede`, and `worktree cleanup` all preserve `--base task/parent`!
            with self.assertRaisesRegex(KeelError, r"keel branch verify child --base task/parent"):
                _run_cli(["branch", "arbitrate", "child", "--base", "task/parent", "--why", "Child slice.", *project])
            with self.assertRaisesRegex(
                KeelError,
                r"keel branch arbitrate child --base task/parent.*keel branch land child --base task/parent --discard",
            ):
                _run_cli(["branch", "supersede", "child", "--base", "task/parent", *project])
            with self.assertRaisesRegex(KeelError, r"keel branch land task/child --base task/parent"):
                _run_cli(["worktree", "cleanup", "child", "--base", "task/parent", *project])

            # Two L4 rejections on `child --base task/parent` hit the convergence cap
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "child", "--base", "task/parent",
                    "--level", "L4", "--why", "Child slice.", "--check", "true", *project,
                ])
                _run_cli([
                    "branch", "verify", "child", "--base", "task/parent",
                    "--why", "Child slice.", *project,
                ])

                # `branch verify`, `branch status`, and `branch land` preview all preserve `--base task/parent`!
                with self.assertRaisesRegex(KeelError, r"keel branch arbitrate child --base task/parent"):
                    _run_cli(["branch", "verify", "child", "--base", "task/parent", "--why", "Child slice.", *project])

            st_cap = _run_cli(["branch", "status", "child", "--base", "task/parent", *project])
            self.assertFalse(st_cap["ready_to_land"])
            self.assertTrue(
                any("keel branch arbitrate child --base task/parent" in b for b in st_cap["blockers"]),
                st_cap["blockers"],
            )
            land_cap = _run_cli(["branch", "land", "child", "--base", "task/parent", *project])
            self.assertFalse(land_cap["ready_to_land"])
            self.assertTrue(
                any("keel branch arbitrate child --base task/parent" in b for b in land_cap["blockers"]),
                land_cap["blockers"],
            )

            # Arbitrate `child --base task/parent` -> `replan`
            with mock.patch.object(AgyExecutor, "execute", make_arb("replan", "STACKED_REPLAN_DIR", "STACKED_DEAD_END")):
                arb_res = _run_cli([
                    "branch", "arbitrate", "child", "--base", "task/parent", "--why", "Child slice.", *project,
                ])
            self.assertEqual(arb_res["action"], "replan")

            # `branch status`, `branch verify`, and `branch dispute` all suggest `keel branch supersede child --base task/parent`!
            st_replan = _run_cli(["branch", "status", "child", "--base", "task/parent", *project])
            self.assertTrue(
                any("keel branch supersede child --base task/parent" in b for b in st_replan["blockers"]),
                st_replan["blockers"],
            )
            with self.assertRaisesRegex(KeelError, r"keel branch supersede child --base task/parent"):
                _run_cli(["branch", "verify", "child", "--base", "task/parent", "--why", "Child slice.", *project])
            with self.assertRaisesRegex(KeelError, r"keel branch supersede child --base task/parent"):
                _run_cli(["branch", "dispute", "child", "--base", "task/parent", "--reason", "Still disputing", *project])

            # Execute `keel branch supersede child --base task/parent` while primary checkout is on `main`
            # and `task/parent` is checked out in `.agents/worktrees/parent`!
            self.assertEqual(_git(repo, "branch", "--show-current"), "main")
            sup_res = _run_cli(["branch", "supersede", "child", "--base", "task/parent", *project])
            self.assertTrue(sup_res["superseded"])
            self.assertEqual((sup_res["action"], sup_res["recovery_count"]), ("replan", 1))
            self.assertEqual(_git(repo, "rev-parse", sup_res["preserved_ref"]), child_head)
            self.assertFalse(wt_child.exists())
            self.assertTrue(wt_parent.exists())

            # Also verify stacked `supersede --archive-task` commits task archival inside `.agents/worktrees/parent`
            # (leaving `main` untouched) and fails cleanly if `task/parent` worktree is dirty!
            child_task_rel = "docs/tasks/todo/20261121-child-arch.md"
            (wt_parent / child_task_rel).parent.mkdir(parents=True, exist_ok=True)
            (wt_parent / child_task_rel).write_text(
                "# Child Arch\n\n## Goal & Why\nStacked archive test.\n\n## Bounds\nchild_arch.py only.\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            _git(wt_parent, "add", child_task_rel)
            _git(wt_parent, "commit", "-m", "docs(tasks): add stacked child todo task")
            main_before_arch = _git(repo, "rev-parse", "main")

            wt_child_arch = Path(_run_cli(["worktree", "create", "child-arch", "--base", "task/parent", *project])["path"])
            (wt_child_arch / "child_arch.py").write_text("ARCH = 1\n", encoding="utf-8")
            _git(wt_child_arch, "add", "child_arch.py")
            _git(wt_child_arch, "commit", "-m", "feat: child-arch slice")
            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "child-arch", "--base", "task/parent",
                    "--task-file", child_task_rel, "--check", "true", *project,
                ])
            _run_cli(["branch", "dispute", "child-arch", "--base", "task/parent", "--reason", "Need pivot", *project])
            with mock.patch.object(AgyExecutor, "execute", make_arb("pivot", "ARCH_PIVOT_DIR", "ARCH_PIVOT_DEAD")):
                _run_cli([
                    "branch", "arbitrate", "child-arch", "--base", "task/parent",
                    "--task-file", child_task_rel, *project,
                ])

            # Dirty `task/parent` worktree blocks `--archive-task`
            (wt_parent / "dirty.tmp").write_text("untracked\n", encoding="utf-8")
            with self.assertRaisesRegex(KeelError, "uncommitted or untracked changes"):
                _run_cli(["branch", "supersede", "child-arch", "--base", "task/parent", "--archive-task", *project])
            (wt_parent / "dirty.tmp").unlink()

            sup_arch = _run_cli(["branch", "supersede", "child-arch", "--base", "task/parent", "--archive-task", *project])
            self.assertTrue(sup_arch["superseded"])
            self.assertEqual(sup_arch["archived_task"], "docs/tasks/archive/20261121-child-arch.md")
            self.assertIsNotNone(sup_arch["supersede_commit_sha"])
            self.assertEqual(_git(repo, "rev-parse", "main"), main_before_arch)
            self.assertEqual(_git(wt_parent, "rev-parse", "HEAD"), sup_arch["supersede_commit_sha"])
            self.assertFalse((wt_parent / child_task_rel).exists())
            self.assertTrue((wt_parent / "docs/tasks/archive/20261121-child-arch.md").is_file())
            self.assertFalse((repo / "docs/tasks/archive/20261121-child-arch.md").exists())

    def test_round13_rewritten_parent_refresh_and_negative_control(self) -> None:
        def make_arb(action: str, directive: str, dead_end: str) -> Any:
            def _arb(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": action,
                            "summary": f"Rewritten-parent arbiter chose {action}.",
                            "directives": [directive],
                            "avoid_dead_ends": [dead_end],
                            "waived_findings": [],
                            "commands_run": ["git status"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}
            return _arb

        # Case A & B: Rebased & fast-forward landed parent recovery (WITHOUT active task archival AND
        # WITH active task archival commit on `main`, testing both inline `--arbitrate` and two-step
        # `verify` -> block second `verify` -> `arbitrate`, plus squash-landed parent WITH task archival!)
        for case_name, with_active_task, land_mode, use_inline_arbitrate in (
            ("ff_no_archive_inline", False, "ff-only", True),
            ("ff_no_archive_twostep", False, "ff-only", False),
            ("ff_with_archive_inline", True, "ff-only", True),
            ("ff_with_archive_twostep", True, "ff-only", False),
            ("squash_with_archive_twostep", True, "squash", False),
        ):
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                _init_repo_with_task(repo, slug="parent" if with_active_task else "unrelated")
                project = ["--project", str(repo)]

                # Create verified `parent` (`P0` on `M0`)
                wt_parent = Path(_run_cli(["worktree", "create", "parent", *project])["path"])
                (wt_parent / "parent.py").write_text("PARENT = 1\n", encoding="utf-8")
                _git(wt_parent, "add", "parent.py")
                _git(wt_parent, "commit", "-m", "feat: parent base commit")
                p0 = _git(wt_parent, "rev-parse", "HEAD")

                with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                    _run_cli([
                        "branch", "verify", "parent", "--why", "Parent slice.", "--check", "true", *project,
                    ])

                # Create capped `child` (`C0` on `P0`, 2 L4 rejections against `--base task/parent`)
                wt_child = Path(_run_cli(["worktree", "create", "child", "--base", "task/parent", *project])["path"])
                (wt_child / "child.py").write_text("CHILD = 1\n", encoding="utf-8")
                _git(wt_child, "add", "child.py")
                _git(wt_child, "commit", "-m", "feat: child slice")
                c0 = _git(wt_child, "rev-parse", "HEAD")

                with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                    _run_cli([
                        "branch", "verify", "child", "--base", "task/parent",
                        "--level", "L4", "--why", "Child slice.", "--check", "true", *project,
                    ])
                    _run_cli([
                        "branch", "verify", "child", "--base", "task/parent",
                        "--why", "Child slice.", *project,
                    ])
                    with self.assertRaisesRegex(KeelError, "convergence cap"):
                        _run_cli([
                            "branch", "verify", "child", "--base", "task/parent",
                            "--why", "Child slice.", *project,
                        ])

                # Advance `main` (`M0 -> M1`), rebase `parent` onto `main` (`P1`), verify & land `parent` onto `main`
                (repo / "main_advance.txt").write_text(f"advance {case_name}\n", encoding="utf-8")
                _git(repo, "add", "main_advance.txt")
                _git(repo, "commit", "-m", "chore: advance main M0 -> M1")
                m1 = _git(repo, "rev-parse", "main")

                _git(wt_parent, "rebase", "main")
                p1 = _git(wt_parent, "rev-parse", "HEAD")
                self.assertNotEqual(p1, p0)

                with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                    _run_cli(["branch", "verify", "parent", "--why", "Rebased parent.", "--check", "true", *project])
                land_args = ["branch", "land", "parent", "--execute", *project]
                if land_mode == "ff-only":
                    land_args.insert(3, "--ff-only")
                land_parent = _run_cli(land_args)
                self.assertTrue(land_parent["landed"])
                new_main = _git(repo, "rev-parse", "main")
                if with_active_task:
                    self.assertIsNotNone(land_parent["archived_task"])
                    if land_mode == "ff-only":
                        self.assertEqual(_git(repo, "rev-parse", "main~1"), p1)
                        self.assertNotEqual(new_main, p1)
                else:
                    self.assertIsNone(land_parent["archived_task"])
                    self.assertEqual(new_main, p1)

                # Rebase `child` onto `main` (`C1`)
                _git(wt_child, "rebase", "--onto", "main", p0)
                c1 = _git(wt_child, "rev-parse", "HEAD")
                self.assertNotEqual(c1, c0)
                self.assertEqual(_git(repo, "merge-base", "main", "task/child"), new_main)

                # Confirm public `compare_rebase` STILL enforces `old-base <= new-base`!
                with self.assertRaisesRegex(KeelError, "old-base <= new-base"):
                    _run_cli([
                        "branch", "compare",
                        "--old-base", p0, "--old-head", c0,
                        "--base", new_main, "--head", c1, *project,
                    ])

                # Confirm `keel branch arbitrate child` BEFORE reviewing `C1` refuses because `C1` is unverified!
                with self.assertRaisesRegex(KeelError, "has not been verified yet; run `keel branch verify child`"):
                    _run_cli(["branch", "arbitrate", "child", "--why", "Child slice.", *project])

                if use_inline_arbitrate:
                    # Inline `keel branch verify child --arbitrate` refreshes once at L4 and arbitrates `C1` cleanly!
                    with (
                        mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                        mock.patch.object(AgyExecutor, "execute", make_arb("replan", "INLINE_DIR", "INLINE_DEAD")),
                    ):
                        v_inline = _run_cli([
                            "branch", "verify", "child", "--arbitrate",
                            "--why", "Child slice.", "--check", "true", *project,
                        ])
                    self.assertEqual((v_inline["outcome"], v_inline["level"], v_inline["head_sha"]), ("request_changes", "L4", c1))
                    self.assertEqual(v_inline["arbitration"]["action"], "replan")
                    self.assertEqual(v_inline["arbitration"]["head_sha"], c1)
                    self.assertEqual(v_inline["arbitration"]["merge_base_sha"], new_main)
                else:
                    # Two-step: first `verify child` runs one fresh L4 round on `C1`;
                    # second `verify child` on the SAME `main` merge-base is BLOCKED at the convergence cap;
                    # then `arbitrate child` arbitrates `C1` cleanly!
                    with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                        v_ref = _run_cli([
                            "branch", "verify", "child", "--why", "Child slice.", "--check", "true", *project,
                        ])
                        self.assertEqual((v_ref["outcome"], v_ref["level"], v_ref["head_sha"]), ("request_changes", "L4", c1))
                        with self.assertRaisesRegex(KeelError, "convergence cap"):
                            _run_cli(["branch", "verify", "child", "--why", "Child slice.", *project])
                    with mock.patch.object(AgyExecutor, "execute", make_arb("replan", "TWOSTEP_DIR", "TWOSTEP_DEAD")):
                        arb_c1 = _run_cli(["branch", "arbitrate", "child", "--why", "Child slice.", *project])
                    self.assertEqual((arb_c1["action"], arb_c1["head_sha"]), ("replan", c1))
                    self.assertEqual(arb_c1["arbitration"]["merge_base_sha"], new_main)

                # Confirm `m1` was indeed behind `new_main`
                self.assertNotEqual(new_main, m1)

        # Negative control: if `parent` (`P0`) was NEVER landed onto `M1` (dropped) and `child` (`C0`)
        # is rebased `--onto main P0`, `keel branch verify child` MUST still refuse at the convergence cap!
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="unrelated")
            project = ["--project", str(repo)]

            wt_parent = Path(_run_cli(["worktree", "create", "parent", *project])["path"])
            (wt_parent / "parent.py").write_text("PARENT_DROPPED = 1\n", encoding="utf-8")
            _git(wt_parent, "add", "parent.py")
            _git(wt_parent, "commit", "-m", "feat: parent commit never landed")
            p0 = _git(wt_parent, "rev-parse", "HEAD")

            wt_child = Path(_run_cli(["worktree", "create", "child", "--base", "task/parent", *project])["path"])
            (wt_child / "child.py").write_text("CHILD = 1\n", encoding="utf-8")
            _git(wt_child, "add", "child.py")
            _git(wt_child, "commit", "-m", "feat: child commit on dropped parent")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "child", "--base", "task/parent",
                    "--level", "L4", "--why", "Child slice.", "--check", "true", *project,
                ])
                _run_cli([
                    "branch", "verify", "child", "--base", "task/parent",
                    "--why", "Child slice.", *project,
                ])

            # Advance `main` with an unrelated commit WITHOUT landing `parent`
            (repo / "unrelated.txt").write_text("unrelated main change\n", encoding="utf-8")
            _git(repo, "add", "unrelated.txt")
            _git(repo, "commit", "-m", "chore: unrelated main advance")

            # Rebase `child` `--onto main P0` (dropping `parent`!)
            _git(wt_child, "rebase", "--onto", "main", p0)
            self.assertFalse((wt_child / "parent.py").exists())

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "child", "--why", "Child slice.", *project])

    def test_round14_multi_commit_squash_parent_inclusion_and_discarded_negative_control(self) -> None:
        def make_arb(action: str, directive: str, dead_end: str) -> Any:
            def _arb(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": action,
                            "summary": f"Round 14 arbiter chose {action}.",
                            "directives": [directive],
                            "avoid_dead_ends": [dead_end],
                            "waived_findings": [],
                            "commands_run": ["git status"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}
            return _arb

        for case_name, advance_main, use_inline_arbitrate in (
            ("two_commit_squash_twostep", False, False),
            ("two_commit_squash_inline", False, True),
            ("two_commit_rebased_squash_twostep", True, False),
            ("two_commit_rebased_squash_inline", True, True),
        ):
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                _init_repo_with_task(repo, slug="parent")
                project = ["--project", str(repo)]

                # Create `parent` at `P0`
                wt_parent = Path(_run_cli(["worktree", "create", "parent", *project])["path"])
                (wt_parent / "parent.py").write_text("PARENT = 1\n", encoding="utf-8")
                _git(wt_parent, "add", "parent.py")
                _git(wt_parent, "commit", "-m", "feat: parent P0")
                p0 = _git(wt_parent, "rev-parse", "HEAD")

                # Branch `child` from `parent` (`P0`) and hit two L4 rejections on `--base task/parent`
                wt_child = Path(_run_cli(["worktree", "create", "child", "--base", "task/parent", *project])["path"])
                (wt_child / "child.py").write_text("CHILD = 1\n", encoding="utf-8")
                _git(wt_child, "add", "child.py")
                _git(wt_child, "commit", "-m", "feat: child slice")
                c0 = _git(wt_child, "rev-parse", "HEAD")

                with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                    _run_cli([
                        "branch", "verify", "child", "--base", "task/parent",
                        "--level", "L4", "--why", "Child slice.", "--check", "true", *project,
                    ])
                    _run_cli([
                        "branch", "verify", "child", "--base", "task/parent",
                        "--why", "Child slice.", *project,
                    ])
                    with self.assertRaisesRegex(KeelError, "convergence cap"):
                        _run_cli([
                            "branch", "verify", "child", "--base", "task/parent",
                            "--why", "Child slice.", *project,
                        ])

                # Add follow-up regression test commit `P1` on `parent` (`P0 -> P1`)
                (wt_parent / "test_parent_regression.py").write_text("def test_parent() -> None:\n    assert True\n", encoding="utf-8")
                _git(wt_parent, "add", "test_parent_regression.py")
                _git(wt_parent, "commit", "-m", "test: add parent regression P1")
                p1_orig = _git(wt_parent, "rev-parse", "HEAD")
                self.assertNotEqual(p1_orig, p0)

                if advance_main:
                    (repo / "main_advance.txt").write_text(f"main advance {case_name}\n", encoding="utf-8")
                    _git(repo, "add", "main_advance.txt")
                    _git(repo, "commit", "-m", "chore: advance main M0 -> M1")
                    _git(wt_parent, "rebase", "main")
                    self.assertNotEqual(_git(wt_parent, "rev-parse", "HEAD"), p1_orig)

                # Verify `parent` and squash-land `parent` onto `main`
                with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                    _run_cli(["branch", "verify", "parent", "--why", "Parent with P1.", "--check", "true", *project])
                land_parent = _run_cli(["branch", "land", "parent", "--execute", *project])
                self.assertTrue(land_parent["landed"])
                self.assertEqual(land_parent["strategy"], "squash")
                new_main = _git(repo, "rev-parse", "main")

                # Cleanly rebase `child` onto `main` (`git rebase --onto main P0`)
                _git(wt_child, "rebase", "--onto", "main", p0)
                c1 = _git(wt_child, "rev-parse", "HEAD")
                self.assertNotEqual(c1, c0)
                self.assertEqual(_git(repo, "merge-base", "main", "task/child"), new_main)
                self.assertTrue((wt_child / "parent.py").is_file())
                self.assertTrue((wt_child / "test_parent_regression.py").is_file())

                # `keel branch arbitrate child` BEFORE reviewing `C1` must refuse!
                with self.assertRaisesRegex(KeelError, "has not been verified yet; run `keel branch verify child`"):
                    _run_cli(["branch", "arbitrate", "child", "--why", "Child slice.", *project])

                if use_inline_arbitrate:
                    with (
                        mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                        mock.patch.object(AgyExecutor, "execute", make_arb("replan", "R14_INLINE_DIR", "R14_INLINE_DEAD")),
                    ):
                        v_inline = _run_cli([
                            "branch", "verify", "child", "--arbitrate",
                            "--why", "Child slice.", "--check", "true", *project,
                        ])
                    self.assertEqual((v_inline["outcome"], v_inline["level"], v_inline["head_sha"]), ("request_changes", "L4", c1))
                    self.assertEqual(v_inline["arbitration"]["action"], "replan")
                    self.assertEqual(v_inline["arbitration"]["head_sha"], c1)
                    self.assertEqual(v_inline["arbitration"]["merge_base_sha"], new_main)
                else:
                    with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                        v_ref = _run_cli([
                            "branch", "verify", "child", "--why", "Child slice.", "--check", "true", *project,
                        ])
                        self.assertEqual((v_ref["outcome"], v_ref["level"], v_ref["head_sha"]), ("request_changes", "L4", c1))
                        with self.assertRaisesRegex(KeelError, "convergence cap"):
                            _run_cli(["branch", "verify", "child", "--why", "Child slice.", *project])
                    with mock.patch.object(AgyExecutor, "execute", make_arb("replan", "R14_TWOSTEP_DIR", "R14_TWOSTEP_DEAD")):
                        arb_c1 = _run_cli(["branch", "arbitrate", "child", "--why", "Child slice.", *project])
                    self.assertEqual((arb_c1["action"], arb_c1["head_sha"]), ("replan", c1))
                    self.assertEqual(arb_c1["arbitration"]["merge_base_sha"], new_main)

        # Negative control: `--discard`ing a parent and rebasing `child --onto main P0` STILL fails at the convergence cap!
        with self.subTest(case="discarded_parent_negative_control"), tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="parent")
            project = ["--project", str(repo)]

            wt_parent = Path(_run_cli(["worktree", "create", "parent", *project])["path"])
            (wt_parent / "parent.py").write_text("PARENT_DISCARDED = 1\n", encoding="utf-8")
            _git(wt_parent, "add", "parent.py")
            _git(wt_parent, "commit", "-m", "feat: parent P0 to be discarded")
            p0 = _git(wt_parent, "rev-parse", "HEAD")

            wt_child = Path(_run_cli(["worktree", "create", "child", "--base", "task/parent", *project])["path"])
            (wt_child / "child.py").write_text("CHILD = 1\n", encoding="utf-8")
            _git(wt_child, "add", "child.py")
            _git(wt_child, "commit", "-m", "feat: child on discarded parent")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "child", "--base", "task/parent",
                    "--level", "L4", "--why", "Child slice.", "--check", "true", *project,
                ])
                _run_cli([
                    "branch", "verify", "child", "--base", "task/parent",
                    "--why", "Child slice.", *project,
                ])

            # Discard `parent` (archives its active task as [DISCARDED] on `main` and deletes `task/parent`)
            disc = _run_cli(["branch", "land", "parent", "--discard", "--reason", "Abandoned parent approach", *project])
            self.assertTrue(disc["discarded"])
            self.assertIsNotNone(disc["discard_commit_sha"])

            # Rebase `child --onto main P0` (dropping `parent`!)
            _git(wt_child, "rebase", "--onto", "main", p0)
            self.assertFalse((wt_child / "parent.py").exists())

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "child", "--why", "Child slice.", *project])

    def test_round14_stacked_supersede_primary_checkout_preconditions(self) -> None:
        def make_arb(action: str, directive: str, dead_end: str) -> Any:
            def _arb(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": action,
                            "summary": f"Stacked precondition arbiter chose {action}.",
                            "directives": [directive],
                            "avoid_dead_ends": [dead_end],
                            "waived_findings": [],
                            "commands_run": ["git status"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}
            return _arb

        # 1) Dirty primary checkout on `task/parent` (untracked file) + `keel branch supersede child --base task/parent`:
        #    must fail upfront, leaving `refs/heads/task/child` intact and creating ZERO `refs/keel/superseded/...` refs!
        with self.subTest(case="dirty_primary_on_parent"), tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="parent-dirty")
            project = ["--project", str(repo)]

            _git(repo, "checkout", "-b", "task/parent")
            (repo / "parent.py").write_text("PARENT = 1\n", encoding="utf-8")
            _git(repo, "add", "parent.py")
            _git(repo, "commit", "-m", "feat: parent in primary checkout")

            wt_child = Path(_run_cli(["worktree", "create", "child", "--base", "task/parent", *project])["path"])
            (wt_child / "child.py").write_text("CHILD = 1\n", encoding="utf-8")
            _git(wt_child, "add", "child.py")
            _git(wt_child, "commit", "-m", "feat: child slice")
            child_head = _git(wt_child, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "child", "--base", "task/parent",
                    "--why", "Child slice.", "--check", "true", *project,
                ])
            _run_cli(["branch", "dispute", "child", "--base", "task/parent", "--reason", "Need replan", *project])
            with mock.patch.object(AgyExecutor, "execute", make_arb("replan", "DIR_1", "DEAD_1")):
                _run_cli(["branch", "arbitrate", "child", "--base", "task/parent", "--why", "Child slice.", *project])

            (repo / "primary_untracked.tmp").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(KeelError, r"Primary checkout .* has uncommitted or untracked changes"):
                _run_cli(["branch", "supersede", "child", "--base", "task/parent", *project])
            self.assertEqual(_git(repo, "rev-parse", "refs/heads/task/child"), child_head)
            self.assertEqual(_git(repo, "for-each-ref", "refs/keel/superseded/"), "")
            self.assertTrue(wt_child.exists())

        # 2) `task/child` checked out in primary checkout + `task/parent` in linked worktree + `--archive-task`:
        #    must fail upfront, leaving `task/parent` with ZERO archive commits and ZERO `refs/keel/superseded/...` refs!
        with self.subTest(case="child_checked_out_in_primary"), tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="parent-wt")
            project = ["--project", str(repo)]

            wt_parent = Path(_run_cli(["worktree", "create", "parent", *project])["path"])
            child_task_rel = "docs/tasks/todo/20261121-child-primary.md"
            (wt_parent / child_task_rel).parent.mkdir(parents=True, exist_ok=True)
            (wt_parent / child_task_rel).write_text(
                "# Child Primary\n\n## Goal & Why\nPrimary checkout guard.\n\n## Bounds\nchild.py only.\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            (wt_parent / "parent.py").write_text("PARENT = 1\n", encoding="utf-8")
            _git(wt_parent, "add", "parent.py", child_task_rel)
            _git(wt_parent, "commit", "-m", "feat: parent with child todo task")
            parent_head_before = _git(wt_parent, "rev-parse", "HEAD")

            _git(repo, "checkout", "-b", "task/child", "task/parent")
            (repo / "child.py").write_text("CHILD = 1\n", encoding="utf-8")
            _git(repo, "add", "child.py")
            _git(repo, "commit", "-m", "feat: child in primary checkout")
            child_head = _git(repo, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "child", "--base", "task/parent",
                    "--task-file", child_task_rel, "--check", "true", *project,
                ])
            _run_cli(["branch", "dispute", "child", "--base", "task/parent", "--reason", "Need pivot", *project])
            with mock.patch.object(AgyExecutor, "execute", make_arb("pivot", "DIR_2", "DEAD_2")):
                _run_cli([
                    "branch", "arbitrate", "child", "--base", "task/parent",
                    "--task-file", child_task_rel, *project,
                ])

            with self.assertRaisesRegex(
                KeelError,
                r"Cannot supersede branch 'task/child' while it is checked out in primary checkout",
            ):
                _run_cli(["branch", "supersede", "child", "--base", "task/parent", "--archive-task", *project])
            self.assertEqual(_git(wt_parent, "rev-parse", "HEAD"), parent_head_before)
            self.assertEqual(_git(repo, "rev-parse", "refs/heads/task/child"), child_head)
            self.assertTrue((wt_parent / child_task_rel).is_file())
            self.assertFalse((wt_parent / "docs/tasks/archive/20261121-child-primary.md").exists())
            self.assertEqual(_git(repo, "for-each-ref", "refs/keel/superseded/"), "")

        # 3) Supported layout (clean `main` in primary checkout + clean linked `task/parent` + clean linked `task/child`):
        #    blocks if `main` is dirty, and succeeds cleanly once `main` is clean!
        with self.subTest(case="supported_stacked_layout"), tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="parent-ok")
            project = ["--project", str(repo)]

            wt_parent = Path(_run_cli(["worktree", "create", "parent", *project])["path"])
            child_task_rel = "docs/tasks/todo/20261121-child-ok.md"
            (wt_parent / child_task_rel).parent.mkdir(parents=True, exist_ok=True)
            (wt_parent / child_task_rel).write_text(
                "# Child OK\n\n## Goal & Why\nSupported stacked supersede.\n\n## Bounds\nchild.py only.\n\n## Done-check\ntrue\n",
                encoding="utf-8",
            )
            (wt_parent / "parent.py").write_text("PARENT = 1\n", encoding="utf-8")
            _git(wt_parent, "add", "parent.py", child_task_rel)
            _git(wt_parent, "commit", "-m", "feat: parent with child todo task")
            parent_head_before = _git(wt_parent, "rev-parse", "HEAD")
            main_before = _git(repo, "rev-parse", "main")

            wt_child = Path(_run_cli(["worktree", "create", "child", "--base", "task/parent", *project])["path"])
            (wt_child / "child.py").write_text("CHILD = 1\n", encoding="utf-8")
            _git(wt_child, "add", "child.py")
            _git(wt_child, "commit", "-m", "feat: child slice")
            child_head = _git(wt_child, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "child", "--base", "task/parent",
                    "--task-file", child_task_rel, "--check", "true", *project,
                ])
            _run_cli(["branch", "dispute", "child", "--base", "task/parent", "--reason", "Need replan", *project])
            with mock.patch.object(AgyExecutor, "execute", make_arb("replan", "DIR_3", "DEAD_3")):
                _run_cli([
                    "branch", "arbitrate", "child", "--base", "task/parent",
                    "--task-file", child_task_rel, *project,
                ])

            # Dirty `main` in primary checkout also blocks upfront with zero side effects
            (repo / "main_dirty.tmp").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(KeelError, r"Primary checkout .* has uncommitted or untracked changes"):
                _run_cli(["branch", "supersede", "child", "--base", "task/parent", "--archive-task", *project])
            self.assertEqual(_git(wt_parent, "rev-parse", "HEAD"), parent_head_before)
            self.assertEqual(_git(repo, "for-each-ref", "refs/keel/superseded/"), "")
            (repo / "main_dirty.tmp").unlink()

            # Clean `main` + clean linked `task/parent` + clean linked `task/child` succeeds cleanly!
            self.assertEqual(_git(repo, "branch", "--show-current"), "main")
            sup = _run_cli(["branch", "supersede", "child", "--base", "task/parent", "--archive-task", *project])
            self.assertTrue(sup["superseded"])
            self.assertEqual(sup["archived_task"], "docs/tasks/archive/20261121-child-ok.md")
            self.assertIsNotNone(sup["supersede_commit_sha"])
            self.assertEqual(_git(repo, "rev-parse", "main"), main_before)
            self.assertEqual(_git(wt_parent, "rev-parse", "HEAD"), sup["supersede_commit_sha"])
            self.assertEqual(_git(repo, "rev-parse", sup["preserved_ref"]), child_head)
            self.assertFalse(wt_child.exists())
            self.assertTrue((wt_parent / "docs/tasks/archive/20261121-child-ok.md").is_file())
            self.assertFalse((wt_parent / child_task_rel).exists())

    def test_round15_conflict_resolved_child_refresh_and_negative_controls(self) -> None:
        def make_arb(action: str, directive: str, dead_end: str) -> Any:
            def _arb(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": action,
                            "summary": f"Round 15 arbiter chose {action}.",
                            "directives": [directive],
                            "avoid_dead_ends": [dead_end],
                            "waived_findings": [],
                            "commands_run": ["git status"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}
            return _arb

        def rebase_with_conflict(wt: Path, rebase_args: list[str], conflict_file: str, resolved_text: str) -> str:
            env = {**os.environ, "GIT_EDITOR": "true"}
            proc = subprocess.run(
                ["git", "-C", str(wt), "rebase", *rebase_args],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertNotEqual(proc.returncode, 0, f"Expected rebase conflict in {wt}, got clean rebase.")
            (wt / conflict_file).write_text(resolved_text, encoding="utf-8")
            _git(wt, "add", conflict_file)
            subprocess.run(
                ["git", "-C", str(wt), "-c", "core.editor=true", "rebase", "--continue"],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            return _git(wt, "rev-parse", "HEAD")

        # 1) Conflict-resolved child rebase onto squash-landed parent (inline `--arbitrate` & two-step)
        for case_name, use_inline_arbitrate in (
            ("squash_conflict_inline", True),
            ("squash_conflict_twostep", False),
        ):
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                _init_repo_with_task(repo, slug="parent")
                project = ["--project", str(repo)]

                wt_parent = Path(_run_cli(["worktree", "create", "parent", *project])["path"])
                (wt_parent / "shared.py").write_text('LINE = "parent_v1"\n', encoding="utf-8")
                _git(wt_parent, "add", "shared.py")
                _git(wt_parent, "commit", "-m", "feat: parent v1")
                p0 = _git(wt_parent, "rev-parse", "HEAD")

                wt_child = Path(_run_cli(["worktree", "create", "child", "--base", "task/parent", *project])["path"])
                (wt_child / "shared.py").write_text('LINE = "child_v1"\n', encoding="utf-8")
                _git(wt_child, "add", "shared.py")
                _git(wt_child, "commit", "-m", "feat: child v1")
                c0 = _git(wt_child, "rev-parse", "HEAD")

                with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                    _run_cli([
                        "branch", "verify", "child", "--base", "task/parent",
                        "--level", "L4", "--why", "Child slice.", "--check", "true", *project,
                    ])
                    _run_cli([
                        "branch", "verify", "child", "--base", "task/parent",
                        "--why", "Child slice.", *project,
                    ])
                    with self.assertRaisesRegex(KeelError, "convergence cap"):
                        _run_cli([
                            "branch", "verify", "child", "--base", "task/parent",
                            "--why", "Child slice.", *project,
                        ])

                # Parent modifies the same line in a follow-up commit, verifies, and squash-lands onto `main`
                (wt_parent / "shared.py").write_text('LINE = "parent_v2"\n', encoding="utf-8")
                _git(wt_parent, "add", "shared.py")
                _git(wt_parent, "commit", "-m", "feat: parent v2 same line")
                with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                    _run_cli(["branch", "verify", "parent", "--why", "Parent v2.", "--check", "true", *project])
                land_parent = _run_cli(["branch", "land", "parent", "--execute", *project])
                self.assertTrue(land_parent["landed"])
                self.assertEqual(land_parent["strategy"], "squash")
                new_main = _git(repo, "rev-parse", "main")

                # Rebase `child --onto main P0` (real merge conflict!), resolve `shared.py`, continue
                c1 = rebase_with_conflict(wt_child, ["--onto", "main", p0], "shared.py", 'LINE = "parent_v2 + child_v1"\n')
                self.assertNotEqual(c1, c0)
                self.assertEqual(_git(repo, "merge-base", "main", "task/child"), new_main)

                # `branch arbitrate child` BEFORE verifying `C1` must refuse!
                with self.assertRaisesRegex(KeelError, "has not been verified yet; run `keel branch verify child`"):
                    _run_cli(["branch", "arbitrate", "child", "--why", "Child slice.", *project])

                if use_inline_arbitrate:
                    with (
                        mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                        mock.patch.object(AgyExecutor, "execute", make_arb("replan", "R15_INLINE_DIR", "R15_INLINE_DEAD")),
                    ):
                        v_inline = _run_cli([
                            "branch", "verify", "child", "--arbitrate",
                            "--why", "Child slice.", "--check", "true", *project,
                        ])
                    self.assertEqual((v_inline["outcome"], v_inline["level"], v_inline["head_sha"]), ("request_changes", "L4", c1))
                    self.assertEqual(v_inline["arbitration"]["action"], "replan")
                    self.assertEqual(v_inline["arbitration"]["head_sha"], c1)
                    self.assertEqual(v_inline["arbitration"]["merge_base_sha"], new_main)
                else:
                    with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                        v_ref = _run_cli([
                            "branch", "verify", "child", "--why", "Child slice.", "--check", "true", *project,
                        ])
                        self.assertEqual((v_ref["outcome"], v_ref["level"], v_ref["head_sha"]), ("request_changes", "L4", c1))
                        with self.assertRaisesRegex(KeelError, "convergence cap"):
                            _run_cli(["branch", "verify", "child", "--why", "Child slice.", *project])
                    with mock.patch.object(AgyExecutor, "execute", make_arb("replan", "R15_TWOSTEP_DIR", "R15_TWOSTEP_DEAD")):
                        arb_c1 = _run_cli(["branch", "arbitrate", "child", "--why", "Child slice.", *project])
                    self.assertEqual((arb_c1["action"], arb_c1["head_sha"]), ("replan", c1))
                    self.assertEqual(arb_c1["arbitration"]["merge_base_sha"], new_main)

        # 2) Parent resolves a rebase conflict before re-verifying & ff-landing (proving `old_mb` via `prior_head_shas`),
        #    and child ALSO resolves a conflict rebasing onto `main`!
        with self.subTest(case="ff_parent_conflict_and_child_conflict"), tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="parent")
            project = ["--project", str(repo)]

            wt_parent = Path(_run_cli(["worktree", "create", "parent", *project])["path"])
            (wt_parent / "shared.py").write_text('LINE = "parent_v1"\n', encoding="utf-8")
            _git(wt_parent, "add", "shared.py")
            _git(wt_parent, "commit", "-m", "feat: parent v1")
            p0 = _git(wt_parent, "rev-parse", "HEAD")

            # Verify parent at P0 so P0 is recorded in parent's `rounds` -> `prior_head_shas` on land
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                _run_cli(["branch", "verify", "parent", "--why", "Parent v1.", "--check", "true", *project])

            wt_child = Path(_run_cli(["worktree", "create", "child", "--base", "task/parent", *project])["path"])
            (wt_child / "shared.py").write_text('LINE = "child_v1"\n', encoding="utf-8")
            _git(wt_child, "add", "shared.py")
            _git(wt_child, "commit", "-m", "feat: child v1")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "child", "--base", "task/parent",
                    "--level", "L4", "--why", "Child slice.", "--check", "true", *project,
                ])
                _run_cli([
                    "branch", "verify", "child", "--base", "task/parent",
                    "--why", "Child slice.", *project,
                ])

            # Advance `main` on the same file, forcing parent itself to resolve a rebase conflict before ff-landing!
            (repo / "shared.py").write_text('LINE = "main_v1"\n', encoding="utf-8")
            _git(repo, "add", "shared.py")
            _git(repo, "commit", "-m", "chore: main shared.py conflict")

            p1 = rebase_with_conflict(wt_parent, ["main"], "shared.py", 'LINE = "main_v1 + parent_v1"\n')
            self.assertNotEqual(p1, p0)
            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                _run_cli(["branch", "verify", "parent", "--why", "Parent conflict resolved.", "--check", "true", *project])
            land_ff = _run_cli(["branch", "land", "parent", "--ff-only", "--execute", *project])
            self.assertTrue(land_ff["landed"])
            self.assertEqual(land_ff["strategy"], "ff-only")
            new_main = _git(repo, "rev-parse", "main")

            # Child rebases `--onto main P0` (conflicts!), resolves, gets ONE fresh L4 review, then arbitrates cleanly
            c1 = rebase_with_conflict(wt_child, ["--onto", "main", p0], "shared.py", 'LINE = "main_v1 + parent_v1 + child_v1"\n')
            with self.assertRaisesRegex(KeelError, "has not been verified yet; run `keel branch verify child`"):
                _run_cli(["branch", "arbitrate", "child", "--why", "Child slice.", *project])

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                v_ref = _run_cli(["branch", "verify", "child", "--why", "Child slice.", "--check", "true", *project])
                self.assertEqual((v_ref["outcome"], v_ref["level"], v_ref["head_sha"]), ("request_changes", "L4", c1))
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "child", "--why", "Child slice.", *project])
            with mock.patch.object(AgyExecutor, "execute", make_arb("replan", "R15_FF_DIR", "R15_FF_DEAD")):
                arb_ff = _run_cli(["branch", "arbitrate", "child", "--why", "Child slice.", *project])
            self.assertEqual((arb_ff["action"], arb_ff["head_sha"]), ("replan", c1))
            self.assertEqual(arb_ff["arbitration"]["merge_base_sha"], new_main)

        # 3) Negative controls: dropped parent and `--discard`ed parent with conflict-resolved child rebases
        #    MUST still fail at the convergence cap!
        for neg_case, do_discard in (
            ("dropped_parent_conflict", False),
            ("discarded_parent_conflict", True),
        ):
            with self.subTest(case=neg_case), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / "repo"
                _init_repo_with_task(repo, slug="parent")
                project = ["--project", str(repo)]

                wt_parent = Path(_run_cli(["worktree", "create", "parent", *project])["path"])
                (wt_parent / "shared.py").write_text('LINE = "parent_v1"\n', encoding="utf-8")
                _git(wt_parent, "add", "shared.py")
                _git(wt_parent, "commit", "-m", "feat: parent v1")
                p0 = _git(wt_parent, "rev-parse", "HEAD")

                if do_discard:
                    with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                        _run_cli(["branch", "verify", "parent", "--why", "Parent v1.", "--check", "true", *project])

                wt_child = Path(_run_cli(["worktree", "create", "child", "--base", "task/parent", *project])["path"])
                (wt_child / "shared.py").write_text('LINE = "child_v1"\n', encoding="utf-8")
                _git(wt_child, "add", "shared.py")
                _git(wt_child, "commit", "-m", "feat: child v1")

                with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                    _run_cli([
                        "branch", "verify", "child", "--base", "task/parent",
                        "--level", "L4", "--why", "Child slice.", "--check", "true", *project,
                    ])
                    _run_cli([
                        "branch", "verify", "child", "--base", "task/parent",
                        "--why", "Child slice.", *project,
                    ])

                if do_discard:
                    disc = _run_cli(["branch", "land", "parent", "--discard", "--reason", "Abandoned", *project])
                    self.assertTrue(disc["discarded"])

                (repo / "shared.py").write_text(f'LINE = "main_{neg_case}"\n', encoding="utf-8")
                _git(repo, "add", "shared.py")
                _git(repo, "commit", "-m", f"chore: main conflicting change ({neg_case})")

                rebase_with_conflict(wt_child, ["--onto", "main", p0], "shared.py", f'LINE = "main_{neg_case} + child_v1"\n')
                with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                    with self.assertRaisesRegex(KeelError, "convergence cap"):
                        _run_cli(["branch", "verify", "child", "--why", "Child slice.", *project])

    def test_round15_non_default_base_supersede_layout_preconditions(self) -> None:
        def make_arb(action: str, directive: str, dead_end: str) -> Any:
            def _arb(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": action,
                            "summary": f"Non-default layout arbiter chose {action}.",
                            "directives": [directive],
                            "avoid_dead_ends": [dead_end],
                            "waived_findings": [],
                            "commands_run": ["git status"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}
            return _arb

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="release-layout")
            project = ["--project", str(repo)]

            # Create non-default `release` branch and an `unrelated` branch
            _git(repo, "checkout", "-b", "release")
            (repo / "release.py").write_text("RELEASE = 1\n", encoding="utf-8")
            _git(repo, "add", "release.py")
            _git(repo, "commit", "-m", "chore: release branch commit")
            _git(repo, "branch", "unrelated", "main")

            # Create `task/child` based on `release` and reach a valid `pivot` decision
            wt_child = Path(_run_cli(["worktree", "create", "child", "--base", "release", *project])["path"])
            (wt_child / "child.py").write_text("CHILD = 1\n", encoding="utf-8")
            _git(wt_child, "add", "child.py")
            _git(wt_child, "commit", "-m", "feat: child on release")
            child_head = _git(wt_child, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "child", "--base", "release",
                    "--why", "Child on release.", "--check", "true", *project,
                ])
            _run_cli(["branch", "dispute", "child", "--base", "release", "--reason", "Need pivot", *project])
            with mock.patch.object(AgyExecutor, "execute", make_arb("pivot", "REL_DIR", "REL_DEAD")):
                _run_cli(["branch", "arbitrate", "child", "--base", "release", "--why", "Child on release.", *project])

            # 1) Primary on `unrelated`, `release` checked out nowhere -> fails upfront without `--archive-task`!
            _git(repo, "checkout", "unrelated")
            with self.assertRaisesRegex(
                KeelError,
                r"Primary checkout .* must be on 'release' or a default branch \(main, master\), currently 'unrelated'",
            ):
                _run_cli(["branch", "supersede", "child", "--base", "release", *project])
            self.assertEqual(_git(repo, "for-each-ref", "refs/keel/superseded/"), "")
            self.assertEqual(_git(repo, "rev-parse", "refs/heads/task/child"), child_head)
            self.assertTrue(wt_child.exists())

            # 2) Primary on `main`, `release` still checked out nowhere -> fails upfront without `--archive-task`!
            _git(repo, "checkout", "main")
            with self.assertRaisesRegex(
                KeelError,
                r"Base branch 'release' must be checked out in the primary checkout or a linked worktree before superseding",
            ):
                _run_cli(["branch", "supersede", "child", "--base", "release", *project])
            self.assertEqual(_git(repo, "for-each-ref", "refs/keel/superseded/"), "")
            self.assertEqual(_git(repo, "rev-parse", "refs/heads/task/child"), child_head)
            self.assertTrue(wt_child.exists())

            # 3) Primary on `release` (clean) -> succeeds cleanly without `--archive-task`!
            _git(repo, "checkout", "release")
            sup = _run_cli(["branch", "supersede", "child", "--base", "release", *project])
            self.assertTrue(sup["superseded"])
            self.assertEqual((sup["action"], sup["recovery_count"]), ("pivot", 1))
            self.assertEqual(_git(repo, "rev-parse", sup["preserved_ref"]), child_head)
            self.assertFalse(wt_child.exists())

    def test_round16_scoped_approve_closes_prior_convergence_cycle(self) -> None:
        arb_calls: list[str] = []

        def make_arb(action: str, directive: str, dead_end: str) -> Any:
            def _arb(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                arb_calls.append(req.run_id)
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": action,
                            "summary": f"Round 16 arbiter chose {action}.",
                            "directives": [directive],
                            "avoid_dead_ends": [dead_end],
                            "waived_findings": [],
                            "commands_run": ["git status"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}
            return _arb

        # 1) Two L4 rejections -> authorized `--force-round` approve -> follow-up commit on same branch:
        #    `branch status` does not report convergence cap (`request_changes_rounds == 0`),
        #    `branch verify` runs cleanly WITHOUT `--force-round` even on `request_changes`,
        #    and Cycle 2 gets its full 2 L4 rounds before capping.
        with self.subTest(case="forced_approve_closes_prior_cap"), tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="cycle-reset")
            project = ["--project", str(repo)]

            wt = Path(_run_cli(["worktree", "create", "cycle-reset", *project])["path"])
            (wt / "calc.py").write_text("VAL = 1\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: cycle 1 commit")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                r1 = _run_cli(["branch", "verify", "cycle-reset", "--level", "L4", "--check", "true", *project])
                r2 = _run_cli(["branch", "verify", "cycle-reset", "--level", "L4", *project])
                self.assertEqual((r1["request_changes_rounds"], r2["request_changes_rounds"]), (1, 2))
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "cycle-reset", "--level", "L4", *project])

            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                r3 = _run_cli(["branch", "verify", "cycle-reset", "--level", "L4", "--force-round", *project])
            self.assertEqual((r3["outcome"], r3["request_changes_rounds"]), ("approve", 0))
            st_approved = _run_cli(["branch", "status", "cycle-reset", *project])
            self.assertTrue(st_approved["ready_to_land"], st_approved["blockers"])
            self.assertEqual(st_approved["request_changes_rounds"], 0)

            # Follow-up commit on the same branch starts Cycle 2 fresh
            (wt / "calc.py").write_text("VAL = 2\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: cycle 2 follow-up commit")

            st_followup = _run_cli(["branch", "status", "cycle-reset", *project])
            self.assertFalse(st_followup["ready_to_land"])
            self.assertEqual(st_followup["request_changes_rounds"], 0)
            self.assertFalse(any("convergence cap" in b.lower() for b in st_followup["blockers"]))

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                c2_r1 = _run_cli(["branch", "verify", "cycle-reset", "--level", "L4", "--check", "true", *project])
                self.assertEqual((c2_r1["outcome"], c2_r1["level"], c2_r1["request_changes_rounds"]), ("request_changes", "L4", 1))
                c2_r2 = _run_cli(["branch", "verify", "cycle-reset", "--level", "L4", *project])
                self.assertEqual((c2_r2["outcome"], c2_r2["level"], c2_r2["request_changes_rounds"]), ("request_changes", "L4", 2))
                with self.assertRaisesRegex(KeelError, "convergence cap"):
                    _run_cli(["branch", "verify", "cycle-reset", "--level", "L4", *project])

        # 2) Cycle 1 L4 rejection -> ordinary approve -> follow-up commit -> Cycle 2 Round 1 L4 `request_changes`
        #    with NO dispute: `verify --arbitrate` does NOT trigger premature arbitration, standalone `arbitrate`
        #    refuses, and Cycle 2 Round 2 L4 `request_changes` reaches 2/2 and becomes eligible for arbitration.
        with self.subTest(case="ordinary_approve_closes_prior_rc_for_arbitration"), tempfile.TemporaryDirectory() as tmp:
            arb_calls.clear()
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="ord-cycle")
            project = ["--project", str(repo)]

            wt = Path(_run_cli(["worktree", "create", "ord-cycle", *project])["path"])
            (wt / "calc.py").write_text("VAL = 10\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: cycle 1 initial")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                c1_r1 = _run_cli(["branch", "verify", "ord-cycle", "--level", "L4", "--check", "true", *project])
            self.assertEqual((c1_r1["outcome"], c1_r1["request_changes_rounds"]), ("request_changes", 1))

            with mock.patch.object(CodexExecutor, "execute", verifier("approve")):
                c1_r2 = _run_cli(["branch", "verify", "ord-cycle", "--level", "L4", *project])
            self.assertEqual((c1_r2["outcome"], c1_r2["request_changes_rounds"]), ("approve", 0))

            # Follow-up commit on same branch with no author dispute
            (wt / "calc.py").write_text("VAL = 20\n", encoding="utf-8")
            _git(wt, "commit", "-am", "feat: cycle 2 follow-up")

            with (
                mock.patch.object(CodexExecutor, "execute", verifier("request_changes")),
                mock.patch.object(AgyExecutor, "execute", make_arb("replan", "R16_DIR", "R16_DEAD")),
            ):
                c2_r1 = _run_cli([
                    "branch", "verify", "ord-cycle", "--level", "L4", "--arbitrate", "--check", "true", *project,
                ])
            self.assertEqual((c2_r1["outcome"], c2_r1["level"], c2_r1["request_changes_rounds"]), ("request_changes", "L4", 1))
            self.assertNotIn("arbitration", c2_r1)
            self.assertEqual(arb_calls, [])

            with self.assertRaisesRegex(
                KeelError,
                "neither convergence cap nor an author rebuttal/dispute is recorded in the active cycle",
            ):
                _run_cli(["branch", "arbitrate", "ord-cycle", *project])

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                c2_r2 = _run_cli(["branch", "verify", "ord-cycle", "--level", "L4", *project])
            self.assertEqual((c2_r2["outcome"], c2_r2["level"], c2_r2["request_changes_rounds"]), ("request_changes", "L4", 2))

            st_c2_cap = _run_cli(["branch", "status", "ord-cycle", *project])
            self.assertEqual(st_c2_cap["request_changes_rounds"], 2)
            self.assertTrue(any("convergence cap" in b.lower() for b in st_c2_cap["blockers"]))

            with mock.patch.object(AgyExecutor, "execute", make_arb("replan", "R16_DIR", "R16_DEAD")):
                arb_res = _run_cli(["branch", "arbitrate", "ord-cycle", *project])
            self.assertEqual(arb_res["action"], "replan")
            self.assertEqual(len(arb_calls), 1)

    def test_round16_custom_branch_supersedes_identifiers(self) -> None:
        from keel_lib.worktree import lineage_state_path

        def make_arb(action: str, directive: str, dead_end: str) -> Any:
            def _arb(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                req.output_path.write_text(
                    json.dumps(
                        {
                            "action": action,
                            "summary": f"Custom branch arbiter chose {action}.",
                            "directives": [directive],
                            "avoid_dead_ends": [dead_end],
                            "waived_findings": [],
                            "commands_run": ["git status"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-arb"}
            return _arb

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo_with_task(repo, slug="cpp-base")
            project = ["--project", str(repo)]

            # Non-`_SLUG_RE` branch under `task/` safely uses URL-encoded `custom+<encoded>.json`,
            # while standard `task/<slug>` keeps `<slug>.json`.
            self.assertTrue(lineage_state_path(repo / ".git", "task/c++20").name.startswith("custom+"))
            self.assertEqual(lineage_state_path(repo / ".git", "task/normal-slug").name, "normal-slug.json")

            wt_cpp = Path(_run_cli(["worktree", "create", "cpp20", "--branch", "feature/c++20", *project])["path"])
            (wt_cpp / "cpp.py").write_text("STD = 20\n", encoding="utf-8")
            _git(wt_cpp, "add", "cpp.py")
            _git(wt_cpp, "commit", "-m", "feat: c++20 prototype")
            cpp_head = _git(wt_cpp, "rev-parse", "HEAD")

            with mock.patch.object(CodexExecutor, "execute", verifier("request_changes")):
                _run_cli([
                    "branch", "verify", "feature/c++20", "--why", "Verify C++20 prototype.", "--check", "true", *project,
                ])
            _run_cli(["branch", "dispute", "feature/c++20", "--reason", "Need replan for ABI", *project])
            with mock.patch.object(AgyExecutor, "execute", make_arb("replan", "USE_STD_SPAN", "RAW_POINTER_ABI")):
                _run_cli(["branch", "arbitrate", "feature/c++20", "--why", "Verify C++20 prototype.", *project])

            sup = _run_cli(["branch", "supersede", "feature/c++20", *project])
            self.assertTrue(sup["superseded"])
            self.assertEqual((sup["branch"], sup["slug"]), ("feature/c++20", "c++20"))
            self.assertEqual(_git(repo, "rev-parse", sup["preserved_ref"]), cpp_head)
            self.assertFalse(wt_cpp.exists())
            self.assertTrue(Path(sup["lineage_path"]).name.startswith("custom+"))

            # All three `--supersedes` forms inherit lineage in both `worktree create` and `agent run`
            captured_briefs: list[str] = []

            def capture_writer(adapter: Any, req: Any, defaults: Any) -> dict[str, Any]:
                captured_briefs.append(req.brief_path.read_text(encoding="utf-8"))
                return {"status": "completed", "executor": adapter.name, "level": req.level, "model": "test-agy"}

            for idx, sup_target in enumerate(("c++20", "feature/c++20", "refs/heads/feature/c++20"), start=1):
                with self.subTest(supersedes_target=sup_target):
                    wt_slug = f"succ-wt-{idx}"
                    wt_res = _run_cli(["worktree", "create", wt_slug, "--supersedes", sup_target, *project])
                    self.assertEqual(wt_res["recovery_count"], 1)
                    self.assertEqual(wt_res["superseded_from"]["branch"], "feature/c++20")
                    self.assertEqual(wt_res["superseded_from"]["slug"], "c++20")
                    wt_state = load_branch_state(repo / ".git", f"task/{wt_slug}")
                    self.assertEqual(wt_state["recovery_count"], 1)
                    self.assertEqual(wt_state["prior_directives"], ["USE_STD_SPAN"])
                    self.assertEqual(wt_state["avoid_dead_ends"], ["RAW_POINTER_ABI"])

                    ag_slug = f"succ-ag-{idx}"
                    with mock.patch.object(AgyExecutor, "execute", capture_writer):
                        _run_cli([
                            "agent", "run", *project, "--worktree", ag_slug, "--supersedes", sup_target,
                            "--executor", "agy", "--mode", "write", "--preset", "implement", "--level", "L2",
                            "--why", "Implement clean C++20 successor.", "--task", "Use std::span.",
                        ])
                    self.assertIn("### Prior Attempt Dead-Ends & Arbiter Directives", captured_briefs[-1])
                    self.assertIn("USE_STD_SPAN", captured_briefs[-1])
                    self.assertIn("RAW_POINTER_ABI", captured_briefs[-1])
                    ag_state = load_branch_state(repo / ".git", f"task/{ag_slug}")
                    self.assertEqual(ag_state["recovery_count"], 1)
                    self.assertEqual(ag_state["prior_directives"], ["USE_STD_SPAN"])
                    self.assertEqual(ag_state["avoid_dead_ends"], ["RAW_POINTER_ABI"])

            # New worktree slugs remain strict (`_SLUG_RE`)
            with self.assertRaisesRegex(KeelError, "Invalid worktree slug"):
                _run_cli(["worktree", "create", "c++20", *project])
            with self.assertRaisesRegex(KeelError, "Invalid worktree slug"):
                _run_cli([
                    "agent", "run", *project, "--worktree", "c++20",
                    "--executor", "agy", "--mode", "write", "--level", "L2",
                    "--why", "Bad slug.", "--task", "Should fail.",
                ])

            # Invalid `--supersedes` targets are still rejected
            for bad_target in ("../bad", "@", "foo..bar", "@{-1}", "-flag", "/bad", "bad/", "a//b", "a\\b", "HEAD"):
                with self.subTest(bad_supersedes=bad_target):
                    with self.assertRaisesRegex(KeelError, "Invalid --supersedes target"):
                        _run_cli(["worktree", "create", "bad-succ", f"--supersedes={bad_target}", *project])
                    with self.assertRaisesRegex(KeelError, "Invalid --supersedes target"):
                        _run_cli([
                            "agent", "run", *project, "--worktree", "bad-succ", f"--supersedes={bad_target}",
                            "--executor", "agy", "--mode", "write", "--level", "L2",
                            "--why", "Bad supersedes.", "--task", "Should fail.",
                        ])


if __name__ == "__main__":
    unittest.main()
