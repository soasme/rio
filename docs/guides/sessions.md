# Resume a session

Each `rio run` creates a durable session. At the end of a human-readable run,
Rio prints its ID:

```text
Session: SESSION_ID
```

Continue the same work with that ID:

```bash
rio run --resume SESSION_ID "Now add regression tests."
```

The resumed run uses the original working directory. You may provide
`--provider` or `--model` to override the saved selection; otherwise Rio uses
the session's saved provider and model.

`--cwd` must match the original session directory when used with `--resume`.
