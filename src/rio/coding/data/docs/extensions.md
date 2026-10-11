# Provider extensions

The extension API is available to custom library hosts. Python modules with
`setup(api)` can register providers, tools, commands, and event hooks.

`rio run` does not load extensions. It selects providers from the built-in and
user catalogs, with saved provider preferences. Configure custom endpoints in
`~/.rio/catalog.toml`; unknown providers and invalid models are errors.
