"""The in-place schema migration announces itself, on the CLI only.

A forward-only migration locks out every AIQ installation older than the
new schema, runs implicitly on first open, and until now named nothing.
These tests pin what it says, where it says it, and — just as load-bearing
— the two places it stays silent: the library API, and the installed hook
paths whose stderr contracts are exact.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import support

from aiq.integrations.claude import receive_hook_main
from aiq.journal import (
    JournalScope,
    SCHEMA_VERSION,
    check_journal,
    resolve_scope,
)


FIXTURE_V4_PATH = Path(__file__).parent / "fixtures" / "schema-v4.sql"


def install_fixture(
    journal_path: Path,
    *,
    scope_kind: str,
    scope_root: str,
    scope_id: str,
    schema_version: int = 3,
) -> None:
    """Load the frozen v4 fixture as a journal at ``schema_version``.

    Schema 3 is the incident's starting point and is stated the way
    ``test_migration_fixture`` states it: the frozen v4 fixture minus the
    one table schema 4 introduced.
    """

    def sql_literal(value: str) -> str:
        return value.replace("'", "''")

    script = FIXTURE_V4_PATH.read_text(encoding="utf-8")
    for token, value in {
        "__AIQ_SCOPE_KIND__": sql_literal(scope_kind),
        "__AIQ_SCOPE_ROOT__": sql_literal(scope_root),
        "__AIQ_SCOPE_ID__": sql_literal(scope_id),
    }.items():
        script = script.replace(token, value)

    journal_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(journal_path)
    try:
        connection.executescript(script)
        if schema_version < 4:
            connection.execute("DROP TABLE reader_leases")
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
    journal_path.chmod(0o600)


def install_repo_fixture(repository: Path, **keywords) -> Path:
    """Install the fixture as ``repository``'s repo-scope journal."""

    scope = resolve_scope("repo", cwd=repository)
    install_fixture(
        scope.journal_path,
        scope_kind="repo",
        scope_root=".",
        scope_id="repo",
        **keywords,
    )
    return scope.journal_path


def stored_schema_version(journal_path: Path) -> int:
    connection = sqlite3.connect(f"file:{journal_path}?mode=ro", uri=True)
    try:
        return int(
            connection.execute(
                """
                SELECT value
                FROM journal_metadata
                WHERE key = 'schema_version'
                """
            ).fetchone()[0]
        )
    finally:
        connection.close()


