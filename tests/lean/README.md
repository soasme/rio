# Lean behavior models

Run `lake build` in this directory. CI builds all targets.

| Model | Python behavior |
| --- | --- |
| `RioAgent.lean` | Immutable cell versions, id-addressed step ops with runtime-assigned ids and positions, order-free removal, one op per existing Cell, stale-turn rejection, note-only admission and caller-supplied runnable kinds sharing one step schema, output tail truncation, duplicate step acceptance, recovery without replay, pause consumption, batched completion wake-ups, bounded idle decisions, incremental inbox byte budgets, terminal dispatch, success guards, timer delivery, run-loop wake deadlines, AGENTS.md resolution (`rio.agent`, `rio.coding.cells`, `rio.cli.run`) |
| `RioCli.lean` | Committed content without cell IDs and cumulative output without duplication, provider-free terminal replay after cleanup, catalog-only provider resolution, removal of legacy model-cycling preferences, OSC 7501 status override and lifecycle (`rio.coding.rendering`, `rio.cli.status`) |
| `RioCodingMcp.lean` | Saved MCP trust precedence and fail-closed reads, trusted MCP configuration merging and OAuth token routing (`rio.coding.mcp`) |

These are independent behavior models, not proofs extracted from Python.
They do not prove SQLite synchronization, OS process cleanup, transport behavior,
or hardware durability. Python tests cover the implementation and its failure paths.
