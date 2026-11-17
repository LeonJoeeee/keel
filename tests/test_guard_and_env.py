from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from keel_lib import KeelError  # noqa: E402
from keel_lib.env import is_disposable_verify_path, is_subpath, run_cmd, run_process_group, safe_resolve, same_path, tool_env  # noqa: E402
from keel_lib.guard_rules import detect_agent_mode, evaluate_command  # noqa: E402
from keel_lib.worktree import create_disposable_clone, create_worktree  # noqa: E402


def _init_repo(path: Path, *, default_branch: str = "main") -> str:
    subprocess.run(["git", "init", "-b", default_branch, str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    (path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


class GuardRulesTests(unittest.TestCase):
    def test_false_positive_regressions_and_normal_main_pushes_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            non_git = Path(tmp) / "scratch"
            non_git.mkdir()
            main_safe = [
                "pytest tests/merge/ -q",
                "python3 src/merge.py --check",
                "git push",
                "git push origin main",
                "git push th main",
                "git push -u origin main",
                "git push -ofoo=bar origin main",
                "git push -o force origin main",
                "git push origin v0.2.0",
                'echo "git push --force origin main should not run"',
                "cat <<'EOF' > /tmp/note.txt\ngit push --force origin main\nkeel branch land x --execute\nEOF",
                "echo '<<EOF' && echo ok",
                "# comment mentioning <<EOF\necho ok",
                "printf $'it\\'s\\n' && echo '; git push -f'",
                "git status && git diff",
            ]
            for cmd in main_safe:
                with self.subTest(cmd=cmd):
                    self.assertIsNone(evaluate_command(cmd, mode="main", cwd=non_git))

    def test_destructive_git_pushes_and_shell_compound_bypasses_blocked(self) -> None:
        adversarial_bypasses = [
            "git push --force origin main",
            "git push -f origin main",
            "git push -uf origin main",
            "git push -fu origin main",
            "git push --force-with-lease origin main",
            "git push --force-with-lease=main:deadbeef origin main",
            "git push --force-if-includes origin main",
            "git push --delete origin task/old",
            "git push -d origin task/old",
            "git push --mirror origin",
            "git push origin +main",
            "git push origin +HEAD:main",
            "git push origin :task/old",
            "git -c remote.origin.push=+HEAD:refs/heads/main push origin",
            "git push>/tmp/out --force origin main",
            "git&>/dev/null push -f origin main",
            "git >|/dev/null push -f origin main",
            "git >&1tmp push -f origin main",
            "git {fd}>/dev/null push -f origin main",
            "git $'push' --force origin main",
            'git $"push" --force origin main',
            "# comment \\\ngit push --force origin main",
            "cat <<'EOF' >/dev/null\nmacro \\\nEOF\ngit push -f origin main",
            "sudo git -C /tmp/repo push --force origin main",
            "echo $(git push --force origin main)",
            "if true; then git push -f origin main; fi",
            "{ git push -f origin main; }",
            "! git push -f origin main",
            "</dev/null git push -f origin main",
            "git \\\npush \\\n--force origin main",
            "cat <<EOF > /tmp/x\n$(git push -f origin main)\nEOF",
            "echo '<<EOF'\ngit push -f origin main",
        ]
        for cmd in adversarial_bypasses:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(evaluate_command(cmd, mode="main", cwd="/tmp"))

        # Subagents (`read` and `write`) block ALL `git push` commands
        for sub_mode in ("read", "write"):
            for cmd in ("git push", "git push origin task/feat", "git push th main"):
                self.assertIsNotNone(evaluate_command(cmd, mode=sub_mode, cwd="/tmp"))

    def test_keel_cli_subagent_guards(self) -> None:
        for mode in ("read", "write"):
            self.assertIsNotNone(evaluate_command("keel branch land feat --execute", mode=mode))
            self.assertIsNotNone(evaluate_command("keel branch land feat --discard --reason obsolete", mode=mode))
            self.assertIsNotNone(
                evaluate_command("python3 -W default /opt/keel/scripts/keel branch land feat --execute", mode=mode)
            )
            self.assertIsNotNone(evaluate_command("keel worktree cleanup feat --discard", mode=mode))
            # Dry-run `keel branch land` and `keel branch status` are allowed
            self.assertIsNone(evaluate_command("keel branch land feat", mode=mode))
            self.assertIsNone(evaluate_command("keel branch status feat", mode=mode))

        # `read` mode additionally blocks recording verdicts or creating/cleaning worktrees
        self.assertIsNotNone(
            evaluate_command(
                "keel branch record-verdict feat --verdict-file /tmp/v.json --head-sha " + "a" * 40,
                mode="read",
            )
        )
        self.assertIsNotNone(evaluate_command("keel branch verify feat", mode="read"))
        self.assertIsNotNone(evaluate_command("keel worktree create feat", mode="read"))
        self.assertIsNone(evaluate_command("keel branch land feat --execute", mode="main"))
        self.assertIsNone(evaluate_command("keel branch land feat --discard --reason done", mode="main"))

    def test_read_mode_blocks_repo_mutations_but_allows_disposable_verify_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            head_sha = _init_repo(repo)

            # Outside `/tmp/keel-verify-*`: block mutating git subcommands, allow read-only queries
            blocked_in_read = [
                "git commit -m 'x'",
                "git merge task/x",
                "git rebase main",
                "git cherry-pick HEAD",
                "git reset --hard",
                "git checkout -b new-branch",
                "git switch -c new-branch",
                "git restore app.py",
                "git clean -fd",
                "git stash",
                "git stash pop",
                "git tag v1.0.0",
                "git tag -d v1.0.0",
                "git branch new-branch",
                "git branch -D main",
                "git branch -m renamed",
                "git worktree add /tmp/wt",
                "git worktree remove /tmp/wt",
                "git worktree prune",
                "git pull",
                "git am patch.mbox",
                "git apply patch.diff",
            ]
            for cmd in blocked_in_read:
                with self.subTest(cmd=cmd):
                    self.assertIsNotNone(evaluate_command(cmd, mode="read", cwd=repo))

            allowed_in_read = [
                "git status --porcelain -uall",
                "git diff HEAD~0",
                "git log -n 5 --oneline",
                "git stash list",
                "git tag",
                "git tag -l 'v*'",
                "git branch",
                "git branch -a",
                "git branch --show-current",
                "git worktree list",
            ]
            for cmd in allowed_in_read:
                with self.subTest(cmd=cmd):
                    self.assertIsNone(evaluate_command(cmd, mode="read", cwd=repo))

            # Ordinary worktree named `.agents/worktrees/keel-verify-read` MUST NOT be treated as disposable verify!
            wt_tricky = Path(create_worktree(repo, "keel-verify-read")["path"])
            self.assertFalse(is_disposable_verify_path(wt_tricky))
            self.assertEqual(detect_agent_mode(cwd=str(wt_tricky), env={}), "write")
            self.assertIsNotNone(evaluate_command("git checkout -- app.py", mode="read", cwd=wt_tricky))
            self.assertIsNotNone(evaluate_command("git commit -m 'bad'", mode="read", cwd=wt_tricky))

            # Inside disposable `/tmp/keel-verify-*` clone: local working-tree Canary commands ARE allowed,
            # while `git push` and `-C <real_repo>` mutations remain blocked!
            verify_scratch = Path(tempfile.mkdtemp(prefix="keel-verify-unit-"))
            try:
                clone_dir, _ = create_disposable_clone(repo, head_sha, verify_scratch)
                self.assertIsNone(evaluate_command("git checkout -- app.py", mode="read", cwd=clone_dir))
                self.assertIsNone(evaluate_command("git restore .", mode="read", cwd=clone_dir))
                self.assertIsNone(evaluate_command(f"cd {clone_dir} && git reset --hard HEAD", mode="read", cwd=repo))
                self.assertIsNotNone(evaluate_command("git push origin HEAD", mode="read", cwd=clone_dir))
                self.assertIsNotNone(evaluate_command(f"git -C {repo} reset --hard HEAD", mode="read", cwd=clone_dir))
            finally:
                import shutil

                shutil.rmtree(verify_scratch, ignore_errors=True)

    def test_write_and_main_protected_default_branch_seatbelt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _init_repo(repo)
            wt_info = create_worktree(repo, "feat-1")
            wt_path = Path(wt_info["path"])

            # 1. `mode == "write"`: blocks mutating git in primary checkout; allows in worktree; blocks targeting main
            self.assertIsNotNone(evaluate_command("git commit -m 'bad'", mode="write", cwd=repo))
            self.assertIsNotNone(evaluate_command("git add app.py", mode="write", cwd=repo))
            self.assertIsNone(evaluate_command("git status && git log -n 1", mode="write", cwd=repo))

            self.assertIsNone(evaluate_command("git add app.py && git commit -m 'ok'", mode="write", cwd=wt_path))
            self.assertIsNone(evaluate_command(f"cd {wt_path} && git commit -m 'ok'", mode="write", cwd=repo))
            self.assertIsNone(evaluate_command(f"git -C {wt_path} commit -m 'ok'", mode="write", cwd=repo))
            self.assertIsNone(evaluate_command("git rebase main", mode="write", cwd=wt_path))
            self.assertIsNone(evaluate_command("git checkout main -- app.py", mode="write", cwd=wt_path))
            self.assertIsNone(evaluate_command("git checkout -b task/sub main", mode="write", cwd=wt_path))

            for bad_target in (
                "git branch -f main HEAD",
                "git branch -D main",
                "git update-ref refs/heads/main HEAD",
                "git fetch . HEAD:main",
                "git checkout main",
                "git switch main",
                "git switch -C master",
            ):
                with self.subTest(bad_target=bad_target):
                    self.assertIsNotNone(evaluate_command(bad_target, mode="write", cwd=wt_path))

            # 2. `mode == "main"` on `main`: blocks merge/rebase/cherry-pick/reset --hard/non-ff pull
            self.assertIsNotNone(evaluate_command("git merge task/feat-1", mode="main", cwd=repo))
            self.assertIsNotNone(evaluate_command("git merge --ff-only task/feat-1", mode="main", cwd=repo))
            self.assertIsNotNone(evaluate_command("git rebase task/feat-1", mode="main", cwd=repo))
            self.assertIsNotNone(evaluate_command("git cherry-pick task/feat-1", mode="main", cwd=repo))
            self.assertIsNotNone(evaluate_command("git reset --hard HEAD~1", mode="main", cwd=repo))
            self.assertIsNone(evaluate_command("git reset HEAD app.py", mode="main", cwd=repo))
            self.assertIsNotNone(evaluate_command("git pull origin main", mode="main", cwd=repo))
            self.assertIsNone(evaluate_command("git pull --ff-only origin main", mode="main", cwd=repo))

            # 3. `mode == "main"` `git commit` on `main`: ONLY non-empty `docs/tasks/` commits allowed!
            # Empty commit -> blocked
            self.assertIsNotNone(evaluate_command("git commit --allow-empty -m 'empty'", mode="main", cwd=repo))

            # Single-command chained `git add docs/tasks/... && git commit` -> allowed!
            spec_path = repo / "docs" / "tasks" / "active" / "20261117-feat-1.md"
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            spec_path.write_text("# Spec\n\n## Goal & Why\nTest.\n", encoding="utf-8")
            self.assertIsNone(
                evaluate_command(
                    "git add docs/tasks/active/20261117-feat-1.md && git commit -m 'docs(tasks): open spec'",
                    mode="main",
                    cwd=repo,
                )
            )

            #Actually stage and commit the task file, then verify `-am` works when ONLY `docs/tasks/` is modified
            subprocess.run(["git", "-C", str(repo), "add", "docs/tasks/active/20261117-feat-1.md"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "docs(tasks): open spec"], check=True, capture_output=True
            )
            spec_path.write_text("# Spec\n\n## Goal & Why\nUpdated.\n", encoding="utf-8")
            self.assertIsNone(evaluate_command("git commit -am 'docs(tasks): refine'", mode="main", cwd=repo))

            # If a tracked non-`docs/tasks/` file (`app.py`) is ALSO modified, `-a`/`-am` MUST be rejected!
            (repo / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
            self.assertIsNotNone(evaluate_command("git commit -am 'sneak code'", mode="main", cwd=repo))
            # Explicit non-`docs/tasks/` pathspec -> blocked
            self.assertIsNotNone(evaluate_command("git commit -m 'sneak' app.py", mode="main", cwd=repo))
            # Explicit `docs/tasks/` pathspec (without `-i`) -> allowed because it only commits `docs/tasks/...`!
            self.assertIsNone(
                evaluate_command(
                    "git commit -m 'docs(tasks): explicit' docs/tasks/active/20261117-feat-1.md",
                    mode="main",
                    cwd=repo,
                )
            )
            # Chained `git mv docs/tasks/... && git commit` on `main` -> allowed!
            self.assertIsNone(
                evaluate_command(
                    "git mv docs/tasks/active/20261117-feat-1.md docs/tasks/archive/20261117-feat-1.md && git commit -m 'archive'",
                    mode="main",
                    cwd=repo,
                )
            )
            # Staged code file -> blocked
            subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
            self.assertIsNotNone(evaluate_command("git commit -m 'staged code'", mode="main", cwd=repo))

            # Staged rename (`git mv app.py docs/tasks/active/app.py`) MUST be caught via `--no-renames` and blocked!
            subprocess.run(["git", "-C", str(repo), "reset", "--hard", "HEAD"], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repo), "mv", "app.py", "docs/tasks/active/app.py"], check=True, capture_output=True
            )
            self.assertIsNotNone(evaluate_command("git commit -m 'sneak git mv'", mode="main", cwd=repo))
            self.assertIsNotNone(evaluate_command("git commit -am 'sneak git mv -a'", mode="main", cwd=repo))
            self.assertIsNotNone(
                evaluate_command("git commit -im 'sneak include' docs/tasks/active/app.py", mode="main", cwd=repo)
            )

    def test_detect_agent_mode_from_public_cwd_and_env(self) -> None:
        self.assertEqual(detect_agent_mode(cwd="/repo/.agents/worktrees/feat-a", env={}), "write")
        self.assertEqual(detect_agent_mode(cwd="/tmp/keel-verify-feat-abc/checkout", env={}), "read")
        self.assertEqual(detect_agent_mode(cwd="/repo", env={"KEEL_AGENT_MODE": "read"}), "read")
        self.assertEqual(detect_agent_mode(cwd="/repo", env={}), "main")
        line_count = len((ROOT / "scripts" / "keel_lib" / "guard_rules.py").read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(line_count, 650)


class PreToolUseHookBinaryTests(unittest.TestCase):
    def test_antigravity_and_codex_protocols_and_last_user_input_binding(self) -> None:
        hook = ROOT / "hooks" / "pre-tool-use"
        with tempfile.TemporaryDirectory() as tmp:
            clean_base_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("KEEL_AGENT_MODE", "DEVSTANDARD_AGENT_MODE", "DEVSTANDARD_ROLE")
            }
            env = dict(clean_base_env, PLUGIN_DATA=str(Path(tmp) / "plugin-data"))
            run_dir = Path(tmp) / ".agents" / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            brief = run_dir / "brief.md"
            brief.write_text("# Brief\n", encoding="utf-8")
            (run_dir / "run.json").write_text(
                json.dumps({"executor": "agy", "mode": "read", "scratch_verify": True}), encoding="utf-8"
            )

            # 0. Main orchestrator session runs a command FIRST, then inspects brief.md -> NEVER poisons main session!
            subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(
                    {
                        "conversationId": "conv-main-orchestrator",
                        "cwd": "/normal/repo/path",
                        "toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}},
                    }
                ),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(
                    {
                        "conversationId": "conv-main-orchestrator",
                        "cwd": "/normal/repo/path",
                        "toolCall": {"name": "read_file", "args": {"Path": str(brief)}},
                    }
                ),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            res_main_push = subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(
                    {
                        "conversationId": "conv-main-orchestrator",
                        "cwd": "/normal/repo/path",
                        "toolCall": {"name": "run_command", "args": {"CommandLine": "git push origin main"}},
                    }
                ),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(res_main_push.stdout), {"decision": "allow"})

            # 1. Fresh subagent calls `run_command` BEFORE `read_file`, with `lastUserInput` referencing `brief.md`
            # -> binds `conv_id` via `lastUserInput` immediately and blocks `git push`!
            res_last_input = subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(
                    {
                        "conversationId": "conv-subagent-lastinput",
                        "cwd": "/normal/repo/path",
                        "lastUserInput": f"Read `{brief}` in full first and execute that assignment.",
                        "toolCall": {"name": "run_command", "args": {"CommandLine": "git push origin main"}},
                    }
                ),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(res_last_input.stdout)["decision"], "deny")

            # 2. Fresh subagent reads `brief.md` via `read_file` on turn 1 -> binds `conv_id` to `read` mode!
            res_read = subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(
                    {
                        "conversationId": "conv-subagent-readonly",
                        "cwd": "/normal/repo/path",
                        "toolCall": {"name": "read_file", "args": {"Path": str(brief)}},
                    }
                ),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(res_read.stdout), {"decision": "allow"})

            res_sub_cmd = subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(
                    {
                        "conversationId": "conv-subagent-readonly",
                        "cwd": "/normal/repo/path",
                        "toolCall": {"name": "run_command", "args": {"CommandLine": "keel branch land x --execute"}},
                    }
                ),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(res_sub_cmd.stdout)["decision"], "deny")

            # 3. Open stdin pipe latency test (<0.8s)
            proc = subprocess.Popen(
                [sys.executable, str(hook)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
            )
            assert proc.stdin is not None and proc.stdout is not None
            start = time.monotonic()
            proc.stdin.write(
                json.dumps({"cwd": "/tmp", "toolCall": {"name": "write_file", "args": {"Content": "git push -f"}}})
            )
            proc.stdin.flush()
            out_line = proc.stdout.readline()
            elapsed = time.monotonic() - start
            proc.stdin.close()
            proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
            proc.wait(timeout=2)
            self.assertEqual(json.loads(out_line), {"decision": "allow"})
            self.assertLess(elapsed, 0.8)

            # 4. Codex Bash protocol deny
            res_codex = subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(
                    {"cwd": "/tmp", "tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}}
                ),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(
                json.loads(res_codex.stdout)["hookSpecificOutput"]["permissionDecision"],
                "deny",
            )

            # 5. Repository path containing spaces binds `mode="read"` on Turn 1 AND upgrades unlocked fallback on read_file
            space_run = Path(tmp) / "project with spaces" / ".agents" / "runs" / "run-space"
            space_run.mkdir(parents=True)
            space_brief = space_run / "brief.md"
            space_brief.write_text("# Space Brief\n", encoding="utf-8")
            (space_run / "run.json").write_text(
                json.dumps({"executor": "agy", "mode": "read", "scratch_verify": False}), encoding="utf-8"
            )
            res_space_turn1 = subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(
                    {
                        "conversationId": "conv-space-turn1",
                        "cwd": str(Path(tmp) / "project with spaces"),
                        "lastUserInput": f"Read `{space_brief}` in full first and execute that assignment.",
                        "toolCall": {"name": "run_command", "args": {"CommandLine": "git push origin main"}},
                    }
                ),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(res_space_turn1.stdout)["decision"], "deny")

            # Non-shell tool on Turn 1 without brief path -> unlocked fallback; Turn 2 `read_file(space_brief)` upgrades!
            subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(
                    {
                        "conversationId": "conv-space-upgrade",
                        "cwd": str(Path(tmp) / "project with spaces"),
                        "toolCall": {"name": "list_dir", "args": {}},
                    }
                ),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(
                    {
                        "conversationId": "conv-space-upgrade",
                        "cwd": str(Path(tmp) / "project with spaces"),
                        "toolCall": {"name": "read_file", "args": {"Path": str(space_brief)}},
                    }
                ),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            res_after_upgrade = subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(
                    {
                        "conversationId": "conv-space-upgrade",
                        "cwd": str(Path(tmp) / "project with spaces"),
                        "toolCall": {"name": "run_command", "args": {"CommandLine": "git push origin main"}},
                    }
                ),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(res_after_upgrade.stdout)["decision"], "deny")
            self.assertTrue((Path(tmp) / "plugin-data" / "conv_modes.lock").is_file())

            # 5a. Main session calling `read_file("SKILL.md")` -> `run_command("git status")` ->
            # `read_file(".agents/runs/.../brief.md")` -> `run_command("git push --dry-run origin main")` stays `"main"`!
            for ev in (
                {
                    "conversationId": "conv-main-read-first",
                    "cwd": "/normal/repo/path",
                    "toolCall": {"name": "read_file", "args": {"Path": "/normal/repo/path/SKILL.md"}},
                },
                {
                    "conversationId": "conv-main-read-first",
                    "cwd": "/normal/repo/path",
                    "toolCall": {"name": "run_command", "args": {"CommandLine": "git status"}},
                },
                {
                    "conversationId": "conv-main-read-first",
                    "cwd": "/normal/repo/path",
                    "toolCall": {"name": "read_file", "args": {"Path": str(brief)}},
                },
            ):
                subprocess.run(
                    [sys.executable, str(hook)],
                    input=json.dumps(ev),
                    env=env,
                    text=True,
                    capture_output=True,
                    check=True,
                )
            res_main_read_first = subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(
                    {
                        "conversationId": "conv-main-read-first",
                        "cwd": "/normal/repo/path",
                        "toolCall": {
                            "name": "run_command",
                            "args": {"CommandLine": "git push --dry-run origin main"},
                        },
                    }
                ),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(res_main_read_first.stdout)["decision"], "allow")

            # 5b. Subagent calling a non-brief tool on Turn 1 -> `run_command("git push --dry-run origin main")`
            # on Turn 2 with `lastUserInput` pointing to `brief.md` binds to `"read"` and denies!
            subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(
                    {
                        "conversationId": "conv-subagent-turn2-bind",
                        "cwd": "/normal/repo/path",
                        "toolCall": {"name": "read_file", "args": {"Path": "/normal/repo/path/README.md"}},
                    }
                ),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            res_subagent_turn2 = subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(
                    {
                        "conversationId": "conv-subagent-turn2-bind",
                        "cwd": "/normal/repo/path",
                        "lastUserInput": f"Read `{brief}` in full and follow its instructions.",
                        "toolCall": {
                            "name": "run_command",
                            "args": {"CommandLine": "git push --dry-run origin main"},
                        },
                    }
                ),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(res_subagent_turn2.stdout)["decision"], "deny")


