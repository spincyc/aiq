"""Shared engine for reversible user-level hook integrations.

Each adapter owns one marked hook group per managed event inside one JSON
configuration file. The engine provides the filesystem safety, manifest
ownership, drift detection, and lifecycle mechanics; adapters supply the
target file, the ordered event-to-group definition, and payload validation.

The receive boundary dispatches on the delivered ``hook_event_name``:
``UserPromptSubmit`` events are captured into the journal, and ``Stop``
events run a read-only completion gate that blocks stopping (exit 2) while
runnable work remains. The gate fails open: any gate-path error exits 0
with a single stderr diagnostic, because an AIQ defect must never block
the host from stopping.

Installed hooks never create journal storage. Repo-scope capture is
opt-in by journal presence: ``aiq journal init --scope repo`` is the
per-repository opt-in act and ``aiq journal destroy`` the opt-out, so a
repository without an initialized journal is skipped silently. User
scope (a working directory outside any Git repository) keeps
auto-initialization, as do explicit ``aiq ingest`` and the generic
integration.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import sys
import time
from typing import Any, Callable, Mapping

from aiq.journal import JournalError, ingest_message, resolve_scope


HOOK_INPUT_MAX_BYTES = 1_048_576
TARGET_MAX_BYTES = 1_048_576
PROMPT_EVENT = "UserPromptSubmit"
STOP_EVENT = "Stop"

# Installed hooks declare a 10s host timeout, and a hook the host kills
# reports nothing: the message would be lost silently. Capture therefore
# waits only this long for a journal lock, leaving room to fail visibly
# with a diagnostic while the host is still listening.
CAPTURE_LOCK_TIMEOUT_SECONDS = 5.0


class HookIntegrationError(JournalError):
    """A hook integration cannot be inspected or changed safely."""


def single_line(value: str) -> str:
    return "".join(
        character
        if character.isprintable() and character not in {"\t", "\r", "\n"}
        else f"\\u{ord(character):04x}"
        for character in value
    )


def _payload_event(payload: bytes) -> str | None:
    """Best-effort ``hook_event_name`` of one raw payload, or ``None``."""

    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if isinstance(document, dict):
        event = document.get("hook_event_name")
        if isinstance(event, str):
            return event
    return None


def run_receive_hook_main(
    receive: Callable[[bytes], Any],
    *,
    error_class: type[JournalError],
    input_label: str,
    failure_exit_code: int,
    gate: Callable[[bytes], tuple[bool, str] | None] | None = None,
    input_stream: Any = None,
    error_stream: Any = None,
) -> int:
    """Run one stdout-silent hook boundary, dispatched on the event name.

    A ``Stop`` payload runs ``gate`` when one is supplied: a returned
    ``(True, line)`` block exits 2 with that single stderr line (the host
    feeds it back to the model and continues), ``(False, line)`` exits 0
    with that single stderr line as a non-blocking notice (whether the
    host displays it is host-dependent), ``None`` exits 0 silently, and
    any gate error exits 0 with a single stderr diagnostic so an AIQ
    defect never blocks stopping. Every other payload runs ``receive``; a
    capture failure exits ``failure_exit_code`` with a single stderr
    diagnostic. A payload that cannot be read or parsed enough to
    identify its event follows the capture failure path, which also never
    blocks stopping.
    """

    source = sys.stdin.buffer if input_stream is None else input_stream
    errors = sys.stderr if error_stream is None else error_stream
    try:
        payload = source.read(HOOK_INPUT_MAX_BYTES + 1)
        if len(payload) > HOOK_INPUT_MAX_BYTES:
            raise error_class(
                f"{input_label} exceeds {HOOK_INPUT_MAX_BYTES} bytes"
            )
    except Exception as error:
        errors.write(f"AIQ prompt capture failed: {single_line(str(error))}\n")
        errors.flush()
        return failure_exit_code
    if gate is not None and _payload_event(payload) == STOP_EVENT:
        try:
            outcome = gate(payload)
        except Exception as error:
            errors.write(
                f"AIQ completion gate skipped: {single_line(str(error))}\n"
            )
            errors.flush()
            return 0
        if outcome is None:
            return 0
        blocking, line = outcome
        errors.write(f"{single_line(line)}\n")
        errors.flush()
        return 2 if blocking else 0
    try:
        receive(payload)
        return 0
    except Exception as error:
        errors.write(f"AIQ prompt capture failed: {single_line(str(error))}\n")
        errors.flush()
        return failure_exit_code


@dataclass(frozen=True)
class ReceivePayloadSpec:
    """Adapter-specific shape of one received hook payload.

    ``injected_prefixes`` and ``injected_wrappers`` declare the adapter's
    harness-injected prompt markers. Some hosts deliver machine-generated
    content (background-agent notifications, harness reminders) through
    the same prompt channel as typed user input; such content must not be
    captured as a user message the agent is then obligated to settle. The
    skip rule is deliberately conservative — only a prompt that is
    unambiguously harness-injected is skipped, never anything a human
    plausibly typed: after stripping surrounding whitespace, the prompt
    either starts with an ``injected_prefixes`` entry (an opening tag such
    as ``<task-notification``) or is one whole ``<tag>…</tag>`` block for
    an ``injected_wrappers`` tag name. A prompt that merely mentions a
    marker mid-string is captured normally.
    """

    source: str
    input_label: str
    event: str
    required_fields: tuple[str, ...]
    turn_field: str
    turn_required: bool
    injected_prefixes: tuple[str, ...] = ()
    injected_wrappers: tuple[str, ...] = ()


def _is_injected_prompt(prompt: str, receive: ReceivePayloadSpec) -> bool:
    """Report whether one prompt matches the adapter's injected markers."""

    stripped = prompt.strip()
    if receive.injected_prefixes and stripped.startswith(
        receive.injected_prefixes
    ):
        return True
    for tag in receive.injected_wrappers:
        opening, closing = f"<{tag}>", f"</{tag}>"
        if not (
            stripped.startswith(opening) and stripped.endswith(closing)
        ):
            continue
        # Exactly one whole block: the closing tag may appear only at the
        # very end, else user content could hide between wrapper blocks.
        if stripped.index(closing) == len(stripped) - len(closing):
            return True
    return False


@dataclass(frozen=True)
class HookIntegrationSpec:
    """Everything adapter-specific the shared engine needs.

    ``events`` is the ordered tuple of managed hook event names and
    ``hook_groups`` returns the owned group for each of them, keyed by
    event name in the same order. Both the target configuration and the
    manifest manage one owned group per event under one integration id.
    """

    integration: str
    integration_id: str
    error_class: type[JournalError]
    display_name: str
    target_label: str
    state_subdirectory: str
    target_path: Callable[[Mapping[str, str]], Path]
    hook_groups: Callable[[Path, Path], dict[str, dict[str, Any]]]
    events: tuple[str, ...] = (PROMPT_EVENT,)
    created_file_preamble: dict[str, Any] = field(default_factory=dict)
    preflight: Callable[
        [Mapping[str, str], Path, dict[str, Any]],
        dict[str, str] | None,
    ] = lambda environment, target, document: None
    receive: ReceivePayloadSpec | None = None


