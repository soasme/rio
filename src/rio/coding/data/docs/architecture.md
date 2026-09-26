# rio architecture

rio is a SKILL.state coding agent informed by *SKILL.state: Scalable
Long-Horizon Agent Skills* (arXiv:2608.26263). The runtime materializes state
while the model receives an append-only history of state patches and observations.

## The three packages

```text
rio.ai      provider/model streaming layer
rio.agent   state-patch runtime and history compaction
rio.coding  CLI app, resources, skills, extensions, commands, TUI
```

- `rio.ai`: providers, wire-level message/tool types, and the provider-neutral
  assistant stream event union. No knowledge of skills, state, or sessions.
- `rio.agent`: the portable SKILL.state runtime -- `HarnessSpec`, `Harness`,
  `run_skill_loop`, state-delta validation, and the step event stream. No
  knowledge of the coding domain, the filesystem, or any frontend.
- `rio.coding`: the coding domain expressed as one SKILL.state skill (see
  `rio.coding.coding_skill`), plus everything a coding agent needs that isn't
  the runtime itself: resource discovery, tools, project trust, the session
  journal, and frontends (CLI, RPC, TUI).

Keep `rio.agent` free of Typer, Rich, Textual, filesystem layout assumptions,
and coding-specific vocabulary. `rio.coding` is where all of that lives.

## Sessions append history and rebuild state

Each request contains fixed instructions plus a history beginning with an
initial or rebuilt state. The runtime appends every accepted RFC 7396 state
patch and every action observation; private reasoning is never replayed. It
still owns the materialized state and validates patches before actions run.

At 80% of the configured context window, the runtime replaces the history
with one exact materialized state. The next steps append normally. State is
budgeted, so agents must intentionally remove or shorten state fields when a
useful rebuilt state cannot fit.

## File context

The coding skill's `execute_action` callback checks file hashes, executes the
existing tool, and returns its result plus a state delta. The loop commits the
delta; cache-limit notes use the normal tool result.

`state.files[path]` holds `status`, `hash`, and `context` (`total_lines` plus
`slices` keyed by line range). Reads accumulate slices; writes refresh cached
windows. Set `context` to null to forget content while keeping the hash and note.
Content that exceeds the state budget is omitted with a note in the tool result.

The journal keeps the model's original patch and the full resulting state,
including runtime file updates. Resume uses that snapshot, not patch replay.

## Branching is checkpointing, not tree replay

Because every committed step already carries the complete state it produced
(not a diff against a transcript position), a "checkpoint" is just one
journaled entry with a state snapshot attached, and "branching" is adopting
that snapshot as the live state. There is no subtree of messages to replay.

`rio.coding.session_store` defines the journal entry types (`TurnEntry`,
`StepEntry`, `StateResetEntry`, and a handful of bookkeeping entries for
model/thinking-level changes and labels) and the tree utilities that find the
latest checkpoint on a branch (`resume_state`, `state_at_entry`,
`latest_leaf_id`). `rio.coding.session_runner.SessionRunner` is the layer that
actually drives a run against that journal: it writes a `StepEntry` for every
committed step, folds a steering message into the next observation by
restarting the loop from the current state (nothing is lost, because the
state already holds everything the model would have been told), and
implements `restore(entry_id)` by reading that entry's state back out of the
journal and swapping it in wholesale. Read those two modules together to see
how a rio session actually persists and resumes -- there is no message log to
reconcile, only a state to read.

## Frontends

`rio.coding.rpc` is one such frontend: a JSONL protocol that starts and steers
runs, and exposes the session's live execution state (`get_execution_state`)
and its state journal (`get_entries`, `get_tree`, `get_checkpoints`,
`restore`) to an external process such as an editor integration. A CLI and a
Textual TUI are other frontends over the same `CodingSession`. None of them
own any session state themselves -- they all read and drive the one
`Sigma_t` the session already has.
