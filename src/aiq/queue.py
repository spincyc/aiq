from __future__ import annotations

from collections import deque
from copy import deepcopy
import hashlib
import json
import re
import sqlite3
import time
from typing import Any

from aiq.journal import (
    JournalError,
    JournalScope,
    _connect,
    _identifier,
    _utc_now,
)


EFFECT_DOCUMENT_MAX_BYTES = 65536
EFFECT_COUNT_MAX = 64
TASK_STATES = (
    "queued",
    "ready",
    "active",
    "blocked",
    "done",
    "canceled",
    "superseded",
)
TERMINAL_STATES = {"done", "canceled", "superseded"}
FAILURE_STATES = {"blocked", "canceled", "superseded"}
TRANSITIONS = {
    "queued": {"ready", "blocked", "canceled", "superseded"},
    "ready": {"queued", "active", "blocked", "canceled", "superseded"},
    "active": {"queued", "ready", "blocked", "done", "canceled", "superseded"},
    "blocked": {"queued", "ready", "canceled", "superseded"},
    "done": set(),
    "canceled": set(),
    "superseded": set(),
}
TASK_ID_PATTERN = re.compile(r"TASK-([1-9][0-9]*)\Z")
ALIAS_PATTERN = re.compile(r"\$[a-z][a-z0-9_-]{0,31}\Z")
CLAIM_ID_PATTERN = re.compile(r"clm_[0-9a-f]{32}\Z")


def _now_us() -> int:
    return time.time_ns() // 1000


