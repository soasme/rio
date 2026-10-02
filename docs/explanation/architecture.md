# How Rio runs tasks

Rio is a Context Language Model (CLM) agent ([arXiv:2609.37725]): the model
natively manages its own context, and the context is a file.

```text
           write before each step
context ──────────────────────────▶ CONTEXT.md
   ▲                                    │  model edits it with
   │  read back after the action        │  edit / write / bash
   └────────────────────────────────────┘
```

Each step the model sees fixed instructions plus its context, replies with its
reasoning and one tool call, and the runtime runs the tool. The runtime then
reads the context file back. If the model edited it and the result fits the
limit, the edited turns become the context. The step's reply and observation
are appended either way.

The runtime never summarizes. Every observation ends with the current context
size, and near the limit it asks the model to compact. If the context still
outgrows the limit, the runtime withholds the oldest non-user turns so the next
request fits.

The package boundaries follow that design:

| Package | Responsibility |
| --- | --- |
| `rio.ai` | Provider-neutral model streaming. |
| `rio.agent` | CLM runtime: the context file and the step loop. |
| `rio.coding` | Coding skill, tools, sessions, resources, and CLI support. |
| `rio.cli` | Public one-shot command-line entry point. |

The coding layer journals the context after every step. Resuming loads the
latest snapshot and appends the new task to it.

[arXiv:2609.37725]: https://arxiv.org/abs/2609.37725
