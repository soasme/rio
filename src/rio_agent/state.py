"""How the execution state is updated (arXiv:2608.26263 §3.1, Algorithm 1).

The state is a plain JSON object. Each step proposes a partial update. The
runtime merges it into the state using RFC 7396 JSON Merge Patch rules: a
`null` value deletes the key, an object value merges recursively, and any
other value replaces the key in place. The paper calls this a dictionary
merge with null-deletion semantics.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from rio_agent.errors import StateValidationError
from rio_ai.types import JSONObject, JSONValue


def apply_state_delta(state: JSONObject, delta: Mapping[str, JSONValue]) -> JSONObject:
    """Merge `delta` into `state` and return the new state. Does not change `state` in place."""
    result = dict(state)
    for key, value in delta.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = apply_state_delta(result[key], value)  # type: ignore[arg-type]
        else:
            result[key] = value
    return result


def validate_state_delta(
    delta: Mapping[str, JSONValue],
    *,
    allowed_fields: Sequence[str] | None,
) -> None:
    """Reject a delta that isn't a JSON object or that touches undeclared fields.

    `allowed_fields` is the domain's schema, authored once per skill rather
    than per task (paper §4.1). Passing `None` skips the field-membership
    check for skills that don't declare a fixed schema.
    """
    if not isinstance(delta, Mapping):
        raise StateValidationError(f"state_delta must be a JSON object, got {type(delta).__name__}")
    if allowed_fields is None:
        return
    unknown = sorted(set(delta) - set(allowed_fields))
    if unknown:
        raise StateValidationError(
            f"state_delta references undeclared field(s) {unknown}; "
            f"declared fields are {sorted(allowed_fields)}"
        )
