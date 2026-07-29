"""Reversible AIQ-owned block in an explicitly selected guidance file."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import stat
import time
from typing import Any, Iterator, Mapping

from aiq.journal import JournalError


CONTRACT_VERSION = 1
INTEGRATION_ID = "aiq-workqueue.guidance.v1"
GUIDANCE_MAX_BYTES = 1_048_576
BEGIN_MARKER = f"<!-- aiq-guidance-v1:begin id={INTEGRATION_ID} -->"
END_MARKER = f"<!-- aiq-guidance-v1:end id={INTEGRATION_ID} -->"


class GuidanceIntegrationError(JournalError):
    """The guidance integration cannot be inspected or changed safely."""


def _home(environment: Mapping[str, str]) -> Path:
    configured = environment.get("HOME")
    if not configured:
        return Path.home()
    path = Path(configured)
    if not path.is_absolute():
        raise GuidanceIntegrationError("HOME must be an absolute path")
    return path


def _state_home(environment: Mapping[str, str]) -> Path:
    configured = environment.get("XDG_STATE_HOME")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise GuidanceIntegrationError(
                "XDG_STATE_HOME must be an absolute path"
            )
        return path
    return _home(environment) / ".local" / "state"


def _target_path(target: str | Path | None) -> Path:
    if target is None:
        raise GuidanceIntegrationError(
            "guidance integration requires an explicit absolute --target path"
        )
    path = Path(target)
    if not path.is_absolute():
        raise GuidanceIntegrationError(
            "guidance --target must be an absolute path"
        )
    if any(character in os.fspath(path) for character in ("\0", "\r", "\n")):
        raise GuidanceIntegrationError(
            "guidance --target path contains control characters"
        )
    return path


def _integration_state_directory(
    environment: Mapping[str, str],
    target: Path,
) -> Path:
    target_id = hashlib.sha256(os.fsencode(target)).hexdigest()[:16]
    return (
        _state_home(environment)
        / "aiq"
        / "integrations"
        / "guidance"
        / target_id
    )


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    status = path.lstat()
    if (
        not stat.S_ISDIR(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or status.st_uid != os.getuid()
    ):
        raise GuidanceIntegrationError(
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
        raise GuidanceIntegrationError(
            f"{label} is not a safe regular file: {path}"
        ) from error
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.getuid()
            or status.st_nlink != 1
        ):
            raise GuidanceIntegrationError(
                f"{label} is not a safe regular file: {path}"
            )
        if status.st_size > maximum_bytes:
            raise GuidanceIntegrationError(
                f"{label} exceeds {maximum_bytes} bytes"
            )
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
            raise GuidanceIntegrationError(
                f"{label} exceeds {maximum_bytes} bytes"
            )
        return data, status
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, data: bytes, *, mode: int) -> None:
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
            raise GuidanceIntegrationError(
                f"integration lock is unsafe: {lock_path}"
            )
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _sha256(data: bytes | None) -> str | None:
    if data is None:
        return None
    return hashlib.sha256(data).hexdigest()


def _valid_digest(value: Any, *, optional: bool = False) -> bool:
    if optional and value is None:
        return True
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _bootstrap_block() -> str:
    content = (
        resources.files("aiq._resources")
        .joinpath("AGENTS.md")
        .read_text(encoding="utf-8")
    )
    if not content.endswith("\n"):
        content += "\n"
    return f"{BEGIN_MARKER}\n{content}{END_MARKER}\n"


def _load_target(
    target: Path,
) -> tuple[bytes | None, os.stat_result | None]:
    try:
        data, status = _read_bounded_with_status(
            target,
            GUIDANCE_MAX_BYTES,
            label="guidance target",
        )
    except FileNotFoundError:
        return None, None
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GuidanceIntegrationError(
            f"guidance target is not UTF-8: {target}"
        ) from error
    return data, status


def _locate_block(data: bytes, *, target: Path) -> tuple[int, int] | None:
    begin = BEGIN_MARKER.encode()
    end = END_MARKER.encode()
    begin_count = data.count(begin)
    end_count = data.count(end)
    if not begin_count and not end_count:
        return None
    if begin_count != 1 or end_count != 1:
        raise GuidanceIntegrationError(
            f"guidance markers are ambiguous: {target}"
        )
    start = data.find(begin)
    stop = data.find(end)
    if (
        stop < start
        or (start and not data[:start].endswith(b"\n"))
        or data[start + len(begin) : start + len(begin) + 1] != b"\n"
    ):
        raise GuidanceIntegrationError(
            f"guidance markers are malformed: {target}"
        )
    stop += len(end)
    if stop < len(data):
        if data[stop : stop + 1] != b"\n":
            raise GuidanceIntegrationError(
                f"guidance markers are malformed: {target}"
            )
        stop += 1
    return start, stop


def _manifest_path(state_directory: Path) -> Path:
    return state_directory / "manifest.json"


def _object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GuidanceIntegrationError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _validate_manifest(
    manifest: Any,
    *,
    state_directory: Path,
    target: Path,
) -> dict[str, Any]:
    required = {
        "backups",
        "config_sha256",
        "created_file",
        "integration",
        "integration_id",
        "managed_block",
        "managed_block_sha256",
        "separator",
        "status",
        "target",
        "v",
    }
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise GuidanceIntegrationError(
            "integration manifest has an invalid schema"
        )
    if (
        type(manifest["v"]) is not int
        or manifest["v"] != CONTRACT_VERSION
        or manifest["status"] not in ("installed", "uninstalled")
        or manifest["integration"] != "guidance"
        or manifest["integration_id"] != INTEGRATION_ID
        or manifest["target"] != os.fspath(target)
        or not isinstance(manifest["created_file"], bool)
        or manifest["separator"] not in ("", "\n")
    ):
        raise GuidanceIntegrationError(
            "integration manifest has invalid ownership"
        )
    block = manifest["managed_block"]
    if (
        not isinstance(block, str)
        or len(block.encode()) > GUIDANCE_MAX_BYTES
        or not block.startswith(f"{BEGIN_MARKER}\n")
        or not block.endswith(f"\n{END_MARKER}\n")
        or block.count(BEGIN_MARKER) != 1
        or block.count(END_MARKER) != 1
    ):
        raise GuidanceIntegrationError(
            "integration manifest owned block is invalid"
        )
    if (
        not _valid_digest(manifest["managed_block_sha256"])
        or manifest["managed_block_sha256"] != _sha256(block.encode())
        or not _valid_digest(manifest["config_sha256"], optional=True)
        or (
            manifest["status"] == "installed"
            and manifest["config_sha256"] is None
        )
    ):
        raise GuidanceIntegrationError("integration manifest digest is invalid")
    backups = manifest["backups"]
    if not isinstance(backups, list):
        raise GuidanceIntegrationError(
            "integration manifest backups are invalid"
        )
    backup_directory = state_directory / "backups"
    for backup in backups:
        if (
            not isinstance(backup, dict)
            or set(backup) != {"path", "sha256"}
            or not isinstance(backup["path"], str)
            or Path(backup["path"]).parent != backup_directory
            or not _valid_digest(backup["sha256"])
        ):
            raise GuidanceIntegrationError(
                "integration manifest backup is invalid"
            )
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
            raise GuidanceIntegrationError(
                f"integration state directory is unsafe: {state_directory}"
            )
    path = _manifest_path(state_directory)
    if not path.exists() and not path.is_symlink():
        return None
    data, _ = _read_bounded_with_status(
        path,
        262_144,
        label="integration manifest",
    )
    try:
        manifest = json.loads(data, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GuidanceIntegrationError("integration manifest is invalid") from error
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


def _assert_target_unchanged(
    target: Path,
    expected: bytes | None,
    expected_status: os.stat_result | None,
) -> None:
    try:
        current, status = _read_bounded_with_status(
            target,
            GUIDANCE_MAX_BYTES,
            label="guidance target",
        )
    except FileNotFoundError:
        if expected is None and expected_status is None:
            return
        raise GuidanceIntegrationError("guidance target changed before mutation")
    if expected is None or expected_status is None:
        raise GuidanceIntegrationError("guidance target changed before mutation")
    if (
        current != expected
        or status.st_dev != expected_status.st_dev
        or status.st_ino != expected_status.st_ino
    ):
        raise GuidanceIntegrationError("guidance target changed before mutation")


def _build_plan(
    *,
    target: str | Path,
    environment: Mapping[str, str] | None = None,
    repair: bool = False,
) -> dict[str, Any]:
    """Return the exact safe mutation needed for the selected target."""

    effective_environment = os.environ if environment is None else environment
    resolved_target = _target_path(target)
    state_directory = _integration_state_directory(
        effective_environment,
        resolved_target,
    )
    block = _bootstrap_block()
    desired = block.encode()
    result: dict[str, Any] = {
        "v": CONTRACT_VERSION,
        "integration": "guidance",
        "integration_id": INTEGRATION_ID,
        "target": os.fspath(resolved_target),
        "state_directory": os.fspath(state_directory),
        "block_sha256": _sha256(desired),
        "status": "unknown",
        "action": "block",
        "blocked_reason": None,
        "changes": [],
        "_block": block,
    }
    try:
        before, _ = _load_target(resolved_target)
        manifest = _read_manifest(state_directory, target=resolved_target)
    except GuidanceIntegrationError as error:
        result["status"] = "unsafe"
        result["blocked_reason"] = str(error)
        return result

    result["before_sha256"] = _sha256(before)
    try:
        region = (
            None
            if before is None
            else _locate_block(before, target=resolved_target)
        )
    except GuidanceIntegrationError as error:
        result["status"] = "conflict"
        result["blocked_reason"] = str(error)
        return result
    current = None if region is None else before[region[0] : region[1]]

    manifest_active = (
        manifest is not None and manifest.get("status") == "installed"
    )
    manifest_block_mismatch = bool(
        manifest_active
        and current is not None
        and manifest["managed_block"].encode() != current
        and current == desired
    )
    if current is not None and not manifest_active:
        result["status"] = "unmanaged"
        result["blocked_reason"] = (
            "an AIQ-marked guidance block exists without an active AIQ manifest"
        )
        return result

    created_file = before is None
    separator = ""
    if current is None:
        if manifest_active and not repair:
            result["status"] = "drifted"
            result["blocked_reason"] = (
                "the manifest-owned guidance block is missing"
            )
            return result
        if before:
            if not before.endswith(b"\n"):
                separator = "\n"
            after = before + separator.encode() + desired
        else:
            after = desired
        action = "repair" if manifest_active else "install"
        operation = "add"
    elif current != desired:
        if not repair:
            result["status"] = "drifted"
            result["blocked_reason"] = (
                "the AIQ-owned guidance block differs from the packaged content"
            )
            return result
        after = before[: region[0]] + desired + before[region[1] :]
        action = "repair"
        operation = "replace"
        created_file = manifest["created_file"]
        separator = manifest["separator"]
    elif manifest_block_mismatch:
        if not repair:
            result["status"] = "drifted"
            result["blocked_reason"] = (
                "the integration manifest differs from the guidance block"
            )
            return result
        after = before
        action = "repair"
        operation = "replace"
        created_file = manifest["created_file"]
        separator = manifest["separator"]
    else:
        result.update(
            {
                "status": "installed",
                "action": "none",
                "after_sha256": _sha256(before),
                "plan_token": hashlib.sha256(
                    f"{resolved_target}\0{_sha256(before)}\0"
                    f"{_sha256(before)}".encode()
                ).hexdigest(),
            }
        )
        return result

    before_sha = _sha256(before)
    after_sha = _sha256(after)
    result.update(
        {
            "status": "absent" if action == "install" else "drifted",
            "action": action,
            "created_file": created_file,
            "separator": separator,
            "after_sha256": after_sha,
            "plan_token": hashlib.sha256(
                f"{resolved_target}\0{before_sha}\0{after_sha}".encode()
            ).hexdigest(),
            "changes": [
                {
                    "op": operation,
                    "path": "/aiq-owned-block",
                    "block_sha256": result["block_sha256"],
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
    target: str | Path,
    environment: Mapping[str, str] | None = None,
    repair: bool = False,
) -> dict[str, Any]:
    """Return a JSON-safe description of the owned change only."""

    return _public_plan(
        _build_plan(target=target, environment=environment, repair=repair)
    )


def _backup(state_directory: Path, data: bytes | None) -> str | None:
    if data is None:
        return None
    backup_directory = state_directory / "backups"
    _ensure_private_directory(backup_directory)
    digest = _sha256(data)
    path = backup_directory / f"{time.time_ns()}-{digest}.guidance.md"
    _atomic_write(path, data, mode=0o600)
    return os.fspath(path)


def install_integration(
    *,
    target: str | Path,
    environment: Mapping[str, str] | None = None,
    repair: bool = False,
    plan_token: str | None = None,
) -> dict[str, Any]:
    """Install or explicitly repair the owned block in the selected target."""

    effective_environment = os.environ if environment is None else environment
    resolved_target = _target_path(target)
    state_directory = _integration_state_directory(
        effective_environment,
        resolved_target,
    )
    with _integration_lock(state_directory):
        plan = _build_plan(
            target=resolved_target,
            environment=effective_environment,
            repair=repair,
        )
        if plan_token is not None and plan.get("plan_token") != plan_token:
            raise GuidanceIntegrationError("reviewed integration plan is stale")
        if plan["action"] == "block":
            raise GuidanceIntegrationError(plan["blocked_reason"])
        if plan["action"] == "none":
            return _public_plan(plan)

        before, before_status = _load_target(resolved_target)
        if _sha256(before) != plan["before_sha256"]:
            raise GuidanceIntegrationError(
                "guidance target changed while installing"
            )
        previous_manifest = _read_manifest(
            state_directory,
            target=resolved_target,
        )
        backups = (
            list(previous_manifest.get("backups", []))
            if isinstance(previous_manifest, dict)
            else []
        )
        backup = _backup(state_directory, before)
        if backup is not None:
            backups.append({"path": backup, "sha256": plan["before_sha256"]})

        mode = (
            0o644
            if before_status is None
            else stat.S_IMODE(before_status.st_mode)
        )
        _assert_target_unchanged(resolved_target, before, before_status)
        _atomic_write(resolved_target, plan["_after"], mode=mode)
        manifest = {
            "v": CONTRACT_VERSION,
            "status": "installed",
            "integration": "guidance",
            "integration_id": INTEGRATION_ID,
            "target": os.fspath(resolved_target),
            "managed_block": plan["_block"],
            "managed_block_sha256": plan["block_sha256"],
            "separator": plan["separator"],
            "config_sha256": plan["after_sha256"],
            "created_file": plan["created_file"],
            "backups": backups,
        }
        _write_manifest(state_directory, manifest)
        result = _public_plan(plan)
        result["status"] = "installed"
        result["backup"] = backup
        return result


def check_integration(
    *,
    target: str | Path,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Inspect ownership and drift without mutating anything."""

    plan = _build_plan(target=target, environment=environment)
    result = _public_plan(plan)
    result["ok"] = plan["status"] == "installed" and plan["action"] == "none"
    return result


