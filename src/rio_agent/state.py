"""How the execution state is updated.

The state is a plain JSON object. Each step proposes a partial update. The
runtime merges it into the state using RFC 7396 JSON Merge Patch rules: a
`null` value deletes the key, an object value merges recursively, and any
other value replaces the key in place.
"""

from __future__ import annotations

import json
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
    than per task. Passing `None` skips the field-membership check for
    skills that don't declare a fixed schema.
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


def state_size_chars(state: Mapping[str, JSONValue]) -> int:
    """Return the size of `state` as the prompt serializes it.

    Measured in the exact form `rio_agent.prompt.build_step_messages` sends,
    so the number the budget is checked against is the number the model is
    actually charged for.
    """
    return len(json.dumps(state, indent=2, sort_keys=True))


def check_state_budget(state: Mapping[str, JSONValue], *, max_chars: int | None) -> None:
    """Reject a state that no longer fits the prompt.

    The whole state is sent every step, so it cannot be allowed to grow
    without bound: a state that outgrows its budget would push the model's
    own context window over sooner or later. The rejection travels the same
    rollback-retry path as any other invalid delta, so the model gets a chance
    to drop what it no longer needs and propose the step again.
    """
    if max_chars is None:
        return
    size = state_size_chars(state)
    if size <= max_chars:
        return
    raise StateValidationError(
        f"the updated state would be {size} characters, over the {max_chars} limit. "
        "Free space before retrying: set the largest values you no longer need to null, "
        "keeping a one-line summary of each in its place."
    )
