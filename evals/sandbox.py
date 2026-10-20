"""Disposable agent containers and clean, network-free grading environments."""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import psutil
from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parent.parent
IGNORED = {".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    split: Literal["regression", "holdout"] = "regression"
    category: str
    editable: list[str]
    tests: list[str] = Field(min_length=1)
    expected_terminal: Literal["success", "failure"] = "success"
    scenario: Literal["coding", "resume", "provider_retry", "large_output"] = "coding"

    @field_validator("tests")
    @classmethod
    def unique_tests(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("required tests must be unique")
        return value

    @classmethod
    def load(cls, path: Path) -> Case:
        return cls.model_validate_json((path / "case.json").read_text())


class ProcessResult(BaseModel):
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    seconds: float
    timed_out: bool = False


class GradeResult(BaseModel):
    status: Literal["passed", "incorrect", "grader_error", "grader_timeout", "integrity_error"]
    process: ProcessResult
    tests: dict[str, str] = Field(default_factory=dict)
    detail: str = ""


def command(
    argv: list[str],
    *,
    timeout: float = 600,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    start = time.monotonic()
    with subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            with contextlib.suppress(psutil.NoSuchProcess):
                for child in psutil.Process(process.pid).children(recursive=True):
                    with contextlib.suppress(psutil.NoSuchProcess):
                        child.kill()
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=10)
    return ProcessResult(
        returncode=None if timed_out else process.returncode,
        stdout=stdout,
        stderr=stderr,
        seconds=time.monotonic() - start,
        timed_out=timed_out,
    )


def checked(argv: list[str], *, cwd: Path | None = None, timeout: float = 600) -> str:
    result = command(argv, cwd=cwd, timeout=timeout)
    if result.returncode != 0:
        reason = f"timed out after {timeout}s" if result.timed_out else "failed"
        raise RuntimeError(f"{argv[:2]} {reason}: {result.stderr[-4000:]} {result.stdout[-4000:]}")
    return result.stdout.strip()


def copy_project(source: Path, target: Path) -> None:
    shutil.copytree(source, target, symlinks=True, ignore=lambda _p, names: set(names) & IGNORED)


def files(root: Path) -> dict[str, bytes]:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in IGNORED for part in relative.parts):
            continue
        if path.is_symlink():
            result[str(relative)] = b"symlink:" + os.readlink(path).encode()
        elif path.is_file():
            result[str(relative)] = bytes([bool(path.stat().st_mode & 0o111)]) + path.read_bytes()
    return result


def changed_paths(base: Path, candidate: Path) -> list[str]:
    old, new = files(base), files(candidate)
    return sorted(p for p in old.keys() | new.keys() if old.get(p) != new.get(p))


def capture_patch(base: Path, candidate: Path, output: Path) -> list[str]:
    """Use a private index: the agent cannot hide edits by staging or committing."""
    changed = changed_paths(base, candidate)
    with tempfile.TemporaryDirectory(prefix="rio-eval-diff-") as tmp:
        repo = Path(tmp) / "repo"
        copy_project(base, repo)
        checked(["git", "init", "-q", str(repo)])
        checked(["git", "add", "--all", "--force"], cwd=repo)
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
            cwd=repo,
        )
        for child in repo.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in candidate.iterdir():
            if child.name in IGNORED:
                continue
            dest = repo / child.name
            if child.is_symlink():
                dest.symlink_to(os.readlink(child))
            elif child.is_dir():
                copy_project(child, dest)
            else:
                shutil.copy2(child, dest)
        checked(["git", "add", "--all", "--force"], cwd=repo)
        patch = command(["git", "diff", "--cached", "--binary", "HEAD"], cwd=repo)
        if patch.returncode != 0:
            raise RuntimeError(patch.stderr)
        output.write_text(patch.stdout)
    return changed


def check_submission(base: Path, candidate: Path, spec: Case) -> list[str]:
    violations = []
    for name in changed_paths(base, candidate):
        path = candidate / name
        if (
            not any(fnmatch.fnmatchcase(name, pattern) for pattern in spec.editable)
            or path.is_symlink()
            or any((candidate / p).is_symlink() for p in Path(name).parents)
        ):
            violations.append(name)
    return violations


def export_base(case: Path, workspace: Path, cache: Path) -> None:
    if (case / "workspace").is_dir():
        copy_project(case / "workspace", workspace)
        return
    spec = json.loads((case / "swebench.json").read_text())
    mirror = cache / (spec["repo"].replace("/", "__") + ".git")
    if not mirror.exists():
        checked(
            [
                "git",
                "clone",
                "-q",
                "--bare",
                "--filter=blob:none",
                f"https://github.com/{spec['repo']}",
                str(mirror),
            ]
        )
    archive = workspace.parent / "base.tar"
    checked(["git", "-C", str(mirror), "archive", "-o", str(archive), spec["base_commit"]])
    workspace.mkdir()
    checked(["tar", "-xf", str(archive), "-C", str(workspace)])
    archive.unlink()


