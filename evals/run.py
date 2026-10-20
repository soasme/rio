"""Run validated coding tasks in disposable containers and retain every scheduled trial."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import shutil
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from evals.metrics import aggregate, compare, trace_metrics
from evals.sandbox import (
    ROOT,
    Case,
    GradeResult,
    ProcessResult,
    build_runtime,
    build_task,
    capture_patch,
    checked,
    container,
    copy_project,
    export_base,
    files,
    grade,
    installed_workspace,
)
from rio.agent.state import Limits
from rio.coding.credentials import FileCredentialStore
from rio.coding.paths import RioPaths, atomic_write_text
from rio.coding.provider_config import (
    ProviderConfig,
    load_provider_settings,
    resolve_model_endpoint,
    resolve_provider_selection,
    resolve_startup_thinking_level,
)
from rio.coding.provider_runtime import OAuthRuntimeCredentialResolver
from rio.coding.thinking import normalize_thinking_level

CASES = ROOT / "evals/cases"
LOCKFILES = ("uv.lock", "poetry.lock", "pdm.lock", "Pipfile.lock")


def environment(workspace: Path) -> dict[str, Any]:
    locks = {
        name: hashlib.sha256((workspace / name).read_bytes()).hexdigest()
        for name in LOCKFILES
        if (workspace / name).is_file()
    }
    return {
        "lockfiles": locks,
        "distributions": sorted(
            path.name for path in workspace.glob(".venv/lib/python*/site-packages/*.dist-info")
        ),
    }


def environment_changes(before: dict, after: dict) -> dict[str, list[str]]:
    locks = before["lockfiles"].keys() | after["lockfiles"].keys()
    old, new = set(before["distributions"]), set(after["distributions"])
    return {
        "lockfiles_changed": sorted(
            n for n in locks if before["lockfiles"].get(n) != after["lockfiles"].get(n)
        ),
        "venv_added": sorted(new - old),
        "venv_removed": sorted(old - new),
    }


def digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def fingerprints(cases: list[Path]) -> dict[str, str]:
    """Rescan paths each time, including added files and non-Python runtime data."""
    catalog = RioPaths().home / "catalog.toml"
    return {
        "user_catalog_sha256": digest(catalog.read_bytes().hex() if catalog.exists() else None),
        "source_sha256": digest({k: v.hex() for k, v in files(ROOT / "src").items()}),
        "harness_sha256": digest(
            {p.name: p.read_bytes().hex() for p in (ROOT / "evals").iterdir() if p.is_file()}
        ),
        "dependencies_sha256": digest(
            {p: (ROOT / p).read_bytes().hex() for p in ("uv.lock", "pyproject.toml")}
        ),
        "fixtures_sha256": digest(
            {p.name: {k: v.hex() for k, v in files(p).items()} for p in cases}
        ),
    }


def save(path: Path, value: dict) -> None:
    atomic_write_text(path, json.dumps(value, indent=2) + "\n")


def overlay(source: Path, target: Path) -> None:
    for path in source.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            dest = target / path.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)


def validate_case(case: Path, base: Path, image: str, timeout: float) -> dict:
    """Calibrate the grader against broken, correct, and plausible incomplete submissions."""
    spec = Case.load(case)
    probes: dict[str, GradeResult] = {}
    probes["base"] = grade(case, base, base, image, timeout=timeout)
    with tempfile.TemporaryDirectory(prefix="rio-eval-reference-") as tmp:
        solution = Path(tmp) / "workspace"
        copy_project(base, solution)
        if (case / "reference.patch").is_file():
            checked(["git", "apply", str(case / "reference.patch")], cwd=solution)
        elif (case / "reference").is_dir():
            overlay(case / "reference", solution)
        else:
            raise ValueError(f"{case.name} has no reference solution")
        probes["reference"] = grade(case, base, solution, image, timeout=timeout)
        alternatives = sorted((case / "alternatives").glob("*"))
        for alternative in alternatives:
            candidate = Path(tmp) / ("alternative-" + alternative.name)
            copy_project(solution, candidate)
            overlay(alternative, candidate)
            probes["alternative-" + alternative.name] = grade(
                case, base, candidate, image, timeout=timeout
            )
        mutants = sorted((case / "mutants").glob("*"))
        if not mutants:
            raise ValueError(f"{case.name} has no negative control")
        for mutant in mutants:
            candidate = Path(tmp) / mutant.name
            copy_project(solution, candidate)
            overlay(mutant, candidate)
            probes[mutant.name] = grade(case, base, candidate, image, timeout=timeout)
    base_status = "passed" if spec.expected_terminal == "failure" else "incorrect"
    valid = (
        probes["base"].status == base_status
        and probes["reference"].status == "passed"
        and all(probes["alternative-" + p.name].status == "passed" for p in alternatives)
        and all(probes[p.name].status in ("incorrect", "integrity_error") for p in mutants)
    )
    return {
        "valid": valid,
        "probes": {name: result.model_dump() for name, result in probes.items()},
    }


def trial_home(path: Path, provider: ProviderConfig) -> dict[str, str]:
    """Copy only the selected provider credential; never mount the user's home or sessions."""
    rio = path / ".rio"
    rio.mkdir(parents=True)
    home = RioPaths().home
    if (home / "catalog.toml").exists():
        shutil.copy2(home / "catalog.toml", rio / "catalog.toml")
    (rio / "providers.json").write_text(json.dumps({"default_provider": provider.name}))
    source = FileCredentialStore()
    dest = FileCredentialStore(rio / "credentials.json")
    name = provider.credential_name
    if name:
        oauth = source.get_oauth(name)
        if oauth:
            oauth = asyncio.run(
                OAuthRuntimeCredentialResolver(provider, credential_store=source).credential()
            )
            dest.set_oauth(name, oauth)
        elif key := source.get(name):
            dest.set(name, key)
    env = {"HOME": "/root"}
    if provider.api_key_env in os.environ:
        env[provider.api_key_env] = os.environ[provider.api_key_env]
    return env


