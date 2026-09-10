"""`O_t`: what a step observes.

The paper's third input to a step is one value, so this is one value too. It
carries which kind of thing arrived rather than leaving the model to guess
from the text: the result of the action the previous step took, a message
from the user, or -- when a message interrupts a run -- both. A turn's first
step has only a message, because no action has run yet.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HarnessObservation:
    """The inputs a step observes, each labelled for what it is."""

    user_message: str | None = None
    tool_call_result: str | None = None

    def __post_init__(self) -> None:
        if self.user_message is None and self.tool_call_result is None:
            raise ValueError("a step needs something to observe: a user message, a result, or both")
