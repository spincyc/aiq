"""Shared fixtures for the test suite.

Consolidates the environment scrub, Git isolation, repository and
launcher builders, and the dual in-process/subprocess CLI harness that
individual test modules previously duplicated.
"""

from __future__ import annotations

import atexit
import contextlib
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import NamedTuple

from aiq.cli import main as _cli_main
from aiq.journal import initialize_journal, resolve_scope


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"

# Masks the user's real Git configuration: no test may ever read or
# write ~/.gitconfig or the system gitconfig.
GIT_ISOLATION = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

# Isolate the test process itself. In-process fixtures reach Git through
# the aiq library, which strips GIT_* variables before spawning git, so
# only a scratch HOME keeps those spawns away from the user's real
# configuration; GIT_ISOLATION covers direct spawns.
os.environ.update(GIT_ISOLATION)
_scratch_home = tempfile.TemporaryDirectory(prefix="aiq-tests-home-")
atexit.register(_scratch_home.cleanup)
os.environ["HOME"] = _scratch_home.name
os.environ["XDG_CONFIG_HOME"] = str(Path(_scratch_home.name) / "config")


def scrubbed_environment(
    *,
    drop: frozenset[str] | set[str] = frozenset(),
    **overrides: str,
) -> dict[str, str]:
    """Copy os.environ without host AIQ or Git state, then override.

    Drops every AIQ_* and GIT_* variable plus the names in drop, applies
    Git isolation, and finally applies overrides keyed by variable name.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AIQ_", "GIT_")) and key not in drop
    }
    environment.update(GIT_ISOLATION)
    environment.update(overrides)
    return environment


def run_git(cwd: Path, *arguments: str) -> None:
    """Run Git in cwd with the user's real configuration masked."""
    subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        check=True,
        env={**os.environ, **GIT_ISOLATION},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def init_repository(path: Path, *, branch: str = "main") -> Path:
    """Create path if needed and initialize an isolated Git repository."""
    path.mkdir(parents=True, exist_ok=True)
    run_git(path, "init", "--quiet", f"--initial-branch={branch}")
    return path


def initialize_repo_journal(repository: Path) -> Path:
    """Opt a repository in to hook capture: initialize its repo journal.

    Installed hooks never create journal storage, so receive tests that
    expect capture must perform the per-repository opt-in first.
    """
    return initialize_journal(resolve_scope("repo", cwd=repository))


def git_executable() -> Path:
    """Absolute path of the Git executable; fails the test when missing."""
    discovered = shutil.which("git")
    if discovered is None:
        raise AssertionError("test requires Git")
    return Path(discovered).absolute()


