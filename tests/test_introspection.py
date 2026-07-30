from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import support
from aiq.journal import JournalError, ingest_message, resolve_scope
from aiq.queue import (
    _now_us,
    apply_effects,
    claim_message,
    claim_task,
    explain_task,
    list_claims,
    read_status,
    release_claim,
    task_history,
)


class IntrospectionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.environment = patch.dict(
            os.environ,
            {
                "HOME": str(self.root / "home"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
            },
        )
        self.environment.start()
        self.agent_root = self.root / "agent"
        self.agent_root.mkdir()
        self.scope = resolve_scope(
            "agent-root",
            cwd=self.root,
            agent_root=self.agent_root,
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
                owner_id="introspection-test",
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

    def create_pair(self) -> tuple[str, str]:
        message = self.ingest("Create explanation fixtures")
        result = self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [
                    ["create", "$first", {"title": "First"}],
                    [
                        "create",
                        "$second",
                        {"title": "Second", "requires": ["$first"]},
                    ],
                ],
            },
        )
        return result["aliases"]["$first"], result["aliases"]["$second"]


class ExplainTaskTests(IntrospectionTestCase):
    def test_explains_ready_waiting_and_completion(self) -> None:
        first, second = self.create_pair()

        ready = explain_task(self.scope, first)
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["prerequisites"], [])
        self.assertEqual(ready["explanation"], "ready: no prerequisites")
        self.assertIsNone(ready["claim"])

        waiting = explain_task(self.scope, second)
        self.assertEqual(waiting["state"], "queued")
        self.assertEqual(waiting["recorded_state"], "queued")
        self.assertEqual(
            waiting["prerequisites"],
            [{"task_id": first, "state": "ready", "satisfied": False}],
        )
        self.assertEqual(waiting["waiting_on"], [first])
        self.assertEqual(waiting["blocked_by"], [])
        self.assertEqual(
            waiting["explanation"],
            f"queued: waiting on {first}",
        )

        claim = claim_task(self.scope, first, owner_id="worker")["claim"]
        done_message = self.ingest("Finish the first task")
        self.apply(
            done_message.message_id,
            {
                "v": 1,
                "expect": {first: 1},
                "effects": [
                    ["transition", first, "done", {"claim": claim["claim_id"]}]
                ],
            },
        )

        satisfied = explain_task(self.scope, second)
        self.assertEqual(satisfied["state"], "ready")
        self.assertEqual(
            satisfied["prerequisites"],
            [{"task_id": first, "state": "done", "satisfied": True}],
        )
        self.assertEqual(
            satisfied["explanation"],
            "ready: all prerequisites are done",
        )

    def test_explains_failed_prerequisite_and_recorded_blocked(self) -> None:
        first, second = self.create_pair()
        message = self.ingest("Cancel the prerequisite")
        self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {first: 1},
                "effects": [
                    ["transition", first, "canceled", {"reason": "Dropped"}]
                ],
            },
        )

        canceled = explain_task(self.scope, first)
        self.assertEqual(canceled["state"], "canceled")
        self.assertEqual(canceled["explanation"], "canceled: Dropped")

        blocked = explain_task(self.scope, second)
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["recorded_state"], "queued")
        self.assertEqual(blocked["blocked_by"], [first])
        self.assertEqual(
            blocked["explanation"],
            f"blocked: failed prerequisites {first}",
        )

        recorded = self.ingest("Create an explicitly blocked task")
        created = self.apply(
            recorded.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [
                    ["create", "$third", {"title": "Third"}],
                    [
                        "transition",
                        "$third",
                        "blocked",
                        {"reason": "Waiting for input"},
                    ],
                ],
            },
        )
        explicit = explain_task(self.scope, created["aliases"]["$third"])
        self.assertEqual(explicit["state"], "blocked")
        self.assertEqual(explicit["recorded_state"], "blocked")
        self.assertEqual(
            explicit["explanation"],
            "blocked: Waiting for input",
        )

    def test_explains_active_claim_and_supersession(self) -> None:
        first, second = self.create_pair()
        claim = claim_task(self.scope, first, owner_id="worker")["claim"]

        active = explain_task(self.scope, first)
        self.assertEqual(active["state"], "active")
        self.assertEqual(
            active["claim"],
            {
                "claim_id": claim["claim_id"],
                "owner_id": "worker",
                "expires_at": active["claim"]["expires_at"],
            },
        )
        self.assertTrue(active["claim"]["expires_at"].endswith("Z"))
        self.assertEqual(
            active["explanation"],
            f"active: leased by worker until {active['claim']['expires_at']}",
        )

        release_claim(self.scope, claim["claim_id"])
        message = self.ingest("Replace the first task")
        self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {first: 1, second: 1},
                "effects": [
                    [
                        "transition",
                        first,
                        "superseded",
                        {"by": second, "reason": "Replaced"},
                    ]
                ],
            },
        )
        superseded = explain_task(self.scope, first)
        self.assertEqual(superseded["state"], "superseded")
        self.assertEqual(superseded["superseded_by_task_id"], second)
        self.assertEqual(
            superseded["explanation"],
            f"superseded by {second}: Replaced",
        )

    def test_expired_task_lease_frees_task_in_explain_and_status(self) -> None:
        first, _ = self.create_pair()
        expired = claim_task(
            self.scope,
            first,
            owner_id="worker",
            lease_seconds=1,
            now_us=_now_us() - 10_000_000,
        )["claim"]
        self.assertEqual(expired["owner_id"], "worker")

        explained = explain_task(self.scope, first)
        self.assertEqual(explained["state"], "ready")
        self.assertIsNone(explained["claim"])
        self.assertEqual(explained["explanation"], "ready: no prerequisites")

        status = read_status(self.scope)
        self.assertEqual(status["claims"], {"active": 0})
        self.assertIn(
            first,
            [task["task_id"] for task in status["ready"]],
        )

    def test_rejects_invalid_and_unknown_tasks(self) -> None:
        self.create_pair()
        with self.assertRaisesRegex(JournalError, "invalid task ID"):
            explain_task(self.scope, "TASK-0")
        with self.assertRaisesRegex(JournalError, "task not found: TASK-999"):
            explain_task(self.scope, "TASK-999")


