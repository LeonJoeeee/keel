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
from keel_lib.env_gh import decode_json_stream, gh_api, run_process_group, safe_resolve, tool_env  # noqa: E402
from keel_lib.guard_rules import detect_agent_mode, evaluate_command  # noqa: E402


class GuardRulesTests(unittest.TestCase):
    def test_false_positive_regressions_never_blocked(self) -> None:
        safe_commands = [
            "pytest tests/merge/ -q",
            "python3 src/merge.py --check",
            "git add src/merge.py && git commit -m 'fix merge helper'",
            "git push -u origin task/fix-timeout && gh pr create --base main --title 'Fix'",
            "git rebase origin/main && git push --force-with-lease origin task/fix-timeout",
            "git push origin v0.1.0",
            'echo "gh pr merge \\"42\\" should not run"',
            "cat <<'EOF' > /tmp/note.txt\ngh pr merge 42\ngit push origin main\nEOF",
            "echo '<<EOF' && echo ok",
            "# comment mentioning <<EOF\necho ok",
            "printf $'it\\'s\\n' && echo '; gh pr merge 7'",
        ]
        for mode in ("main", "write"):
            for cmd in safe_commands:
                with self.subTest(mode=mode, cmd=cmd):
                    self.assertIsNone(evaluate_command(cmd, mode=mode))

    def test_dangerous_commands_and_shell_compound_bypasses_blocked(self) -> None:
        adversarial_bypasses = [
            "gh pr merge 42 --squash",
            "gh pr merge>/tmp/out 42",
            "gh pr&>/dev/null merge 42",
            "gh >|/dev/null pr merge 42",
            "gh >&1tmp pr merge 42",
            "gh {fd}>/dev/null pr merge 42",
            "gh $'pr' merge 42",
            'gh $"pr" merge 42',
            "gh pr -R owner/repo merge 42",
            "# comment \\\ngh pr merge 42",
            "cat <<'EOF' >/dev/null\nmacro \\\nEOF\ngh pr merge 42",
            "sudo gh -R owner/repo pr merge 42",
            "echo $(gh pr merge 42)",
            "if true; then gh pr merge 42; fi",
            "{ gh pr merge 42; }",
            "! gh pr merge 42",
            "</dev/null gh pr merge 42",
            "gh \\\npr \\\nmerge 42",
            "cat <<EOF > /tmp/x\n$(gh pr merge 42)\nEOF",
            "echo '<<EOF'\ngh pr merge 42",
            "# <<EOF\ngh pr merge 42",
            "git push origin main",
            "git push origin HEAD:heads/main",
            "git -c remote.origin.push=HEAD:refs/heads/main push origin",
            "git -c push.default=matching push origin",
            "git push origin :",
            "git push origin 'refs/heads/*:refs/heads/*'",
            "git push --all origin",
            "git push --mirror origin",
            "git -C /tmp/repo push --force origin HEAD:refs/heads/master",
        ]
        for cmd in adversarial_bypasses:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(evaluate_command(cmd, mode="main"))

        self.assertIsNotNone(evaluate_command("keel pr merge 12 --execute", mode="write"))
        self.assertIsNone(evaluate_command("keel pr merge 12", mode="write"))

    def test_implicit_git_push_checks_current_and_upstream_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.com"], check=True)
            (repo / "f.txt").write_text("1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

            # Checked out on `main`: implicit `git push`, `git push origin`, `git push origin HEAD` are blocked!
            for cmd in ("git push", "git push origin", "git push origin HEAD"):
                self.assertIsNotNone(evaluate_command(cmd, mode="write", cwd=repo))

            # Checked out on `task/feat`: implicit `git push` and `git push origin HEAD` are allowed!
            subprocess.run(["git", "-C", str(repo), "checkout", "-b", "task/feat"], check=True, capture_output=True)
            for cmd in ("git push", "git push origin", "git push origin HEAD"):
                self.assertIsNone(evaluate_command(cmd, mode="write", cwd=repo))

            # Tracking `refs/heads/main` upstream from `task/feat`: blocked!
            subprocess.run(["git", "-C", str(repo), "config", "branch.task/feat.merge", "refs/heads/main"], check=True)
            self.assertIsNotNone(evaluate_command("git push", mode="write", cwd=repo))

            # Reset branch merge, configure `remote.origin.push = HEAD:refs/heads/main` -> blocked!
            subprocess.run(["git", "-C", str(repo), "config", "--unset", "branch.task/feat.merge"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "remote.origin.push", "HEAD:refs/heads/main"], check=True)
            self.assertIsNotNone(evaluate_command("git push origin", mode="write", cwd=repo))

    def test_read_mode_blocks_remote_mutations_allows_get_params_and_graphql_vars(self) -> None:
        self.assertIsNotNone(evaluate_command("git push origin task/x", mode="read"))
        self.assertIsNotNone(evaluate_command("keel pr publish-verdict 5 --verdict-file /tmp/v.json --head-sha " + "a" * 40, mode="read"))
        self.assertIsNotNone(evaluate_command("python3 -B /opt/keel/scripts/keel pr verify 5 --pub", mode="read"))
        self.assertIsNotNone(evaluate_command("gh pr -R o/r comment 5 --body 'lgtm'", mode="read"))
        self.assertIsNotNone(evaluate_command("gh pr comment 5 --body 'lgtm'", mode="read"))
        self.assertIsNotNone(evaluate_command("gh pr review 5 --approve", mode="read"))
        self.assertIsNotNone(evaluate_command("gh api repos/o/r/issues/1/comments -f body=hi", mode="read"))
        self.assertIsNotNone(evaluate_command("gh api -X DELETE repos/o/r/issues/1/comments/99", mode="read"))
        self.assertIsNotNone(evaluate_command("gh api -XPOST repos/o/r/issues/1/comments", mode="read"))
        self.assertIsNotNone(evaluate_command("gh api -f body=hi repos/o/r/issues/1/comments", mode="read"))
        self.assertIsNotNone(evaluate_command("gh api --input /tmp/body.json repos/o/r/issues/1/comments", mode="read"))
        self.assertIsNotNone(
            evaluate_command(
                "gh api graphql -f query='mutation { closeIssue(input:{issueId:\"1\"}) { clientMutationId } }'",
                mode="read",
            )
        )
        self.assertIsNone(evaluate_command("gh pr view 5 --json title,headRefOid", mode="read"))
        self.assertIsNone(evaluate_command("gh api repos/o/r/pulls/5", mode="read"))
        self.assertIsNone(evaluate_command("gh api repos/o/r/issues -X GET -f state=open", mode="read"))
        self.assertIsNone(
            evaluate_command(
                "gh api --jq '.data' graphql -f query='query($o:String!,$r:String!){repository(owner:$o,name:$r){id}}' -F o=a -F r=b",
                mode="read",
            )
        )

    def test_detect_agent_mode_from_public_cwd_and_env(self) -> None:
        self.assertEqual(detect_agent_mode(cwd="/repo/.agents/worktrees/feat-a", env={}), "write")
        self.assertEqual(detect_agent_mode(cwd="/tmp/keel-verify-pr9-abc/checkout", env={}), "read")
        self.assertEqual(detect_agent_mode(cwd="/repo", env={"KEEL_AGENT_MODE": "read"}), "read")
        self.assertEqual(detect_agent_mode(cwd="/repo", env={}), "main")


class PreToolUseHookBinaryTests(unittest.TestCase):
    def test_antigravity_and_codex_protocols_and_conv_mode_binding(self) -> None:
        hook = ROOT / "hooks" / "pre-tool-use"
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PLUGIN_DATA=str(Path(tmp) / "plugin-data"))
            run_dir = Path(tmp) / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            brief = run_dir / "brief.md"
            brief.write_text("# Brief\n", encoding="utf-8")
            (run_dir / "run.json").write_text(json.dumps({"executor": "agy", "mode": "read"}), encoding="utf-8")

            # 0. Main orchestrator session runs a normal command FIRST, then inspects brief.md -> MUST NOT poison main session!
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
                        "toolCall": {"name": "run_command", "args": {"CommandLine": "gh pr comment 1 --body ok"}},
                    }
                ),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(json.loads(res_main_push.stdout), {"decision": "allow"})

            # 1. Fresh subagent reads brief.md on turn 1 -> binds conversationId to `read` mode automatically!
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

            # 2. Same subagent later calls `gh pr comment` in `/normal/repo/path` -> DENIED by bound `read` mode!
            res_sub_cmd = subprocess.run(
                [sys.executable, str(hook)],
                input=json.dumps(
                    {
                        "conversationId": "conv-subagent-readonly",
                        "cwd": "/normal/repo/path",
                        "toolCall": {"name": "run_command", "args": {"CommandLine": "gh pr comment 1 --body hi"}},
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
                json.dumps({"cwd": "/tmp", "toolCall": {"name": "write_file", "args": {"Content": "gh pr merge 99"}}})
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
                input=json.dumps({"cwd": "/tmp", "tool_name": "Bash", "tool_input": {"command": "gh pr merge 1"}}),
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(
                json.loads(res_codex.stdout)["hookSpecificOutput"]["permissionDecision"],
                "deny",
            )


class EnvAndGhApiTests(unittest.TestCase):
    def test_large_stderr_does_not_deadlock_run_process_group(self) -> None:
        lines: list[str] = []
        rc, stderr_text = run_process_group(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('E' * (1024 * 512)); sys.stderr.flush(); print('done')",
            ],
            on_stdout_line=lines.append,
        )
        self.assertEqual(rc, 0)
        self.assertEqual("".join(lines).strip(), "done")
        self.assertEqual(len(stderr_text), 1024 * 512)

    def test_decode_paginated_streams(self) -> None:
        self.assertEqual(decode_json_stream("[1, 2][3, 4]"), [1, 2, 3, 4])
        pages = decode_json_stream('{"check_runs": [{"id": 1}]}\n{"check_runs": [{"id": 2}]}')
        self.assertEqual(len(pages), 2)

    def test_gh_api_retries_only_idempotent_transient_reads(self) -> None:
        calls: list[list[str]] = []

        def flaky_read(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if len(calls) < 3:
                return subprocess.CompletedProcess(argv, 1, "", 'Get "https://api.github.com/...": EOF')
            return subprocess.CompletedProcess(argv, 0, '{"ok": true}', "")

        out = gh_api("repos/o/r/pulls/1", ("--paginate",), runner=flaky_read)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(len(calls), 3)

        post_calls = 0

        def flaky_post(argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            nonlocal post_calls
            post_calls += 1
            return subprocess.CompletedProcess(argv, 1, "", 'Post "https://api.github.com/...": EOF')

        with self.assertRaises(KeelError):
            gh_api("repos/o/r/issues/1/comments", ("-X", "POST", "-f", "body=hi"), runner=flaky_post)
        self.assertEqual(post_calls, 1)

    def test_safe_resolve_catches_symlink_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "loop_a"
            b = Path(tmp) / "loop_b"
            os.symlink(b, a)
            os.symlink(a, b)
            with self.assertRaises(KeelError):
                safe_resolve(a)

    def test_tool_env_sets_noninteractive_defaults(self) -> None:
        env = tool_env()
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GH_PROMPT_DISABLED"], "1")
        from keel_lib.env_gh import run_cmd
        res = run_cmd(["nonexistent-keel-binary-xyz"], check=False)
        self.assertEqual(res.returncode, 127)
        with self.assertRaises(KeelError):
            run_cmd(["nonexistent-keel-binary-xyz"], check=True)


if __name__ == "__main__":
    unittest.main()
