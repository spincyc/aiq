"""Read-only local health diagnostics for the AIQ installation."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
from typing import Any, Mapping

from aiq.config import Config, ConfigError, resolve_config
from aiq.integrations import HOOK_INTEGRATIONS
from aiq.integrations._hooks import HookIntegrationError
from aiq.journal import (
    SCHEMA_VERSION,
    SQLITE_MINIMUM_VERSION,
    JournalError,
    JournalScope,
    resolve_scope,
)


PYTHON_MINIMUM_VERSION = (3, 11)
PYTHON_MAXIMUM_EXCLUSIVE = (3, 15)


@dataclass(frozen=True)
class DoctorReport:
    """Ordered check results plus the configuration when it resolved."""

    status: str
    checks: tuple[dict[str, str], ...]
    config: Config | None


def _check(check: str, status: str, detail: str) -> dict[str, str]:
    return {"check": check, "status": status, "detail": detail}


def _python_check() -> dict[str, str]:
    found = ".".join(str(part) for part in sys.version_info[:3])
    if PYTHON_MINIMUM_VERSION <= sys.version_info[:2] < PYTHON_MAXIMUM_EXCLUSIVE:
        return _check("python", "ok", found)
    return _check(
        "python",
        "fail",
        f"Python 3.11-3.14 is required; found {found}",
    )


def _sqlite_check() -> dict[str, str]:
    if sqlite3.sqlite_version_info >= SQLITE_MINIMUM_VERSION:
        return _check("sqlite", "ok", sqlite3.sqlite_version)
    required = ".".join(str(part) for part in SQLITE_MINIMUM_VERSION)
    return _check(
        "sqlite",
        "fail",
        f"SQLite {required} or newer is required; "
        f"found {sqlite3.sqlite_version}",
    )


def _git_check() -> dict[str, str]:
    found = shutil.which("git")
    if found:
        return _check("git", "ok", found)
    return _check(
        "git",
        "warn",
        "git not found on PATH; repo scope is unavailable",
    )


def inspect_journal(scope: JournalScope) -> dict[str, str]:
    """Cheap read-only journal health check.

    Shared by ``aiq doctor`` and ``aiq reconcile``'s report-only default.
    Never opens the journal for writing, migrates storage, or rehashes
    content; deep verification stays explicit in ``aiq journal check``.
    Returns a check record with ``status`` ok/warn/fail/skipped and a
    ``detail`` line.
    """

    path = scope.journal_path
    if not os.path.lexists(path):
        return _check("journal", "skipped", f"journal not initialized: {path}")
    try:
        status = path.lstat()
    except OSError as error:
        return _check("journal", "fail", f"cannot stat journal: {error}")
    if not stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode):
        return _check(
            "journal",
            "fail",
            f"journal is not a regular file: {path}",
        )
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=10,
        )
        try:
            quick = connection.execute("PRAGMA quick_check(1)").fetchone()[0]
            version_row = connection.execute(
                "SELECT value FROM journal_metadata"
                " WHERE key = 'schema_version'"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as error:
        return _check("journal", "fail", f"cannot read journal: {error}")
    if quick != "ok":
        return _check(
            "journal",
            "fail",
            f"SQLite quick check failed: {quick}",
        )
    try:
        version = int(version_row[0]) if version_row else None
    except ValueError:
        version = None
    if version is None or version < 1:
        return _check(
            "journal",
            "fail",
            "journal has no valid schema_version metadata",
        )
    if version > SCHEMA_VERSION:
        return _check(
            "journal",
            "fail",
            f"journal schema {version} is newer than supported "
            f"schema {SCHEMA_VERSION}",
        )
    mode = stat.S_IMODE(status.st_mode)
    if mode != 0o600:
        return _check(
            "journal",
            "warn",
            f"journal permissions are {mode:04o}; expected 0600",
        )
    if version < SCHEMA_VERSION:
        return _check(
            "journal",
            "warn",
            f"journal schema {version} awaits migration by aiq journal check",
        )
    return _check("journal", "ok", f"schema v{version}; quick check ok")


def _capture_check(scope: JournalScope | None) -> dict[str, str]:
    """Whether installed hooks would capture prompts for the scope.

    Installed hooks never create journal storage, so in a repository
    whose journal is not initialized prompt capture is inactive until
    ``aiq journal init --scope repo`` opts the repository in. Other
    scopes auto-initialize on capture.
    """

    if scope is None:
        return _check("capture", "skipped", "scope resolution failed")
    if scope.kind == "repo" and not scope.journal_path.exists():
        return _check(
            "capture",
            "warn",
            "prompt capture is inactive: repo journal not initialized; "
            "run aiq journal init --scope repo to opt in",
        )
    return _check(
        "capture",
        "ok",
        f"hooks capture prompts to {scope.journal_path}",
    )


_INTEGRATION_MODULES = tuple(
    (f"integration.{integration_id}", record.module)
    for integration_id, record in sorted(HOOK_INTEGRATIONS.items())
)


def _integration_check(
    name: str,
    module: Any,
    *,
    invoked_launcher: str | Path | None,
    python_executable: str | Path | None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    integration_id = name.partition(".")[2]
    absent = (
        "not installed: run aiq integration install "
        f"{integration_id} --user"
    )
    try:
        if not module.integration_present(environment=environment):
            return _check(name, "skipped", absent)
        result = module.check_integration(
            invoked_launcher=invoked_launcher,
            python_executable=python_executable,
            environment=environment,
        )
    except HookIntegrationError as error:
        return _check(name, "warn", str(error))
    if result.get("ok"):
        return _check(name, "ok", f"installed at {result['target']}")
    if result.get("status") == "absent":
        return _check(name, "skipped", absent)
    detail = f"status={result.get('status')}"
    if result.get("blocked_reason"):
        detail = f"{detail}: {result['blocked_reason']}"
    return _check(name, "warn", detail)


def _report_check(config: Config | None) -> dict[str, str]:
    """Read-only health check for the ``aiq report`` target.

    Never creates directories or journals; reports ``skipped`` when no
    dev report target is configured, ``warn`` when the configured target
    cannot accept reports, and ``ok`` when a report would reach an
    initialized repo journal.
    """

    configured = config.dev_report_repo if config is not None else None
    if configured is None:
        return _check("report", "skipped", "dev_report_repo not configured")
    target = Path(configured)
    if not target.is_absolute():
        return _check(
            "report",
            "warn",
            f"dev report target is not an absolute path: {target}",
        )
    if not target.is_dir():
        return _check(
            "report",
            "warn",
            f"dev report target does not exist: {target}",
        )
    try:
        scope = resolve_scope("repo", cwd=target)
    except JournalError as error:
        return _check("report", "warn", str(error))
    if not scope.journal_path.is_file():
        return _check(
            "report",
            "warn",
            f"target journal not initialized: run aiq journal init in {target}",
        )
    return _check("report", "ok", f"target {target}")


def run_doctor(
    *,
    requested_scope: str | None,
    cwd: Path,
    agent_root: Path | None = None,
    repo_config: bool = True,
    invoked_launcher: str | Path | None = None,
    python_executable: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> DoctorReport:
    """Run every cheap read-only diagnostic and never mutate local state.

    Stable check order: python, sqlite, config, git, scope, journal,
    capture, journal.deep, one ``integration.<id>`` row per known
    integration (sorted by id), then report.
    """

    checks: list[dict[str, str]] = [_python_check(), _sqlite_check()]

    config: Config | None = None
    cli_scope = None if requested_scope == "agent-root" else requested_scope
    try:
        options: dict[str, object] = {"cwd": cwd, "cli": {"scope": cli_scope}}
        if not repo_config:
            options["repo_path"] = None
        config = resolve_config(**options)
    except (ConfigError, OSError) as error:
        checks.append(_check("config", "fail", str(error)))
    else:
        checks.append(
            _check(
                "config",
                "ok",
                f"scope={config.scope} owner={config.owner} "
                f"output={config.output}",
            )
        )

    checks.append(_git_check())

    if requested_scope == "agent-root":
        effective_scope = "agent-root"
    elif config is not None:
        effective_scope = config.scope
    else:
        effective_scope = requested_scope or "auto"
    scope: JournalScope | None = None
    try:
        scope = resolve_scope(effective_scope, cwd=cwd, agent_root=agent_root)
    except JournalError as error:
        checks.append(_check("scope", "fail", str(error)))
    else:
        checks.append(
            _check(
                "scope",
                "ok",
                f"kind={scope.kind} journal={scope.journal_path}",
            )
        )

    if scope is None:
        checks.append(_check("journal", "skipped", "scope resolution failed"))
    else:
        checks.append(inspect_journal(scope))
    checks.append(_capture_check(scope))
    checks.append(
        _check(
            "journal.deep",
            "skipped",
            "deep verification is explicit: run aiq journal check "
            "(it may migrate storage)",
        )
    )
    for name, module in _INTEGRATION_MODULES:
        checks.append(
            _integration_check(
                name,
                module,
                invoked_launcher=invoked_launcher,
                python_executable=python_executable,
                environment=environment,
            )
        )
    checks.append(_report_check(config))

    failed = any(check["status"] == "fail" for check in checks)
    return DoctorReport(
        status="failed" if failed else "ok",
        checks=tuple(checks),
        config=config,
    )
