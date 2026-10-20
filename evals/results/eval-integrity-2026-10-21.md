# Evaluation integrity run report — 2026-10-21

The 60 saved model attempts passed **55/60 after corrected grading** across 20 cases, with three attempts per case. The original run scored 54/60. All scheduled attempts finished, the original run is valid, and its input fingerprints remained stable. The separate GitHub Copilot smoke passed **2/4**. Another **17 attempts** were rejected by provider configuration or account availability; they are reported separately below.

## Validation

- 600 Python tests passed; Ruff and mypy passed (88 source files).
- All four Lean library targets built successfully.
- All 20 graders passed calibration: 65 reference, alternative, baseline, and negative-control probes.
- CI runs the Python, Lean, and credential-free grader checks. Live model outcomes are recorded below; agent failures are not hidden by the CI result.

## Grader correction and artifact regrade

Manual review found one false negative: the optional-dependency grader changed the installed-module environment after importing the submitted module. This unfairly rejected a valid implementation that selected its dependency at import time. The grader now reloads the submitted module after each environment setup, and calibration includes a valid import-time implementation. It still rejects swallowed dependency errors.

All 60 original patches were reapplied to fresh copies from the original image IDs and regraded at revision `783bc2288fd6b9aa66ffb08c968846e85b7810e9`. Installed environments were checked against the original manifest. No agent was rerun and no submitted code was changed. Only case 009 trial 2 changed from incorrect to passed; the corrected score is 55/60. The original 54/60 summary is preserved in the JSON alongside this separate adjudication. The table below uses corrected grading and original agent timings; it is not a new end-to-end run or a baseline comparison.

## Primary repeated run

- Provider/model: `anthropic/claude-sonnet-4-6`, thinking `medium`.
- Tested revision: `053189b292b3dfd8768a188d9b4be220321a3993`; clean worktree at run start.
- Started: 2026-10-20T11:25:09+00:00. The date in this report uses Pacific/Auckland.
- Three sequential attempts per case; 300-second agent deadline. Separate fresh agent and offline grader containers; 2 CPUs, 2 GiB RAM, 256 processes per container.
- Outcomes: 5 incorrect, 55 passed.
- Total agent time including failed attempts: 1708.0s; time per successful attempt: 31.1s.
- Reported tokens across all attempts: unknown; tokens per successful attempt including failures: unknown.
- Dollar cost: unknown. Agent latency excludes setup and grading; the JSON retains those timings separately.

| Case | Passed | 95% Wilson interval | Median / p95 agent seconds | Outcomes |
| --- | ---: | --- | ---: | --- |
| 001-divide | 3/3 | 43.9%–100.0% | 23.4 / 26.8 | 3 passed |
| 002-slugify | 3/3 | 43.9%–100.0% | 28.3 / 43.6 | 3 passed |
| 003-flask-blueprint-name | 3/3 | 43.9%–100.0% | 26.5 / 64.0 | 3 passed |
| 004-pytest-caplog-level | 3/3 | 43.9%–100.0% | 31.9 / 43.4 | 3 passed |
| 005-inventory-service | 0/3 | 0.0%–56.1% | 32.0 / 35.0 | 3 incorrect |
| 006-csv-totals | 3/3 | 43.9%–100.0% | 39.1 / 47.8 | 3 passed |
| 007-config-merge | 3/3 | 43.9%–100.0% | 24.2 / 25.0 | 3 passed |
| 008-cli-streams | 3/3 | 43.9%–100.0% | 17.7 / 18.5 | 3 passed |
| 009-optional-dependency | 1/3 | 6.1%–79.2% | 12.2 / 12.7 | 2 incorrect, 1 passed |
| 010-log-diagnosis | 3/3 | 43.9%–100.0% | 53.2 / 127.2 | 3 passed |
| 011-retry-errors | 3/3 | 43.9%–100.0% | 24.1 / 28.0 | 3 passed |
| 012-atomic-write | 3/3 | 43.9%–100.0% | 25.3 / 27.6 | 3 passed |
| 013-project-constraints | 3/3 | 43.9%–100.0% | 27.1 / 30.5 | 3 passed |
| 014-sqlite-migration | 3/3 | 43.9%–100.0% | 16.9 / 28.5 | 3 passed |
| 015-cache-falsey | 3/3 | 43.9%–100.0% | 22.3 / 27.6 | 3 passed |
| 016-confined-path | 3/3 | 43.9%–100.0% | 26.7 / 47.4 | 3 passed |
| 017-blocked-task | 3/3 | 43.9%–100.0% | 3.7 / 5.4 | 3 passed |
| 018-resume-side-effect | 3/3 | 43.9%–100.0% | 15.0 / 25.8 | 3 passed |
| 019-timezone-order | 3/3 | 43.9%–100.0% | 33.6 / 51.3 | 3 passed |
| 020-pagination | 3/3 | 43.9%–100.0% | 32.5 / 79.4 | 3 passed |

