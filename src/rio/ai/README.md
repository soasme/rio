# rio.ai

`rio.ai` sends model requests and turns provider responses into a common stream
of assistant events. Callers supply messages and tool definitions for each
request. The package works independently of the durable session runtime.

## Provider contract

`ModelProvider.stream_response` accepts `model`, `system`, `messages`, and
`tools`, with optional cancellation and session identity. It returns an async
iterator of `AssistantMessageEvent` values. Text, thinking and tool-call events
carry partial content; `AssistantDoneEvent` carries the completed message and
usage, while `AssistantErrorEvent` carries an error message.

Providers describe tool calls; the caller decides how to execute them. Rio's
agent runtime supplies the `step` tool and owns its acceptance protocol.
Close network providers with `await provider.aclose()` when finished.

## Modules

| Module | Responsibility |
| --- | --- |
| [provider.py](provider.py) | Provider and cancellation protocols. |
| [messages.py](messages.py), [events.py](events.py) | Shared message, content, usage and event types. |
| [openai_compatible.py](openai_compatible.py) | OpenAI-compatible chat completions and Responses requests. |
| [anthropic.py](anthropic.py), [google.py](google.py), [mistral.py](mistral.py), [openai_codex.py](openai_codex.py) | Provider-specific requests and stream parsing. |
| [stream.py](stream.py) | Translate parsed responses into assistant events. |
| [http.py](http.py), [retry.py](retry.py) | HTTP transport and retry support. |
| [fake.py](fake.py) | Deterministic event streams for offline examples and tests. |

Provider selection, catalog metadata and saved credentials belong to
[rio.coding](../coding/README.md). Durable execution belongs to
[rio.agent](../agent/README.md). See the
[offline session example](../coding/data/examples/offline_session.py) for
`FakeProvider` usage.
