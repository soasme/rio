# Resume a session

Each run prints a session ID. Resume it with:

```sh
rio run --resume SESSION_ID
```

The SQLite journal lives at `~/.rio/sessions/SESSION_ID.sqlite3`. Resume keeps the
original goal, model, limits, cells, and results. Omit the task text. A finished
run stays finished. Start a new run for another task or another attempt.

Interrupted scripts become `UnknownExecution` and are never replayed automatically.
The model must inspect evidence and resolve them. Project files and external
services are not restored. See [Architecture](../explanation/architecture.md).