class MigrationAnnouncementTests(unittest.TestCase):
    """What the CLI says when it migrates a journal in place."""

    def _agent_root_scope(self, root: Path) -> JournalScope:
        agent_root = root / "agent-root"
        agent_root.mkdir()
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
            return resolve_scope("agent-root", cwd=root, agent_root=agent_root)

    def _run_agent_root_cli(
        self,
        root: Path,
        scope: JournalScope,
        *arguments: str,
    ) -> support.CliResult:
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
            return support.run_cli(
                *arguments,
                "--scope",
                "agent-root",
                "--agent-root",
                str(scope.root),
                "--cwd",
                str(root),
                "--no-repo-config",
            )

    def test_cli_announces_the_migration_it_is_about_to_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self._agent_root_scope(root)
            install_fixture(
                scope.journal_path,
                scope_kind=scope.kind,
                scope_root=str(scope.root),
                scope_id=scope.scope_id,
            )

            result = self._run_agent_root_cli(
                root,
                scope,
                "journal",
                "check",
                "--json",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            # The announcement rides on stderr, so the machine-readable
            # response on stdout is still one clean JSON document.
            payload = json.loads(result.stdout)
            self.assertEqual(payload["v"], 1)
            self.assertEqual(payload["status"], "ok")

            lines = result.stderr.splitlines()
            self.assertEqual(len(lines), 1, result.stderr)
            notice = lines[0]
            self.assertIn(
                f"aiq: migrating journal schema 3 -> {SCHEMA_VERSION} "
                "in place:",
                notice,
            )
            self.assertIn(str(scope.journal_path), notice)
            self.assertIn(f"(scope {scope.kind})", notice)
            self.assertIn(
                "forward-only, so AIQ installations older than schema "
                f"{SCHEMA_VERSION} can no longer open this journal",
                notice,
            )

            # The named backup is a real file that already exists by the
            # time the line is printed, which is what makes the notice
            # actionable rather than merely informative.
            _, _, backup = notice.partition("pre-migration backup: ")
            self.assertTrue(backup)
            backup_path = Path(backup)
            self.assertTrue(backup_path.is_file(), backup)
            self.assertEqual(
                backup_path.parent,
                scope.journal_path.parent / "backups",
            )
            self.assertEqual(stored_schema_version(backup_path), 3)
            self.assertEqual(
                stored_schema_version(scope.journal_path),
                SCHEMA_VERSION,
            )

    def test_settled_journal_announces_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self._agent_root_scope(root)

            initialized = self._run_agent_root_cli(
                root,
                scope,
                "journal",
                "init",
            )
            checked = self._run_agent_root_cli(
                root,
                scope,
                "journal",
                "check",
            )

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            # Nothing to migrate, so nothing is said, on either call.
            self.assertEqual(initialized.stderr, "")
            self.assertEqual(checked.stderr, "")

    def test_announcement_is_written_once_per_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self._agent_root_scope(root)
            install_fixture(
                scope.journal_path,
                scope_kind=scope.kind,
                scope_root=str(scope.root),
                scope_id=scope.scope_id,
            )

            first = self._run_agent_root_cli(root, scope, "status")
            second = self._run_agent_root_cli(root, scope, "status")

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(len(first.stderr.splitlines()), 1)
            # The migration has already run; a settled journal is silent.
            self.assertEqual(second.stderr, "")


class AutoFallbackAnnouncementTests(unittest.TestCase):
    """The fallback that chose the journal is named, not merely implied."""

    def test_auto_outside_a_repository_records_the_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(root / "state"), **support.GIT_ISOLATION},
            ):
                resolved = resolve_scope("auto", cwd=root)
                named = resolve_scope("user", cwd=root)

            self.assertEqual(resolved.kind, "user")
            self.assertEqual(resolved.requested_kind, "auto")
            # Naming user scope is a choice, not a fallback, and reads as
            # one; the resolved journal is the same file either way.
            self.assertIsNone(named.requested_kind)
            self.assertEqual(resolved.journal_path, named.journal_path)
            # The resolution detail stays out of the documented Scope
            # response shape.
            self.assertEqual(resolved.to_dict(), named.to_dict())

    def test_announcement_names_the_auto_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_home = root / "state"
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(state_home), **support.GIT_ISOLATION},
            ):
                scope = resolve_scope("auto", cwd=root)
                self.assertEqual(scope.kind, "user")
                install_fixture(
                    scope.journal_path,
                    scope_kind="user",
                    scope_root=str(scope.root),
                    scope_id=scope.scope_id,
                )

                result = support.run_cli(
                    "journal",
                    "check",
                    "--scope",
                    "auto",
                    "--cwd",
                    str(root),
                    "--no-repo-config",
                )

            self.assertEqual(result.returncode, 0, result.stderr)
            lines = result.stderr.splitlines()
            self.assertEqual(len(lines), 1, result.stderr)
            # This is the incident sentence: a user journal nobody named.
            self.assertIn(
                "(scope user, selected by --scope auto fallback outside "
                "any repository)",
                lines[0],
            )
            self.assertIn(str(scope.journal_path), lines[0])


