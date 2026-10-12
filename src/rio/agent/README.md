# rio.agent

## What is `rio.agent`?

- The whole package rests on this idea: the model edits its own state
  through one tool (`step`) using three ops (`add`, `replace`, `remove`).
  Every accepted change is a journal event, and state is a fold over those events.
  Durability, resume, idempotent retries and `UnknownExecution` all follow from that one choice.
- `admit()` in [state.py](state.py) covers ID assignment, lineage, positions,
  context budget and runtime reset. `Session.prepare_turn()` rebuilds runtime observations.
- `CellKind` is a five-field extension point: name, field schemas, prompt,
  validator and command builder. [rio.coding.cells](../coding/cells.py) adds
  `code` and `cmd` without changing the core. Custom agents can add other kinds.
- Each model round sees a fixed state snapshot, without conversation history.
  Scripts run in separate processes. A separate lock file protects ownership
  before SQLite opens the database. See [Architecture](../../../docs/explanation/architecture.md).

## Responsibilities

| Module | Responsibility |
| --- | --- |
| [session.py](session.py) | Admit turns and own event writes, including controls and run endings with metrics. |
| [store.py](store.py) | Commit the SQLite journal and fold events into state. |
| [state.py](state.py) | Validate cell kinds, apply operations and enforce context budgets. |
| [runner.py](runner.py) | Drive model turns, idle decisions and terminal replay. |
| [owner.py](owner.py), [worker.py](worker.py) | Supervise processes, recover workers and run validators. |
| [api.py](api.py) | Let scripts read committed history and schedule timers. |

## Use

Import `Store` from `rio.agent.store` and `Session` from `rio.agent.session`.
Create or open a `Store`, then create or open a `Session` with the same cell
kinds. Notes are built in; runnable kinds come from the caller. Pass a
`rio.ai.ModelProvider` to `runner.run`, or call `runner.replay` for a completed
session without provider setup. Both recover recorded workers before publishing.

`Session.create` accepts numeric `Limits`, validator commands and an
`external_wakeup` policy. The model cannot change these settings. Closing the
store releases ownership; the caller closes any provider it created.

See the [library guide](../../../docs/guides/library-usage.md) for a coding
session and the [Lean models](../../../tests/lean/README.md) for verification
scope. Scripts are not sandboxed; recovery does not undo external effects.
