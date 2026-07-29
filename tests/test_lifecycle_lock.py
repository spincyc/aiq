from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from aiq import journal
from aiq.journal import JournalError, JournalScope, resolve_scope


def _hold_lifecycle_lock(
    scope: JournalScope,
    exclusive: bool,
    attempted: Any,
    acquired: Any,
    release: Any,
) -> None:
    attempted.set()
    with journal.lifecycle_lock(scope, exclusive=exclusive):
        acquired.set()
        if not release.wait(10):
            raise RuntimeError("test did not release lifecycle lock")


class LifecycleLockTest(unittest.TestCase):
    def scope(self, root: Path) -> JournalScope:
        agent_root = root / "agent-root"
        agent_root.mkdir()
        return resolve_scope(
            "agent-root",
            cwd=root,
            agent_root=agent_root,
        )

    def start_lock_process(
        self,
        scope: JournalScope,
        *,
        exclusive: bool,
    ):
        context = multiprocessing.get_context("spawn")
        attempted = context.Event()
        acquired = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_lifecycle_lock,
            args=(scope, exclusive, attempted, acquired, release),
        )
        process.start()
        self.assertTrue(attempted.wait(5), "child did not attempt lifecycle lock")
        return process, acquired, release

    def finish_process(self, process, release) -> None:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
            self.fail("lifecycle-lock child did not exit")
        self.assertEqual(process.exitcode, 0)

    def test_lifecycle_lock_is_private_and_retained_beside_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state_home = root / "state"
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(state_home)},
            ):
                scope = self.scope(root)
                with journal.lifecycle_lock(scope, exclusive=False):
                    lock_path = journal._lifecycle_lock_path(scope)
                    self.assertTrue(lock_path.is_file())

                self.assertEqual(
                    stat.S_IMODE(lock_path.parent.stat().st_mode),
                    0o700,
                )
                self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
                self.assertEqual(
                    lock_path,
                    scope.journal_path.parent / "lifecycle.lock",
                )

    def test_resolved_scope_lock_does_not_follow_later_environment_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first_state = root / "first-state"
            second_state = root / "second-state"
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(first_state)},
            ):
                scope = self.scope(root)
                original_path = journal._lifecycle_lock_path(scope)

            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(second_state)},
            ):
                self.assertEqual(
                    journal._lifecycle_lock_path(scope),
                    original_path,
                )
                with journal.lifecycle_lock(scope, exclusive=False):
                    self.assertTrue(original_path.is_file())

            self.assertFalse(second_state.exists())

    def test_repo_lock_moves_with_repository_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            moved_repository = root / "moved-repository"
            repository.mkdir()
            subprocess.run(
                ["git", "-C", str(repository), "init", "-b", "main"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            original_scope = resolve_scope("repo", cwd=repository)
            with journal.lifecycle_lock(original_scope, exclusive=False):
                original_lock_path = journal._lifecycle_lock_path(
                    original_scope
                )
            repository.rename(moved_repository)

            moved_scope = resolve_scope("repo", cwd=moved_repository)
            moved_lock_path = journal._lifecycle_lock_path(moved_scope)

            self.assertEqual(
                moved_lock_path,
                moved_scope.journal_path.parent / "lifecycle.lock",
            )
            self.assertEqual(
                moved_lock_path,
                (
                    moved_repository
                    / ".git"
                    / "aiq"
                    / "lifecycle.lock"
                ).resolve(),
            )
            self.assertTrue(moved_lock_path.is_file())
            self.assertFalse(original_lock_path.exists())

    def test_shared_locks_can_coexist_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(root / "state")},
            ):
                scope = self.scope(root)
                with journal.lifecycle_lock(scope, exclusive=False):
                    process, acquired, release = self.start_lock_process(
                        scope,
                        exclusive=False,
                    )
                    try:
                        self.assertTrue(
                            acquired.wait(5),
                            "second shared lifecycle lock was blocked",
                        )
                    finally:
                        self.finish_process(process, release)

    def test_connection_holds_shared_lock_until_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(root / "state")},
            ):
                scope = self.scope(root)
                connection = journal._connect(scope)
                process, acquired, release = self.start_lock_process(
                    scope,
                    exclusive=True,
                )
                try:
                    self.assertFalse(
                        acquired.wait(0.25),
                        "exclusive lock bypassed an open journal connection",
                    )
                    connection.close()
                    self.assertTrue(
                        acquired.wait(5),
                        "exclusive lock stayed blocked after connection close",
                    )
                finally:
                    connection.close()
                    self.finish_process(process, release)

    def test_exclusive_lock_blocks_shared_lock_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(root / "state")},
            ):
                scope = self.scope(root)
                with journal.lifecycle_lock(scope, exclusive=True):
                    process, acquired, release = self.start_lock_process(
                        scope,
                        exclusive=False,
                    )
                    self.assertFalse(
                        acquired.wait(0.25),
                        "shared lock bypassed the exclusive lifecycle lock",
                    )
                try:
                    self.assertTrue(
                        acquired.wait(5),
                        "shared lock stayed blocked after exclusive release",
                    )
                finally:
                    self.finish_process(process, release)

    def test_failed_connection_releases_lifecycle_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": str(root / "state")},
            ):
                scope = self.scope(root)
                with patch.object(
                    journal,
                    "_initialize_journal",
                    side_effect=JournalError("injected failure"),
                ):
                    with self.assertRaisesRegex(
                        JournalError,
                        "injected failure",
                    ):
                        journal._connect(scope)

                process, acquired, release = self.start_lock_process(
                    scope,
                    exclusive=True,
                )
                try:
                    self.assertTrue(
                        acquired.wait(5),
                        "failed connection leaked its shared lifecycle lock",
                    )
                finally:
                    self.finish_process(process, release)


if __name__ == "__main__":
    unittest.main()