def run_case(
    case: Path,
    base: Path,
    image: str,
    *,
    provider: ProviderConfig,
    model: str,
    thinking: str | None,
    timeout: int,
    output: Path,
) -> dict[str, Any]:
    spec = Case.load(case)
    with tempfile.TemporaryDirectory(prefix=f"rio-eval-{case.name}-") as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        copy_project(base, workspace)
        checked(["git", "init", "-q", str(workspace)])
        checked(["git", "add", "-A"], cwd=workspace)
        checked(
            [
                "git",
                "-c",
                "user.name=eval",
                "-c",
                "user.email=eval@localhost",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--allow-empty",
                "-qm",
                "base",
            ],
            cwd=workspace,
        )
        home = root / "home"
        env = trial_home(home, provider)
        restriction = (
            "Only change these task files: " + ", ".join(spec.editable) + "."
            if spec.editable
            else "Do not change any project files."
        )
        task = (case / "task.md").read_text() + (
            "\n\n"
            + restriction
            + " Preserve all other project files. Do not fetch existing solutions online."
        )
        argv = [
            "/opt/rio/bin/python",
            "-c",
            "from rio.cli import main; main()",
            "run",
            task,
            "--cwd",
            "/workspace",
            "--output",
            "json",
            "--provider",
            provider.name,
            "--model",
            model,
        ]
        if thinking:
            argv += ["--thinking", thinking]
        mounts = {workspace: "/workspace", home: "/root"}
        if spec.scenario == "provider_retry":
            mounts[ROOT / "evals/inject_provider_failure.py"] = "/inject.py:ro"
            argv = [argv[0], "/inject.py", *argv[3:]]
        if spec.scenario == "resume":
            setup = root / "resume.json"
            setup.write_text(
                json.dumps(
                    {
                        "task": task,
                        "provider": provider.name,
                        "model": model,
                        "thinking": thinking,
                    }
                )
            )
            seeded = container(
                image,
                ["/opt/rio/bin/python", "/seed.py", "/seed.json"],
                mounts={
                    **mounts,
                    ROOT / "evals/seed_resume.py": "/seed.py:ro",
                    setup: "/seed.json:ro",
                },
                timeout=60,
                env=env,
            )
            if seeded.returncode != 0:
                raise RuntimeError(seeded.stderr)
            argv = [
                "/opt/rio/bin/python",
                "-c",
                "from rio.cli import main; main()",
                "run",
                "--resume",
                seeded.stdout.strip(),
                "--output",
                "json",
            ]
        process = container(image, argv, mounts=mounts, timeout=timeout, env=env, network=True)
        (output / "agent.jsonl").write_text(process.stdout)
        (output / "agent.stderr").write_text(process.stderr)
        changed = capture_patch(base, workspace, output / "agent.diff")
        metrics = trace_metrics(process.stdout)
        graded = grade(case, base, workspace, image, timeout=timeout)
        save(output / "grade.json", graded.model_dump())
        status = trial_status(spec, process, metrics, graded)
        return {
            "case": case.name,
            "status": status,
            "passed": status == "passed",
            "agent_exit": process.returncode,
            "grader_status": graded.status,
            "seconds": process.seconds,
            "grading_seconds": graded.process.seconds,
            "changed_paths": changed,
            **metrics,
        }


