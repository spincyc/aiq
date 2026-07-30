from __future__ import annotations

import argparse

from aiq.cli._protocol import (
    _add_config_arguments,
    _emit,
    _explicit_json,
    _invoked_console_launcher,
    _invoked_python_executable,
    _single_line,
)
from aiq.doctor import run_doctor


def _doctor(arguments: argparse.Namespace) -> int:
    report = run_doctor(
        requested_scope=arguments.scope,
        cwd=arguments.cwd,
        agent_root=arguments.agent_root,
        repo_config=not arguments.no_repo_config,
        invoked_launcher=_invoked_console_launcher(),
        python_executable=_invoked_python_executable(),
    )
    as_json = _explicit_json(arguments) or (
        report.config is not None and report.config.output == "json"
    )
    if as_json:
        _emit(
            {"status": report.status, "checks": list(report.checks)},
            as_json=True,
        )
    else:
        name_width = max(len(check["check"]) for check in report.checks)
        for check in report.checks:
            print(
                f"{check['check']:<{name_width}}  "
                f"{check['status']:<7}  "
                f"{_single_line(check['detail'])}"
            )
    return 0 if report.status == "ok" else 1


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    doctor = subparsers.add_parser(
        "doctor",
        description=(
            "Run cheap read-only local health checks. Deep journal "
            "verification stays explicit through aiq journal check, "
            "which may migrate supported storage."
        ),
    )
    _add_config_arguments(doctor)
    doctor.set_defaults(handler=_doctor)
