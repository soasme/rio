# Rio coding evals

These are end-to-end checks of Rio's one-shot coding workflow. Each trial copies a
small starting project into a fresh temporary directory, runs the public `rio run`
command there, then runs a test file that was kept outside that directory. A pass
requires both Rio to finish successfully and the test to pass. The runner saves
Rio's JSON event stream, stderr, grader output, final workspace, and a
machine-readable summary.

Run with an already configured provider and model:

```bash
uv run --dev python -m evals.run --provider PROVIDER --model MODEL
```

Use `--trials 3` to see how often each task succeeds across independent attempts.
Use `--case 001-divide` to run one case, or `--timeout 600` for a slower model. Results
go to `.eval-results/` by default. This is a manual eval; it is not part of CI
because it makes model calls and outcomes can vary.

The current cases are deliberately small. They check the runner and provide an
initial signal, not a reliable comparison between models. Grow the suite from
real Rio failures and representative user tasks. Prefix each case directory with
a numeric id, such as `003-name`. For each new case, make the task unambiguous, keep the grader outside `workspace/`, check that the starting
files fail, and confirm a known good solution passes. Record the model, provider,
Rio revision, trial count, and pass rate when comparing runs. Inspect failed
event streams before changing the agent; a setup or grading error can look like
an agent failure. Workspace copies separate trials, but they are not a security
sandbox; use a container for untrusted agents or private benchmark tasks.

This design follows the guidance to use isolated trials, verify the resulting
environment, prefer deterministic graders, and repeat trials for variable
agents: [Anthropic, *Demystifying evals for AI agents*](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents).
Trace inspection complements outcome scoring:
[OpenAI, *Evaluate agent workflows*](https://developers.openai.com/api/docs/guides/agent-evals).
For larger repository tasks, use issue-style fixtures and tests as in
[SWE-bench](https://arxiv.org/abs/2310.06770), while reviewing grader quality.
