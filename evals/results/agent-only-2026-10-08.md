# Single-runtime check — 2026-10-08

The `rio.agent` runtime passed all four coding cases. This is one trial per case,
compared with the previous durable-runtime smoke run. Both runs used Anthropic
`claude-sonnet-4-6`, a 600-second timeout, and matching fixtures, Python, and host.

| Case | Previous durable run | Single-runtime run | Passes |
| --- | ---: | ---: | ---: |
| 001-divide | 19.57 s | 16.00 s | 1/1 → 1/1 |
| 002-slugify | 24.24 s | 10.70 s | 1/1 → 1/1 |
| 003-flask-blueprint-name | 65.45 s | 91.89 s | 1/1 → 1/1 |
| 004-pytest-caplog-level | 38.71 s | 44.59 s | 1/1 → 1/1 |

Divide and slugify were faster; Flask and caplog were slower. A single trial does
not establish a performance improvement. The earlier three-trial comparison with
the notebook baseline remains in [the historical report](durable-2026-10-07.md).

- Candidate revision: `b56d21d7e46547c7ae2bcc2c05b99889b860cf0b`.
- Baseline revision: `c4a495fe9aeb6d5183e167cef205782bcfe48293`.
- Validation: 628 Python tests passed, two Windows-only tests skipped on macOS;
  both Lean targets, Ruff, and the wheel build passed. GitHub's Linux pytest and
  Lean checks also passed for the candidate revision.
- The wheel has no notebook modules or Jupyter dependencies.
- Raw traces and grader output are in `.eval-results/agent-only/` locally.
- The local 20-script durability benchmark ran during the start of the first case;
  this adds another reason not to treat these timing differences as causal.

The [JSON artifact](agent-only-2026-10-08.json) includes both summaries, source and
fixture hashes, per-case comparisons, and the local durability benchmark. That
benchmark observed one interrupted execution, one `UnknownExecution`, and zero
automatic reruns. Host/power failure was not tested.

## Reproduce

At the candidate revision, extract the artifact's `baseline` object into
`/tmp/rio-baseline-summary.json`, then run:

```sh
uv run python -m evals.run --trials 1 --timeout 600 \
  --provider anthropic --model claude-sonnet-4-6 \
  --output-dir .eval-results/agent-only \
  --compare /tmp/rio-baseline-summary.json
```

Use more trials on both revisions for a stronger comparison. The current CLI
and eval runner have no runtime selector.
