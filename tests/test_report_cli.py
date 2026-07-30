from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import support
from aiq import __version__
from aiq.cli import report as report_module
from aiq.journal import JournalError, ingest_message, resolve_scope


# The exact wording ingest uses when one idempotency key is re-presented
# with a different message identity. `report` must no longer read it.
IDENTITY_CONFLICT_WORDING = (
    "idempotency key already belongs to a different message identity"
)


def report_content(summary: str, detail: str) -> str:
    return json.dumps(
        {"aiq_version": __version__, "detail": detail, "summary": summary},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class ReportCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.target = self.root / "aiq-dev"
        self.origin_a = self.root / "origin-a"
        self.origin_b = self.root / "origin-b"
        for repository in (self.target, self.origin_a, self.origin_b):
            support.init_repository(repository)
        environment = support.scrubbed_environment(
            HOME=str(self.root / "home"),
            XDG_CONFIG_HOME=str(self.root / "config"),
            XDG_STATE_HOME=str(self.root / "state"),
        )
        patcher = patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(
            self.run_cli(
                "journal", "init",
                "--scope", "repo", "--cwd", str(self.target), "--json",
            )[0],
            0,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_cli(self, *arguments: str) -> support.CliResult:
        return support.run_cli(*arguments)

    def report(
        self,
        *extra: str,
        summary: str = "Ingest crashes on empty content",
        detail: str = "Traceback from aiq ingest --stdin with empty input",
        origin: Path | None = None,
        to: bool = True,
    ) -> support.CliResult:
        arguments = [
            "report",
            "--summary", summary,
            "--detail", detail,
            "--cwd", str(origin or self.origin_a),
            "--json",
        ]
        if to:
            arguments += ["--to", str(self.target)]
        return self.run_cli(*arguments, *extra)

    def target_tasks(self) -> list[dict[str, object]]:
        code, stdout, stderr = self.run_cli(
            "task", "list",
            "--scope", "repo", "--cwd", str(self.target), "--json",
        )
        self.assertEqual(code, 0, stderr)
        return json.loads(stdout)["tasks"]

    def assert_error(
        self,
        result: support.CliResult,
        exit_code: int,
        code: str,
    ) -> None:
        actual_exit, stdout, stderr = result
        self.assertEqual(actual_exit, exit_code, stderr)
        self.assertEqual(stdout, "")
        payload = json.loads(stderr)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], code)

    def test_report_creates_task_in_target_repository(self) -> None:
        code, stdout, stderr = self.report()

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["v"], 1)
        self.assertEqual(payload["status"], "reported")
        self.assertEqual(payload["scope"]["kind"], "repo")
        self.assertIs(payload["detail_truncated"], False)
        tasks = self.target_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_id"], payload["task_id"])
        self.assertEqual(tasks[0]["title"], "Ingest crashes on empty content")
        self.assertEqual(tasks[0]["priority"], 60)
        self.assertEqual(tasks[0]["state"], "ready")
        self.assertFalse(
            (self.origin_a / ".git" / "aiq" / "journal.sqlite3").exists()
        )

    def test_priority_and_detail_file_are_honored(self) -> None:
        detail_file = self.root / "detail.txt"
        detail_file.write_text("Detail from a file", encoding="utf-8")
        code, stdout, stderr = self.run_cli(
            "report",
            "--summary", "Doctor mislabels sqlite",
            "--detail-file", str(detail_file),
            "--to", str(self.target),
            "--priority", "90",
            "--cwd", str(self.origin_a),
            "--json",
        )

        self.assertEqual(code, 0, stderr)
        task_id = json.loads(stdout)["task_id"]
        code, stdout, stderr = self.run_cli(
            "task", "show", str(task_id),
            "--scope", "repo", "--cwd", str(self.target), "--json",
        )
        self.assertEqual(code, 0, stderr)
        task = json.loads(stdout)["task"]
        self.assertEqual(task["objective"], "Detail from a file")
        self.assertEqual(task["priority"], 90)

    def test_identical_report_deduplicates_across_origins(self) -> None:
        first = json.loads(self.report()[1])

        same_origin = self.report()
        other_origin = self.report(origin=self.origin_b)

        for code, stdout, stderr in (same_origin, other_origin):
            self.assertEqual(code, 0, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "duplicate")
            self.assertEqual(payload["message_id"], first["message_id"])
            self.assertEqual(payload["task_id"], first["task_id"])
        self.assertEqual(len(self.target_tasks()), 1)
        self.assertFalse(
            (self.origin_b / ".git" / "aiq" / "journal.sqlite3").exists()
        )

    def test_unset_dev_report_repo_is_invalid_config(self) -> None:
        result = self.report(to=False)
        self.assert_error(result, 2, "invalid_config")
        message = json.loads(result[2])["error"]
        for remedy in ("dev_report_repo", "AIQ_DEV_REPORT_REPO", "--to PATH"):
            self.assertIn(remedy, message)
        self.assertEqual(self.target_tasks(), [])

    def test_environment_variable_selects_target(self) -> None:
        with patch.dict(
            os.environ,
            {"AIQ_DEV_REPORT_REPO": str(self.target)},
        ):
            code, stdout, stderr = self.report(to=False)

        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "reported")
        self.assertEqual(len(self.target_tasks()), 1)

    def test_to_override_wins_over_environment(self) -> None:
        decoy = support.init_repository(self.root / "decoy")
        with patch.dict(os.environ, {"AIQ_DEV_REPORT_REPO": str(decoy)}):
            code, stdout, stderr = self.report()

        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "reported")
        self.assertEqual(len(self.target_tasks()), 1)
        self.assertFalse(
            (decoy / ".git" / "aiq" / "journal.sqlite3").exists()
        )

    def test_relative_to_is_rejected(self) -> None:
        result = self.run_cli(
            "report",
            "--summary", "Summary",
            "--detail", "Detail",
            "--to", "relative/aiq-dev",
            "--cwd", str(self.origin_a),
            "--json",
        )

        self.assert_error(result, 2, "invalid_argument")

    def test_missing_target_and_uninitialized_journal_fail_clearly(self) -> None:
        missing = self.report("--to", str(self.root / "absent"), to=False)
        self.assert_error(missing, 3, "not_found")

        uninitialized = self.report("--to", str(self.origin_b), to=False)
        self.assert_error(uninitialized, 3, "not_found")
        self.assertFalse(
            (self.origin_b / ".git" / "aiq" / "journal.sqlite3").exists()
        )

    def test_summary_and_detail_bounds_are_enforced(self) -> None:
        self.assert_error(
            self.report(summary=""), 2, "invalid_argument"
        )
        self.assert_error(
            self.report(summary="s" * 201), 2, "invalid_argument"
        )
        self.assert_error(
            self.report(detail="d" * 16001), 2, "invalid_argument"
        )
        self.assertEqual(self.target_tasks(), [])

        code, stdout, stderr = self.report(detail="d" * 16000)
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertIs(payload["detail_truncated"], True)
        task_id = payload["task_id"]
        code, stdout, stderr = self.run_cli(
            "task", "show", str(task_id),
            "--scope", "repo", "--cwd", str(self.target), "--json",
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(json.loads(stdout)["task"]["objective"]), 2000)

    def test_priority_bounds_are_enforced(self) -> None:
        self.assert_error(
            self.report("--priority", "1000001"), 2, "invalid_argument"
        )
        self.assert_error(
            self.report("--priority", "-1000001"), 2, "invalid_argument"
        )
        self.assertEqual(self.target_tasks(), [])

        code, stdout, stderr = self.report(
            "--priority", "1000000", summary="Upper bound report"
        )
        self.assertEqual(code, 0, stderr)
        code, stdout, stderr = self.report(
            "--priority", "-1000000", summary="Lower bound report"
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(
            sorted(task["priority"] for task in self.target_tasks()),
            [-1_000_000, 1_000_000],
        )

    def test_preingested_identical_content_is_reported_duplicate(self) -> None:
        summary = "Queue next loses a claim"
        detail = "claim_next_tasks returns an expired lease"
        content = report_content(summary, detail)
        scope = resolve_scope("repo", cwd=self.target)
        ingested = ingest_message(
            scope,
            content,
            source="dev-report",
            idempotency_key=hashlib.sha256(content.encode()).hexdigest(),
            cwd=str(self.origin_b),
        )

        for origin in (self.origin_b, self.origin_a):
            code, stdout, stderr = self.report(
                summary=summary, detail=detail, origin=origin
            )
            self.assertEqual(code, 0, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["status"], "duplicate")
            self.assertEqual(payload["message_id"], ingested.message_id)
            # The pre-ingested message never produced a tracking task.
            self.assertNotIn("task_id", payload)
        self.assertEqual(self.target_tasks(), [])

    def test_cross_origin_duplicate_comes_from_the_ingest_conflict_branch(
        self,
    ) -> None:
        """The other-origin duplicate really is the conflict recovery.

        A same-origin repeat is ordinary idempotent re-ingest and never
        reaches the handler. Only the conflict branch looks the stored
        message up by key, so that lookup running is the proof that the
        branch under test is what produced the answer.
        """
        first = json.loads(self.report()[1])

        with patch(
            "aiq.cli.report.find_message_by_idempotency_key",
            wraps=report_module.find_message_by_idempotency_key,
        ) as lookup:
            same_origin = self.report()
            self.assertEqual(lookup.call_count, 0, "same origin must not conflict")
            other_origin = self.report(origin=self.origin_b)
            self.assertEqual(lookup.call_count, 1)

        self.assertEqual(other_origin[0], 0, other_origin[2])
        self.assertEqual(json.loads(same_origin[1])["status"], "duplicate")
        payload = json.loads(other_origin[1])
        self.assertEqual(payload["status"], "duplicate")
        self.assertEqual(payload["message_id"], first["message_id"])

    def test_ingest_conflict_is_recognized_by_its_code_alone(self) -> None:
        """A reworded conflict still resolves to the known duplicate."""
        first = json.loads(self.report()[1])

        with patch(
            "aiq.cli.report.ingest_message",
            side_effect=JournalError(
                "some future rewording of the same refusal",
                code="state_conflict",
            ),
        ):
            code, stdout, stderr = self.report()

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "duplicate")
        self.assertEqual(payload["message_id"], first["message_id"])
        self.assertEqual(payload["task_id"], first["task_id"])

    def test_ingest_failure_with_another_code_is_never_swallowed(self) -> None:
        """Wording cannot buy an error the duplicate treatment.

        This is the narrowing that removing the substring fallback bought:
        the identity-conflict wording carried by an error that is *not* a
        state conflict now surfaces as that error, instead of silently
        answering `duplicate` and exiting 0.
        """
        self.report()

        for failure_code, exit_code in (("contention", 4), ("io_error", 6)):
            with self.subTest(code=failure_code):
                with patch(
                    "aiq.cli.report.ingest_message",
                    side_effect=JournalError(
                        IDENTITY_CONFLICT_WORDING, code=failure_code
                    ),
                ):
                    result = self.report()
                self.assert_error(result, exit_code, failure_code)

    def test_ingest_conflict_without_a_stored_message_propagates(self) -> None:
        """A conflict AIQ cannot explain is reported, not hidden."""
        with patch(
            "aiq.cli.report.ingest_message",
            side_effect=JournalError(
                IDENTITY_CONFLICT_WORDING, code="state_conflict"
            ),
        ):
            result = self.report()

        self.assert_error(result, 4, "state_conflict")
        self.assertEqual(self.target_tasks(), [])

    def test_lost_claim_race_is_recognized_by_its_code_alone(self) -> None:
        """A concurrent claimer yields a duplicate, whatever the wording."""
        with patch(
            "aiq.cli.report.claim_message",
            side_effect=JournalError(
                "some future rewording of the same refusal",
                code="not_claimable",
            ),
        ):
            code, stdout, stderr = self.report()

        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "duplicate")
        # The message was stored, but this instance created no task for it.
        self.assertEqual(self.target_tasks(), [])

    def test_claim_failure_with_another_code_is_never_swallowed(self) -> None:
        with patch(
            "aiq.cli.report.claim_message",
            side_effect=JournalError(
                "message is not claimable: msg_x", code="contention"
            ),
        ):
            result = self.report()

        self.assert_error(result, 4, "contention")
        self.assertEqual(self.target_tasks(), [])


if __name__ == "__main__":
    unittest.main()
