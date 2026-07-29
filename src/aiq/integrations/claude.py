"""Reversible Claude Code ``UserPromptSubmit`` and ``Stop`` integration.

Claude Code stores user-level hooks inside ``settings.json`` next to
unrelated settings, delivers ``prompt_id`` instead of ``turn_id``, and
treats hook exit code 2 from ``UserPromptSubmit`` as a blocking error
that erases the user's prompt. The adapter therefore preserves unrelated
settings keys, derives idempotency from ``prompt_id`` when it is
delivered (without one each event is captured, with no deduplication),
and fails capture with exit code 1 so a capture failure never blocks the
prompt.

The same installed command is also registered under the ``Stop`` event
as a completion gate: while runnable AIQ work remains and the payload's
``stop_hook_active`` loop guard is falsy, it exits 2 with one stderr
line, which Claude Code feeds back to the model and continues the turn.
The gate fails open — any gate error exits 0 with a diagnostic — so an
AIQ defect never blocks stopping.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
from typing import Any, BinaryIO, Mapping, TextIO

from aiq.integrations import _hooks
from aiq.integrations._hooks import HookIntegrationError


CONTRACT_VERSION = 1
INTEGRATION_ID = "aiq-workqueue.claude.user-prompt.v1"
PURPOSE = (
    "Capture Claude Code UserPromptSubmit events and gate Stop events on "
    "runnable work."
)
HOOK_INPUT_MAX_BYTES = _hooks.HOOK_INPUT_MAX_BYTES


class ClaudeIntegrationError(HookIntegrationError):
    """The Claude Code integration cannot be inspected or changed safely."""


def _claude_config_directory(environment: Mapping[str, str]) -> Path:
    configured = environment.get("CLAUDE_CONFIG_DIR")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise ClaudeIntegrationError(
                "CLAUDE_CONFIG_DIR must be an absolute path"
            )
        return path
    return (
        _hooks.home_directory(environment, error_class=ClaudeIntegrationError)
        / ".claude"
    )


def _target_path(environment: Mapping[str, str]) -> Path:
    return _claude_config_directory(environment) / "settings.json"


def _hook_groups(
    python_executable: Path,
    git_executable: Path,
) -> dict[str, dict[str, Any]]:
    """Ordered owned groups: the same command under both managed events."""

    command = (
        f"{shlex.quote(os.fspath(python_executable))} -I -m aiq "
        "integration receive claude "
        f"--integration-id {INTEGRATION_ID} "
        f"--git-executable {shlex.quote(os.fspath(git_executable))}"
    )
    return {
        _hooks.PROMPT_EVENT: {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": 10,
                    "statusMessage": "AIQ: capturing message",
                }
            ]
        },
        _hooks.STOP_EVENT: {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": 10,
                    "statusMessage": "AIQ: checking for runnable work",
                }
            ]
        },
    }


def _preflight(
    environment: Mapping[str, str],
    target: Path,
    document: dict[str, Any],
) -> dict[str, str] | None:
    if document.get("disableAllHooks") is True:
        return {
            "status": "disabled",
            "blocked_reason": (
                "Claude Code hooks are disabled by disableAllHooks in "
                "settings.json"
            ),
        }
    return None


SPEC = _hooks.HookIntegrationSpec(
    integration="claude",
    integration_id=INTEGRATION_ID,
    error_class=ClaudeIntegrationError,
    display_name="Claude Code",
    target_label="Claude Code settings",
    state_subdirectory="claude",
    target_path=_target_path,
    hook_groups=_hook_groups,
    events=(_hooks.PROMPT_EVENT, _hooks.STOP_EVENT),
    preflight=_preflight,
    receive=_hooks.ReceivePayloadSpec(
        source="claude",
        input_label="Claude Code hook input",
        event="UserPromptSubmit",
        required_fields=("session_id", "cwd"),
        turn_field="prompt_id",
        turn_required=False,
    ),
)


def integration_present(
    *,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Report whether the Claude Code target or AIQ-owned state exists."""

    return _hooks.integration_present(SPEC, environment=environment)


