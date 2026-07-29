from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shlex
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from aiq.integrations import codex as codex_module
from aiq.integrations.codex import (
    CodexIntegrationError,
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


class CodexIntegrationTest(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[dict[str, str], Path]:
        home = root / "home"
        state = root / "state"
        codex_home = root / "codex home"
        launcher = root / "bin" / "aiq tool"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
        environment = {
            "HOME": str(home),
            "XDG_STATE_HOME": str(state),
            "CODEX_HOME": str(codex_home),
        }
        return environment, launcher

    def test_print_is_stable_fragment_with_quoted_absolute_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, launcher = self.fixture(root)

            first = print_integration(launcher=launcher)
            second = print_integration(launcher=launcher)
            document = json.loads(first)
            command = document["hooks"]["UserPromptSubmit"][0]["hooks"][0][
                "command"
            ]

            self.assertEqual(first, second)
            self.assertIn(f"--integration-id {INTEGRATION_ID}", command)
            self.assertTrue(command.startswith(shlex.quote(str(launcher))))
            self.assertIn(" integration receive codex ", command)

    def test_plan_and_install_merge_without_replacing_unrelated_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            target.parent.mkdir(parents=True)
            original = {
                "description": "mine",
                "unknown": {"keep": True},
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "stop"}]}],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "TOP_SECRET_UNRELATED_COMMAND",
                                }
                            ]
                        }
                    ],
                },
            }
            target.write_text(json.dumps(original))

            plan = plan_integration(launcher=launcher, environment=environment)
            result = install_integration(launcher=launcher, environment=environment)
            installed = json.loads(target.read_text())

            self.assertEqual(plan["action"], "install")
            self.assertEqual(plan["changes"][0]["op"], "add")
            self.assertNotIn("TOP_SECRET_UNRELATED_COMMAND", json.dumps(plan))
            self.assertNotIn("_after", plan)
            json.dumps(plan)
            self.assertEqual(result["status"], "installed")
            self.assertEqual(installed["description"], "mine")
            self.assertEqual(installed["unknown"], {"keep": True})
            self.assertEqual(installed["hooks"]["Stop"], original["hooks"]["Stop"])
            self.assertEqual(len(installed["hooks"]["UserPromptSubmit"]), 2)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_install_is_idempotent_and_check_requires_manual_trust(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)

            first = install_integration(launcher=launcher, environment=environment)
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            installed = target.read_bytes()
            second = install_integration(launcher=launcher, environment=environment)
            checked = check_integration(launcher=launcher, environment=environment)

            self.assertEqual(first["action"], "install")
            self.assertEqual(second["action"], "none")
            self.assertEqual(target.read_bytes(), installed)
            self.assertTrue(checked["ok"])
            self.assertEqual(checked["trust"], "manual_review_required")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_install_rejects_stale_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            plan = plan_integration(launcher=launcher, environment=environment)
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            target.parent.mkdir(parents=True)
            target.write_text('{"other":true}\n')

            with self.assertRaisesRegex(CodexIntegrationError, "stale"):
                install_integration(
                    launcher=launcher,
                    environment=environment,
                    plan_token=plan["plan_token"],
                )

    def test_symlink_target_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            target.parent.mkdir(parents=True)
            real = root / "managed-hooks.json"
            real.write_text("{}\n")
            target.symlink_to(real)

            plan = plan_integration(launcher=launcher, environment=environment)

            self.assertEqual(plan["status"], "unsafe")
            self.assertIn("regular file", plan["blocked_reason"])
            with self.assertRaises(CodexIntegrationError):
                install_integration(launcher=launcher, environment=environment)
            self.assertEqual(real.read_text(), "{}\n")

    def test_inline_hooks_and_disabled_hooks_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            codex_home = Path(environment["CODEX_HOME"])
            codex_home.mkdir(parents=True)
            config = codex_home / "config.toml"
            config.write_text("[hooks]\n")
            empty = plan_integration(launcher=launcher, environment=environment)
            config.write_text("[hooks]\n[[hooks.Stop]]\n")
            inline = plan_integration(launcher=launcher, environment=environment)
            config.write_text("[features]\nhooks = false\n")
            disabled = plan_integration(launcher=launcher, environment=environment)

            self.assertEqual(empty["action"], "install")
            self.assertEqual(inline["status"], "conflict")
            self.assertEqual(disabled["status"], "disabled")

    def test_drift_requires_explicit_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            install_integration(launcher=launcher, environment=environment)
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            document = json.loads(target.read_text())
            document["hooks"]["UserPromptSubmit"][0]["hooks"][0]["timeout"] = 11
            target.write_text(json.dumps(document))

            plan = plan_integration(launcher=launcher, environment=environment)
            self.assertEqual(plan["status"], "drifted")
            with self.assertRaisesRegex(CodexIntegrationError, "differs"):
                install_integration(launcher=launcher, environment=environment)

            repaired = install_integration(
                launcher=launcher,
                environment=environment,
                repair=True,
            )
            self.assertEqual(repaired["status"], "installed")
            self.assertEqual(
                json.loads(target.read_text())["hooks"]["UserPromptSubmit"][0][
                    "hooks"
                ][0]["timeout"],
                10,
            )
            uninstall_integration(environment=environment)
            self.assertFalse(target.exists())

    def test_uninstall_preserves_later_unrelated_changes_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            install_integration(launcher=launcher, environment=environment)
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            document = json.loads(target.read_text())
            document["hooks"]["Stop"] = [
                {"hooks": [{"type": "command", "command": "later"}]}
            ]
            target.write_text(json.dumps(document))

            first = uninstall_integration(environment=environment)
            second = uninstall_integration(environment=environment)
            remaining = json.loads(target.read_text())

            self.assertEqual(first["action"], "uninstall")
            self.assertEqual(second["action"], "none")
            self.assertIn("Stop", remaining["hooks"])
            self.assertNotIn("UserPromptSubmit", remaining["hooks"])

    def test_uninstall_removes_file_created_by_aiq_and_retains_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            installed = install_integration(
                launcher=launcher,
                environment=environment,
            )
            target = Path(environment["CODEX_HOME"]) / "hooks.json"

            result = uninstall_integration(environment=environment)
            backups = list(
                Path(installed["state_directory"]).joinpath("backups").glob("*")
            )

            self.assertFalse(target.exists())
            self.assertEqual(result["status"], "uninstalled")
            self.assertEqual(len(backups), 1)
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)

    def test_uninstall_rejects_tampered_manifest_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            installed = install_integration(
                launcher=launcher,
                environment=environment,
            )
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            manifest_path = Path(installed["state_directory"]) / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["created_file"] = "yes"
            manifest_path.write_text(json.dumps(manifest))

            with self.assertRaisesRegex(CodexIntegrationError, "ownership"):
                uninstall_integration(environment=environment)

            self.assertTrue(target.exists())

    def test_uninstall_preserves_unknown_optional_manifest_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            installed = install_integration(
                launcher=launcher,
                environment=environment,
            )
            manifest_path = Path(installed["state_directory"]) / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["future_optional"] = {
                "ignored_by_v1": True,
            }
            manifest_path.write_text(json.dumps(manifest))

            result = uninstall_integration(environment=environment)
            retained = json.loads(manifest_path.read_text())

            self.assertEqual(result["status"], "uninstalled")
            self.assertEqual(
                retained["future_optional"],
                {"ignored_by_v1": True},
            )

    def test_uninstall_fences_target_changed_immediately_before_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            install_integration(launcher=launcher, environment=environment)
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            original_assertion = codex_module._assert_target_unchanged

            def change_then_check(path, expected, expected_status):
                path.write_text('{"changed_by_other_process":true}\n')
                original_assertion(path, expected, expected_status)

            with patch.object(
                codex_module,
                "_assert_target_unchanged",
                side_effect=change_then_check,
            ):
                with self.assertRaisesRegex(
                    CodexIntegrationError,
                    "changed before mutation",
                ):
                    uninstall_integration(environment=environment)

            self.assertEqual(
                json.loads(target.read_text()),
                {"changed_by_other_process": True},
            )

    def test_receive_hook_is_idempotent_and_uses_codex_source(self) -> None:
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
                    "turn_id": "turn",
                    "cwd": str(repository),
                    "prompt": "persist exactly\n",
                }
            )

            first = receive_hook(payload)
            second = receive_hook(payload)
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
            self.assertEqual(source, "codex")

    def test_receive_hook_main_is_silent_on_success_and_exit_two_on_failure(
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
                    "turn_id": "turn",
                    "cwd": str(repository),
                    "prompt": "capture",
                }
            ).encode()
            errors = io.StringIO()

            success = receive_hook_main(
                input_stream=io.BytesIO(payload),
                error_stream=errors,
            )
            failure = receive_hook_main(
                input_stream=io.BytesIO(b"{}"),
                error_stream=errors,
            )

            self.assertEqual(success, 0)
            self.assertEqual(failure, 2)
            self.assertIn("capture failed", errors.getvalue())
if __name__ == "__main__":
    unittest.main()
