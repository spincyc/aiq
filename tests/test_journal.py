from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from aiq import journal as journal_module
from aiq.journal import (
    JournalError,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    check_journal,
    create_snapshot,
    ingest_message,
    initialize_journal,
    list_inbox,
    resolve_scope,
)


def run_git(cwd: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


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
                repository / ".git" / "aiq" / "journal.sqlite3",
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

            self.assertEqual(scope.root, repository / ".git")

    def test_missing_git_falls_back_to_stable_user_scope(self) -> None:
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
                scope = resolve_scope("auto", cwd=root)
                with self.assertRaisesRegex(JournalError, "Git is unavailable"):
                    resolve_scope("repo", cwd=root)

            self.assertEqual(scope.kind, "user")
            self.assertEqual(scope.scope_id, "user")
            self.assertEqual(
                scope.journal_path,
                state_home / "aiq" / "journal.sqlite3",
            )

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

            with self.assertRaises(JournalError):
                ingest_message(
                    scope,
                    "different",
                    session_id="session",
                    turn_id="turn",
                    cwd=str(root),
                )

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
            finally:
                migrated.close()

            self.assertEqual(metadata["schema_version"], str(SCHEMA_VERSION))
            self.assertEqual(check_result["schema_version"], SCHEMA_VERSION)
            self.assertEqual(content, "preserve exactly")
            self.assertEqual(event, "evt_existing")
            self.assertEqual(migration[:2], (1, 2))
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


if __name__ == "__main__":
    unittest.main()
