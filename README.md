# rio

Rio is a fully autonomous, one-shot coding agent. It has no interactive terminal UI,
no conversation history, no human steer in the middle and no follow-up turns.
Start a task, let it run, and Rio exits only when the task completes or aborts.

## Use

Install via `uv`:

```bash
uv tool install rio
```

Run a adhoc task:
```
rio "Fix gh issue 123."
```

Run a predefined workflow:

```
rio task.md
```

The workflow is to write the task in a Markdown file, then run Rio
against it.


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

* `rio.ai` is a multi-provider LLM streaming SDK.
* `rio.agent` is the [SKILL.state] runtime.
* `rio.coding` supplies the autonomous coding skill and tools.
* `rio.cli` is the public one-shot command-line entry point.

[SKILL.state]: https://arxiv.org/abs/2608.26263
