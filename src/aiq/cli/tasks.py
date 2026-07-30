from __future__ import annotations

import argparse
import os
from typing import Any, Mapping

from aiq.cli._protocol import _emit, _scope, _scope_parser, _single_line
from aiq.cli._render import _task_detail, _task_reference, _task_summary
from aiq.journal import project_label
from aiq.queue import (
    TASK_STATES,
    explain_task,
    list_tasks,
    settle_tasks_done,
    show_task,
    task_history,
)


def _task_list(arguments: argparse.Namespace) -> int:
    scope = _scope(arguments)
    tasks = [
        _task_summary(task)
        for task in list_tasks(
            scope,
            states=set(arguments.state) if arguments.state else None,
            limit=arguments.limit,
        )
    ]
    if arguments.json:
        _emit({"tasks": tasks}, as_json=True)
        return 0
    # Human output only: JSON never prefixes task IDs, so JSON callers
    # never pay for this read.
    project = project_label(scope)
    for task in tasks:
        print(
            f"{_task_reference(project, task['task_id'])}\t{task['state']}\t"
            f"r{task['revision']}\t{task['priority']}\t"
            f"{_single_line(task['title'])}"
        )
    return 0


def _task_show(arguments: argparse.Namespace) -> int:
    scope = _scope(arguments)
    detail = _task_detail(show_task(scope, arguments.task_id))
    if not arguments.json:
        print(_task_reference(project_label(scope), detail["task_id"]))
    _emit({"task": detail}, as_json=arguments.json)
    return 0


def _task_explain(arguments: argparse.Namespace) -> int:
    explained = explain_task(_scope(arguments), arguments.task_id)
    if arguments.json:
        _emit({"explain": explained}, as_json=True)
        return 0
    print(
        f"{explained['task_id']}\t{explained['state']}\t"
        f"r{explained['revision']}\t{_single_line(explained['explanation'])}"
    )
    for prerequisite in explained["prerequisites"]:
        met = "met" if prerequisite["satisfied"] else "unmet"
        print(
            f"requires\t{prerequisite['task_id']}\t"
            f"{prerequisite['state']}\t{met}"
        )
    return 0


def _history_compact(entry: Mapping[str, Any]) -> str:
    event_type = entry["type"]
    detail = entry["detail"]
    if event_type == "task.created":
        return f"r{detail['revision']} {detail['state']}"
    if event_type == "task.revised":
        return f"r{detail['revision']} {','.join(detail['fields'])}"
    if event_type == "task.state_changed":
        compact = f"r{detail['revision']} {detail['state']}"
        if detail["superseded_by_task_id"] is not None:
            compact += f" by={detail['superseded_by_task_id']}"
        return compact
    if event_type in {"task.dependency_added", "task.dependency_removed"}:
        sign = "+" if event_type.endswith("added") else "-"
        return f"r{detail['revision']} {sign}{detail['dependency']}"
    if event_type == "claim.acquired":
        return f"{detail['claim_id']} {detail['owner_id']}"
    if event_type.startswith("claim."):
        return f"{detail['claim_id']} {detail['disposition']}"
    return ""


def _task_history(arguments: argparse.Namespace) -> int:
    scope = _scope(arguments)
    events = task_history(
        scope,
        arguments.task_id,
        limit=arguments.limit,
    )
    if arguments.json:
        _emit({"task_id": arguments.task_id, "events": events}, as_json=True)
        return 0
    print(_task_reference(project_label(scope), arguments.task_id))
    for entry in events:
        print(
            f"{entry['occurred_at']}\t{entry['type']}\t"
            f"{_single_line(_history_compact(entry))}"
        )
    return 0


def _task_done(arguments: argparse.Namespace) -> int:
    config = arguments.effective_config
    owner = arguments.owner if arguments.owner is not None else config.owner
    result = settle_tasks_done(
        _scope(arguments),
        task_ids=arguments.task_ids,
        summary=arguments.summary,
        owner_id=owner,
        lease_seconds=config.lease_seconds,
        cwd=os.fspath(arguments.cwd.resolve()),
    )
    if arguments.json:
        _emit(result, as_json=True)
        return 0
    for task in result["tasks"]:
        print(f"{task['task_id']}\t{task['state']}\tr{task['revision']}")
    return 0


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    task = subparsers.add_parser("task")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_list = _scope_parser(task_commands, "list")
    task_list.add_argument("--state", action="append", choices=TASK_STATES)
    task_list.add_argument("--limit", type=int, default=100)
    task_list.set_defaults(handler=_task_list)
    task_show = _scope_parser(task_commands, "show")
    task_show.add_argument("task_id")
    task_show.set_defaults(handler=_task_show)
    task_explain = _scope_parser(task_commands, "explain")
    task_explain.add_argument("task_id")
    task_explain.set_defaults(handler=_task_explain)
    task_history = _scope_parser(task_commands, "history")
    task_history.add_argument("task_id")
    task_history.add_argument("--limit", type=int, default=50)
    task_history.set_defaults(handler=_task_history)
    task_done = _scope_parser(task_commands, "done")
    task_done.description = (
        "Settle every named task as done in one transaction: the summary "
        "is recorded as a message and one atomic effects document "
        "transitions all named tasks, reusing the caller's active task "
        "claim when the owner matches and leasing ready tasks inside the "
        "same transaction. Any ineligible task fails the whole command."
    )
    task_done.add_argument("task_ids", nargs="+", metavar="task_id")
    task_done.add_argument("--summary", required=True)
    task_done.add_argument("--owner")
    task_done.set_defaults(handler=_task_done)
