from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from aiq.journal import (
    JournalError,
    JournalScope,
    SCHEMA_VERSION,
    check_journal,
    list_inbox,
    resolve_scope,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "schema-v1.sql"


class MigrationFixtureTest(unittest.TestCase):
    def _scope(self, root: Path) -> JournalScope:
        agent_root = root / "agent-root"
        agent_root.mkdir()
        with patch.dict(
            os.environ,
            {"XDG_STATE_HOME": str(root / "state")},
        ):
            return resolve_scope(
                "agent-root",
                cwd=root,
                agent_root=agent_root,
            )

    def _install_v1_fixture(
        self,
        scope: JournalScope,
        *,
        schema_version: int = 1,
    ) -> None:
        def sql_literal(value: str) -> str:
            return value.replace("'", "''")

        script = FIXTURE_PATH.read_text(encoding="utf-8")
        replacements = {
            "__AIQ_SCOPE_KIND__": sql_literal(scope.kind),
            "__AIQ_SCOPE_ROOT__": sql_literal(str(scope.root)),
            "__AIQ_SCOPE_ID__": sql_literal(scope.scope_id),
        }
        for token, value in replacements.items():
            script = script.replace(token, value)

        scope.journal_path.parent.mkdir(mode=0o700, parents=True)
        connection = sqlite3.connect(scope.journal_path)
        try:
            connection.executescript(script)
            if schema_version != 1:
                connection.execute(
                    """
                    UPDATE journal_metadata
                    SET value = ?
                    WHERE key = 'schema_version'
                    """,
                    (str(schema_version),),
                )
                connection.commit()
        finally:
            connection.close()
        scope.journal_path.chmod(0o600)

    def test_frozen_v1_fixture_migrates_and_preserves_valid_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            scope = self._scope(Path(temporary_directory))
            self._install_v1_fixture(scope)

            result = check_journal(scope)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["schema_version"], SCHEMA_VERSION)
            connection = sqlite3.connect(scope.journal_path)
            try:
                message = connection.execute(
                    """
                    SELECT
                      received_at,
                      source,
                      content,
                      content_sha256,
                      idempotency_key,
                      session_id,
                      turn_id,
                      cwd
                    FROM messages
                    WHERE message_id = 'msg_existing'
                    """
                ).fetchone()
                event = connection.execute(
                    """
                    SELECT
                      sequence,
                      event_id,
                      occurred_at,
                      event_type,
                      message_id,
                      task_id,
                      payload_json
                    FROM events
                    WHERE event_id = 'evt_existing'
                    """
                ).fetchone()
                migration = connection.execute(
                    """
                    SELECT from_version, to_version, backup_name
                    FROM schema_migrations
                    """
                ).fetchone()
                reader_lease_table = connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table' AND name = 'reader_leases'
                    """
                ).fetchone()
                claim_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(claims)"
                    )
                }
                current_integrity = connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]
                current_foreign_keys = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
            finally:
                connection.close()

            self.assertEqual(
                message,
                (
                    "2026-01-01T00:00:00.000000+00:00",
                    "user",
                    "preserve exactly",
                    "783708352c1d00ef8c629f084f15add96"
                    "afc3e574eb3900b8e26864b4569cd5b",
                    "fixture:schema-v1:existing",
                    "fixture-session",
                    "fixture-turn",
                    str(scope.root),
                ),
            )
            self.assertEqual(
                event,
                (
                    1,
                    "evt_existing",
                    "2026-01-01T00:00:00.000000+00:00",
                    "message.received",
                    "msg_existing",
                    None,
                    "{}",
                ),
            )
            self.assertEqual(migration[:2], (1, SCHEMA_VERSION))
            self.assertEqual(reader_lease_table, ("reader_leases",))
            # Every later schema lands in one pass, including the v5
            # columns, which are added by ALTER and so name no object.
            self.assertLessEqual(
                {"holder_host", "holder_sid"},
                claim_columns,
            )
            self.assertEqual(current_integrity, "ok")
            self.assertEqual(current_foreign_keys, [])
            self.assertEqual(
                list_inbox(scope, include_content=True)[0]["content"],
                "preserve exactly",
            )

            backup_path = (
                scope.journal_path.parent / "backups" / migration[2]
            )
            self.assertTrue(backup_path.is_file())
            self.assertEqual(
                stat.S_IMODE(backup_path.stat().st_mode),
                0o600,
            )
            backup = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
            try:
                backup_version = backup.execute(
                    """
                    SELECT value
                    FROM journal_metadata
                    WHERE key = 'schema_version'
                    """
                ).fetchone()[0]
                backup_message = backup.execute(
                    """
                    SELECT content, idempotency_key
                    FROM messages
                    WHERE message_id = 'msg_existing'
                    """
                ).fetchone()
                backup_integrity = backup.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]
                backup_foreign_keys = backup.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
            finally:
                backup.close()

            self.assertEqual(backup_version, "1")
            self.assertEqual(
                backup_message,
                ("preserve exactly", "fixture:schema-v1:existing"),
            )
            self.assertEqual(backup_integrity, "ok")
            self.assertEqual(backup_foreign_keys, [])

            check_journal(scope)
            self.assertEqual(
                list(
                    (scope.journal_path.parent / "backups").glob(
                        "pre-migration-v1-to-v*-*.sqlite3"
                    )
                ),
                [backup_path],
            )

    def test_future_fixture_schema_is_refused_without_migration_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            scope = self._scope(Path(temporary_directory))
            future_version = SCHEMA_VERSION + 1
            self._install_v1_fixture(
                scope,
                schema_version=future_version,
            )

            with self.assertRaisesRegex(
                JournalError,
                rf"schema {future_version} is newer than supported",
            ):
                check_journal(scope)

            connection = sqlite3.connect(scope.journal_path)
            try:
                version = connection.execute(
                    """
                    SELECT value
                    FROM journal_metadata
                    WHERE key = 'schema_version'
                    """
                ).fetchone()[0]
                content = connection.execute(
                    """
                    SELECT content
                    FROM messages
                    WHERE message_id = 'msg_existing'
                    """
                ).fetchone()[0]
            finally:
                connection.close()

            self.assertEqual(version, str(future_version))
            self.assertEqual(content, "preserve exactly")
            backup_directory = scope.journal_path.parent / "backups"
            self.assertFalse(backup_directory.exists())


if __name__ == "__main__":
    unittest.main()
