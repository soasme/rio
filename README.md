# rio

Three packages:

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
- **`rio_coding`** -- a coding agent (CLI, TUI, tools, skills, extensions)
  ported from tau's `tau_coding` and retargeted onto `rio_agent`. Installs the
  `rio` command.

## Why rio_agent exists

Long-horizon LLM agents traditionally append every observation, action, and
reasoning trace to a growing conversation, so per-step prompt size is
`O(t)` and cumulative tokens over `T` steps are `O(T^2)`. SKILL.state
replaces that with three fixed inputs per step:

1. an **immutable procedural skill specification** `P` (authored once per
   domain, not per task),
2. a **structured execution state** `Σ_t` (a plain JSON object), and
3. the **latest observation** `O_t` only -- no history.

Each step, the model must respond with one `skill_step` tool call carrying
`(R_t, ΔΣ_t, a_t)`: private reasoning, a state delta, and an action. The
runtime validates `ΔΣ_t` and `a_t` deterministically (a rollback-retry cycle
follows an invalid proposal), commits `Σ_{t+1} = Σ_t ⊕ ΔΣ_t` via JSON Merge
Patch (RFC 7396: `null` deletes a field, an object merges recursively,
anything else replaces), executes `a_t`, and **discards `R_t` forever** --
it is never replayed. That keeps per-step prompt size at
`O(|P| + |Σ| + |O|)`, independent of how many steps have already run, so
cumulative tokens over `T` steps are `O(T)` when state and observations remain
bounded. Rio excludes history from prompts; it does not enforce a hard byte
limit on the model-authored state.

`rio_agent`'s public interface -- a bare async-generator loop
(`run_skill_loop`) plus a stateful wrapper (`Harness`) that both
emit a typed event stream and can be `subscribe()`d to -- deliberately
mirrors the shape of `rio_ai`'s ported tau_agent-style `run_agent_loop` /
`AgentHarness`, so the loop interface should feel familiar; only what's
inside the loop differs.

```python
from rio_ai import AgentTool, AgentToolResult, FakeProvider, TextContent
from rio_agent import HarnessSpec, Harness, HarnessConfig


async def run_shell(tool_call_id, arguments, signal=None, on_update=None):
    output = ...  # execute arguments["command"] in the sandbox
    return AgentToolResult(content=[TextContent(text=output)])


skill = HarnessSpec(
    name="ctf-solver",
    instructions="You are solving an InterCode CTF challenge. ...",
    state_fields=("discovered_flags", "tested_hypotheses", "active_files", "working_dir"),
    initial_state={"discovered_flags": [], "tested_hypotheses": [], "working_dir": "/root"},
    actions=(
        AgentTool(
            name="run_shell",
            label="Run shell",
            description="Execute a shell command in the challenge container.",
            parameters={"type": "object", "properties": {"command": {"type": "string"}}},
            execute_fn=run_shell,
        ),
    ),
)

harness = Harness(HarnessConfig(provider=my_provider, model="claude-sonnet-5", skill=skill))
async for event in harness.run("Challenge files are in /root/ctf."):
    ...
```

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
for an API key). Configuration and journals live under `~/.rio`.
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
