from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from aiq.integrations.claude import (
    ClaudeIntegrationError,
    INTEGRATION_ID,
    check_integration,
    install_integration,
    plan_integration,
    print_integration,
    receive_hook,
    receive_hook_main,
    uninstall_integration,
)
from aiq.journal import check_journal, resolve_scope


class ClaudeIntegrationTest(unittest.TestCase):
    def git_executable(self) -> Path:
        discovered = shutil.which("git")
        self.assertIsNotNone(discovered)
        return Path(discovered).absolute()

    def fixture(self, root: Path) -> tuple[dict[str, str], Path]:
        home = root / "home"
        state = root / "state"
        claude_config = root / "claude config"
        launcher = root / "bin" / "aiq tool"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
        environment = {
            "HOME": str(home),
            "XDG_STATE_HOME": str(state),
            "CLAUDE_CONFIG_DIR": str(claude_config),
            "PATH": str(self.git_executable().parent),
        }
        return environment, launcher

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
            self.assertEqual(
                document["hooks"]["Stop"], existing["hooks"]["Stop"]
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
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(
                ["git", "-C", str(repository), "init", "-q", "-b", "main"],
                check=True,
            )
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
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(
                ["git", "-C", str(repository), "init", "-q", "-b", "main"],
                check=True,
            )
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
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(
                ["git", "-C", str(repository), "init", "-q", "-b", "main"],
                check=True,
            )
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

    def test_receive_hook_main_is_silent_on_success_and_exit_one_on_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(
                ["git", "-C", str(repository), "init", "-q", "-b", "main"],
                check=True,
            )
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


if __name__ == "__main__":
    unittest.main()
