"""Session identity across POSIX sessions.

Every test that crosses a session boundary here runs its steps as
separate POSIX session leaders. That is the whole point: a host such as
Claude Code runs each shell command in a session of its own, so the
process that took a lease or a claim is already gone when the next
command -- or the `Stop` hook -- asks about it. A test that merely calls
`subprocess.run` cannot reproduce that, because a child inherits its
parent's session, which is exactly how this defect survived a passing
suite.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import tempfile
import unittest

import support
from aiq.config import (
    GENERIC_SESSION_ID_KEY,
    HOST_SESSION_ID_KEYS,
    _default_reader,
    resolve_config,
    session_token,
)
from aiq.integrations.claude import gate_hook
from aiq.journal import resolve_scope
from aiq.queue import read_status


CLAUDE_KEY = "CLAUDE_CODE_SESSION_ID"
SESSION_ONE = "11111111-1111-4111-8111-111111111111"
SESSION_TWO = "22222222-2222-4222-8222-222222222222"


class SessionTokenTest(unittest.TestCase):
    """The precedence by which one session's identity is decided."""

    def test_claude_code_is_a_known_host_variable(self) -> None:
        # The verified fact this whole mechanism rests on: Claude Code
        # exports this variable into every command it runs, and puts the
        # same value in the `session_id` of the hook payloads for that
        # session. If the name ever changes, this fails loudly here
        # rather than silently mis-identifying sessions at runtime.
        self.assertIn(CLAUDE_KEY, HOST_SESSION_ID_KEYS)
        self.assertEqual(GENERIC_SESSION_ID_KEY, "AIQ_SESSION_ID")

    def test_the_generic_variable_outranks_a_hosts_own(self) -> None:
        environment = {
            GENERIC_SESSION_ID_KEY: SESSION_ONE,
            CLAUDE_KEY: SESSION_TWO,
        }
        self.assertEqual(session_token(environment), SESSION_ONE)
        # It also outranks an identity the host handed us directly, so an
        # operator can override a host that is naming sessions unhelpfully.
        self.assertEqual(
            session_token(environment, supplied=SESSION_TWO),
            SESSION_ONE,
        )

    def test_a_supplied_identity_outranks_the_hosts_environment(self) -> None:
        # What the completion gate relies on: the payload's session id is
        # authoritative for the session being gated, while a variable in
        # the hook process's environment is merely whatever it inherited.
        self.assertEqual(
            session_token({CLAUDE_KEY: SESSION_TWO}, supplied=SESSION_ONE),
            SESSION_ONE,
        )

    def test_a_hosts_variable_is_used_when_nothing_outranks_it(self) -> None:
        self.assertEqual(session_token({CLAUDE_KEY: SESSION_ONE}), SESSION_ONE)

    def test_nothing_supplied_falls_back_to_the_posix_locator(self) -> None:
        self.assertIsNone(session_token({}))
        self.assertEqual(
            _default_reader({}),
            f"{socket.gethostname()}-{os.getsid(0)}",
        )

    def test_empty_and_unusable_values_count_as_unset(self) -> None:
        self.assertIsNone(session_token({GENERIC_SESSION_ID_KEY: ""}))
        self.assertIsNone(session_token({GENERIC_SESSION_ID_KEY: "  \t "}))
        # A surprising value is reduced, never fatal: this string comes
        # from a host we do not control, and no AIQ command may fail on it.
        self.assertEqual(
            session_token({GENERIC_SESSION_ID_KEY: " a b\nc "}),
            "abc",
        )
        self.assertEqual(
            session_token({GENERIC_SESSION_ID_KEY: "x" * 500}),
            "x" * 200,
        )

    def test_the_session_identity_becomes_the_default_reader(self) -> None:
        self.assertEqual(
            _default_reader({CLAUDE_KEY: SESSION_ONE}),
            SESSION_ONE,
        )
        self.assertEqual(
            _default_reader({}, session_id=SESSION_ONE),
            SESSION_ONE,
        )

    def test_a_configured_reader_outranks_every_derived_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = {
                "HOME": str(root),
                CLAUDE_KEY: SESSION_ONE,
                "AIQ_READER": "explicit-fan-out",
            }
            from_environment = resolve_config(
                cwd=root,
                environ=environment,
                user_path=None,
                repo_path=None,
            )
            self.assertEqual(from_environment.reader, "explicit-fan-out")
            self.assertEqual(
                from_environment.sources["reader"],
                "env:AIQ_READER",
            )

            from_cli = resolve_config(
                cwd=root,
                cli={"reader": "explicit-cli"},
                environ=environment,
                user_path=None,
                repo_path=None,
            )
            self.assertEqual(from_cli.reader, "explicit-cli")

            # And with nothing explicit, the host's identity is the default.
            derived = resolve_config(
                cwd=root,
                environ={"HOME": str(root), CLAUDE_KEY: SESSION_ONE},
                user_path=None,
                repo_path=None,
            )
            self.assertEqual(derived.reader, SESSION_ONE)
            self.assertEqual(derived.sources["reader"], "default")


