# rio.cli

`rio.cli` is the command-line application for one-shot coding tasks. It joins
provider configuration, coding cells and the durable session runtime.

```bash
rio login anthropic
rio run --provider anthropic "Fix the failing tests."
rio run --resume SESSION_ID
rio version
```

## Run and resume

[run.py](run.py) exposes `run_persistent_session(prompt, *, ...)`. Options are
keyword arguments, including `cwd`, `provider_name`, `model`, `thinking_level`,
`resume`, `output_mode` and `agents_md`. It returns `(succeeded, session_id)`.

A new run reads project instructions, resolves a catalog provider, and creates a
SQLite session under `~/.rio/sessions/`. An active resume uses the recorded goal,
provider, model and instructions. A completed session cleans up recorded workers
and replays its journal without provider setup or the original project directory.
Cleanup failures stop replay. Start a new session for a new task.

Human output shows committed content, execution results and the session ID.
JSON output emits one journal event per line, including `session_id`.

## Design principles

Follow these when changing or reviewing this package.

- **Thin by design.** The CLI parses arguments, wires `rio.coding` and
  `rio.agent` together, and renders events. Session, provider and execution
  logic belongs in those packages, where library callers can use it too.
- **Render the journal, never a side channel.** Human and JSON output come only
  from committed events, so a replay prints what the live run printed.
- **Resume uses what was recorded.** A resumed session takes its goal,
  provider, model and instructions from the journal, not from current flags or
  configuration.
- **Nothing imports `rio.cli`.** It is the top of the dependency graph.

## Modules

| Module | Responsibility |
| --- | --- |
| [__init__.py](__init__.py) | Parse commands, resolve task text or Markdown files, dispatch handlers and set exit status. |
| [run.py](run.py) | Create or resume sessions, construct providers and publish results. |
| [render.py](render.py) | Render committed events as a human transcript. |
| [status.py](status.py) | Report terminal program status through OSC 7501. |

The installed `rio` entry point calls `main()`. Python callers can use
`app(argv)` for command dispatch or `run_persistent_session` for an async run.
See the [CLI reference](../../../docs/reference/cli.md) for all commands and
options, and [sessions](../../../docs/guides/sessions.md) for recovery behavior.
