"""Tests for update checks, the self-updater, reload summaries, and packaged docs.

Ported from tau's ``test_update_check.py`` and ``test_updater.py``. No network
is used anywhere here: PyPI lookups go through an injected fetcher, and the
updater's subprocess/launcher/executable-finder seams are all faked.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from rio.coding import updater
from rio.coding.reload import CodingReloadSummary, ReloadCategorySummary
from rio.coding.self_docs import rio_docs_path, rio_examples_path, rio_readme_path
from rio.coding.update_check import (
    PYPI_JSON_URL,
    UPDATE_CHECK_TIMEOUT_SECONDS,
    ReleaseNoteSection,
    ReleaseNotesEntry,
    fetch_latest_pypi_version,
    load_release_notes,
    release_notes_between,
    startup_release_notes_notice,
    startup_update_notice,
)
from rio.coding.updater import detect_install_method, update_rio

# -- update_check --------------------------------------------------------


def test_startup_update_notice_reports_newer_stable_release(tmp_path) -> None:
    from datetime import UTC, datetime

    calls: list[tuple[str, float]] = []

    def fetcher(url: str, timeout: float) -> dict[str, object]:
        calls.append((url, timeout))
        return {"releases": {"0.1.0": [{}], "0.2.0": [{}], "0.3.0rc1": [{}]}}

    notice = startup_update_notice(
        "0.1.0",
        fetcher=fetcher,
        cache_path=tmp_path / "update-check.json",
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        env={},
    )

    assert notice is not None
    assert notice.current_version == "0.1.0"
    assert notice.latest_version == "0.2.0"
    assert "rio 0.2.0 is available (installed: 0.1.0)" in notice.message
    assert "Run `rio update` to upgrade" in notice.message
    assert calls == [(PYPI_JSON_URL, UPDATE_CHECK_TIMEOUT_SECONDS)]


def test_startup_update_notice_is_quiet_when_current(tmp_path) -> None:
    notice = startup_update_notice(
        "0.2.0",
        fetcher=lambda _url, _timeout: {"releases": {"0.2.0": [{}]}},
        cache_path=tmp_path / "update-check.json",
        env={},
    )

    assert notice is None


def test_startup_update_notice_uses_fresh_cache(tmp_path) -> None:
    from datetime import UTC, datetime

    cache_path = tmp_path / "update-check.json"
    cache_path.write_text(
        '{"checked_at":"2026-01-01T00:00:00+00:00","latest_version":"0.2.0"}\n',
        encoding="utf-8",
    )

    notice = startup_update_notice(
        "0.1.0",
        fetcher=lambda _url, _timeout: (_ for _ in ()).throw(AssertionError("no fetch")),
        cache_path=cache_path,
        now=lambda: datetime(2026, 1, 1, 12, tzinfo=UTC),
        env={},
    )

    assert notice is not None
    assert notice.latest_version == "0.2.0"


def test_startup_update_notice_uses_fresh_empty_cache(tmp_path) -> None:
    from datetime import UTC, datetime

    cache_path = tmp_path / "update-check.json"
    cache_path.write_text(
        '{"checked_at":"2026-01-01T00:00:00+00:00","latest_version":null}\n',
        encoding="utf-8",
    )

    notice = startup_update_notice(
        "0.1.0",
        fetcher=lambda _url, _timeout: (_ for _ in ()).throw(AssertionError("no fetch")),
        cache_path=cache_path,
        now=lambda: datetime(2026, 1, 1, 12, tzinfo=UTC),
        env={},
    )

    assert notice is None


def test_startup_update_notice_refreshes_stale_cache(tmp_path) -> None:
    from datetime import UTC, datetime, timedelta

    cache_path = tmp_path / "update-check.json"
    cache_path.write_text(
        '{"checked_at":"2026-01-01T00:00:00+00:00","latest_version":"0.2.0"}\n',
        encoding="utf-8",
    )

    notice = startup_update_notice(
        "0.1.0",
        fetcher=lambda _url, _timeout: {"releases": {"0.3.0": [{}]}},
        cache_path=cache_path,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=2),
        env={},
    )

    assert notice is not None
    assert notice.latest_version == "0.3.0"


def test_startup_update_notice_ignores_failures(tmp_path) -> None:
    def broken_fetcher(_url: str, _timeout: float) -> dict[str, object]:
        raise TimeoutError("offline")

    assert (
        startup_update_notice(
            "0.1.0",
            fetcher=broken_fetcher,
            cache_path=tmp_path / "update-check.json",
            env={},
        )
        is None
    )


def test_startup_update_notice_can_be_disabled(tmp_path) -> None:
    notice = startup_update_notice(
        "0.1.0",
        fetcher=lambda _url, _timeout: (_ for _ in ()).throw(AssertionError("no fetch")),
        cache_path=tmp_path / "update-check.json",
        env={"RIO_NO_UPDATE_CHECK": "1"},
    )

    assert notice is None


def test_startup_update_notice_skips_ci(tmp_path) -> None:
    notice = startup_update_notice(
        "0.1.0",
        fetcher=lambda _url, _timeout: (_ for _ in ()).throw(AssertionError("no fetch")),
        cache_path=tmp_path / "update-check.json",
        env={"CI": "true"},
    )

    assert notice is None


def test_load_release_notes_reads_shared_json(tmp_path) -> None:
    notes_path = tmp_path / "releases.json"
    notes_path.write_text(
        """
        [
          {
            "version": "0.1.2",
            "date": "2026-07-03",
            "sections": {"New": ["Feature"], "Fixed": ["Fix"]}
          }
        ]
        """,
        encoding="utf-8",
    )

    notes = load_release_notes(notes_path)

    assert notes == (
        ReleaseNotesEntry(
            version="0.1.2",
            date="2026-07-03",
            sections=(
                ReleaseNoteSection(title="New", items=("Feature",)),
                ReleaseNoteSection(title="Fixed", items=("Fix",)),
            ),
        ),
    )


def test_startup_release_notes_notice_records_first_seen_version(tmp_path) -> None:
    state_path = tmp_path / "release-notes-state.json"

    notice = startup_release_notes_notice(
        "0.1.2",
        state_path=state_path,
        release_notes=(
            ReleaseNotesEntry(
                version="0.1.2",
                date=None,
                sections=(ReleaseNoteSection(title="New", items=("New TUI release notes",)),),
            ),
        ),
    )

    assert notice is None
    assert '"last_seen_version": "0.1.2"' in state_path.read_text(encoding="utf-8")


def test_startup_release_notes_notice_reports_upgrade_once(tmp_path) -> None:
    state_path = tmp_path / "release-notes-state.json"
    state_path.write_text('{"last_seen_version":"0.1.1"}\n', encoding="utf-8")
    release_notes = (
        ReleaseNotesEntry(
            version="0.1.2",
            date=None,
            sections=(ReleaseNoteSection(title="New", items=("New feature", "Bug fix")),),
        ),
    )

    notice = startup_release_notes_notice(
        "0.1.2",
        state_path=state_path,
        release_notes=release_notes,
    )

    assert notice is not None
    assert notice.previous_version == "0.1.1"
    assert notice.current_version == "0.1.2"
    assert notice.notes == ("New feature", "Bug fix")
    assert notice.message == "rio updated to 0.1.2\n\n**New**\n- New feature\n- Bug fix"

    second_notice = startup_release_notes_notice(
        "0.1.2",
        state_path=state_path,
        release_notes=release_notes,
    )
    assert second_notice is None


def test_startup_release_notes_notice_survives_missing_release_notes_file(
    tmp_path, monkeypatch
) -> None:
    import rio.coding.update_check as update_check_module

    monkeypatch.setattr(
        update_check_module, "RELEASE_NOTES_PATH", tmp_path / "missing" / "releases.json"
    )
    state_path = tmp_path / "release-notes-state.json"
    state_path.write_text('{"last_seen_version":"0.1.1"}\n', encoding="utf-8")

    notice = startup_release_notes_notice("0.1.2", state_path=state_path)

    assert notice is not None
    assert notice.entries == ()


def test_startup_release_notes_notice_survives_malformed_release_notes_file(
    tmp_path, monkeypatch
) -> None:
    import rio.coding.update_check as update_check_module

    broken_path = tmp_path / "releases.json"
    broken_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(update_check_module, "RELEASE_NOTES_PATH", broken_path)
    state_path = tmp_path / "release-notes-state.json"
    state_path.write_text('{"last_seen_version":"0.1.1"}\n', encoding="utf-8")

    notice = startup_release_notes_notice("0.1.2", state_path=state_path)

    assert notice is not None
    assert notice.entries == ()


def test_startup_release_notes_notice_combines_skipped_versions(tmp_path) -> None:
    state_path = tmp_path / "release-notes-state.json"
    state_path.write_text('{"last_seen_version":"0.1.0"}\n', encoding="utf-8")
    release_notes = (
        ReleaseNotesEntry(
            version="0.1.2",
            date=None,
            sections=(ReleaseNoteSection(title="New", items=("Second change",)),),
        ),
        ReleaseNotesEntry(
            version="0.1.1",
            date=None,
            sections=(ReleaseNoteSection(title="Fixed", items=("First change",)),),
        ),
        ReleaseNotesEntry(
            version="0.1.3",
            date=None,
            sections=(ReleaseNoteSection(title="New", items=("Future change",)),),
        ),
    )

    notice = startup_release_notes_notice(
        "0.1.2",
        state_path=state_path,
        release_notes=release_notes,
    )

    assert notice is not None
    assert notice.notes == ("First change", "Second change")


def test_release_notes_between_ignores_future_versions() -> None:
    entries = (
        ReleaseNotesEntry(version="0.1.1", date=None, sections=()),
        ReleaseNotesEntry(version="0.1.2", date=None, sections=()),
        ReleaseNotesEntry(version="0.1.3", date=None, sections=()),
    )

    assert release_notes_between("0.1.1", "0.1.2", entries) == (entries[1],)


def test_fetch_latest_pypi_version_falls_back_to_info_version() -> None:
    latest = fetch_latest_pypi_version(
        fetcher=lambda _url, _timeout: {"info": {"version": "0.4.0"}}
    )

    assert latest == "0.4.0"


def test_fetch_latest_pypi_version_skips_malformed_release_versions() -> None:
    latest = fetch_latest_pypi_version(
        fetcher=lambda _url, _timeout: {"releases": {"0.3.0": [{}], "wat": [{}]}}
    )

    assert latest == "0.3.0"


def test_fetch_latest_pypi_version_rejects_malformed_versions() -> None:
    with pytest.raises(Exception) as excinfo:
        fetch_latest_pypi_version(fetcher=lambda _url, _timeout: {"info": {"version": "wat"}})
    assert excinfo.value.__class__.__name__ == "InvalidVersion"


def test_load_release_notes_resolves_default_path() -> None:
    """Regression: load_release_notes() resolves the module-level RELEASE_NOTES_PATH
    correctly in both dev and installed layouts, and reflects the reset rio 0.1.0
    history (tau's release history is not carried over)."""
    entries = load_release_notes()
    assert len(entries) == 1, "the port resets release notes to a single rio 0.1.0 entry"
    assert entries[0].version == "0.1.0"


# -- updater --------------------------------------------------------------


def _success(command: tuple[str, ...], **kwargs: object) -> CompletedProcess[str]:
    assert kwargs == {"capture_output": True, "text": True, "check": False}
    return CompletedProcess(command, 0, stdout="upgraded", stderr="")


def test_detect_install_method_uses_receipts_before_installer_metadata(tmp_path: Path) -> None:
    assert detect_install_method(tmp_path) is None
    assert detect_install_method(tmp_path, installer="uv") == "uv-pip"
    assert detect_install_method(tmp_path, installer="pip") == "pip"

    (tmp_path / "uv-receipt.toml").touch()
    assert detect_install_method(tmp_path, installer="pip") == "uv-tool"

    (tmp_path / "uv-receipt.toml").unlink()
    (tmp_path / "pipx_metadata.json").touch()
    assert detect_install_method(tmp_path, installer="pip") == "pipx"


def test_update_rio_uses_uv_tool_for_uv_owned_tool_environment(tmp_path: Path) -> None:
    (tmp_path / "uv-receipt.toml").touch()
    calls: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], **kwargs: object) -> CompletedProcess[str]:
        calls.append(command)
        return _success(command, **kwargs)

    result = update_rio(
        runner=runner,
        environment_prefix=tmp_path,
        inspect_distribution=False,
        latest_version_fetcher=lambda: "0.2.4",
        platform_name="linux",
    )

    assert result.succeeded is True
    assert result.command == ("uv", "tool", "install", "rio@0.2.4")
    assert calls == [("uv", "tool", "install", "rio@0.2.4")]


