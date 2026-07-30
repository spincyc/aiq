from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import sqlite3
import time
from typing import Any, Iterator

from aiq.config import (
    READER_LEASE_SECONDS_DEFAULT,
    READER_LEASE_SECONDS_MAXIMUM,
    READER_LEASE_SECONDS_MINIMUM,
    _default_reader,
    _reader_locator,
)
from aiq.journal import (
    MESSAGE_LIFECYCLE_EVENT_SQL,
    JournalError,
    JournalScope,
    _connect,
    _identifier,
    _ingest_connected,
    _read_project_label,
    _utc_now,
    default_project_label,
)


EFFECT_DOCUMENT_MAX_BYTES = 65536
EFFECT_COUNT_MAX = 64
# The reportable message states. Deliberately omits 'superseded' even
# though MESSAGE_LIFECYCLE_EVENTS recognizes 'message.superseded': no
# writer emits that event today, so status surfaces report only the five
# reachable states while the lifecycle queries stay forward-compatible.
MESSAGE_STATES = (
    "received",
    "processing",
    "applied",
    "needs_input",
    "failed",
)
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
# One journal is one scope is one reader role, so the lease table holds
# at most this single row.
READER_LEASE_SCOPE = 0


def _now_us() -> int:
    return time.time_ns() // 1000


def _z_timestamp(value: str) -> str:
    """Render one UTC ISO-8601 timestamp with a Z suffix."""

    return value.replace("+00:00", "Z")


def _us_timestamp(value: int) -> str:
    """Render one microsecond epoch timestamp as Z-suffixed ISO-8601."""

    return _z_timestamp(
        datetime.fromtimestamp(value / 1_000_000, tz=timezone.utc)
        .isoformat(timespec="microseconds")
    )


