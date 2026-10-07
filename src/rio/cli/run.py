"""Run a coding task in a SQLite session."""

from __future__ import annotations

import json
import statistics
import uuid
from pathlib import Path

from rio.coding.context import load_agents_md
from rio.coding.extensions.startup import resolve_dynamic_startup
from rio.coding.paths import RioPaths
from rio.coding.project_trust import TrustOverride
from rio.coding.provider_config import (
    ProviderConfigError,
    load_provider_settings,
    resolve_provider_selection,
    resolve_startup_thinking_level,
)
from rio.coding.provider_runtime import create_model_provider
from rio.coding.rendering import PrintOutputMode
from rio.coding.rendering.steps import PlainEventRenderer
from rio.coding.system_prompt import format_project_context


async def run_persistent_session(
    prompt: str,
    cwd: Path | None = None,
    provider_name: str | None = None,
    model: str | None = None,
    thinking_level: str | None = None,
    extension_paths: tuple[Path, ...] = (),
    trust_override: TrustOverride | None = None,
    resume: str | None = None,
    output_mode: PrintOutputMode = PrintOutputMode.human,
    agents_md: Path | None = None,
) -> tuple[bool, str]:
    from rio.agent import Session, Store
    from rio.agent.runner import run

    root = RioPaths().sessions_dir
    if resume is not None and (
        len(resume) != 32 or any(c not in "0123456789abcdef" for c in resume)
    ):
        raise ValueError("Expected a 32-character session ID")
    session_id = resume or uuid.uuid4().hex
    path = root / f"{session_id}.sqlite3"
    if resume and not path.is_file():
        raise ValueError(f"No Session found with id '{resume}'")
    with Store(path) as store:
        if store.data:
            session = Session(store)
            recorded = Path(session.meta["cwd"])
            if cwd is not None and cwd.resolve() != recorded:
                raise ValueError("--cwd must match the resumed Session")
            cwd = recorded
            if prompt and prompt != session.state["goal"]:
                raise ValueError("Resume preserves the original goal; omit TASK or start a new run")
            saved = session.meta["model_config"]
            if provider_name and provider_name != saved["provider"]:
                raise ValueError("Resume preserves the configured provider")
            if model and model != saved["model"]:
                raise ValueError("Resume preserves the configured model")
            if agents_md is not None:
                raise ValueError("Resume preserves the project instructions")
            provider_name, model = saved["provider"], saved["model"]
            thinking_level = saved.get("thinking")
        else:
            session = None
            cwd = (cwd or Path.cwd()).resolve()
        if not cwd.is_dir():
            raise ValueError(f"Working directory does not exist: {cwd}")
        if session is None:
            context = load_agents_md(cwd, agents_md)
            instructions = format_project_context((context,) if context else ())
        dynamic = None
        try:
            selection = resolve_provider_selection(
                load_provider_settings(), provider_name=provider_name, model=model
            )
        except ProviderConfigError:
            if provider_name is None:
                raise
            dynamic = await resolve_dynamic_startup(
                provider_name=provider_name,
                model=model,
                cwd=cwd,
                extension_paths=extension_paths,
                trust_override=trust_override,
            )
            if dynamic is None:
                raise
            provider = dynamic.provider
            provider_name, model = dynamic.provider_name, dynamic.model
        else:
            provider_name, model = selection.provider.name, selection.model
            thinking_level = resolve_startup_thinking_level(
                selection.provider, selection.model, cli_override=thinking_level
            )
            provider = create_model_provider(
                selection.provider, model=model, thinking_level=thinking_level
            )
        try:
            if session is None:
                session = Session.create(
                    store,
                    prompt,
                    cwd,
                    model_config={
                        "provider": provider_name,
                        "model": model,
                        "thinking": thinking_level,
                    },
                    instructions=instructions,
                )

            def publish(event):
                if output_mode == PrintOutputMode.json:
                    print(json.dumps({"session_id": session_id, **event}), flush=True)
                else:
                    renderer.render(event)

            renderer = PlainEventRenderer()
            succeeded = await run(session, provider, model, publish)
            if "metrics" not in store.data:
                store.commit(
                    "durability_metrics",
                    {
                        "metrics": {
                            "commit_p50_seconds": statistics.median(store.commit_seconds),
                            "commit_max_seconds": max(store.commit_seconds),
                            "rebuild_seconds": store.rebuild_seconds,
                            "journal_bytes": store.journal_bytes,
                        }
                    },
                )
                publish(store.events(store.sequence - 1)[0])
            return succeeded, session_id
        finally:
            await provider.aclose()
            if dynamic:
                await dynamic.runtime.aclose()
