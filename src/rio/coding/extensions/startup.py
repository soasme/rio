"""Resolve extension providers from loaded snapshots without network discovery."""

from dataclasses import dataclass
from pathlib import Path

from rio.coding.credentials import FileCredentialStore, credentials_path
from rio.coding.extensions.providers import DynamicProvider
from rio.coding.extensions.runtime import ExtensionRuntime
from rio.coding.paths import RioPaths
from rio.coding.project_trust import ProjectTrustCoordinator, ProjectTrustStore, TrustOverride
from rio.coding.provider_config import ProviderConfigError, load_provider_settings
from rio.coding.provider_runtime import ClosableModelProvider, create_dynamic_model_provider
from rio.coding.resources import RioResourcePaths
from rio.coding.shell_config import load_shell_settings


@dataclass(frozen=True, slots=True)
class DynamicStartup:
    runtime: ExtensionRuntime
    provider: ClosableModelProvider
    provider_name: str
    model: str


async def resolve_dynamic_startup(
    *,
    provider_name: str,
    model: str | None = None,
    cwd: Path,
    paths: RioPaths | None = None,
    extension_paths: tuple[Path, ...] = (),
    trust_override: TrustOverride | None = None,
) -> DynamicStartup | None:
    """Select a cached provider from user, explicit, and trusted project extensions."""
    paths = paths or RioPaths()
    credentials = FileCredentialStore(credentials_path(paths))
    runtime = ExtensionRuntime(
        paths=paths,
        credentials=credentials,
        durable_providers=load_provider_settings(paths).providers,
    )
    try:
        _, trust = await ProjectTrustCoordinator(ProjectTrustStore(paths)).resolve(
            cwd,
            override=trust_override,
            default=load_shell_settings().default_project_trust,
            persist=False,
        )
        runtime.load(
            RioResourcePaths(root=paths.home, agents_root=paths.agents_home, cwd=cwd, paths=paths),
            include_project_dir=trust.trusted,
            extra_paths=extension_paths,
        )
        effective = runtime.provider_registry.effective(provider_name)
        if effective is None or not isinstance(effective.definition, DynamicProvider):
            await runtime.aclose()
            return None
        definition = effective.definition
        selected = model or definition.default_model
        if selected is None:
            raise ProviderConfigError(f"No default model for provider {provider_name}")
        provider = await create_dynamic_model_provider(
            definition,
            model=selected,
            credential_store=credentials,
        )
        return DynamicStartup(runtime, provider, provider_name, selected)
    except BaseException:
        await runtime.aclose()
        raise
