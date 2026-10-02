# rio CLI

rio drives one `rio.coding.session.CodingSession` from a one-shot print-mode CLI. The CLI entry point is `rio.cli:app` (script name `rio`).

## Commands

```bash
rio login anthropic --method api-key
rio run --provider anthropic --model MODEL "Explain this project"
rio run --resume SESSION_ID "Continue with the next task"
```

`rio login PROVIDER` saves credentials for a configured or built-in provider.
Add an OpenAI-compatible provider directly to `~/.rio/providers.json`:

```json
{
  "default_provider": "local",
  "providers": [
    {
      "type": "openai-compatible",
      "name": "local",
      "base_url": "http://127.0.0.1:8080/v1",
      "api": "openai-completions",
      "api_key_env": "LOCAL_API_KEY",
      "credential_name": null,
      "models": ["qwen3-coder"],
      "default_model": "qwen3-coder"
    }
  ]
}
```

Set `LOCAL_API_KEY` before running rio. Use `openai-responses` for providers
that implement the Responses API.
`rio run --thinking LEVEL` selects reasoning effort. `--approve` allows ambient
project resources for the run; `--no-approve` disables them. Each `rio run`
creates a durable session. Human output prints its id; pass it to `--resume`
to load its context and continue it. Use `--output json` for one JSON event per line;
the default `human` format prints messages and tool invocations.

## Thinking level

`-t/--thinking LEVEL` sets the initial reasoning-effort level for a run (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`; see `rio.coding.thinking`). An unsupported level for the selected model is an error listing the levels that model supports.

## Safety boundary

Project trust controls ambient project-resource loading; it is not a sandbox. See `security.md`.