class SeparateSessionTest(unittest.TestCase):
    """Steps that really do run in different POSIX sessions."""

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("test requires Git")
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)
        self.repository = support.init_repository(root / "repository")
        self.state = root / "state"
        self.state.mkdir()
        self.scope = resolve_scope("repo", cwd=self.repository)
        self.run_step("journal", "init", "--scope", "repo")

    def environment(self, session_id: str | None = None) -> dict[str, str]:
        overrides = {"XDG_STATE_HOME": str(self.state)}
        if session_id is not None:
            overrides[CLAUDE_KEY] = session_id
        return support.scrubbed_environment(**overrides)

    def run_step(
        self,
        *arguments: str,
        session_id: str | None = None,
    ) -> support.SeparateSessionResult:
        """Run one command as its own, brand-new POSIX session leader."""
        return support.run_cli_in_a_separate_session(
            *arguments,
            cwd=self.repository,
            environment=self.environment(session_id),
        )

    def json_run(self, *arguments: str, **keywords):
        result = self.run_step(*arguments, "--json", **keywords)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout), result

    def test_a_release_is_recognized_across_posix_sessions(self) -> None:
        """The reported defect, and its fix, end to end.

        One session dequeues; a *different* POSIX session releases the
        role. With a host-supplied identity the second run recognizes the
        first's lease as its own and records the release -- the signal a
        bounded run needs -- even though the acquiring session is dead.
        """
        self.run_step("enqueue", "Write the release notes")

        dequeued, first = self.json_run("dequeue", session_id=SESSION_ONE)
        self.assertEqual(len(dequeued["items"]), 1)

        released, second = self.json_run(
            "reader", "release", session_id=SESSION_ONE
        )

        # The steps really were different sessions: this is the condition
        # the old host-plus-POSIX-session identity could not survive.
        self.assertNotEqual(first.session, second.session)
        self.assertEqual(released["status"], "released")
        self.assertTrue(released["released"])
        self.assertFalse(released["replayed"])
        # The lease is recorded as released, which is what a completion
        # gate reads; the old code left `released_at_us` unset.
        self.assertEqual(released["reader"]["status"], "released")
        # And the claim taken by the first session is recognized as this
        # session's too, so releasing the role warns about unsettled work
        # instead of reporting a clean exit.
        self.assertEqual(released["claims_held"], 1)
        self.assertIn("still holding 1 active claim", second.stderr)

    def test_a_replayed_release_says_it_already_stands(self) -> None:
        self.run_step("enqueue", "Write the release notes")
        self.json_run("dequeue", session_id=SESSION_ONE)
        self.json_run("reader", "release", session_id=SESSION_ONE)

        replayed, result = self.json_run(
            "reader", "release", session_id=SESSION_ONE
        )

        self.assertEqual(replayed["status"], "already_released")
        self.assertFalse(replayed["released"])
        self.assertTrue(replayed["replayed"])
        self.assertNotIn("nothing to release", result.stderr)

    def test_a_release_with_no_host_identity_reports_honestly(self) -> None:
        """Without a host identity the release cannot be recognized.

        That is not a regression -- a POSIX session id genuinely cannot
        identify a session on such a host -- but reporting it as a
        success was. The command must say that nothing of the caller's
        was released, so nothing downstream waits for a signal that was
        never recorded.
        """
        self.run_step("enqueue", "Write the release notes")

        dequeued, first = self.json_run("dequeue")
        self.assertEqual(len(dequeued["items"]), 1)

        released, second = self.json_run("reader", "release")

        self.assertNotEqual(first.session, second.session)
        self.assertEqual(released["status"], "not_held")
        self.assertFalse(released["released"])
        # Still exit 0: release is a total, replayable declaration and
        # refusing would break replay. Honesty is in the report.
        self.assertEqual(second.returncode, 0)
        self.assertIn("nothing to release", second.stderr)
        # Nothing was recorded, so the lease is not released.
        self.assertNotEqual(released["reader"]["status"], "released")

        status, _ = self.json_run("reader", "status")
        self.assertNotEqual(status["reader"]["status"], "released")

    def test_a_foreign_host_session_is_refused_the_live_role(self) -> None:
        """A live holder is protected from a stranger, not overrun.

        Under the POSIX-only locator the holder's shell was already dead,
        so any later command read the lease as abandoned and took it. A
        host-supplied identity names a session the host still keeps, so
        the second session is correctly told it is not the reader.
        """
        self.run_step("enqueue", "Write the release notes")
        self.run_step("enqueue", "And the changelog")
        self.json_run("dequeue", session_id=SESSION_ONE)

        stolen = self.run_step("dequeue", "--json", session_id=SESSION_TWO)
        self.assertEqual(stolen.returncode, 4, stolen.stderr)
        self.assertIn("reader_held", stolen.stdout + stolen.stderr)

        foreign_release = self.run_step(
            "reader", "release", "--json", session_id=SESSION_TWO
        )
        self.assertEqual(foreign_release.returncode, 4, foreign_release.stderr)

        # The rightful holder, in yet another POSIX session, still has it.
        again, _ = self.json_run("dequeue", session_id=SESSION_ONE)
        self.assertEqual(len(again["items"]), 1)

    def test_claims_from_a_dead_session_are_still_this_sessions(self) -> None:
        self.run_step("enqueue", "Write the release notes")
        self.json_run("dequeue", session_id=SESSION_ONE)

        mine = read_status(self.scope, session_id=SESSION_ONE)
        theirs = read_status(self.scope, session_id=SESSION_TWO)
        derived = read_status(self.scope)

        self.assertEqual(mine["claims"]["active"], 1)
        self.assertEqual(mine["claims"]["active_this_session"], 1)
        # A different session must not inherit the obligation, and this
        # test process -- a third session again -- must not either.
        self.assertEqual(theirs["claims"]["active_this_session"], 0)
        self.assertEqual(derived["claims"]["active_this_session"], 0)

    def test_the_stop_gate_reads_the_payload_session_identity(self) -> None:
        """The gate agrees with the CLI because the host tells them both.

        The release is recorded by a command in one POSIX session; the
        gate runs in this process, a different session entirely, and
        recognizes it only because the payload carries the same
        host-supplied identity.
        """
        self.run_step("enqueue", "Write the release notes")
        dequeued, _ = self.json_run("dequeue", session_id=SESSION_ONE)
        # Settle the claimed task, so the release is the only thing left
        # deciding whether this session may stop.
        task_id = dequeued["items"][0]["task"]["task_id"]
        self.json_run(
            "task",
            "done",
            task_id,
            "--summary",
            "Drafted",
            session_id=SESSION_ONE,
        )
        self.run_step("enqueue", "Something else entirely")
        self.json_run("reader", "release", session_id=SESSION_ONE)

        stood_down = gate_hook(
            self.stop_payload(SESSION_ONE),
            git_executable=support.git_executable(),
        )
        blocked = gate_hook(
            self.stop_payload(SESSION_TWO),
            git_executable=support.git_executable(),
        )

        self.assertIsNotNone(stood_down)
        self.assertFalse(stood_down[0])
        self.assertIn("this session released the reader role", stood_down[1])
        # A different session is not covered by that release and is still
        # obliged to look at the queue.
        self.assertIsNotNone(blocked)
        self.assertTrue(blocked[0])
        self.assertIn("runnable work remains", blocked[1])

    def test_the_stop_gate_stands_down_for_a_foreign_live_session(self) -> None:
        self.run_step("enqueue", "Write the release notes")
        self.run_step("enqueue", "And the changelog")
        self.json_run("dequeue", session_id=SESSION_ONE)

        foreign = gate_hook(
            self.stop_payload(SESSION_TWO),
            git_executable=support.git_executable(),
        )
        holder = gate_hook(
            self.stop_payload(SESSION_ONE),
            git_executable=support.git_executable(),
        )

        # Session two is a writer only: session one holds the role.
        self.assertIsNotNone(foreign)
        self.assertFalse(foreign[0])
        self.assertIn("holds the reader lease", foreign[1])
        # The holder itself is still accountable and still blocks.
        self.assertIsNotNone(holder)
        self.assertTrue(holder[0])

    def test_a_payload_without_a_session_id_keeps_the_old_bias(self) -> None:
        self.run_step("enqueue", "Write the release notes")
        self.json_run("dequeue", session_id=SESSION_ONE)

        reason = gate_hook(
            json.dumps(
                {
                    "hook_event_name": "Stop",
                    "cwd": str(self.repository),
                }
            ),
            git_executable=support.git_executable(),
        )

        # Nothing identifies this gate as any session, so nothing is
        # proved and the gate blocks -- the conservative bias, unchanged.
        self.assertIsNotNone(reason)
        self.assertTrue(reason[0])

    def stop_payload(self, session_id: str | None) -> str:
        payload = {
            "hook_event_name": "Stop",
            "cwd": str(self.repository),
        }
        if session_id is not None:
            payload["session_id"] = session_id
        return json.dumps(payload)