class EnvHelpersTests(unittest.TestCase):
    def test_run_cmd_missing_executable_raises_keel_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_executable = str(Path(tmp) / "missing-executable")
            for cwd in (None, tmp):
                with self.subTest(cwd=cwd):
                    with self.assertRaisesRegex(KeelError, r"Executable not found \(127\)") as caught:
                        run_cmd([missing_executable], cwd=cwd, check=True)
                    self.assertIn(missing_executable, str(caught.exception))
                    self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)

    def test_run_cmd_missing_executable_without_check_returns_127(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            argv = [str(Path(tmp) / "missing-executable")]
            for cwd in (None, tmp):
                with self.subTest(cwd=cwd):
                    result = run_cmd(argv, cwd=cwd, check=False)
                    self.assertEqual(result.args, argv)
                    self.assertEqual(result.returncode, 127)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(argv[0], result.stderr)
                    self.assertNotIn("Working directory", result.stderr)

    def test_run_cmd_missing_cwd_and_non_directory_cwd_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_cwd = Path(tmp) / "missing-cwd"
            with self.assertRaisesRegex(KeelError, r"Working directory \(cwd\) not found") as caught:
                run_cmd([sys.executable, "-c", "pass"], cwd=missing_cwd, check=True)
            self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)
            res_missing = run_cmd([sys.executable, "-c", "pass"], cwd=missing_cwd, check=False)
            self.assertEqual(res_missing.returncode, 127)
            self.assertIn("Working directory (cwd) not found", res_missing.stderr)

            file_path = Path(tmp) / "file"
            file_path.write_text("not a directory", encoding="utf-8")
            with self.assertRaisesRegex(KeelError, r"Working directory \(cwd\)") as caught_nd:
                run_cmd([sys.executable, "-c", "pass"], cwd=file_path, check=True)
            self.assertIsInstance(caught_nd.exception.__cause__, NotADirectoryError)
            res_nd = run_cmd([sys.executable, "-c", "pass"], cwd=file_path, check=False)
            self.assertEqual(res_nd.returncode, 127)
            self.assertNotIn("Executable not found", res_nd.stderr)

    def test_run_process_group_diagnostics_and_large_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_exec = str(Path(tmp) / "missing-exec")
            rc, err = run_process_group([missing_exec], cwd=tmp)
            self.assertEqual(rc, 127)
            self.assertIn("Executable not found", err)

            missing_cwd = Path(tmp) / "missing-cwd"
            rc2, err2 = run_process_group([sys.executable, "-c", "pass"], cwd=missing_cwd)
            self.assertEqual(rc2, 127)
            self.assertIn("Working directory (cwd) not found", err2)

            loop = Path(tmp) / "loop"
            loop.symlink_to(loop.name)
            with self.assertRaises(KeelError):
                safe_resolve(loop)
            rc3, err3 = run_process_group([sys.executable, "-c", "pass"], cwd=loop)
            self.assertEqual(rc3, 127)
            self.assertIn("Working directory (cwd)", err3)

            file_path = Path(tmp) / "file"
            file_path.write_text("x", encoding="utf-8")
            with self.assertRaises(NotADirectoryError):
                run_cmd([str(file_path / "child")], cwd=tmp, check=True)

        lines: list[str] = []
        rc_ok, stderr_text = run_process_group(
            [sys.executable, "-c", "import sys; sys.stderr.write('E' * (1024 * 512)); sys.stderr.flush(); print('done')"],
            on_stdout_line=lines.append,
        )
        self.assertEqual(rc_ok, 0)
        self.assertEqual("".join(lines).strip(), "done")
        self.assertEqual(len(stderr_text), 1024 * 512)

    def test_path_identity_and_tool_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "real"
            child = base / "nested"
            child.mkdir(parents=True)
            alias = Path(tmp) / "alias"
            alias.symlink_to(base)
            self.assertTrue(same_path(base, alias))
            self.assertTrue(is_subpath(alias / "nested", base))
        env = tool_env({"CUSTOM_KEY": "val"})
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["CUSTOM_KEY"], "val")


if __name__ == "__main__":
    unittest.main()
