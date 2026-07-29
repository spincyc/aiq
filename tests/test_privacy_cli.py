from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"


class PrivacyCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "home"
        self.config_home = self.root / "config"
        self.state_home = self.root / "state"
        self.codex_home = self.root / "codex"
        self.working_directory = self.root / "work"
        for directory in (
            self.home,
            self.config_home,
            self.state_home,
            self.codex_home,
            self.working_directory,
        ):
            directory.mkdir()

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
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(SOURCE_ROOT),
                "XDG_CONFIG_HOME": str(self.config_home),
                "XDG_STATE_HOME": str(self.state_home),
            }
        )
        return environment

    def run_aiq(
        self,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "aiq", *arguments],
            cwd=self.working_directory,
            env=self.environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def json_success(
        self,
        completed: subprocess.CompletedProcess[str],
    ) -> dict[str, object]:
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout.count("\n"), 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["v"], 1)
        return payload

    def json_state_conflict(
        self,
        completed: subprocess.CompletedProcess[str],
    ) -> dict[str, object]:
        self.assertEqual(completed.returncode, 4, completed)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr.count("\n"), 1)
        payload = json.loads(completed.stderr)
        self.assertEqual(payload["v"], 1)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], "state_conflict")
        self.assertIsInstance(payload["error"], str)
        return payload

    def ingest_private_message(self) -> None:
        completed = self.run_aiq(
            "ingest",
            "--message",
            "exact private content\n",
            "--scope",
            "user",
            "--json",
        )
        payload = self.json_success(completed)
        self.assertTrue(payload["created"])

    def destroy_plan(self) -> dict[str, object]:
        return self.json_success(
            self.run_aiq(
                "journal",
                "destroy",
                "--plan",
                "--scope",
                "user",
                "--json",
            )
        )

    def destroy_confirm(self, token: str) -> subprocess.CompletedProcess[str]:
        return self.run_aiq(
            "journal",
            "destroy",
            "--confirm",
            token,
            "--scope",
            "user",
            "--json",
        )

    def test_export_is_versioned_private_and_refuses_clobber(self) -> None:
        self.ingest_private_message()
        export = self.root / "journal-export.jsonl"

        payload = self.json_success(
            self.run_aiq(
                "journal",
                "export",
                str(export),
                "--scope",
                "user",
                "--json",
            )
        )

        self.assertEqual(payload["status"], "exported")
        self.assertEqual(payload["output_path"], str(export.resolve()))
        self.assertEqual(payload["format"], "aiq-journal-jsonl")
        self.assertEqual(payload["format_version"], 1)
        self.assertEqual(stat.S_IMODE(export.stat().st_mode), 0o600)
        original = export.read_bytes()

        conflict = self.run_aiq(
            "journal",
            "export",
            str(export),
            "--scope",
            "user",
            "--json",
        )

        self.json_state_conflict(conflict)
        self.assertEqual(export.read_bytes(), original)

    def test_wrong_destroy_token_is_versioned_and_preserves_state(self) -> None:
        self.ingest_private_message()
        plan = self.destroy_plan()
        self.assertEqual(plan["status"], "confirmation_required")
        self.assertTrue(plan["journal_present"])
        journal_path = Path(plan["scope"]["journal_path"])

        conflict = self.destroy_confirm("0" * 64)

        self.json_state_conflict(conflict)
        self.assertTrue(journal_path.is_file())
        second_plan = self.destroy_plan()
        self.assertEqual(
            second_plan["confirmation_token"],
            plan["confirmation_token"],
        )

    def test_destroy_retains_external_export_and_integration_state(self) -> None:
        self.ingest_private_message()
        export = self.root / "external-export.jsonl"
        self.json_success(
            self.run_aiq(
                "journal",
                "export",
                str(export),
                "--scope",
                "user",
                "--json",
            )
        )
        export_bytes = export.read_bytes()
        hooks = self.codex_home / "hooks.json"
        hooks_bytes = b'{"hooks":{"Unrelated":[{"hooks":[]}]}}\n'
        hooks.write_bytes(hooks_bytes)

        plan = self.destroy_plan()
        journal_path = Path(plan["scope"]["journal_path"])
        destroyed = self.json_success(
            self.destroy_confirm(str(plan["confirmation_token"]))
        )

        self.assertEqual(destroyed["status"], "destroyed")
        self.assertGreaterEqual(destroyed["deleted_files"], 1)
        self.assertFalse(journal_path.exists())
        self.assertEqual(export.read_bytes(), export_bytes)
        self.assertEqual(hooks.read_bytes(), hooks_bytes)

    def test_absent_destroy_is_repeatable_success(self) -> None:
        first_plan = self.destroy_plan()
        self.assertEqual(first_plan["status"], "already_absent")
        first = self.json_success(
            self.destroy_confirm(str(first_plan["confirmation_token"]))
        )
        self.assertEqual(first["status"], "already_absent")
        self.assertEqual(first["deleted_files"], 0)

        second_plan = self.destroy_plan()
        second = self.json_success(
            self.destroy_confirm(str(second_plan["confirmation_token"]))
        )

        self.assertEqual(second_plan["status"], "already_absent")
        self.assertEqual(second["status"], "already_absent")
        self.assertEqual(second["deleted_files"], 0)


if __name__ == "__main__":
    unittest.main()