class LibrarySilenceTests(unittest.TestCase):
    """A library import must not make a program write to stderr."""

    def _scope(self, root: Path) -> JournalScope:
        agent_root = root / "agent-root"
        agent_root.mkdir()
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}):
            return resolve_scope("agent-root", cwd=root, agent_root=agent_root)

    def test_library_migration_is_silent_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self._scope(root)
            install_fixture(
                scope.journal_path,
                scope_kind=scope.kind,
                scope_root=str(scope.root),
                scope_id=scope.scope_id,
            )
            errors = io.StringIO()

            with contextlib.redirect_stderr(errors):
                result = check_journal(scope)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["schema_version"], SCHEMA_VERSION)
            self.assertEqual(errors.getvalue(), "")

    def test_announcing_scope_opts_the_same_call_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self._scope(root)
            install_fixture(
                scope.journal_path,
                scope_kind=scope.kind,
                scope_root=str(scope.root),
                scope_id=scope.scope_id,
            )
            errors = io.StringIO()

            with contextlib.redirect_stderr(errors):
                result = check_journal(scope.announcing())

            self.assertEqual(result["status"], "ok")
            self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_an_unwritable_stderr_never_fails_the_migration(self) -> None:
        class BrokenStream:
            def write(self, value: str) -> int:
                raise ValueError("stream is closed")

            def flush(self) -> None:
                raise ValueError("stream is closed")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scope = self._scope(root)
            install_fixture(
                scope.journal_path,
                scope_kind=scope.kind,
                scope_root=str(scope.root),
                scope_id=scope.scope_id,
            )

            with patch("sys.stderr", BrokenStream()):
                result = check_journal(scope.announcing())

            # A diagnostic that cannot be delivered is not a reason to
            # leave a journal half-migrated.
            self.assertEqual(result["status"], "ok")
            self.assertEqual(
                stored_schema_version(scope.journal_path),
                SCHEMA_VERSION,
            )


class HookPathContractTests(unittest.TestCase):
    """Migrating on a hook path changes nothing about the hook contract.

    Both installed hooks resolve their own scopes and never opt in, so a
    migration underneath them stays silent: capture writes nothing at all
    on success, and the gate keeps its documented exactly-one-line stderr
    budget on the channel the host feeds back to the model.
    """

    def git_executable(self) -> Path:
        return support.git_executable()

    def test_capture_migrates_silently_and_writes_no_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            journal_path = install_repo_fixture(repository)
            payload = json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session",
                    "prompt_id": "prompt-1",
                    "cwd": str(repository),
                    "prompt": "capture across a migration",
                }
            ).encode()
            errors = io.StringIO()
            process_out, process_err = io.StringIO(), io.StringIO()

            with (
                contextlib.redirect_stdout(process_out),
                contextlib.redirect_stderr(process_err),
            ):
                code = receive_hook_main(
                    input_stream=io.BytesIO(payload),
                    error_stream=errors,
                    git_executable=self.git_executable(),
                )

            self.assertEqual(code, 0)
            # Claude Code injects a UserPromptSubmit hook's stdout into
            # the prompt; capture owns none of it.
            self.assertEqual(process_out.getvalue(), "")
            self.assertEqual(process_err.getvalue(), "")
            self.assertEqual(errors.getvalue(), "")
            # The migration still happened, and the prompt still landed.
            self.assertEqual(stored_schema_version(journal_path), SCHEMA_VERSION)
            scope = resolve_scope("repo", cwd=repository)
            self.assertEqual(check_journal(scope)["status"], "ok")

    def test_stop_gate_keeps_one_line_across_a_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = support.init_repository(root / "repository")
            journal_path = install_repo_fixture(repository)
            payload = json.dumps(
                {
                    "hook_event_name": "Stop",
                    "session_id": "session",
                    "cwd": str(repository),
                    "stop_hook_active": False,
                }
            ).encode()
            errors = io.StringIO()
            process_out, process_err = io.StringIO(), io.StringIO()

            with (
                contextlib.redirect_stdout(process_out),
                contextlib.redirect_stderr(process_err),
            ):
                code = receive_hook_main(
                    input_stream=io.BytesIO(payload),
                    error_stream=errors,
                    git_executable=self.git_executable(),
                )

            # The gate allows or blocks; it never fails the host, and it
            # never emits a second line for a migration.
            self.assertIn(code, (0, 2))
            self.assertEqual(process_out.getvalue(), "")
            self.assertEqual(process_err.getvalue(), "")
            self.assertLessEqual(len(errors.getvalue().splitlines()), 1)
            self.assertEqual(stored_schema_version(journal_path), SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
