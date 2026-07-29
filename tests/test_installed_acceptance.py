from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


class InstalledAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary_directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary_directory.name)
        cls.project = cls.root / "project"
        cls.run_directory = cls.root / "run"
        cls.run_directory.mkdir()
        cls.project.mkdir()

        for filename in (
            "LICENSE",
            "MANIFEST.in",
            "README.md",
            "pyproject.toml",
        ):
            shutil.copy2(REPOSITORY_ROOT / filename, cls.project / filename)
        shutil.copytree(REPOSITORY_ROOT / "src", cls.project / "src")

        cls.venv = cls.root / "venv"
        cls._run_setup(
            [
                sys.executable,
                "-m",
                "venv",
                "--system-site-packages",
                str(cls.venv),
            ]
        )
        cls.python = cls.venv / "bin" / "python"
        cls.console = cls.venv / "bin" / "aiq"
        cls._run_setup(
            [
                str(cls.python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-build-isolation",
                "--no-cache-dir",
                "--no-deps",
                str(cls.project),
            ]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary_directory.cleanup()

    @classmethod
    def _environment(cls) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["PYTHONNOUSERSITE"] = "1"
        environment["XDG_CONFIG_HOME"] = str(cls.root / "config")
        environment["XDG_STATE_HOME"] = str(cls.root / "state")
        return environment

    @classmethod
    def _run_setup(cls, command: list[str]) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command,
            cwd=cls.root,
            env=cls._environment(),
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"command failed ({result.returncode}): {command!r}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        return result

    def run_installed(
        self,
        *arguments: str,
        module: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = (
            [str(self.python), "-m", "aiq", *arguments]
            if module
            else [str(self.console), *arguments]
        )
        return subprocess.run(
            command,
            cwd=self.run_directory,
            env=self._environment(),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def test_module_and_console_entry_points_report_installed_version(self) -> None:
        with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as configuration:
            expected = tomllib.load(configuration)["project"]["version"] + "\n"

        module_result = self.run_installed("--version", module=True)
        console_result = self.run_installed("--version")

        for result in (module_result, console_result):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, expected)
            self.assertEqual(result.stderr, "")

    def test_module_and_console_entry_points_have_identical_behavior(self) -> None:
        module_result = self.run_installed(
            "capability",
            "list",
            "--json",
            module=True,
        )
        console_result = self.run_installed("capability", "list", "--json")

        self.assertEqual(module_result.returncode, 0, module_result.stderr)
        self.assertEqual(console_result.returncode, 0, console_result.stderr)
        self.assertEqual(module_result.stderr, "")
        self.assertEqual(console_result.stderr, "")
        self.assertEqual(console_result.stdout, module_result.stdout)
        self.assertTrue(json.loads(console_result.stdout)["capabilities"])

    def test_installed_resource_matches_canonical_guidance_bytes(self) -> None:
        script = """
from importlib import resources
import json
from pathlib import Path
import aiq

print(json.dumps({
    "guidance": resources.files("aiq._resources").joinpath("AGENTS.md").read_bytes().hex(),
    "module": str(Path(aiq.__file__).resolve()),
}, sort_keys=True))
"""
        result = subprocess.run(
            [str(self.python), "-c", script],
            cwd=self.run_directory,
            env=self._environment(),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(
            bytes.fromhex(payload["guidance"]),
            (REPOSITORY_ROOT / "AGENTS.md").read_bytes(),
        )
        Path(payload["module"]).relative_to(self.venv.resolve())
        self.assertFalse(Path(payload["module"]).is_relative_to(REPOSITORY_ROOT))


if __name__ == "__main__":
    unittest.main()
