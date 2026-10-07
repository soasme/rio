# Durable Computation State

Status: draft
Date: 2026-10-06

This document defines the agreed next Rio core. It describes intended behavior, not an implemented or tested durability guarantee. It incorporates the design interview decisions Q1–Q21. PEP 723 and `uv run` replace the earlier library-level uv requirement.

## 1. Core contract

Rio uses one SKILL.state. The whole State is model context. Each round, the LLM calls one tool, `step`, with a JSON Patch. The Harness commits the patch, evaluates State, runs accepted code Cells, and records output. The LLM manages its context by adding, replacing, summarizing, and removing Cells.

Cells are immutable and independent. Each new code Cell version requests one finite execution. Notes do not execute code. A Session can last for months without retaining a worker between events.

Session stores the facts required to rebuild State and find unfinished work. Memory contains only disposable projections, buffers, and live processes. Recovery restores the computation model, not the Working Environment. Files, installed environments, processes, and remote services may change or disappear.

After restart, Rio preserves committed work and results. An interrupted execution produces `UnknownExecution`; Rio does not automatically run it again. The LLM inspects current facts and decides how to continue.

Rio does not guarantee identical model responses, restoration of external inputs, safe repetition of arbitrary effects, or exactly-once external actions. There is no human approval state. If the agent cannot proceed within its permissions and budget, it records terminal failure.

## 2. Minimal model

| Object | Meaning |
| --- | --- |
| Session | SQLite records of State changes, Cells, executions, observations, and run endings |
| State | Current goal, selected immutable Cells, and relevant runtime facts |
| Cell | An immutable note or Python script, identified by an increasing integer |
| Inbox | Persistent observations waiting for a model round |

The first version has one State and one local owner per Session. One code Cell runs at a time in each Session. Different Sessions may run concurrently; their shared external resources are not coordinated by Rio.

There is no kernel, general Task class, workflow graph, persistent Scratchpad, or per-operation effect ledger. Execution records describe Cell runs. They do not introduce another model-facing object hierarchy.

## 3. State and immutable Cells

State has six top-level fields:

```json
{
  "schema": "rio.state/1",
  "id": "s1",
  "revision": 12,
  "goal": "Fix the Flask route and pass the required tests",
  "cells": [
    {
      "id": 1,
      "previous_id": null,
      "kind": "note",
      "text": "Inspect app/routes.py before changing it"
    },
    {
      "id": 2,
      "previous_id": null,
      "kind": "code",
      "runtime": "python",
      "source": "# /// script\n# dependencies = []\n# ///\nfrom pathlib import Path\nprint(Path('app/routes.py').read_text())\n"
    }
  ],
  "runtime": {
    "next_cell_id": 3,
    "cells": {},
    "inbox": [],
    "usage": {}
  }
}
```

There is no separate `context` field or State lifecycle `status`. The Harness owns `revision` and `runtime`. Original goal requirements, permissions, supplied validators, and budget limits cannot be weakened by a model patch. Session records whether a run has ended.

`cells` is an ordered array of current Cell versions. IDs start at 1 and increase by one for every accepted new version. The Harness validates allocations against `runtime.next_cell_id` and advances it in the same transaction. Removed IDs are never reused.

An update replaces a full Cell object at the same array position. It receives the next ID and sets `previous_id` to the replaced Cell's ID. A new lineage uses `previous_id: null`. Updating Cell 1 in `[1, 2]` produces `[3, 2]`, with Cell 3 pointing to 1. Session retains Cell 1 unchanged. Array order does not imply numeric ID order.

The predecessor link records history only. It does not grant access to another Cell's code or memory. The first version has linear histories, without branches or merges. Results refer to the exact Cell that ran and never transfer to its successor.

Every new code version requests one run, even if its source is unchanged or only its metadata changes. To run the same code again, create a successor. There is no `request` counter, `enabled` flag, executable-content comparison, or `value` Cell kind. Use notes for commentary and draft code; structured data belongs in output or ordinary files.

### Editing context and controlling work

Removing a Cell removes it and its associated output from current context. It does not cancel accepted execution or delete Session history. The Harness keeps compact records of pending work and unresolved uncertainty in `runtime`, even when their Cells are no longer selected. Completion still enters Inbox.

Replacement is rejected while the predecessor is queued or running. Wait for completion or cancel it first. Removal remains allowed. A removed historical version cannot be reinserted as a current head; subsequent independent work can use a new lineage.

