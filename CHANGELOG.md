# 0.4.3

* refactor: consolidate rio_agent/rio_ai/rio_coding/rio_tui into one rio package ([#78](https://github.com/soasme/rio/pull/78))
* feat(coding): add /fork to branch a new session from the live state ([#76](https://github.com/soasme/rio/pull/76))
* feat(coding): support batch reads via read.arguments.files ([#74](https://github.com/soasme/rio/pull/74))

# 0.4.2

* fix(tui): offer every backend slash command in the `/` popover ([#70](https://github.com/soasme/rio/pull/70))
* feat(state): keep file contents in the execution state ([#69](https://github.com/soasme/rio/pull/69))
* fix(coding): state the single-file contract in the read tool description ([#68](https://github.com/soasme/rio/pull/68))
* feat(coding): journal step reasoning as a non-resumable session entry ([#67](https://github.com/soasme/rio/pull/67))
* fix(tui): label tool blocks with the resolved target when the model uses an argument alias ([#66](https://github.com/soasme/rio/pull/66))
* feat(tui): replace inherited emoji icons with ascii markers ([#64](https://github.com/soasme/rio/pull/64))

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

