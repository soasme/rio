"""Command-line entry point for Rio."""

from __future__ import annotations

import sys
from functools import partial
from os import environ
from pathlib import Path
from typing import Annotated

import anyio
import typer

from rio_ai.env import (
    DEFAULT_OPENAI_COMPATIBLE_BASE_URL,
    DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES,
    DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS,
    DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS,
)
from rio_ai.provider import ModelProvider
from rio_coding.auth_commands import login_provider, logout_provider
from rio_coding.catalog_loader import user_catalog_path
from rio_coding.credentials import FileCredentialStore
from rio_coding.extension_installer import ExtensionInstallError, install_extension
from rio_coding.extensions.startup import resolve_dynamic_startup
from rio_coding.frontend_session import ConfiguredSession
from rio_coding.models_dev_store import (
    ModelsDevRefreshError,
    ModelsDevRefreshResult,
    refresh_models_dev_catalog,
)
from rio_coding.project_trust import ProjectTrustCoordinator, ProjectTrustStore, TrustOverride
from rio_coding.provider_config import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER_NAME,
    CredentialReader,
    OpenAICompatibleProviderConfig,
    ProviderConfig,
    ProviderConfigError,
    ProviderSettings,
    load_provider_settings,
    provider_kind,
    resolve_provider_selection,
    resolve_startup_thinking_level,
    save_provider_settings,
    upsert_openai_compatible_provider,
)
from rio_coding.provider_runtime import DeferredModelProvider, create_model_provider
from rio_coding.rendering import PrintOutputMode, create_event_renderer
from rio_coding.resources import RioResourcePaths
from rio_coding.rpc import RpcServer
from rio_coding.session import CodingSession, CodingSessionConfig
from rio_coding.session_export import (
    default_session_export_artifact_path,
    export_session_artifact,
    normalize_export_format,
)
from rio_coding.session_manager import CodingSessionRecord, SessionManager, validate_session_id
from rio_coding.session_store import InMemorySessionStorage, JsonlSessionStorage, SessionStorage
from rio_coding.shell_config import load_shell_settings
from rio_coding.thinking import ThinkingLevel, normalize_thinking_level
from rio_coding.updater import update_rio
from rio_coding.version import current_version as _current_version

app = typer.Typer(name="rio", help="SKILL.state coding agent.", add_completion=False)


def providers_command() -> None:
    """List configured model providers."""
    render_provider_settings(load_provider_settings(), credential_reader=FileCredentialStore())


def install_command(args: list[str]) -> None:
    """Install an extension into Rio's user extension directory."""
    source: str | None = None
    force = False
    for arg in args:
        if arg == "--force":
            force = True
        elif arg.startswith("-"):
            raise typer.BadParameter(f"Unknown option for `rio install`: {arg}")
        elif source is None:
            source = arg
        else:
            raise typer.BadParameter("Usage: rio install <source> [--force]")
    if source is None:
        raise typer.BadParameter("Usage: rio install <source> [--force]")

    typer.echo(
        "Warning: extensions execute arbitrary Python with your user permissions. "
        "Only install sources you trust.",
        err=True,
    )
    try:
        destination = install_extension(source, force=force)
    except ExtensionInstallError as exc:
        typer.echo(f"Could not install extension: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Installed {source} to {destination}")


def setup_command(
    *,
    provider_name: str = DEFAULT_PROVIDER_NAME,
    base_url: str = DEFAULT_OPENAI_COMPATIBLE_BASE_URL,
    api_key_env: str = "OPENAI_API_KEY",
    model: str = DEFAULT_MODEL,
    timeout_seconds: float = DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES,
    max_retry_delay_seconds: float = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS,
    set_default: bool = True,
) -> None:
    """Create or update an OpenAI-compatible provider entry."""
    settings = load_provider_settings()
    provider = OpenAICompatibleProviderConfig(
        name=provider_name,
        base_url=base_url.rstrip("/"),
        api_key_env=api_key_env,
        models=(model,),
        default_model=model,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        max_retry_delay_seconds=max_retry_delay_seconds,
    )
    updated = upsert_openai_compatible_provider(settings, provider, set_default=set_default)
    path = save_provider_settings(updated)
    typer.echo(
        f"Saved provider '{provider.name}' to {user_catalog_path()} and preferences to {path}"
    )
    if provider.api_key_env not in environ:
        typer.echo(f"Set {provider.api_key_env} before running Rio with this provider.", err=True)


