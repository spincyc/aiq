from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from aiq.cli import _classify_error, build_parser
from aiq.integrations.codex import CodexIntegrationError


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
JSON_COMMAND_PATHS = {
    tuple(name.split("."))
    for name in """
        capability.list capability.show claim.release config.check config.show
        inbox.apply inbox.claim inbox.fail inbox.list inbox.needs-input ingest
        integration.check integration.install integration.list integration.plan
        integration.print integration.uninstall journal.check journal.destroy
        journal.export journal.init journal.path journal.snapshot queue.next
        queue.peek task.list task.show
    """.split()
}


def implemented_json_paths() -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()

    def visit(parser: object, prefix: tuple[str, ...] = ()) -> None:
        actions = getattr(parser, "_actions")
        subparsers = [
            action
            for action in actions
            if action.__class__.__name__ == "_SubParsersAction"
        ]
        if subparsers:
            for name, child in subparsers[0].choices.items():
                visit(child, (*prefix, name))
        elif any("--json" in action.option_strings for action in actions):
            result.add(prefix)

    visit(build_parser())
    return result


class CliProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "repository"
        for path in (
            self.root / "home",
            self.root / "config",
            self.root / "state",
            self.root / "codex",
            self.repository,
        ):
            path.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.repository)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.launcher = self.root / "bin" / "aiq"
        self.launcher.parent.mkdir()
        self.launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.launcher.chmod(0o700)
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("AIQ_", "GIT_"))
            and key
            not in {
                "CODEX_HOME",
                "PYTHONPATH",
                "XDG_CONFIG_HOME",
                "XDG_STATE_HOME",
            }
        }
        self.environment.update(
            {
                "CODEX_HOME": str(self.root / "codex"),
                "HOME": str(self.root / "home"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(SOURCE_ROOT),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
            }
        )
        self.scope = (
            "--scope",
            "repo",
            "--cwd",
            str(self.repository),
            "--json",
        )
        self.exercised: set[tuple[str, ...]] = set()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_aiq(
        self,
        *arguments: str,
        input_text: str | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "aiq", *arguments],
            cwd=self.repository,
            env=self.environment if environment is None else environment,
            input=input_text,
            text=True,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def ok(
        self,
        *arguments: str,
        input_text: str | None = None,
    ) -> dict[str, object]:
        path = next(
            (
                tuple(arguments[:length])
                for length in (2, 1)
                if tuple(arguments[:length]) in JSON_COMMAND_PATHS
            ),
            None,
        )
        self.assertIsNotNone(path, arguments)
        self.exercised.add(path)
        completed = self.run_aiq(*arguments, input_text=input_text)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout.count("\n"), 1, completed.stdout)
        payload = json.loads(completed.stdout)
        self.assertIs(type(payload), dict)
        self.assertEqual(payload.get("v"), 1)
        return payload

    def assert_error(
        self,
        completed: subprocess.CompletedProcess[str],
        exit_code: int,
        code: str,
    ) -> None:
        self.assertEqual(completed.returncode, exit_code, completed)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr.count("\n"), 1, completed.stderr)
        payload = json.loads(completed.stderr)
        self.assertEqual(set(payload), {"code", "error", "status", "v"})
        self.assertEqual(
            (payload["v"], payload["status"], payload["code"]),
            (1, "error", code),
        )
        self.assertIs(type(payload["error"]), str)
        self.assertNotIn("\n", payload["error"])

    def test_every_json_command_uses_protocol_v1(self) -> None:
        self.ok("config", "show", "--cwd", str(self.repository), "--json")
        self.ok("config", "check", "--cwd", str(self.repository), "--json")
        self.ok("capability", "list", "--json")
        self.ok("capability", "show", "journal.check", "--json")

        self.ok("integration", "list", "--json")
        self.ok(
            "integration", "print", "codex", "--user",
            "--launcher", str(self.launcher), "--json",
        )
        plan = self.ok(
            "integration", "plan", "codex", "--user",
            "--launcher", str(self.launcher), "--json",
        )
        self.ok(
            "integration", "install", "codex", "--user",
            "--launcher", str(self.launcher),
            "--plan-token", str(plan["plan_token"]), "--json",
        )
        self.ok(
            "integration", "check", "codex", "--user",
            "--launcher", str(self.launcher), "--json",
        )
        self.ok("integration", "uninstall", "codex", "--user", "--json")

        path = self.ok("journal", "path", *self.scope)
        self.assertEqual(set(path), {"scope", "v"})
        self.ok("journal", "init", *self.scope)
        ingested = self.ok(
            "ingest", "--message", "Create a protocol-test task",
            "--source", "protocol-test", *self.scope,
        )
        message_id = str(ingested["message_id"])
        self.ok("inbox", "list", *self.scope)
        claimed = self.ok(
            "inbox", "claim", message_id,
            "--owner", "protocol-test", *self.scope,
        )
        effects = json.dumps(
            {
                "v": 1,
                "expect": {},
                "effects": [
                    ["create", "$task", {"title": "Protocol task", "priority": 7}]
                ],
            },
            separators=(",", ":"),
        )
        applied = self.ok(
            "inbox", "apply", message_id,
            "--claim", claimed["claim"]["claim_id"],
            "--effects", "-", *self.scope, input_text=effects,
        )
        task_id = str(applied["aliases"]["$task"])
        self.ok("task", "list", *self.scope)
        task = self.ok("task", "show", task_id, *self.scope)
        self.assertEqual(set(task), {"task", "v"})
        self.ok("queue", "peek", *self.scope)
        next_result = self.ok(
            "queue", "next", "--owner", "protocol-test", *self.scope,
        )
        self.assertEqual(set(next_result), {"items", "v"})
        self.assertEqual(len(next_result["items"]), 1)
        item = next_result["items"][0]
        self.assertEqual(set(item), {"claim", "task"})
        self.assertNotIn("claim", item["task"])
        self.ok("claim", "release", item["claim"]["claim_id"], *self.scope)

        for command, status in (("needs-input", "needs_input"), ("fail", "failed")):
            receipt = self.ok(
                "ingest", "--message", f"Message to {command}", *self.scope,
            )
            claim = self.ok(
                "inbox", "claim", str(receipt["message_id"]),
                "--owner", "protocol-test", *self.scope,
            )
            disposed = self.ok(
                "inbox", command, str(receipt["message_id"]),
                "--claim", claim["claim"]["claim_id"],
                "--reason", "protocol coverage", *self.scope,
            )
            self.assertEqual(disposed["status"], status)

        self.ok("journal", "check", *self.scope)
        self.ok("journal", "snapshot", *self.scope)
        self.ok(
            "journal", "export", str(self.root / "journal.jsonl"), *self.scope,
        )
        destroy = self.ok("journal", "destroy", "--plan", *self.scope)
        self.ok(
            "journal", "destroy",
            "--confirm", str(destroy["confirmation_token"]), *self.scope,
        )
        self.assertEqual(implemented_json_paths(), JSON_COMMAND_PATHS)
        self.assertEqual(self.exercised, JSON_COMMAND_PATHS)

    def test_error_envelope_exit_classes_and_stream_separation(self) -> None:
        self.assert_error(
            self.run_aiq("task", "show", *self.scope),
            2,
            "invalid_argument",
        )
        self.assert_error(
            self.run_aiq("task", "show", "TASK-999", *self.scope),
            3,
            "not_found",
        )
        event = {
            "v": 1,
            "source": "protocol-test",
            "content": "first",
            "idempotency_key": "protocol-test:conflict",
            "cwd": str(self.repository),
        }
        first = self.run_aiq(
            "ingest", "--event-json", "-", *self.scope,
            input_text=json.dumps(event),
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        event["content"] = "different"
        self.assert_error(
            self.run_aiq(
                "ingest", "--event-json", "-", *self.scope,
                input_text=json.dumps(event),
            ),
            4,
            "state_conflict",
        )

        connection = sqlite3.connect(
            self.repository / ".git" / "aiq" / "journal.sqlite3"
        )
        try:
            connection.execute(
                "UPDATE journal_metadata SET value='999' "
                "WHERE key='schema_version'"
            )
            connection.commit()
        finally:
            connection.close()
        self.assert_error(
            self.run_aiq("journal", "check", *self.scope),
            5,
            "schema_incompatible",
        )
        self.assert_error(
            self.run_aiq(
                "integration", "plan", "codex", "--user",
                "--launcher", str(self.root / "missing-aiq"), "--json",
            ),
            6,
            "unsupported_environment",
        )
        self.assert_error(
            self.run_aiq(
                "integration", "plan", "codex", "--user",
                "--launcher", "relative-aiq", "--json",
            ),
            2,
            "invalid_argument",
        )
        self.assert_error(
            self.run_aiq(
                "integration", "plan", "codex", "--user", "--json",
                environment={**self.environment, "PATH": ""},
            ),
            6,
            "unsupported_environment",
        )
        self.assert_error(
            self.run_aiq(
                "integration", "plan", "codex", "--user",
                "--launcher", str(self.launcher),
                "--git-executable", "relative-git", "--json",
            ),
            2,
            "invalid_argument",
        )
        self.assert_error(
            self.run_aiq(
                "integration", "plan", "codex", "--user",
                "--launcher", str(self.launcher),
                "--git-executable", str(self.root / "missing-git"), "--json",
            ),
            6,
            "unsupported_environment",
        )
        self.assert_error(
            self.run_aiq(
                "integration", "plan", "codex", "--user",
                "--launcher", str(self.launcher), "--json",
                environment={**self.environment, "PATH": ""},
            ),
            6,
            "unsupported_environment",
        )

    def test_python_runtime_errors_are_environment_errors(self) -> None:
        self.assertEqual(
            _classify_error(
                CodexIntegrationError("Python executable is unavailable")
            ),
            ("unsupported_environment", 6),
        )

    def test_git_discovery_failures_are_environment_errors(self) -> None:
        fake_bin = self.root / "failing-git"
        fake_bin.mkdir()
        fake_git = fake_bin / "git"
        fake_git.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' 'fatal: detected dubious ownership' >&2\n"
            "exit 128\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o700)
        environment = {
            **self.environment,
            "PATH": str(fake_bin),
        }

        self.assert_error(
            self.run_aiq(
                "journal", "path",
                "--scope", "repo", "--cwd", str(self.repository), "--json",
                environment=environment,
            ),
            6,
            "unsupported_environment",
        )

        fake_git.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' 'fatal: not a git repository' >&2\n"
            "exit 128\n",
            encoding="utf-8",
        )
        outside = self.root / "outside"
        outside.mkdir()
        self.assert_error(
            self.run_aiq(
                "journal", "path",
                "--scope", "repo", "--cwd", str(outside), "--json",
                environment=environment,
            ),
            6,
            "unsupported_environment",
        )
        fallback = self.run_aiq(
            "journal", "path",
            "--scope", "auto", "--cwd", str(outside), "--json",
            environment=environment,
        )
        self.assertEqual(fallback.returncode, 0, fallback.stderr)
        self.assertEqual(json.loads(fallback.stdout)["scope"]["kind"], "user")

    def test_human_error_is_terminal_safe(self) -> None:
        event = {
            "v": 1,
            "source": "protocol-test",
            "content": "safe",
            "\x1b[31m\n\t": True,
        }
        completed = self.run_aiq(
            "ingest", "--event-json", "-",
            "--scope", "repo", "--cwd", str(self.repository),
            input_text=json.dumps(event),
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr.count("\n"), 1)
        self.assertTrue(completed.stderr.startswith("aiq: "))
        self.assertNotIn("\x1b", completed.stderr)
        self.assertNotIn("\r", completed.stderr)
        self.assertNotIn("\t", completed.stderr)


if __name__ == "__main__":
    unittest.main()
