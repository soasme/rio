# Glossary

These terms describe the current implementation. Prefer **turn** for a saved
model decision and qualify **attempt** as a model attempt or provider attempt.

## Turns, steps, rounds, and attempts

| Term | Meaning in Rio | Code |
| --- | --- | --- |
| **Turn** | A durable decision record with a fixed State snapshot, system prompt, tool schema, delivered observation IDs, and mutable attempts, response, and receipt. `prepare_turn()` reuses an unaccepted turn; otherwise it creates one. | [Turn](../src/rio/agent/shapes.py), [Session.prepare_turn](../src/rio/agent/session.py) |
| **Step** | The model's `step` tool call proposing a list of cell operations (`ops`). A usable response contains exactly one such call. A proposal may be rejected; acceptance commits its effects and a receipt. A step can contain several operations and queue several executions. | [STEP](../src/rio/agent/prompt.py), [Session.accept](../src/rio/agent/session.py) |
| **Round** | Existing wording for a model decision cycle. `Limits.rounds` and `usage["rounds"]` count newly prepared turns, including turns that never reach acceptance. `_model_round()` obtains or reuses a response and extracts its step operations; it does not increment this counter. | [Limits](../src/rio/agent/state.py), [runner](../src/rio/agent/runner.py) |
| **Model attempt** | A journaled attempt to obtain a response for an existing turn. `Session.attempt()` increments that turn's `attempts` and the session's `usage["attempts"]` before the provider call. Retrying preserves the prepared context. Reusing a saved response does not start another attempt. | [Session.attempt](../src/rio/agent/session.py), [runner](../src/rio/agent/runner.py) |
| **Provider attempt** | One request attempt inside `rio.ai`'s transport retry loop. `RetryAttempt.number` starts at zero; `max_retries` permits additional attempts after the first. Several provider attempts can occur within one model attempt without incrementing the session counters. | [retry.py](../src/rio/ai/retry.py) |

```text
prepare a new turn                         rounds +1
  model attempt                           attempts +1
    provider attempt [transport retries]
  response -> step proposal -> reject
  model attempt, same prepared turn        attempts +1, rounds unchanged
  response -> step proposal -> accept      receipt; queue runnable cells
```

`Limits.invalid_attempts` caps all model attempts per turn, including the first,
despite its name. It also bounds invalid idle decisions in the run loop.
`Limits.rounds` is checked before creating a new turn; an existing unaccepted
turn is returned before that check. Token and attempt limits still apply when
starting another model attempt. A rejected proposal consumes an attempt, not
another turn. A model attempt can also fail before producing any step.

## State and durable records

| Term | Meaning in Rio |
| --- | --- |
| **Session / run** | A session is the durable record of one goal, configuration, turns, cells, and results. A run drives that session toward a terminal result. Resuming continues the same session; a terminal session stays terminal. |
| **State** | The model-facing context: schema, session ID, revision, goal, selected cells, and runtime observations. The model changes cells through step operations; the remaining fields are runtime-owned. |
| **Cell** | An immutable, ID-addressed item in State. Adding or replacing creates a new ID; replacement links to the old ID through `previous_id`. Removing drops a cell from context without cancelling its execution or deleting history. |
| **Cell kind** | A cell's shape and meaning. `note` is built in and never executes. Callers register runnable kinds through `CellKind`; `rio.coding` supplies `code` (Python script) and `cmd` (argument list). |
| **Operation / op** | One `add`, `replace`, or `remove` entry in a step's `ops` list. Existing cells are addressed by ID, at most once per step. |
| **Patch / admission** | A proposed cell edit and the process of validating it. `admit()` validates operations and constructs candidate State. `Session.accept()` checks session constraints and commits the edit; `InvalidPatch` identifies a rejected proposal. |
| **Receipt** | The durable acknowledgement of an accepted turn: turn ID, resulting revision, new cell IDs, and journal event sequence. Repeating the same operations for that accepted turn returns the same receipt; conflicting operations are rejected. |
| **Revision** | A State version used to reject stale turns. Preparation and admission each advance the revision; it is not the turn counter or the journal sequence. |
| **Journal / event** | The ordered SQLite record of committed transitions. Each event has a sequence number, type, timestamp, and changes. |
| **Projection** | The current records obtained by folding journal events. `Store.data` includes metadata, turns, executions, inbox messages, and other collections; it contains more than the model-facing State. |
| **Runtime ledger** | `RuntimeLedger`, inherited by `Session`, records execution, inbox, timer, control, validator, and terminal transitions. |

