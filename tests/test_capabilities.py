from __future__ import annotations

import json
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
    "claim.list": ("claim", "list"),
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
    "queue.dequeue": ("dequeue",),
    "queue.next": ("queue", "next"),
    "reader.acquire": ("reader", "acquire"),
    "reader.release": ("reader", "release"),
    "reader.status": ("reader", "status"),
    "reconcile.run": ("reconcile",),
    "report.send": ("report",),
    "queue.peek": ("queue", "peek"),
    "status.show": ("status",),
    "task.done": ("task", "done"),
    "task.enqueue": ("enqueue",),
    "task.explain": ("task", "explain"),
    "task.history": ("task", "history"),
    "task.list": ("task", "list"),
    "task.overview": ("list",),
    "task.show": ("task", "show"),
}

INTERNAL_COMMAND_PATHS = {
    ("integration", "receive"),
}

INTERNAL_SCOPE_HOOKS = ("agent-root", "--agent-root")

CONTRACT_SCOPE_CHOICES = "[--scope auto|repo|user]"

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

    def test_no_descriptor_advertises_the_internal_scope_hooks(self) -> None:
        """Descriptors must not teach agents to use what cli-v1 disowns.

        ``docs/contracts/cli-v1.md`` documents the ``agent-root`` scope
        choice and the ``--agent-root PATH`` option as internal, unstable
        hooks outside the contract. Descriptors are the surface agents are
        told to trust in place of inferring commands, so advertising either
        one anywhere in a descriptor is a defect, not a detail.
        """

        for capability_id, descriptor in CAPABILITIES.items():
            rendered = json.dumps(descriptor, sort_keys=True)
            for hook in INTERNAL_SCOPE_HOOKS:
                with self.subTest(capability=capability_id, hook=hook):
                    self.assertNotIn(hook, rendered)

    def test_advertised_scope_choices_match_the_documented_contract(self) -> None:
        advertising = {
            capability_id
            for capability_id, descriptor in CAPABILITIES.items()
            if "--scope" in descriptor["command"]
        }

        self.assertEqual(advertising, {"journal.init"})
        for capability_id in advertising:
            with self.subTest(capability=capability_id):
                self.assertIn(
                    CONTRACT_SCOPE_CHOICES,
                    CAPABILITIES[capability_id]["command"],
                )

    def test_parser_still_accepts_the_unadvertised_internal_scope_hook(self) -> None:
        arguments = build_parser().parse_args(
            [
                "journal",
                "init",
                "--scope",
                "agent-root",
                "--agent-root",
                "/tmp/aiq-agent-root",
            ]
        )

        self.assertEqual(arguments.scope, "agent-root")
        self.assertEqual(str(arguments.agent_root), "/tmp/aiq-agent-root")

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
