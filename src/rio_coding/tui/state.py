"""Frontend-only step history; only execution state is model memory."""

from dataclasses import dataclass, field

from rio_ai.types import JSONObject
from rio_coding.tui.themes import StepStreamRole


@dataclass(frozen=True, slots=True)
class StepStreamItem:
    role: StepStreamRole
    text: str
    continuation: bool = False
    full_text: str | None = None


@dataclass(slots=True)
class TuiState:
    state: JSONObject = field(default_factory=dict)
    items: list[StepStreamItem] = field(default_factory=list)
    step: int = 0
    running: bool = False
    queued: int = 0
    active_action: str | None = None
