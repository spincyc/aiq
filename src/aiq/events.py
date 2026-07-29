from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


EVENT_VERSION = 1
EVENT_JSON_MAX_BYTES = 8 * 1024 * 1024
CONTENT_MAX_BYTES = 1024 * 1024
SOURCE_MAX_BYTES = 64
IDEMPOTENCY_KEY_MAX_BYTES = 512
CONTEXT_ID_MAX_BYTES = 256
CWD_MAX_BYTES = 4096

_SOURCE_PATTERN = re.compile(r"[a-z][a-z0-9._-]*")
_REQUIRED_FIELDS = frozenset({"v", "source", "content"})
_OPTIONAL_FIELDS = frozenset(
    {"idempotency_key", "session_id", "turn_id", "cwd"}
)
_ALLOWED_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS


class EventError(ValueError):
    """A canonical event could not be decoded or validated."""

    def __init__(self, code: str, message: str, *, path: str = "$") -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True)
class CanonicalEvent:
    """Provider-neutral input for one exact message."""

    source: str
    content: str
    idempotency_key: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    cwd: str | None = None
    v: int = EVENT_VERSION

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "v": self.v,
            "source": self.source,
            "content": self.content,
        }
        for name in ("idempotency_key", "session_id", "turn_id", "cwd"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result


class _DuplicateKey(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class _NonstandardConstant(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str) -> None:
    raise _NonstandardConstant(value)


def _utf8_size(value: str, *, path: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise EventError(
            "invalid_event",
            f"{path} must contain valid Unicode",
            path=path,
        ) from error


def _required_string(
    document: dict[str, Any],
    name: str,
    *,
    maximum_bytes: int,
) -> str:
    path = f"$.{name}"
    value = document.get(name)
    if not isinstance(value, str):
        raise EventError(
            "invalid_event",
            f"{path} must be a string",
            path=path,
        )
    if value == "":
        raise EventError(
            "invalid_event",
            f"{path} must not be empty",
            path=path,
        )
    if _utf8_size(value, path=path) > maximum_bytes:
        raise EventError(
            "invalid_event",
            f"{path} exceeds {maximum_bytes} UTF-8 bytes",
            path=path,
        )
    return value


def _optional_string(
    document: dict[str, Any],
    name: str,
    *,
    maximum_bytes: int,
) -> str | None:
    if name not in document:
        return None
    return _required_string(document, name, maximum_bytes=maximum_bytes)


def _decode_document(raw: bytes | str) -> dict[str, Any]:
    if isinstance(raw, bytes):
        encoded = raw
        if len(encoded) > EVENT_JSON_MAX_BYTES:
            raise EventError(
                "event_too_large",
                f"event JSON exceeds {EVENT_JSON_MAX_BYTES} bytes",
            )
        try:
            text = encoded.decode("utf-8")
        except UnicodeDecodeError as error:
            raise EventError(
                "invalid_utf8",
                "event JSON is not valid UTF-8",
            ) from error
    elif isinstance(raw, str):
        try:
            encoded = raw.encode("utf-8")
        except UnicodeEncodeError as error:
            raise EventError(
                "invalid_utf8",
                "event JSON is not valid Unicode",
            ) from error
        if len(encoded) > EVENT_JSON_MAX_BYTES:
            raise EventError(
                "event_too_large",
                f"event JSON exceeds {EVENT_JSON_MAX_BYTES} bytes",
            )
        text = raw
    else:
        raise EventError(
            "invalid_input",
            "event JSON must be bytes or a string",
        )

    try:
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
    except _DuplicateKey as error:
        raise EventError(
            "invalid_json",
            f"event JSON repeats key {error.key!r}",
            path=f"$.{error.key}",
        ) from error
    except _NonstandardConstant as error:
        raise EventError(
            "invalid_json",
            f"event JSON contains nonstandard constant {error}",
        ) from error
    except json.JSONDecodeError as error:
        raise EventError(
            "invalid_json",
            f"event JSON is invalid at line {error.lineno}, column {error.colno}",
        ) from error

    if not isinstance(document, dict):
        raise EventError(
            "invalid_event",
            "event JSON must be an object",
        )
    return document


def validate_event(document: object) -> CanonicalEvent:
    """Strictly validate a decoded canonical event document."""

    if not isinstance(document, dict):
        raise EventError(
            "invalid_event",
            "event must be an object",
        )
    if any(not isinstance(name, str) for name in document):
        raise EventError(
            "invalid_event",
            "event field names must be strings",
        )
    missing = sorted(_REQUIRED_FIELDS - document.keys())
    if missing:
        name = missing[0]
        raise EventError(
            "invalid_event",
            f"$.{name} is required",
            path=f"$.{name}",
        )
    unknown = sorted(document.keys() - _ALLOWED_FIELDS)
    if unknown:
        name = unknown[0]
        raise EventError(
            "invalid_event",
            f"$.{name} is not allowed",
            path=f"$.{name}",
        )

    version = document["v"]
    if type(version) is not int or version != EVENT_VERSION:
        raise EventError(
            "unsupported_event_version",
            f"$.v must be the integer {EVENT_VERSION}",
            path="$.v",
        )

    source = _required_string(
        document,
        "source",
        maximum_bytes=SOURCE_MAX_BYTES,
    )
    if _SOURCE_PATTERN.fullmatch(source) is None:
        raise EventError(
            "invalid_event",
            "$.source must match [a-z][a-z0-9._-]*",
            path="$.source",
        )
    content = _required_string(
        document,
        "content",
        maximum_bytes=CONTENT_MAX_BYTES,
    )
    idempotency_key = _optional_string(
        document,
        "idempotency_key",
        maximum_bytes=IDEMPOTENCY_KEY_MAX_BYTES,
    )
    session_id = _optional_string(
        document,
        "session_id",
        maximum_bytes=CONTEXT_ID_MAX_BYTES,
    )
    turn_id = _optional_string(
        document,
        "turn_id",
        maximum_bytes=CONTEXT_ID_MAX_BYTES,
    )
    cwd = _optional_string(
        document,
        "cwd",
        maximum_bytes=CWD_MAX_BYTES,
    )
    if cwd is not None:
        if "\x00" in cwd:
            raise EventError(
                "invalid_event",
                "$.cwd must not contain NUL",
                path="$.cwd",
            )
        if not Path(cwd).is_absolute():
            raise EventError(
                "invalid_event",
                "$.cwd must be an absolute path",
                path="$.cwd",
            )

    return CanonicalEvent(
        v=version,
        source=source,
        content=content,
        idempotency_key=idempotency_key,
        session_id=session_id,
        turn_id=turn_id,
        cwd=cwd,
    )


def parse_event_json(raw: bytes | str) -> CanonicalEvent:
    """Decode and strictly validate one canonical event JSON document."""

    return validate_event(_decode_document(raw))
