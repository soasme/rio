"""Provider-neutral request and response message models."""

from __future__ import annotations

from collections.abc import Mapping
from time import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rio.ai.types import JSONValue


def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(part.title() for part in parts[1:])


def current_timestamp_ms() -> int:
    """Return the current Unix timestamp in milliseconds."""
    return int(time() * 1000)


class WireModel(BaseModel):
    """Strict model with Python field names and Pi-compatible JSON aliases."""

    model_config = ConfigDict(
        extra="forbid",
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        alias_generator=_to_camel,
    )


class UsageCost(WireModel):
    """Billed response cost in USD."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    total: float = 0.0


class Usage(WireModel):
    """Provider-reported token usage for one assistant response."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_write_1h: int | None = None
    reasoning: int | None = None
    total_tokens: int = 0
    cost: UsageCost = UsageCost()


class ResponseTiming(WireModel):
    """Monotonic request durations for one assistant response."""

    time_to_first_output_ms: int | None = Field(default=None, ge=0)
    total_duration_ms: int = Field(ge=0)


class TextContent(WireModel):
    type: Literal["text"] = "text"
    text: str


class ThinkingContent(WireModel):
    type: Literal["thinking"] = "thinking"
    thinking: str


class ToolCall(WireModel):
    """A tool call content block requested by the assistant."""

    type: Literal["toolCall"] = "toolCall"
    id: str
    name: str
    arguments: dict[str, JSONValue] = Field(default_factory=dict)


RAW_TOOL_ARGUMENTS_KEY = "_raw_arguments"


def malformed_tool_arguments(arguments_text: str) -> dict[str, JSONValue]:
    """Carry a tool call's unparsed argument text in place of its arguments.

    A model that is cut off at the output token limit mid-call leaves argument
    JSON that never closes. Every provider parser lands here, so the text is
    kept under one key callers can recognize rather than being dropped for an
    empty mapping that looks like a call with no arguments.
    """
    return {RAW_TOOL_ARGUMENTS_KEY: arguments_text}


def raw_tool_arguments(arguments: Mapping[str, JSONValue]) -> str | None:
    """Return a tool call's unparsed argument text, or ``None`` if it parsed."""
    if len(arguments) != 1:
        return None
    raw = arguments.get(RAW_TOOL_ARGUMENTS_KEY)
    return raw if isinstance(raw, str) else None


type AssistantContent = TextContent | ThinkingContent | ToolCall


class UserMessage(WireModel):
    """One request message. Rio sends each turn as fresh user text, never a transcript."""

    role: Literal["user"] = "user"
    content: str
    timestamp: int = Field(default_factory=current_timestamp_ms)


class AssistantDiagnosticError(WireModel):
    name: str | None = None
    message: str
    stack: str | None = None
    code: str | int | None = None


class AssistantMessageDiagnostic(WireModel):
    type: str
    timestamp: int = Field(default_factory=current_timestamp_ms)
    error: AssistantDiagnosticError | None = None
    details: dict[str, JSONValue] | None = None


StopReason = Literal["stop", "length", "toolUse", "error"]


class AssistantMessage(WireModel):
    """An assistant response with ordered content blocks."""

    role: Literal["assistant"] = "assistant"
    content: list[AssistantContent] = Field(default_factory=list)
    api: str = "unknown"
    provider: str = "unknown"
    model: str = "unknown"
    response_model: str | None = None
    response_provider: str | None = None
    response_id: str | None = None
    diagnostics: list[AssistantMessageDiagnostic] | None = None
    usage: Usage = Usage()
    timing: ResponseTiming | None = None
    stop_reason: StopReason = "stop"
    error_message: str | None = None
    timestamp: int = Field(default_factory=current_timestamp_ms)

    @model_validator(mode="before")
    @classmethod
    def _normalize_convenient_content(cls, value: object) -> object:
        """Accept a string only as a Python construction convenience.

        The stored model and serialized protocol are always block based. This
        keeps provider and test construction concise without creating a second
        message representation.
        """
        if not isinstance(value, dict):
            return value
        data = dict(value)
        content = data.get("content")
        if isinstance(content, str):
            data["content"] = [TextContent(text=content)] if content else []
        usage = data.get("usage")
        if usage is None:
            data["usage"] = Usage()
        return data

    @property
    def text(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextContent))

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(block for block in self.content if isinstance(block, ToolCall))


def assistant_content(
    text: str,
    tool_calls: list[ToolCall] | tuple[ToolCall, ...] = (),
) -> list[AssistantContent]:
    """Build canonical ordered assistant blocks from parser accumulators."""
    blocks: list[AssistantContent] = [TextContent(text=text)] if text else []
    blocks.extend(tool_calls)
    return blocks
