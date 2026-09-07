# Terminal application

```text
┌ Plan / Project ┐  ┌ Session tabs ──────────────────────┐
│ Collapsible    │  │                                     │
│ sidebar        │  │ User message                        │
│                │  │ ▸ Tool invocation · done            │
│                │  │ Markdown answer / inline terminal   │
│                │  ├─────────────────────────────────────┤
│                │  │ ❯ Multiline prompt                  │
└────────────────┘  │ Model   Project directory   Status  │
                    └─────────────────────────────────────┘
```

`rio_tui` is a new conversation application. It does not use the prototype's
transcript renderer, state dashboard, event adapter, themes, or dialogs.
The conversation grows upward from the prompt. The optional sidebar holds the
plan and project tree; raw execution state is available through `/state`.

Each tool invocation is a separate expandable widget. Answers are native Markdown
widgets, edits use diff views, and shell commands run in inline PTYs. Tool output
and shell output remain selectable. Right-click a block to copy it or edit a
previous prompt. Extension questions replace the editor inline and restore it
when answered or cancelled.

Each session screen owns its coding session, workers, prompt draft, bridge, and
conversation. Background runs continue while another tab is active. Resume loads
the active journal branch for display and prompt history. The model still sees
only its execution state. The initial backend remains owned by the CLI; additional
session providers are owned and closed by the application.

`rio_coding` owns model providers, tools, extensions, and journals. `rio_tui` owns
all interaction and rendering. There is no external-agent installation or launch
catalog in the application.
