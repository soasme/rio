# Architecture

Rio runs one coding task at a time. The model edits its own context as JSON.
Rio saves each accepted change in SQLite and runs code in separate Python processes.

## Packages

| Package | Job |
| --- | --- |
| `rio.ai` | Send requests to model providers and read their responses. |
| `rio.agent` | Own state, check steps, schedule cells, and recover sessions. No OS dependencies. |
| `rio.coding` | Define code and cmd cells, run them in processes; configure providers and credentials. |
| `rio.cli` | Start or resume a task and print committed results. |

## One step

```mermaid
flowchart LR
    S[Saved state] --> M[Model]
    M -->|step ops| C[Check and commit]
    C --> W[Run Python script]
    W -->|commit output and result| I[Inbox]
    I --> S
```

State has a goal, a revision, cells, and runtime observations. `rio.agent` knows
one cell kind, the note. A note-only agent can record facts and conclude. Callers
add runnable kinds with `CellKind`: a name, its fields and their JSON schemas, a prompt,
and a validator. The owner decides how a cell runs. `rio.coding.cells` adds `code`
(a complete Python script) and `cmd` (a command) and maps each to argv for
`ProcessOwner`; `rio run` uses both. The model calls one tool, `step`, with a
list of ops: `add` a cell, or `replace` or `remove` a cell by ID. Rio assigns IDs
and only appends: a replacement gets a new ID at the end and links to the old cell. Replaced
and removed cells stay in place, listed in `stale`, until compaction drops them once they
outweigh live cells. The cached prompt prefix through the last unchanged cell survives the
edit. Old versions stay in history. Removing a cell from context does not cancel its work.

Cells are memory; controls are actions. A `control` op requests an action and
never becomes a cell:

```json
{"op": "control", "control": {"type": "cancel", "target_cell_id": 4, "reason": "stale"}}
```

Each model turn uses a saved, fixed state. New observations wait in the inbox.
Successful completions wait until queued work drains or an error pauses it,
so one turn sees them together.
Accepting a step also records which observations that turn consumed. Retrying
an accepted step returns the same receipt and does not launch the code again.
A complete saved model response can be accepted after restart. Lost responses
use bounded retries; missing token usage stays unknown.

Invalid steps and provider failures retry the same turn on separate budgets.
`Limits.invalid_attempts` covers model output the runtime rejects; the next attempt
shows the model the rejection. `Limits.provider_attempts` covers failed requests,
error responses and missing responses; these back off for up to two minutes, are
journaled as `provider_failed`, and are not shown to the model. An HTTP error that
retrying cannot fix, such as 401 or 404, fails the run at once. The owner keeps
running work, timers and controls during either backoff, and a restart resumes the
saved backoff.

## Running code

One owner runs runnable cells in order. `rio.agent.runner.Owner` schedules them;
`rio.coding.owner.ProcessOwner` runs each in its own supervised process. A
`code` cell runs with `uv run --script`. Each script has PEP 723
metadata and its own process. Files can pass data between scripts; variables do
not carry over. For example:

```python
# /// script
# dependencies = []
# ///
from pathlib import Path

print(Path("README.md").read_text())
```

A `cmd` cell, such as `{"kind": "cmd", "argv": ["pytest", "-q"]}`, runs its argv
directly, without a shell or uv. It shares the script queue and supervision.

The owner commits launch permission and the supervisor's identity before allowing
code to start. It commits output before printing it, normally every 100 ms.
Output over 2000 lines or 50 KB keeps its tail, ends with a
`[Showing lines X-Y of Z. Full output: PATH]` notice, and sets the truncation flag.
The owner keeps a rolling tail and byte and line counts while streaming, so memory
stays bounded. The full output goes to a log in the owner's temporary directory,
within a 64 MB disk budget shared by all Cells; once it runs out, the notice says
how much was kept. Logs are deleted when the owner exits. An idle session has no
script process.

Each model turn fits runtime records into the context budget. Streams within an
equal share of the remaining bytes stay whole; larger ones show a tail, and the
record's `omitted` field counts the leading bytes left out per stream. The
`execution_finished` event in the result's `evidence_refs` keeps the recorded
output. A finished, certain record of a live Cell settles once: it moves to
`settled` with its shown output fixed and is sent after the Cells that existed when it
settled, so later turns read it from the provider's cache. Queued, running and
unresolved records stay in `runtime.cells` and are refit every turn. If even the
records without output do not fit, the turn defers new settlements, then drops settled
records, then shows no outstanding work, plus a `context` observation naming the
Cells left out. Rio stops the run only when the goal and Cells alone exceed the budget.

## Recovery

Sessions live at `~/.rio/sessions/SESSION_ID.sqlite3`. Resume with
`rio run --resume SESSION_ID`. The goal, limits, model configuration, and recorded
results stay the same. A completed run cleans up recorded workers and replays its
journal without loading a provider or requiring the original project directory.
Cleanup failures stop replay. Use a new run for a new task.

SQLite uses WAL and `synchronous=FULL`. Events rebuild state without executing
code or calling a model. `Session` owns event writes; `Store` commits and folds
them into state. A failed commit stops new work and publication. Reopen
the session to check what committed; a missing receipt does not prove rollback.

Recovery checks worker identities, which `ProcessOwner` keeps in its own `workers`
records, and stops surviving local processes. A script
that started without a recorded result becomes `UnknownExecution`. Rio never
replays it automatically. Errors pause the queue until the model consumes their
observations. Unknown results also need an explicit resolution with recorded
evidence. The original unknown result stays in history.

## Controls and limits

Controls cancel work (`cancel`, with `target_cell_id`), resolve uncertainty
(`resolution`, with `target_cell_id` and `evidence_refs`), or end the task
(`conclusion`, with `result`). Each needs a `reason`. Accepted controls get
IDs 1, 2, ... in order and are handled once, including after a restart.
A rejected control returns a `control_rejected` observation that carries the
control. Cancellation runs in the owner without waiting for a worker slot. Success requires
no pending work, unread observations, or unresolved results, and passing any
configured validators. Validators use the same process supervision.

`Session.create` accepts `Limits`, validator specs, and an opaque `owner_config`.
`ProcessOwner` runs each validator spec as an argument list and starts processes in
`owner_config["cwd"]`. These are outside model authority. Limits cover model turns, tokens, script time, retries,
context, output, pending work, inbox messages, and journal size. Cleanup and final
records may exceed the journal admission limit. Set
`Session.create(external_wakeup=True)` when an application supplies external
observations that can wake an idle session; the default is `False`.

`Session.receive` saves an external observation before acknowledging it. Stable
IDs make duplicate delivery harmless; conflicting IDs are rejected. The prefixes
`execution:`, `control:`, `timer:`, and `idle:` are reserved for runtime records.
Scripts can read committed history with `rio.coding.api.records(after=...)` and
schedule a timer with `timer(request_id, deadline, payload)`. A timer uses an
absolute Unix deadline and produces one event per request ID.

## Boundaries

Execution requires POSIX and local filesystem locks. Symbolic links share the
owner lock; hard-linked databases are rejected. Rio stops if it cannot establish
that a previous worker has stopped. The database records work; it does not restore
project files or external services. Detached processes and remote effects need
application-specific controls. Scripts are not sandboxed, and external effects
are not guaranteed to happen exactly once.

Tests cover process crashes, lost commit receipts, replay, and child cleanup.
They do not prove power-loss durability. That needs testing on the target storage.

See the [design contract](../design/durable.md), [Lean models](../../tests/lean/README.md),
and [evals](../../evals/README.md) for details and checks.
