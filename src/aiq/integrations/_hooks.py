"""Shared engine for reversible user-level prompt-hook integrations.

Each adapter owns one marked hook group inside one JSON configuration file.
The engine provides the filesystem safety, manifest ownership, drift
detection, and lifecycle mechanics; adapters supply the target file, hook
group definition, and payload validation.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time
from typing import Any, Callable, Mapping

from aiq.journal import JournalError


HOOK_INPUT_MAX_BYTES = 1_048_576
TARGET_MAX_BYTES = 1_048_576
_EVENT = "UserPromptSubmit"


class HookIntegrationError(JournalError):
    """A hook integration cannot be inspected or changed safely."""


def single_line(value: str) -> str:
    return "".join(
        character
        if character.isprintable() and character not in {"\t", "\r", "\n"}
        else f"\\u{ord(character):04x}"
        for character in value
    )


def run_receive_hook_main(
    receive: Callable[[bytes], Any],
    *,
    error_class: type[JournalError],
    input_label: str,
    failure_exit_code: int,
    input_stream: Any = None,
    error_stream: Any = None,
) -> int:
    """Run a stdout-silent hook boundary around one ``receive`` call."""

    source = sys.stdin.buffer if input_stream is None else input_stream
    errors = sys.stderr if error_stream is None else error_stream
    try:
        payload = source.read(HOOK_INPUT_MAX_BYTES + 1)
        if len(payload) > HOOK_INPUT_MAX_BYTES:
            raise error_class(
                f"{input_label} exceeds {HOOK_INPUT_MAX_BYTES} bytes"
            )
        receive(payload)
        return 0
    except Exception as error:
        errors.write(f"AIQ prompt capture failed: {single_line(str(error))}\n")
        errors.flush()
        return failure_exit_code


@dataclass(frozen=True)
class HookIntegrationSpec:
    """Everything adapter-specific the shared engine needs."""

    integration: str
    integration_id: str
    error_class: type[JournalError]
    display_name: str
    target_label: str
    state_subdirectory: str
    target_path: Callable[[Mapping[str, str]], Path]
    hook_group: Callable[[Path, Path], dict[str, Any]]
    created_file_preamble: dict[str, Any] = field(default_factory=dict)
    preflight: Callable[[Mapping[str, str], Path], dict[str, str] | None] = (
        lambda environment, target: None
    )


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


def _ensure_private_directory(spec: HookIntegrationSpec, path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    status = path.lstat()
    if (
        not stat.S_ISDIR(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or status.st_uid != os.getuid()
    ):
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


def marker_count(group: Any, *, integration_id: str) -> int:
    if not isinstance(group, dict):
        return 0
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        return 0
    marker = f"--integration-id {integration_id}"
    return sum(
        1
        for handler in handlers
        if isinstance(handler, dict)
        and isinstance(handler.get("command"), str)
        and marker in handler["command"]
    )


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
        groups = hooks.get(_EVENT)
        if groups is not None and not isinstance(groups, list):
            raise spec.error_class(
                f"{spec.display_name} {_EVENT} hooks must be a JSON array"
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
        or not set(containers).issubset({"hooks", _EVENT})
    ):
        raise spec.error_class(
            "integration manifest created containers are invalid"
        )
    group = manifest["managed_group"]
    if (
        not isinstance(group, dict)
        or group
        != spec.hook_group(
            Path(manifest["python_executable"]),
            Path(manifest["git_executable"]),
        )
        or marker_count(group, integration_id=spec.integration_id) != 1
    ):
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
    if state_directory.exists() or state_directory.is_symlink():
        status = state_directory.lstat()
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or status.st_uid != os.getuid()
            or stat.S_IMODE(status.st_mode) & 0o077
        ):
            raise spec.error_class(
                f"integration state directory is unsafe: {state_directory}"
            )
    path = _manifest_path(state_directory)
    if not path.exists() and not path.is_symlink():
        return None
    data = read_bounded(
        path,
        262_144,
        label="integration manifest",
        error_class=spec.error_class,
    )
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


def _groups(document: dict[str, Any]) -> list[Any]:
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return []
    groups = hooks.get(_EVENT)
    return groups if isinstance(groups, list) else []


def _managed_groups(
    spec: HookIntegrationSpec,
    document: dict[str, Any],
) -> list[Any]:
    return [
        group
        for group in _groups(document)
        if marker_count(group, integration_id=spec.integration_id)
    ]


def _append_group(
    document: dict[str, Any],
    group: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    result = json.loads(json.dumps(document))
    created: list[str] = []
    hooks = result.get("hooks")
    if hooks is None:
        hooks = {}
        result["hooks"] = hooks
        created.append("hooks")
    groups = hooks.get(_EVENT)
    if groups is None:
        groups = []
        hooks[_EVENT] = groups
        created.append(_EVENT)
    groups.append(group)
    return result, created


def _replace_managed_group(
    spec: HookIntegrationSpec,
    document: dict[str, Any],
    replacement: dict[str, Any],
) -> dict[str, Any]:
    result = json.loads(json.dumps(document))
    groups = result["hooks"][_EVENT]
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
    """Render an externally managed configuration fragment."""

    launcher_path(
        launcher,
        error_class=spec.error_class,
        invoked_launcher=invoked_launcher,
        environment=environment,
    )
    resolved_git_executable = git_executable_path(
        git_executable,
        error_class=spec.error_class,
        environment=environment,
    )
    resolved_python_executable = python_executable_path(
        python_executable,
        error_class=spec.error_class,
    )
    group = spec.hook_group(
        resolved_python_executable,
        resolved_git_executable,
    )
    return _encode_hooks({"hooks": {_EVENT: [group]}}).decode("utf-8")


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
    desired_group = spec.hook_group(
        resolved_python_executable,
        resolved_git_executable,
    )
    result: dict[str, Any] = {
        "v": 1,
        "integration": spec.integration,
        "integration_id": spec.integration_id,
        "target": os.fspath(target),
        "state_directory": os.fspath(state_directory),
        "desired_group": desired_group,
        "status": "unknown",
        "action": "block",
        "blocked_reason": None,
        "changes": [],
        "_launcher": os.fspath(resolved_launcher),
        "_git_executable": os.fspath(resolved_git_executable),
        "_python_executable": os.fspath(resolved_python_executable),
    }
    try:
        blocked = spec.preflight(effective_environment, target)
        before, document, _ = _load_target(spec, target)
        manifest = _read_manifest(spec, state_directory, target=target)
    except JournalError as error:
        result["status"] = "unsafe"
        result["blocked_reason"] = str(error)
        return result

    result["before_sha256"] = sha256_or_none(before)
    if blocked is not None:
        result["status"] = blocked["status"]
        result["blocked_reason"] = blocked["blocked_reason"]
        return result

    managed = _managed_groups(spec, document)
    marker_total = sum(
        marker_count(group, integration_id=spec.integration_id)
        for group in _groups(document)
    )
    if len(managed) > 1 or marker_total > 1:
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
    manifest_group_mismatch = bool(
        manifest_active
        and managed
        and manifest.get("managed_group") != managed[0]
    )
    if manifest_group_mismatch and not repair:
        result["status"] = "drifted"
        result["blocked_reason"] = (
            f"the integration manifest differs from the configured "
            f"{spec.display_name} hook"
        )
        return result
    if managed and not manifest_active:
        result["status"] = "unmanaged"
        result["blocked_reason"] = (
            f"an AIQ-marked {spec.display_name} hook exists without an "
            "active AIQ manifest"
        )
        return result

    created: list[str] = []
    created_file = before is None
    if not managed:
        if manifest_active and not repair:
            result["status"] = "drifted"
            result["blocked_reason"] = (
                f"the manifest-owned {spec.display_name} hook is missing"
            )
            return result
        after_document, created = _append_group(document, desired_group)
        action = "repair" if manifest_active else "install"
    elif managed[0] != desired_group:
        if not repair:
            result["status"] = "drifted"
            result["blocked_reason"] = (
                f"the manifest-owned {spec.display_name} hook differs from "
                "the desired definition"
            )
            return result
        after_document = _replace_managed_group(spec, document, desired_group)
        action = "repair"
        if manifest_active:
            created_file = manifest["created_file"]
            created = list(manifest["created_containers"])
    elif manifest_group_mismatch:
        after_document = document
        action = "repair"
        created_file = manifest["created_file"]
        created = list(manifest["created_containers"])
    else:
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

    if created_file and spec.created_file_preamble:
        after_document = {
            **spec.created_file_preamble,
            **after_document,
        }
    after = _encode_hooks(after_document)
    before_sha = sha256_or_none(before)
    after_sha = sha256_or_none(after)
    result.update(
        {
            "status": "absent" if action == "install" else "drifted",
            "action": action,
            "created_file": created_file,
            "created_containers": created,
            "after_sha256": after_sha,
            "plan_token": hashlib.sha256(
                f"{target}\0{before_sha}\0{after_sha}".encode()
            ).hexdigest(),
            "changes": [
                {
                    "op": "add" if action == "install" else "replace",
                    "path": f"/hooks/{_EVENT}/aiq-owned-group",
                    "value": desired_group,
                }
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

        before, _, before_status = _load_target(spec, target)
        if sha256_or_none(before) != plan["before_sha256"]:
            raise spec.error_class(
                f"{spec.target_label} changed while installing"
            )
        previous_manifest = _read_manifest(spec, state_directory, target=target)
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


def _remove_managed_group(
    spec: HookIntegrationSpec,
    document: dict[str, Any],
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    result = json.loads(json.dumps(document))
    hooks = result.get("hooks")
    groups = hooks.get(_EVENT) if isinstance(hooks, dict) else None
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
            f"expected exactly one manifest-owned {spec.display_name} hook"
        )
    index = indexes[0]
    if groups[index] != manifest.get("managed_group"):
        raise spec.error_class(
            f"the manifest-owned {spec.display_name} hook has drifted; "
            "refusing uninstall"
        )
    del groups[index]

    created = set(manifest.get("created_containers", []))
    if not groups and _EVENT in created:
        del hooks[_EVENT]
    if not hooks and "hooks" in created:
        del result["hooks"]

    delete_file = False
    if manifest.get("created_file"):
        remaining = dict(result)
        for key, value in spec.created_file_preamble.items():
            if remaining.get(key) == value:
                del remaining[key]
        delete_file = not remaining
    return result, delete_file


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
        after_document, delete_file = _remove_managed_group(
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
            "status": "uninstalled",
            "action": "uninstall",
            "target": os.fspath(target),
            "backup": backup,
            "changes": [
                {
                    "op": "remove",
                    "path": f"/hooks/{_EVENT}/aiq-owned-group",
                    "integration_id": spec.integration_id,
                }
            ],
        }