def dead_session_id() -> int:
    """A POSIX session id whose session has certainly exited.

    The child is made its own session leader, so its pid is its session
    id; waiting for it reaps the process and leaves that id unused.
    """
    process = subprocess.Popen(
        [sys.executable, "-c", ""],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    process.wait()
    return process.pid


def hold_reader_lease_with_locator(
    scope,
    *,
    host: str,
    session: int,
    owner_id: str = "located-worker",
    lease_seconds: int = 3600,
) -> str:
    """Record an unexpired reader lease naming one holder locator.

    Only a self-derived identity records a locator at all, so both the
    identity and the locator are patched for the acquisition; every
    later probe runs unpatched against the recorded pair. This is how a
    test names a specific holder -- a reaped session, another host, or
    the test's own session -- without owning such a process.
    """
    from unittest.mock import patch

    from aiq.queue import acquire_reader_lease

    reader_id = f"{host}-{session}"
    with (
        patch("aiq.queue._default_reader", return_value=reader_id),
        patch("aiq.queue._reader_locator", return_value=(host, session)),
    ):
        acquire_reader_lease(
            scope,
            owner_id=owner_id,
            reader_id=reader_id,
            lease_seconds=lease_seconds,
        )
    return reader_id


def hold_reader_lease_from_dead_session(
    scope,
    *,
    owner_id: str = "dead-worker",
    lease_seconds: int = 3600,
) -> str:
    """Record an unexpired reader lease whose holder session is gone.

    This is the shape an agent harness leaves behind routinely: each
    shell invocation can be its own POSIX session, so a lease outlives
    the session that took it. The unpatched probe afterwards compares
    this host against a reaped session id and proves the holder dead.
    """
    return hold_reader_lease_with_locator(
        scope,
        host=socket.gethostname(),
        session=dead_session_id(),
        owner_id=owner_id,
        lease_seconds=lease_seconds,
    )


# Takes the reader lease as its own POSIX session leader, then parks on
# stdin so that session stays alive for the whole test. The identity is
# the one this child derives for itself, which is what makes the
# recorded holder locator real: nothing is patched here, so the holder
# is a genuinely foreign live session -- the only reading that stands a
# completion gate down.
_LIVE_READER_PROGRAM = """
import sys
from pathlib import Path

from aiq.config import _default_reader
from aiq.journal import resolve_scope
from aiq.queue import acquire_reader_lease

scope_name, cwd, owner_id, lease_seconds, agent_root = sys.argv[1:6]
reader_id = _default_reader()
acquire_reader_lease(
    resolve_scope(
        scope_name,
        cwd=Path(cwd),
        agent_root=Path(agent_root) if agent_root else None,
    ),
    owner_id=owner_id,
    reader_id=reader_id,
    lease_seconds=int(lease_seconds),
)
sys.stdout.write(reader_id + "\\n")
sys.stdout.flush()
sys.stdin.read()
"""


@contextlib.contextmanager
def reader_lease_held_by_live_session(
    scope_name: str,
    cwd: Path,
    *,
    owner_id: str = "live-worker",
    lease_seconds: int = 3600,
    agent_root: Path | None = None,
    environment: dict[str, str] | None = None,
):
    """Hold the reader lease from another live session for the block.

    Yields that session's reader identity. The holder is a real child
    process in its own POSIX session, so its recorded locator names this
    host and a session id that is alive and is not the test process's
    own -- the only shape that proves foreignness. The session ends on
    exit, which leaves the lease abandoned rather than held.
    """
    import aiq

    environment = dict(os.environ if environment is None else environment)
    package_root = str(Path(aiq.__file__).resolve().parents[1])
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{package_root}{os.pathsep}{existing}" if existing else package_root
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _LIVE_READER_PROGRAM,
            scope_name,
            str(cwd),
            owner_id,
            str(lease_seconds),
            "" if agent_root is None else str(agent_root),
        ],
        start_new_session=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        reader_id = process.stdout.readline().strip()
        if not reader_id:
            process.stdin.close()
            raise AssertionError(
                "live reader session failed to take the lease: "
                f"{process.stderr.read()}"
            )
        yield reader_id
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()


def write_launcher(path: Path, *, mode: int = 0o755) -> Path:
    """Write an executable launcher stub such as a console entry point."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(mode)
    return path


def integration_fixture(
    root: Path,
    **directories: str,
) -> tuple[dict[str, str], Path]:
    """Minimal isolated integration environment and launcher stub.

    Keyword arguments name extra environment variables mapped to
    directory names under root, e.g. CODEX_HOME="codex home".
    """
    launcher = write_launcher(root / "bin" / "aiq tool")
    environment = {
        "HOME": str(root / "home"),
        "XDG_STATE_HOME": str(root / "state"),
        "PATH": str(git_executable().parent),
        **GIT_ISOLATION,
    }
    for variable, directory in directories.items():
        environment[variable] = str(root / directory)
    return environment, launcher


class CliResult(NamedTuple):
    """Uniform result for both CLI harness modes."""

    returncode: int
    stdout: str
    stderr: str


def run_cli(
    *arguments: str,
    in_process: bool = True,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    input_text: str | None = None,
) -> CliResult:
    """Run the aiq CLI, in-process by default.

    In-process runs share the test's working directory, environment, and
    stdin; pass in_process=False with cwd and environment when the test
    needs real process isolation.
    """
    if in_process:
        if cwd is not None or environment is not None or input_text is not None:
            raise ValueError("in-process runs share the test process state")
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                code = _cli_main(list(arguments))
            except SystemExit as error:
                code = int(error.code or 0)
        return CliResult(code, stdout.getvalue(), stderr.getvalue())
    completed = subprocess.run(
        [sys.executable, "-m", "aiq", *arguments],
        cwd=cwd,
        env=environment,
        input=input_text,
        text=True,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return CliResult(completed.returncode, completed.stdout, completed.stderr)
