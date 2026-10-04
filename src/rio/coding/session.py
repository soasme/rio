"""The coding-agent environment: resources, a notebook context, and a run loop.

The model manages its own context, a Jupyter notebook whose code cells run in
the session's working directory, so the session never summarizes or compacts.
Its own work is discovering project resources, assembling the skill
instructions from them, and driving `SessionRunner` over a durable journal.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import anyio

from rio.agent import HarnessSpec, KernelExecutor, Notebook, context_limit
from rio.ai.provider import ModelProvider
from rio.ai.types import JSONValue
from rio.coding.cell_magic import journal_startup
from rio.coding.coding_skill import CodingSkillOptions, build_coding_skill
from rio.coding.context import discover_project_context_with_diagnostics
from rio.coding.events import (
    CodingSessionEvent,
    SessionInfoChangedEvent,
    StateRestoredEvent,
    ThinkingLevelChangedEvent,
)
from rio.coding.extensions import DynamicProvider, ExtensionRuntime
from rio.coding.mcp import prepare as prepare_mcp
from rio.coding.paths import RioPaths
from rio.coding.prompt_templates import (
    expand_prompt_template_command,
    load_prompt_templates_with_diagnostics,
)
from rio.coding.provider_config import (
    ProviderConfigError,
    load_provider_settings,
    toggle_saved_scoped_model,
    toggle_saved_stable_scoped_model,
)
from rio.coding.resources import (
    ResourceDiagnostic,
    RioResourcePaths,
    discover_system_prompt_resources,
    resource_paths_with_cwd,
    resource_paths_with_project_trust,
)
from rio.coding.session_runner import SessionRunner, SessionRunnerConfig
from rio.coding.session_store import (
    CustomEntry,
    JsonlSessionStorage,
    LabelEntry,
    ModelChangeEntry,
    SessionEntry,
    SessionInfoEntry,
    SessionStorage,
    ThinkingLevelChangeEntry,
    checkpoints,
)
from rio.coding.skills import (
    Skill,
    expand_skill_command,
    expand_skill_name_command,
    load_skills_with_diagnostics,
    shadowed_skill_diagnostics,
)
from rio.coding.step_footprint import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    StepFootprint,
    context_window_utilization,
    estimate_step_footprint,
)
from rio.coding.system_prompt import (
    BuildSystemPromptOptions,
    ProjectContextFile,
    PromptSection,
    build_skill_instructions,
)

#: Run once when a session's kernel starts: adds the `%%edit` cell magic. Each
#: session also adds `%cell`, bound to its journal.
KERNEL_STARTUP = "%load_ext rio.coding.edit_magic"

#: A coding run is bounded so a runaway loop cannot burn tokens indefinitely.
DEFAULT_MAX_STEPS = 200


@dataclass(slots=True)
class CodingSessionConfig:
    """Everything needed to stand a coding session up."""

    provider: ModelProvider
    model: str
    cwd: Path = field(default_factory=Path.cwd)
    provider_name: str | None = None
    storage: SessionStorage | None = None
    paths: RioPaths = field(default_factory=RioPaths)
    resource_paths: RioResourcePaths | None = None
    project_resources_trusted: bool = True
    custom_prompt: str | None = None
    append_system_prompt: str | None = None
    extra_guidelines: Sequence[str] = ()
    extra_sections: Sequence[PromptSection] = ()
    thinking_level: str | None = None
    max_steps: int | None = DEFAULT_MAX_STEPS
    max_retries: int = 2
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    session_title: str | None = None
    command_registry: object | None = None
    extension_runtime: ExtensionRuntime | None = None
    extension_paths: Sequence[Path] = ()
    load_extensions: bool = True
    #: Connect the servers in `mcp.json` and install `mcp` in the kernel.
    load_mcp: bool = True
    #: Hold the session's own journal writes until `_commit_prepared_entries()`.
    #: A candidate session that is never adopted -- a declined trust prompt, a
    #: failed provider -- then leaves no trace in the journal.
    defer_authoritative_writes: bool = False


@dataclass(frozen=True, slots=True)
class SessionResources:
    """What discovery found on disk for this session."""

    skills: tuple[Skill, ...] = ()
    context_files: tuple[ProjectContextFile, ...] = ()
    prompt_templates: tuple = ()
    system_prompt_files: tuple[Path, ...] = ()
    diagnostics: tuple[ResourceDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ContextUsage:
    """How much of the model's context window the next step occupies."""

    footprint: StepFootprint
    context_window_tokens: int

    @property
    def utilization(self) -> float:
        return context_window_utilization(self.footprint, self.context_window_tokens)

    @property
    def total_tokens(self) -> int:
        return self.footprint.total_tokens

    @property
    def limit_tokens(self) -> int:
        """The context size the model is asked to stay under."""
        return context_limit(self.context_window_tokens)


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """A selectable model and the provider that serves it."""

    provider_name: str
    model: str


