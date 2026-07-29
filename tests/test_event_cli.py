from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


class EventCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "HOME": str(self.home),
                "PYTHONDONTWRITEBYTECODE": "1",
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_STATE_HOME": str(self.root / "state"),
            }
        )
        source_path = str(REPOSITORY_ROOT / "src")
        inherited_python_path = self.environment.get("PYTHONPATH")
        self.environment["PYTHONPATH"] = (
            f"{source_path}{os.pathsep}{inherited_python_path}"
            if inherited_python_path
            else source_path
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def repository(self, name: str) -> Path:
        path = self.root / name
        path.mkdir()
        subprocess.run(
            ["git", "init", "--quiet", "--initial-branch=main", str(path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return path

    def run_aiq(
        self,
        *arguments: str,
        cwd: Path | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "aiq", *arguments],
            cwd=cwd or self.root,
            env=self.environment,
            input=input_text,
            text=True,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def event_json(self, document: object) -> str:
        return json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def ingest(
        self,
        document: object,
        *,
        command_cwd: Path,
        source: str = "-",
    ) -> subprocess.CompletedProcess[str]:
        input_text = self.event_json(document) if source == "-" else None
        return self.run_aiq(
            "ingest",
            "--event-json",
            source,
            "--scope",
            "repo",
            "--cwd",
            str(command_cwd),
            "--json",
            input_text=input_text,
        )

    def list_messages(self, repository: Path) -> list[dict[str, object]]:
        completed = self.run_aiq(
            "inbox",
            "list",
            "--include-content",
            "--scope",
            "repo",
            "--cwd",
            str(repository),
            "--json",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["v"], 1)
        return payload["messages"]

    def assert_json_error(
        self,
        completed: subprocess.CompletedProcess[str],
        *,
        exit_code: int,
        code: str,
    ) -> dict[str, object]:
        self.assertEqual(completed.returncode, exit_code)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr.count("\n"), 1)
        payload = json.loads(completed.stderr)
        self.assertEqual(
            set(("v", "status", "code", "error")) - payload.keys(),
            set(),
        )
        self.assertEqual(payload["v"], 1)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], code)
        self.assertIsInstance(payload["error"], str)
        return payload

    def test_stdin_preserves_exact_multiline_unicode_and_stream_contract(self) -> None:
        repository = self.repository("repository")
        content = " leading space\n雪 and ☃\nline\twith NUL:\u0000\n"
        event = {
            "v": 1,
            "source": "editor.bridge",
            "content": content,
            "idempotency_key": "editor:event-1",
            "session_id": "session-雪",
            "turn_id": "turn-1",
            "cwd": str(repository),
        }

        completed = self.ingest(event, command_cwd=repository)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout.count("\n"), 1)
        receipt = json.loads(completed.stdout)
        self.assertEqual(receipt["v"], 1)
        self.assertTrue(receipt["created"])
        messages = self.list_messages(repository)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], content)
        self.assertEqual(messages[0]["source"], event["source"])
        self.assertEqual(messages[0]["session_id"], event["session_id"])
        self.assertEqual(messages[0]["turn_id"], event["turn_id"])

    def test_identical_file_retry_returns_the_same_message(self) -> None:
        repository = self.repository("repository")
        event = {
            "v": 1,
            "source": "test-runner",
            "content": "retry me exactly\n",
            "idempotency_key": "test-runner:delivery-42",
            "cwd": str(repository),
        }
        event_path = self.root / "event.json"
        event_path.write_text(self.event_json(event), encoding="utf-8")

        first = self.ingest(
            event,
            command_cwd=repository,
            source=str(event_path),
        )
        second = self.ingest(
            event,
            command_cwd=repository,
            source=str(event_path),
        )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        first_receipt = json.loads(first.stdout)
        second_receipt = json.loads(second.stdout)
        self.assertTrue(first_receipt["created"])
        self.assertFalse(second_receipt["created"])
        self.assertEqual(
            first_receipt["message_id"],
            second_receipt["message_id"],
        )
        self.assertEqual(len(self.list_messages(repository)), 1)

    def test_conflicting_identity_and_content_is_a_state_conflict(self) -> None:
        repository = self.repository("repository")
        original = {
            "v": 1,
            "source": "bridge",
            "content": "original",
            "idempotency_key": "bridge:one-delivery",
            "cwd": str(repository),
        }
        conflict = {**original, "content": "different"}

        first = self.ingest(original, command_cwd=repository)
        second = self.ingest(conflict, command_cwd=repository)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assert_json_error(
            second,
            exit_code=4,
            code="state_conflict",
        )
        messages = self.list_messages(repository)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "original")

    def test_invalid_event_json_is_strict_and_does_not_mutate(self) -> None:
        repository = self.repository("repository")
        invalid_documents = (
            '{"v":1,',
            '{"v":1,"source":"one","source":"two","content":"x"}',
            '{"v":1,"source":"bridge","content":"x","unknown":true}',
            '{"v":1,"source":"bridge","content":NaN}',
        )

        for raw in invalid_documents:
            with self.subTest(raw=raw):
                completed = self.run_aiq(
                    "ingest",
                    "--event-json",
                    "-",
                    "--scope",
                    "repo",
                    "--cwd",
                    str(repository),
                    "--json",
                    input_text=raw,
                )
                self.assert_json_error(
                    completed,
                    exit_code=2,
                    code="invalid_document",
                )

        self.assertEqual(self.list_messages(repository), [])

    def test_event_cwd_overrides_cli_cwd_for_scope_and_storage(self) -> None:
        command_repository = self.repository("command-repository")
        event_repository = self.repository("event-repository")
        event = {
            "v": 1,
            "source": "bridge",
            "content": "route by event cwd",
            "cwd": str(event_repository),
        }

        completed = self.ingest(
            event,
            command_cwd=command_repository,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.list_messages(command_repository), [])
        messages = self.list_messages(event_repository)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["cwd"], str(event_repository.resolve()))

    def test_cli_cwd_is_used_when_event_cwd_is_omitted(self) -> None:
        repository = self.repository("repository")
        event = {
            "v": 1,
            "source": "bridge",
            "content": "route by command cwd",
        }

        completed = self.ingest(event, command_cwd=repository)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        messages = self.list_messages(repository)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["cwd"], str(repository.resolve()))


if __name__ == "__main__":
    unittest.main()
