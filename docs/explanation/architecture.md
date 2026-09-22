# How Rio runs tasks

Rio is built around a structured execution state rather than a growing chat
transcript. Each step receives the skill instructions, the current state, and
the latest observation. It produces one action and a state update.

```text
instructions + state + observation
                 |
                 v
          one action + state update
                 |
                 v
             observation
```

The state records the task plan, findings, changed files, and blockers. That
allows Rio to resume a session without replaying a long conversation, while
keeping the model prompt bounded.

The package boundaries follow that design:

| Package | Responsibility |
| --- | --- |
| `rio.ai` | Provider-neutral model streaming. |
| `rio.agent` | Structured-state runtime and step loop. |
| `rio.coding` | Coding skill, tools, sessions, resources, and CLI support. |

The coding layer journals committed state snapshots. Resuming a session loads
the latest snapshot and continues from it; it does not reconstruct a chat
history.
