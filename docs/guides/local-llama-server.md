# Use a local llama-server model

Rio can use a running `llama-server` through its OpenAI-compatible API. Rio
does not start or discover the server; start it before running Rio.

## Add the server to the catalog

Create or update `~/.rio/catalog.toml`. Replace `MODEL_ID` with the model ID
reported by your server's `/v1/models` endpoint.

```toml
schema_version = 1

[[providers]]
name = "local-llama"
display_name = "Local llama-server"
kind = "openai-compatible"
base_url = "http://127.0.0.1:8080/v1"
api_key_env = "LLAMA_SERVER_API_KEY"
models = ["MODEL_ID"]
default_model = "MODEL_ID"
docs_url = "https://github.com/ggml-org/llama.cpp"
```

The default `llama-server` endpoint is `http://127.0.0.1:8080`; Rio needs the
OpenAI-compatible `/v1` base URL. Change the host or port in `base_url` if your
server uses a different endpoint.

## Set the API-key variable

Rio requires an API-key environment variable for OpenAI-compatible providers.
If your local server does not validate a key, any non-empty placeholder works:

```bash
export LLAMA_SERVER_API_KEY=local
```

If the server is configured to require authentication, set this variable to its
actual key instead.

## Run Rio

Select the configured provider and model:

```bash
rio run --provider local-llama --model MODEL_ID "Explain this project."
```

Keep the server bound to a trusted network interface. A local model server is
not a sandbox for the code Rio is asked to inspect or change.