def test_update_rio_hands_windows_uv_tool_update_to_waiting_process(tmp_path: Path) -> None:
    (tmp_path / "uv-receipt.toml").touch()
    handoff_dir = tmp_path / "handoff"
    launches: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def launcher(command: tuple[str, ...], **kwargs: object) -> object:
        launches.append((command, kwargs))
        return object()

    def runner(*args: object, **kwargs: object) -> CompletedProcess[str]:
        del args, kwargs
        raise AssertionError("uv must not run in the live rio process")

    result = update_rio(
        runner=runner,
        environment_prefix=tmp_path,
        inspect_distribution=False,
        latest_version_fetcher=lambda: "0.2.4",
        platform_name="win32",
        detached_launcher=launcher,
        executable_finder=lambda name: (
            "C:/Windows/powershell.exe" if name == "powershell.exe" else None
        ),
        parent_pid=4242,
        handoff_directory=handoff_dir,
    )

    assert result.succeeded is True
    assert result.deferred is True
    assert result.command == ("uv", "tool", "install", "rio@0.2.4")
    assert "scheduled" in result.stdout
    assert str(handoff_dir / "update.log") in result.stdout
    assert len(launches) == 1
    detached_command, options = launches[0]
    assert detached_command[-7:-1] == (
        str(handoff_dir / "update.ps1"),
        "-ParentProcessId",
        "4242",
        "-LogPath",
        str(handoff_dir / "update.log"),
        "-UpdatePayloadBase64",
    )
    assert json.loads(base64.b64decode(detached_command[-1])) == {
        "executable": "uv",
        "arguments": '"tool" "install" "rio@0.2.4"',
    }
    assert options["creationflags"] == 0x00000208
    script = (handoff_dir / "update.ps1").read_text(encoding="utf-8")
    assert "Wait-Process -InputObject $ParentProcess -ErrorAction Stop" in script
    assert "NoProcessFoundForGivenId" in script
    assert script.index("Wait-Process -InputObject $ParentProcess") < script.index(
        "$UpdateProcess.Start()"
    )
    assert "System.Diagnostics.ProcessStartInfo" in script
    assert "$StartInfo.UseShellExecute = $false" in script


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("", '""'),
        ("plain", '"plain"'),
        ("space value", '"space value"'),
        ('quote"value', '"quote\\"value"'),
        ("trailing\\", '"trailing\\\\"'),
        ('slashes\\\\"quote', '"slashes\\\\\\\\\\"quote"'),
        ("semi;&$()", '"semi;&$()"'),
        ("snowman \u2603", '"snowman \u2603"'),
    ],
)
def test_quote_windows_argument_uses_microsoft_runtime_rules(argument: str, expected: str) -> None:
    assert updater._quote_windows_argument(argument) == expected


