# Lean behavior models

Run `lake build` in this directory. CI builds all targets.

| Model | Python behavior |
| --- | --- |
| `RioEvals.lean` | Required tests must finish and pass; zero exits, skips, missing tests, and timeouts cannot bypass grading. Comparisons require stable inputs and all scheduled trials. Artifacts depend on original/final files, not the agent's Git index or HEAD. Failed attempts contribute to cost, and unknown usage is not free. (`evals/sandbox.py`, `evals/run.py`, `evals/metrics.py`) |
| `RioAgent.lean` | Explicit prepared-turn state changes without implicit reducer updates, runtime ledger patches preserving untouched fields and records, immutable cell versions, id-addressed step ops with runtime-assigned ids and positions, append-only replacement with stale versions until compaction, controls as ops outside State with their own distinct ids and exact per-type fields, order-free removal, one op per existing Cell, stale-turn rejection, turn budgets counting newly prepared turns separately from model attempts, note-only admission and caller-supplied runnable kinds sharing one step schema, bounded output tails with fresh output replacing old tails, streamed Cell output tails and logs within an owner disk budget, worker results distinguishing zero/nonzero exits from missing exit records, admission runtime reset preserving allocation, duplicate step acceptance, recovery without replay, pause consumption, batched completion wake-ups, bounded idle decisions, explicit run stops versus retryable patches and propagated errors, provider failures backing off on their own budget with refunded attempts, incremental inbox byte budgets, whole-observation admission bounds with the oldest message reserved in each prepared turn, prepared-turn output shares with omitted byte counts and fallback to outstanding records before a stop, terminal dispatch, success guards, single-commit success and failure conclusions, POSIX checks before recovery, timer delivery, run-loop wake deadlines, AGENTS.md resolution (`rio.agent.session`, `rio.agent.ledger`, `rio.coding.cells`, `rio.coding.owner`, `rio.cli.run`) |
| `RioAI.lean` | Final-only provider outcomes, fragment order, retry isolation, missing terminal errors, truncation priority and retry limits (`rio.ai.provider`, `rio.ai.retry`) |
| `RioCli.lean` | Committed content without cell IDs and cumulative output without duplication, provider-free terminal replay after cleanup, replay without journal writes beyond worker cleanup, metrics recorded once at run end, catalog-only provider definitions with login saving only the default provider and preserving preferences, unknown keys, and catalog bytes, ignored retired preferences, strict explicit thinking levels, stable login/request credential keys, catalog-only provider resolution, model/provider/kind API precedence and model/provider/protocol compat precedence independent of provider names and URLs, provider/model selection independent of extra saved preferences, OSC 7501 status override and lifecycle (`rio.cli.render`, `rio.cli.status`) |

These are independent behavior models, not proofs extracted from Python.
`RioAgent.lean` also checks that model-facing State is a header block and one block per
Cell before stale ids, revision, and runtime, and that replacements append and mark the old
version stale, and that a settled execution sorts after the Cells that existed when it settled and before later ones, so the prefix survives edits, settled output, and per-turn changes. This models block
ordering, not provider cache hits.
They do not prove SQLite synchronization, OS process cleanup, transport behavior,
or hardware durability. Python tests cover the implementation and its failure paths.

`RioCli.lean` also models required subcommands: a missing command exits with status 2,
and a selected command proceeds to dispatch (`rio.cli`).
It also models release checks with bundled model refresh enabled by default and an explicit
opt-out that preserves verification (`scripts/bumpversion.py`).
It also models OAuth refresh: the runtime resolver refreshes only expired credentials
(`rio.coding.provider_runtime`).

The current user-facing contracts are described in the
[CLI reference](../../docs/reference/cli.md) and
[session guide](../../docs/guides/sessions.md). In particular,
`RioCli.lean` models provider-free terminal replay only after cleanup succeeds.

See the [glossary](../../docs/glossary.md) for turns, attempts, and operations.

`RioAgent.lean` also models strict integer decoding and preservation of omitted versus
explicitly null fields through record serialization. Python model tests cover aliases,
extension fields, replay, and validation errors. Release checks include lint, mypy,
pytest, and Lean whether bundled model refresh is enabled or skipped.
