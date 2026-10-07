# Lean behavior models

Run `lake build` in this directory. CI builds all targets.

| Model | Python behavior |
| --- | --- |
| `RioAgent.lean` | Immutable cell versions, cell index hints, predecessor admission, runnable cell kinds, duplicate step acceptance, recovery without replay, pause consumption, terminal dispatch, success guards, timer delivery, AGENTS.md resolution (`rio.agent`, `rio.cli.run`) |
| `RioCli.lean` | Committed content without cell IDs and cumulative output without duplication (`rio.coding.rendering`) |
| `RioCodingMcp.lean` | Trusted MCP configuration merging and OAuth token routing (`rio.coding.mcp`) |

These are independent behavior models, not proofs extracted from Python.
They do not prove SQLite synchronization, OS process cleanup, transport behavior,
or hardware durability. Python tests cover the implementation and its failure paths.
