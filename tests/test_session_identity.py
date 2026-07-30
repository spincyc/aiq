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

    def test_naming_the_holder_never_revokes_a_live_lease(self) -> None:
        """A reader identity is a public name, not a credential.

        `aiq reader status` prints the holder's identity and `--reader`
        accepts it, so releasing on a matching string would let any
        onlooker end a live lease -- and, because release leaves the
        recorded holder locator in place, hand the rightful holder a
        `released_by_self` reading it never declared. Releasing takes
        proof of holding instead: the same locator comparison that
        decides `reader.live`.
        """
        self.run_step("enqueue", "Write the release notes")
        self.json_run("dequeue", session_id=SESSION_ONE)

        # A second session reads the holder's identity straight out of
        # the public status, then names it back.
        published, _ = self.json_run(
            "reader", "status", session_id=SESSION_TWO
        )
        self.assertEqual(published["reader"]["reader_id"], SESSION_ONE)
        stolen = self.run_step(
            "reader",
            "release",
            "--reader",
            published["reader"]["reader_id"],
            "--json",
            session_id=SESSION_TWO,
        )

        self.assertEqual(stolen.returncode, 4, stolen.stdout)
        self.assertIn("reader_held", stolen.stderr)

        # The holder still holds the role and has declared nothing, so
        # the gate the attempt would have switched off still blocks.
        held = read_status(self.scope, session_id=SESSION_ONE)
        self.assertEqual(held["reader"]["status"], "held")
        self.assertFalse(held["reader"]["released_by_self"])
        blocked = gate_hook(
            self.stop_payload(SESSION_ONE),
            git_executable=support.git_executable(),
        )
        self.assertIsNotNone(blocked)
        self.assertTrue(blocked[0])

        # The holder itself needs no flag and no argument: it releases
        # from a third POSIX session again, and its own session identity
        # is the whole proof.
        released, _ = self.json_run(
            "reader", "release", session_id=SESSION_ONE
        )
        self.assertEqual(released["status"], "released")
        self.assertTrue(released["released"])

    def test_a_forced_break_frees_the_role_and_declares_nothing(self) -> None:
        """The operator override, and the two things it must not do.

        A lease held by a host-identified session is never proved dead,
        because nothing can probe such an identity, so an abandoned one
        is otherwise recoverable only by waiting out
        `reader_lease_seconds` -- up to a day. `--force` is the way to
        take it back, and it is a deliberate, named act precisely so
        that knowing a public identity string is not.
        """
        self.run_step("enqueue", "Write the release notes")
        self.run_step("enqueue", "And the changelog")
        self.json_run("dequeue", session_id=SESSION_ONE)

        forced, result = self.json_run(
            "reader", "release", "--force", session_id=SESSION_TWO
        )

        self.assertEqual(forced["status"], "forced")
        # Breaking a lease is not this session declaring it finished, so
        # `released` is false; it did change the row, so `replayed` is
        # false too.
        self.assertFalse(forced["released"])
        self.assertFalse(forced["replayed"])
        self.assertIn("broke the live reader lease", result.stderr)

        # No session gets a declaration out of it: not the breaker, who
        # never held the role, and not the former holder, who never gave
        # it up. Both still answer to the gate.
        for session in (SESSION_ONE, SESSION_TWO):
            with self.subTest(session=session):
                status = read_status(self.scope, session_id=session)
                self.assertFalse(status["reader"]["released_by_self"])
                reason = gate_hook(
                    self.stop_payload(session),
                    git_executable=support.git_executable(),
                )
                self.assertIsNotNone(reason)
                self.assertTrue(reason[0])

        # The role really is free for whoever asks next.
        taken, _ = self.json_run("dequeue", session_id=SESSION_TWO)
        self.assertEqual(len(taken["items"]), 1)

    def test_a_bounded_run_still_ends_on_its_own_release(self) -> None:
        """The documented "run one task, then stop" mode, end to end.

        This is the shape that has to keep working now that releasing
        demands proof: every step is its own POSIX session, and only the
        host-supplied identity ties them together.
        """
        self.run_step("enqueue", "The one task")
        self.run_step("enqueue", "Deliberately left behind")

        dequeued, _ = self.json_run(
            "dequeue", "--limit", "1", session_id=SESSION_ONE
        )
        self.json_run(
            "task",
            "done",
            dequeued["items"][0]["task"]["task_id"],
            "--summary",
            "Drafted",
            session_id=SESSION_ONE,
        )
        released, _ = self.json_run(
            "reader", "release", session_id=SESSION_ONE
        )
        self.assertEqual(released["status"], "released")

        # The documented stop predicate, read from one status call.
        status, _ = self.json_run("status", session_id=SESSION_ONE)
        self.assertEqual(status["reader"]["status"], "released")
        self.assertTrue(status["reader"]["released_by_self"])
        self.assertEqual(status["claims"]["active_this_session"], 0)
        self.assertGreater(status["tasks"]["ready"], 0)

        stood_down = gate_hook(
            self.stop_payload(SESSION_ONE),
            git_executable=support.git_executable(),
        )
        self.assertIsNotNone(stood_down)
        self.assertFalse(stood_down[0])
        self.assertIn("this session released the reader role", stood_down[1])

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

    def test_a_tokenless_caller_never_inherits_a_token_holders_lease(
        self,
    ) -> None:
        """A bare session id proves nothing about a token-named session.

        The holder here recorded *both* halves of a locator: a
        host-supplied token, and the POSIX pair of the process that took
        the lease -- this very process, so the pair matches exactly. A
        caller carrying no token of its own must still not read that
        lease as its own or as a live stranger's, because the recorded
        token says the holder belongs to a host that keeps its own
        sessions, and on such a host the process behind that session id
        is very likely already reaped. Session ids are reissued, so
        matching one is no evidence; `_reader_holder_is_dead` has always
        refused to probe such a holder, and these two must agree with
        it.

        Without this rule a tokenless caller that happened to land on a
        recycled session id would read a stranger's release as its own
        and could release the stranger's live lease -- putting "I am
        done" into another session's mouth, on a number.
        """
        from aiq.queue import (
            _reader_holder_is_dead,
            _reader_holder_is_foreign_live,
            _reader_holder_is_this_session,
        )

        os.environ[GENERIC_SESSION_ID_KEY] = SESSION_ONE
        try:
            support.hold_reader_lease_with_locator(
                self.scope,
                host=socket.gethostname(),
                session=os.getsid(0),
            )
        finally:
            os.environ.pop(GENERIC_SESSION_ID_KEY, None)
        row = self.lease_row()
        # Both halves really are recorded, and the POSIX pair is this
        # process's own -- so only the token rule can produce a false.
        self.assertEqual(row["holder_session"], SESSION_ONE)
        self.assertEqual(row["holder_host"], socket.gethostname())
        self.assertEqual(row["holder_sid"], os.getsid(0))

        self.assertFalse(_reader_holder_is_this_session(row, token=None))
        self.assertFalse(_reader_holder_is_foreign_live(row, token=None))
        self.assertFalse(_reader_holder_is_dead(row))

        # The holder's own token still recognizes it, and a different
        # token still reads it as the live stranger it is.
        self.assertTrue(
            _reader_holder_is_this_session(row, token=SESSION_ONE)
        )
        self.assertTrue(
            _reader_holder_is_foreign_live(row, token=SESSION_TWO)
        )

    def test_a_tokenless_caller_never_counts_a_token_holders_claim(
        self,
    ) -> None:
        """The claim count is decided on the identical evidence.

        `_count_active_claims_this_session` documents that its SQL
        mirrors the locator rule exactly and that the two must change
        together, so the token asymmetry above is pinned on this side
        too.
        """
        from aiq.queue import (
            _count_active_claims_this_session,
            _now_us,
            enqueue_task,
        )

        enqueue_task(self.scope, title="Claimed work", owner_id="claimer")
        os.environ[GENERIC_SESSION_ID_KEY] = SESSION_ONE
        try:
            claimed = support.claim_next_task_with_locator(
                self.scope,
                locator=(socket.gethostname(), os.getsid(0)),
            )
        finally:
            os.environ.pop(GENERIC_SESSION_ID_KEY, None)
        self.assertEqual(len(claimed), 1)

        connection = sqlite3.connect(self.scope.journal_path)
        connection.row_factory = sqlite3.Row
        try:
            now = _now_us()
            self.assertEqual(
                _count_active_claims_this_session(
                    connection, now_us=now, token=SESSION_ONE
                ),
                1,
            )
            # Same claim, same matching POSIX pair, no token to compare:
            # not this session's, so it blocks nobody.
            self.assertEqual(
                _count_active_claims_this_session(
                    connection, now_us=now, token=None
                ),
                0,
            )
            self.assertEqual(
                _count_active_claims_this_session(
                    connection, now_us=now, token=SESSION_TWO
                ),
                0,
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
