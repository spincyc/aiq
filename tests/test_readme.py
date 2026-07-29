from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
README = REPOSITORY_ROOT / "README.md"
ROOT_AGENTS = REPOSITORY_ROOT / "AGENTS.md"
PACKAGED_AGENTS = REPOSITORY_ROOT / "src" / "aiq" / "_resources" / "AGENTS.md"

MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)(?:\s+[^)]*)?\)")
DOC_TEST_MARKER = re.compile(
    r"^\s*<!--\s*aiq-doc-test:\s*([a-z0-9][a-z0-9_-]*)\s*-->\s*$"
)
SHELL_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(sh|bash|shell)\s*$")


def marked_shell_blocks(readme: str) -> list[tuple[str, str, str]]:
    lines = readme.splitlines()
    blocks: list[tuple[str, str, str]] = []

    for marker_index, line in enumerate(lines):
        marker = DOC_TEST_MARKER.match(line)
        if marker is None:
            continue

        fence_index = marker_index + 1
        while fence_index < len(lines) and not lines[fence_index].strip():
            fence_index += 1
        if fence_index >= len(lines):
            raise AssertionError(f"{marker.group(1)!r} has no fenced block")

        opening = SHELL_FENCE.match(lines[fence_index])
        if opening is None:
            raise AssertionError(
                f"{marker.group(1)!r} must precede a sh, bash, or shell block"
            )

        fence, dialect = opening.groups()
        closing = re.compile(
            rf"^\s*{re.escape(fence[0])}{{{len(fence)},}}\s*$"
        )
        body: list[str] = []
        for block_line in lines[fence_index + 1 :]:
            if closing.match(block_line):
                blocks.append((marker.group(1), dialect, "\n".join(body) + "\n"))
                break
            body.append(block_line)
        else:
            raise AssertionError(f"{marker.group(1)!r} has no closing fence")

    return blocks


class ReadmeTests(unittest.TestCase):
    def test_local_links_resolve_inside_repository(self) -> None:
        readme = README.read_text(encoding="utf-8")
        local_links: list[str] = []

        for raw_target in MARKDOWN_LINK.findall(readme):
            target = raw_target.strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue

            local_links.append(target)
            candidate = (README.parent / unquote(parsed.path)).resolve()
            try:
                candidate.relative_to(REPOSITORY_ROOT)
            except ValueError:
                self.fail(f"README link escapes the repository: {target}")
            self.assertTrue(candidate.is_file(), f"README link is missing: {target}")

        self.assertTrue(local_links, "README should contain local documentation links")

    def test_packaged_agent_guidance_matches_terse_root_copy(self) -> None:
        root_guidance = ROOT_AGENTS.read_bytes()
        self.assertEqual(root_guidance, PACKAGED_AGENTS.read_bytes())

        word_count = len(root_guidance.decode("utf-8").split())
        self.assertLessEqual(word_count, 200, f"AGENTS.md has {word_count} words")

    def test_marked_shell_blocks_have_valid_syntax(self) -> None:
        blocks = marked_shell_blocks(README.read_text(encoding="utf-8"))
        names = [name for name, _dialect, _body in blocks]
        self.assertEqual(len(names), len(set(names)), "doc-test names must be unique")

        for name, dialect, body in blocks:
            shell = "/bin/bash" if dialect == "bash" else "/bin/sh"
            result = subprocess.run(
                [shell, "-n"],
                input=body,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"README block {name!r} has invalid shell syntax:\n{result.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