def build_runtime() -> str:
    """A content-addressed image includes Rio, its lockfile, and no eval fixtures."""
    with tempfile.TemporaryDirectory(prefix="rio-eval-image-") as tmp:
        context = Path(tmp)
        shutil.copytree(ROOT / "src", context / "src", ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE"):
            shutil.copy2(ROOT / name, context / name)
        shutil.copy2(ROOT / "evals/Dockerfile", context / "Dockerfile")
        tag = (
            "rio-eval-runtime:"
            + hashlib.sha256(
                b"".join(k.encode() + v for k, v in files(context).items())
            ).hexdigest()[:24]
        )
        checked(["docker", "build", "-q", "-t", tag, str(context)], timeout=900)
        return checked(["docker", "image", "inspect", "--format", "{{.Id}}", tag])


def build_task(case: Path, base: Path, runtime: str) -> str:
    spec: dict[str, Any] = {"python": "3.12", "install": ["pytest==8.4.2"], "version": "0"}
    if (case / "swebench.json").exists():
        spec = json.loads((case / "swebench.json").read_text())
    runtime_tag = "rio-eval-base:" + runtime.removeprefix("sha256:")
    checked(["docker", "tag", runtime, runtime_tag])
    with tempfile.TemporaryDirectory(prefix="rio-eval-task-image-") as tmp:
        context = Path(tmp)
        copy_project(base, context / "workspace")
        (context / "install.json").write_text(json.dumps(spec))
        (context / "Dockerfile").write_text(
            f"FROM {runtime_tag}\nCOPY workspace /workspace\nCOPY install.json /install.json\n"
            "WORKDIR /workspace\n"
            'RUN python -c \'import json,os,subprocess; s=json.load(open("/install.json")); '
            'subprocess.run(["uv","venv","--python",s["python"],"/opt/task"],check=True); '
            'subprocess.run(["uv","pip","install","--python","/opt/task",'
            '*s["install"]],env={**os.environ,"SETUPTOOLS_SCM_PRETEND_VERSION":s["version"]},'
            "check=True)'\nENV VIRTUAL_ENV=/opt/task\n"
            "ENV PATH=/opt/task/bin:/opt/rio/bin:/usr/local/bin:/usr/bin:/bin\n"
        )
        return checked(["docker", "build", "-q", str(context)], timeout=900).splitlines()[-1]


def installed_workspace(image: str, base: Path) -> dict:
    """Retain trusted generated metadata (e.g. pytest's _version.py) before mounting source."""
    name = checked(["docker", "create", image, "true"])
    try:
        checked(["docker", "cp", f"{name}:/workspace/.", str(base)])
    finally:
        checked(["docker", "rm", name])
    result = container(
        image,
        [
            "/opt/task/bin/python",
            "-c",
            "import json,platform,importlib.metadata as m; "
            "print(json.dumps({'python':platform.python_version(),"
            "'packages':sorted((d.metadata['Name'],d.version) for d in m.distributions())}))",
        ],
        mounts={},
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def container(
    image: str,
    argv: list[str],
    *,
    mounts: dict[Path, str],
    timeout: float,
    network: bool = False,
    env: dict[str, str] | None = None,
    readonly: bool = False,
) -> ProcessResult:
    name = "rio-eval-" + uuid.uuid4().hex
    args = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--init",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=256",
        "--memory=2g",
        "--cpus=2",
    ]
    if not network:
        args += ["--network=none"]
    if readonly:
        args += ["--read-only", "--tmpfs", "/tmp:rw,nosuid,size=512m", "--user", "65534:65534"]
    for source, destination in mounts.items():
        args += ["-v", f"{source.resolve()}:{destination}"]
    for key in env or {}:
        # Values live in the docker client's environment, never its command line.
        args += ["-e", key]
    args += [image, *argv]
    try:
        return command(args, timeout=timeout, env={**os.environ, **(env or {})})
    finally:
        # Killing the docker client alone does not stop its container or descendants.
        command(["docker", "rm", "-f", name], timeout=20)


def assess_grade(process: ProcessResult, report: Path, expected: list[str]) -> GradeResult:
    if process.timed_out:
        return GradeResult(status="grader_timeout", process=process)
    try:
        payload = json.loads(report.read_text())
        observed = payload["tests"]
        collected = payload["collected"]
        if (
            not isinstance(observed, dict)
            or set(collected) != set(expected)
            or len(collected) != len(expected)
        ):
            raise ValueError("collected tests differ from required tests")
        if set(observed) != set(expected):
            raise ValueError("required tests did not all finish")
        status: Literal["passed", "incorrect", "grader_error"]
        if process.returncode == 0 and all(value == "passed" for value in observed.values()):
            status = "passed"
        elif process.returncode in (0, 1):
            status = "incorrect"
        else:
            status = "grader_error"
        return GradeResult(status=status, process=process, tests=observed)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return GradeResult(status="grader_error", process=process, detail=str(exc))


def grade(case: Path, base: Path, candidate: Path, image: str, *, timeout: float) -> GradeResult:
    spec = Case.load(case)
    violations = check_submission(base, candidate, spec)
    if violations:
        return GradeResult(
            status="integrity_error",
            detail=", ".join(violations),
            process=ProcessResult(returncode=None, seconds=0),
        )
    with tempfile.TemporaryDirectory(prefix="rio-eval-grade-") as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        copy_project(candidate, workspace)
        if (case / "test.patch").is_file():
            checked(["git", "apply", str(case / "test.patch")], cwd=workspace)
        grading = root / "grading"
        grading.mkdir()
        shutil.copy2(ROOT / "evals/pytest_report.py", grading / "rio_eval_report.py")
        if (case / "grade.py").is_file():
            shutil.copy2(case / "grade.py", grading / "grade.py")
        reports = root / "reports"
        reports.mkdir(mode=0o777)
        reports.chmod(0o777)
        tests = ["/grading/grade.py"] if (case / "grade.py").is_file() else spec.tests
        result = container(
            image,
            [
                "/opt/task/bin/python",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "rio_eval_report",
                *tests,
            ],
            mounts={workspace: "/workspace:ro", grading: "/grading:ro", reports: "/reports"},
            timeout=timeout,
            readonly=True,
            env={
                "PYTHONPATH": "/grading:/workspace",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "RIO_EVAL_REPORT": "/reports/tests.json",
            },
        )
        return assess_grade(result, reports / "tests.json", spec.tests)
