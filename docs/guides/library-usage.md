# Use Rio as a library

Install with `uv add rio`. Register the coding cell kinds when creating or
opening a coding session. A completed session can replay before provider setup:

```python
import asyncio
from pathlib import Path

from rio.agent.runner import replay, run
from rio.agent.session import Session
from rio.agent.store import Store
from rio.coding import cells
from rio.coding.owner import ProcessOwner
from rio.coding.provider_config import load_provider_settings, resolve_provider_selection
from rio.coding.provider_runtime import create_model_provider


async def main() -> None:
    with Store(Path("task.sqlite3")) as store:
        session = (
            Session(store, cells.KINDS)
            if store.data
            else Session.create(
                store,
                "Add a health-check endpoint",
                kinds=cells.KINDS,
                owner_config={"cwd": str(Path.cwd())},
                instructions=cells.INSTRUCTIONS,
            )
        )
        if session.meta.terminal:
            succeeded = await replay(ProcessOwner(session), print)
        else:
            selection = resolve_provider_selection(load_provider_settings())
            provider = create_model_provider(selection.provider, model=selection.model)
            try:
                succeeded = await run(ProcessOwner(session), provider, selection.model, print)
            finally:
                await provider.aclose()
        print("success:", succeeded)


asyncio.run(main())
```

The callback receives committed `JournalEvent` Pydantic models. Access fields with
`event.seq`, `event.type`, and `event.changes`. Sessions expose models through
`session.state`, `session.meta`, and `session.data`; for example, use
`turn.id` and `session.data.executions["1"].phase`.

Use `model_dump(mode="json", by_alias=True, exclude_unset=True)` for the journal
representation. `State.schema_version` serializes as `schema`, and
`Meta.provider_model_config` serializes as `model_config`. The journal format is
unchanged; `Store.data` and event changes remain JSON dictionaries.

Opening the same database
rebuilds state without executing code. `run` recovers interrupted workers before
continuing; `replay` cleans up recorded workers and publishes a terminal session.
Cleanup failures stop replay.

This example selects a provider from current settings for each active run.
For the CLI's saved provider selection and project instructions, call
`rio.cli.run.run_persistent_session` with task text and keyword options such as
`cwd`, `provider_name`, `model`, and `resume`.

Without `kinds`, a session supports notes and controls only. Custom applications supply
`CellKind` definitions; see [rio.agent](../../src/rio/agent/README.md).
Pass `Limits` and `validators` to `Session.create` to set execution budgets and
required checks. `ProcessOwner` runs validators as argument lists, such as
`[["python", "-m", "pytest"]]`, and cells and validators in `owner_config["cwd"]`.
Use `Session.receive(message_id, kind, payload)` for external observations and
`Session.create(external_wakeup=True)` to allow waiting for external work.
See [Architecture](../explanation/architecture.md) for recovery rules and limits.
