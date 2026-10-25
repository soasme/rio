"""Evaluation metrics must not turn failures or missing measurements into wins."""

from __future__ import annotations

import json
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from evals.metrics import aggregate, compare, trace_metrics
from evals.run import environment, environment_changes
from evals.sandbox import (
    Case,
    ProcessResult,
    assess_grade,
    capture_patch,
    check_submission,
    checked,
    command,
    copy_project,
)


def row(case: str, seconds: float, passed: bool = True) -> dict[str, Any]:
    return {
        "case": case,
        "seconds": seconds,
        "passed": passed,
        "model_turns": 2,
        "context_bytes": 100,
    }


def summary(*rows: dict[str, Any]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    results = []
    for row in rows:
        counts[row["case"]] += 1
        results.append({**row, "trial": counts[row["case"]]})
    return {
        "schema_version": 2,
        "complete": True,
        "valid": True,
        "provider": "fake",
        "model": "fake",
        "thinking": None,
        "timeout": 600,
        "fixtures_sha256": "fixtures",
        "harness_sha256": "harness",
        "dependencies_sha256": "dependencies",
        "endpoint_sha256": "endpoint",
        "platform": "fake",
        "python": "3.12",
        "limits": {},
        "sandbox": "docker",
        "agent_network": True,
        "task_environments": {},
        "cases": list(counts),
        "trials": max(counts.values()),
        "policy": {"max_pass_rate_drop": 0.0, "max_latency_ratio": 1.2},
        "results": results,
    }


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


def test_prompt_tokens_are_split_by_cache_outcome() -> None:
    def answered(input: int, read: int, write: int) -> str:
        usage = {"input": input, "cacheRead": read, "cacheWrite": write}
        return json.dumps(
            {"type": "model_response", "changes": {"turns": {"t": {"response": {"usage": usage}}}}}
        )

    metrics = trace_metrics("\n".join([answered(10, 0, 90), answered(5, 90, 5)]))
    assert (metrics["input_tokens"], metrics["cache_read_tokens"]) == (15, 90)
    assert metrics["cache_write_tokens"] == 95
    assert aggregate([{**row("001", 10), **metrics}])["001"]["cache_hit_rate"] == 0.45
    assert aggregate([row("001", 10)])["001"]["cache_hit_rate"] is None


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


@pytest.mark.parametrize("field", ["harness_sha256", "dependencies_sha256", "thinking", "policy"])
def test_comparison_requires_complete_matching_metadata(field: str) -> None:
    baseline = summary(row("001", 10))
    candidate = summary(row("001", 10))
    del candidate[field]
    with pytest.raises(ValueError, match=field):
        compare(baseline, candidate)


@pytest.mark.parametrize("change", [{"valid": False}, {"complete": False}, {"schema_version": 1}])
def test_invalid_or_partial_runs_cannot_compare(change: dict) -> None:
    baseline = summary(row("001", 10))
    candidate = {**baseline, **change}
    with pytest.raises(ValueError, match="complete, valid"):
        compare(baseline, candidate)


def test_missing_and_duplicate_trials_cannot_compare() -> None:
    baseline = summary(row("001", 10), row("001", 11))
    candidate = {**baseline, "results": baseline["results"][:1]}
    with pytest.raises(ValueError, match="scheduled"):
        compare(baseline, candidate)
    candidate["results"] = [baseline["results"][0]] * 2
    with pytest.raises(ValueError, match="scheduled"):
        compare(baseline, candidate)


def test_uncertainty_and_cost_include_failures() -> None:
    result = aggregate(
        [
            {**row("001", 10), "tokens": 10},
            {**row("001", 20, False), "tokens": 30, "status": "agent_timeout"},
        ]
    )["001"]
    assert result["tokens_per_success"] == 40
    assert result["seconds_per_success"] == 30
    assert result["timeout_rate"] == 0.5
    assert result["pass_rate_ci95"][0] < 0.1
    assert result["pass_rate_ci95"][1] > 0.9
    assert not compare(summary(row("001", 10)), summary(row("001", 13)))["gate_passed"]


@pytest.mark.parametrize("state", ["staged", "committed"])
def test_patch_preserves_all_changes_against_original_base(tmp_path: Path, state: str) -> None:
    base, candidate = tmp_path / "base", tmp_path / "candidate"
    base.mkdir()
    (base / "a.py").write_text("original\n")
    (base / "deleted.py").write_text("delete\n")
    copy_project(base, candidate)
    checked(["git", "init", "-q", str(candidate)])
    checked(["git", "add", "-A"], cwd=candidate)
    commit = [
        "git",
        "-c",
        "user.name=eval",
        "-c",
        "user.email=eval@localhost",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-qm",
        "snapshot",
    ]
    checked(commit, cwd=candidate)
    (candidate / "a.py").write_text("changed\n")
    (candidate / "deleted.py").unlink()
    (candidate / "new.py").write_text("new\n")
    checked(["git", "add", "-A"], cwd=candidate)
    if state == "committed":
        checked(commit, cwd=candidate)
    output = tmp_path / "agent.diff"
    assert capture_patch(base, candidate, output) == ["a.py", "deleted.py", "new.py"]
    replay = tmp_path / "replay"
    copy_project(base, replay)
    checked(["git", "apply", str(output)], cwd=replay)
    assert (replay / "a.py").read_text() == "changed\n"
    assert (replay / "new.py").read_text() == "new\n"
    assert not (replay / "deleted.py").exists()


def test_grading_rejects_config_edits_and_symlinks(tmp_path: Path) -> None:
    base, candidate = tmp_path / "base", tmp_path / "candidate"
    base.mkdir()
    candidate.mkdir()
    spec = Case(category="test", editable=["src/*"], tests=["test"])
    (candidate / "pytest.ini").write_text("[pytest]\naddopts = --ignore=tests\n")
    (candidate / "src").mkdir()
    (candidate / "src/escape.py").symlink_to("/etc/passwd")
    assert check_submission(base, candidate, spec) == ["pytest.ini", "src/escape.py"]


@pytest.mark.parametrize("outcome", ["skipped", "failed", "xfailed"])
def test_zero_exit_does_not_override_test_outcome(tmp_path: Path, outcome: str) -> None:
    report = tmp_path / "tests.json"
    report.write_text(json.dumps({"collected": ["test"], "tests": {"test": outcome}}))
    assert assess_grade(ProcessResult(returncode=0, seconds=1), report, ["test"]).status != "passed"


def test_missing_tests_and_missing_report_are_grader_errors(tmp_path: Path) -> None:
    report = tmp_path / "tests.json"
    process = ProcessResult(returncode=0, seconds=1)
    assert assess_grade(process, report, ["test"]).status == "grader_error"
    report.write_text(json.dumps({"collected": [], "tests": {}}))
    assert assess_grade(process, report, ["test"]).status == "grader_error"
    assert (
        assess_grade(
            ProcessResult(returncode=None, seconds=1, timed_out=True), report, ["test"]
        ).status
        == "grader_timeout"
    )


@pytest.mark.parametrize(
    "body",
    [
        "import pytest\n\ndef test_check() -> None:\n    pytest.skip('skip')\n",
        "import os\n\ndef test_check() -> None:\n    os._exit(0)\n",
        "import pytest\n\n@pytest.mark.xfail\ndef test_check() -> None:\n    assert False\n",
    ],
)
def test_real_pytest_bypasses_do_not_pass(tmp_path: Path, body: str) -> None:
    import os

    plugin = Path(__file__).resolve().parents[1] / "evals/pytest_report.py"
    shutil.copy2(plugin, tmp_path / "rio_eval_report.py")
    (tmp_path / "grade.py").write_text(body)
    report = tmp_path / "tests.json"
    result = command(
        [sys.executable, "-m", "pytest", "-q", "-p", "rio_eval_report", "grade.py"],
        cwd=tmp_path,
        timeout=10,
        env={
            **os.environ,
            "PYTHONPATH": str(tmp_path),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "RIO_EVAL_REPORT": str(report),
        },
    )
    assert assess_grade(result, report, ["grade.py::test_check"]).status != "passed"


def test_process_timeout_is_bounded() -> None:
    result = command([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.05)
    assert result.timed_out and result.returncode is None
    assert result.seconds < 5


def test_manifest_detects_non_python_and_added_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evals import run

    (tmp_path / "src").mkdir()
    (tmp_path / "evals").mkdir()
    (tmp_path / "uv.lock").write_text("lock")
    (tmp_path / "pyproject.toml").write_text("project")
    monkeypatch.setattr(run, "ROOT", tmp_path)
    old = run.fingerprints([])
    (tmp_path / "src/catalog.json").write_text("{}")
    assert run.fingerprints([])["source_sha256"] != old["source_sha256"]
    (tmp_path / "evals/new.py").write_text("# grader")
    assert run.fingerprints([])["harness_sha256"] != old["harness_sha256"]
    (tmp_path / "uv.lock").write_text("different")
    assert run.fingerprints([])["dependencies_sha256"] != old["dependencies_sha256"]


def test_setup_failure_retains_every_scheduled_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evals import run

    cases = tmp_path / "cases"
    case = cases / "one"
    case.mkdir(parents=True)
    (case / "case.json").write_text(
        Case(category="test", editable=["app.py"], tests=["test"]).model_dump_json()
    )
    output = tmp_path / "results"
    monkeypatch.setattr(run, "CASES", cases)
    monkeypatch.setattr(run, "fingerprints", lambda cases: {})
    monkeypatch.setattr(run, "checked", lambda *args, **kwargs: "revision")

    def broken_build() -> str:
        raise RuntimeError("Docker unavailable")

    monkeypatch.setattr(run, "build_runtime", broken_build)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evals.run",
            "--validate",
            "--output-dir",
            str(output),
            "--cache-dir",
            str(tmp_path / "cache"),
        ],
    )
    assert run.main() == 1
    report = json.loads((output / "summary.json").read_text())
    assert report["complete"] and not report["valid"]
    assert report["total"] == report["finished"] == 1
    assert report["passed"] == 0
    assert report["results"][0]["status"] == "setup_error"


