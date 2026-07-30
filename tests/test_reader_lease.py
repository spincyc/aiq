"""Scope-level reader lease: many writers, one reader.

Any session may ingest and enqueue; exactly one may drain. These tests
cover exclusion, the writer paths that stay open, implicit acquisition on
a successful consume, expiry takeover, explicit release, the
settlement-versus-dispatch split, acquisition races, migration and
export, scope independence, and the status datum the Stop gate reads.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

import support
from aiq.config import _default_reader
from aiq.journal import (
    JournalError,
    check_journal,
    ingest_message,
    initialize_journal,
    list_inbox,
    resolve_scope,
)
from aiq.privacy import export_journal
from aiq.queue import (
    _now_us,
    acquire_reader_lease,
    apply_effects,
    claim_message,
    claim_next_tasks,
    dispose_message,
    enqueue_task,
    next_tasks,
    read_reader_lease,
    read_status,
    release_claim,
    release_reader_lease,
    settle_tasks_done,
)


SECOND = 1_000_000
READER_A = "reader-a"
READER_B = "reader-b"


class ReaderLeaseTest(unittest.TestCase):
    """Library-level semantics against one isolated agent-root scope."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.environment = patch.dict(
            os.environ,
            {"XDG_STATE_HOME": str(self.root / "state")},
        )
        self.environment.start()
        self.agent_root = self.root / "agent"
        self.agent_root.mkdir()
        self.scope = resolve_scope(
            "agent-root",
            cwd=self.root,
            agent_root=self.agent_root,
        )
        self.now = _now_us()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary_directory.cleanup()

    def enqueue(self, title: str, *, priority: int = 0) -> str:
        return enqueue_task(
            self.scope,
            title=title,
            priority=priority,
            owner_id="writer",
            cwd=str(self.root),
        )["task_id"]

    def dequeue(self, reader_id: str, **overrides):
        arguments = {
            "owner_id": "worker",
            "reader_id": reader_id,
            "reader_lease_seconds": 60,
            "now_us": self.now,
        }
        arguments.update(overrides)
        return claim_next_tasks(self.scope, **arguments)

    def lease(self, reader_id: str | None = None, **overrides):
        return read_reader_lease(
            self.scope,
            reader_id=reader_id,
            now_us=overrides.get("now_us", self.now),
        )

    # 1. Exclusion.

    def test_a_foreign_live_reader_excludes_every_consume(self) -> None:
        self.enqueue("Contested work")
        self.assertTrue(self.dequeue(READER_A))
        ingested = ingest_message(self.scope, "Unread", cwd=str(self.root))

        for consume in (
            lambda: self.dequeue(READER_B),
            lambda: claim_message(
                self.scope,
                owner_id="worker",
                reader_id=READER_B,
                now_us=self.now,
            ),
            lambda: claim_message(
                self.scope,
                owner_id="worker",
                message_id=ingested.message_id,
                reader_id=READER_B,
                now_us=self.now,
            ),
        ):
            with self.subTest(consume=consume):
                with self.assertRaises(JournalError) as raised:
                    consume()
                self.assertEqual(raised.exception.code, "reader_held")
                self.assertIn(f'reader "{READER_A}"', str(raised.exception))
                self.assertIn(
                    "ingest and enqueue remain open",
                    str(raised.exception),
                )

    def test_exclusion_holds_even_when_nothing_is_waiting(self) -> None:
        self.enqueue("Only work")
        self.assertTrue(self.dequeue(READER_A))
        # Everything drained: the queue and the inbox are both empty, and
        # the truthful answer for a non-holder is still that it is not
        # the reader, not a cheerful empty result.
        self.assertEqual(next_tasks(self.scope), [])
        self.assertEqual(list_inbox(self.scope), [])

        with self.assertRaises(JournalError) as raised:
            self.dequeue(READER_B)
        self.assertEqual(raised.exception.code, "reader_held")

        with self.assertRaises(JournalError) as raised:
            claim_message(
                self.scope,
                owner_id="worker",
                reader_id=READER_B,
                now_us=self.now,
            )
        self.assertEqual(raised.exception.code, "reader_held")

    # 2. Writers stay open.

    def test_writers_and_readers_stay_open_under_a_foreign_lease(self) -> None:
        self.enqueue("First")
        self.assertTrue(self.dequeue(READER_A))

        ingested = ingest_message(self.scope, "From a writer", cwd=str(self.root))
        second = self.enqueue("Second")

        self.assertTrue(ingested.created)
        self.assertEqual([task["task_id"] for task in next_tasks(self.scope)], [second])
        self.assertEqual(
            [message["message_id"] for message in list_inbox(self.scope)],
            [ingested.message_id],
        )
        status = read_status(self.scope, reader_id=READER_B, now_us=self.now)
        self.assertEqual(status["tasks"]["ready"], 1)
        self.assertEqual(status["reader"]["reader_id"], READER_A)

    # 3. Implicit acquisition and sliding renewal.

    def test_a_successful_consume_acquires_and_a_second_renews(self) -> None:
        self.enqueue("First")
        self.enqueue("Second")

        first = self.dequeue(READER_A)
        self.assertTrue(first[0]["reader_acquired"])
        acquired = self.lease(READER_A)
        self.assertEqual(acquired["status"], "held")
        self.assertTrue(acquired["self"])
        self.assertEqual(acquired["epoch"], 1)

        later = self.now + 10 * SECOND
        second = self.dequeue(READER_A, now_us=later)
        self.assertFalse(second[0]["reader_acquired"])
        renewed = self.lease(READER_A, now_us=later)
        self.assertEqual(renewed["epoch"], 1)
        self.assertGreater(renewed["expires_at"], acquired["expires_at"])
        self.assertEqual(renewed["acquired_at"], acquired["acquired_at"])

    def test_an_empty_consume_never_makes_a_poller_the_reader(self) -> None:
        self.assertIsNone(
            claim_message(
                self.scope,
                owner_id="poller",
                reader_id=READER_A,
                now_us=self.now,
            )
        )
        self.assertEqual(self.dequeue(READER_A), [])

        self.assertEqual(self.lease(READER_A)["status"], "absent")

    # 4. Expiry takeover.

    def test_an_expired_lease_is_taken_over_and_can_be_handed_back(self) -> None:
        self.enqueue("Work")
        self.assertTrue(self.dequeue(READER_A))
        after = self.now + 61 * SECOND

        self.assertEqual(self.lease(READER_A, now_us=after)["status"], "expired")
        taken = acquire_reader_lease(
            self.scope,
            owner_id="worker",
            reader_id=READER_B,
            lease_seconds=60,
            now_us=after,
        )
        self.assertTrue(taken["acquired"])
        self.assertEqual(taken["reader"]["epoch"], 2)

        with self.assertRaises(JournalError) as raised:
            self.dequeue(READER_A, now_us=after)
        self.assertEqual(raised.exception.code, "reader_held")

        release_reader_lease(self.scope, reader_id=READER_B, now_us=after)
        regained = acquire_reader_lease(
            self.scope,
            owner_id="worker",
            reader_id=READER_A,
            lease_seconds=60,
            now_us=after,
        )
        self.assertTrue(regained["acquired"])
        self.assertEqual(regained["reader"]["epoch"], 3)

    # 5. Explicit release.

    def test_release_frees_the_role_replays_and_refuses_a_live_holder(self) -> None:
        acquire_reader_lease(
            self.scope,
            owner_id="worker",
            reader_id=READER_A,
            lease_seconds=60,
            now_us=self.now,
        )

        with self.assertRaises(JournalError) as raised:
            release_reader_lease(
                self.scope,
                reader_id=READER_B,
                now_us=self.now,
            )
        self.assertEqual(raised.exception.code, "reader_held")

        released = release_reader_lease(
            self.scope,
            reader_id=READER_A,
            now_us=self.now,
        )
        self.assertFalse(released["replayed"])
        self.assertEqual(released["reader"]["status"], "released")

        replayed = release_reader_lease(
            self.scope,
            reader_id=READER_A,
            now_us=self.now,
        )
        self.assertTrue(replayed["replayed"])
        # Nothing held at all replays just as successfully.
        self.assertTrue(
            release_reader_lease(
                self.scope,
                reader_id=READER_B,
                now_us=self.now,
            )["replayed"]
        )

        taken = acquire_reader_lease(
            self.scope,
            owner_id="worker",
            reader_id=READER_B,
            lease_seconds=60,
            now_us=self.now,
        )
        self.assertTrue(taken["acquired"])
        self.assertEqual(taken["reader"]["epoch"], 2)

    # 6. Single-reader governs dispatch, not settlement.

    def test_settling_owned_work_survives_losing_the_reader_role(self) -> None:
        task_id = self.enqueue("Owned work")
        self.enqueue("Untouched work")
        claimed = self.dequeue(READER_A, lease_seconds=900)
        self.assertEqual(claimed[0]["task"]["task_id"], task_id)
        message = ingest_message(self.scope, "Owned message", cwd=str(self.root))
        message_claim = claim_message(
            self.scope,
            owner_id="worker",
            message_id=message.message_id,
            reader_id=READER_A,
            reader_lease_seconds=60,
            now_us=self.now,
        )
        assert message_claim is not None
        parked = ingest_message(self.scope, "Parked message", cwd=str(self.root))
        parked_claim = claim_message(
            self.scope,
            owner_id="worker",
            message_id=parked.message_id,
            reader_id=READER_A,
            reader_lease_seconds=60,
            now_us=self.now,
        )
        assert parked_claim is not None

        # The reader lease expires long before the item leases do.
        after = self.now + 61 * SECOND
        acquire_reader_lease(
            self.scope,
            owner_id="other",
            reader_id=READER_B,
            lease_seconds=3600,
            now_us=after,
        )

        applied = apply_effects(
            self.scope,
            message.message_id,
            {
                "v": 1,
                "expect": {},
                "effects": [["create", "$note", {"title": "Recorded anyway"}]],
            },
            claim_id=message_claim["claim_id"],
            reader_id=READER_A,
        )
        self.assertEqual(applied["status"], "applied")
        failed = dispose_message(
            self.scope,
            parked.message_id,
            claim_id=parked_claim["claim_id"],
            disposition="failed",
            reason="cannot proceed",
            reader_id=READER_A,
            now_us=after,
        )
        self.assertEqual(failed["status"], "failed")
        settled = settle_tasks_done(
            self.scope,
            task_ids=[task_id],
            summary="Finished before losing the role",
            owner_id="worker",
            reader_id=READER_A,
            cwd=str(self.root),
            now_us=after,
        )
        self.assertEqual(settled["status"], "done")
        # A held claim is released voluntarily, never revoked by the
        # reader role changing hands.
        spare = ingest_message(self.scope, "Spare", cwd=str(self.root))
        spare_claim = claim_message(
            self.scope,
            owner_id="worker",
            message_id=spare.message_id,
            reader_id=READER_B,
            now_us=after,
        )
        assert spare_claim is not None
        self.assertEqual(
            release_claim(
                self.scope,
                spare_claim["claim_id"],
                now_us=after,
            )["status"],
            "released",
        )
        self.assertEqual(check_journal(self.scope)["status"], "ok")

    def test_settling_a_merely_ready_task_needs_the_reader_role(self) -> None:
        task_id = self.enqueue("Never dispatched")
        acquire_reader_lease(
            self.scope,
            owner_id="other",
            reader_id=READER_B,
            lease_seconds=3600,
            now_us=self.now,
        )

        with self.assertRaises(JournalError) as raised:
            settle_tasks_done(
                self.scope,
                task_ids=[task_id],
                summary="Settling work nobody dispatched",
                owner_id="worker",
                reader_id=READER_A,
                cwd=str(self.root),
                now_us=self.now,
            )
        self.assertEqual(raised.exception.code, "reader_held")
        # Refused before any write: the summary message never landed.
        self.assertEqual(list_inbox(self.scope), [])

    def test_task_done_renews_but_never_takes_the_role(self) -> None:
        task_id = self.enqueue("Owned work")
        self.dequeue(READER_A, lease_seconds=900)
        release_reader_lease(self.scope, reader_id=READER_A, now_us=self.now)

        settle_tasks_done(
            self.scope,
            task_ids=[task_id],
            summary="Settled with no role held",
            owner_id="worker",
            reader_id=READER_A,
            cwd=str(self.root),
            now_us=self.now,
        )

        self.assertEqual(self.lease(READER_A)["status"], "released")

    # 7. Acquisition race.

    def test_acquisition_race_has_exactly_one_winner(self) -> None:
        initialize_journal(self.scope)
        barrier = threading.Barrier(8)

        def compete(index: int):
            barrier.wait()
            try:
                return acquire_reader_lease(
                    self.scope,
                    owner_id=f"owner-{index}",
                    reader_id=f"reader-{index}",
                    lease_seconds=3600,
                )
            except JournalError:
                return None

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(compete, range(8)))

        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertTrue(winners[0]["acquired"])
        self.assertEqual(winners[0]["reader"]["epoch"], 1)

    # 8. Migration compatibility and export content.

    def test_a_held_lease_leaves_check_and_export_unchanged(self) -> None:
        message = ingest_message(self.scope, "Exported", cwd=str(self.root))
        claim = claim_message(
            self.scope,
            owner_id="worker",
            message_id=message.message_id,
            reader_id=READER_A,
            now_us=self.now,
        )
        assert claim is not None
        without_lease = self.root / "baseline.jsonl"
        with_lease = self.root / "held.jsonl"

        first = export_journal(self.scope, without_lease)
        release_reader_lease(self.scope, reader_id=READER_A, now_us=self.now)
        acquire_reader_lease(
            self.scope,
            owner_id="worker",
            reader_id=READER_B,
            lease_seconds=3600,
            now_us=self.now,
        )
        second = export_journal(self.scope, with_lease)

        # An ephemeral coordination row is not semantic export content,
        # so churning it changes neither record types nor counts.
        self.assertEqual(first["records"], second["records"])
        self.assertEqual(
            without_lease.read_bytes(),
            with_lease.read_bytes(),
        )
        self.assertNotIn("reader_lease", with_lease.read_text(encoding="utf-8"))
        self.assertEqual(check_journal(self.scope)["status"], "ok")

    # 10. The datum TASK-44 will read.

    def test_status_reports_the_reader_datum_for_the_gate(self) -> None:
        fresh = read_status(self.scope, reader_id=READER_A, now_us=self.now)
        self.assertEqual(fresh["reader"]["status"], "absent")
        self.assertFalse(fresh["reader"]["held"])
        self.assertFalse(fresh["reader"]["self"])
        # No lease names no live reader, so the gate stays accountable.
        self.assertFalse(fresh["reader"]["live"])

        self.enqueue("Work")
        self.dequeue(READER_A)

        held = read_status(self.scope, reader_id=READER_A, now_us=self.now)
        self.assertEqual(held["reader"]["status"], "held")
        self.assertTrue(held["reader"]["held"])
        self.assertTrue(held["reader"]["self"])
        self.assertEqual(held["reader"]["reader_id"], READER_A)
        # An explicitly configured identity records no locator, so no
        # session can be shown to be draining this queue: held, but not
        # the live foreign reader that would relieve a gate.
        self.assertFalse(held["reader"]["live"])

        foreign = read_status(self.scope, reader_id=READER_B, now_us=self.now)
        self.assertTrue(foreign["reader"]["held"])
        self.assertFalse(foreign["reader"]["self"])
        self.assertFalse(foreign["reader"]["live"])

        # An expired lease names nobody currently draining the queue.
        expired = read_status(
            self.scope,
            reader_id=READER_B,
            now_us=self.now + 3600 * SECOND,
        )
        self.assertEqual(expired["reader"]["status"], "expired")
        self.assertFalse(expired["reader"]["live"])

        # Without an identity to compare against there is no self.
        anonymous = read_status(self.scope, now_us=self.now)
        self.assertIsNone(anonymous["reader"]["self"])
        self.assertEqual(
            set(anonymous["reader"]),
            {
                "status",
                "held",
                "self",
                "owner_id",
                "reader_id",
                "expires_at",
                "live",
                "released_by_self",
            },
        )
        self.assertFalse(anonymous["reader"]["released_by_self"])

    def test_status_reports_a_dead_holders_lease_as_not_live(self) -> None:
        reader_id = support.hold_reader_lease_from_dead_session(self.scope)

        status = read_status(self.scope, reader_id=READER_A)

        # The lease is unexpired, but the session that took it is gone,
        # so nothing is draining this queue and the next consumer may
        # take over: stale, not held.
        self.assertEqual(status["reader"]["status"], "stale")
        self.assertFalse(status["reader"]["held"])
        self.assertFalse(status["reader"]["self"])
        self.assertEqual(status["reader"]["reader_id"], reader_id)
        self.assertFalse(status["reader"]["live"])

    def test_status_reports_a_live_foreign_sessions_lease_as_live(
        self,
    ) -> None:
        with support.reader_lease_held_by_live_session(
            "agent-root",
            self.root,
            agent_root=self.agent_root,
        ) as reader_id:
            status = read_status(self.scope, reader_id=READER_A)

        # A running session on this host recorded its own locator, which
        # is the one reading that proves someone else is accountable.
        self.assertNotEqual(reader_id, READER_A)
        self.assertEqual(status["reader"]["status"], "held")
        self.assertTrue(status["reader"]["held"])
        self.assertFalse(status["reader"]["self"])
        self.assertEqual(status["reader"]["reader_id"], reader_id)
        self.assertTrue(status["reader"]["live"])

    def test_status_never_reports_this_sessions_own_lease_as_live(
        self,
    ) -> None:
        # The lease this very session took, under an identity the reader
        # asking is not configured with -- the shape a hook sees when it
        # does not inherit the agent shell's AIQ_READER.
        reader_id = support.hold_reader_lease_with_locator(
            self.scope,
            host=socket.gethostname(),
            session=os.getsid(0),
        )

        status = read_status(self.scope, reader_id=READER_A)

        # The identities differ, but the locator names this very
        # session: nobody else is draining the queue, so the caller is
        # still accountable for the work.
        self.assertEqual(status["reader"]["status"], "held")
        self.assertFalse(status["reader"]["self"])
        self.assertEqual(status["reader"]["reader_id"], reader_id)
        self.assertFalse(status["reader"]["live"])

    def test_status_never_reports_a_holder_on_another_host_as_live(
        self,
    ) -> None:
        # A live session id, but on a host whose processes this one
        # cannot probe at all.
        support.hold_reader_lease_with_locator(
            self.scope,
            host="other-host",
            session=os.getpid(),
        )

        status = read_status(self.scope, reader_id=READER_A)

        # A foreign host's session id means nothing here, so liveness is
        # unprovable and the caller stays accountable.
        self.assertEqual(status["reader"]["status"], "held")
        self.assertFalse(status["reader"]["self"])
        self.assertFalse(status["reader"]["live"])

    # 11. The datum a bounded run's deliberate stop is read from.

    def test_status_reports_this_sessions_own_release_as_such(self) -> None:
        # The shape a bounded run leaves behind: this session took the
        # role by consuming, then gave it up on purpose. The identity the
        # caller asks under differs, exactly as a hook's does when it
        # does not inherit the agent shell's AIQ_READER.
        reader_id = support.release_reader_lease_from_this_session(self.scope)

        status = read_status(self.scope, reader_id=READER_A)

        self.assertEqual(status["reader"]["status"], "released")
        self.assertFalse(status["reader"]["held"])
        self.assertFalse(status["reader"]["self"])
        self.assertEqual(status["reader"]["reader_id"], reader_id)
        # A release is nobody draining the queue, so it is never live...
        self.assertFalse(status["reader"]["live"])
        # ...but the locator proves this session is the one that said so.
        self.assertTrue(status["reader"]["released_by_self"])

    def test_status_never_reports_a_foreign_release_as_this_sessions(
        self,
    ) -> None:
        def released_on_another_host(scope) -> None:
            support.release_reader_lease_with_locator(
                scope,
                host="other-host",
                session=os.getsid(0),
            )

        def released_by_another_session(scope) -> None:
            support.release_reader_lease_with_locator(
                scope,
                host=socket.gethostname(),
                session=support.dead_session_id(),
            )

        def released_without_a_locator(scope) -> None:
            # An explicitly configured identity records no locator, so
            # nothing ties the release to any particular session.
            acquire_reader_lease(
                scope,
                owner_id="worker",
                reader_id=READER_B,
                lease_seconds=60,
            )
            release_reader_lease(scope, reader_id=READER_B)

        for index, (name, prepare) in enumerate(
            (
                ("on another host", released_on_another_host),
                ("by another session", released_by_another_session),
                ("without a locator", released_without_a_locator),
            )
        ):
            with self.subTest(release=name):
                agent_root = self.root / f"foreign-release-{index}"
                agent_root.mkdir()
                scope = resolve_scope(
                    "agent-root",
                    cwd=self.root,
                    agent_root=agent_root,
                )
                prepare(scope)

                status = read_status(scope, reader_id=READER_A)

                self.assertEqual(status["reader"]["status"], "released")
                self.assertFalse(status["reader"]["live"])
                # Somebody else's release is not this session declaring
                # anything, so the gate it feeds keeps blocking.
                self.assertFalse(status["reader"]["released_by_self"])

    def test_a_release_stops_standing_a_gate_down_once_it_lapses(
        self,
    ) -> None:
        """A declaration about a lease cannot outlive the lease.

        The row survives release on purpose -- that is what keeps
        `epoch` monotonic and the last holder nameable -- so without a
        bound it would go on reading as a standing "I am done" forever.
        POSIX session ids restart low after a reboot, so an unrelated
        later session can match a kept locator by accident and start
        life with its completion gate already switched off. The lease's
        own expiry is the bound.
        """
        reader_id = support.release_reader_lease_from_this_session(self.scope)
        now = _now_us()
        lapsed = now + 3601 * SECOND

        standing = read_status(self.scope, reader_id=reader_id, now_us=now)
        self.assertEqual(standing["reader"]["status"], "released")
        self.assertTrue(standing["reader"]["released_by_self"])

        lapsed_status = read_status(
            self.scope,
            reader_id=reader_id,
            now_us=lapsed,
        )

        # The same row, the same locator, and the same session asking --
        # and it declares nothing now, because the lease it was about is
        # long gone.
        self.assertEqual(lapsed_status["reader"]["status"], "expired")
        self.assertFalse(lapsed_status["reader"]["released_by_self"])

    def test_a_lapsed_release_no_longer_replays_as_still_standing(
        self,
    ) -> None:
        reader_id = support.release_reader_lease_from_this_session(self.scope)

        replayed = release_reader_lease(
            self.scope,
            reader_id=reader_id,
            now_us=_now_us() + 3601 * SECOND,
        )

        # `already_released` claims the earlier declaration still
        # stands. Once the lease has lapsed it does not, so the honest
        # answer is that there is nothing of this caller's here.
        self.assertEqual(replayed["status"], "not_held")
        self.assertTrue(replayed["replayed"])
        self.assertFalse(replayed["released"])

    # 12. Releasing takes proof of holding, and the one explicit
    # override that may break that rule.

    def test_naming_a_located_holder_is_refused_and_force_is_not(
        self,
    ) -> None:
        """Proof of holding, and the deliberate operator exception.

        The holder here is on another host, so it can be neither
        impersonated nor proved dead from this process -- the shape a
        release must refuse rather than perform.
        """
        reader_id = support.hold_reader_lease_with_locator(
            self.scope,
            host="other-host",
            session=os.getsid(0),
        )

        # Naming the holder exactly is still not being the holder.
        with self.assertRaises(JournalError) as raised:
            release_reader_lease(self.scope, reader_id=reader_id)
        self.assertEqual(raised.exception.code, "reader_held")
        self.assertEqual(
            read_reader_lease(self.scope, reader_id=reader_id)["status"],
            "held",
        )

        forced = release_reader_lease(
            self.scope,
            reader_id=reader_id,
            force=True,
        )

        self.assertEqual(forced["status"], "forced")
        self.assertFalse(forced["released"])
        self.assertFalse(forced["replayed"])
        # The break clears the holder locator, so the broken lease
        # declares nothing for the session it was taken from. Without
        # that, the forbidden revocation would merely have moved behind
        # a flag.
        self.assertFalse(
            read_status(self.scope, reader_id=reader_id)["reader"][
                "released_by_self"
            ]
        )
        # And the role really is free for the next session.
        taken = acquire_reader_lease(
            self.scope,
            owner_id="worker",
            reader_id=READER_B,
            lease_seconds=60,
        )
        self.assertTrue(taken["acquired"])

    def test_force_is_unnecessary_for_a_lease_this_caller_holds(
        self,
    ) -> None:
        """Proof beats the flag: a rightful release stays a declaration.

        Passing `--force` where it is not needed must not downgrade an
        honest release into a break, or a bounded run that habitually
        passed it would stop being able to stand its own gate down.
        """
        reader_id = support.hold_reader_lease_with_locator(
            self.scope,
            host=socket.gethostname(),
            session=os.getsid(0),
        )

        released = release_reader_lease(
            self.scope,
            reader_id=reader_id,
            force=True,
        )

        self.assertEqual(released["status"], "released")
        self.assertTrue(released["released"])
        self.assertTrue(
            read_status(self.scope, reader_id=reader_id)["reader"][
                "released_by_self"
            ]
        )

    # 13. Whose claims are these? Releasing the role settles nothing, so
    # the gate needs a claim count it can hold *this* session to.

    def test_status_counts_this_sessions_own_claims_separately(self) -> None:
        self.enqueue("Mine")
        self.dequeue(READER_A)

        status = read_status(self.scope, reader_id=READER_A, now_us=self.now)

        # This process took the claim, so it is both scope-wide active
        # and attributable to this session.
        self.assertEqual(
            status["claims"],
            {"active": 1, "active_this_session": 1},
        )

    def test_status_never_counts_a_foreign_sessions_claim_as_this_ones(
        self,
    ) -> None:
        self.enqueue("Somebody else's")
        claimed = support.claim_next_task_from_another_session(self.scope)
        self.assertEqual(len(claimed), 1)

        status = read_status(self.scope, reader_id=READER_A, now_us=self.now)

        # Scope-wide the claim is live, which is exactly why `active`
        # alone cannot gate a session: another session on this host is
        # working that item, and this one can neither settle nor release
        # it honestly.
        self.assertEqual(status["claims"]["active"], 1)
        self.assertEqual(status["claims"]["active_this_session"], 0)

    def test_status_never_counts_a_locatorless_claim_as_this_sessions(
        self,
    ) -> None:
        self.enqueue("Written before schema 5")
        # The shape of every claim stored before schema 5 added the
        # locator columns, and of any claim taken where no POSIX session
        # can be derived: the columns are simply NULL.
        claimed = support.claim_next_task_with_locator(
            self.scope,
            locator=None,
        )
        self.assertEqual(len(claimed), 1)

        status = read_status(self.scope, reader_id=READER_A, now_us=self.now)

        # Unattributed is not this session's. The gate's usual bias --
        # missing proof means block -- inverts here, because absent
        # evidence "is this claim mine?" must answer no just as "is
        # somebody else covering for me?" does. Blocking on a claim this
        # session cannot settle would be unclearable; under-counting a
        # pre-migration claim expires on its own.
        self.assertEqual(status["claims"]["active"], 1)
        self.assertEqual(status["claims"]["active_this_session"], 0)

    def test_status_stops_counting_a_settled_claim_as_this_sessions(
        self,
    ) -> None:
        task_id = self.enqueue("Settle me")
        self.dequeue(READER_A)

        settle_tasks_done(
            self.scope,
            task_ids=[task_id],
            summary="done",
            owner_id="worker",
            cwd=str(self.root),
            reader_id=READER_A,
            now_us=self.now,
        )
        status = read_status(self.scope, reader_id=READER_A, now_us=self.now)

        # Settling releases the claim, which is the remedy the gate names.
        self.assertEqual(status["claims"]["active_this_session"], 0)

    def test_a_dead_separate_sessions_claim_is_never_this_sessions(
        self,
    ) -> None:
        """The per-invocation-session host, reproduced faithfully.

        A host may give every shell invocation its own POSIX session, so
        the process that claimed is already gone when anything later asks
        about it. ``subprocess.run`` cannot reproduce that -- a child
        inherits its parent's session -- so this spawns a real session
        leader and reaps it.

        The point of the assertions is the *coupling*: on such a host
        this session can prove neither that a claim is its own nor that a
        release was its own, because both read the same locator. So the
        gate's release stand-down is unreachable there, and the claim
        check that refines it cannot be the thing that lets a stranded
        claim through -- the gate blocks on the counts either way.
        """

        self.enqueue("Taken by a session that then exits")
        reader_id, session, claimed = support.dequeue_from_a_separate_session(
            "agent-root",
            self.root,
            agent_root=self.agent_root,
        )

        status = read_status(self.scope, reader_id=READER_A)

        self.assertEqual(claimed, 1)
        self.assertNotEqual(session, os.getsid(0))
        self.assertNotEqual(reader_id, _default_reader())
        # Scope-wide the claim is live and blocks the gate on the counts.
        self.assertEqual(status["claims"]["active"], 1)
        # It is not this session's, and cannot become so: the session
        # that took it no longer exists.
        self.assertEqual(status["claims"]["active_this_session"], 0)
        # The same evidence, read for the lease that same dead session
        # took: not live, and no release of it could ever read as this
        # session's. The two predicates stand or fall together.
        self.assertEqual(status["reader"]["status"], "stale")
        self.assertFalse(status["reader"]["live"])
        self.assertFalse(status["reader"]["released_by_self"])

    def test_a_later_session_cannot_release_a_dead_sessions_lease(
        self,
    ) -> None:
        """Why the stand-down is unreachable under per-call sessions.

        Release is keyed on the reader identity, which defaults to this
        host plus this POSIX session. A later invocation on such a host
        derives a different identity, so it matches no lease, records no
        release, and replays vacuously -- and the claim it did not settle
        stays behind. This is a limitation of the reader identity itself,
        not of the per-session claim count, which reports the same zero
        for the same reason.
        """

        self.enqueue("Claimed and abandoned")
        support.dequeue_from_a_separate_session(
            "agent-root",
            self.root,
            agent_root=self.agent_root,
        )

        released = release_reader_lease(self.scope, reader_id=_default_reader())

        # Nothing was released: no lease names this identity.
        self.assertTrue(released["replayed"])
        self.assertEqual(released["claims_held"], 0)
        self.assertEqual(
            read_reader_lease(self.scope)["status"],
            "stale",
        )
        # So the gate never sees a release to stand down for, and keeps
        # blocking on the claim it can still see and name.
        status = read_status(self.scope, reader_id=READER_A)
        self.assertFalse(status["reader"]["released_by_self"])
        self.assertEqual(status["claims"]["active"], 1)

    def test_release_reports_the_callers_own_unsettled_claims(self) -> None:
        self.enqueue("Mine")
        self.enqueue("Somebody else's")
        self.dequeue(READER_A)
        support.claim_next_task_from_another_session(self.scope)

        released = release_reader_lease(
            self.scope,
            reader_id=READER_A,
            now_us=self.now,
        )

        # Release never refuses -- a mid-item handoff is legitimate and
        # the call stays replayable -- but it says plainly that giving
        # the role back settled nothing, counting only this session's own.
        self.assertEqual(released["status"], "released")
        self.assertFalse(released["replayed"])
        self.assertEqual(released["claims_held"], 1)
        self.assertEqual(
            read_reader_lease(
                self.scope,
                reader_id=READER_A,
                now_us=self.now,
            )["status"],
            "released",
        )

    def test_release_reports_no_claims_when_the_caller_settled_first(
        self,
    ) -> None:
        task_id = self.enqueue("Settle me")
        self.dequeue(READER_A)
        settle_tasks_done(
            self.scope,
            task_ids=[task_id],
            summary="done",
            owner_id="worker",
            cwd=str(self.root),
            reader_id=READER_A,
            now_us=self.now,
        )

        released = release_reader_lease(
            self.scope,
            reader_id=READER_A,
            now_us=self.now,
        )

        self.assertEqual(released["claims_held"], 0)


