"""One-shot task execution command."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import anyio
import typer
from rich.console import Console
from rich.panel import Panel
from rich.pretty import Pretty
from rich.text import Text

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
from rio.coding.session import CodingSession, CodingSessionConfig
from rio.coding.session_manager import CodingSessionRecord, SessionManager
from rio.coding.session_store import InMemorySessionStorage, JsonlSessionStorage, SessionStorage
from rio.coding.shell_config import load_shell_settings
from rio.coding.thinking import ThinkingLevel, normalize_thinking_level

console = Console()


class SessionNotFoundError(ValueError):
    """Raised when a --resume session id does not exist."""


class CwdMismatchError(ValueError):
    """Raised when --cwd conflicts with the resumed session's directory."""


class CwdNotFoundError(ValueError):
    """Raised when the requested working directory does not exist."""


def _resolve_task(parts: list[str]) -> str:
    """Join task arguments, expanding a sole Markdown task file."""
    task = " ".join(parts).strip()
    path = Path(task).expanduser()
    if path.is_file() and path.suffix.lower() == ".md":
        return path.read_text(encoding="utf-8")
    return task


def run(
    task: Annotated[
        list[str] | None, typer.Argument(help="Task text or a Markdown task file.")
    ] = None,
    provider: Annotated[str | None, typer.Option()] = None,
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    cwd: Annotated[Path | None, typer.Option()] = None,
    thinking: Annotated[str | None, typer.Option("--thinking", "-t")] = None,
    extension: Annotated[list[Path] | None, typer.Option("--extension", "-e")] = None,
    approve: Annotated[bool, typer.Option("--approve", "-a")] = False,
    no_approve: Annotated[bool, typer.Option("--no-approve")] = False,
    resume: Annotated[
        str | None, typer.Option("--resume", "-r", help="Session id to resume.")
    ] = None,
) -> None:
    """Execute a task, optionally resuming a previous session."""
    if approve and no_approve:
        raise typer.BadParameter("--approve and --no-approve cannot be used together")
    prompt = _resolve_task(task or [])
    if not prompt:
        raise typer.BadParameter("A task or Markdown task file is required")
    level = normalize_thinking_level(thinking) if thinking else None
    try:
        succeeded, session_id = anyio.run(
            run_persistent_session,
            prompt,
            cwd,
            provider,
            model,
            level,
            tuple(extension or ()),
            "approve" if approve else "decline" if no_approve else None,
            resume,
        )
    except SessionNotFoundError as exc:
        raise typer.BadParameter(str(exc), param_hint="--resume") from exc
    except (CwdMismatchError, CwdNotFoundError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--cwd") from exc
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"[dim]Session: {session_id}[/dim]")
    if not succeeded:
        raise typer.Exit(1)


async def run_persistent_session(
    prompt: str,
    cwd: Path | None = None,
    provider_name: str | None = None,
    model: str | None = None,
    thinking_level: ThinkingLevel | None = None,
    extension_paths: tuple[Path, ...] = (),
    trust_override: TrustOverride | None = None,
    resume: str | None = None,
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
            console.print(f"[yellow]{diagnostic}[/yellow]")
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
        return await _render_run(session, prompt), selected_name, selected_model
    finally:
        if session is not None:
            await session.aclose()
        await active_provider.aclose()
        if session is None and dynamic is not None:
            await dynamic.runtime.aclose()


async def _render_run(session: CodingSession, prompt: str) -> bool:
    """Render committed steps until the run completes or aborts."""
    from rio.agent import (
        ActionEndEvent,
        ActionStartEvent,
        RunEndEvent,
        StateUpdateEvent,
        ValidationErrorEvent,
    )
    from rio.coding.events import AutoRetryEndEvent, SessionRunEndEvent

    failed = False
    console.print(Panel(Text("Running", style="bold"), title="Rio"))
    async for event in session.run(prompt):
        if isinstance(event, ActionStartEvent):
            console.print(f"[cyan]→[/cyan] {event.name}")
        elif isinstance(event, StateUpdateEvent):
            console.print(Pretty(event.delta, expand_all=False))
        elif isinstance(event, ActionEndEvent):
            status = "red" if event.is_error else "green"
            console.print(f"[{status}]← {event.name}[/{status}] {event.result.text or ''}")
        elif isinstance(event, ValidationErrorEvent):
            console.print(f"[yellow]Retry:[/yellow] {event.error}")
        elif isinstance(event, AutoRetryEndEvent):
            failed = not event.success
            if event.final_error:
                console.print(f"[red]{event.final_error}[/red]")
        elif isinstance(event, SessionRunEndEvent) and event.answer:
            console.print(Panel(event.answer, title="Result", border_style="green"))
        elif isinstance(event, RunEndEvent):
            console.print(f"[dim]{event.steps} step(s)[/dim]")
    return not failed


async def run_print_mode(
    *,
    prompt: str,
    model: str,
    cwd: Path,
    provider: ModelProvider,
    storage: SessionStorage | None = None,
) -> bool:
    """Run one task with an injected provider for integration tests."""
    session = await CodingSession.load(
        CodingSessionConfig(
            provider=provider, model=model, cwd=cwd, storage=storage or InMemorySessionStorage()
        )
    )
    try:
        return await _render_run(session, prompt)
    finally:
        await session.aclose()