class TaskHistoryTests(IntrospectionTestCase):
    def test_history_is_newest_first_bounded_and_content_free(self) -> None:
        message = self.ingest("SECRET-PROMPT create two tasks")
        claim = claim_message(
            self.scope,
            owner_id="introspection-test",
            message_id=message.message_id,
        )
        assert claim is not None
        self.message_claims[message.message_id] = claim["claim_id"]
        result = self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [
                    ["create", "$first", {"title": "First"}],
                    ["create", "$second", {"title": "Second"}],
                ],
            },
        )
        first = result["aliases"]["$first"]
        second = result["aliases"]["$second"]
        revise = self.ingest("Revise and rewire the first task")
        self.apply(
            revise.message_id,
            {
                "v": 1,
                "expect": {first: 1, second: 1},
                "effects": [
                    ["update", first, {"title": "Renamed", "priority": 5}],
                    ["require", first, second],
                ],
            },
        )
        rewire = self.ingest("Remove the dependency again")
        self.apply(
            rewire.message_id,
            {
                "v": 1,
                "expect": {first: 3, second: 1},
                "effects": [["unrequire", first, second]],
            },
        )
        task_claim = claim_task(self.scope, first, owner_id="worker")["claim"]
        release_claim(self.scope, task_claim["claim_id"])

        events = task_history(self.scope, first)

        self.assertEqual(
            [entry["type"] for entry in events],
            [
                "claim.released",
                "claim.acquired",
                "task.dependency_removed",
                "task.dependency_added",
                "task.revised",
                "task.created",
            ],
        )
        self.assertEqual(
            events[-1]["detail"],
            {"revision": 1, "state": "queued"},
        )
        self.assertEqual(
            events[-2]["detail"],
            {"revision": 2, "fields": ["priority", "title"]},
        )
        self.assertEqual(
            events[-3]["detail"],
            {"revision": 3, "dependency": second},
        )
        self.assertEqual(
            events[2]["detail"],
            {"revision": 4, "dependency": second},
        )
        acquired = events[1]["detail"]
        self.assertEqual(acquired["claim_id"], task_claim["claim_id"])
        self.assertEqual(acquired["owner_id"], "worker")
        self.assertTrue(acquired["expires_at"].endswith("Z"))
        self.assertEqual(
            events[0]["detail"],
            {
                "claim_id": task_claim["claim_id"],
                "disposition": "released",
            },
        )
        for entry in events:
            self.assertEqual(set(entry), {"occurred_at", "type", "detail"})
        self.assertNotIn("SECRET-PROMPT", json.dumps(events))

        bounded = task_history(self.scope, first, limit=2)
        self.assertEqual(bounded, events[:2])

    def test_history_records_transitions_and_consumed_claims(self) -> None:
        first, second = self.create_pair()
        claim = claim_task(self.scope, first, owner_id="worker")["claim"]
        message = self.ingest("Finish the first task")
        self.apply(
            message.message_id,
            {
                "v": 1,
                "expect": {first: 1},
                "effects": [
                    ["transition", first, "done", {"claim": claim["claim_id"]}]
                ],
            },
        )

        events = task_history(self.scope, first)

        self.assertEqual(
            [entry["type"] for entry in events],
            [
                "claim.consumed",
                "task.state_changed",
                "claim.acquired",
                "task.created",
            ],
        )
        self.assertEqual(
            events[0]["detail"],
            {"claim_id": claim["claim_id"], "disposition": "completed"},
        )
        self.assertEqual(
            events[1]["detail"],
            {
                "revision": 2,
                "state": "done",
                "reason": None,
                "superseded_by_task_id": None,
            },
        )

    def test_rejects_invalid_arguments(self) -> None:
        first, _ = self.create_pair()
        with self.assertRaisesRegex(JournalError, "invalid task ID"):
            task_history(self.scope, "task-1")
        with self.assertRaisesRegex(JournalError, "task not found: TASK-999"):
            task_history(self.scope, "TASK-999")
        for limit in (0, 1001):
            with self.assertRaisesRegex(
                JournalError,
                "history limit must be between 1 and 1000",
            ):
                task_history(self.scope, first, limit=limit)


