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
            #    L2 reject -> L3 approve -> new commit -> L2 reject -> L3 reject -> L4 reject (only 1 L4 rejection!).
            #    Both `branch status` and `branch verify` report convergence cap (`1/2` at L4, `4/4` total) and block 5th verify!
            wt_tot = Path(_run_cli(["worktree", "create", "total-cap-only", *project])["path"])
            (wt_tot / "calc.py").write_text("TOT = 1\n", encoding="utf-8")
            _git(wt_tot, "commit", "-am", "feat: total cap commit 1")
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
                _run_cli([*run_tot, "--level", "L2"])
                self.assertEqual(_run_cli(verify_tot)["level"], "L2")  # rc #1 (L2)
                tot_outcome = "approve"
                _run_cli([*run_tot, "--level", "L3"])
                self.assertEqual(_run_cli(verify_tot)["outcome"], "approve")  # resets active floor
                (wt_tot / "calc.py").write_text("TOT = 2\n", encoding="utf-8")
                _git(wt_tot, "commit", "-am", "feat: total cap commit 2")
                tot_outcome = "request_changes"
                _run_cli([*run_tot, "--level", "L2"])
                self.assertEqual(_run_cli(verify_tot)["level"], "L2")  # rc #2 (L2)
                _run_cli([*run_tot, "--level", "L3"])
                self.assertEqual(_run_cli(verify_tot)["level"], "L3")  # rc #3 (L3)
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


if __name__ == "__main__":
    unittest.main()
