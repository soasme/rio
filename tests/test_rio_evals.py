"""Evaluation metrics must not turn failures or missing measurements into wins."""

import json

import pytest
from evals.metrics import aggregate, compare, trace_metrics


def row(case, seconds, passed=True):
    return {
        "case": case,
        "seconds": seconds,
        "passed": passed,
        "model_rounds": 2,
        "context_bytes": 100,
    }


def summary(*rows):
    return {"provider": "fake", "model": "fake", "timeout": 600, "results": list(rows)}


def test_comparison_reports_all_four_cases_and_failed_speedup_is_not_a_win():
    names = ["001", "002", "003", "004"]
    baseline = summary(*(row(n, 10) for n in names))
    candidate = summary(*(row(n, 5, n != "004") for n in names))
    result = compare(baseline, candidate)
    assert set(result["cases"]) == set(names)
    assert result["cases"]["001"]["observed_improvement"]
    assert not result["cases"]["004"]["observed_improvement"]
    assert result["cases"]["004"]["successful_latency_ratio"] is None


@pytest.mark.parametrize("field", ["provider", "model", "timeout"])
def test_comparison_rejects_different_settings(field):
    baseline, candidate = summary(row("001", 10)), summary(row("001", 5))
    candidate[field] = "changed"
    with pytest.raises(ValueError, match=field):
        compare(baseline, candidate)


def test_comparison_rejects_unmatched_cases_or_trials():
    with pytest.raises(ValueError, match="cases"):
        compare(summary(row("001", 10)), summary(row("002", 5)))
    with pytest.raises(ValueError, match="trial"):
        compare(summary(row("001", 10)), summary(row("001", 5), row("001", 6)))


def test_unknown_tokens_are_never_reported_as_zero():
    assert trace_metrics("")["tokens"] is None
    trace = json.dumps(
        {
            "type": "model_attempt",
            "changes": {"meta": {"usage": {"tokens": 123, "unknown_usage": 1}}},
        }
    )
    assert trace_metrics(trace)["known_tokens"] == 123
    assert trace_metrics(trace)["tokens"] is None


def test_success_time_excludes_failed_attempts_but_latency_keeps_them():
    result = aggregate([row("001", 10), row("001", 1, False)])["001"]
    assert result["median_seconds"] == 5.5
    assert result["median_success_seconds"] == 10
    assert result["pass_rate"] == 0.5
