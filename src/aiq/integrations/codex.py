"""Reversible Codex ``UserPromptSubmit`` and ``Stop`` integration.

The installed command captures ``UserPromptSubmit`` events and is also
registered under the ``Stop`` event as a completion gate: while runnable
AIQ work remains and the payload's ``stop_hook_active`` loop guard is
falsy, it exits 2 with one stderr line, which Codex feeds back to the
model and continues the turn. The gate fails open — any gate error exits
0 with a diagnostic — so an AIQ defect never blocks stopping.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import tomllib
from typing import Any, BinaryIO, Mapping, TextIO

from aiq.integrations import _hooks
from aiq.integrations._hooks import HookIntegrationError


CONTRACT_VERSION = 1
INTEGRATION_ID = "aiq-workqueue.codex.user-prompt.v1"
PURPOSE = (
    "Capture Codex UserPromptSubmit events and gate Stop events on "
    "runnable work."
)
HOOK_INPUT_MAX_BYTES = _hooks.HOOK_INPUT_MAX_BYTES
HOOK_DESCRIPTION = "AIQ local work-journal integration."


class CodexIntegrationError(HookIntegrationError):
    """The Codex integration cannot be inspected or changed safely."""


def _codex_home(environment: Mapping[str, str]) -> Path:
    configured = environment.get("CODEX_HOME")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise CodexIntegrationError("CODEX_HOME must be an absolute path")
        return path
    return (
        _hooks.home_directory(environment, error_class=CodexIntegrationError)
        / ".codex"
    )


def _target_path(environment: Mapping[str, str]) -> Path:
    return _codex_home(environment) / "hooks.json"


def _hook_groups(
    python_executable: Path,
    git_executable: Path,
) -> dict[str, dict[str, Any]]:
    """Ordered owned groups: the same command under both managed events."""

    command = (
        f"{shlex.quote(os.fspath(python_executable))} -I -m aiq "
        "integration receive codex "
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


def _inline_configuration_status(codex_home: Path) -> dict[str, bool]:
    path = codex_home / "config.toml"
    if not path.exists() and not path.is_symlink():
        return {"hooks": False, "disabled": False}
    data = _hooks.read_bounded(
        path,
        1_048_576,
        label="Codex config",
        error_class=CodexIntegrationError,
    )
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


def _preflight(
    environment: Mapping[str, str],
    target: Path,
    document: dict[str, Any],
) -> dict[str, str] | None:
    inline = _inline_configuration_status(target.parent)
    if inline["disabled"]:
        return {
            "status": "disabled",
            "blocked_reason": "Codex lifecycle hooks are disabled in config.toml",
        }
    if inline["hooks"]:
        return {
            "status": "conflict",
            "blocked_reason": (
                "config.toml already contains inline hooks; use integration "
                "print and manage one representation externally"
            ),
        }
    return None


SPEC = _hooks.HookIntegrationSpec(
    integration="codex",
    integration_id=INTEGRATION_ID,
    error_class=CodexIntegrationError,
    display_name="Codex",
    target_label="Codex hooks",
    state_subdirectory="codex",
    target_path=_target_path,
    hook_groups=_hook_groups,
    events=(_hooks.PROMPT_EVENT, _hooks.STOP_EVENT),
    created_file_preamble={"description": HOOK_DESCRIPTION},
    preflight=_preflight,
    receive=_hooks.ReceivePayloadSpec(
        source="codex",
        input_label="Codex hook input",
        event="UserPromptSubmit",
        required_fields=("session_id", "turn_id", "cwd"),
        turn_field="turn_id",
        turn_required=True,
    ),
)


def integration_present(
    *,
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Report whether the Codex target or AIQ-owned state exists."""

    return _hooks.integration_present(SPEC, environment=environment)


def print_integration(
    *,
    launcher: str | Path | None = None,
    git_executable: str | Path | None = None,
    python_executable: str | Path | None = None,
    invoked_launcher: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Render an externally managed ``hooks.json`` fragment."""

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
    """Install or explicitly repair the user-level Codex hook."""

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


def installed_manifest(
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Return the validated installed user-level manifest, if one exists."""

    return _hooks.installed_manifest(SPEC, environment=environment)


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
    """Validate and durably ingest one Codex ``UserPromptSubmit`` payload."""

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
    """Evaluate one Codex ``Stop`` completion-gate payload.

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
    """Run the stdout-silent Codex hook boundary.

    A ``UserPromptSubmit`` payload is captured; a capture failure exits
    1, never 2, because Codex treats a blocking exit code from that
    event as denying the prompt, and a capture failure must never block
    the user's prompt. A ``Stop`` payload runs the completion gate
    instead: exit 2 with one stderr line blocks stopping while runnable
    work remains and ``stop_hook_active`` is falsy, exit 0 is silent
    otherwise, and any gate failure exits 0 with a single diagnostic so
    an AIQ defect never blocks stopping.
    """

    return _hooks.run_receive_hook_main(
        lambda payload: receive_hook(
            payload,
            integration_id=integration_id,
            git_executable=git_executable,
            agent_root=agent_root,
        ),
        error_class=CodexIntegrationError,
        input_label="Codex hook input",
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