def test_executable_mode_change_is_part_of_submission(tmp_path: Path) -> None:
    base, candidate = tmp_path / "base", tmp_path / "candidate"
    base.mkdir()
    (base / "app.py").write_text("pass\n")
    copy_project(base, candidate)
    (candidate / "app.py").chmod(0o755)
    spec = Case(category="test", editable=[], tests=["test"])
    assert check_submission(base, candidate, spec) == ["app.py"]


def test_blocked_task_requires_an_agent_failure_decision() -> None:
    from evals.run import trial_status
    from evals.sandbox import GradeResult

    spec = Case(category="blocked", editable=[], tests=["test"], expected_terminal="failure")
    process = ProcessResult(returncode=1, seconds=1)
    graded = GradeResult(status="passed", process=ProcessResult(returncode=0, seconds=1))
    metrics = {**trace_metrics(""), "terminal": "failure", "provider_errors": 8}
    assert trial_status(spec, process, metrics, graded) == "agent_failure"
    metrics["conclusions"] = ["failure"]
    assert trial_status(spec, process, metrics, graded) == "passed"


@pytest.mark.parametrize(
    "scenario,observations",
    [
        ("large_output", {"truncated_observations": 1}),
        ("provider_retry", {"provider_errors": 1}),
        ("resume", {"unknown_executions": 1, "resolutions": 1}),
    ],
)
def test_runtime_scenario_must_actually_be_exercised(scenario: str, observations: dict) -> None:
    from evals.run import trial_status
    from evals.sandbox import GradeResult

    spec = Case.model_validate(
        {
            "category": "runtime",
            "editable": [],
            "tests": ["test"],
            "scenario": scenario,
        }
    )
    process = ProcessResult(returncode=0, seconds=1)
    graded = GradeResult(status="passed", process=process)
    metrics = {**trace_metrics(""), "terminal": "success"}
    assert trial_status(spec, process, metrics, graded) == "agent_failure"
    assert trial_status(spec, process, {**metrics, **observations}, graded) == "passed"