Intervals describe repeated attempts on each case. With only three attempts, uncertainty is wide and p95 is the maximum observation. The overall fraction is descriptive; repeated attempts are not 60 independent tasks.

### Sampled passing patches

Manual inspection of trial 1 patches for division, Flask blueprint validation, pytest handler-level restoration, interrupted-side-effect receipt, and pagination found changes consistent with the task contracts and no test modifications. Runtime traces additionally confirm actual truncation, provider retry, and evidence-based resolution. This is a sample, not an exhaustive semantic audit.

### Failed outcomes

All three inventory submissions returned the original stock object for an empty order.
The two remaining optional-dependency failures swallowed errors from an installed encoder.

- 005-inventory-service, trial 1: **incorrect**. `grade.py::test_empty_order_returns_independent_stock` (failed)
- 005-inventory-service, trial 2: **incorrect**. `grade.py::test_empty_order_returns_independent_stock` (failed)
- 005-inventory-service, trial 3: **incorrect**. `grade.py::test_empty_order_returns_independent_stock` (failed)
- 009-optional-dependency, trial 1: **incorrect**. `grade.py::test_installed_dependency_errors_are_not_swallowed` (failed)
- 009-optional-dependency, trial 3: **incorrect**. `grade.py::test_installed_dependency_errors_are_not_swallowed` (failed)

### Runtime scenario evidence

| Scenario | Trial | Outcome | Observed evidence |
| --- | ---: | --- | --- |
| 010-log-diagnosis | 1 | passed | 2 truncation events |
| 010-log-diagnosis | 2 | passed | 2 truncation events |
| 010-log-diagnosis | 3 | passed | 5 truncation events |
| 011-retry-errors | 1 | passed | 1 provider failure(s); unresolved=False |
| 011-retry-errors | 2 | passed | 1 provider failure(s); unresolved=False |
| 011-retry-errors | 3 | passed | 1 provider failure(s); unresolved=False |
| 017-blocked-task | 1 | passed | Explicit failure conclusion=True |
| 017-blocked-task | 2 | passed | Explicit failure conclusion=True |
| 017-blocked-task | 3 | passed | Explicit failure conclusion=True |
| 018-resume-side-effect | 1 | passed | 1 unknown execution(s), 1 resolution record(s); ledger/receipt grader=passed |
| 018-resume-side-effect | 2 | passed | 1 unknown execution(s), 1 resolution record(s); ledger/receipt grader=passed |
| 018-resume-side-effect | 3 | passed | 1 unknown execution(s), 1 resolution record(s); ledger/receipt grader=passed |

## Cross-provider smoke

`github-copilot/gpt-4.1`, thinking `off`, one attempt each on division, inventory, provider retry, and interrupted side effects. Tested revision `f533e654ecc1216fffa79eace1814be92ec2a0c7`. The later primary revision only clarifies the large-output task, which is outside this smoke subset.

