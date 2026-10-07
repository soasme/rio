# Architecture

Rio runs one coding task at a time. The model edits its own context as JSON.
Rio saves each accepted change in SQLite and runs code in separate Python processes.

## Packages

| Package | Job |
| --- | --- |
| `rio.ai` | Send requests to model providers and read their responses. |
| `rio.agent` | Own state, check patches, run scripts, and recover sessions. |
| `rio.coding` | Configure providers, credentials, extensions, and MCP clients. |
| `rio.cli` | Start or resume a task and print committed results. |

## One step

```mermaid
flowchart LR
    S[Saved state] --> M[Model]
    M -->|step patch| C[Check and commit]
    C --> W[Run Python script]
    W -->|commit output and result| I[Inbox]
    I --> S
```

State has a goal, a revision, cells, and runtime observations. A cell is a note,
a complete Python script, or a command. The model calls one tool, `step`, with a
JSON Patch. The patch must test the revision first. It can append, replace, or
remove whole cells. A replacement gets a new ID and links to the old cell. Old versions stay
in history. Removing a cell from context does not cancel its work.

Each model round uses a saved, fixed state. New observations wait in the inbox.
Accepting a patch also records which observations that round consumed. Retrying
an accepted step returns the same receipt and does not launch the code again.
A complete saved model response can be accepted after restart. Lost responses
use bounded retries; missing token usage stays unknown.

## Running code

One owner runs scripts in order with `uv run --script`. Each script has PEP 723
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
Output has a size limit and a visible truncation flag. An idle session has no
script process.

## Recovery

Sessions live at `~/.rio/sessions/SESSION_ID.sqlite3`. Resume with
`rio run --resume SESSION_ID`. The goal, limits, model configuration, and recorded
results stay the same. A finished run stays finished; use a new run for a new task.

SQLite uses WAL and `synchronous=FULL`. Events rebuild state without executing
code or calling a model. A failed commit stops new work and publication. Reopen
the session to check what committed; a missing receipt does not prove rollback.

Recovery checks worker identities and stops surviving local processes. A script
that started without a recorded result becomes `UnknownExecution`. Rio never
replays it automatically. Errors pause the queue until the model consumes their
observations. Unknown results also need an explicit resolution with recorded
evidence. The original unknown result stays in history.

## Controls and limits

Notes can request cancellation, resolve uncertainty, or conclude the task.
Cancellation runs in the owner without waiting for a worker slot. Success requires
no pending work, unread observations, or unresolved results, and passing any
configured validators. Validators use the same process supervision.

`Session.create` accepts `Limits` and validator argument lists. These are outside
model patch authority. Limits cover model rounds, tokens, script time, retries,
context, output, pending work, inbox messages, and journal size. Cleanup and final
records may exceed the journal admission limit.

`Session.receive` saves an external observation before acknowledging it. Stable
IDs make duplicate delivery harmless; conflicting IDs are rejected. The prefixes
`execution:`, `control:`, `timer:`, and `idle:` are reserved for runtime records.
Scripts can read committed history with `rio.agent.api.records(after=...)` and
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
