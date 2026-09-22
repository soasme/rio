"""One-shot task execution command."""

from __future__ import annotations

import sys
from pathlib import Path

from rio.ai.provider import ModelProvider
from rio.coding.extensions.startup import resolve_dynamic_startup
from rio.coding.project_trust import ProjectTrustCoordinator, ProjectTrustStore, TrustOverride
from rio.coding.provider_config import (
    ProviderConfigError,
    load_provider_settings,
    resolve_provider_selection,
    resolve_startup_thinking_level,
)
from rio.coding.provider_runtime import create_model_provider
from rio.coding.rendering import PlainEventRenderer, PrintOutputMode, create_event_renderer
from rio.coding.session import CodingSession, CodingSessionConfig
from rio.coding.session_manager import CodingSessionRecord, SessionManager
from rio.coding.session_store import InMemorySessionStorage, JsonlSessionStorage, SessionStorage
from rio.coding.shell_config import load_shell_settings
from rio.coding.thinking import ThinkingLevel


class SessionNotFoundError(ValueError):
    """Raised when a --resume session id does not exist."""


class CwdMismatchError(ValueError):
    """Raised when --cwd conflicts with the resumed session's directory."""


class CwdNotFoundError(ValueError):
    """Raised when the requested working directory does not exist."""


async def run_persistent_session(
    prompt: str,
    cwd: Path | None = None,
    provider_name: str | None = None,
    model: str | None = None,
    thinking_level: ThinkingLevel | None = None,
    extension_paths: tuple[Path, ...] = (),
    trust_override: TrustOverride | None = None,
    resume: str | None = None,
    output_mode: PrintOutputMode = PrintOutputMode.human,
    *,
    session_manager: SessionManager | None = None,
) -> tuple[bool, str]:
    """Run against a durable session journal, creating or resuming one."""
    manager = session_manager or SessionManager()
    requested_cwd = cwd or Path.cwd()
    record: CodingSessionRecord | None = None
    if resume is not None:
        record = manager.get_session(resume)
        if record is None:
            raise SessionNotFoundError(f"No session found with id '{resume}'")
        if cwd is not None and cwd.resolve() != record.cwd:
            raise CwdMismatchError("--cwd must match the resumed session's working directory")
        requested_cwd = record.cwd
        provider_name = provider_name or record.provider_name
        model = model or record.model
    if record is None:
        record = manager.create_session(
            cwd=requested_cwd, model=model or "default", provider_name=provider_name
        )
    succeeded, selected_name, selected_model = await _run_configured_session(
        prompt,
        requested_cwd,
        provider_name,
        model,
        thinking_level,
        extension_paths,
        trust_override,
        output_mode,
        storage=JsonlSessionStorage(record.path),
    )
    manager.touch_session(record.id, model=selected_model, provider_name=selected_name)
    return succeeded, record.id


async def run_configured_session(
    prompt: str,
    cwd: Path,
    provider_name: str | None = None,
    model: str | None = None,
    thinking_level: ThinkingLevel | None = None,
    extension_paths: tuple[Path, ...] = (),
    trust_override: TrustOverride | None = None,
    output_mode: PrintOutputMode = PrintOutputMode.human,
    *,
    storage: SessionStorage | None = None,
) -> bool:
    """Construct and execute one coding session."""
    succeeded, _, _ = await _run_configured_session(
        prompt,
        cwd,
        provider_name,
        model,
        thinking_level,
        extension_paths,
        trust_override,
        output_mode,
        storage=storage,
    )
    return succeeded


async def _run_configured_session(
    prompt: str,
    cwd: Path,
    provider_name: str | None = None,
    model: str | None = None,
    thinking_level: ThinkingLevel | None = None,
    extension_paths: tuple[Path, ...] = (),
    trust_override: TrustOverride | None = None,
    output_mode: PrintOutputMode = PrintOutputMode.human,
    *,
    storage: SessionStorage | None = None,
) -> tuple[bool, str, str]:
    """Construct and execute one coding session."""
    if not cwd.is_dir():
        raise CwdNotFoundError(f"Working directory does not exist: {cwd}")
    dynamic = None
    try:
        selection = resolve_provider_selection(
            load_provider_settings(), provider_name=provider_name, model=model
        )
    except ProviderConfigError:
        if provider_name is None:
            raise
        dynamic = await resolve_dynamic_startup(
            provider_name=provider_name, model=model, cwd=cwd, extension_paths=extension_paths
        )
        if dynamic is None:
            raise
        selected_name, selected_model = dynamic.provider_name, dynamic.model
        active_provider, level = dynamic.provider, thinking_level
    else:
        selected_name, selected_model = selection.provider.name, selection.model
        level = resolve_startup_thinking_level(
            selection.provider, selection.model, cli_override=thinking_level
        )
        active_provider = create_model_provider(
            selection.provider, model=selection.model, thinking_level=level
        )
    session: CodingSession | None = None
    try:
        shell = load_shell_settings()
        _, trust = await ProjectTrustCoordinator(ProjectTrustStore()).resolve(
            cwd, override=trust_override, default=shell.default_project_trust
        )
        for diagnostic in trust.diagnostics:
            print(diagnostic, file=sys.stderr)
        session = await CodingSession.load(
            CodingSessionConfig(
                provider=active_provider,
                extension_runtime=dynamic.runtime if dynamic else None,
                extension_paths=extension_paths,
                model=selected_model,
                cwd=cwd,
                provider_name=selected_name,
                storage=storage or InMemorySessionStorage(),
                project_resources_trusted=trust.trusted,
                thinking_level=level,
                shell_command_prefix=shell.shell_command_prefix,
            )
        )
        return await _render_run(session, prompt, output_mode), selected_name, selected_model
    finally:
        if session is not None:
            await session.aclose()
        await active_provider.aclose()
        if session is None and dynamic is not None:
            await dynamic.runtime.aclose()


async def _render_run(
    session: CodingSession, prompt: str, output_mode: PrintOutputMode = PrintOutputMode.human
) -> bool:
    """Render a session run in the requested output format."""
    renderer = create_event_renderer(output_mode)
    if isinstance(renderer, PlainEventRenderer):
        renderer.render_message(prompt)
    async for event in session.run(prompt):
        renderer.render(event)
    return renderer.finish()


async def run_print_mode(
    *,
    prompt: str,
    model: str,
    cwd: Path,
    provider: ModelProvider,
    storage: SessionStorage | None = None,
    output_mode: PrintOutputMode = PrintOutputMode.human,
) -> bool:
    """Run one task with an injected provider for integration tests."""
    session = await CodingSession.load(
        CodingSessionConfig(
            provider=provider, model=model, cwd=cwd, storage=storage or InMemorySessionStorage()
        )
    )
    try:
        return await _render_run(session, prompt, output_mode)
    finally:
        await session.aclose()
