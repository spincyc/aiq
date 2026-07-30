from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import support


class ConfigCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "home"
        self.config_home = self.root / "config"
        self.state_home = self.root / "state"
        self.home.mkdir()
        self.config_home.mkdir()
        self.state_home.mkdir()
        self.repository = support.init_repository(self.root / "repository")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def environment(self, **updates: str) -> dict[str, str]:
        return support.scrubbed_environment(
            HOME=str(self.home),
            PYTHONPATH=str(support.SOURCE_ROOT),
            XDG_CONFIG_HOME=str(self.config_home),
            XDG_STATE_HOME=str(self.state_home),
            **updates,
        )

    def run_aiq(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> support.CliResult:
        return support.run_cli(
            *arguments,
            in_process=False,
            cwd=self.repository,
            environment=environment or self.environment(),
        )

    def write_user_config(self, content: str) -> Path:
        path = self.config_home / "aiq" / "config.toml"
        path.parent.mkdir()
        path.write_text(content, encoding="utf-8")
        return path

    def write_repo_config(self, content: str) -> Path:
        path = self.repository / ".aiq.toml"
        path.write_text(content, encoding="utf-8")
        return path

    def assert_json_error(
        self,
        result: support.CliResult,
        *,
        code: str,
    ) -> dict[str, object]:
        self.assertEqual(result.returncode, 2, result)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        payload = json.loads(result.stderr)
        self.assertEqual(payload["v"], 1)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], code)
        self.assertIsInstance(payload["error"], str)
        self.assertNotIn("\n", payload["error"])
        return payload

    def test_show_resolves_precedence_and_reports_winning_sources(self) -> None:
        self.write_user_config(
            "\n".join(
                (
                    "version = 1",
                    'scope = "auto"',
                    'owner = "user-owner"',
                    "lease_seconds = 10",
                    "snapshot_keep = 11",
                    'output = "human"',
                    "",
                )
            )
        )
        self.write_repo_config(
            "\n".join(
                (
                    "version = 1",
                    "lease_seconds = 20",
                    "",
                )
            )
        )
        result = self.run_aiq(
            "config",
            "show",
            "--cwd",
            str(self.repository),
            "--scope",
            "user",
            "--lease-seconds",
            "40",
            "--json",
            "--sources",
            environment=self.environment(
                AIQ_OWNER="environment-owner",
                AIQ_LEASE_SECONDS="30",
                AIQ_OUTPUT="json",
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["v"], 1)
        self.assertEqual(payload["scope"], "user")
        self.assertEqual(payload["owner"], "environment-owner")
        self.assertEqual(payload["lease_seconds"], 40)
        self.assertEqual(payload["snapshot_keep"], 11)
        self.assertEqual(payload["output"], "json")
        self.assertEqual(payload["sources"]["scope"], "cli")
        self.assertEqual(payload["sources"]["owner"], "env:AIQ_OWNER")
        self.assertEqual(payload["sources"]["lease_seconds"], "cli")
        self.assertEqual(
            payload["sources"]["snapshot_keep"],
            f"user:{self.config_home / 'aiq' / 'config.toml'}",
        )
        self.assertEqual(payload["sources"]["output"], "env:AIQ_OUTPUT")
    def test_no_repo_config_skips_even_an_invalid_repository_file(self) -> None:
        user_path = self.write_user_config(
            'version = 1\nscope = "user"\nlease_seconds = 17\n'
        )
        self.write_repo_config("version = 1\nunknown = true\n")

        result = self.run_aiq(
            "config",
            "show",
            "--cwd",
            str(self.repository),
            "--no-repo-config",
            "--json",
            "--sources",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["v"], 1)
        self.assertEqual(payload["scope"], "user")
        self.assertEqual(payload["lease_seconds"], 17)
        self.assertEqual(payload["sources"]["scope"], f"user:{user_path}")
        self.assertEqual(
            payload["sources"]["lease_seconds"],
            f"user:{user_path}",
        )
        self.assertNotIn("repo:", "\n".join(payload["sources"].values()))

    def test_check_accepts_valid_configuration_without_creating_state(self) -> None:
        self.write_user_config('version = 1\nscope = "user"\n')

        result = self.run_aiq(
            "config",
            "check",
            "--cwd",
            str(self.repository),
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["v"], 1)
        self.assertEqual(payload["status"], "ok")
        self.assertFalse((self.state_home / "aiq").exists())

    def test_unknown_user_key_has_stable_invalid_config_error(self) -> None:
        self.write_user_config("version = 1\nmystery = 1\n")

        result = self.run_aiq(
            "config",
            "check",
            "--cwd",
            str(self.repository),
            "--json",
        )

        payload = self.assert_json_error(result, code="invalid_config")
        self.assertIn("mystery", payload["error"])

    def test_forbidden_repository_key_has_stable_invalid_config_error(self) -> None:
        self.write_repo_config('version = 1\nowner = "not-allowed"\n')

        result = self.run_aiq(
            "config",
            "show",
            "--cwd",
            str(self.repository),
            "--json",
        )

        payload = self.assert_json_error(result, code="invalid_config")
        self.assertIn("owner", payload["error"])

    def test_invalid_environment_value_has_stable_invalid_config_error(self) -> None:
        result = self.run_aiq(
            "config",
            "check",
            "--cwd",
            str(self.repository),
            "--json",
            environment=self.environment(AIQ_LEASE_SECONDS="-1"),
        )

        payload = self.assert_json_error(result, code="invalid_config")
        self.assertIn("AIQ_LEASE_SECONDS", payload["error"])


if __name__ == "__main__":
    unittest.main()