def installed_manifest(
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Return the validated installed user-level manifest, if one exists."""

    return _hooks.installed_manifest(SPEC, environment=environment)


def print_integration(
    *,
    launcher: str | Path | None = None,
    git_executable: str | Path | None = None,
    python_executable: str | Path | None = None,
    invoked_launcher: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Render an externally managed ``settings.json`` hooks fragment."""

    return _hooks.render_fragment(
        SPEC,
        launcher=launcher,
        git_executable=git_executable,
        python_executable=python_executable,
        invoked_launcher=invoked_launcher,
        environment=environment,
    )


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

    return _hooks.plan_integration(
        SPEC,
        launcher=launcher,
        git_executable=git_executable,
        python_executable=python_executable,
        invoked_launcher=invoked_launcher,
        environment=environment,
        repair=repair,
    )


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
    """Install or explicitly repair the user-level Claude Code hook."""

    return _hooks.install_integration(
        SPEC,
        launcher=launcher,
        git_executable=git_executable,
        python_executable=python_executable,
        invoked_launcher=invoked_launcher,
        environment=environment,
        repair=repair,
        plan_token=plan_token,
    )


def check_integration(
    *,
    launcher: str | Path | None = None,
    git_executable: str | Path | None = None,
    python_executable: str | Path | None = None,
    invoked_launcher: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Inspect integration ownership, drift, and executable availability."""

    return _hooks.check_integration(
        SPEC,
        launcher=launcher,
        git_executable=git_executable,
        python_executable=python_executable,
        invoked_launcher=invoked_launcher,
        environment=environment,
    )


def uninstall_integration(
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Remove only the hook recorded as AIQ-owned."""

    return _hooks.uninstall_integration(SPEC, environment=environment)


def receive_hook(
    payload: str | bytes,
    *,
    integration_id: str = INTEGRATION_ID,
    git_executable: str | Path | None = None,
    agent_root: Path | None = None,
) -> dict[str, Any]:
    """Validate and durably ingest one Claude Code ``UserPromptSubmit`` payload."""

    return _hooks.receive_hook(
        SPEC,
        payload,
        integration_id=integration_id,
        git_executable=git_executable,
        agent_root=agent_root,
    )


def gate_hook(
    payload: str | bytes,
    *,
    integration_id: str = INTEGRATION_ID,
    git_executable: str | Path | None = None,
    agent_root: Path | None = None,
) -> str | None:
    """Evaluate one Claude Code ``Stop`` completion-gate payload.

    Returns the single-line block reason, or ``None`` when stopping is
    allowed (loop guard set, or nothing runnable in scope).
    """

    return _hooks.gate_stop_hook(
        SPEC,
        payload,
        integration_id=integration_id,
        git_executable=git_executable,
        agent_root=agent_root,
    )


def receive_hook_main(
    *,
    input_stream: BinaryIO | None = None,
    error_stream: TextIO | None = None,
    integration_id: str = INTEGRATION_ID,
    git_executable: str | Path | None = None,
    agent_root: Path | None = None,
) -> int:
    """Run the stdout-silent Claude Code hook boundary.

    A ``UserPromptSubmit`` payload is captured; a capture failure exits
    1, never 2, because Claude Code treats exit 2 from that event as a
    blocking error that erases the prompt. A ``Stop`` payload runs the
    completion gate instead: exit 2 with one stderr line blocks stopping
    while runnable work remains and ``stop_hook_active`` is falsy, exit
    0 is silent otherwise, and any gate failure exits 0 with a single
    diagnostic so an AIQ defect never blocks stopping.
    """

    return _hooks.run_receive_hook_main(
        lambda payload: receive_hook(
            payload,
            integration_id=integration_id,
            git_executable=git_executable,
            agent_root=agent_root,
        ),
        error_class=ClaudeIntegrationError,
        input_label="Claude Code hook input",
        failure_exit_code=1,
        gate=lambda payload: gate_hook(
            payload,
            integration_id=integration_id,
            git_executable=git_executable,
            agent_root=agent_root,
        ),
        input_stream=input_stream,
        error_stream=error_stream,
    )
