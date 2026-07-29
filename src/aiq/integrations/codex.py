"""Reversible Codex ``UserPromptSubmit`` integration."""

from __future__ import annotations

from contextlib import contextmanager
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
import tomllib
from typing import Any, BinaryIO, Iterator, Mapping, TextIO

from aiq.journal import JournalError, ingest_message, resolve_scope


CONTRACT_VERSION = 1
INTEGRATION_ID = "aiq-workqueue.codex.user-prompt.v1"
HOOK_INPUT_MAX_BYTES = 1_048_576
HOOK_DESCRIPTION = "AIQ local work-journal integration."


class CodexIntegrationError(JournalError):
    """The Codex integration cannot be inspected or changed safely."""


def _home(environment: Mapping[str, str]) -> Path:
    configured = environment.get("HOME")
    if not configured:
        return Path.home()
    path = Path(configured)
    if not path.is_absolute():
        raise CodexIntegrationError("HOME must be an absolute path")
    return path


def _codex_home(environment: Mapping[str, str]) -> Path:
    configured = environment.get("CODEX_HOME")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise CodexIntegrationError("CODEX_HOME must be an absolute path")
        return path
    return _home(environment) / ".codex"


def _state_home(environment: Mapping[str, str]) -> Path:
    configured = environment.get("XDG_STATE_HOME")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise CodexIntegrationError("XDG_STATE_HOME must be an absolute path")
        return path
    return _home(environment) / ".local" / "state"


def _target_path(environment: Mapping[str, str]) -> Path:
    return _codex_home(environment) / "hooks.json"


def _integration_state_directory(
    environment: Mapping[str, str],
    target: Path,
) -> Path:
    target_id = hashlib.sha256(os.fsencode(target)).hexdigest()[:16]
    return (
        _state_home(environment)
        / "aiq"
        / "integrations"
        / "codex"
        / target_id
    )


