from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
CANONICAL_AGENTS = REPOSITORY_ROOT / "AGENTS.md"


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
            self.repository,
            self.bin_directory,
        ):
            directory.mkdir()
        subprocess.run(
            ["git", "init", "--quiet", str(self.repository)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.launcher = self.bin_directory / "aiq"
        self.launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.launcher.chmod(0o755)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def environment(self) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("AIQ_")
            and key
            not in {
                "CODEX_HOME",
                "GIT_COMMON_DIR",
                "GIT_DIR",
                "GIT_WORK_TREE",
                "PYTHONPATH",
                "XDG_CONFIG_HOME",
                "XDG_STATE_HOME",
            }
        }
        environment.update(
            {
                "CODEX_HOME": str(self.codex_home),
                "HOME": str(self.home),
                "PATH": (
                    f"{self.bin_directory}{os.pathsep}"
                    f"{environment.get('PATH', os.defpath)}"
                ),
                "PYTHONPATH": str(SOURCE_ROOT),
                "XDG_STATE_HOME": str(self.state_home),
            }
        )
        return environment

    def run_aiq(
        self,
        *arguments: str,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "aiq", *arguments],
            cwd=self.repository,
            env=self.environment(),
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def assert_json_success(
        self,
        result: subprocess.CompletedProcess[str],
    ) -> dict[str, object]:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["v"], 1)
        return payload

    def lifecycle_command(
        self,
        operation: str,
        *,
        include_launcher: bool = True,
    ) -> subprocess.CompletedProcess[str]:
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
        self.assertTrue(command.startswith(str(self.launcher)))
        self.assertIn(" integration receive codex ", command)
        self.assertFalse(self.codex_home.exists())
        self.assertFalse((self.state_home / "aiq").exists())

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
        self.assertEqual(installed["hooks"]["Stop"], original["hooks"]["Stop"])
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

    def test_receive_is_silent_on_success_and_visible_on_error(self) -> None:
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
            input_text="{",
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(rejected.stdout, "")
        self.assertEqual(len(rejected.stderr.splitlines()), 1)
        self.assertIn("AIQ prompt capture failed:", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
