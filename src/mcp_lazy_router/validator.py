"""Pure-Python JSON Schema validator for pre-flight MCP argument checks."""

from __future__ import annotations

import json
import re
from typing import Any


def _json_type_name(val: Any) -> str:
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "boolean"
    if isinstance(val, int):
        return "integer"
    if isinstance(val, float):
        return "number"
    if isinstance(val, str):
        return "string"
    if isinstance(val, list):
        return "array"
    if isinstance(val, dict):
        return "object"
    return type(val).__name__


def _matches_type(val: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(val, str)
    if expected == "boolean":
        return isinstance(val, bool)
    if expected == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if expected == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if expected == "array":
        return isinstance(val, list)
    if expected == "object":
        return isinstance(val, dict)
    if expected == "null":
        return val is None
    return True


def _json_equal(a: Any, b: Any) -> bool:
    """Compare JSON values while strictly distinguishing bool from int/float."""
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if type(a) is not type(b):
        if isinstance(a, list) and isinstance(b, list):
            return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
        if isinstance(a, dict) and isinstance(b, dict):
            return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    return a == b


def _canonical_signature(val: Any) -> str:
    """Produce a type-safe canonical key for uniqueItems checking."""
    if isinstance(val, bool):
        return f"bool:{val}"
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        if isinstance(val, float) and val.is_integer():
            return f"num:{int(val)}"
        return f"num:{val}"
    if isinstance(val, list):
        return "[" + ",".join(_canonical_signature(x) for x in val) + "]"
    if isinstance(val, dict):
        items = ",".join(
            f"{json.dumps(k)}:{_canonical_signature(v)}" for k, v in sorted(val.items())
        )
        return "{" + items + "}"
    return json.dumps(val, ensure_ascii=False)


def _validate_node(val: Any, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []

    if not isinstance(schema, dict):
        return errors

    # Combinators
    if "allOf" in schema and isinstance(schema["allOf"], list):
        for idx, sub in enumerate(schema["allOf"]):
            if isinstance(sub, dict):
                sub_errs = _validate_node(val, sub, path)
                errors.extend(sub_errs)

    if "anyOf" in schema and isinstance(schema["anyOf"], list):
        sub_list = [s for s in schema["anyOf"] if isinstance(s, dict)]
        if sub_list and not any(len(_validate_node(val, s, path)) == 0 for s in sub_list):
            errors.append(f"{path}: value does not match any schema in 'anyOf'")

    if "oneOf" in schema and isinstance(schema["oneOf"], list):
        sub_list = [s for s in schema["oneOf"] if isinstance(s, dict)]
        match_count = sum(1 for s in sub_list if len(_validate_node(val, s, path)) == 0)
        if sub_list and match_count != 1:
            errors.append(
                f"{path}: value must match exactly 1 schema in 'oneOf' (matched {match_count})"
            )

    # Enum / const
    if "const" in schema:
        if not _json_equal(val, schema["const"]):
            errors.append(f"{path}: expected constant value {schema['const']!r}")

    if "enum" in schema and isinstance(schema["enum"], list):
        if not any(_json_equal(val, candidate) for candidate in schema["enum"]):
            errors.append(f"{path}: value {val!r} is not in enum {schema['enum']!r}")

    # Type check
    expected_type = schema.get("type")
    if isinstance(expected_type, str):
        if not _matches_type(val, expected_type):
            errors.append(
                f"{path}: expected type '{expected_type}', got '{_json_type_name(val)}'"
            )
            return errors
    elif isinstance(expected_type, list):
        type_names = [t for t in expected_type if isinstance(t, str)]
        if type_names and not any(_matches_type(val, t) for t in type_names):
            errors.append(
                f"{path}: expected one of types {type_names}, got '{_json_type_name(val)}'"
            )
            return errors

    # String constraints
    if isinstance(val, str):
        min_len = schema.get("minLength")
        if isinstance(min_len, int) and not isinstance(min_len, bool) and len(val) < min_len:
            errors.append(f"{path}: string length {len(val)} < minLength {min_len}")
        max_len = schema.get("maxLength")
        if isinstance(max_len, int) and not isinstance(max_len, bool) and len(val) > max_len:
            errors.append(f"{path}: string length {len(val)} > maxLength {max_len}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                if re.search(pattern, val) is None:
                    errors.append(f"{path}: string does not match pattern {pattern!r}")
            except re.error as exc:
                errors.append(f"{path}: invalid schema regex {pattern!r}: {exc}")

    # Numeric constraints (excluding bool!)
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and val < minimum:
            errors.append(f"{path}: value {val} < minimum {minimum}")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and val > maximum:
            errors.append(f"{path}: value {val} > maximum {maximum}")
        exc_min = schema.get("exclusiveMinimum")
        if isinstance(exc_min, (int, float)) and not isinstance(exc_min, bool) and val <= exc_min:
            errors.append(f"{path}: value {val} <= exclusiveMinimum {exc_min}")
        exc_max = schema.get("exclusiveMaximum")
        if isinstance(exc_max, (int, float)) and not isinstance(exc_max, bool) and val >= exc_max:
            errors.append(f"{path}: value {val} >= exclusiveMaximum {exc_max}")

    # Array constraints
    if isinstance(val, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and not isinstance(min_items, bool) and len(val) < min_items:
            errors.append(f"{path}: array length {len(val)} < minItems {min_items}")
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and not isinstance(max_items, bool) and len(val) > max_items:
            errors.append(f"{path}: array length {len(val)} > maxItems {max_items}")
        if schema.get("uniqueItems") is True:
            seen: set[str] = set()
            for idx, item in enumerate(val):
                sig = _canonical_signature(item)
                if sig in seen:
                    errors.append(f"{path}[{idx}]: duplicate item violates uniqueItems")
                    break
                seen.add(sig)
        items_schema = schema.get("items")
        if isinstance(items_schema, dict):
            for idx, item in enumerate(val):
                errors.extend(_validate_node(item, items_schema, f"{path}[{idx}]"))
        elif isinstance(items_schema, list):
            for idx, sub in enumerate(items_schema):
                if idx < len(val) and isinstance(sub, dict):
                    errors.extend(_validate_node(val[idx], sub, f"{path}[{idx}]"))

    # Object constraints
    if isinstance(val, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for req_key in required:
                if isinstance(req_key, str) and req_key not in val:
                    errors.append(f"{path}.{req_key}: missing required property '{req_key}'")

        props = schema.get("properties")
        prop_map = props if isinstance(props, dict) else {}
        for key, prop_val in val.items():
            if key in prop_map and isinstance(prop_map[key], dict):
                errors.extend(_validate_node(prop_val, prop_map[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: additional property '{key}' is not allowed")

    return errors


def validate_arguments(arguments: Any, schema: dict[str, Any] | None) -> list[str]:
    """Validate MCP tool arguments against a JSON Schema subset. Returns list of error strings."""
    if arguments is None:
        arguments = {}
    if not schema:
        if not isinstance(arguments, dict):
            return [f"$: expected type 'object', got '{_json_type_name(arguments)}'"]
        return []
    effective_schema = dict(schema)
    if "type" not in effective_schema and (
        "properties" in effective_schema or "required" in effective_schema
    ):
        effective_schema["type"] = "object"
    return _validate_node(arguments, effective_schema, "$")
