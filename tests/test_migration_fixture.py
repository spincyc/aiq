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
    SCHEMA_V4_STATEMENTS,
    SCHEMA_VERSION,
    check_journal,
    list_inbox,
    resolve_scope,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "schema-v1.sql"
FIXTURE_V4_PATH = Path(__file__).parent / "fixtures" / "schema-v4.sql"


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

    def _install_fixture(
        self,
        scope: JournalScope,
        fixture_path: Path,
        *,
        schema_version: int,
        after: tuple[str, ...] = (),
    ) -> None:
        """Load a frozen fixture as this scope's journal.

        ``schema_version`` overrides the version the fixture declares, and
        ``after`` runs plain SQL once the fixture is loaded. Both exist so a
        baseline can be stated by the test rather than borrowed from the
        schema module the migration under test lives in.
        """

        def sql_literal(value: str) -> str:
            return value.replace("'", "''")

        script = fixture_path.read_text(encoding="utf-8")
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
            declared = connection.execute(
                """
                SELECT value
                FROM journal_metadata
                WHERE key = 'schema_version'
                """
            ).fetchone()[0]
            for statement in after:
                connection.execute(statement)
            if declared != str(schema_version):
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

    def _install_v1_fixture(
        self,
        scope: JournalScope,
        *,
        schema_version: int = 1,
    ) -> None:
        self._install_fixture(
            scope,
            FIXTURE_PATH,
            schema_version=schema_version,
        )

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

    def _migration_row(self, scope: JournalScope) -> tuple:
        connection = sqlite3.connect(scope.journal_path)
        try:
            return connection.execute(
                """
                SELECT from_version, to_version, backup_name
                FROM schema_migrations
                ORDER BY migration_id DESC
                LIMIT 1
                """
            ).fetchone()
        finally:
            connection.close()

    def test_frozen_v4_fixture_migrates_and_leaves_claims_unattributed(
        self,
    ) -> None:
        """The 4 -> 5 hop, the one every deployed journal will run.

        Schema 5 adds the holder locator to `claims` by ALTER, so the
        property is that a claim written before the columns existed stays
        byte for byte what it was and simply reads NULL for them -- and
        that the append-only guards a rebuild would have had to recreate
        were never disturbed in the first place.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            scope = self._scope(Path(temporary_directory))
            self._install_fixture(scope, FIXTURE_V4_PATH, schema_version=4)

            result = check_journal(scope)

            connection = sqlite3.connect(scope.journal_path)
            connection.row_factory = sqlite3.Row
            try:
                claims = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM claims ORDER BY claim_id"
                    )
                ]
                claim_columns = [
                    row[1]
                    for row in connection.execute("PRAGMA table_info(claims)")
                ]
                lease = dict(
                    connection.execute(
                        "SELECT * FROM reader_leases WHERE lease_scope = 0"
                    ).fetchone()
                )
                claim_triggers = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'trigger' AND tbl_name = 'claims'
                        """
                    )
                }
                integrity = connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]
                foreign_keys = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
            finally:
                connection.close()

            migration = self._migration_row(scope)
            backup_path = scope.journal_path.parent / "backups" / migration[2]
            backup = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
            backup.row_factory = sqlite3.Row
            try:
                backup_version = backup.execute(
                    """
                    SELECT value
                    FROM journal_metadata
                    WHERE key = 'schema_version'
                    """
                ).fetchone()[0]
                backup_claim_columns = [
                    row[1] for row in backup.execute("PRAGMA table_info(claims)")
                ]
                backup_claims = [
                    dict(row)
                    for row in backup.execute(
                        "SELECT * FROM claims ORDER BY claim_id"
                    )
                ]
                backup_integrity = backup.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]
            finally:
                backup.close()

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["schema_version"], SCHEMA_VERSION)
            self.assertEqual(result["messages"], 2)
            self.assertEqual(result["tasks"], 1)
            self.assertEqual(result["claims"], 3)
            self.assertEqual(result["project"], "fixture-project")

            # The migration row names the hop and the snapshot taken for it.
            self.assertEqual(migration[:2], (4, SCHEMA_VERSION))
            self.assertEqual(
                migration[2].split("-")[:5],
                ["pre", "migration", "v4", "to", f"v{SCHEMA_VERSION}"],
            )
            self.assertTrue(backup_path.is_file())
            self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)
            # The snapshot is the journal as it stood before the hop, which
            # is the only thing that makes it a rollback target.
            self.assertEqual(backup_version, "4")
            self.assertEqual(backup_integrity, "ok")
            self.assertNotIn("holder_host", backup_claim_columns)
            self.assertNotIn("holder_sid", backup_claim_columns)

            # The ALTER appends; it must not reorder or drop what v4 wrote.
            self.assertEqual(
                claim_columns,
                [*backup_claim_columns, "holder_host", "holder_sid"],
            )
            self.assertEqual(
                [claim["claim_id"] for claim in claims],
                ["clm_enqueue_applied", "clm_message", "clm_task"],
            )
            self.assertEqual(
                [claim["resource_kind"] for claim in claims],
                ["message", "message", "task"],
            )
            # A task claim's basis revision is what a rebuild would most
            # easily have lost; every v4 column reads back what it held.
            self.assertEqual(claims[2]["basis_revision"], 1)
            self.assertEqual(
                [
                    {
                        column: claim[column]
                        for column in backup_claim_columns
                    }
                    for claim in claims
                ],
                backup_claims,
            )
            # Taken before the columns existed, so no session can now
            # recognize any of them as its own.
            self.assertEqual(
                [(claim["holder_host"], claim["holder_sid"]) for claim in claims],
                [(None, None), (None, None), (None, None)],
            )

            # reader_leases already carried a locator at v4; a migration
            # that touches `claims` must leave the lease row alone.
            self.assertEqual(lease["holder_host"], "fixture-host")
            self.assertEqual(lease["holder_sid"], 4242)
            self.assertEqual(lease["reader_id"], "rdr_fixture")
            self.assertEqual(lease["epoch"], 1)

            # The append-only guards are metadata a rebuild would have had
            # to drop and recreate; the ALTER never disturbs them.
            self.assertEqual(
                claim_triggers,
                {
                    "claims_no_update",
                    "claims_no_delete",
                    "claims_no_replace",
                    "claims_validate_insert",
                },
            )
            self.assertEqual(integrity, "ok")
            self.assertEqual(foreign_keys, [])

            # A second open finds nothing left to migrate.
            self.assertEqual(check_journal(scope)["status"], "ok")
            self.assertEqual(self._migration_row(scope)[:2], (4, SCHEMA_VERSION))
            self.assertEqual(
                list(
                    (scope.journal_path.parent / "backups").glob(
                        "pre-migration-v*-to-v*.sqlite3"
                    )
                ),
                [backup_path],
            )

    def test_frozen_v4_fixture_without_leases_migrates_from_v3(self) -> None:
        """The 3 -> 4 hop, which adds `reader_leases` and nothing else.

        The v3 baseline is the frozen v4 fixture minus the one table
        schema 4 introduced, stated here rather than built by calling the
        schema module. The assertion below pins that derivation: if a
        later edit gives schema 4 a second statement, this fails loudly
        and asks for a frozen v3 fixture instead of quietly testing a
        baseline that never existed.
        """

        self.assertEqual(len(SCHEMA_V4_STATEMENTS), 1)
        self.assertIn("CREATE TABLE reader_leases", SCHEMA_V4_STATEMENTS[0])

        with tempfile.TemporaryDirectory() as temporary_directory:
            scope = self._scope(Path(temporary_directory))
            self._install_fixture(
                scope,
                FIXTURE_V4_PATH,
                schema_version=3,
                after=("DROP TABLE reader_leases",),
            )

            result = check_journal(scope)

            connection = sqlite3.connect(scope.journal_path)
            try:
                leases = connection.execute(
                    "SELECT COUNT(*) FROM reader_leases"
                ).fetchone()[0]
                lease_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(reader_leases)"
                    )
                }
                claim_locators = connection.execute(
                    """
                    SELECT holder_host, holder_sid
                    FROM claims
                    ORDER BY claim_id
                    """
                ).fetchall()
                foreign_keys = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
            finally:
                connection.close()

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["schema_version"], SCHEMA_VERSION)
            self.assertEqual(result["claims"], 3)
            # A migrated-in lease table starts empty: no session held the
            # reader role before the table existed to say so.
            self.assertEqual(leases, 0)
            self.assertLessEqual(
                {"lease_id", "epoch", "owner_id", "reader_id"},
                lease_columns,
            )
            # Both hops ran in the one pass, so the v5 columns are here too.
            self.assertEqual(
                claim_locators,
                [(None, None), (None, None), (None, None)],
            )
            self.assertEqual(foreign_keys, [])

            migration = self._migration_row(scope)
            self.assertEqual(migration[:2], (3, SCHEMA_VERSION))
            self.assertEqual(
                migration[2].split("-")[:5],
                ["pre", "migration", "v3", "to", f"v{SCHEMA_VERSION}"],
            )

            backup_path = scope.journal_path.parent / "backups" / migration[2]
            self.assertTrue(backup_path.is_file())
            backup = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
            try:
                backup_tables = {
                    row[0]
                    for row in backup.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                backup_version = backup.execute(
                    """
                    SELECT value
                    FROM journal_metadata
                    WHERE key = 'schema_version'
                    """
                ).fetchone()[0]
            finally:
                backup.close()

            self.assertEqual(backup_version, "3")
            self.assertNotIn("reader_leases", backup_tables)

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
