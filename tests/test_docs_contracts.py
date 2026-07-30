from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import unittest
from urllib.parse import unquote, urlsplit

from aiq.cli import build_parser
from aiq.config import (
    CONFIG_KEYS,
    ENVIRONMENT_KEYS,
    REPO_CONFIG_KEYS,
    USER_CONFIG_KEYS,
    resolve_config,
)
from aiq.journal import JournalError
from aiq.queue import parse_effect_document


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPOSITORY_ROOT / "docs"
EFFECTS_SCHEMA = REPOSITORY_ROOT / "schemas" / "effects-v1.schema.json"
CONFIGURATION_DOC = DOCS_ROOT / "configuration.md"
CLI_CONTRACT_DOC = DOCS_ROOT / "contracts" / "cli-v1.md"
EFFECTS_CONTRACT_DOC = DOCS_ROOT / "contracts" / "effects-v1.md"

MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)(?:\s+[^)]*)?\)")
AIQ_COMMAND = re.compile(
    r"(?<![\w./-])aiq[ \t]+"
    r"([a-z][a-z0-9-]*)(?:[ \t]+([a-z][a-z0-9-]*))?"
)
COMMAND_TABLE_ROW = re.compile(
    r"^\|\s*`"
    r"((?:journal|inbox|task|queue|claim|capability|config|integration)"
    r"\s+[a-z][a-z0-9-]*)\b"
)
JSON_FENCE = re.compile(r"^```json\s*$")


def _markdown_files() -> list[Path]:
    return sorted(
        (
            *REPOSITORY_ROOT.glob("*.md"),
            *DOCS_ROOT.rglob("*.md"),
        )
    )


def _parser_command_paths() -> set[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()

    def visit(
        parser: argparse.ArgumentParser,
        prefix: tuple[str, ...] = (),
    ) -> None:
        subparser_actions = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        if not subparser_actions:
            paths.add(prefix)
            return
        for name, child in subparser_actions[0].choices.items():
            visit(child, (*prefix, name))

    visit(build_parser())
    return paths


def _documented_command_paths() -> set[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()
    for path in _markdown_files():
        text = path.read_text(encoding="utf-8")
        for first, second in AIQ_COMMAND.findall(text):
            paths.add((first, second) if second else (first,))
        if path == CLI_CONTRACT_DOC:
            for line in text.splitlines():
                match = COMMAND_TABLE_ROW.match(line)
                if match is not None:
                    paths.add(tuple(match.group(1).split()))
    return paths


def _json_code_blocks(path: Path) -> list[object]:
    blocks: list[object] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines):
        if JSON_FENCE.match(lines[index]) is None:
            index += 1
            continue
        closing = index + 1
        while closing < len(lines) and lines[closing].strip() != "```":
            closing += 1
        if closing == len(lines):
            raise AssertionError(f"unterminated JSON block in {path}")
        blocks.append(json.loads("\n".join(lines[index + 1 : closing])))
        index = closing + 1
    return blocks


def _json_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


def _resolve_reference(root: dict[str, object], reference: str) -> object:
    if not reference.startswith("#/"):
        raise AssertionError(f"unsupported schema reference: {reference}")
    value: object = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or part not in value:
            raise AssertionError(f"unresolved schema reference: {reference}")
        value = value[part]
    return value


def _schema_matches(
    instance: object,
    schema: object,
    root: dict[str, object],
) -> bool:
    try:
        _validate_schema(instance, schema, root)
    except ValueError:
        return False
    return True


def _validate_schema(
    instance: object,
    schema: object,
    root: dict[str, object],
) -> None:
    if schema is True:
        return
    if schema is False:
        raise ValueError("false schema")
    if not isinstance(schema, dict):
        raise AssertionError("schema nodes must be objects or booleans")

    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str):
            raise AssertionError("$ref must be a string")
        _validate_schema(instance, _resolve_reference(root, reference), root)

    if "const" in schema and not _json_equal(instance, schema["const"]):
        raise ValueError("const")
    if "enum" in schema and not any(
        _json_equal(instance, item) for item in schema["enum"]
    ):
        raise ValueError("enum")

    expected_type = schema.get("type")
    type_checks = {
        "array": lambda value: isinstance(value, list),
        "integer": lambda value: type(value) is int,
        "null": lambda value: value is None,
        "object": lambda value: isinstance(value, dict),
        "string": lambda value: isinstance(value, str),
    }
    if expected_type is not None:
        if expected_type not in type_checks:
            raise AssertionError(f"unsupported schema type: {expected_type}")
        if not type_checks[expected_type](instance):
            raise ValueError("type")

    for child in schema.get("allOf", []):
        _validate_schema(instance, child, root)
    if "oneOf" in schema:
        matches = sum(
            _schema_matches(instance, child, root)
            for child in schema["oneOf"]
        )
        if matches != 1:
            raise ValueError("oneOf")
    if "not" in schema and _schema_matches(instance, schema["not"], root):
        raise ValueError("not")
    if "if" in schema:
        branch = "then" if _schema_matches(instance, schema["if"], root) else "else"
        if branch in schema:
            _validate_schema(instance, schema[branch], root)

    if isinstance(instance, dict):
        required = schema.get("required", [])
        if any(key not in instance for key in required):
            raise ValueError("required")
        if len(instance) < schema.get("minProperties", 0):
            raise ValueError("minProperties")
        properties = schema.get("properties", {})
        for key, child_instance in instance.items():
            if key in properties:
                _validate_schema(child_instance, properties[key], root)
                continue
            additional = schema.get("additionalProperties", True)
            _validate_schema(child_instance, additional, root)
        if "propertyNames" in schema:
            for key in instance:
                _validate_schema(key, schema["propertyNames"], root)

    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            raise ValueError("minItems")
        if len(instance) > schema.get("maxItems", len(instance)):
            raise ValueError("maxItems")
        prefix_items = schema.get("prefixItems", [])
        for index, child_schema in enumerate(prefix_items):
            if index < len(instance):
                _validate_schema(instance[index], child_schema, root)
        items_schema = schema.get("items", True)
        for child_instance in instance[len(prefix_items) :]:
            _validate_schema(child_instance, items_schema, root)
        if schema.get("uniqueItems", False):
            encoded = [
                json.dumps(item, sort_keys=True, separators=(",", ":"))
                for item in instance
            ]
            if len(encoded) != len(set(encoded)):
                raise ValueError("uniqueItems")

    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            raise ValueError("minLength")
        if len(instance) > schema.get("maxLength", len(instance)):
            raise ValueError("maxLength")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, instance) is None:
            raise ValueError("pattern")

    if type(instance) is int:
        if instance < schema.get("minimum", instance):
            raise ValueError("minimum")
        if instance > schema.get("maximum", instance):
            raise ValueError("maximum")


