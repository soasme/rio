# rio extensions

rio extensions are Python modules that register custom tools, slash commands, and provider definitions, and that can observe session lifecycle events. An extension defines `setup(rio)` and uses the documented registration APIs; it does not reach into private session or Textual internals.

## Locations

- `~/.rio/extensions/`: discovered by default.
- `<project>/.rio/extensions/`: requires project approval (see `security.md`).
- `rio -e PATH`: explicitly load a file or directory.

## Trust

Project extensions execute arbitrary Python and remain disabled without explicit project-trust approval; they cannot approve themselves. Built-in extensions, by contrast, ship inside the installed rio package and are trusted because their code is not discovered from the working directory. See `security.md` for the trust boundary these decisions sit inside.

## Development checklist

1. Read this document completely before implementing an extension.
2. Confirm the requested capability exists in the extension API before inventing a workaround.
3. Define `setup(rio)` and use documented registration APIs.
4. Keep extension behavior in `rio_coding`, not `rio_agent` -- the runtime stays domain- and frontend-free (see `architecture.md`).
5. Put user extensions in `~/.rio/extensions/`. Project extensions require explicit trust; never enable one from an untrusted repository. Use `rio -e PATH` for isolated testing.
6. Test through the real extension runtime so discovery, imports, and `setup` registration are exercised. Add deterministic tests with fake providers/tools.
7. Run the repository's full pytest, Ruff, and formatting checks.


## Runtime and UI

Extensions register `AgentTool` actions and synchronous slash-command handlers
through the setup API. Input hooks can transform a task before execution; step
hooks observe copied event data. Journal entries can store extension-owned data,
but prior events and reasoning are never replayed into model prompts.

In the terminal frontend, `ctx.ui` supports notifications, selection and input
dialogs, prompt-adjacent widgets, sidebar sections, and a custom main view.
Check `has_ui`, `supports_components`, and `supports_sidebar` before using host
capabilities. Startup can occur before the terminal mounts, and print/RPC hosts
do not provide interactive UI. Reload removes the old generation's widgets and
registrations before loading the replacement extension.
