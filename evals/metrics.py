"""Comparable outcome and cost measurements; missing usage stays unknown."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict


def trace_metrics(trace: str) -> dict:
    rounds = executions = errors = unknowns = 0
    input_bytes = 0
    usage = None
    durability = {}
    for line in trace.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        kind = event.get("type")
        changes = event.get("changes", {})
        if kind == "turn_prepared":
            rounds += 1
            turn = next(iter(changes["turns"].values()))
            input_bytes += len(json.dumps(turn["state"]).encode())
        elif kind == "execution_started":
            executions += 1
        elif kind == "execution_finished":
            for execution in changes["executions"].values():
                result = execution["result"]["type"]
                errors += result == "error"
                unknowns += result == "UnknownExecution"
        if "usage" in changes.get("meta", {}):
            usage = changes["meta"]["usage"]
        if kind == "durability_metrics":
            durability.update(changes.get("metrics", {}))
    return {
        "model_rounds": rounds,
        "executions": executions,
        "execution_errors": errors,
        "unknown_executions": unknowns,
        "context_bytes": input_bytes,
        "tokens": usage["tokens"] if usage and not usage["unknown_usage"] else None,
        "known_tokens": usage["tokens"] if usage else None,
        "unknown_usage_attempts": usage["unknown_usage"] if usage else None,
        "commit_p50_seconds": durability.get("commit_p50_seconds"),
        "rebuild_seconds": durability.get("rebuild_seconds"),
    }


def aggregate(results: list[dict]) -> dict:
    groups = defaultdict(list)
    for result in results:
        groups[result["case"]].append(result)
    return {
        case: {
            "trials": len(rows),
            "passed": sum(r["passed"] for r in rows),
            "pass_rate": sum(r["passed"] for r in rows) / len(rows),
            "median_seconds": statistics.median(r["seconds"] for r in rows),
            "median_success_seconds": statistics.median([r["seconds"] for r in rows if r["passed"]])
            if any(r["passed"] for r in rows)
            else None,
            "median_model_rounds": statistics.median(r["model_rounds"] for r in rows),
            "median_context_bytes": statistics.median(r["context_bytes"] for r in rows),
        }
        for case, rows in groups.items()
    }


def compare(baseline: dict, candidate: dict) -> dict:
    """Report each case separately. Never label a faster failed trial an improvement."""
    for field in ("provider", "model", "timeout", "fixtures_sha256", "platform", "python"):
        if baseline.get(field) != candidate.get(field):
            raise ValueError(f"Comparison requires matching {field}")
    before, after = aggregate(baseline["results"]), aggregate(candidate["results"])
    if before.keys() != after.keys():
        raise ValueError("Comparison requires matching cases")
    comparison = {}
    for case, old in before.items():
        new = after[case]
        if old["trials"] != new["trials"]:
            raise ValueError("Comparison requires matching trial counts per case")
        a, b = old["median_success_seconds"], new["median_success_seconds"]
        comparison[case] = {
            "baseline": old,
            "candidate": new,
            "pass_rate_delta": new["pass_rate"] - old["pass_rate"],
            "successful_latency_ratio": b / a if a and b is not None else None,
            "observed_improvement": new["pass_rate"] > old["pass_rate"]
            or (new["pass_rate"] == old["pass_rate"] == 1 and b < a),
        }
    return {
        "cases": comparison,
        "note": "Observed differences are descriptive, not evidence of statistical significance. "
        "Repeat trials on both revisions; inspect failures and report regressions.",
    }
