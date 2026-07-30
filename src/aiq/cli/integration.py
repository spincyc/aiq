from __future__ import annotations

import argparse
from importlib import resources
import json
from pathlib import Path
import sys
from typing import Any

from aiq.cli._protocol import (
    _add_user_selector,
    _emit,
    _explicit_json,
    _invoked_console_launcher,
    _invoked_python_executable,
)
from aiq.integrations import (
    HOOK_INTEGRATIONS,
    INTEGRATIONS,
    KIND_GUIDANCE,
    guidance,
)


_INTEGRATION_CHOICES = tuple(sorted(HOOK_INTEGRATIONS))


def _integration_list(arguments: argparse.Namespace) -> int:
    integrations = sorted(
        (
            *(
                {
                    "id": record.integration_id,
                    "purpose": record.purpose,
                    "version": record.contract_version,
                }
                for record in INTEGRATIONS.values()
            ),
            {
                "id": "generic",
                "purpose": "Ingest canonical provider-neutral event JSON.",
                "version": 1,
            },
        ),
        key=lambda integration: integration["id"],
    )
    if _explicit_json(arguments):
        _emit({"integrations": integrations}, as_json=True)
    else:
        for integration in integrations:
            print(f"{integration['id']}\t{integration['purpose']}")
    return 0


def _guidance_target(arguments: argparse.Namespace) -> Path:
    if arguments.user:
        raise guidance.GuidanceIntegrationError(
            "the guidance integration uses --target, not --user"
        )
    for option in ("launcher", "git_executable"):
        if getattr(arguments, option, None) is not None:
            raise guidance.GuidanceIntegrationError(
                "the guidance integration does not accept "
                f"--{option.replace('_', '-')}"
            )
    return arguments.target


def _require_user_selector(
    arguments: argparse.Namespace,
    operation: str,
) -> None:
    integration_id = arguments.integration_id
    error_class = HOOK_INTEGRATIONS[integration_id].module.SPEC.error_class
    corrected = f"run aiq integration {operation} {integration_id} --user"
    if getattr(arguments, "target", None) is not None:
        raise error_class(
            f"the {integration_id} integration uses --user, not --target: "
            f"{corrected}"
        )
    if not arguments.user:
        raise error_class(
            f"the {integration_id} integration requires --user: {corrected}"
        )


def _hook_lifecycle_kwargs(
    arguments: argparse.Namespace,
    operation: str,
) -> dict[str, Any]:
    _require_user_selector(arguments, operation)
    if operation == "uninstall":
        return {}
    return {
        "launcher": arguments.launcher,
        "invoked_launcher": _invoked_console_launcher(),
        "python_executable": _invoked_python_executable(),
        "git_executable": arguments.git_executable,
    }


def _guidance_lifecycle_kwargs(
    arguments: argparse.Namespace,
    operation: str,
) -> dict[str, Any]:
    return {"target": _guidance_target(arguments)}


# Extra per-operation options forwarded from parsed arguments, plus the
# per-kind base kwargs builder used for every lifecycle operation.
_LIFECYCLE_OPTIONS: dict[str, tuple[str, ...]] = {
    "plan": ("repair",),
    "install": ("repair", "plan_token"),
    "check": (),
    "uninstall": (),
}


def _integration_lifecycle(
    arguments: argparse.Namespace,
    operation: str,
) -> int:
    record = INTEGRATIONS[arguments.integration_id]
    build_kwargs = (
        _guidance_lifecycle_kwargs
        if record.kind == KIND_GUIDANCE
        else _hook_lifecycle_kwargs
    )
    kwargs = build_kwargs(arguments, operation)
    for option in _LIFECYCLE_OPTIONS[operation]:
        kwargs[option] = getattr(arguments, option)
    _emit(
        getattr(record.module, f"{operation}_integration")(**kwargs),
        as_json=_explicit_json(arguments),
    )
    return 0


def _integration_plan(arguments: argparse.Namespace) -> int:
    return _integration_lifecycle(arguments, "plan")


def _integration_install(arguments: argparse.Namespace) -> int:
    return _integration_lifecycle(arguments, "install")


def _integration_check(arguments: argparse.Namespace) -> int:
    return _integration_lifecycle(arguments, "check")