class CodingSession:
    """A coding agent bound to a working directory, its resources, and a journal."""

    def __init__(
        self,
        config: CodingSessionConfig,
        *,
        resources: SessionResources,
        skill: HarnessSpec,
        runner: SessionRunner,
    ) -> None:
        self._config = config
        self._provider_settings = load_provider_settings(config.paths)
        self._provider_registry = (
            config.extension_runtime.provider_registry if config.extension_runtime else None
        )
        self._resource_paths = config.resource_paths or RioResourcePaths(
            paths=config.paths, root=config.paths.home
        )
        self._resources = resources
        self._skill = skill
        self._runner = runner
        self._session_title = config.session_title
        self._thinking_level = config.thinking_level
        self._staged_entries: list[SessionEntry] = []
        self._has_run = False

    # -- construction --------------------------------------------------------

    @classmethod
    async def load(cls, config: CodingSessionConfig) -> CodingSession:
        """Discover resources, build the skill specification, and resume the notebook."""
        cwd = config.cwd.resolve()
        resource_paths = resource_paths_with_project_trust(
            resource_paths_with_cwd(config.resource_paths, cwd),
            trusted=config.project_resources_trusted,
        )
        resources = _discover(resource_paths, config)
        runtime = config.extension_runtime or ExtensionRuntime(paths=config.paths)
        if config.load_extensions:
            runtime.load(
                resource_paths,
                extra_paths=config.extension_paths,
                include_project_dir=config.project_resources_trusted,
                include_user_dir=config.extension_runtime is None,
            )
        config.extension_runtime = runtime
        if config.command_registry is None:
            config.command_registry = runtime.build_command_registry()
        resources = _with_shadow_diagnostics(resources, config.command_registry)
        storage = config.storage
        if storage is None:
            storage = JsonlSessionStorage(config.paths.default_session_path(cwd))
        startup = f"{KERNEL_STARTUP}\n{journal_startup(storage)}"
        if config.load_mcp:
            mcp = await anyio.to_thread.run_sync(
                lambda: prepare_mcp(
                    cwd, project_trusted=config.project_resources_trusted, paths=config.paths
                )
            )
            if mcp.section is not None:
                config.extra_sections = (*config.extra_sections, mcp.section)
            if mcp.startup:
                startup = f"{startup}\n{mcp.startup}"
        kernel = KernelExecutor(cwd, startup=startup)
        skill = _build_skill(config, resources, kernel)

        runner = SessionRunner(
            SessionRunnerConfig(
                provider=config.provider,
                model=config.model,
                skill=skill,
                storage=storage,
                max_steps=config.max_steps,
                max_retries=config.max_retries,
                context_window_tokens=config.context_window_tokens,
            )
        )
        session = cls(config, resources=resources, skill=skill, runner=runner)
        await runner.load()
        await session._ensure_session_info()
        runtime.bind(session)
        await runtime.emit_session_start("startup")
        return session

    async def _ensure_session_info(self) -> None:
        entries = await self.session_entries()
        if any(isinstance(entry, SessionInfoEntry) for entry in entries):
            for entry in entries:
                if isinstance(entry, LabelEntry):
                    self._session_title = entry.label
            return
        await self._append(
            SessionInfoEntry(
                cwd=str(self.cwd),
                title=self._session_title,
                skill=self._skill.name,
            )
        )

    # -- configuration -------------------------------------------------------

    @property
    def config(self) -> CodingSessionConfig:
        return self._config

    @property
    def cwd(self) -> Path:
        return self._config.cwd.resolve()

    @property
    def model(self) -> str:
        return self._config.model

    @property
    def provider(self) -> ModelProvider:
        return self._config.provider

    @property
    def provider_name(self) -> str | None:
        return self._config.provider_name

    @property
    def thinking_level(self) -> str | None:
        return self._thinking_level

    @property
    def storage(self) -> SessionStorage | None:
        return self._runner.config.storage

    @property
    def command_registry(self) -> object | None:
        return self._config.command_registry

    @property
    def available_model_choices(self) -> tuple[ModelChoice, ...]:
        """Return provider/model choices from the effective runtime view."""
        choices: list[ModelChoice] = []
        dynamic_ids: set[str] = set()
        for effective in self._provider_registry.effective_providers():
            if isinstance(effective.definition, DynamicProvider):
                dynamic = effective.definition
                dynamic_ids.add(dynamic.id)
                choices.extend(ModelChoice(dynamic.id, model.id) for model in dynamic.models)
        if self._provider_settings is not None:
            choices.extend(
                ModelChoice(provider.name, model)
                for provider in self._provider_settings.providers
                if provider.name not in dynamic_ids
                for model in provider.models
            )
        if not choices and self._provider_settings is None:
            return (ModelChoice(provider_name=self.provider_name or "default", model=self.model),)
        return tuple(choices)

    @property
    def scoped_model_choices(self) -> tuple[ModelChoice, ...]:
        """Return scoped references, including inert trusted built-in references."""
        if self._provider_settings is None:
            return ()
        available = set(self.available_model_choices)
        choices: list[ModelChoice] = []
        for item in self._provider_settings.scoped_models:
            choice = ModelChoice(provider_name=item.provider, model=item.model)
            if choice in available or self._stable_dynamic_scoped_provider(item.provider):
                choices.append(choice)
        return tuple(choices)

    @property
    def unavailable_scoped_model_choices(self) -> tuple[ModelChoice, ...]:
        """Return persisted references that have no current provider snapshot row."""
        available = set(self.available_model_choices)
        return tuple(choice for choice in self.scoped_model_choices if choice not in available)

    def _stable_dynamic_scoped_provider(self, provider_name: str) -> bool:
        return any(
            layer.token.source_id.startswith("built-in:")
            and layer.provider.stable_scoped_references
            for layer in self._provider_registry.layers(provider_name)
        )

    def _effective_stable_dynamic_scoped_provider(self, provider_name: str) -> bool:
        effective = self._provider_registry.effective(provider_name)
        return bool(
            effective is not None
            and effective.source_id.startswith("built-in:")
            and isinstance(effective.definition, DynamicProvider)
            and effective.definition.stable_scoped_references
        )

    def is_scoped_model(self, choice: ModelChoice) -> bool:
        """Return whether a provider/model pair is in the scoped model list."""
        return choice in self.scoped_model_choices

    def toggle_scoped_model(self, choice: ModelChoice) -> tuple[ModelChoice, ...]:
        """Add or remove a model from the persisted scoped model list."""
        if self._provider_settings is None:
            raise ProviderConfigError("Provider settings are not available for this session")
        available = set(self.available_model_choices)
        existing = choice in self.scoped_model_choices
        effective = self._provider_registry.effective(choice.provider_name)
        if effective is not None and isinstance(effective.definition, DynamicProvider):
            if not (
                self._effective_stable_dynamic_scoped_provider(choice.provider_name)
                or (existing and self._stable_dynamic_scoped_provider(choice.provider_name))
            ):
                raise ProviderConfigError(
                    "Only effective trusted built-in dynamic providers support scoped references"
                )
            if choice not in available and not existing:
                raise ProviderConfigError(
                    f"Model is unavailable: {choice.provider_name}:{choice.model}"
                )
            toggle = toggle_saved_stable_scoped_model
        else:
            if choice not in available:
                raise ProviderConfigError(
                    f"Model is not available: {choice.provider_name}:{choice.model}"
                )
            toggle = toggle_saved_scoped_model

        self._provider_settings = toggle(
            provider_name=choice.provider_name,
            model=choice.model,
            paths=self._resource_paths.paths,
            fallback_settings=self._provider_settings,
        )
        return self.scoped_model_choices

    # -- resources -----------------------------------------------------------

    @property
    def skill(self) -> HarnessSpec:
        """The skill specification: fixed for the whole run."""
        return self._skill

    @property
    def kernel(self) -> KernelExecutor:
        """The live kernel the notebook's cells run in. It lives as long as the notebook."""
        assert isinstance(self._skill.executor, KernelExecutor)
        return self._skill.executor

    @property
    def system_prompt(self) -> str:
        """The instructions half of every step's prompt."""
        return self._skill.instructions

    @property
    def skills(self) -> tuple[Skill, ...]:
        return self._resources.skills

    @property
    def context_files(self) -> tuple[ProjectContextFile, ...]:
        return self._resources.context_files

    @property
    def prompt_templates(self) -> tuple:
        return self._resources.prompt_templates

    @property
    def system_prompt_files(self) -> tuple[Path, ...]:
        return self._resources.system_prompt_files

    @property
    def resource_diagnostics(self) -> tuple[ResourceDiagnostic, ...]:
        return self._resources.diagnostics + self.extensions.diagnostics

    @property
    def extensions(self) -> ExtensionRuntime:
        assert self._config.extension_runtime is not None
        return self._config.extension_runtime

    # -- notebook ------------------------------------------------------------

    @property
    def notebook(self) -> Notebook:
        """The notebook the model sees next. This is the session's entire memory of the run."""
        return self._runner.notebook

    @property
    def answer(self) -> str | None:
        """What the last run answered, if it has answered."""
        return self._runner.answer

    # -- footprint -----------------------------------------------------------

    @property
    def step_footprint(self) -> StepFootprint:
        """The token cost of the next step's prompt."""
        return estimate_step_footprint(instructions=self.system_prompt, notebook=self.notebook)

    @property
    def context_usage(self) -> ContextUsage:
        return ContextUsage(
            footprint=self.step_footprint,
            context_window_tokens=self._config.context_window_tokens,
        )

    @property
    def context_window_tokens(self) -> int:
        return self._config.context_window_tokens

    # -- run state -----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._runner.is_running

    def cancel(self) -> None:
        self._runner.cancel()

    # -- session identity ----------------------------------------------------

    @property
    def session_title(self) -> str | None:
        return self._session_title

    @property
    def session_name(self) -> str:
        return self._session_title or self.cwd.name

    async def set_session_name(self, name: str) -> SessionInfoChangedEvent:
        self._session_title = name
        await self._append(LabelEntry(label=name))
        return SessionInfoChangedEvent(name=name)

    # -- running -------------------------------------------------------------

    async def run(self, text: str) -> AsyncIterator[CodingSessionEvent]:
        """Run one task, appending its text to the notebook as a user cell."""
        if self._has_run:
            raise RuntimeError("CodingSession supports one run only")
        self._has_run = True
        outcome = await self.extensions.run_input_hooks(text)
        if outcome.handled:
            return
        async for event in self._runner.run(self.expand_prompt_text(outcome.text)):
            await self.extensions.emit_event(event)
            yield event

    def prompt(self, text: str) -> AsyncIterator[CodingSessionEvent]:
        """Compatibility alias for the single permitted run."""
        return self.run(text)

    def expand_prompt_text(self, text: str) -> str:
        """Expand a `/skill:<name>` or bare `/<name>` invocation into instructions.

        `/skill:<name>` is explicit and always wins. A bare `/<name>` resolves in
        one fixed order -- built-in slash command, then prompt template, then
        skill -- so every frontend agrees on what a name means. Interactive
        frontends execute built-in commands before a prompt ever reaches here;
        the check below is what makes `rio -p` and RPC resolve names the same
        way. A name that matches nothing is returned untouched.
        """
        expanded = expand_skill_command(text, self._resources.skills)
        if expanded is not None:
            return expanded
        if _names_builtin_command(self._config.command_registry, text):
            return text
        template = expand_prompt_template_command(text, self._resources.prompt_templates)
        if template is not None:
            return template
        skill = expand_skill_name_command(text, self._resources.skills)
        return skill if skill is not None else text

    # -- journal -------------------------------------------------------------

    async def session_entries(self) -> list[SessionEntry]:
        storage = self.storage
        return await storage.read_all() if storage is not None else []

    async def checkpoints(self) -> list[SessionEntry]:
        """Journal entries that can be branched from."""
        return checkpoints(await self.session_entries())

    async def restore(self, entry_id: str, *, reason: str | None = None) -> StateRestoredEvent:
        """Branch: adopt an earlier notebook as the live one, with a fresh kernel."""
        event = await self._runner.restore(entry_id, reason=reason)
        await self.kernel.shutdown()
        return event

    async def append_custom_entry(
        self, entry: SessionEntry | str, data: dict[str, JSONValue] | None = None
    ) -> SessionEntry:
        if isinstance(entry, str):
            entry = CustomEntry(namespace=entry, data=data or {})
        await self._append(entry)
        return entry

    async def _append(self, entry: SessionEntry) -> None:
        if self._config.defer_authoritative_writes:
            self._staged_entries.append(entry)
            return
        await self._runner.append_entries([entry])

    async def _commit_prepared_entries(self) -> None:
        """Flush staged writes as one batch and start writing through.

        Called when a prepared session is adopted. The batch is atomic, so a
        journal never shows half of a session's startup metadata.
        """
        await self._runner.append_entries(self._staged_entries)
        self._staged_entries = []
        self._config.defer_authoritative_writes = False

    # -- reconfiguration -----------------------------------------------------

    async def set_model(self, model: str, *, provider_name: str | None = None) -> None:
        """Point the session at a different model.

        Nothing has to be converted: the notebook is provider-neutral JSON.
        """
        self._config.model = model
        if provider_name is not None:
            self._config.provider_name = provider_name
        self._runner.rebind(model=model)
        await self._append(ModelChangeEntry(model=model, provider=self.provider_name))

    async def set_provider(
        self, provider: ModelProvider, *, name: str | None = None, model: str | None = None
    ) -> None:
        self._config.provider = provider
        if name is not None:
            self._config.provider_name = name
        if model is not None:
            self._config.model = model
        self._runner.rebind(provider=provider, model=model)
        await self._append(ModelChangeEntry(model=self._config.model, provider=self.provider_name))

    async def set_thinking_level(self, level: str | None) -> ThinkingLevelChangedEvent:
        self._thinking_level = level
        await self._append(ThinkingLevelChangeEntry(thinking_level=level))
        return ThinkingLevelChangedEvent(level=level or "off")

    async def reload(self) -> SessionResources:
        """Re-discover resources on disk and rebuild the skill specification.

        A rebuilt specification takes effect on the very next step.
        """
        cwd = self.cwd
        resource_paths = resource_paths_with_project_trust(
            resource_paths_with_cwd(self._config.resource_paths, cwd),
            trusted=self._config.project_resources_trusted,
        )
        runtime = self.extensions
        successor = ExtensionRuntime(paths=self._config.paths)
        if self._config.load_extensions:
            successor.load(
                resource_paths,
                extra_paths=self._config.extension_paths,
                include_project_dir=self._config.project_resources_trusted,
            )
        resources = _discover(resource_paths, self._config)
        candidate_config = replace(self._config, extension_runtime=successor)
        skill = _build_skill(candidate_config, resources, self.kernel)
        await runtime.emit_session_shutdown("reload")
        await runtime.aclose()
        self._config.extension_runtime = successor
        self._provider_registry = successor.provider_registry
        self._config.command_registry = successor.build_command_registry()
        self._resources = _with_shadow_diagnostics(resources, self._config.command_registry)
        self._skill = skill
        successor.bind(self)
        await successor.emit_session_start("reload")
        self._runner.rebind_skill(self._skill)
        return self._resources

    async def new_session(self) -> None:
        """Clear the notebook and start over in the same directory, with a fresh kernel."""
        await self._runner.reset(reason="new session")
        await self.kernel.aclose()

    async def resume(self) -> Notebook:
        """Adopt the notebook recorded in the journal, with a fresh kernel."""
        notebook = await self._runner.load()
        await self.kernel.shutdown()
        return notebook

    async def fork_from(self, notebook: Notebook, *, parent_session_id: str | None) -> None:
        """Adopt `notebook` as the live one, journaling the session forked from."""
        await self.append_custom_entry("fork", {"parent_session_id": parent_session_id})
        await self._runner.reset(notebook, reason=f"forked from session {parent_session_id}")
        await self.kernel.aclose()

    async def aclose(self) -> None:
        self._runner.cancel()
        await self.kernel.aclose()
        await self.extensions.emit_session_shutdown("quit")
        await self.extensions.aclose()