def trial_status(spec: Case, process: ProcessResult, metrics: dict, graded: GradeResult) -> str:
    expected_exit = 0 if spec.expected_terminal == "success" else 1
    if process.timed_out:
        return "agent_timeout"
    if metrics["terminal"] != spec.expected_terminal or process.returncode != expected_exit:
        return "provider_error" if metrics["provider_errors"] else "agent_failure"
    if graded.status != "passed":
        return graded.status
    if (
        (spec.expected_terminal == "failure" and "failure" not in metrics["conclusions"])
        or (
            spec.scenario == "resume"
            and (metrics["unknown_executions"] < 1 or metrics["resolutions"] < 1)
        )
        or (spec.scenario == "provider_retry" and metrics["provider_errors"] < 1)
        or (spec.scenario == "large_output" and metrics["truncated_observations"] < 1)
    ):
        return "agent_failure"
    return "passed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--thinking")
    parser.add_argument("--case", action="append")
    parser.add_argument("--split", choices=("regression", "holdout", "all"), default="regression")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--validate", action="store_true", help="Check graders without model calls")
    parser.add_argument("--compare", type=Path)
    parser.add_argument(
        "--output-dir", type=Path, default=Path(".eval-results") / time.strftime("%Y%m%d-%H%M%S")
    )
    parser.add_argument("--cache-dir", type=Path, default=ROOT / ".eval-cache")
    args = parser.parse_args()
    if args.trials < 1 or args.timeout < 1:
        parser.error("--trials and --timeout must be positive")
    available = {p.name: p for p in CASES.iterdir() if (p / "case.json").is_file()}
    names = args.case or sorted(
        name
        for name, p in available.items()
        if args.split == "all" or Case.load(p).split == args.split
    )
    if not names or len(names) != len(set(names)) or any(n not in available for n in names):
        parser.error("select nonempty, unique, known cases")
    cases = [available[n] for n in names]
    if args.output_dir.exists():
        parser.error("--output-dir must be new; prior reports are never overwritten")
    args.output_dir.mkdir(parents=True)
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    initial = fingerprints(cases)
    metadata: dict[str, Any] = {
        "schema_version": 2,
        **initial,
        "revision": checked(["git", "rev-parse", "HEAD"], cwd=ROOT),
        "dirty": bool(checked(["git", "status", "--porcelain"], cwd=ROOT)),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "timeout": args.timeout,
        "trials": 1 if args.validate else args.trials,
        "cases": names,
        "started_at": time.time(),
        "validation_only": args.validate,
        "policy": {"max_pass_rate_drop": 0.0, "max_latency_ratio": 1.2},
        "limits": asdict(Limits()),
        "sandbox": "docker",
        "agent_network": True,
    }
    selection = None
    thinking = None
    trials = [
        {"case": name, "trial": trial, "status": "pending", "passed": False}
        for trial in range(1, (1 if args.validate else args.trials) + 1)
        for name in names
    ]
    summary = {
        **metadata,
        "complete": False,
        "valid": False,
        "results": trials,
        "total": len(trials),
        "passed": 0,
        "finished": 0,
        "pass_rate": 0.0,
    }
    save(args.output_dir / "summary.json", summary)
    try:
        if not args.validate:
            selection = resolve_provider_selection(
                load_provider_settings(), provider_name=args.provider, model=args.model
            )
            thinking = resolve_startup_thinking_level(
                selection.provider,
                selection.model,
                cli_override=normalize_thinking_level(args.thinking) if args.thinking else None,
            )
            metadata.update(
                {
                    "provider": selection.provider.name,
                    "model": selection.model,
                    "thinking": thinking,
                    "endpoint_sha256": digest(
                        asdict(resolve_model_endpoint(selection.provider, selection.model))
                    ),
                }
            )
        runtime = build_runtime()
    except Exception as exc:
        for row in trials:
            row.update(status="setup_error", error=str(exc))
        summary.update(
            complete=True, finished=len(trials), per_case=aggregate(trials), error=str(exc)
        )
        save(args.output_dir / "summary.json", summary)
        print(str(exc))
        return 1
    metadata["runtime_image"] = runtime
    metadata["task_environments"] = {}
    with tempfile.TemporaryDirectory(prefix="rio-eval-batch-") as tmp:
        prepared: dict[str, tuple[Path, str]] = {}
        errors: dict[str, str] = {}
        for case in cases:
            try:
                base = Path(tmp) / case.name
                export_base(case, base, args.cache_dir)
                image = build_task(case, base, runtime)
                metadata["task_environments"][case.name] = installed_workspace(image, base)
                validation = validate_case(case, base, image, args.timeout)
                save(args.output_dir / f"{case.name}-validation.json", validation)
                print(
                    f"{case.name}: grader {'valid' if validation['valid'] else 'INVALID'}",
                    flush=True,
                )
                if not validation["valid"]:
                    raise ValueError("grader calibration failed; see validation report")
                prepared[case.name] = (base, image)
            except Exception as exc:
                errors[case.name] = str(exc)
        for row in trials:
            name = row["case"]
            output = args.output_dir / name / str(row["trial"])
            output.mkdir(parents=True)
            start = time.monotonic()
            try:
                if name in errors:
                    row.update(status="setup_error", error=errors[name])
                elif args.validate:
                    row.update(status="passed", passed=True)
                else:
                    assert selection is not None
                    base, image = prepared[name]
                    row.update(
                        run_case(
                            available[name],
                            base,
                            image,
                            provider=selection.provider,
                            model=selection.model,
                            thinking=thinking,
                            timeout=args.timeout,
                            output=output,
                        )
                    )
            except Exception as exc:
                row.update(status="harness_error", error=str(exc))
            row["total_seconds"] = time.monotonic() - start
            save(output / "result.json", row)
            finished = [r for r in trials if r["status"] != "pending"]
            summary = {
                **metadata,
                "complete": False,
                "valid": False,
                "results": trials,
                "total": len(trials),
                "finished": len(finished),
                "passed": sum(r["passed"] for r in trials),
                "per_case": aggregate(finished),
                "images": {n: image for n, (_, image) in prepared.items()},
            }
            save(args.output_dir / "summary.json", summary)
            print(f"{name} trial {row['trial']}: {row['status']}", flush=True)
    stable = fingerprints(cases) == initial
    summary.update(
        complete=True,
        valid=stable
        and not errors
        and not any(
            r["status"] in ("harness_error", "grader_error", "grader_timeout")
            or r.get("grader_status") in ("grader_error", "grader_timeout")
            for r in trials
        ),
        finished_at=time.time(),
        inputs_unchanged=stable,
        pass_rate=sum(r["passed"] for r in trials) / len(trials),
    )
    save(args.output_dir / "summary.json", summary)
    if args.compare:
        comparison = compare(json.loads(args.compare.read_text()), summary)
        save(args.output_dir / "comparison.json", comparison)
        if not comparison["gate_passed"]:
            return 1
    print(f"{summary['passed']}/{summary['total']} passed; valid={summary['valid']}")
    return 0 if summary["valid"] and all(r["passed"] for r in trials) else 1


if __name__ == "__main__":
    raise SystemExit(main())
