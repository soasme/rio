# Local inference

Run a local OpenAI-compatible server before starting Rio, then add its base URL
and model ID to `~/.rio/catalog.toml`:

```toml
schema_version = 1

[[providers]]
name = "local-llama"
base_url = "http://127.0.0.1:8080/v1"
models = ["MODEL_ID"]
```

Select it with `rio run --provider local-llama --model MODEL_ID "TASK"`.
Rio sends requests to the configured endpoint. Server installation, model
downloads, discovery, and process management belong to the server's own tools.

See [Models](models.md) for catalog configuration.
