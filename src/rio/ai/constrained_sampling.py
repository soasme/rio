"""JSON-schema strict-mode conversion for provider constrained sampling.

Ported from pi's `constrained-sampling.ts` (JSON-schema portion only -- pi's grammar
(lark/regex) constrained sampling is not ported, since no Rio provider adapter has a
grammar-tool consumer today).

A tool that wants defense against malformed argument names/shapes opts in via
`AgentTool.constrained_sampling = {"type": "json_schema", "strict": "prefer" | "require"}`.
Provider adapters call `resolve_json_schema_strict_sampling` to decide whether to ask
the provider for strict/constrained decoding, and `get_json_schema_tool_parameters` to
get the (possibly rewritten) JSON schema to send on the wire. When a provider honors
the resulting `strict: true`, it cannot emit a tool call with an argument name or shape
outside the schema -- the failure class this fixes is prevented at the source instead
of being patched after the fact in the tool executor.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from rio.ai.types import JSONValue

if TYPE_CHECKING:
    from rio.ai.tools import AgentTool

_UNSUPPORTED_STRICT_SCHEMA_KEYS = (
    "$ref",
    "$defs",
    "definitions",
    "allOf",
    "oneOf",
    "patternProperties",
    "dependentSchemas",
    "dependencies",
    "unevaluatedProperties",
    "propertyNames",
    "contains",
    "prefixItems",
    "not",
    "if",
    "then",
    "else",
)


class UnsupportedStrictJsonSchemaError(ValueError):
    """Raised when a schema cannot be safely rewritten into the strict subset."""


def _is_schema_object(value: object) -> bool:
    return isinstance(value, Mapping)


def _is_structured_schema(schema: object) -> bool:
    if not _is_schema_object(schema):
        return False
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        types: list[Any] = [raw_type]
    elif isinstance(raw_type, list):
        types = raw_type
    else:
        types = []
    return (
        "object" in types
        or "array" in types
        or schema.get("properties") is not None
        or schema.get("items") is not None
    )


def _schema_allows_null(schema: object) -> bool:
    if not _is_schema_object(schema):
        return False
    schema_type = schema.get("type")
    if schema_type == "null" or (isinstance(schema_type, list) and "null" in schema_type):
        return True
    if "const" in schema and schema.get("const") is None:
        return True
    enum = schema.get("enum")
    if isinstance(enum, list) and None in enum:
        return True
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        return any(_schema_allows_null(variant) for variant in any_of)
    return False


def _make_json_schema_node_strict(schema: dict[str, Any]) -> None:
    if not _is_schema_object(schema):
        raise UnsupportedStrictJsonSchemaError("boolean schemas are unsupported")
    for key in _UNSUPPORTED_STRICT_SCHEMA_KEYS:
        if schema.get(key) is not None:
            raise UnsupportedStrictJsonSchemaError(f"{key} schemas are unsupported")

    any_of = schema.get("anyOf")
    if any_of is not None:
        if not isinstance(any_of, list) or not any_of:
            raise UnsupportedStrictJsonSchemaError("anyOf must contain at least one schema")
        for variant in any_of:
            if _is_structured_schema(variant):
                raise UnsupportedStrictJsonSchemaError("object and array unions are unsupported")
            _make_json_schema_node_strict(variant)

    items = schema.get("items")
    if items is not None:
        if isinstance(items, list):
            raise UnsupportedStrictJsonSchemaError("tuple schemas are unsupported")
        _make_json_schema_node_strict(items)

    is_object_schema = schema.get("type") == "object"
    if schema.get("properties") is not None and not is_object_schema:
        raise UnsupportedStrictJsonSchemaError("properties require type object")
    if not is_object_schema:
        return

    additional_properties = schema.get("additionalProperties")
    if additional_properties is not None and additional_properties is not False:
        raise UnsupportedStrictJsonSchemaError(
            "schema-valued or true additionalProperties is unsupported"
        )
    properties = schema.get("properties")
    if properties is not None and not _is_schema_object(properties):
        raise UnsupportedStrictJsonSchemaError("object properties must be a schema map")
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list) or any(not isinstance(key, str) for key in required)
    ):
        raise UnsupportedStrictJsonSchemaError("object required must be a string array")

    properties = dict(schema.get("properties") or {})
    property_names = list(properties.keys())
    required_set = set(required) if isinstance(required, list) else set()
    if any(key not in property_names for key in required_set):
        raise UnsupportedStrictJsonSchemaError("required contains an unknown property")

    for key, property_schema in properties.items():
        _make_json_schema_node_strict(property_schema)
        if key not in required_set and not _schema_allows_null(property_schema):
            properties[key] = {"anyOf": [property_schema, {"type": "null"}]}

    schema["properties"] = properties
    schema["required"] = property_names
    schema["additionalProperties"] = False


def make_strict_json_schema(schema: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    """Convert a tool schema to the strict subset expected by constrained sampling.

    Raises `UnsupportedStrictJsonSchemaError` when the schema uses a construct that
    cannot be safely rewritten (`$ref`, `oneOf`, object/array unions, schema-valued
    `additionalProperties`, ...).
    """
    cloned = copy.deepcopy(dict(schema))
    if not _is_schema_object(cloned):
        raise UnsupportedStrictJsonSchemaError("root schema must have type object")
    _make_json_schema_node_strict(cloned)
    if cloned.get("type") != "object":
        raise UnsupportedStrictJsonSchemaError("root schema must have type object")
    return cloned


def get_json_schema_tool_parameters(
    schema: Mapping[str, JSONValue], strict: bool | None
) -> dict[str, JSONValue]:
    """Return the schema to send on the wire: strict-rewritten when `strict is True`."""
    if strict is True:
        return make_strict_json_schema(schema)
    return dict(schema)


def resolve_json_schema_strict_sampling(
    tool: AgentTool, supports_strict_mode: bool
) -> bool | None:
    """Decide whether to request strict/constrained decoding for `tool`.

    Returns `True` when the tool opted in (`constrained_sampling`) and its schema
    converts cleanly; `None` when the tool didn't opt in, the config isn't
    `json_schema`, or conversion isn't possible and the tool only "prefer"s strict
    mode. Raises `ValueError` when the tool "require"s strict mode but it isn't
    available or the schema can't be converted.
    """
    config = tool.constrained_sampling
    if not config or config.get("type") != "json_schema":
        return None
    strict_requirement = config.get("strict")

    if supports_strict_mode:
        try:
            make_strict_json_schema(tool.input_schema)
            return True
        except UnsupportedStrictJsonSchemaError as exc:
            if strict_requirement != "require":
                return None
            raise ValueError(
                f'Tool "{tool.name}" requires JSON-schema constrained sampling, but {exc}.'
            ) from exc

    if strict_requirement == "require":
        raise ValueError(
            f'Tool "{tool.name}" requires JSON-schema constrained sampling, but strict '
            "tools are unsupported."
        )
    return None
