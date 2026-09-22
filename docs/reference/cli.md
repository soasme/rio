# Command-line interface

```text
rio login PROVIDER [--method METHOD]
rio run [OPTIONS] TASK
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
| `-e`, `--extension PATH` | Load an extension path. Repeatable. |
| `-a`, `--approve` | Trust project resources. |
| `--no-approve` | Do not trust project resources. |
| `-r`, `--resume SESSION_ID` | Resume a durable session. |
| `--output human\|json` | Output format; defaults to `human`. |

`human` prints a compact transcript and session ID. `json` emits one JSON
event per line and is intended for programmatic consumers.
