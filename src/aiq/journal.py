from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import time
from typing import Any, Iterator
import uuid


SCHEMA_VERSION = 3
SQLITE_MINIMUM_VERSION = (3, 37, 0)

# Every event type that can record a message's lifecycle state. The latest
# of these per message defines its current state. 'message.superseded' is
# deliberately included even though no current writer emits it and the
# reportable-state surface (queue.MESSAGE_STATES) omits it: recognizing it
# here keeps any future supersession event terminal instead of silently
# falling back to an older lifecycle event and misreporting the message.
MESSAGE_LIFECYCLE_EVENTS = (
    "message.received",
    "message.processing",
    "message.applied",
    "message.needs_input",
    "message.failed",
    "message.superseded",
)
# Static SQL fragment for `event_type IN (...)` filters; the event names
# are module constants, never user input.
MESSAGE_LIFECYCLE_EVENT_SQL = ", ".join(
    f"'{event}'" for event in MESSAGE_LIFECYCLE_EVENTS
)


class JournalError(RuntimeError):
    """Journal operation failed.

    ``code`` optionally carries a stable machine-readable error code from
    the raise site so the CLI can classify the failure without matching
    message substrings.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class _NotGitRepository(JournalError):
    """Git confirmed that a path is outside a repository."""


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise JournalError(f"journal directory is not a real directory: {path}") from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
            raise JournalError(f"journal directory is not private: {path}")
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)


def _validate_private_file(path: Path) -> None:
    status = path.lstat()
    if (
        not stat.S_ISREG(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or status.st_uid != os.getuid()
        or status.st_nlink != 1
    ):
        raise JournalError(f"journal file is not a private regular file: {path}")


def _chmod_private_file(path: Path) -> None:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise JournalError(
            f"journal file is not a private regular file: {path}"
        ) from error
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.getuid()
            or status.st_nlink != 1
        ):
            raise JournalError(
                f"journal file is not a private regular file: {path}"
            )
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _open_private_lock(path: Path):
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise JournalError(f"cannot open private journal lock: {path}") from error
    status = os.fstat(descriptor)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.getuid()
        or status.st_nlink != 1
    ):
        os.close(descriptor)
        raise JournalError(f"journal lock is not a private regular file: {path}")
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "a+")


def _scope_lifecycle_lock_path(
    journal_path: Path,
) -> Path:
    if not journal_path.is_absolute():
        raise JournalError("journal path must be absolute")
    return journal_path.parent / "lifecycle.lock"


def _lifecycle_lock_path(scope: JournalScope) -> Path:
    if scope.lifecycle_lock_path is not None:
        return scope.lifecycle_lock_path
    return _scope_lifecycle_lock_path(scope.journal_path)


@contextmanager
def lifecycle_lock(
    scope: JournalScope,
    *,
    exclusive: bool,
) -> Iterator[None]:
    """Coordinate normal access with destructive operations for one scope."""
    lock_path = _lifecycle_lock_path(scope)
    _ensure_private_directory(lock_path.parent)
    with _open_private_lock(lock_path) as lock_file:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(lock_file, operation)
        except OSError as error:
            raise JournalError(
                f"cannot acquire journal lifecycle lock: {lock_path}"
            ) from error
        try:
            yield
        finally:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except OSError as error:
                raise JournalError(
                    f"cannot release journal lifecycle lock: {lock_path}"
                ) from error


@dataclass(frozen=True)
class JournalScope:
    kind: str
    root: Path
    scope_id: str
    journal_path: Path
    lifecycle_lock_path: Path | None = None

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "root": str(self.root),
            "scope_id": self.scope_id,
            "journal_path": str(self.journal_path),
        }


@dataclass(frozen=True)
class IngestResult:
    message_id: str
    event_id: str
    sequence: int
    state: str
    created: bool
    scope: JournalScope
    deduped: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["scope"] = self.scope.to_dict()
        return result


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS journal_metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS messages (
  message_id TEXT PRIMARY KEY,
  received_at TEXT NOT NULL,
  source TEXT NOT NULL,
  content TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  idempotency_key TEXT UNIQUE,
  session_id TEXT,
  turn_id TEXT,
  cwd TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  occurred_at TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message_id TEXT REFERENCES messages(message_id),
  task_id TEXT,
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
) STRICT;

CREATE INDEX IF NOT EXISTS events_message_sequence
  ON events(message_id, sequence);

CREATE TRIGGER IF NOT EXISTS messages_no_update
BEFORE UPDATE ON messages
BEGIN
  SELECT RAISE(ABORT, 'messages are append-only');
END;

CREATE TRIGGER IF NOT EXISTS messages_no_delete
BEFORE DELETE ON messages
BEGIN
  SELECT RAISE(ABORT, 'messages are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
  SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
  SELECT RAISE(ABORT, 'events are append-only');
END;
"""