class ReaderLeaseScopeTest(unittest.TestCase):
    """The role is per scope, and a linked worktree shares its primary."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.environment = patch.dict(
            os.environ,
            {"XDG_STATE_HOME": str(self.root / "state")},
        )
        self.environment.start()
        self.repository = support.init_repository(self.root / "repository")

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary_directory.cleanup()

    def test_one_scope_lease_never_reaches_another_scope(self) -> None:
        repo_scope = resolve_scope("repo", cwd=self.repository)
        user_scope = resolve_scope("user", cwd=self.repository)
        for scope in (repo_scope, user_scope):
            enqueue_task(
                scope,
                title="Scoped work",
                owner_id="writer",
                cwd=str(self.repository),
            )
        acquire_reader_lease(
            repo_scope,
            owner_id="worker",
            reader_id=READER_A,
            lease_seconds=3600,
        )

        self.assertTrue(
            claim_next_tasks(
                user_scope,
                owner_id="worker",
                reader_id=READER_B,
            )
        )
        with self.assertRaises(JournalError) as raised:
            claim_next_tasks(
                repo_scope,
                owner_id="worker",
                reader_id=READER_B,
            )
        self.assertEqual(raised.exception.code, "reader_held")

    def test_a_linked_worktree_shares_the_primary_repository_lease(self) -> None:
        support.run_git(self.repository, "config", "user.name", "AIQ Test")
        support.run_git(
            self.repository,
            "config",
            "user.email",
            "aiq@example.invalid",
        )
        support.run_git(self.repository, "commit", "--allow-empty", "-m", "root")
        linked = self.root / "linked"
        support.run_git(
            self.repository,
            "worktree",
            "add",
            "--detach",
            str(linked),
        )
        repo_scope = resolve_scope("repo", cwd=self.repository)
        linked_scope = resolve_scope("repo", cwd=linked)
        self.assertEqual(repo_scope.journal_path, linked_scope.journal_path)
        enqueue_task(
            repo_scope,
            title="Shared work",
            owner_id="writer",
            cwd=str(self.repository),
        )
        acquire_reader_lease(
            repo_scope,
            owner_id="worker",
            reader_id=READER_A,
            lease_seconds=3600,
        )

        with self.assertRaises(JournalError) as raised:
            claim_next_tasks(
                linked_scope,
                owner_id="worker",
                reader_id=READER_B,
            )
        self.assertEqual(raised.exception.code, "reader_held")


class ReaderLeaseCliTest(unittest.TestCase):
    """The reader_held envelope and the `aiq reader` command surface."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "home").mkdir()
        self.repository = support.init_repository(self.root / "repository")
        self.scope = ("--scope", "repo", "--cwd", str(self.repository))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_aiq(self, *arguments: str, reader: str) -> support.CliResult:
        return support.run_cli(
            *arguments,
            in_process=False,
            cwd=self.repository,
            environment=support.scrubbed_environment(
                AIQ_READER=reader,
                HOME=str(self.root / "home"),
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONPATH=str(support.SOURCE_ROOT),
                XDG_CONFIG_HOME=str(self.root / "config"),
                XDG_STATE_HOME=str(self.root / "state"),
            ),
        )

    def ok(self, *arguments: str, reader: str) -> dict:
        completed = self.run_aiq(*arguments, "--json", reader=reader)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def assert_reader_held(self, *arguments: str, reader: str) -> None:
        completed = self.run_aiq(*arguments, "--json", reader=reader)
        self.assertEqual(completed.returncode, 4, completed.stdout)
        self.assertEqual(completed.stdout, "")
        payload = json.loads(completed.stderr)
        self.assertEqual(payload["code"], "reader_held")
        self.assertEqual(payload["status"], "error")
        self.assertIn(f'reader "{READER_A}"', payload["error"])

    def test_a_second_session_is_refused_while_writers_stay_open(self) -> None:
        self.ok("journal", "init", *self.scope, reader=READER_A)
        self.ok("enqueue", "Contested", *self.scope, reader=READER_A)
        acquired = self.ok("reader", "acquire", *self.scope, reader=READER_A)
        self.assertTrue(acquired["acquired"])

        self.assert_reader_held("dequeue", *self.scope, reader=READER_B)
        self.assert_reader_held("queue", "next", *self.scope, reader=READER_B)
        self.assert_reader_held("inbox", "claim", *self.scope, reader=READER_B)

        # The same session's writers and readers keep working.
        self.ok("ingest", "--message", "Still open", *self.scope, reader=READER_B)
        self.ok("enqueue", "Also open", *self.scope, reader=READER_B)
        self.ok("queue", "peek", *self.scope, reader=READER_B)
        self.ok("status", *self.scope, reader=READER_B)
        self.ok("inbox", "list", *self.scope, reader=READER_B)

        # And the refusal survives draining the queue completely.
        self.ok("dequeue", "--limit", "2", *self.scope, reader=READER_A)
        drained = self.ok("queue", "peek", *self.scope, reader=READER_A)
        self.assertEqual(drained["tasks"], [])
        self.assert_reader_held("dequeue", *self.scope, reader=READER_B)

    def test_reader_status_names_the_holder_in_both_forms(self) -> None:
        self.ok("journal", "init", *self.scope, reader=READER_A)
        self.ok("reader", "acquire", *self.scope, reader=READER_A)

        payload = self.ok("reader", "status", *self.scope, reader=READER_B)
        self.assertEqual(payload["reader"]["reader_id"], READER_A)
        self.assertEqual(payload["reader"]["status"], "held")
        self.assertFalse(payload["reader"]["self"])

        human = self.run_aiq("reader", "status", *self.scope, reader=READER_B)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertEqual(human.stdout.count("\n"), 1)
        self.assertIn(READER_A, human.stdout)

    def test_release_hands_the_role_to_the_next_session(self) -> None:
        self.ok("journal", "init", *self.scope, reader=READER_A)
        self.ok("enqueue", "Handed over", *self.scope, reader=READER_A)
        self.ok("reader", "acquire", *self.scope, reader=READER_A)
        self.assert_reader_held("dequeue", *self.scope, reader=READER_B)

        self.ok("reader", "release", *self.scope, reader=READER_A)

        dequeued = self.ok("dequeue", *self.scope, reader=READER_B)
        self.assertTrue(dequeued["reader_acquired"])
        self.assertEqual(len(dequeued["items"]), 1)


if __name__ == "__main__":
    unittest.main()
