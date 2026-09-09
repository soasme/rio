"""The coding-agent environment: resources, tools, and a SKILL.state run loop.

tau's session had to be a librarian of conversation -- it held the transcript,
estimated how close it was to the context window, summarized it when it got too
long, and translated it whenever the provider changed. None of that exists here.
A rio session holds one JSON object, and the model is handed a fresh prompt built
from it every step.

What is left is genuinely the session's own work: discovering project resources,
assembling the skill specification `P` from them, owning the tools that become
actions, and driving `SessionRunner` over a durable journal.
"""

from __future__ import annotations

import os
import platform
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

from rio_agent import HarnessSpec
from rio_ai.provider import ModelProvider
from rio_ai.tools import AgentTool
from rio_ai.types import JSONObject, JSONValue
from rio_coding.coding_skill import (
    CodingSkillOptions,
    build_coding_skill,
    describe_state,
    initial_coding_state,
    plan_progress,
    touched_files,
)
from rio_coding.context import discover_project_context_with_diagnostics
from rio_coding.events import (
    CodingSessionEvent,
    SessionInfoChangedEvent,
    StateRestoredEvent,
    ThinkingLevelChangedEvent,
)
from rio_coding.extensions import DynamicProvider, ExtensionRuntime
from rio_coding.paths import RioPaths
from rio_coding.prompt_templates import (
    expand_prompt_template_command,
    load_prompt_templates_with_diagnostics,
)
from rio_coding.provider_config import (
    ProviderConfigError,
    load_provider_settings,
    toggle_saved_scoped_model,
    toggle_saved_stable_scoped_model,
)
from rio_coding.resources import (
    ResourceDiagnostic,
    RioResourcePaths,
    discover_system_prompt_resources,
    resource_paths_with_cwd,
    resource_paths_with_project_trust,
)
from rio_coding.session_runner import SessionRunner, SessionRunnerConfig
from rio_coding.session_store import (
    CustomEntry,
    JsonlSessionStorage,
    LabelEntry,
    ModelChangeEntry,
    SessionEntry,
    SessionInfoEntry,
    SessionStorage,
    ThinkingLevelChangeEntry,
    checkpoints,
    entry_state,
)
from rio_coding.skills import (
    Skill,
    expand_skill_command,
    expand_skill_name_command,
    load_skills_with_diagnostics,
    shadowed_skill_diagnostics,
)
from rio_coding.step_footprint import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    StepFootprint,
    context_window_utilization,
    estimate_step_footprint,
    projected_cumulative_tokens,
)
from rio_coding.system_prompt import (
    BuildSystemPromptOptions,
    ProjectContextFile,
    PromptSection,
    build_skill_instructions,
)
from rio_coding.tools import ImageSupportState, create_coding_tools

