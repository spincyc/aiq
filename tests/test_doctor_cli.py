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

EXPECTED_CHECKS = (
    "python",
    "sqlite",
    "config",
    "git",
    "scope",
    "journal",
    "journal.deep",
    "integration.codex",
)


class DoctorCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "home"
        self.config_home = self.root / "config"
        self.state_home = self.root / "state"
        self.repository = self.root / "repository"
        for path in (
            self.home,
            self.config_home,
            self.state_home,
            self.repository,
        ):
            path.mkdir()
        subprocess.run(
            ["git", "init", "--quiet", str(self.repository)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def environment(self) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("AIQ_", "GIT_"))
            and key
            not in {
                "CODEX_HOME",
                "PYTHONPATH",
                "XDG_CONFIG_HOME",
                "XDG_STATE_HOME",
            }
        }
        environment.update(
            {
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
            cwd=self.repository,
            env=self.environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def doctor_payload(
        self,
        completed: subprocess.CompletedProcess[str],
    ) -> tuple[dict[str, object], dict[str, dict[str, str]]]:
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout.count("\n"), 1, completed.stdout)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["v"], 1)
        checks = payload["checks"]
        self.assertEqual(
            tuple(check["check"] for check in checks),
            EXPECTED_CHECKS,
        )
        for check in checks:
            self.assertEqual(set(check), {"check", "detail", "status"})
            self.assertIn(check["status"], {"ok", "warn", "fail", "skipped"})
        return payload, {check["check"]: check for check in checks}

    def test_doctor_reports_ok_for_initialized_repo_journal(self) -> None:
        initialized = self.run_aiq(
            "journal", "init", "--scope", "repo", "--json"
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)

        completed = self.run_aiq("doctor", "--scope", "repo", "--json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload, checks = self.doctor_payload(completed)
        self.assertEqual(payload["status"], "ok")
        for name in ("python", "sqlite", "config", "git", "scope", "journal"):
            self.assertEqual(checks[name]["status"], "ok", checks[name])
        self.assertEqual(checks["journal.deep"]["status"], "skipped")
        self.assertIn("journal check", checks["journal.deep"]["detail"])
        self.assertEqual(checks["integration.codex"]["status"], "skipped")

    def test_doctor_skips_missing_journal_without_creating_it(self) -> None:
        journal_path = self.state_home / "aiq" / "journal.sqlite3"

        completed = self.run_aiq("doctor", "--scope", "user", "--json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload, checks = self.doctor_payload(completed)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(checks["journal"]["status"], "skipped")
        self.assertIn("not initialized", checks["journal"]["detail"])
        self.assertFalse(journal_path.exists())

    def test_doctor_fails_on_broken_configuration(self) -> None:
        config_path = self.config_home / "aiq" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("version = 1\nscope = [broken", encoding="utf-8")

        completed = self.run_aiq("doctor", "--json")
        self.assertEqual(completed.returncode, 1, completed.stdout)
        payload, checks = self.doctor_payload(completed)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(checks["config"]["status"], "fail")
        self.assertEqual(checks["scope"]["status"], "ok")

    def test_doctor_fails_on_unreadable_journal(self) -> None:
        initialized = self.run_aiq(
            "journal", "init", "--scope", "user", "--json"
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        journal_path = self.state_home / "aiq" / "journal.sqlite3"
        journal_path.write_bytes(b"this is not a SQLite database")
        journal_path.chmod(0o600)

        completed = self.run_aiq("doctor", "--scope", "user", "--json")
        self.assertEqual(completed.returncode, 1, completed.stdout)
        payload, checks = self.doctor_payload(completed)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(checks["journal"]["status"], "fail")

    def test_doctor_human_output_is_aligned_and_terse(self) -> None:
        completed = self.run_aiq("doctor", "--scope", "user")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        lines = completed.stdout.splitlines()
        self.assertEqual(len(lines), len(EXPECTED_CHECKS))
        for line, name in zip(lines, EXPECTED_CHECKS):
            self.assertEqual(line.split()[0], name)
            self.assertIn(line.split()[1], {"ok", "warn", "fail", "skipped"})


if __name__ == "__main__":
    unittest.main()
