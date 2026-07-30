from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import support
from aiq.cli import (
    CONFIG_OUTPUT_COMMANDS,
    _classify_error,
    _versioned,
    build_parser,
)
from aiq.integrations.codex import CodexIntegrationError
JSON_COMMAND_PATHS = {
    tuple(name.split("."))
    for name in """
        capability.list capability.show claim.list claim.release config.check
        config.show dequeue doctor enqueue inbox.apply inbox.claim inbox.fail
        inbox.list inbox.needs-input ingest integration.check
        integration.install integration.list integration.plan
        integration.print integration.uninstall journal.check journal.destroy
        journal.export journal.init journal.path journal.snapshot list
        queue.next queue.peek reconcile report status task.done task.explain
        task.history task.list task.show
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
        ):
            path.mkdir()
        support.init_repository(self.repository)
        self.launcher = support.write_launcher(
            self.root / "bin" / "aiq", mode=0o700
        )
        self.environment = support.scrubbed_environment(
            CODEX_HOME=str(self.root / "codex"),
            HOME=str(self.root / "home"),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONPATH=str(support.SOURCE_ROOT),
            XDG_CONFIG_HOME=str(self.root / "config"),
            XDG_STATE_HOME=str(self.root / "state"),
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
    ) -> support.CliResult:
        return support.run_cli(
            *arguments,
            in_process=False,
            cwd=self.repository,
            environment=self.environment if environment is None else environment,
            input_text=input_text,
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
        completed: support.CliResult,
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
        self.assertEqual(set(path), {"project", "scope", "v"})
        # Reported before the journal exists, derived, and creating no
        # storage.
        self.assertEqual(path["project"], "repository")
        initialized = self.ok("journal", "init", *self.scope)
        self.assertEqual(
            set(initialized), {"project", "scope", "status", "v"}
        )
        self.assertEqual(initialized["project"], "repository")
        doctor = self.ok("doctor", *self.scope)
        self.assertEqual(set(doctor), {"checks", "status", "v"})
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
        explained = self.ok("task", "explain", task_id, *self.scope)
        self.assertEqual(set(explained), {"explain", "v"})
        self.assertEqual(explained["explain"]["state"], "ready")
        history = self.ok("task", "history", task_id, *self.scope)
        self.assertEqual(set(history), {"events", "task_id", "v"})
        self.assertEqual(
            history["events"][-1]["type"],
            "task.created",
        )
        self.ok("queue", "peek", *self.scope)
        status = self.ok("status", *self.scope)
        self.assertEqual(
            set(status),
            {
                "blocked",
                "claims",
                "messages",
                "project",
                "ready",
                "scope",
                "tasks",
                "v",
            },
        )
        self.assertEqual(status["project"], "repository")
        self.assertEqual(status["messages"]["applied"], 1)
        self.assertEqual(status["tasks"]["ready"], 1)
        self.assertEqual(status["claims"]["active"], 0)
        self.assertEqual(len(status["ready"]), 1)
        ready_entry = dict(status["ready"][0])
        created_at = ready_entry.pop("created_at")
        datetime.fromisoformat(created_at)
        self.assertEqual(
            ready_entry,
            {"task_id": task_id, "priority": 7, "title": "Protocol task"},
        )
        next_result = self.ok(
            "queue", "next", "--owner", "protocol-test", *self.scope,
        )
        self.assertEqual(set(next_result), {"items", "v"})
        self.assertEqual(len(next_result["items"]), 1)
        item = next_result["items"][0]
        self.assertEqual(set(item), {"claim", "task"})
        self.assertNotIn("claim", item["task"])
        claims = self.ok("claim", "list", *self.scope)
        self.assertEqual(set(claims), {"claims", "v"})
        self.assertEqual(
            [claim["claim_id"] for claim in claims["claims"]],
            [item["claim"]["claim_id"]],
        )
        self.assertEqual(claims["claims"][0]["status"], "active")
        self.ok("claim", "release", item["claim"]["claim_id"], *self.scope)

        enqueued = self.ok(
            "enqueue", "Transactional task", "--objective", "Enqueue flow",
            "--priority", "3", "--requires", task_id, *self.scope,
        )
        self.assertEqual(set(enqueued), {"message_id", "state", "task_id", "v"})
        self.assertEqual(enqueued["state"], "queued")
        listed = self.ok("list", *self.scope)
        self.assertEqual(
            [entry["task_id"] for entry in listed["tasks"]],
            [task_id, enqueued["task_id"]],
        )
        self.assertEqual(
            set(listed["tasks"][0]),
            {"priority", "revision", "state", "task_id", "title"},
        )
        dequeued = self.ok("dequeue", "--owner", "protocol-test", *self.scope)
        self.assertEqual(set(dequeued), {"items", "v"})
        self.assertEqual(dequeued["items"][0]["task"]["task_id"], task_id)
        settled = self.ok(
            "task", "done", task_id, "--summary", "Protocol settlement",
            "--owner", "protocol-test", *self.scope,
        )
        self.assertEqual(set(settled), {"message_id", "status", "tasks", "v"})
        self.assertEqual(settled["status"], "done")
        self.assertEqual(
            settled["tasks"],
            [{"task_id": task_id, "revision": 2, "state": "done"}],
        )
        self.ok(
            "task", "done", enqueued["task_id"],
            "--summary", "Protocol settlement follow-up",
            "--owner", "protocol-test", *self.scope,
        )
        finished = self.ok("list", "--all", *self.scope)
        self.assertEqual(
            [entry["state"] for entry in finished["tasks"]],
            ["done", "done"],
        )

        stored = self.ok(
            "ingest", "--message", "Deduplicated protocol content",
            "--if-new", *self.scope,
        )
        deduped = self.ok(
            "ingest", "--message", "Deduplicated protocol content",
            "--if-new", *self.scope,
        )
        self.assertEqual(
            set(stored),
            {"created", "deduped", "message_id", "scope", "state", "v"},
        )
        self.assertTrue(stored["created"])
        self.assertFalse(stored["deduped"])
        self.assertFalse(deduped["created"])
        self.assertTrue(deduped["deduped"])
        self.assertEqual(deduped["message_id"], stored["message_id"])

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

        reported = self.ok(
            "report", "--summary", "Protocol report",
            "--detail", "Protocol report detail",
            "--to", str(self.repository), *self.scope,
        )
        self.assertEqual(reported["status"], "reported")

        self.ok("journal", "check", *self.scope)
        reconciled = self.ok("reconcile", "--user", *self.scope)
        self.assertEqual(reconciled["status"], "ok")
        self.assertEqual(reconciled["integrations"][0]["status"], "skipped")
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
        # Plan is report-only: unresolvable executables yield an unsafe
        # plan with exit 0. Install surfaces them as error envelopes.
        unsafe = self.run_aiq(
            "integration", "plan", "codex", "--user",
            "--launcher", str(self.root / "missing-aiq"), "--json",
        )
        self.assertEqual(unsafe.returncode, 0, unsafe.stderr)
        unsafe_plan = json.loads(unsafe.stdout)
        self.assertEqual(unsafe_plan["status"], "unsafe")
        self.assertEqual(unsafe_plan["action"], "block")
        self.assertIn("unavailable", unsafe_plan["blocked_reason"])
        self.assert_error(
            self.run_aiq(
                "integration", "install", "codex", "--user",
                "--launcher", str(self.root / "missing-aiq"), "--json",
            ),
            6,
            "unsupported_environment",
        )
        self.assert_error(
            self.run_aiq(
                "integration", "install", "codex", "--user",
                "--launcher", "relative-aiq", "--json",
            ),
            2,
            "invalid_argument",
        )
        self.assert_error(
            self.run_aiq(
                "integration", "install", "codex", "--user", "--json",
                environment={**self.environment, "PATH": ""},
            ),
            6,
            "unsupported_environment",
        )
        self.assert_error(
            self.run_aiq(
                "integration", "install", "codex", "--user",
                "--launcher", str(self.launcher),
                "--git-executable", "relative-git", "--json",
            ),
            2,
            "invalid_argument",
        )
        self.assert_error(
            self.run_aiq(
                "integration", "install", "codex", "--user",
                "--launcher", str(self.launcher),
                "--git-executable", str(self.root / "missing-git"), "--json",
            ),
            6,
            "unsupported_environment",
        )
        self.assert_error(
            self.run_aiq(
                "integration", "install", "codex", "--user",
                "--launcher", str(self.launcher), "--json",
                environment={**self.environment, "PATH": ""},
            ),
            6,
            "unsupported_environment",
        )

    def test_config_output_commands_match_registered_parsers(self) -> None:
        def loads_config(parser) -> bool:
            if parser._defaults.get("load_config"):
                return True
            return any(
                loads_config(child)
                for action in parser._actions
                if action.__class__.__name__ == "_SubParsersAction"
                for child in action.choices.values()
            )

        root = build_parser()
        subparsers = next(
            action
            for action in root._actions
            if action.__class__.__name__ == "_SubParsersAction"
        )
        registered = {
            name
            for name, child in subparsers.choices.items()
            if loads_config(child)
        }
        # doctor and ingest resolve configuration inside their handlers:
        # doctor reports configuration failures as checks, and ingest
        # resolves configuration only after the event supplies the
        # effective cwd.
        self.assertEqual(
            registered,
            CONFIG_OUTPUT_COMMANDS - {"doctor", "ingest"},
        )
        self.assertLessEqual({"doctor", "ingest"}, CONFIG_OUTPUT_COMMANDS)

    def test_config_error_honors_configured_json_output(self) -> None:
        config_path = self.root / "config" / "aiq" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            'version = 1\noutput = "json"\n',
            encoding="utf-8",
        )

        completed = self.run_aiq(
            "config", "show", "--lease-seconds", "0",
            "--cwd", str(self.repository),
        )

        self.assert_error(completed, 2, "invalid_config")

    def test_envelope_rejects_conflicting_payload_version(self) -> None:
        self.assertEqual(
            _versioned({"v": 1, "status": "installed"}),
            {"v": 1, "status": "installed"},
        )
        with self.assertRaisesRegex(
            AssertionError,
            "conflicts with protocol envelope version",
        ):
            _versioned({"v": 2, "status": "installed"})

    def test_export_argument_validation_is_invalid_argument(self) -> None:
        self.assert_error(
            self.run_aiq("journal", "export", ".", *self.scope),
            2,
            "invalid_argument",
        )

    def test_unknown_effects_alias_is_invalid_document(self) -> None:
        self.run_aiq("journal", "init", *self.scope)
        ingested = json.loads(
            self.run_aiq(
                "ingest", "--message", "Unknown alias coverage", *self.scope,
            ).stdout
        )
        message_id = str(ingested["message_id"])
        claimed = json.loads(
            self.run_aiq(
                "inbox", "claim", message_id,
                "--owner", "protocol-test", *self.scope,
            ).stdout
        )
        effects = json.dumps(
            {
                "v": 1,
                "expect": {},
                "effects": [["update", "$missing", {"title": "New"}]],
            },
            separators=(",", ":"),
        )
        self.assert_error(
            self.run_aiq(
                "inbox", "apply", message_id,
                "--claim", claimed["claim"]["claim_id"],
                "--effects", "-", *self.scope, input_text=effects,
            ),
            2,
            "invalid_document",
        )

    def test_environment_output_selects_json_for_uncfg_commands(self) -> None:
        environment = {**self.environment, "AIQ_OUTPUT": "json"}
        for arguments, key in (
            (("capability", "list"), "capabilities"),
            (("integration", "list"), "integrations"),
        ):
            with self.subTest(command=arguments):
                completed = self.run_aiq(*arguments, environment=environment)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stderr, "")
                self.assertEqual(completed.stdout.count("\n"), 1)
                payload = json.loads(completed.stdout)
                self.assertEqual(payload["v"], 1)
                self.assertIn(key, payload)

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
