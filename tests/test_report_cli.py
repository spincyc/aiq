from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from aiq import __version__
from aiq.cli import main
from aiq.journal import ingest_message, resolve_scope


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
            repository.mkdir()
            subprocess.run(
                ["git", "init", "--quiet", str(repository)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("AIQ_", "GIT_"))
        }
        environment.update(
            {
                "HOME": str(self.root / "home"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
            }
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

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                code = main(list(arguments))
            except SystemExit as error:
                code = int(error.code or 0)
        return code, stdout.getvalue(), stderr.getvalue()

    def report(
        self,
        *extra: str,
        summary: str = "Ingest crashes on empty content",
        detail: str = "Traceback from aiq ingest --stdin with empty input",
        origin: Path | None = None,
        to: bool = True,
    ) -> tuple[int, str, str]:
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
        result: tuple[int, str, str],
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
        self.assert_error(self.report(to=False), 2, "invalid_config")
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
        decoy = self.root / "decoy"
        decoy.mkdir()
        subprocess.run(
            ["git", "init", "--quiet", str(decoy)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
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


if __name__ == "__main__":
    unittest.main()
