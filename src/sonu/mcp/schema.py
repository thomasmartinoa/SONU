"""An outside tool's argument schema (D63): kept to what SONU understands, and checked by SONU itself.

An MCP server describes its tools' arguments with JSON Schema. The planner's output is held to that
schema (Ollama's structured output), and SONU checks the arguments again before calling — so the
schema is cut down to the parts SONU can check: types, properties, required, enum, lengths, ranges,
array items, local ``$ref``s. Anything else (``anyOf``, patterns, formats) becomes "any value", which the
server still checks. Unknown properties are refused at the top level.
"""

from __future__ import annotations

from typing import Any

MAX_DEPTH = 4
MAX_PROPERTIES = 24
MAX_DESCRIPTION = 120
_TYPES = ("string", "integer", "number", "boolean", "array", "object", "null")
_KEPT = ("minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems")


def clean_schema(schema: Any) -> dict[str, Any]:
    """The arguments' schema, cut down to what SONU checks (always an object at the top)."""
    defs = schema.get("$defs") or schema.get("definitions") or {} if isinstance(schema, dict) else {}
    result = _clean(schema, defs if isinstance(defs, dict) else {}, 0)
    if result.get("type") != "object":
        result = {"type": "object", "properties": {}}
    result["additionalProperties"] = False
    return result


def _clean(node: Any, defs: dict[str, Any], depth: int) -> dict[str, Any]:
    if not isinstance(node, dict) or depth > MAX_DEPTH:
        return {}
    ref = node.get("$ref")
    if isinstance(ref, str):
        target = defs.get(ref.rsplit("/", 1)[-1]) if ref.startswith("#/") else None
        return _clean(target, defs, depth + 1)
    kind = node.get("type")
    if isinstance(kind, list):  # ["string", "null"]: the first real one
        kind = next((k for k in kind if k in _TYPES and k != "null"), None)
    if kind not in _TYPES:
        return {}
    result: dict[str, Any] = {"type": kind}
    description = node.get("description")
    if isinstance(description, str) and description.strip():
        result["description"] = " ".join(description.split())[:MAX_DESCRIPTION]
    enum = node.get("enum")
    if isinstance(enum, list) and enum and all(isinstance(v, str | int | float | bool) for v in enum):
        result["enum"] = enum[:50]
    for key in _KEPT:
        value = node.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            result[key] = value
    if kind == "array":
        result["items"] = _clean(node.get("items"), defs, depth + 1)
    if kind == "object":
        properties = node.get("properties")
        props = properties if isinstance(properties, dict) else {}
        result["properties"] = {
            str(name): _clean(sub, defs, depth + 1) for name, sub in list(props.items())[:MAX_PROPERTIES]
        }
        required = node.get("required")
        if isinstance(required, list):
            result["required"] = [name for name in required if name in result["properties"]]
    return result


def problems(value: Any, schema: dict[str, Any], where: str = "") -> list[str]:
    """What's wrong with ``value`` for ``schema`` (a cleaned one); empty when it fits."""
    kind = schema.get("type")
    label = where or "arguments"
    if kind is None:
        return []
    if not _is(value, kind):
        return [f"{label}: should be {'an' if kind in ('array', 'object', 'integer') else 'a'} {kind}"]
    found: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{label}: should be one of {', '.join(map(str, schema['enum'][:8]))}")
    low, high = schema.get("minLength", 0), schema.get("maxLength", float("inf"))
    if kind == "string" and not low <= len(value) <= high:
        found.append(f"{label}: wrong length")
    low, high = schema.get("minimum", float("-inf")), schema.get("maximum", float("inf"))
    if kind in ("integer", "number") and not low <= value <= high:
        found.append(f"{label}: out of range")
    if kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", float("inf")):
            found.append(f"{label}: wrong number of items")
        for index, item in enumerate(value):
            found += problems(item, schema.get("items", {}), f"{label}[{index}]")
    if kind == "object":
        properties = schema.get("properties", {})
        prefix = f"{where}." if where else ""
        for name in schema.get("required", []):
            if name not in value:
                found.append(f"{prefix}{name}: required")
        for name, item in value.items():
            if name in properties:
                found += problems(item, properties[name], f"{prefix}{name}")
            elif schema.get("additionalProperties") is False:
                found.append(f"{prefix}{name}: not an argument of this tool")
    return found


def _is(value: Any, kind: str) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return isinstance(
        value,
        {
            "string": str,
            "boolean": bool,
            "array": list,
            "object": dict,
            "null": type(None),
        }[kind],
    )


def strings_in(value: Any) -> list[str]:
    """Every text in the arguments (nested too): what grounding checks against the user's words."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in strings_in(item)]
    if isinstance(value, list):
        return [text for item in value for text in strings_in(item)]
    return []