def test_windows_command_line_preserves_argument_boundaries() -> None:
    assert updater._windows_command_line(("", "a b", 'c"', "tail\\")) == (
        '"" "a b" "c\\"" "tail\\\\"'
    )


def test_windows_handoff_reports_staging_write_failure_and_removes_owned_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    update_dir = tmp_path / "owned-handoff"

    def make_update_dir(*args: object, **kwargs: object) -> str:
        del args, kwargs
        update_dir.mkdir()
        return str(update_dir)

    original_write_text = Path.write_text

    def fail_script_write(path: Path, *args: object, **kwargs: object) -> int:
        if path == update_dir / "update.ps1":
            raise OSError("disk is full")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(updater.tempfile, "mkdtemp", make_update_dir)
    monkeypatch.setattr(Path, "write_text", fail_script_write)

    result = updater._handoff_windows_update(
        ("uv", "tool", "install", "rio@1.0"),
        launcher=lambda *args, **kwargs: object(),
        executable_finder=lambda name: "powershell.exe",
        parent_pid=4242,
        handoff_directory=None,
    )

    assert result.succeeded is False
    assert result.failures == ("Could not stage the detached Windows updater: disk is full",)
    assert not update_dir.exists()


def test_windows_handoff_reports_launch_failure_and_preserves_caller_directory(
    tmp_path: Path,
) -> None:
    update_dir = tmp_path / "caller-handoff"

    def fail_launch(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OSError("process creation denied")

    result = updater._handoff_windows_update(
        ("uv", "tool", "install", "rio@1.0"),
        launcher=fail_launch,
        executable_finder=lambda name: "powershell.exe",
        parent_pid=4242,
        handoff_directory=update_dir,
    )

    assert result.succeeded is False
    assert result.failures == (
        "Could not start the detached Windows updater: process creation denied",
    )
    assert update_dir.is_dir()
    assert list(update_dir.iterdir()) == []


def _wait_for_file(path: Path, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def _wait_for_text(path: Path, expected: str, timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8-sig")
            if expected in text:
                return text
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {expected!r} in {path}")


def _powershell_engines() -> list[str | None]:
    if sys.platform != "win32":
        return [None]
    engines = [shutil.which(name) for name in ("powershell.exe", "pwsh.exe")]
    available = list(dict.fromkeys(engine for engine in engines if engine is not None))
    return available or [None]


def _engine_id(engine: str | None) -> str:
    return Path(engine).name if engine else "unavailable"


@pytest.mark.parametrize("powershell", _powershell_engines(), ids=_engine_id)
def test_windows_handoff_blocks_for_live_parent_and_preserves_arguments(
    tmp_path: Path, powershell: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if powershell is None:
        pytest.skip("requires Windows and PowerShell")
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    marker = tmp_path / "exact argv.json"
    fake_update = tmp_path / "fake updater.py"
    fake_update.write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding='utf-8')\n"
        "raise SystemExit(23)\n",
        encoding="utf-8",
    )
    arguments = (
        "space value",
        "semi;&$()",
        'quote"value',
        "",
        "trailing\\",
        "-named-looking",
    )
    update_dir = tmp_path / "handoff with spaces"
    executable_dir = tmp_path / "executable path with spaces"
    executable_dir.mkdir()
    fake_python = executable_dir / Path(sys.executable).name
    shutil.copy2(sys.executable, fake_python)
    for runtime_dll in Path(sys.base_prefix).glob("python*.dll"):
        shutil.copy2(runtime_dll, executable_dir / runtime_dll.name)

    monkeypatch.setenv("PYTHONHOME", sys.base_prefix)

    try:
        result = updater._handoff_windows_update(
            (str(fake_python), str(fake_update), str(marker), *arguments),
            launcher=subprocess.Popen,
            executable_finder=lambda name: powershell,
            parent_pid=parent.pid,
            handoff_directory=update_dir,
        )
        assert result.succeeded is True
        time.sleep(0.5)
        assert not marker.exists(), "updater ran while the parent was alive"
        parent.terminate()
        parent.wait(timeout=10)
        _wait_for_file(marker)
        assert json.loads(marker.read_text(encoding="utf-8")) == list(arguments)
        _wait_for_text(update_dir / "update.log", "Update command exited with code 23.")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)


@pytest.mark.parametrize("powershell", _powershell_engines(), ids=_engine_id)
def test_windows_helper_wait_failure_is_fail_closed(tmp_path: Path, powershell: str | None) -> None:
    if powershell is None:
        pytest.skip("requires Windows and PowerShell")
    update_dir = tmp_path / "wait-failure"
    update_dir.mkdir()
    script_path = update_dir / "update.ps1"
    log_path = update_dir / "update.log"
    marker = update_dir / "updater-ran"
    fake_update = update_dir / "fake updater.py"
    script_path.write_text(updater._WINDOWS_UPDATE_SCRIPT, encoding="utf-8")
    fake_update.write_text(
        "import pathlib, sys\npathlib.Path(sys.argv[1]).touch()\n",
        encoding="utf-8",
    )
    wrapper = update_dir / "force-wait-failure.ps1"
    wrapper.write_text(
        "function Wait-Process { throw 'forced wait failure' }\n"
        "$Target = $args[0]\n"
        "$TargetArgs = $args[1..($args.Count - 1)]\n"
        "& $Target @TargetArgs\n"
        "exit $LASTEXITCODE\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(wrapper),
            str(script_path),
            "-ParentProcessId",
            str(os.getpid()),
            "-LogPath",
            str(log_path),
            "-UpdatePayloadBase64",
            base64.b64encode(
                json.dumps(
                    {
                        "executable": sys.executable,
                        "arguments": updater._windows_command_line((str(fake_update), str(marker))),
                    }
                ).encode()
            ).decode("ascii"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode != 0
    assert not marker.exists()
    assert "Could not wait for rio process" in log_path.read_text(encoding="utf-8-sig")


def test_update_rio_reports_uv_latest_version_lookup_failure(tmp_path: Path) -> None:
    (tmp_path / "uv-receipt.toml").touch()

    result = update_rio(
        runner=_success,
        environment_prefix=tmp_path,
        inspect_distribution=False,
        latest_version_fetcher=lambda: None,
    )

    assert result.succeeded is False
    assert result.failures == ("Could not determine the latest rio version from PyPI.",)


def test_update_rio_uses_pipx_for_pipx_owned_environment(tmp_path: Path) -> None:
    (tmp_path / "pipx_metadata.json").touch()

    result = update_rio(
        runner=_success,
        environment_prefix=tmp_path,
        inspect_distribution=False,
    )

    assert result.command == ("pipx", "upgrade", "rio")


def test_update_rio_reuses_uv_pip_for_uv_installed_distribution(tmp_path: Path) -> None:
    result = update_rio(
        runner=_success,
        python_executable="/env/bin/python",
        environment_prefix=tmp_path,
        installer="uv",
        inspect_distribution=False,
    )

    assert result.command == (
        "uv",
        "pip",
        "install",
        "--python",
        "/env/bin/python",
        "--upgrade",
        "rio",
    )


def test_update_rio_uses_current_environment_pip_for_pip_install(tmp_path: Path) -> None:
    result = update_rio(
        runner=_success,
        python_executable="/env/bin/python",
        environment_prefix=tmp_path,
        installer="pip",
        inspect_distribution=False,
    )

    assert result.command == (
        "/env/bin/python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "rio",
    )


def test_update_rio_does_not_fall_back_when_owner_update_fails(tmp_path: Path) -> None:
    (tmp_path / "uv-receipt.toml").touch()
    calls: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], **kwargs: object) -> CompletedProcess[str]:
        del kwargs
        calls.append(command)
        return CompletedProcess(command, 2, stdout="", stderr="uv failed")

    result = update_rio(
        runner=runner,
        environment_prefix=tmp_path,
        inspect_distribution=False,
        latest_version_fetcher=lambda: "0.2.4",
    )

    assert result.succeeded is False
    assert result.failures == ("uv tool install rio@0.2.4: uv failed",)
    assert calls == [("uv", "tool", "install", "rio@0.2.4")]


def test_update_rio_refuses_direct_url_install(tmp_path: Path) -> None:
    result = update_rio(
        runner=_success,
        environment_prefix=tmp_path,
        direct_url="file:///checkout/rio",
        installer="uv",
        inspect_distribution=False,
    )

    assert result.succeeded is False
    assert "original source: file:///checkout/rio" in result.failures[0]


def test_update_rio_refuses_conda_or_pixi_environment(tmp_path: Path) -> None:
    (tmp_path / "conda-meta").mkdir()

    result = update_rio(
        runner=_success,
        environment_prefix=tmp_path,
        inspect_distribution=False,
    )

    assert result.succeeded is False
    assert "Conda/Pixi-managed" in result.failures[0]


def test_update_rio_refuses_unknown_installer(tmp_path: Path) -> None:
    result = update_rio(
        runner=_success,
        environment_prefix=tmp_path,
        installer="custom-manager",
        inspect_distribution=False,
    )

    assert result.succeeded is False
    assert "Package metadata reports: custom-manager" in result.failures[0]


# -- reload -----------------------------------------------------------------


def test_reload_category_summary_reports_delta_and_change() -> None:
    grew = ReloadCategorySummary(before=1, after=3, changed=True)
    unchanged = ReloadCategorySummary(before=2, after=2, changed=False)

    assert grew.delta == 2
    assert unchanged.delta == 0

    summary = CodingReloadSummary(
        skills=grew,
        prompt_templates=unchanged,
        context_files=unchanged,
        extensions=unchanged,
        diagnostics=unchanged,
        system_prompt_rebuilt=True,
    )
    assert summary.skills.changed is True
    assert summary.system_prompt_rebuilt is True


# -- self_docs ----------------------------------------------------------------


def test_self_docs_paths_point_at_the_packaged_data_directory() -> None:
    assert rio_readme_path() == rio_docs_path() / "README.md"
    assert rio_docs_path().name == "docs"
    assert rio_examples_path().name == "examples"
    assert rio_docs_path().parent == rio_examples_path().parent


def test_self_docs_readme_and_docs_directory_exist_in_the_installed_package() -> None:
    assert rio_docs_path().is_dir()
    assert rio_readme_path().is_file()


# -- packaged docs data --------------------------------------------------------

_EXPECTED_DOC_NAMES = {
    "README.md",
    "architecture.md",
    "cli.md",
    "skills.md",
    "models.md",
    "security.md",
    "extensions.md",
    "local-inference.md",
}


def test_packaged_docs_directory_has_exactly_the_expected_documents() -> None:
    names = {path.name for path in rio_docs_path().glob("*.md")}
    assert names == _EXPECTED_DOC_NAMES


@pytest.mark.parametrize("name", sorted(_EXPECTED_DOC_NAMES))
def test_packaged_docs_are_rebranded_away_from_tau(name: str) -> None:
    text = (rio_docs_path() / name).read_text(encoding="utf-8")
    lowered = text.lower()
    assert "tau" not in lowered, f"{name} still mentions tau"
    assert "twotimespi.dev" not in lowered, f"{name} still links twotimespi.dev"
    assert ".tau" not in lowered, f"{name} still references .tau paths"


@pytest.mark.parametrize("name", sorted(_EXPECTED_DOC_NAMES - {"architecture.md"}))
def test_packaged_docs_do_not_describe_compaction_or_transcripts_as_a_feature(name: str) -> None:
    """Only architecture.md may mention transcripts/compaction, and only to explain

    that rio has neither -- see its "Sessions hold state, not a transcript" section.
    Every other doc must not describe either as something rio actually does.
    """
    text = (rio_docs_path() / name).read_text(encoding="utf-8")
    lowered = text.lower()
    assert "auto-compaction" not in lowered
    assert "compact your session" not in lowered
    assert "session history" not in lowered
    assert "transcript replay" not in lowered


def test_architecture_doc_cites_the_skill_state_paper_and_the_three_packages() -> None:
    text = (rio_docs_path() / "architecture.md").read_text(encoding="utf-8")
    assert "SKILL.state" in text
    assert "arXiv:2608.26263" in text
    assert "rio.ai" in text
    assert "rio.agent" in text
    assert "rio.coding" in text
    assert "session_store" in text
    assert "session_runner" in text


def test_release_notes_json_resets_to_a_single_rio_entry() -> None:
    data = json.loads(
        (rio_docs_path().parent / "release-notes" / "releases.json").read_text(encoding="utf-8")
    )
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["version"] == "0.1.0"
