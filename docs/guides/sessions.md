# Resume a session

Each `rio run` creates a SQLite Session and prints its ID. Resume interrupted work:

```sh
rio run --resume SESSION_ID
```

The goal, working directory, limits, and provider/model configuration stay fixed.
A resume rebuilds committed State, stops surviving local workers, retains finished
results, and reports interrupted executions as `UnknownExecution`. It never
reruns an authorized Cell automatically. The model inspects current facts and
chooses subsequent work. A terminal Session remains terminal; another attempt
requires a new `rio run`.

`--cwd`, when provided, must match the original directory. Task text may be omitted
on resume; a different task is rejected. Saved files and package environments are
not restored. See [Durable Sessions](../durable.md) for controls and limits.

## Legacy notebook sessions

JSONL notebook sessions keep their earlier continuation behavior:

```sh
rio run --runtime notebook --resume SESSION_ID "Now add regression tests."
```

This adds a new user Cell to the reconstructed notebook and allows provider/model
overrides. Notebook IDs and SQLite Session IDs use separate storage. To archive
legacy material in SQLite without executing it, use `Session.import_legacy(path)`.
