"""Event renderers for rio coding frontends and print modes."""

from __future__ import annotations

from rio.coding.rendering.base import EventRenderer, PrintOutputMode
from rio.coding.rendering.json import JsonEventRenderer, event_to_json
from rio.coding.rendering.plain import PlainEventRenderer
from rio.coding.rendering.steps import render_completed_run, render_final_state, render_run_steps


def create_event_renderer(mode: PrintOutputMode) -> EventRenderer:
    """Create a live per-event renderer for a print output mode."""
    if mode is PrintOutputMode.json:
        return JsonEventRenderer()
    return PlainEventRenderer()


__all__ = [
    "EventRenderer",
    "JsonEventRenderer",
    "PlainEventRenderer",
    "PrintOutputMode",
    "create_event_renderer",
    "event_to_json",
    "render_completed_run",
    "render_final_state",
    "render_run_steps",
]
