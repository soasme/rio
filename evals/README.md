# Rio coding evaluations

The suite contains 20 scenarios: two smoke tasks, two SWE-bench repository fixes,
and 16 focused engineering and runtime scenarios. The new tasks are synthetic;
they are not a measured sample of production Rio work. Add anonymized real failures
as they become available. The suite measures this workload, not general coding ability.

## Run

Docker must be running. Validate every grader without credentials or model calls:

```sh
uv run python -m evals.run --validate --split all --output-dir .eval-results/validation
```

Run repeated model trials (three per case by default):

```sh
uv run python -m evals.run --provider anthropic --model claude-sonnet-4-6 \
  --trials 3 --timeout 300 --split all --output-dir .eval-results/candidate
```

Use `--case 018-resume-side-effect` to select a scenario. `--thinking` fixes reasoning
effort explicitly; otherwise the resolved value is recorded and passed to Rio.
Output directories must be new. Each run checkpoints every scheduled trial, including
setup failures. A stopped batch remains `complete: false, valid: false`.

The runner validates graders before making any model calls. Every case includes:

- `task.md`: requirements visible to the agent.
- `case.json`: allowed changes, required test IDs, split, and expected terminal result.
- `workspace/`, or a pinned repository base in `swebench.json`.
- Hidden `grade.py`, or a SWE-bench `test.patch` and selected tests.
- `reference/` or `reference.patch`: a passing solution.
- `mutants/`: incomplete solutions or grading-tampering controls that must fail.

Calibration requires the broken start to fail, the reference to pass, and every
negative control to fail. For the deliberately blocked task, an unchanged workspace
is correct; the model must additionally conclude failure, not claim success.
CI runs calibration for both splits without model credentials.

## Isolation and grading

The runtime image contains Rio and its locked dependencies, without eval fixtures.
Each agent runs in a new container with a fresh project and a separate home containing
only the selected provider credential and provider configuration. Host home, sessions,
reference solutions, graders, and Docker socket are not mounted. OAuth refresh happens
on the host before copying the selected credential. Agent containers have network
access for provider calls and normal development; public answer retrieval is not
technically blocked. This remains a limitation for public benchmark tasks.

The grader uses a new container from the original task image, no network, a read-only
filesystem, and a non-root user. Changes outside the explicitly allowed task paths and
changed symlinks are rejected. Submitted changes to installed packages do not carry into
grading. Original tests, configuration, and generated installation metadata are preserved.
The agent sees the same allowed-file restriction in its task.

A trusted pytest plugin checks the exact collected test IDs and final outcomes, including
teardown. Every required test must execute and pass: skips, xfails, missing results, and
zero exits without a complete report fail. This is defense against accidental or simple
bypasses, not a proof that arbitrary malicious Python cannot attack its own test process.
Review traces and patches, including sampled passes.

Git artifacts compare original and final file trees using a private index. Staging,
committing, deleting, and adding files cannot hide submitted changes. Setup commands,
agent execution, and grading have deadlines; timed-out Docker containers are removed.
Containers have 2 CPUs, 2 GiB RAM, and a 256-process limit.

## Coverage and splits

| Cases | Coverage |
| --- | --- |
| 001–004 | Division, ASCII slugification, Flask blueprint validation, pytest logging restoration |
| 005–009 | Multi-file inventory, exact CSV totals, recursive configuration, CLI streams, optional dependencies |
| 010–012 | Large diagnostic output, transient provider failure plus retry behavior, atomic file writes |
| 013–016 | Project constraints and misleading data, SQLite migration, falsey caches, confined paths |
| 017–018 | Honest blocked-task failure, interrupted side effects and evidence-based resolution |
| 019–020 | Reserved cases: timezone ordering and pagination with falsey cursors |

`--split regression` is the default. The `holdout` cases are reserved from prompt tuning;
they are public fixtures, not secret or guaranteed contamination-free. Their reference
grading can be validated without using their model outcomes to tune Rio. If a reserved
case drives a prompt change, move it to regression and replace it before claiming a
held-out result. Do not pool repeated trials as if they were distinct tasks.

The resume scenario seeds a committed launch whose side effect happened but whose
completion was not recorded. A real model must inspect evidence, resolve UnknownExecution,
and finish without duplicating the effect. It is a controlled interruption, not a power-loss
test. Process-kill and protocol failure tests remain in `tests/test_rio_agent.py`.
The provider-retry scenario injects one connection failure before delegating to the real
provider, using the public CLI and normal retry loop. Provider wire formats are separately
covered by deterministic adapter tests; run selected scenarios on each provider used in
production when changing adapters or models.

## Results and comparisons

Each trial retains `agent.jsonl`, `agent.stderr`, `agent.diff`, `grade.json`, and
`result.json`. Per-case calibration artifacts include every probe and grader log.
`summary.json` records the exact source, harness, fixtures, dependency hashes, revision,
dirty status, resolved settings, image IDs, installed task packages, platform, budgets,
all scheduled results, and input stability. Edits during a run invalidate the saved summary.

```sh
uv run python -m evals.run --provider PROVIDER --model MODEL --trials 3 \
  --output-dir .eval-results/candidate --compare .eval-results/baseline/summary.json
```

Run both revisions with this same harness and fixtures. Comparisons reject missing
metadata, incomplete batches, duplicate/missing trials, changed graders, and mismatched
settings or environments. Old version-1 reports are historical only and must be rerun.
Alternate baseline/candidate order across batches and avoid concurrent performance loads.

The primary metric is success on one attempt, estimated across repeats. Reports include
per-case 95% Wilson intervals, median and p95 agent latency, successful latency, timeout
rate, failure categories, and tokens/time per success including failed attempts. With
three trials, intervals are wide and p95 is essentially the maximum; neither establishes
statistical significance. Add trials based on the precision needed for a decision.
Missing usage remains unknown. Dollar cost remains unknown unless reported reliably;
context bytes are neither tokens nor the full prompt.

The predeclared descriptive regression gate permits no observed success-rate drop and
at most a 20% increase in successful median latency when both runs pass every attempt.
This is a regression warning, not a statistical non-inferiority claim or automatic release
approval. Inspect failures, cost tradeoffs, and uncertainty before drawing conclusions.

## Durability

```sh
uv run python -m evals.durability --samples 20
```

This isolates local commit, rebuild, startup, output-fold, memory, and recovery costs.
Run it separately from model trials. OS process tests and the Lean behavior models cover
additional invariants; neither establishes hardware or power-loss guarantees.

## Sources

The workflow follows [Anthropic’s agent-eval guidance](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
and [SWE-bench’s isolated patch evaluation](https://www.swebench.com/SWE-bench/guides/evaluation/).
Grader calibration matters even for established benchmarks: see
[OpenAI’s coding-evaluation audit](https://openai.com/index/separating-signal-from-noise-coding-evaluations/).
The repository reference patches contain the source changes from
[Flask PR 5014](https://github.com/pallets/flask/pull/5014) and
[pytest PR 7571](https://github.com/pytest-dev/pytest/pull/7571); existing upstream licenses apply.
