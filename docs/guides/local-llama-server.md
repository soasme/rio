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
base_url = "http://127.0.0.1:8080/v1"
models = ["MODEL_ID"]
```

The default `llama-server` endpoint is `http://127.0.0.1:8080`; Rio needs the
OpenAI-compatible `/v1` base URL. Change the host or port in `base_url` if your
server uses a different endpoint.

## If the server requires a key

Add `api_key_env = "LLAMA_SERVER_API_KEY"` to the provider table and set the variable:

```bash
export LLAMA_SERVER_API_KEY=your-key
```

If the variable is unset, Rio sends an empty key. `models` is optional too; when
omitted, Rio uses the provider name as the model ID. When models are listed,
the first is the default. See [Catalog keys](providers.md#catalog-keys) for
every supported setting.

## Run Rio

Select the configured provider and model:

```bash
rio run --provider local-llama --model MODEL_ID "Explain this project."
```

Keep the server bound to a trusted network interface. A local model server is
not a sandbox for the code Rio is asked to inspect or change.
