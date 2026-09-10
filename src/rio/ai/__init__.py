"""Provider and Pi-compatible model streaming layer for Rio.

Ported from `huggingface/tau <https://github.com/huggingface/tau>`_'s
``tau_ai`` module (MIT licensed). The vocabulary types that ``tau_ai``
depends on (messages, tools, wire-level types, the ``ModelProvider``
contract, and the canonical assistant stream event union) lived in
``tau_agent`` upstream; they are folded into this package so ``rio.ai`` is
self-contained and installable independent of the ``rio.agent`` runtime.
See ``NOTICE`` at the repository root for the original license text.
"""

# ruff: noqa: F401 - this module intentionally defines the public facade

from rio.ai.anthropic import AnthropicProvider
from rio.ai.env import (
    DEFAULT_ANTHROPIC_BASE_URL,
    DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES,
    DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS,
    DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS,
    AnthropicConfig,
    OpenAICompatibleConfig,
    RuntimeProviderAuth,
    openai_compatible_config_from_env,
)
from rio.ai.events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantMessageEvent,
    AssistantStartEvent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from rio.ai.fake import FakeProvider
from rio.ai.google import GoogleGenerativeAIProvider
from rio.ai.messages import (
    AgentMessage,
    AssistantMessage,
    BashExecutionMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
    CustomMessage,
    ImageContent,
    ResponseTiming,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
    content_text,
    malformed_tool_arguments,
    message_text,
    raw_tool_arguments,
)
from rio.ai.mistral import MistralConversationsProvider
from rio.ai.model_limits import ModelLimitsProvider, RuntimeModelLimits
from rio.ai.openai_codex import (
    DEFAULT_OPENAI_CODEX_BASE_URL,
    OpenAICodexConfig,
    OpenAICodexCredentials,
    OpenAICodexProvider,
)
from rio.ai.openai_compatible import OpenAICompatibleProvider
from rio.ai.provider import CancellationToken, ModelProvider
from rio.ai.tools import (
    AgentTool,
    AgentToolResult,
    ToolCancellationToken,
    ToolExecutionMode,
    ToolExecutor,
    ToolUpdateCallback,
)
from rio.ai.types import JSONObject, JSONPrimitive, JSONValue

__all__ = [name for name in globals() if not name.startswith("_")]
