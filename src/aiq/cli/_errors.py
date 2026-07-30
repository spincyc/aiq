from __future__ import annotations

import json
import sqlite3

from aiq.config import ConfigError
from aiq.events import EventError
from aiq.integrations import guidance
from aiq.integrations._hooks import HookIntegrationError
from aiq.journal import JournalError


# Stable machine-readable codes set at JournalError raise sites, mapped to
# their (code, exit) classification. Every raise site in the tree sets one, so
# a diagnostic can be reworded without changing the documented code.
_JOURNAL_ERROR_CODE_EXITS: dict[str, tuple[str, int]] = {
    "claim_expired": ("claim_expired", 4),
    "claim_mismatch": ("claim_mismatch", 4),
    "contention": ("contention", 4),
    "integrity_failed": ("integrity_failed", 5),
    "internal_error": ("internal_error", 70),
    "invalid_argument": ("invalid_argument", 2),
    "invalid_document": ("invalid_document", 2),
    "io_error": ("io_error", 6),
    "not_claimable": ("not_claimable", 4),
    "not_found": ("not_found", 3),
    "reader_held": ("reader_held", 4),
    "revision_conflict": ("revision_conflict", 4),
    "schema_incompatible": ("schema_incompatible", 5),
    "state_conflict": ("state_conflict", 4),
    "unsupported_environment": ("unsupported_environment", 6),
}


def _classify_journal_error(error: JournalError) -> tuple[str, int]:
    explicit = _JOURNAL_ERROR_CODE_EXITS.get(getattr(error, "code", None))
    if explicit is not None:
        return explicit
    # Every `JournalError` raise site sets a known `code=`, which
    # `JournalErrorRaiseSiteCoverageTests` enforces, so reaching here means
    # a site was added without one or with a code missing from the table
    # above. That is an AIQ defect, not a classifiable journal outcome, and
    # it is reported as one rather than guessed at from the wording.
    return "internal_error", 70


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
