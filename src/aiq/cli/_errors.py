from __future__ import annotations

import json
import sqlite3

from aiq.config import ConfigError
from aiq.events import EventError
from aiq.integrations import guidance
from aiq.integrations._hooks import HookIntegrationError
from aiq.journal import JournalError


# Stable machine-readable codes set at JournalError raise sites, mapped to
# their (code, exit) classification. Codes take precedence over the substring
# fallback rules below.
_JOURNAL_ERROR_CODE_EXITS: dict[str, tuple[str, int]] = {
    "contention": ("contention", 4),
    "invalid_argument": ("invalid_argument", 2),
    "invalid_document": ("invalid_document", 2),
    "not_claimable": ("not_claimable", 4),
    "not_found": ("not_found", 3),
    "reader_held": ("reader_held", 4),
    "state_conflict": ("state_conflict", 4),
}


def _classify_journal_error(error: JournalError) -> tuple[str, int]:
    explicit = _JOURNAL_ERROR_CODE_EXITS.get(getattr(error, "code", None))
    if explicit is not None:
        return explicit
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
            "must be positive",
            "unsupported claim",
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
    if isinstance(error, guidance.GuidanceIntegrationError):
        message = str(error).lower()
        if (
            "--target" in message
            or "--user" in message
            or "--launcher" in message
            or "--git-executable" in message
            or "control characters" in message
        ):
            return "invalid_argument", 2
        return "integration_drift", 6
    if isinstance(error, HookIntegrationError):
        message = str(error).lower()
        if "requires --user" in message or "not --target" in message:
            return "invalid_argument", 2
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
