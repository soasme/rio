"""Provider-neutral tool definitions; callers own execution."""

from collections.abc import Mapping
from dataclasses import dataclass

from rio.ai.messages import ToolCall  # noqa: F401
from rio.ai.types import JSONValue


@dataclass(frozen=True, slots=True)
class AgentTool:
    name: str
    description: str
    parameters: Mapping[str, JSONValue]
