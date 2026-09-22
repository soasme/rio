"""Tests for `rio.ai.constrained_sampling`, ported from pi's

`packages/ai/test/constrained-sampling.test.ts` (JSON-schema portion only).
"""

from __future__ import annotations

import pytest

from rio.ai.constrained_sampling import (
    UnsupportedStrictJsonSchemaError,
    get_json_schema_tool_parameters,
    make_strict_json_schema,
    resolve_json_schema_strict_sampling,
)
from rio.ai.tools import AgentTool


async def _noop_execute(tool_call_id, arguments, signal=None, on_update=None):
    raise NotImplementedError


def _make_tool(parameters: dict, *, constrained_sampling: dict | None = None) -> AgentTool:
    return AgentTool(
        name="sample_tool",
        label="sample_tool",
        description="Sample tool",
        parameters=parameters,
        execute_fn=_noop_execute,
        constrained_sampling=constrained_sampling,
    )


def test_optional_properties_become_nullable_and_nested_objects_get_locked_down():
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "number"},
            "metadata": {
                "type": "object",
                "properties": {"enabled": {"type": "boolean"}},
            },
            "nullable": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["path", "metadata"],
    }

    strict = make_strict_json_schema(parameters)

    # The input schema is not mutated in place.
    assert "additionalProperties" not in parameters
    assert parameters["required"] == ["path", "metadata"]

    assert strict["additionalProperties"] is False
    assert strict["required"] == ["path", "offset", "metadata", "nullable"]
    assert strict["properties"]["offset"] == {"anyOf": [{"type": "number"}, {"type": "null"}]}
    assert strict["properties"]["metadata"] == {
        "type": "object",
        "additionalProperties": False,
        "required": ["enabled"],
        "properties": {"enabled": {"anyOf": [{"type": "boolean"}, {"type": "null"}]}},
    }
    # Already-nullable fields are not re-wrapped.
    assert strict["properties"]["nullable"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}


@pytest.mark.parametrize(
    ("parameters", "error"),
    [
        (
            {
                "type": "object",
                "properties": {
                    "metadata": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": {"type": "string"},
                    }
                },
                "required": ["metadata"],
            },
            "additionalProperties is unsupported",
        ),
        (
            {
                "allOf": [
                    {"type": "object", "properties": {"a": {"type": "string"}}},
                    {"type": "object", "properties": {"b": {"type": "number"}}},
                ],
            },
            "allOf schemas are unsupported",
        ),
        (
            {
                "type": "object",
                "properties": {
                    "value": {
                        "anyOf": [
                            {"type": "object", "properties": {"nested": {"type": "string"}}},
                            {"type": "null"},
                        ]
                    }
                },
                "required": ["value"],
            },
            "object and array unions are unsupported",
        ),
        (
            {
                "type": "object",
                "properties": {"child": {"$ref": "https://example.com/child.json"}},
                "required": ["child"],
            },
            "$ref schemas are unsupported",
        ),
    ],
)
def test_schemas_that_cannot_be_safely_converted_are_rejected(parameters, error):
    with pytest.raises(UnsupportedStrictJsonSchemaError) as exc_info:
        make_strict_json_schema(parameters)
    assert error in str(exc_info.value)

    tool = _make_tool(parameters, constrained_sampling={"type": "json_schema", "strict": "prefer"})
    assert resolve_json_schema_strict_sampling(tool, True) is None
    assert get_json_schema_tool_parameters(tool.input_schema, None) == parameters

    require_tool = _make_tool(
        parameters, constrained_sampling={"type": "json_schema", "strict": "require"}
    )
    with pytest.raises(ValueError) as require_exc_info:
        resolve_json_schema_strict_sampling(require_tool, True)
    assert error in str(require_exc_info.value)


def test_resolve_returns_none_when_tool_did_not_opt_in():
    tool = _make_tool({"type": "object", "properties": {}})
    assert resolve_json_schema_strict_sampling(tool, True) is None


def test_resolve_returns_none_when_constrained_sampling_is_not_json_schema():
    tool = _make_tool(
        {"type": "object", "properties": {}},
        constrained_sampling={"type": "grammar", "variants": {}},
    )
    assert resolve_json_schema_strict_sampling(tool, True) is None


def test_resolve_prefers_strict_when_supported_and_schema_converts_cleanly():
    tool = _make_tool(
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        constrained_sampling={"type": "json_schema", "strict": "prefer"},
    )
    assert resolve_json_schema_strict_sampling(tool, True) is True


def test_resolve_falls_back_to_none_when_provider_does_not_support_strict_mode():
    tool = _make_tool(
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        constrained_sampling={"type": "json_schema", "strict": "prefer"},
    )
    assert resolve_json_schema_strict_sampling(tool, False) is None


def test_resolve_raises_when_strict_is_required_but_provider_does_not_support_it():
    tool = _make_tool(
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        constrained_sampling={"type": "json_schema", "strict": "require"},
    )
    with pytest.raises(ValueError, match="strict tools are unsupported"):
        resolve_json_schema_strict_sampling(tool, False)


def test_get_json_schema_tool_parameters_passes_through_when_not_strict():
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    assert get_json_schema_tool_parameters(parameters, None) == parameters
    assert get_json_schema_tool_parameters(parameters, False) == parameters
