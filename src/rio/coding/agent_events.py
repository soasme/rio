"""Events emitted by `rio.coding.loop.run_notebook_loop`.

A flat stream of small, typed records a UI or logger can subscribe to. Each
step is one model call, one patch, and one run of the changed cells;
`notebook` is the full context the model will see next.
"""

from __future__ import annotations

from dataclasses import dataclass

from rio.ai.types import JSONObject
from rio.coding.notebook import Notebook


@dataclass(frozen=True, slots=True)
class RunStartEvent:
    skill: str


@dataclass(frozen=True, slots=True)
class StepStartEvent:
    step: int
    notebook: Notebook


@dataclass(frozen=True, slots=True)
class ReasoningEvent:
    """The text the model wrote before its patch. It is not kept in the notebook."""

    step: int
    reasoning: str


@dataclass(frozen=True, slots=True)
class ValidationErrorEvent:
    """The reply had no usable patch; a retry with a correction follows."""

    step: int
    attempt: int
    error: str


@dataclass(frozen=True, slots=True)
class PatchEvent:
    """The model's patch was applied; `cells` are the indices of code cells about to run."""

    step: int
    patch: list[JSONObject]
    cells: list[int]


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    """The changed cells ran; their outputs are in `notebook`."""

    step: int
    cells: list[int]
    notebook: Notebook


@dataclass(frozen=True, slots=True)
class StepEndEvent:
    step: int
    notebook: Notebook
    reply: str | None

    @property
    def terminated(self) -> bool:
        return self.reply is not None


@dataclass(frozen=True, slots=True)
class RunEndEvent:
    steps: int
    notebook: Notebook
    reply: str | None


type AgentEvent = (
    RunStartEvent
    | StepStartEvent
    | ReasoningEvent
    | ValidationErrorEvent
    | PatchEvent
    | ExecutionEvent
    | StepEndEvent
    | RunEndEvent
)
