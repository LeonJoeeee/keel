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
from keel_lib.context import (  # noqa: E402
    assemble_brief,
    extract_changed_markdown_sections,
    extract_markdown_section,
    save_run_record,
)
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
    def setUp(self) -> None:
        self.enterContext(patch("keel_lib.executors.codex.discover_mcp_approval_flags", return_value=[]))
        self.enterContext(patch("keel_lib.executors.agy.run_process_group",
                                side_effect=AssertionError("unexpected agy launch")))

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
        self.assertEqual(defaults["max_structural_recoveries"], 2)
        self.assertEqual(defaults["routing"], {"general": "agy", "implement": "agy", "verify": "codex"})
        self.assertEqual(
            defaults["executors"]["codex"],
            {
                "models_ranked": ["gpt-6.2-astra", "gpt-6.2-sol"],
                "levels": {
                    "L1": {"model": "gpt-6.2-sol", "effort": "medium"},
                    "L2": {"model": "gpt-6.2-sol", "effort": "max"},
                    "L3": {"model": "gpt-6.2-astra", "effort": "high"},
                    "L4": {"model": "gpt-6.2-astra", "effort": "xhigh"},
                },
                "subagent_default": {"model": "inherit", "effort": "inherit"},
            },
        )
        self.assertEqual(
            defaults["executors"]["agy"],
            {
                "models_ranked": [
                    "gemini-4-carbon-high",
                    "gemini-4-neon-xhigh",
                    "gemini-4-neon-high",
                    "gemini-4-neon-medium",
                    "gemini-4-hydrogen-high",
                ],
                "levels": {
                    "L1": {"model": "gemini-4-hydrogen-high"},
                    "L2": {"model": "gemini-4-neon-medium"},
                    "L3": {"model": "gemini-4-neon-high"},
                    "L4": {"model": "gemini-4-carbon-high"},
                },
            },
        )
        for executor, require_effort in (("codex", True), ("agy", False)):
            ranked = defaults["executors"][executor]["models_ranked"]
            cfg_levels = defaults["executors"][executor]["levels"]
            rank_positions: list[int] = []
            for level in levels:
                with self.subTest(executor=executor, level=level):
                    entry = cfg_levels[level]
                    expected_effort = entry["effort"] if require_effort else entry.get("effort")
                    self.assertEqual(
                        base.resolve_executor_level_config(executor, level.lower(), defaults),
                        (level, entry["model"], expected_effort),
                    )
                    self.assertIn(entry["model"], ranked)
                    rank_positions.append(ranked.index(entry["model"]))
            self.assertEqual(rank_positions, sorted(rank_positions, reverse=True))

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

            agy = repo / "agy"
            agy.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "if 'failing-model' in sys.argv:\n"
                "    print(json.dumps({'status': 'ERROR', 'error': 'failed test run'}))\n"
                "    sys.exit(4)\n"
                "print(json.dumps({'conversation_id': 'test-session', 'status': 'SUCCESS', "
                "'response': json.dumps(sys.argv[1:])}))\n"
            )
            agy.chmod(0o755)

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
            self.assertEqual((host["executor"], host["status"]), ("agy", "completed"))
            continued = cli(*resume, "--level", "L3", "--model", "custom-model", "--effort", "high")
            self.assertEqual((continued["level"], continued["model"], continued["effort"]),
                             ("L3", "custom-model", "high"))
            argv = json.loads(Path(continued["output_path"]).read_text())
            self.assertEqual(argv[:2], ["--conversation", host["session_id"]])
            self.assertIn("--add-dir", argv)
            self.assertNotIn("--new-project", argv)
            self.assertEqual(json.loads(Path(continued["result_path"]).read_text())["status"], "SUCCESS")

            embedded = cli(*resume, "--model", "test-model-low")
            argv = json.loads(Path(embedded["output_path"]).read_text())
            self.assertNotIn("--effort", argv)
            self.assertEqual(argv[argv.index("--model") + 1], "test-model-low")
            original = record_path.read_bytes()
            self.assertIn("conflicts", cli(*resume, "--effort", "high", status=2))
            self.assertEqual(record_path.read_bytes(), original)
            result_path = Path(json.loads(original)["result_path"])
            completed_result = result_path.read_bytes()
            self.assertEqual(json.loads(completed_result)["status"], "SUCCESS")
            self.assertIn("failed", cli(*resume, "--model", "failing-model", status=2))
            self.assertEqual(record_path.read_bytes(), original)
            self.assertEqual(result_path.read_bytes(), completed_result)

            verify = cli(*run, "--preset", "verify", "--executor", "agy", "--level", "L2")
            original = record_path.read_bytes()
            self.assertIn("ephemeral", cli(*resume, status=2).lower())
            self.assertEqual(record_path.read_bytes(), original)
            self.assertTrue(verify["ephemeral"])

            # `preset="arbitrate"` enforces `verify_min_level` and runs as a non-resumable ephemeral session
            self.assertIn("level", cli(*run, "--preset", "arbitrate", "--level", "L1", status=2).lower())
            self.assertIn("level", cli(*run, "--preset", "arbitrate", "--executor", "agy", "--level", "L1", status=2).lower())
            arb_codex = cli(*run, "--preset", "arbitrate", "--level", "L4", "--enforce-verdict-schema")
            self.assertEqual(arb_codex["executor"], "codex")
            self.assertTrue(arb_codex["ephemeral"])
            self.assertTrue(arb_codex["output_path"].endswith("arbiter.json"))
            self.assertTrue(str(arb_codex["schema_path"]).endswith("arbiter.schema.json"))
            self.assertIn("--ephemeral", json.loads(Path(arb_codex["output_path"]).read_text()))
            self.assertIn("ephemeral", cli(*resume, status=2).lower())

            arb_agy = cli(*run, "--preset", "arbitrate", "--executor", "agy", "--level", "L4")
            self.assertTrue(arb_agy["ephemeral"])
            self.assertIn("ephemeral", cli(*resume, status=2).lower())

    def test_agy_synchronous_argv_and_native_json(self) -> None:
        defaults = load_defaults()
        adapter = AgyExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            repo = path / "repo"
            _init_repo(repo)
            req = RunRequest(
                run_id="test", run_dir=path, brief_path=path / "brief.md", workdir=repo,
                project_root=repo, mode="read", preset="general", level="L2", model=None,
                effort=None, schema_path=SCHEMAS_DIR / "verdict.schema.json",
                output_path=path / "out.json", extra_git_dirs=(path / "extra",),
                why="Persist why in running record.",
            )
            result = {"status": "SUCCESS", "conversation_id": "test-id", "response": "fallback",
                      "structured_output": {"title": "hello"}, "usage": {"tokens": 4}}
            def launch(argv, *, cwd, env, on_stdout_line):
                running = json.loads((path / "run.json").read_text())
                self.assertEqual((running["status"], running["executor"], running["mode"]),
                                 ("running", "agy", req.mode))
                self.assertEqual(running["scratch_verify"], req.scratch_verify)
                self.assertEqual(running["why"], "Persist why in running record.")
                self.assertEqual(cwd, repo)
                self.assertEqual(env["KEEL_AGENT_MODE"], req.mode)
                self.assertEqual(argv[0], "agy")
                self.assertNotIn("--new-project", argv)
                self.assertIn("--dangerously-skip-permissions", argv)
                self.assertEqual(argv[argv.index("--model") + 1],
                                 defaults["executors"]["agy"]["levels"][req.level]["model"])
                self.assertEqual(argv[argv.index("--output-format") + 1], "json")
                self.assertEqual(argv[argv.index("--json-schema") + 1], str(req.schema_path))
                self.assertEqual(argv[-2], "-p")
                self.assertIn(f"`{req.brief_path}`", argv[-1])
                expected_dirs = {str(path)}
                if req.mode == "write" or req.scratch_verify:
                    expected_dirs.update((str(repo / ".git"), str(path / "extra")))
                self.assertEqual({argv[i+1] for i, arg in enumerate(argv) if arg == "--add-dir"},
                                 expected_dirs)
                on_stdout_line(json.dumps(result))
                return 0, ""

            with patch("keel_lib.executors.agy.run_process_group", side_effect=launch):
                for req.level, req.mode, req.scratch_verify in (
                    ("L1", "read", False), ("L2", "write", False),
                    ("L3", "read", True), ("L4", "read", False),
                ):
                    with self.subTest(level=req.level):
                        completed = adapter.execute(req, defaults)
                        self.assertEqual(completed["status"], "completed")
                        self.assertEqual(completed["usage"], result["usage"])
                        self.assertEqual(json.loads(req.output_path.read_text()), result["structured_output"])
                        self.assertEqual(json.loads((path / "agy-result.json").read_text()), result)

            with patch("keel_lib.executors.agy.run_process_group") as process:
                for override in (
                    *({"model": m} for m in ("pro", "flash", "flash_lite", "inherit", "bad model")),
                    *({"model": "custom-" + suffix, "effort": "medium" if suffix != "medium" else "max"}
                      for suffix in ("low", "medium", "high", "xhigh", "max")),
                    {"effort": "unknown"}, {"level": None}, {"preset": "verify", "level": "L1"},
                ):
                    with self.subTest(override=override), self.assertRaises(base.KeelConfigError):
                        adapter.execute(replace(req, **override), defaults)
                process.assert_not_called()

    def test_internal_codex_verify_requires_level_and_enforces_floor(self) -> None:
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
                    ("verify", "read", True),
                ):
                    with self.subTest(preset=preset, scratch=scratch):
                        missing_level = replace(req, preset=preset, mode=mode, scratch_verify=scratch)
                        with self.assertRaisesRegex(base.KeelConfigError, "--level"):
                            CodexExecutor().execute(missing_level, load_defaults())
                self.assertEqual(launch.call_count, 0)
                shipped = load_defaults()
                l3_cfg = shipped["executors"]["codex"]["levels"]["L3"]
                result = CodexExecutor().execute(replace(req, level="L3"), shipped)
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
                model="parent-model",
                effort="max",
                output_path=out_file,
                defaults=defaults,
            )
            s_idx = argv_ro.index("-s")
            self.assertEqual(argv_ro[s_idx + 1], "read-only")
            self.assertNotIn("--add-dir", argv_ro)
            self.assertIn('agents.default_subagent_model="parent-model"', argv_ro)
            self.assertIn('agents.default_subagent_reasoning_effort="max"', argv_ro)

            # 1b. Explicit `subagent_default` overrides (`child-model` @ `high`) apply to child subagents cleanly!
            custom_sub_defaults = {
                "executors": {
                    "codex": {
                        "subagent_default": {"model": "child-model", "effort": "high"},
                    }
                }
            }
            argv_custom_sub = codex.build_exec_argv(
                workdir=repo,
                mode="read",
                model="parent-model",
                effort="max",
                output_path=out_file,
                defaults=custom_sub_defaults,
            )
            self.assertEqual(argv_custom_sub[argv_custom_sub.index("-m") + 1], "parent-model")
            self.assertIn('model_reasoning_effort="max"', argv_custom_sub)
            self.assertIn('agents.default_subagent_model="child-model"', argv_custom_sub)
            self.assertIn('agents.default_subagent_reasoning_effort="high"', argv_custom_sub)

            # 2. `mode="read"` inside disposable `/tmp/keel-verify-*` clone (`allow_disposable_write=True`)
            # -> `-s workspace-write` scoped ONLY to `/tmp` clone + its separated git dir (real repo never added!)
            verify_scratch = Path(tmp) / "keel-verify-codex"
            clone_dir, sep_git = create_disposable_clone(repo, head_sha, verify_scratch)
            argv_verify = codex.build_exec_argv(
                workdir=clone_dir,
                mode="read",
                model="parent-model",
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
                model="parent-model",
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
            self.assertIn("captured automatically via CLI output", brief_agy_same)

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
            self.assertIn("## 2. Real solutions over fake green checks", brief_codex)
            self.assertIn("avoid arbitrary line-count quotas", brief_codex)
            self.assertIn("## 3. Protect human attention", brief_codex)
            self.assertNotIn("\n## 4.", brief_codex)
            self.assertNotIn("Orchestrator core loop", brief_codex)
            self.assertNotIn("Default to Orchestrator", brief_codex)
            self.assertNotIn("--force-round", brief_codex)
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
            def agy_stub(argv, *, on_stdout_line, **kwargs):
                on_stdout_line(json.dumps({"status": "SUCCESS", "conversation_id": "id", "response": "done"}))
                return 0, ""

            with contextlib.redirect_stdout(buf_wt), patch("keel_lib.executors.agy.run_process_group", agy_stub):
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
                model="parent-model",
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

    def test_root_agents_md_has_six_project_sections_in_order(self) -> None:
        agents_text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        lines = agents_text.splitlines()
        headings = [ln.strip() for ln in lines if ln.startswith("## ")]
        self.assertEqual(
            headings,
            [
                "## Core need",
                "## Design Philosophy",
                "## Architecture & Boundaries",
                "## Commands",
                "## Gotchas",
                "## Where to look",
            ],
        )

        spine_text = (ROOT / "rules" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("## 1. Global core-need alignment", spine_text)
        self.assertIn("## Design Philosophy", spine_text)
        sec4 = spine_text[spine_text.index("## 4. Orchestrator core loop") :]
        for required_clause in (
            "Default to Orchestrator; dispatch substantive writers (R1, R5, D1).",
            "Reserve direct main-session edits strictly for trivial, few-line single-point changes where writing a brief outweighs the edit.",
            "keel agent run --mode write --preset implement",
            "keel agent resume",
            "Handle a single cohesive slice (including coupled cross-file edits that must change together to pass checks)",
            "Split any multi-part overhaul by responsibility",
            "get `approve` from `keel branch verify` on slice N before starting slice N+1",
            "keel worktree create <slice-N+1> --base task/<slice-N>",
            "lock non-trivial stacked specs in `docs/tasks/todo/<YYYYMMDD>-<slug>.md` on `main` before branching the stack and pass `--task-file`",
            "never mutate an already-verified parent branch",
            "Verify each slice before advancing or landing (R3, P4).",
            "Stop after two rejected L4 rounds—at most 4 rounds from L2 (`L2 -> L3 -> L4 -> L4`), 3 from L3 (`L3 -> L4 -> L4`), or 2 from L4 (`L4 -> L4`)",
            "standing execution or landing delegation never authorizes `--force-round`, which requires explicit human authorization for that specific escalation",
            "keel agent run --level <level> [--executor agy|codex] [--mode read|write] [--preset general|implement|verify] [--worktree <slug>] [--why <why>] (--task <text> | --task-file <file>) [--context-file <file>]",
            "keel branch verify <slug> [--base <ref>] [--task-file <file>] [--why <why>] [--context-file <file>] [--level <level>]",
            "keel branch status [<slug>] [--base <ref>]",
            "keel branch land <slug> [--base <ref>] [--squash | --ff-only]",
            "land slices in order using `--ff-only` on parent slices",
            "if a parent has no `docs/tasks/active/` file to archive (including one inherited from `main`)",
            "rebase the child `--onto main <parent-head-sha>`",
            "### Review Rebuttals",
        ):
            with self.subTest(clause=required_clause):
                self.assertIn(required_clause, sec4)

        req_text = (ROOT / "docs" / "requirements.md").read_text(encoding="utf-8")
        self.assertIn("## Design Philosophy", req_text)
        self.assertLess(
            req_text.index("## Design Philosophy"),
            req_text.index("## Layer 1: Pure Requirements"),
        )
        self.assertIn("Reserve direct main-session edits for trivial, few-line changes", req_text)
        self.assertIn("dispatch substantive single-slice work to a clean-context writer", req_text)
        self.assertIn("without mandatory ticket templates, hosted pull requests, or custom CI job names", req_text)

        skill_text = (ROOT / "skills" / "keel-repo-ops" / "SKILL.md").read_text(encoding="utf-8")
        for required_skill_clause in (
            "six-section",
            "six sections in order",
            "## Core need",
            "## Design Philosophy",
            "## Architecture & Boundaries",
            "## Commands",
            "## Gotchas",
            "## Where to look",
            "Bad vs Good `## Core need`",
            "Bad vs Good Task Spec",
            "### Review Rebuttals",
            "Reserve direct main-session edits for trivial, few-line changes, and dispatch substantive slices to a clean-context writer (`D1`).",
        ):
            with self.subTest(skill_clause=required_skill_clause):
                self.assertIn(required_skill_clause, skill_text)

    def test_single_doc_task_handoff_markdown_extraction_and_context_file(self) -> None:
        # 1. Markdown section extraction handles Goal & Why / Why, nested subheadings, and fenced code blocks
        #    (including fences opened inside list items and blockquotes)
        doc_with_fences = (
            "# Task: Parser Fix\n\n"
            "Intro with example fences:\n"
            "```markdown\n"
            "## Goal & Why\n"
            "FAKE FENCED BACKTICK GOAL\n"
            "```\n\n"
            "~~~markdown\n"
            "## Why\n"
            "FAKE FENCED TILDE GOAL\n"
            "~~~\n\n"
            "- ```md\n"
            "  ## Why\n"
            "  FAKE LIST-FENCED GOAL\n"
            "  ```\n"
            "* ~~~md\n"
            "  ## Goal & Why\n"
            "  FAKE ASTERISK-FENCED GOAL\n"
            "  ~~~\n"
            "1. ```markdown\n"
            "   ## Why\n"
            "   FAKE NUMBERED-LIST FENCED GOAL\n"
            "   ```\n"
            "> ```md\n"
            "> ## Goal & Why\n"
            "> FAKE BLOCKQUOTE FENCED GOAL\n"
            "> ```\n\n"
            "## Goal & Why\n\n"
            "Keep user CSV dates lossless.\n\n"
            "### Edge Cases\n"
            "Reject ambiguous DMY/MDY formats.\n\n"
            "## Bounds\n"
            "parser.py only.\n"
        )
        extracted = extract_markdown_section(doc_with_fences, ("Goal & Why", "Why"))
        self.assertEqual(
            extracted,
            "Keep user CSV dates lossless.\n\n### Edge Cases\nReject ambiguous DMY/MDY formats.",
        )

        # Default `levels=(2,)` rejects `###` headings; explicit `levels=(2, 3)` extracts level-3 sections
        doc_h3 = (
            "## Decisions & Findings\n\n"
            "### Review Rebuttals\n"
            "Evidence-backed pushback.\n\n"
            "### Other Note\n"
            "Should not be included.\n"
        )
        self.assertIsNone(extract_markdown_section(doc_h3, ("Review Rebuttals",)))
        self.assertEqual(
            extract_markdown_section(doc_h3, ("Review Rebuttals",), levels=(2, 3)),
            "Evidence-backed pushback.",
        )

        # Empty, fenced-only, blockquote-inside-top-level-fence, or nested `### Why` under `## Bounds` returns None
        self.assertIsNone(
            extract_markdown_section(
                "```md\n## Goal & Why\nInside fence\n```\n\n## Bounds\nOnly x.py\n",
                ("Goal & Why", "Why"),
            )
        )
        self.assertIsNone(
            extract_markdown_section(
                "```md\n> ```\n## Why\nStill inside top-level fence!\n",
                ("Goal & Why", "Why"),
            )
        )
        self.assertIsNone(
            extract_markdown_section(
                "> ```md\n> > ```\n> ## Why\n> Still inside depth-1 blockquote fence!\n",
                ("Goal & Why", "Why"),
            )
        )
        self.assertEqual(
            extract_markdown_section(
                "> ```md\n> ## Why\n> Quoted unclosed fence\n\n## Goal & Why\nOutside ended blockquote.\n",
                ("Goal & Why", "Why"),
            ),
            "Outside ended blockquote.",
        )
        self.assertIsNone(
            extract_markdown_section(
                "- Reference template:\n\n    ```markdown\n  ## Why\n  Example only.\n    ```\n",
                ("Goal & Why", "Why"),
            )
        )
        # CommonMark §5.1 lazy paragraph continuation inside list items and blockquotes
        lazy_list_fence = (
            "- The previous ticket used this example\n"
            "as its reference template:\n\n"
            "    ```markdown\n"
            "  ## Why\n"
            "  Old ticket objective.\n"
            "    ```\n"
        )
        self.assertIsNone(extract_markdown_section(lazy_list_fence, ("Goal & Why", "Why")))
        self.assertEqual(
            extract_markdown_section(
                lazy_list_fence + "\n## Goal & Why\nReal objective.\n",
                ("Goal & Why", "Why"),
            ),
            "Real objective.",
        )
        self.assertEqual(
            extract_markdown_section(
                "> Previous note wrapped\n"
                "onto unindented lazy continuation:\n"
                "> ```markdown\n"
                "> ## Why\n"
                "> Fake quoted why.\n"
                "> ```\n\n"
                "## Goal & Why\nReal after lazy quote.\n",
                ("Goal & Why", "Why"),
            ),
            "Real after lazy quote.",
        )
        # CommonMark §4.4 indented code blocks (top-level, multi-paragraph with blank lines, and after Setext headings)
        self.assertIsNone(
            extract_markdown_section(
                "    ```markdown\n    ## Why\n    Fake.\n",
                ("Goal & Why", "Why"),
            )
        )
        self.assertEqual(
            extract_markdown_section(
                "    ```markdown\n    ## Why\n    Fake.\n\n## Goal & Why\nReal after indented code.\n",
                ("Goal & Why", "Why"),
            ),
            "Real after indented code.",
        )
        self.assertEqual(
            extract_markdown_section(
                "    ```markdown\n\n    ## Why\n    Still indented code.\n\n## Goal & Why\nReal after blank in code.\n",
                ("Goal & Why", "Why"),
            ),
            "Real after blank in code.",
        )
        self.assertEqual(
            extract_markdown_section(
                "Intro Heading\n---\n    ```markdown\n    ## Why\n    Indented code after setext.\n\n## Goal & Why\nReal after setext.\n",
                ("Goal & Why", "Why"),
            ),
            "Real after setext.",
        )
        # CommonMark §4.6 HTML comment blocks (multi-line, single-line, paragraph-interrupting) and HTML blocks
        self.assertIsNone(
            extract_markdown_section(
                "<!--\n## Why\nCommented template.\n-->\n",
                ("Goal & Why", "Why"),
            )
        )
        self.assertIsNone(
            extract_markdown_section(
                "<!-- ## Why -->\nCommented single-line heading.\n",
                ("Goal & Why", "Why"),
            )
        )
        self.assertEqual(
            extract_markdown_section(
                "<!--\n## Why\nCommented placeholder.\n-->\n\n## Goal & Why\nActual user goal.\n",
                ("Goal & Why", "Why"),
            ),
            "Actual user goal.",
        )
        self.assertEqual(
            extract_markdown_section(
                "- Wrapped list note\n<!--\n## Why\nInterrupted by comment.\n-->\n\n## Goal & Why\nGoal after comment.\n",
                ("Goal & Why", "Why"),
            ),
            "Goal after comment.",
        )
        self.assertEqual(
            extract_markdown_section(
                "<details>\n<summary>Reference</summary>\n## Why\nCollapsed example.\n</details>\n\n## Goal & Why\nGoal after details.\n",
                ("Goal & Why", "Why"),
            ),
            "Goal after details.",
        )
        # Round 6: UTF-8 BOM (`\ufeff`) before fence / heading + preserved first-line horizontal indentation
        self.assertIsNone(
            extract_markdown_section(
                "\ufeff~~~md\n## Why\nFake inside BOM tilde fence.\n~~~\n",
                ("Goal & Why", "Why"),
            )
        )
        self.assertEqual(
            extract_markdown_section(
                "\ufeff~~~md\n## Why\nFake inside BOM tilde fence.\n~~~\n\n## Goal & Why\nReal after BOM fence.\n",
                ("Goal & Why", "Why"),
            ),
            "Real after BOM fence.",
        )
        self.assertEqual(
            extract_markdown_section("\ufeff## Why\nDirect BOM goal.\n", ("Goal & Why", "Why")),
            "Direct BOM goal.",
        )
        self.assertEqual(
            extract_markdown_section(
                "## Goal & Why\n  \n    ## Done-check\n    Indented example.\n\nKeep bounds intact.\n",
                ("Goal & Why", "Why"),
            ),
            "    ## Done-check\n    Indented example.\n\nKeep bounds intact.",
        )
        changed_indented = extract_changed_markdown_sections(
            "# Task\n\n## Decisions & Findings\nInitial.\n",
            "# Task\n\n## Decisions & Findings\n  \n    ## Done-check\n    Example code block\n",
            ("Progress", "Decisions & Findings", "Review Rebuttals"),
            levels=(2, 3),
        )
        self.assertEqual(
            changed_indented,
            "## Decisions & Findings\n\n    ## Done-check\n    Example code block",
        )
        self.assertEqual(
            extract_markdown_section(
                "1. Example:\n\n     ```markdown\n   ## Fake Why\n     ```\n\n"
                "## Goal & Why\nReal numbered-list follow-up goal.\n",
                ("Goal & Why", "Why"),
            ),
            "Real numbered-list follow-up goal.",
        )
        self.assertEqual(
            extract_markdown_section(
                "1. ```markdown\n   ## Fake\n   ```\n\n## Why\nReal goal.\n",
                ("Goal & Why", "Why"),
            ),
            "Real goal.",
        )
        self.assertIsNone(
            extract_markdown_section(
                "> ## Why\n> Quoted example\n\n- ## Why\n  List example\n",
                ("Goal & Why", "Why"),
            )
        )
        self.assertIsNone(
            extract_markdown_section(
                "## Bounds\nOnly x.py\n\n### Why\nNested level-3 why under Bounds\n",
                ("Goal & Why", "Why"),
            )
        )
        self.assertIsNone(
            extract_markdown_section("## Goal & Why\n   \n## Bounds\nOnly x.py\n", ("Goal & Why", "Why"))
        )

        # Changed sections deduplicate nested `### Review Rebuttals` inside `## Decisions & Findings`
        base_md = "## Progress\n- [ ] Step 1\n\n## Decisions & Findings\nInitial note.\n"
        head_md = (
            "## Progress\n- [x] Step 1\n\n"
            "## Decisions & Findings\nInitial note.\n\n"
            "### Review Rebuttals\nFinding 1 is out of scope.\n"
        )
        changed = extract_changed_markdown_sections(
            base_md, head_md, ("Progress", "Decisions & Findings", "Review Rebuttals"), levels=(2, 3)
        )
        assert changed is not None
        self.assertIn("## Progress\n\n- [x] Step 1", changed)
        self.assertIn("## Decisions & Findings", changed)
        self.assertEqual(changed.count("### Review Rebuttals"), 1)

        # 2. assemble_brief supports why=None (extracted from task) and extra_context
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _init_repo(repo)
            (repo / "AGENTS.md").write_text("## Core need\nExact parsing.\n", encoding="utf-8")

            brief = assemble_brief(
                why=None,
                task="# Task\n\n## Why\nPrevent silent date swaps.\n\n## Bounds\napp.py\n",
                preset="implement",
                mode="write",
                workdir=repo,
                project_root=repo,
                extra_context="Pivot note: keep CLI flag backwards compatible.",
            )
            self.assertIn("## Why (Global Alignment)\n\nPrevent silent date swaps.", brief)
            self.assertIn("## Additional Context\n\nPivot note: keep CLI flag backwards compatible.", brief)
            self.assertLess(
                brief.index("## Task, Scope & Done Criteria"),
                brief.index("## Additional Context"),
            )
            self.assertLess(brief.index("## Additional Context"), brief.index("## Lens: Implementation"))

            with self.assertRaises(KeelError) as err_ctx:
                assemble_brief(
                    why=None,
                    task="# Task\n\n## Bounds\napp.py\n",
                    preset="general",
                    mode="read",
                    workdir=repo,
                    project_root=repo,
                )
            self.assertNotIn("1-3 sentences", str(err_ctx.exception))

            # 3. Real-Git CLI `keel agent run` without `--why`, with `--context-file`, and clean failure modes
            agy = repo / "agy"
            agy.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "print(json.dumps({'conversation_id': 'handoff-sess', 'status': 'SUCCESS', 'response': 'ok'}))\n",
                encoding="utf-8",
            )
            agy.chmod(0o755)

            clean_sub_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("KEEL_AGENT_MODE", "DEVSTANDARD_AGENT_MODE", "DEVSTANDARD_ROLE")
            }
            clean_sub_env["PATH"] = str(repo) + os.pathsep + os.environ["PATH"]

            def run_agent(*cli_args: str, expected_rc: int = 0) -> tuple[int, str, str]:
                proc = subprocess.run(
                    [str(ROOT / "scripts" / "keel"), "agent", "run", *cli_args, "--project", str(repo)],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=clean_sub_env,
                )
                self.assertEqual(proc.returncode, expected_rc, proc.stderr)
                return proc.returncode, proc.stdout, proc.stderr

            # (a) Missing both `--why` and `## Goal & Why` fails cleanly with exit 2 and ZERO orphan run dir / worktree!
            no_why_spec = repo / "no-why.md"
            no_why_spec.write_text(
                "# Task\n\n```markdown\n## Goal & Why\nFenced only!\n```\n\n## Bounds\napp.py\n",
                encoding="utf-8",
            )
            _, _, err_no_why = run_agent(
                "--level", "L2", "--executor", "agy", "--worktree", "never-created",
                "--task-file", str(no_why_spec), "--run-id", "orphan-check-1",
                expected_rc=2,
            )
            self.assertIn("Goal & Why", err_no_why)
            self.assertFalse((repo / ".agents/runs/orphan-check-1").exists())
            self.assertFalse((repo / ".agents/worktrees/never-created").exists())

            # (b) Empty `## Goal & Why` also fails cleanly with exit 2 and ZERO orphan run dir!
            empty_why_spec = repo / "empty-why.md"
            empty_why_spec.write_text("# Task\n\n## Goal & Why\n\n## Bounds\napp.py\n", encoding="utf-8")
            run_agent(
                "--level", "L2", "--executor", "agy",
                "--task-file", str(empty_why_spec), "--run-id", "orphan-check-2",
                expected_rc=2,
            )
            self.assertFalse((repo / ".agents/runs/orphan-check-2").exists())

            # (b2) Task containing only `## Bounds` + nested `### Why` fails cleanly with exit 2 and ZERO orphan run dir!
            h3_why_spec = repo / "h3-why.md"
            h3_why_spec.write_text(
                "# Task\n\n## Bounds\napp.py\n\n### Why\nNested subsection under Bounds must not qualify.\n",
                encoding="utf-8",
            )
            _, _, err_h3_why = run_agent(
                "--level", "L2", "--executor", "agy", "--worktree", "never-created-h3",
                "--task-file", str(h3_why_spec), "--run-id", "orphan-check-h3",
                expected_rc=2,
            )
            self.assertIn("Goal & Why", err_h3_why)
            self.assertFalse((repo / ".agents/runs/orphan-check-h3").exists())
            self.assertFalse((repo / ".agents/worktrees/never-created-h3").exists())

            # (b3) Top-level ` ```md ` containing `> ``` ` does NOT close the fence prematurely; exits 2 with zero run dir!
            bq_in_fence_spec = repo / "bq-in-fence.md"
            bq_in_fence_spec.write_text(
                "# Task\n\n```md\n> ```\n## Why\nStill inside unclosed top-level fence!\n",
                encoding="utf-8",
            )
            _, _, err_bq_fence = run_agent(
                "--level", "L2", "--executor", "agy", "--worktree", "never-created-bq",
                "--task-file", str(bq_in_fence_spec), "--run-id", "orphan-check-bq-in-fence",
                expected_rc=2,
            )
            self.assertIn("Goal & Why", err_bq_fence)
            self.assertFalse((repo / ".agents/runs/orphan-check-bq-in-fence").exists())
            self.assertFalse((repo / ".agents/worktrees/never-created-bq").exists())

            # (c) Missing, untraversable (`chmod 000`), or non-UTF-8 `--context-file` / `--task-file` fails cleanly with exit 2!
            if os.geteuid() != 0:
                locked_dir = repo / "locked-ctx-dir"
                locked_dir.mkdir()
                locked_ctx = locked_dir / "ctx.md"
                locked_ctx.write_text("hidden\n", encoding="utf-8")
                locked_dir.chmod(0o000)
                try:
                    _, _, err_perm = run_agent(
                        "--level", "L2", "--executor", "agy",
                        "--why", "Check permission error handling.",
                        "--task", "Inspect.",
                        "--context-file", str(locked_ctx),
                        "--run-id", "orphan-check-perm",
                        expected_rc=2,
                    )
                    self.assertIn("keel error:", err_perm)
                    self.assertIn("--context-file", err_perm)
                    self.assertNotIn("Traceback", err_perm)
                    self.assertFalse((repo / ".agents/runs/orphan-check-perm").exists())
                finally:
                    locked_dir.chmod(0o755)
                    locked_ctx.unlink()
                    locked_dir.rmdir()
            good_spec = repo / "good-spec.md"
            good_spec.write_text(
                "# Task: Good\n\n"
                "- ```md\n"
                "  ## Why\n"
                "  Ignored inside bullet code fence\n"
                "  ```\n\n"
                "## Goal & Why\nKeep single-document handoff authoritative.\n\n"
                "## Bounds\napp.py\n",
                encoding="utf-8",
            )
            _, _, err_ctx_missing = run_agent(
                "--level", "L2", "--executor", "agy",
                "--task-file", str(good_spec),
                "--context-file", str(repo / "missing-context.md"),
                "--run-id", "orphan-check-3",
                expected_rc=2,
            )
            self.assertIn("--context-file", err_ctx_missing)
            self.assertFalse((repo / ".agents/runs/orphan-check-3").exists())

            utf16_ctx = repo / "utf16-context.md"
            utf16_ctx.write_bytes("UTF-16 context note".encode("utf-16"))
            _, _, err_ctx_utf16 = run_agent(
                "--level", "L2", "--executor", "agy",
                "--task-file", str(good_spec),
                "--context-file", str(utf16_ctx),
                "--run-id", "orphan-check-utf16",
                expected_rc=2,
            )
            self.assertIn("keel error:", err_ctx_utf16)
            self.assertIn("UTF-8", err_ctx_utf16)
            self.assertNotIn("Traceback", err_ctx_utf16)
            self.assertFalse((repo / ".agents/runs/orphan-check-utf16").exists())

            _, _, err_task_utf16 = run_agent(
                "--level", "L2", "--executor", "agy",
                "--task-file", str(utf16_ctx),
                "--run-id", "orphan-check-task-utf16",
                expected_rc=2,
            )
            self.assertIn("keel error:", err_task_utf16)
            self.assertIn("UTF-8", err_task_utf16)
            self.assertNotIn("Traceback", err_task_utf16)
            self.assertFalse((repo / ".agents/runs/orphan-check-task-utf16").exists())

            # (d) `--task-file` without `--why` + valid `--context-file` succeeds cleanly (without leaking bullet fence or Bounds)!
            ctx_file = repo / "rework-notes.md"
            ctx_file.write_text("Address round-1 finding on empty inputs.\n", encoding="utf-8")
            _, out_ok, _ = run_agent(
                "--level", "L2", "--executor", "agy", "--worktree", "handoff-wt",
                "--task-file", str(good_spec),
                "--context-file", str(ctx_file),
                "--run-id", "handoff-ok-1",
                expected_rc=0,
            )
            payload_ok = json.loads(out_ok)
            self.assertEqual(payload_ok["why"], "Keep single-document handoff authoritative.")
            brief_ok = Path(payload_ok["brief_path"]).read_text(encoding="utf-8")
            self.assertIn("## Why (Global Alignment)\n\nKeep single-document handoff authoritative.", brief_ok)
            self.assertNotIn("Ignored inside bullet code fence", brief_ok.split("## Task, Scope & Done Criteria")[0])
            self.assertNotIn("## Bounds", brief_ok.split("## Task, Scope & Done Criteria")[0])
            self.assertIn("## Additional Context\n\nAddress round-1 finding on empty inputs.", brief_ok)

            # UTF-16 `--message-file` on `agent resume` also fails cleanly with exit 2 and no orphan followup file
            utf16_msg = repo / "utf16-msg.md"
            utf16_msg.write_bytes("UTF-16 resume message".encode("utf-16"))
            res_utf16 = subprocess.run(
                [
                    str(ROOT / "scripts" / "keel"), "agent", "resume", "handoff-ok-1",
                    "--message-file", str(utf16_msg), "--project", str(repo),
                ],
                cwd=repo, capture_output=True, text=True, timeout=10,
                env=clean_sub_env,
            )
            self.assertEqual(res_utf16.returncode, 2)
            self.assertIn("keel error:", res_utf16.stderr)
            self.assertIn("UTF-8", res_utf16.stderr)
            self.assertNotIn("Traceback", res_utf16.stderr)
            self.assertEqual(list((repo / ".agents/runs/handoff-ok-1").glob("followup-*.md")), [])

            # (b4) Multi-line list continuation fence (`- Reference template:\n\n    ```markdown\n  ## Why...`) exits 2!
            list_cont_fence_spec = repo / "list-cont-fence.md"
            list_cont_fence_spec.write_text(
                "- Reference template:\n\n    ```markdown\n  ## Why\n  Example only.\n    ```\n",
                encoding="utf-8",
            )
            _, _, err_list_cont = run_agent(
                "--level", "L2", "--executor", "agy", "--worktree", "never-created-list-cont",
                "--task-file", str(list_cont_fence_spec), "--run-id", "orphan-check-list-cont",
                expected_rc=2,
            )
            self.assertIn("Goal & Why", err_list_cont)
            self.assertFalse((repo / ".agents/runs/orphan-check-list-cont").exists())
            self.assertFalse((repo / ".agents/worktrees/never-created-list-cont").exists())

            # (b5) Blockquoted (`> ## Why`) or list-nested (`- ## Why`) headings without top-level `## Why` exit 2!
            nested_heading_spec = repo / "nested-headings-only.md"
            nested_heading_spec.write_text(
                "# Task\n\n> ## Why\n> Quoted example\n\n- ## Why\n  List example\n\n## Bounds\napp.py\n",
                encoding="utf-8",
            )
            run_agent(
                "--level", "L2", "--executor", "agy",
                "--task-file", str(nested_heading_spec), "--run-id", "orphan-check-nested-headings",
                expected_rc=2,
            )
            self.assertFalse((repo / ".agents/runs/orphan-check-nested-headings").exists())

            # (e) `## Why` heading variant + numbered-list continuation & inline fences work without `--why`
            why_only_spec = repo / "why-only.md"
            why_only_spec.write_text(
                "# Task\n\n"
                "1. Example:\n\n"
                "     ```markdown\n"
                "   ## Fake Why\n"
                "     ```\n\n"
                "## Goal & Why\n"
                "Real numbered-list follow-up goal.\n\n"
                "## Bounds\napp.py\n",
                encoding="utf-8",
            )
            _, out_why, _ = run_agent(
                "--level", "L2", "--executor", "agy",
                "--task-file", str(why_only_spec),
                "--run-id", "handoff-ok-2",
                expected_rc=0,
            )
            brief_why = Path(json.loads(out_why)["brief_path"]).read_text(encoding="utf-8")
            self.assertIn("## Why (Global Alignment)\n\nReal numbered-list follow-up goal.", brief_why)

            inline_num_spec = repo / "inline-num.md"
            inline_num_spec.write_text(
                "1. ```markdown\n   ## Fake\n   ```\n\n## Why\nReal goal.\n\n## Bounds\napp.py\n",
                encoding="utf-8",
            )
            _, out_inline_num, _ = run_agent(
                "--level", "L2", "--executor", "agy",
                "--task-file", str(inline_num_spec),
                "--run-id", "handoff-ok-3",
                expected_rc=0,
            )
            brief_inline_num = Path(json.loads(out_inline_num)["brief_path"]).read_text(encoding="utf-8")
            self.assertIn("## Why (Global Alignment)\n\nReal goal.", brief_inline_num)

            # (f) Round 4 CommonMark lazy paragraph continuation & 4-space indented code block CLI checks
            lazy_only_spec = repo / "lazy-list-cont-only.md"
            lazy_only_spec.write_text(
                "- The previous ticket used this example\n"
                "as its reference template:\n\n"
                "    ```markdown\n"
                "  ## Why\n"
                "  Old ticket objective.\n"
                "    ```\n",
                encoding="utf-8",
            )
            _, _, err_lazy_only = run_agent(
                "--level", "L2", "--executor", "agy", "--worktree", "never-created-lazy-cont",
                "--task-file", str(lazy_only_spec), "--run-id", "orphan-check-lazy-cont",
                expected_rc=2,
            )
            self.assertIn("Goal & Why", err_lazy_only)
            self.assertFalse((repo / ".agents/runs/orphan-check-lazy-cont").exists())
            self.assertFalse((repo / ".agents/worktrees/never-created-lazy-cont").exists())

            lazy_then_real_spec = repo / "lazy-list-then-real.md"
            lazy_then_real_spec.write_text(
                "- The previous ticket used this example\n"
                "as its reference template:\n\n"
                "    ```markdown\n"
                "  ## Why\n"
                "  Old ticket objective.\n"
                "    ```\n\n"
                "## Goal & Why\n"
                "Real objective.\n\n"
                "## Bounds\napp.py\n",
                encoding="utf-8",
            )
            _, out_lazy_real, _ = run_agent(
                "--level", "L2", "--executor", "agy",
                "--task-file", str(lazy_then_real_spec),
                "--run-id", "handoff-ok-lazy-cont",
                expected_rc=0,
            )
            payload_lazy_real = json.loads(out_lazy_real)
            self.assertEqual(payload_lazy_real["why"], "Real objective.")
            brief_lazy_real = Path(payload_lazy_real["brief_path"]).read_text(encoding="utf-8")
            self.assertIn("## Why (Global Alignment)\n\nReal objective.", brief_lazy_real)
            self.assertNotIn("Old ticket objective.", brief_lazy_real.split("## Task, Scope & Done Criteria")[0])

            indented_code_only_spec = repo / "indented-code-only.md"
            indented_code_only_spec.write_text(
                "    ```markdown\n    ## Why\n    Fake.\n",
                encoding="utf-8",
            )
            run_agent(
                "--level", "L2", "--executor", "agy",
                "--task-file", str(indented_code_only_spec),
                "--run-id", "orphan-check-indented-code",
                expected_rc=2,
            )
            self.assertFalse((repo / ".agents/runs/orphan-check-indented-code").exists())

            indented_code_then_real_spec = repo / "indented-code-then-real.md"
            indented_code_then_real_spec.write_text(
                "    ```markdown\n"
                "    ## Why\n"
                "    Fake.\n\n"
                "## Goal & Why\n"
                "Real after indented code.\n\n"
                "## Bounds\napp.py\n",
                encoding="utf-8",
            )
            _, out_indented_real, _ = run_agent(
                "--level", "L2", "--executor", "agy",
                "--task-file", str(indented_code_then_real_spec),
                "--run-id", "handoff-ok-indented-code",
                expected_rc=0,
            )
            payload_indented_real = json.loads(out_indented_real)
            self.assertEqual(payload_indented_real["why"], "Real after indented code.")
            brief_indented_real = Path(payload_indented_real["brief_path"]).read_text(encoding="utf-8")
            self.assertIn("## Why (Global Alignment)\n\nReal after indented code.", brief_indented_real)
            self.assertNotIn("Fake.", brief_indented_real.split("## Task, Scope & Done Criteria")[0])

            # (g) Round 5: leading line-1 4-space indented `    ## Why` + CommonMark §4.6 HTML comment blocks
            leading_indented_spec = repo / "leading-indented-h2.md"
            leading_indented_spec.write_text(
                "    ## Why\n    Example only.\n\n## Goal & Why\nPrevent data loss.\n\n## Bounds\napp.py\n",
                encoding="utf-8",
            )
            _, out_leading_ind, _ = run_agent(
                "--level", "L2", "--executor", "agy",
                "--task-file", str(leading_indented_spec),
                "--run-id", "handoff-ok-leading-indented",
                expected_rc=0,
            )
            payload_leading_ind = json.loads(out_leading_ind)
            self.assertEqual(payload_leading_ind["why"], "Prevent data loss.")
            brief_leading_ind = Path(payload_leading_ind["brief_path"]).read_text(encoding="utf-8")
            self.assertIn("## Why (Global Alignment)\n\nPrevent data loss.", brief_leading_ind)
            self.assertIn("## Task, Scope & Done Criteria\n\n    ## Why\n", brief_leading_ind)

            html_comment_only_spec = repo / "html-comment-only.md"
            html_comment_only_spec.write_text(
                "<!--\n## Why\nCommented template.\n-->\n\n<!-- ## Why -->\nBody.\n",
                encoding="utf-8",
            )
            _, _, err_html_only = run_agent(
                "--level", "L2", "--executor", "agy", "--worktree", "never-created-html-comment",
                "--task-file", str(html_comment_only_spec), "--run-id", "orphan-check-html-comment",
                expected_rc=2,
            )
            self.assertIn("Goal & Why", err_html_only)
            self.assertFalse((repo / ".agents/runs/orphan-check-html-comment").exists())
            self.assertFalse((repo / ".agents/worktrees/never-created-html-comment").exists())

            single_line_comment_spec = repo / "single-line-comment-only.md"
            single_line_comment_spec.write_text(
                "<!-- ## Why -->\nNot a real heading.\n",
                encoding="utf-8",
            )
            run_agent(
                "--level", "L2", "--executor", "agy",
                "--task-file", str(single_line_comment_spec), "--run-id", "orphan-check-single-comment",
                expected_rc=2,
            )
            self.assertFalse((repo / ".agents/runs/orphan-check-single-comment").exists())

            html_comment_then_real_spec = repo / "html-comment-then-real.md"
            html_comment_then_real_spec.write_text(
                "<!--\n## Why\nCommented placeholder.\n-->\n\n## Goal & Why\nActual user goal.\n\n## Bounds\napp.py\n",
                encoding="utf-8",
            )
            _, out_html_real, _ = run_agent(
                "--level", "L2", "--executor", "agy",
                "--task-file", str(html_comment_then_real_spec),
                "--run-id", "handoff-ok-html-comment",
                expected_rc=0,
            )
            payload_html_real = json.loads(out_html_real)
            self.assertEqual(payload_html_real["why"], "Actual user goal.")
            brief_html_real = Path(payload_html_real["brief_path"]).read_text(encoding="utf-8")
            why_alignment_part = brief_html_real.split("## Task, Scope & Done Criteria")[0]
            self.assertIn("## Why (Global Alignment)\n\nActual user goal.", why_alignment_part)
            self.assertNotIn("Commented placeholder", why_alignment_part)
            self.assertNotIn("-->", why_alignment_part)

            # (h) Round 6: UTF-8 BOM (`\ufeff`) before `~~~md` fence and directly before `## Why` on `agent run`
            bom_fence_only_spec = repo / "bom-fence-only.md"
            bom_fence_only_spec.write_bytes(
                "\ufeff~~~md\n## Why\nFake BOM fenced why.\n~~~\n".encode("utf-8")
            )
            run_agent(
                "--level", "L2", "--executor", "agy",
                "--task-file", str(bom_fence_only_spec), "--run-id", "orphan-check-bom-fence",
                expected_rc=2,
            )
            self.assertFalse((repo / ".agents/runs/orphan-check-bom-fence").exists())

            bom_fence_real_spec = repo / "bom-fence-then-real.md"
            bom_fence_real_spec.write_bytes(
                "\ufeff~~~md\n## Why\nFake BOM fenced why.\n~~~\n\n## Goal & Why\nReal goal after BOM fence.\n".encode("utf-8")
            )
            _, out_bom_fence, _ = run_agent(
                "--level", "L2", "--executor", "agy",
                "--task-file", str(bom_fence_real_spec), "--run-id", "handoff-ok-bom-fence",
                expected_rc=0,
            )
            payload_bom_fence = json.loads(out_bom_fence)
            self.assertEqual(payload_bom_fence["why"], "Real goal after BOM fence.")
            brief_bom_fence = Path(payload_bom_fence["brief_path"]).read_text(encoding="utf-8")
            self.assertNotIn("\ufeff", brief_bom_fence)
            self.assertIn("## Why (Global Alignment)\n\nReal goal after BOM fence.", brief_bom_fence)

            bom_direct_why_spec = repo / "bom-direct-why.md"
            bom_direct_why_spec.write_bytes("\ufeff## Why\nDirect BOM why.\n\n## Bounds\napp.py\n".encode("utf-8"))
            _, out_bom_why, _ = run_agent(
                "--level", "L2", "--executor", "agy",
                "--task-file", str(bom_direct_why_spec), "--run-id", "handoff-ok-bom-why",
                expected_rc=0,
            )
            self.assertEqual(json.loads(out_bom_why)["why"], "Direct BOM why.")

            _, out_bom_inline_task, _ = run_agent(
                "--level", "L2", "--executor", "agy",
                "--task", "\ufeff## Why\nInline BOM why.\n", "--run-id", "handoff-ok-bom-inline",
                expected_rc=0,
            )
            self.assertEqual(json.loads(out_bom_inline_task)["why"], "Inline BOM why.")

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

    def test_disjoint_custom_branch_lineage_paths_and_domain_carryforward_stripping(self) -> None:
        from keel_lib.context import strip_review_history_sections
        from keel_lib.worktree import lineage_state_path, resolve_supersedes_lineage, save_lineage_state

        with tempfile.TemporaryDirectory() as tmp:
            common_dir = Path(tmp) / ".git"
            task_p = lineage_state_path(common_dir, "task/parser")
            flat_p = lineage_state_path(common_dir, "parser")
            pref_p = lineage_state_path(common_dir, "fix/parser")
            nested_task_p = lineage_state_path(common_dir, "task/sub/parser")
            self.assertEqual(task_p.name, "parser.json")
            self.assertEqual(flat_p.name, "custom+parser.json")
            self.assertEqual(pref_p.name, "custom+fix%2Fparser.json")
            self.assertEqual(nested_task_p.name, "custom+task%2Fsub%2Fparser.json")
            self.assertEqual(len({task_p, flat_p, pref_p, nested_task_p}), 4)

            save_lineage_state(
                common_dir,
                "task/parser",
                {"slug": "parser", "branch": "task/parser", "recovery_count": 1, "prior_directives": ["TASK_D"]},
            )
            save_lineage_state(
                common_dir,
                "parser",
                {"slug": "parser", "branch": "parser", "recovery_count": 2, "prior_directives": ["FLAT_D"]},
            )
            self.assertEqual(resolve_supersedes_lineage(common_dir, "task/parser")["prior_directives"], ["TASK_D"])
            self.assertEqual(resolve_supersedes_lineage(common_dir, "parser")["prior_directives"], ["TASK_D"])
            self.assertEqual(resolve_supersedes_lineage(common_dir, "refs/heads/parser")["prior_directives"], ["FLAT_D"])

            # When `task/<slug>` has no lineage, bare `<slug>` falls back to flat custom branch `custom+<slug>.json`
            save_lineage_state(
                common_dir,
                "solo-flat",
                {"slug": "solo-flat", "branch": "solo-flat", "recovery_count": 1, "prior_directives": ["SOLO_FLAT_D"]},
            )
            self.assertEqual(resolve_supersedes_lineage(common_dir, "solo-flat")["prior_directives"], ["SOLO_FLAT_D"])

        domain_md = (
            "# Tax loss carry-forward\n\n"
            "Cap ordinary offset at $3,000.\n\n"
            "## Carry forward schedule\n\n"
            "Track short-term vs long-term buckets.\n\n"
            "### Prior Review Carry-Forward Ledger\n\n"
            "Internal Round 1 blocker.\n\n"
            "### Review Ledger\n\n"
            "Internal ledger summary.\n"
        )
        cleaned = strip_review_history_sections(domain_md)
        self.assertIn("# Tax loss carry-forward", cleaned)
        self.assertIn("Cap ordinary offset at $3,000.", cleaned)
        self.assertIn("## Carry forward schedule", cleaned)
        self.assertIn("Track short-term vs long-term buckets.", cleaned)
        self.assertNotIn("Internal Round 1 blocker.", cleaned)
        self.assertNotIn("Internal ledger summary.", cleaned)


if __name__ == "__main__":
    unittest.main()
