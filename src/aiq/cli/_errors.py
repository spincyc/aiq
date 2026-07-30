from __future__ import annotations

import json
import sqlite3

from aiq.config import ConfigError
from aiq.events import EventError
from aiq.integrations import guidance
from aiq.integrations._hooks import HookIntegrationError
from aiq.journal import JournalError


# Every stable code in docs/contracts/errors.md, mapped to its (code, exit)
# classification. Codes are set at the raise site that detects the failure —
# on `JournalError` and on every subclass of it, the integration errors
# included — so a diagnostic can be reworded without changing the code. The
# table must stay complete: a code absent from it is not pinnable, because
# `_classify_error` falls through to the legacy rules below instead.
_JOURNAL_ERROR_CODE_EXITS: dict[str, tuple[str, int]] = {
    "claim_expired": ("claim_expired", 4),
    "claim_mismatch": ("claim_mismatch", 4),
    "contention": ("contention", 4),
    "integration_drift": ("integration_drift", 6),
    "integrity_failed": ("integrity_failed", 5),
    "internal_error": ("internal_error", 70),
    "invalid_argument": ("invalid_argument", 2),
    "invalid_config": ("invalid_config", 2),
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
    """Classify one journal error by its pinned code alone.

    This is the whole classification for a plain ``JournalError``. The
    integration subclasses go through ``_classify_error``, which applies
    the same code-first rule and then the legacy wording rules.
    """

    explicit = _JOURNAL_ERROR_CODE_EXITS.get(getattr(error, "code", None))
    if explicit is not None:
        return explicit
    # Every raise site in the tree sets a known `code=`, which
    # `JournalErrorRaiseSiteCoverageTests` enforces, so reaching here means
    # a site was added without one or with a code missing from the table
    # above. That is an AIQ defect, not a classifiable journal outcome, and
    # it is reported as one rather than guessed at from the wording.
    return "internal_error", 70


def _classify_error(error: Exception) -> tuple[str, int]:
    # Precedence: an explicit code decides, before any class-based or
    # wording-based rule. This must stay first. `HookIntegrationError` and
    # `GuidanceIntegrationError` are `JournalError` subclasses matched by
    # earlier `isinstance` arms below, so testing those arms first would
    # make `code=` inert on exactly the raise sites that set it most.
    if isinstance(error, JournalError):
        explicit = _JOURNAL_ERROR_CODE_EXITS.get(getattr(error, "code", None))
        if explicit is not None:
            return explicit
    if isinstance(error, ConfigError):
        return "invalid_config", 2
    if isinstance(error, EventError):
        return "invalid_document", 2
    # Legacy substring rules, retained only for an error that reaches the CLI
    # without a code. `JournalErrorRaiseSiteCoverageTests` keeps every raise
    # site in the tree pinned, so nothing in AIQ is classified by wording;
    # these decide only for an uncoded error built outside the tree.
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
