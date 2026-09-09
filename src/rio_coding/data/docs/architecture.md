# rio architecture

rio is a SKILL.state coding agent: *SKILL.state: Scalable Long-Horizon Agent
Skills* (arXiv:2608.26263). That paper's design choice is the thing to
understand before touching any of the three packages below, because it is
the reason rio's session layer looks nothing like a typical chat-transcript
agent.

## The three packages

```text
rio_ai      provider/model streaming layer
rio_agent   SKILL.state runtime (instructions + execution state + observation)
rio_coding  CLI app, resources, skills, extensions, commands, TUI
```

- `rio_ai`: providers, wire-level message/tool types, and the provider-neutral
  assistant stream event union. No knowledge of skills, state, or sessions.
- `rio_agent`: the portable SKILL.state runtime -- `HarnessSpec`, `Harness`,
  `run_skill_loop`, state-delta validation, and the step event stream. No
  knowledge of the coding domain, the filesystem, or any frontend.
- `rio_coding`: the coding domain expressed as one SKILL.state skill (see
  `rio_coding.coding_skill`), plus everything a coding agent needs that isn't
  the runtime itself: resource discovery, tools, project trust, the session
  journal, and frontends (CLI, RPC, TUI).

Keep `rio_agent` free of Typer, Rich, Textual, filesystem layout assumptions,
and coding-specific vocabulary. `rio_coding` is where all of that lives.

## Sessions hold state, not a transcript

A conventional coding agent keeps an append-only list of every message ever
exchanged, replays that list into the model on each turn, and eventually has
to compact or summarize it once it grows too large. rio's session does none
of this. Each step, the model is given exactly three things:

1. `P` -- the skill's instructions (the system prompt), fixed for the whole
   run.
2. `Sigma_t` -- a structured JSON execution state: the goal, a plan, findings,
   touched files, blockers, and so on (see `rio_coding.coding_skill` for the
   coding skill's exact schema).
3. `O_t` -- the latest observation, one `rio_agent.HarnessObservation`
   carrying what arrived and what kind of thing it is: the result of the one
   action the previous step took, a message from the user, or both when a
   message interrupts a run. Each gets its own labelled section of the
   prompt. A turn's first step observes only a message -- no action has run
   yet.

The model answers with a single mandatory tool call carrying `(reasoning,
state_delta, action)`. The runtime validates the delta, merges it into the
state (`Sigma_{t+1} = Sigma_t (merge) DeltaSigma_t`, an RFC 7396 JSON merge
patch), **discards the reasoning permanently**, executes the one action, and
feeds its result back as the next observation.

Three consequences follow directly, and they are what make rio's session code
look different from a transcript-based one:

- **No conversation history.** Nothing a later step needs is "remembered" by
  being in a message list somewhere -- if it matters, it has to be written
  into the execution state. `rio_coding.session.CodingSession.prompt()` hands
  the user's text to the runtime as the first step's input. It is journaled
  for audit, but previous turns are never appended to future model prompts. A
  follow-up message therefore continues the session through the state alone:
  the state carries over untouched, and the run's answer is the terminating
  action's message rather than a field a later turn could read as its own.
- **No context compaction.** Compaction exists to bound a transcript that
  grows without limit. rio's per-step prompt is `P` + `Sigma_t` + `O_t`, and
  previous turns are excluded. `rio_coding.step_footprint` estimates the
  current prompt cost. The paper's `O(T)` cumulative scaling assumes bounded
  state and observations, so the state carries a size budget of its own
  (`HarnessSpec.state_budget_chars`, a share of the model's context window):
  a delta that would push the state past it is rejected the same way an
  invalid one is, and the model frees space before retrying. The cumulative
  projection assumes the current footprint stays unchanged.
- **Exactly one action per step.** There is no multi-tool assistant turn and
  no parallel tool call. One step, one action, one observation.

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

`rio_coding.session_store` defines the journal entry types (`TurnEntry`,
`StepEntry`, `StateResetEntry`, and a handful of bookkeeping entries for
model/thinking-level changes and labels) and the tree utilities that find the
latest checkpoint on a branch (`resume_state`, `state_at_entry`,
`latest_leaf_id`). `rio_coding.session_runner.SessionRunner` is the layer that
actually drives a run against that journal: it writes a `StepEntry` for every
committed step, folds a steering message into the next observation by
restarting the loop from the current state (nothing is lost, because the
state already holds everything the model would have been told), and
implements `restore(entry_id)` by reading that entry's state back out of the
journal and swapping it in wholesale. Read those two modules together to see
how a rio session actually persists and resumes -- there is no message log to
reconcile, only a state to read.

## Frontends

`rio_coding.rpc` is one such frontend: a JSONL protocol that starts and steers
runs, and exposes the session's live execution state (`get_execution_state`)
and its state journal (`get_entries`, `get_tree`, `get_checkpoints`,
`restore`) to an external process such as an editor integration. A CLI and a
Textual TUI are other frontends over the same `CodingSession`. None of them
own any session state themselves -- they all read and drive the one
`Sigma_t` the session already has.
