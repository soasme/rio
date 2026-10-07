# Use Rio as a library

Install with `uv add rio`. Create a session, then run it with a provider:

```python
import asyncio
from pathlib import Path

from rio.agent import Session, Store
from rio.agent.runner import run
from rio.coding.provider_config import load_provider_settings, resolve_provider_selection
from rio.coding.provider_runtime import create_model_provider

async def main():
    selection = resolve_provider_selection(load_provider_settings())
    provider = create_model_provider(selection.provider, model=selection.model)
    try:
        with Store(Path("task.sqlite3")) as store:
            session = (
                Session(store) if store.data else
                Session.create(store, "Add a health-check endpoint", Path.cwd())
            )
            succeeded = await run(session, provider, selection.model, print)
            print("success:", succeeded)
    finally:
        await provider.aclose()

asyncio.run(main())
```

The callback receives committed event dictionaries. Opening the same database
rebuilds state without executing code. `run` recovers interrupted workers before
continuing. A terminal session stays terminal.

Pass `Limits` and `validators` to `Session.create` to set execution budgets and
required checks. Validators are argument lists, such as `[["python", "-m", "pytest"]]`.
Use `Session.receive(message_id, kind, payload)` for external observations.
See [Architecture](../explanation/architecture.md) for recovery rules and limits.