def integration_present(
    *,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Report whether the Codex target or AIQ-owned state exists."""

    effective_environment = os.environ if environment is None else environment
    target = _target_path(effective_environment)
    state_directory = _integration_state_directory(
        effective_environment,
        target,
    )
    return target.exists() or state_directory.exists()


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    status = path.lstat()
    if (
        not stat.S_ISDIR(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or status.st_uid != os.getuid()
    ):
        raise CodexIntegrationError(
            f"integration state directory is unsafe: {path}"
        )
    path.chmod(0o700)


def _read_bounded_with_status(
    path: Path,
    maximum_bytes: int,
    *,
    label: str,
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
        raise CodexIntegrationError(
            f"{label} is not a safe regular file: {path}"
        ) from error
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.getuid()
            or status.st_nlink != 1
        ):
            raise CodexIntegrationError(
                f"{label} is not a safe regular file: {path}"
            )
        if status.st_size > maximum_bytes:
            raise CodexIntegrationError(f"{label} exceeds {maximum_bytes} bytes")
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
            raise CodexIntegrationError(f"{label} exceeds {maximum_bytes} bytes")
        return data, status
    finally:
        os.close(descriptor)


def _read_bounded(path: Path, maximum_bytes: int, *, label: str) -> bytes:
    return _read_bounded_with_status(
        path,
        maximum_bytes,
        label=label,
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
def _integration_lock(state_directory: Path) -> Iterator[None]:
    _ensure_private_directory(state_directory)
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
            raise CodexIntegrationError(
                f"integration lock is unsafe: {lock_path}"
            )
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _launcher_path(
    launcher: str | Path | None,
    *,
    invoked_launcher: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    candidate = launcher
    if candidate is None:
        candidate = invoked_launcher
    if candidate is None:
        search_path = (
            os.environ.get("PATH", "")
            if environment is None
            else environment.get("PATH", "")
        )
        discovered = (
            None
            if search_path == ""
            else shutil.which("aiq", path=search_path)
        )
        if discovered is None:
            raise CodexIntegrationError(
                "cannot determine the AIQ launcher; provide an absolute "
                "--launcher path"
            )
        candidate = Path(discovered).absolute()
    path = Path(candidate)
    if not path.is_absolute():
        raise CodexIntegrationError("AIQ launcher must be an absolute path")
    if any(character in os.fspath(path) for character in ("\0", "\r", "\n")):
        raise CodexIntegrationError("AIQ launcher path contains control characters")
    try:
        status = path.stat()
    except OSError as error:
        raise CodexIntegrationError(f"AIQ launcher is unavailable: {path}") from error
    if not stat.S_ISREG(status.st_mode) or not os.access(path, os.X_OK):
        raise CodexIntegrationError(f"AIQ launcher is not executable: {path}")
    return path


def _git_executable_path(
    git_executable: str | Path | None,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    candidate = git_executable
    if candidate is None:
        search_path = (
            os.environ.get("PATH", "")
            if environment is None
            else environment.get("PATH", "")
        )
        discovered = (
            None
            if search_path == ""
            else shutil.which("git", path=search_path)
        )
        if discovered is None:
            raise CodexIntegrationError(
                "cannot determine the Git executable; provide an absolute "
                "--git-executable path"
            )
        candidate = Path(discovered).absolute()
    path = Path(candidate)
    if not path.is_absolute():
        raise CodexIntegrationError("Git executable must be an absolute path")
    if any(character in os.fspath(path) for character in ("\0", "\r", "\n")):
        raise CodexIntegrationError(
            "Git executable path contains control characters"
        )
    try:
        status = path.stat()
    except OSError as error:
        raise CodexIntegrationError(
            f"Git executable is unavailable: {path}"
        ) from error
    if not stat.S_ISREG(status.st_mode) or not os.access(path, os.X_OK):
        raise CodexIntegrationError(
            f"Git executable is not executable: {path}"
        )
    return path


def _python_executable_path(
    python_executable: str | Path | None,
) -> Path:
    candidate = sys.executable if python_executable is None else python_executable
    path = Path(candidate)
    if not path.is_absolute():
        raise CodexIntegrationError(
            "Python executable must be an absolute path"
        )
    if any(character in os.fspath(path) for character in ("\0", "\r", "\n")):
        raise CodexIntegrationError(
            "Python executable path contains control characters"
        )
    try:
        status = path.stat()
    except OSError as error:
        raise CodexIntegrationError(
            f"Python executable is unavailable: {path}"
        ) from error
    if not stat.S_ISREG(status.st_mode) or not os.access(path, os.X_OK):
        raise CodexIntegrationError(
            f"Python executable is not executable: {path}"
        )
    return path


def _hook_group(
    python_executable: Path,
    git_executable: Path,
) -> dict[str, Any]:
    command = (
        f"{shlex.quote(os.fspath(python_executable))} -I -m aiq "
        "integration receive codex "
        f"--integration-id {INTEGRATION_ID} "
        f"--git-executable {shlex.quote(os.fspath(git_executable))}"
    )
    return {
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": 10,
                "statusMessage": "AIQ: capturing message",
            }
        ]
    }


def _marker_count(group: Any) -> int:
    if not isinstance(group, dict):
        return 0
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        return 0
    marker = f"--integration-id {INTEGRATION_ID}"
    return sum(
        1
        for handler in handlers
        if isinstance(handler, dict)
        and isinstance(handler.get("command"), str)
        and marker in handler["command"]
    )


def _decode_hooks(data: bytes, *, target: Path) -> dict[str, Any]:
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CodexIntegrationError(f"Codex hooks are not UTF-8: {target}") from error
    try:
        document = json.loads(decoded, object_pairs_hook=_object_without_duplicates)
    except json.JSONDecodeError as error:
        raise CodexIntegrationError(f"Codex hooks are invalid JSON: {target}") from error
    if not isinstance(document, dict):
        raise CodexIntegrationError("Codex hooks root must be a JSON object")
    hooks = document.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise CodexIntegrationError("Codex hooks field must be a JSON object")
    if isinstance(hooks, dict):
        groups = hooks.get("UserPromptSubmit")
        if groups is not None and not isinstance(groups, list):
            raise CodexIntegrationError(
                "Codex UserPromptSubmit hooks must be a JSON array"
            )
    return document


def _object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CodexIntegrationError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _encode_hooks(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()


def _sha256(data: bytes | None) -> str | None:
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
        raise CodexIntegrationError("integration manifest has an invalid schema")
    if (
        type(manifest["v"]) is not int
        or manifest["v"] != CONTRACT_VERSION
        or not isinstance(manifest["status"], str)
        or manifest["status"] not in ("installed", "uninstalled")
        or manifest["integration"] != "codex"
        or manifest["integration_id"] != INTEGRATION_ID
        or manifest["target"] != os.fspath(target)
        or not isinstance(manifest["created_file"], bool)
    ):
        raise CodexIntegrationError("integration manifest has invalid ownership")
    launcher = manifest["launcher"]
    if (
        not isinstance(launcher, str)
        or not Path(launcher).is_absolute()
        or any(character in launcher for character in ("\0", "\r", "\n"))
    ):
        raise CodexIntegrationError("integration manifest launcher is invalid")
    git_executable = manifest["git_executable"]
    if (
        not isinstance(git_executable, str)
        or not Path(git_executable).is_absolute()
        or any(
            character in git_executable
            for character in ("\0", "\r", "\n")
        )
    ):
        raise CodexIntegrationError(
            "integration manifest Git executable is invalid"
        )
    python_executable = manifest["python_executable"]
    if (
        not isinstance(python_executable, str)
        or not Path(python_executable).is_absolute()
        or any(
            character in python_executable
            for character in ("\0", "\r", "\n")
        )
    ):
        raise CodexIntegrationError(
            "integration manifest Python executable is invalid"
        )
    containers = manifest["created_containers"]
    if (
        not isinstance(containers, list)
        or not all(isinstance(item, str) for item in containers)
        or len(containers) != len(set(containers))
        or not set(containers).issubset({"hooks", "UserPromptSubmit"})
    ):
        raise CodexIntegrationError(
            "integration manifest created containers are invalid"
        )
    group = manifest["managed_group"]
    if (
        not isinstance(group, dict)
        or group
        != _hook_group(
            Path(python_executable),
            Path(git_executable),
        )
        or _marker_count(group) != 1
    ):
        raise CodexIntegrationError("integration manifest owned hook is invalid")
    group_digest = _sha256(
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
        raise CodexIntegrationError("integration manifest digest is invalid")
    backups = manifest["backups"]
    if not isinstance(backups, list):
        raise CodexIntegrationError("integration manifest backups are invalid")
    backup_directory = state_directory / "backups"
    for backup in backups:
        if (
            not isinstance(backup, dict)
            or set(backup) != {"path", "sha256"}
            or not isinstance(backup["path"], str)
            or Path(backup["path"]).parent != backup_directory
            or not _valid_digest(backup["sha256"])
        ):
            raise CodexIntegrationError("integration manifest backup is invalid")
    return manifest


def _read_manifest(
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
            raise CodexIntegrationError(
                f"integration state directory is unsafe: {state_directory}"
            )
    path = _manifest_path(state_directory)
    if not path.exists() and not path.is_symlink():
        return None
    data = _read_bounded(path, 262_144, label="integration manifest")
    try:
        manifest = json.loads(data, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexIntegrationError("integration manifest is invalid") from error
    return _validate_manifest(
        manifest,
        state_directory=state_directory,
        target=target,
    )


def _write_manifest(state_directory: Path, manifest: dict[str, Any]) -> None:
    _ensure_private_directory(state_directory)
    data = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    _atomic_write(_manifest_path(state_directory), data, mode=0o600)


def _inline_configuration_status(codex_home: Path) -> dict[str, bool]:
    path = codex_home / "config.toml"
    if not path.exists() and not path.is_symlink():
        return {"hooks": False, "disabled": False}
    data = _read_bounded(path, 1_048_576, label="Codex config")
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise CodexIntegrationError(f"Codex config is invalid TOML: {path}") from error
    hooks = document.get("hooks")
    features = document.get("features")
    disabled = isinstance(features, dict) and features.get("hooks") is False
    return {
        "hooks": isinstance(hooks, dict) and bool(hooks),
        "disabled": disabled,
    }


def _load_target(
    target: Path,
) -> tuple[bytes | None, dict[str, Any], os.stat_result | None]:
    try:
        data, status = _read_bounded_with_status(
            target,
            1_048_576,
            label="Codex hooks",
        )
    except FileNotFoundError:
        return None, {}, None
    return data, _decode_hooks(data, target=target), status


def _assert_target_unchanged(
    target: Path,
    expected: bytes | None,
    expected_status: os.stat_result | None,
) -> None:
    try:
        current, status = _read_bounded_with_status(
            target,
            1_048_576,
            label="Codex hooks",
        )
    except FileNotFoundError:
        if expected is None and expected_status is None:
            return
        raise CodexIntegrationError("Codex hooks changed before mutation")
    if expected is None or expected_status is None:
        raise CodexIntegrationError("Codex hooks changed before mutation")
    if (
        current != expected
        or status.st_dev != expected_status.st_dev
        or status.st_ino != expected_status.st_ino
    ):
        raise CodexIntegrationError("Codex hooks changed before mutation")


def _groups(document: dict[str, Any]) -> list[Any]:
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return []
    groups = hooks.get("UserPromptSubmit")
    return groups if isinstance(groups, list) else []


def _managed_groups(document: dict[str, Any]) -> list[Any]:
    return [group for group in _groups(document) if _marker_count(group)]


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
    groups = hooks.get("UserPromptSubmit")
    if groups is None:
        groups = []
        hooks["UserPromptSubmit"] = groups
        created.append("UserPromptSubmit")
    groups.append(group)
    return result, created


def _replace_managed_group(
    document: dict[str, Any],
    replacement: dict[str, Any],
) -> dict[str, Any]:
    result = json.loads(json.dumps(document))
    groups = result["hooks"]["UserPromptSubmit"]
    indexes = [index for index, group in enumerate(groups) if _marker_count(group)]
    if len(indexes) != 1:
        raise CodexIntegrationError("expected exactly one AIQ Codex hook")
    groups[indexes[0]] = replacement
    return result


def print_integration(
    *,
    launcher: str | Path | None = None,
    git_executable: str | Path | None = None,
    python_executable: str | Path | None = None,
    invoked_launcher: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Render an externally managed ``hooks.json`` fragment."""

    _launcher_path(
        launcher,
        invoked_launcher=invoked_launcher,
        environment=environment,
    )
    resolved_git_executable = _git_executable_path(
        git_executable,
        environment=environment,
    )
    resolved_python_executable = _python_executable_path(python_executable)
    group = _hook_group(
        resolved_python_executable,
        resolved_git_executable,
    )
    return _encode_hooks(
        {"hooks": {"UserPromptSubmit": [group]}}
    ).decode("utf-8")


def _build_plan(
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
    target = _target_path(effective_environment)
    state_directory = _integration_state_directory(effective_environment, target)
    resolved_launcher = _launcher_path(
        launcher,
        invoked_launcher=invoked_launcher,
        environment=effective_environment,
    )
    resolved_git_executable = _git_executable_path(
        git_executable,
        environment=effective_environment,
    )
    resolved_python_executable = _python_executable_path(python_executable)
    desired_group = _hook_group(
        resolved_python_executable,
        resolved_git_executable,
    )
    result: dict[str, Any] = {
        "v": CONTRACT_VERSION,
        "integration": "codex",
        "integration_id": INTEGRATION_ID,
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
        inline = _inline_configuration_status(target.parent)
        before, document, _ = _load_target(target)
        manifest = _read_manifest(state_directory, target=target)
    except CodexIntegrationError as error:
        result["status"] = "unsafe"
        result["blocked_reason"] = str(error)
        return result

    result["before_sha256"] = _sha256(before)
    if inline["disabled"]:
        result["status"] = "disabled"
        result["blocked_reason"] = "Codex lifecycle hooks are disabled in config.toml"
        return result
    if inline["hooks"]:
        result["status"] = "conflict"
        result["blocked_reason"] = (
            "config.toml already contains inline hooks; use integration print "
            "and manage one representation externally"
        )
        return result

    managed = _managed_groups(document)
    marker_total = sum(_marker_count(group) for group in _groups(document))
    if len(managed) > 1 or marker_total > 1:
        result["status"] = "conflict"
        result["blocked_reason"] = "multiple AIQ Codex hooks are configured"
        return result

    manifest_active = (
        manifest is not None
        and manifest.get("status") == "installed"
        and manifest.get("target") == os.fspath(target)
        and manifest.get("integration_id") == INTEGRATION_ID
    )
    manifest_group_mismatch = bool(
        manifest_active
        and managed
        and manifest.get("managed_group") != managed[0]
    )
    if manifest_group_mismatch and not repair:
        result["status"] = "drifted"
        result["blocked_reason"] = (
            "the integration manifest differs from the configured Codex hook"
        )
        return result
    if managed and not manifest_active:
        result["status"] = "unmanaged"
        result["blocked_reason"] = (
            "an AIQ-marked Codex hook exists without an active AIQ manifest"
        )
        return result

    created: list[str] = []
    created_file = before is None
    if not managed:
        if manifest_active and not repair:
            result["status"] = "drifted"
            result["blocked_reason"] = "the manifest-owned Codex hook is missing"
            return result
        after_document, created = _append_group(document, desired_group)
        action = "repair" if manifest_active else "install"
    elif managed[0] != desired_group:
        if not repair:
            result["status"] = "drifted"
            result["blocked_reason"] = (
                "the manifest-owned Codex hook differs from the desired definition"
            )
            return result
        after_document = _replace_managed_group(document, desired_group)
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
                "after_sha256": _sha256(before),
                "plan_token": hashlib.sha256(
                    f"{target}\0{_sha256(before)}\0{_sha256(before)}".encode()
                ).hexdigest(),
            }
        )
        return result

    if created_file:
        after_document = {
            "description": HOOK_DESCRIPTION,
            **after_document,
        }
    after = _encode_hooks(after_document)
    before_sha = _sha256(before)
    after_sha = _sha256(after)
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
                    "path": "/hooks/UserPromptSubmit/aiq-owned-group",
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
            launcher=launcher,
            git_executable=git_executable,
            python_executable=python_executable,
            invoked_launcher=invoked_launcher,
            environment=environment,
            repair=repair,
        )
    )