Cells cannot share variables, import Cell definitions, or call other Cells. They may read ordinary files and retrieve Session records as data. Each code Cell contains its own executable source and dependency metadata. Reading a file produced earlier does not create an automatic dependency or restoration guarantee.

## 4. The step transaction

The only model tool accepts a restricted RFC 6902 JSON Patch. [R1]

```json
{
  "patch": [
    {"op": "test", "path": "/revision", "value": 12},
    {"op": "test", "path": "/cells/0/id", "value": 1},
    {
      "op": "replace",
      "path": "/cells/0",
      "value": {
        "id": 3,
        "previous_id": 1,
        "kind": "note",
        "text": "Check the route and its tests"
      }
    }
  ]
}
```

Replace whole Cell objects. Reject edits such as `/cells/0/source`, stale predecessor IDs, and multiple successors of one Cell in a patch. Append new lineages at `/cells/-`. Allocate IDs in patch operation order. Remove array elements from higher indexes first. Do not use `move` or `copy` to change Cell identity, or disguise an update as removal and insertion elsewhere.

Validate the complete candidate, including context limits, before committing. One SQLite transaction stores new Cell versions, accepts the patch, advances revision and IDs, creates execution records, records control requests, and consumes the round's delivered messages. Invalid patches have no partial effect.

A durable `turn_id` identifies a model round. Only one patch can be accepted for that turn. Duplicate submissions return the original receipt; conflicting submissions are rejected. Enforce a unique execution record per code Cell ID. Rebuilding or repeatedly evaluating State creates no additional execution.

Process accepted control requests before dispatching queued code. Publish receipts and dispatch work only after commit.

## 5. Execution and supervision

Use Python processes for code execution and async control work in the owner. Borrow BEAM's ownership, monitoring, and message ideas; do not introduce BEAM or a new language. [R3–R4]

| Execution phase | Meaning | Restart behavior |
| --- | --- | --- |
| `queued` | Accepted; launch has not been authorized | Eligible to start when dispatch is allowed |
| `started` | Launch authorization committed; code may have run | Record UnknownExecution; never automatically rerun |
| `finished` | Output, error, cancellation, or interruption committed | Retain the result |

Commit `started` before launching `uv run`. A crash between authorization and actual launch can conservatively produce UnknownExecution. Treating this as uncertain avoids accidental duplicate execution. Environment setup failures return ordinary execution errors when their outcome is known; a new code version can request another attempt.

Run queued Cells serially in accepted order, independent of their current array positions. A successful execution allows dispatch to continue. An error or interruption durably pauses dispatch until the model consumes that observation in its next accepted patch. Apply cancellations before resuming the remaining queue. The model can cancel stale queued work before adding investigation Cells.

A Cell starts at its script entry point in a fresh process. It can run for hours while that process survives. Rio does not save stacks or replay internal calls. Uncommitted progress can be lost.

Use an OS lock for exclusive Session ownership and durable worker identity records. Before recovery admits any new execution, stop surviving local workers and their child processes. A PID alone is insufficient proof of identity. If Rio cannot establish that those workers have stopped, recovery fails. An ownership token can reject stale receipts, but it cannot prevent an old process with direct I/O from changing files.

Remote jobs remain part of the Working Environment. Stopping a local worker does not stop a remote request already sent. The LLM must inspect those outcomes.

In-memory messages are wake-up hints. Startup scans and periodic checks discover committed work when hints are lost. Waiting Sessions need no Cell process.

## 6. SQLite and model rounds

SQLite is the sole persistent backend. Store ordered events, immutable Cell versions, execution records, and retained outputs. Derived indexes and optional snapshots can be rebuilt. Rebuilding State is a pure reducer: it performs no external I/O and executes no Cells or model calls.

Session records include:

- State creation, accepted patches, Cell history, and control receipts.
- Model request inputs, attempts, accepted responses, and known usage.
- Execution authorization, output, errors, cancellation, and uncertainty.
- Inbox receipt and consumption, timers, and dispatch pauses.
- Configured limits, budget consumption, validators, and terminal events.

Use WAL and `synchronous=FULL` on writer connections, and verify the settings at startup. The target is process and host/power failure with supported local storage that honors synchronization. Disk loss and corruption require separate backup or recovery. [R2]

If the outcome of a SQLite commit is uncertain, stop admission and publication, stop local workers, and discard the in-memory projection. Reopen storage and inspect committed stable IDs before continuing. Existing external work can still have effects. If storage cannot be recovered, report storage failure. The LLM does not repair uncertain database commits.

