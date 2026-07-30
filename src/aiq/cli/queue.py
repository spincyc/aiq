from __future__ import annotations

import argparse
import os

from aiq.cli._protocol import (
    _add_config_arguments,
    _emit,
    _scope,
    _scope_parser,
    _single_line,
)
from aiq.cli._render import _claim_public, _task_summary
from aiq.queue import (
    TASK_STATES,
    claim_next_tasks,
    enqueue_task,
    next_tasks,
    overview_tasks,
)


def _queue_peek(arguments: argparse.Namespace) -> int:
    tasks = [
        _task_summary(task)
        for task in next_tasks(_scope(arguments), limit=arguments.limit)
    ]
    if arguments.json:
        _emit({"tasks": tasks}, as_json=True)
        return 0
    for task in tasks:
        print(
            f"{task['task_id']}\t{task['state']}\t"
            f"r{task['revision']}\t{task['priority']}\t"
            f"{_single_line(task['title'])}"
        )
    return 0


def _queue_next(arguments: argparse.Namespace) -> int:
    config = arguments.effective_config
    owner = arguments.owner if arguments.owner is not None else config.owner
    lease = (
        arguments.lease_seconds
        if arguments.lease_seconds is not None
        else config.lease_seconds
    )
    items = [
        {
            "task": _task_summary(item["task"]),
            "claim": _claim_public(item["claim"]),
        }
        for item in claim_next_tasks(
            _scope(arguments),
            owner_id=owner,
            lease_seconds=lease,
            limit=arguments.limit,
        )
    ]
    if arguments.json:
        _emit({"items": items}, as_json=True)
        return 0
    for item in items:
        task = item["task"]
        claim = item["claim"]
        print(
            f"{task['task_id']}\t{task['state']}\t"
            f"r{task['revision']}\t{task['priority']}\t"
            f"{claim['claim_id']}\t{_single_line(task['title'])}"
        )
    return 0


def _enqueue(arguments: argparse.Namespace) -> int:
    config = arguments.effective_config
    result = enqueue_task(
        _scope(arguments),
        title=arguments.title,
        objective=arguments.objective,
        priority=arguments.priority,
        requires=arguments.requires,
        owner_id=config.owner,
        lease_seconds=config.lease_seconds,
        cwd=os.fspath(arguments.cwd.resolve()),
    )
    if arguments.json:
        _emit(
            {
                "task_id": result["task_id"],
                "message_id": result["message_id"],
                "state": result["state"],
            },
            as_json=True,
        )
        return 0
    print(f"{result['task_id']}\t{result['state']}\t{result['message_id']}")
    return 0


def _list(arguments: argparse.Namespace) -> int:
    if arguments.state:
        states = set(arguments.state)
    elif arguments.all:
        states = set(TASK_STATES)
    else:
        states = None
    tasks = overview_tasks(
        _scope(arguments),
        states=states,
        limit=arguments.limit,
    )
    if arguments.json:
        _emit({"tasks": tasks}, as_json=True)
        return 0
    for task in tasks:
        print(
            f"{task['task_id']}\t{task['state']}\t{task['priority']}\t"
            f"{_single_line(task['title'])}"
        )
    return 0


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    queue = subparsers.add_parser("queue")
    queue_commands = queue.add_subparsers(dest="queue_command", required=True)
    queue_peek = _scope_parser(queue_commands, "peek")
    queue_peek.add_argument("--limit", type=int, default=1)
    queue_peek.set_defaults(handler=_queue_peek)
    queue_next = _scope_parser(queue_commands, "next")
    queue_next.add_argument("--owner")
    queue_next.add_argument("--lease-seconds", type=int)
    queue_next.add_argument("--limit", type=int, default=1)
    queue_next.set_defaults(handler=_queue_next)

    enqueue = subparsers.add_parser(
        "enqueue",
        description=(
            "Create one task in one transaction: an auto-generated "
            "message records the request, is claimed, and one atomic "
            "create-task effects document is applied."
        ),
    )
    _add_config_arguments(enqueue)
    enqueue.add_argument("title")
    enqueue.add_argument("--objective")
    enqueue.add_argument("--priority", type=int, default=0)
    enqueue.add_argument(
        "--requires",
        nargs="+",
        default=[],
        metavar="TASK-ID",
    )
    enqueue.set_defaults(load_config=True, handler=_enqueue)

    dequeue = subparsers.add_parser(
        "dequeue",
        description=(
            "Lease ready work; the ergonomic synonym of queue next with "
            "identical semantics. The lease is time-bounded ownership, "
            "never removal: the task stays in the ledger and becomes "
            "claimable again when the lease expires or is released."
        ),
    )
    _add_config_arguments(dequeue)
    dequeue.add_argument("--owner")
    dequeue.add_argument("--lease-seconds", type=int)
    dequeue.add_argument("--limit", type=int, default=1)
    dequeue.set_defaults(load_config=True, handler=_queue_next)

    list_tasks_parser = subparsers.add_parser(
        "list",
        description=(
            "List tasks in task-number order. The default shows the "
            "non-terminal states; --all adds done, canceled, and "
            "superseded, and --state selects states explicitly."
        ),
    )
    _add_config_arguments(list_tasks_parser)
    list_selection = list_tasks_parser.add_mutually_exclusive_group()
    list_selection.add_argument(
        "--state",
        action="append",
        choices=TASK_STATES,
    )
    list_selection.add_argument("--all", action="store_true")
    list_tasks_parser.add_argument("--limit", type=int, default=50)
    list_tasks_parser.set_defaults(load_config=True, handler=_list)
