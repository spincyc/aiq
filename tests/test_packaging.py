from __future__ import annotations

from importlib import resources
from importlib.metadata import PackageNotFoundError
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch

import aiq


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def project_configuration() -> dict:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as configuration:
        return tomllib.load(configuration)


class PackagingTest(unittest.TestCase):
    def test_public_metadata_and_entry_point(self) -> None:
        configuration = project_configuration()
        project = configuration["project"]

        self.assertEqual(project["name"], "aiq-workqueue")
        self.assertEqual(project["version"], aiq._SOURCE_VERSION)
        self.assertEqual(project["requires-python"], ">=3.11,<3.15")
        self.assertEqual(project["license"], "Apache-2.0")
        self.assertEqual(project["license-files"], ["LICENSE"])
        self.assertEqual(project["dependencies"], [])
        self.assertEqual(project["scripts"], {"aiq": "aiq.cli:main"})

    def test_version_prefers_installed_distribution_metadata(self) -> None:
        with patch.object(aiq, "_distribution_version", return_value="9.8.7"):
            self.assertEqual(aiq._resolve_version(), "9.8.7")

    def test_source_version_is_the_project_version(self) -> None:
        project_version = project_configuration()["project"]["version"]

        with patch.object(
            aiq,
            "_distribution_version",
            side_effect=PackageNotFoundError,
        ):
            self.assertEqual(aiq._resolve_version(), project_version)

    def test_src_layout_and_packaged_bootstrap_are_declared(self) -> None:
        configuration = project_configuration()
        setuptools = configuration["tool"]["setuptools"]

        self.assertEqual(setuptools["package-dir"], {"": "src"})
        self.assertFalse(setuptools["include-package-data"])
        self.assertEqual(
            setuptools["packages"]["find"],
            {
                "where": ["src"],
                "include": ["aiq", "aiq.*"],
                "namespaces": False,
            },
        )
        self.assertEqual(
            setuptools["package-data"],
            {"aiq._resources": ["AGENTS.md"]},
        )

    def test_packaged_bootstrap_matches_repository_guidance(self) -> None:
        canonical = (REPOSITORY_ROOT / "AGENTS.md").read_bytes()
        packaged = (
            resources.files("aiq._resources").joinpath("AGENTS.md").read_bytes()
        )

        self.assertEqual(packaged, canonical)
        self.assertLessEqual(len(canonical.decode("utf-8").split()), 200)


if __name__ == "__main__":
    unittest.main()