#: A coding run is bounded so a runaway loop cannot burn tokens indefinitely.
#: The bound is on step *count*, not on prompt size -- prompt size is already
#: constant, which is the point.
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
    tools: Sequence[AgentTool] | None = None
    shell_command_prefix: str | None = None
    image_support: ImageSupportState | None = None
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
    """How much of the model's context window one step occupies.

    Unlike tau's equivalent this is not a running total that creeps toward a
    limit. It is the same number on step 1 and on step 1000, so it reads as a
    property of the session rather than a countdown.
    """

    footprint: StepFootprint
    context_window_tokens: int

    @property
    def utilization(self) -> float:
        return context_window_utilization(self.footprint, self.context_window_tokens)

    @property
    def total_tokens(self) -> int:
        return self.footprint.total_tokens

    def projected_tokens(self, steps: int) -> int:
        """Total prompt tokens after `steps` steps -- linear, not quadratic."""
        return projected_cumulative_tokens(self.footprint, steps)


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
        self._last_observation: str | None = None
        self._staged_entries: list[SessionEntry] = []

    # -- construction --------------------------------------------------------

    @classmethod
    async def load(cls, config: CodingSessionConfig) -> CodingSession:
        """Discover resources, build the skill specification, and resume state."""
        cwd = config.cwd.resolve()
        resource_paths = resource_paths_with_project_trust(
            resource_paths_with_cwd(config.resource_paths, cwd),
            trusted=config.project_resources_trusted,
        )
        resources = _discover(resource_paths, config)
        tools = tuple(
            config.tools
            if config.tools is not None
            else create_coding_tools(
                cwd=cwd,
                shell_command_prefix=config.shell_command_prefix,
                image_support=config.image_support,
            )
        )
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
        skill = _build_skill(config, resources, runtime.compose_tools(tools), cwd)

        storage = config.storage
        if storage is None:
            storage = JsonlSessionStorage(config.paths.default_session_path(cwd))

        runner = SessionRunner(
            SessionRunnerConfig(
                provider=config.provider,
                model=config.model,
                skill=skill,
                storage=storage,
                max_steps=config.max_steps,
                max_retries=config.max_retries,
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
        """The skill specification `P`: fixed for the whole run."""
        return self._skill

    @property
    def system_prompt(self) -> str:
        """The instructions half of every step's prompt."""
        return self._skill.instructions

    @property
    def tools(self) -> tuple[AgentTool, ...]:
        """The declared actions. Exactly one of these runs per step."""
        return self._skill.actions

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

    # -- execution state -----------------------------------------------------

    @property
    def state(self) -> dict[str, JSONValue]:
        """The execution state. This is the session's entire memory of the run."""
        return self._runner.state

    @property
    def plan_progress(self) -> tuple[int, int]:
        return plan_progress(self.state)

    @property
    def touched_files(self) -> list[str]:
        return touched_files(self.state)

    @property
    def answer(self) -> str | None:
        """What the last run answered, if it has answered."""
        return self._runner.answer

    @property
    def state_summary(self) -> str:
        return describe_state(self.state)

    # -- footprint -----------------------------------------------------------

    @property
    def step_footprint(self) -> StepFootprint:
        """The token cost of the next step's prompt."""
        return estimate_step_footprint(
            instructions=self.system_prompt,
            state=self.state,
            observation=self._last_observation or "",
            tools=self.tools,
        )

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

    @property
    def queued_message_count(self) -> int:
        return self._runner.queued_message_count

    @property
    def queued_steering_messages(self) -> tuple[str, ...]:
        return self._runner.queued_steering_messages

    @property
    def queued_follow_up_messages(self) -> tuple[str, ...]:
        return self._runner.queued_follow_up_messages

    def queue_steering_message(self, text: str, *, custom_type=None, details=None):
        return self._runner.queue_steering_message(text)

    def queue_follow_up_message(self, text: str, *, custom_type=None, details=None):
        return self._runner.queue_follow_up_message(text)

    def clear_queued_messages(self):
        return self._runner.clear_queued_messages()

    def pop_latest_steering_message(self) -> str | None:
        return self._runner.pop_latest_steering_message()

    def pop_latest_follow_up_message(self) -> str | None:
        return self._runner.pop_latest_follow_up_message()

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

    async def prompt(self, text: str) -> AsyncIterator[CodingSessionEvent]:
        """Run one turn from a user message.

        The message is what the first step observes, labelled as a user
        message rather than dressed up as an action result -- no action has run
        yet. It is not appended to anything: on the next step the model sees
        only what the state retained of it, which is why the instructions tell
        the model to record the goal. The state itself carries over untouched,
        so a second message continues the session instead of restarting it.
        """
        outcome = await self.extensions.run_input_hooks(text)
        if outcome.handled:
            return
        observation = self.expand_prompt_text(outcome.text)
        self._last_observation = observation
        async for event in self._runner.run(observation):
            await self.extensions.emit_event(event)
            yield event

    async def continue_(self) -> AsyncIterator[CodingSessionEvent]:
        """Run the next queued follow-up message, if there is one."""
        pending = self._runner.pop_latest_follow_up_message()
        if pending is None:
            return
        async for event in self.prompt(pending):
            yield event

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
        """Branch: adopt an earlier execution state as the live one."""
        event = await self._runner.restore(entry_id, reason=reason)
        self._last_observation = None
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

        Nothing has to be converted. The next prompt is rebuilt from the
        execution state, which is provider-neutral JSON, so a mid-run model
        change carries the whole run across without translating a transcript.
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

        Cheap and total: because `P` is only ever sent as the current step's
        system prompt, a rebuilt specification takes effect on the very next
        step with no history written against the old one.
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
        builtin_tools = self._config.tools
        if builtin_tools is None:
            builtin_tools = create_coding_tools(
                cwd=cwd,
                shell_command_prefix=self._config.shell_command_prefix,
                image_support=self._config.image_support,
            )
        candidate_config = replace(self._config, extension_runtime=successor)
        skill = _build_skill(
            candidate_config, resources, successor.compose_tools(builtin_tools), cwd
        )
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
        """Clear the execution state and start over in the same directory."""
        await self._runner.reset(
            initial_coding_state(cwd=self.cwd, environment=_environment(self.cwd)),
            reason="new session",
        )
        self._last_observation = None

    async def resume(self) -> dict[str, JSONValue]:
        """Adopt the state recorded in the journal."""
        state = await self._runner.load()
        self._last_observation = None
        return state

    async def fork_from(
        self, state: dict[str, JSONValue], *, parent_session_id: str | None
    ) -> None:
        """Adopt `state` as the live state, journaling the session forked from.

        Used to seed a brand-new session's journal from another session's live
        state -- a state_reset, same as `new_session()`, but into an
        empty journal rather than this one, with a lineage note beside it.
        """
        await self.append_custom_entry("fork", {"parent_session_id": parent_session_id})
        await self._runner.reset(dict(state), reason=f"forked from session {parent_session_id}")
        self._last_observation = None

    async def aclose(self) -> None:
        self._runner.cancel()
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


def _environment(cwd: Path) -> JSONObject:
    """The ambient facts a coding run needs, captured once into the state."""
    return {
        "cwd": str(cwd),
        "os": platform.system(),
        "shell": os.environ.get("SHELL", ""),
        "date": date.today().isoformat(),
    }


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
    config: CodingSessionConfig,
    resources: SessionResources,
    tools: Sequence[AgentTool],
    cwd: Path,
) -> HarnessSpec:
    instructions = build_skill_instructions(
        BuildSystemPromptOptions(
            cwd=cwd,
            tools=tuple(tools),
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
    return build_coding_skill(
        CodingSkillOptions(
            instructions=instructions,
            cwd=cwd,
            tools=tuple(tools),
            environment=_environment(cwd),
            context_window_tokens=config.context_window_tokens,
        )
    )


def default_session_path(cwd: Path, *, paths: RioPaths | None = None) -> Path:
    """Return the default journal path for a project directory."""
    return (paths or RioPaths()).default_session_path(cwd)


def jsonl_session_storage(path: Path | str) -> JsonlSessionStorage:
    """Return JSONL-backed journal storage."""
    return JsonlSessionStorage(path)


def state_of(entry: SessionEntry) -> dict[str, JSONValue] | None:
    """Return the execution state an entry captured, if it captured one."""
    return entry_state(entry)


CommandHandler = Callable[[str], object]