def _parse_config_matrix() -> dict[str, dict[str, str]]:
    text = CONFIGURATION_DOC.read_text(encoding="utf-8")
    section = text.split("## Keys", 1)[1].split("## Environment", 1)[0]
    rows: dict[str, dict[str, str]] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 5 or not cells[0].startswith("`"):
            continue
        key = cells[0].strip("`")
        rows[key] = {
            "default": cells[2].strip("`"),
            "user": cells[3],
            "repo": cells[4],
        }
    return rows


def _parse_environment_matrix() -> dict[str, str]:
    text = CONFIGURATION_DOC.read_text(encoding="utf-8")
    section = text.split("## Environment", 1)[1].split("## Scope", 1)[0]
    rows: dict[str, str] = {}
    for variable, key in re.findall(
        r"^\|\s*`(AIQ_[A-Z_]+)`\s*\|\s*`([a-z_]+)`\s*\|$",
        section,
        flags=re.MULTILINE,
    ):
        rows[variable] = key
    return rows


class DocumentationContractTests(unittest.TestCase):
    def test_all_relative_documentation_links_resolve_in_repository(self) -> None:
        checked: list[tuple[Path, str]] = []
        for document in _markdown_files():
            for raw_target in MARKDOWN_LINK.findall(
                document.read_text(encoding="utf-8")
            ):
                target = raw_target.strip("<>")
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                candidate = (document.parent / unquote(parsed.path)).resolve()
                try:
                    candidate.relative_to(REPOSITORY_ROOT)
                except ValueError:
                    self.fail(
                        f"{document.relative_to(REPOSITORY_ROOT)} link escapes "
                        f"the repository: {target}"
                    )
                self.assertTrue(
                    candidate.is_file(),
                    f"{document.relative_to(REPOSITORY_ROOT)} link is missing: "
                    f"{target}",
                )
                checked.append((document, target))
        self.assertTrue(checked, "documentation should contain relative links")

    def test_effects_schema_and_runtime_accept_documented_fixture(self) -> None:
        schema = json.loads(EFFECTS_SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["$schema"],
            "https://json-schema.org/draft/2020-12/schema",
        )
        fixtures = _json_code_blocks(EFFECTS_CONTRACT_DOC)
        self.assertTrue(fixtures, "effects contract should include a JSON fixture")
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                self.assertTrue(_schema_matches(fixture, schema, schema))
                raw = json.dumps(fixture, ensure_ascii=False, separators=(",", ":"))
                self.assertEqual(parse_effect_document(raw), fixture)

    def test_effect_operation_fixtures_match_schema_and_runtime_parser(self) -> None:
        schema = json.loads(EFFECTS_SCHEMA.read_text(encoding="utf-8"))
        fixtures = (
            {"v": 1, "expect": {}, "effects": [], "reason": "No changes"},
            {
                "v": 1,
                "expect": {},
                "effects": [
                    [
                        "create",
                        "$work",
                        {
                            "title": "Work",
                            "objective": "Finish",
                            "priority": 1,
                            "requires": [],
                        },
                    ]
                ],
            },
            {
                "v": 1,
                "expect": {"TASK-1": 1},
                "effects": [
                    ["update", "TASK-1", {"objective": None, "parent": None}],
                    ["transition", "TASK-1", "ready"],
                    ["require", "TASK-1", "TASK-2"],
                    ["unrequire", "TASK-1", "TASK-3"],
                ],
            },
            {
                "v": 1,
                "expect": {"TASK-1": 2, "TASK-2": 1},
                "effects": [
                    ["transition", "TASK-1", "blocked", {"reason": "Waiting"}],
                    [
                        "transition",
                        "TASK-2",
                        "done",
                        {"claim": "clm_0123456789abcdef0123456789abcdef"},
                    ],
                ],
            },
            {
                "v": 1,
                "expect": {"TASK-1": 2, "TASK-2": 1},
                "effects": [
                    [
                        "transition",
                        "TASK-1",
                        "superseded",
                        {"by": "TASK-2", "reason": "Replaced"},
                    ]
                ],
            },
        )
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                self.assertTrue(_schema_matches(fixture, schema, schema))
                raw = json.dumps(fixture, ensure_ascii=False, separators=(",", ":"))
                self.assertEqual(parse_effect_document(raw), fixture)

        invalid_fixtures = (
            {"v": 1, "expect": {}, "effects": []},
            {
                "v": 1,
                "expect": {},
                "effects": [["unknown", "TASK-1"]],
            },
        )
        for fixture in invalid_fixtures:
            with self.subTest(invalid=fixture):
                self.assertFalse(_schema_matches(fixture, schema, schema))
                with self.assertRaises(JournalError):
                    parse_effect_document(json.dumps(fixture))

    def test_documented_command_paths_exist_in_runtime_parser(self) -> None:
        documented = _documented_command_paths()
        implemented = _parser_command_paths()
        self.assertTrue(documented, "documentation should name CLI commands")
        self.assertEqual(
            documented - implemented,
            set(),
            "documented CLI command paths are absent from the parser",
        )

    def test_config_key_matrix_matches_implementation(self) -> None:
        matrix = _parse_config_matrix()
        self.assertEqual(set(matrix), set(CONFIG_KEYS))
        self.assertEqual(
            {key for key, values in matrix.items() if values["user"] == "yes"},
            set(USER_CONFIG_KEYS),
        )
        self.assertEqual(
            {key for key, values in matrix.items() if values["repo"] == "yes"},
            set(REPO_CONFIG_KEYS),
        )

        defaults = resolve_config(
            environ={},
            user_path=None,
            repo_path=None,
            default_owner="OS user",
            # The matrix cell links to the section that states the whole
            # precedence, because the default is derived and no single
            # phrase names it honestly.
            default_reader="The [session identity](#session-identity)",
        ).to_dict()
        documented_defaults = {
            key: values["default"] for key, values in matrix.items()
        }
        runtime_defaults = {
            key: str(defaults[key]) for key in CONFIG_KEYS
        }
        self.assertEqual(documented_defaults, runtime_defaults)
        self.assertEqual(_parse_environment_matrix(), ENVIRONMENT_KEYS)


if __name__ == "__main__":
    unittest.main()
