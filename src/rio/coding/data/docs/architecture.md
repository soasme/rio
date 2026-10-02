# rio architecture

rio is a coding agent built on *Context Language Models* (arXiv:2609.37725):
the model manages its own context, and the context is a runnable Jupyter
notebook.

## The three packages

```text
rio.ai      provider/model streaming layer
rio.agent   CLM runtime: the notebook, patch checks, cell execution, the step loop
rio.coding  CLI app, resources, skills, extensions, commands, session journal
```

- `rio.ai`: providers, wire-level message/tool types, and the provider-neutral
  assistant stream event union. No knowledge of skills, notebooks, or sessions.
- `rio.agent`: the portable CLM runtime -- `HarnessSpec`, `Harness`,
  `run_notebook_loop`, `rio.agent.notebook`, and the step event stream. No
  knowledge of the coding domain or any frontend.
- `rio.coding`: the coding domain expressed as one skill (see
  `rio.coding.coding_skill`), plus resource discovery, project trust, the
  session journal, and frontends.

## The context is a notebook

The context is an nbformat v4 notebook kept as JSON. Each step the model sees
the whole notebook in one user message and replies with one `skill_step` call:

- `patch`: an RFC 6902 JSON Patch against the notebook;
- `reply` (optional): the answer to the user. Setting it ends the run, and the
  runtime appends it as a markdown cell with `metadata.rio.role = "assistant"`.

The runtime applies the patch and fills in the fields a new cell may omit
(`id`, `metadata`, `outputs`, `execution_count`). It rejects the reply through
the retry path when the patch fails, the result is not a valid notebook, cell
ids repeat, or the notebook would grow past the limit. A patch that shrinks an
oversized notebook is accepted.

Code cells the patch added or whose source changed then run through papermill
in the session's working directory. Each step starts a fresh kernel and replays
the cells before the last changed one, so earlier variables are defined. Only
changed cells, and replayed cells that fail, get new outputs; every other cell
keeps its own. Outputs lose terminal colors and binary data, and long text is
cut. Replayed cells repeat their side effects.

Cells are Python run by IPython: `!cmd` and `%%bash` run shell commands, and
plain Python reads, writes, and edits files. Markdown cells are notes. User
tasks are markdown cells with `metadata.rio.role = "user"`.

Validity is the runtime's job; strategy is the model's. The runtime never
summarizes or drops cells. The model keeps the notebook small by removing stale
outputs and cells.

## Sessions journal the notebook

The notebook is never stored outside the session file. A `StateResetEntry`
holds a whole notebook. Each committed step writes a `StepEntry` holding the
JSON Patch from the previous notebook to the new one (the model's edits, the
new outputs, and any user or reply cell), the cells that ran, and the reply.
Resuming replays the patches on the active branch; `--resume` appends the new
task as a user cell. Branching rebuilds the notebook at an earlier step and
journals it as a reset.

`rio.coding.session_store` defines the entry types and the tree utilities
(`notebook_at_entry`, `resume_notebook`, `latest_leaf_id`).
`rio.coding.session_runner.SessionRunner` drives a run against that journal.
