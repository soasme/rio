# 0.4.1

* `rio_tui` derives hotkeys, the command palette, and `/help` from one command table.
* `rio_coding` invokes skills as `/<skill-name> <prompt>`.
* `rio_agent` recovers from a step call truncated at the output limit.
* `rio_coding` vendors bundled models.
* `rio_tui` disables the footer.
* `rio_tui` renames `rio_tui.widgets.*` to `rio_tui.widget_*`.
* `rio_tui` fixes read failures when the LLM does not honor the spec.

# 0.4.0

Debut of these four modules:

* `rio_ai` is ported from `tau`.
* `rio_agent` is a fresh implementation of `SKILL.state` paper.
* `rio_coding` is a coding agent harness.
* `rio_tui` is a coding agent terminal interface.

