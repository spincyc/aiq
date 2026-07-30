from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import support

EXPECTED_CHECKS = (
    "python",
    "sqlite",
    "config",
    "git",
    "scope",
    "journal",
    "capture",
    "journal.deep",
    "integration.claude",
    "integration.codex",
    "report",
)


class DoctorCapabilityContractTests(unittest.TestCase):
    def test_capability_descriptor_checks_match_doctor_order(self) -> None:
        # The bootstrap names capability discovery as the authoritative
        # surface, so the descriptor's checks list must track run_doctor.
        from aiq.capabilities import CAPABILITIES

        self.assertEqual(
            tuple(CAPABILITIES["doctor"]["contract"]["checks"]),
            EXPECTED_CHECKS,
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
        ):
            path.mkdir()
        support.init_repository(self.repository)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def environment(self) -> dict[str, str]:
        return support.scrubbed_environment(
            drop={"CODEX_HOME"},
            HOME=str(self.home),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONPATH=str(support.SOURCE_ROOT),
            XDG_CONFIG_HOME=str(self.config_home),
            XDG_STATE_HOME=str(self.state_home),
        )

    def run_aiq(
        self,
        *arguments: str,
        extra_environment: dict[str, str] | None = None,
    ) -> support.CliResult:
        environment = self.environment()
        if extra_environment:
            environment.update(extra_environment)
        return support.run_cli(
            *arguments,
            in_process=False,
            cwd=self.repository,
            environment=environment,
        )

    def doctor_payload(
        self,
        completed: support.CliResult,
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
        for name in (
            "python",
            "sqlite",
            "config",
            "git",
            "scope",
            "journal",
            "capture",
        ):
            self.assertEqual(checks[name]["status"], "ok", checks[name])
        self.assertEqual(checks["journal.deep"]["status"], "skipped")
        self.assertIn("journal check", checks["journal.deep"]["detail"])
        for integration_id in ("claude", "codex"):
            check = checks[f"integration.{integration_id}"]
            self.assertEqual(check["status"], "skipped")
            self.assertEqual(
                check["detail"],
                "not installed: run aiq integration install "
                f"{integration_id} --user",
            )
        self.assertEqual(checks["report"]["status"], "skipped")
        self.assertEqual(
            checks["report"]["detail"],
            "dev_report_repo not configured",
        )

    def test_doctor_report_check_warns_on_unusable_target(self) -> None:
        missing = self.root / "absent"
        completed = self.run_aiq(
            "doctor", "--scope", "repo", "--json",
            extra_environment={"AIQ_DEV_REPORT_REPO": str(missing)},
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        payload, checks = self.doctor_payload(completed)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(checks["report"]["status"], "warn")
        self.assertIn("does not exist", checks["report"]["detail"])
        self.assertIn(str(missing), checks["report"]["detail"])

        uninitialized = support.init_repository(self.root / "uninitialized")
        completed = self.run_aiq(
            "doctor", "--scope", "repo", "--json",
            extra_environment={"AIQ_DEV_REPORT_REPO": str(uninitialized)},
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        payload, checks = self.doctor_payload(completed)
        self.assertEqual(checks["report"]["status"], "warn")
        self.assertEqual(
            checks["report"]["detail"],
            "target journal not initialized: "
            f"run aiq journal init in {uninitialized}",
        )
        self.assertFalse(
            (uninitialized / ".git" / "aiq" / "journal.sqlite3").exists()
        )

    def test_doctor_report_check_ok_for_initialized_target(self) -> None:
        initialized = self.run_aiq(
            "journal", "init", "--scope", "repo", "--json"
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)

        completed = self.run_aiq(
            "doctor", "--scope", "repo", "--json",
            extra_environment={"AIQ_DEV_REPORT_REPO": str(self.repository)},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload, checks = self.doctor_payload(completed)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(checks["report"]["status"], "ok")
        self.assertIn(str(self.repository), checks["report"]["detail"])

    def test_doctor_skips_missing_journal_without_creating_it(self) -> None:
        journal_path = self.state_home / "aiq" / "journal.sqlite3"

        completed = self.run_aiq("doctor", "--scope", "user", "--json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload, checks = self.doctor_payload(completed)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(checks["journal"]["status"], "skipped")
        self.assertIn("not initialized", checks["journal"]["detail"])
        self.assertFalse(journal_path.exists())

    def test_doctor_warns_when_repo_capture_is_inactive(self) -> None:
        completed = self.run_aiq("doctor", "--scope", "repo", "--json")

        self.assertEqual(completed.returncode, 0, completed.stdout)
        payload, checks = self.doctor_payload(completed)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(checks["capture"]["status"], "warn")
        self.assertIn(
            "prompt capture is inactive",
            checks["capture"]["detail"],
        )
        self.assertIn(
            "aiq journal init --scope repo",
            checks["capture"]["detail"],
        )
        self.assertFalse(
            (self.repository / ".git" / "aiq" / "journal.sqlite3").exists()
        )

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

    def test_doctor_warns_on_journal_permissions_and_exits_zero(self) -> None:
        initialized = self.run_aiq(
            "journal", "init", "--scope", "user", "--json"
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        journal_path = self.state_home / "aiq" / "journal.sqlite3"
        journal_path.chmod(0o644)

        completed = self.run_aiq("doctor", "--scope", "user", "--json")

        self.assertEqual(completed.returncode, 0, completed.stdout)
        payload, checks = self.doctor_payload(completed)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(checks["journal"]["status"], "warn")
        self.assertIn("0644", checks["journal"]["detail"])
        self.assertIn("0600", checks["journal"]["detail"])

    def test_doctor_reports_scope_resolution_failure(self) -> None:
        completed = self.run_aiq(
            "doctor", "--scope", "repo", "--cwd", str(self.home), "--json"
        )

        self.assertEqual(completed.returncode, 1, completed.stdout)
        payload, checks = self.doctor_payload(completed)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(checks["scope"]["status"], "fail")
        self.assertEqual(checks["journal"]["status"], "skipped")
        self.assertEqual(
            checks["journal"]["detail"],
            "scope resolution failed",
        )

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
