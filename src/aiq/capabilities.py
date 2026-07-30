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
    "claim.list": _capability(
        "List bounded unreleased message and task leases with expiry status.",
        (
            "aiq claim list [--owner OWNER] [--resource message|task] "
            "[--status active|expired] [--limit N] [--json]"
        ),
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
        (
            "aiq config check [--owner OWNER] [--reader ID] "
            "[--lease-seconds SECONDS] [--snapshot-keep COUNT] "
            "[--no-repo-config] [--json]"
        ),
        mutates=False,
        idempotency="read-only",
    ),
    "config.show": _capability(
        "Read effective configuration and optionally its source layers.",
        (
            "aiq config show [--sources] [--owner OWNER] [--reader ID] "
            "[--lease-seconds SECONDS] [--snapshot-keep COUNT] "
            "[--no-repo-config] [--json]"
        ),
        mutates=False,
        idempotency="read-only",
    ),
    "doctor": _capability(
        "Summarize local configuration, dependency, journal, scope, and "
        "integration health without mutating state.",
        "aiq doctor [--json]",
        mutates=False,
        idempotency="read-only",
        contract={
            "checks": [
                "python",
                "sqlite",
                "config",
                "git",
                "scope",
                "journal",
                "capture",
                "journal.deep",
                "integration.claude",
                "integration.codex",
                "report",
            ],
            "statuses": ["ok", "warn", "fail", "skipped"],
            "exit": "0 when no check fails; 1 when any check fails",
            "deep": (
                "deep journal verification stays explicit through "
                "aiq journal check, which may migrate supported storage"
            ),
        },
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
        "Lease one unapplied message and return its exact content; an "
        "explicit MESSAGE_ID may also resume a parked needs_input message "
        "once its missing input has arrived, or reopen a failed message "
        "whose disposition was misjudged. Requires the scope's reader "
        "lease, which a successful claim takes implicitly when it is "
        "free; another live reader is refused with reader_held.",
        (
            "aiq inbox claim [MESSAGE_ID] [--owner OWNER] "
            "[--lease-seconds N] [--json]"
        ),
        mutates=True,
        idempotency="not retry-safe after a lost receipt",
        version=2,
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
        "Detect whether a managed hook or guidance integration is installed "
        "or drifted.",
        (
            "aiq integration check ((claude|codex) --user [--launcher PATH] "
            "[--git-executable PATH] | guidance --target PATH) [--json]"
        ),
        mutates=False,
        idempotency="read-only",
        version=2,
    ),
    "integration.install": _capability(
        "Install or explicitly repair a managed hook or guidance integration.",
        (
            "aiq integration install ((claude|codex) --user "
            "[--launcher PATH] [--git-executable PATH] "
            "| guidance --target PATH) "
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
        "Preview a sanitized managed hook or guidance integration change.",
        (
            "aiq integration plan ((claude|codex) --user "
            "[--launcher PATH] [--git-executable PATH] "
            "| guidance --target PATH) [--repair] [--json]"
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
        (
            "aiq integration uninstall "
            "((claude|codex) --user | guidance --target PATH) [--json]"
        ),
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
        "Create or validate the selected local journal; initializing a "
        "repository journal is the act that opts that repository into "
        "hook capture.",
        "aiq journal init [--scope auto|repo|user] [--label TEXT] [--json]",
        mutates=True,
        idempotency="safe retry",
        version=3,
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
            "[--source NAME] [--idempotency-key KEY] "
            "[--session-id ID] [--turn-id ID] "
            "[--if-new] [--quiet] [--json]"
        ),
        mutates=True,
        idempotency="safe retry when an idempotency key is supplied; "
        "--if-new returns the existing received message for identical "
        "content with a deduped flag instead of storing a duplicate",
    ),
    "reader.acquire": _capability(
        "Hold the scope's single reader role without consuming work, so "
        "an idle session keeps the right to drain.",
        (
            "aiq reader acquire [--reader ID] [--owner OWNER] "
            "[--lease-seconds N] [--json]"
        ),
        mutates=True,
        idempotency="acquiring while holding renews the same lease",
    ),
    "reader.release": _capability(
        "Give up the scope's reader role, leaving every held claim "
        "untouched to recover on its own schedule, and record that this "
        "session stopped draining the queue. That record is how a "
        "bounded run -- one task, or a fixed batch -- ends with ready "
        "work deliberately left behind instead of being blocked by the "
        "completion gate. It is recorded only when the caller proves it "
        "holds the lease, so status released with declared false means "
        "the role came back but no completion signal was written.",
        "aiq reader release [--reader ID] [--force] [--json]",
        mutates=True,
        idempotency="safe retry; a still-live lease this caller proved "
        "holding replays as already_released, while an expired one "
        "reports not_held because the declaration lapsed with it",
        version=2,
    ),
    "reader.status": _capability(
        "Read who currently holds the scope's reader role and until when.",
        "aiq reader status [--json]",
        mutates=False,
        idempotency="read-only",
    ),
    "reconcile.run": _capability(
        "Report AIQ-owned integration and journal health after an external "
        "installer upgrades AIQ; --apply repairs planned AIQ-owned drift "
        "and validates or migrates selected journal state.",
        (
            "aiq reconcile --user [--apply] "
            "[--launcher PATH] [--git-executable PATH] [--json]"
        ),
        mutates=True,
        idempotency="report-only by default (cheap read-only journal "
        "inspection); --apply repairs only planned AIQ-owned drift",
    ),
    "report.send": _capability(
        "Report an AIQ defect as one deduplicated bug-fix task in the local "
        "AIQ development repository's queue.",
        (
            "aiq report --summary TEXT (--detail TEXT|--detail-file FILE|-) "
            "[--to PATH] [--priority N] [--json]"
        ),
        mutates=True,
        idempotency="identical report replays as a duplicate carrying the "
        "tracking task without creating a second task",
    ),
    "queue.dequeue": _capability(
        "Lease the highest-priority eligible task; the ergonomic synonym "
        "of queue.next with identical time-bounded lease semantics, never "
        "removal, and the identical reader-lease requirement.",
        (
            "aiq dequeue [--owner OWNER] [--lease-seconds N] "
            "[--limit N] [--json]"
        ),
        mutates=True,
        idempotency="not retry-safe after a lost receipt",
        version=2,
    ),
    "queue.next": _capability(
        "Lease the highest-priority eligible task. Requires the scope's "
        "reader lease, which a successful lease takes implicitly when it "
        "is free; another live reader is refused with reader_held, even "
        "when the queue is empty.",
        (
            "aiq queue next [--owner OWNER] [--lease-seconds N] "
            "[--limit N] [--json]"
        ),
        mutates=True,
        idempotency="not retry-safe after a lost receipt",
        version=2,
    ),
    "queue.peek": _capability(
        "Preview bounded eligible work without reserving it.",
        "aiq queue peek [--limit N] [--json]",
        mutates=False,
        idempotency="read-only",
    ),
    "status.show": _capability(
        "Read one bounded work-state snapshot without message content.",
        "aiq status [--json]",
        mutates=False,
        idempotency=(
            "changes no work state; opening a journal at an older stored "
            "schema first runs the pending migration with a backup"
        ),
    ),
    "task.done": _capability(
        "Settle one or more ready or owned active tasks as done through "
        "one recorded summary message and one atomic effects application; "
        "any ineligible task fails the whole command. Single-reader "
        "governs dispatch, not settlement: settling a task already active "
        "under the caller stays open to every session, while settling a "
        "merely ready task leases it here and needs the reader lease. It "
        "never takes the lease implicitly.",
        (
            "aiq task done TASK_ID [TASK_ID ...] --summary TEXT "
            "[--owner OWNER] [--json]"
        ),
        mutates=True,
        idempotency="all-or-nothing; a retry after success fails because "
        "done tasks are terminal",
        version=2,
    ),
    "task.enqueue": _capability(
        "Create one task in one transaction through an auto-recorded "
        "message and one atomic create-task effects application.",
        (
            "aiq enqueue TITLE [--objective TEXT] [--priority N] "
            "[--requires TASK-ID ...] [--json]"
        ),
        mutates=True,
        idempotency="each call records one new message and task; failures "
        "roll the whole request back",
    ),
    "task.list": _capability(
        "List bounded compact current task state.",
        "aiq task list [--state STATE]... [--limit N] [--json]",
        mutates=False,
        idempotency="read-only",
    ),
    "task.overview": _capability(
        "List tasks in task-number order; terminal states are included "
        "with --all or an explicit --state filter. --state accumulates "
        "one state per occurrence and is mutually exclusive with --all.",
        "aiq list [--state STATE]... [--all] [--limit N] [--json]",
        mutates=False,
        idempotency="read-only",
    ),
    "task.explain": _capability(
        "Explain deterministically why one task is or is not eligible.",
        "aiq task explain TASK_ID [--json]",
        mutates=False,
        idempotency="read-only",
    ),
    "task.history": _capability(
        "Read one task's bounded recorded event lineage, newest first.",
        "aiq task history TASK_ID [--limit N] [--json]",
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
        raise JournalError(
            f"capability not found: {capability_id}",
            code="not_found",
        ) from error
    return {"id": capability_id, **deepcopy(capability)}
