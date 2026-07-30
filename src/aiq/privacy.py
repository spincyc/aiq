from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from typing import Any, BinaryIO, Iterator

from aiq import journal
from aiq.journal import JournalError, JournalScope


EXPORT_FORMAT = "aiq-journal-jsonl"
EXPORT_FORMAT_VERSION = 1

_EXPORT_RECORDS = (
    ("message", "messages", ("message_id",), {}),
    ("event", "events", ("sequence",), {"payload_json": "payload"}),
    ("task", "tasks", ("task_id",), {}),
    (
        "task_revision",
        "task_revisions",
        ("task_id", "revision"),
        {"dependencies_json": "dependencies"},
    ),
    (
        "task_effect",
        "task_effects",
        ("message_id", "effect_index"),
        {"payload_json": "payload"},
    ),
    (
        "message_application",
        "message_applications",
        ("message_id",),
        {
            "document_json": "document",
            "result_json": "result",
        },
    ),
    ("claim", "claims", ("claim_id",), {}),
    ("claim_release", "claim_releases", ("claim_id",), {}),
)
# Stored columns that carry no semantic history and are therefore never
# exported. `task_number` is allocator state. A claim's holder locator is
# the host and POSIX session id of the process that took it: live
# coordination state, excluded for the same reason the whole reader lease
# is, so an export still names no host and no session id.
_EXPORT_EXCLUDED_COLUMNS = {
    "task": ("task_number",),
    "claim": ("holder_host", "holder_sid"),
}
_SCHEMA_V2_TABLE_NAMES = frozenset(
    {
        "journal_metadata",
        "reader_leases",
        "schema_migrations",
        "task_numbers",
        *(table for _, table, _, _ in _EXPORT_RECORDS),
    }
)

_TIMESTAMP_PATTERN = r"[0-9]{8}T[0-9]{12}Z"
_IDENTIFIER_PATTERN = r"[0-9a-f]{32}"
_BACKUP_NAME = re.compile(
    rf"(?:"
    rf"journal-{_TIMESTAMP_PATTERN}-{_IDENTIFIER_PATTERN}\.sqlite3"
    rf"|pre-migration-v[0-9]+-to-v[0-9]+-"
    rf"{_TIMESTAMP_PATTERN}-{_IDENTIFIER_PATTERN}\.sqlite3"
    rf")"
)
_BACKUP_TEMP_NAME = re.compile(rf"\.{_BACKUP_NAME.pattern}\.tmp")


