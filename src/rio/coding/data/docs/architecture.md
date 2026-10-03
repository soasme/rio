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
  `run_context_loop`, `rio.agent.notebook`, and the step event stream. No
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

Code cells the patch added or whose source changed then run in order, in the
session's working directory; the first one that fails stops the rest. All cells
share one IPython kernel that lives for the session (`rio.agent.KernelExecutor`,
built on nbclient), so variables build up across steps and no cell runs twice
unless its source changes. A resume, rewind, or new session starts an empty
kernel. Only the cells that ran get new outputs; every other cell keeps its own.
Outputs lose terminal colors and binary data, and long text is cut.

The notebook records the kernel. `metadata.rio.kernel` holds the current
kernel's `id` and whether it is `running`. Each code cell that ran has
`metadata.rio.kernel` set to the id of the kernel that ran it, and
`metadata.rio.defines` listing the names it bound. The kernel records those
names itself (`rio.agent.kernel_ext` compares the namespace before and after
the cell), so names made by `exec` count and a function's locals do not.

A new kernel gets a new id, so after a resume, rewind, or new session every
cell that ran before is stale: its variables, open files, and subprocesses are
gone. Each request lists the stale cells. A cell may stay stale, but reading
its variables is stopped twice:

- Before running, a patch is rejected when a cell it runs reads a name that
  only stale cells defined. Reads are found statically, after IPython turns
  magics and `!cmd` into Python.
- In the kernel, each such name is bound to a `StaleValue` placeholder. Almost
  any use of it raises `StaleVariableError`, naming the cell to run again. This
  catches the reads the static check misses: `globals()[...]`, `eval`, `exec`.

Removing a cell's stamp (`{"op": "remove", "path": "/cells/3/metadata/rio/kernel"}`)
runs it again without editing it, so the model can re-run a stale cell before
the cells that read its variables, in the same patch.

Cells are Python run by IPython: `!cmd` and `%%bash` run shell commands, plain
Python reads and writes files, and the `%%edit PATH` cell magic
(`rio.coding.edit_magic`) applies SEARCH/REPLACE blocks with the `edit` tool's
rules. Markdown cells are notes. User tasks are markdown cells with
`metadata.rio.role = "user"`.

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