def _reject_constant(value: str) -> None:
    raise JournalError(f"invalid JSON number: {value}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JournalError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_effect_document(raw: str) -> dict[str, Any]:
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError as error:
        raise JournalError("effects document is not valid UTF-8") from error
    if len(encoded) > EFFECT_DOCUMENT_MAX_BYTES:
        raise JournalError(
            f"effects document exceeds {EFFECT_DOCUMENT_MAX_BYTES} bytes"
        )
    try:
        document = json.loads(
            raw,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, RecursionError) as error:
        raise JournalError(f"invalid effects JSON: {error}") from error
    if not isinstance(document, dict):
        raise JournalError("effects document must be a JSON object")
    _validate_document_shape(document)
    return document


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise JournalError(f"effects document is not canonical JSON: {error}") from error


def _exact_keys(
    value: dict[str, Any],
    *,
    allowed: set[str],
    required: set[str],
    path: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        raise JournalError(f"{path} has unknown keys: {', '.join(unknown)}")
    if missing:
        raise JournalError(f"{path} is missing keys: {', '.join(missing)}")


def _integer(value: Any, *, path: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise JournalError(f"{path} must be an integer")
    if not minimum <= value <= maximum:
        raise JournalError(f"{path} must be between {minimum} and {maximum}")
    return value


def _text(
    value: Any,
    *,
    path: str,
    minimum: int = 0,
    maximum: int,
) -> str:
    if not isinstance(value, str):
        raise JournalError(f"{path} must be a string")
    if not minimum <= len(value) <= maximum:
        raise JournalError(
            f"{path} length must be between {minimum} and {maximum}"
        )
    if "\x00" in value:
        raise JournalError(f"{path} must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise JournalError(f"{path} is not valid UTF-8") from error
    return value


def _task_reference(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise JournalError(f"{path} must be a task ID or local alias")
    if TASK_ID_PATTERN.fullmatch(value) or ALIAS_PATTERN.fullmatch(value):
        return value
    raise JournalError(f"{path} is not a canonical task ID or local alias")


def _validate_document_shape(document: dict[str, Any]) -> None:
    _exact_keys(
        document,
        allowed={"v", "expect", "effects", "reason"},
        required={"v", "expect", "effects"},
        path="document",
    )
    if document["v"] != 1 or isinstance(document["v"], bool):
        raise JournalError("document.v must be 1")
    if not isinstance(document["expect"], dict):
        raise JournalError("document.expect must be an object")
    for task_id, revision in document["expect"].items():
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise JournalError(f"document.expect has invalid task ID: {task_id}")
        _integer(
            revision,
            path=f"document.expect.{task_id}",
            minimum=1,
            maximum=2**63 - 1,
        )
    effects = document["effects"]
    if not isinstance(effects, list):
        raise JournalError("document.effects must be an array")
    if len(effects) > EFFECT_COUNT_MAX:
        raise JournalError(f"document.effects may contain at most {EFFECT_COUNT_MAX} effects")
    if not effects:
        reason = document.get("reason")
        _text(reason, path="document.reason", minimum=1, maximum=1000)
    elif "reason" in document:
        raise JournalError("document.reason is allowed only when effects is empty")
    for index, effect in enumerate(effects):
        if not isinstance(effect, list) or not effect:
            raise JournalError(f"document.effects[{index}] must be a nonempty array")
        if not isinstance(effect[0], str) or effect[0] not in {
            "create",
            "update",
            "transition",
            "require",
            "unrequire",
        }:
            raise JournalError(
                f"document.effects[{index}] has unknown operation: {effect[0]!r}"
            )


def _load_current_tasks(
    connection: sqlite3.Connection,
    *,
    now_us: int | None = None,
) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
          current.task_id,
          current.revision,
          current.event_sequence,
          current.state,
          current.title,
          current.objective,
          current.priority,
          current.parent_task_id,
          current.dependencies_json,
          current.reason,
          current.superseded_by_task_id,
          task.task_number,
          task.created_at,
          task.created_by_message_id,
          task.created_sequence
        FROM current_tasks AS current
        JOIN tasks AS task ON task.task_id = current.task_id
        """
    ).fetchall()
    tasks: dict[str, dict[str, Any]] = {}
    for row in rows:
        task = dict(row)
        task["dependencies"] = json.loads(task.pop("dependencies_json"))
        task["claim"] = None
        tasks[task["task_id"]] = task
    effective_now = _now_us() if now_us is None else now_us
    claims = connection.execute(
        """
        SELECT
          claim.claim_id,
          claim.resource_kind,
          claim.resource_id,
          claim.owner_id,
          claim.fence,
          claim.basis_revision,
          claim.expires_at_us
        FROM claims AS claim
        LEFT JOIN claim_releases AS release
          ON release.claim_id = claim.claim_id
        WHERE claim.resource_kind = 'task'
          AND release.claim_id IS NULL
          AND claim.expires_at_us > ?
        ORDER BY claim.fence
        """,
        (effective_now,),
    ).fetchall()
    for claim in claims:
        task_id = claim["resource_id"]
        if task_id not in tasks:
            raise JournalError(f"task claim references missing task: {task_id}")
        if tasks[task_id]["claim"] is not None:
            raise JournalError(f"task has multiple active claims: {task_id}")
        if (
            claim["basis_revision"] != tasks[task_id]["revision"]
            or tasks[task_id]["state"] in TERMINAL_STATES
        ):
            raise JournalError(f"task has a stale active claim: {task_id}")
        tasks[task_id]["claim"] = dict(claim)
    return tasks


def _effective_states(tasks: dict[str, dict[str, Any]]) -> dict[str, str]:
    states: dict[str, str] = {}
    dependents: dict[str, list[str]] = {task_id: [] for task_id in tasks}
    remaining: dict[str, int] = {}
    ready: deque[str] = deque()
    for task_id, task in tasks.items():
        for prerequisite in task["dependencies"]:
            if prerequisite not in tasks:
                raise JournalError(f"dependency task not found: {prerequisite}")
            dependents[prerequisite].append(task_id)
        if task.get("claim") is not None or task["state"] not in {"queued", "ready"}:
            remaining[task_id] = 0
            ready.append(task_id)
        else:
            remaining[task_id] = len(task["dependencies"])
            if not task["dependencies"]:
                ready.append(task_id)

    while ready:
        task_id = ready.popleft()
        if task_id in states:
            continue
        task = tasks[task_id]
        intrinsic = task["state"]
        if task.get("claim") is not None:
            states[task_id] = "active"
        elif intrinsic not in {"queued", "ready"}:
            states[task_id] = intrinsic
        else:
            prerequisite_states = [
                states[prerequisite]
                for prerequisite in task["dependencies"]
            ]
            if any(state in FAILURE_STATES for state in prerequisite_states):
                states[task_id] = "blocked"
            elif any(state != "done" for state in prerequisite_states):
                states[task_id] = "queued"
            else:
                states[task_id] = "ready"
        for dependent in dependents[task_id]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                ready.append(dependent)

    if len(states) != len(tasks):
        unresolved = min(set(tasks) - set(states))
        raise JournalError(f"dependency cycle contains {unresolved}")
    return states


def _task_output(
    task: dict[str, Any],
    effective_state: str,
    all_states: dict[str, str],
) -> dict[str, Any]:
    blocked_by = sorted(
        dependency
        for dependency in task["dependencies"]
        if all_states[dependency] in FAILURE_STATES
    )
    waiting_on = sorted(
        dependency
        for dependency in task["dependencies"]
        if all_states[dependency] not in {"done", *FAILURE_STATES}
    )
    return {
        "task_id": task["task_id"],
        "revision": task["revision"],
        "state": effective_state,
        "recorded_state": task["state"],
        "title": task["title"],
        "objective": task["objective"],
        "priority": task["priority"],
        "parent_task_id": task["parent_task_id"],
        "dependencies": list(task["dependencies"]),
        "blocked_by": blocked_by,
        "waiting_on": waiting_on,
        "reason": task["reason"],
        "superseded_by_task_id": task["superseded_by_task_id"],
        "created_at": task["created_at"],
        "created_by_message_id": task["created_by_message_id"],
        "last_sequence": task["event_sequence"],
        "claim": deepcopy(task.get("claim")),
    }


def _append_claim_release(
    connection: sqlite3.Connection,
    claim: sqlite3.Row | dict[str, Any],
    *,
    disposition: str,
    now_us: int,
) -> int:
    event_type = {
        "released": "claim.released",
        "applied": "claim.consumed",
        "completed": "claim.consumed",
        "needs_input": "claim.consumed",
        "failed": "claim.consumed",
        "revoked": "claim.revoked",
        "expired": "claim.expired",
    }[disposition]
    message_id = (
        claim["resource_id"] if claim["resource_kind"] == "message" else None
    )
    task_id = claim["resource_id"] if claim["resource_kind"] == "task" else None
    cursor = connection.execute(
        """
        INSERT INTO events(
          event_id,
          occurred_at,
          event_type,
          message_id,
          task_id,
          payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            _identifier("evt"),
            _utc_now(),
            event_type,
            message_id,
            task_id,
            _canonical_json(
                {
                    "claim_id": claim["claim_id"],
                    "disposition": disposition,
                }
            ),
        ),
    )
    connection.execute(
        """
        INSERT INTO claim_releases(
          claim_id,
          event_sequence,
          disposition,
          released_at_us
        ) VALUES (?, ?, ?, ?)
        """,
        (claim["claim_id"], cursor.lastrowid, disposition, now_us),
    )
    return cursor.lastrowid


def _recover_expired_claims(
    connection: sqlite3.Connection,
    *,
    resource_kind: str,
    now_us: int,
) -> int:
    expired = connection.execute(
        """
        SELECT claim.*
        FROM claims AS claim
        LEFT JOIN claim_releases AS release
          ON release.claim_id = claim.claim_id
        WHERE claim.resource_kind = ?
          AND release.claim_id IS NULL
          AND claim.expires_at_us <= ?
        ORDER BY claim.fence
        """,
        (resource_kind, now_us),
    ).fetchall()
    for claim in expired:
        _append_claim_release(
            connection,
            claim,
            disposition="expired",
            now_us=now_us,
        )
        if resource_kind == "message":
            connection.execute(
                """
                INSERT INTO events(
                  event_id,
                  occurred_at,
                  event_type,
                  message_id,
                  payload_json
                ) VALUES (?, ?, 'message.received', ?, ?)
                """,
                (
                    _identifier("evt"),
                    _utc_now(),
                    claim["resource_id"],
                    _canonical_json(
                        {
                            "recovered_claim_id": claim["claim_id"],
                        }
                    ),
                ),
            )
    return len(expired)


def _claim_resource(
    connection: sqlite3.Connection,
    *,
    resource_kind: str,
    resource_id: str,
    owner_id: str,
    lease_seconds: int,
    now_us: int,
    basis_revision: int | None,
) -> dict[str, Any]:
    claim_id = _identifier("clm")
    expires_at_us = now_us + lease_seconds * 1_000_000
    message_id = resource_id if resource_kind == "message" else None
    task_id = resource_id if resource_kind == "task" else None
    cursor = connection.execute(
        """
        INSERT INTO events(
          event_id,
          occurred_at,
          event_type,
          message_id,
          task_id,
          payload_json
        ) VALUES (?, ?, 'claim.acquired', ?, ?, ?)
        """,
        (
            _identifier("evt"),
            _utc_now(),
            message_id,
            task_id,
            _canonical_json(
                {
                    "claim_id": claim_id,
                    "owner_id": owner_id,
                    "expires_at_us": expires_at_us,
                    **(
                        {"basis_revision": basis_revision}
                        if basis_revision is not None
                        else {}
                    ),
                }
            ),
        ),
    )
    connection.execute(
        """
        INSERT INTO claims(
          claim_id,
          resource_kind,
          resource_id,
          owner_id,
          fence,
          basis_revision,
          acquired_at_us,
          expires_at_us
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            claim_id,
            resource_kind,
            resource_id,
            owner_id,
            cursor.lastrowid,
            basis_revision,
            now_us,
            expires_at_us,
        ),
    )
    return {
        "claim_id": claim_id,
        "resource_kind": resource_kind,
        "resource_id": resource_id,
        "owner_id": owner_id,
        "fence": cursor.lastrowid,
        "basis_revision": basis_revision,
        "acquired_at_us": now_us,
        "expires_at_us": expires_at_us,
    }


def claim_message(
    scope: JournalScope,
    *,
    owner_id: str,
    lease_seconds: int = 900,
    message_id: str | None = None,
    now_us: int | None = None,
) -> dict[str, Any] | None:
    owner = _text(owner_id, path="owner_id", minimum=1, maximum=200)
    lease = _integer(
        lease_seconds,
        path="lease_seconds",
        minimum=1,
        maximum=86400,
    )
    effective_now = _now_us() if now_us is None else now_us
    connection = _connect(scope)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _recover_expired_claims(
            connection,
            resource_kind="message",
            now_us=effective_now,
        )
        parameters: list[Any] = []
        requested = ""
        if message_id is not None:
            requested = "AND message.message_id = ?"
            parameters.append(message_id)
        row = connection.execute(
            f"""
            WITH lifecycle AS (
              SELECT
                event.message_id,
                event.event_type,
                event.sequence,
                ROW_NUMBER() OVER (
                  PARTITION BY event.message_id
                  ORDER BY event.sequence DESC
                ) AS rank
              FROM events AS event
              WHERE event.message_id IS NOT NULL
                AND event.event_type IN (
                  'message.received',
                  'message.processing',
                  'message.applied',
                  'message.needs_input',
                  'message.failed',
                  'message.superseded'
                )
            )
            SELECT message.*
            FROM messages AS message
            JOIN lifecycle
              ON lifecycle.message_id = message.message_id
             AND lifecycle.rank = 1
            WHERE lifecycle.event_type = 'message.received'
              {requested}
              AND NOT EXISTS (
                SELECT 1
                FROM claims AS claim
                LEFT JOIN claim_releases AS release
                  ON release.claim_id = claim.claim_id
                WHERE claim.resource_kind = 'message'
                  AND claim.resource_id = message.message_id
                  AND release.claim_id IS NULL
              )
            ORDER BY lifecycle.sequence, message.message_id
            LIMIT 1
            """,
            parameters,
        ).fetchone()
        if row is None:
            if message_id is not None:
                exists = connection.execute(
                    "SELECT 1 FROM messages WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                if not exists:
                    raise JournalError(f"message not found: {message_id}")
                raise JournalError(f"message is not claimable: {message_id}")
            connection.commit()
            return None
        claim = _claim_resource(
            connection,
            resource_kind="message",
            resource_id=row["message_id"],
            owner_id=owner,
            lease_seconds=lease,
            now_us=effective_now,
            basis_revision=None,
        )
        connection.execute(
            """
            INSERT INTO events(
              event_id,
              occurred_at,
              event_type,
              message_id,
              payload_json
            ) VALUES (?, ?, 'message.processing', ?, ?)
            """,
            (
                _identifier("evt"),
                _utc_now(),
                row["message_id"],
                _canonical_json({"claim_id": claim["claim_id"]}),
            ),
        )
        connection.commit()
        return {
            **claim,
            "message": {
                "message_id": row["message_id"],
                "received_at": row["received_at"],
                "source": row["source"],
                "content": row["content"],
            },
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_next_tasks(
    scope: JournalScope,
    *,
    owner_id: str,
    lease_seconds: int = 900,
    limit: int = 1,
    now_us: int | None = None,
) -> list[dict[str, Any]]:
    owner = _text(owner_id, path="owner_id", minimum=1, maximum=200)
    lease = _integer(
        lease_seconds,
        path="lease_seconds",
        minimum=1,
        maximum=86400,
    )
    if limit < 1 or limit > 64:
        raise JournalError("queue limit must be between 1 and 64")
    effective_now = _now_us() if now_us is None else now_us
    connection = _connect(scope)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _recover_expired_claims(
            connection,
            resource_kind="task",
            now_us=effective_now,
        )
        tasks = _load_current_tasks(connection, now_us=effective_now)
        states = _effective_states(tasks)
        candidates = [
            task
            for task in tasks.values()
            if states[task["task_id"]] == "ready"
        ]
        candidates.sort(
            key=lambda task: (
                -task["priority"],
                task["created_sequence"],
                task["task_number"],
            )
        )
        claimed: list[dict[str, Any]] = []
        for task in candidates[:limit]:
            claim = _claim_resource(
                connection,
                resource_kind="task",
                resource_id=task["task_id"],
                owner_id=owner,
                lease_seconds=lease,
                now_us=effective_now,
                basis_revision=task["revision"],
            )
            task["claim"] = claim
            claimed.append(
                {
                    "task": _task_output(task, "active", states),
                    "claim": claim,
                }
            )
        connection.commit()
        return claimed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_task(
    scope: JournalScope,
    task_id: str,
    *,
    owner_id: str,
    lease_seconds: int = 900,
    now_us: int | None = None,
) -> dict[str, Any]:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise JournalError(f"invalid task ID: {task_id}")
    owner = _text(owner_id, path="owner_id", minimum=1, maximum=200)
    lease = _integer(
        lease_seconds,
        path="lease_seconds",
        minimum=1,
        maximum=86400,
    )
    effective_now = _now_us() if now_us is None else now_us
    connection = _connect(scope)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _recover_expired_claims(
            connection,
            resource_kind="task",
            now_us=effective_now,
        )
        tasks = _load_current_tasks(connection, now_us=effective_now)
        if task_id not in tasks:
            raise JournalError(f"task not found: {task_id}")
        states = _effective_states(tasks)
        if states[task_id] != "ready":
            raise JournalError(f"task is not ready: {task_id}: {states[task_id]}")
        task = tasks[task_id]
        claim = _claim_resource(
            connection,
            resource_kind="task",
            resource_id=task_id,
            owner_id=owner,
            lease_seconds=lease,
            now_us=effective_now,
            basis_revision=task["revision"],
        )
        task["claim"] = claim
        connection.commit()
        return {
            "task": _task_output(task, "active", states),
            "claim": claim,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def release_claim(
    scope: JournalScope,
    claim_id: str,
    *,
    now_us: int | None = None,
) -> dict[str, Any]:
    if not CLAIM_ID_PATTERN.fullmatch(claim_id):
        raise JournalError(f"invalid claim ID: {claim_id}")
    effective_now = _now_us() if now_us is None else now_us
    connection = _connect(scope)
    try:
        connection.execute("BEGIN IMMEDIATE")
        claim = connection.execute(
            """
            SELECT
              claim.*,
              release.disposition AS release_disposition,
              release.event_sequence AS release_sequence
            FROM claims AS claim
            LEFT JOIN claim_releases AS release
              ON release.claim_id = claim.claim_id
            WHERE claim.claim_id = ?
            """,
            (claim_id,),
        ).fetchone()
        if not claim:
            raise JournalError(f"claim is not active: {claim_id}")
        if claim["release_disposition"] is not None:
            if claim["release_disposition"] != "released":
                raise JournalError(f"claim is not active: {claim_id}")
            connection.commit()
            return {
                "status": "released",
                "claim_id": claim_id,
                "resource_kind": claim["resource_kind"],
                "resource_id": claim["resource_id"],
                "sequence": claim["release_sequence"],
                "replayed": True,
            }
        if claim["expires_at_us"] <= effective_now:
            _recover_expired_claims(
                connection,
                resource_kind=claim["resource_kind"],
                now_us=effective_now,
            )
            connection.commit()
            raise JournalError(f"claim has expired: {claim_id}")
        sequence = _append_claim_release(
            connection,
            claim,
            disposition="released",
            now_us=effective_now,
        )
        if claim["resource_kind"] == "message":
            connection.execute(
                """
                INSERT INTO events(
                  event_id,
                  occurred_at,
                  event_type,
                  message_id,
                  payload_json
                ) VALUES (?, ?, 'message.received', ?, ?)
                """,
                (
                    _identifier("evt"),
                    _utc_now(),
                    claim["resource_id"],
                    _canonical_json({"released_claim_id": claim_id}),
                ),
            )
        connection.commit()
        return {
            "status": "released",
            "claim_id": claim_id,
            "resource_kind": claim["resource_kind"],
            "resource_id": claim["resource_id"],
            "sequence": sequence,
            "replayed": False,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def dispose_message(
    scope: JournalScope,
    message_id: str,
    *,
    claim_id: str,
    disposition: str,
    reason: str,
    now_us: int | None = None,
) -> dict[str, Any]:
    if disposition not in {"needs_input", "failed"}:
        raise JournalError(f"invalid message disposition: {disposition}")
    if not CLAIM_ID_PATTERN.fullmatch(claim_id):
        raise JournalError(f"invalid claim ID: {claim_id}")
    explanation = _text(reason, path="reason", minimum=1, maximum=1000)
    effective_now = _now_us() if now_us is None else now_us
    connection = _connect(scope)
    try:
        connection.execute("BEGIN IMMEDIATE")
        claim = connection.execute(
            """
            SELECT claim.*, release.disposition AS release_disposition
            FROM claims AS claim
            LEFT JOIN claim_releases AS release
              ON release.claim_id = claim.claim_id
            WHERE claim.claim_id = ?
              AND claim.resource_kind = 'message'
              AND claim.resource_id = ?
            """,
            (claim_id, message_id),
        ).fetchone()
        if claim is None:
            raise JournalError(f"message claim does not match: {claim_id}")
        if claim["release_disposition"] is not None:
            if claim["release_disposition"] != disposition:
                raise JournalError(f"message claim is not active: {claim_id}")
            event = connection.execute(
                """
                SELECT sequence, payload_json
                FROM events
                WHERE message_id = ? AND event_type = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (message_id, f"message.{disposition}"),
            ).fetchone()
            if event is None:
                raise JournalError(f"message disposition event is missing: {message_id}")
            payload = json.loads(event["payload_json"])
            if payload != {"claim_id": claim_id, "reason": explanation}:
                raise JournalError(
                    f"message already has a different disposition: {message_id}"
                )
            connection.commit()
            return {
                "status": disposition,
                "message_id": message_id,
                "claim_id": claim_id,
                "sequence": event["sequence"],
                "replayed": True,
            }
        if claim["expires_at_us"] <= effective_now:
            _recover_expired_claims(
                connection,
                resource_kind="message",
                now_us=effective_now,
            )
            connection.commit()
            raise JournalError(f"message claim has expired: {claim_id}")
        _append_claim_release(
            connection,
            claim,
            disposition=disposition,
            now_us=effective_now,
        )
        cursor = connection.execute(
            """
            INSERT INTO events(
              event_id,
              occurred_at,
              event_type,
              message_id,
              payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                _identifier("evt"),
                _utc_now(),
                f"message.{disposition}",
                message_id,
                _canonical_json({"claim_id": claim_id, "reason": explanation}),
            ),
        )
        connection.commit()
        return {
            "status": disposition,
            "message_id": message_id,
            "claim_id": claim_id,
            "sequence": cursor.lastrowid,
            "replayed": False,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_tasks(
    scope: JournalScope,
    *,
    states: set[str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 1000:
        raise JournalError("task limit must be between 1 and 1000")
    if states and not states <= set(TASK_STATES):
        raise JournalError("unsupported task state filter")
    connection = _connect(scope)
    try:
        tasks = _load_current_tasks(connection)
        effective = _effective_states(tasks)
        selected = [
            task
            for task in tasks.values()
            if (
                effective[task["task_id"]] in states
                if states is not None
                else effective[task["task_id"]] not in TERMINAL_STATES
            )
        ]
        selected.sort(
            key=lambda task: (
                -task["priority"],
                task["created_sequence"],
                task["task_number"],
            )
        )
        return [
            _task_output(task, effective[task["task_id"]], effective)
            for task in selected[:limit]
        ]
    finally:
        connection.close()


def show_task(scope: JournalScope, task_id: str) -> dict[str, Any]:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise JournalError(f"invalid task ID: {task_id}")
    connection = _connect(scope)
    try:
        tasks = _load_current_tasks(connection)
        if task_id not in tasks:
            raise JournalError(f"task not found: {task_id}")
        effective = _effective_states(tasks)
        return _task_output(tasks[task_id], effective[task_id], effective)
    finally:
        connection.close()


def next_tasks(
    scope: JournalScope,
    *,
    limit: int = 1,
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 64:
        raise JournalError("queue limit must be between 1 and 64")
    return list_tasks(scope, states={"ready"}, limit=limit)


def _resolve(reference: str, aliases: dict[str, str]) -> str:
    if reference.startswith("$"):
        try:
            return aliases[reference]
        except KeyError as error:
            raise JournalError(f"unknown local task alias: {reference}") from error
    return reference


def _validate_graph(tasks: dict[str, dict[str, Any]]) -> None:
    for task_id, task in tasks.items():
        parent = task["parent_task_id"]
        if parent is not None and parent not in tasks:
            raise JournalError(f"parent task not found: {parent}")
        replacement = task["superseded_by_task_id"]
        if replacement is not None:
            if replacement not in tasks:
                raise JournalError(f"replacement task not found: {replacement}")
            if tasks[replacement]["state"] == "canceled":
                raise JournalError(
                    f"replacement task is not eligible: "
                    f"{replacement}: {tasks[replacement]['state']}"
                )
        for dependency in task["dependencies"]:
            if dependency not in tasks:
                raise JournalError(f"dependency task not found: {dependency}")
            if dependency == task_id:
                raise JournalError(f"task cannot depend on itself: {task_id}")

    def check_edges(field: str, label: str) -> None:
        outgoing: dict[str, list[str]] = {}
        reverse: dict[str, list[str]] = {task_id: [] for task_id in tasks}
        indegree: dict[str, int] = {}
        for task_id, task in tasks.items():
            value = task[field]
            references = [] if value is None else [value]
            if field == "dependencies":
                references = value
            outgoing[task_id] = list(references)
            indegree[task_id] = len(references)
            for reference in references:
                reverse[reference].append(task_id)
        ready = deque(
            sorted(task_id for task_id, count in indegree.items() if count == 0)
        )
        visited = 0
        while ready:
            task_id = ready.popleft()
            visited += 1
            for dependent in reverse[task_id]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
        if visited != len(tasks):
            unresolved = min(
                task_id for task_id, count in indegree.items() if count > 0
            )
            raise JournalError(f"{label} cycle contains {unresolved}")

    check_edges("dependencies", "dependency")
    check_edges("parent_task_id", "parent")
    check_edges("superseded_by_task_id", "supersession")


def _copy_revision(task: dict[str, Any]) -> dict[str, Any]:
    revised = deepcopy(task)
    revised["revision"] += 1
    return revised


def _event_payload(operation: str, effect: list[Any]) -> str:
    return _canonical_json({"effect": effect, "operation": operation})


def apply_effects(
    scope: JournalScope,
    message_id: str,
    document: dict[str, Any],
    *,
    claim_id: str,
) -> dict[str, Any]:
    if not isinstance(message_id, str) or not message_id.startswith("msg_"):
        raise JournalError(f"invalid message ID: {message_id}")
    if not CLAIM_ID_PATTERN.fullmatch(claim_id):
        raise JournalError(f"invalid claim ID: {claim_id}")
    _validate_document_shape(document)
    canonical = _canonical_json(document)
    effects_hash = hashlib.sha256(canonical.encode()).hexdigest()
    effects = document["effects"]

    connection = _connect(scope)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT claim_id, effects_sha256, result_json
            FROM message_applications
            WHERE message_id = ?
            """,
            (message_id,),
        ).fetchone()
        if existing:
            if existing["effects_sha256"] != effects_hash:
                raise JournalError(
                    "message already has a different effects application"
                )
            if existing["claim_id"] != claim_id:
                raise JournalError(
                    "application replay claim does not match the original claim"
                )
            result = json.loads(existing["result_json"])
            result["replayed"] = True
            connection.commit()
            return result

        message = connection.execute(
            """
            SELECT
              message.message_id,
              (
                SELECT event_type
                FROM events
                WHERE message_id = message.message_id
                  AND event_type IN (
                    'message.received',
                    'message.processing',
                    'message.applied',
                    'message.needs_input',
                    'message.failed',
                    'message.superseded'
                  )
                ORDER BY sequence DESC
                LIMIT 1
              ) AS state_event_type
            FROM messages AS message
            WHERE message.message_id = ?
            """,
            (message_id,),
        ).fetchone()
        if not message:
            raise JournalError(f"message not found: {message_id}")
        message_claim = connection.execute(
            """
            SELECT claim.*
            FROM claims AS claim
            LEFT JOIN claim_releases AS release
              ON release.claim_id = claim.claim_id
            WHERE claim.claim_id = ?
              AND claim.resource_kind = 'message'
              AND claim.resource_id = ?
              AND release.claim_id IS NULL
            """,
            (claim_id, message_id),
        ).fetchone()
        if not message_claim:
            raise JournalError(f"message claim is not active: {claim_id}")
        effective_now = _now_us()
        if message_claim["expires_at_us"] <= effective_now:
            raise JournalError(f"message claim has expired: {claim_id}")
        message_state = message["state_event_type"].removeprefix("message.")
        if message_state == "applied":
            raise JournalError(
                f"message has an applied event without an application: {message_id}"
            )
        if message_state != "processing":
            raise JournalError(
                f"message is not applicable: {message_id}: {message_state}"
            )

        tasks = _load_current_tasks(connection)
        initial_revisions = {
            task_id: task["revision"] for task_id, task in tasks.items()
        }
        expect = document["expect"]
        for task_id, revision in expect.items():
            actual = initial_revisions.get(task_id)
            if actual is None:
                raise JournalError(f"task not found: {task_id}")
            if actual != revision:
                raise JournalError(
                    f"task revision changed: {task_id}: "
                    f"expected {revision}, found {actual}"
                )

        aliases: dict[str, str] = {}
        create_indexes: dict[str, int] = {}
        for index, effect in enumerate(effects):
            if effect[0] != "create":
                continue
            if len(effect) != 3:
                raise JournalError(f"create effect {index} must have 3 items")
            alias = effect[1]
            if not isinstance(alias, str) or not ALIAS_PATTERN.fullmatch(alias):
                raise JournalError(f"create effect {index} has invalid alias")
            if alias in aliases:
                raise JournalError(f"duplicate local task alias: {alias}")
            cursor = connection.execute(
                "INSERT INTO task_numbers DEFAULT VALUES"
            )
            task_number = cursor.lastrowid
            aliases[alias] = f"TASK-{task_number}"
            create_indexes[alias] = index

        plans: list[dict[str, Any]] = []
        touched: set[str] = set()
        update_targets: set[str] = set()
        transition_targets: set[str] = set()
        edge_operations: set[tuple[str, str]] = set()
        task_claims_to_release: dict[str, tuple[dict[str, Any], str]] = {}

        def require_expected(task_id: str) -> None:
            if task_id in initial_revisions and task_id not in expect:
                raise JournalError(
                    f"document.expect is missing referenced task: {task_id}"
                )

        def existing_at(reference: str, index: int) -> str:
            canonical_id = _resolve(reference, aliases)
            if reference.startswith("$") and create_indexes[reference] >= index:
                raise JournalError(
                    f"local alias must be created before effect {index}: {reference}"
                )
            if canonical_id not in tasks:
                raise JournalError(f"task not found: {canonical_id}")
            require_expected(canonical_id)
            return canonical_id

        def resolved_before(reference: str, index: int) -> str:
            if reference.startswith("$") and create_indexes[reference] >= index:
                raise JournalError(
                    f"local alias must be created before effect {index}: {reference}"
                )
            return _resolve(reference, aliases)

        for index, effect in enumerate(effects):
            operation = effect[0]
            if operation == "create":
                alias = effect[1]
                task_id = aliases[alias]
                spec = effect[2]
                if not isinstance(spec, dict):
                    raise JournalError(f"create effect {index} spec must be an object")
                _exact_keys(
                    spec,
                    allowed={"title", "objective", "priority", "parent", "requires"},
                    required={"title"},
                    path=f"effects[{index}].spec",
                )
                title = _text(
                    spec["title"],
                    path=f"effects[{index}].title",
                    minimum=1,
                    maximum=200,
                )
                objective = spec.get("objective")
                if objective is not None:
                    objective = _text(
                        objective,
                        path=f"effects[{index}].objective",
                        maximum=2000,
                    )
                priority = _integer(
                    spec.get("priority", 0),
                    path=f"effects[{index}].priority",
                    minimum=-1000000,
                    maximum=1000000,
                )
                parent_reference = spec.get("parent")
                parent = None
                if parent_reference is not None:
                    parent = resolved_before(
                        _task_reference(
                            parent_reference,
                            path=f"effects[{index}].parent",
                        ),
                        index,
                    )
                    require_expected(parent)
                requires = spec.get("requires", [])
                if not isinstance(requires, list) or len(requires) > 64:
                    raise JournalError(
                        f"effects[{index}].requires must be an array of at most 64 tasks"
                    )
                dependencies = [
                    resolved_before(
                        _task_reference(
                            reference,
                            path=f"effects[{index}].requires",
                        ),
                        index,
                    )
                    for reference in requires
                ]
                if len(dependencies) != len(set(dependencies)):
                    raise JournalError(f"create effect {index} has duplicate dependencies")
                for dependency in dependencies:
                    require_expected(dependency)
                task = {
                    "task_id": task_id,
                    "task_number": int(task_id.removeprefix("TASK-")),
                    "revision": 1,
                    "event_sequence": 0,
                    "state": "queued",
                    "title": title,
                    "objective": objective,
                    "priority": priority,
                    "parent_task_id": parent,
                    "reason": None,
                    "superseded_by_task_id": None,
                    "dependencies": dependencies,
                    "created_at": _utc_now(),
                    "created_by_message_id": message_id,
                    "created_sequence": 0,
                }
                tasks[task_id] = task
                plans.append(
                    {
                        "index": index,
                        "operation": operation,
                        "task": deepcopy(task),
                        "effect": effect,
                    }
                )
                touched.add(task_id)
                continue

            if operation == "update":
                if len(effect) != 3:
                    raise JournalError(f"update effect {index} must have 3 items")
                reference = _task_reference(effect[1], path=f"effects[{index}].task")
                task_id = existing_at(reference, index)
                if task_id in update_targets:
                    raise JournalError(f"duplicate update effect for {task_id}")
                update_targets.add(task_id)
                current = tasks[task_id]
                if current["state"] in TERMINAL_STATES:
                    raise JournalError(
                        f"terminal task is immutable: {task_id}: {current['state']}"
                    )
                if current.get("claim") is not None:
                    raise JournalError(f"active task cannot be updated: {task_id}")
                patch = effect[2]
                if not isinstance(patch, dict):
                    raise JournalError(f"update effect {index} patch must be an object")
                _exact_keys(
                    patch,
                    allowed={"title", "objective", "priority", "parent"},
                    required=set(),
                    path=f"effects[{index}].patch",
                )
                if not patch:
                    raise JournalError(f"update effect {index} patch must not be empty")
                revised = _copy_revision(current)
                if "title" in patch:
                    revised["title"] = _text(
                        patch["title"],
                        path=f"effects[{index}].title",
                        minimum=1,
                        maximum=200,
                    )
                if "objective" in patch:
                    objective = patch["objective"]
                    revised["objective"] = (
                        None
                        if objective is None
                        else _text(
                            objective,
                            path=f"effects[{index}].objective",
                            maximum=2000,
                        )
                    )
                if "priority" in patch:
                    revised["priority"] = _integer(
                        patch["priority"],
                        path=f"effects[{index}].priority",
                        minimum=-1000000,
                        maximum=1000000,
                    )
                if "parent" in patch:
                    parent_reference = patch["parent"]
                    revised["parent_task_id"] = (
                        None
                        if parent_reference is None
                        else resolved_before(
                            _task_reference(
                                parent_reference,
                                path=f"effects[{index}].parent",
                            ),
                            index,
                        )
                    )
                    if revised["parent_task_id"]:
                        require_expected(revised["parent_task_id"])
                tasks[task_id] = revised
                plans.append(
                    {
                        "index": index,
                        "operation": operation,
                        "task": deepcopy(revised),
                        "effect": effect,
                    }
                )
                touched.add(task_id)
                continue

            if operation == "transition":
                if len(effect) not in {3, 4}:
                    raise JournalError(f"transition effect {index} must have 3 or 4 items")
                reference = _task_reference(effect[1], path=f"effects[{index}].task")
                task_id = existing_at(reference, index)
                if task_id in transition_targets:
                    raise JournalError(f"duplicate transition effect for {task_id}")
                transition_targets.add(task_id)
                destination = effect[2]
                if destination not in TASK_STATES:
                    raise JournalError(
                        f"transition effect {index} has invalid state: {destination!r}"
                    )
                metadata = effect[3] if len(effect) == 4 else {}
                if not isinstance(metadata, dict):
                    raise JournalError(
                        f"transition effect {index} metadata must be an object"
                    )
                _exact_keys(
                    metadata,
                    allowed={"reason", "by", "claim"},
                    required=set(),
                    path=f"effects[{index}].metadata",
                )
                current = tasks[task_id]
                current_effective = _effective_states(tasks)[task_id]
                if destination == current["state"]:
                    raise JournalError(
                        f"task transition is a no-op: {task_id}: {destination}"
                    )
                if destination == "active":
                    raise JournalError(
                        f"active state requires a queue claim: {task_id}"
                    )
                if destination == "done":
                    transition_claim_id = metadata.get("claim")
                    if (
                        not isinstance(transition_claim_id, str)
                        or current.get("claim") is None
                        or current["claim"]["claim_id"] != transition_claim_id
                        or current["claim"]["basis_revision"] != current["revision"]
                    ):
                        raise JournalError(
                            f"done transition requires the current task claim: {task_id}"
                        )
                elif "claim" in metadata:
                    raise JournalError(
                        f"transition effect {index} allows claim only for done"
                    )
                if destination not in TRANSITIONS[current_effective]:
                    raise JournalError(
                        f"invalid task transition: {task_id}: "
                        f"{current_effective} -> {destination}"
                    )
                reason = metadata.get("reason")
                if destination in {"blocked", "canceled", "superseded"}:
                    reason = _text(
                        reason,
                        path=f"effects[{index}].reason",
                        minimum=1,
                        maximum=1000,
                    )
                elif reason is not None:
                    reason = _text(
                        reason,
                        path=f"effects[{index}].reason",
                        maximum=1000,
                    )
                replacement = None
                if destination == "superseded":
                    if "by" not in metadata:
                        raise JournalError(
                            f"transition effect {index} requires metadata.by"
                        )
                    replacement = resolved_before(
                        _task_reference(
                            metadata["by"],
                            path=f"effects[{index}].by",
                        ),
                        index,
                    )
                    if replacement == task_id:
                        raise JournalError(f"task cannot supersede itself: {task_id}")
                    if replacement not in tasks:
                        raise JournalError(f"replacement task not found: {replacement}")
                    require_expected(replacement)
                elif "by" in metadata:
                    raise JournalError(
                        f"transition effect {index} allows by only for superseded"
                    )
                revised = _copy_revision(current)
                revised["state"] = destination
                revised["reason"] = reason
                revised["superseded_by_task_id"] = replacement
                tasks[task_id] = revised
                if current.get("claim") is not None:
                    disposition = "completed" if destination == "done" else "revoked"
                    task_claims_to_release[current["claim"]["claim_id"]] = (
                        current["claim"],
                        disposition,
                    )
                    revised["claim"] = None
                plans.append(
                    {
                        "index": index,
                        "operation": operation,
                        "task": deepcopy(revised),
                        "effect": effect,
                    }
                )
                touched.add(task_id)
                continue

            if len(effect) != 3:
                raise JournalError(f"{operation} effect {index} must have 3 items")
            task_reference = _task_reference(
                effect[1],
                path=f"effects[{index}].task",
            )
            dependency_reference = _task_reference(
                effect[2],
                path=f"effects[{index}].dependency",
            )
            task_id = existing_at(task_reference, index)
            dependency_id = existing_at(dependency_reference, index)
            edge_key = (task_id, dependency_id)
            if edge_key in edge_operations:
                raise JournalError(
                    f"duplicate dependency effect: {task_id} -> {dependency_id}"
                )
            edge_operations.add(edge_key)
            current = tasks[task_id]
            if current.get("claim") is not None or current["state"] in TERMINAL_STATES:
                raise JournalError(
                    f"dependencies are immutable in active or terminal task: {task_id}"
                )
            revised = _copy_revision(current)
            dependencies = set(revised["dependencies"])
            if operation == "require":
                if dependency_id in dependencies:
                    raise JournalError(
                        f"dependency already exists: {task_id} -> {dependency_id}"
                    )
                dependencies.add(dependency_id)
            else:
                if dependency_id not in dependencies:
                    raise JournalError(
                        f"dependency does not exist: {task_id} -> {dependency_id}"
                    )
                dependencies.remove(dependency_id)
            revised["dependencies"] = sorted(dependencies)
            tasks[task_id] = revised
            plans.append(
                {
                    "index": index,
                    "operation": operation,
                    "task": deepcopy(revised),
                    "effect": effect,
                }
            )
            touched.add(task_id)

        extra_expectations = sorted(set(expect) - set(initial_revisions))
        if extra_expectations:
            raise JournalError(
                f"document.expect contains unknown tasks: {', '.join(extra_expectations)}"
            )
        _validate_graph(tasks)

        for plan in plans:
            task = plan["task"]
            event_type = {
                "create": "task.created",
                "update": "task.revised",
                "transition": "task.state_changed",
                "require": "task.dependency_added",
                "unrequire": "task.dependency_removed",
            }[plan["operation"]]
            cursor = connection.execute(
                """
                INSERT INTO events(
                  event_id,
                  occurred_at,
                  event_type,
                  message_id,
                  task_id,
                  payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _identifier("evt"),
                    _utc_now(),
                    event_type,
                    message_id,
                    task["task_id"],
                    _event_payload(plan["operation"], plan["effect"]),
                ),
            )
            sequence = cursor.lastrowid
            if plan["operation"] == "create":
                task["created_sequence"] = sequence
                tasks[task["task_id"]]["created_sequence"] = sequence
                connection.execute(
                    """
                    INSERT INTO tasks(
                      task_id,
                      task_number,
                      created_at,
                      created_by_message_id,
                      created_sequence
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        task["task_id"],
                        task["task_number"],
                        task["created_at"],
                        message_id,
                        sequence,
                    ),
                )
            if tasks[task["task_id"]]["revision"] == task["revision"]:
                tasks[task["task_id"]]["event_sequence"] = sequence
            connection.execute(
                """
                INSERT INTO task_effects(
                  message_id,
                  effect_index,
                  event_sequence,
                  operation,
                  task_id,
                  payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    plan["index"],
                    sequence,
                    plan["operation"],
                    task["task_id"],
                    _event_payload(plan["operation"], plan["effect"]),
                ),
            )
            connection.execute(
                """
                INSERT INTO task_revisions(
                  task_id,
                  revision,
                  event_sequence,
                  state,
                  title,
                  objective,
                  priority,
                  parent_task_id,
                  dependencies_json,
                  reason,
                  superseded_by_task_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task["task_id"],
                    task["revision"],
                    sequence,
                    task["state"],
                    task["title"],
                    task["objective"],
                    task["priority"],
                    task["parent_task_id"],
                    _canonical_json(task["dependencies"]),
                    task["reason"],
                    task["superseded_by_task_id"],
                ),
            )

        release_now = _now_us()
        for task_claim, disposition in task_claims_to_release.values():
            _append_claim_release(
                connection,
                task_claim,
                disposition=disposition,
                now_us=release_now,
            )
        _append_claim_release(
            connection,
            message_claim,
            disposition="applied",
            now_us=release_now,
        )

        applied_at = _utc_now()
        applied_cursor = connection.execute(
            """
            INSERT INTO events(
              event_id,
              occurred_at,
              event_type,
              message_id,
              payload_json
            ) VALUES (?, ?, 'message.applied', ?, ?)
            """,
            (
                _identifier("evt"),
                applied_at,
                message_id,
                _canonical_json({"effects_sha256": effects_hash}),
            ),
        )
        final_effective = _effective_states(tasks)
        result = {
            "status": "applied",
            "message_id": message_id,
            "effects_sha256": effects_hash,
            "aliases": aliases,
            "tasks": [
                _task_output(
                    tasks[task_id],
                    final_effective[task_id],
                    final_effective,
                )
                for task_id in sorted(
                    touched,
                    key=lambda value: int(value.removeprefix("TASK-")),
                )
            ],
            "applied_sequence": applied_cursor.lastrowid,
            "replayed": False,
        }
        result_json = _canonical_json(result)
        connection.execute(
            """
            INSERT INTO message_applications(
              message_id,
              claim_id,
              effects_sha256,
              applied_at,
              applied_event_sequence,
              effect_count,
              document_json,
              result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                claim_id,
                effects_hash,
                applied_at,
                applied_cursor.lastrowid,
                len(effects),
                canonical,
                result_json,
            ),
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def audit_queue(connection: sqlite3.Connection) -> dict[str, int]:
    effect_context: dict[int, tuple[list[Any], dict[str, str]]] = {}
    orphan_effect = connection.execute(
        """
        SELECT effect.message_id, effect.effect_index
        FROM task_effects AS effect
        LEFT JOIN message_applications AS application
          ON application.message_id = effect.message_id
        WHERE application.message_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if orphan_effect:
        raise JournalError(
            f"task effect has no sealed application: "
            f"{orphan_effect['message_id']}:{orphan_effect['effect_index']}"
        )
    applications = connection.execute(
        """
        SELECT
          message_id,
          claim_id,
          effects_sha256,
          applied_event_sequence,
          effect_count,
          document_json,
          result_json
        FROM message_applications
        ORDER BY applied_event_sequence
        """
    ).fetchall()
    for application in applications:
        try:
            document = json.loads(
                application["document_json"],
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_constant,
            )
            _validate_document_shape(document)
        except (json.JSONDecodeError, JournalError, RecursionError) as error:
            raise JournalError(
                f"invalid stored effects document for {application['message_id']}: "
                f"{error}"
            ) from error
        canonical = _canonical_json(document)
        if canonical != application["document_json"]:
            raise JournalError(
                f"stored effects document is not canonical: "
                f"{application['message_id']}"
            )
        actual_hash = hashlib.sha256(canonical.encode()).hexdigest()
        if actual_hash != application["effects_sha256"]:
            raise JournalError(
                f"effects document hash mismatch: {application['message_id']}"
            )
        event = connection.execute(
            """
            SELECT event_type, message_id, payload_json
            FROM events
            WHERE sequence = ?
            """,
            (application["applied_event_sequence"],),
        ).fetchone()
        if (
            not event
            or event["event_type"] != "message.applied"
            or event["message_id"] != application["message_id"]
        ):
            raise JournalError(
                f"application event mismatch: {application['message_id']}"
            )
        event_payload = json.loads(event["payload_json"])
        if (
            not isinstance(event_payload, dict)
            or event_payload.get("effects_sha256") != actual_hash
        ):
            raise JournalError(
                f"application event hash mismatch: {application['message_id']}"
            )
        effects = connection.execute(
            """
            SELECT effect_index, operation, task_id, event_sequence, payload_json
            FROM task_effects
            WHERE message_id = ?
            ORDER BY effect_index
            """,
            (application["message_id"],),
        ).fetchall()
        if len(effects) != application["effect_count"]:
            raise JournalError(
                f"application effect count mismatch: {application['message_id']}"
            )
        if [row["effect_index"] for row in effects] != list(range(len(effects))):
            raise JournalError(
                f"application effects are not contiguous: "
                f"{application['message_id']}"
            )
        if len(document["effects"]) != len(effects):
            raise JournalError(
                f"application document effect count mismatch: "
                f"{application['message_id']}"
            )
        for row, document_effect in zip(effects, document["effects"], strict=True):
            expected_payload = _event_payload(row["operation"], document_effect)
            if row["payload_json"] != expected_payload:
                raise JournalError(
                    f"application effect payload mismatch: "
                    f"{application['message_id']}:{row['effect_index']}"
                )
        try:
            result = json.loads(application["result_json"])
        except json.JSONDecodeError as error:
            raise JournalError(
                f"application result is invalid: {application['message_id']}"
            ) from error
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("aliases"), dict)
            or result.get("message_id") != application["message_id"]
            or result.get("effects_sha256") != actual_hash
            or result.get("applied_sequence")
            != application["applied_event_sequence"]
        ):
            raise JournalError(
                f"application result mismatch: {application['message_id']}"
            )
        aliases = result["aliases"]
        if any(
            not isinstance(alias, str)
            or not isinstance(task_id, str)
            or not TASK_ID_PATTERN.fullmatch(task_id)
            for alias, task_id in aliases.items()
        ):
            raise JournalError(
                f"application aliases are invalid: {application['message_id']}"
            )
        for row, document_effect in zip(effects, document["effects"], strict=True):
            effect_context[row["event_sequence"]] = (document_effect, aliases)
        applied_claim = connection.execute(
            """
            SELECT 1
            FROM claims AS claim
            JOIN claim_releases AS release
              ON release.claim_id = claim.claim_id
            WHERE claim.resource_kind = 'message'
              AND claim.resource_id = ?
              AND claim.claim_id = ?
              AND release.disposition = 'applied'
            """,
            (application["message_id"], application["claim_id"]),
        ).fetchone()
        if not applied_claim:
            raise JournalError(
                f"application claim mismatch: {application['message_id']}"
            )

    task_rows = connection.execute(
        """
        SELECT
          task_id,
          task_number,
          created_sequence,
          created_by_message_id
        FROM tasks
        ORDER BY task_number
        """
    ).fetchall()
    task_ids = {row["task_id"] for row in task_rows}
    allocated_numbers = {
        row[0] for row in connection.execute("SELECT task_number FROM task_numbers")
    }
    if allocated_numbers != {row["task_number"] for row in task_rows}:
        raise JournalError("task number allocation does not match tasks")
    for row in task_rows:
        if row["task_number"] < 1 or row["task_id"] != f"TASK-{row['task_number']}":
            raise JournalError(f"invalid task identity: {row['task_id']}")
        revisions = connection.execute(
            """
            SELECT *
            FROM task_revisions
            WHERE task_id = ?
            ORDER BY revision
            """,
            (row["task_id"],),
        ).fetchall()
        if not revisions:
            raise JournalError(f"task has no revisions: {row['task_id']}")
        if [revision["revision"] for revision in revisions] != list(
            range(1, len(revisions) + 1)
        ):
            raise JournalError(
                f"task revisions are not contiguous: {row['task_id']}"
            )
        if revisions[0]["event_sequence"] != row["created_sequence"]:
            raise JournalError(
                f"task creation sequence mismatch: {row['task_id']}"
            )
        previous: sqlite3.Row | None = None
        for revision in revisions:
            dependencies = json.loads(revision["dependencies_json"])
            if (
                not isinstance(dependencies, list)
                or any(
                    not isinstance(dependency, str)
                    or not TASK_ID_PATTERN.fullmatch(dependency)
                    for dependency in dependencies
                )
                or dependencies != sorted(set(dependencies))
                or row["task_id"] in dependencies
                or any(dependency not in task_ids for dependency in dependencies)
            ):
                raise JournalError(
                    f"invalid task dependencies: "
                    f"{row['task_id']}:r{revision['revision']}"
                )
            effect = connection.execute(
                """
                SELECT message_id, operation, task_id, payload_json
                FROM task_effects
                WHERE event_sequence = ?
                """,
                (revision["event_sequence"],),
            ).fetchone()
            if not effect:
                raise JournalError(
                    f"task revision has no effect: "
                    f"{row['task_id']}:r{revision['revision']}"
                )
            if effect["task_id"] != row["task_id"]:
                raise JournalError(
                    f"task effect target mismatch: "
                    f"{row['task_id']}:r{revision['revision']}"
                )
            payload = json.loads(effect["payload_json"])
            document_effect = payload.get("effect")
            if not isinstance(document_effect, list) or len(document_effect) < 1:
                raise JournalError(
                    f"task effect payload is invalid: "
                    f"{row['task_id']}:r{revision['revision']}"
                )
            operation = effect["operation"]
            context = effect_context.get(revision["event_sequence"])
            if context is None or context[0] != document_effect:
                raise JournalError(
                    f"task effect has no application context: "
                    f"{row['task_id']}:r{revision['revision']}"
                )
            aliases = context[1]

            def resolve_reference(reference: Any) -> Any:
                return aliases.get(reference, reference) if isinstance(reference, str) else reference

            if previous is None:
                if (
                    operation != "create"
                    or len(document_effect) != 3
                    or not isinstance(document_effect[2], dict)
                    or resolve_reference(document_effect[1]) != row["task_id"]
                    or effect["message_id"] != row["created_by_message_id"]
                    or revision["state"] != "queued"
                ):
                    raise JournalError(
                        f"task first revision is invalid: {row['task_id']}"
                    )
                spec = document_effect[2]
                expected_dependencies = sorted(
                    resolve_reference(reference)
                    for reference in spec.get("requires", [])
                )
                expected = {
                    "title": spec.get("title"),
                    "objective": spec.get("objective"),
                    "priority": spec.get("priority", 0),
                    "parent_task_id": resolve_reference(spec.get("parent")),
                    "dependencies_json": _canonical_json(expected_dependencies),
                    "reason": None,
                    "superseded_by_task_id": None,
                }
                if any(revision[field] != value for field, value in expected.items()):
                    raise JournalError(
                        f"task creation revision mismatch: {row['task_id']}"
                    )
            else:
                identity_fields = (
                    "title",
                    "objective",
                    "priority",
                    "parent_task_id",
                )
                stable_fields = (
                    *identity_fields,
                    "reason",
                    "superseded_by_task_id",
                )
                if operation == "update":
                    if (
                        len(document_effect) != 3
                        or not isinstance(document_effect[2], dict)
                        or resolve_reference(document_effect[1]) != row["task_id"]
                        or revision["state"] != previous["state"]
                        or revision["dependencies_json"]
                        != previous["dependencies_json"]
                    ):
                        raise JournalError(
                            f"update changed lifecycle data: "
                            f"{row['task_id']}:r{revision['revision']}"
                        )
                    patch = document_effect[2]
                    expected = {
                        field: previous[field]
                        for field in (
                            "title",
                            "objective",
                            "priority",
                            "parent_task_id",
                            "reason",
                            "superseded_by_task_id",
                        )
                    }
                    for field in ("title", "objective", "priority"):
                        if field in patch:
                            expected[field] = patch[field]
                    if "parent" in patch:
                        expected["parent_task_id"] = resolve_reference(patch["parent"])
                    if any(revision[field] != value for field, value in expected.items()):
                        raise JournalError(
                            f"task update revision mismatch: "
                            f"{row['task_id']}:r{revision['revision']}"
                        )
                elif operation == "transition":
                    if (
                        any(revision[field] != previous[field] for field in identity_fields)
                        or revision["dependencies_json"]
                        != previous["dependencies_json"]
                        or len(document_effect) < 3
                        or resolve_reference(document_effect[1]) != row["task_id"]
                        or document_effect[2] != revision["state"]
                        or previous["state"] in TERMINAL_STATES
                    ):
                        raise JournalError(
                            f"transition revision mismatch: "
                            f"{row['task_id']}:r{revision['revision']}"
                        )
                    destination = revision["state"]
                    metadata = (
                        document_effect[3]
                        if len(document_effect) == 4
                        and isinstance(document_effect[3], dict)
                        else {}
                    )
                    expected_reason = metadata.get("reason")
                    expected_replacement = (
                        resolve_reference(metadata.get("by"))
                        if destination == "superseded"
                        else None
                    )
                    if (
                        revision["reason"] != expected_reason
                        or revision["superseded_by_task_id"] != expected_replacement
                    ):
                        raise JournalError(
                            f"transition metadata mismatch: "
                            f"{row['task_id']}:r{revision['revision']}"
                        )
                    if destination == "active":
                        raise JournalError(
                            f"active revision has no queue claim: "
                            f"{row['task_id']}:r{revision['revision']}"
                        )
                    if destination == "done":
                        claim_id = metadata.get("claim")
                        completed_claim = connection.execute(
                            """
                            SELECT 1
                            FROM claims AS claim
                            JOIN claim_releases AS release
                              ON release.claim_id = claim.claim_id
                            WHERE claim.claim_id = ?
                              AND claim.resource_kind = 'task'
                              AND claim.resource_id = ?
                              AND claim.basis_revision = ?
                              AND claim.fence < ?
                              AND release.event_sequence > ?
                              AND release.disposition = 'completed'
                            """,
                            (
                                claim_id,
                                row["task_id"],
                                previous["revision"],
                                revision["event_sequence"],
                                revision["event_sequence"],
                            ),
                        ).fetchone()
                        if not completed_claim:
                            raise JournalError(
                                f"done revision has no completed claim: "
                                f"{row['task_id']}:r{revision['revision']}"
                            )
                    elif destination not in TRANSITIONS[previous["state"]]:
                        raise JournalError(
                            f"invalid stored transition: {row['task_id']}: "
                            f"{previous['state']} -> {destination}"
                        )
                elif operation in {"require", "unrequire"}:
                    if (
                        len(document_effect) != 3
                        or resolve_reference(document_effect[1]) != row["task_id"]
                        or any(
                            revision[field] != previous[field]
                            for field in stable_fields
                        )
                        or revision["state"] != previous["state"]
                    ):
                        raise JournalError(
                            f"dependency effect changed task fields: "
                            f"{row['task_id']}:r{revision['revision']}"
                        )
                    before = set(json.loads(previous["dependencies_json"]))
                    after = set(dependencies)
                    dependency = resolve_reference(document_effect[2])
                    expected = (
                        before | {dependency}
                        if operation == "require"
                        else before - {dependency}
                    )
                    if after != expected:
                        raise JournalError(
                            f"dependency revision mismatch: "
                            f"{row['task_id']}:r{revision['revision']}"
                        )
                else:
                    raise JournalError(
                        f"invalid task operation after creation: {operation}"
                    )
            previous = revision

    tasks = _load_current_tasks(connection)
    _validate_graph(tasks)
    _effective_states(tasks)
    claim_count = connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    duplicate_claim = connection.execute(
        """
        SELECT claim.resource_kind, claim.resource_id
        FROM claims AS claim
        LEFT JOIN claim_releases AS release
          ON release.claim_id = claim.claim_id
        WHERE release.claim_id IS NULL
        GROUP BY claim.resource_kind, claim.resource_id
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if duplicate_claim:
        raise JournalError(
            f"resource has multiple active claims: "
            f"{duplicate_claim['resource_kind']}:{duplicate_claim['resource_id']}"
        )
    for claim in connection.execute("SELECT * FROM claims"):
        if claim["resource_kind"] == "message":
            exists = connection.execute(
                "SELECT 1 FROM messages WHERE message_id = ?",
                (claim["resource_id"],),
            ).fetchone()
        else:
            exists = connection.execute(
                "SELECT revision FROM current_tasks WHERE task_id = ?",
                (claim["resource_id"],),
            ).fetchone()
            if exists and claim["basis_revision"] > exists["revision"]:
                raise JournalError(
                    f"task claim basis is in the future: {claim['claim_id']}"
                )
        if not exists:
            raise JournalError(
                f"claim resource does not exist: {claim['claim_id']}"
            )
    return {
        "applications": len(applications),
        "tasks": len(task_rows),
        "claims": claim_count,
    }
