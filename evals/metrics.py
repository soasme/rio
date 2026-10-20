"""Comparable outcome and cost measurements; missing usage stays unknown."""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict


def trace_metrics(trace: str) -> dict:
    turns = executions = errors = unknowns = 0
    input_bytes = 0
    usage = None
    durability = {}
    terminal = None
    provider_errors = resolutions = 0
    costs: list[float] = []
    unknown_costs = 0
    conclusions: list[str] = []
    truncated_observations = 0
    for line in trace.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        kind = event.get("type")
        changes = event.get("changes", {})
        provider_errors += kind == "provider_failed"
        truncated_observations += kind == "output_truncated"
        resolutions += len(changes.get("resolutions", {}))
        for control in changes.get("controls", {}).values():
            request = control.get("control", {})
            if request.get("type") == "conclusion":
                conclusions.append(request["result"])
        ending = changes.get("meta", {}).get("terminal")
        if ending:
            terminal = ending["result"]
        if kind == "turn_prepared":
            turns += 1
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
        if kind == "model_response":
            for turn in changes.get("turns", {}).values():
                cost = turn["response"].get("usage", {}).get("cost", {}).get("total")
                if isinstance(cost, int | float) and cost > 0:
                    costs.append(float(cost))
                else:
                    unknown_costs += 1
        durability.update(changes.get("metrics", {}))
    return {
        "model_turns": turns,
        "executions": executions,
        "execution_errors": errors,
        "unknown_executions": unknowns,
        "context_bytes": input_bytes,
        "tokens": usage["tokens"] if usage and not usage["unknown_usage"] else None,
        "known_tokens": usage["tokens"] if usage else None,
        "unknown_usage_attempts": usage["unknown_usage"] if usage else None,
        "commit_p50_seconds": durability.get("commit_p50_seconds"),
        "rebuild_seconds": durability.get("rebuild_seconds"),
        "terminal": terminal,
        "provider_errors": provider_errors,
        "resolutions": resolutions,
        "conclusions": conclusions,
        "truncated_observations": truncated_observations,
        "dollars": sum(costs)
        if costs and not unknown_costs and usage and not usage["unknown_usage"]
        else None,
    }


def wilson(passed: int, trials: int) -> list[float]:
    """95% Wilson interval for one task's independent attempts."""
    z = 1.959963984540054
    p = passed / trials
    denominator = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denominator
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return [max(0.0, center - half), min(1.0, center + half)]


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def aggregate(results: list[dict]) -> dict:
    groups = defaultdict(list)
    for result in results:
        groups[result["case"]].append(result)
    output = {}
    for case, rows in groups.items():
        passed = sum(r["passed"] for r in rows)
        times = [r["seconds"] for r in rows if "seconds" in r]
        successful = [r["seconds"] for r in rows if r["passed"] and "seconds" in r]
        turns = [r.get("model_turns", r.get("model_rounds", 0)) for r in rows]
        tokens = [r.get("tokens") for r in rows]
        dollars = [r.get("dollars") for r in rows]
        output[case] = {
            "trials": len(rows),
            "passed": passed,
            "pass_rate": passed / len(rows),
            "pass_rate_ci95": wilson(passed, len(rows)),
            "median_seconds": statistics.median(times) if times else None,
            "p95_seconds": percentile(times, 0.95),
            "median_success_seconds": statistics.median(successful) if successful else None,
            "median_model_turns": statistics.median(turns),
            "median_context_bytes": statistics.median(r.get("context_bytes", 0) for r in rows),
            "statuses": dict(
                Counter(r.get("status", "passed" if r["passed"] else "incorrect") for r in rows)
            ),
            "timeout_rate": sum(r.get("status") == "agent_timeout" for r in rows) / len(rows),
            # Failed attempts contribute to the numerator. Unknown usage stays unknown.
            "tokens_per_success": sum(t for t in tokens if t is not None) / passed
            if passed and all(t is not None for t in tokens)
            else None,
            "seconds_per_success": sum(times) / passed
            if passed and len(times) == len(rows)
            else None,
            "dollars_per_success": sum(d for d in dollars if d is not None) / passed
            if passed and all(d is not None for d in dollars)
            else None,
        }
    return output


def compare(baseline: dict, candidate: dict) -> dict:
    """Report each case separately. Never label a faster failed trial an improvement."""
    required = (
        "schema_version",
        "provider",
        "model",
        "thinking",
        "timeout",
        "fixtures_sha256",
        "harness_sha256",
        "dependencies_sha256",
        "endpoint_sha256",
        "platform",
        "python",
        "limits",
        "sandbox",
        "agent_network",
        "policy",
        "task_environments",
    )
    for run in (baseline, candidate):
        if run.get("schema_version") != 2 or not run.get("complete") or not run.get("valid"):
            raise ValueError("Comparison requires complete, valid version-2 runs")
        if run.get("validation_only"):
            raise ValueError("Comparison requires model trials")
        planned = {(name, trial) for name in run["cases"] for trial in range(1, run["trials"] + 1)}
        observed = [(r["case"], r["trial"]) for r in run["results"]]
        if set(observed) != planned or len(observed) != len(planned):
            raise ValueError("Comparison requires every scheduled trial exactly once")
    for field in required:
        if field not in baseline or field not in candidate or baseline[field] != candidate[field]:
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
            or (
                new["pass_rate"] == old["pass_rate"] == 1
                and a is not None
                and b is not None
                and b < a
            ),
            "regression": (
                new["pass_rate"] < old["pass_rate"] - candidate["policy"]["max_pass_rate_drop"]
                or (
                    new["pass_rate"] == old["pass_rate"] == 1
                    and a is not None
                    and b is not None
                    and b > a * candidate["policy"]["max_latency_ratio"]
                )
            ),
        }
    return {
        "cases": comparison,
        "gate_passed": not any(row["regression"] for row in comparison.values()),
        "note": "Observed differences are descriptive, not evidence of statistical significance. "
        "Repeat trials on both revisions; inspect failures and report regressions.",
    }