def update_models_command() -> None:
    """Force-refresh and persist the runtime model catalog."""

    async def refresh() -> ModelsDevRefreshResult:
        return await refresh_models_dev_catalog(force=True)

    try:
        result = anyio.run(refresh)
    except ModelsDevRefreshError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from error
    status = "refreshed" if result.refreshed else "unchanged"
    typer.echo(
        f"Model catalogs {status}: {result.model_count} models cached at {result.cache_path}"
    )


def update_command() -> None:
    """Upgrade Rio using the installer that manages the current environment."""
    result = update_rio()
    if not result.succeeded:
        typer.echo("Could not safely update Rio:", err=True)
        for failure in result.failures:
            typer.echo(f"- {failure}", err=True)
        raise typer.Exit(1)
    if result.stdout:
        typer.echo(result.stdout)
    if result.stderr:
        typer.echo(result.stderr, err=True)
    if result.deferred:
        typer.echo(f"Rio update handed off with: {' '.join(result.command or ())}")
    else:
        typer.echo(f"Rio update completed with: {' '.join(result.command or ())}")


def render_session_list(records: list[CodingSessionRecord]) -> None:
    """Render indexed sessions for the CLI."""
    if not records:
        typer.echo("No sessions found.")
        return

    for record in records:
        title = record.title or "Untitled"
        typer.echo(f"{record.id}\t{title}\t{record.model}\t{record.cwd}")


async def export_session_command(
    session_ref: str,
    output_path: Path | None = None,
    export_format: str | None = None,
    session_manager: SessionManager | None = None,
) -> Path:
    """Export an indexed session id or JSONL file path."""
    session_path, title = _resolve_export_source(session_ref, session_manager)
    entries = await JsonlSessionStorage(session_path).read_all()
    normalized_format = normalize_export_format(
        export_format or (output_path.suffix.removeprefix(".") if output_path else "html")
    )
    destination = _resolve_export_destination(
        output_path,
        session_path=session_path,
        format=normalized_format,
    )
    return export_session_artifact(
        entries,
        destination,
        title=title,
        source=str(session_path),
        format=normalized_format,
    )


