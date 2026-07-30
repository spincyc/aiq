from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Sequence

from aiq import __version__
from aiq.cli import (
    capability,
    claims,
    config,
    doctor,
    inbox,
    integration,
    journal,
    queue,
    reconcile,
    report,
    status,
    tasks,
)
from aiq.cli._errors import (
    _JOURNAL_ERROR_CODE_EXITS,
    _classify_error,
    _classify_journal_error,
)
from aiq.cli._protocol import (
    CONFIG_OUTPUT_COMMANDS,
    PROTOCOL_VERSION,
    _ArgumentParser,
    _add_config_arguments,
    _add_user_selector,
    _config_cli_values,
    _decode_utf8,
    _emit,
    _emit_error,
    _error_json,
    _explicit_json,
    _invocation_wants_json,
    _invoked_console_launcher,
    _invoked_python_executable,
    _prepare_config,
    _read_file_bounded,
    _read_stdin_bounded,
    _read_text_argument,
    _resolve_config,
    _scope,
    _scope_parser,
    _single_line,
    _versioned,
)
from aiq.cli._render import (
    _MESSAGE_SUMMARY_FIELDS,
    _TASK_DETAIL_FIELDS,
    _TASK_SUMMARY_FIELDS,
    _application_public,
    _claim_public,
    _message_summary,
    _task_detail,
    _task_summary,
    _timestamp_from_microseconds,
)
from aiq.cli.inbox import MESSAGE_INPUT_MAX_BYTES
from aiq.config import ConfigError
from aiq.events import EventError
from aiq.journal import JournalError


# Command families in registration order; each register() wires its
# top-level parsers, so help and dispatch ordering match this tuple.
_FAMILIES = (
    config,
    doctor,
    journal,
    inbox,
    tasks,
    queue,
    claims,
    status,
    report,
    capability,
    integration,
    reconcile,
)


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="aiq")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for family in _FAMILIES:
        family.register(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    invocation = list(argv) if argv is not None else sys.argv[1:]
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        _prepare_config(arguments)
        return arguments.handler(arguments)
    except (
        ConfigError,
        EventError,
        JournalError,
        json.JSONDecodeError,
        OSError,
        sqlite3.Error,
        UnicodeDecodeError,
        ValueError,
    ) as error:
        code, exit_code = _classify_error(error)
        _emit_error(code, str(error), as_json=_error_json(arguments, invocation))
        return exit_code
    except Exception as error:
        _emit_error(
            "internal_error",
            str(error) or error.__class__.__name__,
            as_json=_error_json(arguments, invocation),
        )
        return 70
