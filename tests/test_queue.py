from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from aiq import queue as queue_module
from aiq.journal import (
    JournalError,
    check_journal,
    ingest_message,
    list_inbox,
    resolve_scope,
)
from aiq.queue import (
    TASK_STATES,
    TRANSITIONS,
    _effective_states,
    _now_us,
    _validate_graph,
    apply_effects,
    claim_message,
    claim_next_tasks,
    claim_task,
    dispose_message,
    enqueue_task,
    explain_task,
    list_claims,
    list_tasks,
    next_tasks,
    overview_tasks,
    parse_effect_document,
    read_status,
    release_claim,
    settle_tasks_done,
    show_task,
    task_history,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TRANSITIONS = {
    "queued": {"ready", "blocked", "canceled", "superseded"},
    "ready": {"queued", "active", "blocked", "canceled", "superseded"},
    "active": {"queued", "ready", "blocked", "done", "canceled", "superseded"},
    "blocked": {"queued", "ready", "canceled", "superseded"},
    "done": set(),
    "canceled": set(),
    "superseded": set(),
}


class QueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.environment = patch.dict(
            os.environ,
            {"XDG_STATE_HOME": str(self.root / "state")},
        )
        self.environment.start()
        agent_root = self.root / "agent"
        agent_root.mkdir()
        self.scope = resolve_scope(
            "agent-root",
            cwd=self.root,
            agent_root=agent_root,
        )
        self.message_claims: dict[str, str] = {}

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary_directory.cleanup()

    def ingest(self, content: str):
        return ingest_message(self.scope, content, cwd=str(self.root))

    def apply(self, message_id: str, document: dict):
        claim_id = self.message_claims.get(message_id)
        if claim_id is None:
            claim = claim_message(
                self.scope,
                owner_id="queue-test",
                message_id=message_id,
            )
            assert claim is not None
            claim_id = claim["claim_id"]
            self.message_claims[message_id] = claim_id
        return apply_effects(
            self.scope,
            message_id,
            document,
            claim_id=claim_id,
        )

    def test_create_dependency_and_queue_readiness(self) -> None:
        message = self.ingest("Create implementation and documentation tasks")
        result = self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [
                    ["create", "$implementation", {"title": "Implement queue"}],
                    [
                        "create",
                        "$docs",
                        {
                            "title": "Document queue",
                            "priority": 100,
                            "requires": ["$implementation"],
                        },
                    ],
                ],
            },
        )

        implementation = result["aliases"]["$implementation"]
        documentation = result["aliases"]["$docs"]
        self.assertEqual(
            [task["task_id"] for task in next_tasks(self.scope)],
            [implementation],
        )
        self.assertEqual(show_task(self.scope, documentation)["state"], "queued")
        self.assertEqual(list_inbox(self.scope), [])

        claimed = [claim_task(self.scope, implementation, owner_id="worker")]
        self.assertEqual(claimed[0]["task"]["task_id"], implementation)
        task_claim_id = claimed[0]["claim"]["claim_id"]

        done_message = self.ingest("Implementation verified")
        self.apply(
            done_message.message_id,
            {
                "v": 1,
                "expect": {implementation: 1},
                "effects": [
                    [
                        "transition",
                        implementation,
                        "done",
                        {"claim": task_claim_id},
                    ]
                ],
            },
        )

        self.assertEqual(
            [task["task_id"] for task in next_tasks(self.scope)],
            [documentation],
        )

    def test_priority_orders_only_ready_tasks(self) -> None:
        message = self.ingest("Create prioritized work")
        result = self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [
                    ["create", "$low", {"title": "Low", "priority": -5}],
                    ["create", "$high", {"title": "High", "priority": 20}],
                ],
            },
        )

        ordered = next_tasks(self.scope, limit=2)

        self.assertEqual(
            [task["task_id"] for task in ordered],
            [result["aliases"]["$high"], result["aliases"]["$low"]],
        )

    def test_application_retry_and_conflict(self) -> None:
        message = self.ingest("Create one task")
        document = {
            "v": 1,
            "expect": {},
            "effects": [["create", "$one", {"title": "One"}]],
        }

        first = self.apply(message.message_id, document)
        second = self.apply(message.message_id, document)

        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["aliases"], second["aliases"])
        self.assertEqual(len(list_tasks(self.scope)), 1)

        before_rejected_replay = list_tasks(self.scope)
        with self.assertRaisesRegex(
            JournalError,
            "replay claim does not match the original claim",
        ):
            apply_effects(
                self.scope,
                message.message_id,
                document,
                claim_id="clm_" + "0" * 32,
            )
        self.assertEqual(list_tasks(self.scope), before_rejected_replay)

        with self.assertRaisesRegex(
            JournalError,
            "different effects application",
        ):
            self.apply(
                message.message_id,
                {
                    "v": 1,
                    "expect": {},
                    "effects": [["create", "$other", {"title": "Other"}]],
                },
            )
        self.assertEqual(len(list_tasks(self.scope)), 1)

    def test_invalid_late_effect_rolls_back_entire_batch(self) -> None:
        message = self.ingest("This batch must be atomic")
        with self.assertRaisesRegex(JournalError, "task not found"):
            self.apply(
                message.message_id,
                {
                    "v": 1,
                    "expect": {},
                    "effects": [
                        ["create", "$valid", {"title": "Would be valid"}],
                        ["update", "TASK-999", {"title": "Missing"}],
                    ],
                },
            )

        self.assertEqual(list_tasks(self.scope), [])
        self.assertEqual(list_inbox(self.scope)[0]["message_id"], message.message_id)

        result = self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [["create", "$valid", {"title": "Now valid"}]],
            },
        )
        self.assertEqual(result["aliases"]["$valid"], "TASK-1")

    def test_revision_fence_and_dependency_cycle(self) -> None:
        message = self.ingest("Create two tasks")
        result = self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [
                    ["create", "$a", {"title": "A"}],
                    ["create", "$b", {"title": "B"}],
                ],
            },
        )
        task_a = result["aliases"]["$a"]
        task_b = result["aliases"]["$b"]

        stale_message = self.ingest("Use a stale revision")
        with self.assertRaisesRegex(JournalError, "revision changed"):
            self.apply(
                stale_message.message_id,
                {
                    "v": 1,
                    "expect": {task_a: 99},
                    "effects": [["update", task_a, {"priority": 1}]],
                },
            )

        cycle_message = self.ingest("Create a cycle")
        with self.assertRaisesRegex(JournalError, "dependency cycle"):
            self.apply(
                cycle_message.message_id,
                {
                    "v": 1,
                    "expect": {task_a: 1, task_b: 1},
                    "effects": [
                        ["require", task_a, task_b],
                        ["require", task_b, task_a],
                    ],
                },
            )
        self.assertEqual(show_task(self.scope, task_a)["revision"], 1)
        self.assertEqual(show_task(self.scope, task_b)["revision"], 1)

    def test_transition_matrix_rejects_every_undeclared_edge(self) -> None:
        self.assertEqual(TRANSITIONS, EXPECTED_TRANSITIONS)
        for source, destinations in EXPECTED_TRANSITIONS.items():
            for destination in EXPECTED_TRANSITIONS:
                if destination in destinations or destination == source:
                    continue
                with self.subTest(source=source, destination=destination):
                    message = self.ingest(f"Create {source} to {destination}")
                    create_effects = (
                        [
                            ["create", "$prerequisite", {"title": "Prerequisite"}],
                            [
                                "create",
                                "$task",
                                {
                                    "title": "Transition",
                                    "requires": ["$prerequisite"],
                                },
                            ],
                        ]
                        if source == "queued"
                        else [
                            ["create", "$task", {"title": "Transition"}],
                            *(
                                [["create", "$replacement", {"title": "Replacement"}]]
                                if source == "superseded"
                                else []
                            ),
                        ]
                    )
                    result = self.apply(
                        message.message_id,
                        {
                            "v": 1,
                            "expect": {},
                            "effects": create_effects,
                        },
                    )
                    task_id = result["aliases"]["$task"]

                    def move(to_state: str, metadata: dict | None = None) -> None:
                        setup_message = self.ingest(f"Move fixture to {to_state}")
                        current_revision = show_task(self.scope, task_id)["revision"]
                        expectations = {task_id: current_revision}
                        if metadata and metadata.get("by"):
                            replacement_id = metadata["by"]
                            expectations[replacement_id] = show_task(
                                self.scope,
                                replacement_id,
                            )["revision"]
                        setup_effect = ["transition", task_id, to_state]
                        if metadata:
                            setup_effect.append(metadata)
                        self.apply(
                            setup_message.message_id,
                            {
                                "v": 1,
                                "expect": expectations,
                                "effects": [setup_effect],
                            },
                        )

                    if source == "active":
                        claim_task(
                            self.scope,
                            task_id,
                            owner_id="fixture",
                        )
                    elif source == "blocked":
                        move("blocked", {"reason": "fixture"})
                    elif source == "done":
                        task_claim = claim_task(
                            self.scope,
                            task_id,
                            owner_id="fixture",
                        )["claim"]["claim_id"]
                        move("done", {"claim": task_claim})
                    elif source == "canceled":
                        move("canceled", {"reason": "fixture"})
                    elif source == "superseded":
                        move(
                            "superseded",
                            {
                                "reason": "fixture",
                                "by": result["aliases"]["$replacement"],
                            },
                        )
                    transition_message = self.ingest("Reject transition")
                    current = show_task(self.scope, task_id)
                    metadata = (
                        {"reason": "invalid", "by": task_id}
                        if destination == "superseded"
                        else {"reason": "invalid"}
                        if destination in {"blocked", "canceled"}
                        else {}
                    )
                    effect = ["transition", task_id, destination]
                    if metadata:
                        effect.append(metadata)
                    with self.assertRaises(JournalError):
                        self.apply(
                            transition_message.message_id,
                            {
                                "v": 1,
                                "expect": {task_id: current["revision"]},
                                "effects": [effect],
                            },
                        )

    def test_effect_parser_is_strict_and_bounded(self) -> None:
        with self.assertRaisesRegex(JournalError, "duplicate JSON key"):
            parse_effect_document('{"v":1,"v":1,"expect":{},"effects":[]}')
        with self.assertRaisesRegex(JournalError, "unknown keys"):
            parse_effect_document(
                '{"v":1,"expect":{},"effects":[],"reason":"none","extra":1}'
            )
        with self.assertRaisesRegex(JournalError, "exceeds"):
            parse_effect_document(" " * 65537)
        with self.assertRaisesRegex(JournalError, r"document\.v must be 1"):
            parse_effect_document(
                '{"v":1.0,"expect":{},"effects":[],"reason":"float version"}'
            )
        with self.assertRaisesRegex(JournalError, "unknown operation"):
            parse_effect_document(
                '{"v":1,"expect":{},"effects":[[["not-a-string"]]]}'
            )

    def test_unknown_alias_reference_is_a_journal_error(self) -> None:
        message = self.ingest("Create referenced work")
        result = self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [["create", "$work", {"title": "Work"}]],
            },
        )
        work = result["aliases"]["$work"]
        attempts = (
            (
                "create.parent",
                {},
                [["create", "$child", {"title": "Child", "parent": "$missing"}]],
            ),
            (
                "create.requires",
                {},
                [["create", "$child", {"title": "Child", "requires": ["$missing"]}]],
            ),
            (
                "update.parent",
                {work: 1},
                [["update", work, {"parent": "$missing"}]],
            ),
            (
                "transition.by",
                {work: 1},
                [
                    [
                        "transition",
                        work,
                        "superseded",
                        {"reason": "replace", "by": "$missing"},
                    ]
                ],
            ),
        )
        for name, expect, effects in attempts:
            with self.subTest(position=name):
                attempt = self.ingest(f"Reject unknown alias in {name}")
                with self.assertRaisesRegex(
                    JournalError,
                    r"unknown local task alias: \$missing",
                ):
                    self.apply(
                        attempt.message_id,
                        {"v": 1, "expect": expect, "effects": effects},
                    )
        self.assertEqual(show_task(self.scope, work)["revision"], 1)

    def test_apply_rejects_oversized_document_dict(self) -> None:
        message = self.ingest("Reject oversized document")
        document = {
            "v": 1,
            "expect": {},
            "effects": [
                [
                    "create",
                    f"$task-{index}",
                    {"title": "Padded", "objective": "x" * 2000},
                ]
                for index in range(40)
            ],
        }
        with self.assertRaisesRegex(JournalError, "exceeds 65536 bytes"):
            self.apply(message.message_id, document)
        self.assertEqual(list_tasks(self.scope), [])

    def test_read_snapshot_ignores_writer_between_selects(self) -> None:
        setup_message = self.ingest("Create surviving work")
        survivor = self.apply(
            setup_message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [["create", "$survivor", {"title": "Survivor"}]],
            },
        )["aliases"]["$survivor"]
        contested_message = self.ingest("Create contested work")
        fired = threading.Event()

        def write_between_selects() -> int:
            if not fired.is_set():
                fired.set()
                created = self.apply(
                    contested_message.message_id,
                    {
                        "v": 1,
                        "expect": {},
                        "effects": [["create", "$contested", {"title": "Contested"}]],
                    },
                )
                claim_task(
                    self.scope,
                    created["aliases"]["$contested"],
                    owner_id="racer",
                )
            return _now_us()

        # _read_snapshot pins its read snapshot before sampling the clock
        # via _now_us; a writer committing a new claimed task at that
        # moment must not be visible to the snapshot's later SELECTs.
        with patch("aiq.queue._now_us", write_between_selects):
            listed = list_tasks(self.scope)

        self.assertTrue(fired.is_set())
        self.assertEqual([task["task_id"] for task in listed], [survivor])
        self.assertEqual(len(list_tasks(self.scope)), 2)

    def test_failed_dependency_cannot_be_activated(self) -> None:
        message = self.ingest("Create dependent work")
        result = self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [
                    ["create", "$dependency", {"title": "Dependency"}],
                    [
                        "create",
                        "$dependent",
                        {
                            "title": "Dependent",
                            "requires": ["$dependency"],
                        },
                    ],
                ],
            },
        )
        dependency = result["aliases"]["$dependency"]
        dependent = result["aliases"]["$dependent"]
        cancel_message = self.ingest("Cancel dependency")
        self.apply(
            cancel_message.message_id,
            {
                "v": 1,
                "expect": {dependency: 1},
                "effects": [
                    [
                        "transition",
                        dependency,
                        "canceled",
                        {"reason": "cannot proceed"},
                    ]
                ],
            },
        )
        self.assertEqual(
            claim_next_tasks(self.scope, owner_id="worker"),
            [],
        )
        self.assertEqual(show_task(self.scope, dependent)["state"], "blocked")
        with self.assertRaisesRegex(
            JournalError,
            f"task is not ready: {dependent}: blocked",
        ):
            claim_task(self.scope, dependent, owner_id="worker")

    def test_supersession_cycle_is_rejected_atomically(self) -> None:
        message = self.ingest("Create alternatives")
        result = self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [
                    ["create", "$a", {"title": "A"}],
                    ["create", "$b", {"title": "B"}],
                    ["create", "$c", {"title": "C"}],
                ],
            },
        )
        task_a = result["aliases"]["$a"]
        task_b = result["aliases"]["$b"]
        task_c = result["aliases"]["$c"]
        cycle_message = self.ingest("Create an indirect supersession cycle")
        with self.assertRaisesRegex(JournalError, "supersession cycle"):
            self.apply(
                cycle_message.message_id,
                {
                    "v": 1,
                    "expect": {task_a: 1, task_b: 1, task_c: 1},
                    "effects": [
                        [
                            "transition",
                            task_a,
                            "superseded",
                            {"reason": "B replaces A", "by": task_b},
                        ],
                        [
                            "transition",
                            task_b,
                            "superseded",
                            {"reason": "C replaces B", "by": task_c},
                        ],
                        [
                            "transition",
                            task_c,
                            "superseded",
                            {"reason": "A replaces C", "by": task_a},
                        ],
                    ],
                },
            )
        self.assertEqual(show_task(self.scope, task_a)["revision"], 1)
        self.assertEqual(show_task(self.scope, task_b)["revision"], 1)
        self.assertEqual(show_task(self.scope, task_c)["revision"], 1)

    def test_supersession_rejects_invalid_replacement_targets(self) -> None:
        message = self.ingest("Create work and a canceled replacement")
        result = self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [
                    ["create", "$work", {"title": "Work"}],
                    ["create", "$replacement", {"title": "Replacement"}],
                ],
            },
        )
        work = result["aliases"]["$work"]
        replacement = result["aliases"]["$replacement"]
        cancel_message = self.ingest("Cancel replacement")
        self.apply(
            cancel_message.message_id,
            {
                "v": 1,
                "expect": {replacement: 1},
                "effects": [
                    [
                        "transition",
                        replacement,
                        "canceled",
                        {"reason": "not viable"},
                    ]
                ],
            },
        )

        attempts = (
            (
                "self",
                work,
                f"task cannot supersede itself: {work}",
                {work: 1},
            ),
            (
                "missing",
                "TASK-999",
                "replacement task not found: TASK-999",
                {work: 1},
            ),
            (
                "canceled",
                replacement,
                f"replacement task is not eligible: {replacement}: canceled",
                {work: 1, replacement: 2},
            ),
        )
        for name, target, error, expectations in attempts:
            with self.subTest(target=name):
                attempt = self.ingest(f"Reject {name} replacement")
                with self.assertRaisesRegex(JournalError, error):
                    self.apply(
                        attempt.message_id,
                        {
                            "v": 1,
                            "expect": expectations,
                            "effects": [
                                [
                                    "transition",
                                    work,
                                    "superseded",
                                    {"reason": "replace", "by": target},
                                ]
                            ],
                        },
                    )
                self.assertEqual(show_task(self.scope, work)["revision"], 1)

    def test_supersession_chain_may_resolve_to_done_replacement(self) -> None:
        message = self.ingest("Create replacement chain")
        result = self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [
                    ["create", "$original", {"title": "Original"}],
                    ["create", "$interim", {"title": "Interim"}],
                    ["create", "$final", {"title": "Final"}],
                ],
            },
        )
        original = result["aliases"]["$original"]
        interim = result["aliases"]["$interim"]
        final = result["aliases"]["$final"]
        final_claim = claim_task(
            self.scope,
            final,
            owner_id="worker",
        )["claim"]["claim_id"]
        done_message = self.ingest("Finish final replacement")
        self.apply(
            done_message.message_id,
            {
                "v": 1,
                "expect": {final: 1},
                "effects": [
                    ["transition", final, "done", {"claim": final_claim}],
                ],
            },
        )
        supersede_message = self.ingest("Use replacement chain")
        self.apply(
            supersede_message.message_id,
            {
                "v": 1,
                "expect": {original: 1, interim: 1, final: 2},
                "effects": [
                    [
                        "transition",
                        interim,
                        "superseded",
                        {"reason": "use final", "by": final},
                    ],
                    [
                        "transition",
                        original,
                        "superseded",
                        {"reason": "use interim chain", "by": interim},
                    ],
                ],
            },
        )

        self.assertEqual(
            show_task(self.scope, original)["superseded_by_task_id"],
            interim,
        )
        self.assertEqual(
            show_task(self.scope, interim)["superseded_by_task_id"],
            final,
        )

    def test_graph_checks_are_iterative_for_deep_chains(self) -> None:
        size = 2500

        def task(task_number: int) -> dict:
            return {
                "task_id": f"TASK-{task_number}",
                "state": "queued",
                "parent_task_id": None,
                "superseded_by_task_id": None,
                "dependencies": [],
                "claim": None,
            }

        tasks = {
            f"TASK-{task_number}": task(task_number)
            for task_number in range(1, size + 1)
        }
        for task_number in range(2, size + 1):
            tasks[f"TASK-{task_number}"]["dependencies"] = [
                f"TASK-{task_number - 1}"
            ]

        _validate_graph(tasks)
        states = _effective_states(tasks)

        self.assertEqual(states["TASK-1"], "ready")
        self.assertEqual(states[f"TASK-{size}"], "queued")

        for task_number in range(1, size):
            current = tasks[f"TASK-{task_number}"]
            current["dependencies"] = []
            current["state"] = "superseded"
            current["superseded_by_task_id"] = f"TASK-{task_number + 1}"
        tasks[f"TASK-{size}"]["dependencies"] = []
        _validate_graph(tasks)

    def test_append_only_replace_is_rejected_and_audit_passes(self) -> None:
        message = self.ingest("immutable")
        connection = sqlite3.connect(self.scope.journal_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT OR REPLACE INTO messages
                    SELECT
                      message_id,
                      received_at,
                      source,
                      'replaced',
                      content_sha256,
                      idempotency_key,
                      session_id,
                      turn_id,
                      cwd
                    FROM messages
                    WHERE message_id = ?
                    """,
                    (message.message_id,),
                )
        finally:
            connection.close()
        self.assertEqual(check_journal(self.scope)["status"], "ok")

    def test_audit_detects_revision_that_disagrees_with_effect(self) -> None:
        message = self.ingest("Create auditable work")
        self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [["create", "$work", {"title": "Original"}]],
            },
        )
        connection = sqlite3.connect(self.scope.journal_path)
        try:
            connection.executescript(
                """
                DROP TRIGGER task_revisions_no_update;
                UPDATE task_revisions SET title = 'Corrupted';
                CREATE TRIGGER task_revisions_no_update
                BEFORE UPDATE ON task_revisions
                BEGIN
                  SELECT RAISE(ABORT, 'task_revisions are append-only');
                END;
                """
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(JournalError, "creation revision mismatch"):
            check_journal(self.scope)

    def test_audit_detects_sealed_effect_without_task_revision(self) -> None:
        create_message = self.ingest("Create auditable work")
        created = self.apply(
            create_message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [["create", "$work", {"title": "Original"}]],
            },
        )
        task_id = created["aliases"]["$work"]
        update_message = self.ingest("Revise auditable work")
        self.apply(
            update_message.message_id,
            {
                "v": 1,
                "expect": {task_id: 1},
                "effects": [["update", task_id, {"title": "Revised"}]],
            },
        )
        connection = sqlite3.connect(self.scope.journal_path)
        try:
            connection.executescript(
                """
                DROP TRIGGER task_revisions_no_delete;
                """
            )
            connection.execute(
                """
                DELETE FROM task_revisions
                WHERE task_id = ? AND revision = 2
                """,
                (task_id,),
            )
            connection.executescript(
                """
                CREATE TRIGGER task_revisions_no_delete
                BEFORE DELETE ON task_revisions
                BEGIN
                  SELECT RAISE(ABORT, 'task_revisions are append-only');
                END;
                """
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(
            JournalError,
            "sealed task effect has no task revision",
        ):
            check_journal(self.scope)

    def test_message_claim_race_has_one_winner(self) -> None:
        message = self.ingest("Claim exactly once")
        barrier = threading.Barrier(8)

        def compete(index: int):
            barrier.wait()
            try:
                return claim_message(
                    self.scope,
                    owner_id=f"owner-{index}",
                    message_id=message.message_id,
                )
            except JournalError:
                return None

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(compete, range(8)))

        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["message"]["content"], "Claim exactly once")

    def test_message_disposition_is_parked_and_retryable(self) -> None:
        message = self.ingest("Needs a decision")
        claim = claim_message(
            self.scope,
            owner_id="worker",
            message_id=message.message_id,
        )
        assert claim is not None

        first = dispose_message(
            self.scope,
            message.message_id,
            claim_id=claim["claim_id"],
            disposition="needs_input",
            reason="Choose a backend",
        )
        second = dispose_message(
            self.scope,
            message.message_id,
            claim_id=claim["claim_id"],
            disposition="needs_input",
            reason="Choose a backend",
        )

        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(list_inbox(self.scope)[0]["state"], "needs_input")
        self.assertIsNone(claim_message(self.scope, owner_id="other"))
        self.assertEqual(check_journal(self.scope)["status"], "ok")

    def test_needs_input_message_resumes_through_explicit_claim(self) -> None:
        message = self.ingest("Parked until input arrives")
        claim = claim_message(
            self.scope,
            owner_id="worker",
            message_id=message.message_id,
        )
        assert claim is not None
        dispose_message(
            self.scope,
            message.message_id,
            claim_id=claim["claim_id"],
            disposition="needs_input",
            reason="need a decision",
        )

        # An unaddressed claim never draws a parked message; only the
        # explicit MESSAGE_ID resumes it once the input has arrived.
        self.assertIsNone(claim_message(self.scope, owner_id="worker"))
        resumed = claim_message(
            self.scope,
            owner_id="worker",
            message_id=message.message_id,
        )
        assert resumed is not None
        self.assertEqual(
            resumed["message"]["content"],
            "Parked until input arrives",
        )

        applied = apply_effects(
            self.scope,
            message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [["create", "$resumed", {"title": "Resumed work"}]],
            },
            claim_id=resumed["claim_id"],
        )

        self.assertEqual(applied["status"], "applied")
        self.assertEqual(list_inbox(self.scope), [])
        self.assertEqual(check_journal(self.scope)["status"], "ok")

    def test_resumed_needs_input_message_can_fail_terminally(self) -> None:
        message = self.ingest("Parked and then abandoned")
        claim = claim_message(
            self.scope,
            owner_id="worker",
            message_id=message.message_id,
        )
        assert claim is not None
        dispose_message(
            self.scope,
            message.message_id,
            claim_id=claim["claim_id"],
            disposition="needs_input",
            reason="waiting for input",
        )
        resumed = claim_message(
            self.scope,
            owner_id="worker",
            message_id=message.message_id,
        )
        assert resumed is not None

        dispose_message(
            self.scope,
            message.message_id,
            claim_id=resumed["claim_id"],
            disposition="failed",
            reason="the input never arrived",
        )

        self.assertEqual(list_inbox(self.scope)[0]["state"], "failed")
        with self.assertRaisesRegex(JournalError, "not claimable"):
            claim_message(
                self.scope,
                owner_id="worker",
                message_id=message.message_id,
            )
        self.assertEqual(check_journal(self.scope)["status"], "ok")

    def test_enqueue_is_one_recorded_and_applied_transaction(self) -> None:
        first = enqueue_task(
            self.scope,
            title="Enqueued work",
            objective="Reach the goal",
            priority=9,
            owner_id="worker",
        )
        dependent = enqueue_task(
            self.scope,
            title="Dependent work",
            requires=[first["task_id"]],
            owner_id="worker",
        )

        self.assertEqual(first["state"], "ready")
        self.assertEqual(dependent["state"], "queued")
        shown = show_task(self.scope, first["task_id"])
        self.assertEqual(shown["created_by_message_id"], first["message_id"])
        self.assertEqual(shown["title"], "Enqueued work")
        self.assertEqual(shown["objective"], "Reach the goal")
        self.assertEqual(shown["priority"], 9)
        self.assertEqual(
            show_task(self.scope, dependent["task_id"])["dependencies"],
            [first["task_id"]],
        )
        # Both auto-generated messages were applied inside their own
        # transaction: nothing is pending and no claim remains held.
        self.assertEqual(list_inbox(self.scope), [])
        status = read_status(self.scope)
        self.assertEqual(status["messages"]["applied"], 2)
        self.assertEqual(status["claims"]["active"], 0)
        self.assertEqual(check_journal(self.scope)["status"], "ok")

    def test_enqueue_rolls_back_completely_on_any_failure(self) -> None:
        failures = (
            ({"title": "Bad", "requires": ["TASK-999"]}, "task not found"),
            ({"title": ""}, "title"),
            ({"title": "Bad", "requires": ["not-a-task"]}, "invalid task ID"),
            (
                {"title": "Bad", "requires": ["TASK-1", "TASK-1"]},
                "duplicate required",
            ),
        )
        for kwargs, error in failures:
            with self.subTest(error=error):
                with self.assertRaisesRegex(JournalError, error):
                    enqueue_task(self.scope, owner_id="worker", **kwargs)

        self.assertEqual(list_tasks(self.scope), [])
        self.assertEqual(list_inbox(self.scope), [])
        self.assertEqual(
            read_status(self.scope)["messages"],
            {
                "received": 0,
                "processing": 0,
                "applied": 0,
                "needs_input": 0,
                "failed": 0,
            },
        )

    def test_settle_done_reuses_owned_claim_and_leases_ready(self) -> None:
        leased = enqueue_task(
            self.scope,
            title="Leased work",
            owner_id="worker",
        )
        ready = enqueue_task(self.scope, title="Ready work", owner_id="worker")
        claim_task(self.scope, leased["task_id"], owner_id="worker")

        result = settle_tasks_done(
            self.scope,
            task_ids=[leased["task_id"], ready["task_id"]],
            summary="Both branches are verified complete",
            owner_id="worker",
        )

        self.assertEqual(result["status"], "done")
        self.assertEqual(
            result["tasks"],
            [
                {"task_id": leased["task_id"], "revision": 2, "state": "done"},
                {"task_id": ready["task_id"], "revision": 2, "state": "done"},
            ],
        )
        for task_id in (leased["task_id"], ready["task_id"]):
            shown = show_task(self.scope, task_id)
            self.assertEqual(shown["state"], "done")
            self.assertEqual(
                shown["reason"],
                "Both branches are verified complete",
            )
        # The summary message and every task claim were consumed inside
        # the one transaction.
        self.assertEqual(list_inbox(self.scope), [])
        self.assertEqual(list_claims(self.scope), [])
        self.assertEqual(read_status(self.scope)["messages"]["applied"], 3)
        self.assertEqual(check_journal(self.scope)["status"], "ok")

    def test_settle_done_is_all_or_nothing(self) -> None:
        ready = enqueue_task(self.scope, title="Ready work", owner_id="worker")
        dependent = enqueue_task(
            self.scope,
            title="Queued work",
            requires=[ready["task_id"]],
            owner_id="worker",
        )
        before = read_status(self.scope)

        with self.assertRaisesRegex(
            JournalError,
            f"not settleable: {dependent['task_id']}: queued",
        ) as caught:
            settle_tasks_done(
                self.scope,
                task_ids=[ready["task_id"], dependent["task_id"]],
                summary="Premature settlement",
                owner_id="worker",
            )

        self.assertEqual(caught.exception.code, "state_conflict")
        self.assertEqual(show_task(self.scope, ready["task_id"])["state"], "ready")
        self.assertEqual(show_task(self.scope, ready["task_id"])["revision"], 1)
        self.assertEqual(read_status(self.scope), before)

        claim_task(self.scope, ready["task_id"], owner_id="other")
        with self.assertRaisesRegex(JournalError, "held by another owner") as held:
            settle_tasks_done(
                self.scope,
                task_ids=[ready["task_id"]],
                summary="Wrong owner",
                owner_id="worker",
            )
        self.assertEqual(held.exception.code, "not_claimable")
        self.assertEqual(show_task(self.scope, ready["task_id"])["state"], "active")
        self.assertEqual(check_journal(self.scope)["status"], "ok")

    def test_settle_done_retry_after_success_changes_nothing(self) -> None:
        created = enqueue_task(
            self.scope,
            title="Terminal work",
            owner_id="worker",
        )
        settle_tasks_done(
            self.scope,
            task_ids=[created["task_id"]],
            summary="Verified complete",
            owner_id="worker",
        )
        before = read_status(self.scope)

        with self.assertRaisesRegex(
            JournalError,
            f"not settleable: {created['task_id']}: done",
        ):
            settle_tasks_done(
                self.scope,
                task_ids=[created["task_id"]],
                summary="Verified complete",
                owner_id="worker",
            )

        self.assertEqual(read_status(self.scope), before)
        self.assertEqual(show_task(self.scope, created["task_id"])["revision"], 2)
        self.assertEqual(check_journal(self.scope)["status"], "ok")

    def test_overview_orders_by_task_number_and_filters_states(self) -> None:
        first = enqueue_task(
            self.scope,
            title="Alpha",
            priority=1,
            owner_id="worker",
        )
        second = enqueue_task(
            self.scope,
            title="Beta",
            priority=50,
            owner_id="worker",
        )
        third = enqueue_task(
            self.scope,
            title="Gamma",
            requires=[second["task_id"]],
            owner_id="worker",
        )
        settle_tasks_done(
            self.scope,
            task_ids=[first["task_id"]],
            summary="Alpha complete",
            owner_id="worker",
        )

        default = overview_tasks(self.scope)
        everything = overview_tasks(self.scope, states=set(TASK_STATES))
        done_only = overview_tasks(self.scope, states={"done"})

        # Task-number order, not priority order, and terminal states only
        # on request.
        self.assertEqual(
            [task["task_id"] for task in default],
            [second["task_id"], third["task_id"]],
        )
        self.assertEqual(
            [task["task_id"] for task in everything],
            [first["task_id"], second["task_id"], third["task_id"]],
        )
        self.assertEqual(everything[0]["state"], "done")
        self.assertEqual(
            set(everything[0]),
            {"task_id", "revision", "state", "priority", "title"},
        )
        self.assertEqual(
            [task["task_id"] for task in done_only],
            [first["task_id"]],
        )
        self.assertEqual(
            len(overview_tasks(self.scope, states=set(TASK_STATES), limit=1)),
            1,
        )
        with self.assertRaisesRegex(JournalError, "task limit"):
            overview_tasks(self.scope, limit=0)

    def test_claim_release_is_retryable(self) -> None:
        message = self.ingest("Release this claim")
        claim = claim_message(
            self.scope,
            owner_id="worker",
            message_id=message.message_id,
        )
        assert claim is not None

        first = release_claim(self.scope, claim["claim_id"])
        second = release_claim(self.scope, claim["claim_id"])

        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["sequence"], second["sequence"])
        self.assertEqual(list_inbox(self.scope)[0]["state"], "received")
        self.assertEqual(check_journal(self.scope)["status"], "ok")

    def test_task_claim_race_has_one_winner(self) -> None:
        message = self.ingest("Create contested work")
        result = self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [["create", "$work", {"title": "Contested work"}]],
            },
        )
        task_id = result["aliases"]["$work"]
        barrier = threading.Barrier(8)

        def compete(index: int):
            barrier.wait()
            try:
                return claim_task(
                    self.scope,
                    task_id,
                    owner_id=f"owner-{index}",
                )
            except JournalError:
                return None

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(compete, range(8)))

        self.assertEqual(sum(result is not None for result in results), 1)

    def test_task_claim_expiry_recovery_fences_stale_claim(self) -> None:
        message = self.ingest("Create expiring work")
        result = self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [["create", "$work", {"title": "Expiring work"}]],
            },
        )
        task_id = result["aliases"]["$work"]
        start_us = time.time_ns() // 1000
        first = claim_task(
            self.scope,
            task_id,
            owner_id="first",
            lease_seconds=1,
            now_us=start_us,
        )["claim"]
        second = claim_task(
            self.scope,
            task_id,
            owner_id="second",
            lease_seconds=1,
            now_us=start_us + 1_000_001,
        )["claim"]

        self.assertGreater(second["fence"], first["fence"])
        with self.assertRaisesRegex(JournalError, "not active"):
            release_claim(
                self.scope,
                first["claim_id"],
                now_us=start_us + 1_000_002,
            )
        self.assertEqual(
            show_task(self.scope, task_id)["claim"]["claim_id"],
            second["claim_id"],
        )

    def test_apply_requires_matching_message_claim(self) -> None:
        message = self.ingest("Do not mutate without a claim")
        with self.assertRaisesRegex(JournalError, "claim"):
            apply_effects(
                self.scope,
                message.message_id,
                {"v": 1, "expect": {}, "effects": [], "reason": "no task"},
                claim_id="clm_" + "0" * 32,
            )
        self.assertEqual(list_inbox(self.scope)[0]["message_id"], message.message_id)

    def test_cli_applies_stdin_and_lists_tasks_as_json(self) -> None:
        message = self.ingest("Exercise the CLI")
        base = [
            sys.executable,
            "-m",
            "aiq",
        ]
        scope_arguments = [
            "--scope",
            "agent-root",
            "--cwd",
            str(self.root),
            "--agent-root",
            str(self.root / "agent"),
            "--json",
        ]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(
                None,
                (
                    str(REPO_ROOT / "src"),
                    environment.get("PYTHONPATH"),
                ),
            )
        )
        document = {
            "v": 1,
            "expect": {},
            "effects": [["create", "$cli", {"title": "CLI task"}]],
        }
        claim = claim_message(
            self.scope,
            owner_id="cli-test",
            message_id=message.message_id,
        )
        assert claim is not None

        applied = subprocess.run(
            [
                *base,
                "inbox",
                "apply",
                message.message_id,
                "--effects",
                "-",
                "--claim",
                claim["claim_id"],
                *scope_arguments,
            ],
            input=json.dumps(document),
            env=environment,
            text=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        listed = subprocess.run(
            [*base, "task", "list", *scope_arguments],
            env=environment,
            text=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(json.loads(applied.stdout)["status"], "applied")
        self.assertEqual(json.loads(listed.stdout)["tasks"][0]["title"], "CLI task")

    def test_human_cli_escapes_terminal_control_characters(self) -> None:
        ingest_message(
            self.scope,
            "content\u001b[31m",
            source="queue-test",
            cwd=str(self.root),
        )
        create_message = self.ingest("Create a task with an untrusted title")
        self.apply(
            create_message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [
                    ["create", "$unsafe", {"title": "title\u001b[2J"}],
                ],
            },
        )
        base = [
            sys.executable,
            "-m",
            "aiq",
        ]
        scope_arguments = [
            "--scope",
            "agent-root",
            "--cwd",
            str(self.root),
            "--agent-root",
            str(self.root / "agent"),
        ]
        environment = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                filter(
                    None,
                    (
                        str(REPO_ROOT / "src"),
                        os.environ.get("PYTHONPATH"),
                    ),
                )
            ),
        }

        completed = [
            subprocess.run(
                [
                    *base,
                    "inbox",
                    "list",
                    "--include-content",
                    *scope_arguments,
                ],
                env=environment,
                text=True,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ),
            subprocess.run(
                [
                    *base,
                    "task",
                    "list",
                    *scope_arguments,
                ],
                env=environment,
                text=True,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ),
        ]

        for result in completed:
            self.assertNotIn("\u001b", result.stdout)
            self.assertIn("\\u001b", result.stdout)

    def test_read_paths_close_connections_on_success_and_error(self) -> None:
        created = self.apply(
            self.ingest("Create observed work").message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [["create", "$work", {"title": "Observed work"}]],
            },
        )
        task_id = created["aliases"]["$work"]
        captured: list[sqlite3.Connection] = []
        original_connect = queue_module._connect

        def capturing_connect(scope):
            connection = original_connect(scope)
            captured.append(connection)
            return connection

        with patch.object(queue_module, "_connect", capturing_connect):
            list_tasks(self.scope)
            show_task(self.scope, task_id)
            read_status(self.scope)
            explain_task(self.scope, task_id)
            task_history(self.scope, task_id)
            list_claims(self.scope)
            with self.assertRaisesRegex(JournalError, "task not found"):
                explain_task(self.scope, "TASK-999")

        self.assertEqual(len(captured), 7)
        for connection in captured:
            with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                connection.execute("SELECT 1")

    def test_explain_task_now_us_exercises_lease_expiry(self) -> None:
        created = self.apply(
            self.ingest("Create leased work").message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [["create", "$work", {"title": "Leased work"}]],
            },
        )
        task_id = created["aliases"]["$work"]
        start_us = _now_us()
        claim = claim_task(
            self.scope,
            task_id,
            owner_id="worker",
            lease_seconds=1,
            now_us=start_us,
        )["claim"]

        active = explain_task(self.scope, task_id, now_us=start_us + 500_000)
        self.assertEqual(active["state"], "active")
        self.assertEqual(active["claim"]["claim_id"], claim["claim_id"])
        self.assertEqual(active["claim"]["owner_id"], "worker")
        self.assertTrue(active["claim"]["expires_at"].endswith("Z"))
        self.assertEqual(
            active["explanation"],
            f"active: leased by worker until {active['claim']['expires_at']}",
        )

        expired = explain_task(self.scope, task_id, now_us=start_us + 1_000_001)
        self.assertEqual(expired["state"], "ready")
        self.assertIsNone(expired["claim"])
        self.assertEqual(expired["explanation"], "ready: no prerequisites")

    def test_read_status_reports_bounded_counts_without_content(self) -> None:
        self.assertEqual(
            read_status(self.scope),
            {
                "messages": {
                    "received": 0,
                    "processing": 0,
                    "applied": 0,
                    "needs_input": 0,
                    "failed": 0,
                },
                "tasks": {
                    "queued": 0,
                    "ready": 0,
                    "active": 0,
                    "blocked": 0,
                    "done": 0,
                    "canceled": 0,
                    "superseded": 0,
                },
                "claims": {"active": 0},
                "ready": [],
            },
        )

        created = self.apply(
            self.ingest("Create ready and dependent work").message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [
                    ["create", "$ready", {"title": "Ready work", "priority": 5}],
                    [
                        "create",
                        "$queued",
                        {"title": "Dependent work", "requires": ["$ready"]},
                    ],
                ],
            },
        )
        ready_id = created["aliases"]["$ready"]
        self.ingest("Pending message")
        processing = self.ingest("Processing message")
        claim_message(
            self.scope,
            owner_id="status-test",
            message_id=processing.message_id,
        )
        for content, disposition in (
            ("Parked message", "needs_input"),
            ("Broken message", "failed"),
        ):
            receipt = self.ingest(content)
            claim = claim_message(
                self.scope,
                owner_id="status-test",
                message_id=receipt.message_id,
            )
            assert claim is not None
            dispose_message(
                self.scope,
                receipt.message_id,
                claim_id=claim["claim_id"],
                disposition=disposition,
                reason="status coverage",
            )

        status = read_status(self.scope)
        self.assertEqual(
            status["messages"],
            {
                "received": 1,
                "processing": 1,
                "applied": 1,
                "needs_input": 1,
                "failed": 1,
            },
        )
        self.assertEqual(status["tasks"]["ready"], 1)
        self.assertEqual(status["tasks"]["queued"], 1)
        self.assertEqual(status["claims"], {"active": 1})
        (ready_entry,) = status["ready"]
        created_at = ready_entry.pop("created_at")
        datetime.fromisoformat(created_at)
        self.assertEqual(
            ready_entry,
            {"task_id": ready_id, "priority": 5, "title": "Ready work"},
        )

        claimed = claim_next_tasks(self.scope, owner_id="status-test")
        self.assertEqual(claimed[0]["task"]["task_id"], ready_id)
        active_status = read_status(self.scope)
        self.assertEqual(active_status["tasks"]["active"], 1)
        self.assertEqual(active_status["tasks"]["ready"], 0)
        self.assertEqual(active_status["ready"], [])
        self.assertEqual(active_status["claims"], {"active": 2})

    def test_read_status_counts_expired_message_lease_as_received(self) -> None:
        message = self.ingest("Expiring message")
        claim_message(
            self.scope,
            owner_id="status-test",
            message_id=message.message_id,
            lease_seconds=1,
            now_us=1_000_000,
        )

        status = read_status(self.scope)

        self.assertEqual(status["messages"]["processing"], 0)
        self.assertEqual(status["messages"]["received"], 1)
        self.assertEqual(status["claims"], {"active": 0})

    def test_read_status_bounds_ready_tasks_by_priority(self) -> None:
        self.apply(
            self.ingest("Create many ready tasks").message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [
                    [
                        "create",
                        f"$task{index}",
                        {"title": f"Task {index}", "priority": index},
                    ]
                    for index in range(7)
                ],
            },
        )

        status = read_status(self.scope)

        self.assertEqual(status["tasks"]["ready"], 7)
        self.assertEqual(len(status["ready"]), 5)
        self.assertEqual(
            [task["priority"] for task in status["ready"]],
            [6, 5, 4, 3, 2],
        )
        self.assertEqual(
            [
                task["priority"]
                for task in read_status(self.scope, ready_limit=1)["ready"]
            ],
            [6],
        )
        with self.assertRaisesRegex(JournalError, "queue limit"):
            read_status(self.scope, ready_limit=0)


if __name__ == "__main__":
    unittest.main()
