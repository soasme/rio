# rio.agent

## What is `rio.agent`?

- The whole package rests on this idea: the model edits its own state
  through one tool (`step`) using three ops (`add`, `replace`, `remove`).
  Every accepted change is a journal event, and state is a fold over those events.
  Durability, resume, idempotent retries and `UnknownExecution` all follow from that one choice.
- `admit()` in [state.py](state.py) covers ID assignment, lineage, positions,
  context budget and runtime reset. `Session.prepare_turn()` rebuilds runtime observations.
- `CellKind` is a four-field extension point: name, field schemas, prompt and
  validator. The `Owner` decides how a Cell runs. [rio.coding.cells](../coding/cells.py)
  adds `code` and `cmd` without changing the core. Custom agents can add other kinds.
- Each model round sees a fixed state snapshot, without conversation history.
  A separate lock file protects ownership before SQLite opens the database.
  See [Architecture](../../../docs/explanation/architecture.md).

Each turn journals its prepared context once. Later turn events contain changed
fields, and output events append new chunks. Replay also accepts older full records.

## Constraint

This module is environment agnostic and must not rely on operating systems.
It does not start processes, open sockets or read environment variables.
Agents that execute work, such as `rio.coding`, supply that through an `Owner` subclass.
`Store` is the one exception: it opens a local SQLite file and its lock sidecar.

The core knows that an execution was queued, started, produced output on
owner-named streams and finished with a result. Validator specs and `owner_config`
are opaque JSON that only the `Owner` interprets. Owner-specific records, such as
worker identities, live in owner collections written with `Session.record`.

## Responsibilities

| Module | Responsibility |
| --- | --- |
| [session.py](session.py) | Create sessions, prepare model context and admit turns. |
| [ledger.py](ledger.py) | Journal executions, inbox, timers, validators, controls and run endings with metrics. |
| [store.py](store.py) | Commit the SQLite journal and fold events into state. |
| [state.py](state.py) | Validate cell kinds, apply operations and enforce context budgets. |
| [runner.py](runner.py) | Drive model turns, idle decisions and terminal replay. `Owner` dispatches cells, handles controls and conclusions, and waits for wake-ups. |

## Use

Import `Store` from `rio.agent.store` and `Session` from `rio.agent.session`.
`Session` inherits runtime operations from `RuntimeLedger`, keeping one public entry point.
Single-record updates share `_patch`; multi-record transitions commit atomically.
Create or open a `Store`, then create or open a `Session` with the same cell
kinds. Notes are built in; runnable kinds come from the caller. Pass a
`rio.ai.ModelProvider` and an `Owner` to `runner.run`, or call `runner.replay` for a
completed session without provider setup. Both recover leftover work before publishing.
`Owner` runs note-only sessions and finishes Cells left started by a lost owner as
`UnknownExecution`. Subclasses implement `launch` and `validate` to execute runnable
kinds and validators, and extend `recover` to stop their leftover work first.

`Session.prepare_turn` and `Session.attempt` raise `RunStopped` from
`rio.agent.session` when a budget or lifecycle limit prevents progress. The runner
records these stops as failure conclusions and retries `InvalidPatch` rejections.
Other errors in session operations or saved-response validation propagate to the
caller without recording a failure conclusion. Provider request errors remain retryable.

`Session.create` accepts numeric `Limits`, validator specs, an `owner_config` and an
`external_wakeup` policy. The model cannot change these settings. Closing the
store releases ownership; the caller closes any provider it created.

See the [library guide](../../../docs/guides/library-usage.md) for a coding
session and the [Lean models](../../../tests/lean/README.md) for verification
scope. Scripts are not sandboxed; recovery does not undo external effects.