def _claim_summary(claim: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    """Render one claim's public summary with a Z-suffixed expiry."""

    return {
        "claim_id": claim["claim_id"],
        "owner_id": claim["owner_id"],
        "expires_at": _us_timestamp(claim["expires_at_us"]),
    }


def _queue_order_key(task: dict[str, Any]) -> tuple[int, int, int]:
    """Order queue candidates by priority, then stable creation order."""

    return (-task["priority"], task["created_sequence"], task["task_number"])


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
    if type(document["v"]) is not int or document["v"] != 1:
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


def _read_reader_lease_row(
    connection: sqlite3.Connection,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM reader_leases WHERE lease_scope = ?",
        (READER_LEASE_SCOPE,),
    ).fetchone()


def _reader_lease_status(row: sqlite3.Row | None, now_us: int) -> str:
    if row is None:
        return "absent"
    if row["released_at_us"] is not None:
        return "released"
    if row["expires_at_us"] <= now_us:
        return "expired"
    return "held"


def _reader_holder_locator(reader_id: str) -> tuple[str | None, int | None]:
    """Return the host and session to record for one reader identity.

    Only an identity this process derived for itself describes a process
    that can later be probed for liveness. An explicitly configured
    reader may name any session on any host -- including a deliberately
    shared fan-out identity -- so it records no locator, which disables
    the dead-holder fast path instead of guessing about a stranger.
    """

    if reader_id != _default_reader():
        return (None, None)
    locator = _reader_locator()
    return (None, None) if locator is None else locator


def _reader_holder_is_dead(row: sqlite3.Row) -> bool:
    """True only when the recorded holder's session is provably gone.

    A locator is recorded only for a derived identity, so a matching host
    proves the session id is comparable here. ``ProcessLookupError`` is
    the only proof of death: a permission error means some live process
    owns that id, and every other answer is treated as alive.
    """

    host = row["holder_host"]
    session = row["holder_sid"]
    if host is None or session is None:
        return False
    locator = _reader_locator()
    if locator is None or locator[0] != host:
        return False
    try:
        os.kill(session, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _reader_holder_is_foreign_live(row: sqlite3.Row) -> bool:
    """True only when the holder is provably some *other* live session.

    This is the inverse burden of :func:`_reader_holder_is_dead`, not its
    negation: that probe answers "may this lease be taken over?" and so
    assumes life whenever death is unproven, while this one answers "is
    another session already accountable for this queue?" and so demands
    positive proof. Every gap in the evidence -- an explicitly configured
    identity that recorded no locator, a locator naming another host we
    cannot probe, a probe that answers neither "alive" nor "gone" -- is
    resolved against standing a completion gate down.

    A recorded session equal to this process's own is deliberately not
    foreign. Host hooks run as children of the session that took the
    lease and inherit its session id, so the locator, not the reader
    identity string, is what tells a session apart from itself: the two
    surfaces may derive different identities from the same session when
    a configuration file or ``AIQ_READER`` reaches only one of them.
    """

    host = row["holder_host"]
    session = row["holder_sid"]
    if host is None or session is None:
        return False
    locator = _reader_locator()
    if locator is None or locator[0] != host or locator[1] == session:
        return False
    try:
        os.kill(session, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A live process owns that id; only its owner may signal it.
        return True
    except OSError:
        return False
    return True


def _reader_holder_is_this_session(row: sqlite3.Row) -> bool:
    """True only when the recorded holder locator names this session.

    The mirror image of :func:`_reader_holder_is_foreign_live` over the
    same evidence, and it demands the same proof: a locator was recorded
    (which happens only for a self-derived identity), it names this host,
    and it names this process's own POSIX session. No liveness probe is
    needed — this process is the running proof.

    The locator, not the reader identity string, is what tells a session
    apart from itself. A host hook runs as a child of the session that
    took the lease and inherits its session id, but it does not inherit
    that shell's environment, so the two surfaces can derive different
    identity strings from one session. Comparing identities would miss
    the match; comparing locators does not.
    """

    host = row["holder_host"]
    session = row["holder_sid"]
    if host is None or session is None:
        return False
    locator = _reader_locator()
    return locator is not None and locator[0] == host and locator[1] == session


def _reader_lease_conflict(
    row: sqlite3.Row | None,
    *,
    reader_id: str,
    now_us: int,
) -> sqlite3.Row | None:
    """Return the live foreign holder standing in ``reader_id``'s way."""

    if row is None or _reader_lease_status(row, now_us) != "held":
        return None
    if row["reader_id"] == reader_id or _reader_holder_is_dead(row):
        return None
    return row


def _raise_reader_held(holder: sqlite3.Row) -> None:
    owner = holder["owner_id"]
    reader = holder["reader_id"]
    expires = _us_timestamp(holder["expires_at_us"])
    raise JournalError(
        f'reader lease is held by owner "{owner}" reader "{reader}" '
        f"until {expires}; ingest and enqueue remain open",
        code="reader_held",
    )


def _require_reader_lease(
    connection: sqlite3.Connection,
    *,
    reader_id: str | None,
    now_us: int,
) -> None:
    """Refuse dispatch unless ``reader_id`` may hold the reader role.

    Called before the queue is even probed, so a non-holder is told the
    truthful thing -- that it is not the reader -- whether or not work
    happens to be waiting. A ``None`` reader identity means the caller
    supplied none and the role is not enforced for this call.
    """

    if reader_id is None:
        return
    holder = _reader_lease_conflict(
        _read_reader_lease_row(connection),
        reader_id=reader_id,
        now_us=now_us,
    )
    if holder is not None:
        _raise_reader_held(holder)


def _renew_reader_lease(
    connection: sqlite3.Connection,
    *,
    reader_id: str | None,
    lease_seconds: int,
    now_us: int,
    owner_id: str | None = None,
) -> bool:
    """Slide an already-held lease forward; never take the role.

    The single conditional UPDATE is the atomic self-check: it matches
    only a live, unreleased lease already naming this reader. An omitted
    owner leaves the recorded one alone, which suits the commands that
    only keep a held lease warm.
    """

    if reader_id is None:
        return False
    cursor = connection.execute(
        """
        UPDATE reader_leases
        SET owner_id = COALESCE(?, owner_id),
            renewed_at_us = ?,
            expires_at_us = ?
        WHERE lease_scope = ?
          AND reader_id = ?
          AND released_at_us IS NULL
          AND expires_at_us > ?
        """,
        (
            owner_id,
            now_us,
            now_us + lease_seconds * 1_000_000,
            READER_LEASE_SCOPE,
            reader_id,
            now_us,
        ),
    )
    return cursor.rowcount == 1


def _hold_reader_lease(
    connection: sqlite3.Connection,
    *,
    owner_id: str,
    reader_id: str | None,
    lease_seconds: int,
    now_us: int,
) -> bool:
    """Take or extend the reader role after a successful consume.

    Returns True when this call took the role -- a first acquisition or a
    takeover of a free, released, expired, or provably dead lease, each
    of which advances ``epoch`` -- and False when it merely renewed a
    lease this reader already held, or when no reader identity applies.
    The conflict re-check runs inside the caller's immediate transaction,
    so exactly one of several racing readers can win.
    """

    if reader_id is None:
        return False
    if _renew_reader_lease(
        connection,
        reader_id=reader_id,
        lease_seconds=lease_seconds,
        now_us=now_us,
        owner_id=owner_id,
    ):
        return False
    row = _read_reader_lease_row(connection)
    holder = _reader_lease_conflict(row, reader_id=reader_id, now_us=now_us)
    if holder is not None:
        _raise_reader_held(holder)
    host, session = _reader_holder_locator(reader_id)
    connection.execute(
        """
        INSERT INTO reader_leases(
          lease_scope,
          lease_id,
          epoch,
          owner_id,
          reader_id,
          holder_host,
          holder_sid,
          acquired_at_us,
          renewed_at_us,
          expires_at_us,
          released_at_us
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(lease_scope) DO UPDATE SET
          lease_id = excluded.lease_id,
          epoch = excluded.epoch,
          owner_id = excluded.owner_id,
          reader_id = excluded.reader_id,
          holder_host = excluded.holder_host,
          holder_sid = excluded.holder_sid,
          acquired_at_us = excluded.acquired_at_us,
          renewed_at_us = excluded.renewed_at_us,
          expires_at_us = excluded.expires_at_us,
          released_at_us = NULL
        """,
        (
            READER_LEASE_SCOPE,
            _identifier("rdl"),
            1 if row is None else row["epoch"] + 1,
            owner_id,
            reader_id,
            host,
            session,
            now_us,
            now_us,
            now_us + lease_seconds * 1_000_000,
        ),
    )
    return True


def _reader_lease_public(
    row: sqlite3.Row | None,
    *,
    reader_id: str | None,
    now_us: int,
) -> dict[str, Any]:
    """Render one reader lease for public output.

    ``self`` is null exactly when the caller supplied no reader identity
    to compare against, and names the recorded holder otherwise, whether
    or not that holder's lease is still live.

    An unexpired lease whose recorded holder is provably gone reads as
    ``stale`` rather than ``held``: the next consumer may take it, so
    reporting it as held would show a queue as owned while it is free.
    The distinction is presentational — claim, release, and takeover
    decide liveness for themselves against the stored row.
    """

    status = _reader_lease_status(row, now_us)
    if status == "held" and row is not None and _reader_holder_is_dead(row):
        status = "stale"
    if row is None:
        return {
            "status": status,
            "held": False,
            "self": None if reader_id is None else False,
            "owner_id": None,
            "reader_id": None,
            "acquired_at": None,
            "expires_at": None,
            "expires_in_seconds": None,
            "epoch": None,
        }
    return {
        "status": status,
        "held": status == "held",
        "self": None if reader_id is None else row["reader_id"] == reader_id,
        "owner_id": row["owner_id"],
        "reader_id": row["reader_id"],
        "acquired_at": _us_timestamp(row["acquired_at_us"]),
        "expires_at": _us_timestamp(row["expires_at_us"]),
        "expires_in_seconds": max(
            0,
            (row["expires_at_us"] - now_us) // 1_000_000,
        ),
        "epoch": row["epoch"],
    }


def _reader_status_summary(
    row: sqlite3.Row | None,
    lease: dict[str, Any],
) -> dict[str, Any]:
    """Project the gate-relevant subset carried by :func:`read_status`.

    ``live`` states directly what a completion gate needs, and nothing
    looser: that the role is held by a demonstrably different session
    that is still running, so this caller is not the one accountable for
    the queue. It is true only for a ``held`` lease -- the rendered
    status already separates a lease abandoned by a crashed session as
    ``stale`` -- whose recorded holder locates a live session other than
    this process's own. Unprovable foreignness, including the common
    case of an explicitly configured identity that records no locator at
    all, reads false so the gate keeps blocking.

    ``released_by_self`` states the complementary thing: that *this*
    session gave the role up on purpose. It is true only for a
    ``released`` lease whose recorded holder locator names this host and
    this process's own POSIX session -- the recorded, deliberate act of
    an ``aiq reader release`` from this session. A release by anyone
    else, and a release under an identity that recorded no locator,
    reads false, because neither is this session declaring anything.

    The two are mutually exclusive by construction: a lease cannot be
    both ``held`` and ``released``.
    """

    summary = {
        key: lease[key]
        for key in (
            "status",
            "held",
            "self",
            "owner_id",
            "reader_id",
            "expires_at",
        )
    }
    summary["live"] = (
        lease["status"] == "held"
        and row is not None
        and _reader_holder_is_foreign_live(row)
    )
    summary["released_by_self"] = (
        lease["status"] == "released"
        and row is not None
        and _reader_holder_is_this_session(row)
    )
    return summary


def read_reader_lease(
    scope: JournalScope,
    *,
    reader_id: str | None = None,
    now_us: int | None = None,
) -> dict[str, Any]:
    """Report one scope's reader lease without creating storage."""

    if not scope.journal_path.exists():
        return _reader_lease_public(None, reader_id=reader_id, now_us=0)
    with _read_snapshot(scope, now_us) as (connection, effective_now):
        return _reader_lease_public(
            _read_reader_lease_row(connection),
            reader_id=reader_id,
            now_us=effective_now,
        )


def acquire_reader_lease(
    scope: JournalScope,
    *,
    owner_id: str,
    reader_id: str,
    lease_seconds: int = READER_LEASE_SECONDS_DEFAULT,
    now_us: int | None = None,
) -> dict[str, Any]:
    """Hold the reader role explicitly, without consuming anything.

    Acquiring while already holding renews the same lease, so a poller
    can keep the role warm; another live holder is refused.
    """

    owner = _text(owner_id, path="owner_id", minimum=1, maximum=200)
    reader = _text(reader_id, path="reader_id", minimum=1, maximum=200)
    lease = _integer(
        lease_seconds,
        path="reader_lease_seconds",
        minimum=READER_LEASE_SECONDS_MINIMUM,
        maximum=READER_LEASE_SECONDS_MAXIMUM,
    )
    effective_now = _now_us() if now_us is None else now_us
    connection = _connect(scope)
    try:
        connection.execute("BEGIN IMMEDIATE")
        acquired = _hold_reader_lease(
            connection,
            owner_id=owner,
            reader_id=reader,
            lease_seconds=lease,
            now_us=effective_now,
        )
        lease_public = _reader_lease_public(
            _read_reader_lease_row(connection),
            reader_id=reader,
            now_us=effective_now,
        )
        connection.commit()
        return {
            "status": "acquired",
            "acquired": acquired,
            "reader": lease_public,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def release_reader_lease(
    scope: JournalScope,
    *,
    reader_id: str,
    now_us: int | None = None,
) -> dict[str, Any]:
    """Give up the reader role, leaving every held claim untouched.

    Holding nothing, an already released lease, and an expired lease all
    replay successfully; only another live holder is refused. Losing the
    role never revokes a claim, which recovers on its own schedule.
    """

    reader = _text(reader_id, path="reader_id", minimum=1, maximum=200)
    effective_now = _now_us() if now_us is None else now_us
    connection = _connect(scope)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _read_reader_lease_row(connection)
        holder = _reader_lease_conflict(
            row,
            reader_id=reader,
            now_us=effective_now,
        )
        if holder is not None:
            _raise_reader_held(holder)
        held = (
            row is not None
            and row["reader_id"] == reader
            and _reader_lease_status(row, effective_now) == "held"
        )
        if held:
            connection.execute(
                """
                UPDATE reader_leases
                SET released_at_us = ?
                WHERE lease_scope = ?
                """,
                (effective_now, READER_LEASE_SCOPE),
            )
            row = _read_reader_lease_row(connection)
        lease_public = _reader_lease_public(
            row,
            reader_id=reader,
            now_us=effective_now,
        )
        connection.commit()
        return {
            "status": "released",
            "replayed": not held,
            "reader": lease_public,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


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


def _claim_message_connected(
    connection: sqlite3.Connection,
    message_id: str,
    *,
    owner_id: str,
    lease_seconds: int,
    now_us: int,
) -> dict[str, Any]:
    """Lease one message inside an already-open transaction."""

    claim = _claim_resource(
        connection,
        resource_kind="message",
        resource_id=message_id,
        owner_id=owner_id,
        lease_seconds=lease_seconds,
        now_us=now_us,
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
            message_id,
            _canonical_json({"claim_id": claim["claim_id"]}),
        ),
    )
    return claim


def claim_message(
    scope: JournalScope,
    *,
    owner_id: str,
    lease_seconds: int = 900,
    message_id: str | None = None,
    reader_id: str | None = None,
    reader_lease_seconds: int = READER_LEASE_SECONDS_DEFAULT,
    now_us: int | None = None,
) -> dict[str, Any] | None:
    owner = _text(owner_id, path="owner_id", minimum=1, maximum=200)
    lease = _integer(
        lease_seconds,
        path="lease_seconds",
        minimum=1,
        maximum=86400,
    )
    reader_lease = _integer(
        reader_lease_seconds,
        path="reader_lease_seconds",
        minimum=READER_LEASE_SECONDS_MINIMUM,
        maximum=READER_LEASE_SECONDS_MAXIMUM,
    )
    effective_now = _now_us() if now_us is None else now_us
    connection = _connect(scope)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Before any write and before the inbox is even probed: an empty
        # inbox is not a licence for a writer to consume.
        _require_reader_lease(
            connection,
            reader_id=reader_id,
            now_us=effective_now,
        )
        _recover_expired_claims(
            connection,
            resource_kind="message",
            now_us=effective_now,
        )
        parameters: list[Any] = []
        requested = ""
        # An unaddressed claim draws only from `received` messages. An
        # explicit MESSAGE_ID may additionally resume a parked
        # `needs_input` message once its missing input has arrived, or
        # reopen a `failed` message whose disposition was misjudged.
        claimable_states = "'message.received'"
        if message_id is not None:
            requested = "AND message.message_id = ?"
            parameters.append(message_id)
            claimable_states = (
                "'message.received', 'message.needs_input', 'message.failed'"
            )
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
                AND event.event_type IN ({MESSAGE_LIFECYCLE_EVENT_SQL})
            )
            SELECT message.*
            FROM messages AS message
            JOIN lifecycle
              ON lifecycle.message_id = message.message_id
             AND lifecycle.rank = 1
            WHERE lifecycle.event_type IN ({claimable_states})
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
                raise JournalError(
                    f"message is not claimable: {message_id}",
                    code="not_claimable",
                )
            connection.commit()
            return None
        claim = _claim_message_connected(
            connection,
            row["message_id"],
            owner_id=owner,
            lease_seconds=lease,
            now_us=effective_now,
        )
        # Acquisition follows a successful consume only, so an empty poll
        # never turns a passing writer into the reader.
        reader_acquired = _hold_reader_lease(
            connection,
            owner_id=owner,
            reader_id=reader_id,
            lease_seconds=reader_lease,
            now_us=effective_now,
        )
        connection.commit()
        return {
            **claim,
            "reader_acquired": reader_acquired,
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
    reader_id: str | None = None,
    reader_lease_seconds: int = READER_LEASE_SECONDS_DEFAULT,
    now_us: int | None = None,
) -> list[dict[str, Any]]:
    owner = _text(owner_id, path="owner_id", minimum=1, maximum=200)
    lease = _integer(
        lease_seconds,
        path="lease_seconds",
        minimum=1,
        maximum=86400,
    )
    reader_lease = _integer(
        reader_lease_seconds,
        path="reader_lease_seconds",
        minimum=READER_LEASE_SECONDS_MINIMUM,
        maximum=READER_LEASE_SECONDS_MAXIMUM,
    )
    if limit < 1 or limit > 64:
        raise JournalError("queue limit must be between 1 and 64")
    effective_now = _now_us() if now_us is None else now_us
    connection = _connect(scope)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Before any write and before the queue is even read: an empty
        # queue is not a licence for a writer to consume.
        _require_reader_lease(
            connection,
            reader_id=reader_id,
            now_us=effective_now,
        )
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
        candidates.sort(key=_queue_order_key)
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
        if claimed:
            # One transaction, one decision: the flag is the same for
            # every task leased by this call.
            reader_acquired = _hold_reader_lease(
                connection,
                owner_id=owner,
                reader_id=reader_id,
                lease_seconds=reader_lease,
                now_us=effective_now,
            )
            for item in claimed:
                item["reader_acquired"] = reader_acquired
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
    reader_id: str | None = None,
    reader_lease_seconds: int = READER_LEASE_SECONDS_DEFAULT,
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
    reader_lease = _integer(
        reader_lease_seconds,
        path="reader_lease_seconds",
        minimum=READER_LEASE_SECONDS_MINIMUM,
        maximum=READER_LEASE_SECONDS_MAXIMUM,
    )
    effective_now = _now_us() if now_us is None else now_us
    connection = _connect(scope)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Naming one ready task is still dispatch, so it is gated like
        # the unaddressed queue draw.
        _require_reader_lease(
            connection,
            reader_id=reader_id,
            now_us=effective_now,
        )
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
        reader_acquired = _hold_reader_lease(
            connection,
            owner_id=owner,
            reader_id=reader_id,
            lease_seconds=reader_lease,
            now_us=effective_now,
        )
        connection.commit()
        return {
            "task": _task_output(task, "active", states),
            "claim": claim,
            "reader_acquired": reader_acquired,
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
            raise JournalError(
                f"claim is not active: {claim_id}",
                code="claim_mismatch",
            )
        if claim["release_disposition"] is not None:
            if claim["release_disposition"] != "released":
                raise JournalError(
                    f"claim is not active: {claim_id}",
                    code="claim_mismatch",
                )
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
            raise JournalError(
                f"claim has expired: {claim_id}",
                code="claim_expired",
            )
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
    reader_id: str | None = None,
    reader_lease_seconds: int = READER_LEASE_SECONDS_DEFAULT,
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
            raise JournalError(
                f"message claim does not match: {claim_id}",
                code="claim_mismatch",
            )
        if claim["release_disposition"] is not None:
            if claim["release_disposition"] != disposition:
                raise JournalError(
                    f"message claim is not active: {claim_id}",
                    code="claim_mismatch",
                )
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
            raise JournalError(
                f"message claim has expired: {claim_id}",
                code="claim_expired",
            )
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
        # Ungated for the same reason as inbox apply: parking or closing
        # a claimed message hands out no work. Renewal only extends a
        # lease this reader already holds.
        _renew_reader_lease(
            connection,
            reader_id=reader_id,
            lease_seconds=reader_lease_seconds,
            now_us=effective_now,
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


@contextmanager
def _read_snapshot(
    scope: JournalScope,
    now_us: int | None = None,
) -> Iterator[tuple[sqlite3.Connection, int]]:
    """Yield (connection, effective_now) inside one closed read snapshot.

    Every read path shares this transaction lifecycle: one BEGIN DEFERRED
    transaction whose snapshot is pinned before the clock is sampled, a
    guaranteed rollback, and a guaranteed close of the connection.
    """

    connection = _connect(scope)
    try:
        connection.execute("BEGIN DEFERRED")
        try:
            # Pin the WAL read snapshot before sampling the clock so the
            # claim-expiry comparisons and every later SELECT observe one
            # consistent instant of the database.
            connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            yield connection, (_now_us() if now_us is None else now_us)
        finally:
            connection.rollback()
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
    with _read_snapshot(scope) as (connection, effective_now):
        tasks = _load_current_tasks(connection, now_us=effective_now)
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
        selected.sort(key=_queue_order_key)
        return [
            _task_output(task, effective[task["task_id"]], effective)
            for task in selected[:limit]
        ]


def show_task(scope: JournalScope, task_id: str) -> dict[str, Any]:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise JournalError(f"invalid task ID: {task_id}")
    with _read_snapshot(scope) as (connection, effective_now):
        tasks = _load_current_tasks(connection, now_us=effective_now)
        if task_id not in tasks:
            raise JournalError(f"task not found: {task_id}")
        effective = _effective_states(tasks)
        return _task_output(tasks[task_id], effective[task_id], effective)


def next_tasks(
    scope: JournalScope,
    *,
    limit: int = 1,
) -> list[dict[str, Any]]:
    if limit < 1 or limit > 64:
        raise JournalError("queue limit must be between 1 and 64")
    return list_tasks(scope, states={"ready"}, limit=limit)


def read_status(
    scope: JournalScope,
    *,
    ready_limit: int = 5,
    reader_id: str | None = None,
    now_us: int | None = None,
) -> dict[str, Any]:
    """Summarize message, task, and claim counts plus the top ready tasks.

    Reports bounded counts only -- never message or task content -- from
    one read snapshot, plus two bounded listings: the top ready tasks and
    up to five blocked tasks, each blocked entry naming the failed
    prerequisites (``blocked_by``) causing the block. ``project`` carries
    the journal's project label so callers rendering task references need
    no second journal open. A missing journal yields empty counts and the
    derived default label without creating the journal.

    ``reader`` reports the scope's reader lease from this same snapshot,
    so a caller deciding from one status read needs no second open. Its
    ``self`` field is null unless ``reader_id`` names whom to compare
    the recorded holder against, its ``live`` field is true only for
    a held lease whose recorded holder is provably a different live
    session on this host -- the one reading that relieves this caller of
    the work -- and its ``released_by_self`` field is true only for a
    released lease this very session gave up, the recorded signal that
    this caller finished draining on purpose.
    """

    if ready_limit < 1 or ready_limit > 64:
        raise JournalError("queue limit must be between 1 and 64")
    message_counts = dict.fromkeys(MESSAGE_STATES, 0)
    task_counts = dict.fromkeys(TASK_STATES, 0)
    result: dict[str, Any] = {
        "project": default_project_label(scope),
        "messages": message_counts,
        "tasks": task_counts,
        "claims": {"active": 0},
        "reader": _reader_status_summary(
            None,
            _reader_lease_public(None, reader_id=reader_id, now_us=0),
        ),
        "ready": [],
        "blocked": [],
    }
    if not scope.journal_path.exists():
        return result
    with _read_snapshot(scope, now_us) as (connection, effective_now):
        stored_label = _read_project_label(connection)
        if stored_label is not None:
            result["project"] = stored_label
        rows = connection.execute(
            f"""
            WITH lifecycle AS (
              SELECT
                event.message_id,
                event.event_type,
                ROW_NUMBER() OVER (
                  PARTITION BY event.message_id
                  ORDER BY event.sequence DESC
                ) AS rank
              FROM events AS event
              WHERE event.message_id IS NOT NULL
                AND event.event_type IN ({MESSAGE_LIFECYCLE_EVENT_SQL})
            )
            SELECT
              CASE WHEN
                lifecycle.event_type = 'message.processing'
                AND EXISTS (
                  SELECT 1
                  FROM claims AS claim
                  LEFT JOIN claim_releases AS release
                    ON release.claim_id = claim.claim_id
                  WHERE claim.resource_kind = 'message'
                    AND claim.resource_id = lifecycle.message_id
                    AND release.claim_id IS NULL
                    AND claim.expires_at_us <= ?
                )
              THEN 'message.received'
              ELSE lifecycle.event_type
              END AS state_event,
              COUNT(*) AS total
            FROM lifecycle
            WHERE lifecycle.rank = 1
            GROUP BY state_event
            """,
            (effective_now,),
        ).fetchall()
        for row in rows:
            state = row["state_event"].removeprefix("message.")
            if state in message_counts:
                message_counts[state] += row["total"]
        tasks = _load_current_tasks(connection, now_us=effective_now)
        states = _effective_states(tasks)
        for task_id in tasks:
            task_counts[states[task_id]] += 1
        ready = [
            task
            for task in tasks.values()
            if states[task["task_id"]] == "ready"
        ]
        ready.sort(key=_queue_order_key)
        result["ready"] = [
            {
                "task_id": task["task_id"],
                "priority": task["priority"],
                "title": task["title"],
                "created_at": task["created_at"],
            }
            for task in ready[:ready_limit]
        ]
        blocked = [
            task
            for task in tasks.values()
            if states[task["task_id"]] == "blocked"
        ]
        blocked.sort(key=_queue_order_key)
        result["blocked"] = [
            {
                "task_id": task["task_id"],
                "priority": task["priority"],
                "title": task["title"],
                # The failed prerequisites causing the block, matching
                # the _task_output derivation; intrinsically blocked
                # tasks (recorded state "blocked") report an empty list.
                "blocked_by": sorted(
                    dependency
                    for dependency in task["dependencies"]
                    if states[dependency] in FAILURE_STATES
                ),
            }
            for task in blocked[:5]
        ]
        result["claims"]["active"] = connection.execute(
            """
            SELECT COUNT(*) AS total
            FROM claims AS claim
            LEFT JOIN claim_releases AS release
              ON release.claim_id = claim.claim_id
            WHERE release.claim_id IS NULL
              AND claim.expires_at_us > ?
            """,
            (effective_now,),
        ).fetchone()["total"]
        reader_row = _read_reader_lease_row(connection)
        result["reader"] = _reader_status_summary(
            reader_row,
            _reader_lease_public(
                reader_row,
                reader_id=reader_id,
                now_us=effective_now,
            ),
        )
        return result


def _explanation(
    task: dict[str, Any],
    state: str,
    lease: dict[str, Any] | None,
    blocked_by: list[str],
    waiting_on: list[str],
) -> str:
    if state == "active":
        return (
            f"active: leased by {lease['owner_id']} "
            f"until {lease['expires_at']}"
        )
    if state == "ready":
        if task["dependencies"]:
            return "ready: all prerequisites are done"
        return "ready: no prerequisites"
    if state == "queued":
        return f"queued: waiting on {', '.join(waiting_on)}"
    if state == "blocked":
        if task["state"] == "blocked":
            return f"blocked: {task['reason']}"
        return f"blocked: failed prerequisites {', '.join(blocked_by)}"
    if state == "canceled":
        return f"canceled: {task['reason']}"
    if state == "superseded":
        return (
            f"superseded by {task['superseded_by_task_id']}: {task['reason']}"
        )
    if state == "done":
        return "done"
    raise JournalError(f"unknown task state: {state}")


def explain_task(
    scope: JournalScope,
    task_id: str,
    *,
    now_us: int | None = None,
) -> dict[str, Any]:
    """Explain one task's effective queue state from a single snapshot."""

    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise JournalError(f"invalid task ID: {task_id}")
    with _read_snapshot(scope, now_us) as (connection, effective_now):
        tasks = _load_current_tasks(connection, now_us=effective_now)
        if task_id not in tasks:
            raise JournalError(f"task not found: {task_id}")
        states = _effective_states(tasks)
        task = tasks[task_id]
        output = _task_output(task, states[task_id], states)
        prerequisites = [
            {
                "task_id": dependency,
                "state": states[dependency],
                "satisfied": states[dependency] == "done",
            }
            for dependency in sorted(output["dependencies"])
        ]
        lease = (
            _claim_summary(output["claim"])
            if output["claim"] is not None
            else None
        )
        return {
            "task_id": task_id,
            "revision": output["revision"],
            "state": output["state"],
            "recorded_state": output["recorded_state"],
            "prerequisites": prerequisites,
            "blocked_by": output["blocked_by"],
            "waiting_on": output["waiting_on"],
            "claim": lease,
            "reason": output["reason"],
            "superseded_by_task_id": output["superseded_by_task_id"],
            "explanation": _explanation(
                task,
                output["state"],
                lease,
                output["blocked_by"],
                output["waiting_on"],
            ),
        }


_HISTORY_TASK_EVENTS = {
    "task.created",
    "task.revised",
    "task.state_changed",
    "task.dependency_added",
    "task.dependency_removed",
}


def _history_detail(
    task_id: str,
    row: sqlite3.Row,
    payload: dict[str, Any],
) -> dict[str, Any]:
    event_type = row["event_type"]
    if event_type == "claim.acquired":
        expires_at_us = payload.get("expires_at_us")
        return {
            "claim_id": payload.get("claim_id"),
            "owner_id": payload.get("owner_id"),
            "expires_at": (
                _us_timestamp(expires_at_us)
                if isinstance(expires_at_us, int)
                and not isinstance(expires_at_us, bool)
                else None
            ),
        }
    if event_type.startswith("claim."):
        return {
            "claim_id": payload.get("claim_id"),
            "disposition": payload.get("disposition"),
        }
    if event_type not in _HISTORY_TASK_EVENTS:
        return {}
    if row["revision"] is None:
        raise JournalError(f"task event has no revision: {task_id}")
    detail: dict[str, Any] = {"revision": row["revision"]}
    if event_type == "task.created":
        detail["state"] = row["state"]
    elif event_type == "task.revised":
        effect = payload.get("effect")
        patch = (
            effect[2]
            if isinstance(effect, list)
            and len(effect) == 3
            and isinstance(effect[2], dict)
            else {}
        )
        detail["fields"] = sorted(patch)
    elif event_type == "task.state_changed":
        detail["state"] = row["state"]
        detail["reason"] = row["reason"]
        detail["superseded_by_task_id"] = row["superseded_by_task_id"]
    else:
        # The dependency delta is deliberately derived by diffing stored
        # revisions rather than by reading the event payload: payload
        # references may carry unresolved local aliases ("$name"), while
        # revisions always hold canonical task IDs. The audit guarantees
        # each dependency event changes exactly one edge.
        before = (
            set(json.loads(row["previous_dependencies_json"]))
            if row["previous_dependencies_json"] is not None
            else set()
        )
        after = set(json.loads(row["dependencies_json"]))
        changed = sorted(after ^ before)
        assert len(changed) == 1, changed
        detail["dependency"] = changed[0]
    return detail


def task_history(
    scope: JournalScope,
    task_id: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return one task's recorded events, newest first, from one snapshot."""

    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise JournalError(f"invalid task ID: {task_id}")
    if limit < 1 or limit > 1000:
        raise JournalError("history limit must be between 1 and 1000")
    with _read_snapshot(scope) as (connection, _effective_now):
        exists = connection.execute(
            "SELECT 1 FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not exists:
            raise JournalError(f"task not found: {task_id}")
        # One query joins events to their task revisions; the LAG window
        # supplies the previous revision's dependencies for dependency
        # deltas. The window runs over all of the task's revisions, so a
        # limited page still sees its predecessors.
        rows = connection.execute(
            """
            SELECT
              event.occurred_at,
              event.event_type,
              event.payload_json,
              revision.revision,
              revision.state,
              revision.reason,
              revision.superseded_by_task_id,
              revision.dependencies_json,
              revision.previous_dependencies_json
            FROM events AS event
            LEFT JOIN (
              SELECT
                event_sequence,
                revision,
                state,
                reason,
                superseded_by_task_id,
                dependencies_json,
                LAG(dependencies_json) OVER (
                  ORDER BY revision
                ) AS previous_dependencies_json
              FROM task_revisions
              WHERE task_id = ?
            ) AS revision
              ON revision.event_sequence = event.sequence
            WHERE event.task_id = ?
            ORDER BY event.sequence DESC
            LIMIT ?
            """,
            (task_id, task_id, limit),
        ).fetchall()
        return [
            {
                "occurred_at": _z_timestamp(row["occurred_at"]),
                "type": row["event_type"],
                "detail": _history_detail(
                    task_id,
                    row,
                    json.loads(row["payload_json"]),
                ),
            }
            for row in rows
        ]


def list_claims(
    scope: JournalScope,
    *,
    owner_id: str | None = None,
    resource_kind: str | None = None,
    status: str | None = None,
    limit: int = 100,
    now_us: int | None = None,
) -> list[dict[str, Any]]:
    """List unreleased claims in acquisition order, bounded by limit."""

    if limit < 1 or limit > 1000:
        raise JournalError("claim limit must be between 1 and 1000")
    if resource_kind is not None and resource_kind not in {"message", "task"}:
        raise JournalError(
            f"unsupported claim resource filter: {resource_kind}"
        )
    if status is not None and status not in {"active", "expired"}:
        raise JournalError(f"unsupported claim status filter: {status}")
    owner = (
        None
        if owner_id is None
        else _text(owner_id, path="owner_id", minimum=1, maximum=200)
    )
    with _read_snapshot(scope, now_us) as (connection, effective_now):
        conditions = ["release.claim_id IS NULL"]
        parameters: list[Any] = []
        if owner is not None:
            conditions.append("claim.owner_id = ?")
            parameters.append(owner)
        if resource_kind is not None:
            conditions.append("claim.resource_kind = ?")
            parameters.append(resource_kind)
        if status == "active":
            conditions.append("claim.expires_at_us > ?")
            parameters.append(effective_now)
        elif status == "expired":
            conditions.append("claim.expires_at_us <= ?")
            parameters.append(effective_now)
        rows = connection.execute(
            f"""
            SELECT
              claim.claim_id,
              claim.resource_kind,
              claim.resource_id,
              claim.owner_id,
              claim.basis_revision,
              claim.expires_at_us
            FROM claims AS claim
            LEFT JOIN claim_releases AS release
              ON release.claim_id = claim.claim_id
            WHERE {" AND ".join(conditions)}
            ORDER BY claim.fence
            LIMIT ?
            """,
            (*parameters, limit),
        ).fetchall()
        return [
            {
                **_claim_summary(row),
                "resource_kind": row["resource_kind"],
                "resource_id": row["resource_id"],
                "basis_revision": row["basis_revision"],
                "status": (
                    "active"
                    if row["expires_at_us"] > effective_now
                    else "expired"
                ),
            }
            for row in rows
        ]


def _resolve(reference: str, aliases: dict[str, str]) -> str:
    if reference.startswith("$"):
        try:
            return aliases[reference]
        except KeyError as error:
            raise JournalError(
                f"unknown local task alias: {reference}",
                code="invalid_document",
            ) from error
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
    reader_id: str | None = None,
    reader_lease_seconds: int = READER_LEASE_SECONDS_DEFAULT,
    now_us: int | None = None,
) -> dict[str, Any]:
    connection = _connect(scope)
    try:
        connection.execute("BEGIN IMMEDIATE")
        result = _apply_effects_connected(
            connection,
            message_id,
            document,
            claim_id=claim_id,
        )
        # Ungated on purpose: the message claim already proves legitimate
        # consumption of this message, and applying it hands out no new
        # work. A reader still holding the role keeps it warm.
        _renew_reader_lease(
            connection,
            reader_id=reader_id,
            lease_seconds=reader_lease_seconds,
            now_us=_now_us() if now_us is None else now_us,
        )
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _apply_effects_connected(
    connection: sqlite3.Connection,
    message_id: str,
    document: dict[str, Any],
    *,
    claim_id: str,
) -> dict[str, Any]:
    """Apply one claimed message's effects inside an open transaction.

    The caller owns the connection, the surrounding transaction, and the
    commit or rollback. The public :func:`apply_effects` wraps this in
    one immediate transaction; composed transactional commands reuse it
    so a message, its claim, and its effects commit atomically.
    """

    if not isinstance(message_id, str) or not message_id.startswith("msg_"):
        raise JournalError(f"invalid message ID: {message_id}")
    if not CLAIM_ID_PATTERN.fullmatch(claim_id):
        raise JournalError(f"invalid claim ID: {claim_id}")
    _validate_document_shape(document)
    canonical = _canonical_json(document)
    if len(canonical.encode("utf-8")) > EFFECT_DOCUMENT_MAX_BYTES:
        raise JournalError(
            f"effects document exceeds {EFFECT_DOCUMENT_MAX_BYTES} bytes"
        )
    effects_hash = hashlib.sha256(canonical.encode()).hexdigest()
    effects = document["effects"]

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
                "application replay claim does not match the original claim",
                code="claim_mismatch",
            )
        result = json.loads(existing["result_json"])
        result["replayed"] = True
        return result

    message = connection.execute(
        f"""
        SELECT
          message.message_id,
          (
            SELECT event_type
            FROM events
            WHERE message_id = message.message_id
              AND event_type IN ({MESSAGE_LIFECYCLE_EVENT_SQL})
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
        raise JournalError(
            f"message claim is not active: {claim_id}",
            code="claim_mismatch",
        )
    effective_now = _now_us()
    if message_claim["expires_at_us"] <= effective_now:
        raise JournalError(
            f"message claim has expired: {claim_id}",
            code="claim_expired",
        )
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
                f"expected {revision}, found {actual}",
                code="revision_conflict",
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
        canonical_id = _resolve(reference, aliases)
        if reference.startswith("$") and create_indexes[reference] >= index:
            raise JournalError(
                f"local alias must be created before effect {index}: {reference}"
            )
        return canonical_id

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
    return result


def overview_tasks(
    scope: JournalScope,
    *,
    states: set[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List compact task rows in task-number order from one snapshot.

    Unlike :func:`list_tasks`, an explicit ``states`` filter may select
    terminal states; the default remains the non-terminal states.
    """

    if limit < 1 or limit > 1000:
        raise JournalError("task limit must be between 1 and 1000")
    if states and not states <= set(TASK_STATES):
        raise JournalError("unsupported task state filter")
    with _read_snapshot(scope) as (connection, effective_now):
        tasks = _load_current_tasks(connection, now_us=effective_now)
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
        selected.sort(key=lambda task: task["task_number"])
        return [
            {
                "task_id": task["task_id"],
                "revision": task["revision"],
                "state": effective[task["task_id"]],
                "priority": task["priority"],
                "title": task["title"],
            }
            for task in selected[:limit]
        ]


def enqueue_task(
    scope: JournalScope,
    *,
    title: str,
    objective: str | None = None,
    priority: int = 0,
    requires: list[str] | tuple[str, ...] = (),
    owner_id: str,
    lease_seconds: int = 900,
    cwd: str | None = None,
    now_us: int | None = None,
) -> dict[str, Any]:
    """Create one task through the full message pipeline atomically.

    One transaction persists an auto-generated request message, claims
    it, applies a single create-task effects document, and marks the
    message applied. The task mutation still flows through a recorded
    message and one atomic effects application; nothing bypasses the
    pipeline, and any failure rolls the whole request back.
    """

    owner = _text(owner_id, path="owner_id", minimum=1, maximum=200)
    lease = _integer(
        lease_seconds,
        path="lease_seconds",
        minimum=1,
        maximum=86400,
    )
    requirements = list(requires)
    for reference in requirements:
        if not isinstance(reference, str) or not TASK_ID_PATTERN.fullmatch(
            reference
        ):
            raise JournalError(
                f"invalid task ID: {reference}",
                code="invalid_argument",
            )
    if len(requirements) != len(set(requirements)):
        raise JournalError(
            "duplicate required task ID",
            code="invalid_argument",
        )
    specification: dict[str, Any] = {"title": title, "priority": priority}
    if objective is not None:
        specification["objective"] = objective
    if requirements:
        specification["requires"] = requirements
    content = _canonical_json(
        {"action": "enqueue", "spec": specification, "v": 1}
    )
    effective_now = _now_us() if now_us is None else now_us
    connection = _connect(scope)
    try:
        connection.execute("BEGIN IMMEDIATE")
        ingested = _ingest_connected(
            connection,
            scope,
            content,
            source="cli",
            cwd=cwd,
        )
        claim = _claim_message_connected(
            connection,
            ingested.message_id,
            owner_id=owner,
            lease_seconds=lease,
            now_us=effective_now,
        )
        expect: dict[str, int] = {}
        for reference in requirements:
            row = connection.execute(
                "SELECT revision FROM current_tasks WHERE task_id = ?",
                (reference,),
            ).fetchone()
            if row is None:
                raise JournalError(
                    f"task not found: {reference}",
                    code="not_found",
                )
            expect[reference] = row["revision"]
        result = _apply_effects_connected(
            connection,
            ingested.message_id,
            {
                "v": 1,
                "expect": expect,
                "effects": [["create", "$task", specification]],
            },
            claim_id=claim["claim_id"],
        )
        connection.commit()
        task = result["tasks"][0]
        return {
            "task_id": task["task_id"],
            "message_id": ingested.message_id,
            "state": task["state"],
            "revision": task["revision"],
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def settle_tasks_done(
    scope: JournalScope,
    *,
    task_ids: list[str] | tuple[str, ...],
    summary: str,
    owner_id: str,
    lease_seconds: int = 900,
    cwd: str | None = None,
    reader_id: str | None = None,
    reader_lease_seconds: int = READER_LEASE_SECONDS_DEFAULT,
    now_us: int | None = None,
) -> dict[str, Any]:
    """Transition every named task to done in one atomic transaction.

    One transaction persists the summary as a message, claims it, and
    applies a single effects document transitioning every named task to
    done with each task's current revision resolved into ``expect``. A
    task already active under ``owner_id`` completes with its existing
    claim; a ready task is leased inside the same transaction. Any
    ineligible task fails the whole command without partial changes.

    Single-reader governs dispatch, not settlement. Settling a task
    already active under this owner is pure settlement and stays open to
    every session, because every session is obliged to record what it
    finished. Settling a merely ready task leases it here, which is
    dispatch, and therefore requires the reader role.
    """

    identifiers = list(task_ids)
    if not identifiers:
        raise JournalError(
            "at least one task ID is required",
            code="invalid_argument",
        )
    for task_id in identifiers:
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(
            task_id
        ):
            raise JournalError(
                f"invalid task ID: {task_id}",
                code="invalid_argument",
            )
    if len(identifiers) != len(set(identifiers)):
        raise JournalError(
            "duplicate task ID",
            code="invalid_argument",
        )
    owner = _text(owner_id, path="owner_id", minimum=1, maximum=200)
    lease = _integer(
        lease_seconds,
        path="lease_seconds",
        minimum=1,
        maximum=86400,
    )
    explanation = _text(summary, path="summary", minimum=1, maximum=1000)
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
        settlement_claims: dict[str, dict[str, Any]] = {}
        for task_id in identifiers:
            if task_id not in tasks:
                raise JournalError(
                    f"task not found: {task_id}",
                    code="not_found",
                )
            task = tasks[task_id]
            state = states[task_id]
            if state == "active":
                claim = task["claim"]
                if claim["owner_id"] != owner:
                    raise JournalError(
                        f"task is not settleable: {task_id}: active claim "
                        f"is held by another owner",
                        code="not_claimable",
                    )
                settlement_claims[task_id] = dict(claim)
            elif state == "ready":
                _require_reader_lease(
                    connection,
                    reader_id=reader_id,
                    now_us=effective_now,
                )
                settlement_claims[task_id] = _claim_resource(
                    connection,
                    resource_kind="task",
                    resource_id=task_id,
                    owner_id=owner,
                    lease_seconds=lease,
                    now_us=effective_now,
                    basis_revision=task["revision"],
                )
            else:
                raise JournalError(
                    f"task is not settleable: {task_id}: {state}",
                    code="state_conflict",
                )
        ingested = _ingest_connected(
            connection,
            scope,
            explanation,
            source="cli",
            cwd=cwd,
        )
        message_claim = _claim_message_connected(
            connection,
            ingested.message_id,
            owner_id=owner,
            lease_seconds=lease,
            now_us=effective_now,
        )
        result = _apply_effects_connected(
            connection,
            ingested.message_id,
            {
                "v": 1,
                "expect": {
                    task_id: tasks[task_id]["revision"]
                    for task_id in identifiers
                },
                "effects": [
                    [
                        "transition",
                        task_id,
                        "done",
                        {
                            "claim": settlement_claims[task_id]["claim_id"],
                            "reason": explanation,
                        },
                    ]
                    for task_id in identifiers
                ],
            },
            claim_id=message_claim["claim_id"],
        )
        # Settlement never takes the role; it only keeps a held one warm.
        _renew_reader_lease(
            connection,
            reader_id=reader_id,
            lease_seconds=reader_lease_seconds,
            now_us=effective_now,
        )
        connection.commit()
        return {
            "status": "done",
            "message_id": ingested.message_id,
            "tasks": [
                {
                    "task_id": task["task_id"],
                    "revision": task["revision"],
                    "state": task["state"],
                }
                for task in result["tasks"]
            ],
        }
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
                f"application claim mismatch: {application['message_id']}",
                code="claim_mismatch",
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
            context = effect_context.pop(revision["event_sequence"], None)
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

    if effect_context:
        event_sequence = min(effect_context)
        effect = connection.execute(
            """
            SELECT message_id, effect_index
            FROM task_effects
            WHERE event_sequence = ?
            """,
            (event_sequence,),
        ).fetchone()
        if effect is None:
            raise JournalError(
                f"sealed task effect is missing: event {event_sequence}"
            )
        raise JournalError(
            f"sealed task effect has no task revision: "
            f"{effect['message_id']}:{effect['effect_index']}"
        )

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
