# rio

Four packages:

- **`rio_ai`** -- a multi-provider LLM streaming SDK (Anthropic, Google
  Gemini, Mistral, OpenAI Codex, OpenAI-compatible), ported from
  [huggingface/tau](https://github.com/huggingface/tau)'s `tau_ai` module.
  The message/tool/type vocabulary `tau_ai` depends on (`tau_agent.messages`,
  `tau_agent.tools`, `tau_agent.types`, `tau_agent.provider`,
  `tau_agent.provider_events` upstream) is folded in here too, so `rio_ai`
  is self-contained. See [`NOTICE`](NOTICE) for the upstream MIT license.
- **`rio_agent`** -- a from-scratch runtime implementing
  [*SKILL.state: Scalable Long-Horizon Agent Skills*](https://arxiv.org/abs/2608.26263)
  (Badhe, Tiwari & Chung; EMNLP), built on `rio_ai`.
- **`rio_coding`** -- the coding backend (CLI, tools, skills, extensions), built on `rio_agent`.
- **`rio_tui`** -- the terminal application, wired to `rio_coding`. It owns
  session tabs, the prompt editor, shell, file navigation, diffs, and settings.

## Coding agent

```bash
uv sync
uv run rio setup --provider local --base-url http://localhost:8080/v1 --model my-model
uv run rio                         # interactive terminal interface
uv run rio -p "Inspect this project and explain its entry point"
uv run rio -p --mode json "Add a regression test for the parser"
uv run rio --mode rpc              # JSONL commands on stdin
uv run rio sessions
uv run rio --session SESSION_ID
uv run rio export SESSION_ID --format html
```

Choose an existing provider with `--provider NAME --model MODEL`; `rio providers`
lists the configured catalog. Credentials may come from the provider's environment
variable or Rio's credential store (`rio login PROVIDER`; use `--method api-key`
for an API key). A successful login remembers the provider for future launches.
API keys and OAuth tokens are stored in `~/.rio/credentials.json` with owner-only
permissions; the file is unencrypted. OAuth tokens refresh automatically.
Use `rio logout PROVIDER` to remove saved credentials. Configuration and journals
live under `~/.rio`.
Use `--approve` to allow ambient project instructions and extensions for a run;
project trust controls resource loading, not what shell commands can access.

The coding tools are `read`, `write`, `edit`, `bash`, and `respond`. Each step
updates structured state and executes one action. Sessions store snapshots for
resume and checkpoint restoration; they never replay a transcript into the model.
HTML exports show steps, final state, and estimated token footprints.

See the [installed documentation](src/rio_coding/data/docs/README.md) for
providers, skills, extensions, RPC, and the state-based runtime. The
[offline example](src/rio_coding/data/examples/offline_session.py) runs a complete
session with a fake provider and requires no credentials.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
```

Tests use scripted providers (`rio_ai.FakeProvider`) and mocked HTTP; no live
provider credentials are required. Headless Textual tests exercise terminal
interaction, and tool integration tests work in temporary directories. See in
particular `tests/test_rio_agent_loop.py::test_prompt_footprint_is_bounded_across_steps`,
which asserts that per-step prompt size stays constant across many steps
rather than growing -- a direct runtime check of the paper's core claim.
