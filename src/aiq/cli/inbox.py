from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from aiq.cli._protocol import (
    _add_config_arguments,
    _decode_utf8,
    _emit,
    _read_stdin_bounded,
    _read_text_argument,
    _resolve_config,
    _scope,
    _scope_parser,
    _single_line,
)
from aiq.cli._render import (
    _application_public,
    _claim_public,
    _message_summary,
)
from aiq.events import EVENT_JSON_MAX_BYTES, parse_event_json
from aiq.journal import ingest_message, list_inbox
from aiq.queue import (
    EFFECT_DOCUMENT_MAX_BYTES,
    apply_effects,
    claim_message,
    dispose_message,
    parse_effect_document,
)


MESSAGE_INPUT_MAX_BYTES = 1_048_576


def _ingest(arguments: argparse.Namespace) -> int:
    if arguments.event_json is not None:
        event = parse_event_json(
            _read_text_argument(
                arguments.event_json,
                EVENT_JSON_MAX_BYTES,
                label="event JSON",
            )
        )
        effective_cwd = Path(event.cwd) if event.cwd is not None else arguments.cwd
        arguments.cwd = effective_cwd
        _resolve_config(arguments)
        scope = _scope(arguments, cwd=effective_cwd)
        result = ingest_message(
            scope,
            event.content,
            source=event.source,
            idempotency_key=event.idempotency_key,
            session_id=event.session_id,
            turn_id=event.turn_id,
            cwd=os.fspath(effective_cwd.resolve()),
            if_new=arguments.if_new,
        )
    else:
        _resolve_config(arguments)
        content = (
            arguments.message
            if arguments.message is not None
            else _decode_utf8(
                _read_stdin_bounded(
                    MESSAGE_INPUT_MAX_BYTES,
                    label="message input",
                ),
                label="message input",
            )
        )
        scope = _scope(arguments)
        result = ingest_message(
            scope,
            content,
            source=arguments.source,
            idempotency_key=arguments.idempotency_key,
            session_id=arguments.session_id,
            turn_id=arguments.turn_id,
            cwd=os.fspath(arguments.cwd.resolve()),
            if_new=arguments.if_new,
        )
    if not arguments.quiet:
        payload = {
            "message_id": result.message_id,
            "state": result.state,
            "created": result.created,
            "scope": result.scope.to_dict(),
        }
        if arguments.if_new:
            payload["deduped"] = result.deduped
        _emit(payload, as_json=arguments.json)
    return 0


def _inbox_list(arguments: argparse.Namespace) -> int:
    messages = [
        _message_summary(message, include_content=arguments.include_content)
        for message in list_inbox(
            _scope(arguments),
            limit=arguments.limit,
            include_content=arguments.include_content,
        )
    ]
    if arguments.json:
        _emit({"messages": messages}, as_json=True)
        return 0
    for message in messages:
        print(
            f"{message['message_id']}\t{message['state']}\t"
            f"{message['received_at']}\t{_single_line(message['source'])}"
        )
        if arguments.include_content:
            print(_single_line(message["content"]))
    return 0


def _inbox_claim(arguments: argparse.Namespace) -> int:
    config = arguments.effective_config
    owner = arguments.owner if arguments.owner is not None else config.owner
    lease = (
        arguments.lease_seconds
        if arguments.lease_seconds is not None
        else config.lease_seconds
    )
    result = claim_message(
        _scope(arguments),
        owner_id=owner,
        lease_seconds=lease,
        message_id=arguments.message_id,
    )
    if result is None:
        payload: dict[str, Any] = {"claim": None, "message": None}
    else:
        payload = {
            "claim": _claim_public(result),
            "message": result["message"],
        }
    _emit(payload, as_json=arguments.json)
    return 0


def _inbox_apply(arguments: argparse.Namespace) -> int:
    result = apply_effects(
        _scope(arguments),
        arguments.message_id,
        parse_effect_document(
            _read_text_argument(
                arguments.effects,
                EFFECT_DOCUMENT_MAX_BYTES,
                label="effects document",
            )
        ),
        claim_id=arguments.claim,
    )
    _emit(_application_public(result), as_json=arguments.json)
    return 0


def _inbox_dispose(arguments: argparse.Namespace) -> int:
    result = dispose_message(
        _scope(arguments),
        arguments.message_id,
        claim_id=arguments.claim,
        disposition=arguments.disposition,
        reason=arguments.reason,
    )
    _emit(
        {
            key: result[key]
            for key in (
                "status",
                "message_id",
                "claim_id",
                "replayed",
            )
        },
        as_json=arguments.json,
    )
    return 0


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    ingest = subparsers.add_parser("ingest")
    _add_config_arguments(ingest)
    input_group = ingest.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--message")
    input_group.add_argument("--stdin", action="store_true")
    input_group.add_argument("--event-json", type=Path, metavar="FILE|-")
    ingest.add_argument("--source", default="user")
    ingest.add_argument("--idempotency-key")
    ingest.add_argument("--session-id")
    ingest.add_argument("--turn-id")
    ingest.add_argument(
        "--if-new",
        action="store_true",
        help=(
            "return the existing message instead of storing a duplicate "
            "when a received message in the selected scope already "
            "carries the identical content"
        ),
    )
    ingest.add_argument("--quiet", action="store_true")
    ingest.set_defaults(handler=_ingest)

    inbox = subparsers.add_parser("inbox")
    inbox_commands = inbox.add_subparsers(dest="inbox_command", required=True)
    inbox_list = _scope_parser(inbox_commands, "list")
    inbox_list.add_argument("--limit", type=int, default=20)
    inbox_list.add_argument("--include-content", action="store_true")
    inbox_list.set_defaults(handler=_inbox_list)
    inbox_claim = _scope_parser(inbox_commands, "claim")
    inbox_claim.add_argument("message_id", nargs="?")
    inbox_claim.add_argument("--owner")
    inbox_claim.add_argument("--lease-seconds", type=int)
    inbox_claim.set_defaults(handler=_inbox_claim)
    inbox_apply = _scope_parser(inbox_commands, "apply")
    inbox_apply.add_argument("message_id")
    inbox_apply.add_argument("--effects", type=Path, required=True)
    inbox_apply.add_argument("--claim", required=True)
    inbox_apply.set_defaults(handler=_inbox_apply)
    for command, disposition in (
        ("needs-input", "needs_input"),
        ("fail", "failed"),
    ):
        inbox_dispose = _scope_parser(inbox_commands, command)
        inbox_dispose.add_argument("message_id")
        inbox_dispose.add_argument("--claim", required=True)
        inbox_dispose.add_argument("--reason", required=True)
        inbox_dispose.set_defaults(
            handler=_inbox_dispose,
            disposition=disposition,
        )
