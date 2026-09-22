"""One-shot command-line interface for Rio."""

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
from rio.coding.session_store import InMemorySessionStorage, SessionStorage
from rio.coding.shell_config import load_shell_settings
from rio.coding.thinking import ThinkingLevel, normalize_thinking_level
from rio.coding.version import current_version as _current_version

app = typer.Typer(name="rio", help="One-shot SKILL.state coding agent.", add_completion=False)
console = Console()


def _resolve_task(parts: list[str]) -> str:
    """Join task arguments, expanding a sole Markdown task file."""
    task = " ".join(parts).strip()
    path = Path(task).expanduser()
    if path.is_file() and path.suffix.lower() == ".md":
        return path.read_text(encoding="utf-8")
    return task


@app.command()
def main(
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
    version: Annotated[bool, typer.Option("--version", "-v")] = False,
) -> None:
    """Execute one task and exit when it completes or aborts."""
    if version:
        console.print(f"rio {_current_version()}")
        return
    if approve and no_approve:
        raise typer.BadParameter("--approve and --no-approve cannot be used together")
    prompt = _resolve_task(task or [])
    if not prompt:
        raise typer.BadParameter("A task or Markdown task file is required")
    level = normalize_thinking_level(thinking) if thinking else None
    succeeded = anyio.run(
        run_configured_session,
        prompt,
        cwd or Path.cwd(),
        provider,
        model,
        level,
        tuple(extension or ()),
        "approve" if approve else "decline" if no_approve else None,
    )
    if not succeeded:
        raise typer.Exit(1)


async def run_configured_session(
    prompt: str,
    cwd: Path,
    provider_name: str | None = None,
    model: str | None = None,
    mode: str | None = None,
    thinking_level: ThinkingLevel | None = None,
    extension_paths: tuple[Path, ...] = (),
    trust_override: TrustOverride | None = None,
) -> bool:
    """Construct and execute one isolated coding session."""
    if not cwd.is_dir():
        raise ValueError(f"Working directory does not exist: {cwd}")
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
                storage=InMemorySessionStorage(),
                project_resources_trusted=trust.trusted,
                thinking_level=level,
                shell_command_prefix=shell.shell_command_prefix,
            )
        )
        return await _render_run(session, prompt)
    finally:
        if session is not None:
            await session.aclose()
        await active_provider.aclose()
        if session is None and dynamic is not None:
            await dynamic.runtime.aclose()


async def _render_run(session: CodingSession, prompt: str) -> bool:
    """Render committed steps until the one-shot run completes or aborts."""
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
