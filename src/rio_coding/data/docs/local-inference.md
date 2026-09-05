# Local inference

rio does not (yet) include a built-in, hidden `llama.cpp` local-backend integration -- there is no `local_backends.py`, no `/local` command, and no dynamic per-session provider registry in `rio_coding`. If your checkout adds one later, document its endpoint precedence, credential handling, and safety boundary here, following the pattern in `security.md`.

## Using a local server today

Any local server that speaks the OpenAI-compatible API (llama.cpp's `llama-server`, for example) can be configured like any other OpenAI-compatible provider: add an entry to `~/.rio/catalog.toml` with its base URL and the exact model ID it reports, and select it with `--provider <name> --model <id>`. rio does not scan ports, processes, or the local network to find it, and it does not install, start, or stop the server -- see `security.md` for the general boundary this falls under.

See `models.md` for how the provider catalog is structured and overridden.
