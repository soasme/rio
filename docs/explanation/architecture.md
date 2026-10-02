# How Rio runs tasks

Rio is a Context Language Model (CLM) agent ([arXiv:2609.37725]): the model
manages its own context, and the context is a runnable Jupyter notebook.

```text
            JSON Patch (skill_step)
notebook ───────────────────────────▶ patched notebook
   ▲                                       │ valid nbformat v4?
   │ outputs of the changed code cells     │ under the size limit?
   └──────── papermill runs them ◀─────────┘
```

Each step the model sees fixed instructions plus the whole notebook as JSON. It
replies with one `skill_step` call carrying an RFC 6902 JSON Patch. The runtime
applies the patch and checks that the result is a valid notebook that fits the
limit. If it is not valid, the runtime asks the model to retry. Then papermill
runs the code cells the patch added or changed. Those cells get fresh outputs;
every other cell keeps its own. The notebook with the new outputs is the next
step's context.

Cells are Python run by IPython, so a cell can read, write, and edit files, run
`!cmd` or `%%bash`, or compute anything else. Each step starts a fresh kernel
and replays the cells before the last changed one, so the variables they define
are available. Replayed cells repeat their side effects.

The model manages the notebook itself: it removes stale outputs and cells and
keeps notes in markdown cells. The runtime never summarizes. It only caps each
new output and rejects a patch that grows the notebook past the limit. A step
that sets `reply` ends the run, and the reply is kept as a markdown cell.

The package boundaries follow that design:

| Package | Responsibility |
| --- | --- |
| `rio.ai` | Provider-neutral model streaming. |
| `rio.agent` | CLM runtime: the notebook, patch checks, cell execution, and the step loop. |
| `rio.coding` | Coding skill, sessions, resources, and CLI support. |
| `rio.cli` | Public one-shot command-line entry point. |

The coding layer journals each step as the JSON Patch from the previous
notebook to the new one, outputs included. The notebook is never stored
elsewhere: resuming replays the patches on the active branch and appends the
new task as a user cell.

[arXiv:2609.37725]: https://arxiv.org/abs/2609.37725
