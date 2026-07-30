"""Structural checks over CHANGELOG.md and the release version couplings.

The assertions live in ``tools/release-check`` so that one implementation
serves both ``make release-check`` and the test suite; this module loads that
script and exercises it against the repository and against synthetic
changelogs.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "tools" / "release-check"


def load_release_check():
    loader = importlib.machinery.SourceFileLoader("aiq_release_check", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise AssertionError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


release_check = load_release_check()


def structure(text: str, project_version: str = "1.0.0") -> list[str]:
    sections, links, defects = release_check.parse_changelog(text)
    return sorted(
        set(defects + release_check.changelog_violations(sections, links, project_version))
    )


VALID = """# Changelog

## [Unreleased]

### Added

- Something.

## [1.0.0] - 2026-01-01

### Added

- A first release.

### Fixed

- A defect.

[Unreleased]: https://example.invalid/compare/v1.0.0...HEAD
[1.0.0]: https://example.invalid/releases/tag/v1.0.0
"""


class ChangelogStructureTests(unittest.TestCase):
    def test_a_well_formed_changelog_has_no_violations(self) -> None:
        self.assertEqual(structure(VALID), [])

    def test_repeated_version_section_is_reported(self) -> None:
        text = VALID.replace(
            "## [1.0.0] - 2026-01-01\n\n### Added\n\n- A first release.\n\n",
            "## [1.0.0] - 2026-01-01\n\n### Added\n\n- A first release.\n\n"
            "## [1.0.0] - 2026-01-02\n\n### Changed\n\n- A second copy.\n\n",
        )
        self.assertEqual(structure(text), ["[1.0.0]: duplicate version section"])

    def test_repeated_heading_within_a_section_is_reported(self) -> None:
        text = VALID.replace(
            "### Fixed\n\n- A defect.",
            "### Fixed\n\n- A defect.\n\n### Fixed\n\n- Another defect.",
        )
        self.assertEqual(structure(text), ["[1.0.0]: duplicate heading: ### Fixed"])

    def test_the_same_heading_in_two_sections_is_allowed(self) -> None:
        text = VALID.replace("### Added\n\n- Something.", "### Fixed\n\n- Something.")

        self.assertEqual(structure(text), [])

    def test_missing_link_reference_is_reported(self) -> None:
        text = VALID.replace(
            "[1.0.0]: https://example.invalid/releases/tag/v1.0.0\n", ""
        )
        self.assertEqual(structure(text), ["[1.0.0]: version has no link reference"])

    def test_orphan_link_reference_is_reported(self) -> None:
        text = VALID + "[0.9.0]: https://example.invalid/releases/tag/v0.9.0\n"
        self.assertEqual(
            structure(text), ["[0.9.0]: link reference has no version section"]
        )

    def test_newest_section_must_match_the_project_version(self) -> None:
        text = VALID.replace("## [Unreleased]\n\n### Added\n\n- Something.\n\n", "")
        text = text.replace("[Unreleased]: https://example.invalid/compare/v1.0.0...HEAD\n", "")

        self.assertEqual(structure(text, project_version="1.0.0"), [])
        self.assertEqual(
            structure(text, project_version="2.0.0"),
            [
                "top section [1.0.0] is neither [Unreleased] nor the "
                "pyproject.toml version [2.0.0]"
            ],
        )

    def test_unreleased_must_lead_the_file(self) -> None:
        text = """# Changelog

## [1.0.0] - 2026-01-01

### Added

- A first release.

## [Unreleased]

### Added

- Something.

