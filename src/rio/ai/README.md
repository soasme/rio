# rio.ai

`rio.ai` sends one model request and returns one final assistant message.
Callers supply user text and tool definitions for each request. The package
works independently of the durable session runtime.

## Provider contract

`await ModelProvider.complete(model=..., system=..., messages=..., tools=...)`
returns an `AssistantMessage`. `messages` are `UserMessage`s of plain text: Rio
sends each turn as fresh State, never a transcript, so providers serialize no
assistant history, tool results, images or replay signatures. A provider failure
is a message with `stop_reason == "error"`, an `error_message` and any partial
content. Transport streaming stays internal: fragments are collected without
copying the growing message. Normal task cancellation still stops an in-flight
request.

Providers describe tool calls; the caller decides how to execute them. Rio's
agent runtime supplies the `step` tool and owns its acceptance protocol.
Close network providers with `await provider.aclose()` when finished.

## Modules

| Module | Responsibility |
| --- | --- |
| [provider.py](provider.py) | Provider protocol and response collection. |
| [messages.py](messages.py) | Shared message, content and usage types. |
| [openai_compatible.py](openai_compatible.py) | OpenAI-compatible chat completions and Responses requests. |
| [anthropic.py](anthropic.py), [google.py](google.py), [mistral.py](mistral.py), [openai_codex.py](openai_codex.py) | Provider-specific requests and stream parsing. |
| [http.py](http.py), [retry.py](retry.py) | HTTP transport and retry support. |
| [fake.py](fake.py) | Scripted responses for offline examples and tests. |

Provider selection, catalog metadata and saved credentials belong to
[rio.coding](../coding/README.md). Durable execution belongs to
[rio.agent](../agent/README.md). See the
[offline session example](../coding/data/examples/offline_session.py) for
`FakeProvider` usage.

Tool definitions contain only `name`, `description`, and `parameters`; schemas are
sent as supplied. Codex reuses the Responses parser while keeping subscription
authentication and its retry rules.
