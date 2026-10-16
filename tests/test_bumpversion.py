"""Release preparation keeps model refresh optional and includes its output."""

from pathlib import Path
from unittest.mock import Mock, call

import pytest
from scripts import bumpversion


@pytest.mark.parametrize(
    "flags, refresh", [([], True), (["--refresh-models"], True), (["--no-refresh-models"], False)]
)
@pytest.mark.parametrize("publish", [False, True])
def test_release_model_refresh(monkeypatch, tmp_path, flags, refresh, publish):
    catalog = "src/rio/coding/data/models-dev-catalog.json"
    commands = []
    snapshots = []

    def run_checked(*args, cwd):
        commands.append((args, cwd))
        if args[:2] == ("git", "clone"):
            clone = Path(args[-1])
            clone.mkdir()
            (clone / "pyproject.toml").write_text('version = "1.2.3"\n')
            (clone / "CHANGELOG.md").write_text("# 1.2.3\n")
            (clone / catalog).parent.mkdir(parents=True)
            (clone / catalog).write_text("original")
        if args[-1] == "scripts/vendor_bundled_models.py":
            (cwd / catalog).write_text("refreshed")
        if args[-1] == "pytest":
            snapshots.append((cwd / catalog).read_text())
            assert (cwd / "pyproject.toml").read_text() == 'version = "1.2.4"\n'
            assert (cwd / "CHANGELOG.md").read_text().startswith("# 1.2.4\n")

    diff = Mock(return_value="release diff\n")
    monkeypatch.setattr(
        "sys.argv", ["bumpversion.py", "patch", *flags, *(["-y"] if publish else [])]
    )
    monkeypatch.setattr(bumpversion, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(bumpversion, "origin_url", lambda root: "test-origin")
    monkeypatch.setattr(bumpversion, "latest_release_commit", lambda root: "release")
    monkeypatch.setattr(bumpversion, "changelog_entries", lambda root, commit: ["* fix: example"])
    monkeypatch.setattr(bumpversion, "run_checked", run_checked)
    monkeypatch.setattr(bumpversion, "run", diff)

    bumpversion.main()

    clone = commands[1][1]
    refresh_command = ("uv", "run", "--no-sync", "python", "scripts/vendor_bundled_models.py")
    expected = [
        ("git", "checkout", "main"),
        ("uv", "lock"),
        ("uv", "sync", "--locked", "--dev"),
        *([refresh_command] if refresh else []),
        ("uv", "run", "--no-sync", "ruff", "check", "."),
        ("uv", "run", "--no-sync", "pytest"),
    ]
    if publish:
        expected += [
            ("git", "add", "CHANGELOG.md", "pyproject.toml", "uv.lock", catalog),
            ("git", "commit", "-m", "chore(release): 1.2.4"),
            ("git", "tag", "v1.2.4"),
            ("git", "push"),
            ("git", "push", "origin", "v1.2.4"),
        ]
    assert commands[1:] == [(command, clone) for command in expected]
    assert snapshots == ["refreshed" if refresh else "original"]
    assert diff.call_args == call(
        "git", "diff", "--", "CHANGELOG.md", "pyproject.toml", "uv.lock", catalog, cwd=clone
    )
