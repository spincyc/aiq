from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from aiq.config import Config, ConfigError, resolve_config
from aiq.journal import JournalError, resolve_scope


PROTOCOL_VERSION = 1


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if _error_json(None, sys.argv[1:]):
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
    if command not in CONFIG_OUTPUT_COMMANDS:
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


def _explicit_json(arguments: argparse.Namespace) -> bool:
    """True when JSON output was selected by --json or AIQ_OUTPUT=json.

    Configuration-selected output is not consulted here; for commands
    that load configuration, _resolve_config folds it into
    ``arguments.json`` before handlers run.
    """
    return bool(
        getattr(arguments, "json", False)
        or os.environ.get("AIQ_OUTPUT") == "json"
    )


def _error_json(
    arguments: argparse.Namespace | None,
    invocation: Sequence[str],
) -> bool:
    """Resolve JSON selection for one error envelope.

    Explicit --json or AIQ_OUTPUT wins; when parsing or configuration
    resolution failed before ``arguments.json`` could fold in the
    configured output, fall back to re-resolving configuration from the
    raw invocation for commands that honor configured output.
    """
    if arguments is not None:
        if _explicit_json(arguments):
            return True
        config = getattr(arguments, "effective_config", None)
        if config is not None:
            return config.output == "json"
    return _invocation_wants_json(invocation)


def _single_line(value: str) -> str:
    return "".join(
        character
        if character.isprintable() and character not in {"\t", "\r", "\n"}
        else f"\\u{ord(character):04x}"
        for character in value
    )


def _versioned(payload: Mapping[str, Any]) -> dict[str, Any]:
    if "v" in payload and payload["v"] != PROTOCOL_VERSION:
        raise AssertionError(
            f"internal error: payload version {payload['v']!r} conflicts "
            f"with protocol envelope version {PROTOCOL_VERSION}; a "
            "diverging payload contract version requires a new envelope "
            "field"
        )
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


# Single source for the top-level commands whose output format may come
# from resolved configuration. `doctor` and `ingest` resolve configuration
# inside their handlers (doctor reports configuration failures as checks;
# ingest resolves after the event supplies the effective cwd); every other
# member registers load_config=True on its parser so _prepare_config
# resolves configuration before the handler runs.
CONFIG_OUTPUT_COMMANDS = frozenset(
    {
        "claim",
        "config",
        "dequeue",
        "doctor",
        "enqueue",
        "inbox",
        "ingest",
        "journal",
        "list",
        "queue",
        "reconcile",
        "report",
        "status",
        "task",
    }
)