class RecordedLocatorCompatibilityTest(unittest.TestCase):
    """Locators recorded before host identities existed."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)
        agent_root = root / "agent-root"
        agent_root.mkdir()
        self.state = root / "state"
        os.environ["XDG_STATE_HOME"] = str(self.state)
        self.addCleanup(os.environ.pop, "XDG_STATE_HOME", None)
        self.scope = resolve_scope(
            "agent-root",
            cwd=root,
            agent_root=agent_root,
        )
        from aiq.journal import initialize_journal

        initialize_journal(self.scope)

    def lease_row(self) -> sqlite3.Row:
        connection = sqlite3.connect(self.scope.journal_path)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute("SELECT * FROM reader_leases").fetchone()
        finally:
            connection.close()

    def test_a_posix_only_locator_still_names_this_session(self) -> None:
        """A lease recorded before schema 6 keeps working as it did.

        Nothing crashes on the missing half of the locator, and the POSIX
        pair still decides -- which is the right answer wherever one
        session really does span many commands.
        """
        from aiq.queue import _reader_holder_is_this_session

        support.release_reader_lease_from_this_session(self.scope)
        row = self.lease_row()
        self.assertIsNone(row["holder_session"])

        # No host identity on this side either: the POSIX pair decides,
        # and it names this very process.
        self.assertTrue(_reader_holder_is_this_session(row, token=None))
        # With a host identity on only one side there is no common
        # ground beyond the POSIX pair, which still matches here.
        self.assertTrue(
            _reader_holder_is_this_session(row, token=SESSION_ONE)
        )

    def test_a_locator_of_a_different_shape_never_raises(self) -> None:
        from aiq.queue import (
            _reader_holder_is_dead,
            _reader_holder_is_foreign_live,
            _reader_holder_is_this_session,
        )

        support.hold_reader_lease_with_locator(
            self.scope,
            host="somewhere-else",
            session=support.dead_session_id(),
        )
        row = self.lease_row()

        for token in (None, SESSION_ONE):
            with self.subTest(token=token):
                self.assertFalse(
                    _reader_holder_is_this_session(row, token=token)
                )
                self.assertFalse(
                    _reader_holder_is_foreign_live(row, token=token)
                )
                # Another host's session cannot be probed from here, so
                # death is unproven and the lease is left alone.
                self.assertFalse(_reader_holder_is_dead(row))


if __name__ == "__main__":
    unittest.main()
