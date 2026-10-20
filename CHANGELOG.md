# 0.9.2

* fix(evals): isolate grading and expand validated scenario coverage ([#189](https://github.com/soasme/rio/pull/189))
* fix(agent): bound whole inbox observations and reserve the oldest message ([#184](https://github.com/soasme/rio/pull/184))
* refactor: use Pydantic records and enforce Python typing ([#187](https://github.com/soasme/rio/pull/187))
* fix(coding): resolve model endpoint once and give retries one owner ([#186](https://github.com/soasme/rio/pull/186))
* fix(coding): bound Cell output memory and log disk use ([#185](https://github.com/soasme/rio/pull/185))
* docs: add design principles to package READMEs ([#183](https://github.com/soasme/rio/pull/183))
* refactor(agent): split context fitting out of prepare_turn ([#182](https://github.com/soasme/rio/pull/182))
* fix(agent): decode string-encoded step ops and reject same-step conclusions ([#181](https://github.com/soasme/rio/pull/181))
* refactor(ai): share provider client lifecycle in HTTPProvider ([#180](https://github.com/soasme/rio/pull/180))
* refactor(coding)!: select providers by api, drop provider kind ([#179](https://github.com/soasme/rio/pull/179))
* refactor: share chat tool-call parsing and drop stale catalog shims ([#178](https://github.com/soasme/rio/pull/178))
* refactor(ai): make providers a single request with a final message ([#177](https://github.com/soasme/rio/pull/177))
* feat(release): add optional bundled model refresh ([#176](https://github.com/soasme/rio/pull/176))
* fix(agent): preserve stable state prompt prefix ([#175](https://github.com/soasme/rio/pull/175))
* refactor(coding): declare provider quirks in the catalog ([#174](https://github.com/soasme/rio/pull/174))
* refactor(coding): simplify process owner execution ([#173](https://github.com/soasme/rio/pull/173))
* fix(coding): remove unused provider configuration paths ([#172](https://github.com/soasme/rio/pull/172))

# 0.9.1

* fix(coding): keep uv run from re-syncing the active venv ([#171](https://github.com/soasme/rio/pull/171))
* fix(agent): fit runtime output into context instead of stopping the run ([#170](https://github.com/soasme/rio/pull/170))
* fix(agent): retry provider failures on their own budget ([#169](https://github.com/soasme/rio/pull/169))
* fix(agent): keep the live projection a fold of journal events ([#168](https://github.com/soasme/rio/pull/168))
* refactor(agent): remove dead journal shim and tidy small definitions ([#167](https://github.com/soasme/rio/pull/167))
* refactor(agent)!: move controls from notes to a control op ([#166](https://github.com/soasme/rio/pull/166))
* refactor(agent): declare note roles once in a role table ([#165](https://github.com/soasme/rio/pull/165))
* refactor: simplify agent terminology and action names ([#163](https://github.com/soasme/rio/pull/163))
* fix(agent): commit prepared turn state explicitly ([#164](https://github.com/soasme/rio/pull/164))
* refactor(agent): split turn admission from runtime ledger ([#162](https://github.com/soasme/rio/pull/162))
* refactor(agent)!: move process records and argv config out of rio.agent ([#161](https://github.com/soasme/rio/pull/161))
* fix(agent): distinguish intentional run stops from value errors ([#160](https://github.com/soasme/rio/pull/160))
* refactor(agent)!: keep rio.agent environment agnostic ([#159](https://github.com/soasme/rio/pull/159))
* refactor(coding)!: consolidate shared OAuth code and trim credential API ([#158](https://github.com/soasme/rio/pull/158))
* refactor(ai)!: drop unset config options and name shared defaults neutrally ([#157](https://github.com/soasme/rio/pull/157))
* refactor(cli)!: move terminal rendering into cli and tidy owner control flow ([#156](https://github.com/soasme/rio/pull/156))
* refactor(coding)!: remove unused catalog and OAuth layers ([#155](https://github.com/soasme/rio/pull/155))
* fix: protect credentials and preserve stream/session integrity ([#153](https://github.com/soasme/rio/pull/153))
* refactor(ai)!: remove unused layers and share provider request handling ([#154](https://github.com/soasme/rio/pull/154))
* refactor(agent)!: trim state and standardize output tails ([#152](https://github.com/soasme/rio/pull/152))
* fix(cli): require subcommands and simplify login ([#151](https://github.com/soasme/rio/pull/151))

# 0.9.0

* refactor!: split run loop, record metrics at run end, define providers only in catalog ([#150](https://github.com/soasme/rio/pull/150))
* refactor(coding)!: remove unused MCP integration ([#149](https://github.com/soasme/rio/pull/149))
* docs: describe current runtime and package boundaries ([#148](https://github.com/soasme/rio/pull/148))
* fix(cli)!: complete one-shot cleanup and terminal replay ([#147](https://github.com/soasme/rio/pull/147))
* refactor(coding)!: remove extensions and local backend management ([#146](https://github.com/soasme/rio/pull/146))
* refactor(cli)!: resolve run providers only from the catalog ([#145](https://github.com/soasme/rio/pull/145))
* fix(agent): centralize event writes in Session ([#144](https://github.com/soasme/rio/pull/144))
* refactor(agent): type session state shapes ([#143](https://github.com/soasme/rio/pull/143))
* refactor(agent): wait on events instead of polling in run loop ([#142](https://github.com/soasme/rio/pull/142))
* feat(agent)!: address cells by id in step ([#141](https://github.com/soasme/rio/pull/141))
* refactor(coding): define provider helpers once and collapse merge logic ([#140](https://github.com/soasme/rio/pull/140))
* refactor(ai): share provider retry loop and honor Retry-After ([#139](https://github.com/soasme/rio/pull/139))
* refactor: remove legacy coding-agent code ([#138](https://github.com/soasme/rio/pull/138))
* refactor(agent): move code and cmd cells into rio.coding ([#137](https://github.com/soasme/rio/pull/137))
* fix(agent): wake the model once after queued work drains ([#136](https://github.com/soasme/rio/pull/136))
* fix(agent): keep the tail of long cell output and save the full output ([#135](https://github.com/soasme/rio/pull/135))

# 0.8.2

* feat(cli): report program status via OSC 7501 ([#133](https://github.com/soasme/rio/pull/133))
* fix(agent): explain repeated cell operation rejections ([#132](https://github.com/soasme/rio/pull/132))
* fix(agent): explain stale predecessor rejections ([#131](https://github.com/soasme/rio/pull/131))
* fix(agent): explain out-of-range cell index rejections ([#130](https://github.com/soasme/rio/pull/130))

# 0.8.1

* fix(cli): restore code, notes, and output in durable transcripts ([#127](https://github.com/soasme/rio/pull/127))
* feat(agent): add cmd cell kind for direct argv execution ([#129](https://github.com/soasme/rio/pull/129))
* feat(cli): load AGENTS.md into the system prompt ([#128](https://github.com/soasme/rio/pull/128))

# 0.8.0

* chore: update vendor bundled models catalog ([#126](https://github.com/soasme/rio/pull/126))
* feat(agent)!: implement durable state as the only runtime ([#125](https://github.com/soasme/rio/pull/125))
* feat(ai): rio.ai module enhancement ([#123](https://github.com/soasme/rio/pull/123))

# 0.7.1

* feat(agent): manage every cell each step; recall removed cells from the journal ([#122](https://github.com/soasme/rio/pull/122))
* docs: update readme
* test(coding): decouple catalog tests from models.dev snapshot ([#121](https://github.com/soasme/rio/pull/121))
* feat(coding): support MCP servers ([#119](https://github.com/soasme/rio/pull/119))
* feat(agent): isolate each session kernel in a private venv ([#118](https://github.com/soasme/rio/pull/118))
* feat(coding): add SQLite session storage ([#117](https://github.com/soasme/rio/pull/117))
* refactor(agent): move spec definitions to rio.agent.spec ([#116](https://github.com/soasme/rio/pull/116))

# 0.7.0

* feat(evals): add SWE-bench Verified cases ([#113](https://github.com/soasme/rio/pull/113))
* fix(cli): do not crash on long task text ([#114](https://github.com/soasme/rio/pull/114))
* fix(agent): do not end the run on a blank reply ([#112](https://github.com/soasme/rio/pull/112))
* feat: add basic coding evals ([#103](https://github.com/soasme/rio/pull/103))
* feat(agent)!: replace SKILL.state with a notebook-context CLM loop ([#111](https://github.com/soasme/rio/pull/111))
* fix(coding): stop echoing file content for read/edit/write in console ([#110](https://github.com/soasme/rio/pull/110))
* revert(coding): bring back the respond action ([#109](https://github.com/soasme/rio/pull/109))

# 0.6.3

* refactor(coding): remove codemode ([#108](https://github.com/soasme/rio/pull/108))

# 0.6.2

* fix: Session ID must be printed as the first line ([#105](https://github.com/soasme/rio/pull/105))
* feat(coding): expose codemode by default ([#107](https://github.com/soasme/rio/pull/107))
* fix: omit Authorization header when api_key is not set ([#104](https://github.com/soasme/rio/pull/104))

# 0.6.1

* neat: ruff fix
* neat: vendor model update
* feat(coding): finish on final action text ([#102](https://github.com/soasme/rio/pull/102))
* feat(coding): add codemode tool backed by pydantic-monty ([#101](https://github.com/soasme/rio/pull/101))
* feat(cli): add colored dot indicator for session status ([#98](https://github.com/soasme/rio/pull/98))
* fix(catalog): allow minimal provider entries and blank API keys ([#100](https://github.com/soasme/rio/pull/100))
* fix(coding): respond should come with message
* docs: add a lean verification rule to agents.md
* docs(guides): add library usage documentation ([#99](https://github.com/soasme/rio/pull/99))

# 0.6.0

* feat(agent): append state patches to history ([#97](https://github.com/soasme/rio/pull/97))
* test(coding): model core behavior in Lean ([#96](https://github.com/soasme/rio/pull/96))
* test(agent): verify loop transitions in Lean ([#94](https://github.com/soasme/rio/pull/94))
* chore: add release bump script ([#93](https://github.com/soasme/rio/pull/93))
* fix(coding): require path for read/write/edit, drop batch files mode ([#92](https://github.com/soasme/rio/pull/92))
* feat(cli): add rio version command ([#91](https://github.com/soasme/rio/pull/91))
* docs: add documentation structure ([#90](https://github.com/soasme/rio/pull/90))

# 0.5.0

* feat(cli): use argparse output modes ([#89](https://github.com/soasme/rio/pull/89))
* fix(cli): stop blaming --cwd for unrelated run errors ([#88](https://github.com/soasme/rio/pull/88))
* feat: refresh vendor catalog ([#87](https://github.com/soasme/rio/pull/87))

# 0.4.5

* feat(coding): retain providers json config ([#86](https://github.com/soasme/rio/pull/86))
* feat: add run login and resume commands ([#85](https://github.com/soasme/rio/pull/85))
* docs: fix usage to use uv tool install ([#84](https://github.com/soasme/rio/pull/84))
* fix(ai): request strict JSON-schema tool calling from providers that support it ([#84](https://github.com/soasme/rio/pull/84))

# 0.4.4

* fix(coding): reject empty or placeholder-stub respond messages ([#83](https://github.com/soasme/rio/pull/83))
* fix(cli): drop unused mode param breaking thinking level ([#82](https://github.com/soasme/rio/pull/82))
* refactor(cli): make coding runs one-shot ([#81](https://github.com/soasme/rio/pull/81))
* fix(agent): downgrade user messages back to plain observations ([#80](https://github.com/soasme/rio/pull/80))

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

