# AGENTS.md

* When implementing, follow YAGNI pattern.
* After implementing, update lean verification and make sure the verification passes.
* Before changing code, create `.worktrees/<name>`.
* When commiting code and creating a PR, use conventional message.
* When raise a PR, summarize the current topic visually. Pick the smallest view that makes the key point clear.
* When answering, keep text short and concise.
* When composing text, remove emojis, fluff, cheerful filler text and use concise, clear, simple language.
* When adding parameters, ensure API's simplicity and Orthogonality.
* When writing issues, follow the templates under `.github/ISSUE_TEMPLATE/`.
* When bumping a version, update CHANGELOG.md using recent comits, bump pyproject.toml version and git tag/push.
* To update vendor bundled models, run `scripts/vendor_bundled_models.py` and send a PR for the code change.
