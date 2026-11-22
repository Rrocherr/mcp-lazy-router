"""Unit tests for the pure-Python JSON Schema pre-flight validator."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcp_lazy_router.validator import validate_arguments


class ValidatorTests(unittest.TestCase):
    def test_bool_is_rejected_for_integer_and_number(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "flag": {"type": "boolean"},
            },
        }
        errs = validate_arguments({"count": True, "ratio": False, "flag": True}, schema)
        self.assertEqual(len(errs), 2)
        self.assertTrue(any("$.count" in e for e in errs))
        self.assertTrue(any("$.ratio" in e for e in errs))

        ok = validate_arguments({"count": 5, "ratio": 2, "flag": False}, schema)
        self.assertEqual(ok, [])

    def test_enum_and_const_distinguish_bool_from_int(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "mode": {"enum": [1, "auto"]},
                "strict_zero": {"const": 0},
            },
        }
        errs = validate_arguments({"mode": True, "strict_zero": False}, schema)
        self.assertEqual(len(errs), 2)
        self.assertEqual(validate_arguments({"mode": 1, "strict_zero": 0}, schema), [])

    def test_required_additional_properties_and_nested_objects(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "user": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 2, "maxLength": 10},
                        "age": {"type": "integer", "minimum": 0, "maximum": 120},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
            "required": ["user"],
            "additionalProperties": False,
        }
        errs = validate_arguments({"user": {"age": 150, "extra": 1}, "bad": True}, schema)
        self.assertTrue(any("$.user.name" in e for e in errs))
        self.assertTrue(any("$.user.age" in e for e in errs))
        self.assertTrue(any("$.user.extra" in e for e in errs))
        self.assertTrue(any("$.bad" in e for e in errs))

        self.assertEqual(validate_arguments({"user": {"name": "Ada", "age": 30}}, schema), [])

    def test_arrays_patterns_and_combinators(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3,
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": r"^[a-z]+$"},
                },
                "id": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                "choice": {"oneOf": [{"type": "integer", "minimum": 10}, {"type": "integer", "maximum": 5}]},
                "both": {"allOf": [{"type": "integer"}, {"minimum": 2}]},
            },
        }
        self.assertEqual(
            validate_arguments({"tags": ["dev", "ops"], "id": 42, "choice": 12, "both": 3}, schema),
            [],
        )
        errs = validate_arguments(
            {"tags": ["dev", "dev", "BAD!"], "id": True, "choice": 7, "both": 1},
            schema,
        )
        self.assertGreaterEqual(len(errs), 4)


if __name__ == "__main__":
    unittest.main()
