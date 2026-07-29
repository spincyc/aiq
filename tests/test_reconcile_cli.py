from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


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
        self.launcher = self.bin_directory / "aiq"
        self.launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.launcher.chmod(0o755)
        if shutil.which("git") is None:
            self.fail("test requires Git")

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
                "CODEX_HOME": str(self.codex_home),
                "HOME": str(self.home),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(SOURCE_ROOT),
                "XDG_STATE_HOME": str(self.state_home),
            }
        )
        return environment

    def run_aiq(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "aiq", *arguments],
            cwd=self.root,
            env=self.environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def json_payload(
        self,
        result: subprocess.CompletedProcess[str],
        *,
        returncode: int = 0,
    ) -> dict[str, object]:
        self.assertEqual(result.returncode, returncode, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.count("\n"), 1, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["v"], 1)
        return payload

    def reconcile(
        self,
        *arguments: str,
        returncode: int = 0,
    ) -> dict[str, object]:
        return self.json_payload(
            self.run_aiq(
                "reconcile", "--user", "--scope", "user",
                "--cwd", str(self.root), "--json", *arguments,
            ),
            returncode=returncode,
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
        group = manifest["managed_group"]
        command = group["hooks"][0]["command"]
        _, _, suffix = command.partition(" -I -m aiq")
        group["hooks"][0]["command"] = (
            f"{shlex.quote(stale_python)} -I -m aiq{suffix}"
        )
        manifest["python_executable"] = stale_python
        manifest["managed_group_sha256"] = hashlib.sha256(
            json.dumps(group, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        hooks_path = self.codex_home / "hooks.json"
        document = json.loads(hooks_path.read_text(encoding="utf-8"))
        document["hooks"]["UserPromptSubmit"] = [group]
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

    def test_absent_integration_and_journal_are_skipped(self) -> None:
        payload = self.reconcile()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["problems"], 0)
        self.assertFalse(payload["apply"])
        integration = payload["integrations"][0]
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
        self.assertEqual(payload["integrations"][0]["status"], "skipped")

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
        integration = report["integrations"][0]
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
        repaired = applied["integrations"][0]
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
        self.assertEqual(settled["integrations"][0]["status"], "ok")

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
        self.assertEqual(payload["integrations"][0]["status"], "blocked")
        self.assertEqual(hooks_path.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