Publish model-visible and user-visible progress only from committed records. Buffer stdout and streaming tokens for a configurable short interval, initially 100 ms. A crash may lose that unpublished buffer. Do not buffer required causal commits such as patch acceptance, launch authorization, control requests, message acknowledgment, or run endings. Do not hold a mutation transaction across external work.

### Fixed State during generation

Between rounds, fold a bounded set of Inbox observations into State and commit the resulting revision. Prepare one model request with its turn ID, exact State, delivered message IDs, prompt and tool schema references, model configuration, and retry count.

State remains fixed during that request. Workers can finish and incoming observations can commit to Inbox, but they do not modify the model's snapshot. Accept its patch against the revision it saw. The next round incorporates later observations. This permits decisions based on slightly old information and avoids constant revision conflicts.

Commit message consumption with patch acceptance. A paused dispatch resumes only after the error or interruption that caused the pause has been delivered and consumed. An unrelated in-flight model response cannot clear a newer pause.

For an unaccepted request after restart, query a persisted provider handle if supported; otherwise retry the saved request within budget. Never apply partial JSON. Accepted turns are not requested again. Persist retries, backoff deadlines, and known usage. Repeated provider calls may differ and may incur additional charges; unknown usage remains unknown.

## 7. UnknownExecution and resolution

Rio tracks work at Cell boundaries. Code can use ordinary file, network, and subprocess APIs within configured permissions. There is no mandatory intent/result protocol for each operation, automatic conflict detection, or deduplication of semantically equivalent programs. Deployment permissions are separate from the durability protocol; neither uv nor a prompt is a security boundary.

If a worker is interrupted before a reliable final result commits, record an output such as:

```json
{
  "type": "UnknownExecution",
  "cell_id": 2,
  "reason": "worker_lost_before_result_commit",
  "worker_stopped": true,
  "evidence_refs": ["session:event:104"]
}
```

Commit the output and its Inbox notice together, once per interruption. Retain source, timestamps, committed output, and any recorded external identifiers. Rio may not know which internal operations completed. An ordinary exception also does not imply rollback of earlier effects.

UnknownExecution ends that execution, not the Session. The next model round can add query Cells to inspect files, logs, hashes, processes, or remote APIs. Investigation produces new recorded observations. The LLM chooses whether to continue, retry, wait, or fail.

To resolve uncertainty, add an immutable note with `role: "resolution"`, a `target_cell_id`, recorded `evidence_refs`, and text explaining the decision. The Harness checks that the target and evidence exist and records the resolution. It does not certify the reasoning. The original UnknownExecution remains unchanged in history. A bare acknowledgment without evidence does not resolve uncertainty.

A fresh Cell ID permits a fresh execution, not a claim that external repetition is safe. Rio cannot mechanically prevent the LLM from repeating an action that already succeeded. This is an explicit limit of the design.

## 8. PEP 723, uv, and ordinary files

Each code Cell is a complete Python script. Its PEP 723 metadata declares dependencies and Python requirements. Execute it with `uv run`; uv supports this metadata and prepares the script environment. No new dependency schema or uv library interface is needed. [R5–R6]

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx==0.28.1"]
# ///
import json
import httpx

