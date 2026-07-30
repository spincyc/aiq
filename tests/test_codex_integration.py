from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
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
            status = {
                "messages": {"received": 0, "needs_input": 0},
                "tasks": {"ready": 2},
                "claims": {"active": 1},
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
                "AIQ: runnable work remains: 2 ready tasks, 1 active claim "
                "— run aiq status",
            )

    def test_stop_gate_ignores_parked_needs_input_messages(self) -> None:
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
                allowed = gate_hook(
                    payload,
                    git_executable=self.git_executable(),
                )
            with patch("aiq.queue.read_status", return_value=mixed):
                blocked = gate_hook(
                    payload,
                    git_executable=self.git_executable(),
                )

            # A parked needs_input message awaits the user, not the
            # agent: alone it never blocks stopping, and it never counts
            # toward the unapplied-message total.
            self.assertIsNone(allowed)
            self.assertEqual(
                blocked,
                "AIQ: runnable work remains: 1 unapplied message "
                "— run aiq status",
            )

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