SCHEMA_V2_STATEMENTS = (
    """
    CREATE TABLE schema_migrations (
      migration_id INTEGER PRIMARY KEY,
      from_version INTEGER NOT NULL,
      to_version INTEGER NOT NULL,
      migrated_at TEXT NOT NULL,
      backup_name TEXT
    ) STRICT
    """,
    """
    CREATE TABLE task_numbers (
      task_number INTEGER PRIMARY KEY AUTOINCREMENT
    ) STRICT
    """,
    """
    CREATE TABLE tasks (
      task_id TEXT PRIMARY KEY,
      task_number INTEGER NOT NULL UNIQUE
        REFERENCES task_numbers(task_number),
      created_at TEXT NOT NULL,
      created_by_message_id TEXT NOT NULL REFERENCES messages(message_id),
      created_sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence),
      CHECK (task_id = 'TASK-' || task_number)
    ) STRICT
    """,
    """
    CREATE TABLE task_revisions (
      task_id TEXT NOT NULL REFERENCES tasks(task_id),
      revision INTEGER NOT NULL CHECK (revision > 0),
      event_sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence),
      state TEXT NOT NULL CHECK (
        state IN (
          'queued', 'ready', 'active', 'blocked',
          'done', 'canceled', 'superseded'
        )
      ),
      title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
      objective TEXT CHECK (objective IS NULL OR length(objective) <= 2000),
      priority INTEGER NOT NULL CHECK (priority BETWEEN -1000000 AND 1000000),
      parent_task_id TEXT REFERENCES tasks(task_id),
      dependencies_json TEXT NOT NULL CHECK (
        json_valid(dependencies_json)
        AND json_type(dependencies_json) = 'array'
      ),
      reason TEXT CHECK (reason IS NULL OR length(reason) <= 1000),
      superseded_by_task_id TEXT REFERENCES tasks(task_id),
      PRIMARY KEY (task_id, revision),
      CHECK (parent_task_id IS NULL OR parent_task_id <> task_id),
      CHECK (
        (state = 'superseded' AND superseded_by_task_id IS NOT NULL)
        OR
        (state <> 'superseded' AND superseded_by_task_id IS NULL)
      )
    ) STRICT
    """,
    """
    CREATE TABLE task_effects (
      message_id TEXT NOT NULL REFERENCES messages(message_id),
      effect_index INTEGER NOT NULL CHECK (effect_index >= 0),
      event_sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence),
      operation TEXT NOT NULL CHECK (
        operation IN ('create', 'update', 'transition', 'require', 'unrequire')
      ),
      task_id TEXT NOT NULL REFERENCES tasks(task_id),
      payload_json TEXT NOT NULL CHECK (
        json_valid(payload_json) AND json_type(payload_json) = 'object'
      ),
      PRIMARY KEY (message_id, effect_index)
    ) STRICT
    """,
    """
    CREATE TABLE message_applications (
      message_id TEXT PRIMARY KEY REFERENCES messages(message_id),
      claim_id TEXT NOT NULL UNIQUE REFERENCES claims(claim_id),
      effects_sha256 TEXT NOT NULL CHECK (
        length(effects_sha256) = 64
        AND effects_sha256 NOT GLOB '*[^0-9a-f]*'
      ),
      applied_at TEXT NOT NULL,
      applied_event_sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence),
      effect_count INTEGER NOT NULL CHECK (effect_count >= 0),
      document_json TEXT NOT NULL CHECK (
        json_valid(document_json) AND json_type(document_json) = 'object'
      ),
      result_json TEXT NOT NULL CHECK (
        json_valid(result_json) AND json_type(result_json) = 'object'
      )
    ) STRICT
    """,
    """
    CREATE TABLE claims (
      claim_id TEXT PRIMARY KEY,
      resource_kind TEXT NOT NULL CHECK (
        resource_kind IN ('message', 'task')
      ),
      resource_id TEXT NOT NULL,
      owner_id TEXT NOT NULL CHECK (length(owner_id) BETWEEN 1 AND 200),
      fence INTEGER NOT NULL UNIQUE REFERENCES events(sequence),
      basis_revision INTEGER CHECK (
        basis_revision IS NULL OR basis_revision > 0
      ),
      acquired_at_us INTEGER NOT NULL,
      expires_at_us INTEGER NOT NULL CHECK (expires_at_us > acquired_at_us),
      CHECK (
        (resource_kind = 'message' AND basis_revision IS NULL)
        OR
        (resource_kind = 'task' AND basis_revision IS NOT NULL)
      )
    ) STRICT
    """,
    """
    CREATE TABLE claim_releases (
      claim_id TEXT PRIMARY KEY REFERENCES claims(claim_id),
      event_sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence),
      disposition TEXT NOT NULL CHECK (
        disposition IN (
          'released', 'applied', 'completed', 'revoked', 'expired',
          'needs_input', 'failed'
        )
      ),
      released_at_us INTEGER NOT NULL
    ) STRICT
    """,
    """
    CREATE TRIGGER tasks_validate_insert
    BEFORE INSERT ON tasks
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM events
        WHERE sequence = NEW.created_sequence
          AND event_type = 'task.created'
          AND message_id = NEW.created_by_message_id
          AND task_id = NEW.task_id
      )
      THEN RAISE(ABORT, 'task creation event mismatch')
      END;
    END
    """,
    """
    CREATE TRIGGER task_effects_validate_insert
    BEFORE INSERT ON task_effects
    BEGIN
      SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM message_applications
        WHERE message_id = NEW.message_id
      )
      THEN RAISE(ABORT, 'message application is sealed')
      END;

      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM events
        WHERE sequence = NEW.event_sequence
          AND message_id = NEW.message_id
          AND task_id = NEW.task_id
          AND event_type = CASE NEW.operation
            WHEN 'create' THEN 'task.created'
            WHEN 'update' THEN 'task.revised'
            WHEN 'transition' THEN 'task.state_changed'
            WHEN 'require' THEN 'task.dependency_added'
            WHEN 'unrequire' THEN 'task.dependency_removed'
          END
          AND payload_json = NEW.payload_json
      )
      THEN RAISE(ABORT, 'task effect event mismatch')
      END;
    END
    """,
    """
    CREATE TRIGGER task_revisions_validate_insert
    BEFORE INSERT ON task_revisions
    BEGIN
      SELECT CASE WHEN NEW.revision <> COALESCE(
        (
          SELECT MAX(revision) + 1
          FROM task_revisions
          WHERE task_id = NEW.task_id
        ),
        1
      )
      THEN RAISE(ABORT, 'task revision is not contiguous')
      END;

      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM task_effects
        WHERE event_sequence = NEW.event_sequence
          AND task_id = NEW.task_id
      )
      THEN RAISE(ABORT, 'task revision effect mismatch')
      END;
    END
    """,
    """
    CREATE TRIGGER message_applications_validate_insert
    BEFORE INSERT ON message_applications
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM events
        WHERE sequence = NEW.applied_event_sequence
          AND event_type = 'message.applied'
          AND message_id = NEW.message_id
      )
      THEN RAISE(ABORT, 'message application event mismatch')
      END;

      SELECT CASE WHEN NEW.effect_count <> (
        SELECT COUNT(*)
        FROM task_effects
        WHERE message_id = NEW.message_id
      )
      THEN RAISE(ABORT, 'message application effect count mismatch')
      END;

      SELECT CASE WHEN NEW.effect_count > 0 AND (
        SELECT MIN(effect_index) <> 0
          OR MAX(effect_index) <> NEW.effect_count - 1
        FROM task_effects
        WHERE message_id = NEW.message_id
      )
      THEN RAISE(ABORT, 'message application effects are not contiguous')
      END;

      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM claims AS claim
        JOIN claim_releases AS release
          ON release.claim_id = claim.claim_id
        WHERE claim.claim_id = NEW.claim_id
          AND claim.resource_kind = 'message'
          AND claim.resource_id = NEW.message_id
          AND release.disposition = 'applied'
      )
      THEN RAISE(ABORT, 'message application claim mismatch')
      END;
    END
    """,
    """
    CREATE TRIGGER claims_validate_insert
    BEFORE INSERT ON claims
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM events
        WHERE sequence = NEW.fence
          AND event_type = 'claim.acquired'
          AND (
            (
              NEW.resource_kind = 'message'
              AND message_id = NEW.resource_id
              AND task_id IS NULL
            )
            OR
            (
              NEW.resource_kind = 'task'
              AND task_id = NEW.resource_id
              AND message_id IS NULL
            )
          )
      )
      THEN RAISE(ABORT, 'claim event mismatch')
      END;
    END
    """,
    """
    CREATE TRIGGER claim_releases_validate_insert
    BEFORE INSERT ON claim_releases
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM claims AS claim
        JOIN events AS event ON event.sequence = NEW.event_sequence
        WHERE claim.claim_id = NEW.claim_id
          AND event.event_type = CASE NEW.disposition
            WHEN 'released' THEN 'claim.released'
            WHEN 'applied' THEN 'claim.consumed'
            WHEN 'completed' THEN 'claim.consumed'
            WHEN 'needs_input' THEN 'claim.consumed'
            WHEN 'failed' THEN 'claim.consumed'
            WHEN 'revoked' THEN 'claim.revoked'
            WHEN 'expired' THEN 'claim.expired'
          END
          AND json_extract(event.payload_json, '$.claim_id') = NEW.claim_id
          AND json_extract(event.payload_json, '$.disposition') = NEW.disposition
          AND (
            (
              claim.resource_kind = 'message'
              AND event.message_id = claim.resource_id
            )
            OR
            (
              claim.resource_kind = 'task'
              AND event.task_id = claim.resource_id
            )
          )
      )
      THEN RAISE(ABORT, 'claim release event mismatch')
      END;
    END
    """,
    """
    CREATE VIEW current_tasks AS
    SELECT revision.*
    FROM task_revisions AS revision
    WHERE NOT EXISTS (
      SELECT 1
      FROM task_revisions AS newer
      WHERE newer.task_id = revision.task_id
        AND newer.revision > revision.revision
    )
    """,
    """
    CREATE INDEX task_revisions_latest
      ON task_revisions(task_id, revision DESC)
    """,
    """
    CREATE INDEX task_current_queue_order
      ON task_revisions(state, priority DESC, event_sequence, task_id)
    """,
    """
    CREATE INDEX events_task_sequence
      ON events(task_id, sequence)
      WHERE task_id IS NOT NULL
    """,
)

