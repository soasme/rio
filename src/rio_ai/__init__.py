"""Provider and Pi-compatible model streaming layer for Rio.

Ported from `huggingface/tau <https://github.com/huggingface/tau>`_'s
``tau_ai`` module (MIT licensed). The vocabulary types that ``tau_ai``
depends on (messages, tools, wire-level types, the ``ModelProvider``
contract, and the canonical assistant stream event union) lived in
``tau_agent`` upstream; they are folded into this package so ``rio_ai`` is
self-contained and installable independent of the ``rio_agent`` runtime.
See ``NOTICE`` at the repository root for the original license text.
"""

# ruff: noqa: F401 - this module intentionally defines the public facade

from rio_ai.anthropic import AnthropicProvider
from rio_ai.env import (
    DEFAULT_ANTHROPIC_BASE_URL,
    DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES,
    DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS,
    DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS,
    AnthropicConfig,
    OpenAICompatibleConfig,
    RuntimeProviderAuth,
    openai_compatible_config_from_env,
)
from rio_ai.events import (
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
from rio_ai.fake import FakeProvider
from rio_ai.google import GoogleGenerativeAIProvider
from rio_ai.messages import (
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
    message_text,
)
from rio_ai.mistral import MistralConversationsProvider
from rio_ai.model_limits import ModelLimitsProvider, RuntimeModelLimits
from rio_ai.openai_codex import (
    DEFAULT_OPENAI_CODEX_BASE_URL,
    OpenAICodexConfig,
    OpenAICodexCredentials,
    OpenAICodexProvider,
)
from rio_ai.openai_compatible import OpenAICompatibleProvider
from rio_ai.provider import CancellationToken, ModelProvider
from rio_ai.tools import (
    AgentTool,
    AgentToolResult,
    ToolCancellationToken,
    ToolExecutionMode,
    ToolExecutor,
    ToolUpdateCallback,
)
from rio_ai.types import JSONObject, JSONPrimitive, JSONValue

__all__ = [name for name in globals() if not name.startswith("_")]
