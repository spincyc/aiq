"""Strict, layered configuration for AIQ."""

from __future__ import annotations

from dataclasses import dataclass
import getpass
import os
from pathlib import Path
import re
import stat
import subprocess
import tomllib
from types import MappingProxyType
from typing import Any, Mapping
import unicodedata


CONFIG_VERSION = 1
CONFIG_MAX_BYTES = 65536

CONFIG_KEYS = frozenset(
    {
        "scope",
        "owner",
        "lease_seconds",
        "snapshot_keep",
        "output",
    }
)
USER_CONFIG_KEYS = CONFIG_KEYS
REPO_CONFIG_KEYS = frozenset(
    {
        "lease_seconds",
    }
)

ENVIRONMENT_KEYS = {
    "AIQ_SCOPE": "scope",
    "AIQ_OWNER": "owner",
    "AIQ_LEASE_SECONDS": "lease_seconds",
    "AIQ_SNAPSHOT_KEEP": "snapshot_keep",
    "AIQ_OUTPUT": "output",
}

_INTEGER_PATTERN = re.compile(r"[0-9]+\Z")
_DISCOVER = object()


class ConfigError(ValueError):
    """Raised when AIQ configuration is invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    """Effective configuration and the winning source for each value."""

    version: int
    scope: str
    owner: str
    lease_seconds: int
    snapshot_keep: int
    output: str
    sources: Mapping[str, str]

    def to_dict(self, *, include_sources: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "version": self.version,
            "scope": self.scope,
            "owner": self.owner,
            "lease_seconds": self.lease_seconds,
            "snapshot_keep": self.snapshot_keep,
            "output": self.output,
        }
        if include_sources:
            result["sources"] = dict(self.sources)
        return result


def _validate_string(
    value: object,
    *,
    key: str,
    minimum: int = 1,
    maximum: int,
) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    if not minimum <= len(value) <= maximum:
        raise ConfigError(
            f"{key} length must be between {minimum} and {maximum}"
        )
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ConfigError(f"{key} must not contain control characters")
    return value


def _validate_integer(
    value: object,
    *,
    key: str,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int:
        raise ConfigError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} must be between {minimum} and {maximum}")
    return value


def _validate_value(key: str, value: object) -> str | int:
    if key == "scope":
        scope = _validate_string(value, key=key, maximum=20)
        if scope not in {"auto", "repo", "user"}:
            raise ConfigError("scope must be one of: auto, repo, user")
        return scope
    if key == "owner":
        return _validate_string(value, key=key, maximum=200)
    if key == "lease_seconds":
        return _validate_integer(
            value,
            key=key,
            minimum=1,
            maximum=86400,
        )
    if key == "snapshot_keep":
        return _validate_integer(
            value,
            key=key,
            minimum=1,
            maximum=10000,
        )
    if key == "output":
        output = _validate_string(value, key=key, maximum=10)
        if output not in {"human", "json"}:
            raise ConfigError("output must be one of: human, json")
        return output
    raise ConfigError(f"unknown configuration key: {key}")


def _validate_layer(
    values: Mapping[str, object],
    *,
    allowed: frozenset[str],
    source: str,
    ignore_none: bool = False,
) -> dict[str, str | int]:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ConfigError(
            f"{source} does not allow configuration key: {unknown[0]}"
        )
    return {
        key: _validate_value(key, value)
        for key, value in values.items()
        if not (ignore_none and value is None)
    }


def _read_bounded_regular_file(path: Path, *, allow_symlink: bool) -> bytes:
    candidate = path
    if candidate.is_symlink():
        if not allow_symlink:
            raise ConfigError(
                f"repository configuration must not be a symlink: {path}"
            )
        try:
            candidate = candidate.resolve(strict=True)
        except OSError as error:
            raise ConfigError(
                f"cannot resolve configuration file {path}: {error}"
            ) from error

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as error:
        raise ConfigError(f"cannot open configuration file {path}: {error}") from error
    try:
        file_status = os.fstat(descriptor)
        if not stat.S_ISREG(file_status.st_mode):
            raise ConfigError(f"configuration is not a regular file: {path}")
        chunks: list[bytes] = []
        remaining = CONFIG_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(data) > CONFIG_MAX_BYTES:
        raise ConfigError(
            f"configuration exceeds {CONFIG_MAX_BYTES} bytes: {path}"
        )
    return data


def load_config_file(
    path: Path,
    *,
    source: str,
) -> dict[str, str | int]:
    """Load one user or repository TOML file.

    Missing files contribute no values. User configuration may be symlinked;
    repository configuration must be a regular file at the repository root.
    """

    if source not in {"user", "repo"}:
        raise ConfigError(f"unsupported configuration source: {source}")
    if not path.exists() and not path.is_symlink():
        return {}

    raw = _read_bounded_regular_file(path, allow_symlink=source == "user")
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ConfigError(f"configuration is not valid UTF-8: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML configuration {path}: {error}") from error

    version = document.pop("version", None)
    if type(version) is not int or version != CONFIG_VERSION:
        raise ConfigError(
            f"configuration {path} must declare version = {CONFIG_VERSION}"
        )
    allowed = USER_CONFIG_KEYS if source == "user" else REPO_CONFIG_KEYS
    return _validate_layer(
        document,
        allowed=allowed,
        source=f"{source} configuration {path}",
    )


def user_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the deterministic XDG user configuration path."""

    environment = os.environ if environ is None else environ
    configured = environment.get("XDG_CONFIG_HOME")
    if configured:
        config_home = Path(configured)
        if not config_home.is_absolute():
            raise ConfigError("XDG_CONFIG_HOME must be an absolute path")
    else:
        home = environment.get("HOME")
        home_path = Path(home).expanduser() if home else Path.home()
        if not home_path.is_absolute():
            raise ConfigError("HOME must be an absolute path")
        config_home = home_path / ".config"
    return config_home / "aiq" / "config.toml"