def _backup(
    state_directory: Path,
    target: Path,
    data: bytes | None,
) -> str | None:
    if data is None:
        return None
    backup_directory = state_directory / "backups"
    _ensure_private_directory(backup_directory)
    digest = _sha256(data)
    path = backup_directory / f"{time.time_ns()}-{digest}.hooks.json"
    _atomic_write(path, data, mode=0o600)
    return os.fspath(path)


def install_integration(
    *,
    launcher: str | Path | None = None,
    git_executable: str | Path | None = None,
    python_executable: str | Path | None = None,
    invoked_launcher: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    repair: bool = False,
    plan_token: str | None = None,
) -> dict[str, Any]:
    """Install or explicitly repair the user-level Codex hook."""

    effective_environment = os.environ if environment is None else environment
    target = _target_path(effective_environment)
    state_directory = _integration_state_directory(effective_environment, target)
    with _integration_lock(state_directory):
        plan = _build_plan(
            launcher=launcher,
            git_executable=git_executable,
            python_executable=python_executable,
            invoked_launcher=invoked_launcher,
            environment=effective_environment,
            repair=repair,
        )
        if plan_token is not None and plan.get("plan_token") != plan_token:
            raise CodexIntegrationError("reviewed integration plan is stale")
        if plan["action"] == "block":
            raise CodexIntegrationError(plan["blocked_reason"])
        if plan["action"] == "none":
            return _public_plan(plan)

        before, _, before_status = _load_target(target)
        if _sha256(before) != plan["before_sha256"]:
            raise CodexIntegrationError("Codex hooks changed while installing")
        previous_manifest = _read_manifest(state_directory, target=target)
        backups = (
            list(previous_manifest.get("backups", []))
            if isinstance(previous_manifest, dict)
            else []
        )
        backup = _backup(state_directory, target, before)
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
        _assert_target_unchanged(target, before, before_status)
        _atomic_write(target, plan["_after"], mode=mode)
        manifest = {
            "v": CONTRACT_VERSION,
            "status": "installed",
            "integration": "codex",
            "integration_id": INTEGRATION_ID,
            "target": os.fspath(target),
            "launcher": plan["_launcher"],
            "git_executable": plan["_git_executable"],
            "python_executable": plan["_python_executable"],
            "managed_group": plan["desired_group"],
            "managed_group_sha256": _sha256(
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
        _write_manifest(state_directory, manifest)
        result = _public_plan(plan)
        result["status"] = "installed"
        result["backup"] = backup
        return result


def check_integration(
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
    document: dict[str, Any],
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    result = json.loads(json.dumps(document))
    hooks = result.get("hooks")
    groups = hooks.get("UserPromptSubmit") if isinstance(hooks, dict) else None
    if not isinstance(groups, list):
        raise CodexIntegrationError("the manifest-owned Codex hook is missing")
    indexes = [index for index, group in enumerate(groups) if _marker_count(group)]
    if len(indexes) != 1:
        raise CodexIntegrationError("expected exactly one manifest-owned Codex hook")
    index = indexes[0]
    if groups[index] != manifest.get("managed_group"):
        raise CodexIntegrationError(
            "the manifest-owned Codex hook has drifted; refusing uninstall"
        )
    del groups[index]

    created = set(manifest.get("created_containers", []))
    if not groups and "UserPromptSubmit" in created:
        del hooks["UserPromptSubmit"]
    if not hooks and "hooks" in created:
        del result["hooks"]

    delete_file = False
    if manifest.get("created_file"):
        remaining = dict(result)
        if remaining.get("description") == HOOK_DESCRIPTION:
            del remaining["description"]
        delete_file = not remaining
    return result, delete_file


def uninstall_integration(
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Remove only the hook recorded as AIQ-owned."""

    effective_environment = os.environ if environment is None else environment
    target = _target_path(effective_environment)
    state_directory = _integration_state_directory(effective_environment, target)
    with _integration_lock(state_directory):
        manifest = _read_manifest(state_directory, target=target)
        if manifest is None:
            raise CodexIntegrationError("no AIQ Codex integration manifest exists")
        if manifest.get("status") == "uninstalled":
            return {
                "v": CONTRACT_VERSION,
                "integration": "codex",
                "status": "uninstalled",
                "action": "none",
                "target": os.fspath(target),
            }
        if (
            manifest.get("status") != "installed"
            or manifest.get("target") != os.fspath(target)
            or manifest.get("integration_id") != INTEGRATION_ID
        ):
            raise CodexIntegrationError("AIQ Codex integration manifest is invalid")

        before, document, before_status = _load_target(target)
        if before is None:
            raise CodexIntegrationError("the manifest-owned Codex hooks file is missing")
        after_document, delete_file = _remove_managed_group(document, manifest)
        after = None if delete_file else _encode_hooks(after_document)
        backup = _backup(state_directory, target, before)
        backups = list(manifest.get("backups", []))
        if backup is not None:
            backups.append({"path": backup, "sha256": _sha256(before)})

        _assert_target_unchanged(target, before, before_status)
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
                raise CodexIntegrationError("Codex hooks changed before mutation")
            mode = stat.S_IMODE(before_status.st_mode)
            _atomic_write(target, after, mode=mode)

        manifest.update(
            {
                "status": "uninstalled",
                "config_sha256": _sha256(after),
                "backups": backups,
            }
        )
        _write_manifest(state_directory, manifest)
        return {
            "v": CONTRACT_VERSION,
            "integration": "codex",
            "status": "uninstalled",
            "action": "uninstall",
            "target": os.fspath(target),
            "backup": backup,
            "changes": [
                {
                    "op": "remove",
                    "path": "/hooks/UserPromptSubmit/aiq-owned-group",
                    "integration_id": INTEGRATION_ID,
                }
            ],
        }


def receive_hook(
    payload: str | bytes,
    *,
    integration_id: str = INTEGRATION_ID,
    git_executable: str | Path | None = None,
    agent_root: Path | None = None,
) -> dict[str, Any]:
    """Validate and durably ingest one Codex ``UserPromptSubmit`` payload."""

    if integration_id != INTEGRATION_ID:
        raise CodexIntegrationError("unsupported Codex integration id")
    if git_executable is None:
        raise CodexIntegrationError(
            "Codex hook requires an absolute Git executable"
        )
    resolved_git_executable = _git_executable_path(git_executable)
    if isinstance(payload, str):
        raw = payload.encode()
    else:
        raw = payload
    if len(raw) > HOOK_INPUT_MAX_BYTES:
        raise CodexIntegrationError(
            f"Codex hook input exceeds {HOOK_INPUT_MAX_BYTES} bytes"
        )
    try:
        document = json.loads(raw, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexIntegrationError("Codex hook input is invalid JSON") from error
    if not isinstance(document, dict):
        raise CodexIntegrationError("Codex hook input must be a JSON object")
    if document.get("hook_event_name") != "UserPromptSubmit":
        raise CodexIntegrationError("Codex hook is not a UserPromptSubmit event")
    prompt = document.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise CodexIntegrationError("Codex hook has no non-empty string prompt")
    values: dict[str, str | None] = {}
    for field in ("session_id", "turn_id", "cwd"):
        value = document.get(field)
        if not isinstance(value, str) or not value:
            raise CodexIntegrationError(
                f"Codex hook {field} must be a non-empty string"
            )
        values[field] = value
    cwd = Path(values["cwd"])
    if not cwd.is_absolute() or not cwd.is_dir():
        raise CodexIntegrationError("Codex hook working directory is invalid")

    scope = resolve_scope(
        "auto",
        cwd=cwd,
        agent_root=agent_root,
        git_executable=resolved_git_executable,
    )
    result = ingest_message(
        scope,
        prompt,
        source="codex",
        session_id=values["session_id"],
        turn_id=values["turn_id"],
        cwd=os.fspath(cwd.resolve()),
    )
    return result.to_dict()


def _single_line(value: str) -> str:
    return "".join(
        character
        if character.isprintable() and character not in {"\t", "\r", "\n"}
        else f"\\u{ord(character):04x}"
        for character in value
    )


def receive_hook_main(
    *,
    input_stream: BinaryIO | None = None,
    error_stream: TextIO | None = None,
    integration_id: str = INTEGRATION_ID,
    git_executable: str | Path | None = None,
    agent_root: Path | None = None,
) -> int:
    """Run the fail-closed, stdout-silent Codex hook boundary."""

    source = sys.stdin.buffer if input_stream is None else input_stream
    errors = sys.stderr if error_stream is None else error_stream
    try:
        payload = source.read(HOOK_INPUT_MAX_BYTES + 1)
        if len(payload) > HOOK_INPUT_MAX_BYTES:
            raise CodexIntegrationError(
                f"Codex hook input exceeds {HOOK_INPUT_MAX_BYTES} bytes"
            )
        receive_hook(
            payload,
            integration_id=integration_id,
            git_executable=git_executable,
            agent_root=agent_root,
        )
        return 0
    except Exception as error:
        errors.write(f"AIQ prompt capture failed: {_single_line(str(error))}\n")
        errors.flush()
        return 2