def _run_export_cli(args: list[str]) -> None:
    """Run `rio export`/`rio --export` and exit."""
    try:
        session_ref, output_path, export_format = _parse_export_cli_args(args)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    try:
        exported_path = anyio.run(
            export_session_command,
            session_ref,
            output_path,
            export_format,
        )
    except (RuntimeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Exported session to {exported_path}")
    raise typer.Exit()


def _resolve_prompt_input(value: str, *, option: str) -> str:
    """Resolve an existing UTF-8 file, otherwise preserve literal prompt text."""
    try:
        path = Path(value).expanduser()
    except RuntimeError:
        return value
    try:
        exists = path.exists()
    except OSError as exc:
        raise typer.BadParameter(
            f"Could not inspect {option} path {path}: {exc}",
            param_hint=option,
        ) from exc
    if not exists:
        return value
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise typer.BadParameter(
            f"Could not read {option} file {path}: {exc}",
            param_hint=option,
        ) from exc


def _resolve_append_system_prompts(values: tuple[str, ...] | list[str]) -> str | None:
    """Resolve repeated append inputs in order and separate them by one blank line."""
    if not values:
        return None
    return "\n\n".join(
        _resolve_prompt_input(value, option="--append-system-prompt") for value in values
    )


def _merge_stdin_prompt(prompt: str) -> str:
    """Merge piped stdin content into a print-mode prompt, mirroring Pi.

    When stdin is not a terminal (e.g. `cat file | rio -p "..."`), its
    contents are prepended to the prompt text.
    """
    stdin = sys.stdin
    if stdin is None:
        return prompt
    try:
        if stdin.isatty():
            return prompt
    except (AttributeError, ValueError):
        return prompt
    try:
        piped = stdin.read()
    except (OSError, ValueError):
        return prompt
    if not piped:
        return prompt
    if not prompt:
        return piped
    return f"{piped}\n\n{prompt}"


def _parse_export_cli_args(args: list[str]) -> tuple[str, Path | None, str | None]:
    if not args:
        raise RuntimeError("Usage: rio export <session-id-or-jsonl> [--format html|jsonl] [output]")
    session_ref = args[0]
    output_path: Path | None = None
    export_format: str | None = None
    index = 1
    while index < len(args):
        arg = args[index]
        if arg == "--format":
            index += 1
            if index >= len(args):
                raise RuntimeError(
                    "Usage: rio export <session-id-or-jsonl> [--format html|jsonl] [output]"
                )
            export_format = args[index]
        elif arg.startswith("--format="):
            export_format = arg.partition("=")[2]
        elif arg.startswith("-"):
            raise RuntimeError(f"Unknown export option: {arg}")
        elif output_path is None:
            output_path = Path(arg).expanduser()
        else:
            raise RuntimeError(
                "Usage: rio export <session-id-or-jsonl> [--format html|jsonl] [output]"
            )
        index += 1
    return session_ref, output_path, export_format


def _resolve_export_destination(
    output_path: Path | None,
    *,
    session_path: Path,
    format: str,
) -> Path:
    if output_path is None:
        return default_session_export_artifact_path(
            session_path,
            destination_dir=Path.cwd(),
            format=format,
        )
    if output_path.suffix:
        return output_path
    return default_session_export_artifact_path(
        session_path,
        destination_dir=output_path,
        format=format,
    )


def _resolve_export_source(
    session_ref: str,
    session_manager: SessionManager | None = None,
) -> tuple[Path, str]:
    candidate_path = Path(session_ref).expanduser()
    if candidate_path.exists():
        if candidate_path.is_dir():
            raise RuntimeError(f"Session export source is a directory: {candidate_path}")
        return candidate_path, f"Rio session {candidate_path.stem}"

    manager = session_manager or SessionManager()
    record = manager.get_session(session_ref)
    if record is None:
        raise RuntimeError(f"Unknown session or file: {session_ref}")

    title = record.title or f"Rio session {record.id}"
    return record.path, title


def render_provider_settings(
    settings: ProviderSettings,
    *,
    credential_reader: CredentialReader | None = None,
) -> None:
    """Render configured providers for the CLI."""
    for provider in settings.providers:
        marker = "*" if provider.name == settings.default_provider else " "
        models = ",".join(provider.models)
        typer.echo(
            f"{marker}\t{provider.name}\t{provider_kind(provider)}\t"
            f"{provider.default_model}\t{models}\t{provider.api_key_env}\t"
            f"{_provider_credential_status(provider, credential_reader=credential_reader)}\t"
            f"{provider.base_url}\t{provider.timeout_seconds:g}s\t"
            f"retries={provider.max_retries}\t"
            f"retry_delay={provider.max_retry_delay_seconds:g}s"
        )


def _provider_credential_status(
    provider: ProviderConfig,
    *,
    credential_reader: CredentialReader | None,
) -> str:
    if provider.credential_name and credential_reader is not None:
        if provider_kind(provider) == "openai-codex":
            get_oauth = getattr(credential_reader, "get_oauth", None)
            if get_oauth is not None and get_oauth(provider.credential_name) is not None:
                return f"stored:{provider.credential_name}"
        elif credential_reader.get(provider.credential_name):
            return f"stored:{provider.credential_name}"
    if environ.get(provider.api_key_env):
        return f"env:{provider.api_key_env}"
    return "missing"


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def main(
    ctx: typer.Context,
    prompt_args: Annotated[list[str] | None, typer.Argument()] = None,
    print_mode: Annotated[bool, typer.Option("--print", "-p")] = False,
    mode: Annotated[str | None, typer.Option(help="Output mode: text, json, or rpc.")] = None,
    provider: Annotated[str | None, typer.Option()] = None,
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    cwd: Annotated[Path | None, typer.Option()] = None,
    session: Annotated[str | None, typer.Option(help="Resume an indexed session.")] = None,
    session_id: Annotated[str | None, typer.Option()] = None,
    thinking: Annotated[str | None, typer.Option("--thinking", "-t")] = None,
    extension: Annotated[list[Path] | None, typer.Option("--extension", "-e")] = None,
    system_prompt: Annotated[str | None, typer.Option()] = None,
    append_system_prompt: Annotated[list[str] | None, typer.Option()] = None,
    approve: Annotated[bool, typer.Option("--approve", "-a")] = False,
    no_approve: Annotated[bool, typer.Option("--no-approve")] = False,
    version: Annotated[bool, typer.Option("--version", "-v")] = False,
    base_url: str = DEFAULT_OPENAI_COMPATIBLE_BASE_URL,
    api_key_env: str = "OPENAI_API_KEY",
    timeout_seconds: float = DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRIES,
    max_retry_delay_seconds: float = DEFAULT_OPENAI_COMPATIBLE_MAX_RETRY_DELAY_SECONDS,
    set_default: bool = True,
    models: bool = False,
    method: str | None = None,
) -> None:
    """Run a prompt, or manage setup, providers, sessions, export, install, and update."""
    if version:
        typer.echo(f"rio {_current_version()}")
        return
    args = prompt_args or []
    if not print_mode and mode is None and args:
        command, *rest = args
        try:
            if command in ("login", "logout"):
                if len(rest) != 1:
                    raise typer.BadParameter(f"Usage: rio {command} PROVIDER")
                result = (
                    anyio.run(partial(login_provider, rest[0], method=method))
                    if command == "login"
                    else logout_provider(rest[0])
                )
                typer.echo(result)
                return
            if command == "providers" and not rest:
                providers_command()
                return
            if command == "sessions" and not rest:
                render_session_list(SessionManager().list_sessions())
                return
            if command == "setup" and not rest:
                setup_command(
                    provider_name=provider or DEFAULT_PROVIDER_NAME,
                    model=model or DEFAULT_MODEL,
                    base_url=base_url,
                    api_key_env=api_key_env,
                    timeout_seconds=timeout_seconds,
                    max_retries=max_retries,
                    max_retry_delay_seconds=max_retry_delay_seconds,
                    set_default=set_default,
                )
                return
            if command == "export":
                _run_export_cli(rest)
            if command == "install":
                install_command(rest)
                return
            if command == "update":
                if rest:
                    raise typer.BadParameter("Usage: rio update [--models]")
                (update_models_command if models else update_command)()
                return
        except (ValueError, OSError) as exc:
            raise typer.BadParameter(str(exc)) from exc
    if any(arg.startswith("--") for arg in args):
        raise typer.BadParameter("Unknown option in prompt; quote the prompt as one argument")
    if ctx.args:
        raise typer.BadParameter(f"Unknown arguments: {' '.join(ctx.args)}")
    if models:
        raise typer.BadParameter("--models requires rio update")
    if mode not in (None, "text", "json", "rpc"):
        raise typer.BadParameter("--mode must be text, json, or rpc")
    if approve and no_approve:
        raise typer.BadParameter("--approve and --no-approve cannot be used together")
    if session and session_id:
        raise typer.BadParameter("--session and --session-id cannot be used together")
    if mode == "rpc" and (args or print_mode):
        raise typer.BadParameter("RPC mode reads commands from stdin; omit prompts and --print")
    headless = print_mode or mode in ("text", "json")
    prompt = " ".join(args)
    if headless:
        prompt = _merge_stdin_prompt(prompt)
        if not prompt.strip():
            raise typer.BadParameter("Print mode requires a prompt or piped input")
    try:
        if session_id:
            validate_session_id(session_id)
        level = normalize_thinking_level(thinking) if thinking else None
        succeeded = anyio.run(
            partial(
                run_configured_session,
                prompt=prompt,
                mode=mode or ("text" if headless else "tui"),
                cwd=cwd or Path.cwd(),
                provider_name=provider,
                model=model,
                resume_session_id=session,
                session_id=session_id,
                thinking_level=level,
                extension_paths=tuple(extension or ()),
                custom_system_prompt=(
                    _resolve_prompt_input(system_prompt, option="--system-prompt")
                    if system_prompt is not None
                    else None
                ),
                append_system_prompt=_resolve_append_system_prompts(append_system_prompt or []),
                trust_override="approve" if approve else "decline" if no_approve else None,
            )
        )
    except (RuntimeError, ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if not succeeded:
        raise typer.Exit(1)


async def run_configured_session(
    *,
    prompt: str,
    mode: str,
    cwd: Path,
    provider_name: str | None = None,
    model: str | None = None,
    resume_session_id: str | None = None,
    session_id: str | None = None,
    thinking_level: ThinkingLevel | None = None,
    custom_system_prompt: str | None = None,
    append_system_prompt: str | None = None,
    trust_override: TrustOverride | None = None,
    extension_paths: tuple[Path, ...] = (),
) -> bool:
    """Resolve provider and journal once for all three frontends."""
    if not cwd.is_dir():
        raise ValueError(f"Working directory does not exist: {cwd}")
    manager = SessionManager()
    record = manager.get_session(resume_session_id) if resume_session_id else None
    if resume_session_id and record is None:
        raise ValueError(f"Unknown session: {resume_session_id}")
    selected_name = provider_name or (record.provider_name if record and model is None else None)
    selected_model = model or (record.model if record and provider_name is None else None)
    dynamic = None
    try:
        selection = resolve_provider_selection(
            load_provider_settings(),
            provider_name=selected_name,
            model=selected_model,
        )
    except ProviderConfigError:
        if selected_name is None:
            raise
        dynamic = await resolve_dynamic_startup(
            provider_name=selected_name,
            model=selected_model,
            cwd=record.cwd if record else cwd,
            extension_paths=extension_paths,
        )
        if dynamic is None:
            raise
        selected_name, selected_model = dynamic.provider_name, dynamic.model
        provider, level = dynamic.provider, thinking_level
    else:
        selected_name, selected_model = selection.provider.name, selection.model
        level = resolve_startup_thinking_level(
            selection.provider,
            selection.model,
            cli_override=thinking_level,
        )
        if mode == "tui":
            # Login may save a credential reference as well as the credential,
            # so read settings again when the user actually submits a task.
            provider = DeferredModelProvider(
                lambda: create_model_provider(
                    load_provider_settings().get_provider(selected_name),
                    model=selected_model,
                    thinking_level=level,
                )
            )
        else:
            provider = create_model_provider(
                selection.provider, model=selection.model, thinking_level=level
            )
    session = None
    try:
        shell = load_shell_settings()
        target_cwd = record.cwd if record else cwd
        _, trust = await ProjectTrustCoordinator(ProjectTrustStore()).resolve(
            target_cwd, override=trust_override, default=shell.default_project_trust
        )
        for diagnostic in trust.diagnostics:
            typer.echo(diagnostic, err=True)
        if record is None:
            record = manager.create_session_exclusive(
                cwd=cwd,
                model=selected_model,
                provider_name=selected_name,
                session_id=session_id,
            )
        session = await CodingSession.load(
            CodingSessionConfig(
                provider=provider,
                extension_runtime=dynamic.runtime if dynamic else None,
                extension_paths=extension_paths,
                load_extensions=True,
                model=selected_model,
                cwd=record.cwd,
                provider_name=selected_name,
                storage=JsonlSessionStorage(record.path),
                project_resources_trusted=trust.trusted,
                thinking_level=level,
                custom_prompt=custom_system_prompt,
                append_system_prompt=append_system_prompt,
                shell_command_prefix=shell.shell_command_prefix,
            )
        )
        session = ConfiguredSession(session, session_manager=manager, session_id=record.id)
        if mode == "rpc":
            await RpcServer(session).run()
            return True
        if mode == "tui":
            from rio_tui import run_tui_app

            await run_tui_app(session, initial_prompt=prompt or None)
            return True
        return await _render_prompt(session, prompt, PrintOutputMode(mode))
    finally:
        try:
            if session is not None:
                await session.aclose()
        finally:
            await provider.aclose()
            if session is None and dynamic is not None:
                await dynamic.runtime.aclose()


async def _render_prompt(session: CodingSession, prompt: str, output: PrintOutputMode) -> bool:
    renderer = create_event_renderer(output)
    async for event in session.prompt(prompt):
        renderer.render(event)
    return renderer.finish()


async def run_print_mode(
    *,
    prompt: str,
    model: str,
    cwd: Path,
    provider: ModelProvider,
    output: PrintOutputMode = PrintOutputMode.text,
    storage: SessionStorage | None = None,
    resource_paths: RioResourcePaths | None = None,
    custom_system_prompt: str | None = None,
    append_system_prompt: str | None = None,
) -> bool:
    """Run one prompt with an injected provider and optional durable journal."""
    session = await CodingSession.load(
        CodingSessionConfig(
            provider=provider,
            model=model,
            cwd=cwd,
            storage=storage or InMemorySessionStorage(),
            resource_paths=resource_paths,
            custom_prompt=custom_system_prompt,
            append_system_prompt=append_system_prompt,
        )
    )
    try:
        return await _render_prompt(session, prompt, output)
    finally:
        await session.aclose()
