# rio terminal interface

Run `rio` to open the conversation application. No API key is needed to open it;
use `/login` to choose a provider. Sign-in questions appear in the prompt area.
Browser sign-in also accepts a pasted redirect URL. Escape cancels a question.

The conversation grows upward from the prompt. Answers render as Markdown, and
each tool invocation expands independently. Edits display unified or split diffs.
Right-click a conversation block to copy it or edit a previous prompt.

- Enter sends; Shift+Enter or Ctrl+J inserts a line.
- Up/Down browse prompt history at the first/last editor line.
- `/` opens command completion; Tab or Enter accepts a completion.
- `!` enters shell mode. Commands run in inline interactive terminals. Escape
  returns focus to the prompt or leaves shell mode; Ctrl+C interrupts a terminal.
  Completed output stays in the conversation, and `cd` updates the shell directory.
- Ctrl+B toggles the collapsible plan/project sidebar.
- Ctrl+F finds files. Type a fuzzy path, Enter attaches it, and Ctrl+O previews it.
- Ctrl+N opens a session. Ctrl+R resumes one. Ctrl+[ and Ctrl+] switch tabs;
  Ctrl+W closes a tab. Background runs continue, and drafts stay with their tabs.
- Ctrl+K opens commands. Ctrl+, opens settings. Ctrl+D exits.

Settings include the theme, conversation column, sidebar, thinking visibility,
completion bell, and diff layout. They are saved in `~/.rio/tui.json`.

`/model`, `/provider`, and `/thinking` select backend configuration. `/skills` and
`/prompts` choose project resources. `/name TITLE` renames a session. `/checkpoints`
and `/restore ID` inspect and restore execution state. `/diff` reviews working-tree
changes; press S to switch to staged changes. `/state` displays raw execution state.

Input submitted during a run steers its next step. Escape interrupts the active
run. Resuming restores the active journal branch for display and the latest state
for the model. The displayed conversation is never replayed as model memory.
Restoring a checkpoint does not undo filesystem edits or shell commands.

Extensions use the same backend session as built-in tools. Their questions appear
inline; their optional components receive the application's Textual theme.
Headless sessions have no visual theme. See [extensions](extensions.md).
