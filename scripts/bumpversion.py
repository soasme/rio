#!/usr/bin/env python3
"""Prepare a release by updating the changelog, project version, and lockfile."""

from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
from pathlib import Path

VERSION_PATTERN = re.compile(r"^version = \"([^\"]+)\"$", re.MULTILINE)
RELEASE_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
PULL_REQUEST_PATTERN = re.compile(r" \(#(\d+)\)$")


def run(*args: str, cwd: Path) -> str:
    return subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    ).stdout


def run_checked(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True)


def repository_root() -> Path:
    return Path(run("git", "rev-parse", "--show-toplevel", cwd=Path.cwd()).strip())


def origin_url(root: Path) -> str:
    return run("git", "remote", "get-url", "origin", cwd=root).strip()


def latest_release_commit(root: Path) -> str:
    commits = run("git", "log", "--format=%H%x00%s", cwd=root).splitlines()
    for commit in commits:
        commit_id, subject = commit.split("\0", maxsplit=1)
        if subject.startswith("chore(release): "):
            return commit_id
    raise RuntimeError("No chore(release) commit found.")


def changelog_entries(root: Path, release_commit: str) -> list[str]:
    subjects = run("git", "log", "--format=%s", f"{release_commit}..HEAD", cwd=root).splitlines()
    entries = []
    for subject in subjects:
        subject = subject.rstrip(".")
        entry = PULL_REQUEST_PATTERN.sub(
            lambda match: f" ([#{match.group(1)}](https://github.com/soasme/rio/pull/{match.group(1)}))",
            subject,
        )
        entries.append(f"* {entry}")
    return entries


def update_pyproject(path: Path, version: str) -> None:
    content = path.read_text(encoding="utf-8")
    match = VERSION_PATTERN.search(content)
    if match is None:
        raise RuntimeError(f"Could not find a project version in {path}.")
    if match.group(1) == version:
        raise RuntimeError(f"Project is already at version {version}.")
    path.write_text(
        content[: match.start(1)] + version + content[match.end(1) :],
        encoding="utf-8",
    )


def bumped_version(path: Path, part: str) -> str:
    content = path.read_text(encoding="utf-8")
    match = VERSION_PATTERN.search(content)
    if match is None:
        raise RuntimeError(f"Could not find a project version in {path}.")
    version_match = RELEASE_VERSION_PATTERN.fullmatch(match.group(1))
    if version_match is None:
        raise RuntimeError(f"Project version is not major.minor.patch: {match.group(1)}")

    major, minor, patch = map(int, version_match.groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def update_changelog(path: Path, version: str, entries: list[str]) -> None:
    content = path.read_text(encoding="utf-8")
    if f"# {version}\n" in content:
        raise RuntimeError(f"CHANGELOG.md already contains version {version}.")
    entries_text = "\n".join(entries)
    path.write_text(f"# {version}\n\n{entries_text}\n\n{content}", encoding="utf-8")


def prepare_release(root: Path, part: str) -> str:
    version = bumped_version(root / "pyproject.toml", part)
    release_commit = latest_release_commit(root)
    entries = changelog_entries(root, release_commit)
    if not entries:
        raise RuntimeError("No commits found since the latest chore(release) commit.")

    update_changelog(root / "CHANGELOG.md", version, entries)
    update_pyproject(root / "pyproject.toml", version)
    run_checked("uv", "lock", cwd=root)
    run_checked("uv", "sync", "--locked", "--dev", cwd=root)
    run_checked("uv", "run", "--no-sync", "ruff", "check", ".", cwd=root)
    run_checked("uv", "run", "--no-sync", "pytest", cwd=root)
    return version


def print_plan(root: Path) -> None:
    print("Release plan:")
    print(run("git", "diff", "--", "CHANGELOG.md", "pyproject.toml", "uv.lock", cwd=root), end="")


def commit_release(root: Path, version: str) -> None:
    run_checked("git", "add", "CHANGELOG.md", "pyproject.toml", "uv.lock", cwd=root)
    run_checked("git", "commit", "-m", f"chore(release): {version}", cwd=root)
    run_checked("git", "tag", f"v{version}", cwd=root)
    run_checked("git", "push", cwd=root)
    run_checked("git", "push", "origin", f"v{version}", cwd=root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "part",
        choices=("major", "minor", "patch"),
        help="version component to bump",
    )
    parser.add_argument(
        "--commit",
        default="main",
        help="branch, tag, or commit to release from (default: main)",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="commit, tag, and push the planned release",
    )
    args = parser.parse_args()

    root = repository_root()
    with tempfile.TemporaryDirectory(prefix="rio-bumpversion-") as temporary_directory:
        clone = Path(temporary_directory) / "rio"
        run_checked("git", "clone", "--quiet", origin_url(root), str(clone), cwd=root)
        run_checked("git", "checkout", args.commit, cwd=clone)
        version = prepare_release(clone, args.part)
        print_plan(clone)
        if args.yes:
            commit_release(clone, version)


if __name__ == "__main__":
    main()
