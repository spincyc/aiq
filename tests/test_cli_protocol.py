from __future__ import annotations

import ast
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import support
from support import REPOSITORY_ROOT
from aiq.cli import (
    CONFIG_OUTPUT_COMMANDS,
    _classify_error,
    _classify_journal_error,
    _versioned,
    build_parser,
)
from aiq.cli._errors import _JOURNAL_ERROR_CODE_EXITS
from aiq.integrations import _hooks
from aiq.integrations.codex import SPEC as CODEX_SPEC, CodexIntegrationError
from aiq.journal import (
    SCHEMA_VERSION,
    JournalError,
    check_journal,
    ingest_message,
    list_inbox,
    resolve_scope,
    validate_project_label,
)
from aiq.privacy import export_journal
from aiq.queue import (
    _now_us,
    apply_effects,
    claim_message,
    claim_task,
    dispose_message,
    release_claim,
    show_task,
)
JSON_COMMAND_PATHS = {
    tuple(name.split("."))
    for name in """
        capability.list capability.show claim.list claim.release config.check
        config.show dequeue doctor enqueue inbox.apply inbox.claim inbox.fail
        inbox.list inbox.needs-input ingest integration.check
        integration.install integration.list integration.plan
        integration.print integration.uninstall journal.check journal.destroy
        journal.export journal.init journal.path journal.snapshot list
        queue.next queue.peek reader.acquire reader.release reader.status
        reconcile report status task.done task.explain
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
                "reader",
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
        self.assertEqual(set(next_result), {"items", "reader_acquired", "v"})
        # The earlier inbox claim already took the reader role for this
        # session, so leasing the task renewed it rather than taking it.
        self.assertFalse(next_result["reader_acquired"])
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
        self.assertEqual(set(dequeued), {"items", "reader_acquired", "v"})
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

        reader_state = self.ok("reader", "status", *self.scope)
        self.assertEqual(set(reader_state), {"reader", "scope", "v"})
        self.assertEqual(reader_state["reader"]["status"], "held")
        self.assertTrue(reader_state["reader"]["self"])
        acquired = self.ok("reader", "acquire", *self.scope)
        self.assertEqual(set(acquired), {"acquired", "reader", "status", "v"})
        # Acquiring while already holding renews the same lease.
        self.assertFalse(acquired["acquired"])
        self.assertEqual(acquired["reader"]["epoch"], 1)
        released = self.ok("reader", "release", *self.scope)
        self.assertEqual(
            set(released),
            {"claims_held", "reader", "released", "replayed", "status", "v"},
        )
        self.assertEqual(released["status"], "released")
        self.assertTrue(released["released"])
        self.assertFalse(released["replayed"])
        # Nothing of this session's is still claimed, so release is silent
        # on stderr; `self.ok` already asserted that.
        self.assertEqual(released["claims_held"], 0)
        # Replaying the same release is still successful, and says which
        # of the two successes it was: the declaration already stands.
        replayed = self.ok("reader", "release", *self.scope)
        self.assertEqual(replayed["status"], "already_released")
        self.assertFalse(replayed["released"])
        self.assertTrue(replayed["replayed"])

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

    def test_an_uncoded_integration_error_is_a_defect_not_a_guess(self) -> None:
        # The retired substring rules read "Python executable" out of this
        # wording and answered ``unsupported_environment``. Wording now
        # classifies nothing: an integration error reaching the CLI with no
        # code is an AIQ defect and is reported as one.
        self.assertEqual(
            _classify_error(
                CodexIntegrationError("Python executable is unavailable")
            ),
            ("internal_error", 70),
        )
        # An unregistered code is the same defect, not a partial match.
        self.assertEqual(
            _classify_error(
                CodexIntegrationError("drifted", code="no_such_code")
            ),
            ("internal_error", 70),
        )
        # What users see for this failure is unchanged, because the real
        # raise site pins the code the wording used to imply.
        with self.assertRaises(CodexIntegrationError) as raised:
            _hooks.python_executable_path(
                str(self.root / "absent" / "python"),
                error_class=CodexIntegrationError,
            )
        self.assertEqual(raised.exception.code, "unsupported_environment")
        self.assertEqual(
            _classify_error(raised.exception),
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


class JournalErrorCodeIdentityTests(unittest.TestCase):
    """Stable codes come from the raise site, not from message wording.

    Each test provokes the real raise site and reads ``JournalError.code``
    directly, so rewording a diagnostic cannot silently change the
    documented code. Classification is asserted too, pinning the
    code-to-exit mapping.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.environment = patch.dict(
            os.environ,
            {"XDG_STATE_HOME": str(self.root / "state")},
        )
        self.environment.start()
        agent_root = self.root / "agent"
        agent_root.mkdir()
        self.scope = resolve_scope(
            "agent-root",
            cwd=self.root,
            agent_root=agent_root,
        )

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary_directory.cleanup()

    def assert_raise_site_code(
        self,
        error: JournalError,
        code: str,
        exit_code: int,
    ) -> None:
        self.assertEqual(error.code, code, str(error))
        self.assertEqual(_classify_journal_error(error), (code, exit_code))

    def claimed_message(self, content: str, **kwargs: object) -> tuple[str, str]:
        message = ingest_message(self.scope, content, cwd=str(self.root))
        claim = claim_message(
            self.scope,
            owner_id="code-identity-test",
            message_id=message.message_id,
            **kwargs,
        )
        self.assertIsNotNone(claim)
        return message.message_id, claim["claim_id"]

    def test_unsupported_environment_is_set_at_the_raise_site(self) -> None:
        with self.assertRaises(JournalError) as raised:
            resolve_scope("nowhere", cwd=self.root)
        self.assert_raise_site_code(
            raised.exception, "unsupported_environment", 6
        )

    def test_schema_incompatible_is_set_at_the_raise_site(self) -> None:
        ingest_message(self.scope, "create the journal", cwd=str(self.root))
        connection = sqlite3.connect(self.scope.journal_path)
        try:
            connection.execute(
                "UPDATE journal_metadata SET value = ? "
                "WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION + 1),),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(JournalError) as raised:
            check_journal(self.scope)
        self.assert_raise_site_code(
            raised.exception, "schema_incompatible", 5
        )

    def test_integrity_failed_is_set_at_the_raise_site(self) -> None:
        ingest_message(self.scope, "create the journal", cwd=str(self.root))
        connection = sqlite3.connect(self.scope.journal_path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                """
                INSERT INTO events(
                  event_id,
                  occurred_at,
                  event_type,
                  message_id,
                  payload_json
                )
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    "evt_dangling",
                    "2026-01-01T00:00:00Z",
                    "message.ingested",
                    "msg_missing",
                    "{}",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(JournalError) as raised:
            check_journal(self.scope)
        self.assert_raise_site_code(raised.exception, "integrity_failed", 5)

    def test_claim_mismatch_is_set_at_the_raise_site(self) -> None:
        ingest_message(self.scope, "create the journal", cwd=str(self.root))
        with self.assertRaises(JournalError) as raised:
            release_claim(self.scope, "clm_" + "0" * 32)
        self.assert_raise_site_code(raised.exception, "claim_mismatch", 4)

    def test_claim_expired_is_set_at_the_raise_site(self) -> None:
        _, claim_id = self.claimed_message("expire this claim", lease_seconds=1)
        with self.assertRaises(JournalError) as raised:
            release_claim(
                self.scope,
                claim_id,
                now_us=_now_us() + 60 * 1_000_000,
            )
        self.assert_raise_site_code(raised.exception, "claim_expired", 4)

    def test_revision_conflict_is_set_at_the_raise_site(self) -> None:
        message_id, claim_id = self.claimed_message("create a task")
        created = apply_effects(
            self.scope,
            message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [["create", "$task", {"title": "Pin the code"}]],
            },
            claim_id=claim_id,
        )
        task_id = created["aliases"]["$task"]

        stale_id, stale_claim = self.claimed_message("use a stale revision")
        with self.assertRaises(JournalError) as raised:
            apply_effects(
                self.scope,
                stale_id,
                {
                    "v": 1,
                    "expect": {task_id: 99},
                    "effects": [["update", task_id, {"priority": 1}]],
                },
                claim_id=stale_claim,
            )
        self.assert_raise_site_code(raised.exception, "revision_conflict", 4)

    def test_wording_never_classifies_and_an_unpinned_error_is_a_defect(
        self,
    ) -> None:
        # This message reads exactly like a revision conflict. Nothing reads
        # it: classification comes from ``code=`` alone, so an error that
        # reaches the CLI without one is reported as an AIQ defect rather
        # than guessed at.
        message = "task revision changed: TASK-1: expected 1, found 2"
        self.assertEqual(
            _classify_journal_error(JournalError(message)),
            ("internal_error", 70),
        )
        self.assertEqual(
            _classify_journal_error(JournalError(message, code="not_found")),
            ("not_found", 3),
        )
        self.assertEqual(
            _classify_journal_error(
                JournalError(
                    "the reader lease is held; ingest stays open",
                    code="reader_held",
                )
            ),
            ("reader_held", 4),
        )

    def created_task(self, title: str = "Pin the code") -> str:
        message_id, claim_id = self.claimed_message(f"create: {title}")
        created = apply_effects(
            self.scope,
            message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [["create", "$task", {"title": title}]],
            },
            claim_id=claim_id,
        )
        return created["aliases"]["$task"]

    def test_not_found_is_set_at_the_raise_site(self) -> None:
        ingest_message(self.scope, "create the journal", cwd=str(self.root))
        with self.assertRaises(JournalError) as raised:
            show_task(self.scope, "TASK-404")
        self.assert_raise_site_code(raised.exception, "not_found", 3)

    def test_invalid_argument_is_set_at_the_raise_site(self) -> None:
        ingest_message(self.scope, "create the journal", cwd=str(self.root))
        with self.assertRaises(JournalError) as raised:
            list_inbox(self.scope, limit=0)
        self.assert_raise_site_code(raised.exception, "invalid_argument", 2)

    def test_invalid_document_is_set_at_the_raise_site(self) -> None:
        message_id, claim_id = self.claimed_message("apply a bad document")
        with self.assertRaises(JournalError) as raised:
            apply_effects(
                self.scope,
                message_id,
                {"v": 2, "expect": {}, "effects": []},
                claim_id=claim_id,
            )
        self.assert_raise_site_code(raised.exception, "invalid_document", 2)

    def test_not_claimable_is_set_at_the_raise_site(self) -> None:
        task_id = self.created_task()
        self.assertIsNotNone(
            claim_task(self.scope, task_id, owner_id="code-identity-test")
        )
        with self.assertRaises(JournalError) as raised:
            claim_task(self.scope, task_id, owner_id="second-worker")
        self.assert_raise_site_code(raised.exception, "not_claimable", 4)

    def test_state_conflict_is_set_at_the_raise_site(self) -> None:
        task_id = self.created_task()
        message_id, claim_id = self.claimed_message("depend on itself")
        with self.assertRaises(JournalError) as raised:
            apply_effects(
                self.scope,
                message_id,
                {
                    "v": 1,
                    "expect": {task_id: 1},
                    "effects": [["require", task_id, task_id]],
                },
                claim_id=claim_id,
            )
        self.assert_raise_site_code(raised.exception, "state_conflict", 4)

    def test_export_integrity_failure_is_set_at_the_raise_site(self) -> None:
        ingest_message(self.scope, "create the journal", cwd=str(self.root))
        connection = sqlite3.connect(self.scope.journal_path)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                """
                INSERT INTO events(
                  event_id,
                  occurred_at,
                  event_type,
                  message_id,
                  payload_json
                )
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    "evt_dangling",
                    "2026-01-01T00:00:00Z",
                    "message.ingested",
                    "msg_missing",
                    "{}",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(JournalError) as raised:
            export_journal(self.scope, self.root / "export.jsonl")
        self.assert_raise_site_code(raised.exception, "integrity_failed", 5)

    def test_export_schema_mismatch_is_set_at_the_raise_site(self) -> None:
        ingest_message(self.scope, "create the journal", cwd=str(self.root))
        connection = sqlite3.connect(self.scope.journal_path)
        try:
            connection.execute(
                "UPDATE journal_metadata SET value = ? "
                "WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION + 1),),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(JournalError) as raised:
            export_journal(self.scope, self.root / "export.jsonl")
        self.assert_raise_site_code(raised.exception, "schema_incompatible", 5)

    # The classifications below moved in the TASK-54 contract correction.
    # Each one pins the corrected code so the movement cannot silently
    # regress to the classification the retired substring rules produced.

    def test_stored_queue_invariant_is_an_integrity_failure(self) -> None:
        # ``audit_queue`` reports stored-data violations. This one used to
        # surface as ``state_conflict`` at exit 4, beside SQLite's own
        # integrity checks at exit 5.
        ingest_message(self.scope, "create the journal", cwd=str(self.root))
        connection = sqlite3.connect(self.scope.journal_path)
        try:
            connection.execute("INSERT INTO task_numbers DEFAULT VALUES")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(JournalError) as raised:
            check_journal(self.scope)
        self.assertIn("task number allocation", str(raised.exception))
        self.assert_raise_site_code(raised.exception, "integrity_failed", 5)

    def test_unusable_lock_path_is_an_io_error(self) -> None:
        # A filesystem precondition on journal state used to report
        # ``state_conflict`` at exit 4 rather than an exit-6 environment code.
        ingest_message(self.scope, "create the journal", cwd=str(self.root))
        lock_path = self.scope.journal_path.parent / "lifecycle.lock"
        lock_path.unlink()
        lock_path.mkdir()

        with self.assertRaises(JournalError) as raised:
            check_journal(self.scope)
        self.assert_raise_site_code(raised.exception, "io_error", 6)

    def test_effect_shape_violation_is_an_invalid_document(self) -> None:
        # An effects document that violates its versioned contract used to
        # report ``state_conflict`` at exit 4 instead of exit 2.
        message_id, claim_id = self.claimed_message("apply a misshapen effect")
        with self.assertRaises(JournalError) as raised:
            apply_effects(
                self.scope,
                message_id,
                {"v": 1, "expect": {}, "effects": [["create", "$task"]]},
                claim_id=claim_id,
            )
        self.assertIn("must have 3 items", str(raised.exception))
        self.assert_raise_site_code(raised.exception, "invalid_document", 2)

    def test_rejected_disposition_argument_is_an_invalid_argument(self) -> None:
        message_id, claim_id = self.claimed_message("dispose me wrongly")
        with self.assertRaises(JournalError) as raised:
            dispose_message(
                self.scope,
                message_id,
                claim_id=claim_id,
                disposition="abandoned",
                reason="not a disposition",
            )
        self.assert_raise_site_code(raised.exception, "invalid_argument", 2)

    def test_relative_state_home_is_an_unsupported_environment(self) -> None:
        # "XDG_STATE_HOME" contains "state", which the retired rules read as
        # ``state_conflict``.
        with patch.dict(os.environ, {"XDG_STATE_HOME": "relative/state"}):
            with self.assertRaises(JournalError) as raised:
                resolve_scope("user", cwd=self.root)
        self.assert_raise_site_code(
            raised.exception, "unsupported_environment", 6
        )

    def test_wrapped_event_validation_is_an_invalid_document(self) -> None:
        # The last site on the retired fallback: ingest re-raises the event
        # layer's diagnostic, which was classified by wording alone.
        with self.assertRaises(JournalError) as raised:
            ingest_message(self.scope, "hello", source="Bad Source")
        self.assert_raise_site_code(raised.exception, "invalid_document", 2)

    # The three integration classifications below were pinned to whatever
    # the substring rules produced and recorded in errors.md as accidents
    # awaiting correction. Each test pins the corrected code at its raise
    # site so the correction cannot silently regress.

    def test_launcher_control_characters_are_an_invalid_argument(self) -> None:
        # A malformed caller-supplied scalar, exactly as it already was for
        # ``--git-executable``. This moved from ``integration_drift`` at
        # exit 6, a breaking exit-category change.
        with self.assertRaises(CodexIntegrationError) as raised:
            _hooks.launcher_path(
                str(self.root / "bin") + "/ai\nq",
                error_class=CodexIntegrationError,
            )
        self.assertIn("control characters", str(raised.exception))
        self.assert_raise_site_code(raised.exception, "invalid_argument", 2)

    def test_a_launcher_that_cannot_run_is_an_unsupported_environment(
        self,
    ) -> None:
        # A resolved path the host cannot execute, as it already was for
        # Git and Python. This moved from ``integration_drift``; both codes
        # exit 6, so the change is code-only.
        launcher = self.root / "aiq"
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o600)
        with self.assertRaises(CodexIntegrationError) as raised:
            _hooks.launcher_path(
                str(launcher), error_class=CodexIntegrationError
            )
        self.assertIn("not executable", str(raised.exception))
        self.assert_raise_site_code(
            raised.exception, "unsupported_environment", 6
        )

    def manifest_with(self, **overrides: object) -> dict[str, object]:
        """A manifest valid through the executable-field checks."""

        manifest: dict[str, object] = {
            "backups": [],
            "config_sha256": None,
            "created_containers": [],
            "created_file": False,
            "integration": CODEX_SPEC.integration,
            "integration_id": CODEX_SPEC.integration_id,
            "git_executable": "/usr/bin/git",
            "launcher": "/usr/local/bin/aiq",
            "managed_group": {},
            "managed_group_sha256": None,
            "python_executable": "/usr/bin/python3",
            "status": "uninstalled",
            "target": os.fspath(self.root / "config.toml"),
            "v": 1,
        }
        manifest.update(overrides)
        return manifest

    def test_a_corrupt_manifest_executable_field_is_drift(self) -> None:
        # The manifest is bad, not the host, which was never consulted.
        # The Git and Python fields moved from ``unsupported_environment``;
        # both codes exit 6, so the change is code-only. The sibling
        # ``launcher`` field already reported drift and still does.
        for field_name, description in (
            ("git_executable", "Git executable"),
            ("python_executable", "Python executable"),
            ("launcher", "launcher"),
        ):
            for corrupt in ("relative/path", "/abs/with\nnewline", 7):
                with self.subTest(field=field_name, value=corrupt):
                    with self.assertRaises(CodexIntegrationError) as raised:
                        _hooks._validate_manifest(
                            CODEX_SPEC,
                            self.manifest_with(**{field_name: corrupt}),
                            state_directory=self.root / "state",
                            target=self.root / "config.toml",
                        )
                    self.assertIn(description, str(raised.exception))
                    self.assert_raise_site_code(
                        raised.exception, "integration_drift", 6
                    )

    def test_pinned_code_beats_a_rival_phrase_in_its_own_message(self) -> None:
        # ``validate_project_label`` is pinned ``invalid_argument`` while its
        # diagnostic contains "must be", which the retired fallback rules
        # read as ``invalid_document``. The raise-site code decides.
        with self.assertRaises(JournalError) as raised:
            validate_project_label("   ")
        self.assert_raise_site_code(raised.exception, "invalid_argument", 2)
        self.assertIn("must be", str(raised.exception))
        # The same independence in the other direction: a phrase that used to
        # name a different code does not outrank the pinned one.
        self.assertEqual(
            _classify_journal_error(
                JournalError("task not found: TASK-1", code="state_conflict")
            ),
            ("state_conflict", 4),
        )


class JournalErrorRaiseSiteCoverageTests(unittest.TestCase):
    """Every ``JournalError`` raise site carries its own stable code.

    ``_classify_error`` consults an explicit ``code`` before any
    class-based or wording-based rule, and an unpinned plain
    ``JournalError`` classifies as ``internal_error``. This test is what
    keeps that honest, failing on a new raise site without ``code=``
    rather than letting it reach users as an implementation defect.

    The raised names are derived from the tree rather than hardcoded, so
    the integration subclasses -- ``HookIntegrationError`` and its
    adapters, ``GuidanceIntegrationError`` -- are covered, and so is any
    subclass added later. ``error_class`` covers the two indirect forms
    the shared integration engine uses: ``raise error_class(...)`` through
    a parameter and ``raise spec.error_class(...)`` through an attribute.
    """

    # Keyed on the repo-relative POSIX path, never a bare filename: the
    # tree holds both src/aiq/journal.py and src/aiq/cli/journal.py.
    EXEMPT: set[tuple[str, str]] = set()

    # Raise sites whose code is a runtime value rather than a literal.
    # Registration must be deliberate; the codes those expressions can
    # take are checked by the two tests below.
    DYNAMIC_CODE_SITES = {
        ("src/aiq/integrations/_hooks.py", "executable_path"),
        ("src/aiq/integrations/_hooks.py", "install_integration"),
        ("src/aiq/integrations/guidance.py", "install_integration"),
    }

    @staticmethod
    def _source_files() -> list[Path]:
        return sorted((REPOSITORY_ROOT / "src" / "aiq").rglob("*.py"))

    @staticmethod
    def _relative(path: Path) -> str:
        return path.relative_to(REPOSITORY_ROOT).as_posix()

    def _raised_names(self) -> set[str]:
        """Names of ``JournalError`` and every subclass declared in-tree."""

        bases: dict[str, list[str]] = {}
        for path in self._source_files():
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.ClassDef):
                    continue
                bases[node.name] = [
                    base.id
                    if isinstance(base, ast.Name)
                    else getattr(base, "attr", "")
                    for base in node.bases
                ]
        names = {"JournalError"}
        while True:
            grown = {
                name
                for name, parents in bases.items()
                if any(parent in names for parent in parents)
            }
            if grown <= names:
                break
            names |= grown
        # Indirect raises through the shared engine's spec-supplied class.
        return names | {"error_class"}

    def _raise_sites(self):
        """Yield ``(relative_path, lineno, function, call)`` per raise."""

        raised = self._raised_names()
        for path in self._source_files():
            tree = ast.parse(path.read_text())
            owner: dict[int, str] = {}
            for node in ast.walk(tree):
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    end = node.end_lineno or node.lineno
                    for line in range(node.lineno, end + 1):
                        owner[line] = node.name
            for node in ast.walk(tree):
                if not isinstance(node, ast.Raise):
                    continue
                call = node.exc
                if not isinstance(call, ast.Call):
                    continue
                function = call.func
                name = (
                    function.id
                    if isinstance(function, ast.Name)
                    else getattr(function, "attr", None)
                )
                if name not in raised:
                    continue
                yield (
                    self._relative(path),
                    node.lineno,
                    owner.get(node.lineno, "<module>"),
                    call,
                )

    def test_raised_names_cover_the_integration_subclasses(self) -> None:
        raised = self._raised_names()
        for name in (
            "JournalError",
            "_NotGitRepository",
            "HookIntegrationError",
            "ClaudeIntegrationError",
            "CodexIntegrationError",
            "GuidanceIntegrationError",
            "error_class",
        ):
            self.assertIn(name, raised)

    def test_every_raise_site_sets_a_code(self) -> None:
        unpinned: list[str] = []
        total = 0
        for relative, lineno, function, call in self._raise_sites():
            total += 1
            if any(keyword.arg == "code" for keyword in call.keywords):
                continue
            if (relative, function) in self.EXEMPT:
                continue
            unpinned.append(f"{relative}:{lineno} in {function}")
        self.assertEqual(unpinned, [], "raise sites without code=")
        self.assertGreater(total, 340)

    def test_every_pinned_code_is_a_known_stable_code(self) -> None:
        for relative, lineno, function, call in self._raise_sites():
            for keyword in call.keywords:
                if keyword.arg != "code":
                    continue
                site = f"{relative}:{lineno} in {function}"
                if isinstance(keyword.value, ast.Constant):
                    self.assertIn(
                        keyword.value.value, _JOURNAL_ERROR_CODE_EXITS, site
                    )
                    continue
                self.assertIn(
                    (relative, function), self.DYNAMIC_CODE_SITES, site
                )
                # A literal fallback in a runtime expression -- the
                # ``or "integration_drift"`` in
                # ``plan.get("_blocked_code") or "integration_drift"`` --
                # is still a code and must be a known one. Only ``or``
                # operands are codes; a string elsewhere in the
                # expression is a lookup key, not a classification.
                for node in ast.walk(keyword.value):
                    if not isinstance(node, ast.BoolOp):
                        continue
                    for operand in node.values:
                        if isinstance(operand, ast.Constant) and isinstance(
                            operand.value, str
                        ):
                            self.assertIn(
                                operand.value, _JOURNAL_ERROR_CODE_EXITS, site
                            )

    def test_every_code_valued_argument_is_a_known_stable_code(self) -> None:
        """Values feeding a runtime ``code=`` are themselves known codes.

        ``executable_path`` takes its control-character and
        not-executable codes as arguments, so the literals live at the
        signature defaults rather than at the raise. Both the defaults
        and any overriding call site are checked here. A ``_code`` name
        bound to a non-string, such as ``failure_exit_code``, is a
        different kind of code and is left alone.
        """

        def is_code(node: ast.AST) -> bool:
            return isinstance(node, ast.Constant) and isinstance(
                node.value, str
            )

        found = 0
        for path in self._source_files():
            relative = self._relative(path)
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        if not (keyword.arg or "").endswith("_code"):
                            continue
                        if not is_code(keyword.value):
                            continue
                        found += 1
                        self.assertIn(
                            keyword.value.value,
                            _JOURNAL_ERROR_CODE_EXITS,
                            f"{relative}:{node.lineno} {keyword.arg}",
                        )
                if not isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    continue
                arguments = node.args
                pairs = list(
                    zip(
                        arguments.kwonlyargs,
                        arguments.kw_defaults,
                    )
                ) + list(
                    zip(
                        arguments.args[
                            len(arguments.args) - len(arguments.defaults) :
                        ],
                        arguments.defaults,
                    )
                )
                for argument, default in pairs:
                    if not argument.arg.endswith("_code"):
                        continue
                    if not is_code(default):
                        continue
                    found += 1
                    self.assertIn(
                        default.value,
                        _JOURNAL_ERROR_CODE_EXITS,
                        f"{relative}:{node.lineno} {argument.arg}",
                    )
        self.assertGreater(found, 0)

    def test_every_documented_code_is_registered(self) -> None:
        """errors.md's stable-code table and the exit table agree."""

        contract = (
            REPOSITORY_ROOT / "docs" / "contracts" / "errors.md"
        ).read_text()
        section = contract.split("## Stable codes", 1)[1].split("\n## ", 1)[0]
        documented = set(re.findall(r"^\| `([a-z_]+)` \|", section, re.M))
        self.assertGreater(len(documented), 15)
        self.assertEqual(documented, set(_JOURNAL_ERROR_CODE_EXITS))


if __name__ == "__main__":
    unittest.main()
