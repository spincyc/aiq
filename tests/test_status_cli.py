from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

import support


class StatusCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repository = support.init_repository(self.root / "repository")
        self.environment = support.scrubbed_environment(
            HOME=str(self.home),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONPATH=str(support.SOURCE_ROOT),
            XDG_CONFIG_HOME=str(self.root / "config"),
            XDG_STATE_HOME=str(self.root / "state"),
        )
        self.scope = ("--scope", "repo", "--cwd", str(self.repository))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_aiq(
        self,
        *arguments: str,
        input_text: str | None = None,
    ) -> support.CliResult:
        return support.run_cli(
            *arguments,
            in_process=False,
            cwd=self.repository,
            environment=self.environment,
            input_text=input_text,
        )

    def ok(
        self,
        *arguments: str,
        input_text: str | None = None,
    ) -> dict[str, object]:
        completed = self.run_aiq(
            *arguments, "--json", input_text=input_text
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def create_ready_task(self) -> str:
        self.ok("journal", "init", *self.scope)
        ingested = self.ok(
            "ingest", "--message", "Confidential prompt content",
            "--source", "status-test", *self.scope,
        )
        message_id = str(ingested["message_id"])
        claimed = self.ok(
            "inbox", "claim", message_id,
            "--owner", "status-test", *self.scope,
        )
        effects = json.dumps(
            {
                "v": 1,
                "expect": {},
                "effects": [
                    ["create", "$task", {"title": "Status task", "priority": 3}]
                ],
            },
            separators=(",", ":"),
        )
        applied = self.ok(
            "inbox", "apply", message_id,
            "--claim", str(claimed["claim"]["claim_id"]),
            "--effects", "-", *self.scope, input_text=effects,
        )
        return str(applied["aliases"]["$task"])

    def test_json_status_is_versioned_bounded_and_content_free(self) -> None:
        task_id = self.create_ready_task()

        completed = self.run_aiq("status", *self.scope, "--json")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout.count("\n"), 1)
        self.assertNotIn("Confidential", completed.stdout)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["v"], 1)
        self.assertEqual(
            set(payload),
            {"blocked", "claims", "messages", "ready", "scope", "tasks", "v"},
        )
        self.assertEqual(payload["blocked"], [])
        self.assertEqual(
            payload["messages"],
            {
                "received": 0,
                "processing": 0,
                "applied": 1,
                "needs_input": 0,
                "failed": 0,
            },
        )
        self.assertEqual(payload["tasks"]["ready"], 1)
        self.assertEqual(payload["claims"], {"active": 0})
        (entry,) = payload["ready"]
        created_at = entry.pop("created_at")
        datetime.fromisoformat(created_at)
        self.assertEqual(
            entry,
            {"task_id": task_id, "priority": 3, "title": "Status task"},
        )

    def test_human_status_is_terse_and_content_free(self) -> None:
        task_id = self.create_ready_task()

        completed = self.run_aiq("status", *self.scope)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertNotIn("Confidential", completed.stdout)
        lines = completed.stdout.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual(
            lines[0],
            "messages  received=0  processing=0  applied=1  "
            "needs_input=0  failed=0",
        )
        self.assertEqual(
            lines[1],
            "tasks     queued=0  ready=1  active=0  blocked=0  "
            "done=0  canceled=0  superseded=0",
        )
        self.assertEqual(lines[2], "claims    active=0")
        self.assertEqual(lines[3], f"ready     {task_id}\tp3\tStatus task")

    def create_blocked_task(self) -> tuple[str, str]:
        """Create a dependent task and cancel its prerequisite.

        Returns ``(prerequisite_id, dependent_id)``; the dependent is
        effectively blocked by the canceled prerequisite.
        """

        ingested = self.ok(
            "ingest", "--message", "Create a doomed pair",
            "--source", "status-test", *self.scope,
        )
        message_id = str(ingested["message_id"])
        claimed = self.ok(
            "inbox", "claim", message_id,
            "--owner", "status-test", *self.scope,
        )
        effects = json.dumps(
            {
                "v": 1,
                "expect": {},
                "effects": [
                    ["create", "$prereq", {"title": "Doomed prerequisite"}],
                    [
                        "create",
                        "$dep",
                        {
                            "title": "Blocked task",
                            "priority": 6,
                            "requires": ["$prereq"],
                        },
                    ],
                ],
            },
            separators=(",", ":"),
        )
        applied = self.ok(
            "inbox", "apply", message_id,
            "--claim", str(claimed["claim"]["claim_id"]),
            "--effects", "-", *self.scope, input_text=effects,
        )
        prereq_id = str(applied["aliases"]["$prereq"])
        dep_id = str(applied["aliases"]["$dep"])
        cancel = self.ok(
            "ingest", "--message", "Cancel the prerequisite",
            "--source", "status-test", *self.scope,
        )
        cancel_id = str(cancel["message_id"])
        cancel_claim = self.ok(
            "inbox", "claim", cancel_id,
            "--owner", "status-test", *self.scope,
        )
        cancel_effects = json.dumps(
            {
                "v": 1,
                "expect": {prereq_id: 1},
                "effects": [
                    ["transition", prereq_id, "canceled", {"reason": "obsolete"}]
                ],
            },
            separators=(",", ":"),
        )
        self.ok(
            "inbox", "apply", cancel_id,
            "--claim", str(cancel_claim["claim"]["claim_id"]),
            "--effects", "-", *self.scope, input_text=cancel_effects,
        )
        return prereq_id, dep_id

    def test_status_lists_blocked_tasks_with_causes(self) -> None:
        task_id = self.create_ready_task()
        prereq_id, dep_id = self.create_blocked_task()

        payload = self.ok("status", *self.scope)
        self.assertEqual(payload["tasks"]["blocked"], 1)
        self.assertEqual(
            payload["blocked"],
            [
                {
                    "task_id": dep_id,
                    "priority": 6,
                    "title": "Blocked task",
                    "blocked_by": [prereq_id],
                }
            ],
        )

        completed = self.run_aiq("status", *self.scope)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = completed.stdout.splitlines()
        self.assertEqual(
            lines[-2], f"ready     {task_id}\tp3\tStatus task"
        )
        self.assertEqual(
            lines[-1],
            f"blocked   {dep_id}\tp6\tBlocked task\tblocked by {prereq_id}",
        )

    def test_missing_journal_reports_zeros_without_creating_storage(self) -> None:
        payload = self.ok("status", *self.scope)

        self.assertEqual(payload["v"], 1)
        self.assertEqual(sum(payload["messages"].values()), 0)
        self.assertEqual(sum(payload["tasks"].values()), 0)
        self.assertEqual(payload["claims"], {"active": 0})
        self.assertEqual(payload["ready"], [])
        self.assertEqual(payload["blocked"], [])
        self.assertFalse(
            (self.repository / ".git" / "aiq" / "journal.sqlite3").exists()
        )


if __name__ == "__main__":
    unittest.main()
