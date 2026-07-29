from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from aiq import journal, privacy
from aiq.journal import JournalError, JournalScope, ingest_message
from aiq.privacy import (
    EXPORT_FORMAT,
    destroy_journal,
    export_journal,
    plan_journal_destroy,
)
from aiq.queue import apply_effects, claim_message


class PrivacyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lock_state = tempfile.TemporaryDirectory()
        self.environment = patch.dict(
            os.environ,
            {"XDG_STATE_HOME": self.lock_state.name},
        )
        self.environment.start()
        self.addCleanup(self.lock_state.cleanup)
        self.addCleanup(self.environment.stop)

    def scope(self, root: Path) -> JournalScope:
        journal_directory = root / "state"
        return JournalScope(
            kind="agent-root",
            root=root / "agent",
            scope_id="scope_test",
            journal_path=journal_directory / "journal.sqlite3",
        )

    def lifecycle_lock(self):
        return patch.object(
            journal,
            "lifecycle_lock",
            create=True,
            side_effect=lambda _scope, *, exclusive: nullcontext(),
        )

    def test_lifecycle_lock_integration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.scope(root)
            ingest_message(scope, "private")
            output = root / "export.jsonl"

            export_journal(scope, output)
            plan = plan_journal_destroy(scope)
            result = destroy_journal(scope, plan["confirmation_token"])

            self.assertTrue(output.exists())
            self.assertEqual(result["status"], "destroyed")
            self.assertFalse(scope.journal_path.exists())

    def test_export_is_complete_deterministic_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.scope(root)
            content = "exact\nunicode: \N{SNOWMAN}\ncontrol: \u001b"
            message = ingest_message(scope, content, cwd=str(root))
            claim = claim_message(
                scope,
                owner_id="privacy-test",
                message_id=message.message_id,
            )
            assert claim is not None
            apply_effects(
                scope,
                message.message_id,
                {
                    "effects": [
                        [
                            "create",
                            "$task",
                            {
                                "objective": "Preserve semantic history",
                                "title": "Export safely",
                            },
                        ]
                    ],
                    "expect": {},
                    "v": 1,
                },
                claim_id=claim["claim_id"],
            )
            first = root / "first.jsonl"
            second = root / "second.jsonl"

            with self.lifecycle_lock():
                first_result = export_journal(scope, first)
                second_result = export_journal(scope, second)

            first_bytes = first.read_bytes()
            self.assertEqual(first_bytes, second.read_bytes())
            self.assertEqual(
                stat.S_IMODE(first.stat().st_mode),
                0o600,
            )
            records = [json.loads(line) for line in first_bytes.splitlines()]
            self.assertEqual(records[0]["format"], EXPORT_FORMAT)
            self.assertEqual(records[0]["content"], "full")
            self.assertTrue(records[0]["sensitive"])
            self.assertNotIn("source_scope", records[0])
            self.assertNotIn("schema_version", records[0])
            self.assertEqual(records[-1]["type"], "manifest")
            message = next(
                record["row"]
                for record in records
                if record.get("record_type") == "message"
            )
            self.assertEqual(message["content"], content)
            semantic_records = [
                record
                for record in records
                if record["type"] == "record"
            ]
            self.assertFalse(
                any(
                    key.endswith("_json")
                    for record in semantic_records
                    for key in record["row"]
                )
            )
            event = next(
                record["row"]
                for record in semantic_records
                if record["record_type"] == "event"
            )
            self.assertEqual(event["payload"], {})
            self.assertNotIn("payload_json", event)
            task = next(
                record["row"]
                for record in semantic_records
                if record["record_type"] == "task"
            )
            self.assertNotIn("task_number", task)
            revision = next(
                record["row"]
                for record in semantic_records
                if record["record_type"] == "task_revision"
            )
            self.assertEqual(revision["dependencies"], [])
            application = next(
                record["row"]
                for record in semantic_records
                if record["record_type"] == "message_application"
            )
            self.assertIsInstance(application["document"], dict)
            self.assertIsInstance(application["result"], dict)
            exported_types = {
                record.get("record_type")
                for record in records
                if record["type"] == "record"
            }
            self.assertNotIn("journal_metadata", exported_types)
            self.assertNotIn("schema_migration", exported_types)
            self.assertNotIn("task_number", exported_types)
            hashed_bytes = b"".join(
                line + b"\n" for line in first_bytes.splitlines()[:-1]
            )
            self.assertEqual(
                records[-1]["content_sha256"],
                hashlib.sha256(hashed_bytes).hexdigest(),
            )
            self.assertEqual(
                first_result["content_sha256"],
                records[-1]["content_sha256"],
            )
            self.assertEqual(first_result["bytes"], len(first_bytes))

    def test_export_refuses_existing_and_managed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.scope(root)
            ingest_message(scope, "private")
            existing = root / "existing.jsonl"
            existing.write_text("preserve")

            with self.lifecycle_lock():
                with self.assertRaisesRegex(JournalError, "already exists"):
                    export_journal(scope, existing)
                with self.assertRaisesRegex(JournalError, "managed journal"):
                    export_journal(
                        scope,
                        scope.journal_path.parent / "export.jsonl",
                    )

            self.assertEqual(existing.read_text(), "preserve")

    def test_export_failure_does_not_publish_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.scope(root)
            ingest_message(scope, "private")
            output = root / "failed.jsonl"

            with (
                self.lifecycle_lock(),
                patch("aiq.privacy._export_rows", side_effect=RuntimeError("fail")),
            ):
                with self.assertRaisesRegex(RuntimeError, "fail"):
                    export_journal(scope, output)

            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".failed.jsonl.*.tmp")), [])

    def test_destroy_requires_current_plan_and_removes_only_managed_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.scope(root)
            ingest_message(scope, "private")
            backup_directory = scope.journal_path.parent / "backups"
            backup_directory.mkdir(mode=0o700)
            backup = (
                backup_directory
                / "journal-20260101T010203000000Z-"
                "0123456789abcdef0123456789abcdef.sqlite3"
            )
            backup.write_bytes(b"backup")
            backup.chmod(0o600)
            wal = Path(f"{scope.journal_path}-wal")
            wal.write_bytes(b"wal")
            wal.chmod(0o600)
            rollback_journal = Path(f"{scope.journal_path}-journal")
            rollback_journal.write_bytes(b"rollback")
            rollback_journal.chmod(0o600)
            external = root / "external-export.jsonl"
            external.write_text("keep")

            calls: list[bool] = []

            def lock(_scope, *, exclusive):
                calls.append(exclusive)
                return nullcontext()

            with patch.object(
                journal,
                "lifecycle_lock",
                create=True,
                side_effect=lock,
            ):
                plan = plan_journal_destroy(scope)
                self.assertEqual(
                    plan["targets"],
                    [
                        {
                            "kind": "backup_directory",
                            "path": "backups",
                            "size": None,
                        },
                        {
                            "kind": "snapshot",
                            "path": f"backups/{backup.name}",
                            "size": 6,
                        },
                        {
                            "kind": "journal",
                            "path": "journal.sqlite3",
                            "size": scope.journal_path.stat().st_size,
                        },
                        {
                            "kind": "rollback_journal",
                            "path": "journal.sqlite3-journal",
                            "size": 8,
                        },
                        {
                            "kind": "wal",
                            "path": "journal.sqlite3-wal",
                            "size": 3,
                        },
                    ],
                )
                with self.assertRaisesRegex(JournalError, "confirmation"):
                    destroy_journal(scope, "wrong")
                result = destroy_journal(scope, plan["confirmation_token"])

            self.assertEqual(calls, [False, True, True])
            self.assertEqual(result["status"], "destroyed")
            self.assertFalse(scope.journal_path.exists())
            self.assertFalse(wal.exists())
            self.assertFalse(rollback_journal.exists())
            self.assertFalse(backup_directory.exists())
            self.assertEqual(external.read_text(), "keep")
            self.assertTrue(scope.journal_path.parent.is_dir())

    def test_destroy_rejects_stale_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.scope(root)
            ingest_message(scope, "private")

            with self.lifecycle_lock():
                plan = plan_journal_destroy(scope)
                wal = Path(f"{scope.journal_path}-wal")
                wal.write_bytes(b"new committed state")
                wal.chmod(0o600)
                with self.assertRaisesRegex(JournalError, "stale"):
                    destroy_journal(scope, plan["confirmation_token"])

            self.assertTrue(scope.journal_path.exists())
            self.assertTrue(wal.exists())

    def test_destroy_rejects_unmanaged_or_unsafe_backup_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.scope(root)
            ingest_message(scope, "private")
            backup_directory = scope.journal_path.parent / "backups"
            backup_directory.mkdir(mode=0o700)
            unknown = backup_directory / "notes.txt"
            unknown.write_text("user data")

            with self.lifecycle_lock():
                with self.assertRaisesRegex(JournalError, "unmanaged entry"):
                    plan_journal_destroy(scope)

            self.assertEqual(unknown.read_text(), "user data")
            self.assertTrue(scope.journal_path.exists())

    def test_destroy_rejects_nonprivate_managed_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.scope(root)
            ingest_message(scope, "private")
            scope.journal_path.parent.chmod(0o755)

            with self.lifecycle_lock():
                with self.assertRaisesRegex(JournalError, "private directory"):
                    plan_journal_destroy(scope)

            self.assertTrue(scope.journal_path.exists())

    def test_destroy_does_not_follow_managed_name_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.scope(root)
            ingest_message(scope, "private")
            backup_directory = scope.journal_path.parent / "backups"
            backup_directory.mkdir(mode=0o700)
            external = root / "external"
            external.write_text("preserve")
            unsafe = (
                backup_directory
                / "journal-20260101T010203000000Z-"
                "0123456789abcdef0123456789abcdef.sqlite3"
            )
            unsafe.symlink_to(external)

            with self.lifecycle_lock():
                with self.assertRaisesRegex(JournalError, "unsafe"):
                    plan_journal_destroy(scope)

            self.assertEqual(external.read_text(), "preserve")
            self.assertTrue(unsafe.is_symlink())
            self.assertTrue(scope.journal_path.exists())

    def test_destroy_rejects_intermediate_directory_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.scope(root)
            ingest_message(scope, "private")
            backup_directory = scope.journal_path.parent / "backups"
            backup_directory.mkdir(mode=0o700)
            backup_name = (
                "journal-20260101T010203000000Z-"
                "0123456789abcdef0123456789abcdef.sqlite3"
            )
            backup = backup_directory / backup_name
            backup.write_bytes(b"managed")
            backup.chmod(0o600)
            external_directory = root / "external"
            external_directory.mkdir(mode=0o700)
            external = external_directory / backup_name
            external.write_bytes(b"external")
            plan = None

            with self.lifecycle_lock():
                plan = plan_journal_destroy(scope)

            original_token = privacy._inventory_token
            swapped_directory = scope.journal_path.parent / "backups-original"

            def swap_after_token(scope_argument, inventory):
                token = original_token(scope_argument, inventory)
                backup_directory.rename(swapped_directory)
                backup_directory.symlink_to(
                    external_directory,
                    target_is_directory=True,
                )
                return token

            with (
                self.lifecycle_lock(),
                patch(
                    "aiq.privacy._inventory_token",
                    side_effect=swap_after_token,
                ),
            ):
                with self.assertRaisesRegex(JournalError, "private directory"):
                    destroy_journal(scope, plan["confirmation_token"])

            self.assertEqual(external.read_bytes(), b"external")
            self.assertEqual(
                (swapped_directory / backup_name).read_bytes(),
                b"managed",
            )
            self.assertTrue(scope.journal_path.exists())

    def test_destroy_absent_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self.scope(root)
            scope.journal_path.parent.mkdir(mode=0o700)

            with self.lifecycle_lock():
                plan = plan_journal_destroy(scope)
                result = destroy_journal(scope, plan["confirmation_token"])

            self.assertEqual(plan["status"], "already_absent")
            self.assertEqual(result["status"], "already_absent")
            self.assertFalse(scope.journal_path.exists())


if __name__ == "__main__":
    unittest.main()
