from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

import support


SOURCE_ROOT = support.SOURCE_ROOT
CANONICAL_AGENTS = support.REPOSITORY_ROOT / "AGENTS.md"


class IntegrationCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "home"
        self.state_home = self.root / "state"
        self.codex_home = self.root / "codex home"
        self.repository = self.root / "repository"
        self.bin_directory = self.root / "bin"
        for directory in (
            self.home,
            self.state_home,
            self.bin_directory,
        ):
            directory.mkdir()
        support.init_repository(self.repository)
        self.launcher = support.write_launcher(self.bin_directory / "aiq")
        self.git_executable = support.git_executable()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def environment(self) -> dict[str, str]:
        return support.scrubbed_environment(
            drop={"XDG_CONFIG_HOME"},
            CODEX_HOME=str(self.codex_home),
            HOME=str(self.home),
            PATH=(
                f"{self.bin_directory}{os.pathsep}"
                f"{os.environ.get('PATH', os.defpath)}"
            ),
            PYTHONPATH=str(SOURCE_ROOT),
            XDG_STATE_HOME=str(self.state_home),
        )

    def run_aiq(
        self,
        *arguments: str,
        input_text: str | None = None,
    ) -> support.CliResult:
        return support.run_cli(
            *arguments,
            in_process=False,
            cwd=self.repository,
            environment=self.environment(),
            input_text=input_text,
        )

    def assert_json_success(
        self,
        result: support.CliResult | subprocess.CompletedProcess[str],
    ) -> dict[str, object]:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["v"], 1)
        return payload

    def run_console(
        self,
        launcher: Path,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(launcher), *arguments],
            cwd=self.repository,
            env=self.environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def lifecycle_command(
        self,
        operation: str,
        *,
        include_launcher: bool = True,
    ) -> support.CliResult:
        arguments = ["integration", operation, "codex", "--user"]
        if include_launcher:
            arguments.extend(("--launcher", str(self.launcher)))
        arguments.append("--json")
        return self.run_aiq(*arguments)

    def test_list_and_print_surfaces_are_stable_and_read_only(self) -> None:
        listed = self.assert_json_success(
            self.run_aiq("integration", "list", "--json")
        )
        identifiers = {
            item["id"] if isinstance(item, dict) else item
            for item in listed["integrations"]
        }
        self.assertIn("codex", identifiers)
        self.assertIn("generic", identifiers)

        agents = self.run_aiq("integration", "print", "agents")
        self.assertEqual(agents.returncode, 0, agents.stderr)
        self.assertEqual(agents.stderr, "")
        self.assertEqual(
            agents.stdout,
            CANONICAL_AGENTS.read_text(encoding="utf-8"),
        )

        codex = self.run_aiq(
            "integration",
            "print",
            "codex",
            "--user",
            "--launcher",
            str(self.launcher),
        )
        self.assertEqual(codex.returncode, 0, codex.stderr)
        self.assertEqual(codex.stderr, "")
        fragment = json.loads(codex.stdout)
        command = fragment["hooks"]["UserPromptSubmit"][0]["hooks"][0][
            "command"
        ]
        self.assertEqual(
            shlex.split(command)[:4],
            [sys.executable, "-I", "-m", "aiq"],
        )
        self.assertIn(" integration receive codex ", command)
        self.assertFalse(self.codex_home.exists())
        self.assertFalse((self.state_home / "aiq").exists())

    def test_missing_user_selector_names_the_corrected_invocation(
        self,
    ) -> None:
        for integration_id in ("claude", "codex"):
            for operation in ("plan", "install", "check", "uninstall"):
                result = self.run_aiq(
                    "integration", operation, integration_id, "--json"
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                payload = json.loads(result.stderr)
                self.assertEqual(payload["code"], "invalid_argument")
                self.assertEqual(
                    payload["error"],
                    f"the {integration_id} integration requires --user: "
                    f"run aiq integration {operation} {integration_id} "
                    "--user",
                )

        with_target = self.run_aiq(
            "integration", "check", "codex",
            "--target", str(self.root / "guidance.md"), "--json",
        )
        self.assertEqual(with_target.returncode, 2, with_target.stderr)
        payload = json.loads(with_target.stderr)
        self.assertEqual(payload["code"], "invalid_argument")
        self.assertEqual(
            payload["error"],
            "the codex integration uses --user, not --target: "
            "run aiq integration check codex --user",
        )

    def test_codex_lifecycle_preserves_unrelated_hooks_and_is_idempotent(
        self,
    ) -> None:
        target = self.codex_home / "hooks.json"
        target.parent.mkdir()
        original = {
            "description": "user-owned",
            "unknown": {"preserve": True},
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "user-stop",
                            }
                        ]
                    }
                ],
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "user-prompt",
                            }
                        ]
                    }
                ],
            },
        }
        target.write_text(json.dumps(original), encoding="utf-8")

        plan = self.assert_json_success(self.lifecycle_command("plan"))
        self.assertEqual(plan["action"], "install")
        self.assertEqual(plan["status"], "absent")
        self.assertEqual(plan["target"], str(target))
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), original)
        self.assertFalse((self.state_home / "aiq").exists())

        first = self.assert_json_success(self.lifecycle_command("install"))
        self.assertEqual(first["action"], "install")
        self.assertEqual(first["status"], "installed")
        installed = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(installed["description"], original["description"])
        self.assertEqual(installed["unknown"], original["unknown"])
        self.assertEqual(
            installed["hooks"]["Stop"][0], original["hooks"]["Stop"][0]
        )
        self.assertEqual(len(installed["hooks"]["Stop"]), 2)
        self.assertIn(
            "integration receive codex",
            installed["hooks"]["Stop"][1]["hooks"][0]["command"],
        )
        self.assertEqual(
            installed["hooks"]["UserPromptSubmit"][0],
            original["hooks"]["UserPromptSubmit"][0],
        )
        self.assertEqual(len(installed["hooks"]["UserPromptSubmit"]), 2)

        installed_bytes = target.read_bytes()
        second = self.assert_json_success(self.lifecycle_command("install"))
        self.assertEqual(second["action"], "none")
        self.assertEqual(second["status"], "installed")
        self.assertEqual(target.read_bytes(), installed_bytes)

        checked = self.assert_json_success(self.lifecycle_command("check"))
        self.assertTrue(checked["ok"])
        self.assertEqual(checked["status"], "installed")

        first_uninstall = self.assert_json_success(
            self.lifecycle_command("uninstall", include_launcher=False)
        )
        self.assertEqual(first_uninstall["action"], "uninstall")
        self.assertEqual(first_uninstall["status"], "uninstalled")
        remaining = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(remaining, original)

        uninstalled_bytes = target.read_bytes()
        second_uninstall = self.assert_json_success(
            self.lifecycle_command("uninstall", include_launcher=False)
        )
        self.assertEqual(second_uninstall["action"], "none")
        self.assertEqual(second_uninstall["status"], "uninstalled")
        self.assertEqual(target.read_bytes(), uninstalled_bytes)

    def test_lifecycle_prefers_the_invoked_console_outside_path(self) -> None:
        environment_directory = self.root / "venv"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "venv",
                "--without-pip",
                str(environment_directory),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        python_executable = environment_directory / "bin" / "python"
        runtime_executable = Path(
            subprocess.run(
                [
                    str(python_executable),
                    "-c",
                    "import sys; print(sys.executable)",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout.strip()
        )
        site_packages = Path(
            subprocess.run(
                [
                    str(python_executable),
                    "-c",
                    "import site; print(site.getsitepackages()[0])",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ).stdout.strip()
        )
        (site_packages / "aiq-source.pth").write_text(
            f"{SOURCE_ROOT}\n",
            encoding="utf-8",
        )
        console_directory = self.root / "installed console"
        console_directory.mkdir()
        console = console_directory / "aiq"
        console.write_text(
            f"#!{python_executable}\n"
            "from aiq.cli import main\n"
            "raise SystemExit(main())\n",
            encoding="utf-8",
        )
        console.chmod(0o755)

        plan = self.assert_json_success(
            self.run_console(
                console,
                "integration",
                "plan",
                "codex",
                "--user",
                "--git-executable",
                str(self.git_executable),
                "--json",
            )
        )
        planned_command = plan["desired_group"]["UserPromptSubmit"]["hooks"][
            0
        ]["command"]
        self.assertEqual(
            plan["desired_group"]["Stop"]["hooks"][0]["command"],
            planned_command,
        )
        planned_argv = shlex.split(planned_command)
        self.assertEqual(
            planned_argv[:4],
            [str(runtime_executable), "-I", "-m", "aiq"],
        )
        self.assertEqual(
            planned_argv[planned_argv.index("--git-executable") + 1],
            str(self.git_executable),
        )

        printed = self.run_console(
            console,
            "integration",
            "print",
            "codex",
            "--user",
            "--git-executable",
            str(self.git_executable),
        )
        self.assertEqual(printed.returncode, 0, printed.stderr)
        print_command = json.loads(printed.stdout)["hooks"][
            "UserPromptSubmit"
        ][0]["hooks"][0]["command"]
        self.assertEqual(
            shlex.split(print_command)[:4],
            [str(runtime_executable), "-I", "-m", "aiq"],
        )

        installed = self.assert_json_success(
            self.run_console(
                console,
                "integration",
                "install",
                "codex",
                "--user",
                "--git-executable",
                str(self.git_executable),
                "--json",
            )
        )
        self.assertEqual(installed["status"], "installed")
        configured = json.loads(
            (self.codex_home / "hooks.json").read_text(encoding="utf-8")
        )
        installed_command = configured["hooks"]["UserPromptSubmit"][0][
            "hooks"
        ][0]["command"]
        installed_argv = shlex.split(installed_command)
        self.assertEqual(
            installed_argv[:4],
            [str(runtime_executable), "-I", "-m", "aiq"],
        )
        self.assertEqual(
            installed_argv[installed_argv.index("--git-executable") + 1],
            str(self.git_executable),
        )
        manifest = json.loads(
            (
                Path(installed["state_directory"]) / "manifest.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["launcher"], str(console))
        self.assertEqual(
            manifest["python_executable"],
            str(runtime_executable),
        )

        hostile_bin = self.root / "hostile-bin"
        hostile_bin.mkdir()
        hostile_sentinel = self.root / "hostile-git-ran"
        hostile_python_sentinel = self.root / "hostile-python-ran"
        hostile_python = self.root / "hostile-python"
        hostile_python.mkdir()
        (hostile_python / "aiq.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(hostile_python_sentinel)!r}).write_text('hostile')\n",
            encoding="utf-8",
        )
        hostile_git = hostile_bin / "git"
        hostile_git.write_text(
            "#!/bin/sh\n"
            f"printf hostile > {shlex.quote(str(hostile_sentinel))}\n"
            "exit 99\n",
            encoding="utf-8",
        )
        hostile_git.chmod(0o700)
        support.initialize_repo_journal(self.repository)
        for index, search_path in enumerate(("", str(hostile_bin))):
            payload = json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": f"portable-session-{index}",
                    "turn_id": f"portable-turn-{index}",
                    "cwd": str(self.repository),
                    "prompt": f"portable capture {index}",
                }
            )
            received = subprocess.run(
                installed_command,
                cwd=self.repository,
                env={
                    **self.environment(),
                    "PATH": search_path,
                    "PYTHONHOME": str(self.root / "hostile-home"),
                    "PYTHONPATH": str(hostile_python),
                },
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=True,
                executable="/bin/sh",
                check=False,
            )
            self.assertEqual(received.returncode, 0, received.stderr)
            self.assertEqual(received.stdout, "")
            self.assertEqual(received.stderr, "")
        self.assertFalse(hostile_sentinel.exists())
        self.assertFalse(hostile_python_sentinel.exists())

        checked = self.assert_json_success(
            self.run_console(
                console,
                "integration",
                "check",
                "codex",
                "--user",
                "--git-executable",
                str(self.git_executable),
                "--json",
            )
        )
        self.assertTrue(checked["ok"])

    def test_receive_is_silent_on_success_and_visible_on_error(self) -> None:
        support.initialize_repo_journal(self.repository)
        payload = json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "session",
                "turn_id": "turn",
                "cwd": str(self.repository),
                "prompt": "capture exactly\n",
            }
        )
        received = self.run_aiq(
            "integration",
            "receive",
            "codex",
            "--integration-id",
            "aiq-workqueue.codex.user-prompt.v1",
            "--git-executable",
            str(self.git_executable),
            input_text=payload,
        )
        self.assertEqual(received.returncode, 0, received.stderr)
        self.assertEqual(received.stdout, "")
        self.assertEqual(received.stderr, "")

        inbox = self.assert_json_success(
            self.run_aiq(
                "inbox",
                "list",
                "--scope",
                "repo",
                "--cwd",
                str(self.repository),
                "--include-content",
                "--json",
            )
        )
        self.assertEqual(len(inbox["messages"]), 1)
        self.assertEqual(inbox["messages"][0]["content"], "capture exactly\n")
        self.assertEqual(inbox["messages"][0]["source"], "codex")

        rejected = self.run_aiq(
            "integration",
            "receive",
            "codex",
            "--integration-id",
            "aiq-workqueue.codex.user-prompt.v1",
            "--git-executable",
            str(self.git_executable),
            input_text="{",
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertEqual(rejected.stdout, "")
        self.assertEqual(len(rejected.stderr.splitlines()), 1)
        self.assertIn("AIQ prompt capture failed:", rejected.stderr)

    def receive_stop(self, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        return self.run_aiq(
            "integration",
            "receive",
            "codex",
            "--integration-id",
            "aiq-workqueue.codex.user-prompt.v1",
            "--git-executable",
            str(self.git_executable),
            input_text=json.dumps(payload),
        )

    def test_receive_stop_runs_completion_gate(self) -> None:
        stop_payload = {
            "hook_event_name": "Stop",
            "session_id": "session",
            "cwd": str(self.repository),
        }

        # No journal exists yet: nothing is runnable, allow silently.
        empty = self.receive_stop(stop_payload)
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertEqual(empty.stdout, "")
        self.assertEqual(empty.stderr, "")

        support.initialize_repo_journal(self.repository)
        captured = self.run_aiq(
            "integration",
            "receive",
            "codex",
            "--integration-id",
            "aiq-workqueue.codex.user-prompt.v1",
            "--git-executable",
            str(self.git_executable),
            input_text=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session",
                    "turn_id": "turn",
                    "cwd": str(self.repository),
                    "prompt": "runnable work",
                }
            ),
        )
        self.assertEqual(captured.returncode, 0, captured.stderr)
        enqueued = self.run_aiq("enqueue", "Settle me", "--json")
        self.assertEqual(enqueued.returncode, 0, enqueued.stderr)
        task_id = json.loads(enqueued.stdout)["task_id"]

        # The ready task and unapplied message block stopping with one
        # actionable stderr line naming the task and the settle command.
        blocked = self.receive_stop(stop_payload)
        self.assertEqual(blocked.returncode, 2)
        self.assertEqual(blocked.stdout, "")
        lines = blocked.stderr.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("AIQ: runnable work remains:", lines[0])
        self.assertIn("1 ready task", lines[0])
        self.assertIn("1 unapplied message", lines[0])
        self.assertIn(f'{task_id} "Settle me"', lines[0])
        self.assertIn(f"aiq task done {task_id} --summary TEXT", lines[0])
        self.assertIn("aiq status", lines[0])

        # The host's loop guard allows the next stop attempt.
        guarded = self.receive_stop(
            {**stop_payload, "stop_hook_active": True}
        )
        self.assertEqual(guarded.returncode, 0, guarded.stderr)
        self.assertEqual(guarded.stdout, "")
        self.assertEqual(guarded.stderr, "")

        # Settle the task and park the captured message needs_input:
        # nothing is runnable, so the stop is allowed (exit 0), but the
        # parked question is surfaced as one non-blocking stderr notice.
        listed = self.run_aiq("inbox", "list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        message_id = json.loads(listed.stdout)["messages"][0]["message_id"]
        claimed = self.run_aiq(
            "inbox", "claim", message_id, "--owner", "gate-test", "--json",
        )
        self.assertEqual(claimed.returncode, 0, claimed.stderr)
        claim_id = json.loads(claimed.stdout)["claim"]["claim_id"]
        parked = self.run_aiq(
            "inbox", "needs-input", message_id,
            "--claim", claim_id,
            "--reason", "awaiting a user answer", "--json",
        )
        self.assertEqual(parked.returncode, 0, parked.stderr)
        settled = self.run_aiq(
            "task", "done", task_id,
            "--summary", "settled by the gate test",
            "--owner", "gate-test", "--json",
        )
        self.assertEqual(settled.returncode, 0, settled.stderr)

        noticed = self.receive_stop(stop_payload)
        self.assertEqual(noticed.returncode, 0)
        self.assertEqual(noticed.stdout, "")
        self.assertEqual(
            noticed.stderr,
            "AIQ: no runnable work; 1 parked message awaits user input "
            "— aiq inbox list\n",
        )


if __name__ == "__main__":
    unittest.main()
