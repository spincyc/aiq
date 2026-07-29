from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib import resources
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence

from aiq import __version__
from aiq.capabilities import list_capabilities, show_capability
from aiq.config import Config, ConfigError, resolve_config
from aiq.events import EVENT_JSON_MAX_BYTES, EventError, parse_event_json
from aiq.integrations import claude as claude_integration
from aiq.integrations import codex as codex_integration
from aiq.integrations._hooks import HookIntegrationError
from aiq.journal import (
    JournalError,
    check_journal,
    create_snapshot,
    ingest_message,
    initialize_journal,
    list_inbox,
    resolve_scope,
)
from aiq.privacy import (
    destroy_journal,
    export_journal,
    plan_journal_destroy,
)
from aiq.queue import (
    EFFECT_DOCUMENT_MAX_BYTES,
    MESSAGE_STATES,
    TASK_STATES,
    apply_effects,
    claim_message,
    claim_next_tasks,
    dispose_message,
    list_tasks,
    next_tasks,
    parse_effect_document,
    read_status,
    release_claim,
    show_task,
)


PROTOCOL_VERSION = 1
MESSAGE_INPUT_MAX_BYTES = 1_048_576

_TASK_SUMMARY_FIELDS = (
    "task_id",
    "revision",
    "state",
    "priority",
    "title",
    "blocked_by",
    "waiting_on",
)
_TASK_DETAIL_FIELDS = (
    *_TASK_SUMMARY_FIELDS,
    "objective",
    "parent_task_id",
    "dependencies",
    "reason",
    "superseded_by_task_id",
    "created_at",
    "created_by_message_id",
)
_MESSAGE_SUMMARY_FIELDS = (
    "message_id",
    "received_at",
    "source",
    "content_sha256",
    "session_id",
    "turn_id",
    "cwd",
    "state",
    "lease_status",
)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if _invocation_wants_json(sys.argv[1:]):
            _emit_error("invalid_argument", message, as_json=True)
        else:
            print(f"{self.prog}: {_single_line(message)}", file=sys.stderr)
        raise SystemExit(2)


def _invocation_wants_json(arguments: Sequence[str]) -> bool:
    if "--json" in arguments or os.environ.get("AIQ_OUTPUT") == "json":
        return True
    command = next(
        (value for value in arguments if not value.startswith("-")),
        None,
    )
    if command not in {
        "claim",
        "config",
        "inbox",
        "ingest",
        "journal",
        "queue",
        "status",
        "task",
    }:
        return False
    cwd = Path.cwd()
    for index, value in enumerate(arguments):
        if value == "--cwd" and index + 1 < len(arguments):
            cwd = Path(arguments[index + 1])
        elif value.startswith("--cwd="):
            cwd = Path(value.partition("=")[2])
    options: dict[str, Any] = {"cwd": cwd}
    if "--no-repo-config" in arguments:
        options["repo_path"] = None
    try:
        return resolve_config(**options).output == "json"
    except (ConfigError, OSError):
        return False


def _single_line(value: str) -> str:
    return "".join(
        character
        if character.isprintable() and character not in {"\t", "\r", "\n"}
        else f"\\u{ord(character):04x}"
        for character in value
    )


def _versioned(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, **payload}


