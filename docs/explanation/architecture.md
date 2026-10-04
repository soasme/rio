# How Rio runs tasks

Rio treats LLM context as a runnable Jupyter notebook and lets he model manages
its own context. The agent core is inspired by

* [SKILL.state](https://arxiv.org/html/2608.26263v2)
* [Context Language Models](https://arxiv.org/abs/2609.37725).

```text
            JSON Patch (skill_step)
notebook ───────────────────────────▶ patched notebook
   ▲                                       │ valid nbformat v4?
   │ outputs of the changed code cells     │ under the size limit?
   └──── the live kernel runs them ◀───────┘
```

Each step the model sees fixed instructions plus the whole notebook as JSON. It
replies with one `skill_step` call carrying an RFC 6902 JSON Patch. The runtime
applies the patch and checks that the result is a valid notebook that fits the
limit. If it is not valid, the runtime asks the model to retry. Then the code
cells the patch added or changed run in order; the first one that fails stops
the rest. Those cells get fresh outputs; every other cell keeps its own and
never runs again. The notebook with the new outputs is the next step's context.

All cells run in one IPython kernel that lives for the session, so variables
build up across steps. Rio's own executor (`KernelExecutor`, built on nbclient)
keeps that kernel alive; it talks to it over local sockets. A resume, rewind,
or new session starts an empty kernel.
Each session's kernel uses a private temporary Python environment. Packages
installed with pip from a cell go into that environment and remain available
after a kernel restart. Closing the session removes the environment.

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

A cell can read and write files with plain Python, run `!cmd` or `%%bash`, or
change part of a file with `%%edit`:

```text
%%edit calc.py
<<<<<<< SEARCH
    return a - b
=======
    return a + b
>>>>>>> REPLACE
```

Each search text must match the file exactly once, blocks must not overlap,
and nothing is written unless every block applies. The cell prints a diff.

The model manages the notebook itself. Each step it reviews every cell and
decides one of:

| Decision | Patch |
| --- | --- |
| Keep | No change. |
| Summarize | Replace the cell with a markdown note: decisions, file paths, exact values, and a link to the archived cell. |
| Delete | Remove the cell; only when nothing in it matters. |

A coding session gives the runtime a temporary archive directory. Each cell
whose id leaves the notebook is saved there as `<id>.json`, source and outputs,
and the prompt names the directory. Because the summary links that file, the
cell's full content stays readable from a code cell without staying in context.
The directory is deleted when the session closes.

The runtime never summarizes. It only caps each new output, archives removed
cells, and rejects a patch that grows the notebook past the limit. A step
that sets `reply` ends the run, and the reply is kept as a markdown cell.

The package boundaries follow that design:

| Package | Responsibility |
| --- | --- |
| `rio.ai` | Provider-neutral model streaming. |
| `rio.agent` | Runtime: the notebook, patch checks, cell execution, and the step loop. |
| `rio.coding` | Coding skill, sessions, resources. |
| `rio.cli` | Public one-shot command-line entry point. |

The coding layer journals each step as the JSON Patch from the previous
notebook to the new one, outputs included. The notebook is never stored
elsewhere: resuming replays the patches on the active branch and appends the
new task as a user cell.
