# 0.4.1

* feat(tui): derive hotkeys, palette, and /help from one command table ([#57](https://github.com/soasme/rio/pull/57))
* feat(coding): invoke skills as /<skill-name> <prompt> ([#56](https://github.com/soasme/rio/pull/56))
* fix(agent): recover from a step call truncated at the output limit ([#55](https://github.com/soasme/rio/pull/55))
* ci(coding): vendor bundled models. ([#54](https://github.com/soasme/rio/pull/54))
* feat(tui): disable footer. ([#53](https://github.com/soasme/rio/pull/53))
* neat(tui): refactor rio_tui.widgets.* to rio_tui.widget_*. ([#51](https://github.com/soasme/rio/pull/51))
* fix(tui): read failed due to llm not honoring spec. ([#49](https://github.com/soasme/rio/pull/49))

# 0.4.0

Debut of these four modules:

* `rio_ai` is ported from `tau`.
* `rio_agent` is a fresh implementation of `SKILL.state` paper.
* `rio_coding` is a coding agent harness.
* `rio_tui` is a coding agent terminal interface.

