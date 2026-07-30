from __future__ import annotations

import argparse
import json

from aiq.capabilities import list_capabilities, show_capability
from aiq.cli._protocol import _emit, _explicit_json


def _capability_list(arguments: argparse.Namespace) -> int:
    capabilities = list_capabilities()
    if _explicit_json(arguments):
        _emit({"capabilities": capabilities}, as_json=True)
        return 0
    for capability in capabilities:
        print(
            f"{capability['id']}\tv{capability['version']}\t"
            f"{capability['purpose']}"
        )
    return 0


def _capability_show(arguments: argparse.Namespace) -> int:
    capability = show_capability(arguments.capability_id)
    if _explicit_json(arguments):
        _emit(capability, as_json=True)
    else:
        print(
            json.dumps(
                capability,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    return 0


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    capability = subparsers.add_parser("capability")
    capability_commands = capability.add_subparsers(
        dest="capability_command",
        required=True,
    )
    capability_list = capability_commands.add_parser("list")
    capability_list.add_argument("--json", action="store_true")
    capability_list.set_defaults(handler=_capability_list)
    capability_show = capability_commands.add_parser("show")
    capability_show.add_argument("capability_id")
    capability_show.add_argument("--json", action="store_true")
    capability_show.set_defaults(handler=_capability_show)
