# Coding TUI messages

The messages panel uses a bullet (`•`) for each new message or tool invocation,
with a blank line between entries. Runtime step numbers remain in the state
sidebar for debugging; step-start events do not create transcript entries.
Tool invocations show `Ran <command>` when a command argument is available,
otherwise the tool name and arguments. Results follow with `└` and an indented
body, retaining error colors. All content is literal text, including brackets.

```text
• Ran gh run watch 34028991259 --exit-status --interval 10
  └ Refreshing run status every 10 seconds.

• The tests passed.

• Working (2m 12s • escape to interrupt)
  └ git status --short
```

A transient row beneath the scrollable history updates once a second using a
monotonic clock. It starts when a prompt is submitted and disappears when the
worker finishes, including errors and cancellation. It shows the configured
cancel key and the current action while that action is executing. Updates replace
the row instead of appending repeated Working messages to history.

Rio currently executes tools in the foreground and exposes no background-terminal
registry, `/ps`, or `/stop`. The row therefore displays the active action without
claiming a background-terminal count or offering those commands. If background
execution is added, its registry should supply counts, commands, and lifecycle
state to this same row.

The transcript wraps to the panel's available width, including unbroken URLs and
JSON. Continuations align beneath message text or tool output. Resizing reflows
retained entries; horizontal scrolling is disabled. Scrolling up preserves the
reader's position as new entries arrive. The panel retains at most 1,000 entries;
tool results retain the existing 8,000-character limit with an explicit truncation
notice. No transcript-expansion shortcut is advertised because none exists yet.
The state sidebar and extension main-view replacement keep their existing roles.

Tests cover event formatting, state retention, tool errors, wrapping after resize,
literal text, elapsed time, active commands, and worker cleanup, alongside the
existing interactive TUI tests.