def _integration_uninstall(arguments: argparse.Namespace) -> int:
    return _integration_lifecycle(arguments, "uninstall")


def _integration_print(arguments: argparse.Namespace) -> int:
    as_json = _explicit_json(arguments)
    if arguments.integration_id == "agents":
        guidance = (
            resources.files("aiq._resources")
            .joinpath("AGENTS.md")
            .read_text(encoding="utf-8")
        )
        if as_json:
            _emit(
                {"artifact": "agents", "content": guidance},
                as_json=True,
            )
        else:
            sys.stdout.write(guidance)
        return 0

    module = HOOK_INTEGRATIONS[arguments.integration_id].module
    rendered = module.print_integration(
        launcher=arguments.launcher,
        invoked_launcher=_invoked_console_launcher(),
        python_executable=_invoked_python_executable(),
        git_executable=arguments.git_executable,
    )
    if as_json:
        _emit(
            {
                "integration": arguments.integration_id,
                "fragment": json.loads(rendered),
            },
            as_json=True,
        )
    else:
        sys.stdout.write(rendered)
    return 0


def _integration_receive(arguments: argparse.Namespace) -> int:
    module = HOOK_INTEGRATIONS[arguments.integration].module
    integration_id = (
        module.INTEGRATION_ID
        if arguments.integration_id is None
        else arguments.integration_id
    )
    return module.receive_hook_main(
        integration_id=integration_id,
        git_executable=arguments.git_executable,
    )


def _add_lifecycle_selectors(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "integration_id",
        choices=(*_INTEGRATION_CHOICES, "guidance"),
    )
    _add_user_selector(parser)
    parser.add_argument(
        "--target",
        type=Path,
        help="absolute guidance file managed by the guidance integration",
    )


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    integration = subparsers.add_parser("integration")
    integration_commands = integration.add_subparsers(
        dest="integration_command",
        required=True,
    )
    integration_list = integration_commands.add_parser("list")
    integration_list.add_argument("--json", action="store_true")
    integration_list.set_defaults(handler=_integration_list)
    integration_plan = integration_commands.add_parser("plan")
    _add_lifecycle_selectors(integration_plan)
    integration_plan.add_argument("--launcher", type=Path)
    integration_plan.add_argument("--git-executable", type=Path)
    integration_plan.add_argument("--repair", action="store_true")
    integration_plan.add_argument("--json", action="store_true")
    integration_plan.set_defaults(handler=_integration_plan)
    integration_install = integration_commands.add_parser("install")
    _add_lifecycle_selectors(integration_install)
    integration_install.add_argument("--launcher", type=Path)
    integration_install.add_argument("--git-executable", type=Path)
    integration_install.add_argument("--repair", action="store_true")
    integration_install.add_argument("--plan-token")
    integration_install.add_argument("--json", action="store_true")
    integration_install.set_defaults(handler=_integration_install)
    integration_check = integration_commands.add_parser("check")
    _add_lifecycle_selectors(integration_check)
    integration_check.add_argument("--launcher", type=Path)
    integration_check.add_argument("--git-executable", type=Path)
    integration_check.add_argument("--json", action="store_true")
    integration_check.set_defaults(handler=_integration_check)
    integration_uninstall = integration_commands.add_parser("uninstall")
    _add_lifecycle_selectors(integration_uninstall)
    integration_uninstall.add_argument("--json", action="store_true")
    integration_uninstall.set_defaults(handler=_integration_uninstall)
    integration_print = integration_commands.add_parser("print")
    integration_print.add_argument(
        "integration_id",
        choices=("agents", *_INTEGRATION_CHOICES),
    )
    _add_user_selector(integration_print)
    integration_print.add_argument("--launcher", type=Path)
    integration_print.add_argument("--git-executable", type=Path)
    integration_print.add_argument("--json", action="store_true")
    integration_print.set_defaults(handler=_integration_print)
    integration_receive = integration_commands.add_parser("receive")
    integration_receive.add_argument("integration", choices=_INTEGRATION_CHOICES)
    integration_receive.add_argument("--integration-id")
    integration_receive.add_argument(
        "--git-executable",
        type=Path,
        required=True,
    )
    integration_receive.set_defaults(handler=_integration_receive)