See [state.py](../src/rio/agent/state.py),
[session.py](../src/rio/agent/session.py),
[shapes.py](../src/rio/agent/shapes.py), and
[store.py](../src/rio/agent/store.py).

## Execution and observations

| Term | Meaning in Rio |
| --- | --- |
| **Owner** | The component that recovers work, processes controls, dispatches runnable cells serially, and waits for wake-ups. `Owner` supports note-only sessions; `ProcessOwner` supplies local process execution and validation. |
| **Execution** | The runtime record for a runnable cell version, moving through `queued`, `started`, and `finished`. It holds output and a result separately from the immutable cell. |
| **Output** | Text from an execution, stored by owner-named stream with bounded tails. It is distinct from a model response and from an execution's final result. |
| **Response** | A completed assistant message saved on a turn before step validation. A saved response may still contain an invalid proposal. Provider response events are separate from session journal events. |
| **Observation / inbox** | A recorded fact delivered to the model, such as an execution result, timer, rejected control, or external message. Turn preparation selects unread messages; acceptance marks the selected messages consumed. New arrivals wait for a later turn. |
| **Control** | A note with role `cancel`, `resolution`, or `conclusion`, recorded for the owner to handle. Cancellation controls are processed first. A handled control may have been accepted or rejected. |
| **Pause** | A dispatch stop caused by an execution error or unknown result. Accepting a step that consumes the failure observation clears that pause. |
| **UnknownExecution / resolution** | `UnknownExecution` means Rio cannot establish a started execution's outcome. It is never automatically replayed. A resolution note links recorded evidence to that uncertainty; the original result remains in history. |
| **Conclusion / terminal result** | A conclusion note requests success or failure. An accepted conclusion ends the run. Success requires no pending executions, unfired timers, unresolved uncertainty, or unread observations, plus passing configured validators. Runtime failures can also end a run. |
| **Validator** | An owner-defined check required for success. Results are saved per conclusion and reused after restart; a new conclusion gets new checks. |
| **Timer / wake-up** | A timer records an absolute deadline and delivers one inbox observation per stable request ID. Commits, task completion, deadlines, and model completion wake the owner. `external_wakeup` allows an application to keep an idle session waiting for external input. |
| **Recovery / replay** | Recovery reconciles recorded work with the execution environment. Replay reads committed events without re-executing cells or calling a model. Terminal replay first recovers leftover workers. |

See [ledger.py](../src/rio/agent/ledger.py),
[runner.py](../src/rio/agent/runner.py), and
[ProcessOwner](../src/rio/coding/owner.py).

## Methods whose names describe records

These methods perform actions. The similarly named fields hold data.
`Session.output()` and `Session.handled()` are inherited from `RuntimeLedger`.

| Existing method | Action |
| --- | --- |
| `Owner.controls()` | Process unhandled control notes: cancel executions, record resolutions, or evaluate and apply conclusions. It can stop active work, run validators, and end the run. |
| `Session.attempt(turn_id)` | Record the start of a model attempt, increment counters, clear the saved response, and provisionally mark token usage unknown. It does not call the provider itself. |
| `Session.response(turn_id, response, tokens, known=...)` | Save a model response and add token usage; clear one unknown-usage count when usage is known. It does not request a response or accept its step. |
| `Session.output(cell_id, chunks)` | Append text to a started execution's output streams through a journal event, retaining bounded tails. It does not read or print output. |
| `Session.handled(note_id, outcome, resolution=...)` | Commit a control's handled flag and outcome, optionally record a resolution, and add an inbox observation for rejection. It does not execute the requested control itself. |

For the overall flow, see [How Rio runs tasks](explanation/architecture.md).
The [Lean models](../tests/lean/README.md) check selected behavior contracts
independently of the Python implementation.