# Claim probes filter on resource_kind alone (expiry recovery) or on the
# (resource_kind, resource_id) pair (status and lookup probes); without
# this index every probe scans the claims table.
SCHEMA_V3_STATEMENTS = (
    """
    CREATE INDEX claims_resource_lookup
      ON claims(resource_kind, resource_id)
    """,
)

APPEND_ONLY_V2_TABLES = (
    "schema_migrations",
    "task_numbers",
    "tasks",
    "task_revisions",
    "task_effects",
    "message_applications",
    "claims",
    "claim_releases",
)

REPLACE_GUARDS = {
    "messages": """
      EXISTS (
        SELECT 1 FROM messages WHERE message_id = NEW.message_id
      )
      OR (
        NEW.idempotency_key IS NOT NULL
        AND EXISTS (
          SELECT 1
          FROM messages
          WHERE idempotency_key = NEW.idempotency_key
        )
      )
    """,
    "events": """
      EXISTS (
        SELECT 1
        FROM events
        WHERE sequence = NEW.sequence OR event_id = NEW.event_id
      )
    """,
    "schema_migrations": """
      EXISTS (
        SELECT 1
        FROM schema_migrations
        WHERE migration_id = NEW.migration_id
      )
    """,
    "task_numbers": """
      EXISTS (
        SELECT 1
        FROM task_numbers
        WHERE task_number = NEW.task_number
      )
    """,
    "tasks": """
      EXISTS (
        SELECT 1
        FROM tasks
        WHERE task_id = NEW.task_id
          OR task_number = NEW.task_number
          OR created_sequence = NEW.created_sequence
      )
    """,
    "task_revisions": """
      EXISTS (
        SELECT 1
        FROM task_revisions
        WHERE (task_id = NEW.task_id AND revision = NEW.revision)
          OR event_sequence = NEW.event_sequence
      )
    """,
    "task_effects": """
      EXISTS (
        SELECT 1
        FROM task_effects
        WHERE (message_id = NEW.message_id AND effect_index = NEW.effect_index)
          OR event_sequence = NEW.event_sequence
      )
    """,
    "message_applications": """
      EXISTS (
        SELECT 1
        FROM message_applications
        WHERE message_id = NEW.message_id
          OR applied_event_sequence = NEW.applied_event_sequence
      )
    """,
    "claims": """
      EXISTS (
        SELECT 1
        FROM claims
        WHERE claim_id = NEW.claim_id OR fence = NEW.fence
      )
    """,
    "claim_releases": """
      EXISTS (
        SELECT 1
        FROM claim_releases
        WHERE claim_id = NEW.claim_id OR event_sequence = NEW.event_sequence
      )
    """,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _derived_idempotency_key(
    *,
    source: str,
    session_id: str,
    turn_id: str,
) -> str:
    identity = json.dumps(
        [source, session_id, turn_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return f"session-turn-v1:{digest}"


def _path_id(path: Path) -> str:
    digest = hashlib.sha256(os.fsencode(path)).hexdigest()[:16]
    name = "".join(character if character.isalnum() else "-" for character in path.name)
    return f"{name or 'root'}-{digest}"


def _git_path(
    cwd: Path,
    argument: str,
    *,
    git_executable: str | Path | None = None,
) -> Path:
    command = "git"
    if git_executable is not None:
        raw_executable = os.fspath(git_executable)
        executable = Path(raw_executable)
        if not executable.is_absolute():
            raise JournalError("Git executable must be an absolute path")
        if any(
            character in raw_executable
            for character in ("\0", "\r", "\n")
        ):
            raise JournalError("Git executable path contains control characters")
        try:
            status = executable.stat()
        except OSError as error:
            raise JournalError(
                f"Git is unavailable: {raw_executable}"
            ) from error
        if not stat.S_ISREG(status.st_mode) or not os.access(executable, os.X_OK):
            raise JournalError(
                f"Git is unavailable: not an executable regular file: "
                f"{raw_executable}"
            )
        command = raw_executable

    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("GIT_")
    }
    environment["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            [command, "-C", str(cwd), "rev-parse", argument],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise JournalError(
            "Git is unavailable; repository scope cannot be resolved"
        ) from error
    if result.returncode:
        if "not a git repository" in result.stderr.lower():
            raise _NotGitRepository(f"{cwd} is not inside a Git repository")
        raise JournalError(
            "Git could not resolve repository scope "
            f"(exit status {result.returncode})"
        )
    raw_path = result.stdout.strip()
    if not raw_path:
        raise JournalError("Git returned an empty repository path")
    path = Path(raw_path)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve()


def _state_home() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        state_home = Path(configured)
        if not state_home.is_absolute():
            raise JournalError("XDG_STATE_HOME must be an absolute path")
        return state_home
    return Path.home() / ".local" / "state"


def resolve_scope(
    scope_kind: str = "auto",
    *,
    cwd: Path | None = None,
    agent_root: Path | None = None,
    git_executable: str | Path | None = None,
) -> JournalScope:
    current_directory = (cwd or Path.cwd()).resolve()
    resolved_agent_root = (
        agent_root or Path(__file__).resolve().parent.parent
    ).resolve()

    if scope_kind not in {"auto", "repo", "user", "agent-root"}:
        raise JournalError(f"unsupported journal scope: {scope_kind}")

    if scope_kind in {"auto", "repo"}:
        try:
            common_directory = _git_path(
                current_directory,
                "--git-common-dir",
                git_executable=git_executable,
            )
        except _NotGitRepository:
            if scope_kind == "repo":
                raise
        else:
            journal_path = common_directory / "aiq" / "journal.sqlite3"
            return JournalScope(
                kind="repo",
                root=common_directory,
                scope_id="repo",
                journal_path=journal_path,
                lifecycle_lock_path=_scope_lifecycle_lock_path(journal_path),
            )

    if scope_kind in {"auto", "user"}:
        user_root = _state_home() / "aiq"
        journal_path = user_root / "journal.sqlite3"
        return JournalScope(
            kind="user",
            root=user_root,
            scope_id="user",
            journal_path=journal_path,
            lifecycle_lock_path=_scope_lifecycle_lock_path(journal_path),
        )

    root_id = _path_id(resolved_agent_root)
    state_root = _state_home()
    journal_path = (
        state_root
        / "aiq"
        / "roots"
        / root_id
        / "journal.sqlite3"
    )
    return JournalScope(
        kind="agent-root",
        root=resolved_agent_root,
        scope_id=root_id,
        journal_path=journal_path,
        lifecycle_lock_path=_scope_lifecycle_lock_path(journal_path),
    )


@lru_cache(maxsize=1)
def _require_sqlite_runtime() -> None:
    if sqlite3.sqlite_version_info < SQLITE_MINIMUM_VERSION:
        found = ".".join(str(part) for part in sqlite3.sqlite_version_info)
        required = ".".join(str(part) for part in SQLITE_MINIMUM_VERSION)
        raise JournalError(
            f"SQLite {required} or newer is required; found {found}"
        )

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(":memory:")
        features = connection.execute(
            """
            SELECT
              json_valid('[]'),
              json_type('[]'),
              json_extract('{"value":1}', '$.value')
            """
        ).fetchone()
    except sqlite3.Error as error:
        raise JournalError(
            "SQLite JSON functions are required but unavailable"
        ) from error
    finally:
        if connection is not None:
            connection.close()
    if features != (1, "array", 1):
        raise JournalError("SQLite JSON functions returned incompatible results")


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA synchronous = FULL")


def _enable_wal(connection: sqlite3.Connection) -> None:
    row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
    mode = str(row[0]).lower() if row else ""
    if mode != "wal":
        raise JournalError(
            "SQLite WAL mode is unavailable; journals require a local filesystem"
        )


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    return dict(connection.execute("SELECT key, value FROM journal_metadata"))


def _scope_metadata(scope: JournalScope) -> dict[str, str]:
    if scope.kind == "repo":
        return {
            "scope_kind": "repo",
            "scope_root": ".",
            "scope_id": "repo",
        }
    return {
        "scope_kind": scope.kind,
        "scope_root": str(scope.root),
        "scope_id": scope.scope_id,
    }


def _validate_metadata(
    connection: sqlite3.Connection,
    scope: JournalScope,
    *,
    schema_version: int = SCHEMA_VERSION,
    allow_legacy_repo: bool = False,
) -> None:
    metadata = _metadata(connection)
    expected = {
        "schema_version": str(schema_version),
        **_scope_metadata(scope),
    }
    if allow_legacy_repo and scope.kind == "repo":
        legacy_root = Path(metadata.get("scope_root", ""))
        legacy_id = metadata.get("scope_id")
        if (
            metadata.get("schema_version") == str(schema_version)
            and metadata.get("scope_kind") == "repo"
            and legacy_root.is_absolute()
            and legacy_id == _path_id(legacy_root)
        ):
            return
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise JournalError(
                f"journal metadata mismatch for {key}: "
                f"expected {value!r}, found {metadata.get(key)!r}"
            )


def _normalize_repo_metadata(
    connection: sqlite3.Connection,
    scope: JournalScope,
) -> None:
    if scope.kind != "repo":
        return
    expected = _scope_metadata(scope)
    actual = _metadata(connection)
    updates = (
        (expected[key], key)
        for key in ("scope_root", "scope_id")
        if actual.get(key) != expected[key]
    )
    connection.executemany(
        """
        UPDATE journal_metadata
        SET value = ?
        WHERE key = ?
        """,
        updates,
    )


def _create_v2_schema(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA_V2_STATEMENTS:
        connection.execute(statement)
    for table in APPEND_ONLY_V2_TABLES:
        connection.execute(
            f"""
            CREATE TRIGGER {table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
              SELECT RAISE(ABORT, '{table} is append-only');
            END
            """
        )
        connection.execute(
            f"""
            CREATE TRIGGER {table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
              SELECT RAISE(ABORT, '{table} is append-only');
            END
            """
        )
    for table, conflict_condition in REPLACE_GUARDS.items():
        connection.execute(
            f"""
            CREATE TRIGGER {table}_no_replace
            BEFORE INSERT ON {table}
            WHEN {conflict_condition}
            BEGIN
              SELECT RAISE(ABORT, '{table} is append-only');
            END
            """
        )


def _create_v3_schema(connection: sqlite3.Connection) -> None:
    for statement in SCHEMA_V3_STATEMENTS:
        connection.execute(statement)


def _execute_script_statements(
    connection: sqlite3.Connection,
    script: str,
) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise JournalError("incomplete schema statement")


def _validate_schema_objects(
    connection: sqlite3.Connection,
    *,
    schema_version: int,
) -> None:
    required = {
        ("table", "journal_metadata"),
        ("table", "messages"),
        ("table", "events"),
        ("trigger", "messages_no_update"),
        ("trigger", "messages_no_delete"),
        ("trigger", "events_no_update"),
        ("trigger", "events_no_delete"),
    }
    if schema_version >= 2:
        required.update(
            {
                ("table", table) for table in APPEND_ONLY_V2_TABLES
            }
        )
        required.update(
            {
                ("trigger", f"{table}_no_update")
                for table in APPEND_ONLY_V2_TABLES
            }
        )
        required.update(
            {
                ("trigger", f"{table}_no_delete")
                for table in APPEND_ONLY_V2_TABLES
            }
        )
        required.update(
            {
                ("trigger", f"{table}_no_replace")
                for table in REPLACE_GUARDS
            }
        )
        required.update(
            {
                ("view", "current_tasks"),
                ("trigger", "tasks_validate_insert"),
                ("trigger", "task_effects_validate_insert"),
                ("trigger", "task_revisions_validate_insert"),
                ("trigger", "message_applications_validate_insert"),
                ("trigger", "claims_validate_insert"),
                ("trigger", "claim_releases_validate_insert"),
            }
        )
    if schema_version >= 3:
        required.add(("index", "claims_resource_lookup"))
    actual = {
        (row[0], row[1])
        for row in connection.execute(
            """
            SELECT type, name
            FROM sqlite_master
            WHERE type IN ('table', 'view', 'trigger', 'index')
            """
        )
    }
    missing = sorted(required - actual)
    if missing:
        formatted = ", ".join(f"{kind}:{name}" for kind, name in missing)
        raise JournalError(f"journal schema objects are missing: {formatted}")


def _migration_backup(
    scope: JournalScope,
    *,
    from_version: int,
    to_version: int,
) -> str:
    backup_directory = scope.journal_path.parent / "backups"
    _ensure_private_directory(backup_directory)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = (
        f"pre-migration-v{from_version}-to-v{to_version}-"
        f"{timestamp}-{uuid.uuid4().hex}.sqlite3"
    )
    snapshot_path = backup_directory / name
    temporary_path = backup_directory / f".{name}.tmp"
    source = sqlite3.connect(scope.journal_path, timeout=10)
    destination = sqlite3.connect(temporary_path)
    try:
        source.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise JournalError(
                f"pre-migration snapshot integrity check failed: {integrity}"
            )
        destination.close()
        source.close()
        temporary_path.chmod(0o600)
        temporary_path.replace(snapshot_path)
    finally:
        destination.close()
        source.close()
        if temporary_path.exists():
            temporary_path.unlink()
    return name


def _prune_migration_backups(scope: JournalScope) -> None:
    backup_directory = scope.journal_path.parent / "backups"
    snapshots = sorted(
        backup_directory.glob("pre-migration-v*-to-v*-*.sqlite3"),
        reverse=True,
    )
    for expired_snapshot in snapshots[5:]:
        expired_snapshot.unlink(missing_ok=True)


def _initialize_journal_locked(scope: JournalScope) -> Path:
    original_umask = os.umask(0o077)
    try:
        connection = sqlite3.connect(scope.journal_path, timeout=10)
        try:
            _configure(connection)
            object_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                """
            ).fetchone()[0]
            if object_count == 0:
                _enable_wal(connection)
                _begin_immediate(connection)
                try:
                    _execute_script_statements(connection, SCHEMA_SQL)
                    _create_v2_schema(connection)
                    _create_v3_schema(connection)
                    metadata = {
                        "schema_version": str(SCHEMA_VERSION),
                        **_scope_metadata(scope),
                    }
                    connection.executemany(
                        """
                        INSERT INTO journal_metadata(key, value)
                        VALUES (?, ?)
                        """,
                        metadata.items(),
                    )
                    connection.execute(
                        """
                        INSERT INTO schema_migrations(
                          migration_id,
                          from_version,
                          to_version,
                          migrated_at,
                          backup_name
                        ) VALUES (1, 0, ?, ?, NULL)
                        """,
                        (SCHEMA_VERSION, _utc_now()),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            else:
                try:
                    metadata = _metadata(connection)
                except sqlite3.OperationalError as error:
                    raise JournalError(
                        "existing journal has no readable metadata"
                    ) from error
                raw_version = metadata.get("schema_version")
                try:
                    version = int(raw_version or "")
                except ValueError as error:
                    raise JournalError(
                        f"invalid journal schema version: {raw_version!r}"
                    ) from error
                if version > SCHEMA_VERSION:
                    raise JournalError(
                        f"journal schema {version} is newer than supported "
                        f"schema {SCHEMA_VERSION}"
                    )
                if version < 1:
                    raise JournalError(
                        f"unsupported journal schema version: {version}"
                    )
                _validate_metadata(
                    connection,
                    scope,
                    schema_version=version,
                    allow_legacy_repo=True,
                )
                _validate_schema_objects(
                    connection,
                    schema_version=version,
                )
                _enable_wal(connection)
                if version < SCHEMA_VERSION:
                    _begin_immediate(connection)
                    try:
                        current_version = _metadata(connection).get("schema_version")
                        if current_version != str(version):
                            raise JournalError(
                                "journal schema changed during migration"
                            )
                        backup_name = _migration_backup(
                            scope,
                            from_version=version,
                            to_version=SCHEMA_VERSION,
                        )
                        if version < 2:
                            _create_v2_schema(connection)
                        if version < 3:
                            _create_v3_schema(connection)
                        connection.execute(
                            """
                            INSERT INTO schema_migrations(
                              migration_id,
                              from_version,
                              to_version,
                              migrated_at,
                              backup_name
                            )
                            SELECT COALESCE(MAX(migration_id), 0) + 1, ?, ?, ?, ?
                            FROM schema_migrations
                            """,
                            (version, SCHEMA_VERSION, _utc_now(), backup_name),
                        )
                        cursor = connection.execute(
                            """
                            UPDATE journal_metadata
                            SET value = ?
                            WHERE key = 'schema_version' AND value = ?
                            """,
                            (str(SCHEMA_VERSION), str(version)),
                        )
                        if cursor.rowcount != 1:
                            raise JournalError(
                                "journal schema changed during migration"
                            )
                        _normalize_repo_metadata(connection, scope)
                        _validate_schema_objects(
                            connection,
                            schema_version=SCHEMA_VERSION,
                        )
                        foreign_keys = connection.execute(
                            "PRAGMA foreign_key_check"
                        ).fetchall()
                        if foreign_keys:
                            raise JournalError(
                                "journal foreign-key check failed during migration"
                            )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                    _prune_migration_backups(scope)
                else:
                    expected_scope = _scope_metadata(scope)
                    if (
                        scope.kind == "repo"
                        and (
                            metadata.get("scope_root")
                            != expected_scope["scope_root"]
                            or metadata.get("scope_id")
                            != expected_scope["scope_id"]
                        )
                    ):
                        _begin_immediate(connection)
                        try:
                            _validate_metadata(
                                connection,
                                scope,
                                schema_version=version,
                                allow_legacy_repo=True,
                            )
                            _normalize_repo_metadata(connection, scope)
                            connection.commit()
                        except Exception:
                            connection.rollback()
                            raise
            _validate_metadata(connection, scope)
            _validate_schema_objects(
                connection,
                schema_version=SCHEMA_VERSION,
            )
        finally:
            connection.close()
    finally:
        os.umask(original_umask)

    _chmod_private_file(scope.journal_path)
    return scope.journal_path


def _initialize_journal(scope: JournalScope) -> Path:
    _require_sqlite_runtime()
    _ensure_private_directory(scope.journal_path.parent)
    lock_path = scope.journal_path.parent / "initialization.lock"
    with _open_private_lock(lock_path) as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        if os.path.lexists(scope.journal_path):
            _validate_private_file(scope.journal_path)
        else:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(scope.journal_path, flags, 0o600)
            os.close(descriptor)
        return _initialize_journal_locked(scope)


def initialize_journal(scope: JournalScope) -> Path:
    _require_sqlite_runtime()
    with lifecycle_lock(scope, exclusive=False):
        return _initialize_journal(scope)


class _LifecycleConnection(sqlite3.Connection):
    _aiq_lifecycle_context: Any = None

    def _hold_lifecycle_lock(self, context: Any) -> None:
        self._aiq_lifecycle_context = context

    def close(self) -> None:
        context = self._aiq_lifecycle_context
        self._aiq_lifecycle_context = None
        try:
            super().close()
        finally:
            if context is not None:
                context.__exit__(None, None, None)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _connect(scope: JournalScope) -> sqlite3.Connection:
    _require_sqlite_runtime()
    context = lifecycle_lock(scope, exclusive=False)
    context.__enter__()
    connection: _LifecycleConnection | None = None
    try:
        _initialize_journal(scope)
        connection = sqlite3.connect(
            scope.journal_path,
            timeout=10,
            factory=_LifecycleConnection,
        )
        connection.row_factory = sqlite3.Row
        _configure(connection)
        _validate_metadata(connection, scope)
        connection._hold_lifecycle_lock(context)
        return connection
    except Exception:
        if connection is not None:
            connection.close()
        context.__exit__(None, None, None)
        raise


def _begin_immediate(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as error:
        raise JournalError(
            f"journal write contention: {error}",
            code="contention",
        ) from error


def _canonical_ingest_event(
    content: str,
    *,
    source: str,
    idempotency_key: str | None,
    session_id: str | None,
    turn_id: str | None,
    cwd: str | None,
):
    """Validate one ingest request as a canonical event before storage."""

    from aiq.events import EventError, validate_event

    document: dict[str, Any] = {
        "v": 1,
        "source": source,
        "content": content,
    }
    for name, value in (
        ("idempotency_key", idempotency_key),
        ("session_id", session_id),
        ("turn_id", turn_id),
        ("cwd", cwd),
    ):
        if value is not None:
            document[name] = value
    try:
        return validate_event(document)
    except EventError as error:
        raise JournalError(str(error)) from error


def _ingest_connected(
    connection: sqlite3.Connection,
    scope: JournalScope,
    content: str,
    *,
    source: str = "user",
    idempotency_key: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    cwd: str | None = None,
    if_new: bool = False,
) -> IngestResult:
    """Validate and store one message inside an already-open transaction.

    The caller owns the connection, the surrounding transaction, and the
    commit or rollback. Composed transactional commands reuse this so one
    database transaction can persist a message, claim it, and apply its
    effects atomically.
    """

    event = _canonical_ingest_event(
        content,
        source=source,
        idempotency_key=idempotency_key,
        session_id=session_id,
        turn_id=turn_id,
        cwd=cwd,
    )

    content = event.content
    source = event.source
    idempotency_key = event.idempotency_key
    session_id = event.session_id
    turn_id = event.turn_id
    cwd = event.cwd

    content_hash = hashlib.sha256(content.encode()).hexdigest()
    effective_key = idempotency_key
    if effective_key is None and session_id and turn_id:
        effective_key = _derived_idempotency_key(
            source=source,
            session_id=session_id,
            turn_id=turn_id,
        )

    if if_new:
        duplicate = connection.execute(
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
              m.message_id,
              lifecycle.event_type AS state_event_type,
              received.event_id,
              received.sequence
            FROM messages AS m
            JOIN lifecycle
              ON lifecycle.message_id = m.message_id
             AND lifecycle.rank = 1
            JOIN events AS received
              ON received.sequence = (
                SELECT MIN(sequence)
                FROM events
                WHERE message_id = m.message_id
                  AND event_type = 'message.received'
              )
            WHERE m.content_sha256 = ?
              AND m.content = ?
              AND lifecycle.event_type IN (
                'message.received',
                'message.needs_input'
              )
            ORDER BY received.sequence
            LIMIT 1
            """,
            (content_hash, content),
        ).fetchone()
        if duplicate:
            return IngestResult(
                message_id=duplicate["message_id"],
                event_id=duplicate["event_id"],
                sequence=duplicate["sequence"],
                state=duplicate["state_event_type"].removeprefix("message."),
                created=False,
                scope=scope,
                deduped=True,
            )

    if effective_key:
        existing = connection.execute(
            f"""
            SELECT
              m.message_id,
              m.content_sha256,
              m.source,
              m.session_id,
              m.turn_id,
              m.cwd,
              e.event_id,
              e.sequence,
              (
                SELECT state.event_type
                FROM events AS state
                WHERE state.message_id = m.message_id
                  AND state.event_type IN ({MESSAGE_LIFECYCLE_EVENT_SQL})
                ORDER BY state.sequence DESC
                LIMIT 1
              ) AS state_event_type
            FROM messages AS m
            JOIN events AS e
              ON e.message_id = m.message_id
             AND e.event_type = 'message.received'
            WHERE m.idempotency_key = ?
            ORDER BY e.sequence ASC
            LIMIT 1
            """,
            (effective_key,),
        ).fetchone()
        if existing:
            existing_identity = (
                existing["content_sha256"],
                existing["source"],
                existing["session_id"],
                existing["turn_id"],
                existing["cwd"],
            )
            requested_identity = (
                content_hash,
                source,
                session_id,
                turn_id,
                cwd,
            )
            if existing_identity != requested_identity:
                raise JournalError(
                    "idempotency key already belongs to a different "
                    "message identity",
                    code="state_conflict",
                )
            return IngestResult(
                message_id=existing["message_id"],
                event_id=existing["event_id"],
                sequence=existing["sequence"],
                state=existing["state_event_type"].removeprefix("message."),
                created=False,
                scope=scope,
            )

    received_at = _utc_now()
    message_id = _identifier("msg")
    event_id = _identifier("evt")
    connection.execute(
        """
        INSERT INTO messages(
          message_id,
          received_at,
          source,
          content,
          content_sha256,
          idempotency_key,
          session_id,
          turn_id,
          cwd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            received_at,
            source,
            content,
            content_hash,
            effective_key,
            session_id,
            turn_id,
            cwd,
        ),
    )
    cursor = connection.execute(
        """
        INSERT INTO events(
          event_id,
          occurred_at,
          event_type,
          message_id,
          payload_json
        ) VALUES (?, ?, 'message.received', ?, ?)
        """,
        (event_id, received_at, message_id, "{}"),
    )
    return IngestResult(
        message_id=message_id,
        event_id=event_id,
        sequence=cursor.lastrowid,
        state="received",
        created=True,
        scope=scope,
    )


def ingest_message(
    scope: JournalScope,
    content: str,
    *,
    source: str = "user",
    idempotency_key: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    cwd: str | None = None,
    if_new: bool = False,
) -> IngestResult:
    # Reject noncanonical input before any storage exists or mutates.
    _canonical_ingest_event(
        content,
        source=source,
        idempotency_key=idempotency_key,
        session_id=session_id,
        turn_id=turn_id,
        cwd=cwd,
    )
    connection = _connect(scope)
    try:
        _begin_immediate(connection)
        result = _ingest_connected(
            connection,
            scope,
            content,
            source=source,
            idempotency_key=idempotency_key,
            session_id=session_id,
            turn_id=turn_id,
            cwd=cwd,
            if_new=if_new,
        )
        connection.commit()
        return result
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise JournalError(f"journal integrity violation: {error}") from error
    except sqlite3.OperationalError as error:
        connection.rollback()
        raise JournalError(
            f"journal write contention: {error}",
            code="contention",
        ) from error
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def find_message_by_idempotency_key(
    scope: JournalScope,
    idempotency_key: str,
) -> dict[str, Any] | None:
    """Return the first stored message for one idempotency key, if any."""

    if not scope.journal_path.exists():
        return None
    connection = _connect(scope)
    try:
        row = connection.execute(
            f"""
            SELECT
              m.message_id,
              (
                SELECT state.event_type
                FROM events AS state
                WHERE state.message_id = m.message_id
                  AND state.event_type IN ({MESSAGE_LIFECYCLE_EVENT_SQL})
                ORDER BY state.sequence DESC
                LIMIT 1
              ) AS state_event_type
            FROM messages AS m
            JOIN events AS received
              ON received.message_id = m.message_id
             AND received.event_type = 'message.received'
            WHERE m.idempotency_key = ?
            ORDER BY received.sequence ASC
            LIMIT 1
            """,
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return {
            "message_id": row["message_id"],
            "state": row["state_event_type"].removeprefix("message."),
        }
    finally:
        connection.close()


def list_inbox(
    scope: JournalScope,
    *,
    limit: int = 20,
    include_content: bool = False,
) -> list[dict[str, Any]]:
    if limit < 1:
        raise JournalError("inbox limit must be positive")
    if not scope.journal_path.exists():
        return []
    connection = _connect(scope)
    try:
        content_column = ", m.content" if include_content else ""
        rows = connection.execute(
            f"""
            WITH latest AS (
              SELECT
                event_id,
                sequence,
                event_type,
                message_id,
                ROW_NUMBER() OVER (
                  PARTITION BY message_id
                  ORDER BY sequence DESC
                ) AS rank
              FROM events
              WHERE message_id IS NOT NULL
                AND event_type IN ({MESSAGE_LIFECYCLE_EVENT_SQL})
            )
            SELECT
              m.message_id,
              m.received_at,
              m.source,
              m.content_sha256,
              m.session_id,
              m.turn_id,
              m.cwd,
              latest.event_type,
              latest.sequence,
              CASE WHEN
                latest.event_type = 'message.processing'
                AND EXISTS (
                  SELECT 1
                  FROM claims AS claim
                  LEFT JOIN claim_releases AS release
                    ON release.claim_id = claim.claim_id
                  WHERE claim.resource_kind = 'message'
                    AND claim.resource_id = m.message_id
                    AND release.claim_id IS NULL
                    AND claim.expires_at_us <= ?
                )
              THEN 1 ELSE 0 END AS claim_expired
              {content_column}
            FROM messages AS m
            JOIN latest
              ON latest.message_id = m.message_id
             AND latest.rank = 1
            WHERE latest.event_type IN (
              'message.received',
              'message.processing',
              'message.needs_input',
              'message.failed'
            )
            ORDER BY latest.sequence
            LIMIT ?
            """,
            (time.time_ns() // 1000, limit),
        ).fetchall()
        return [
            {
                **dict(row),
                "state": (
                    "received"
                    if row["claim_expired"]
                    else row["event_type"].removeprefix("message.")
                ),
                "lease_status": (
                    "expired"
                    if row["claim_expired"]
                    else "active"
                    if row["event_type"] == "message.processing"
                    else None
                ),
            }
            for row in rows
        ]
    finally:
        connection.close()


def create_snapshot(
    scope: JournalScope,
    *,
    keep: int = 5,
) -> dict[str, Any]:
    if keep < 1:
        raise JournalError("snapshot retention must be positive")
    if not scope.journal_path.exists():
        raise JournalError(
            f"journal does not exist: {scope.journal_path}",
            code="not_found",
        )

    with lifecycle_lock(scope, exclusive=False):
        _check_journal_locked(scope)
        backup_directory = scope.journal_path.parent / "backups"
        _ensure_private_directory(backup_directory)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        snapshot_path = (
            backup_directory
            / f"journal-{timestamp}-{uuid.uuid4().hex}.sqlite3"
        )
        temporary_path = backup_directory / f".{snapshot_path.name}.tmp"

        original_umask = os.umask(0o077)
        try:
            source = sqlite3.connect(scope.journal_path, timeout=10)
            destination = sqlite3.connect(temporary_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            temporary_path.chmod(0o600)
            temporary_path.replace(snapshot_path)
        finally:
            os.umask(original_umask)
            if temporary_path.exists():
                temporary_path.unlink()

        snapshots = sorted(
            backup_directory.glob("journal-*.sqlite3"),
            reverse=True,
        )
        removed: list[str] = []
        for expired_snapshot in snapshots[keep:]:
            expired_snapshot.unlink(missing_ok=True)
            removed.append(str(expired_snapshot))

    return {
        "status": "created",
        "snapshot_path": str(snapshot_path),
        "removed": removed,
        "retained": min(len(snapshots), keep),
        "scope": scope.to_dict(),
    }


def _check_journal_locked(scope: JournalScope) -> dict[str, Any]:
    if not scope.journal_path.exists():
        raise JournalError(
            f"journal does not exist: {scope.journal_path}",
            code="not_found",
        )
    _initialize_journal(scope)
    connection = sqlite3.connect(scope.journal_path, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        _configure(connection)
        _validate_metadata(connection, scope)
        _validate_schema_objects(
            connection,
            schema_version=SCHEMA_VERSION,
        )
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise JournalError(f"SQLite integrity check failed: {integrity}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise JournalError("SQLite foreign-key check failed")
        message_count = connection.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0]
        event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        task_count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        for message in connection.execute(
            """
            SELECT message_id, content, content_sha256
            FROM messages
            """
        ):
            actual_hash = hashlib.sha256(message["content"].encode()).hexdigest()
            if actual_hash != message["content_sha256"]:
                raise JournalError(
                    f"message content hash mismatch: {message['message_id']}"
                )
            received_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM events
                WHERE message_id = ?
                  AND event_type = 'message.received'
                  AND payload_json = '{}'
                """,
                (message["message_id"],),
            ).fetchone()[0]
            if received_count != 1:
                raise JournalError(
                    f"message received event count is {received_count}: "
                    f"{message['message_id']}"
                )
        from aiq.queue import audit_queue

        queue_audit = audit_queue(connection)
    finally:
        connection.close()

    mode = stat.S_IMODE(scope.journal_path.stat().st_mode)
    if mode != 0o600:
        raise JournalError(
            f"journal permissions are {mode:04o}; expected 0600"
        )

    return {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "messages": message_count,
        "events": event_count,
        "tasks": task_count,
        "applications": queue_audit["applications"],
        "claims": queue_audit["claims"],
        "snapshots": len(
            list((scope.journal_path.parent / "backups").glob("journal-*.sqlite3"))
        ),
        "scope": scope.to_dict(),
    }


def check_journal(scope: JournalScope) -> dict[str, Any]:
    with lifecycle_lock(scope, exclusive=False):
        return _check_journal_locked(scope)
