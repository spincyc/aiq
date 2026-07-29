from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aiq.integrations.guidance import (
    BEGIN_MARKER,
    END_MARKER,
    GuidanceIntegrationError,
    check_integration,
    install_integration,
    plan_integration,
    uninstall_integration,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
PACKAGED_AGENTS = SOURCE_ROOT / "aiq" / "_resources" / "AGENTS.md"


class GuidanceIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "home"
        self.state_home = self.root / "state"
        for directory in (self.home, self.state_home):
            directory.mkdir()
        self.target = self.root / "AGENTS.md"
        self.environment = {
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.state_home),
        }

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def install(self, **overrides: object) -> dict[str, object]:
        options: dict[str, object] = {
            "target": self.target,
            "environment": self.environment,
        }
        options.update(overrides)
        return install_integration(**options)

    def test_install_preserves_existing_content_and_appends_block(self) -> None:
        original = "# Project guidance\n\nKeep local rules.\n"
        self.target.write_text(original, encoding="utf-8")

        plan = plan_integration(
            target=self.target,
            environment=self.environment,
        )
        self.assertEqual((plan["status"], plan["action"]), ("absent", "install"))
        self.assertFalse(plan["created_file"])
        self.assertEqual(
            self.target.read_text(encoding="utf-8"),
            original,
            "plan must be read-only",
        )

        result = self.install(plan_token=plan["plan_token"])
        self.assertEqual(result["status"], "installed")
        backup = result["backup"]
        self.assertTrue(Path(str(backup)).is_file())
        self.assertEqual(
            Path(str(backup)).read_text(encoding="utf-8"),
            original,
        )

        content = self.target.read_text(encoding="utf-8")
        self.assertTrue(content.startswith(original))
        self.assertEqual(content.count(BEGIN_MARKER), 1)
        self.assertEqual(content.count(END_MARKER), 1)
        self.assertIn(PACKAGED_AGENTS.read_text(encoding="utf-8"), content)

        checked = check_integration(
            target=self.target,
            environment=self.environment,
        )
        self.assertTrue(checked["ok"])

    def test_reinstall_is_idempotent(self) -> None:
        self.target.write_text("existing\n", encoding="utf-8")
        self.install()
        installed = self.target.read_bytes()

        repeated = self.install()
        self.assertEqual(repeated["action"], "none")
        self.assertEqual(repeated["status"], "installed")
        self.assertEqual(self.target.read_bytes(), installed)

    def test_edited_block_is_drift_until_explicit_repair(self) -> None:
        self.target.write_text("existing\n", encoding="utf-8")
        self.install()
        installed = self.target.read_text(encoding="utf-8")
        self.target.write_text(
            installed.replace("work queue", "task list").replace(
                "thrift", "haste"
            ),
            encoding="utf-8",
        )

        plan = plan_integration(
            target=self.target,
            environment=self.environment,
        )
        self.assertEqual((plan["status"], plan["action"]), ("drifted", "block"))
        with self.assertRaisesRegex(GuidanceIntegrationError, "differs"):
            self.install()
        self.assertFalse(
            check_integration(
                target=self.target,
                environment=self.environment,
            )["ok"]
        )

        repaired = self.install(repair=True)
        self.assertEqual(repaired["status"], "installed")
        self.assertEqual(self.target.read_text(encoding="utf-8"), installed)

    def test_uninstall_restores_original_bytes_exactly(self) -> None:
        original = b"# Local guidance without trailing newline"
        self.target.write_bytes(original)
        self.install()
        self.assertNotEqual(self.target.read_bytes(), original)

        result = uninstall_integration(
            target=self.target,
            environment=self.environment,
        )
        self.assertEqual(result["status"], "uninstalled")
        self.assertFalse(result["deleted_file"])
        self.assertEqual(self.target.read_bytes(), original)

        replay = uninstall_integration(
            target=self.target,
            environment=self.environment,
        )
        self.assertEqual((replay["status"], replay["action"]), ("uninstalled", "none"))
        self.assertEqual(self.target.read_bytes(), original)

    def test_uninstall_removes_only_a_created_file(self) -> None:
        result = self.install()
        self.assertTrue(result["created_file"])
        self.assertTrue(self.target.is_file())

        uninstalled = uninstall_integration(
            target=self.target,
            environment=self.environment,
        )
        self.assertTrue(uninstalled["deleted_file"])
        self.assertFalse(self.target.exists())

    def test_relative_target_is_rejected(self) -> None:
        for operation in (
            plan_integration,
            install_integration,
            check_integration,
            uninstall_integration,
        ):
            with self.assertRaisesRegex(GuidanceIntegrationError, "absolute"):
                operation(
                    target="relative/AGENTS.md",
                    environment=self.environment,
                )

    def test_unmanaged_markers_block_every_operation(self) -> None:
        self.target.write_text(
            f"prelude\n{BEGIN_MARKER}\nhand-written\n{END_MARKER}\n",
            encoding="utf-8",
        )

        plan = plan_integration(
            target=self.target,
            environment=self.environment,
        )
        self.assertEqual((plan["status"], plan["action"]), ("unmanaged", "block"))
        with self.assertRaisesRegex(GuidanceIntegrationError, "unmanaged|manifest"):
            self.install(repair=True)
        with self.assertRaisesRegex(GuidanceIntegrationError, "manifest"):
            uninstall_integration(
                target=self.target,
                environment=self.environment,
            )

    def test_uninstall_refuses_a_drifted_owned_block(self) -> None:
        self.target.write_text("existing\n", encoding="utf-8")
        self.install()
        edited = self.target.read_text(encoding="utf-8").replace(
            "durable", "mutable"
        )
        self.target.write_text(edited, encoding="utf-8")

        with self.assertRaisesRegex(GuidanceIntegrationError, "drifted"):
            uninstall_integration(
                target=self.target,
                environment=self.environment,
            )
        self.assertEqual(self.target.read_text(encoding="utf-8"), edited)

    def test_stale_plan_token_is_rejected(self) -> None:
        self.target.write_text("first\n", encoding="utf-8")
        plan = plan_integration(
            target=self.target,
            environment=self.environment,
        )
        self.target.write_text("first\nsecond\n", encoding="utf-8")

        with self.assertRaisesRegex(GuidanceIntegrationError, "stale"):
            self.install(plan_token=plan["plan_token"])

    def run_aiq(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("AIQ_")
            and key not in {"PYTHONPATH", "XDG_STATE_HOME"}
        }
        environment.update(
            {
                "HOME": str(self.home),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(SOURCE_ROOT),
                "XDG_STATE_HOME": str(self.state_home),
            }
        )
        return subprocess.run(
            [sys.executable, "-m", "aiq", *arguments],
            cwd=self.root,
            env=environment,
            text=True,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_cli_requires_an_explicit_absolute_target(self) -> None:
        for arguments in (
            ("integration", "plan", "guidance", "--json"),
            (
                "integration", "plan", "guidance",
                "--target", "relative/AGENTS.md", "--json",
            ),
            (
                "integration", "plan", "guidance",
                "--target", str(self.target), "--user", "--json",
            ),
            (
                "integration", "receive", "guidance",
                "--git-executable", "/usr/bin/git",
            ),
        ):
            completed = self.run_aiq(*arguments)
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertEqual(completed.stdout, "")

        self.target.write_text("cli\n", encoding="utf-8")
        installed = self.run_aiq(
            "integration", "install", "guidance",
            "--target", str(self.target), "--json",
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        payload = json.loads(installed.stdout)
        self.assertEqual(
            (payload["v"], payload["integration"], payload["status"]),
            (1, "guidance", "installed"),
        )
        uninstalled = self.run_aiq(
            "integration", "uninstall", "guidance",
            "--target", str(self.target), "--json",
        )
        self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "cli\n")


if __name__ == "__main__":
    unittest.main()