def home_directory(
    environment: Mapping[str, str],
    *,
    error_class: type[JournalError],
) -> Path:
    configured = environment.get("HOME")
    if not configured:
        return Path.home()
    path = Path(configured)
    if not path.is_absolute():
        raise error_class("HOME must be an absolute path")
    return path


def _state_home(
    environment: Mapping[str, str],
    *,
    error_class: type[JournalError],
) -> Path:
    configured = environment.get("XDG_STATE_HOME")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise error_class("XDG_STATE_HOME must be an absolute path")
        return path
    return home_directory(environment, error_class=error_class) / ".local" / "state"


def _integration_state_directory(
    spec: HookIntegrationSpec,
    environment: Mapping[str, str],
    target: Path,
) -> Path:
    target_id = hashlib.sha256(os.fsencode(target)).hexdigest()[:16]
    return (
        _state_home(environment, error_class=spec.error_class)
        / "aiq"
        / "integrations"
        / spec.state_subdirectory
        / target_id
    )


def integration_present(
    spec: HookIntegrationSpec,
    *,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Report whether the adapter's target or AIQ-owned state exists."""

    effective_environment = os.environ if environment is None else environment
    target = spec.target_path(effective_environment)
    state_directory = _integration_state_directory(
        spec,
        effective_environment,
        target,
    )
    return target.exists() or state_directory.exists()


def installed_manifest(
    spec: HookIntegrationSpec,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Return the validated installed user-level manifest, if one exists."""

    effective_environment = os.environ if environment is None else environment
    target = spec.target_path(effective_environment)
    manifest = _read_manifest(
        spec,
        _integration_state_directory(spec, effective_environment, target),
        target=target,
    )
    if manifest is None or manifest.get("status") != "installed":
        return None
    return manifest


def _ensure_private_directory(spec: HookIntegrationSpec, path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    status = path.lstat()
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
        raise spec.error_class(
            f"integration state directory is unsafe: {path}"
        )
    path.chmod(0o700)


def read_bounded_with_status(
    path: Path,
    maximum_bytes: int,
    *,
    label: str,
    error_class: type[JournalError],
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise error_class(
            f"{label} is not a safe regular file: {path}"
        ) from error
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.getuid()
            or status.st_nlink != 1
        ):
            raise error_class(
                f"{label} is not a safe regular file: {path}"
            )
        if status.st_size > maximum_bytes:
            raise error_class(f"{label} exceeds {maximum_bytes} bytes")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum_bytes:
            raise error_class(f"{label} exceeds {maximum_bytes} bytes")
        return data, status
    finally:
        os.close(descriptor)


def read_bounded(
    path: Path,
    maximum_bytes: int,
    *,
    label: str,
    error_class: type[JournalError],
) -> bytes:
    return read_bounded_with_status(
        path,
        maximum_bytes,
        label=label,
        error_class=error_class,
    )[0]


def _atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (
        f".{path.name}.aiq-{os.getpid()}-{time.time_ns()}"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=False) as output_file:
            output_file.write(data)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _integration_lock(spec: HookIntegrationSpec, state_directory: Path):
    _ensure_private_directory(spec, state_directory)
    lock_path = state_directory / "integration.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.getuid()
            or status.st_nlink != 1
        ):
            raise spec.error_class(
                f"integration lock is unsafe: {lock_path}"
            )
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def executable_path(
    candidate: str | Path | None,
    *,
    error_class: type[JournalError],
    noun: str,
    flag: str,
    command: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    if candidate is None and command is not None:
        search_path = (
            os.environ.get("PATH", "")
            if environment is None
            else environment.get("PATH", "")
        )
        discovered = (
            None
            if search_path == ""
            else shutil.which(command, path=search_path)
        )
        if discovered is None:
            raise error_class(
                f"cannot determine the {noun}; provide an absolute "
                f"{flag} path"
            )
        candidate = Path(discovered).absolute()
    if candidate is None:
        raise error_class(
            f"cannot determine the {noun}; provide an absolute {flag} path"
        )
    path = Path(candidate)
    if not path.is_absolute():
        raise error_class(f"{noun} must be an absolute path")
    if any(character in os.fspath(path) for character in ("\0", "\r", "\n")):
        raise error_class(f"{noun} path contains control characters")
    try:
        status = path.stat()
    except OSError as error:
        raise error_class(f"{noun} is unavailable: {path}") from error
    if not stat.S_ISREG(status.st_mode) or not os.access(path, os.X_OK):
        raise error_class(f"{noun} is not executable: {path}")
    return path


def launcher_path(
    launcher: str | Path | None,
    *,
    error_class: type[JournalError],
    invoked_launcher: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    candidate = launcher if launcher is not None else invoked_launcher
    return executable_path(
        candidate,
        error_class=error_class,
        noun="AIQ launcher",
        flag="--launcher",
        command="aiq" if candidate is None else None,
        environment=environment,
    )


def git_executable_path(
    git_executable: str | Path | None,
    *,
    error_class: type[JournalError],
    environment: Mapping[str, str] | None = None,
) -> Path:
    return executable_path(
        git_executable,
        error_class=error_class,
        noun="Git executable",
        flag="--git-executable",
        command="git" if git_executable is None else None,
        environment=environment,
    )


def python_executable_path(
    python_executable: str | Path | None,
    *,
    error_class: type[JournalError],
) -> Path:
    candidate = sys.executable if python_executable is None else python_executable
    return executable_path(
        candidate,
        error_class=error_class,
        noun="Python executable",
        flag="--python-executable",
    )


def _command_has_marker(command: str, *, integration_id: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return any(
        token == "--integration-id" and tokens[index + 1] == integration_id
        for index, token in enumerate(tokens[:-1])
    )


def marker_count(group: Any, *, integration_id: str) -> int:
    if not isinstance(group, dict):
        return 0
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        return 0
    return sum(
        1
        for handler in handlers
        if isinstance(handler, dict)
        and isinstance(handler.get("command"), str)
        and _command_has_marker(
            handler["command"],
            integration_id=integration_id,
        )
    )


def _valid_owned_group(group: Any, *, integration_id: str) -> bool:
    handlers = group.get("hooks") if isinstance(group, dict) else None
    return (
        isinstance(group, dict)
        and isinstance(handlers, list)
        and all(isinstance(handler, dict) for handler in handlers)
        and marker_count(group, integration_id=integration_id) == 1
    )


def _manifest_group_mapping(
    spec: HookIntegrationSpec,
    managed_group: Any,
) -> dict[str, dict[str, Any]] | None:
    """Normalize a manifest ``managed_group`` to an event-keyed mapping.

    Manifests written before multi-event support store one bare group
    object; it owns the first (prompt) event. Newer manifests store an
    ordered mapping of event name to owned group. Returns ``None`` when
    neither shape validates.
    """

    if not isinstance(managed_group, dict) or not managed_group:
        return None
    if isinstance(managed_group.get("hooks"), list):
        mapping: dict[str, Any] = {spec.events[0]: managed_group}
    else:
        mapping = managed_group
    if not set(mapping).issubset(set(spec.events)):
        return None
    if not all(
        _valid_owned_group(group, integration_id=spec.integration_id)
        for group in mapping.values()
    ):
        return None
    return {
        event: mapping[event] for event in spec.events if event in mapping
    }


def object_without_duplicates_hook(
    error_class: type[JournalError],
) -> Callable[[list[tuple[str, Any]]], dict[str, Any]]:
    def build(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise error_class(f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    return build


def _decode_hooks(
    spec: HookIntegrationSpec,
    data: bytes,
    *,
    target: Path,
) -> dict[str, Any]:
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise spec.error_class(
            f"{spec.target_label} are not UTF-8: {target}"
        ) from error
    try:
        document = json.loads(
            decoded,
            object_pairs_hook=object_without_duplicates_hook(spec.error_class),
        )
    except json.JSONDecodeError as error:
        raise spec.error_class(
            f"{spec.target_label} are invalid JSON: {target}"
        ) from error
    if not isinstance(document, dict):
        raise spec.error_class(f"{spec.target_label} root must be a JSON object")
    hooks = document.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise spec.error_class(
            f"{spec.display_name} hooks field must be a JSON object"
        )
    if isinstance(hooks, dict):
        for event in spec.events:
            groups = hooks.get(event)
            if groups is not None and not isinstance(groups, list):
                raise spec.error_class(
                    f"{spec.display_name} {event} hooks must be a JSON array"
                )
    return document


def _encode_hooks(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()


def sha256_or_none(data: bytes | None) -> str | None:
    if data is None:
        return None
    return hashlib.sha256(data).hexdigest()


def _manifest_path(state_directory: Path) -> Path:
    return state_directory / "manifest.json"


def _valid_digest(value: Any, *, optional: bool = False) -> bool:
    if optional and value is None:
        return True
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_manifest(
    spec: HookIntegrationSpec,
    manifest: Any,
    *,
    state_directory: Path,
    target: Path,
) -> dict[str, Any]:
    required = {
        "backups",
        "config_sha256",
        "created_containers",
        "created_file",
        "integration",
        "integration_id",
        "git_executable",
        "launcher",
        "managed_group",
        "managed_group_sha256",
        "python_executable",
        "status",
        "target",
        "v",
    }
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise spec.error_class("integration manifest has an invalid schema")
    if (
        type(manifest["v"]) is not int
        or manifest["v"] != 1
        or not isinstance(manifest["status"], str)
        or manifest["status"] not in ("installed", "uninstalled")
        or manifest["integration"] != spec.integration
        or manifest["integration_id"] != spec.integration_id
        or manifest["target"] != os.fspath(target)
        or not isinstance(manifest["created_file"], bool)
    ):
        raise spec.error_class("integration manifest has invalid ownership")
    for field_name, description in (
        ("launcher", "launcher"),
        ("git_executable", "Git executable"),
        ("python_executable", "Python executable"),
    ):
        value = manifest[field_name]
        if (
            not isinstance(value, str)
            or not Path(value).is_absolute()
            or any(character in value for character in ("\0", "\r", "\n"))
        ):
            raise spec.error_class(
                f"integration manifest {description} is invalid"
            )
    containers = manifest["created_containers"]
    if (
        not isinstance(containers, list)
        or not all(isinstance(item, str) for item in containers)
        or len(containers) != len(set(containers))
        or not set(containers).issubset({"hooks", *spec.events})
    ):
        raise spec.error_class(
            "integration manifest created containers are invalid"
        )
    group = manifest["managed_group"]
    if _manifest_group_mapping(spec, group) is None:
        raise spec.error_class("integration manifest owned hook is invalid")
    group_digest = sha256_or_none(
        json.dumps(group, sort_keys=True, separators=(",", ":")).encode()
    )
    if (
        not _valid_digest(manifest["managed_group_sha256"])
        or manifest["managed_group_sha256"] != group_digest
        or not _valid_digest(manifest["config_sha256"], optional=True)
        or (
            manifest["status"] == "installed"
            and manifest["config_sha256"] is None
        )
    ):
        raise spec.error_class("integration manifest digest is invalid")
    backups = manifest["backups"]
    if not isinstance(backups, list):
        raise spec.error_class("integration manifest backups are invalid")
    backup_directory = state_directory / "backups"
    for backup in backups:
        if (
            not isinstance(backup, dict)
            or set(backup) != {"path", "sha256"}
            or not isinstance(backup["path"], str)
            or Path(backup["path"]).parent != backup_directory
            or not _valid_digest(backup["sha256"])
        ):
            raise spec.error_class("integration manifest backup is invalid")
    return manifest


def _read_manifest(
    spec: HookIntegrationSpec,
    state_directory: Path,
    *,
    target: Path,
) -> dict[str, Any] | None:
    try:
        status = state_directory.lstat()
    except FileNotFoundError:
        status = None
    except OSError as error:
        raise spec.error_class(
            f"integration state directory is unsafe: {state_directory}"
        ) from error
    if status is not None and (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.getuid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise spec.error_class(
            f"integration state directory is unsafe: {state_directory}"
        )
    path = _manifest_path(state_directory)
    try:
        data = read_bounded(
            path,
            262_144,
            label="integration manifest",
            error_class=spec.error_class,
        )
    except FileNotFoundError:
        return None
    try:
        manifest = json.loads(
            data,
            object_pairs_hook=object_without_duplicates_hook(spec.error_class),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise spec.error_class("integration manifest is invalid") from error
    return _validate_manifest(
        spec,
        manifest,
        state_directory=state_directory,
        target=target,
    )


def _write_manifest(
    spec: HookIntegrationSpec,
    state_directory: Path,
    manifest: dict[str, Any],
) -> None:
    _ensure_private_directory(spec, state_directory)
    data = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    _atomic_write(_manifest_path(state_directory), data, mode=0o600)


def _load_target(
    spec: HookIntegrationSpec,
    target: Path,
) -> tuple[bytes | None, dict[str, Any], os.stat_result | None]:
    try:
        data, status = read_bounded_with_status(
            target,
            TARGET_MAX_BYTES,
            label=spec.target_label,
            error_class=spec.error_class,
        )
    except FileNotFoundError:
        return None, {}, None
    return data, _decode_hooks(spec, data, target=target), status


def _assert_target_unchanged(
    spec: HookIntegrationSpec,
    target: Path,
    expected: bytes | None,
    expected_status: os.stat_result | None,
) -> None:
    try:
        current, status = read_bounded_with_status(
            target,
            TARGET_MAX_BYTES,
            label=spec.target_label,
            error_class=spec.error_class,
        )
    except FileNotFoundError:
        if expected is None and expected_status is None:
            return
        raise spec.error_class(f"{spec.target_label} changed before mutation")
    if expected is None or expected_status is None:
        raise spec.error_class(f"{spec.target_label} changed before mutation")
    if (
        current != expected
        or status.st_dev != expected_status.st_dev
        or status.st_ino != expected_status.st_ino
    ):
        raise spec.error_class(f"{spec.target_label} changed before mutation")


def _groups(document: dict[str, Any], event: str) -> list[Any]:
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return []
    groups = hooks.get(event)
    return groups if isinstance(groups, list) else []


def _managed_groups(
    spec: HookIntegrationSpec,
    document: dict[str, Any],
    event: str,
) -> list[Any]:
    return [
        group
        for group in _groups(document, event)
        if marker_count(group, integration_id=spec.integration_id)
    ]


def _append_group(
    document: dict[str, Any],
    event: str,
    group: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    result = json.loads(json.dumps(document))
    created: list[str] = []
    hooks = result.get("hooks")
    if hooks is None:
        hooks = {}
        result["hooks"] = hooks
        created.append("hooks")
    groups = hooks.get(event)
    if groups is None:
        groups = []
        hooks[event] = groups
        created.append(event)
    groups.append(group)
    return result, created


def _repair_missing_group(
    document: dict[str, Any],
    event: str,
    manifest_group: dict[str, Any],
    desired_group: dict[str, Any],
) -> tuple[dict[str, Any], list[str], bool]:
    """Replace the manifest-recorded group in place; append only when absent."""

    for index, group in enumerate(_groups(document, event)):
        if group == manifest_group:
            result = json.loads(json.dumps(document))
            result["hooks"][event][index] = desired_group
            return result, [], True
    after_document, created = _append_group(document, event, desired_group)
    return after_document, created, False


def _replace_managed_group(
    spec: HookIntegrationSpec,
    document: dict[str, Any],
    event: str,
    replacement: dict[str, Any],
) -> dict[str, Any]:
    result = json.loads(json.dumps(document))
    groups = result["hooks"][event]
    indexes = [
        index
        for index, group in enumerate(groups)
        if marker_count(group, integration_id=spec.integration_id)
    ]
    if len(indexes) != 1:
        raise spec.error_class(
            f"expected exactly one AIQ {spec.display_name} hook"
        )
    groups[indexes[0]] = replacement
    return result


def render_fragment(
    spec: HookIntegrationSpec,
    *,
    launcher: str | Path | None = None,
    git_executable: str | Path | None = None,
    python_executable: str | Path | None = None,
    invoked_launcher: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Render an externally managed configuration fragment.

    The printed fragment never references the AIQ launcher, so no
    launcher is required or validated here; ``launcher`` and
    ``invoked_launcher`` are accepted only for signature compatibility.
    """

    del launcher, invoked_launcher
    resolved_git_executable = git_executable_path(
        git_executable,
        error_class=spec.error_class,
        environment=environment,
    )
    resolved_python_executable = python_executable_path(
        python_executable,
        error_class=spec.error_class,
    )
    groups = spec.hook_groups(
        resolved_python_executable,
        resolved_git_executable,
    )
    return _encode_hooks(
        {"hooks": {event: [group] for event, group in groups.items()}}
    ).decode("utf-8")


def _build_plan(
    spec: HookIntegrationSpec,
    *,
    launcher: str | Path | None = None,
    git_executable: str | Path | None = None,
    python_executable: str | Path | None = None,
    invoked_launcher: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    repair: bool = False,
) -> dict[str, Any]:
    """Return the exact safe mutation needed for a user-level integration."""

    effective_environment = os.environ if environment is None else environment
    target = spec.target_path(effective_environment)
    state_directory = _integration_state_directory(
        spec,
        effective_environment,
        target,
    )
    result: dict[str, Any] = {
        "v": 1,
        "integration": spec.integration,
        "integration_id": spec.integration_id,
        "target": os.fspath(target),
        "state_directory": os.fspath(state_directory),
        "desired_group": None,
        "status": "unknown",
        "action": "block",
        "blocked_reason": None,
        "changes": [],
        "before_sha256": None,
        "after_sha256": None,
        "plan_token": None,
        "created_file": None,
        "created_containers": None,
    }
    try:
        resolved_launcher = launcher_path(
            launcher,
            error_class=spec.error_class,
            invoked_launcher=invoked_launcher,
            environment=effective_environment,
        )
        resolved_git_executable = git_executable_path(
            git_executable,
            error_class=spec.error_class,
            environment=effective_environment,
        )
        resolved_python_executable = python_executable_path(
            python_executable,
            error_class=spec.error_class,
        )
        before, document, before_status = _load_target(spec, target)
        blocked = spec.preflight(effective_environment, target, document)
        manifest = _read_manifest(spec, state_directory, target=target)
    except JournalError as error:
        result["status"] = "unsafe"
        result["blocked_reason"] = str(error)
        return result

    desired_groups = spec.hook_groups(
        resolved_python_executable,
        resolved_git_executable,
    )
    result.update(
        {
            "desired_group": desired_groups,
            "before_sha256": sha256_or_none(before),
            "_launcher": os.fspath(resolved_launcher),
            "_git_executable": os.fspath(resolved_git_executable),
            "_python_executable": os.fspath(resolved_python_executable),
            "_before": before,
            "_before_status": before_status,
            "_manifest": manifest,
        }
    )
    if blocked is not None:
        result["status"] = blocked["status"]
        result["blocked_reason"] = blocked["blocked_reason"]
        return result

    managed = {
        event: _managed_groups(spec, document, event)
        for event in spec.events
    }
    conflict = any(
        len(managed[event]) > 1
        or sum(
            marker_count(group, integration_id=spec.integration_id)
            for group in _groups(document, event)
        )
        > 1
        for event in spec.events
    )
    if conflict:
        result["status"] = "conflict"
        result["blocked_reason"] = (
            f"multiple AIQ {spec.display_name} hooks are configured"
        )
        return result

    manifest_active = (
        manifest is not None
        and manifest.get("status") == "installed"
        and manifest.get("target") == os.fspath(target)
        and manifest.get("integration_id") == spec.integration_id
    )
    manifest_groups = (
        _manifest_group_mapping(spec, manifest["managed_group"]) or {}
        if manifest_active
        else {}
    )
    manifest_group_mismatch = manifest_active and any(
        managed[event] and manifest_groups.get(event) != managed[event][0]
        for event in spec.events
    )
    if manifest_group_mismatch and not repair:
        result["status"] = "drifted"
        result["blocked_reason"] = (
            f"the integration manifest differs from the configured "
            f"{spec.display_name} hook"
        )
        return result
    any_managed = any(managed[event] for event in spec.events)
    adopt = False
    if any_managed and not manifest_active:
        if not repair:
            result["status"] = "unmanaged"
            result["blocked_reason"] = (
                f"an AIQ-marked {spec.display_name} hook exists without an "
                "active AIQ manifest; rerun with repair to adopt it"
            )
            return result
        adopt = True

    missing_events = [
        event for event in spec.events if not managed[event]
    ]
    mismatched_events = [
        event
        for event in spec.events
        if managed[event] and managed[event][0] != desired_groups[event]
    ]

    created: list[str] = []
    created_file = before is None
    appended_events: set[str] = set()
    if any_managed or manifest_active:
        if missing_events and manifest_active and not repair:
            result["status"] = "drifted"
            result["blocked_reason"] = (
                f"the manifest-owned {spec.display_name} hook is missing"
            )
            return result
        if mismatched_events and not repair:
            result["status"] = "drifted"
            result["blocked_reason"] = (
                f"the manifest-owned {spec.display_name} hook differs from "
                "the desired definition"
            )
            return result
        if (
            not missing_events
            and not mismatched_events
            and not manifest_group_mismatch
            and not adopt
        ):
            result.update(
                {
                    "status": "installed",
                    "action": "none",
                    "after_sha256": sha256_or_none(before),
                    "plan_token": hashlib.sha256(
                        f"{target}\0{sha256_or_none(before)}\0"
                        f"{sha256_or_none(before)}".encode()
                    ).hexdigest(),
                }
            )
            return result
        action = "repair"
        if manifest_active:
            created_file = manifest["created_file"]
            created = list(manifest["created_containers"])
        after_document = document
        for event in spec.events:
            desired_group = desired_groups[event]
            if managed[event]:
                if managed[event][0] != desired_group or adopt:
                    after_document = _replace_managed_group(
                        spec,
                        after_document,
                        event,
                        desired_group,
                    )
                continue
            manifest_group = manifest_groups.get(event)
            if manifest_group is not None:
                after_document, event_created, replaced = (
                    _repair_missing_group(
                        after_document,
                        event,
                        manifest_group,
                        desired_group,
                    )
                )
                if not replaced:
                    appended_events.add(event)
            else:
                after_document, event_created = _append_group(
                    after_document,
                    event,
                    desired_group,
                )
                appended_events.add(event)
            for container in event_created:
                if container not in created:
                    created.append(container)
    else:
        action = "install"
        after_document = document
        for event in spec.events:
            after_document, event_created = _append_group(
                after_document,
                event,
                desired_groups[event],
            )
            appended_events.add(event)
            for container in event_created:
                if container not in created:
                    created.append(container)

    if created_file and spec.created_file_preamble:
        after_document = {
            **spec.created_file_preamble,
            **after_document,
        }
    after = _encode_hooks(after_document)
    before_sha = sha256_or_none(before)
    after_sha = sha256_or_none(after)
    if action == "install":
        planned_status = "absent"
    elif adopt:
        planned_status = "unmanaged"
    else:
        planned_status = "drifted"
    result.update(
        {
            "status": planned_status,
            "action": action,
            "created_file": created_file,
            "created_containers": created,
            "after_sha256": after_sha,
            "plan_token": hashlib.sha256(
                f"{target}\0{before_sha}\0{after_sha}".encode()
            ).hexdigest(),
            "changes": [
                {
                    "op": (
                        "add" if event in appended_events else "replace"
                    ),
                    "path": f"/hooks/{event}/aiq-owned-group",
                    "value": desired_groups[event],
                }
                for event in spec.events
            ],
            "_after": after,
        }
    )
    return result


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if not key.startswith("_")}


def plan_integration(
    spec: HookIntegrationSpec,
    *,
    launcher: str | Path | None = None,
    git_executable: str | Path | None = None,
    python_executable: str | Path | None = None,
    invoked_launcher: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    repair: bool = False,
) -> dict[str, Any]:
    """Return a JSON-safe description of the owned change only."""

    return _public_plan(
        _build_plan(
            spec,
            launcher=launcher,
            git_executable=git_executable,
            python_executable=python_executable,
            invoked_launcher=invoked_launcher,
            environment=environment,
            repair=repair,
        )
    )


def _backup(
    spec: HookIntegrationSpec,
    state_directory: Path,
    data: bytes | None,
) -> str | None:
    if data is None:
        return None
    backup_directory = state_directory / "backups"
    _ensure_private_directory(spec, backup_directory)
    digest = sha256_or_none(data)
    path = backup_directory / f"{time.time_ns()}-{digest}.hooks.json"
    _atomic_write(path, data, mode=0o600)
    return os.fspath(path)


def install_integration(
    spec: HookIntegrationSpec,
    *,
    launcher: str | Path | None = None,
    git_executable: str | Path | None = None,
    python_executable: str | Path | None = None,
    invoked_launcher: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    repair: bool = False,
    plan_token: str | None = None,
) -> dict[str, Any]:
    """Install or explicitly repair the user-level hook."""

    effective_environment = os.environ if environment is None else environment
    target = spec.target_path(effective_environment)
    state_directory = _integration_state_directory(
        spec,
        effective_environment,
        target,
    )
    with _integration_lock(spec, state_directory):
        plan = _build_plan(
            spec,
            launcher=launcher,
            git_executable=git_executable,
            python_executable=python_executable,
            invoked_launcher=invoked_launcher,
            environment=effective_environment,
            repair=repair,
        )
        if plan_token is not None and plan.get("plan_token") != plan_token:
            raise spec.error_class("reviewed integration plan is stale")
        if plan["action"] == "block":
            raise spec.error_class(plan["blocked_reason"])
        if plan["action"] == "none":
            return _public_plan(plan)

        before = plan["_before"]
        before_status = plan["_before_status"]
        previous_manifest = plan["_manifest"]
        backups = (
            list(previous_manifest.get("backups", []))
            if isinstance(previous_manifest, dict)
            else []
        )
        backup = _backup(spec, state_directory, before)
        if backup is not None:
            backups.append(
                {
                    "path": backup,
                    "sha256": plan["before_sha256"],
                }
            )

        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = (
            0o600
            if before_status is None
            else stat.S_IMODE(before_status.st_mode)
        )
        _assert_target_unchanged(spec, target, before, before_status)
        _atomic_write(target, plan["_after"], mode=mode)
        manifest = {
            "v": 1,
            "status": "installed",
            "integration": spec.integration,
            "integration_id": spec.integration_id,
            "target": os.fspath(target),
            "launcher": plan["_launcher"],
            "git_executable": plan["_git_executable"],
            "python_executable": plan["_python_executable"],
            "managed_group": plan["desired_group"],
            "managed_group_sha256": sha256_or_none(
                json.dumps(
                    plan["desired_group"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ),
            "config_sha256": plan["after_sha256"],
            "created_file": plan["created_file"],
            "created_containers": plan["created_containers"],
            "backups": backups,
        }
        _write_manifest(spec, state_directory, manifest)
        result = _public_plan(plan)
        result["status"] = "installed"
        result["backup"] = backup
        return result


def check_integration(
    spec: HookIntegrationSpec,
    *,
    launcher: str | Path | None = None,
    git_executable: str | Path | None = None,
    python_executable: str | Path | None = None,
    invoked_launcher: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Inspect integration ownership, drift, and executable availability."""

    effective_environment = os.environ if environment is None else environment
    plan = _build_plan(
        spec,
        launcher=launcher,
        git_executable=git_executable,
        python_executable=python_executable,
        invoked_launcher=invoked_launcher,
        environment=effective_environment,
    )
    result = _public_plan(plan)
    result["trust"] = (
        "manual_review_required"
        if plan["status"] == "installed"
        else "not_applicable"
    )
    result["ok"] = plan["status"] == "installed" and plan["action"] == "none"
    return result


def _remove_managed_groups(
    spec: HookIntegrationSpec,
    document: dict[str, Any],
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], bool, list[str]]:
    mapping = _manifest_group_mapping(spec, manifest.get("managed_group"))
    if mapping is None:
        raise spec.error_class("integration manifest owned hook is invalid")
    result = json.loads(json.dumps(document))
    hooks = result.get("hooks")
    removed_events: list[str] = []
    for event, owned_group in mapping.items():
        groups = hooks.get(event) if isinstance(hooks, dict) else None
        if not isinstance(groups, list):
            raise spec.error_class(
                f"the manifest-owned {spec.display_name} hook is missing"
            )
        indexes = [
            index
            for index, group in enumerate(groups)
            if marker_count(group, integration_id=spec.integration_id)
        ]
        if len(indexes) != 1:
            raise spec.error_class(
                f"expected exactly one AIQ {spec.display_name} hook"
            )
        index = indexes[0]
        if groups[index] != owned_group:
            raise spec.error_class(
                f"the manifest-owned {spec.display_name} hook has drifted; "
                "refusing uninstall"
            )
        del groups[index]
        removed_events.append(event)

    created = set(manifest.get("created_containers", []))
    for event in mapping:
        if not hooks[event] and event in created:
            del hooks[event]
    if not hooks and "hooks" in created:
        del result["hooks"]

    delete_file = False
    if manifest.get("created_file"):
        remaining = dict(result)
        for key, value in spec.created_file_preamble.items():
            if remaining.get(key) == value:
                del remaining[key]
        delete_file = not remaining
    return result, delete_file, removed_events


def uninstall_integration(
    spec: HookIntegrationSpec,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Remove only the hook recorded as AIQ-owned."""

    effective_environment = os.environ if environment is None else environment
    target = spec.target_path(effective_environment)
    state_directory = _integration_state_directory(
        spec,
        effective_environment,
        target,
    )
    with _integration_lock(spec, state_directory):
        manifest = _read_manifest(spec, state_directory, target=target)
        if manifest is None:
            raise spec.error_class(
                f"no AIQ {spec.display_name} integration manifest exists"
            )
        if manifest.get("status") == "uninstalled":
            return {
                "v": 1,
                "integration": spec.integration,
                "integration_id": spec.integration_id,
                "status": "uninstalled",
                "action": "none",
                "target": os.fspath(target),
            }
        if (
            manifest.get("status") != "installed"
            or manifest.get("target") != os.fspath(target)
            or manifest.get("integration_id") != spec.integration_id
        ):
            raise spec.error_class(
                f"AIQ {spec.display_name} integration manifest is invalid"
            )

        before, document, before_status = _load_target(spec, target)
        if before is None:
            raise spec.error_class(
                f"the manifest-owned {spec.target_label} file is missing"
            )
        after_document, delete_file, removed_events = _remove_managed_groups(
            spec,
            document,
            manifest,
        )
        after = None if delete_file else _encode_hooks(after_document)
        backup = _backup(spec, state_directory, before)
        backups = list(manifest.get("backups", []))
        if backup is not None:
            backups.append({"path": backup, "sha256": sha256_or_none(before)})

        _assert_target_unchanged(spec, target, before, before_status)
        if after is None:
            target.unlink()
            directory_descriptor = os.open(
                target.parent,
                os.O_RDONLY | os.O_CLOEXEC,
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        else:
            if before_status is None:
                raise spec.error_class(
                    f"{spec.target_label} changed before mutation"
                )
            mode = stat.S_IMODE(before_status.st_mode)
            _atomic_write(target, after, mode=mode)

        manifest.update(
            {
                "status": "uninstalled",
                "config_sha256": sha256_or_none(after),
                "backups": backups,
            }
        )
        _write_manifest(spec, state_directory, manifest)
        return {
            "v": 1,
            "integration": spec.integration,
            "integration_id": spec.integration_id,
            "status": "uninstalled",
            "action": "uninstall",
            "target": os.fspath(target),
            "deleted_file": delete_file,
            "backup": backup,
            "changes": [
                {
                    "op": "remove",
                    "path": f"/hooks/{event}/aiq-owned-group",
                    "integration_id": spec.integration_id,
                }
                for event in removed_events
            ],
        }


def _hook_idempotency_key(
    *,
    source: str,
    session_id: str,
    turn_id: str,
    cwd: str,
    content: str,
) -> str:
    """Derive one idempotency key from the full received message identity.

    The key covers the content and resolved working directory in addition
    to the session and turn identifiers, so a host that re-delivers the
    same turn with different content (for example after slash-command
    expansion) or from a different directory captures a new message
    instead of failing with an identity conflict; only byte-identical
    redelivery replays.
    """

    identity = json.dumps(
        [source, session_id, turn_id, cwd, content],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"hook-identity-v1:{hashlib.sha256(identity).hexdigest()}"


def receive_hook(
    spec: HookIntegrationSpec,
    payload: str | bytes,
    *,
    integration_id: str,
    git_executable: str | Path | None = None,
    agent_root: Path | None = None,
) -> dict[str, Any]:
    """Validate and durably ingest one received hook payload.

    Installed hooks never create journal storage. When the payload's
    working directory resolves to repo scope and that journal does not
    exist, the hook ingests nothing and returns a distinct
    ``{"skipped": "repo-journal-not-initialized"}`` receipt: ``aiq
    journal init --scope repo`` is the per-repository opt-in act and
    ``aiq journal destroy`` the opt-out. User scope keeps
    auto-initialization. Harness-injected prompts are skipped earlier
    with their own ``{"skipped": "injected-notification"}`` receipt.
    """

    receive = spec.receive
    if receive is None:
        raise spec.error_class(
            f"the {spec.display_name} integration does not receive payloads"
        )
    if integration_id != spec.integration_id:
        raise spec.error_class(
            f"unsupported {spec.display_name} integration id"
        )
    if git_executable is None:
        raise spec.error_class(
            f"{spec.display_name} hook requires an absolute Git executable"
        )
    resolved_git_executable = git_executable_path(
        git_executable,
        error_class=spec.error_class,
    )
    raw = payload.encode() if isinstance(payload, str) else payload
    if len(raw) > HOOK_INPUT_MAX_BYTES:
        raise spec.error_class(
            f"{receive.input_label} exceeds {HOOK_INPUT_MAX_BYTES} bytes"
        )
    try:
        document = json.loads(
            raw,
            object_pairs_hook=object_without_duplicates_hook(spec.error_class),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise spec.error_class(
            f"{receive.input_label} is invalid JSON"
        ) from error
    if not isinstance(document, dict):
        raise spec.error_class(
            f"{receive.input_label} must be a JSON object"
        )
    if document.get("hook_event_name") != receive.event:
        raise spec.error_class(
            f"{spec.display_name} hook is not a {receive.event} event"
        )
    prompt = document.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise spec.error_class(
            f"{spec.display_name} hook has no non-empty string prompt"
        )
    if _is_injected_prompt(prompt, receive):
        # Harness-injected content is not a user request: acknowledge it
        # successfully without ingesting and without creating a journal.
        # The distinct receipt shape lets callers tell skip from capture.
        return {"skipped": "injected-notification", "source": receive.source}
    values: dict[str, str] = {}
    for field_name in receive.required_fields:
        value = document.get(field_name)
        if not isinstance(value, str) or not value:
            raise spec.error_class(
                f"{spec.display_name} hook {field_name} must be a "
                "non-empty string"
            )
        values[field_name] = value
    if receive.turn_required:
        turn_id: str | None = values[receive.turn_field]
    else:
        turn_id = document.get(receive.turn_field)
        if turn_id is not None and (
            not isinstance(turn_id, str) or not turn_id
        ):
            raise spec.error_class(
                f"{spec.display_name} hook {receive.turn_field} must be a "
                "non-empty string"
            )
    cwd = Path(values["cwd"])
    if not cwd.is_absolute() or not cwd.is_dir():
        raise spec.error_class(
            f"{spec.display_name} hook working directory is invalid"
        )

    resolved_cwd = os.fspath(cwd.resolve())
    # Without a turn identity there is no safe replay identity: pass no
    # idempotency key, so each delivered event is captured separately.
    idempotency_key = (
        None
        if turn_id is None
        else _hook_idempotency_key(
            source=receive.source,
            session_id=values["session_id"],
            turn_id=turn_id,
            cwd=resolved_cwd,
            content=prompt,
        )
    )
    scope = resolve_scope(
        "auto",
        cwd=cwd,
        agent_root=agent_root,
        git_executable=resolved_git_executable,
    )
    if scope.kind == "repo" and not scope.journal_path.exists():
        # Repo-scope capture is opt-in by journal presence: installed
        # hooks never create journal storage, so a repository that has
        # not run aiq journal init --scope repo is skipped silently
        # with a receipt distinct from the injected-notification skip.
        return {
            "skipped": "repo-journal-not-initialized",
            "source": receive.source,
            "scope": scope.to_dict(),
        }
    result = ingest_message(
        scope,
        prompt,
        source=receive.source,
        idempotency_key=idempotency_key,
        session_id=values["session_id"],
        turn_id=turn_id,
        cwd=resolved_cwd,
        lock_timeout=CAPTURE_LOCK_TIMEOUT_SECONDS,
    )
    return result.to_dict()


def _count_noun(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _truncated_title(title: str, limit: int = 40) -> str:
    if len(title) <= limit:
        return title
    return title[: limit - 1] + "…"


def _parked_fragment(count: int) -> str:
    verb = "awaits" if count == 1 else "await"
    return f"{_count_noun(count, 'parked message')} {verb} user input"


def _coarse_age(created_at_iso: Any, now: datetime) -> str | None:
    """Coarse age such as ``5m``, ``2h``, or ``3d``, or ``None``.

    ``created_at_iso`` is an ISO-8601 UTC timestamp; anything unparseable
    returns ``None`` so the caller omits the age fragment instead of
    raising — the gate fails open, never loudly.
    """

    try:
        created = datetime.fromisoformat(created_at_iso)
    except (TypeError, ValueError):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    seconds = max(0, int((now - created).total_seconds()))
    if seconds < 3_600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3_600}h"
    return f"{seconds // 86_400}d"


def gate_stop_hook(
    spec: HookIntegrationSpec,
    payload: str | bytes,
    *,
    integration_id: str,
    git_executable: str | Path | None = None,
    agent_root: Path | None = None,
) -> tuple[bool, str] | None:
    """Evaluate one ``Stop`` completion gate payload.

    Returns ``(True, reason)`` with the single-line block reason while
    runnable work remains in the scope resolved from the payload working
    directory; the host's ``stop_hook_active`` loop guard being set
    returns ``None`` (silent allow) unconditionally. When ready tasks
    exist, the reason appends up to the first three — task ID,
    double-quoted title truncated to 40 characters, and a coarse
    ready-age — and ends with the exact settle command, so a model
    blocked once per stop chain can act without another lookup. A parked
    ``needs_input`` message is not runnable work — it awaits the user,
    not the agent — so it never blocks stopping, but it is surfaced: a
    block line appends a parked-message fragment, and with nothing
    runnable the gate returns ``(False, notice)`` — one non-blocking
    stderr line naming the parked count — instead of full silence.
    ``None`` (silent allow) means nothing runnable and nothing parked.

    Runnable work obligates the session that may drain it, so two
    readings of the reader lease stand the gate down, both returning
    ``(False, notice)``. The lease is held by a demonstrably different
    and still live session, so this one is a writer only; or this very
    session released the role, the explicit recorded act by which a
    bounded run -- one task, or a fixed batch -- says it finished on
    purpose. Every other reading -- self-held, absent, expired, released
    by somebody else, or held by a session proved dead -- blocks exactly
    as before, because none of them names a live reader who will do the
    work nor this session declining it. That bias is deliberate: agent
    harnesses can give each shell invocation its own POSIX session, so
    leases outlive their sessions routinely, and treating an abandoned
    one as an active reader would silently retire the gate.

    The check is one read-only snapshot; a missing journal counts as
    nothing runnable and never creates storage. Errors raise; the
    boundary in :func:`run_receive_hook_main` fails open on them (exit
    0) so an AIQ defect never blocks stopping.
    """

    if integration_id != spec.integration_id:
        raise spec.error_class(
            f"unsupported {spec.display_name} integration id"
        )
    if git_executable is None:
        raise spec.error_class(
            f"{spec.display_name} hook requires an absolute Git executable"
        )
    resolved_git_executable = git_executable_path(
        git_executable,
        error_class=spec.error_class,
    )
    raw = payload.encode() if isinstance(payload, str) else payload
    if len(raw) > HOOK_INPUT_MAX_BYTES:
        raise spec.error_class(
            f"{spec.display_name} hook input exceeds "
            f"{HOOK_INPUT_MAX_BYTES} bytes"
        )
    try:
        document = json.loads(
            raw,
            object_pairs_hook=object_without_duplicates_hook(spec.error_class),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise spec.error_class(
            f"{spec.display_name} hook input is invalid JSON"
        ) from error
    if not isinstance(document, dict):
        raise spec.error_class(
            f"{spec.display_name} hook input must be a JSON object"
        )
    if document.get("hook_event_name") != STOP_EVENT:
        raise spec.error_class(
            f"{spec.display_name} hook is not a {STOP_EVENT} event"
        )
    if document.get("stop_hook_active"):
        return None
    cwd_value = document.get("cwd")
    if not isinstance(cwd_value, str) or not cwd_value:
        raise spec.error_class(
            f"{spec.display_name} hook cwd must be a non-empty string"
        )
    cwd = Path(cwd_value)
    if not cwd.is_absolute() or not cwd.is_dir():
        raise spec.error_class(
            f"{spec.display_name} hook working directory is invalid"
        )
    scope = resolve_scope(
        "auto",
        cwd=cwd,
        agent_root=agent_root,
        git_executable=resolved_git_executable,
    )
    from aiq.config import resolve_config
    from aiq.queue import read_status

    # Derive this session's reader identity exactly as the CLI does, so
    # the gate compares the same string the drain commands enforce on:
    # configuration and AIQ_READER both apply, and the default is the
    # POSIX session every process of one terminal inherits.
    reader_id = resolve_config(cwd=cwd).reader
    status = read_status(scope, reader_id=reader_id)
    ready_tasks = int(status["tasks"].get("ready", 0))
    active_claims = int(status["claims"].get("active", 0))
    # The subset of those claims this very session holds, proved by the
    # locator recorded on each claim. Scope-wide `active` cannot serve
    # here: it counts a concurrent session's claims too, and `owner_id`
    # cannot separate them because it defaults to the OS user. A patched
    # or older status shape without the datum reads zero, which restores
    # exactly the pre-schema-5 behaviour rather than blocking on nothing.
    own_claims = int(status["claims"].get("active_this_session", 0))
    # A parked needs_input message awaits the user, not the agent, so it
    # deliberately does not count as runnable work here.
    unapplied_messages = int(status["messages"].get("received", 0))
    parked_messages = int(status["messages"].get("needs_input", 0))
    if not (ready_tasks or active_claims or unapplied_messages):
        if parked_messages:
            # Nothing runnable, but a parked question awaits the user:
            # surface it once as a non-blocking notice instead of full
            # silence, so a session never ends with the user unaware.
            return (
                False,
                f"AIQ: no runnable work; {_parked_fragment(parked_messages)}"
                " — aiq inbox list",
            )
        return None
    parts = []
    if ready_tasks:
        parts.append(_count_noun(ready_tasks, "ready task"))
    if active_claims:
        parts.append(_count_noun(active_claims, "active claim"))
    if unapplied_messages:
        parts.append(_count_noun(unapplied_messages, "unapplied message"))
    summary = ", ".join(parts)
    parked_note = (
        f"; {_parked_fragment(parked_messages)}" if parked_messages else ""
    )
    # Runnable work belongs to whoever holds the reader role. Stand down
    # only for a holder *proved* to be a different session that is still
    # alive: the lease is held, its holder recorded a locator naming this
    # host, that session still exists, and it is not this process's own.
    # Every other reading -- no lease, an expired one, one released by
    # somebody else, one left behind by a dead session, one whose holder
    # recorded no locator because the identity was configured explicitly
    # -- means nothing proves another session is draining this queue, so
    # this session is still accountable for the work and must block. The
    # sole exception is this session's own release, handled just below.
    # Proof, not absence
    # of doubt, is the standard: a hook process does not inherit the
    # agent shell's environment, so this gate can derive a different
    # reader identity than the CLI that took the lease, and reading its
    # own session's lease as a stranger's would silently retire the gate
    # for exactly the session doing the work. A deliberate shared-reader
    # fan-out therefore keeps blocking, which is the safe direction. A
    # patched or older status shape without the datum reads falsy and
    # therefore blocks too.
    reader = status.get("reader")
    if not isinstance(reader, dict):
        reader = {}
    if reader.get("self") is False and reader.get("live"):
        return (
            False,
            f"AIQ: not blocking: runnable work remains ({summary}) but "
            f'reader "{reader.get("reader_id")}" holds the reader lease'
            f"{parked_note} — aiq reader status",
        )
    # The other way a session is not accountable for the rest of the
    # queue: it said so. Releasing the reader role is an explicit,
    # recorded act meaning "I am no longer draining this queue", which is
    # exactly the signal a bounded run -- one task, or a fixed batch --
    # needs to finish without the gate reading its deliberate stop as an
    # abandonment. Honoring it needs the same proof as standing down for
    # a foreign reader, and for the same reason: only a release whose
    # recorded holder locator names this host and this session is this
    # session declaring anything. A release by anyone else, and a release
    # under an explicitly configured identity that recorded no locator,
    # keeps blocking. A patched or older status shape without the datum
    # reads falsy and therefore blocks too.
    if reader.get("released_by_self"):
        # Releasing the role is a statement about dispatch, not about the
        # items already taken: it deliberately leaves every per-item claim
        # in place. A session that stops here strands its own claimed work
        # for a whole lease period, unworkable by anyone. So the release
        # stands the gate down only for a session holding nothing of its
        # own; otherwise the obligation is the claim, and the remedy is to
        # settle or release it.
        #
        # This branch is reachable only when the release above proved to
        # be this session's, which needs the recorded locator to match.
        # Where it cannot -- a host giving each shell invocation its own
        # POSIX session -- `released_by_self` is false and control never
        # arrives here, so the gate blocks on the counts below. See the
        # known limitation on `_count_active_claims_this_session`
        # (TASK-61): this check is exactly as reliable as the stand-down
        # it refines, and fails toward blocking.
        if not own_claims:
            return (
                False,
                f"AIQ: not blocking: runnable work remains ({summary}) but "
                "this session released the reader role"
                f"{parked_note} — aiq reader status",
            )
        return (
            True,
            "AIQ: this session released the reader role but still holds "
            f"{_count_noun(own_claims, 'active claim')} of its own"
            f" ({summary}){parked_note} — settle finished work: "
            "aiq task done TASK_ID --summary TEXT — or hand it back: "
            "aiq claim release CLAIM_ID — list yours: "
            "aiq claim list --status active",
        )
    # Make the single block line actionable: name up to the first three
    # ready tasks and the exact settle command, so a model blocked once
    # per stop chain can act without another lookup. Tolerate patched or
    # older status shapes without a ready list (fail-open posture).
    ready_entries = status.get("ready") or []
    if not isinstance(ready_entries, list):
        ready_entries = []
    now = datetime.now(timezone.utc)
    # Name each ready task with its project label so a line read in an
    # orchestrating session says which repository the work belongs to.
    # The settle tail below deliberately keeps the bare ID: that text is
    # meant to be copied into a command. Tolerate a patched or older
    # status shape without a label (fail-open posture).
    project = str(status.get("project") or "")
    fragments = []
    first_ready_id = ""
    for entry in ready_entries[:3]:
        if not isinstance(entry, dict):
            continue
        task_id = str(entry.get("task_id") or "")
        if not task_id:
            continue
        first_ready_id = first_ready_id or task_id
        title = _truncated_title(str(entry.get("title") or ""))
        reference = f"[{project}: {task_id}]" if project else task_id
        fragment = f'{reference} "{title}"'
        age = _coarse_age(entry.get("created_at"), now)
        if age is not None:
            fragment += f" (open {age})"
        fragments.append(fragment)
    if not fragments:
        return (
            True,
            f"AIQ: runnable work remains: {summary}{parked_note}"
            " — run aiq status",
        )
    return (
        True,
        f"AIQ: runnable work remains: {summary}: "
        + "; ".join(fragments)
        + parked_note
        + f" — settle finished work: aiq task done {first_ready_id} "
        "--summary TEXT — or: aiq status",
    )