def _emit(payload: Mapping[str, Any] | str, *, as_json: bool) -> None:
    if as_json:
        if not isinstance(payload, Mapping):
            payload = {"value": payload}
        print(
            json.dumps(
                _versioned(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    if isinstance(payload, str):
        print(_single_line(payload))
        return
    for key, value in payload.items():
        if isinstance(value, str):
            value = _single_line(value)
        print(f"{key}\t{value}")


def _emit_error(code: str, message: str, *, as_json: bool) -> None:
    safe_message = _single_line(message)
    if as_json:
        print(
            json.dumps(
                {
                    "code": code,
                    "error": safe_message,
                    "status": "error",
                    "v": PROTOCOL_VERSION,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
    else:
        print(f"aiq: {safe_message}", file=sys.stderr)


def _read_stdin_bounded(maximum_bytes: int, *, label: str) -> bytes:
    data = sys.stdin.buffer.read(maximum_bytes + 1)
    if len(data) > maximum_bytes:
        raise JournalError(f"{label} exceeds {maximum_bytes} bytes")
    return data


def _read_file_bounded(path: Path, maximum_bytes: int, *, label: str) -> bytes:
    with path.open("rb") as input_file:
        data = input_file.read(maximum_bytes + 1)
    if len(data) > maximum_bytes:
        raise JournalError(f"{label} exceeds {maximum_bytes} bytes")
    return data


def _decode_utf8(data: bytes, *, label: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise JournalError(f"{label} is not valid UTF-8") from error


def _read_text_argument(
    path: Path,
    maximum_bytes: int,
    *,
    label: str,
) -> str:
    data = (
        _read_stdin_bounded(maximum_bytes, label=label)
        if os.fspath(path) == "-"
        else _read_file_bounded(path, maximum_bytes, label=label)
    )
    return _decode_utf8(data, label=label)


def _add_config_arguments(
    parser: argparse.ArgumentParser,
    *,
    operational: bool = True,
) -> None:
    parser.add_argument(
        "--scope",
        choices=("auto", "repo", "user", "agent-root"),
        default=None,
    )
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--agent-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--no-repo-config", action="store_true")
    parser.add_argument("--json", action="store_true")
    if not operational:
        parser.add_argument("--owner")
        parser.add_argument("--lease-seconds", type=int)
        parser.add_argument("--snapshot-keep", type=int)


def _config_cli_values(arguments: argparse.Namespace) -> dict[str, object]:
    values: dict[str, object] = {
        "scope": getattr(arguments, "scope", None),
        "owner": getattr(arguments, "owner", None),
        "lease_seconds": getattr(arguments, "lease_seconds", None),
        "snapshot_keep": getattr(arguments, "snapshot_keep", None),
        "output": None,
    }
    return values


def _resolve_config(arguments: argparse.Namespace) -> Config:
    explicit_json = getattr(arguments, "json", False)
    requested_scope = getattr(arguments, "scope", None)
    options: dict[str, Any] = {
        "cwd": arguments.cwd,
        "cli": _config_cli_values(arguments),
    }
    if requested_scope == "agent-root":
        options["cli"]["scope"] = None
    if getattr(arguments, "no_repo_config", False):
        options["repo_path"] = None
    config = resolve_config(**options)
    arguments.effective_config = config
    arguments.scope = (
        "agent-root" if requested_scope == "agent-root" else config.scope
    )
    arguments.json = explicit_json or config.output == "json"
    return config


def _prepare_config(arguments: argparse.Namespace) -> None:
    if getattr(arguments, "load_config", False):
        _resolve_config(arguments)


def _scope(arguments: argparse.Namespace, *, cwd: Path | None = None):
    return resolve_scope(
        arguments.scope,
        cwd=cwd or arguments.cwd,
        agent_root=arguments.agent_root,
    )


def _task_summary(task: Mapping[str, Any]) -> dict[str, Any]:
    return {field: task[field] for field in _TASK_SUMMARY_FIELDS}


def _task_detail(task: Mapping[str, Any]) -> dict[str, Any]:
    return {field: task.get(field) for field in _TASK_DETAIL_FIELDS}


def _message_summary(
    message: Mapping[str, Any],
    *,
    include_content: bool,
) -> dict[str, Any]:
    result = {field: message.get(field) for field in _MESSAGE_SUMMARY_FIELDS}
    if include_content:
        result["content"] = message.get("content")
    return result


def _timestamp_from_microseconds(value: int) -> str:
    return (
        datetime.fromtimestamp(value / 1_000_000, tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _claim_public(claim: Mapping[str, Any]) -> dict[str, Any]:
    expires_at = claim.get("expires_at")
    if expires_at is None and isinstance(claim.get("expires_at_us"), int):
        expires_at = _timestamp_from_microseconds(claim["expires_at_us"])
    return {
        "claim_id": claim["claim_id"],
        "resource_kind": claim["resource_kind"],
        "resource_id": claim["resource_id"],
        "owner_id": claim["owner_id"],
        "basis_revision": claim.get("basis_revision"),
        "expires_at": expires_at,
    }


def _application_public(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": result["status"],
        "message_id": result["message_id"],
        "effects_sha256": result["effects_sha256"],
        "aliases": result["aliases"],
        "tasks": [_task_summary(task) for task in result["tasks"]],
        "replayed": result["replayed"],
    }


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


def _config_show(arguments: argparse.Namespace) -> int:
    config = _resolve_config(arguments)
    _emit(
        config.to_dict(include_sources=arguments.sources),
        as_json=arguments.json,
    )
    return 0


def _config_check(arguments: argparse.Namespace) -> int:
    _resolve_config(arguments)
    _emit({"status": "ok"}, as_json=arguments.json)
    return 0


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
        )
    if not arguments.quiet:
        _emit(
            {
                "message_id": result.message_id,
                "state": result.state,
                "created": result.created,
                "scope": result.scope.to_dict(),
            },
            as_json=arguments.json,
        )
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


def _task_list(arguments: argparse.Namespace) -> int:
    tasks = [
        _task_summary(task)
        for task in list_tasks(
            _scope(arguments),
            states=set(arguments.state) if arguments.state else None,
            limit=arguments.limit,
        )
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


def _task_show(arguments: argparse.Namespace) -> int:
    detail = _task_detail(show_task(_scope(arguments), arguments.task_id))
    _emit({"task": detail}, as_json=arguments.json)
    return 0


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
    return 0


def _capability_list(arguments: argparse.Namespace) -> int:
    capabilities = list_capabilities()
    if arguments.json:
        _emit({"capabilities": capabilities}, as_json=True)
        return 0
    for capability in capabilities:
        print(
            f"{capability['id']}\tv{capability['version']}\t"
            f"{capability['purpose']}"
        )
    return 0


def _capability_show(arguments: argparse.Namespace) -> int:
    capability = show_capability(arguments.capability_id)
    if arguments.json:
        _emit(capability, as_json=True)
    else:
        print(
            json.dumps(
                capability,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    return 0


_INTEGRATION_MODULES = {
    "claude": claude_integration,
    "codex": codex_integration,
}
_INTEGRATION_CHOICES = tuple(sorted(_INTEGRATION_MODULES))


def _integration_list(arguments: argparse.Namespace) -> int:
    integrations = [
        {
            "id": integration_id,
            "purpose": module.PURPOSE,
            "version": module.CONTRACT_VERSION,
        }
        for integration_id, module in sorted(_INTEGRATION_MODULES.items())
    ]
    integrations.append(
        {
            "id": "generic",
            "purpose": "Ingest canonical provider-neutral event JSON.",
            "version": 1,
        }
    )
    if arguments.json:
        _emit({"integrations": integrations}, as_json=True)
    else:
        for integration in integrations:
            print(f"{integration['id']}\t{integration['purpose']}")
    return 0


def _invoked_console_launcher() -> Path | None:
    invocation = Path(sys.argv[0])
    if invocation.name != "aiq":
        return None
    candidate = Path(os.path.abspath(os.fspath(invocation)))
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        return None
    return candidate


def _invoked_python_executable() -> Path:
    return Path(os.path.abspath(sys.executable))


def _integration_plan(arguments: argparse.Namespace) -> int:
    _emit(
        _INTEGRATION_MODULES[arguments.integration_id].plan_integration(
            launcher=arguments.launcher,
            invoked_launcher=_invoked_console_launcher(),
            python_executable=_invoked_python_executable(),
            git_executable=arguments.git_executable,
            repair=arguments.repair,
        ),
        as_json=arguments.json,
    )
    return 0


def _integration_install(arguments: argparse.Namespace) -> int:
    _emit(
        _INTEGRATION_MODULES[arguments.integration_id].install_integration(
            launcher=arguments.launcher,
            invoked_launcher=_invoked_console_launcher(),
            python_executable=_invoked_python_executable(),
            git_executable=arguments.git_executable,
            repair=arguments.repair,
            plan_token=arguments.plan_token,
        ),
        as_json=arguments.json,
    )
    return 0


def _integration_check(arguments: argparse.Namespace) -> int:
    _emit(
        _INTEGRATION_MODULES[arguments.integration_id].check_integration(
            launcher=arguments.launcher,
            invoked_launcher=_invoked_console_launcher(),
            python_executable=_invoked_python_executable(),
            git_executable=arguments.git_executable,
        ),
        as_json=arguments.json,
    )
    return 0


def _integration_uninstall(arguments: argparse.Namespace) -> int:
    _emit(
        _INTEGRATION_MODULES[arguments.integration_id].uninstall_integration(),
        as_json=arguments.json,
    )
    return 0


def _integration_print(arguments: argparse.Namespace) -> int:
    if arguments.integration_id == "agents":
        guidance = (
            resources.files("aiq._resources")
            .joinpath("AGENTS.md")
            .read_text(encoding="utf-8")
        )
        if arguments.json:
            _emit(
                {"artifact": "agents", "content": guidance},
                as_json=True,
            )
        else:
            sys.stdout.write(guidance)
        return 0

    module = _INTEGRATION_MODULES[arguments.integration_id]
    rendered = module.print_integration(
        launcher=arguments.launcher,
        invoked_launcher=_invoked_console_launcher(),
        python_executable=_invoked_python_executable(),
        git_executable=arguments.git_executable,
    )
    if arguments.json:
        _emit(
            {
                "integration": arguments.integration_id,
                "fragment": json.loads(rendered),
            },
            as_json=True,
        )
    else:
        sys.stdout.write(rendered)
    return 0


def _integration_receive(arguments: argparse.Namespace) -> int:
    module = _INTEGRATION_MODULES[arguments.integration]
    integration_id = (
        module.INTEGRATION_ID
        if arguments.integration_id is None
        else arguments.integration_id
    )
    return module.receive_hook_main(
        integration_id=integration_id,
        git_executable=arguments.git_executable,
    )


def _scope_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name)
    _add_config_arguments(parser)
    parser.set_defaults(load_config=True)
    return parser


def _add_user_selector(
    parser: argparse.ArgumentParser,
    *,
    required: bool = False,
) -> None:
    parser.add_argument(
        "--user",
        action="store_true",
        required=required,
        help="operate on the supported user-level integration",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="aiq")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    config = commands.add_parser("config")
    config_commands = config.add_subparsers(
        dest="config_command",
        required=True,
    )
    config_show = config_commands.add_parser("show")
    _add_config_arguments(config_show, operational=False)
    config_show.add_argument("--sources", action="store_true")
    config_show.set_defaults(handler=_config_show)
    config_check = config_commands.add_parser("check")
    _add_config_arguments(config_check, operational=False)
    config_check.set_defaults(handler=_config_check)

    journal = commands.add_parser("journal")
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

    ingest = commands.add_parser("ingest")
    _add_config_arguments(ingest)
    input_group = ingest.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--message")
    input_group.add_argument("--stdin", action="store_true")
    input_group.add_argument("--event-json", type=Path, metavar="FILE|-")
    ingest.add_argument("--source", default="user")
    ingest.add_argument("--idempotency-key")
    ingest.add_argument("--session-id")
    ingest.add_argument("--turn-id")
    ingest.add_argument("--quiet", action="store_true")
    ingest.set_defaults(handler=_ingest)

    inbox = commands.add_parser("inbox")
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

    task = commands.add_parser("task")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_list = _scope_parser(task_commands, "list")
    task_list.add_argument("--state", action="append", choices=TASK_STATES)
    task_list.add_argument("--limit", type=int, default=100)
    task_list.set_defaults(handler=_task_list)
    task_show = _scope_parser(task_commands, "show")
    task_show.add_argument("task_id")
    task_show.set_defaults(handler=_task_show)

    queue = commands.add_parser("queue")
    queue_commands = queue.add_subparsers(dest="queue_command", required=True)
    queue_peek = _scope_parser(queue_commands, "peek")
    queue_peek.add_argument("--limit", type=int, default=1)
    queue_peek.set_defaults(handler=_queue_peek)
    queue_next = _scope_parser(queue_commands, "next")
    queue_next.add_argument("--owner")
    queue_next.add_argument("--lease-seconds", type=int)
    queue_next.add_argument("--limit", type=int, default=1)
    queue_next.set_defaults(handler=_queue_next)

    claim = commands.add_parser("claim")
    claim_commands = claim.add_subparsers(dest="claim_command", required=True)
    claim_release = _scope_parser(claim_commands, "release")
    claim_release.add_argument("claim_id")
    claim_release.set_defaults(handler=_claim_release)

    status = commands.add_parser("status")
    _add_config_arguments(status)
    status.set_defaults(load_config=True, handler=_status)

    capability = commands.add_parser("capability")
    capability_commands = capability.add_subparsers(
        dest="capability_command",
        required=True,
    )
    capability_list = capability_commands.add_parser("list")
    capability_list.add_argument("--json", action="store_true")
    capability_list.set_defaults(handler=_capability_list)
    capability_show = capability_commands.add_parser("show")
    capability_show.add_argument("capability_id")
    capability_show.add_argument("--json", action="store_true")
    capability_show.set_defaults(handler=_capability_show)

    integration = commands.add_parser("integration")
    integration_commands = integration.add_subparsers(
        dest="integration_command",
        required=True,
    )
    integration_list = integration_commands.add_parser("list")
    integration_list.add_argument("--json", action="store_true")
    integration_list.set_defaults(handler=_integration_list)
    integration_plan = integration_commands.add_parser("plan")
    integration_plan.add_argument("integration_id", choices=_INTEGRATION_CHOICES)
    _add_user_selector(integration_plan, required=True)
    integration_plan.add_argument("--launcher", type=Path)
    integration_plan.add_argument("--git-executable", type=Path)
    integration_plan.add_argument("--repair", action="store_true")
    integration_plan.add_argument("--json", action="store_true")
    integration_plan.set_defaults(handler=_integration_plan)
    integration_install = integration_commands.add_parser("install")
    integration_install.add_argument(
        "integration_id",
        choices=_INTEGRATION_CHOICES,
    )
    _add_user_selector(integration_install, required=True)
    integration_install.add_argument("--launcher", type=Path)
    integration_install.add_argument("--git-executable", type=Path)
    integration_install.add_argument("--repair", action="store_true")
    integration_install.add_argument("--plan-token")
    integration_install.add_argument("--json", action="store_true")
    integration_install.set_defaults(handler=_integration_install)
    integration_check = integration_commands.add_parser("check")
    integration_check.add_argument("integration_id", choices=_INTEGRATION_CHOICES)
    _add_user_selector(integration_check, required=True)
    integration_check.add_argument("--launcher", type=Path)
    integration_check.add_argument("--git-executable", type=Path)
    integration_check.add_argument("--json", action="store_true")
    integration_check.set_defaults(handler=_integration_check)
    integration_uninstall = integration_commands.add_parser("uninstall")
    integration_uninstall.add_argument(
        "integration_id",
        choices=_INTEGRATION_CHOICES,
    )
    _add_user_selector(integration_uninstall, required=True)
    integration_uninstall.add_argument("--json", action="store_true")
    integration_uninstall.set_defaults(handler=_integration_uninstall)
    integration_print = integration_commands.add_parser("print")
    integration_print.add_argument(
        "integration_id",
        choices=("agents", *_INTEGRATION_CHOICES),
    )
    _add_user_selector(integration_print)
    integration_print.add_argument("--launcher", type=Path)
    integration_print.add_argument("--git-executable", type=Path)
    integration_print.add_argument("--json", action="store_true")
    integration_print.set_defaults(handler=_integration_print)
    integration_receive = integration_commands.add_parser("receive")
    integration_receive.add_argument("integration", choices=_INTEGRATION_CHOICES)
    integration_receive.add_argument("--integration-id")
    integration_receive.add_argument(
        "--git-executable",
        type=Path,
        required=True,
    )
    integration_receive.set_defaults(handler=_integration_receive)

    return parser


def _classify_journal_error(error: JournalError) -> tuple[str, int]:
    message = str(error).lower()
    if "not found" in message or "does not exist" in message:
        return "not_found", 3
    if "expired" in message:
        return "claim_expired", 4
    if "revision" in message and (
        "changed" in message or "stale" in message or "expected" in message
    ):
        return "revision_conflict", 4
    if "claim" in message and (
        "mismatch" in message
        or "does not match" in message
        or "not active" in message
    ):
        return "claim_mismatch", 4
    if "not claimable" in message or "not ready" in message:
        return "not_claimable", 4
    if "integrity" in message or "foreign-key" in message:
        return "integrity_failed", 5
    if "schema" in message and (
        "unsupported" in message
        or "newer" in message
        or "incompatible" in message
    ):
        return "schema_incompatible", 5
    if any(
        marker in message
        for marker in (
            "invalid task id",
            "invalid claim id",
            "limit must",
            "limit must be",
            "must be positive",
            "unsupported task state",
        )
    ):
        return "invalid_argument", 2
    if any(
        marker in message
        for marker in (
            "idempotency key",
            "confirmation",
            "transition",
            "state",
            "dependency",
            "supersed",
            "already ",
        )
    ):
        return "state_conflict", 4
    if any(
        marker in message
        for marker in (
            "document",
            "invalid json",
            "input",
            "must be",
            "exceeds",
            "unknown keys",
            "missing keys",
        )
    ):
        return "invalid_document", 2
    if any(
        marker in message
        for marker in (
            "sqlite",
            "wal",
            "git is unavailable",
            "git could not resolve repository scope",
            "git returned an empty repository path",
            "not inside a git repository",
            "unsupported journal scope",
        )
    ):
        return "unsupported_environment", 6
    return "state_conflict", 4


def _classify_error(error: Exception) -> tuple[str, int]:
    if isinstance(error, ConfigError):
        return "invalid_config", 2
    if isinstance(error, EventError):
        return "invalid_document", 2
    if isinstance(error, HookIntegrationError):
        message = str(error).lower()
        if "launcher must be an absolute path" in message:
            return "invalid_argument", 2
        if (
            "git executable must be an absolute path" in message
            or "git executable path contains control characters" in message
            or "python executable must be an absolute path" in message
            or "python executable path contains control characters" in message
        ):
            return "invalid_argument", 2
        if "git executable" in message or "python executable" in message:
            return "unsupported_environment", 6
        if "launcher" in message and (
            "unavailable" in message
            or "cannot find" in message
            or "cannot determine" in message
        ):
            return "unsupported_environment", 6
        return "integration_drift", 6
    if isinstance(error, JournalError):
        return _classify_journal_error(error)
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError, ValueError)):
        return "invalid_document", 2
    if isinstance(error, OSError):
        return "io_error", 6
    if isinstance(error, sqlite3.DatabaseError):
        return "integrity_failed", 5
    return "internal_error", 70


def main(argv: Sequence[str] | None = None) -> int:
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
        _emit_error(
            code,
            str(error),
            as_json=getattr(arguments, "json", False)
            or os.environ.get("AIQ_OUTPUT") == "json",
        )
        return exit_code
    except Exception as error:
        _emit_error(
            "internal_error",
            str(error) or error.__class__.__name__,
            as_json=getattr(arguments, "json", False)
            or os.environ.get("AIQ_OUTPUT") == "json",
        )
        return 70
