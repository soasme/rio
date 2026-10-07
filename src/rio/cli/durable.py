"""CLI integration for SQLite Sessions; legacy notebook runs remain explicitly selectable."""

from __future__ import annotations

import json
import statistics
import uuid
from pathlib import Path

from rio.coding.extensions.startup import resolve_dynamic_startup
from rio.coding.paths import RioPaths
from rio.coding.provider_config import (
    ProviderConfigError,
    load_provider_settings,
    resolve_provider_selection,
    resolve_startup_thinking_level,
)
from rio.coding.provider_runtime import create_model_provider
from rio.coding.rendering import PrintOutputMode
from rio.durable import Session, Store
from rio.durable.runner import run


async def run_durable_session(
    prompt,
    cwd=None,
    provider_name=None,
    model=None,
    thinking_level=None,
    extension_paths=(),
    trust_override=None,
    resume=None,
    output_mode=PrintOutputMode.human,
):
    root = RioPaths().sessions_dir / "durable"
    if resume is not None and (
        len(resume) != 32 or any(c not in "0123456789abcdef" for c in resume)
    ):
        raise ValueError(
            "Expected a durable Session ID; use --runtime notebook for legacy sessions"
        )
    session_id = resume or uuid.uuid4().hex
    path = root / f"{session_id}.sqlite3"
    if resume and not path.is_file():
        raise ValueError(f"No durable Session found with id '{resume}'")
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
            provider_name, model = saved["provider"], saved["model"]
            thinking_level = saved.get("thinking")
        else:
            session = None
            cwd = (cwd or Path.cwd()).resolve()
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
                )

            def publish(event):
                if output_mode == PrintOutputMode.json:
                    print(json.dumps({"session_id": session_id, **event}), flush=True)
                elif event["type"] == "execution_output":
                    # Print only newly committed bytes from each bounded output snapshot.
                    for key, execution in event["changes"]["executions"].items():
                        for name in ("output", "stderr"):
                            text = execution[name]
                            previous = displayed.get((key, name), 0)
                            print(text[previous:], end="", flush=True)
                            displayed[key, name] = len(text)
                elif event["type"] == "run_ended":
                    print(event["changes"]["meta"]["terminal"]["reason"], flush=True)

            displayed = {}
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
