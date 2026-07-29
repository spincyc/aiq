from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from aiq.config import (
    CONFIG_MAX_BYTES,
    ConfigError,
    load_config_file,
    repository_config_path,
    resolve_config,
    user_config_path,
)


class ConfigTests(unittest.TestCase):
    def test_defaults_are_complete_and_immutable(self) -> None:
        config = resolve_config(
            environ={},
            user_path=None,
            repo_path=None,
            default_owner="tester",
        )

        self.assertEqual(
            config.to_dict(),
            {
                "version": 1,
                "scope": "auto",
                "owner": "tester",
                "lease_seconds": 900,
                "snapshot_keep": 5,
                "output": "human",
            },
        )
        self.assertEqual(set(config.sources.values()), {"default"})
        with self.assertRaises(TypeError):
            config.sources["scope"] = "test"  # type: ignore[index]

    def test_precedence_is_cli_env_repo_user_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            user = root / "user.toml"
            repo = root / "repo.toml"
            user.write_text(
                "\n".join(
                    (
                        "version = 1",
                        'scope = "user"',
                        'owner = "from-user"',
                        "lease_seconds = 10",
                        "snapshot_keep = 10",
                        'output = "json"',
                    )
                )
            )
            repo.write_text(
                "\n".join(
                    (
                        "version = 1",
                        "lease_seconds = 20",
                    )
                )
            )

            config = resolve_config(
                cli={"lease_seconds": 40, "scope": None},
                environ={
                    "AIQ_OWNER": "from-env",
                    "AIQ_LEASE_SECONDS": "30",
                },
                user_path=user,
                repo_path=repo,
                default_owner="default-owner",
            )

        self.assertEqual(config.scope, "user")
        self.assertEqual(config.owner, "from-env")
        self.assertEqual(config.lease_seconds, 40)
        self.assertEqual(config.snapshot_keep, 10)
        self.assertEqual(config.output, "json")
        self.assertEqual(config.sources["scope"], f"user:{user}")
        self.assertEqual(config.sources["owner"], "env:AIQ_OWNER")
        self.assertEqual(config.sources["lease_seconds"], "cli")
        self.assertEqual(config.sources["output"], f"user:{user}")

    def test_repo_source_rejects_personal_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / ".aiq.toml"
            for key, value in (
                ("scope", '"repo"'),
                ("owner", '"repo-owner"'),
                ("snapshot_keep", "10"),
                ("output", '"json"'),
            ):
                path.write_text(f"version = 1\n{key} = {value}\n")
                with self.subTest(key=key), self.assertRaisesRegex(
                    ConfigError,
                    f"does not allow configuration key: {key}",
                ):
                    load_config_file(path, source="repo")

    def test_files_are_flat_versioned_and_exact(self) -> None:
        invalid_documents = {
            "missing version": 'scope = "auto"\n',
            "future version": 'version = 2\nscope = "auto"\n',
            "boolean version": 'version = true\nscope = "auto"\n',
            "unknown key": "version = 1\nsurprise = 1\n",
            "nested table": "version = 1\n[claims]\nlease_seconds = 10\n",
            "boolean integer": "version = 1\nlease_seconds = true\n",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "config.toml"
            for name, document in invalid_documents.items():
                path.write_text(document)
                with self.subTest(name=name), self.assertRaises(ConfigError):
                    load_config_file(path, source="user")

    def test_values_are_strictly_validated(self) -> None:
        invalid = (
            {"scope": "agent-root"},
            {"owner": ""},
            {"owner": "bad\nowner"},
            {"lease_seconds": 0},
            {"lease_seconds": 86401},
            {"snapshot_keep": 0},
            {"snapshot_keep": 10001},
            {"output": "yaml"},
            {"unknown": "value"},
        )
        for cli in invalid:
            with self.subTest(cli=cli), self.assertRaises(ConfigError):
                resolve_config(
                    cli=cli,
                    environ={},
                    user_path=None,
                    repo_path=None,
                    default_owner="tester",
                )

    def test_environment_integers_do_not_coerce_whitespace_or_signs(self) -> None:
        for value in ("", " 5", "5 ", "+5", "-5", "1.0", "true"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ConfigError,
                "unsigned decimal integer",
            ):
                resolve_config(
                    environ={"AIQ_LEASE_SECONDS": value},
                    user_path=None,
                    repo_path=None,
                    default_owner="tester",
                )

    def test_xdg_path_requires_an_absolute_directory(self) -> None:
        self.assertEqual(
            user_config_path(
                {
                    "HOME": "/home/tester",
                    "XDG_CONFIG_HOME": "/tmp/test-config",
                }
            ),
            Path("/tmp/test-config/aiq/config.toml"),
        )
        self.assertEqual(
            user_config_path({"HOME": "/home/tester"}),
            Path("/home/tester/.config/aiq/config.toml"),
        )
        with self.assertRaisesRegex(ConfigError, "must be an absolute path"):
            user_config_path({"XDG_CONFIG_HOME": "relative"})
        with self.assertRaisesRegex(ConfigError, "must be an absolute path"):
            user_config_path({"HOME": "relative"})

    def test_repository_path_is_only_the_git_worktree_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            nested = repository / "one" / "two"
            nested.mkdir(parents=True)
            subprocess.run(
                ["git", "init", "--quiet", str(repository)],
                check=True,
            )

            self.assertEqual(
                repository_config_path(nested),
                repository / ".aiq.toml",
            )
            self.assertIsNone(
                repository_config_path(Path(temporary_directory)),
            )

    def test_repository_discovery_ignores_ambient_git_redirection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            expected_repository = root / "expected"
            redirected_repository = root / "redirected"
            subprocess.run(
                ["git", "init", "--quiet", str(expected_repository)],
                check=True,
            )
            subprocess.run(
                ["git", "init", "--quiet", str(redirected_repository)],
                check=True,
            )
            previous = {
                key: os.environ.get(key)
                for key in ("GIT_DIR", "GIT_WORK_TREE")
            }
            try:
                os.environ["GIT_DIR"] = str(redirected_repository / ".git")
                os.environ["GIT_WORK_TREE"] = str(redirected_repository)
                self.assertEqual(
                    repository_config_path(expected_repository),
                    expected_repository / ".aiq.toml",
                )
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_repo_symlink_is_rejected_and_user_symlink_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target.toml"
            target.write_text('version = 1\noutput = "json"\n')
            link = root / "config.toml"
            link.symlink_to(target)

            self.assertEqual(
                load_config_file(link, source="user"),
                {"output": "json"},
            )
            with self.assertRaisesRegex(ConfigError, "must not be a symlink"):
                load_config_file(link, source="repo")

    def test_non_regular_oversized_and_non_utf8_files_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = root / "directory"
            directory.mkdir()
            with self.assertRaisesRegex(ConfigError, "not a regular file"):
                load_config_file(directory, source="user")

            oversized = root / "oversized.toml"
            oversized.write_bytes(b"x" * (CONFIG_MAX_BYTES + 1))
            with self.assertRaisesRegex(ConfigError, "exceeds"):
                load_config_file(oversized, source="user")

            invalid_utf8 = root / "invalid.toml"
            invalid_utf8.write_bytes(b"version = 1\n# \xff\n")
            with self.assertRaisesRegex(ConfigError, "not valid UTF-8"):
                load_config_file(invalid_utf8, source="user")

    def test_missing_files_are_empty_layers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "missing.toml"
            self.assertEqual(load_config_file(missing, source="user"), {})
            self.assertEqual(load_config_file(missing, source="repo"), {})


if __name__ == "__main__":
    unittest.main()
