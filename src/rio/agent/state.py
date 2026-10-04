"""JSON state mutation with RFC 6902 patches."""

from __future__ import annotations

import copy
import json

import jsonpatch

from rio.ai.types import JSONObject, JSONValue


class PatchError(ValueError):
    pass


def apply_patch(state: JSONObject, patch: JSONValue) -> JSONObject:
    """Apply a patch without mutating the input; keep the state a JSON object."""
    if not isinstance(patch, list):
        raise PatchError("patch must be a JSON array of operations")
    try:
        result = jsonpatch.apply_patch(state, copy.deepcopy(patch))
    except (jsonpatch.JsonPatchException, jsonpatch.JsonPointerException, TypeError) as exc:
        raise PatchError(f"patch failed: {exc}") from exc
    if not isinstance(result, dict):
        raise PatchError("patched state must be a JSON object")
    return result


def state_tokens(state: JSONObject) -> int:
    return (len(json.dumps(state, ensure_ascii=False)) + 3) // 4