def uninstall_integration(
    *,
    target: str | Path,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Remove only the block recorded as AIQ-owned."""

    effective_environment = os.environ if environment is None else environment
    resolved_target = _target_path(target)
    state_directory = _integration_state_directory(
        effective_environment,
        resolved_target,
    )
    with _integration_lock(state_directory):
        manifest = _read_manifest(state_directory, target=resolved_target)
        if manifest is None:
            raise GuidanceIntegrationError(
                "no AIQ guidance integration manifest exists"
            )
        if manifest["status"] == "uninstalled":
            return {
                "v": CONTRACT_VERSION,
                "integration": "guidance",
                "integration_id": INTEGRATION_ID,
                "status": "uninstalled",
                "action": "none",
                "target": os.fspath(resolved_target),
            }

        before, before_status = _load_target(resolved_target)
        if before is None:
            raise GuidanceIntegrationError(
                "the manifest-owned guidance file is missing"
            )
        region = _locate_block(before, target=resolved_target)
        if region is None:
            raise GuidanceIntegrationError(
                "the manifest-owned guidance block is missing"
            )
        start, stop = region
        block = manifest["managed_block"].encode()
        if before[start:stop] != block:
            raise GuidanceIntegrationError(
                "the AIQ-owned guidance block has drifted; refusing uninstall"
            )
        separator = manifest["separator"].encode()
        if separator and before[start - len(separator) : start] == separator:
            start -= len(separator)
        after = before[:start] + before[stop:]
        delete_file = manifest["created_file"] and after == b""

        backup = _backup(state_directory, before)
        backups = list(manifest.get("backups", []))
        if backup is not None:
            backups.append({"path": backup, "sha256": _sha256(before)})

        _assert_target_unchanged(resolved_target, before, before_status)
        if delete_file:
            resolved_target.unlink()
            directory_descriptor = os.open(
                resolved_target.parent,
                os.O_RDONLY | os.O_CLOEXEC,
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        else:
            _atomic_write(
                resolved_target,
                after,
                mode=stat.S_IMODE(before_status.st_mode),
            )

        manifest.update(
            {
                "status": "uninstalled",
                "config_sha256": None if delete_file else _sha256(after),
                "backups": backups,
            }
        )
        _write_manifest(state_directory, manifest)
        return {
            "v": CONTRACT_VERSION,
            "integration": "guidance",
            "integration_id": INTEGRATION_ID,
            "status": "uninstalled",
            "action": "uninstall",
            "target": os.fspath(resolved_target),
            "deleted_file": delete_file,
            "backup": backup,
            "changes": [
                {
                    "op": "remove",
                    "path": "/aiq-owned-block",
                    "integration_id": INTEGRATION_ID,
                }
            ],
        }
