"""Execution state Σ and the ΔΣ merge rule from arXiv:2608.26263 (§3.1, Algorithm 1).

Σ is a plain JSON object. A step proposes ΔΣ, a partial update; the runtime
commits Σ_{t+1} = Σ_t ⊕ ΔΣ_t using RFC 7396 JSON Merge Patch semantics: a
`null` value deletes the key, an object value merges recursively, anything
else replaces the key in place. This is the "dictionary merge with
null-deletion semantics" the paper describes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from rio_agent.errors import StateValidationError
from rio_ai.types import JSONObject, JSONValue


def apply_state_delta(state: JSONObject, delta: Mapping[str, JSONValue]) -> JSONObject:
    """Return Σ_{t+1} = Σ_t ⊕ ΔΣ_t. Does not mutate `state`."""
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
