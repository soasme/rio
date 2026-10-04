"""The portable agent handles plain JSON state without coding imports."""

from __future__ import annotations

import pytest

from conftest import step_response
from rio.agent import DEFAULT_SYSTEM_PROMPT, PatchError, apply_patch, run_json_loop
from rio.ai import FakeProvider


def test_patch_is_pure_and_rejects_non_object_state():
    state = {"count": 1}
    assert apply_patch(state, [{"op": "replace", "path": "/count", "value": 2}]) == {"count": 2}
    assert state == {"count": 1}
    with pytest.raises(PatchError, match="JSON object"):
        apply_patch(state, [{"op": "replace", "path": "", "value": []}])


@pytest.mark.asyncio
async def test_json_loop_retries_and_returns_patched_state():
    provider = FakeProvider(
        [
            step_response(patch=[{"op": "remove", "path": "/missing"}]),
            step_response(patch=[{"op": "add", "path": "/done", "value": True}], reply="done"),
        ]
    )
    steps = [step async for step in run_json_loop(provider=provider, model="m", state={})]
    assert len(steps) == 1
    assert steps[0].state == {"done": True}
    assert steps[0].reply == "done"
    assert provider.calls[0][1] == DEFAULT_SYSTEM_PROMPT
    assert "Rejected reply" in provider.calls[1][2][0].text
