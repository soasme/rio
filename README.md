# rio

Rio is a fully autonomous, one-shot coding agent built on
[*SKILL.state: Scalable Long-Horizon Agent Skills*](https://arxiv.org/abs/2608.26263).
It has no interactive terminal UI, no conversation history, and no follow-up
turns. Start a task, let it run, and Rio exits only when the task completes or
aborts.

Each model step receives the fixed skill instructions, current JSON execution
state, and the result of the previous action. The task is stored in the
initial state's `goal` field; it is never treated as a user observation.
Reasoning and prior messages are not replayed.

## Use

```bash
uv sync
uv run rio "Inspect this project and fix the parser"
uv run rio task.md
```

The normal workflow is to write the task in a Markdown file, then run Rio
against it. A sole `*.md` argument is loaded as the task:

```bash
uv run rio feature.md
```

You can add a short instruction when invoking it:

```bash
uv run rio "Follow feature.md, run the tests, and finish the implementation"
```

Use `--provider NAME --model MODEL` to choose a configured provider and
`--approve` to allow project instructions and extensions for that run. Rio
renders committed actions, state changes, retries, and the final result with
Rich.

## Development

```bash
uv run pytest
uv run ruff check .
```

`rio.ai` is a multi-provider LLM streaming SDK. `rio.agent` is the
SKILL.state runtime, and `rio.coding` supplies the autonomous coding skill and
tools. `rio.cli` is the public one-shot command-line entry point.
