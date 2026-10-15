# Glossary

Rio keeps a session for one goal. The model edits cells; the owner executes
runnable cells and records what happened. Each turn gives the model a fixed
view of that session.

## Core terms

| Term | Meaning |
| --- | --- |
| **Session** | The durable history and current state of one goal. Resuming continues the same session. |
| **Cell** | An immutable item the model can add, replace, or remove from its context. A `note` holds text. Other kinds describe work: `code` is a Python script; `cmd` is an argument list. |
| **Turn** | One model decision with a saved State, prompt, and tool schema. It can take several attempts to produce valid operations. Acceptance completes the turn. |
| **Attempt** | One request for a model response within a turn. Rejection retries the same turn with its original context and the rejection reason. |
| **Operation** | An `add`, `replace`, or `remove` edit to cells, or a `control`. The model submits a list of operations through the `step` tool. Acceptance commits the whole list and queues new runnable cells. |
| **Control** | An action requested by a `control` op: `cancel`, `resolution`, or `conclusion`, each with a `reason`. Controls are not cells; they never enter State. The owner handles each accepted control once. |
| **Execution** | The work associated with one runnable cell version: queued, started, then finished with output and a result. Removing a cell from context does not cancel its work. |

`step` is the tool's name. **Run** describes driving a session toward completion.

```text
session
  turn: fixed context
    attempt -> response -> rejected operations
    attempt -> response -> accepted operations
                              cells -> executions -> observations for a later turn
```

`Limits.turns` and `usage["turns"]` count newly prepared turns, including ones
that never reach acceptance. Retrying or reopening an unaccepted turn does not
increment that count. `Limits.invalid_attempts` caps attempts per turn, including
the first, and also bounds invalid idle decisions. Reusing a saved response
starts no new attempt.

The provider may retry a transport request inside one model attempt. That retry
budget is local to `rio.ai`; it does not increment the session's attempt count.
When the request still fails, the session records `provider_failed`, refunds the
turn's attempt, and retries after a backoff. `Limits.provider_attempts` caps these
failures per turn.

## Supporting terms

- **State** is the model's view: goal, revision, selected cells, and runtime
  observations. The model edits only cells. The **inbox** holds observations
  until a turn delivers them and acceptance marks them consumed.
- **Owner** schedules executions and handles controls. `ProcessOwner`
  implements local process execution. A **validator** is an owner-defined check
  required before success.
- **Journal** is the sequence of committed events. Its **projection** is the
  current set of records. A **receipt** identifies an accepted turn; submitting
  the same operations again returns that receipt without queuing work again.
- **UnknownExecution** means a started execution's outcome is uncertain. It is
  never automatically replayed. A resolution control links recorded evidence to
  the uncertainty; the original result remains in history.

## Action names

| Method | Action |
| --- | --- |
| `Owner.handle_controls()` | Process cancellation, resolution, and conclusion controls. |
| `Session.start_attempt()` | Record a new attempt and increment its counters. |
| `Session.record_response()` | Save a response and account for token usage. |
| `Session.provider_failed()` | Record a failed provider request and its backoff. |
| `Session.append_output()` | Append execution output, retaining bounded tails. |
| `Session.mark_handled()` | Record a control outcome and any resolution or rejection observation. |

These methods change state. The corresponding fields (`response`, `output`,
`handled`, and `controls`) hold data. A saved response still needs validation;
a handled control may have been accepted or rejected.

See [architecture](explanation/architecture.md),
[session.py](../src/rio/agent/session.py),
[runner.py](../src/rio/agent/runner.py), and
[ledger.py](../src/rio/agent/ledger.py) for the full behavior.
