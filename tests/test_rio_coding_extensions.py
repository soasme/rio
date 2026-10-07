"""Extension event observers receive copies and retired APIs cannot be reused."""

from types import SimpleNamespace

import pytest

from rio.coding.extensions import ExtensionError, ExtensionRuntime
from rio.coding.resources import RioResourcePaths


async def test_extension_observers_and_retirement(tmp_path):
    extension = tmp_path / "observer.py"
    extension.write_text(
        'def setup(api):\n'
        '    def observe(event, ctx):\n'
        '        event["changes"]["injected"] = True\n'
        '        ctx.state["cells"].append({"id": 99})\n'
        '    api.on("turn_prepared", observe)\n'
    )
    runtime = ExtensionRuntime()
    runtime.load(RioResourcePaths(root=tmp_path, cwd=tmp_path), extra_paths=(extension,))
    session = SimpleNamespace(state={"cells": []})
    runtime.bind(session)
    event = {"type": "turn_prepared", "changes": {}}
    await runtime.emit_event(event)
    assert event["changes"] == {}
    assert session.state == {"cells": []}
    assert not runtime.diagnostics
    api = runtime._extensions[-1].api
    await runtime.aclose()
    with pytest.raises(ExtensionError):
        api.notify("stale")
