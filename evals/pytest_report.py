"""Trusted pytest plugin: collection and every phase must match the case contract."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

collected: list[str] = []
results: dict[str, str] = {}


def nodeid(value: str) -> str:
    return "grade.py::" + value.split("grade.py::", 1)[1] if "grade.py::" in value else value


def pytest_collection_finish(session: Any) -> None:
    collected.extend(nodeid(item.nodeid) for item in session.items)


def pytest_runtest_logreport(report: Any) -> None:
    key = nodeid(report.nodeid)
    # Failed teardown overrides a passed call; a skipped call never counts as a pass.
    if (report.outcome != "passed" or report.when == "call") and results.get(key) not in (
        "failed",
        "skipped",
    ):
        results[key] = "skipped" if hasattr(report, "wasxfail") else report.outcome


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    Path(os.environ["RIO_EVAL_REPORT"]).write_text(
        json.dumps({"collected": collected, "tests": results, "exitstatus": exitstatus})
    )
