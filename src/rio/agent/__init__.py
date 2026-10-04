"""A small JSON state and JSON Patch agent core."""

from rio.agent.errors import ProviderResponseError, RetriesExhaustedError
from rio.agent.loop import AgentStep, run_json_loop
from rio.agent.prompt import DEFAULT_SYSTEM_PROMPT, STEP_TOOL_NAME, build_messages, skill_step_tool
from rio.agent.state import PatchError, apply_patch, state_tokens

__all__ = [
    "AgentStep",
    "DEFAULT_SYSTEM_PROMPT",
    "PatchError",
    "ProviderResponseError",
    "RetriesExhaustedError",
    "STEP_TOOL_NAME",
    "apply_patch",
    "build_messages",
    "run_json_loop",
    "skill_step_tool",
    "state_tokens",
]
