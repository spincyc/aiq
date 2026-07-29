from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiq.events import (
    CONTENT_MAX_BYTES,
    CONTEXT_ID_MAX_BYTES,
    CWD_MAX_BYTES,
    IDEMPOTENCY_KEY_MAX_BYTES,
    SOURCE_MAX_BYTES,
)
from aiq.journal import (
    JournalError,
    ingest_message,
    list_inbox,
    resolve_scope,
)
from aiq.queue import claim_message, dispose_message


class IngestIdentityTest(unittest.TestCase):
    def scope(self, root: Path):
        agent_root = root / "agent-root"
        agent_root.mkdir(parents=True)
        return resolve_scope(
            "agent-root",
            cwd=root,
            agent_root=agent_root,
        )

    def test_explicit_key_requires_complete_identity_match(self) -> None:
        variants = {
            "content": {"content": "changed"},
            "source": {"source": "changed-source"},
            "session_id": {"session_id": "changed-session"},
            "turn_id": {"turn_id": "changed-turn"},
            "cwd": {"cwd": "/changed"},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(root / "state")},
            ):
                for index, (field, change) in enumerate(variants.items()):
                    with self.subTest(field=field):
                        scope = self.scope(root / f"case-{index}")
                        original = {
                            "content": "exact content",
                            "source": "test-source",
                            "idempotency_key": "same-key",
                            "session_id": "session",
                            "turn_id": "turn",
                            "cwd": "/original",
                        }
                        ingest_message(scope, **original)
                        with self.assertRaisesRegex(
                            JournalError,
                            "different message identity",
                        ):
                            ingest_message(
                                scope,
                                **{**original, **change},
                            )
                        self.assertEqual(len(list_inbox(scope)), 1)

    def test_explicit_key_exact_retry_reuses_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(root / "state")},
            ):
                scope = self.scope(root)
                arguments = {
                    "content": "exact content",
                    "source": "test-source",
                    "idempotency_key": "stable-key",
                    "session_id": "session",
                    "turn_id": "turn",
                    "cwd": "/working",
                }
                first = ingest_message(scope, **arguments)
                second = ingest_message(scope, **arguments)

                self.assertTrue(first.created)
                self.assertFalse(second.created)
                self.assertEqual(second.message_id, first.message_id)

    def test_derived_key_is_an_unambiguous_canonical_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(root / "state")},
            ):
                scope = self.scope(root)
                first = ingest_message(
                    scope,
                    "same content",
                    source="test-source",
                    session_id="a:b",
                    turn_id="c",
                )
                second = ingest_message(
                    scope,
                    "same content",
                    source="test-source",
                    session_id="a",
                    turn_id="b:c",
                )
                retry = ingest_message(
                    scope,
                    "same content",
                    source="test-source",
                    session_id="a:b",
                    turn_id="c",
                )

                self.assertNotEqual(first.message_id, second.message_id)
                self.assertEqual(retry.message_id, first.message_id)
                self.assertFalse(retry.created)
                self.assertEqual(len(list_inbox(scope)), 2)

    def test_if_new_deduplicates_identical_unapplied_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(root / "state")},
            ):
                scope = self.scope(root)
                first = ingest_message(
                    scope,
                    "same request",
                    source="test-source",
                    if_new=True,
                )
                duplicate = ingest_message(
                    scope,
                    "same request",
                    source="test-source",
                    if_new=True,
                )
                different = ingest_message(
                    scope,
                    "other request",
                    source="test-source",
                    if_new=True,
                )
                without_flag = ingest_message(
                    scope,
                    "same request",
                    source="test-source",
                )

                self.assertTrue(first.created)
                self.assertFalse(first.deduped)
                self.assertFalse(duplicate.created)
                self.assertTrue(duplicate.deduped)
                self.assertEqual(duplicate.message_id, first.message_id)
                self.assertEqual(duplicate.state, "received")
                self.assertTrue(different.created)
                self.assertFalse(different.deduped)
                # Without the flag an identical message stores normally.
                self.assertTrue(without_flag.created)
                self.assertEqual(len(list_inbox(scope)), 3)

    def test_if_new_matches_needs_input_but_not_settled_messages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(root / "state")},
            ):
                scope = self.scope(root)
                parked = ingest_message(
                    scope,
                    "parked content",
                    source="test-source",
                )
                claim = claim_message(
                    scope,
                    owner_id="worker",
                    message_id=parked.message_id,
                )
                assert claim is not None
                dispose_message(
                    scope,
                    parked.message_id,
                    claim_id=claim["claim_id"],
                    disposition="needs_input",
                    reason="waiting for input",
                )
                parked_match = ingest_message(
                    scope,
                    "parked content",
                    source="test-source",
                    if_new=True,
                )
                self.assertTrue(parked_match.deduped)
                self.assertEqual(parked_match.message_id, parked.message_id)
                self.assertEqual(parked_match.state, "needs_input")

                failed = ingest_message(
                    scope,
                    "failed content",
                    source="test-source",
                )
                failed_claim = claim_message(
                    scope,
                    owner_id="worker",
                    message_id=failed.message_id,
                )
                assert failed_claim is not None
                dispose_message(
                    scope,
                    failed.message_id,
                    claim_id=failed_claim["claim_id"],
                    disposition="failed",
                    reason="unprocessable",
                )
                after_failure = ingest_message(
                    scope,
                    "failed content",
                    source="test-source",
                    if_new=True,
                )
                self.assertTrue(after_failure.created)
                self.assertFalse(after_failure.deduped)

                processing = ingest_message(
                    scope,
                    "processing content",
                    source="test-source",
                )
                claim_message(
                    scope,
                    owner_id="worker",
                    message_id=processing.message_id,
                )
                while_processing = ingest_message(
                    scope,
                    "processing content",
                    source="test-source",
                    if_new=True,
                )
                self.assertTrue(while_processing.created)
                self.assertFalse(while_processing.deduped)

    def test_core_ingestion_rejects_noncanonical_fields_before_mutation(
        self,
    ) -> None:
        invalid_arguments = {
            "empty content": {"content": ""},
            "oversized content": {
                "content": "x" * (CONTENT_MAX_BYTES + 1),
            },
            "non-string content": {"content": 1},
            "invalid source syntax": {"source": "Uppercase"},
            "oversized source": {"source": "a" * (SOURCE_MAX_BYTES + 1)},
            "non-string source": {"source": 1},
            "empty idempotency key": {"idempotency_key": ""},
            "oversized idempotency key": {
                "idempotency_key": "i" * (IDEMPOTENCY_KEY_MAX_BYTES + 1),
            },
            "non-string idempotency key": {"idempotency_key": 1},
            "empty session id": {"session_id": ""},
            "oversized session id": {
                "session_id": "s" * (CONTEXT_ID_MAX_BYTES + 1),
            },
            "non-string session id": {"session_id": 1},
            "empty turn id": {"turn_id": ""},
            "oversized turn id": {
                "turn_id": "t" * (CONTEXT_ID_MAX_BYTES + 1),
            },
            "non-string turn id": {"turn_id": 1},
            "relative cwd": {"cwd": "relative/path"},
            "cwd containing NUL": {"cwd": "/path\0suffix"},
            "oversized cwd": {"cwd": "/" + "c" * CWD_MAX_BYTES},
            "non-string cwd": {"cwd": 1},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(root / "state")},
            ):
                for label, changes in invalid_arguments.items():
                    with self.subTest(label=label):
                        scope = self.scope(root / label.replace(" ", "-"))
                        arguments = {
                            "content": "content",
                            "source": "test-source",
                            **changes,
                        }
                        with self.assertRaises(JournalError):
                            ingest_message(scope, **arguments)
                        self.assertFalse(scope.journal_path.exists())

    def test_core_ingestion_accepts_exact_field_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(root / "state")},
            ):
                scope = self.scope(root)
                result = ingest_message(
                    scope,
                    "x" * CONTENT_MAX_BYTES,
                    source="a" * SOURCE_MAX_BYTES,
                    idempotency_key="i" * IDEMPOTENCY_KEY_MAX_BYTES,
                    session_id="s" * CONTEXT_ID_MAX_BYTES,
                    turn_id="t" * CONTEXT_ID_MAX_BYTES,
                    cwd="/" + "c" * (CWD_MAX_BYTES - 1),
                )

                self.assertTrue(result.created)


if __name__ == "__main__":
    unittest.main()
