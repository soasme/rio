# Use Rio as a library

Rio exposes a simple Python API for running coding tasks programmatically in your own applications.

## Install Rio

```bash
uv add rio
```

## Basic usage

Run a coding task with `CodingSession`:

```python
import asyncio
from pathlib import Path
from rio.coding import CodingSession, CodingSessionConfig
from rio.coding.provider_config import resolve_provider_selection, load_provider_settings
from rio.coding.provider_runtime import create_model_provider

async def run_task():
    # Load provider configuration
    settings = load_provider_settings()
    selection = resolve_provider_selection(settings)
    
    # Create a provider
    provider = create_model_provider(selection.provider, model=selection.model)
    
    try:
        # Create a session
        session = await CodingSession.load(
            CodingSessionConfig(
                provider=provider,
                model=selection.model,
                cwd=Path.cwd(),
            )
        )
        
        # Run a task
        async for event in session.run("Add a simple health-check endpoint"):
            print(event)
            
    finally:
        await session.aclose()
        await provider.aclose()

# Run the task
asyncio.run(run_task())
```

## Configure provider and model

Specify a provider and model explicitly:

```python
from rio.coding.provider_config import resolve_provider_selection, load_provider_settings

settings = load_provider_settings()
selection = resolve_provider_selection(
    settings, 
    provider_name="anthropic", 
    model="claude-opus-4-1"
)
```

## Handle events

Subscribe to session events with a callback:

```python
from rio.coding import CodingSession, CodingSessionConfig
from rio.coding.rendering import create_event_renderer, PrintOutputMode

async def run_task_with_events():
    session = await CodingSession.load(config)
    
    # Create a renderer to process events
    renderer = create_event_renderer(PrintOutputMode.json)
    
    async for event in session.run(prompt):
        # Handle the event
        renderer.render(event)
        # Or process it yourself
        print(f"Event type: {type(event).__name__}")
    
    # Check if the task succeeded
    success = renderer.finish()
    await session.aclose()
```

## Resume a session

Continue work from a previous session:

```python
from rio.coding.session_store import JsonlSessionStorage

# Store the session on disk
storage = JsonlSessionStorage(Path(".rio/sessions/task.jsonl"))

# Later, resume the session
session = await CodingSession.load(
    CodingSessionConfig(
        provider=provider,
        model=selection.model,
        cwd=Path.cwd(),
        storage=storage,
    )
)

# Continue with a follow-up task
async for event in session.run("Run the test suite"):
    renderer.render(event)
```

## Session storage backends

Pass a storage instance as `CodingSessionConfig.storage`. The default is
`JsonlSessionStorage`, which writes a local JSONL file. SQLite stores the same
ordered journal in a local database file. In-memory storage keeps it only for
the life of the storage instance, making it useful for tests and single-process
applications.

```python
from rio.coding.session_store import (
    InMemorySessionStorage,
    JsonlSessionStorage,
    SqliteSessionStorage,
)

storage = JsonlSessionStorage(Path("sessions.jsonl"))  # default format
storage = SqliteSessionStorage(Path("sessions.db"))
storage = InMemorySessionStorage()

config = CodingSessionConfig(
    provider=provider,
    model=selection.model,
    cwd=Path.cwd(),
    storage=storage,
)
```

Reuse the same in-memory instance to continue a session. For SQLite or JSONL,
create another storage instance with the same path to resume after a restart.