| Case | Outcome | Agent seconds |
| --- | --- | ---: |
| 001-divide | passed | 18.8 |
| 005-inventory-service | agent_failure | 23.5 |
| 011-retry-errors | incorrect | 31.2 |
| 018-resume-side-effect | passed | 23.8 |

The inventory attempt exhausted the model retry budget without implementing the stubs. The retry task recovered from the injected provider error, then claimed completion while leaving the implementation stub unchanged; all three required tests failed. Division and evidence-based resume passed. These four attempts are an integration smoke check, not a model ranking.

## Provider rejections

All attempted configurations and outcomes are retained. These requests could not perform the coding tasks and do not measure coding quality.

| Provider / model | Thinking | Rejected attempts | Reason |
| --- | --- | ---: | --- |
| openai-codex / gpt-5.5 | medium | 4 | Model unsupported for this account |
| openai-codex / gpt-5.4 | medium | 4 | Model unsupported for this account |
| openai-codex / gpt-5.3-codex | medium | 4 | Model unsupported for this account |
| github-copilot / gpt-5.4 | medium | 1 | Model unavailable for the selected integrator |
| github-copilot / gpt-4.1 | medium | 4 | Model does not support medium reasoning effort |

The supported Copilot configuration was subsequently exercised with thinking disabled; its four outcomes remain separate.

## Local durability probe

Run separately after the model batch, with 20 local samples. This is a controlled interruption at a committed authorization boundary, not a power-loss experiment.

| Measurement | Result |
| --- | ---: |
| Commit p50 | 0.105 ms |
| Commit p95 | 0.149 ms |
| Rebuild | 12.370 ms |
| Worker startup p50 | 2.920 ms |
| uv script p50 | 90.753 ms |
| Inbox fold p50 | 1.497 ms |
| Recovery | 2.176 ms |
| Idle owner RSS | 44.4 MiB |
| Idle cell processes | 0 |
| Interrupted / unknown / automatically reexecuted | 1 / 1 / 0 |

## Interpretation and next checks

The stronger graders exposed incomplete implementations that weaker checks accepted during development. The final failures remain part of the score. Earlier stopped development runs and the resume preflight are excluded because they used changing graders or setup; they are not combined with this fixed-harness run.

No speedup or regression claim is made against historical reports: both revisions must be rerun with matching version-2 metadata. No Rio production implementation or prompt was changed to optimize these results. The reserved timezone and pagination cases remain public and are not guaranteed contamination-free.

The suite is small and mainly synthetic. Next steps for a production release decision are to add anonymized real failures, repeat enough trials for the desired precision, inspect failed and sampled passing patches, and run each deployed provider/model combination. Agent network access remains enabled, so public-answer retrieval is possible. The offline grader and protected files reduce grading interference but do not prove arbitrary Python cannot attack its own test process.

## Artifacts and reproduction

[eval-integrity-2026-10-21.json](eval-integrity-2026-10-21.json) preserves every original final summary, the separate artifact regrade and its source script, scheduled results, per-case intervals, runtime/dependency/image fingerprints, calibration outcomes, failed test IDs, and durability measurements. Original run revisions are retained; this report is a later documentation commit.

Raw traces, stderr, submitted patches, grader logs, and probe logs remain in the local worktree under `.eval-results/<run>/`. They are not committed; provider-specific error details are summarized above.

```sh
uv run python -m evals.run --validate --split all --output-dir .eval-results/new-validation
uv run python -m evals.run --provider anthropic --model claude-sonnet-4-6 \
  --thinking medium --trials 3 --timeout 300 --split all --output-dir .eval-results/new-primary
uv run python -m evals.run --provider github-copilot --model gpt-4.1 --thinking off \
  --case 001-divide --case 005-inventory-service --case 011-retry-errors \
  --case 018-resume-side-effect --trials 1 --timeout 300 --output-dir .eval-results/new-smoke
uv run python -m evals.durability --samples 20 --output .eval-results/new-durability.json
```
