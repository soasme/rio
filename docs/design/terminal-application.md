# Terminal application

`rio_tui` is a TUI application. 

`rio_coding` owns model providers, tools, extensions, and journals.
`rio_tui` owns all interaction and rendering. 

Overall look:

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

Key design items:

* The conversation grows upward from the prompt.
* The optional sidebar holds the plan and project tree.
* Raw execution state is available through `/state`.
* Each tool invocation is a separate expandable widget.
* Answers are native Markdown widgets.
* Edits use diff views, unified or split.
* Shell commands run in inline PTYs.
* Tool output and shell output remain selectable.
* Right-click a block to copy it or edit a previous prompt.
* Extension questions replace the editor inline and restore it when answered or cancelled.
* Each session screen owns its coding session, workers, prompt draft, bridge, and
  conversation.
* Background runs continue while another tab is active.
* Resume loads the active journal branch for display and prompt history.
* The model sees only its execution state.
* The initial backend remains owned by the CLI; additional
  session providers are owned and closed by the application.

Startup:

* `rio` opens the application. No API key is needed to open it.
* `/login` chooses a provider. Sign-in questions appear in the prompt area.
* Browser sign-in also accepts a pasted redirect URL. Escape cancels a question.

Keys:

* Enter sends; Shift+Enter or Ctrl+J inserts a line.
* Up/Down browse prompt history at the first/last editor line.
* `/` opens command completion; Tab or Enter accepts a completion.
* `!` enters shell mode. Escape returns focus to the prompt or leaves shell mode;
  Ctrl+C interrupts a terminal. Completed output stays in the conversation, and
  `cd` updates the shell directory.
* Ctrl+B toggles the sidebar.
* Ctrl+F finds files. Type a fuzzy path, Enter attaches it, and Ctrl+O previews it.
* Ctrl+N opens a session. Ctrl+R resumes one. Ctrl+[ and Ctrl+] switch tabs;
  Ctrl+W closes a tab. Drafts stay with their tabs.
* Ctrl+K opens commands. Ctrl+, opens settings. Ctrl+D exits.

Commands:

* `/model`, `/provider`, and `/thinking` select backend configuration.
* `/skills` and `/prompts` choose project resources.
* `/name TITLE` renames a session.
* `/checkpoints` and `/restore ID` inspect and restore execution state.
* `/diff` reviews working-tree changes; press S to switch to staged changes.
* `/state` displays raw execution state.

Settings:

* Theme, conversation column, sidebar, thinking visibility, completion bell, and
  diff layout.
* Saved in `~/.rio/tui.json`.

Runs and state:

* Input submitted during a run steers its next step. Escape interrupts the active run.
* Resuming restores the active journal branch for display and the latest state for
  the model. The displayed conversation is never replayed as model memory.
* Restoring a checkpoint does not undo filesystem edits or shell commands.

Extensions:

* Extensions use the same backend session as built-in tools.
* Their questions appear inline; their optional components receive the
  application's Textual theme. Headless sessions have no visual theme.
