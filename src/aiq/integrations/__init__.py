"""Optional integrations for external tools.

This package also hosts the single integration registry consumed by the
CLI and doctor: hook adapters plus the guidance integration, each tagged
with a kind marker so callers can dispatch without per-id branching.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiq.integrations import claude, codex, guidance

KIND_HOOK = "hook"
KIND_GUIDANCE = "guidance"


@dataclass(frozen=True)
class IntegrationRecord:
    """One dispatchable integration adapter with its kind marker."""

    integration_id: str
    kind: str
    module: Any
    purpose: str
    contract_version: int


INTEGRATIONS: dict[str, IntegrationRecord] = {
    record.integration_id: record
    for record in (
        IntegrationRecord(
            "claude",
            KIND_HOOK,
            claude,
            claude.PURPOSE,
            claude.CONTRACT_VERSION,
        ),
        IntegrationRecord(
            "codex",
            KIND_HOOK,
            codex,
            codex.PURPOSE,
            codex.CONTRACT_VERSION,
        ),
        IntegrationRecord(
            "guidance",
            KIND_GUIDANCE,
            guidance,
            "Manage one AIQ-owned block in a chosen guidance file.",
            guidance.CONTRACT_VERSION,
        ),
    )
}

HOOK_INTEGRATIONS: dict[str, IntegrationRecord] = {
    integration_id: record
    for integration_id, record in INTEGRATIONS.items()
    if record.kind == KIND_HOOK
}
