# Command-line interface

```text
rio login PROVIDER [--method METHOD]
rio run [OPTIONS] TASK
rio mcp add|remove|list|login|logout
```

## `rio login`

Saves credentials for a provider.

| Argument | Description |
| --- | --- |
| `PROVIDER` | Provider name. |
| `--method METHOD` | Authentication method, if applicable. |

## `rio run`

Executes a task in a durable session. A sole Markdown file is read as task
text; otherwise the positional arguments are joined into the task.

| Option | Description |
| --- | --- |
| `--provider NAME` | Provider to use. |
| `-m`, `--model MODEL` | Model to use. |
| `--cwd PATH` | Project directory. |
| `-t`, `--thinking LEVEL` | Reasoning-effort level. |
| `--agents-md PATH` | Project instructions file. Defaults to `AGENTS.md` in the working directory when present. |
| `-e`, `--extension PATH` | Provider extension path. Repeatable. |
| `-a`, `--approve` | Allow project provider extensions. |
| `--no-approve` | Do not load project provider extensions. |
| `-r`, `--resume SESSION_ID` | Resume a durable session. |
| `--output human\|json` | Output format; defaults to `human`. |

Omit TASK when resuming a SQLite Session. Resume keeps the original goal, project instructions, and model
configuration; terminal runs remain terminal. Sessions use SQLite only.

`human` prints accepted content (notes, commands, and Python source), execution output and
outcomes, and the session ID. Cell labels and numbers are not displayed. Resume replays the committed
transcript before showing new progress. See the [message design](../design/coding-tui-messages.md).
`json` emits one JSON event per line and is intended for programmatic consumers.

`rio run` reports its status to the terminal with
[OSC 7501](https://mitchellh.com/writing/program-status-osc7501) on stderr:
`working` while it runs, then `done`, `error`, or `idle` on interrupt. It reports
when stderr is a terminal and `TERM` is not `dumb`. Set `RIO_PROGRAM_STATUS=1` or
`RIO_PROGRAM_STATUS=0` to force reporting on or off.

## `rio mcp`

Manages [MCP servers](../guides/mcp.md) without starting a session.

| Command | Description |
| --- | --- |
| `rio mcp add NAME -- COMMAND [ARGS...]` | Add or replace a stdio server. |
| `rio mcp add NAME --url URL` | Add or replace a streamable HTTP server. |
| `rio mcp remove NAME` | Remove a server. |
| `rio mcp list [--json]` | Connect to each server and show its tools. Exits 1 on any failure. |
| `rio mcp login NAME [--timeout SECONDS]` | Sign in to an OAuth server in the browser. |
| `rio mcp logout NAME` | Delete the server's stored OAuth credentials. |

`add` and `remove` change `~/.rio/mcp.json`, or `.rio/mcp.json` with
`-l`/`--local`. `add` also takes `--env KEY=VALUE` and `--cwd DIR` for stdio
servers, `--header KEY=VALUE`, `--oauth-client-id`, `--oauth-client-secret`,
`--oauth-callback-port`, and `--oauth-scope` for HTTP servers, and
`--description TEXT`.
