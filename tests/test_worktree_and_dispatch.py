from __future__ import annotations

import contextlib
from dataclasses import replace
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from keel_lib import KeelError, SCHEMAS_DIR, load_defaults  # noqa: E402
from keel_lib.context import assemble_brief, save_run_record  # noqa: E402
from keel_lib.executors import EXECUTORS, ExecutorAdapter, RunRequest  # noqa: E402
from keel_lib.executors import base
from keel_lib.executors.agy import AgyExecutor  # noqa: E402
from keel_lib.executors.codex import CodexExecutor  # noqa: E402
from keel_lib.worktree import (  # noqa: E402
    cleanup_worktree,
    create_disposable_clone,
    create_worktree,
    git_dirs,
    save_branch_state,
)


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    (path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


class DummyThirdSideExecutor(ExecutorAdapter):
    name = "dummy-sdk"
    native_agent_docs = ()
    loads_plugin_spine = False
    inject_schema_in_brief = True

    def execute(self, req: RunRequest, defaults: dict[str, Any]) -> dict[str, Any]:
        return {"status": "completed", "executor": self.name, "output_path": str(req.output_path)}

    def resume(
        self,
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
        return {"status": "completed", "executor": self.name}


class WorktreeAndDispatchTests(unittest.TestCase):
    def test_pure_git_worktree_remote_stripping_and_ancestry_or_squash_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "adopted"
            repo.mkdir()
            _init_repo(repo)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.invalid/repo.git"], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "th", "ssh://th/repo.git"], check=True)

            (repo / ".gitignore").write_text("/AGENTS.md\n__pycache__/\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "gitignore"], check=True, capture_output=True)
            base_sha = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip()
            (repo / "AGENTS.md").write_text("## Core need\nKeep data lossless.\n", encoding="utf-8")

            # Default `create_worktree` uses local `main` with `fetch=False` (never contacts unreachable origin!)
            wt_info = create_worktree(repo, "feat-1")
            wt_path = Path(wt_info["path"])
            self.assertEqual(wt_info["base"], "main")
            self.assertEqual(wt_info["synced_docs"], ["AGENTS.md"])
            self.assertTrue((wt_path / "AGENTS.md").is_file())
            main_porcelain = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain", "-uall"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(main_porcelain, "")

            # Disposable verification clone syncs gitignored AGENTS.md AND strips ALL git remotes!
            scratch = Path(tmp) / "keel-verify-feat-1"
            clone_dir, _ = create_disposable_clone(repo, base_sha, scratch)
            self.assertTrue((clone_dir / "AGENTS.md").is_file())
            remotes_in_clone = subprocess.run(
                ["git", "-C", str(clone_dir), "remote"], check=True, capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(remotes_in_clone, "")

            # Add a commit on `task/feat-1`: unintegrated cleanup without `--discard` MUST be refused!
            (wt_path / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(wt_path), "add", "app.py"], check=True)
            subprocess.run(["git", "-C", str(wt_path), "commit", "-m", "feat"], check=True, capture_output=True)
            feat_sha = subprocess.run(
                ["git", "-C", str(wt_path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip()

            with self.assertRaises(KeelError):
                cleanup_worktree(repo, "feat-1", caller_cwd=tmp)
            self.assertTrue((wt_path / "AGENTS.md").is_file())

            # Modified gitignored AGENTS.md blocks cleanup unless `--discard`
            (wt_path / "AGENTS.md").write_text("## Core need\nEdited locally!\n", encoding="utf-8")
            common_dir, _ = git_dirs(repo)
            save_branch_state(
                common_dir,
                "task/feat-1",
                {"branch": "task/feat-1", "landed_head_sha": feat_sha, "landed_sha": base_sha},
            )
            with self.assertRaises(KeelError):
                cleanup_worktree(repo, "feat-1", caller_cwd=tmp)

            # Restore gitignored AGENTS.md + add __pycache__: recorded squash-land SHA allows clean teardown!
            (wt_path / "AGENTS.md").write_text("## Core need\nKeep data lossless.\n", encoding="utf-8")
            cache_dir = wt_path / "__pycache__"
            cache_dir.mkdir()
            (cache_dir / "mod.cpython-312.pyc").write_bytes(b"123")
            res = cleanup_worktree(repo, "feat-1", caller_cwd=tmp)
            self.assertFalse(wt_path.exists())
            self.assertEqual(res["deleted_branch"], "task/feat-1")

    def test_level_routing_uses_only_configured_mappings(self) -> None:
        defaults = load_defaults()
        levels = ["L1", "L2", "L3", "L4"]
        self.assertEqual(defaults["levels"], levels)
        self.assertEqual(defaults["routing"], {"general": "agy", "implement": "agy", "verify": "codex"})
        for executor, require_effort in (("codex", True), ("agy", False)):
            cfg_levels = defaults["executors"][executor]["levels"]
            for level in levels:
                with self.subTest(executor=executor, level=level):
                    entry = cfg_levels[level]
                    expected_effort = entry["effort"] if require_effort else entry.get("effort")
                    self.assertEqual(
                        base.resolve_executor_level_config(executor, level.lower(), defaults),
                        (level, entry["model"], expected_effort),
                    )

        synthetic = {
            "levels": levels,
            "executors": {
                "codex": {
                    "levels": {
                        lvl: {"model": f"synth-codex-{lvl.lower()}", "effort": f"eff-{lvl.lower()}"}
                        for lvl in levels
                    }
                },
                "agy": {
                    "levels": {lvl: {"model": f"synth-agy-{lvl.lower()}"} for lvl in levels}
                },
            },
        }
        for lvl in levels:
            self.assertEqual(
                base.resolve_executor_level_config("codex", lvl.lower(), synthetic),
                (lvl, f"synth-codex-{lvl.lower()}", f"eff-{lvl.lower()}"),
            )
            self.assertEqual(
                base.resolve_executor_level_config("agy", lvl.lower(), synthetic),
                (lvl, f"synth-agy-{lvl.lower()}", None),
            )

        cfg = defaults["executors"]["codex"]["levels"]
        cfg["L1"] = {"model": "future-model", "effort": "future-effort"}
        self.assertEqual(base.resolve_executor_level_config("codex", "L1", defaults),
                         ("L1", "future-model", "future-effort"))
        for config in ({}, {"executors": {"codex": {"presets": {"general": cfg["L1"]}}}},
                       {"executors": {"codex": {"levels": {"L1": {"model": "alone"}}}}}):
            with self.assertRaises(base.KeelConfigError):
                base.resolve_executor_level_config("codex", "L1", config)
        self.assertEqual([base.escalate_level(level) for level in levels], ["L2", "L3", "L4", "L4"])
        self.assertEqual(base.max_level("L4", "L1"), "L4")

    def test_atomic_run_record_replaces_complete_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            path = save_run_record(run_dir, {"version": 1})
            with path.open() as original:
                save_run_record(run_dir, {"version": 2})
                self.assertEqual(json.load(original), {"version": 1})
            self.assertEqual(json.loads(path.read_text()), {"version": 2})
            with self.assertRaises(TypeError):
                save_run_record(run_dir, {"bad": object()})
            self.assertEqual(json.loads(path.read_text()), {"version": 2})
            self.assertEqual(list(run_dir.iterdir()), [path])

    def test_cli_validates_levels_and_escalates_codex_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            codex = repo / "codex"
            codex.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "with open('codex-calls', 'a') as calls: print(sys.argv[1], file=calls)\n"
                "if sys.argv[1] == 'mcp': sys.exit(0)\n"
                "pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text(json.dumps(sys.argv[1:]))\n"
                "print(json.dumps({'thread_id': '12345678-1234-1234-1234-123456789abc'}))\n"
                "sys.exit(7 if 'fail' in sys.argv else 0)\n"
            )
            codex.chmod(0o755)

            def cli(*args: str, status: int = 0) -> Any:
                proc = subprocess.run(
                    [str(ROOT / "scripts" / "keel"), "agent", *args, "--project", str(repo)],
                    cwd=repo, capture_output=True, text=True, timeout=10,
                    env={**os.environ, "PATH": str(repo) + os.pathsep + os.environ["PATH"]},
                )
                self.assertEqual(proc.returncode, status, proc.stderr)
                if status:
                    return proc.stderr
                return json.loads(proc.stdout)

            run = ["run", "--why", "Check routing.", "--task", "Inspect.", "--run-id", "test"]
            for args in ([], ["--executor", "agy"], ["--level", "L5"], ["--preset", "verify", "--level", "L1"]):
                self.assertIn("level", cli(*run, *args, status=2).lower())
            self.assertFalse((repo / ".agents/runs/test").exists())

            initial = cli(*run, "--executor", "codex", "--level", "l2", "--model", "custom", "--effort", "low")
            resume = ["resume", "test", "--message", "Continue."]
            session_id = initial["session_id"]

            def assert_settings(record: dict[str, Any], level: str, model: str, effort: str) -> None:
                self.assertEqual((record["level"], record["model"], record["effort"]), (level, model, effort))
                argv = json.loads(Path(record["output_path"]).read_text())
                self.assertEqual(argv[argv.index("-m") + 1], model)
                self.assertIn(f'model_reasoning_effort="{effort}"', argv)
                if record is not initial:
                    self.assertEqual(argv[argv.index("resume") + 1], session_id)

            assert_settings(initial, "L2", "custom", "low")
            assert_settings(cli(*resume), "L2", "custom", "low")
            l4_cfg = load_defaults()["executors"]["codex"]["levels"]["L4"]
            assert_settings(cli(*resume, "--level", "l4"), "L4", l4_cfg["model"], l4_cfg["effort"])
            assert_settings(cli(*resume, "--effort", "medium", "--model", "another"), "L4", "another", "medium")

            record_path = repo / ".agents/runs/test/run.json"
            legacy = json.loads(record_path.read_text())
            del legacy["level"]
            legacy.pop("ephemeral", None)
            save_run_record(record_path.parent, legacy)
            resumed = cli(*resume)
            self.assertEqual((resumed["model"], resumed["effort"]), ("another", "medium"))

            self.assertIn("code 7", cli(*run, "--executor", "codex", "--level", "L3",
                                        "--model", "fail", status=2))
            failed = json.loads(record_path.read_text())
            self.assertEqual((failed["level"], failed["model"], failed["status"]), ("L3", "fail", "failed"))
            recovered = cli(*resume, "--level", "L4")
            self.assertEqual(recovered["session_id"], failed["session_id"])
            with self.subTest("runtime resume failure preserves the completed record"):
                completed = record_path.read_bytes()
                self.assertEqual(recovered["status"], "completed")
                self.assertIn("code 7", cli(*resume, "--level", "L1", "--model", "fail", status=2))
                self.assertEqual(record_path.read_bytes(), completed)
                self.assertIs(recovered["ephemeral"], False)

            verify = cli(*run, "--preset", "verify", "--level", "L2")
            self.assertIn("--ephemeral", json.loads(Path(verify["output_path"]).read_text()))
            self.assertIn("level", cli(*resume, "--level", "L1", status=2))
            self.assertEqual(json.loads(record_path.read_text()), verify)
            with self.subTest("ephemeral resume is rejected before launching Codex"):
                completed = record_path.read_bytes()
                calls = (repo / "codex-calls").read_bytes()
                followups = set(record_path.parent.glob("followup-*.md"))
                self.assertIn("ephemeral", cli(*resume, status=2).lower())
                self.assertIs(json.loads(completed)["ephemeral"], True)
                self.assertEqual((repo / "codex-calls").read_bytes(), calls)
                self.assertEqual(record_path.read_bytes(), completed)
                self.assertEqual(set(record_path.parent.glob("followup-*.md")), followups)

            host = cli(*run, "--preset", "implement", "--level", "l1")
            self.assertEqual((host["executor"], host["status"]), ("agy", "awaiting_host_invoke"))
            original = record_path.read_bytes()
            for flag, value in (("--level", "L3"), ("--model", "pro"), ("--effort", "high")):
                with self.subTest(flag=flag):
                    error = cli(*resume, flag, value, status=2)
                    self.assertIn("Antigravity", error)
                    self.assertIn(flag, error)
                    self.assertEqual(record_path.read_bytes(), original)
            continued = cli(*resume)
            self.assertEqual((continued["level"], continued["status"]), ("L1", "awaiting_host_send_message"))

    def test_agy_execute_preserves_host_descriptor_compatibility(self) -> None:
        defaults = load_defaults()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            req = RunRequest(
                run_id="host", run_dir=path, brief_path=path / "brief.md", workdir=path,
                project_root=None, mode="read", preset="general", model=None, effort=None,
                schema_path=None, output_path=path / "output.md", level="l2",
            )
            for preset, scratch, level in (
                ("general", False, None),
                ("general", False, "L2"),
                ("verify", True, None),
            ):
                with self.subTest(preset=preset, scratch=scratch, level=level):
                    descriptor = AgyExecutor().execute(
                        replace(req, preset=preset, scratch_verify=scratch, level=level), defaults
                    )
                    self.assertEqual(descriptor["status"], "awaiting_host_invoke")
                    self.assertNotIn("level", descriptor)

    def test_internal_codex_verify_defaults_to_l3_and_enforces_floor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="keel-verify-") as tmp:
            path = Path(tmp)
            req = RunRequest(
                run_id="verify", run_dir=path, brief_path=path / "brief.md", workdir=path,
                project_root=None, mode="read", preset="verify", model=None, effort=None,
                schema_path=SCHEMAS_DIR / "verdict.schema.json", output_path=path / "verdict.json",
                scratch_verify=True,
            )
            req.brief_path.write_text("Verify.")
            with patch("keel_lib.executors.codex.run_process_group", return_value=(0, "")) as launch:
                for preset, mode, scratch in (
                    ("general", "read", False),
                    ("implement", "write", False),
                    ("verify", "read", False),
                    ("general", "read", True),
                ):
                    with self.subTest(preset=preset, scratch=scratch):
                        missing_level = replace(req, preset=preset, mode=mode, scratch_verify=scratch)
                        with self.assertRaisesRegex(base.KeelConfigError, "--level"):
                            CodexExecutor().execute(missing_level, load_defaults())
                self.assertEqual(launch.call_count, 0)
                shipped = load_defaults()
                l3_cfg = shipped["executors"]["codex"]["levels"]["L3"]
                result = CodexExecutor().execute(req, shipped)
                self.assertEqual(launch.call_count, 1)
                self.assertEqual((result["level"], result["model"], result["effort"]),
                                 ("L3", l3_cfg["model"], l3_cfg["effort"]))
                legacy_verify = {k: v for k, v in result.items() if k != "ephemeral"}
                legacy_verify.update({"mode": "read", "preset": "verify"})
                for rec in (result, legacy_verify):
                    with self.assertRaisesRegex(base.KeelConfigError, "(?i)ephemeral"):
                        CodexExecutor().resume(path, rec, path / "followup.md", req.output_path, shipped)
                req.level = "L1"
                with self.assertRaises(base.KeelConfigError):
                    CodexExecutor().execute(req, load_defaults())

    def test_codex_read_only_vs_disposable_sandbox(self) -> None:
        codex = CodexExecutor()
        defaults = load_defaults()

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            head_sha = _init_repo(repo)
            wt_info = create_worktree(repo, "codex-lane")
            wt_path = Path(wt_info["path"])
            out_file = Path(tmp) / "out.json"
            schema_file = SCHEMAS_DIR / "verdict.schema.json"

            # 1. `mode="read"` outside disposable verify clone -> `-s read-only` and NO `--add-dir` writable grants!
            argv_ro = codex.build_exec_argv(
                workdir=repo,
                mode="read",
                model="gpt-6-astra",
                effort="max",
                output_path=out_file,
                defaults=defaults,
            )
            s_idx = argv_ro.index("-s")
            self.assertEqual(argv_ro[s_idx + 1], "read-only")
            self.assertNotIn("--add-dir", argv_ro)
            self.assertIn('agents.default_subagent_model="gpt-6-astra"', argv_ro)
            self.assertIn('agents.default_subagent_reasoning_effort="max"', argv_ro)

            # 1b. Explicit `subagent_default` overrides (`gpt-6.1-terra` @ `high`) apply to child subagents cleanly!
            custom_sub_defaults = {
                "executors": {
                    "codex": {
                        "subagent_default": {"model": "gpt-6.1-terra", "effort": "high"},
                    }
                }
            }
            argv_custom_sub = codex.build_exec_argv(
                workdir=repo,
                mode="read",
                model="gpt-6-astra",
                effort="max",
                output_path=out_file,
                defaults=custom_sub_defaults,
            )
            self.assertEqual(argv_custom_sub[argv_custom_sub.index("-m") + 1], "gpt-6-astra")
            self.assertIn('model_reasoning_effort="max"', argv_custom_sub)
            self.assertIn('agents.default_subagent_model="gpt-6.1-terra"', argv_custom_sub)
            self.assertIn('agents.default_subagent_reasoning_effort="high"', argv_custom_sub)

            # 2. `mode="read"` inside disposable `/tmp/keel-verify-*` clone (`allow_disposable_write=True`)
            # -> `-s workspace-write` scoped ONLY to `/tmp` clone + its separated git dir (real repo never added!)
            verify_scratch = Path(tmp) / "keel-verify-codex"
            clone_dir, sep_git = create_disposable_clone(repo, head_sha, verify_scratch)
            argv_verify = codex.build_exec_argv(
                workdir=clone_dir,
                mode="read",
                model="gpt-6-astra",
                effort="max",
                output_path=out_file,
                defaults=defaults,
                schema_path=schema_file,
                extra_git_dirs=(sep_git,),
                allow_disposable_write=True,
            )
            self.assertEqual(argv_verify[argv_verify.index("-s") + 1], "workspace-write")
            add_dirs = [argv_verify[i + 1] for i, tok in enumerate(argv_verify) if tok == "--add-dir"]
            self.assertEqual(add_dirs, [str(sep_git)])
            self.assertNotIn(str(repo), " ".join(add_dirs))

            # 3. `mode="write"` in linked worktree -> two `--add-dir` entries and top-level flags precede `resume`
            argv_wt = codex.build_exec_argv(
                workdir=wt_path,
                mode="write",
                model="gpt-6-astra",
                effort="max",
                output_path=out_file,
                defaults=defaults,
                schema_path=schema_file,
                resume_session_id="12345678-1234-1234-1234-123456789abc",
            )
            add_dir_indices = [i for i, tok in enumerate(argv_wt) if tok == "--add-dir"]
            self.assertEqual(len(add_dir_indices), 2)
            resume_idx = argv_wt.index("resume")
            self.assertLess(argv_wt.index("-s"), resume_idx)
            self.assertLess(argv_wt.index("-C"), resume_idx)
            self.assertTrue(all(idx < resume_idx for idx in add_dir_indices))

    def test_three_context_registration_leak_fixes_and_third_executor(self) -> None:
        agy = AgyExecutor()
        codex = CodexExecutor()
        self.assertTrue(agy.loads_plugin_spine)
        self.assertFalse(codex.loads_plugin_spine)

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "proj"
            sibling = Path(tmp) / "sibling"
            disposable = Path(tmp) / "keel-verify-123" / "checkout"
            repo.mkdir()
            sibling.mkdir()
            disposable.mkdir(parents=True)
            _init_repo(repo)
            (repo / "AGENTS.md").write_text("## Core need\nZero data corruption.\n", encoding="utf-8")
            (disposable / "AGENTS.md").write_text("## Core need\nZero data corruption.\n", encoding="utf-8")

            # 1. AGY inside same workspace with identical AGENTS.md bytes -> neither spine nor AGENTS.md duplicated
            brief_agy_same = assemble_brief(
                why="Verify parser invariants.",
                task="Check edge cases.",
                preset="general",
                mode="read",
                workdir=repo,
                project_root=repo,
                native_agent_docs=agy.native_agent_docs,
                loads_plugin_spine=agy.loads_plugin_spine,
                caller_cwd=repo,
                inject_schema_in_brief=agy.inject_schema_in_brief,
            )
            self.assertNotIn("## Engineering Spine", brief_agy_same)
            self.assertNotIn("Zero data corruption.", brief_agy_same)
            self.assertNotIn("captured automatically via CLI output", brief_agy_same)

            # 2. Leak ① fix: AGY with `/tmp` disposable clone OR cross-dir `caller_cwd` OR branch-edited `AGENTS.md`
            # -> MUST inject `AGENTS.md` into `brief.md`!
            brief_agy_tmp = assemble_brief(
                why="Verify disposable clone.",
                task="Run tests.",
                preset="verify",
                mode="read",
                workdir=disposable,
                project_root=repo,
                native_agent_docs=agy.native_agent_docs,
                loads_plugin_spine=agy.loads_plugin_spine,
                caller_cwd=repo,
                inject_schema_in_brief=agy.inject_schema_in_brief,
            )
            self.assertIn("Zero data corruption.", brief_agy_tmp)

            brief_agy_cross = assemble_brief(
                why="Cross-project research.",
                task="Inspect repo.",
                preset="general",
                mode="read",
                workdir=repo,
                project_root=repo,
                native_agent_docs=agy.native_agent_docs,
                loads_plugin_spine=agy.loads_plugin_spine,
                caller_cwd=sibling,
                inject_schema_in_brief=agy.inject_schema_in_brief,
            )
            self.assertIn("Zero data corruption.", brief_agy_cross)

            wt = Path(create_worktree(repo, "edit-agents")["path"])
            (wt / "AGENTS.md").write_text("## Core need\nBranch-updated invariant.\n", encoding="utf-8")
            brief_agy_edited = assemble_brief(
                why="Verify updated AGENTS.md.",
                task="Check branch.",
                preset="implement",
                mode="write",
                workdir=wt,
                project_root=repo,
                native_agent_docs=agy.native_agent_docs,
                loads_plugin_spine=agy.loads_plugin_spine,
                caller_cwd=repo,
                inject_schema_in_brief=agy.inject_schema_in_brief,
            )
            self.assertIn("Branch-updated invariant.", brief_agy_edited)

            # 3. Leak ② & Leak ③ fixes: Codex receives universal sections 1–3 of `rules/AGENTS.md` + `-o` Output Contract!
            brief_codex = assemble_brief(
                why="Implement feature.",
                task="Edit app.py.",
                preset="implement",
                mode="write",
                workdir=repo,
                project_root=repo,
                native_agent_docs=codex.native_agent_docs,
                loads_plugin_spine=codex.loads_plugin_spine,
                caller_cwd=repo,
                inject_schema_in_brief=codex.inject_schema_in_brief,
            )
            self.assertIn("## Engineering Spine", brief_codex)
            self.assertIn("## 1. Global core-need alignment", brief_codex)
            self.assertIn("## 3. Protect human attention", brief_codex)
            self.assertNotIn("## 4. On-demand workflow skills", brief_codex)
            self.assertNotIn("Zero data corruption.", brief_codex)  # Codex natively loads `<workdir>/AGENTS.md`
            self.assertIn("captured automatically via CLI output", brief_codex)

            # 3b. Repo containing ONLY `GEMINI.md` (no `AGENTS.md`) injects `GEMINI.md` into `brief.md` for Codex & cross-dir AGY!
            (repo / "AGENTS.md").unlink()
            (repo / "GEMINI.md").write_text("## Core need\nGemini-only core invariant.\n", encoding="utf-8")
            brief_codex_gemini = assemble_brief(
                why="Ensure Codex receives GEMINI.md when AGENTS.md is absent.",
                task="Inspect app.py.",
                preset="general",
                mode="read",
                workdir=repo,
                project_root=repo,
                native_agent_docs=codex.native_agent_docs,
                loads_plugin_spine=codex.loads_plugin_spine,
                caller_cwd=repo,
                inject_schema_in_brief=codex.inject_schema_in_brief,
            )
            self.assertIn("## Project Context (`GEMINI.md`)", brief_codex_gemini)
            self.assertIn("Gemini-only core invariant.", brief_codex_gemini)

            brief_agy_gemini_cross = assemble_brief(
                why="Ensure cross-dir AGY receives GEMINI.md.",
                task="Inspect app.py.",
                preset="general",
                mode="read",
                workdir=repo,
                project_root=repo,
                native_agent_docs=agy.native_agent_docs,
                loads_plugin_spine=agy.loads_plugin_spine,
                caller_cwd=sibling,
                inject_schema_in_brief=agy.inject_schema_in_brief,
            )
            self.assertIn("Gemini-only core invariant.", brief_agy_gemini_cross)
            (repo / "GEMINI.md").unlink()
            (repo / "AGENTS.md").write_text("## Core need\nZero data corruption.\n", encoding="utf-8")

            # 4. Pluggable 3rd executor via `scripts/keel` CLI (plus `.agents/worktrees/keel-verify-read` stays scratch_verify=False)
            loader = importlib.machinery.SourceFileLoader("keel_cli", str(ROOT / "scripts" / "keel"))
            spec = importlib.util.spec_from_loader("keel_cli", loader)
            assert spec is not None and spec.loader is not None
            keel_cli = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(keel_cli)

            buf_wt = io.StringIO()
            with contextlib.redirect_stdout(buf_wt):
                rc_wt = keel_cli.main(
                    [
                        "agent",
                        "run",
                        "--level", "L2",
                        "--project",
                        str(repo),
                        "--executor",
                        "agy",
                        "--mode",
                        "read",
                        "--worktree",
                        "keel-verify-read",
                        "--why",
                        "Read-only audit in a worktree whose slug starts with keel-verify-.",
                        "--task",
                        "Inspect app.py without mutating.",
                    ]
                )
            self.assertEqual(rc_wt, 0)
            wt_payload = json.loads(buf_wt.getvalue())
            self.assertFalse(wt_payload["scratch_verify"])
            self.assertEqual(wt_payload["level"], "L2")
            codex_wt_argv = codex.build_exec_argv(
                workdir=Path(wt_payload["workdir"]),
                mode="read",
                model="gpt-6-astra",
                effort="max",
                output_path=Path(tmp) / "ro.md",
                defaults=load_defaults(),
                allow_disposable_write=wt_payload["scratch_verify"],
            )
            self.assertEqual(codex_wt_argv[codex_wt_argv.index("-s") + 1], "read-only")
            self.assertNotIn("--add-dir", codex_wt_argv)

            EXECUTORS["dummy-sdk"] = DummyThirdSideExecutor
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = keel_cli.main(
                        [
                            "agent",
                            "run",
                            "--level", "L3",
                            "--project",
                            str(repo),
                            "--executor",
                            "dummy-sdk",
                            "--preset",
                            "general",
                            "--why",
                            "Benchmark serializer against Core need.",
                            "--task",
                            "Compare latency.",
                        ]
                    )
                self.assertEqual(rc, 0)
                payload = json.loads(buf.getvalue())
                brief_sdk = Path(payload["brief_path"]).read_text(encoding="utf-8")
                self.assertIn("## Engineering Spine", brief_sdk)
                self.assertIn("Zero data corruption.", brief_sdk)
            finally:
                EXECUTORS.pop("dummy-sdk", None)

    def test_root_agents_md_exemplifies_five_section_under_100_lines_standard(self) -> None:
        agents_text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        lines = agents_text.splitlines()
        self.assertLess(len(lines), 100)
        headings = [ln.strip() for ln in lines if ln.startswith("## ")]
        self.assertEqual(
            headings,
            [
                "## Core need",
                "## Architecture & Boundaries",
                "## Commands",
                "## Gotchas",
                "## Where to look",
            ],
        )

    def test_install_plugin_per_entry_symlinks_exclude_root_agents_md(self) -> None:
        installer = ROOT / "scripts" / "install-plugin"
        expected_entries = {
            "plugin.json",
            "hooks.json",
            "assets",
            "config",
            "hooks",
            "rules",
            "scripts",
            "skills",
            "LICENSE",
            "README.md",
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plugin_dir = tmp_path / "plugins" / "keel"
            bin_dir = tmp_path / "bin"
            plugin_dir.parent.mkdir(parents=True)
            plugin_dir.symlink_to(ROOT)

            chk_before = subprocess.run(
                [str(installer), "--check", "--plugin-dir", str(plugin_dir), "--bin-dir", str(bin_dir)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(chk_before.returncode, 0)

            subprocess.run(
                [str(installer), "--plugin-dir", str(plugin_dir), "--bin-dir", str(bin_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertFalse(plugin_dir.is_symlink())
            self.assertEqual({p.name for p in plugin_dir.iterdir()}, expected_entries)
            for excluded in ("AGENTS.md", "docs", "tests", ".git", ".github", ".gitignore"):
                self.assertFalse((plugin_dir / excluded).exists())
                self.assertFalse((plugin_dir / excluded).is_symlink())

            chk_ok = subprocess.run(
                [str(installer), "--check", "--plugin-dir", str(plugin_dir), "--bin-dir", str(bin_dir)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(chk_ok.returncode, 0, chk_ok.stderr)

            cli_link = bin_dir / "keel"
            self.assertTrue(cli_link.is_symlink())
            self.assertEqual(cli_link.resolve(), (ROOT / "scripts" / "keel").resolve())

            # Stale symlinks (e.g., an accidental AGENTS.md symlink) fail --check and are pruned on re-install
            (plugin_dir / "AGENTS.md").symlink_to(ROOT / "AGENTS.md")
            chk_bad = subprocess.run(
                [str(installer), "--check", "--plugin-dir", str(plugin_dir), "--bin-dir", str(bin_dir)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(chk_bad.returncode, 0)

            subprocess.run(
                [str(installer), "--plugin-dir", str(plugin_dir), "--bin-dir", str(bin_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertFalse((plugin_dir / "AGENTS.md").exists())
            self.assertFalse((plugin_dir / "AGENTS.md").is_symlink())

            # Regular non-symlink file inside --plugin-dir fails --check and refuses destructive overwrite on install
            rogue_file = plugin_dir / "rogue.txt"
            rogue_file.write_text("do not delete", encoding="utf-8")
            chk_rogue = subprocess.run(
                [str(installer), "--check", "--plugin-dir", str(plugin_dir), "--bin-dir", str(bin_dir)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(chk_rogue.returncode, 0)
            reinstall_rogue = subprocess.run(
                [str(installer), "--plugin-dir", str(plugin_dir), "--bin-dir", str(bin_dir)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(reinstall_rogue.returncode, 2)
            self.assertTrue(rogue_file.is_file())
            rogue_file.unlink()

            # Default mode (no --plugin-dir / --bin-dir) strictly validates resolved symlink targets
            fake_home = tmp_path / "home"
            fake_home.mkdir()
            home_env = {**os.environ, "HOME": str(fake_home)}
            subprocess.run([str(installer)], env=home_env, check=True, capture_output=True, text=True)
            self.assertEqual(
                subprocess.run([str(installer), "--check"], env=home_env, capture_output=True, text=True).returncode,
                0,
            )

            default_plugin_dir = fake_home / ".gemini" / "config" / "plugins" / "keel"
            (default_plugin_dir / "hooks").unlink()
            (default_plugin_dir / "hooks").symlink_to(ROOT / "assets")
            self.assertNotEqual(
                subprocess.run([str(installer), "--check"], env=home_env, capture_output=True, text=True).returncode,
                0,
            )

            subprocess.run([str(installer)], env=home_env, check=True, capture_output=True, text=True)
            default_cli = fake_home / ".local" / "bin" / "keel"
            default_cli.unlink()
            default_cli.symlink_to(ROOT / "LICENSE")
            self.assertNotEqual(
                subprocess.run([str(installer), "--check"], env=home_env, capture_output=True, text=True).returncode,
                0,
            )

            # Overlapping --plugin-dir and --bin-dir rejected before modifying disk
            clean_plug = tmp_path / "overlap-plug"
            for bad_plug, bad_bin in (
                (clean_plug, clean_plug),
                (clean_plug, clean_plug / "bin"),
                (tmp_path / "shared" / "keel", tmp_path / "shared"),
            ):
                overlap = subprocess.run(
                    [str(installer), "--plugin-dir", str(bad_plug), "--bin-dir", str(bad_bin)],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(overlap.returncode, 2)
                self.assertFalse(bad_plug.exists())
                self.assertFalse(bad_bin.exists())

            # POSIX symlink + '..' overlap and repository-root boundary escape refusals
            logical = tmp_path / "logical"
            physical_child = tmp_path / "physical" / "child"
            logical.mkdir()
            physical_child.mkdir(parents=True)
            (logical / "alias").symlink_to(physical_child)
            sym_dotdot_overlap = subprocess.run(
                [
                    str(installer),
                    "--plugin-dir",
                    f"{logical}/alias/../plugin",
                    "--bin-dir",
                    str(tmp_path / "physical" / "plugin"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(sym_dotdot_overlap.returncode, 2)
            self.assertFalse((tmp_path / "physical" / "plugin").exists())

            (logical / "repo_alias").symlink_to(ROOT / "scripts")
            for repo_escape in (f"{logical}/repo_alias/..", f"{logical}/repo_alias/../sub"):
                repo_guard = subprocess.run(
                    [str(installer), "--plugin-dir", repo_escape, "--no-bin"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(repo_guard.returncode, 2)
            self.assertFalse((ROOT / "sub").exists())


if __name__ == "__main__":
    unittest.main()