def _canonical_line(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _write_hashed(
    output: BinaryIO,
    digest: Any,
    value: dict[str, Any],
) -> int:
    encoded = _canonical_line(value)
    output.write(encoded)
    digest.update(encoded)
    return len(encoded)


def _quote_identifier(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise JournalError(
            f"invalid export identifier: {identifier}",
            code="state_conflict",
        )
    return f'"{identifier}"'


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve(strict=True).as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _export_rows(
    connection: sqlite3.Connection,
) -> Iterator[tuple[str, dict[str, Any]]]:
    actual_tables = {
        row[0]
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_schema
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            """
        )
    }
    if actual_tables != _SCHEMA_V2_TABLE_NAMES:
        missing = sorted(_SCHEMA_V2_TABLE_NAMES - actual_tables)
        unexpected = sorted(actual_tables - _SCHEMA_V2_TABLE_NAMES)
        raise JournalError(
            "journal tables do not match export format "
            f"(missing={missing}, unexpected={unexpected})",
            code="integrity_failed",
        )

    for record_type, table, order_columns, json_columns in _EXPORT_RECORDS:
        quoted_table = _quote_identifier(table)
        order = ", ".join(_quote_identifier(column) for column in order_columns)
        for row in connection.execute(
            f"SELECT * FROM {quoted_table} ORDER BY {order}"
        ):
            semantic_row = dict(row)
            for excluded in _EXPORT_EXCLUDED_COLUMNS.get(record_type, ()):
                del semantic_row[excluded]
            for stored_name, public_name in json_columns.items():
                raw_value = semantic_row.pop(stored_name)
                try:
                    semantic_row[public_name] = json.loads(raw_value)
                except (json.JSONDecodeError, TypeError) as error:
                    raise JournalError(
                        f"journal contains invalid JSON in "
                        f"{table}.{stored_name}",
                        code="integrity_failed",
                    ) from error
            yield record_type, semantic_row


def _validated_output_path(scope: JournalScope, output_path: Path) -> Path:
    requested = output_path.expanduser()
    if requested.name in {"", ".", ".."}:
        raise JournalError(
            "export output must name a file",
            code="invalid_argument",
        )
    parent = requested.parent.resolve(strict=True)
    if not parent.is_dir():
        raise JournalError(
            f"export parent is not a directory: {parent}",
            code="invalid_argument",
        )
    target = parent / requested.name
    if os.path.lexists(target):
        raise JournalError(
            f"export output already exists: {target}",
            code="state_conflict",
        )

    journal_directory = scope.journal_path.parent.resolve(strict=False)
    try:
        target.relative_to(journal_directory)
    except ValueError:
        pass
    else:
        raise JournalError(
            "export output must be outside managed journal state",
            code="invalid_argument",
        )
    return target


def _publish_new_file(temporary_path: Path, output_path: Path) -> None:
    published = False
    try:
        os.link(
            temporary_path,
            output_path,
            follow_symlinks=False,
        )
        published = True
        temporary_path.unlink()
        _fsync_directory(output_path.parent)
    except FileExistsError as error:
        raise JournalError(
            f"export output already exists: {output_path}",
            code="state_conflict",
        ) from error
    except OSError as error:
        if published:
            output_path.unlink(missing_ok=True)
            try:
                _fsync_directory(output_path.parent)
            except OSError:
                pass
        raise JournalError(
            f"cannot publish export: {output_path}",
            code="io_error",
        ) from error


def export_journal(
    scope: JournalScope,
    output_path: Path,
) -> dict[str, Any]:
    """Write a complete deterministic JSON Lines export to a new private file."""
    target = _validated_output_path(scope, output_path)

    temporary_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary_path = Path(temporary_name)
    os.fchmod(temporary_descriptor, 0o600)
    record_count = 0
    byte_count = 0
    counts: dict[str, int] = {}
    digest = hashlib.sha256()

    try:
        with os.fdopen(temporary_descriptor, "wb") as output:
            with journal.lifecycle_lock(scope, exclusive=False):
                if not os.path.lexists(scope.journal_path):
                    raise JournalError(
                        f"journal does not exist: {scope.journal_path}",
                        code="not_found",
                    )
                journal._validate_private_file(scope.journal_path)
                connection = _readonly_connection(scope.journal_path)
                try:
                    connection.execute("BEGIN")
                    integrity = connection.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()[0]
                    if integrity != "ok":
                        raise JournalError(
                            f"SQLite integrity check failed: {integrity}",
                            code="integrity_failed",
                        )
                    if connection.execute("PRAGMA foreign_key_check").fetchall():
                        raise JournalError(
                            "SQLite foreign-key check failed",
                            code="integrity_failed",
                        )

                    metadata = dict(
                        connection.execute(
                            "SELECT key, value FROM journal_metadata"
                        )
                    )
                    raw_schema_version = metadata.get("schema_version")
                    try:
                        schema_version = int(raw_schema_version or "")
                    except ValueError as error:
                        raise JournalError(
                            "journal has an invalid schema version",
                            code="integrity_failed",
                        ) from error
                    if schema_version != journal.SCHEMA_VERSION:
                        raise JournalError(
                            f"journal schema {schema_version} is unsupported "
                            f"by export format {EXPORT_FORMAT_VERSION}",
                            code="schema_incompatible",
                        )

                    byte_count += _write_hashed(
                        output,
                        digest,
                        {
                            "content": "full",
                            "format": EXPORT_FORMAT,
                            "format_version": EXPORT_FORMAT_VERSION,
                            "media_type": "application/x-ndjson",
                            "sensitive": True,
                            "type": "header",
                        },
                    )
                    for record_type, row in _export_rows(connection):
                        byte_count += _write_hashed(
                            output,
                            digest,
                            {
                                "row": row,
                                "type": "record",
                                "record_type": record_type,
                            },
                        )
                        counts[record_type] = counts.get(record_type, 0) + 1
                        record_count += 1
                    connection.rollback()
                finally:
                    connection.close()

            content_sha256 = digest.hexdigest()
            footer = _canonical_line(
                {
                    "content_sha256": content_sha256,
                    "counts": {
                        record_type: counts.get(record_type, 0)
                        for record_type, _, _, _ in _EXPORT_RECORDS
                    },
                    "records": record_count,
                    "type": "manifest",
                }
            )
            output.write(footer)
            byte_count += len(footer)
            output.flush()
            os.fsync(output.fileno())

        _publish_new_file(temporary_path, target)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    return {
        "bytes": byte_count,
        "content_sha256": content_sha256,
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "output_path": str(target),
        "records": record_count,
        "scope": scope.to_dict(),
        "status": "exported",
    }


def _private_directory(path: Path, *, label: str) -> os.stat_result | None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISDIR(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or status.st_uid != os.getuid()
        or stat.S_IMODE(status.st_mode) != 0o700
    ):
        raise JournalError(
            f"{label} is not a private directory: {path}",
            code="io_error",
        )
    return status


def _owned_regular_file(path: Path) -> os.stat_result:
    status = path.lstat()
    if (
        not stat.S_ISREG(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or status.st_uid != os.getuid()
        or status.st_nlink != 1
        or stat.S_IMODE(status.st_mode) != 0o600
    ):
        raise JournalError(
            f"managed journal path is unsafe: {path}",
            code="io_error",
        )
    return status


def _is_backup_name(name: str) -> bool:
    return bool(
        _BACKUP_NAME.fullmatch(name)
        or _BACKUP_TEMP_NAME.fullmatch(name)
    )


@dataclass(frozen=True)
class _ManagedEntry:
    kind: str
    path: Path
    status: os.stat_result


def _managed_inventory(scope: JournalScope) -> list[_ManagedEntry]:
    journal_directory = scope.journal_path.parent
    _private_directory(journal_directory, label="journal directory")

    inventory: list[_ManagedEntry] = []
    for kind, path in (
        ("journal", scope.journal_path),
        ("wal", Path(f"{scope.journal_path}-wal")),
        ("shm", Path(f"{scope.journal_path}-shm")),
        ("rollback_journal", Path(f"{scope.journal_path}-journal")),
    ):
        if os.path.lexists(path):
            inventory.append(
                _ManagedEntry(kind, path, _owned_regular_file(path))
            )

    backup_directory = journal_directory / "backups"
    if os.path.lexists(backup_directory):
        backup_status = _private_directory(
            backup_directory,
            label="backup directory",
        )
        assert backup_status is not None
        inventory.append(
            _ManagedEntry(
                "backup_directory",
                backup_directory,
                backup_status,
            )
        )
        for path in sorted(backup_directory.iterdir(), key=lambda item: item.name):
            if not _is_backup_name(path.name):
                raise JournalError(
                    f"backup directory contains an unmanaged entry: {path}",
                    code="io_error",
                )
            kind = (
                "backup_temporary"
                if _BACKUP_TEMP_NAME.fullmatch(path.name)
                else "migration_backup"
                if path.name.startswith("pre-migration-")
                else "snapshot"
            )
            inventory.append(
                _ManagedEntry(kind, path, _owned_regular_file(path))
            )
    return sorted(
        inventory,
        key=lambda entry: str(entry.path.relative_to(journal_directory)),
    )


def _inventory_token(
    scope: JournalScope,
    inventory: list[_ManagedEntry],
) -> str:
    description = {
        "files": [
            {
                "changed_ns": entry.status.st_ctime_ns,
                "device": entry.status.st_dev,
                "inode": entry.status.st_ino,
                "kind": entry.kind,
                "mode": stat.S_IMODE(entry.status.st_mode),
                "modified_ns": entry.status.st_mtime_ns,
                "path": str(
                    entry.path.relative_to(scope.journal_path.parent)
                ),
                "size": entry.status.st_size,
            }
            for entry in inventory
        ],
        "scope": {
            "kind": scope.kind,
            "scope_id": scope.scope_id,
            "journal_path": str(scope.journal_path),
        },
    }
    encoded = json.dumps(
        description,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _destroy_plan_locked(scope: JournalScope) -> dict[str, Any]:
    inventory = _managed_inventory(scope)
    backup_count = sum(
        1
        for entry in inventory
        if entry.kind in {"snapshot", "migration_backup"}
    )
    total_bytes = sum(
        entry.status.st_size
        for entry in inventory
        if entry.kind != "backup_directory"
    )
    return {
        "confirmation_token": _inventory_token(scope, inventory),
        "files": sum(
            entry.kind != "backup_directory" for entry in inventory
        ),
        "journal_present": any(
            entry.kind == "journal" for entry in inventory
        ),
        "managed_backups": backup_count,
        "scope": scope.to_dict(),
        "status": "confirmation_required" if inventory else "already_absent",
        "targets": [
            {
                "kind": entry.kind,
                "path": str(
                    entry.path.relative_to(scope.journal_path.parent)
                ),
                "size": (
                    None
                    if entry.kind == "backup_directory"
                    else entry.status.st_size
                ),
            }
            for entry in inventory
        ],
        "total_bytes": total_bytes,
    }


def plan_journal_destroy(scope: JournalScope) -> dict[str, Any]:
    """Describe managed data and return a state-bound confirmation token."""
    with journal.lifecycle_lock(scope, exclusive=False):
        return _destroy_plan_locked(scope)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_private_directory(
    path: Path | str,
    *,
    label: str,
    directory_descriptor: int | None = None,
) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(
            path,
            flags,
            dir_fd=directory_descriptor,
        )
    except OSError as error:
        raise JournalError(
            f"{label} is not a private directory: {path}",
            code="io_error",
        ) from error
    status = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.getuid()
        or stat.S_IMODE(status.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise JournalError(
            f"{label} is not a private directory: {path}",
            code="io_error",
        )
    return descriptor


def _same_entry(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        first.st_mode,
        first.st_uid,
        first.st_nlink,
        first.st_size,
        first.st_mtime_ns,
        first.st_ctime_ns,
    ) == (
        second.st_dev,
        second.st_ino,
        second.st_mode,
        second.st_uid,
        second.st_nlink,
        second.st_size,
        second.st_mtime_ns,
        second.st_ctime_ns,
    )


def _same_directory(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        first.st_mode,
        first.st_uid,
    ) == (
        second.st_dev,
        second.st_ino,
        second.st_mode,
        second.st_uid,
    )


def _unlink_unchanged(
    entry: _ManagedEntry,
    *,
    directory_descriptor: int,
) -> None:
    try:
        current = os.stat(
            entry.path.name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise JournalError(
            f"managed journal path changed after confirmation: {entry.path}",
            code="state_conflict",
        ) from error
    if not _same_entry(entry.status, current):
        raise JournalError(
            f"managed journal path changed after confirmation: {entry.path}",
            code="state_conflict",
        )
    os.unlink(entry.path.name, dir_fd=directory_descriptor)


def _delete_inventory(
    scope: JournalScope,
    inventory: list[_ManagedEntry],
) -> None:
    journal_descriptor = _open_private_directory(
        scope.journal_path.parent,
        label="journal directory",
    )
    backup_descriptor: int | None = None
    try:
        backup_entry = next(
            (
                entry
                for entry in inventory
                if entry.kind == "backup_directory"
            ),
            None,
        )
        if backup_entry is not None:
            backup_descriptor = _open_private_directory(
                "backups",
                label="backup directory",
                directory_descriptor=journal_descriptor,
            )
            if not _same_directory(
                backup_entry.status,
                os.fstat(backup_descriptor),
            ):
                raise JournalError(
                    "backup directory changed after confirmation",
                    code="state_conflict",
                )

        for entry in inventory:
            if entry.kind == "backup_directory":
                continue
            descriptor = (
                backup_descriptor
                if entry.path.parent
                == scope.journal_path.parent / "backups"
                else journal_descriptor
            )
            assert descriptor is not None
            _unlink_unchanged(entry, directory_descriptor=descriptor)

        if backup_descriptor is not None:
            os.fsync(backup_descriptor)
            os.close(backup_descriptor)
            backup_descriptor = None
            current_backup = os.stat(
                "backups",
                dir_fd=journal_descriptor,
                follow_symlinks=False,
            )
            assert backup_entry is not None
            if not _same_directory(backup_entry.status, current_backup):
                raise JournalError(
                    "backup directory changed after confirmation",
                    code="state_conflict",
                )
            os.rmdir("backups", dir_fd=journal_descriptor)
        os.fsync(journal_descriptor)
    finally:
        if backup_descriptor is not None:
            os.close(backup_descriptor)
        os.close(journal_descriptor)


def destroy_journal(
    scope: JournalScope,
    confirmation_token: str,
) -> dict[str, Any]:
    """Destroy only validated AIQ-managed journal data and retained backups."""
    with journal.lifecycle_lock(scope, exclusive=True):
        inventory = _managed_inventory(scope)
        if not inventory:
            return {
                "deleted_files": 0,
                "scope": scope.to_dict(),
                "status": "already_absent",
            }
        expected_token = _inventory_token(scope, inventory)
        if not confirmation_token or not hmac.compare_digest(
            confirmation_token,
            expected_token,
        ):
            raise JournalError(
                "journal destroy confirmation is missing, invalid, or stale",
                code="state_conflict",
            )

        deleted_files = sum(
            entry.kind != "backup_directory" for entry in inventory
        )
        _delete_inventory(scope, inventory)

        return {
            "deleted_files": deleted_files,
            "scope": scope.to_dict(),
            "status": "destroyed",
        }
