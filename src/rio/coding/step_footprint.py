"""Per-step token-footprint estimation for CLM runs.

Each step's prompt is the fixed skill instructions, the tool definitions, and
the context the model manages. Only the context varies, and the model keeps it
under its limit by editing its context file.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rio.agent import render_context
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
    """Token cost of one step's prompt: instructions + context + tools."""

    instructions_tokens: int
    context_tokens: int
    tools_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.instructions_tokens + self.context_tokens + self.tools_tokens


def estimate_step_footprint(
    *,
    instructions: str,
    context: Sequence[JSONObject],
    tools: Sequence[AgentTool] = (),
) -> StepFootprint:
    """Return the estimated token footprint of one step's prompt."""
    return StepFootprint(
        instructions_tokens=estimate_text_tokens(instructions),
        context_tokens=estimate_text_tokens(render_context(context)) if context else 0,
        tools_tokens=sum(estimate_tool_tokens(tool) for tool in tools),
    )


def context_window_utilization(footprint: StepFootprint, context_window_tokens: int) -> float:
    """Return the fraction of the model's context window one step occupies."""
    if context_window_tokens <= 0:
        raise ValueError("context_window_tokens must be positive")
    return footprint.total_tokens / context_window_tokens
