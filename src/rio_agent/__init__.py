"""rio_agent: a SKILL.state long-horizon agent runtime.

Implements the runtime architecture from Badhe, Tiwari & Chung,
"SKILL.state: Scalable Long-Horizon Agent Skills" (arXiv:2608.26263, EMNLP):
an immutable procedural skill specification P, a structured mutable
execution state Σ, and the latest observation O are the only inputs to each
step; intermediate reasoning is discarded after producing a validated state
update, keeping per-step prompt size at O(|P| + |Σ| + |O|) instead of
growing with the number of steps already taken.

Exposes a loop interface shaped like `rio_ai`'s ported tau_agent-style
`AgentHarness`/`run_agent_loop` (a stateful harness plus a bare async-
generator loop function, both emitting a typed event stream), but backed
internally by execution state rather than an append-only transcript.
"""

# ruff: noqa: F401 - this module intentionally defines the public facade

from rio_agent.errors import ActionNotFoundError, RetriesExhaustedError, StateValidationError
from rio_agent.events import (
    ActionEndEvent,
    ActionStartEvent,
    ReasoningDiscardedEvent,
    RunEndEvent,
    RunStartEvent,
    SkillEvent,
    StateUpdateEvent,
    StepEndEvent,
    StepStartEvent,
    ValidationErrorEvent,
)
from rio_agent.harness import (
    EventListener,
    SimpleCancellationToken,
    SkillStateHarness,
    SkillStateHarnessConfig,
)
from rio_agent.loop import run_skill_loop
from rio_agent.prompt import STEP_TOOL_NAME, build_step_messages, skill_step_tool
from rio_agent.skill import SkillSpec
from rio_agent.state import apply_state_delta, validate_state_delta

__all__ = [name for name in globals() if not name.startswith("_")]
