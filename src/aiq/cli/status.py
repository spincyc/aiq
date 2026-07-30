from __future__ import annotations

import argparse

from aiq.cli._protocol import (
    _add_config_arguments,
    _emit,
    _scope,
    _single_line,
)
from aiq.cli._render import _task_reference
from aiq.queue import MESSAGE_STATES, TASK_STATES, read_status


def _status(arguments: argparse.Namespace) -> int:
    scope = _scope(arguments)
    result = read_status(scope, reader_id=arguments.effective_config.reader)
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
    project = result["project"]
    for task in result["ready"]:
        print(
            f"{'ready':<8}  {_task_reference(project, task['task_id'])}\t"
            f"p{task['priority']}\t{_single_line(task['title'])}"
        )
    for task in result["blocked"]:
        line = (
            f"{'blocked':<8}  {_task_reference(project, task['task_id'])}\t"
            f"p{task['priority']}\t{_single_line(task['title'])}"
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
