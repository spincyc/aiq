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
from typing import NamedTuple


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


class Installation(NamedTuple):
    name: str
    root: Path
    python: Path
    console: Path


class InstalledAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary_directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary_directory.name)
        cls.run_directory = cls.root / "run"
        cls.run_directory.mkdir()

        wheel_directory = cls.root / "wheel"
        wheel_project = cls.root / "wheel-project"
        cls._copy_project(wheel_project)
        wheel_directory.mkdir()
        cls._run_setup(
            [
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--wheel",
                "--outdir",
                str(wheel_directory),
                str(wheel_project),
            ]
        )
        wheel = cls._only_distribution(wheel_directory, "*.whl")

        sdist_directory = cls.root / "sdist"
        sdist_project = cls.root / "sdist-project"
        cls._copy_project(sdist_project)
        sdist_directory.mkdir()
        cls._run_setup(
            [
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--sdist",
                "--outdir",
                str(sdist_directory),
                str(sdist_project),
            ]
        )
        sdist = cls._only_distribution(sdist_directory, "*.tar.gz")

        extracted_directory = cls.root / "sdist-extracted"
        extracted_directory.mkdir()
        shutil.unpack_archive(sdist, extracted_directory)
        extracted_projects = [
            path for path in extracted_directory.iterdir() if path.is_dir()
        ]
        if len(extracted_projects) != 1:
            raise AssertionError(
                f"expected one extracted sdist project, found {extracted_projects!r}"
            )

        sdist_wheel_directory = cls.root / "sdist-wheel"
        sdist_wheel_directory.mkdir()
        cls._run_setup(
            [
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--wheel",
                "--outdir",
                str(sdist_wheel_directory),
                str(extracted_projects[0]),
            ]
        )
        sdist_wheel = cls._only_distribution(
            sdist_wheel_directory,
            "*.whl",
        )

        cls.installations = [
            cls._install_wheel("direct-wheel", wheel),
            cls._install_wheel("sdist-derived-wheel", sdist_wheel),
        ]

    @classmethod
    def _copy_project(cls, destination: Path) -> None:
        destination.mkdir()
        for filename in (
            "LICENSE",
            "MANIFEST.in",
            "README.md",
            "pyproject.toml",
        ):
            shutil.copy2(REPOSITORY_ROOT / filename, destination / filename)
        shutil.copytree(
            REPOSITORY_ROOT / "src",
            destination / "src",
            ignore=shutil.ignore_patterns(
                "__pycache__",
                "*.egg-info",
                "*.pyc",
            ),
        )

    @classmethod
    def _only_distribution(cls, directory: Path, pattern: str) -> Path:
        distributions = list(directory.glob(pattern))
        if len(distributions) != 1:
            raise AssertionError(
                f"expected one {pattern} distribution, found {distributions!r}"
            )
        return distributions[0]

    @classmethod
    def _install_wheel(cls, name: str, wheel: Path) -> Installation:
        venv = cls.root / f"venv-{name}"
        cls._run_setup(
            [
                sys.executable,
                "-m",
                "venv",
                str(venv),
            ]
        )
        python = venv / "bin" / "python"
        console = venv / "bin" / "aiq"
        cls._run_setup(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--no-deps",
                str(wheel),
            ]
        )
        return Installation(name, venv, python, console)

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
        installation: Installation,
        *arguments: str,
        module: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = (
            [str(installation.python), "-m", "aiq", *arguments]
            if module
            else [str(installation.console), *arguments]
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

        for installation in self.installations:
            with self.subTest(artifact=installation.name):
                module_result = self.run_installed(
                    installation,
                    "--version",
                    module=True,
                )
                console_result = self.run_installed(
                    installation,
                    "--version",
                )

                for result in (module_result, console_result):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, expected)
                    self.assertEqual(result.stderr, "")

    def test_module_and_console_entry_points_have_identical_behavior(self) -> None:
        for installation in self.installations:
            with self.subTest(artifact=installation.name):
                module_result = self.run_installed(
                    installation,
                    "capability",
                    "list",
                    "--json",
                    module=True,
                )
                console_result = self.run_installed(
                    installation,
                    "capability",
                    "list",
                    "--json",
                )

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
        for installation in self.installations:
            with self.subTest(artifact=installation.name):
                result = subprocess.run(
                    [str(installation.python), "-c", script],
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
                Path(payload["module"]).relative_to(installation.root.resolve())
                self.assertFalse(
                    Path(payload["module"]).is_relative_to(
                        REPOSITORY_ROOT.resolve()
                    )
                )


if __name__ == "__main__":
    unittest.main()
