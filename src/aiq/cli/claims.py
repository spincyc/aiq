from __future__ import annotations

import argparse

from aiq.cli._protocol import _emit, _scope, _scope_parser, _single_line
from aiq.cli._render import _task_reference
from aiq.journal import project_label
from aiq.queue import list_claims, release_claim


def _claim_release(arguments: argparse.Namespace) -> int:
    result = release_claim(_scope(arguments), arguments.claim_id)
    _emit(
        {
            key: result[key]
            for key in (
                "status",
                "claim_id",
                "resource_kind",
                "resource_id",
                "replayed",
            )
        },
        as_json=arguments.json,
    )
    return 0


def _claim_list(arguments: argparse.Namespace) -> int:
    scope = _scope(arguments)
    claims = list_claims(
        scope,
        owner_id=arguments.owner,
        resource_kind=arguments.resource,
        status=arguments.status,
        limit=arguments.limit,
    )
    if arguments.json:
        _emit({"claims": claims}, as_json=True)
        return 0
    project = project_label(scope)
    for claim in claims:
        resource = (
            _task_reference(project, claim["resource_id"])
            if claim["resource_kind"] == "task"
            else claim["resource_id"]
        )
        print(
            f"{claim['claim_id']}\t{claim['resource_kind']}\t"
            f"{resource}\t{_single_line(claim['owner_id'])}\t"
            f"{claim['status']}\t{claim['expires_at']}"
        )
    return 0


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    claim = subparsers.add_parser("claim")
    claim_commands = claim.add_subparsers(dest="claim_command", required=True)
    claim_list = _scope_parser(claim_commands, "list")
    claim_list.add_argument("--owner")
    claim_list.add_argument("--resource", choices=("message", "task"))
    claim_list.add_argument("--status", choices=("active", "expired"))
    claim_list.add_argument("--limit", type=int, default=100)
    claim_list.set_defaults(handler=_claim_list)
    claim_release = _scope_parser(claim_commands, "release")
    claim_release.add_argument("claim_id")
    claim_release.set_defaults(handler=_claim_release)
