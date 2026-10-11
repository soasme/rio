"""Run a coding task in a SQLite session."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from rio.agent import Session, Store
from rio.agent.runner import replay, run
from rio.coding import cells
from rio.coding.context import load_agents_md
from rio.coding.paths import RioPaths
from rio.coding.provider_config import (
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
    *,
    cwd: Path | None = None,
    provider_name: str | None = None,
    model: str | None = None,
    thinking_level: str | None = None,
    resume: str | None = None,
    output_mode: PrintOutputMode = PrintOutputMode.human,
    agents_md: Path | None = None,
) -> tuple[bool, str]:
    session_id = resume or uuid.uuid4().hex
    path = _session_path(resume, session_id)
    renderer = PlainEventRenderer()

    def publish(event):
        if output_mode == PrintOutputMode.json:
            print(json.dumps({"session_id": session_id, **event}), flush=True)
        else:
            renderer.render(event)

    with Store(path) as store:
        if store.data:
            session = Session(store, cells.KINDS)
            _check_resume(session, prompt, cwd, provider_name, model, agents_md)
            if session.meta["terminal"]:
                return await replay(session, publish), session_id
            config = session.meta["model_config"]
            cwd = Path(session.meta["cwd"])
            provider_name, model = config["provider"], config["model"]
            thinking_level = config.get("thinking")
        else:
            session = None
            cwd = (cwd or Path.cwd()).resolve()
        if not cwd.is_dir():
            raise ValueError(f"Working directory does not exist: {cwd}")
        selection = resolve_provider_selection(
            load_provider_settings(), provider_name=provider_name, model=model
        )
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
                    kinds=cells.KINDS,
                    model_config={
                        "provider": provider_name,
                        "model": model,
                        "thinking": thinking_level,
                    },
                    instructions=_instructions(cwd, agents_md),
                )
            return await run(session, provider, model, publish), session_id
        finally:
            await provider.aclose()


def _session_path(resume: str | None, session_id: str) -> Path:
    if resume is not None and (
        len(resume) != 32 or any(c not in "0123456789abcdef" for c in resume)
    ):
        raise ValueError("Expected a 32-character session ID")
    path = RioPaths().sessions_dir / f"{session_id}.sqlite3"
    if resume and not path.is_file():
        raise ValueError(f"No Session found with id '{resume}'")
    return path


def _check_resume(
    session: Session,
    prompt: str,
    cwd: Path | None,
    provider_name: str | None,
    model: str | None,
    agents_md: Path | None,
) -> None:
    """A resumed Session keeps its goal, project, model, and instructions."""
    if cwd is not None and cwd.resolve() != Path(session.meta["cwd"]):
        raise ValueError("--cwd must match the resumed Session")
    if prompt and prompt != session.state["goal"]:
        raise ValueError("Resume preserves the original goal; omit TASK or start a new run")
    saved = session.meta["model_config"]
    if provider_name and provider_name != saved["provider"]:
        raise ValueError("Resume preserves the configured provider")
    if model and model != saved["model"]:
        raise ValueError("Resume preserves the configured model")
    if agents_md is not None:
        raise ValueError("Resume preserves the project instructions")


def _instructions(cwd: Path, agents_md: Path | None) -> str:
    context = load_agents_md(cwd, agents_md)
    return cells.INSTRUCTIONS + format_project_context((context,) if context else ())
