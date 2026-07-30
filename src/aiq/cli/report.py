from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from aiq import __version__
from aiq.cli._protocol import (
    _add_config_arguments,
    _emit,
    _read_text_argument,
)
from aiq.config import ConfigError
from aiq.journal import (
    JournalError,
    find_message_by_idempotency_key,
    ingest_message,
    resolve_scope,
)
from aiq.queue import (
    TASK_STATES,
    apply_effects,
    claim_message,
    list_tasks,
)


_REPORT_OWNER = "dev-report"
_REPORT_SUMMARY_MAX_CHARS = 200
_REPORT_DETAIL_MAX_CHARS = 16000
_REPORT_OBJECTIVE_MAX_CHARS = 2000


def _report_target(arguments: argparse.Namespace) -> Path:
    if arguments.to is not None:
        if not arguments.to.is_absolute():
            raise JournalError(
                "--to must be an absolute path",
                code="invalid_argument",
            )
        return arguments.to
    configured = arguments.effective_config.dev_report_repo
    if configured is None:
        raise ConfigError(
            "no dev report target: set dev_report_repo in the user "
            "configuration or AIQ_DEV_REPORT_REPO, or pass --to PATH"
        )
    return Path(configured)


def _report_emit(
    arguments: argparse.Namespace,
    payload: Mapping[str, Any],
) -> int:
    if arguments.json:
        _emit(payload, as_json=True)
        return 0
    print(
        "\t".join(
            str(payload[key])
            for key in ("status", "task_id", "message_id")
            if payload.get(key) is not None
        )
    )
    return 0


def _report_tracking_task_id(scope: Any, message_id: str) -> str | None:
    """Resolve the tracking task created by one stored report message."""
    for task in list_tasks(scope, states=set(TASK_STATES), limit=1000):
        if task.get("created_by_message_id") == message_id:
            return task["task_id"]
    return None


def _report_duplicate(
    arguments: argparse.Namespace,
    scope: Any,
    message_id: str,
) -> int:
    payload: dict[str, Any] = {
        "status": "duplicate",
        "scope": scope.to_dict(),
        "message_id": message_id,
    }
    task_id = _report_tracking_task_id(scope, message_id)
    if task_id is not None:
        payload["task_id"] = task_id
    return _report_emit(arguments, payload)


def _report(arguments: argparse.Namespace) -> int:
    summary = arguments.summary
    if not 1 <= len(summary) <= _REPORT_SUMMARY_MAX_CHARS:
        raise JournalError(
            f"summary must contain 1 to {_REPORT_SUMMARY_MAX_CHARS} characters",
            code="invalid_argument",
        )
    detail = (
        arguments.detail
        if arguments.detail is not None
        else _read_text_argument(
            arguments.detail_file,
            4 * _REPORT_DETAIL_MAX_CHARS,
            label="detail",
        )
    )
    if not 1 <= len(detail) <= _REPORT_DETAIL_MAX_CHARS:
        raise JournalError(
            f"detail must contain 1 to {_REPORT_DETAIL_MAX_CHARS} characters",
            code="invalid_argument",
        )
    if not -1_000_000 <= arguments.priority <= 1_000_000:
        raise JournalError(
            "priority must be between -1000000 and 1000000",
            code="invalid_argument",
        )
    target = _report_target(arguments)
    if not target.is_dir():
        raise JournalError(f"dev report target does not exist: {target}")
    scope = resolve_scope("repo", cwd=target)
    if not scope.journal_path.is_file():
        raise JournalError(
            f"dev report target journal does not exist: {scope.journal_path} "
            "(initialize the development checkout with: aiq journal init)"
        )
    content = json.dumps(
        {"aiq_version": __version__, "detail": detail, "summary": summary},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    idempotency_key = hashlib.sha256(content.encode("utf-8")).hexdigest()
    try:
        ingested = ingest_message(
            scope,
            content,
            source="dev-report",
            idempotency_key=idempotency_key,
            cwd=os.fspath(arguments.cwd.resolve()),
        )
    except JournalError as error:
        # An identical report from another origin repository stores a
        # different message identity; treat it as the same known defect.
        # The message substring is a fallback until every raise site
        # carries a stable code.
        if (
            getattr(error, "code", None) != "state_conflict"
            and "different message identity" not in str(error)
        ):
            raise
        existing = find_message_by_idempotency_key(scope, idempotency_key)
        if existing is None:
            raise
        return _report_duplicate(arguments, scope, existing["message_id"])
    if not ingested.created:
        return _report_duplicate(arguments, scope, ingested.message_id)
    try:
        claimed = claim_message(
            scope,
            owner_id=_REPORT_OWNER,
            lease_seconds=arguments.effective_config.lease_seconds,
            message_id=ingested.message_id,
        )
    except JournalError as error:
        # A concurrent instance claimed or applied the message first.
        if (
            getattr(error, "code", None) != "not_claimable"
            and "not claimable" not in str(error)
        ):
            raise
        claimed = None
    if claimed is None:
        return _report_duplicate(arguments, scope, ingested.message_id)
    applied = apply_effects(
        scope,
        ingested.message_id,
        {
            "v": 1,
            "expect": {},
            "effects": [
                [
                    "create",
                    "$report",
                    {
                        "title": summary,
                        "objective": detail[:_REPORT_OBJECTIVE_MAX_CHARS],
                        "priority": arguments.priority,
                    },
                ]
            ],
        },
        claim_id=claimed["claim_id"],
    )
    return _report_emit(
        arguments,
        {
            "status": "reported",
            "task_id": applied["aliases"]["$report"],
            "message_id": ingested.message_id,
            "detail_truncated": len(detail) > _REPORT_OBJECTIVE_MAX_CHARS,
            "scope": scope.to_dict(),
        },
    )


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    report = subparsers.add_parser("report")
    _add_config_arguments(report)
    report.add_argument("--summary", required=True)
    detail_group = report.add_mutually_exclusive_group(required=True)
    detail_group.add_argument("--detail")
    detail_group.add_argument("--detail-file", type=Path, metavar="FILE|-")
    report.add_argument("--to", type=Path)
    report.add_argument("--priority", type=int, default=60)
    report.set_defaults(load_config=True, handler=_report)
