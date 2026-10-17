"""Evaluation metrics must not turn failures or missing measurements into wins."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from evals.metrics import aggregate, compare, trace_metrics
from evals.run import environment, environment_changes


def row(case: str, seconds: float, passed: bool = True) -> dict[str, Any]:
    return {
        "case": case,
        "seconds": seconds,
        "passed": passed,
        "model_turns": 2,
        "context_bytes": 100,
    }


def summary(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"provider": "fake", "model": "fake", "timeout": 600, "results": list(rows)}


def test_comparison_reports_all_four_cases_and_failed_speedup_is_not_a_win() -> None:
    names = ["001", "002", "003", "004"]
    baseline = summary(*(row(n, 10) for n in names))
    candidate = summary(*(row(n, 5, n != "004") for n in names))
    result = compare(baseline, candidate)
    assert set(result["cases"]) == set(names)
    assert result["cases"]["001"]["observed_improvement"]
    assert not result["cases"]["004"]["observed_improvement"]
    assert result["cases"]["004"]["successful_latency_ratio"] is None


@pytest.mark.parametrize("field", ["provider", "model", "timeout"])
def test_comparison_rejects_different_settings(field: str) -> None:
    baseline, candidate = summary(row("001", 10)), summary(row("001", 5))
    candidate[field] = "changed"
    with pytest.raises(ValueError, match=field):
        compare(baseline, candidate)


def test_comparison_rejects_unmatched_cases_or_trials() -> None:
    with pytest.raises(ValueError, match="cases"):
        compare(summary(row("001", 10)), summary(row("002", 5)))
    with pytest.raises(ValueError, match="trial"):
        compare(summary(row("001", 10)), summary(row("001", 5), row("001", 6)))


def test_unknown_tokens_are_never_reported_as_zero() -> None:
    assert trace_metrics("")["tokens"] is None
    trace = json.dumps(
        {
            "type": "model_attempt",
            "changes": {"meta": {"usage": {"tokens": 123, "unknown_usage": 1}}},
        }
    )
    assert trace_metrics(trace)["known_tokens"] == 123
    assert trace_metrics(trace)["tokens"] is None


def test_success_time_excludes_failed_attempts_but_latency_keeps_them() -> None:
    result = aggregate([row("001", 10), row("001", 1, False)])["001"]
    assert result["median_seconds"] == 5.5
    assert result["median_success_seconds"] == 10
    assert result["pass_rate"] == 0.5


def test_turn_metrics_count_preparations_and_compare_legacy_results() -> None:
    trace = "\n".join(
        json.dumps(event)
        for event in [
            {"type": "turn_prepared", "changes": {"turns": {"one": {"state": {}}}}},
            {"type": "model_attempt", "changes": {}},
            {"type": "patch_rejected", "changes": {}},
            {"type": "model_attempt", "changes": {}},
        ]
    )
    assert trace_metrics(trace)["model_turns"] == 1
    old = row("001", 10)
    old["model_rounds"] = old.pop("model_turns")
    result = compare(summary(old), summary(row("001", 10)))["cases"]["001"]
    assert result["baseline"]["median_model_turns"] == 2
    assert result["baseline"] == result["candidate"]


def test_environment_changes_report_new_lockfiles_and_venv_upgrades(tmp_path: Path) -> None:
    site = tmp_path / ".venv/lib/python3.11/site-packages"
    (site / "werkzeug-2.3.0.dist-info").mkdir(parents=True)
    (tmp_path / "poetry.lock").write_text("old")
    before = environment(tmp_path)
    assert environment_changes(before, environment(tmp_path)) == {
        "lockfiles_changed": [],
        "venv_added": [],
        "venv_removed": [],
    }
    (site / "werkzeug-2.3.0.dist-info").rename(site / "werkzeug-3.1.0.dist-info")
    (tmp_path / "uv.lock").write_text("new")
    (tmp_path / "poetry.lock").unlink()
    assert environment_changes(before, environment(tmp_path)) == {
        "lockfiles_changed": ["poetry.lock", "uv.lock"],
        "venv_added": ["werkzeug-3.1.0.dist-info"],
        "venv_removed": ["werkzeug-2.3.0.dist-info"],
    }
