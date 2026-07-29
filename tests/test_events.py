from __future__ import annotations

import json
import unittest

from aiq.events import (
    CONTENT_MAX_BYTES,
    CONTEXT_ID_MAX_BYTES,
    CWD_MAX_BYTES,
    EVENT_JSON_MAX_BYTES,
    EVENT_VERSION,
    IDEMPOTENCY_KEY_MAX_BYTES,
    SOURCE_MAX_BYTES,
    CanonicalEvent,
    EventError,
    parse_event_json,
    validate_event,
)


def encoded(document: object) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


class CanonicalEventTest(unittest.TestCase):
    def assert_event_error(
        self,
        raw: bytes | str,
        *,
        code: str,
        path: str = "$",
    ) -> EventError:
        with self.assertRaises(EventError) as caught:
            parse_event_json(raw)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.path, path)
        return caught.exception

    def test_minimal_event(self) -> None:
        event = parse_event_json(
            b'{"v":1,"source":"generic","content":"do this"}'
        )

        self.assertEqual(
            event,
            CanonicalEvent(source="generic", content="do this"),
        )
        self.assertEqual(
            event.to_dict(),
            {"v": EVENT_VERSION, "source": "generic", "content": "do this"},
        )

    def test_full_event_preserves_exact_values(self) -> None:
        document = {
            "v": 1,
            "source": "example-agent.v2",
            "content": " first line\nsecond line\t\x00\u2603 ",
            "idempotency_key": "example:event/123",
            "session_id": "session:one",
            "turn_id": "turn:two",
            "cwd": "/tmp/repository with spaces",
        }

        event = parse_event_json(encoded(document))

        self.assertEqual(event.to_dict(), document)

    def test_string_input_is_supported(self) -> None:
        event = parse_event_json(
            '{"v":1,"source":"script","content":"message"}'
        )

        self.assertEqual(event.source, "script")

    def test_decoded_document_can_be_validated_without_json_round_trip(self) -> None:
        event = validate_event(
            {"v": 1, "source": "provider", "content": "message"}
        )

        self.assertEqual(
            event,
            CanonicalEvent(source="provider", content="message"),
        )

    def test_top_level_must_be_object(self) -> None:
        self.assert_event_error(
            '["not", "an", "object"]',
            code="invalid_event",
        )

    def test_decoded_field_names_must_be_strings(self) -> None:
        with self.assertRaises(EventError) as caught:
            validate_event(
                {  # type: ignore[dict-item]
                    "v": 1,
                    "source": "generic",
                    "content": "x",
                    1: "invalid",
                }
            )
        self.assertEqual(caught.exception.code, "invalid_event")

    def test_required_fields_are_reported_deterministically(self) -> None:
        error = self.assert_event_error(
            "{}",
            code="invalid_event",
            path="$.content",
        )

        self.assertEqual(str(error), "$.content is required")

    def test_unknown_field_is_rejected(self) -> None:
        self.assert_event_error(
            '{"v":1,"source":"generic","content":"x","metadata":{}}',
            code="invalid_event",
            path="$.metadata",
        )

    def test_duplicate_key_is_rejected(self) -> None:
        self.assert_event_error(
            '{"v":1,"source":"one","source":"two","content":"x"}',
            code="invalid_json",
            path="$.source",
        )

    def test_nonstandard_json_constants_are_rejected(self) -> None:
        self.assert_event_error(
            '{"v":1,"source":"generic","content":NaN}',
            code="invalid_json",
        )

    def test_malformed_json_is_rejected(self) -> None:
        self.assert_event_error(
            '{"v":1',
            code="invalid_json",
        )

    def test_invalid_utf8_is_rejected(self) -> None:
        self.assert_event_error(
            b'{"v":1,"source":"generic","content":"\xff"}',
            code="invalid_utf8",
        )

    def test_input_type_is_rejected(self) -> None:
        self.assert_event_error(123, code="invalid_input")  # type: ignore[arg-type]

    def test_encoded_document_limit_is_enforced_before_parsing(self) -> None:
        self.assert_event_error(
            b" " * (EVENT_JSON_MAX_BYTES + 1),
            code="event_too_large",
        )

    def test_version_must_be_exact_integer_one(self) -> None:
        for version in (True, 0, 2, 1.0, "1"):
            with self.subTest(version=version):
                self.assert_event_error(
                    encoded(
                        {
                            "v": version,
                            "source": "generic",
                            "content": "x",
                        }
                    ),
                    code="unsupported_event_version",
                    path="$.v",
                )

    def test_source_is_bounded_and_has_stable_syntax(self) -> None:
        valid = "a" * SOURCE_MAX_BYTES
        self.assertEqual(
            parse_event_json(
                encoded({"v": 1, "source": valid, "content": "x"})
            ).source,
            valid,
        )
        for source in (
            "",
            "Generic",
            "two words",
            "-leading",
            "a" * (SOURCE_MAX_BYTES + 1),
        ):
            with self.subTest(source=source):
                self.assert_event_error(
                    encoded({"v": 1, "source": source, "content": "x"}),
                    code="invalid_event",
                    path="$.source",
                )

    def test_content_is_nonempty_bounded_utf8(self) -> None:
        content = "\N{SNOWMAN}" * (CONTENT_MAX_BYTES // 3)
        event = parse_event_json(
            encoded({"v": 1, "source": "generic", "content": content})
        )
        self.assertEqual(event.content, content)

        for invalid in ("", "x" * (CONTENT_MAX_BYTES + 1)):
            with self.subTest(length=len(invalid)):
                self.assert_event_error(
                    encoded(
                        {"v": 1, "source": "generic", "content": invalid}
                    ),
                    code="invalid_event",
                    path="$.content",
                )

    def test_lone_surrogate_is_rejected(self) -> None:
        self.assert_event_error(
            '{"v":1,"source":"generic","content":"\\ud800"}',
            code="invalid_event",
            path="$.content",
        )

    def test_optional_fields_must_be_nonempty_strings(self) -> None:
        for name in ("idempotency_key", "session_id", "turn_id", "cwd"):
            for value in (None, "", 1, True, []):
                with self.subTest(name=name, value=value):
                    self.assert_event_error(
                        encoded(
                            {
                                "v": 1,
                                "source": "generic",
                                "content": "x",
                                name: value,
                            }
                        ),
                        code="invalid_event",
                        path=f"$.{name}",
                    )

    def test_optional_identifier_limits_are_enforced(self) -> None:
        limits = {
            "idempotency_key": IDEMPOTENCY_KEY_MAX_BYTES,
            "session_id": CONTEXT_ID_MAX_BYTES,
            "turn_id": CONTEXT_ID_MAX_BYTES,
        }
        for name, maximum in limits.items():
            with self.subTest(name=name):
                document = {
                    "v": 1,
                    "source": "generic",
                    "content": "x",
                    name: "x" * maximum,
                }
                self.assertEqual(
                    getattr(parse_event_json(encoded(document)), name),
                    document[name],
                )
                document[name] += "x"
                self.assert_event_error(
                    encoded(document),
                    code="invalid_event",
                    path=f"$.{name}",
                )

    def test_cwd_must_be_absolute_and_cannot_contain_nul(self) -> None:
        for cwd in ("relative/path", "/tmp/\x00suffix"):
            with self.subTest(cwd=cwd):
                self.assert_event_error(
                    encoded(
                        {
                            "v": 1,
                            "source": "generic",
                            "content": "x",
                            "cwd": cwd,
                        }
                    ),
                    code="invalid_event",
                    path="$.cwd",
                )

    def test_cwd_byte_limit_is_enforced(self) -> None:
        cwd = "/" + ("x" * (CWD_MAX_BYTES - 1))
        self.assertEqual(
            parse_event_json(
                encoded(
                    {
                        "v": 1,
                        "source": "generic",
                        "content": "x",
                        "cwd": cwd,
                    }
                )
            ).cwd,
            cwd,
        )
        self.assert_event_error(
            encoded(
                {
                    "v": 1,
                    "source": "generic",
                    "content": "x",
                    "cwd": cwd + "x",
                }
            ),
            code="invalid_event",
            path="$.cwd",
        )


if __name__ == "__main__":
    unittest.main()
