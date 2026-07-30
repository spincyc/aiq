from __future__ import annotations

import argparse
from pathlib import Path

from aiq.cli._protocol import _emit, _scope, _scope_parser
from aiq.journal import check_journal, create_snapshot, initialize_journal
from aiq.privacy import (
    destroy_journal,
    export_journal,
    plan_journal_destroy,
)


def _journal_path(arguments: argparse.Namespace) -> int:
    scope = _scope(arguments)
    _emit(
        {"scope": scope.to_dict()} if arguments.json else str(scope.journal_path),
        as_json=arguments.json,
    )
    return 0


def _journal_init(arguments: argparse.Namespace) -> int:
    scope = _scope(arguments)
    path = initialize_journal(scope)
    _emit(
        {"status": "initialized", "scope": scope.to_dict()}
        if arguments.json
        else str(path),
        as_json=arguments.json,
    )
    return 0


def _journal_check(arguments: argparse.Namespace) -> int:
    result = check_journal(_scope(arguments))
    _emit(
        {
            key: result[key]
            for key in (
                "status",
                "messages",
                "tasks",
                "applications",
                "claims",
                "snapshots",
                "scope",
            )
        },
        as_json=arguments.json,
    )
    return 0


def _journal_snapshot(arguments: argparse.Namespace) -> int:
    keep = (
        arguments.keep
        if arguments.keep is not None
        else arguments.effective_config.snapshot_keep
    )
    _emit(
        create_snapshot(_scope(arguments), keep=keep),
        as_json=arguments.json,
    )
    return 0


def _journal_export(arguments: argparse.Namespace) -> int:
    _emit(
        export_journal(_scope(arguments), arguments.output),
        as_json=arguments.json,
    )
    return 0


def _journal_destroy(arguments: argparse.Namespace) -> int:
    result = (
        plan_journal_destroy(_scope(arguments))
        if arguments.plan
        else destroy_journal(_scope(arguments), arguments.confirm)
    )
    _emit(result, as_json=arguments.json)
    return 0


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    journal = subparsers.add_parser("journal")
    journal_commands = journal.add_subparsers(
        dest="journal_command",
        required=True,
    )
    journal_path = _scope_parser(journal_commands, "path")
    journal_path.set_defaults(handler=_journal_path)
    journal_init = _scope_parser(journal_commands, "init")
    journal_init.set_defaults(handler=_journal_init)
    journal_check = _scope_parser(journal_commands, "check")
    journal_check.set_defaults(handler=_journal_check)
    journal_snapshot = _scope_parser(journal_commands, "snapshot")
    journal_snapshot.add_argument("--keep", type=int)
    journal_snapshot.set_defaults(handler=_journal_snapshot)
    journal_export = _scope_parser(journal_commands, "export")
    journal_export.add_argument("output", type=Path)
    journal_export.set_defaults(handler=_journal_export)
    journal_destroy = _scope_parser(journal_commands, "destroy")
    destroy_mode = journal_destroy.add_mutually_exclusive_group(required=True)
    destroy_mode.add_argument("--plan", action="store_true")
    destroy_mode.add_argument("--confirm", metavar="TOKEN")
    journal_destroy.set_defaults(handler=_journal_destroy)
