# rio.ai

`rio.ai` sends model requests and turns provider responses into a common stream
of assistant events. Callers supply messages and tool definitions for each
request. The package works independently of the durable session runtime.

## Provider contract

`ModelProvider.stream_response` accepts `model`, `system`, `messages`, and
`tools`, and returns an async iterator with one final `AssistantMessageEvent`.
`AssistantDoneEvent` carries the completed message and usage;
`AssistantErrorEvent` carries an error message and any partial content.
Transport streaming stays internal: fragments are collected without copying
the growing message. Normal task cancellation still stops an in-flight request.

Providers describe tool calls; the caller decides how to execute them. Rio's
agent runtime supplies the `step` tool and owns its acceptance protocol.
Close network providers with `await provider.aclose()` when finished.

## Modules

| Module | Responsibility |
| --- | --- |
| [provider.py](provider.py) | Provider protocol, final events, and response collection. |
| [messages.py](messages.py) | Shared message, content and usage types. |
| [openai_compatible.py](openai_compatible.py) | OpenAI-compatible chat completions and Responses requests. |
| [anthropic.py](anthropic.py), [google.py](google.py), [mistral.py](mistral.py), [openai_codex.py](openai_codex.py) | Provider-specific requests and stream parsing. |
| [http.py](http.py), [retry.py](retry.py) | HTTP transport and retry support. |
| [fake.py](fake.py) | Deterministic event streams for offline examples and tests. |

Provider selection, catalog metadata and saved credentials belong to
[rio.coding](../coding/README.md). Durable execution belongs to
[rio.agent](../agent/README.md). See the
[offline session example](../coding/data/examples/offline_session.py) for
`FakeProvider` usage.

The former partial-event classes, cancellation/session parameters, constrained
sampling, unused transcript variants, and tool execution/rendering fields have
been removed. Tool definitions contain only `name`, `description`, and `parameters`;
schemas are sent as supplied. Codex reuses the Responses parser and input conversion
while preserving subscription authentication, replay IDs, and retry rules.