[Unreleased]: https://example.invalid/compare/v1.0.0...HEAD
[1.0.0]: https://example.invalid/releases/tag/v1.0.0
"""
        self.assertIn(
            "[Unreleased] must be the first section, not [1.0.0]", structure(text)
        )

    def test_headings_inside_fenced_blocks_are_ignored(self) -> None:
        text = VALID.replace(
            "- A defect.",
            "- A defect:\n\n```\n### Fixed\n## [9.9.9]\n```",
        )
        self.assertEqual(structure(text), [])

    def test_malformed_version_heading_is_reported(self) -> None:
        text = VALID.replace("## [1.0.0] - 2026-01-01", "## 1.0.0 - 2026-01-01")
        self.assertIn(
            "line 9: malformed version heading: ## 1.0.0 - 2026-01-01",
            structure(text),
        )


class RepositoryChangelogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_version = release_check.read_project_version()
        text = (REPOSITORY_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.sections, links, defects = release_check.parse_changelog(text)
        self.violations = set(defects) | set(
            release_check.changelog_violations(
                self.sections, links, self.project_version
            )
        )

    def test_only_known_structure_defects_remain(self) -> None:
        unexpected = sorted(
            self.violations - release_check.KNOWN_CHANGELOG_ISSUES
        )

        self.assertEqual(unexpected, [])

    def test_every_known_defect_still_reproduces(self) -> None:
        stale = sorted(release_check.KNOWN_CHANGELOG_ISSUES - self.violations)

        self.assertEqual(
            stale,
            [],
            "a fixed defect is still listed in KNOWN_CHANGELOG_ISSUES in "
            "tools/release-check; delete the entry",
        )

    def test_the_release_versions_agree(self) -> None:
        failures = release_check.coupling_failures(
            self.project_version,
            release_check.read_source_version(),
            self.sections,
            None,
        )

        self.assertEqual(failures, [])


class TagCouplingTests(unittest.TestCase):
    SECTIONS = [("1.0.0", ["### Added"])]

    def test_a_matching_tag_passes(self) -> None:
        self.assertEqual(
            release_check.coupling_failures("1.0.0", "1.0.0", self.SECTIONS, "v1.0.0"),
            [],
        )

    def test_a_mismatched_tag_names_both_sides(self) -> None:
        failures = release_check.coupling_failures(
            "1.0.0", "1.0.0", self.SECTIONS, "v2.0.0"
        )

        self.assertEqual(
            failures,
            [
                "tag v2.0.0 and pyproject.toml version 1.0.0 disagree; "
                "expected tag v1.0.0"
            ],
        )

    def test_an_unprefixed_tag_is_rejected(self) -> None:
        failures = release_check.coupling_failures(
            "1.0.0", "1.0.0", self.SECTIONS, "1.0.0"
        )

        self.assertEqual(
            failures,
            [
                "tag 1.0.0 is not v-prefixed; the release tag for "
                "pyproject.toml version 1.0.0 is v1.0.0"
            ],
        )

    def test_a_source_version_mismatch_names_both_files(self) -> None:
        failures = release_check.coupling_failures(
            "1.0.0", "0.9.0", self.SECTIONS, None
        )

        self.assertEqual(
            failures,
            [
                "pyproject.toml version 1.0.0 and src/aiq/__init__.py "
                "_SOURCE_VERSION 0.9.0 disagree"
            ],
        )

    def test_a_changelog_mismatch_names_the_newest_section(self) -> None:
        failures = release_check.coupling_failures(
            "1.0.0", "1.0.0", [("Unreleased", []), ("0.9.0", [])], None
        )

        self.assertEqual(
            failures,
            [
                "pyproject.toml version 1.0.0 and the newest CHANGELOG.md "
                "version section [0.9.0] disagree"
            ],
        )

    def test_a_tag_cannot_be_cut_while_unreleased_leads(self) -> None:
        failures = release_check.coupling_failures(
            "1.0.0", "1.0.0", [("Unreleased", []), ("1.0.0", [])], "v1.0.0"
        )

        self.assertEqual(
            failures,
            [
                "tag v1.0.0 and CHANGELOG.md disagree: the file still opens "
                "with [Unreleased]; rename that section to [1.0.0] before "
                "cutting the tag"
            ],
        )


if __name__ == "__main__":
    unittest.main()
