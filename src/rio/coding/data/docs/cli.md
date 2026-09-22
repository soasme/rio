# rio CLI and RPC

rio drives one `rio.coding.session.CodingSession` from a one-shot print-mode CLI. The CLI entry point is `rio.cli:app` (script name `rio`).

## Commands

```bash
rio login anthropic --method api-key
rio run --provider anthropic --model MODEL "Explain this project"
rio run --resume SESSION_ID "Continue with the next task"
```

`rio login PROVIDER` saves credentials for a configured or built-in provider.
`rio run --thinking LEVEL` selects reasoning effort. `--approve` allows ambient
project resources for the run; `--no-approve` disables them. Each `rio run`
creates a durable session and prints its id. Pass it to `--resume` to load its
state and continue it.

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
