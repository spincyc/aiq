from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


_TASK_SUMMARY_FIELDS = (
    "task_id",
    "revision",
    "state",
    "priority",
    "title",
    "blocked_by",
    "waiting_on",
)
_TASK_DETAIL_FIELDS = (
    *_TASK_SUMMARY_FIELDS,
    "objective",
    "parent_task_id",
    "dependencies",
    "reason",
    "superseded_by_task_id",
    "created_at",
    "created_by_message_id",
)
_MESSAGE_SUMMARY_FIELDS = (
    "message_id",
    "received_at",
    "source",
    "content_sha256",
    "session_id",
    "turn_id",
    "cwd",
    "state",
    "lease_status",
)


def _task_reference(project: str, task_id: str) -> str:
    """Render one task reference for human output: ``[aiq: TASK-19]``.

    Human listings name the project a task belongs to so references stay
    unambiguous when several repositories or an orchestrating project are
    in play. JSON output never carries the prefix: ``task_id`` values stay
    bare and the label is reported once as a top-level ``project`` field.
    Bare IDs also stay bare wherever the printed text is meant to be
    copied into a command, such as the Stop gate's settle tail.
    """

    return f"[{project}: {task_id}]"


def _task_summary(task: Mapping[str, Any]) -> dict[str, Any]:
    return {field: task[field] for field in _TASK_SUMMARY_FIELDS}


def _task_detail(task: Mapping[str, Any]) -> dict[str, Any]:
    return {field: task.get(field) for field in _TASK_DETAIL_FIELDS}


def _message_summary(
    message: Mapping[str, Any],
    *,
    include_content: bool,
) -> dict[str, Any]:
    result = {field: message.get(field) for field in _MESSAGE_SUMMARY_FIELDS}
    if include_content:
        result["content"] = message.get("content")
    return result


def _timestamp_from_microseconds(value: int) -> str:
    return (
        datetime.fromtimestamp(value / 1_000_000, tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _claim_public(claim: Mapping[str, Any]) -> dict[str, Any]:
    expires_at = claim.get("expires_at")
    if expires_at is None and isinstance(claim.get("expires_at_us"), int):
        expires_at = _timestamp_from_microseconds(claim["expires_at_us"])
    return {
        "claim_id": claim["claim_id"],
        "resource_kind": claim["resource_kind"],
        "resource_id": claim["resource_id"],
        "owner_id": claim["owner_id"],
        "basis_revision": claim.get("basis_revision"),
        "expires_at": expires_at,
    }


def _application_public(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": result["status"],
        "message_id": result["message_id"],
        "effects_sha256": result["effects_sha256"],
        "aliases": result["aliases"],
        "tasks": [_task_summary(task) for task in result["tasks"]],
        "replayed": result["replayed"],
    }
