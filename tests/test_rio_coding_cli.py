"""CLI integration checks without credentials or network calls."""

import asyncio
import json

import pytest
from typer.testing import CliRunner

from conftest import step_response
from rio_ai import FakeProvider
from rio_coding import cli
from rio_coding.paths import RioPaths
from rio_coding.rendering import PrintOutputMode
from rio_coding.session_manager import SessionManager
from rio_coding.session_store import InMemorySessionStorage, StepEntry

runner = CliRunner()


def test_help_and_version():
    assert runner.invoke(cli.app, ["--help"]).exit_code == 0
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("rio ")


@pytest.mark.parametrize(
    "args",
    [
        ["--mode", "bogus"],
        ["--print"],
        ["--mode", "rpc", "prompt"],
        ["--approve", "--no-approve"],
        ["--session", "one", "--session-id", "two"],
    ],
)
def test_invalid_invocations(args):
    assert runner.invoke(cli.app, args).exit_code == 2


def test_print_routes_options_and_stdin(monkeypatch, tmp_path):
    calls = []

    async def run(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(cli, "run_configured_session", run)
    result = runner.invoke(cli.app, ["-p", "explain", "--cwd", str(tmp_path)], input="source")
    assert result.exit_code == 0, result.output
    assert calls[0]["prompt"] == "source\n\nexplain"
    assert calls[0]["cwd"] == tmp_path
    assert calls[0]["mode"] == "text"


def test_failed_run_exits_nonzero(monkeypatch):
    async def fail(**kwargs):
        return False

    monkeypatch.setattr(cli, "run_configured_session", fail)
    assert runner.invoke(cli.app, ["-p", "task"]).exit_code == 1


async def test_print_executes_skill_state_and_journals(tmp_path, capsys):
    provider = FakeProvider(
        streams=[
            step_response(
                reasoning="Answer directly.",
                state_delta={},
                action="respond",
                args={"message": "Hello"},
            )
        ]
    )
    storage = InMemorySessionStorage()
    assert await cli.run_print_mode(
        prompt="hello",
        model="fake",
        cwd=tmp_path,
        provider=provider,
        storage=storage,
        output=PrintOutputMode.json,
    )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events
    assert any(isinstance(entry, StepEntry) for entry in await storage.read_all())


async def test_export_indexed_journal(tmp_path):
    manager = SessionManager(RioPaths(home=tmp_path / "home"))
    record = manager.create_session_exclusive(cwd=tmp_path, model="fake", session_id="example")
    output = await cli.export_session_command(
        record.id, tmp_path / "result.html", session_manager=manager
    )
    assert output.exists()
    assert "<html" in output.read_text()


def test_export_unknown_source_reports_error():
    result = runner.invoke(cli.app, ["export", "/nonexistent/rio-session.jsonl"])
    assert result.exit_code == 2
    assert "Unknown session or file" in result.output


async def test_configured_run_resumes_and_closes_provider(monkeypatch, tmp_path):
    from types import SimpleNamespace

    manager = SessionManager(RioPaths(home=tmp_path / "home"))
    record = manager.create_session_exclusive(
        cwd=tmp_path, model="remembered", provider_name="saved", session_id="resume"
    )
    seen = {}

    class Provider(FakeProvider):
        closed = False

        async def aclose(self):
            self.closed = True

    provider = Provider(
        streams=[
            step_response(
                reasoning="Done",
                state_delta={},
                action="respond",
                args={"message": "Done"},
            )
        ]
    )

    def select(settings, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(provider=SimpleNamespace(name="saved"), model="remembered")

    monkeypatch.setattr(cli, "SessionManager", lambda: manager)
    monkeypatch.setattr(cli, "load_provider_settings", lambda: object())
    monkeypatch.setattr(cli, "resolve_provider_selection", select)
    monkeypatch.setattr(cli, "resolve_startup_thinking_level", lambda *a, **k: None)
    monkeypatch.setattr(cli, "create_model_provider", lambda *a, **k: provider)
    assert await cli.run_configured_session(
        prompt="hello",
        mode="text",
        cwd=tmp_path,
        resume_session_id=record.id,
        trust_override="decline",
    )
    assert seen == {"provider_name": "saved", "model": "remembered"}
    assert provider.closed
    assert len(manager.list_sessions()) == 1
    assert '"type":"step"' in record.path.read_text().replace(" ", "")


async def test_api_key_login_logout(monkeypatch, tmp_path):
    from rio_coding import auth_commands
    from rio_coding.credentials import FileCredentialStore
    from rio_coding.provider_config import OpenAICompatibleProviderConfig

    store = FileCredentialStore(tmp_path / "auth.json")
    saved = []
    monkeypatch.setattr(auth_commands, "FileCredentialStore", lambda: store)
    monkeypatch.setattr(
        auth_commands,
        "_provider",
        lambda name: OpenAICompatibleProviderConfig(
            name=name,
            base_url="https://example.test/v1",
            api_key_env="EXAMPLE_KEY",
            models=("test",),
            default_model="test",
        ),
    )
    monkeypatch.setattr(
        auth_commands,
        "upsert_saved_provider",
        lambda provider, *, set_default: saved.append(provider) if set_default else None,
    )
    assert "Logged in" in await auth_commands.login_provider("test", api_key="secret")
    assert store.get("test") == "secret"
    assert saved[0].credential_name == "test"
    assert "Logged out" in auth_commands.logout_provider("test")
    assert store.get("test") is None


async def test_frontend_switch_constructs_and_closes_provider(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from rio_coding import frontend_session
    from rio_coding.session import CodingSession, CodingSessionConfig

    candidates = []

    class Provider(FakeProvider):
        closed = False

        async def aclose(self):
            self.closed = True

    def create(config, **kwargs):
        provider = Provider(streams=[])
        candidates.append((provider, kwargs))
        return provider

    monkeypatch.setattr(frontend_session, "load_provider_settings", lambda: object())
    monkeypatch.setattr(
        frontend_session,
        "resolve_provider_selection",
        lambda settings, **k: SimpleNamespace(
            provider=SimpleNamespace(name=k["provider_name"]), model=k["model"]
        ),
    )
    monkeypatch.setattr(
        frontend_session, "resolve_startup_thinking_level", lambda *a, **k: k["cli_override"]
    )
    monkeypatch.setattr(frontend_session, "create_model_provider", create)
    session = await CodingSession.load(
        CodingSessionConfig(
            provider=FakeProvider(streams=[]),
            model="old",
            provider_name="old-provider",
            cwd=tmp_path,
            storage=InMemorySessionStorage(),
        )
    )
    frontend = frontend_session.ConfiguredSession(session)
    await frontend.set_model("new", provider_name="new-provider")
    assert session.provider is candidates[0][0]
    assert session.model == "new"
    await frontend.set_thinking_level("high")
    assert candidates[0][0].closed
    assert candidates[1][1]["thinking_level"] == "high"
    await frontend.aclose()
    assert candidates[1][0].closed


async def test_frontend_resume_changes_journal(monkeypatch, tmp_path):
    from rio_coding import frontend_session
    from rio_coding.session import CodingSession, CodingSessionConfig

    class Provider(FakeProvider):
        async def aclose(self):
            pass

    manager = SessionManager(RioPaths(home=tmp_path / "home"))
    target = manager.create_session_exclusive(cwd=tmp_path, model="target", session_id="target")
    original = await CodingSession.load(
        CodingSessionConfig(
            provider=Provider(streams=[]),
            model="old",
            cwd=tmp_path,
            storage=InMemorySessionStorage(),
        )
    )
    frontend = frontend_session.ConfiguredSession(original, session_manager=manager)

    async def candidate(*args):
        return Provider(streams=[]), "test", "target", None

    monkeypatch.setattr(frontend, "_candidate", candidate)
    await frontend.resume_session(target.id)
    assert frontend.session_id == target.id
    assert frontend.storage.path == target.path
    assert frontend.model == "target"
    await frontend.aclose()


async def test_oauth_login_uses_callbacks_and_persists(monkeypatch, tmp_path):
    from rio_coding import auth_commands
    from rio_coding.credentials import FileCredentialStore, OAuthCredential
    from rio_coding.provider_config import OpenAICompatibleProviderConfig

    store = FileCredentialStore(tmp_path / "oauth.json")
    callbacks = object()
    credential = OAuthCredential(access="access", refresh="refresh", expires=9999999999999)

    class OAuth:
        async def login(self, received):
            assert received is callbacks
            return credential

    monkeypatch.setattr(auth_commands, "FileCredentialStore", lambda: store)
    monkeypatch.setattr(auth_commands, "get_oauth_provider", lambda name: OAuth())
    monkeypatch.setattr(
        auth_commands,
        "_provider",
        lambda name: OpenAICompatibleProviderConfig(
            name=name,
            base_url="https://example.test",
            api_key_env="KEY",
            models=("test",),
            default_model="test",
        ),
    )
    monkeypatch.setattr(auth_commands, "upsert_saved_provider", lambda provider, **kwargs: None)
    await auth_commands.login_provider("example", callbacks=callbacks)
    assert store.get_oauth("example") == credential


async def test_failed_switch_keeps_live_provider(monkeypatch, tmp_path):
    from rio_coding.frontend_session import ConfiguredSession
    from rio_coding.session import CodingSession, CodingSessionConfig

    class Provider(FakeProvider):
        closed = False

        async def aclose(self):
            self.closed = True

    original = Provider(streams=[])
    replacement = Provider(streams=[])
    session = await CodingSession.load(
        CodingSessionConfig(
            provider=original, model="original", cwd=tmp_path, storage=InMemorySessionStorage()
        )
    )
    frontend = ConfiguredSession(session)

    async def candidate(*args):
        return replacement, "replacement", "replacement", "high"

    async def fail(entries):
        raise OSError("disk full")

    monkeypatch.setattr(frontend, "_candidate", candidate)
    monkeypatch.setattr(session._runner, "append_entries", fail)
    with pytest.raises(OSError, match="disk full"):
        await frontend.set_model("replacement")
    assert session.provider is original
    assert session.model == "original"
    assert session.thinking_level is None
    assert replacement.closed
    assert not original.closed


def test_tui_starts_without_key_and_can_login_then_run(monkeypatch, tmp_path):
    from pathlib import Path

    from textual.widgets import Input

    import rio_tui as tui
    from rio_coding.credentials import FileCredentialStore
    from rio_tui.dialogs import Picker
    from rio_tui.widgets.conversation import Notice

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    completed = []

    class Provider(FakeProvider):
        closed = False

        async def aclose(self):
            self.closed = True

    provider = Provider(
        [
            step_response(
                reasoning="",
                state_delta={},
                action="respond",
                args={"message": "Authenticated"},
            )
        ]
    )

    async def headless(session, initial_prompt=None):
        app = tui.RioTuiApp(session)
        async with app.run_test() as pilot:
            app.workspace.submit("/login")
            await pilot.pause()
            assert isinstance(app.screen, Picker)
            await pilot.press("escape")

            # A missing credential is an in-app error, and leaves login usable.
            app.workspace.submit("hello")
            await asyncio.gather(*(w.wait() for w in app.workers if w.node is app.workspace))
            await pilot.pause()
            output = " ".join(item.text for item in app.screen.query(Notice))
            assert "Missing provider API key" in output
            assert not app.workspace.busy

            def authenticated(config, **kwargs):
                assert config.credential_name == "openai"
                assert FileCredentialStore().get(config.credential_name) == "test-api-key"
                return provider

            monkeypatch.setattr("rio_coding.frontend_session.create_model_provider", authenticated)
            app.workspace.submit("/login openai")
            await pilot.pause()
            app.screen.query_one(Input).value = "test-api-key"
            await pilot.press("enter")
            await asyncio.gather(*(w.wait() for w in app.workers if w.node is app.workspace))
            assert FileCredentialStore().get("openai") == "test-api-key"
            assert session.provider_name == "openai"
            app.workspace.submit("hello again")
            await asyncio.gather(*(w.wait() for w in app.workers if w.node is app.workspace))
            assert session.answer == "Authenticated"
            completed.append(True)

    monkeypatch.setattr(tui, "run_tui_app", headless)
    result = runner.invoke(cli.app, ["--cwd", str(tmp_path)])
    assert result.exit_code == 0, (result.output, result.exception)
    assert completed == [True]
    assert provider.closed


def test_headless_mode_still_reports_missing_key(monkeypatch, tmp_path):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = runner.invoke(cli.app, ["-p", "hello", "--provider", "openai"])
    assert result.exit_code != 0
    assert "Missing provider API key" in result.output


@pytest.mark.parametrize("auth_method", ["api_key", "oauth"])
async def test_login_survives_restart_and_logout(monkeypatch, tmp_path, auth_method):
    from pathlib import Path

    from rio_coding import auth_commands
    from rio_coding.credentials import FileCredentialStore, OAuthCredential
    from rio_coding.provider_config import (
        load_provider_settings,
        provider_has_usable_credentials,
        resolve_provider_selection,
    )
    from rio_coding.provider_runtime import create_model_provider

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    name = "deepseek" if auth_method == "api_key" else "openai-codex"
    credential = OAuthCredential(
        access="test-access",
        refresh="test-refresh",
        expires=9999999999999,
        account_id="test-account",
    )

    class OAuth:
        async def login(self, callbacks):
            return credential

    if auth_method == "oauth":
        monkeypatch.setattr(auth_commands, "get_oauth_provider", lambda name: OAuth())
        await auth_commands.login_provider(name)
    else:
        await auth_commands.login_provider(name, api_key="test-key")

    # Reconstruct startup state from disk, without reusing the login's objects.
    selection = resolve_provider_selection(load_provider_settings())
    assert selection.provider.name == name
    store = FileCredentialStore()
    assert provider_has_usable_credentials(selection.provider, credential_reader=store)
    if auth_method == "oauth":
        assert store.get_oauth(name) == credential
    else:
        assert store.get(name) == "test-key"
    assert store.path.stat().st_mode & 0o777 == 0o600
    runtime = create_model_provider(selection.provider, model=selection.model)
    await runtime.aclose()

    auth_commands.logout_provider(name)
    restarted_store = FileCredentialStore()
    assert not provider_has_usable_credentials(
        load_provider_settings().get_provider(name), credential_reader=restarted_store
    )


async def test_failed_login_preserves_startup_provider(monkeypatch, tmp_path):
    from pathlib import Path

    from rio_coding import auth_commands
    from rio_coding.credentials import FileCredentialStore
    from rio_coding.provider_config import load_provider_settings

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    await auth_commands.login_provider("openai", api_key="test-key")

    class OAuth:
        async def login(self, callbacks):
            raise RuntimeError("Authentication failed")

    monkeypatch.setattr(auth_commands, "get_oauth_provider", lambda name: OAuth())
    with pytest.raises(RuntimeError, match="Authentication failed"):
        await auth_commands.login_provider("openai-codex")
    assert load_provider_settings().default_provider == "openai"
    assert FileCredentialStore().get("openai") == "test-key"
    assert FileCredentialStore().get_oauth("openai-codex") is None


async def test_frontend_opens_independent_session(monkeypatch, tmp_path):
    from rio_coding.frontend_session import ConfiguredSession
    from rio_coding.session import CodingSession, CodingSessionConfig

    class Provider(FakeProvider):
        closed = False

        async def aclose(self):
            self.closed = True

    manager = SessionManager(RioPaths(home=tmp_path / "home"))
    original_provider = Provider(streams=[])
    original = await CodingSession.load(
        CodingSessionConfig(
            provider=original_provider,
            model="test",
            cwd=tmp_path,
            storage=InMemorySessionStorage(),
            load_extensions=False,
        )
    )
    frontend = ConfiguredSession(original, session_manager=manager)
    providers = []

    async def candidate(*args):
        provider = Provider(streams=[])
        providers.append(provider)
        return provider, "test", "test", None

    monkeypatch.setattr(frontend, "_candidate", candidate)
    opened = await frontend.open_session()
    try:
        assert frontend.session is original
        assert opened.session is not original
        assert opened.storage is not original.storage
        assert opened.session_id is not None
        assert not original_provider.closed
        assert opened.session.extensions is not original.extensions
    finally:
        await opened.aclose()
        await frontend.aclose()
    assert providers[0].closed
    assert not original_provider.closed  # The caller owns the initial provider.
