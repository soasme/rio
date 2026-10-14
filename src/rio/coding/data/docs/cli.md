# Command-line interface

```text
rio login PROVIDER [--method METHOD]
rio run [OPTIONS] TASK
rio version
```

A subcommand is required. Bare `rio` prints usage to stderr and exits with status 2.
Use `rio --help` to print help and exit successfully.

## `rio login`

Saves credentials for a provider.

| Argument | Description |
| --- | --- |
| `PROVIDER` | Provider name. |
| `--method METHOD` | Authentication method, if applicable. |

## `rio run`

Executes a task in a durable session. A sole Markdown file is read as task
text; otherwise the positional arguments are joined into the task.
Providers come from the configured catalog; each run selects one provider and model.

| Option | Description |
| --- | --- |
| `--provider NAME` | Provider to use. |
| `-m`, `--model MODEL` | Model to use. |
| `--cwd PATH` | Project directory. |
| `-t`, `--thinking LEVEL` | Reasoning-effort level. |
| `--agents-md PATH` | Project instructions file. Defaults to `AGENTS.md` in the working directory when present. |
| `-r`, `--resume SESSION_ID` | Resume a durable session. |
| `--output human\|json` | Output format; defaults to `human`. |

Omit TASK when resuming. Resume keeps the original goal, project instructions,
and model configuration. An unfinished session needs its configured provider and
project directory. A completed session cleans up recorded workers and replays its
journal without provider setup or the original directory. Cleanup failures stop
replay. See [sessions](https://github.com/soasme/rio/blob/main/docs/guides/sessions.md).

`human` prints accepted content (notes, commands, Python source, and control reasons), execution output and
outcomes, and the session ID. Cell labels and numbers are not displayed. Resume replays the committed
transcript before showing new progress. See the [message design](https://github.com/soasme/rio/blob/main/docs/design/coding-tui-messages.md).
`json` emits one JSON event per line and is intended for programmatic consumers.

`rio run` reports its status to the terminal with
[OSC 7501](https://mitchellh.com/writing/program-status-osc7501) on stderr:
`working` while it runs, then `done`, `error`, or `idle` on interrupt. It reports
when stderr is a terminal and `TERM` is not `dumb`. Set `RIO_PROGRAM_STATUS=1` or
`RIO_PROGRAM_STATUS=0` to force reporting on or off.

## `rio version`

Prints the installed version.
