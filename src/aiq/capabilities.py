"""Deterministic discovery metadata for the installed AIQ command surface."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from aiq.journal import JournalError


CAPABILITY_CATALOG_VERSION = 1
CAPABILITY_VERSION = 1


def _capability(
    purpose: str,
    command: str,
    *,
    mutates: bool,
    idempotency: str,
    contract: dict[str, Any] | None = None,
    version: int = CAPABILITY_VERSION,
) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "version": version,
        "available": True,
        "purpose": purpose,
        "command": command,
        "mutates": mutates,
        "idempotency": idempotency,
    }
    if contract is not None:
        descriptor["contract"] = contract
    return descriptor


_CAPABILITIES: dict[str, dict[str, Any]] = {
    "capability.list": _capability(
        "List installed capability IDs without loading their full contracts.",
        "aiq capability list [--json]",
        mutates=False,
        idempotency="read-only",
    ),
    "capability.show": _capability(
        "Read one installed capability contract.",
        "aiq capability show CAPABILITY_ID [--json]",
        mutates=False,
        idempotency="read-only",
    ),
    "claim.release": _capability(
        "Release a message or task lease without completing its work.",
        "aiq claim release CLAIM_ID [--json]",
        mutates=True,
        idempotency="safe retry for the same claim",
    ),
    "config.check": _capability(
        "Validate effective configuration without opening a journal.",
        "aiq config check [--json]",
        mutates=False,
        idempotency="read-only",
    ),
    "config.show": _capability(
        "Read effective configuration and optionally its source layers.",
        "aiq config show [--sources] [--json]",
        mutates=False,
        idempotency="read-only",
    ),
    "inbox.apply": _capability(
        "Commit one claimed message's task effects atomically.",
        "aiq inbox apply MESSAGE_ID --claim CLAIM_ID --effects FILE|- [--json]",
        mutates=True,
        idempotency="identical document and original claim replay one receipt",
        contract={
            "document": {
                "v": 1,
                "required": ["v", "expect", "effects"],
                "optional": ["reason"],
                "empty_effects": "requires reason",
            },
            "operations": {
                "create": [
                    "create",
                    "$alias",
                    {
                        "title": "required",
                        "objective": "optional",
                        "priority": "optional integer",
                        "parent": "optional task reference",
                        "requires": ["optional task references"],
                    },
                ],
                "update": [
                    "update",
                    "task reference",
                    {
                        "title": "optional",
                        "objective": "optional or null",
                        "priority": "optional integer",
                        "parent": "optional or null",
                    },
                ],
                "transition": [
                    "transition",
                    "task reference",
                    "state",
                    {
                        "reason": "required for blocked/canceled/superseded",
                        "by": "required replacement for superseded",
                        "claim": "required current task claim for done",
                    },
                ],
                "require": ["require", "dependent task", "prerequisite task"],
                "unrequire": ["unrequire", "dependent task", "prerequisite task"],
            },
            "rules": [
                "Existing task references require their current revision in expect.",
                "Aliases must be created before use.",
                "New tasks start queued; dependencies determine readiness.",
                "Only a queue claim makes a task active.",
                "The document commits completely or not at all.",
            ],
        },
    ),
    "inbox.claim": _capability(
        "Lease one unapplied message and return its exact content.",
        "aiq inbox claim [MESSAGE_ID] [--owner OWNER] [--json]",
        mutates=True,
        idempotency="not retry-safe after a lost receipt",
    ),
    "inbox.fail": _capability(
        "Close a claimed message that cannot be processed.",
        "aiq inbox fail MESSAGE_ID --claim CLAIM_ID --reason TEXT [--json]",
        mutates=True,
        idempotency="safe retry for the same claim and outcome",
    ),
    "inbox.list": _capability(
        "List bounded message state; exact content is opt-in.",
        "aiq inbox list [--include-content] [--limit N] [--json]",
        mutates=False,
        idempotency="read-only",
    ),
    "inbox.needs-input": _capability(
        "Park a claimed message until missing user input arrives.",
        (
            "aiq inbox needs-input MESSAGE_ID --claim CLAIM_ID "
            "--reason TEXT [--json]"
        ),
        mutates=True,
        idempotency="safe retry for the same claim and outcome",
    ),
    "integration.check": _capability(
        "Detect whether a user-level hook integration is installed or drifted.",
        (
            "aiq integration check (claude|codex) --user [--launcher PATH] "
            "[--git-executable PATH] [--json]"
        ),
        mutates=False,
        idempotency="read-only",
        version=2,
    ),
    "integration.install": _capability(
        "Install or explicitly repair a user-level hook integration.",
        (
            "aiq integration install (claude|codex) --user "
            "[--launcher PATH] [--git-executable PATH] "
            "[--plan-token TOKEN] [--repair] [--json]"
        ),
        mutates=True,
        idempotency="safe retry when installed state is unchanged",
        version=2,
    ),
    "integration.list": _capability(
        "List available integration adapters.",
        "aiq integration list [--json]",
        mutates=False,
        idempotency="read-only",
    ),
    "integration.plan": _capability(
        "Preview a sanitized user-level hook integration change.",
        (
            "aiq integration plan (claude|codex) --user "
            "[--launcher PATH] [--git-executable PATH] [--repair] [--json]"
        ),
        mutates=False,
        idempotency="read-only",
        version=2,
    ),
    "integration.print": _capability(
        "Print an AGENTS bootstrap or hook fragment for external management.",
        (
            "aiq integration print (agents|claude|codex) "
            "[--user] [--launcher PATH] [--git-executable PATH] [--json]"
        ),
        mutates=False,
        idempotency="read-only",
        version=2,
    ),
    "integration.uninstall": _capability(
        "Remove unchanged AIQ-owned integration material.",
        "aiq integration uninstall (claude|codex) --user [--json]",
        mutates=True,
        idempotency="safe retry when no owned material remains",
    ),
    "journal.check": _capability(
        "Verify journal storage and semantic history; migrate supported storage.",
        "aiq journal check [--json]",
        mutates=True,
        idempotency="safe retry",
    ),
    "journal.destroy": _capability(
        "Plan or confirm deletion of only the selected managed journal state.",
        "aiq journal destroy (--plan|--confirm TOKEN) [--json]",
        mutates=True,
        idempotency="confirmation is bound to the current destruction plan",
    ),
    "journal.export": _capability(
        "Write a deterministic private logical export to a new explicit path.",
        "aiq journal export OUTPUT [--json]",
        mutates=True,
        idempotency="refuses to replace an existing output",
    ),
    "journal.init": _capability(
        "Create or validate the selected local journal.",
        "aiq journal init [--json]",
        mutates=True,
        idempotency="safe retry",
    ),
    "journal.path": _capability(
        "Resolve the selected journal location without opening it.",
        "aiq journal path [--json]",
        mutates=False,
        idempotency="read-only",
    ),
    "journal.snapshot": _capability(
        "Create and verify a private SQLite snapshot.",
        "aiq journal snapshot [--keep N] [--json]",
        mutates=True,
        idempotency="each call creates a snapshot",
    ),
    "message.ingest": _capability(
        "Persist one exact message before affected work.",
        (
            "aiq ingest "
            "(--message TEXT|--stdin|--event-json FILE|-) "
            "[--idempotency-key KEY] [--json]"
        ),
        mutates=True,
        idempotency="safe retry when an idempotency key is supplied",
    ),
    "queue.next": _capability(
        "Lease the highest-priority eligible task.",
        "aiq queue next [--owner OWNER] [--limit N] [--json]",
        mutates=True,
        idempotency="not retry-safe after a lost receipt",
    ),
    "queue.peek": _capability(
        "Preview bounded eligible work without reserving it.",
        "aiq queue peek [--limit N] [--json]",
        mutates=False,
        idempotency="read-only",
    ),
    "task.list": _capability(
        "List bounded compact current task state.",
        "aiq task list [--state STATE] [--limit N] [--json]",
        mutates=False,
        idempotency="read-only",
    ),
    "task.show": _capability(
        "Read one task's complete current state.",
        "aiq task show TASK_ID [--json]",
        mutates=False,
        idempotency="read-only",
    ),
}

CAPABILITIES = dict(sorted(_CAPABILITIES.items()))


def list_capabilities() -> list[dict[str, Any]]:
    """Return compact descriptors in canonical capability-ID order."""

    return [
        {
            "id": capability_id,
            "version": capability["version"],
            "available": capability["available"],
            "purpose": capability["purpose"],
        }
        for capability_id, capability in CAPABILITIES.items()
    ]


def capability_catalog() -> dict[str, Any]:
    """Return the versioned compact catalog."""

    return {
        "v": CAPABILITY_CATALOG_VERSION,
        "capabilities": list_capabilities(),
    }


def show_capability(capability_id: str) -> dict[str, Any]:
    """Return one complete descriptor without exposing registry mutation."""

    try:
        capability = CAPABILITIES[capability_id]
    except KeyError as error:
        raise JournalError(f"capability not found: {capability_id}") from error
    return {"id": capability_id, **deepcopy(capability)}
