from __future__ import annotations

import unittest

from aiq.capabilities import (
    CAPABILITIES,
    CAPABILITY_CATALOG_VERSION,
    capability_catalog,
    list_capabilities,
    show_capability,
)
from aiq.cli import build_parser
from aiq.journal import JournalError


EXPECTED_COMMAND_PATHS = {
    "capability.list": ("capability", "list"),
    "capability.show": ("capability", "show"),
    "claim.release": ("claim", "release"),
    "config.check": ("config", "check"),
    "config.show": ("config", "show"),
    "doctor": ("doctor",),
    "inbox.apply": ("inbox", "apply"),
    "inbox.claim": ("inbox", "claim"),
    "inbox.fail": ("inbox", "fail"),
    "inbox.list": ("inbox", "list"),
    "inbox.needs-input": ("inbox", "needs-input"),
    "integration.check": ("integration", "check"),
    "integration.install": ("integration", "install"),
    "integration.list": ("integration", "list"),
    "integration.plan": ("integration", "plan"),
    "integration.print": ("integration", "print"),
    "integration.uninstall": ("integration", "uninstall"),
    "journal.check": ("journal", "check"),
    "journal.destroy": ("journal", "destroy"),
    "journal.export": ("journal", "export"),
    "journal.init": ("journal", "init"),
    "journal.path": ("journal", "path"),
    "journal.snapshot": ("journal", "snapshot"),
    "message.ingest": ("ingest",),
    "queue.next": ("queue", "next"),
    "queue.peek": ("queue", "peek"),
    "task.list": ("task", "list"),
    "task.show": ("task", "show"),
}

INTERNAL_COMMAND_PATHS = {
    ("integration", "receive"),
}

GIT_EXECUTABLE_CAPABILITIES = {
    "integration.check",
    "integration.install",
    "integration.plan",
    "integration.print",
}


def parser_command_paths() -> set[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()

    def visit(parser, prefix: tuple[str, ...] = ()) -> None:
        subparsers = [
            action
            for action in parser._actions
            if action.__class__.__name__ == "_SubParsersAction"
        ]
        if not subparsers:
            paths.add(prefix)
            return
        for name, child in subparsers[0].choices.items():
            visit(child, (*prefix, name))

    visit(build_parser())
    return paths


class CapabilityContractTests(unittest.TestCase):
    def test_catalog_is_versioned_compact_and_sorted(self) -> None:
        catalog = capability_catalog()

        self.assertEqual(catalog["v"], CAPABILITY_CATALOG_VERSION)
        self.assertEqual(catalog["capabilities"], list_capabilities())
        identifiers = [item["id"] for item in catalog["capabilities"]]
        self.assertEqual(identifiers, sorted(identifiers))
        self.assertEqual(
            set(catalog["capabilities"][0]),
            {"available", "id", "purpose", "version"},
        )

    def test_registry_matches_implemented_cli_commands(self) -> None:
        self.assertEqual(set(CAPABILITIES), set(EXPECTED_COMMAND_PATHS))
        parser_paths = parser_command_paths()
        self.assertLessEqual(INTERNAL_COMMAND_PATHS, parser_paths)
        self.assertEqual(
            set(EXPECTED_COMMAND_PATHS.values()),
            parser_paths - INTERNAL_COMMAND_PATHS,
        )

        for capability_id, path in EXPECTED_COMMAND_PATHS.items():
            descriptor = show_capability(capability_id)
            self.assertTrue(descriptor["available"])
            self.assertGreaterEqual(descriptor["version"], 1)
            self.assertTrue(descriptor["command"].startswith("aiq " + " ".join(path)))
            self.assertIsInstance(descriptor["mutates"], bool)
            self.assertTrue(descriptor["idempotency"])

            if capability_id in GIT_EXECUTABLE_CAPABILITIES:
                self.assertEqual(descriptor["version"], 2)
                self.assertIn("[--git-executable PATH]", descriptor["command"])

    def test_show_returns_an_isolated_descriptor(self) -> None:
        shown = show_capability("inbox.apply")
        shown["contract"]["rules"].append("mutated")

        self.assertNotIn(
            "mutated",
            show_capability("inbox.apply")["contract"]["rules"],
        )

    def test_unknown_capability_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            JournalError,
            "capability not found: journal.restore",
        ):
            show_capability("journal.restore")


if __name__ == "__main__":
    unittest.main()
