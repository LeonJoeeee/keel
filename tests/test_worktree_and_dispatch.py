from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from keel_lib import KeelError, load_defaults  # noqa: E402
from keel_lib.context import assemble_brief  # noqa: E402
from keel_lib.executors import EXECUTORS, ExecutorAdapter, RunRequest  # noqa: E402
from keel_lib.executors.codex import CodexExecutor  # noqa: E402
from keel_lib.worktree import cleanup_worktree, create_disposable_clone, create_worktree  # noqa: E402


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
    ) -> dict[str, Any]:
        return {"status": "completed", "executor": self.name}


class WorktreeAndDispatchTests(unittest.TestCase):
    def test_adopted_repo_zero_dirty_worktree_and_gitignored_agents_md_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "adopted"
            repo.mkdir()
            head_sha = _init_repo(repo)
            # Simulate repo that gitignores /AGENTS.md locally
            (repo / ".gitignore").write_text("/AGENTS.md\n__pycache__/\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "gitignore"], check=True, capture_output=True)
            head_sha = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip()
            (repo / "AGENTS.md").write_text("## Core need\nKeep data lossless.\n", encoding="utf-8")

            wt_info = create_worktree(repo, "feat-1", base="HEAD", fetch=False)
            wt_path = Path(wt_info["path"])
            self.assertEqual(wt_info["synced_docs"], ["AGENTS.md"])
            self.assertTrue((wt_path / "AGENTS.md").is_file())
            # Main repo status stays 100% clean (.git/info/exclude absorbed .agents/)
            main_porcelain = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain", "-uall"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(main_porcelain, "")

            # Disposable verification clone ALSO syncs gitignored AGENTS.md
            scratch = Path(tmp) / "verify-clone"
            clone_dir, _ = create_disposable_clone(repo, head_sha, scratch)
            self.assertTrue((clone_dir / "AGENTS.md").is_file())

            # Create ignored __pycache__ inside worktree — cleanup should NOT wedge on caches!
            cache_dir = wt_path / "__pycache__"
            cache_dir.mkdir()
            (cache_dir / "mod.cpython-312.pyc").write_bytes(b"123")

            # Modifying gitignored AGENTS.md inside worktree blocks accidental loss unless --discard
            (wt_path / "AGENTS.md").write_text("## Core need\nEdited locally!\n", encoding="utf-8")
            with self.assertRaises(KeelError):
                cleanup_worktree(repo, "feat-1", merged_head_sha=head_sha, caller_cwd=tmp)

            # Rejected cleanup (wrong merged_head_sha) MUST NOT unlink synced AGENTS.md!
            (wt_path / "AGENTS.md").write_text("## Core need\nKeep data lossless.\n", encoding="utf-8")
            with self.assertRaises(KeelError):
                cleanup_worktree(repo, "feat-1", merged_head_sha="0" * 40, caller_cwd=tmp)
            self.assertTrue((wt_path / "AGENTS.md").is_file())

            # Valid merged_head_sha -> cleanup succeeds cleanly even with __pycache__!
            res = cleanup_worktree(repo, "feat-1", merged_head_sha=head_sha, caller_cwd=tmp)
            self.assertFalse(wt_path.exists())
            self.assertEqual(res["deleted_branch"], "task/feat-1")

    def test_codex_argv_worktree_dual_git_dirs_and_resume_flag_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _init_repo(repo)
            wt_info = create_worktree(repo, "codex-lane", base="HEAD", fetch=False)
            wt_path = Path(wt_info["path"])

            codex = CodexExecutor()
            defaults = load_defaults()
            out_file = Path(tmp) / "out.json"
            schema_file = ROOT / "config" / "schemas" / "verdict.schema.json"

            argv = codex.build_exec_argv(
                workdir=wt_path,
                mode="write",
                model="gpt-6.1-sol",
                effort="xhigh",
                output_path=out_file,
                defaults=defaults,
                schema_path=schema_file,
                resume_session_id="12345678-1234-1234-1234-123456789abc",
            )
            # Linked worktree must pass TWO distinct --add-dir entries (common .git + worktrees/codex-lane)
            add_dir_indices = [i for i, tok in enumerate(argv) if tok == "--add-dir"]
            self.assertEqual(len(add_dir_indices), 2)

            # Top-level flags (-s, -C, --add-dir) MUST appear BEFORE 'resume'
            resume_idx = argv.index("resume")
            self.assertLess(argv.index("-s"), resume_idx)
            self.assertLess(argv.index("-C"), resume_idx)
            self.assertTrue(all(idx < resume_idx for idx in add_dir_indices))
            self.assertIn("--output-schema", argv)
            self.assertEqual(argv[-1], "-")

    def test_codex_defaults_xhigh_verify_and_subagent_inheritance(self) -> None:
        codex = CodexExecutor()
        defaults = load_defaults()
        codex_cfg = defaults["executors"]["codex"]

        self.assertEqual(codex_cfg["presets"]["implement"], {"model": "gpt-6.1-sol", "effort": "xhigh"})
        self.assertEqual(codex_cfg["presets"]["verify"], {"model": "gpt-6.1-sol", "effort": "xhigh"})
        self.assertEqual(codex_cfg["subagent_default"], {"model": "inherit", "effort": "inherit"})

        self.assertEqual(codex._resolve_model_effort("verify", None, None, defaults), ("gpt-6.1-sol", "xhigh"))
        self.assertEqual(codex._resolve_model_effort("implement", "inherit", "inherit", defaults), ("gpt-6.1-sol", "xhigh"))

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            out_file = workdir / "out.md"

            argv_sol = codex.build_exec_argv(
                workdir=workdir,
                mode="read",
                model="gpt-6.1-sol",
                effort="xhigh",
                output_path=out_file,
                defaults=defaults,
            )
            self.assertIn('agents.default_subagent_model="gpt-6.1-sol"', argv_sol)
            self.assertIn('agents.default_subagent_reasoning_effort="xhigh"', argv_sol)
            self.assertFalse(any("gpt-6.1-terra" in tok for tok in argv_sol))
            self.assertFalse(any('="inherit"' in tok for tok in argv_sol))

            argv_astra = codex.build_exec_argv(
                workdir=workdir,
                mode="write",
                model="gpt-6-astra",
                effort="max",
                output_path=out_file,
                defaults=defaults,
            )
            self.assertIn('agents.default_subagent_model="gpt-6-astra"', argv_astra)
            self.assertIn('agents.default_subagent_reasoning_effort="max"', argv_astra)
            self.assertFalse(any("gpt-6.1-terra" in tok for tok in argv_astra))

            explicit_override_defaults = {
                "executors": {
                    "codex": {
                        "subagent_default": {
                            "model": "gpt-6.1-terra",
                            "effort": "high",
                        }
                    }
                }
            }
            argv_override = codex.build_exec_argv(
                workdir=workdir,
                mode="read",
                model="gpt-6.1-sol",
                effort="xhigh",
                output_path=out_file,
                defaults=explicit_override_defaults,
            )
            self.assertIn('agents.default_subagent_model="gpt-6.1-terra"', argv_override)
            self.assertIn('agents.default_subagent_reasoning_effort="high"', argv_override)

    def test_universal_context_engineering_and_pluggable_third_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "proj"
            repo.mkdir()
            _init_repo(repo)
            (repo / "AGENTS.md").write_text("## Core need\nZero data corruption.\n", encoding="utf-8")

            # 1. Native AGY auto-loads AGENTS.md & GEMINI.md -> brief does NOT duplicate AGENTS.md
            brief_agy = assemble_brief(
                why="Verify parser invariants preserve lossless round-trip.",
                task="Check edge cases in app.py.",
                preset="general",
                mode="read",
                workdir=repo,
                project_root=repo,
                native_agent_docs=("AGENTS.md", "GEMINI.md"),
            )
            self.assertNotIn("Zero data corruption.", brief_agy)
            self.assertIn("Verify parser invariants", brief_agy)

            # 1b. Repo with ONLY GEMINI.md dispatched to Codex (which natively loads only AGENTS.md) -> injects GEMINI.md!
            (repo / "AGENTS.md").unlink()
            (repo / "GEMINI.md").write_text("## Core need\nGemini-only core invariant.\n", encoding="utf-8")
            brief_codex_gemini = assemble_brief(
                why="Ensure Codex still receives GEMINI.md.",
                task="Inspect app.py.",
                preset="general",
                mode="read",
                workdir=repo,
                project_root=repo,
                native_agent_docs=("AGENTS.md",),
            )
            self.assertIn("Gemini-only core invariant.", brief_codex_gemini)
            (repo / "GEMINI.md").unlink()
            (repo / "AGENTS.md").write_text("## Core need\nZero data corruption.\n", encoding="utf-8")

            # 2. Pluggable 3rd executor with auto_loads_agents_md=False -> automatically gets AGENTS.md!
            import contextlib
            import importlib.machinery
            import importlib.util
            import io

            loader = importlib.machinery.SourceFileLoader("keel_cli", str(ROOT / "scripts" / "keel"))
            spec = importlib.util.spec_from_loader("keel_cli", loader)
            assert spec is not None and spec.loader is not None
            keel_cli = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(keel_cli)

            EXECUTORS["dummy-sdk"] = DummyThirdSideExecutor
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = keel_cli.main(
                        [
                            "agent",
                            "run",
                            "--project",
                            str(repo),
                            "--executor",
                            "dummy-sdk",
                            "--preset",
                            "general",
                            "--why",
                            "Benchmark alternative serializer against Core need.",
                            "--task",
                            "Compare msgpack vs json latency.",
                        ]
                    )
                self.assertEqual(rc, 0)
                payload = json.loads(buf.getvalue())
                self.assertEqual(payload["executor"], "dummy-sdk")
                brief_sdk = Path(payload["brief_path"]).read_text(encoding="utf-8")
                self.assertIn("Zero data corruption.", brief_sdk)
            finally:
                EXECUTORS.pop("dummy-sdk", None)

    def test_install_plugin_per_entry_symlinks_exclude_root_agents_md(self) -> None:
        self.assertTrue((ROOT / "AGENTS.md").is_file())
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
            # Simulate upgrading from an existing top-level directory symlink
            plugin_dir.symlink_to(ROOT)
            self.assertTrue(plugin_dir.is_symlink())

            # Top-level symlink must fail --check
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
            self.assertTrue(plugin_dir.is_dir())
            self.assertEqual({p.name for p in plugin_dir.iterdir()}, expected_entries)
            for name in expected_entries:
                entry = plugin_dir / name
                self.assertTrue(entry.is_symlink())
                self.assertEqual(entry.resolve(), (ROOT / name).resolve())
            for excluded in ("AGENTS.md", "docs", "tests", ".git", ".github", ".gitignore"):
                self.assertFalse((plugin_dir / excluded).exists())
                self.assertFalse((plugin_dir / excluded).is_symlink())

            cli_link = bin_dir / "keel"
            self.assertTrue(cli_link.is_symlink())
            self.assertEqual(cli_link.resolve(), (ROOT / "scripts" / "keel").resolve())

            # --check passes on valid per-entry install
            chk_ok = subprocess.run(
                [str(installer), "--check", "--plugin-dir", str(plugin_dir), "--bin-dir", str(bin_dir)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(chk_ok.returncode, 0, chk_ok.stderr)

            # Stale symlinks (e.g., an accidental AGENTS.md symlink) fail --check and are cleaned on re-install
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


if __name__ == "__main__":
    unittest.main()
