"""Model providers for Rio: one request in, one final assistant message out.

Ported from `huggingface/tau <https://github.com/huggingface/tau>`_'s
``tau_ai`` module (MIT licensed) and narrowed to Rio's stateless turns: each
request is fresh user text, never a replayed transcript. ``rio.ai`` is
installable independent of the ``rio.agent`` runtime. See ``NOTICE`` at the
repository root for the original license text.
"""

# ruff: noqa: F401 - this module intentionally defines the public facade

from rio.ai._provider_events import ProviderEvent, ProviderRetryEvent, ProviderToolCallDeltaEvent
from rio.ai.anthropic import AnthropicProvider
from rio.ai.env import (
    DEFAULT_ANTHROPIC_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_DELAY_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    AnthropicConfig,
    HTTPConfig,
    OpenAICompatibleConfig,
    RuntimeProviderAuth,
)
from rio.ai.fake import FakeProvider
from rio.ai.google import GoogleGenerativeAIConfig, GoogleGenerativeAIProvider
from rio.ai.messages import (
    AssistantMessage,
    ResponseTiming,
    TextContent,
    ThinkingContent,
    ToolCall,
    Usage,
    UsageCost,
    UserMessage,
    malformed_tool_arguments,
    raw_tool_arguments,
)
from rio.ai.mistral import MistralConfig, MistralConversationsProvider
from rio.ai.openai_codex import (
    DEFAULT_OPENAI_CODEX_BASE_URL,
    OpenAICodexConfig,
    OpenAICodexCredentials,
    OpenAICodexProvider,
)
from rio.ai.openai_compatible import OpenAICompatibleProvider
from rio.ai.provider import ModelProvider
from rio.ai.tools import AgentTool
from rio.ai.types import JSONObject, JSONPrimitive, JSONValue

__all__ = [name for name in globals() if not name.startswith("_")]
