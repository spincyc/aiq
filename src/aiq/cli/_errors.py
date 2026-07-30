from __future__ import annotations

import json
import sqlite3

from aiq.config import ConfigError
from aiq.events import EventError
from aiq.journal import JournalError


# Every stable code in docs/contracts/errors.md, mapped to its (code, exit)
# classification. Codes are set at the raise site that detects the failure —
# on `JournalError` and on every subclass of it, the integration errors
# included — so a diagnostic can be reworded without changing the code. The
# table must stay complete: a code absent from it is not pinnable, because a
# journal error carrying an unregistered code is reported as an AIQ defect.
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

    This is the whole classification for every ``JournalError``, the
    integration subclasses included. Nothing else is consulted: not the
    class, and not the wording.
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
    # Precedence: an explicit code decides, before any class-based rule.
    # This must stay first. `HookIntegrationError` and
    # `GuidanceIntegrationError` are `JournalError` subclasses, so a
    # class-based arm for either one would make `code=` inert on exactly
    # the raise sites that set it most. There is deliberately no rule that
    # reads a message: wording classifies nothing.
    if isinstance(error, JournalError):
        return _classify_journal_error(error)
    if isinstance(error, ConfigError):
        return "invalid_config", 2
    if isinstance(error, EventError):
        return "invalid_document", 2
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError, ValueError)):
        return "invalid_document", 2
    if isinstance(error, OSError):
        return "io_error", 6
    if isinstance(error, sqlite3.DatabaseError):
        return "integrity_failed", 5
    return "internal_error", 70
