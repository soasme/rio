# Lean behavior models

Run `lake build` in this directory. CI builds all targets.

| Model | Python behavior |
| --- | --- |
| `RioAgent.lean` | Immutable cell versions, id-addressed step ops with runtime-assigned ids and positions, order-free removal, one op per existing Cell, stale-turn rejection, note-only admission and caller-supplied runnable kinds sharing one step schema, bounded output tails with fresh output replacing old tails, admission runtime reset preserving allocation, duplicate step acceptance, recovery without replay, pause consumption, batched completion wake-ups, bounded idle decisions, incremental inbox byte budgets, terminal dispatch, success guards, timer delivery, run-loop wake deadlines, AGENTS.md resolution (`rio.agent`, `rio.coding.cells`, `rio.cli.run`) |
| `RioAI.lean` | Final-only provider outcomes, fragment order, retry isolation, missing terminal errors, truncation priority and retry limits (`rio.ai.provider`, `rio.ai.retry`) |
| `RioCli.lean` | Committed content without cell IDs and cumulative output without duplication, provider-free terminal replay after cleanup, replay without journal writes beyond worker cleanup, metrics recorded once at run end, catalog-only provider definitions with preference-only saves preserving catalog bytes, stable login/request credential keys, catalog-only provider resolution, provider/model selection independent of extra saved preferences, OSC 7501 status override and lifecycle (`rio.coding.rendering`, `rio.cli.status`) |

These are independent behavior models, not proofs extracted from Python.
They do not prove SQLite synchronization, OS process cleanup, transport behavior,
or hardware durability. Python tests cover the implementation and its failure paths.

`RioCli.lean` also models required subcommands: a missing command exits with status 2,
and a selected command proceeds to dispatch (`rio.cli`).
It also models OAuth refresh: the runtime resolver refreshes only expired credentials
(`rio.coding.provider_runtime`).

The current user-facing contracts are described in the
[CLI reference](../../docs/reference/cli.md) and
[session guide](../../docs/guides/sessions.md). In particular,
`RioCli.lean` models provider-free terminal replay only after cleanup succeeds.
