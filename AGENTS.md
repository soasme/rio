# AGENTS.md

* When implementing, follow YAGNI pattern.
* Use Pydantic models for structured records instead of TypedDict. Keep dictionaries for JSON payloads and journal deltas.
* All Python functions and methods must annotate parameters and return values (except implicit self and cls).
* After implementing, update Lean verification and pass `uv run ruff check .`, `uv run mypy`, `uv run pytest`, and `lake build` in `tests/lean`.
* Before changing code, create `.worktrees/<name>`.
* When commiting code and creating a PR, use conventional message.
* When raise a PR, summarize the current topic visually. Pick the smallest view that makes the key point clear.
* When answering, keep text short and concise.
* When composing text, remove emojis, fluff, cheerful filler text and use concise, clear, simple language.
* When adding parameters, ensure API's simplicity and Orthogonality.
* When writing issues, follow the templates under `.github/ISSUE_TEMPLATE/`.
* To bump a version, run `python3 scripts/bumpversion.py [major|minor|patch] -y`.
* To update vendor bundled models, run `scripts/vendor_bundled_models.py` and send a PR for the code change.
