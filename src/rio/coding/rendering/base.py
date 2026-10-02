"""Shared event-rendering primitives for rio coding frontends and print modes.

There is no character-by-character assistant stream to replay live. What a
renderer consumes is `rio.coding.events.CodingSessionEvent`: the per-step
lifecycle events `rio.agent` emits, plus the session-level events
`rio.coding.events` defines.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from rio.coding.events import CodingSessionEvent


class PrintOutputMode(StrEnum):
    """Output modes supported by non-interactive print mode.

    tau also had a `transcript` mode: a live, human-readable stream of
    character-by-character assistant text and tool-call blocks, and an
    unimplemented `rpc` mode. Neither has a live equivalent here. There is no
    character-level text stream to replay -- each step's reply arrives whole.
    `rpc` was never implemented by tau's rendering module either; it lived in
    a CLI layer this package does not port. A completed run's step-by-step
    account -- tau's `transcript` mode's actual successor -- is rendered after
    the fact from the journal by `rio.coding.rendering.steps`, which does not
    implement `EventRenderer`
    because it has no live event stream to consume.
    """

    human = "human"
    json = "json"


class EventRenderer(Protocol):
    """Consumes coding-session events and renders them for a frontend or output mode."""

    def render(self, event: CodingSessionEvent) -> None:
        """Render one event."""

    def finish(self) -> bool:
        """Finish rendering and return whether the run succeeded."""