print(json.dumps({"version": httpx.__version__}))
```

Materialize the committed source as a script for execution. Capture stdout, stderr, and exit status. Scripts use normal Python syntax; there is no implicit top-level `return`. Include `dependencies = []` when no external packages are needed. Session retains the exact source and metadata. [R5–R6]

Runtime credentials and host environment configuration stay outside source and model context. Record non-secret execution configuration and interpreter/package versions when available. PEP 723 declarations do not by themselves promise identical future dependency resolution. Environment directories and package caches are disposable; preparation can fail if required artifacts are unavailable.

Use ordinary files or temporary files for scratch scripts, notes, and intermediate data. Rio supplies no scratch namespace, versioned filesystem, or scratch garbage collector. A recorded path is not a backup. Missing or changed files are conditions for the model to inspect or recreate. Code can check expected hashes when exact bytes matter.

Record required recovery facts in SQLite, not solely in temporary files. Large output may spill to a file, with an explicit notice that the full content is not retained in Session. File loss does not prevent State reconstruction, but it can prevent task completion.

## 9. Inbox, timers, and control notes

Persist Inbox messages before acknowledging receipt. Each has a stable message ID, kind, payload reference, causal identity, and Session sequence. Completion messages name the exact Cell ID. External events target the Session, with no Cell subscriptions to transfer between versions. Events wake the model; they never rerun existing Cell versions.

Duplicate delivery does not duplicate consumption or accepted work. External senders require stable IDs and redelivery for end-to-end delivery guarantees. Apply backpressure before acknowledgment. Do not silently discard completion or error records.

A code Cell can register a timer through a small Harness API. Commit the registration before acknowledging it to the worker. Store absolute deadlines and generations; recovery emits one logical event per expired generation. Registration request identities protect against duplicate delivery. A timer does not suspend or later resume the registering Cell.

If an accepted patch creates no work or conclusion, the Session may sleep while there is a running worker, timer, or configured external wake-up source. Without one, return an invalid-idle observation. Repeated invalid idle decisions consume the retry budget and eventually fail. Do not poll the model without new input.

### Cancellation

A note with `role: "cancel"` and `target_cell_id` requests cancellation. The owner handles it directly after patch commit, without waiting for a code execution slot. Record handling by note ID so repeated State evaluation does not repeat the command.

Queued cancellation finishes the target without launching it. Running cancellation stops the local worker and its children, then records the observed outcome. If the outcome is uncertain, return UnknownExecution. Failure to establish local termination blocks new execution. Cancellation cannot undo earlier writes or recall external requests. Removing a cancellation note does not undo an accepted cancellation.

### Conclusions

A note with `role: "conclusion"`, `result: "success" | "failure"`, and a reason requests a run ending. Ordinary text such as “done” has no control effect. Record the request outcome so it is not repeatedly processed. Rejected requests return observations; a new request requires a new note version.

The LLM judges whether a natural-language goal is achieved. Supplied validators are mandatory and cannot be weakened by model patches. The Harness checks their recorded outcomes and mechanical conditions; it does not prove semantic success for arbitrary goals.

Success requires no outstanding work or unresolved uncertainty and passing required validators. Account for observations that arrived during generation before accepting success; return them to a new round if they could invalidate the conclusion.

Failure stops admission, cancels queued work, and stops local workers. Record remaining external uncertainty and any inability to stop a worker in the terminal record. Failure need not resolve every unknown outcome. Restart never revives a terminal run; another attempt requires an explicit new run.

## 10. Context and resource limits

Bound State size and each output before it enters context. Reserve room for new observations and patch generation. Retain recorded output in Session within storage limits. Removing selected Cells and outputs reduces context without erasing history; read-only retrieval remains available through code.

Reject patches that exceed the State budget. Ask the LLM to prune before the hard limit. Keep compact pending and uncertainty records visible, and bound admitted work so those records fit. If the model cannot produce a valid smaller State within its retry budget, fail.

Persist configured model-round, token, execution-time, and consecutive-invalid-patch limits. Invalid idle decisions use the retry budget. Restart does not reset usage. The LLM judges lack of progress; there is no separate semantic progress detector. Exact numeric limits are deployment configuration.

## 11. Recovery and required tests

Recovery follows this order:

1. Obtain exclusive ownership and open SQLite. Fail if storage is unreadable or inconsistent.
2. Rebuild committed State, executions, requests, Inbox, timers, controls, limits, and run endings.
3. Stop surviving local workers and their child processes before admitting new execution. Fail recovery if termination cannot be established. Terminal runs remain terminal even when cleanup is needed.
4. Retain finished output. Convert interrupted started executions to UnknownExecution, commit notices, and pause dispatch.
5. Restore timers and recover unaccepted model requests. Apply committed control requests idempotently. Preserve any dispatch pause until its observation is consumed.
6. Let the LLM inspect uncertainty and choose subsequent work. Start queued work only when dispatch is allowed.

Example: a Cell edits a Flask route and starts tests. Rio stops before final output commits. On restart it does not rerun the Cell. It returns UnknownExecution with committed output and the original source. The LLM cancels stale queued work if needed, runs a query Cell, inspects the file, and verifies it. A resolution note cites those observations. A matching file alone does not prove every other operation in the original script completed.

| Failure or input | Required result |
| --- | --- |
| Repeated State evaluation or duplicate patch | One accepted version and at most one execution record per code ID |
| Update Cell 1 in `[1, 2]` | `[3, 2]`; Cell 3 points to 1; stored Cell 1 stays unchanged |
| In-place field edit or stale predecessor | Reject the whole patch |
| Replace unchanged code | New version requests one run |
| Replace a busy Cell | Reject until it finishes or cancellation settles |
| Remove a busy or uncertain Cell | Context shrinks; pending work or uncertainty remains visible |
| Concurrent accepted code Cells | Serial dispatch in accepted order |
| Error with more queued work | Pause until the model consumes the error in an accepted patch |
| Cancel while worker slot is occupied | Owner handles cancellation without running another code Cell |
| Crash after patch commit, before dispatch | Queued work survives |
| Crash after launch authorization | UnknownExecution; no automatic rerun |
| Old worker or child survives restart | Stop it before new execution, or fail recovery |
| Lost model response or partial JSON | Bounded retry; no partial or duplicate accepted patch |
| Observation arrives during generation | Persist to Inbox; keep the round's State fixed |
| Unrelated response arrives after a new error | It cannot clear the new dispatch pause |
| Uncertain SQLite commit | Stop admission/publication and local workers; reopen and inspect |
| Streaming output is visible | The chunk was committed first |
| Resolution note with no evidence | Cannot clear uncertainty |
| Successful resolution note | Original UnknownExecution remains in history |
| Scratch file disappears | No claimed restoration; inspect or recreate input |
| Environment disappears | Prepare from script metadata or report failure |
| Context or output exceeds limit | Bounded context, explicit truncation, pruning or failure |
| Idle decision without a wake-up source | Invalid-idle observation and bounded retries |
| Restart at timer expiry | One logical message per generation |
| Success with pending work or failed validator | Reject and return an observation |
| Failure with unresolved external work | Terminal failure preserves uncertainty |
| Restart a terminal run | No revival or budget reset |
| Remove snapshots and derived indexes | Rebuild committed State without external I/O |

Test process termination and host/power failure separately. A process-kill test does not prove power-loss durability. Measure commit latency, rebuild time, worker and uv startup, Inbox delay, idle memory, coding completion rate, and recovery quality. Durability remains a design claim until these tests pass.

## 12. Scope and relation to earlier designs

Implement the SQLite reducer and commit protocol, step admission, serial execution and worker cleanup, fixed model rounds, Inbox and timers, control notes, then fault-injection tests and coding evaluations.

BEAM informs process ownership and supervision. Rio keeps Python. The CLM approach informs model-managed context. Rio v0.7.0–v0.7.1 provide the earlier notebook-based direction; this design keeps JSON Patch and Session history while replacing shared kernel execution. [R8–R10, R13]

Import legacy notes and outputs as historical material. Code with implicit variable dependencies must become an independent script before execution. Importing historical code does not run it; accepting a new code Cell does. JSONL is only a one-time import source.

Pi Durable is a reference for committed state and interrupted-work handling. This design deliberately uses a smaller Cell-boundary protocol. It does not adopt per-operation intent/effect/result tracking, and therefore cannot promise its associated operation-level recovery behavior. [R11–R12]

The core excludes stack persistence, internal-call replay, mandatory effect adapters, dependency graphs, a persistent Scratchpad, parallel code execution within one Session, multi-machine takeover, automatic compensation, and executable rewind. SQLite layouts, OS-specific worker containment, API wire formats, and numeric limits remain implementation details within this contract.

## References

- [R1: RFC 6902 JSON Patch](https://www.rfc-editor.org/rfc/rfc6902)
- [R2: SQLite synchronization settings](https://sqlite.org/pragma.html#pragma_synchronous)
- [R3: Erlang processes](https://www.erlang.org/doc/system/ref_man_processes.html)
- [R4: OTP supervisor behavior](https://www.erlang.org/doc/system/sup_princ.html)
- [R5: uv script dependencies and environments](https://docs.astral.sh/uv/guides/scripts/)
- [R6: PEP 723 — Inline script metadata](https://peps.python.org/pep-0723/)
- [R8: Rio v0.7.0](https://github.com/soasme/rio/tree/c760a9f9f6ea4b21b8ecf6adee026768253bdb3d)
- [R9: Rio changes through v0.7.1](https://github.com/soasme/rio/compare/v0.7.0...v0.7.1)
- [R10: Rio v0.7.1 Session records](https://github.com/soasme/rio/blob/v0.7.1/src/rio/coding/session_store/entries.py)
- [R11: Pi Durable specification](https://github.com/earendil-works/pi/blob/main/packages/durable/docs/spec.md)
- [R12: Pi Durable README](https://github.com/earendil-works/pi/blob/main/packages/durable/README.md)
- [R13: Context Language Models reference implementation](https://github.com/facebookresearch/context-language-models)
