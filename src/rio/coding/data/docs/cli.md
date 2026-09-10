# rio CLI and RPC

rio drives one `rio.coding.session.CodingSession` from a small number of frontends: an interactive TUI, a one-shot print mode, and a JSONL RPC mode for editor/tool integrations. The CLI entry point is `rio.coding.cli:app` (script name `rio`).

## Commands

```bash
rio providers
rio login anthropic --method api-key
rio logout anthropic
rio -p --provider anthropic --model MODEL "Explain this project"
rio -p --mode json "Run the relevant tests"
rio --session SESSION_ID
rio sessions
rio export SESSION_ID --format html
rio install /path/to/extension
rio update --models
```

`rio setup --provider NAME --base-url URL --model MODEL` saves an
OpenAI-compatible provider. `--thinking LEVEL` selects reasoning effort.
`--system-prompt` and repeated `--append-system-prompt` accept literal text or
an existing UTF-8 file path. `--approve` allows ambient project resources for
the run; `--no-approve` disables them.

## RPC mode

`rio.coding.rpc.RpcServer` reads newline-delimited JSON commands from stdin and writes newline-delimited JSON responses/events to stdout -- see `architecture.md` for why the events it streams describe SKILL.state steps rather than a conversation.

Every command is a JSON object with a `type` and, for correlated commands, an `id` that is echoed back on the response. `prompt`, `steer`, `follow_up`, and `continue` start or steer a run and stream the run's events (a `RunStartEvent`, one `StepStartEvent`/`StateUpdateEvent`/`ActionStartEvent`/`ActionEndEvent`/`StepEndEvent` cycle per step, then a `RunEndEvent` and the session-level `run_end`/`agent_settled` events) after an initial success/failure response. `get_state` reports model, provider, thinking level, and queued-message counts; `get_execution_state` reports the session's actual memory of the run -- its state object, plan progress, touched files, and answer. `get_entries`, `get_tree`, and `get_checkpoints` read the state journal, and `restore` adopts an earlier checkpoint as the live state. See `rio/coding/rpc.py` for the complete command list.

Input EOF stops accepting commands and lets an accepted run finish. Use `abort`
to cancel it explicitly. Background run failures produce an error response
with the original request id.

## Thinking level

`-t/--thinking LEVEL` sets the initial reasoning-effort level for a run (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`; see `rio.coding.thinking`). An unsupported level for the selected model is an error listing the levels that model supports.

## Safety boundary

Project trust controls ambient project-resource loading; it is not a sandbox. See `security.md`.
