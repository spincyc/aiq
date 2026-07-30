from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shlex
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import support
from aiq.integrations import _hooks as hooks_engine
from aiq.integrations import codex as codex_module
from aiq.integrations.codex import (
    CodexIntegrationError,
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
from aiq.journal import check_journal, resolve_scope
from aiq.queue import (
    acquire_reader_lease,
    enqueue_task,
    release_reader_lease,
)


# The identity the gate itself resolves, and an explicitly configured
# one, which names a holder no locator can tell apart from this session.
GATE_READER = "gate-session"
CONFIGURED_READER = "another-configured-reader"


class CodexIntegrationTest(unittest.TestCase):
    def git_executable(self) -> Path:
        return support.git_executable()

    def fixture(self, root: Path) -> tuple[dict[str, str], Path]:
        return support.integration_fixture(root, CODEX_HOME="codex home")

    def test_print_is_stable_fragment_with_isolated_absolute_runtime(self) -> None:
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
            self.assertIn(
                f"--git-executable {shlex.quote(str(self.git_executable()))}",
                command,
            )
            self.assertTrue(
                command.startswith(
                    f"{shlex.quote(sys.executable)} -I -m aiq "
                )
            )
            self.assertIn(" integration receive codex ", command)
            self.assertNotIn(str(launcher), command)

    def test_relative_explicit_launcher_blocks_plan_and_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            environment, _ = self.fixture(Path(temporary_directory))

            plan = plan_integration(
                launcher=Path("bin/aiq"),
                environment=environment,
            )

            self.assertEqual(plan["status"], "unsafe")
            self.assertEqual(plan["action"], "block")
            self.assertIn("absolute", plan["blocked_reason"])
            with self.assertRaisesRegex(CodexIntegrationError, "absolute"):
                install_integration(
                    launcher=Path("bin/aiq"),
                    environment=environment,
                )

    def test_print_does_not_require_a_launcher(self) -> None:
        rendered = print_integration(
            git_executable=self.git_executable(),
            environment={},
        )

        self.assertIn(" integration receive codex ", rendered)

    def test_relative_explicit_git_executable_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, launcher = self.fixture(Path(temporary_directory))

            with self.assertRaisesRegex(CodexIntegrationError, "absolute"):
                print_integration(
                    launcher=launcher,
                    git_executable=Path("bin/git"),
                )

    def test_relative_explicit_python_executable_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, launcher = self.fixture(Path(temporary_directory))

            with self.assertRaisesRegex(CodexIntegrationError, "absolute"):
                print_integration(
                    launcher=launcher,
                    python_executable=Path("bin/python"),
                )

    def test_explicit_absolute_launcher_preserves_symlink_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, real_launcher = self.fixture(root)
            launcher_link = root / "bin" / "aiq-link"
            launcher_link.symlink_to(real_launcher)

            environment, _ = self.fixture(root / "other")
            installed = install_integration(
                launcher=launcher_link,
                environment=environment,
            )
            manifest = json.loads(
                (Path(installed["state_directory"]) / "manifest.json").read_text()
            )

            self.assertEqual(manifest["launcher"], str(launcher_link))
            self.assertNotEqual(manifest["launcher"], str(real_launcher))

    def test_invoked_launcher_wins_over_competing_path_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, invoked_launcher = self.fixture(root)
            competing_launcher = root / "competing" / "aiq"
            competing_launcher.parent.mkdir()
            competing_launcher.write_text("#!/bin/sh\nexit 0\n")
            competing_launcher.chmod(0o755)
            environment["PATH"] = os.pathsep.join(
                (str(competing_launcher.parent), environment["PATH"])
            )

            installed = install_integration(
                invoked_launcher=invoked_launcher,
                environment=environment,
            )
            document = json.loads(
                (Path(environment["CODEX_HOME"]) / "hooks.json").read_text()
            )
            command = document["hooks"]["UserPromptSubmit"][0]["hooks"][0][
                "command"
            ]
            manifest = json.loads(
                (Path(installed["state_directory"]) / "manifest.json").read_text()
            )

            self.assertTrue(
                command.startswith(
                    f"{shlex.quote(sys.executable)} -I -m aiq "
                )
            )
            self.assertNotIn(str(invoked_launcher), command)
            self.assertNotIn(str(competing_launcher), command)
            self.assertEqual(manifest["launcher"], str(invoked_launcher))

    def test_invoked_launcher_does_not_require_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, invoked_launcher = self.fixture(root)

            rendered = print_integration(
                invoked_launcher=invoked_launcher,
                git_executable=self.git_executable(),
                environment={},
            )

            self.assertIn(
                f"{shlex.quote(sys.executable)} -I -m aiq",
                rendered,
            )
            self.assertNotIn(str(invoked_launcher), rendered)
            with self.assertRaisesRegex(
                CodexIntegrationError,
                "cannot determine",
            ):
                print_integration(environment={})

    def test_explicit_git_wins_over_competing_setup_path_and_is_stored(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            reviewed_git = root / "reviewed bin" / "git tool"
            reviewed_git.parent.mkdir()
            reviewed_git.write_text("#!/bin/sh\nexit 0\n")
            reviewed_git.chmod(0o755)
            competing_git = root / "competing" / "git"
            competing_git.parent.mkdir()
            competing_git.write_text("#!/bin/sh\nexit 99\n")
            competing_git.chmod(0o755)
            environment["PATH"] = str(competing_git.parent)

            installed = install_integration(
                launcher=launcher,
                git_executable=reviewed_git,
                environment=environment,
            )
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            command = json.loads(target.read_text())["hooks"][
                "UserPromptSubmit"
            ][0]["hooks"][0]["command"]
            manifest = json.loads(
                (Path(installed["state_directory"]) / "manifest.json").read_text()
            )

            self.assertIn(
                f"--git-executable {shlex.quote(str(reviewed_git))}",
                command,
            )
            self.assertNotIn(str(competing_git), command)
            self.assertEqual(manifest["git_executable"], str(reviewed_git))

    def test_explicit_python_with_spaces_is_quoted_stored_and_isolated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            reviewed_python = root / "reviewed runtime" / "python tool"
            reviewed_python.parent.mkdir()
            reviewed_python.write_text("#!/bin/sh\nexit 0\n")
            reviewed_python.chmod(0o755)
            environment["PYTHONPATH"] = str(root / "hostile modules")
            environment["PYTHONHOME"] = str(root / "hostile home")

            installed = install_integration(
                launcher=launcher,
                python_executable=reviewed_python,
                environment=environment,
            )
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            command = json.loads(target.read_text())["hooks"][
                "UserPromptSubmit"
            ][0]["hooks"][0]["command"]
            manifest = json.loads(
                (Path(installed["state_directory"]) / "manifest.json").read_text()
            )

            self.assertEqual(
                shlex.split(command)[:4],
                [str(reviewed_python), "-I", "-m", "aiq"],
            )
            self.assertEqual(
                manifest["python_executable"],
                str(reviewed_python),
            )
            self.assertNotIn(environment["PYTHONPATH"], command)
            self.assertNotIn(environment["PYTHONHOME"], command)

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
            target.chmod(0o644)

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
            self.assertEqual(
                installed["hooks"]["Stop"][0], original["hooks"]["Stop"][0]
            )
            self.assertEqual(len(installed["hooks"]["Stop"]), 2)
            self.assertIn(
                f"--integration-id {INTEGRATION_ID}",
                installed["hooks"]["Stop"][1]["hooks"][0]["command"],
            )
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
            config.write_text(
                '[hooks.state]\n'
                '[hooks.state."/x/hooks.json:user_prompt_submit:0:0"]\n'
                'trusted_hash = "sha256:abc"\n'
            )
            trust_only = plan_integration(
                launcher=launcher,
                environment=environment,
            )
            config.write_text("[features]\nhooks = false\n")
            disabled = plan_integration(launcher=launcher, environment=environment)

            self.assertEqual(empty["action"], "install")
            self.assertEqual(inline["status"], "conflict")
            self.assertEqual(trust_only["action"], "install")
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

    def test_git_executable_change_is_drift_until_explicit_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            first_git = root / "first" / "git"
            second_git = root / "second" / "git"
            for executable in (first_git, second_git):
                executable.parent.mkdir()
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)
            install_integration(
                launcher=launcher,
                git_executable=first_git,
                environment=environment,
            )

            checked = check_integration(
                launcher=launcher,
                git_executable=second_git,
                environment=environment,
            )
            repaired = install_integration(
                launcher=launcher,
                git_executable=second_git,
                environment=environment,
                repair=True,
            )
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            command = json.loads(target.read_text())["hooks"][
                "UserPromptSubmit"
            ][0]["hooks"][0]["command"]

            self.assertEqual(checked["status"], "drifted")
            self.assertFalse(checked["ok"])
            self.assertEqual(repaired["status"], "installed")
            self.assertIn(
                f"--git-executable {shlex.quote(str(second_git))}",
                command,
            )

    def test_uninstall_preserves_later_unrelated_changes_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            install_integration(launcher=launcher, environment=environment)
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            document = json.loads(target.read_text())
            document["hooks"]["Stop"].append(
                {"hooks": [{"type": "command", "command": "later"}]}
            )
            target.write_text(json.dumps(document))

            first = uninstall_integration(environment=environment)
            second = uninstall_integration(environment=environment)
            remaining = json.loads(target.read_text())

            self.assertEqual(first["action"], "uninstall")
            self.assertEqual(first["integration_id"], INTEGRATION_ID)
            self.assertFalse(first["deleted_file"])
            self.assertEqual(second["action"], "none")
            self.assertEqual(second["integration_id"], INTEGRATION_ID)
            self.assertEqual(
                remaining["hooks"]["Stop"],
                [{"hooks": [{"type": "command", "command": "later"}]}],
            )
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
            self.assertEqual(result["integration_id"], INTEGRATION_ID)
            self.assertTrue(result["deleted_file"])
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

    def test_manifest_requires_valid_absolute_git_executable(self) -> None:
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
            manifest["git_executable"] = "git"
            manifest_path.write_text(json.dumps(manifest))

            with self.assertRaisesRegex(
                CodexIntegrationError,
                "Git executable is invalid",
            ):
                uninstall_integration(environment=environment)

            self.assertTrue(target.exists())

    def test_manifest_requires_valid_absolute_python_executable(self) -> None:
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
            manifest["python_executable"] = "python"
            manifest_path.write_text(json.dumps(manifest))

            with self.assertRaisesRegex(
                CodexIntegrationError,
                "Python executable is invalid",
            ):
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
            original_assertion = hooks_engine._assert_target_unchanged

            def change_then_check(spec, path, expected, expected_status):
                path.write_text('{"changed_by_other_process":true}\n')
                original_assertion(spec, path, expected, expected_status)

            with patch.object(
                hooks_engine,
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
            repository = support.init_repository(root / "repository")
            support.initialize_repo_journal(repository)
            payload = json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session",
                    "turn_id": "turn",
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
            self.assertEqual(source, "codex")

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
                    "turn_id": "turn",
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
            self.assertEqual(receipt["source"], "codex")
            self.assertEqual(receipt["scope"], scope.to_dict())
            self.assertNotIn("created", receipt)
            self.assertEqual(status, 0)
            self.assertEqual(errors.getvalue(), "")
            self.assertFalse(scope.journal_path.exists())
            self.assertFalse((repository / ".git" / "aiq").exists())

    def test_receive_hook_requires_turn_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            payload = json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session",
                    "cwd": temporary_directory,
                    "prompt": "capture",
                }
            )

            with self.assertRaisesRegex(
                CodexIntegrationError,
                "Codex hook turn_id must be a non-empty string",
            ):
                receive_hook(payload, git_executable=self.git_executable())

    def test_receive_hook_captures_changed_content_for_same_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            support.initialize_repo_journal(repository)
            other = support.init_repository(root / "other")
            support.initialize_repo_journal(other)
            base = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session",
                "turn_id": "turn",
                "cwd": str(repository),
                "prompt": "original",
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
            moved = receive_hook(
                json.dumps({**base, "cwd": str(other)}),
                git_executable=self.git_executable(),
            )
            scope = resolve_scope("repo", cwd=repository)
            checked = check_journal(scope)

            self.assertTrue(first["created"])
            self.assertFalse(replayed["created"])
            self.assertTrue(expanded["created"])
            self.assertTrue(moved["created"])
            self.assertEqual(checked["messages"], 2)

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
                    "turn_id": "turn",
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

    def _runnable_repository(self, root: Path) -> Path:
        """An opted-in repository holding exactly one ready task."""

        repository = support.init_repository(root / "repository")
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

    def test_stop_gate_blocks_a_release_that_leaves_own_claims_behind(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = self._runnable_repository(Path(temporary_directory))
            scope = resolve_scope("repo", cwd=repository)
            enqueue_task(scope, title="Also settle me", owner_id="gate-test")
            # Release leaves per-item claims alone by design, so stopping
            # here would strand this session's own claimed task until its
            # lease expired.
            support.claim_next_task_with_locator(
                scope,
                locator=(socket.gethostname(), os.getsid(0)),
                owner_id="gate-test",
            )
            support.release_reader_lease_from_this_session(scope)

            status, errors = self._run_gate(repository)

            self.assertEqual(status, 2)
            self.assertEqual(
                errors,
                "AIQ: this session released the reader role but still "
                "holds 1 active claim of its own (1 ready task, 1 active "
                "claim) — settle finished work: aiq task done TASK_ID "
                "--summary TEXT — or hand it back: aiq claim release "
                "CLAIM_ID — list yours: aiq claim list --status active\n",
            )

    def test_stop_gate_release_stands_down_over_a_foreign_sessions_claim(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = self._runnable_repository(Path(temporary_directory))
            scope = resolve_scope("repo", cwd=repository)
            enqueue_task(scope, title="Not mine", owner_id="gate-test")
            # `owner_id` defaults to the OS user, so a concurrent session
            # claims under the same owner; only the locator separates them.
            support.claim_next_task_from_another_session(scope)
            support.release_reader_lease_from_this_session(scope)

            status, errors = self._run_gate(repository)

            self.assertEqual(status, 0)
            self.assertEqual(
                errors,
                "AIQ: not blocking: runnable work remains (1 ready task, "
                "1 active claim) but this session released the reader "
                "role — aiq reader status\n",
            )

    def test_stop_gate_release_notice_is_exactly_one_fixed_line(self) -> None:
        """Pin the release notice itself, not the boundary beneath it.

        This branch renders counts and fixed words only -- no task title
        reaches it -- so it can prove nothing about the stderr
        sanitizer, and asserting sanitization here passed against an
        identity sanitizer. What it can prove is what the notice
        actually says, which is the contract a bounded run reads, so
        that is what it pins. Sanitization is covered where hostile text
        really does reach the boundary, in
        :meth:`test_stop_gate_escapes_and_truncates_hostile_titles`.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = support.init_repository(
                Path(temporary_directory) / "repository"
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
            self.assertEqual(
                errors,
                "AIQ: not blocking: runnable work remains (1 ready task) "
                "but this session released the reader role"
                " — aiq reader status\n",
            )

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

    def test_stop_gate_blocks_once_then_respects_loop_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            support.initialize_repo_journal(repository)
            receive_hook(
                json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "session",
                        "turn_id": "turn",
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
            self.assertIn("1 unapplied message", lines[0])
            self.assertIn("run aiq status", lines[0])
            self.assertEqual(guarded, 0)
            self.assertEqual(guarded_errors.getvalue(), "")

    def test_stop_gate_reports_ready_tasks_and_active_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
            status = {
                "project": "aiq",
                "messages": {"received": 0, "needs_input": 0},
                "tasks": {"ready": 2},
                "claims": {"active": 1},
                "ready": [
                    {
                        "task_id": "TASK-7",
                        "priority": 5,
                        "title": "Ship the release notes",
                        "created_at": two_hours_ago.isoformat(),
                    },
                    {
                        "task_id": "TASK-9",
                        "priority": 3,
                        # Over the 40-character budget; the age is
                        # omitted because created_at is unparseable.
                        "title": "x" * 50,
                        "created_at": "not-a-timestamp",
                    },
                ],
            }

            with patch("aiq.queue.read_status", return_value=status):
                reason = gate_hook(
                    json.dumps(
                        {
                            "hook_event_name": "Stop",
                            "session_id": "session",
                            "cwd": str(repository),
                        }
                    ),
                    git_executable=self.git_executable(),
                )

            truncated = "x" * 39 + "…"
            self.assertEqual(
                reason,
                (
                    True,
                    "AIQ: runnable work remains: 2 ready tasks, "
                    "1 active claim: [aiq: TASK-7] "
                    '"Ship the release notes" '
                    f'(open 2h); [aiq: TASK-9] "{truncated}" '
                    "— settle finished work: aiq task done TASK-7 "
                    "--summary TEXT — or: aiq status",
                ),
            )

    def test_stop_gate_omits_label_when_status_reports_none(self) -> None:
        """A status shape without a project label still blocks, unlabeled.

        The gate's posture is fail-open: a patched or older read_status
        that reports no ``project`` must degrade to the bare task ID
        rather than render an empty label such as ``[: TASK-2]``.
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            status = {
                "messages": {"received": 0, "needs_input": 0},
                "tasks": {"ready": 1},
                "claims": {"active": 0},
                "ready": [
                    {"task_id": "TASK-2", "priority": 1, "title": "Unlabeled"}
                ],
            }

            with patch("aiq.queue.read_status", return_value=status):
                reason = gate_hook(
                    json.dumps(
                        {
                            "hook_event_name": "Stop",
                            "session_id": "session",
                            "cwd": str(repository),
                        }
                    ),
                    git_executable=self.git_executable(),
                )

            self.assertEqual(
                reason,
                (
                    True,
                    'AIQ: runnable work remains: 1 ready task: TASK-2 '
                    '"Unlabeled" — settle finished work: aiq task done '
                    "TASK-2 --summary TEXT — or: aiq status",
                ),
            )

    def test_stop_gate_names_at_most_three_ready_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            status = {
                "project": "aiq",
                "messages": {"received": 0, "needs_input": 0},
                "tasks": {"ready": 4},
                "claims": {"active": 0},
                "ready": [
                    {"task_id": f"TASK-{number}", "priority": 1, "title": f"t{number}"}
                    for number in range(1, 5)
                ],
            }

            with patch("aiq.queue.read_status", return_value=status):
                reason = gate_hook(
                    json.dumps(
                        {
                            "hook_event_name": "Stop",
                            "session_id": "session",
                            "cwd": str(repository),
                        }
                    ),
                    git_executable=self.git_executable(),
                )

            self.assertIsNotNone(reason)
            blocking, line = reason
            self.assertTrue(blocking)
            for named in ("TASK-1", "TASK-2", "TASK-3"):
                self.assertIn(f'[aiq: {named}] "', line)
            self.assertNotIn("TASK-4", line)
            # The settle tail names the first ready task bare, so it can
            # be copied straight into a shell.
            self.assertIn("aiq task done TASK-1 --summary TEXT", line)

    def test_stop_gate_escapes_and_truncates_hostile_titles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            status = {
                "project": "aiq",
                "messages": {"received": 0, "needs_input": 0},
                "tasks": {"ready": 1},
                "claims": {"active": 0},
                "ready": [
                    {
                        "task_id": "TASK-3",
                        "priority": 0,
                        "title": (
                            "line one\nline two\t"
                            "tabbed tail that keeps going past forty"
                        ),
                    }
                ],
            }
            errors = io.StringIO()

            with patch("aiq.queue.read_status", return_value=status):
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
            # The title is truncated to 40 characters first, then the
            # stderr boundary escapes the embedded newline and tab.
            self.assertIn(
                '[aiq: TASK-3] "line one\\u000aline two\\u0009'
                'tabbed tail that keep…"',
                lines[0],
            )
            self.assertNotIn("\t", lines[0])
            self.assertIn("aiq task done TASK-3 --summary TEXT", lines[0])

    def test_stop_gate_surfaces_parked_needs_input_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            payload = json.dumps(
                {
                    "hook_event_name": "Stop",
                    "session_id": "session",
                    "cwd": str(repository),
                }
            )
            parked_only = {
                "messages": {"received": 0, "needs_input": 3},
                "tasks": {"ready": 0},
                "claims": {"active": 0},
            }
            mixed = {
                "messages": {"received": 1, "needs_input": 2},
                "tasks": {"ready": 0},
                "claims": {"active": 0},
            }

            with patch("aiq.queue.read_status", return_value=parked_only):
                noticed = gate_hook(
                    payload,
                    git_executable=self.git_executable(),
                )
            with patch("aiq.queue.read_status", return_value=mixed):
                blocked = gate_hook(
                    payload,
                    git_executable=self.git_executable(),
                )

            # A parked needs_input message awaits the user, not the
            # agent: it never blocks stopping and never counts toward
            # the unapplied-message total, but it is surfaced — as a
            # non-blocking notice when nothing is runnable, and as an
            # appended fragment on the block line otherwise.
            self.assertEqual(
                noticed,
                (
                    False,
                    "AIQ: no runnable work; 3 parked messages await "
                    "user input — aiq inbox list",
                ),
            )
            self.assertEqual(
                blocked,
                (
                    True,
                    "AIQ: runnable work remains: 1 unapplied message; "
                    "2 parked messages await user input — run aiq status",
                ),
            )

    def test_stop_gate_notice_exits_zero_with_one_stderr_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            parked_only = {
                "messages": {"received": 0, "needs_input": 1},
                "tasks": {"ready": 0},
                "claims": {"active": 0},
            }
            stop_payload = {
                "hook_event_name": "Stop",
                "session_id": "session",
                "cwd": str(repository),
            }
            notice_errors = io.StringIO()
            guarded_errors = io.StringIO()

            with patch("aiq.queue.read_status", return_value=parked_only):
                noticed = receive_hook_main(
                    input_stream=io.BytesIO(
                        json.dumps(stop_payload).encode()
                    ),
                    error_stream=notice_errors,
                    git_executable=self.git_executable(),
                )
                # The loop guard stays fully silent even with parked
                # messages.
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

    def test_receive_hook_uses_reviewed_git_with_hostile_empty_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            repository.mkdir()
            git_executable = self.git_executable()
            subprocess.run(
                [
                    str(git_executable),
                    "-C",
                    str(repository),
                    "init",
                    "-q",
                    "-b",
                    "main",
                ],
                check=True,
                env={**os.environ, **support.GIT_ISOLATION},
            )
            support.initialize_repo_journal(repository)
            hostile_directory = root / "hostile"
            hostile_directory.mkdir()
            hostile_git = hostile_directory / "git"
            hostile_git.write_text("#!/bin/sh\nexit 97\n")
            hostile_git.chmod(0o755)
            payload = json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session",
                    "turn_id": "turn",
                    "cwd": str(repository),
                    "prompt": "capture without hook PATH",
                }
            ).encode()
            errors = io.StringIO()

            for hook_path in ("", str(hostile_directory)):
                with self.subTest(hook_path=hook_path):
                    with patch.dict(
                        os.environ,
                        {"PATH": hook_path},
                        clear=True,
                    ):
                        status = receive_hook_main(
                            input_stream=io.BytesIO(payload),
                            error_stream=errors,
                            git_executable=git_executable,
                        )
                    self.assertEqual(status, 0, errors.getvalue())

            scope = resolve_scope(
                "repo",
                cwd=repository,
                git_executable=git_executable,
            )
            self.assertTrue(scope.journal_path.is_file())
if __name__ == "__main__":
    unittest.main()
