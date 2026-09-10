"""Fixed per-step token-footprint estimation for SKILL.state runs.

Ported from tau's ``context_window.py``. tau mixed two concerns in that
module: estimating token counts, and running an LLM-summarization compaction
pass because tau's transcript grows without bound as a session goes on.

Under SKILL.state, the model never sees a growing transcript: each step's
prompt is exactly the fixed skill instructions, the current execution state,
and the latest observation (see ``rio.agent.prompt.build_step_messages`` and
``rio.agent.loop.run_skill_loop``). There is nothing to compact, so only the
estimation half of tau's module is ported here. This module's centerpiece,
``StepFootprint``, is the paper's headline property made measurable: the
footprint of step ``t`` does not depend on ``t``, so cumulative prompt tokens
over ``T`` steps grow as ``O(T)`` rather than ``O(T^2)``. See
``tests/test_rio_coding_footprint.py`` for a runtime check of that property.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from rio.ai.messages import (
    AgentMessage,
    AssistantMessage,
    ThinkingContent,
    ToolResultMessage,
    message_text,
)
from rio.ai.tools import AgentTool
from rio.ai.types import JSONObject

CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD_TOKENS = 4
TOOL_OVERHEAD_TOKENS = 16
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000


def estimate_text_tokens(text: str) -> int:
    """Return a deterministic rough token estimate for text."""
    if not text:
        return 0
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def estimate_message_tokens(message: AgentMessage) -> int:
    """Return a rough token estimate for one provider-neutral message."""
    tokens = MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(message_text(message))
    if isinstance(message, AssistantMessage):
        tokens += sum(
            estimate_text_tokens(block.thinking)
            for block in message.content
            if isinstance(block, ThinkingContent)
        )
        tokens += sum(
            estimate_text_tokens(call.name) + estimate_text_tokens(str(call.arguments))
            for call in message.tool_calls
        )
    elif isinstance(message, ToolResultMessage):
        tokens += estimate_text_tokens(message.tool_name)
    return tokens


def estimate_tool_tokens(tool: AgentTool) -> int:
    """Return a rough token estimate for one tool definition."""
    return (
        TOOL_OVERHEAD_TOKENS
        + estimate_text_tokens(tool.name)
        + estimate_text_tokens(tool.description)
        + estimate_text_tokens(str(tool.input_schema))
    )


@dataclass(frozen=True, slots=True)
class StepFootprint:
    """Token cost of one SKILL.state step's prompt: instructions + state + observation.

    Each field is the estimated size of one part of the fixed three-piece
    prompt (see module docstring). None of them depend on how many steps have
    already run, which is exactly the property that keeps a SKILL.state run's
    total prompt cost linear in the number of steps.
    """

    instructions_tokens: int
    state_tokens: int
    observation_tokens: int
    tools_tokens: int

    @property
    def total_tokens(self) -> int:
        """Return the full estimated prompt size for this step."""
        return (
            self.instructions_tokens
            + self.state_tokens
            + self.observation_tokens
            + self.tools_tokens
        )


def estimate_step_footprint(
    *,
    instructions: str,
    state: JSONObject,
    observation: str,
    tools: Sequence[AgentTool] = (),
) -> StepFootprint:
    """Return the estimated token footprint of one SKILL.state step's prompt.

    Mirrors what ``rio.agent.prompt.build_step_messages`` actually sends to
    the model: the skill instructions as the system prompt, the state
    serialized as pretty-printed JSON, and the latest observation appended
    below it. ``tools`` should normally be just the single
    ``skill_step_tool(skill)`` definition, since that is the only tool the
    SKILL.state loop ever offers the model per step.
    """
    state_block = (
        "Skill Execution State:\n```json\n" + json.dumps(state, indent=2, sort_keys=True) + "\n```"
    )
    observation_block = "Latest Observation:\n" + observation
    return StepFootprint(
        instructions_tokens=estimate_text_tokens(instructions),
        state_tokens=estimate_text_tokens(state_block),
        observation_tokens=MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(observation_block),
        tools_tokens=sum(estimate_tool_tokens(tool) for tool in tools),
    )


def context_window_utilization(footprint: StepFootprint, context_window_tokens: int) -> float:
    """Return the fraction of the model's context window one step occupies.

    This is constant across a run: unlike a growing transcript, a
    SKILL.state step's prompt size does not depend on which step it is.
    """
    if context_window_tokens <= 0:
        raise ValueError("context_window_tokens must be positive")
    return footprint.total_tokens / context_window_tokens


def projected_cumulative_tokens(footprint: StepFootprint, steps: int) -> int:
    """Return total prompt tokens after ``steps`` steps.

    Linear in ``steps``, because no step's prompt includes any earlier
    step's prompt or observation -- that is the whole point of SKILL.state.
    """
    if steps < 0:
        raise ValueError("steps must be non-negative")
    return footprint.total_tokens * steps
