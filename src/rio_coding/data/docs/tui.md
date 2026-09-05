# rio terminal interface

Run `rio` to open the Textual interface. No API key is required to open it;
use `/login` to choose a provider and save credentials. Credentials are resolved
when a task first needs the model, so no restart is needed after login.

 The left pane shows live actions and
observations; the sidebar shows the current execution state and prompt footprint.
The display is not replayed into model prompts. Prompt cost depends on current
state and observation size, not on the number of displayed events.

Enter a task in the input field. Input submitted during a run steers the next
step; Escape cancels the active run. Ctrl+D exits.

- `/state`, `/session`, `/tools`, `/system`, `/diagnostics`: inspect the session.
- `/model`, `/provider`, `/thinking`: select runtime configuration.
- `/skills`, `/prompts`: choose a discovered skill or prompt template.
- `/sessions`, `/resume ID`, `/new`, `/name TITLE`: manage sessions.
- `/checkpoints`, `/restore ID`: inspect and adopt state snapshots.
- `/theme`: choose and save a terminal theme.
- `/local`: configure and manage registered local inference backends.
- `/login PROVIDER`, `/logout PROVIDER`: manage credentials.
- `/reload`: rediscover resources and extensions between runs.
- `/clear`, `/cancel`, `/quit`: control the display and active work.

Model and provider changes through the configured frontend construct a new
provider and preserve execution state. Checkpoint restoration changes the
agent's remembered state; it does not undo filesystem edits or shell commands.

Extension tools and commands use the same session and runtime as built-in tools.
See [extensions](extensions.md) for their registration and trust boundaries.
