"""Provider ownership and live reconfiguration shared by interactive frontends."""

from dataclasses import replace

from rio_coding.events import ThinkingLevelChangedEvent
from rio_coding.extensions.providers import DynamicProvider
from rio_coding.project_trust import ProjectTrustCoordinator, ProjectTrustStore
from rio_coding.provider_config import (
    load_provider_settings,
    provider_thinking_levels,
    resolve_provider_selection,
    resolve_startup_thinking_level,
)
from rio_coding.provider_runtime import create_dynamic_model_provider, create_model_provider
from rio_coding.session import CodingSession
from rio_coding.session_manager import SessionManager
from rio_coding.session_store import JsonlSessionStorage, ModelChangeEntry, ThinkingLevelChangeEntry
from rio_coding.thinking import normalize_thinking_level


class ConfiguredSession:
    """Forward the session API while constructing providers for runtime changes.

    The initial provider remains owned by the caller. Replacement providers are
    owned here and closed when replaced or when the frontend closes.
    """

    def __init__(self, session: CodingSession, *, session_manager=None, session_id=None) -> None:
        self.session = session
        self._owned_provider = None
        self.session_manager = session_manager or SessionManager()
        self.session_id = session_id

    def __getattr__(self, name):
        return getattr(self.session, name)

    @property
    def available_providers(self):
        names = [p.name for p in load_provider_settings().providers]
        for item in self.session.extensions.provider_registry.effective_providers():
            if isinstance(item.definition, DynamicProvider):
                names.append(item.definition.id)
        return tuple(dict.fromkeys(names))

    @property
    def available_models(self):
        item = self.session.extensions.provider_registry.effective(self.provider_name)
        if item is not None and isinstance(item.definition, DynamicProvider):
            return tuple(model.id for model in item.definition.models)
        return load_provider_settings().get_provider(self.provider_name).models

    @property
    def available_thinking_levels(self):
        item = self.session.extensions.provider_registry.effective(self.provider_name)
        if item is not None and isinstance(item.definition, DynamicProvider):
            model = next((m for m in item.definition.models if m.id == self.model), None)
            return model.thinking_levels or () if model else ()
        provider = load_provider_settings().get_provider(self.provider_name)
        return provider_thinking_levels(provider, model=self.model)

    async def set_provider_name(self, name):
        await self._switch(None, name, None)

    async def _candidate(self, model, provider_name, thinking_level):
        registry = self.session.extensions.provider_registry
        effective = registry.effective(provider_name) if provider_name else None
        if effective is not None and isinstance(effective.definition, DynamicProvider):
            definition = effective.definition
            model = model or definition.default_model
            if model is None:
                raise ValueError(f"No default model for {provider_name}")
            provider = await create_dynamic_model_provider(
                definition,
                model=model,
                credential_store=registry.credentials,
                environment=registry.environment,
            )
            return provider, provider_name, model, thinking_level
        selection = resolve_provider_selection(
            load_provider_settings(), provider_name=provider_name, model=model
        )
        level = resolve_startup_thinking_level(
            selection.provider, selection.model, cli_override=thinking_level
        )
        provider = create_model_provider(
            selection.provider, model=selection.model, thinking_level=level
        )
        return provider, selection.provider.name, selection.model, level

    async def resume_session(self, session_id):
        record = self.session_manager.get_session(session_id)
        if record is None:
            raise ValueError(f"Unknown session: {session_id}")
        await self._replace_session(record)

    async def new_session(self):
        if self.is_running:
            raise ValueError("Wait for the active run before switching sessions")
        record = self.session_manager.create_session_exclusive(
            cwd=self.cwd, model=self.model, provider_name=self.provider_name
        )
        await self._replace_session(record)

    async def _replace_session(self, record):
        if self.is_running:
            raise ValueError("Wait for the active run before switching sessions")
        provider, name, model, level = await self._candidate(
            record.model, record.provider_name, None
        )
        try:
            _, trust = await ProjectTrustCoordinator(ProjectTrustStore()).resolve(record.cwd)
            candidate = await CodingSession.load(
                replace(
                    self.session.config,
                    provider=provider,
                    provider_name=name,
                    model=model,
                    thinking_level=level,
                    cwd=record.cwd,
                    storage=JsonlSessionStorage(record.path),
                    extension_runtime=None,
                    project_resources_trusted=trust.trusted,
                )
            )
        except BaseException:
            await provider.aclose()
            raise
        await self.aclose()
        self.session = candidate
        self.session_id = record.id
        self._owned_provider = provider

    async def _switch(self, model, provider_name, thinking_level):
        if self.is_running:
            raise ValueError("Wait for the active run before changing providers")
        candidate, name, model, level = await self._candidate(model, provider_name, thinking_level)
        try:
            # Publish both metadata entries atomically before touching the live provider.
            await self.session._runner.append_entries(
                [
                    ModelChangeEntry(model=model, provider=name),
                    ThinkingLevelChangeEntry(thinking_level=level),
                ]
            )
        except BaseException:
            await candidate.aclose()
            raise
        self.session.config.provider = candidate
        self.session.config.provider_name = name
        self.session.config.model = model
        self.session._thinking_level = level
        self.session._runner.rebind(provider=candidate, model=model)
        previous = self._owned_provider
        self._owned_provider = candidate
        if previous is not None:
            await previous.aclose()
        return ThinkingLevelChangedEvent(level=level or "off")

    async def set_model(self, model: str, *, provider_name: str | None = None) -> None:
        await self._switch(model, provider_name or self.provider_name, self.thinking_level)

    async def set_thinking_level(self, level: str | None):
        normalized = normalize_thinking_level(level) if level else "off"
        return await self._switch(self.model, self.provider_name, normalized)

    async def aclose(self):
        try:
            await self.session.aclose()
            if self.session_id is not None:
                self.session_manager.touch_session(
                    self.session_id,
                    model=self.model,
                    provider_name=self.provider_name,
                    title=self.session_title,
                )
        finally:
            if self._owned_provider is not None:
                provider, self._owned_provider = self._owned_provider, None
                await provider.aclose()