async def test_resume_fixture_seeds_a_valid_interrupted_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evals.seed_resume import main

    from rio.agent.session import Session
    from rio.agent.store import Store
    from rio.coding.cells import KINDS
    from rio.coding.owner import ProcessOwner

    home, workspace = tmp_path / "home", tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "seed.json"
    config.write_text(
        json.dumps(
            {
                "task": "Finish the charge",
                "provider": "fake",
                "model": "fake",
                "thinking": None,
            }
        )
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(sys, "argv", ["seed_resume.py", str(config)])
    main()
    path = home / ".rio/sessions" / ("a" * 32 + ".sqlite3")
    with Store(path) as store:
        session = Session(store, KINDS)
        assert session.data.executions["1"].phase == "started"
        async with ProcessOwner(session):
            result = session.data.executions["1"].result
            assert result is not None and result.type == "UnknownExecution"
            assert session.meta.pause is not None
    assert (workspace / "ledger.txt").read_text() == "charge-1\n"


def test_recovered_provider_error_does_not_mask_an_agent_failure() -> None:
    from evals.run import trial_status
    from evals.sandbox import GradeResult

    metrics = trace_metrics(
        "\n".join(
            json.dumps(event)
            for event in [
                {"type": "provider_failed", "changes": {}},
                {"type": "model_response", "changes": {"turns": {}}},
            ]
        )
    )
    assert metrics["provider_errors"] == 1 and not metrics["provider_failure_unresolved"]
    spec = Case(category="coding", editable=[], tests=["test"])
    process = ProcessResult(returncode=1, seconds=1)
    grade = GradeResult(status="passed", process=process)
    assert trial_status(spec, process, metrics, grade) == "agent_failure"
