from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
import tempfile
import unittest

import support
from aiq.journal import SCHEMA_VERSION


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "schema-v1.sql"


def _codex_entry(payload):
    return next(
        entry
        for entry in payload["integrations"]
        if entry["integration"] == "codex"
    )


class ReconcileCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "home"
        self.state_home = self.root / "state"
        self.codex_home = self.root / "codex"
        self.bin_directory = self.root / "bin"
        for directory in (
            self.home,
            self.state_home,
            self.codex_home,
            self.bin_directory,
        ):
            directory.mkdir()
        self.launcher = support.write_launcher(self.bin_directory / "aiq")
        support.git_executable()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def environment(self) -> dict[str, str]:
        return support.scrubbed_environment(
            drop={"XDG_CONFIG_HOME"},
            CODEX_HOME=str(self.codex_home),
            HOME=str(self.home),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONPATH=str(support.SOURCE_ROOT),
            XDG_STATE_HOME=str(self.state_home),
        )

    def run_aiq(self, *arguments: str) -> support.CliResult:
        return support.run_cli(
            *arguments,
            in_process=False,
            cwd=self.root,
            environment=self.environment(),
        )

    def json_payload(
        self,
        result: support.CliResult,
        *,
        returncode: int = 0,
        silent: bool = True,
    ) -> dict[str, object]:
        """Assert one clean JSON response, silent on stderr by default.

        ``silent=False`` is for the single call that migrates a journal
        in place and therefore announces it; that call asserts the
        announcement itself rather than letting the exception go
        unexamined.
        """

        self.assertEqual(result.returncode, returncode, result.stderr)
        if silent:
            self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.count("\n"), 1, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["v"], 1)
        return payload

    def reconcile(
        self,
        *arguments: str,
        returncode: int = 0,
        silent: bool = True,
    ) -> dict[str, object]:
        return self.json_payload(
            self.run_aiq(
                "reconcile", "--user", "--scope", "user",
                "--cwd", str(self.root), "--json", *arguments,
            ),
            returncode=returncode,
            silent=silent,
        )

    def install_integration(self) -> None:
        self.json_payload(
            self.run_aiq(
                "integration", "install", "codex", "--user",
                "--launcher", str(self.launcher), "--json",
            )
        )

    def manifest_path(self) -> Path:
        matches = list(
            (self.state_home / "aiq" / "integrations" / "codex").glob(
                "*/manifest.json"
            )
        )
        self.assertEqual(len(matches), 1)
        return matches[0]

    def simulate_stale_python(self) -> str:
        """Rewrite the owned hook and manifest to a pre-upgrade Python path."""

        stale_python = str(self.root / "old-runtime" / "python3")
        manifest_path = self.manifest_path()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        groups = manifest["managed_group"]
        hooks_path = self.codex_home / "hooks.json"
        document = json.loads(hooks_path.read_text(encoding="utf-8"))
        for event, group in groups.items():
            command = group["hooks"][0]["command"]
            _, _, suffix = command.partition(" -I -m aiq")
            group["hooks"][0]["command"] = (
                f"{shlex.quote(stale_python)} -I -m aiq{suffix}"
            )
            document["hooks"][event] = [group]
        manifest["python_executable"] = stale_python
        manifest["managed_group_sha256"] = hashlib.sha256(
            json.dumps(groups, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        hooks_data = (
            json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        ).encode()
        hooks_path.write_bytes(hooks_data)
        manifest["config_sha256"] = hashlib.sha256(hooks_data).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        return stale_python

    def install_v1_user_journal(self) -> Path:
        """Install the frozen schema-v1 fixture at the user journal path."""

        journal_root = self.state_home / "aiq"
        journal_path = journal_root / "journal.sqlite3"
        journal_root.mkdir(mode=0o700, parents=True)
        script = FIXTURE_PATH.read_text(encoding="utf-8")
        for token, value in (
            ("__AIQ_SCOPE_KIND__", "user"),
            ("__AIQ_SCOPE_ROOT__", str(journal_root).replace("'", "''")),
            ("__AIQ_SCOPE_ID__", "user"),
        ):
            script = script.replace(token, value)
        connection = sqlite3.connect(journal_path)
        try:
            connection.executescript(script)
        finally:
            connection.close()
        journal_path.chmod(0o600)
        return journal_path

    def journal_schema_version(self, journal_path: Path) -> int:
        connection = sqlite3.connect(journal_path)
        try:
            row = connection.execute(
                "SELECT value FROM journal_metadata"
                " WHERE key = 'schema_version'"
            ).fetchone()
        finally:
            connection.close()
        return int(row[0])

    def test_default_run_is_read_only_and_apply_migrates(self) -> None:
        journal_path = self.install_v1_user_journal()

        report = self.reconcile(returncode=1)

        self.assertEqual(report["status"], "attention")
        self.assertEqual(report["problems"], 1)
        self.assertEqual(report["journal"]["status"], "drifted")
        self.assertIn("migration", report["journal"]["reason"])
        self.assertEqual(self.journal_schema_version(journal_path), 1)

        completed = self.run_aiq(
            "reconcile", "--user", "--scope", "user",
            "--cwd", str(self.root), "--json", "--apply",
        )
        applied = self.json_payload(completed, silent=False)

        self.assertEqual(applied["status"], "ok")
        self.assertEqual(applied["journal"]["status"], "ok")
        self.assertIsNone(applied["journal"]["reason"])
        self.assertEqual(
            self.journal_schema_version(journal_path),
            SCHEMA_VERSION,
        )
        # `reconcile --apply` is the documented post-upgrade migration
        # command, so this is exactly where the one-way in-place change
        # must name the file it is about to rewrite.
        announcements = completed.stderr.splitlines()
        self.assertEqual(len(announcements), 1, completed.stderr)
        self.assertIn(
            f"aiq: migrating journal schema 1 -> {SCHEMA_VERSION} in place:",
            announcements[0],
        )
        self.assertIn(str(journal_path), announcements[0])
        self.assertIn("pre-migration backup:", announcements[0])

    def test_human_output_uses_uniform_tab_rows(self) -> None:
        completed = self.run_aiq(
            "reconcile", "--user", "--scope", "user", "--cwd", str(self.root),
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        *rows, summary = completed.stdout.splitlines()
        self.assertEqual(
            [tuple(row.split("\t")[:3]) for row in rows],
            [
                ("integration", "claude", "skipped"),
                ("integration", "codex", "skipped"),
                ("journal", "-", "skipped"),
            ],
        )
        for row in rows:
            self.assertEqual(len(row.split("\t")), 4, row)
        self.assertEqual(summary, "status\tok\tproblems\t0")

    def test_absent_integration_and_journal_are_skipped(self) -> None:
        payload = self.reconcile()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["problems"], 0)
        self.assertFalse(payload["apply"])
        integration = _codex_entry(payload)
        self.assertEqual(integration["integration"], "codex")
        self.assertEqual(integration["status"], "skipped")
        self.assertEqual(integration["action"], "none")
        self.assertEqual(payload["journal"]["status"], "skipped")

    def test_uninstalled_integration_is_skipped(self) -> None:
        self.install_integration()
        self.json_payload(
            self.run_aiq("integration", "uninstall", "codex", "--user", "--json")
        )

        payload = self.reconcile()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(_codex_entry(payload)["status"], "skipped")

    def test_report_detects_stale_runtime_and_apply_repairs_it(self) -> None:
        self.json_payload(
            self.run_aiq(
                "journal", "init", "--scope", "user",
                "--cwd", str(self.root), "--json",
            )
        )
        self.install_integration()
        stale_python = self.simulate_stale_python()

        report = self.reconcile(returncode=1)
        self.assertEqual(report["status"], "attention")
        self.assertEqual(report["problems"], 1)
        integration = _codex_entry(report)
        self.assertEqual(integration["status"], "drifted")
        self.assertEqual(integration["action"], "repair")
        self.assertEqual(report["journal"]["status"], "ok")
        hooks_text = (self.codex_home / "hooks.json").read_text(
            encoding="utf-8"
        )
        self.assertIn(stale_python, hooks_text)

        applied = self.reconcile("--apply")
        self.assertEqual(applied["status"], "ok")
        self.assertEqual(applied["problems"], 0)
        repaired = _codex_entry(applied)
        self.assertEqual(repaired["status"], "repaired")
        self.assertEqual(repaired["action"], "repair")
        self.assertEqual(applied["journal"]["status"], "ok")
        hooks_text = (self.codex_home / "hooks.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(stale_python, hooks_text)
        self.assertIn(shlex.quote(sys.executable), hooks_text)
        manifest = json.loads(
            self.manifest_path().read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["python_executable"],
            os.path.abspath(sys.executable),
        )

        settled = self.reconcile()
        self.assertEqual(_codex_entry(settled)["status"], "ok")

    def test_foreign_marked_hook_is_not_repaired_even_with_apply(self) -> None:
        self.install_integration()
        hooks_path = self.codex_home / "hooks.json"
        document = json.loads(hooks_path.read_text(encoding="utf-8"))
        document["hooks"]["UserPromptSubmit"].append(
            document["hooks"]["UserPromptSubmit"][0]
        )
        hooks_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        before = hooks_path.read_text(encoding="utf-8")

        payload = self.reconcile("--apply", returncode=1)

        self.assertEqual(payload["status"], "attention")
        self.assertEqual(_codex_entry(payload)["status"], "blocked")
        self.assertEqual(hooks_path.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
