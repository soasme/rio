# How Rio runs tasks

Rio builds structured execution state in the runtime while the model receives
an append-only history. Each step adds an accepted state patch and an action
observation.

```text
instructions + initial/rebuilt state + patches + observations
                         |
                         v
                    one action + state patch
```

The runtime applies RFC 7396 patches to build the state (plan, findings,
files, and blockers). At 80% of the model context window it replaces history
with one exact materialized-state record, then resumes appending. State fields
remain intentionally bounded so an agent can decide what to forget when its
rebuilt state needs more room.

The package boundaries follow that design:

| Package | Responsibility |
| --- | --- |
| `rio.ai` | Provider-neutral model streaming. |
| `rio.agent` | Structured-state runtime and step loop. |
| `rio.coding` | Coding skill, tools, sessions, resources, and CLI support. |
| `rio.cli` | Public one-shot command-line entry point. |

The coding layer journals committed state snapshots. Resuming loads the latest
snapshot and starts a fresh history baseline.
