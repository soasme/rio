"""Run Rio against isolated coding tasks and grade the resulting files."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CASES = Path(__file__).resolve().parent / "cases"


def run_case(
    case: Path, *, provider: str | None, model: str | None, timeout: int, output: Path
) -> dict[str, object]:
    """Run one trial; keep graders outside the directory given to Rio."""
    with tempfile.TemporaryDirectory(prefix=f"rio-eval-{case.name}-") as temporary:
        workspace = Path(temporary) / "workspace"
        shutil.copytree(case / "workspace", workspace)
        command = [
            sys.executable,
            "-c",
            "from rio.cli import main; main()",
            "run",
            (case / "task.md").read_text(encoding="utf-8"),
            "--cwd",
            str(workspace),
            "--no-approve",
            "--output",
            "json",
        ]
        if provider:
            command.extend(("--provider", provider))
        if model:
            command.extend(("--model", model))
        start = time.monotonic()
        try:
            agent = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout, check=False
            )
            agent_exit: int | None = agent.returncode
            agent_stdout, agent_stderr = agent.stdout, agent.stderr
        except subprocess.TimeoutExpired as exc:
            agent_exit = None
            agent_stdout = _decode(exc.stdout)
            agent_stderr = _decode(exc.stderr) + f"\nTimed out after {timeout}s"
        duration = round(time.monotonic() - start, 2)
        output.mkdir(parents=True, exist_ok=True)
        (output / "agent.jsonl").write_text(agent_stdout, encoding="utf-8")
        (output / "agent.stderr").write_text(agent_stderr, encoding="utf-8")

        grader = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(case / "grade.py")],
            cwd=workspace,
            env={**os.environ, "PYTHONPATH": str(workspace)},
            capture_output=True,
            text=True,
            check=False,
        )
        (output / "grade.txt").write_text(grader.stdout + grader.stderr, encoding="utf-8")
        shutil.copytree(
            workspace,
            output / "workspace",
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
        )
        result: dict[str, object] = {
            "case": case.name,
            "passed": agent_exit == 0 and grader.returncode == 0,
            "agent_exit": agent_exit,
            "grader_exit": grader.returncode,
            "seconds": duration,
        }
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", help="Rio provider; defaults to saved selection")
    parser.add_argument("--model", help="Rio model; defaults to provider selection")
    parser.add_argument("--case", action="append", help="Case name; defaults to all cases")
    parser.add_argument("--trials", type=int, default=1, help="Independent attempts per case")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds allowed per Rio run")
    parser.add_argument("--output-dir", type=Path, default=Path(".eval-results"))
    args = parser.parse_args()
    if args.trials < 1 or args.timeout < 1:
        parser.error("--trials and --timeout must be positive")
    names = args.case or sorted(path.name for path in CASES.iterdir() if path.is_dir())
    for name in names:
        if not name.isidentifier() or not (CASES / name / "task.md").is_file():
            parser.error(f"unknown case: {name}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for name in names:
        for trial in range(1, args.trials + 1):
            result = run_case(
                CASES / name,
                provider=args.provider,
                model=args.model,
                timeout=args.timeout,
                output=args.output_dir / name / str(trial),
            )
            results.append(result)
            print(f"{name} trial {trial}: {'pass' if result['passed'] else 'fail'}", flush=True)
    passed = sum(bool(result["passed"]) for result in results)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    summary = {
        "provider": args.provider,
        "model": args.model,
        "revision": revision,
        "passed": passed,
        "total": len(results),
        "pass_rate": passed / len(results),
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps({**summary, "results": results}, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{passed}/{len(results)} passed; details: {args.output_dir / 'summary.json'}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
