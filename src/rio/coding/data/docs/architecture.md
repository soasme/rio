# rio architecture

rio is a coding agent built on *Context Language Models* (arXiv:2609.37725):
the model natively manages its own context, and the context is a file.

## The three packages

```text
rio.ai      provider/model streaming layer
rio.agent   CLM runtime: the context file and the step loop
rio.coding  CLI app, resources, skills, extensions, commands, session journal
```

- `rio.ai`: providers, wire-level message/tool types, and the provider-neutral
  assistant stream event union. No knowledge of skills, contexts, or sessions.
- `rio.agent`: the portable CLM runtime -- `HarnessSpec`, `Harness`,
  `run_context_loop`, the context file format, and the step event stream. No
  knowledge of the coding domain or any frontend.
- `rio.coding`: the coding domain expressed as one skill (see
  `rio.coding.coding_skill`), plus resource discovery, tools, project trust,
  the session journal, and frontends.

## The context is a file

The context is a list of turns, each `{"role", "text"}`. Before every step the
runtime writes it to a private file, one `[[CTX_TURN <i> role=<role>]]` block
per turn, and names that file in the system prompt. Each step the model replies
with its reasoning and one tool call. The runtime runs the tool, then reads the
file back:

- unchanged: the step's reply and observation are appended;
- edited and within the limit: the edited turns replace the context, then the
  step's reply and observation are appended;
- edited but over the limit: the edit is rejected with a note.

The model edits the file with its ordinary tools (`edit`, `write`, `bash`). It
may shorten stale output, delete dead ends, reorder turns, or add turns with
any role label. Text before the first header becomes a `notes` turn and a turn
with an empty body is dropped. Header-shaped lines inside a body are escaped,
so a tool result that quotes the file cannot inject turns.

Validity is the runtime's job; strategy is the model's. The runtime never
summarizes. It reports the context size after every observation and asks the
model to compact near the limit. If the model lets the context outgrow the
limit, the runtime withholds the oldest non-user turns so the next request
still fits.

## Sessions journal the context

Each committed step writes a `StepEntry` holding its action, observation, and
the full context that resulted. Resuming reads the newest snapshot on the
active branch; `--resume` appends the new task to that context. A checkpoint is
any entry with a snapshot, and branching adopts that snapshot as the live
context. There is no replay.

`rio.coding.session_store` defines the entry types and the tree utilities that
find the latest snapshot (`state_at_entry`, `latest_leaf_id`, `entry_context`).
`rio.coding.session_runner.SessionRunner` drives a run against that journal.
