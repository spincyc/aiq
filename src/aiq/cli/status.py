from __future__ import annotations

import argparse

from aiq.cli._protocol import (
    _add_config_arguments,
    _emit,
    _scope,
    _single_line,
)
from aiq.queue import MESSAGE_STATES, TASK_STATES, read_status


def _status(arguments: argparse.Namespace) -> int:
    scope = _scope(arguments)
    result = read_status(scope)
    if arguments.json:
        _emit({**result, "scope": scope.to_dict()}, as_json=True)
        return 0
    for label, states in (
        ("messages", MESSAGE_STATES),
        ("tasks", TASK_STATES),
    ):
        counts = result[label]
        print(
            f"{label:<8}  "
            + "  ".join(f"{state}={counts[state]}" for state in states)
        )
    print(f"{'claims':<8}  active={result['claims']['active']}")
    for task in result["ready"]:
        print(
            f"{'ready':<8}  {task['task_id']}\tp{task['priority']}\t"
            f"{_single_line(task['title'])}"
        )
    for task in result["blocked"]:
        line = (
            f"{'blocked':<8}  {task['task_id']}\tp{task['priority']}\t"
            f"{_single_line(task['title'])}"
        )
        if task["blocked_by"]:
            line += f"\tblocked by {', '.join(task['blocked_by'])}"
        print(line)
    return 0


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    status = subparsers.add_parser("status")
    _add_config_arguments(status)
    status.set_defaults(load_config=True, handler=_status)