def repository_config_path(cwd: Path) -> Path | None:
    """Return the exact worktree-root configuration path, when inside Git."""

    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("GIT_")
    }
    result = subprocess.run(
        ["git", "-C", str(cwd.resolve()), "rev-parse", "--show-toplevel"],
        check=False,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode:
        return None
    root = Path(result.stdout.strip())
    if not root.is_absolute():
        raise ConfigError("Git returned a non-absolute repository root")
    return root / ".aiq.toml"


def _environment_layer(
    environ: Mapping[str, str],
) -> tuple[dict[str, str | int], dict[str, str]]:
    values: dict[str, object] = {}
    sources: dict[str, str] = {}
    for environment_key, config_key in ENVIRONMENT_KEYS.items():
        if environment_key not in environ:
            continue
        raw = environ[environment_key]
        if config_key in {"lease_seconds", "snapshot_keep"}:
            if not _INTEGER_PATTERN.fullmatch(raw):
                raise ConfigError(
                    f"{environment_key} must be an unsigned decimal integer"
                )
            value: object = int(raw)
        else:
            value = raw
        values[config_key] = value
        sources[config_key] = f"env:{environment_key}"
    return (
        _validate_layer(values, allowed=CONFIG_KEYS, source="environment"),
        sources,
    )


def _default_owner() -> str:
    try:
        owner = getpass.getuser()
    except (KeyError, OSError):
        owner = ""
    if not owner:
        owner = f"uid-{os.getuid()}"
    return _validate_string(owner, key="owner", maximum=200)


def resolve_config(
    *,
    cwd: Path | None = None,
    cli: Mapping[str, object] | None = None,
    environ: Mapping[str, str] | None = None,
    user_path: Path | None | object = _DISCOVER,
    repo_path: Path | None | object = _DISCOVER,
    default_owner: str | None = None,
) -> Config:
    """Resolve defaults < user < repository < environment < CLI.

    Pass ``None`` for either path to disable that file layer. Omitting a path
    discovers the standard XDG or Git worktree-root location.
    """

    environment = os.environ if environ is None else environ
    effective_cwd = (cwd or Path.cwd()).resolve()
    owner = _validate_value(
        "owner",
        _default_owner() if default_owner is None else default_owner,
    )
    values: dict[str, str | int] = {
        "scope": "auto",
        "owner": owner,
        "lease_seconds": 900,
        "snapshot_keep": 5,
        "output": "human",
    }
    sources = {key: "default" for key in values}

    effective_user_path = (
        user_config_path(environment) if user_path is _DISCOVER else user_path
    )
    if effective_user_path is not None:
        if not isinstance(effective_user_path, Path):
            raise TypeError("user_path must be a pathlib.Path or None")
        layer = load_config_file(effective_user_path, source="user")
        values.update(layer)
        sources.update({key: f"user:{effective_user_path}" for key in layer})

    effective_repo_path = (
        repository_config_path(effective_cwd)
        if repo_path is _DISCOVER
        else repo_path
    )
    if effective_repo_path is not None:
        if not isinstance(effective_repo_path, Path):
            raise TypeError("repo_path must be a pathlib.Path or None")
        layer = load_config_file(effective_repo_path, source="repo")
        values.update(layer)
        sources.update({key: f"repo:{effective_repo_path}" for key in layer})

    environment_values, environment_sources = _environment_layer(environment)
    values.update(environment_values)
    sources.update(environment_sources)

    cli_values = _validate_layer(
        cli or {},
        allowed=CONFIG_KEYS,
        source="CLI",
        ignore_none=True,
    )
    values.update(cli_values)
    sources.update({key: "cli" for key in cli_values})

    return Config(
        version=CONFIG_VERSION,
        scope=str(values["scope"]),
        owner=str(values["owner"]),
        lease_seconds=int(values["lease_seconds"]),
        snapshot_keep=int(values["snapshot_keep"]),
        output=str(values["output"]),
        sources=MappingProxyType(dict(sources)),
    )
