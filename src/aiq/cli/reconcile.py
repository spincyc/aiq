from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
from typing import Any

from aiq.cli._protocol import (
    _add_config_arguments,
    _add_user_selector,
    _emit,
    _invoked_console_launcher,
    _invoked_python_executable,
    _scope,
    _single_line,
)
from aiq.doctor import inspect_journal
from aiq.integrations import HOOK_INTEGRATIONS
from aiq.integrations._hooks import HookIntegrationError
from aiq.journal import JournalError, check_journal


_RECONCILE_PROBLEM_STATUSES = frozenset({"blocked", "drifted", "failed"})


def _recorded_executable(recorded: str) -> Path | None:
    path = Path(recorded)
    if path.is_file() and os.access(path, os.X_OK):
        return path
    return None


def _reconcile_integration(
    integration_id: str,
    module: Any,
    arguments: argparse.Namespace,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "integration": integration_id,
        "status": "skipped",
        "action": "none",
        "reason": None,
    }
    try:
        manifest = module.installed_manifest()
    except HookIntegrationError as error:
        entry.update({"status": "failed", "reason": str(error)})
        return entry
    if manifest is None:
        entry["reason"] = (
            f"no installed AIQ {integration_id} integration manifest"
        )
        return entry
    entry["target"] = manifest["target"]
    invoked_launcher = _invoked_console_launcher()
    if arguments.launcher is None and invoked_launcher is None:
        invoked_launcher = _recorded_executable(manifest["launcher"])
    git_executable: Path | None = arguments.git_executable
    if git_executable is None:
        git_executable = _recorded_executable(manifest["git_executable"])
    options: dict[str, Any] = {
        "launcher": arguments.launcher,
        "invoked_launcher": invoked_launcher,
        "python_executable": _invoked_python_executable(),
        "git_executable": git_executable,
    }
    try:
        plan = module.plan_integration(**options)
        if plan["status"] == "installed" and plan["action"] == "none":
            entry["status"] = "ok"
            return entry
        if plan["status"] != "drifted":
            entry.update(
                {"status": "blocked", "reason": plan["blocked_reason"]}
            )
            return entry
        if not arguments.apply:
            entry.update(
                {
                    "status": "drifted",
                    "action": "repair",
                    "reason": plan["blocked_reason"],
                }
            )
            return entry
        repair_plan = module.plan_integration(**options, repair=True)
        if repair_plan["action"] != "repair":
            entry.update(
                {
                    "status": "blocked",
                    "reason": repair_plan["blocked_reason"]
                    or "no safe owned repair is planned",
                }
            )
            return entry
        repaired = module.install_integration(
            **options,
            repair=True,
            plan_token=repair_plan["plan_token"],
        )
        entry.update(
            {
                "status": "repaired",
                "action": "repair",
                "backup": repaired.get("backup"),
            }
        )
    except HookIntegrationError as error:
        entry.update({"status": "failed", "reason": str(error)})
    return entry


_RECONCILE_INSPECTION_STATUSES = {
    "ok": "ok",
    "skipped": "skipped",
    "warn": "drifted",
    "fail": "failed",
}


def _reconcile_journal(arguments: argparse.Namespace) -> dict[str, Any]:
    try:
        scope = _scope(arguments)
        if not arguments.apply:
            # Report-only default: a cheap read-only inspection shared
            # with doctor. Full validation and migration stay behind
            # --apply.
            inspection = inspect_journal(scope)
            status = _RECONCILE_INSPECTION_STATUSES[inspection["status"]]
            return {
                "status": status,
                "reason": None if status == "ok" else inspection["detail"],
                "scope": scope.to_dict(),
            }
        result = check_journal(scope)
    except JournalError as error:
        message = str(error)
        status = (
            "skipped"
            if getattr(error, "code", None) == "not_found"
            or "does not exist" in message
            else "failed"
        )
        return {"status": status, "reason": message}
    except (OSError, sqlite3.Error) as error:
        return {"status": "failed", "reason": str(error)}
    return {"status": result["status"], "reason": None, "scope": result["scope"]}


def _reconcile(arguments: argparse.Namespace) -> int:
    integrations = [
        _reconcile_integration(integration_id, record.module, arguments)
        for integration_id, record in sorted(HOOK_INTEGRATIONS.items())
    ]
    journal = _reconcile_journal(arguments)
    problems = sum(
        entry["status"] in _RECONCILE_PROBLEM_STATUSES
        for entry in (*integrations, journal)
    )
    status = "ok" if problems == 0 else "attention"
    if arguments.json:
        _emit(
            {
                "status": status,
                "apply": arguments.apply,
                "integrations": integrations,
                "journal": journal,
                "problems": problems,
            },
            as_json=True,
        )
    else:
        # Uniform TSV rows: kind, name, status, reason (empty when none).
        rows = [
            *(
                (
                    "integration",
                    entry["integration"],
                    entry["status"],
                    entry.get("reason") or "",
                )
                for entry in integrations
            ),
            ("journal", "-", journal["status"], journal.get("reason") or ""),
        ]
        for row in rows:
            print("\t".join(_single_line(str(field)) for field in row))
        print(f"status\t{status}\tproblems\t{problems}")
    return 0 if problems == 0 else 1


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    reconcile = subparsers.add_parser("reconcile")
    _add_config_arguments(reconcile)
    _add_user_selector(reconcile, required=True)
    reconcile.add_argument("--apply", action="store_true")
    reconcile.add_argument("--launcher", type=Path)
    reconcile.add_argument("--git-executable", type=Path)
    reconcile.set_defaults(handler=_reconcile, load_config=True)
