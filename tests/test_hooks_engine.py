from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from aiq.integrations import _hooks as hooks_engine
from aiq.integrations import codex as codex_module
from aiq.integrations.codex import (
    CodexIntegrationError,
    INTEGRATION_ID,
    check_integration,
    install_integration,
    plan_integration,
    uninstall_integration,
)


def _canonical_digest(group: dict) -> str:
    return hashlib.sha256(
        json.dumps(group, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _group_with_command(command: object) -> dict:
    return {"hooks": [{"type": "command", "command": command}]}


class HooksEngineTest(unittest.TestCase):
    def git_executable(self) -> Path:
        discovered = shutil.which("git")
        self.assertIsNotNone(discovered)
        return Path(discovered).absolute()

    def fixture(self, root: Path) -> tuple[dict[str, str], Path]:
        home = root / "home"
        state = root / "state"
        codex_home = root / "codex home"
        launcher = root / "bin" / "aiq tool"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
        environment = {
            "HOME": str(home),
            "XDG_STATE_HOME": str(state),
            "CODEX_HOME": str(codex_home),
            "PATH": str(self.git_executable().parent),
        }
        return environment, launcher

    def test_manifest_from_older_hook_template_loads_and_uninstalls(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            installed = install_integration(
                launcher=launcher,
                environment=environment,
            )
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            manifest_path = Path(installed["state_directory"]) / "manifest.json"

            # Simulate a manifest written by an older AIQ whose hook template
            # used a different timeout: mutate the owned group in both the
            # target and the manifest, keeping the canonical digest correct.
            document = json.loads(target.read_text())
            document["hooks"]["UserPromptSubmit"][0]["hooks"][0]["timeout"] = 11
            target.write_text(json.dumps(document))
            manifest = json.loads(manifest_path.read_text())
            manifest["managed_group"]["UserPromptSubmit"]["hooks"][0][
                "timeout"
            ] = 11
            manifest["managed_group_sha256"] = _canonical_digest(
                manifest["managed_group"]
            )
            manifest_path.write_text(json.dumps(manifest))

            plan = plan_integration(launcher=launcher, environment=environment)
            checked = check_integration(
                launcher=launcher,
                environment=environment,
            )
            result = uninstall_integration(environment=environment)

            # The manifest still loads: template drift is reported as drift,
            # never as an unreadable manifest, and uninstall still works.
            self.assertEqual(plan["status"], "drifted")
            self.assertEqual(checked["status"], "drifted")
            self.assertEqual(result["status"], "uninstalled")
            self.assertEqual(result["action"], "uninstall")
            self.assertFalse(target.exists())

    def test_manifest_rejects_group_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            installed = install_integration(
                launcher=launcher,
                environment=environment,
            )
            manifest_path = Path(installed["state_directory"]) / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["managed_group"]["UserPromptSubmit"]["hooks"][0][
                "timeout"
            ] = 11
            manifest_path.write_text(json.dumps(manifest))

            with self.assertRaisesRegex(CodexIntegrationError, "digest"):
                uninstall_integration(environment=environment)

    def test_marker_requires_exact_integration_id_token_pair(self) -> None:
        counted = hooks_engine.marker_count(
            _group_with_command(f"x --integration-id {INTEGRATION_ID} -y z"),
            integration_id=INTEGRATION_ID,
        )
        template_groups = codex_module._hook_groups(
            Path("/usr/bin/python3"),
            self.git_executable(),
        )
        template_counts = {
            event: hooks_engine.marker_count(
                group,
                integration_id=INTEGRATION_ID,
            )
            for event, group in template_groups.items()
        }
        longer_id = hooks_engine.marker_count(
            _group_with_command(f"x --integration-id {INTEGRATION_ID}x -y z"),
            integration_id=INTEGRATION_ID,
        )
        embedded = hooks_engine.marker_count(
            _group_with_command(
                f"echo 'note --integration-id {INTEGRATION_ID} inside'"
            ),
            integration_id=INTEGRATION_ID,
        )
        glued = hooks_engine.marker_count(
            _group_with_command(f"x see--integration-id {INTEGRATION_ID}"),
            integration_id=INTEGRATION_ID,
        )
        unparseable = hooks_engine.marker_count(
            _group_with_command(f"'oops --integration-id {INTEGRATION_ID}"),
            integration_id=INTEGRATION_ID,
        )
        non_string = hooks_engine.marker_count(
            _group_with_command(["--integration-id", INTEGRATION_ID]),
            integration_id=INTEGRATION_ID,
        )

        self.assertEqual(counted, 1)
        self.assertEqual(
            template_counts,
            {"UserPromptSubmit": 1, "Stop": 1},
        )
        self.assertEqual(longer_id, 0)
        self.assertEqual(embedded, 0)
        self.assertEqual(glued, 0)
        self.assertEqual(unparseable, 0)
        self.assertEqual(non_string, 0)

    def test_missing_git_executable_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            environment["PATH"] = ""

            plan = plan_integration(launcher=launcher, environment=environment)
            checked = check_integration(
                launcher=launcher,
                environment=environment,
            )

            self.assertEqual(plan["status"], "unsafe")
            self.assertEqual(plan["action"], "block")
            self.assertIn("Git executable", plan["blocked_reason"])
            self.assertIsNone(plan["desired_group"])
            self.assertIsNone(plan["plan_token"])
            self.assertEqual(checked["status"], "unsafe")
            self.assertFalse(checked["ok"])
            with self.assertRaisesRegex(
                CodexIntegrationError,
                "Git executable",
            ):
                install_integration(launcher=launcher, environment=environment)

    def test_repair_replaces_manifest_recorded_group_without_duplicates(
        self,
    ) -> None:
        recorded = {"hooks": [{"type": "command", "command": "recorded"}]}
        desired = {"hooks": [{"type": "command", "command": "desired"}]}
        unrelated = {"hooks": [{"type": "command", "command": "unrelated"}]}
        document = {"hooks": {"UserPromptSubmit": [unrelated, recorded]}}

        replaced_document, replaced_created, replaced = (
            hooks_engine._repair_missing_group(
                document,
                "UserPromptSubmit",
                recorded,
                desired,
            )
        )
        appended_document, appended_created, appended = (
            hooks_engine._repair_missing_group(
                {"hooks": {"UserPromptSubmit": [unrelated]}},
                "UserPromptSubmit",
                recorded,
                desired,
            )
        )

        self.assertTrue(replaced)
        self.assertEqual(replaced_created, [])
        self.assertEqual(
            replaced_document["hooks"]["UserPromptSubmit"],
            [unrelated, desired],
        )
        self.assertFalse(appended)
        self.assertEqual(appended_created, [])
        self.assertEqual(
            appended_document["hooks"]["UserPromptSubmit"],
            [unrelated, desired],
        )

    def test_missing_managed_group_repair_appends_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            install_integration(launcher=launcher, environment=environment)
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            document = json.loads(target.read_text())
            document["hooks"]["UserPromptSubmit"] = []
            target.write_text(json.dumps(document))

            plan = plan_integration(launcher=launcher, environment=environment)
            self.assertEqual(plan["status"], "drifted")
            self.assertIn("missing", plan["blocked_reason"])

            repaired = install_integration(
                launcher=launcher,
                environment=environment,
                repair=True,
            )
            groups = json.loads(target.read_text())["hooks"]["UserPromptSubmit"]

            self.assertEqual(repaired["status"], "installed")
            self.assertEqual(len(groups), 1)

    def test_multi_event_install_and_uninstall_are_symmetric(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            target.parent.mkdir(parents=True)
            original = {
                "description": "mine",
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": "prompt"}]}
                    ],
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "stop"}]}
                    ],
                },
            }
            target.write_text(json.dumps(original))

            installed = install_integration(
                launcher=launcher,
                environment=environment,
            )
            document = json.loads(target.read_text())
            manifest = json.loads(
                (
                    Path(installed["state_directory"]) / "manifest.json"
                ).read_text()
            )

            # One owned group is appended under each managed event and the
            # manifest records both under one integration id.
            self.assertEqual(installed["status"], "installed")
            for event in ("UserPromptSubmit", "Stop"):
                groups = document["hooks"][event]
                self.assertEqual(len(groups), 2)
                self.assertEqual(
                    groups[0],
                    original["hooks"][event][0],
                )
                self.assertIn(
                    f"--integration-id {INTEGRATION_ID}",
                    groups[1]["hooks"][0]["command"],
                )
            self.assertEqual(
                set(manifest["managed_group"]),
                {"UserPromptSubmit", "Stop"},
            )
            self.assertEqual(manifest["integration_id"], INTEGRATION_ID)

            removed = uninstall_integration(environment=environment)

            self.assertEqual(removed["status"], "uninstalled")
            self.assertEqual(
                {change["path"] for change in removed["changes"]},
                {
                    "/hooks/UserPromptSubmit/aiq-owned-group",
                    "/hooks/Stop/aiq-owned-group",
                },
            )
            self.assertEqual(json.loads(target.read_text()), original)

    def test_single_event_manifest_upgrades_to_stop_group_via_repair(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            installed = install_integration(
                launcher=launcher,
                environment=environment,
            )
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            manifest_path = Path(installed["state_directory"]) / "manifest.json"

            # Rewrite the install as an older single-event one: the target
            # has only the UserPromptSubmit group and the manifest stores
            # one bare owned group (the pre-multi-event manifest form).
            document = json.loads(target.read_text())
            del document["hooks"]["Stop"]
            target.write_text(json.dumps(document))
            manifest = json.loads(manifest_path.read_text())
            legacy_group = manifest["managed_group"]["UserPromptSubmit"]
            manifest["managed_group"] = legacy_group
            manifest["managed_group_sha256"] = _canonical_digest(legacy_group)
            manifest["created_containers"] = ["hooks", "UserPromptSubmit"]
            manifest_path.write_text(json.dumps(manifest))

            # The old manifest still loads; the absent Stop group is
            # ordinary drift repaired by the existing repair flow.
            plan = plan_integration(launcher=launcher, environment=environment)
            self.assertEqual(plan["status"], "drifted")
            self.assertIn("missing", plan["blocked_reason"])

            repaired = install_integration(
                launcher=launcher,
                environment=environment,
                repair=True,
            )
            document = json.loads(target.read_text())
            upgraded = json.loads(manifest_path.read_text())
            checked = check_integration(
                launcher=launcher,
                environment=environment,
            )

            self.assertEqual(repaired["status"], "installed")
            self.assertEqual(len(document["hooks"]["UserPromptSubmit"]), 1)
            self.assertEqual(len(document["hooks"]["Stop"]), 1)
            self.assertEqual(
                set(upgraded["managed_group"]),
                {"UserPromptSubmit", "Stop"},
            )
            self.assertEqual(
                upgraded["created_containers"],
                ["hooks", "UserPromptSubmit", "Stop"],
            )
            self.assertTrue(checked["ok"])

            result = uninstall_integration(environment=environment)
            self.assertEqual(result["status"], "uninstalled")
            self.assertFalse(target.exists())

    def test_interrupted_install_is_recoverable_with_explicit_repair(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment, launcher = self.fixture(root)
            installed = install_integration(
                launcher=launcher,
                environment=environment,
            )
            target = Path(environment["CODEX_HOME"]) / "hooks.json"
            manifest_path = Path(installed["state_directory"]) / "manifest.json"

            # Simulate a crash between writing the target and the manifest.
            manifest_path.unlink()

            plan = plan_integration(launcher=launcher, environment=environment)
            self.assertEqual(plan["status"], "unmanaged")
            self.assertEqual(plan["action"], "block")
            self.assertIn("repair", plan["blocked_reason"])
            with self.assertRaisesRegex(
                CodexIntegrationError,
                "without an active AIQ manifest",
            ):
                install_integration(launcher=launcher, environment=environment)

            repaired = install_integration(
                launcher=launcher,
                environment=environment,
                repair=True,
            )
            manifest = json.loads(manifest_path.read_text())
            checked = check_integration(
                launcher=launcher,
                environment=environment,
            )

            self.assertEqual(repaired["status"], "installed")
            # Adoption cannot prove AIQ created the file or containers.
            self.assertFalse(manifest["created_file"])
            self.assertEqual(manifest["created_containers"], [])
            self.assertTrue(checked["ok"])
            self.assertEqual(checked["status"], "installed")
            self.assertEqual(
                len(
                    json.loads(target.read_text())["hooks"]["UserPromptSubmit"]
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
