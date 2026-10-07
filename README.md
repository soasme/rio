# rio

Rio is a fully autonomous, one-shot coding agent.
It has no built-in interactive TUI, no conversation history, no human steer in the middle and no follow-up turns.
Start a task and Rio runs until the task completes or aborts.

## Getting Started

Install via `uv`:

```bash
uv tool install rio
```

## Usage

Login to your preferred providers:

* `rio login anthropic`
* `rio login github-copilot`
* `rio login codex`
* ...

Run a adhoc task:
```
rio run "Fix gh issue 123."
```

Rio keeps its State in SQLite and executes independent Python scripts through `uv`.
Committed work survives restart; interrupted scripts return `UnknownExecution` for
inspection. See [Architecture](docs/explanation/architecture.md).

Run a predefined workflow:

```
rio run task.md
```

The workflow is to write the task in a Markdown file, then run Rio
against it.


You can add a short instruction when invoking it:

```bash
uv run rio run "Follow feature.md, run the tests, and finish the implementation"
```

Use `--provider NAME --model MODEL` to choose a provider and a model.

Rio prints a compact human transcript by default. Use `--output json` for one JSON
event per line. Each human-mode run prints a session id; continue it with
`rio run --resume SESSION_ID`. Start a new run for a new task.

## Documentation

See the [documentation](docs/README.md) for tutorials, guides, command-line
reference, and an explanation of Rio's architecture.

## Development

```bash
uv run pytest
uv run ruff check .
```

To run the Lean 4 verification, install [elan](https://github.com/leanprover/elan)
and build the pinned toolchain from the repository root:

```bash
cd tests/lean && lake build
```

See [the Lean behavior models](tests/lean/README.md) for their scope and limits.

* `rio.ai` streams model responses from multiple providers.
* `rio.agent` owns SQLite state, immutable cells, workers, and recovery.
* `rio.coding` supplies provider configuration, credentials, and MCP clients.
* `rio.cli` runs coding tasks.
