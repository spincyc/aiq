from __future__ import annotations

import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import time
import unittest

from unittest.mock import patch

import support
from aiq import journal
from aiq.integrations import _hooks
from aiq.integrations.claude import (
    ClaudeIntegrationError,
    INTEGRATION_ID,
    check_integration,
    gate_hook,
    install_integration,
    plan_integration,
    print_integration,
    receive_hook,
    receive_hook_main,
    uninstall_integration,
)
from aiq.journal import JournalError, check_journal, resolve_scope
from aiq.queue import (
    acquire_reader_lease,
    claim_message,
    dispose_message,
    enqueue_task,
    release_reader_lease,
)


# The identity the gate itself resolves, and an explicitly configured
# one, which names a holder no locator can tell apart from this session.
GATE_READER = "gate-session"
CONFIGURED_READER = "another-configured-reader"


class ClaudeIntegrationTest(unittest.TestCase):
    def git_executable(self) -> Path:
        return support.git_executable()

    def fixture(self, root: Path) -> tuple[dict[str, str], Path]:
        return support.integration_fixture(
            root, CLAUDE_CONFIG_DIR="claude config"
        )

    def test_print_is_stable_nested_fragment_without_matcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, launcher = self.fixture(root)

            first = print_integration(launcher=launcher)
            second = print_integration(launcher=launcher)

            self.assertEqual(first, second)
            document = json.loads(first)
            groups = document["hooks"]["UserPromptSubmit"]
            self.assertEqual(len(groups), 1)
            self.assertNotIn("matcher", groups[0])
            handler = groups[0]["hooks"][0]
            self.assertEqual(handler["type"], "command")
            self.assertIn("integration receive claude", handler["command"])
            self.assertIn(f"--integration-id {INTEGRATION_ID}", handler["command"])
            stop_groups = document["hooks"]["Stop"]
            self.assertEqual(len(stop_groups), 1)
            self.assertNotIn("matcher", stop_groups[0])
            self.assertEqual(
                stop_groups[0]["hooks"][0]["command"],
                handler["command"],
            )

    def test_relative_claude_config_dir_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            environment["CLAUDE_CONFIG_DIR"] = "relative/claude"

            with self.assertRaisesRegex(
                ClaudeIntegrationError,
                "CLAUDE_CONFIG_DIR must be an absolute path",
            ):
                plan_integration(launcher=launcher, environment=environment)

    def test_install_preserves_unrelated_settings_and_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            target = Path(environment["CLAUDE_CONFIG_DIR"]) / "settings.json"
            target.parent.mkdir(parents=True)
            existing = {
                "model": "opus",
                "permissions": {"allow": ["Bash(ls:*)"]},
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": "existing"}]}
                    ],
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "unrelated"}]}
                    ],
                },
            }
            target.write_text(json.dumps(existing))

            plan = plan_integration(launcher=launcher, environment=environment)
            installed = install_integration(
                launcher=launcher,
                environment=environment,
                plan_token=plan["plan_token"],
            )
            document = json.loads(target.read_text())

            self.assertEqual(plan["action"], "install")
            self.assertEqual(installed["status"], "installed")
            self.assertFalse(installed["created_file"])
            self.assertNotIn("description", document)
            self.assertEqual(document["model"], "opus")
            self.assertEqual(
                document["permissions"], {"allow": ["Bash(ls:*)"]}
            )
            stop_groups = document["hooks"]["Stop"]
            self.assertEqual(len(stop_groups), 2)
            self.assertEqual(stop_groups[0], existing["hooks"]["Stop"][0])
            self.assertIn(
                f"--integration-id {INTEGRATION_ID}",
                stop_groups[1]["hooks"][0]["command"],
            )
            groups = document["hooks"]["UserPromptSubmit"]
            self.assertEqual(len(groups), 2)
            self.assertEqual(groups[0]["hooks"][0]["command"], "existing")
            self.assertIn(
                f"--integration-id {INTEGRATION_ID}",
                groups[1]["hooks"][0]["command"],
            )

    def test_install_is_idempotent_and_uninstall_restores_created_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            target = Path(environment["CLAUDE_CONFIG_DIR"]) / "settings.json"

            first = install_integration(
                launcher=launcher,
                environment=environment,
            )
            second = install_integration(
                launcher=launcher,
                environment=environment,
            )
            checked = check_integration(
                launcher=launcher,
                environment=environment,
            )

            self.assertEqual(first["status"], "installed")
            self.assertTrue(first["created_file"])
            self.assertEqual(second["action"], "none")
            self.assertTrue(checked["ok"])
            self.assertEqual(checked["trust"], "manual_review_required")

            removed = uninstall_integration(environment=environment)
            repeated = uninstall_integration(environment=environment)

            self.assertEqual(removed["status"], "uninstalled")
            self.assertEqual(removed["integration_id"], INTEGRATION_ID)
            self.assertTrue(removed["deleted_file"])
            self.assertFalse(target.exists())
            self.assertEqual(repeated["action"], "none")
            self.assertEqual(repeated["integration_id"], INTEGRATION_ID)

    def test_disable_all_hooks_blocks_plan_and_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            target = Path(environment["CLAUDE_CONFIG_DIR"]) / "settings.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({"disableAllHooks": True}))

            plan = plan_integration(launcher=launcher, environment=environment)

            self.assertEqual(plan["status"], "disabled")
            self.assertEqual(plan["action"], "block")
            # The blocked plan keeps the stable public schema: the target
            # was read once before preflight, and the mutation-only keys
            # are present but unset.
            self.assertIsNotNone(plan["before_sha256"])
            for key in (
                "after_sha256",
                "plan_token",
                "created_file",
                "created_containers",
            ):
                self.assertIn(key, plan)
                self.assertIsNone(plan[key])
            with self.assertRaisesRegex(
                ClaudeIntegrationError,
                "disableAllHooks",
            ):
                install_integration(launcher=launcher, environment=environment)

    def test_drift_requires_explicit_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            target = Path(environment["CLAUDE_CONFIG_DIR"]) / "settings.json"
            install_integration(launcher=launcher, environment=environment)

            document = json.loads(target.read_text())
            handler = document["hooks"]["UserPromptSubmit"][0]["hooks"][0]
            handler["timeout"] = 55
            target.write_text(json.dumps(document))

            drifted = plan_integration(
                launcher=launcher,
                environment=environment,
            )
            self.assertEqual(drifted["status"], "drifted")
            self.assertEqual(drifted["action"], "block")

            repaired = install_integration(
                launcher=launcher,
                environment=environment,
                repair=True,
            )
            self.assertEqual(repaired["status"], "installed")
            restored = json.loads(target.read_text())
            self.assertEqual(
                restored["hooks"]["UserPromptSubmit"][0]["hooks"][0]["timeout"],
                10,
            )

    def test_receive_hook_deduplicates_by_prompt_id_and_uses_claude_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            support.initialize_repo_journal(repository)
            payload = json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session",
                    "prompt_id": "prompt-1",
                    "transcript_path": "/transcript.jsonl",
                    "permission_mode": "default",
                    "cwd": str(repository),
                    "prompt": "persist exactly\n",
                }
            )

            first = receive_hook(
                payload,
                git_executable=self.git_executable(),
            )
            second = receive_hook(
                payload,
                git_executable=self.git_executable(),
            )
            scope = resolve_scope("repo", cwd=repository)
            checked = check_journal(scope)

            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(checked["messages"], 1)
            connection = sqlite3.connect(scope.journal_path)
            try:
                source = connection.execute(
                    "SELECT source FROM messages"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(source, "claude")

    def test_receive_hook_without_prompt_id_captures_each_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            support.initialize_repo_journal(repository)
            payload = json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session",
                    "cwd": str(repository),
                    "prompt": "same prompt",
                }
            )

            first = receive_hook(
                payload,
                git_executable=self.git_executable(),
            )
            second = receive_hook(
                payload,
                git_executable=self.git_executable(),
            )

            self.assertTrue(first["created"])
            self.assertTrue(second["created"])

    def test_receive_hook_rejects_invalid_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            repository.mkdir()
            invalid_payloads = (
                {"hook_event_name": "Stop", "prompt": "x"},
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session",
                    "cwd": str(repository),
                    "prompt": "",
                },
                {
                    "hook_event_name": "UserPromptSubmit",
                    "cwd": str(repository),
                    "prompt": "x",
                },
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session",
                    "prompt_id": "",
                    "cwd": str(repository),
                    "prompt": "x",
                },
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session",
                    "cwd": "relative/path",
                    "prompt": "x",
                },
            )

            for payload in invalid_payloads:
                with self.assertRaises(ClaudeIntegrationError):
                    receive_hook(
                        json.dumps(payload),
                        git_executable=self.git_executable(),
                    )

            with self.assertRaisesRegex(
                ClaudeIntegrationError,
                "Claude Code hook prompt_id must be a non-empty string",
            ):
                receive_hook(
                    json.dumps(
                        {
                            "hook_event_name": "UserPromptSubmit",
                            "session_id": "session",
                            "prompt_id": 7,
                            "cwd": str(repository),
                            "prompt": "x",
                        }
                    ),
                    git_executable=self.git_executable(),
                )

    def test_receive_hook_captures_changed_content_for_same_prompt_id(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            support.initialize_repo_journal(repository)
            base = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session",
                "prompt_id": "prompt-1",
                "cwd": str(repository),
                "prompt": "/expand",
            }

            first = receive_hook(
                json.dumps(base),
                git_executable=self.git_executable(),
            )
            replayed = receive_hook(
                json.dumps(base),
                git_executable=self.git_executable(),
            )
            expanded = receive_hook(
                json.dumps({**base, "prompt": "expanded slash command"}),
                git_executable=self.git_executable(),
            )
            scope = resolve_scope("repo", cwd=repository)
            checked = check_journal(scope)

            self.assertTrue(first["created"])
            self.assertFalse(replayed["created"])
            self.assertTrue(expanded["created"])
            self.assertEqual(checked["messages"], 2)

    def test_receive_hook_skips_injected_prompts_and_captures_mentions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            base = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session",
                "cwd": str(repository),
            }
            notification = receive_hook(
                json.dumps(
                    {
                        **base,
                        "prompt": (
                            "<task-notification>\n"
                            "Background agent finished.\n"
                            "</task-notification>"
                        ),
                    }
                ),
                git_executable=self.git_executable(),
            )
            reminder = receive_hook(
                json.dumps(
                    {
                        **base,
                        "prompt": (
                            "  <system-reminder>\n"
                            "Injected context block.\n"
                            "</system-reminder>\n"
                        ),
                    }
                ),
                git_executable=self.git_executable(),
            )
            scope = resolve_scope("repo", cwd=repository)

            self.assertEqual(notification["skipped"], "injected-notification")
            self.assertEqual(reminder["skipped"], "injected-notification")
            self.assertNotIn("created", notification)
            self.assertFalse(scope.journal_path.exists())

            errors = io.StringIO()
            skipped_main = receive_hook_main(
                input_stream=io.BytesIO(
                    json.dumps(
                        {**base, "prompt": "<task-notification>done"}
                    ).encode()
                ),
                error_stream=errors,
                git_executable=self.git_executable(),
            )
            self.assertEqual(skipped_main, 0)
            self.assertEqual(errors.getvalue(), "")
            self.assertFalse(scope.journal_path.exists())

            support.initialize_repo_journal(repository)
            plain = receive_hook(
                json.dumps({**base, "prompt": "capture this request"}),
                git_executable=self.git_executable(),
            )
            mention = receive_hook(
                json.dumps(
                    {
                        **base,
                        "prompt": (
                            "explain how the <task-notification> and "
                            "<system-reminder> markers are skipped"
                        ),
                    }
                ),
                git_executable=self.git_executable(),
            )
            unclosed = receive_hook(
                json.dumps(
                    {
                        **base,
                        "prompt": "<system-reminder> is a tag I grep for",
                    }
                ),
                git_executable=self.git_executable(),
            )
            sandwiched = receive_hook(
                json.dumps(
                    {
                        **base,
                        "prompt": (
                            "<system-reminder>a</system-reminder>\n"
                            "please fix the login bug today\n"
                            "<system-reminder>b</system-reminder>"
                        ),
                    }
                ),
                git_executable=self.git_executable(),
            )
            adjacent = receive_hook(
                json.dumps(
                    {
                        **base,
                        "prompt": (
                            "<system-reminder>a</system-reminder>"
                            "<system-reminder>b</system-reminder>"
                        ),
                    }
                ),
                git_executable=self.git_executable(),
            )

            self.assertTrue(plain["created"])
            self.assertTrue(mention["created"])
            self.assertTrue(unclosed["created"])
            self.assertTrue(sandwiched["created"])
            self.assertTrue(adjacent["created"])
            self.assertEqual(check_journal(scope)["messages"], 5)

    def test_receive_hook_reports_a_busy_journal_before_the_host_timeout(
        self,
    ) -> None:
        # A hook the host kills at its timeout loses the message with no
        # diagnostic, so capture must give up while it can still report.
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            support.initialize_repo_journal(repository)
            scope = resolve_scope("repo", cwd=repository)
            payload = json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session",
                    "cwd": str(repository),
                    "prompt": "capture me while the journal is busy",
                }
            )
            errors = io.StringIO()

            with patch.object(_hooks, "CAPTURE_LOCK_TIMEOUT_SECONDS", 0.2):
                with journal.lifecycle_lock(scope, exclusive=True):
                    started = time.monotonic()
                    code = receive_hook_main(
                        input_stream=io.BytesIO(payload.encode()),
                        error_stream=errors,
                        git_executable=self.git_executable(),
                    )
                    elapsed = time.monotonic() - started

            self.assertEqual(code, 1)
            self.assertLess(elapsed, 5)
            self.assertEqual(errors.getvalue().count("\n"), 1)
            self.assertIn("journal is busy", errors.getvalue())
            self.assertEqual(check_journal(scope)["messages"], 0)

    def test_receive_hook_skips_uninitialized_repo_without_storage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            payload = json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session",
                    "cwd": str(repository),
                    "prompt": "not opted in yet",
                }
            )

            receipt = receive_hook(
                payload,
                git_executable=self.git_executable(),
            )
            errors = io.StringIO()
            status = receive_hook_main(
                input_stream=io.BytesIO(payload.encode()),
                error_stream=errors,
                git_executable=self.git_executable(),
            )
            scope = resolve_scope("repo", cwd=repository)

            self.assertEqual(
                receipt["skipped"],
                "repo-journal-not-initialized",
            )
            self.assertEqual(receipt["source"], "claude")
            self.assertEqual(receipt["scope"], scope.to_dict())
            self.assertNotIn("created", receipt)
            self.assertEqual(status, 0)
            self.assertEqual(errors.getvalue(), "")
            self.assertFalse(scope.journal_path.exists())
            self.assertFalse((repository / ".git" / "aiq").exists())

    def test_journal_init_opts_repository_in_to_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            payload = json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session",
                    "cwd": str(repository),
                    "prompt": "captured after opt-in",
                }
            )

            skipped = receive_hook(
                payload,
                git_executable=self.git_executable(),
            )
            support.initialize_repo_journal(repository)
            captured = receive_hook(
                payload,
                git_executable=self.git_executable(),
            )
            scope = resolve_scope("repo", cwd=repository)

            self.assertEqual(
                skipped["skipped"],
                "repo-journal-not-initialized",
            )
            self.assertTrue(captured["created"])
            self.assertEqual(check_journal(scope)["messages"], 1)

    def test_receive_hook_outside_git_auto_creates_user_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            payload = json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session",
                    "cwd": str(workspace),
                    "prompt": "user scope capture",
                }
            )

            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(root / "state")},
            ):
                result = receive_hook(
                    payload,
                    git_executable=self.git_executable(),
                )
                scope = resolve_scope("user")

            self.assertEqual(result["scope"]["kind"], "user")
            self.assertTrue(result["created"])
            self.assertTrue(scope.journal_path.is_file())

    def test_receive_hook_main_is_silent_on_success_and_exit_one_on_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            support.initialize_repo_journal(repository)
            payload = json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session",
                    "prompt_id": "prompt-1",
                    "cwd": str(repository),
                    "prompt": "capture",
                }
            ).encode()
            errors = io.StringIO()

            success = receive_hook_main(
                input_stream=io.BytesIO(payload),
                error_stream=errors,
                git_executable=self.git_executable(),
            )
            failure = receive_hook_main(
                input_stream=io.BytesIO(b"{}"),
                error_stream=errors,
                git_executable=self.git_executable(),
            )

            self.assertEqual(success, 0)
            self.assertEqual(failure, 1)
            self.assertIn("capture failed", errors.getvalue())

    def _initialized_repository(self, root: Path) -> Path:
        return support.init_repository(root / "repository")

    def test_stop_gate_blocks_once_then_respects_loop_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = self._initialized_repository(
                Path(temporary_directory)
            )
            support.initialize_repo_journal(repository)
            receive_hook(
                json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "session",
                        "cwd": str(repository),
                        "prompt": "unapplied work",
                    }
                ),
                git_executable=self.git_executable(),
            )
            task = enqueue_task(
                resolve_scope("repo", cwd=repository),
                title="Settle me",
                owner_id="gate-test",
            )
            stop_payload = {
                "hook_event_name": "Stop",
                "session_id": "session",
                "cwd": str(repository),
                "stop_hook_active": False,
            }
            blocked_errors = io.StringIO()
            guarded_errors = io.StringIO()

            blocked = receive_hook_main(
                input_stream=io.BytesIO(json.dumps(stop_payload).encode()),
                error_stream=blocked_errors,
                git_executable=self.git_executable(),
            )
            guarded = receive_hook_main(
                input_stream=io.BytesIO(
                    json.dumps(
                        {**stop_payload, "stop_hook_active": True}
                    ).encode()
                ),
                error_stream=guarded_errors,
                git_executable=self.git_executable(),
            )

            self.assertEqual(blocked, 2)
            lines = blocked_errors.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("AIQ: runnable work remains:", lines[0])
            self.assertIn("1 ready task", lines[0])
            self.assertIn("1 unapplied message", lines[0])
            task_id = task["task_id"]
            # The named ready task carries the project label; the settle
            # tail keeps the bare ID so it stays copy-pasteable.
            self.assertIn(f'[repository: {task_id}] "Settle me"', lines[0])
            self.assertIn(
                f"aiq task done {task_id} --summary TEXT", lines[0]
            )
            self.assertIn("aiq status", lines[0])
            self.assertEqual(guarded, 0)
            self.assertEqual(guarded_errors.getvalue(), "")

    def test_stop_gate_allows_silently_without_runnable_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = self._initialized_repository(
                Path(temporary_directory)
            )
            errors = io.StringIO()

            # No journal exists for the scope: nothing is runnable and
            # the gate creates no storage.
            status = receive_hook_main(
                input_stream=io.BytesIO(
                    json.dumps(
                        {
                            "hook_event_name": "Stop",
                            "session_id": "session",
                            "cwd": str(repository),
                        }
                    ).encode()
                ),
                error_stream=errors,
                git_executable=self.git_executable(),
            )
            scope = resolve_scope("repo", cwd=repository)

            self.assertEqual(status, 0)
            self.assertEqual(errors.getvalue(), "")
            self.assertFalse(scope.journal_path.exists())

    def _park_captured_message(self, repository: Path) -> None:
        """Capture one prompt, then park it ``needs_input``."""

        receipt = receive_hook(
            json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session",
                    "cwd": str(repository),
                    "prompt": "which color?",
                }
            ),
            git_executable=self.git_executable(),
        )
        scope = resolve_scope("repo", cwd=repository)
        claim = claim_message(
            scope,
            owner_id="gate-test",
            message_id=receipt["message_id"],
        )
        assert claim is not None
        dispose_message(
            scope,
            receipt["message_id"],
            claim_id=claim["claim_id"],
            disposition="needs_input",
            reason="awaiting a user answer",
        )

    def test_stop_gate_block_line_names_parked_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = self._initialized_repository(
                Path(temporary_directory)
            )
            support.initialize_repo_journal(repository)
            self._park_captured_message(repository)
            task = enqueue_task(
                resolve_scope("repo", cwd=repository),
                title="Settle me",
                owner_id="gate-test",
            )
            errors = io.StringIO()

            blocked = receive_hook_main(
                input_stream=io.BytesIO(
                    json.dumps(
                        {
                            "hook_event_name": "Stop",
                            "session_id": "session",
                            "cwd": str(repository),
                        }
                    ).encode()
                ),
                error_stream=errors,
                git_executable=self.git_executable(),
            )

            self.assertEqual(blocked, 2)
            lines = errors.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("AIQ: runnable work remains: 1 ready task", lines[0])
            # The parked fragment is appended after the ready-task
            # fragments and before the settle tail.
            self.assertIn(
                "; 1 parked message awaits user input — settle finished "
                f"work: aiq task done {task['task_id']} --summary TEXT",
                lines[0],
            )

    def test_stop_gate_notice_surfaces_parked_messages_without_blocking(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = self._initialized_repository(
                Path(temporary_directory)
            )
            support.initialize_repo_journal(repository)
            self._park_captured_message(repository)
            stop_payload = {
                "hook_event_name": "Stop",
                "session_id": "session",
                "cwd": str(repository),
            }
            notice_errors = io.StringIO()
            guarded_errors = io.StringIO()

            # Nothing is runnable, so the stop is allowed (exit 0), but
            # the parked question is surfaced as one stderr notice.
            noticed = receive_hook_main(
                input_stream=io.BytesIO(json.dumps(stop_payload).encode()),
                error_stream=notice_errors,
                git_executable=self.git_executable(),
            )
            # The loop guard stays fully silent even with parked messages.
            guarded = receive_hook_main(
                input_stream=io.BytesIO(
                    json.dumps(
                        {**stop_payload, "stop_hook_active": True}
                    ).encode()
                ),
                error_stream=guarded_errors,
                git_executable=self.git_executable(),
            )

            self.assertEqual(noticed, 0)
            self.assertEqual(
                notice_errors.getvalue(),
                "AIQ: no runnable work; 1 parked message awaits user "
                "input — aiq inbox list\n",
            )
            self.assertEqual(guarded, 0)
            self.assertEqual(guarded_errors.getvalue(), "")

    def _runnable_repository(self, root: Path) -> Path:
        """An opted-in repository holding exactly one ready task."""

        repository = self._initialized_repository(root)
        support.initialize_repo_journal(repository)
        enqueue_task(
            resolve_scope("repo", cwd=repository),
            title="Settle me",
            owner_id="gate-test",
        )
        return repository

    def _run_gate(self, repository: Path) -> tuple[int, str]:
        """Run one Stop payload through the gate as GATE_READER."""

        errors = io.StringIO()
        with patch.dict(os.environ, {"AIQ_READER": GATE_READER}):
            status = receive_hook_main(
                input_stream=io.BytesIO(
                    json.dumps(
                        {
                            "hook_event_name": "Stop",
                            "session_id": "session",
                            "cwd": str(repository),
                        }
                    ).encode()
                ),
                error_stream=errors,
                git_executable=self.git_executable(),
            )
        return status, errors.getvalue()

    def test_stop_gate_blocks_the_session_holding_the_reader_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = self._runnable_repository(Path(temporary_directory))
            acquire_reader_lease(
                resolve_scope("repo", cwd=repository),
                owner_id="gate-test",
                reader_id=GATE_READER,
                lease_seconds=3600,
            )

            status, errors = self._run_gate(repository)

            # The reader owes the work, so the block line is unchanged.
            self.assertEqual(status, 2)
            lines = errors.splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("AIQ: runnable work remains: 1 ready task", lines[0])
            self.assertIn('"Settle me"', lines[0])

    def test_stop_gate_lets_a_writer_only_session_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = self._runnable_repository(Path(temporary_directory))
            # A real process in its own POSIX session, so the lease
            # records a locator this host can probe: the only shape that
            # proves another session is draining the queue.
            with support.reader_lease_held_by_live_session(
                "repo",
                repository,
            ) as reader_id:
                status, errors = self._run_gate(repository)

            self.assertNotEqual(reader_id, GATE_READER)
            # That session owns the work, so a session that only files
            # work stops freely with one exit-0 notice.
            self.assertEqual(status, 0)
            self.assertEqual(
                errors,
                "AIQ: not blocking: runnable work remains (1 ready task) "
                f'but reader "{reader_id}" holds the reader lease — '
                "aiq reader status\n",
            )

    def test_stop_gate_stands_down_after_this_session_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = self._runnable_repository(Path(temporary_directory))
            scope = resolve_scope("repo", cwd=repository)
            # Without the release the gate blocks: ready work remains and
            # nothing says this session is done with it.
            blocking_status, blocking_errors = self._run_gate(repository)
            # The bounded run's deliberate stop: the role this session
            # took is handed back explicitly.
            support.release_reader_lease_from_this_session(scope)

            status, errors = self._run_gate(repository)

            self.assertEqual(blocking_status, 2)
            self.assertIn(
                "AIQ: runnable work remains: 1 ready task", blocking_errors
            )
            # An explicit release is this session declaring its batch
            # finished, so it stops with one exit-0 notice instead.
            self.assertEqual(status, 0)
            self.assertEqual(
                errors,
                "AIQ: not blocking: runnable work remains (1 ready task) "
                "but this session released the reader role — "
                "aiq reader status\n",
            )

    def test_stop_gate_release_notice_is_one_sanitized_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = self._initialized_repository(
                Path(temporary_directory)
            )
            support.initialize_repo_journal(repository)
            scope = resolve_scope("repo", cwd=repository)
            enqueue_task(
                scope,
                title="Ship\nthe\treport",
                owner_id="gate-test",
            )
            support.release_reader_lease_from_this_session(scope)

            status, errors = self._run_gate(repository)

            self.assertEqual(status, 0)
            # The notice names no task titles, so nothing hostile can
            # reach it; the boundary sanitizes it to one line regardless.
            self.assertEqual(len(errors.splitlines()), 1)
            self.assertTrue(errors.endswith("\n"))
            for character in ("\t", "\r"):
                self.assertNotIn(character, errors)

    def test_stop_gate_blocks_whenever_no_live_reader_holds_the_lease(
        self,
    ) -> None:
        def absent(scope: object) -> None:
            return None

        def expired(scope: object) -> None:
            acquire_reader_lease(
                scope,
                owner_id="drainer",
                reader_id=CONFIGURED_READER,
                lease_seconds=60,
                now_us=(time.time_ns() // 1000) - 3_600_000_000,
            )

        def released(scope: object) -> None:
            acquire_reader_lease(
                scope,
                owner_id="drainer",
                reader_id=CONFIGURED_READER,
                lease_seconds=3600,
            )
            release_reader_lease(scope, reader_id=CONFIGURED_READER)

        def released_by_another_session(scope: object) -> None:
            # A stranger's deliberate release says nothing about this
            # session, which is still accountable for the queue.
            support.release_reader_lease_with_locator(
                scope,
                host=socket.gethostname(),
                session=support.dead_session_id(),
            )

        def released_on_another_host(scope: object) -> None:
            support.release_reader_lease_with_locator(
                scope,
                host="other-host",
                session=os.getsid(0),
            )

        def dead_holder(scope: object) -> None:
            support.hold_reader_lease_from_dead_session(scope)

        def unlocated_holder(scope: object) -> None:
            # An explicitly configured identity records no locator, so
            # nothing tells this holder apart from the session running
            # the gate. A hook does not inherit the agent shell's
            # AIQ_READER, so this is exactly what the session holding
            # its own lease looks like from inside the gate.
            acquire_reader_lease(
                scope,
                owner_id="drainer",
                reader_id=CONFIGURED_READER,
                lease_seconds=3600,
            )

        def holder_on_another_host(scope: object) -> None:
            # A live session id, but on a host whose processes cannot be
            # probed from here, so liveness stays unproven.
            support.hold_reader_lease_with_locator(
                scope,
                host="other-host",
                session=os.getpid(),
            )

        def holder_in_this_session(scope: object) -> None:
            # The gate's own POSIX session, recorded under an identity
            # the gate does not resolve to: alive, but not somebody else.
            support.hold_reader_lease_with_locator(
                scope,
                host=socket.gethostname(),
                session=os.getsid(0),
            )

        # None of these proves a live reader other than this session will
        # do the work, nor is any of them this session declining it, so
        # the session that is stopping stays accountable. The dead holder
        # and the unlocated one are the load-bearing cases: an agent
        # harness leaves abandoned leases behind routinely, and a
        # configured identity names a holder that may well be this very
        # session, so honoring either would silently retire the gate. The
        # three releases are the same discipline applied to the
        # stand-down signal: only a release this session can be shown to
        # have made counts as this session declaring its batch finished.
        for lease, prepare in (
            ("absent", absent),
            ("expired", expired),
            ("released", released),
            ("released by another session", released_by_another_session),
            ("released on another host", released_on_another_host),
            ("dead holder", dead_holder),
            ("unlocated holder", unlocated_holder),
            ("holder on another host", holder_on_another_host),
            ("holder in this session", holder_in_this_session),
        ):
            with self.subTest(lease=lease):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    repository = self._runnable_repository(
                        Path(temporary_directory)
                    )
                    prepare(resolve_scope("repo", cwd=repository))

                    status, errors = self._run_gate(repository)

                    self.assertEqual(status, 2)
                    lines = errors.splitlines()
                    self.assertEqual(len(lines), 1)
                    self.assertIn(
                        "AIQ: runnable work remains: 1 ready task",
                        lines[0],
                    )

    def test_stop_gate_fails_open_on_gate_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = self._initialized_repository(
                Path(temporary_directory)
            )
            support.initialize_repo_journal(repository)
            receive_hook(
                json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "session",
                        "cwd": str(repository),
                        "prompt": "unapplied work",
                    }
                ),
                git_executable=self.git_executable(),
            )
            stop_payload = {
                "hook_event_name": "Stop",
                "session_id": "session",
                "cwd": str(repository),
            }

            # An invalid payload never blocks stopping.
            bad_payload_errors = io.StringIO()
            bad_payload = receive_hook_main(
                input_stream=io.BytesIO(
                    json.dumps(
                        {**stop_payload, "cwd": "relative/path"}
                    ).encode()
                ),
                error_stream=bad_payload_errors,
                git_executable=self.git_executable(),
            )

            # A locked journal never blocks stopping.
            locked_errors = io.StringIO()
            with patch(
                "aiq.queue.read_status",
                side_effect=JournalError("the journal is locked"),
            ):
                locked = receive_hook_main(
                    input_stream=io.BytesIO(
                        json.dumps(stop_payload).encode()
                    ),
                    error_stream=locked_errors,
                    git_executable=self.git_executable(),
                )

            # An unreadable journal never blocks stopping.
            scope = resolve_scope("repo", cwd=repository)
            scope.journal_path.write_bytes(b"not a sqlite database")
            unreadable_errors = io.StringIO()
            unreadable = receive_hook_main(
                input_stream=io.BytesIO(json.dumps(stop_payload).encode()),
                error_stream=unreadable_errors,
                git_executable=self.git_executable(),
            )

            for status, errors in (
                (bad_payload, bad_payload_errors),
                (locked, locked_errors),
                (unreadable, unreadable_errors),
            ):
                self.assertEqual(status, 0)
                lines = errors.getvalue().splitlines()
                self.assertEqual(len(lines), 1)
                self.assertIn("AIQ completion gate skipped:", lines[0])
            self.assertIn("locked", locked_errors.getvalue())


if __name__ == "__main__":
    unittest.main()