class ListClaimsTests(IntrospectionTestCase):
    def test_lists_filters_and_marks_expiry(self) -> None:
        first, _ = self.create_pair()
        message = self.ingest("Message to lease")
        expired_claim = claim_message(
            self.scope,
            owner_id="reader",
            message_id=message.message_id,
            lease_seconds=1,
            now_us=_now_us() - 10_000_000,
        )
        assert expired_claim is not None
        task_claim = claim_task(self.scope, first, owner_id="worker")["claim"]

        claims = list_claims(self.scope)
        self.assertEqual(
            [claim["claim_id"] for claim in claims],
            [expired_claim["claim_id"], task_claim["claim_id"]],
        )
        self.assertEqual(
            claims[0],
            {
                "claim_id": expired_claim["claim_id"],
                "resource_kind": "message",
                "resource_id": message.message_id,
                "owner_id": "reader",
                "basis_revision": None,
                "expires_at": claims[0]["expires_at"],
                "status": "expired",
            },
        )
        self.assertEqual(
            claims[1],
            {
                "claim_id": task_claim["claim_id"],
                "resource_kind": "task",
                "resource_id": first,
                "owner_id": "worker",
                "basis_revision": 1,
                "expires_at": claims[1]["expires_at"],
                "status": "active",
            },
        )
        self.assertTrue(claims[1]["expires_at"].endswith("Z"))

        owned = list_claims(self.scope, owner_id="worker")
        self.assertEqual(
            [claim["claim_id"] for claim in owned],
            [task_claim["claim_id"]],
        )
        messages = list_claims(self.scope, resource_kind="message")
        self.assertEqual(
            [claim["claim_id"] for claim in messages],
            [expired_claim["claim_id"]],
        )
        active = list_claims(self.scope, status="active")
        self.assertEqual(
            [claim["claim_id"] for claim in active],
            [task_claim["claim_id"]],
        )
        expired = list_claims(self.scope, status="expired")
        self.assertEqual(
            [claim["claim_id"] for claim in expired],
            [expired_claim["claim_id"]],
        )
        self.assertEqual(list_claims(self.scope, limit=1), claims[:1])

        release_claim(self.scope, task_claim["claim_id"])
        self.assertEqual(list_claims(self.scope, status="active"), [])

    def test_rejects_invalid_filters(self) -> None:
        for limit in (0, 1001):
            with self.assertRaisesRegex(
                JournalError,
                "claim limit must be between 1 and 1000",
            ):
                list_claims(self.scope, limit=limit)
        with self.assertRaisesRegex(
            JournalError,
            "unsupported claim resource filter",
        ):
            list_claims(self.scope, resource_kind="lock")
        with self.assertRaisesRegex(
            JournalError,
            "unsupported claim status filter",
        ):
            list_claims(self.scope, status="released")
        with self.assertRaisesRegex(JournalError, "owner_id length"):
            list_claims(self.scope, owner_id="")


