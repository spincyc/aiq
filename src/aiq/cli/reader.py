from __future__ import annotations

import argparse

from aiq.cli._protocol import _emit, _scope, _scope_parser, _single_line
from aiq.queue import (
    acquire_reader_lease,
    read_reader_lease,
    release_reader_lease,
)


def _reader_status(arguments: argparse.Namespace) -> int:
    scope = _scope(arguments)
    lease = read_reader_lease(
        scope,
        reader_id=arguments.effective_config.reader,
    )
    if arguments.json:
        _emit({"reader": lease, "scope": scope.to_dict()}, as_json=True)
        return 0
    print(
        f"{lease['status']}\t{_single_line(lease['reader_id'] or '-')}\t"
        f"{_single_line(lease['owner_id'] or '-')}\t"
        f"{lease['expires_at'] or '-'}"
    )
    return 0


def _reader_acquire(arguments: argparse.Namespace) -> int:
    config = arguments.effective_config
    result = acquire_reader_lease(
        _scope(arguments),
        owner_id=config.owner,
        reader_id=config.reader,
        # --lease-seconds names the reader lease here, not an item lease;
        # this command leases no message and no task.
        lease_seconds=(
            arguments.lease_seconds
            if arguments.lease_seconds is not None
            else config.reader_lease_seconds
        ),
    )
    if arguments.json:
        _emit(result, as_json=True)
        return 0
    _emit(
        {"status": result["status"], "acquired": result["acquired"]},
        as_json=False,
    )
    return 0


def _reader_release(arguments: argparse.Namespace) -> int:
    result = release_reader_lease(
        _scope(arguments),
        reader_id=arguments.effective_config.reader,
    )
    if arguments.json:
        _emit(result, as_json=True)
        return 0
    _emit(
        {"status": result["status"], "replayed": result["replayed"]},
        as_json=False,
    )
    return 0


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    reader = subparsers.add_parser(
        "reader",
        description=(
            "Inspect or hold the scope's single reader role. Many "
            "sessions may ingest and enqueue; one at a time may drain."
        ),
    )
    reader_commands = reader.add_subparsers(
        dest="reader_command",
        required=True,
    )
    reader_status = _scope_parser(reader_commands, "status")
    reader_status.set_defaults(handler=_reader_status)
    reader_acquire = _scope_parser(reader_commands, "acquire")
    reader_acquire.add_argument("--reader")
    reader_acquire.add_argument("--owner")
    reader_acquire.add_argument("--lease-seconds", type=int)
    reader_acquire.set_defaults(handler=_reader_acquire)
    reader_release = _scope_parser(reader_commands, "release")
    reader_release.add_argument("--reader")
    reader_release.set_defaults(handler=_reader_release)
