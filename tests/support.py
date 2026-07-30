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
import subprocess
import sys
import tempfile
from typing import NamedTuple

from aiq.cli import main as _cli_main


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


def git_executable() -> Path:
    """Absolute path of the Git executable; fails the test when missing."""
    discovered = shutil.which("git")
    if discovered is None:
        raise AssertionError("test requires Git")
    return Path(discovered).absolute()


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