class IntrospectionCliTests(IntrospectionTestCase):
    def run_cli(self, *arguments: str) -> support.CliResult:
        return support.run_cli(
            *arguments,
            "--scope",
            "agent-root",
            "--agent-root",
            str(self.agent_root),
            "--cwd",
            str(self.root),
            "--no-repo-config",
        )

    def test_task_explain_json_and_human(self) -> None:
        first, second = self.create_pair()

        code, stdout, stderr = self.run_cli(
            "task", "explain", second, "--json"
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout.count("\n"), 1)
        payload = json.loads(stdout)
        self.assertEqual(payload["v"], 1)
        self.assertEqual(payload["explain"]["state"], "queued")
        self.assertEqual(payload["explain"]["waiting_on"], [first])

        code, stdout, stderr = self.run_cli("task", "explain", second)
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(
            stdout.splitlines(),
            [
                f"{second}\tqueued\tr1\tqueued: waiting on {first}",
                f"requires\t{first}\tready\tunmet",
            ],
        )

    def test_task_history_json_and_human(self) -> None:
        first, _ = self.create_pair()

        code, stdout, stderr = self.run_cli(
            "task", "history", first, "--json"
        )
        self.assertEqual((code, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertEqual(payload["v"], 1)
        self.assertEqual(payload["task_id"], first)
        self.assertEqual(payload["events"][-1]["type"], "task.created")

        code, stdout, stderr = self.run_cli(
            "task", "history", first, "--limit", "1"
        )
        self.assertEqual((code, stderr), (0, ""))
        lines = stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith("\ttask.created\tr1 queued"))

    def test_claim_list_json_human_and_filters(self) -> None:
        first, _ = self.create_pair()
        claim = claim_task(self.scope, first, owner_id="worker")["claim"]

        code, stdout, stderr = self.run_cli("claim", "list", "--json")
        self.assertEqual((code, stderr), (0, ""))
        payload = json.loads(stdout)
        self.assertEqual(payload["v"], 1)
        self.assertEqual(len(payload["claims"]), 1)
        listed = payload["claims"][0]
        self.assertEqual(listed["claim_id"], claim["claim_id"])
        self.assertEqual(listed["status"], "active")

        code, stdout, stderr = self.run_cli(
            "claim", "list", "--owner", "worker", "--resource", "task"
        )
        self.assertEqual((code, stderr), (0, ""))
        line = stdout.splitlines()[0]
        self.assertTrue(line.startswith(f"{claim['claim_id']}\ttask\t{first}"))
        self.assertIn("\tworker\tactive\t", line)

        code, stdout, stderr = self.run_cli(
            "claim", "list", "--status", "expired", "--json"
        )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["claims"], [])

    def test_errors_use_protocol_envelope(self) -> None:
        self.create_pair()
        code, stdout, stderr = self.run_cli(
            "task", "explain", "TASK-999", "--json"
        )
        self.assertEqual(code, 3)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(
            (payload["v"], payload["status"], payload["code"]),
            (1, "error", "not_found"),
        )

        code, stdout, stderr = self.run_cli(
            "task", "history", "TASK-999", "--json"
        )
        self.assertEqual(code, 3)

        code, stdout, stderr = self.run_cli(
            "claim", "list", "--limit", "0", "--json"
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["code"], "invalid_argument")


if __name__ == "__main__":
    unittest.main()
