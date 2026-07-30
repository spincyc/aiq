from __future__ import annotations

import argparse

from aiq.cli._protocol import _add_config_arguments, _emit


def _config_show(arguments: argparse.Namespace) -> int:
    _emit(
        arguments.effective_config.to_dict(include_sources=arguments.sources),
        as_json=arguments.json,
    )
    return 0


def _config_check(arguments: argparse.Namespace) -> int:
    _emit({"status": "ok"}, as_json=arguments.json)
    return 0


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    config = subparsers.add_parser("config")
    config_commands = config.add_subparsers(
        dest="config_command",
        required=True,
    )
    config_show = config_commands.add_parser("show")
    _add_config_arguments(config_show, operational=False)
    config_show.add_argument("--sources", action="store_true")
    config_show.set_defaults(handler=_config_show, load_config=True)
    config_check = config_commands.add_parser("check")
    _add_config_arguments(config_check, operational=False)
    config_check.set_defaults(handler=_config_check, load_config=True)