def _names_builtin_command(registry: object | None, text: str) -> bool:
    """Return whether bare `/name` text is claimed by a registered command."""
    stripped = text.strip()
    if not stripped.startswith("/") or stripped.startswith("//"):
        return False
    get_command = getattr(registry, "get", None)
    if get_command is None:
        return False
    name = stripped.split(maxsplit=1)[0].removeprefix("/").strip()
    return bool(name) and get_command(name) is not None


def _with_shadow_diagnostics(
    resources: SessionResources, registry: object | None
) -> SessionResources:
    """Append notes for skills a bare `/<name>` invocation cannot reach.

    Deferred until the command registry exists, since extensions may register
    commands of their own and those shadow a skill name just as built-ins do.
    """
    list_commands = getattr(registry, "list_commands", None)
    command_names: list[str] = []
    for command in list_commands() if list_commands is not None else ():
        command_names.append(command.name)
        command_names.extend(command.aliases)
    diagnostics = shadowed_skill_diagnostics(
        resources.skills,
        command_names=command_names,
        prompt_templates={template.name: template.path for template in resources.prompt_templates},
    )
    if not diagnostics:
        return resources
    return replace(resources, diagnostics=(*resources.diagnostics, *diagnostics))


def _discover(resource_paths: RioResourcePaths, config: CodingSessionConfig) -> SessionResources:
    skills, skill_diagnostics = load_skills_with_diagnostics(resource_paths)
    context_entries, context_diagnostics = discover_project_context_with_diagnostics(resource_paths)
    templates, template_diagnostics = load_prompt_templates_with_diagnostics(resource_paths)
    prompt_resources = discover_system_prompt_resources(
        resource_paths,
        custom_prompt_explicit=config.custom_prompt is not None,
    )
    return SessionResources(
        skills=tuple(skills),
        context_files=tuple(context_entries),
        prompt_templates=tuple(templates),
        system_prompt_files=tuple(
            path
            for path in (
                prompt_resources.custom_prompt_path,
                *prompt_resources.append_prompt_paths,
            )
            if path is not None
        ),
        diagnostics=(
            *skill_diagnostics,
            *context_diagnostics,
            *template_diagnostics,
            *prompt_resources.diagnostics,
        ),
    )


def _build_skill(
    config: CodingSessionConfig, resources: SessionResources, kernel: KernelExecutor
) -> HarnessSpec:
    assert kernel.cwd is not None
    instructions = build_skill_instructions(
        BuildSystemPromptOptions(
            cwd=kernel.cwd,
            skills=resources.skills,
            custom_prompt=config.custom_prompt,
            append_system_prompt=config.append_system_prompt,
            context_files=resources.context_files,
            extra_guidelines=tuple(config.extra_guidelines)
            + (config.extension_runtime.prompt_guidelines if config.extension_runtime else ()),
            extra_sections=tuple(config.extra_sections)
            + (config.extension_runtime.prompt_sections if config.extension_runtime else ()),
        )
    )
    return build_coding_skill(CodingSkillOptions(instructions=instructions, executor=kernel))


def default_session_path(cwd: Path, *, paths: RioPaths | None = None) -> Path:
    """Return the default journal path for a project directory."""
    return (paths or RioPaths()).default_session_path(cwd)


def jsonl_session_storage(path: Path | str) -> JsonlSessionStorage:
    """Return JSONL-backed journal storage."""
    return JsonlSessionStorage(path)


CommandHandler = Callable[[str], object]
