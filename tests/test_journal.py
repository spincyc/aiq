from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from support import run_git
from aiq import journal as journal_module
from aiq.journal import (
    JournalError,
    PROJECT_LABEL_MAX_LENGTH,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    check_journal,
    create_snapshot,
    default_project_label,
    ingest_message,
    initialize_journal,
    list_inbox,
    project_label,
    resolve_scope,
    validate_project_label,
)
from aiq.queue import read_status


class JournalTest(unittest.TestCase):
    def agent_scope(self, temporary_root: Path):
        state_home = temporary_root / "state"
        agent_root = temporary_root / "agent-root"
        agent_root.mkdir()
        environment = {"XDG_STATE_HOME": str(state_home)}
        with patch.dict(os.environ, environment):
            return resolve_scope(
                "agent-root",
                cwd=temporary_root,
                agent_root=agent_root,
            )

    def test_repo_scope_is_shared_by_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            worktree = root / "worktree"
            repository.mkdir()
            run_git(repository, "init", "-b", "main")
            run_git(repository, "config", "user.name", "AIQ Test")
            run_git(repository, "config", "user.email", "aiq@example.invalid")
            (repository / "tracked").write_text("initial\n")
            run_git(repository, "add", "tracked")
            run_git(repository, "commit", "-m", "Initial")
            run_git(repository, "worktree", "add", "-b", "task", str(worktree), "main")

            primary_scope = resolve_scope("repo", cwd=repository)
            worktree_scope = resolve_scope("repo", cwd=worktree)

            self.assertEqual(primary_scope.journal_path, worktree_scope.journal_path)
            self.assertEqual(
                primary_scope.journal_path,
                (repository / ".git" / "aiq" / "journal.sqlite3").resolve(),
            )

    def test_repo_journal_survives_move_and_normalizes_legacy_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            moved_repository = root / "moved repository"
            repository.mkdir()
            run_git(repository, "init", "-b", "main")

            original_scope = resolve_scope("repo", cwd=repository)
            message = ingest_message(
                original_scope,
                "survive repository move",
                cwd=str(repository),
            )
            connection = sqlite3.connect(original_scope.journal_path)
            try:
                connection.execute(
                    """
                    UPDATE journal_metadata
                    SET value = ?
                    WHERE key = 'scope_root'
                    """,
                    (str(original_scope.root),),
                )
                connection.execute(
                    """
                    UPDATE journal_metadata
                    SET value = ?
                    WHERE key = 'scope_id'
                    """,
                    (journal_module._path_id(original_scope.root),),
                )
                connection.commit()
            finally:
                connection.close()

            repository.rename(moved_repository)
            moved_scope = resolve_scope("repo", cwd=moved_repository)

            self.assertEqual(check_journal(moved_scope)["messages"], 1)
            self.assertEqual(
                list_inbox(moved_scope)[0]["message_id"],
                message.message_id,
            )
            metadata_connection = sqlite3.connect(moved_scope.journal_path)
            try:
                metadata = dict(
                    metadata_connection.execute(
                        "SELECT key, value FROM journal_metadata"
                    )
                )
            finally:
                metadata_connection.close()
            self.assertEqual(metadata["scope_root"], ".")
            self.assertEqual(metadata["scope_id"], "repo")

    def test_git_environment_cannot_redirect_repo_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            redirected = root / "redirected"
            repository.mkdir()
            redirected.mkdir()
            run_git(repository, "init", "-b", "main")
            run_git(redirected, "init", "-b", "main")

            with patch.dict(
                os.environ,
                {
                    "GIT_DIR": str(redirected / ".git"),
                    "GIT_WORK_TREE": str(redirected),
                },
            ):
                scope = resolve_scope("repo", cwd=repository)

            self.assertEqual(scope.root, (repository / ".git").resolve())

    def test_explicit_git_ignores_empty_or_hostile_path(self) -> None:
        discovered_git = shutil.which("git")
        self.assertIsNotNone(discovered_git)
        git_executable = Path(discovered_git)
        self.assertTrue(git_executable.is_absolute())

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            hostile_bin = root / "hostile-bin"
            repository.mkdir()
            hostile_bin.mkdir()
            run_git(repository, "init", "-b", "main")
            hostile_git = hostile_bin / "git"
            hostile_git.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'hostile Git was invoked' >&2\n"
                "exit 99\n",
            )
            hostile_git.chmod(0o700)

            for path in ("", str(hostile_bin)):
                with self.subTest(path=path):
                    with patch.dict(os.environ, {"PATH": path}):
                        scope = resolve_scope(
                            "repo",
                            cwd=repository,
                            git_executable=git_executable,
                        )
                    self.assertEqual(
                        scope.root,
                        (repository / ".git").resolve(),
                    )

    def test_explicit_git_path_is_validated_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            unavailable = root / "missing-git"
            non_executable = root / "non-executable-git"
            non_executable.write_text("#!/bin/sh\nexit 0\n")

            with self.assertRaisesRegex(JournalError, "absolute path"):
                resolve_scope(
                    "auto",
                    cwd=root,
                    git_executable="git",
                )
            for invalid in (unavailable, non_executable, root):
                with self.subTest(git_executable=invalid):
                    with self.assertRaisesRegex(
                        JournalError,
                        "Git is unavailable",
                    ):
                        resolve_scope(
                            "auto",
                            cwd=root,
                            git_executable=invalid,
                        )

    def test_missing_git_is_fail_closed_for_automatic_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_home = root / "state"
            with (
                patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}),
                patch.object(
                    journal_module.subprocess,
                    "run",
                    side_effect=FileNotFoundError("git"),
                ),
            ):
                with self.assertRaisesRegex(JournalError, "Git is unavailable"):
                    resolve_scope("auto", cwd=root)
                with self.assertRaisesRegex(JournalError, "Git is unavailable"):
                    resolve_scope("repo", cwd=root)
                scope = resolve_scope("user", cwd=root)

            self.assertEqual(scope.kind, "user")
            self.assertEqual(scope.scope_id, "user")
            self.assertEqual(
                scope.journal_path,
                state_home / "aiq" / "journal.sqlite3",
            )

    def test_unexpected_git_failure_is_not_treated_as_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            failure = subprocess.CompletedProcess(
                args=["git"],
                returncode=128,
                stdout="",
                stderr="fatal: detected dubious ownership in repository",
            )
            with patch.object(
                journal_module.subprocess,
                "run",
                return_value=failure,
            ):
                with self.assertRaisesRegex(
                    JournalError,
                    "Git could not resolve repository scope",
                ):
                    resolve_scope("auto", cwd=root)

    def test_auto_scope_outside_repo_uses_stable_user_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_home = root / "state"
            with patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}):
                scope = resolve_scope("auto", cwd=root)

            self.assertEqual(scope.kind, "user")
            self.assertEqual(scope.root, state_home / "aiq")
            self.assertEqual(scope.journal_path, scope.root / "journal.sqlite3")

    def test_agent_root_scope_uses_xdg_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)

            self.assertEqual(scope.kind, "agent-root")
            self.assertTrue(
                scope.journal_path.is_relative_to(root / "state" / "aiq" / "roots")
            )

    def test_ingest_is_exact_append_only_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            content = "first line\nsecond line\n"

            first = ingest_message(
                scope,
                content,
                session_id="session",
                turn_id="turn",
                cwd=str(root),
            )
            second = ingest_message(
                scope,
                content,
                session_id="session",
                turn_id="turn",
                cwd=str(root),
            )

            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(first.message_id, second.message_id)

            connection = sqlite3.connect(scope.journal_path)
            try:
                stored = connection.execute(
                    "SELECT content FROM messages WHERE message_id = ?",
                    (first.message_id,),
                ).fetchone()[0]
                self.assertEqual(stored, content)
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE messages SET content = 'changed' WHERE message_id = ?",
                        (first.message_id,),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "DELETE FROM events WHERE event_id = ?",
                        (first.event_id,),
                    )
            finally:
                connection.close()

            with self.assertRaisesRegex(
                JournalError,
                "different message identity",
            ) as caught:
                ingest_message(
                    scope,
                    "different",
                    session_id="session",
                    turn_id="turn",
                    cwd=str(root),
                )
            self.assertEqual(caught.exception.code, "state_conflict")

    def test_replay_returns_original_event_despite_recovery_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            first = ingest_message(
                scope,
                "replay me",
                session_id="session",
                turn_id="turn",
                cwd=str(root),
            )

            connection = sqlite3.connect(scope.journal_path)
            try:
                connection.execute(
                    """
                    INSERT INTO events(
                      event_id,
                      occurred_at,
                      event_type,
                      message_id,
                      payload_json
                    ) VALUES (
                      'evt_recovery',
                      '2026-07-29T00:00:00+00:00',
                      'message.received',
                      ?,
                      '{"recovered_claim_id": "clm_expired"}'
                    )
                    """,
                    (first.message_id,),
                )
                connection.commit()
                received_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM events
                    WHERE message_id = ? AND event_type = 'message.received'
                    """,
                    (first.message_id,),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(received_count, 2)

            replayed = ingest_message(
                scope,
                "replay me",
                session_id="session",
                turn_id="turn",
                cwd=str(root),
            )

            self.assertFalse(replayed.created)
            self.assertEqual(replayed.event_id, first.event_id)
            self.assertEqual(replayed.sequence, first.sequence)

    def test_write_contention_raises_journal_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            ingest_message(scope, "initialize", cwd=str(root))

            original_connect = journal_module._connect

            def impatient_connect(target_scope, **keywords):
                connection = original_connect(target_scope, **keywords)
                connection.execute("PRAGMA busy_timeout = 50")
                return connection

            blocker = sqlite3.connect(scope.journal_path)
            try:
                blocker.execute("BEGIN IMMEDIATE")
                with patch.object(journal_module, "_connect", impatient_connect):
                    with self.assertRaisesRegex(
                        JournalError,
                        "locked|busy",
                    ) as caught:
                        ingest_message(scope, "contended", cwd=str(root))
            finally:
                blocker.close()
            self.assertEqual(caught.exception.code, "contention")

    def test_missing_journal_errors_carry_not_found_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            scope = self.agent_scope(Path(temporary_directory))
            for operation in (create_snapshot, check_journal):
                with self.subTest(operation=operation.__name__):
                    with self.assertRaisesRegex(
                        JournalError,
                        "does not exist",
                    ) as caught:
                        operation(scope)
                    self.assertEqual(caught.exception.code, "not_found")

    def test_inbox_hides_content_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            ingest_message(scope, "secret content", cwd=str(root))

            hidden = list_inbox(scope)
            visible = list_inbox(scope, include_content=True)

            self.assertEqual(hidden[0]["state"], "received")
            self.assertNotIn("content", hidden[0])
            self.assertEqual(visible[0]["content"], "secret content")

    def test_inbox_limit_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            for index in range(21):
                ingest_message(scope, f"message {index}", cwd=str(root))

            # The default page is unchanged, and the ceiling is inclusive.
            self.assertEqual(len(list_inbox(scope)), 20)
            self.assertEqual(len(list_inbox(scope, limit=1000)), 21)

            # Out of range is rejected, never clamped.
            for limit in (-1, 0, 1001):
                with self.assertRaisesRegex(
                    JournalError,
                    "inbox limit must be between 1 and 1000",
                ) as caught:
                    list_inbox(scope, limit=limit)
                self.assertEqual(caught.exception.code, "invalid_argument")

    def test_journal_permissions_and_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            initialize_journal(scope)

            result = check_journal(scope)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(
                stat.S_IMODE(scope.journal_path.stat().st_mode),
                0o600,
            )
            self.assertEqual(
                stat.S_IMODE(scope.journal_path.parent.stat().st_mode),
                0o700,
            )
            connection = sqlite3.connect(scope.journal_path)
            try:
                journal_mode = connection.execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(journal_mode, "wal")

    def test_old_sqlite_is_rejected_before_filesystem_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            journal_module._require_sqlite_runtime.cache_clear()
            try:
                with patch.object(
                    journal_module.sqlite3,
                    "sqlite_version_info",
                    (3, 36, 0),
                ):
                    with self.assertRaisesRegex(
                        JournalError,
                        "SQLite 3.37.0 or newer is required",
                    ):
                        initialize_journal(scope)
            finally:
                journal_module._require_sqlite_runtime.cache_clear()

            self.assertFalse(scope.journal_path.parent.exists())

    def test_missing_sqlite_json_is_rejected_before_filesystem_mutation(self) -> None:
        class MissingJsonConnection:
            def execute(self, _statement: str):
                raise sqlite3.OperationalError("no such function: json_valid")

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            journal_module._require_sqlite_runtime.cache_clear()
            try:
                with patch.object(
                    journal_module.sqlite3,
                    "connect",
                    return_value=MissingJsonConnection(),
                ):
                    with self.assertRaisesRegex(
                        JournalError,
                        "SQLite JSON functions are required",
                    ):
                        initialize_journal(scope)
            finally:
                journal_module._require_sqlite_runtime.cache_clear()

            self.assertFalse(scope.journal_path.parent.exists())

    def test_non_wal_filesystem_is_rejected(self) -> None:
        class NonWalConnection:
            def execute(self, _statement: str):
                return self

            def fetchone(self):
                return ("delete",)

        with self.assertRaisesRegex(JournalError, "local filesystem"):
            journal_module._enable_wal(NonWalConnection())

    def test_snapshot_retention_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            ingest_message(scope, "snapshot this", cwd=str(root))

            create_snapshot(scope, keep=2)
            create_snapshot(scope, keep=2)
            result = create_snapshot(scope, keep=2)

            snapshots = list(
                (scope.journal_path.parent / "backups").glob("journal-*.sqlite3")
            )
            self.assertEqual(len(snapshots), 2)
            self.assertEqual(result["retained"], 2)
            self.assertEqual(len(result["removed"]), 1)

    def test_snapshot_prune_tolerates_concurrent_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            ingest_message(scope, "snapshot race", cwd=str(root))
            create_snapshot(scope, keep=1)
            create_snapshot(scope, keep=1)

            original_glob = Path.glob

            def glob_with_concurrent_prune(path, pattern):
                results = sorted(original_glob(path, pattern), reverse=True)
                if pattern == "journal-*.sqlite3":
                    for candidate in results[1:]:
                        candidate.unlink()
                return iter(results)

            with patch.object(Path, "glob", glob_with_concurrent_prune):
                result = create_snapshot(scope, keep=1)

            self.assertEqual(len(result["removed"]), 1)
            self.assertEqual(result["retained"], 1)

    def test_schema_v1_migrates_with_message_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            scope.journal_path.parent.mkdir(parents=True)
            connection = sqlite3.connect(scope.journal_path)
            try:
                connection.executescript(SCHEMA_SQL)
                connection.executemany(
                    """
                    INSERT INTO journal_metadata(key, value)
                    VALUES (?, ?)
                    """,
                    {
                        "schema_version": "1",
                        "scope_kind": scope.kind,
                        "scope_root": str(scope.root),
                        "scope_id": scope.scope_id,
                    }.items(),
                )
                connection.execute(
                    """
                    INSERT INTO messages(
                      message_id,
                      received_at,
                      source,
                      content,
                      content_sha256,
                      cwd
                    ) VALUES (
                      'msg_existing',
                      '2026-01-01T00:00:00+00:00',
                      'user',
                      'preserve exactly',
                      ?,
                      ?
                    )
                    """,
                    (
                        hashlib.sha256(b"preserve exactly").hexdigest(),
                        str(root),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO events(
                      event_id,
                      occurred_at,
                      event_type,
                      message_id,
                      payload_json
                    ) VALUES (
                      'evt_existing',
                      '2026-01-01T00:00:00+00:00',
                      'message.received',
                      'msg_existing',
                      '{}'
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()
            scope.journal_path.chmod(0o600)

            check_result = check_journal(scope)

            migrated = sqlite3.connect(scope.journal_path)
            try:
                metadata = dict(
                    migrated.execute("SELECT key, value FROM journal_metadata")
                )
                content = migrated.execute(
                    "SELECT content FROM messages WHERE message_id = 'msg_existing'"
                ).fetchone()[0]
                event = migrated.execute(
                    "SELECT event_id FROM events WHERE event_id = 'evt_existing'"
                ).fetchone()[0]
                migration = migrated.execute(
                    """
                    SELECT from_version, to_version, backup_name
                    FROM schema_migrations
                    """
                ).fetchone()
                claims_index = migrated.execute(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type = 'index' AND name = 'claims_resource_lookup'
                    """
                ).fetchone()
            finally:
                migrated.close()

            self.assertEqual(metadata["schema_version"], str(SCHEMA_VERSION))
            self.assertEqual(check_result["schema_version"], SCHEMA_VERSION)
            self.assertEqual(content, "preserve exactly")
            self.assertEqual(event, "evt_existing")
            self.assertEqual(migration[:2], (1, SCHEMA_VERSION))
            self.assertIsNotNone(claims_index)
            backup_path = scope.journal_path.parent / "backups" / migration[2]
            self.assertTrue(backup_path.exists())
            backup = sqlite3.connect(backup_path)
            try:
                backup_version = backup.execute(
                    """
                    SELECT value
                    FROM journal_metadata
                    WHERE key = 'schema_version'
                    """
                ).fetchone()[0]
                backup_content = backup.execute(
                    "SELECT content FROM messages WHERE message_id = 'msg_existing'"
                ).fetchone()[0]
            finally:
                backup.close()
            self.assertEqual(backup_version, "1")
            self.assertEqual(backup_content, "preserve exactly")
            self.assertEqual(list_inbox(scope)[0]["message_id"], "msg_existing")

    def test_schema_v2_migrates_with_claims_index_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            scope.journal_path.parent.mkdir(parents=True)
            connection = sqlite3.connect(scope.journal_path)
            try:
                connection.executescript(SCHEMA_SQL)
                journal_module._create_v2_schema(connection)
                connection.executemany(
                    """
                    INSERT INTO journal_metadata(key, value)
                    VALUES (?, ?)
                    """,
                    {
                        "schema_version": "2",
                        "scope_kind": scope.kind,
                        "scope_root": str(scope.root),
                        "scope_id": scope.scope_id,
                    }.items(),
                )
                connection.execute(
                    """
                    INSERT INTO schema_migrations(
                      migration_id,
                      from_version,
                      to_version,
                      migrated_at,
                      backup_name
                    ) VALUES (1, 0, 2, '2026-01-01T00:00:00+00:00', NULL)
                    """
                )
                connection.commit()
            finally:
                connection.close()
            scope.journal_path.chmod(0o600)

            check_result = check_journal(scope)

            migrated = sqlite3.connect(scope.journal_path)
            try:
                metadata = dict(
                    migrated.execute("SELECT key, value FROM journal_metadata")
                )
                migrations = migrated.execute(
                    """
                    SELECT migration_id, from_version, to_version, backup_name
                    FROM schema_migrations
                    ORDER BY migration_id
                    """
                ).fetchall()
                claims_index = migrated.execute(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type = 'index' AND name = 'claims_resource_lookup'
                    """
                ).fetchone()
            finally:
                migrated.close()

            self.assertEqual(metadata["schema_version"], str(SCHEMA_VERSION))
            self.assertEqual(check_result["schema_version"], SCHEMA_VERSION)
            self.assertIsNotNone(claims_index)
            self.assertEqual(len(migrations), 2)
            self.assertEqual(migrations[0][:3], (1, 0, 2))
            self.assertEqual(migrations[1][:3], (2, 2, SCHEMA_VERSION))
            backup_path = (
                scope.journal_path.parent / "backups" / migrations[1][3]
            )
            self.assertTrue(backup_path.exists())
            backup = sqlite3.connect(backup_path)
            try:
                backup_version = backup.execute(
                    """
                    SELECT value
                    FROM journal_metadata
                    WHERE key = 'schema_version'
                    """
                ).fetchone()[0]
                backup_index = backup.execute(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type = 'index' AND name = 'claims_resource_lookup'
                    """
                ).fetchone()
            finally:
                backup.close()
            self.assertEqual(backup_version, "2")
            self.assertIsNone(backup_index)

    def test_schema_v4_migrates_and_preserves_claims_without_locators(
        self,
    ) -> None:
        """The v4 -> v5 claim locator lands without disturbing the ledger.

        Schema 5 adds the two holder-locator columns by ALTER rather than
        by rebuilding an append-only table, so the property under test is
        that an existing claim survives byte for byte and simply reads
        NULL for the columns that did not exist when it was written.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            scope.journal_path.parent.mkdir(parents=True)
            connection = sqlite3.connect(scope.journal_path)
            try:
                connection.executescript(SCHEMA_SQL)
                journal_module._create_v2_schema(connection)
                journal_module._create_v3_schema(connection)
                journal_module._create_v4_schema(connection)
                connection.executemany(
                    """
                    INSERT INTO journal_metadata(key, value)
                    VALUES (?, ?)
                    """,
                    {
                        "schema_version": "4",
                        "scope_kind": scope.kind,
                        "scope_root": str(scope.root),
                        "scope_id": scope.scope_id,
                    }.items(),
                )
                connection.execute(
                    """
                    INSERT INTO schema_migrations(
                      migration_id,
                      from_version,
                      to_version,
                      migrated_at,
                      backup_name
                    ) VALUES (1, 0, 4, '2026-01-01T00:00:00+00:00', NULL)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO messages(
                      message_id,
                      received_at,
                      source,
                      content,
                      content_sha256,
                      cwd
                    ) VALUES (
                      'msg_claimed',
                      '2026-01-01T00:00:00+00:00',
                      'user',
                      'claimed before v5',
                      ?,
                      ?
                    )
                    """,
                    (
                        hashlib.sha256(b"claimed before v5").hexdigest(),
                        str(root),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO events(
                      event_id,
                      occurred_at,
                      event_type,
                      message_id,
                      payload_json
                    ) VALUES (
                      'evt_received',
                      '2026-01-01T00:00:00+00:00',
                      'message.received',
                      'msg_claimed',
                      '{}'
                    )
                    """
                )
                cursor = connection.execute(
                    """
                    INSERT INTO events(
                      event_id,
                      occurred_at,
                      event_type,
                      message_id,
                      payload_json
                    ) VALUES (
                      'evt_claim',
                      '2026-01-01T00:00:00+00:00',
                      'claim.acquired',
                      'msg_claimed',
                      '{"claim_id":"clm_legacy"}'
                    )
                    """
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
                    ) VALUES (
                      'clm_legacy',
                      'message',
                      'msg_claimed',
                      'legacy-worker',
                      ?,
                      NULL,
                      1000,
                      901000000
                    )
                    """,
                    (cursor.lastrowid,),
                )
                connection.commit()
            finally:
                connection.close()
            scope.journal_path.chmod(0o600)

            check_result = check_journal(scope)

            migrated = sqlite3.connect(scope.journal_path)
            migrated.row_factory = sqlite3.Row
            try:
                metadata = dict(
                    migrated.execute(
                        "SELECT key, value FROM journal_metadata"
                    )
                )
                claim = migrated.execute(
                    "SELECT * FROM claims WHERE claim_id = 'clm_legacy'"
                ).fetchone()
                migrations = migrated.execute(
                    """
                    SELECT migration_id, from_version, to_version, backup_name
                    FROM schema_migrations
                    ORDER BY migration_id
                    """
                ).fetchall()
                integrity = migrated.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]
                foreign_keys = migrated.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                # The append-only guards are metadata, not data, so an
                # ALTER must leave them exactly where a rebuild would
                # have had to recreate them.
                triggers = {
                    row[0]
                    for row in migrated.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'trigger' AND tbl_name = 'claims'
                        """
                    )
                }
            finally:
                migrated.close()

            self.assertEqual(metadata["schema_version"], str(SCHEMA_VERSION))
            self.assertEqual(check_result["schema_version"], SCHEMA_VERSION)
            self.assertEqual(check_result["claims"], 1)
            self.assertEqual(
                dict(claim),
                {
                    "claim_id": "clm_legacy",
                    "resource_kind": "message",
                    "resource_id": "msg_claimed",
                    "owner_id": "legacy-worker",
                    "fence": claim["fence"],
                    "basis_revision": None,
                    "acquired_at_us": 1000,
                    "expires_at_us": 901000000,
                    # Written before the columns existed: unattributed,
                    # and therefore never counted as any session's own.
                    "holder_host": None,
                    "holder_sid": None,
                },
            )
            self.assertEqual(
                read_status(scope, now_us=1000)["claims"],
                {"active": 1, "active_this_session": 0},
            )
            self.assertEqual(integrity, "ok")
            self.assertEqual(foreign_keys, [])
            self.assertEqual(
                triggers,
                {
                    "claims_no_update",
                    "claims_no_delete",
                    "claims_no_replace",
                    "claims_validate_insert",
                },
            )
            self.assertEqual(len(migrations), 2)
            self.assertEqual(migrations[1][:3], (2, 4, SCHEMA_VERSION))
            backup_path = (
                scope.journal_path.parent / "backups" / migrations[1][3]
            )
            backup = sqlite3.connect(backup_path)
            try:
                backup_columns = {
                    row[1]
                    for row in backup.execute("PRAGMA table_info(claims)")
                }
            finally:
                backup.close()
            self.assertNotIn("holder_host", backup_columns)
            self.assertNotIn("holder_sid", backup_columns)

    def test_fresh_journal_creates_claims_lookup_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            initialize_journal(scope)
            connection = sqlite3.connect(scope.journal_path)
            try:
                claims_index = connection.execute(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type = 'index' AND name = 'claims_resource_lookup'
                    """
                ).fetchone()
            finally:
                connection.close()
            self.assertIsNotNone(claims_index)

    def test_concurrent_fresh_initialization_converges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            worker_count = 16
            barrier = threading.Barrier(worker_count)

            def initialize() -> Path:
                barrier.wait()
                return initialize_journal(scope)

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                paths = list(executor.map(lambda _: initialize(), range(worker_count)))

            self.assertEqual(set(paths), {scope.journal_path})
            self.assertEqual(check_journal(scope)["schema_version"], SCHEMA_VERSION)

    def test_journal_directory_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            target = root / "redirected"
            target.mkdir()
            scope.journal_path.parent.parent.mkdir(parents=True)
            scope.journal_path.parent.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(JournalError, "real directory"):
                initialize_journal(scope)

    def test_failed_fresh_schema_creation_rolls_back_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.agent_scope(root)
            original = journal_module._create_v2_schema

            def fail_after_partial_table(connection: sqlite3.Connection) -> None:
                connection.execute("CREATE TABLE partial_failure(value TEXT)")
                raise RuntimeError("injected schema failure")

            with patch.object(
                journal_module,
                "_create_v2_schema",
                fail_after_partial_table,
            ):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    initialize_journal(scope)

            connection = sqlite3.connect(scope.journal_path)
            try:
                partial_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM sqlite_master
                    WHERE name = 'partial_failure'
                    """
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(partial_count, 0)

            with patch.object(journal_module, "_create_v2_schema", original):
                initialize_journal(scope)
            self.assertEqual(check_journal(scope)["status"], "ok")

    def test_canonical_event_ingestion_is_repo_scoped_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            repository.mkdir()
            run_git(repository, "init", "-b", "main")
            event = {
                "v": 1,
                "source": "codex",
                "content": "capture this exactly\n",
                "session_id": "session",
                "turn_id": "turn",
                "cwd": str(repository),
            }
            command = [
                sys.executable,
                "-c",
                "from aiq.cli import main; raise SystemExit(main())",
                "ingest",
                "--event-json",
                "-",
                "--scope",
                "repo",
                "--json",
            ]

            first = subprocess.run(
                command,
                input=json.dumps(event),
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            second = subprocess.run(
                command,
                input=json.dumps(event),
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )

            self.assertTrue(json.loads(first.stdout)["created"])
            self.assertFalse(json.loads(second.stdout)["created"])
            scope = resolve_scope("repo", cwd=repository)
            self.assertEqual(check_journal(scope)["messages"], 1)


class ProjectLabelTest(unittest.TestCase):
    """The journal-level project label: derivation, storage, override."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def repo_scope(self, name: str = "repository"):
        repository = self.root / name
        repository.mkdir()
        run_git(repository, "init", "-b", "main")
        return repository, resolve_scope("repo", cwd=repository)

    def stored_label(self, scope) -> str | None:
        connection = sqlite3.connect(scope.journal_path)
        try:
            row = connection.execute(
                "SELECT value FROM journal_metadata WHERE key = 'project_label'"
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else row[0]

    def test_repo_label_defaults_to_the_repository_directory_name(self) -> None:
        _, scope = self.repo_scope("release-tools")

        # The scope root is the Git common directory, so the label comes
        # from its parent -- the repository root -- not from ".git".
        self.assertEqual(scope.root.name, ".git")
        self.assertEqual(default_project_label(scope), "release-tools")

        initialize_journal(scope)

        self.assertEqual(self.stored_label(scope), "release-tools")
        self.assertEqual(project_label(scope), "release-tools")

    def test_user_scope_is_labeled_user(self) -> None:
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}):
            scope = resolve_scope("user")

            # The user journal's directory is named "aiq"; the label is
            # the scope's meaning, not that directory's name.
            self.assertEqual(scope.root.name, "aiq")
            self.assertEqual(default_project_label(scope), "user")
            initialize_journal(scope)
            self.assertEqual(project_label(scope), "user")

    def test_agent_root_scope_uses_the_agent_root_directory_name(self) -> None:
        agent_root = self.root / "orchestrator"
        agent_root.mkdir()
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}):
            scope = resolve_scope(
                "agent-root",
                cwd=self.root,
                agent_root=agent_root,
            )

            self.assertEqual(default_project_label(scope), "orchestrator")
            initialize_journal(scope)
            self.assertEqual(project_label(scope), "orchestrator")

    def test_linked_worktrees_share_the_primary_repository_label(self) -> None:
        repository = self.root / "primary"
        worktree = self.root / "feature-worktree"
        repository.mkdir()
        run_git(repository, "init", "-b", "main")
        run_git(repository, "config", "user.name", "AIQ Test")
        run_git(repository, "config", "user.email", "aiq@example.invalid")
        (repository / "tracked").write_text("initial\n")
        run_git(repository, "add", "tracked")
        run_git(repository, "commit", "-m", "Initial")
        run_git(repository, "worktree", "add", "-b", "task", str(worktree), "main")

        primary = resolve_scope("repo", cwd=repository)
        linked = resolve_scope("repo", cwd=worktree)
        initialize_journal(primary)

        # Worktrees share the primary journal, so they share its label:
        # the worktree directory name never leaks into task references.
        self.assertEqual(project_label(linked), "primary")
        self.assertEqual(project_label(primary), project_label(linked))

    def test_missing_journal_derives_the_label_without_creating_storage(
        self,
    ) -> None:
        _, scope = self.repo_scope("unwritten")

        self.assertEqual(project_label(scope), "unwritten")
        self.assertFalse(scope.journal_path.exists())

    def test_explicit_label_is_persisted_and_can_be_changed(self) -> None:
        _, scope = self.repo_scope()

        initialize_journal(scope, label="Release Train")
        self.assertEqual(project_label(scope), "Release Train")

        # journal_metadata is a plain mutable table -- deliberately
        # outside the append-only triggers -- so a journal can be
        # relabeled in place.
        initialize_journal(scope, label="aiq")
        self.assertEqual(project_label(scope), "aiq")

        # A plain re-init keeps the stored label rather than reverting
        # to the derived default.
        initialize_journal(scope)
        self.assertEqual(project_label(scope), "aiq")

    def test_invalid_labels_are_rejected(self) -> None:
        _, scope = self.repo_scope()
        rejected = (
            "",
            "   ",
            "two\nlines",
            "tabbed\tlabel",
            "carriage\rreturn",
            "bell\x07",
            "x" * (PROJECT_LABEL_MAX_LENGTH + 1),
        )

        for value in rejected:
            with self.subTest(label=value):
                with self.assertRaises(JournalError):
                    validate_project_label(value)
                with self.assertRaises(JournalError):
                    initialize_journal(scope, label=value)

        # A rejected label never reaches storage.
        self.assertFalse(scope.journal_path.exists())
        self.assertEqual(
            validate_project_label("x" * PROJECT_LABEL_MAX_LENGTH),
            "x" * PROJECT_LABEL_MAX_LENGTH,
        )

    def test_pre_label_journal_is_backfilled_on_first_open(self) -> None:
        _, scope = self.repo_scope("legacy")
        initialize_journal(scope)

        # Simulate a journal written before labels existed.
        connection = sqlite3.connect(scope.journal_path)
        try:
            connection.execute(
                "DELETE FROM journal_metadata WHERE key = 'project_label'"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertIsNone(self.stored_label(scope))

        # Any open backfills it: every read path goes through _connect,
        # which runs the same initialization under the lifecycle lock.
        self.assertEqual(read_status(scope)["project"], "legacy")
        self.assertEqual(self.stored_label(scope), "legacy")

    def test_read_status_and_check_report_the_stored_label(self) -> None:
        _, scope = self.repo_scope()
        initialize_journal(scope, label="aiq")

        self.assertEqual(read_status(scope)["project"], "aiq")
        self.assertEqual(check_journal(scope)["project"], "aiq")

    def test_missing_journal_status_reports_the_derived_label(self) -> None:
        _, scope = self.repo_scope("underived")

        self.assertEqual(read_status(scope)["project"], "underived")
        self.assertFalse(scope.journal_path.exists())


if __name__ == "__main__":
    unittest.main()
