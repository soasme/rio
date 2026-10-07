"""Run Rio against isolated coding tasks and grade the resulting files."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

from evals.metrics import aggregate, compare, trace_metrics

CASES = Path(__file__).resolve().parent / "cases"


def prepare_workspace(case: Path, workspace: Path, cache: Path) -> None:
    """Copy a local workspace, or check out a SWE-bench repository with its own venv."""
    if (case / "workspace").is_dir():
        shutil.copytree(case / "workspace", workspace)
        return
    spec = json.loads((case / "swebench.json").read_text(encoding="utf-8"))
    mirror = cache / (spec["repo"].replace("/", "__") + ".git")
    if not mirror.exists():
        url = f"https://github.com/{spec['repo']}"
        _run("git", "clone", "-q", "--bare", "--filter=blob:none", url, str(mirror))
    # Export one commit into a fresh repository so later history, including the fix, is absent.
    workspace.mkdir()
    archive = subprocess.run(
        ["git", "-C", str(mirror), "archive", spec["base_commit"]], capture_output=True, check=True
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(workspace)], input=archive, check=True)
    with (workspace / ".gitignore").open("a", encoding="utf-8") as gitignore:
        gitignore.write("\n.venv/\n")
    _run("git", "init", "-q", cwd=workspace)
    _run("git", "add", "-A", cwd=workspace)
    _run(
        "git",
        "-c",
        "user.name=eval",
        "-c",
        "user.email=eval@localhost",
        "commit",
        "-qm",
        "base",
        cwd=workspace,
    )
    _run("uv", "venv", "-q", "--python", spec["python"], ".venv", cwd=workspace)
    _run(
        "uv",
        "pip",
        "install",
        "-q",
        "--python",
        ".venv",
        *spec["install"],
        cwd=workspace,
        env={**os.environ, "SETUPTOOLS_SCM_PRETEND_VERSION": spec["version"]},
    )


def grade(case: Path, workspace: Path) -> subprocess.CompletedProcess[str]:
    """Run grade.py, or apply the hidden SWE-bench test patch and run its tests."""
    if (case / "grade.py").is_file():
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", str(case / "grade.py")],
            cwd=workspace,
            env={**os.environ, "PYTHONPATH": str(workspace)},
            capture_output=True,
            text=True,
            check=False,
        )
    spec = json.loads((case / "swebench.json").read_text(encoding="utf-8"))
    patch = str(case / "test.patch")
    # Restore test files the agent may have edited so the patch applies cleanly.
    paths = subprocess.run(
        ["git", "apply", "--numstat", patch],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in paths.splitlines():
        subprocess.run(
            ["git", "checkout", "HEAD", "--", line.split("\t")[2]],
            cwd=workspace,
            capture_output=True,
            check=False,
        )
    applied = subprocess.run(
        ["git", "apply", patch], cwd=workspace, capture_output=True, text=True, check=False
    )
    if applied.returncode != 0:
        return applied
    return subprocess.run(
        [
            str(workspace / ".venv" / "bin" / "python"),
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *spec["tests"],
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )


def _run(*command: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def run_case(
    case: Path,
    *,
    provider: str | None,
    model: str | None,
    timeout: int,
    output: Path,
    cache: Path,
    runtime: str = "durable",
) -> dict[str, object]:
    """Run one trial; keep graders outside the directory given to Rio."""
    with tempfile.TemporaryDirectory(prefix=f"rio-eval-{case.name}-") as temporary:
        workspace = Path(temporary) / "workspace"
        prepare_workspace(case, workspace, cache)
        env = dict(os.environ)
        venv = workspace / ".venv"
        if venv.is_dir():
            # Let `python` and `pytest` in Rio's shell resolve to the project environment.
            env["VIRTUAL_ENV"] = str(venv)
            env["PATH"] = f"{venv / 'bin'}{os.pathsep}{env.get('PATH', '')}"
        command = [
            sys.executable,
            "-c",
            "from rio.cli import main; main()",
            "run",
            "--runtime",
            runtime,
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
        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        ) as agent:
            try:
                agent_stdout, agent_stderr = agent.communicate(timeout=timeout)
                agent_exit: int | None = agent.returncode
            except subprocess.TimeoutExpired:
                children = psutil.Process(agent.pid).children(recursive=True)
                for child in children:
                    with contextlib.suppress(psutil.NoSuchProcess):
                        child.kill()
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(agent.pid, signal.SIGKILL)
                agent_stdout, agent_stderr = agent.communicate(timeout=10)
                agent_stderr += f"\nTimed out after {timeout}s"
                agent_exit = None
        duration = round(time.monotonic() - start, 2)
        output.mkdir(parents=True, exist_ok=True)
        (output / "agent.jsonl").write_text(agent_stdout, encoding="utf-8")
        (output / "agent.stderr").write_text(agent_stderr, encoding="utf-8")

        if (workspace / ".git").is_dir():
            # A repository is too large to copy per trial; keep the agent's diff instead.
            subprocess.run(["git", "add", "-A", "-N"], cwd=workspace, check=False)
            diff = subprocess.run(
                ["git", "diff"], cwd=workspace, capture_output=True, text=True, check=False
            ).stdout
            (output / "agent.diff").write_text(diff, encoding="utf-8")
        else:
            shutil.copytree(
                workspace,
                output / "workspace",
                ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
            )
        grader = grade(case, workspace)
        (output / "grade.txt").write_text(grader.stdout + grader.stderr, encoding="utf-8")
        result: dict[str, object] = {
            "case": case.name,
            "passed": agent_exit == 0 and grader.returncode == 0,
            "agent_exit": agent_exit,
            "grader_exit": grader.returncode,
            "seconds": duration,
            "runtime": runtime,
            **trace_metrics(agent_stdout),
        }
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result


def fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(CASES.parent.parent)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("durable", "notebook"), default="durable")
    parser.add_argument(
        "--paired",
        action="store_true",
        help="Alternate notebook and durable trials under identical settings",
    )
    parser.add_argument("--compare", type=Path, help="Baseline summary.json to compare per case")
    parser.add_argument("--provider", help="Rio provider; defaults to saved selection")
    parser.add_argument("--model", help="Rio model; defaults to provider selection")
    parser.add_argument("--case", action="append", help="Case name; defaults to all cases")
    parser.add_argument("--trials", type=int, default=1, help="Independent attempts per case")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds allowed per Rio run")
    parser.add_argument("--output-dir", type=Path, default=Path(".eval-results"))
    parser.add_argument(
        "--cache-dir", type=Path, default=Path(".eval-cache"), help="SWE-bench repo mirrors"
    )
    args = parser.parse_args()
    if args.trials < 1 or args.timeout < 1:
        parser.error("--trials and --timeout must be positive")
    cases = sorted(path.name for path in CASES.iterdir() if (path / "task.md").is_file())
    names = args.case or cases
    for name in names:
        if name not in cases:
            parser.error(f"unknown case: {name}")
    if args.paired and (args.compare or args.runtime != "durable"):
        parser.error("--paired cannot be combined with --compare or --runtime notebook")
    if not args.provider or not args.model:
        from rio.coding.provider_config import load_provider_settings, resolve_provider_selection

        selection = resolve_provider_selection(
            load_provider_settings(), provider_name=args.provider, model=args.model
        )
        args.provider, args.model = selection.provider.name, selection.model
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    root = CASES.parent.parent
    sources = list((root / "src").rglob("*.py")) + [root / "pyproject.toml"]
    metadata = {
        "timeout": args.timeout,
        "provider": args.provider,
        "model": args.model,
        "revision": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip(),
        "source_sha256": fingerprint(sources),
        "fixtures_sha256": fingerprint(
            [
                p
                for name in names
                for p in (CASES / name).rglob("*")
                if p.is_file() and "__pycache__" not in p.parts
            ]
        ),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    runtimes = ["notebook", "durable"] if args.paired else [args.runtime]
    results = {runtime: [] for runtime in runtimes}
    summaries = {}
    for trial in range(1, args.trials + 1):
        for index, name in enumerate(names):
            order = runtimes if (trial + index) % 2 else list(reversed(runtimes))
            for runtime in order:
                destination = args.output_dir / runtime if args.paired else args.output_dir
                result = run_case(
                    CASES / name,
                    provider=args.provider,
                    model=args.model,
                    timeout=args.timeout,
                    output=destination.resolve() / name / str(trial),
                    cache=args.cache_dir.resolve(),
                    runtime=runtime,
                )
                result["trial"] = trial
                results[runtime].append(result)
                rows = results[runtime]
                passed = sum(bool(row["passed"]) for row in rows)
                summaries[runtime] = {
                    **metadata,
                    "runtime": runtime,
                    "passed": passed,
                    "total": len(rows),
                    "pass_rate": passed / len(rows),
                    "per_case": aggregate(rows),
                    "results": rows,
                }
                (destination / "summary.json").write_text(
                    json.dumps(summaries[runtime], indent=2) + "\n", encoding="utf-8"
                )
                print(
                    f"{runtime} {name} trial {trial}: "
                    f"{'pass' if result['passed'] else 'fail'} ({result['seconds']}s)",
                    flush=True,
                )
    if fingerprint(sources) != metadata["source_sha256"]:
        raise RuntimeError("Source changed during evaluation; rerun before comparing")
    baseline = (
        summaries["notebook"]
        if args.paired
        else json.loads(args.compare.read_text())
        if args.compare
        else None
    )
    if baseline:
        comparison = compare(baseline, summaries[args.runtime])
        (args.output_dir / "comparison.json").write_text(json.dumps(comparison, indent=2) + "\n")
        for name, row in comparison["cases"].items():
            print(
                f"{name}: pass-rate delta {row['pass_rate_delta']:+.2f}; "
                f"successful latency ratio {row['successful_latency_ratio']}"
            )
    for runtime, summary in summaries.items():
        print(f"{runtime}: {summary['passed']}/{summary['total']} passed")
    return 0 if all(r["passed"] for rows in results.values() for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
