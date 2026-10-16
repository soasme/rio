"""Tests for rio.coding model and provider catalogs."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import get_args

import pytest

from rio.coding import models_dev
from rio.coding.catalog_loader import (
    CatalogError,
    builtin_catalog,
    builtin_catalog_resource_text,
    builtin_source_catalog,
    effective_catalog,
    user_catalog_path,
)
from rio.coding.models_dev import (
    bundled_models_dev_catalog_overlay,
    models_dev_catalog_document,
    models_dev_catalog_overlay,
    thinking_level_map_from_reasoning_options,
)
from rio.coding.paths import RioPaths
from rio.coding.provider_catalog import (
    ModelCatalogMetadata,
    ModelInput,
    ProviderApi,
)
from rio.coding.provider_config import (
    ProviderSettings,
    openai_compatible_config_from_provider,
    provider_config_from_entry,
    resolve_provider_selection,
)
from rio.coding.thinking import THINKING_LEVELS

# A small fixture mirroring models.dev's response shape, inlined so this
# slice's tests are self-contained in a single file.
FIXTURE: dict[str, object] = {
    "huggingface": {
        "id": "huggingface",
        "models": {
            "moonshotai/Kimi-K3": {
                "id": "moonshotai/Kimi-K3",
                "name": "Kimi K3",
                "reasoning": True,
                "reasoning_options": [
                    {
                        "type": "effort",
                        "values": ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
                    }
                ],
                "tool_call": True,
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "limit": {"context": 1000000, "output": 131072},
                "cost": {"input": 3, "output": 15},
            },
            "zai-org/GLM-5.2": {
                "id": "zai-org/GLM-5.2",
                "name": "GLM-5.2",
                "reasoning": True,
                "reasoning_options": [
                    {
                        "type": "effort",
                        "values": ["none", "high", "max", "default", None],
                    }
                ],
                "tool_call": True,
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": 262144, "output": 131072},
                "cost": {"input": 1.4, "output": 4.4},
            },
            "example/new-tool-model": {
                "id": "example/new-tool-model",
                "reasoning": False,
                "reasoning_options": [],
                "tool_call": True,
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": 65536, "output": 8192},
                "cost": {"input": 0.1, "output": 0.2},
            },
        },
    },
    "togetherai": {
        "id": "togetherai",
        "models": {
            "zai-org/GLM-5.1": {
                "id": "zai-org/GLM-5.1",
                "name": "GLM-5.1",
                "reasoning": True,
                "reasoning_options": [
                    {
                        "type": "effort",
                        "values": ["high", "max"],
                    }
                ],
                "tool_call": True,
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": 202752, "output": 131072},
                "cost": {"input": 1, "output": 3.2},
            }
        },
    },
}


def _fixture() -> dict[str, object]:
    return json.loads(json.dumps(FIXTURE))


VALID_PROVIDER = """
[[providers]]
name = "nebius"
display_name = "Nebius AI Studio"
base_url = "https://api.studio.nebius.ai/v1"
api_key_env = "NEBIUS_API_KEY"
credential_name = "nebius"
models = ["deepseek-ai/DeepSeek-V4-Pro", "Qwen/Qwen3-Coder-480B-A35B-Instruct"]
default_model = "deepseek-ai/DeepSeek-V4-Pro"
docs_url = "https://studio.nebius.ai/docs"
thinking_levels = ["off", "low", "medium", "high"]
thinking_models = ["deepseek-ai/DeepSeek-V4-Pro"]
thinking_default = "medium"
thinking_parameter = "reasoning_effort"

[providers.context_windows]
"deepseek-ai/DeepSeek-V4-Pro" = 163840
"""


def _write_user_catalog(rio_home: Path, body: str) -> RioPaths:
    paths = RioPaths(home=rio_home)
    rio_home.mkdir(parents=True, exist_ok=True)
    user_catalog_path(paths).write_text(f"schema_version = 1\n{body}", encoding="utf-8")
    return paths


# ---------------------------------------------------------------------------
# models_dev
# ---------------------------------------------------------------------------


def test_effort_options_use_pi_level_map_semantics() -> None:
    source = _fixture()
    options = source["huggingface"]["models"]["zai-org/GLM-5.2"]["reasoning_options"]

    assert thinking_level_map_from_reasoning_options(options) == {
        "off": "none",
        "minimal": None,
        "low": None,
        "medium": None,
        "high": "high",
        "xhigh": None,
        "max": "max",
    }


def test_full_effort_options_preserve_every_pi_level() -> None:
    source = _fixture()
    options = source["huggingface"]["models"]["moonshotai/Kimi-K3"]["reasoning_options"]

    assert thinking_level_map_from_reasoning_options(options) == {
        "off": "none",
        "minimal": "minimal",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "xhigh",
        "max": "max",
    }


def test_empty_or_toggle_only_options_do_not_override_manual_behavior() -> None:
    assert thinking_level_map_from_reasoning_options([]) is None
    assert thinking_level_map_from_reasoning_options([{"type": "toggle"}]) is None


def test_generation_adds_new_tool_models_and_provider_aliases() -> None:
    source = _fixture()

    document = models_dev_catalog_document(source, builtin_source_catalog())

    assert set(document["providers"]) == {"huggingface", "together"}
    huggingface = document["providers"]["huggingface"]
    assert "example/new-tool-model" in huggingface["models"]
    assert huggingface["model_metadata"]["example/new-tool-model"] == {
        "reasoning": False,
        "input": ["text"],
        "max_tokens": 8192,
    }
    together = document["providers"]["together"]
    assert together["model_metadata"]["zai-org/GLM-5.1"]["thinking_level_map"] == {
        "high": "high",
        "max": "max",
    }


def test_invalid_generated_catalog_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported schema"):
        models_dev_catalog_overlay({"schema_version": 2, "providers": {}})


def test_missing_bundled_catalog_falls_back_silently(
    monkeypatch: pytest.MonkeyPatch, bundled_models_dev_catalog: None
) -> None:
    def missing_files(_package: str) -> None:
        raise OSError("offline package fixture")

    monkeypatch.setattr(models_dev, "files", missing_files)

    assert bundled_models_dev_catalog_overlay() is None


# ---------------------------------------------------------------------------
# provider_catalog / catalog_loader
# ---------------------------------------------------------------------------


def builtin_provider_entry(name: str):
    return next((entry for entry in builtin_catalog() if entry.name == name), None)


def test_bundled_models_dev_catalog_matches_schema(bundled_models_dev_catalog: None) -> None:
    text = files("rio.coding").joinpath("data/models-dev-catalog.json").read_text(encoding="utf-8")
    document = json.loads(text)
    assert document["schema_version"] == 1
    assert isinstance(document["generated_at"], int)

    # Raises on any schema violation; the loader would otherwise silently fall
    # back to catalog.toml and hide a broken snapshot.
    overlay = models_dev_catalog_overlay(document)
    assert bundled_models_dev_catalog_overlay() == overlay

    source_names = {entry.name for entry in builtin_source_catalog()}
    builtin = {entry.name: entry for entry in builtin_catalog()}
    assert [entry.name for entry in builtin_catalog()] == [
        entry.name for entry in builtin_source_catalog()
    ]
    for provider in overlay["providers"]:
        assert provider["name"] in source_names
        entry = builtin[provider["name"]]
        # The snapshot was merged rather than discarded by the loader's fallback.
        assert set(provider["models"]) - set(entry.removed_models) <= set(entry.models)


def test_builtin_catalog_with_bundled_models_matches_schema(
    bundled_models_dev_catalog: None,
) -> None:
    for entry in builtin_catalog():
        assert entry.models, entry.name
        assert all(isinstance(model, str) and model for model in entry.models), entry.name
        assert entry.default_model in entry.models, entry.name
        assert set(entry.model_metadata) <= set(entry.models), entry.name
        for model, metadata in entry.model_metadata.items():
            assert_model_metadata_schema(f"{entry.name}/{model}", metadata)


def test_builtin_catalog_matches_expected_providers() -> None:
    names = [entry.name for entry in builtin_catalog()]
    assert names == [
        "openai",
        "openai-codex",
        "anthropic",
        "google",
        "deepseek",
        "xai",
        "groq",
        "cerebras",
        "nvidia",
        "openrouter",
        "zai",
        "mistral",
        "minimax",
        "minimax-cn",
        "moonshotai",
        "kimi-code",
        "moonshotai-cn",
        "huggingface",
        "fireworks",
        "together",
        "vercel-ai-gateway",
        "xiaomi",
        "xiaomi-token-plan-cn",
        "xiaomi-token-plan-ams",
        "xiaomi-token-plan-sgp",
        "opencode-go",
        "opencode",
        "github-copilot",
    ]


def test_builtin_catalog_golden_anthropic_entry() -> None:
    entry = builtin_provider_entry("anthropic")
    assert entry is not None
    assert entry.api == "anthropic-messages"
    assert entry.base_url == "https://api.anthropic.com"
    assert entry.api_key_env == "ANTHROPIC_API_KEY"
    assert entry.credential_name == "anthropic"
    assert {
        "claude-fable-5",
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
        "claude-opus-4-1",
        "claude-opus-4-1-20250805",
        "claude-opus-4-5",
        "claude-opus-4-5-20251101",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-5",
        "claude-sonnet-4-5",
        "claude-sonnet-4-5-20250929",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
    } <= set(entry.models)
    assert entry.default_model == "claude-sonnet-4-6"
    opus_5 = entry.model_metadata["claude-opus-5"]
    assert opus_5.max_tokens == 128_000
    assert opus_5.input == ("text", "image")
    assert opus_5.compat == {"forceAdaptiveThinking": True}
    assert entry.thinking_levels == ("off", "minimal", "low", "medium", "high", "xhigh")
    assert entry.thinking_models == ()
    assert entry.thinking_default == "medium"
    assert entry.thinking_parameter == "anthropic.thinking"


def test_builtin_catalog_separates_openai_api_and_codex_context_limits() -> None:
    openai = builtin_provider_entry("openai")
    codex = builtin_provider_entry("openai-codex")

    assert openai is not None
    assert codex is not None
    assert "gpt-5.6" in openai.models
    assert "gpt-5.6" not in codex.models
    assert "gpt-5.6" not in codex.model_metadata
    assert codex.removed_models == ("gpt-5.6",)


@pytest.mark.parametrize(
    ("provider_name", "vision_models"),
    [
        (
            "openai-codex",
            {
                "gpt-5.6-sol",
                "gpt-5.6-terra",
                "gpt-5.6-luna",
                "gpt-5.5",
                "gpt-5.4",
                "gpt-5.4-mini",
                "gpt-5.3-codex",
                "gpt-5.2",
            },
        ),
        (
            "opencode-go",
            {
                "kimi-k2.6",
                "kimi-k2.7-code",
                "mimo-v2.5",
                "minimax-m3",
                "qwen3.6-plus",
                "qwen3.7-plus",
            },
        ),
        (
            "opencode",
            {
                "gpt-5.4",
                "gpt-5.4-mini",
                "gpt-5.5",
                "gpt-5.5-pro",
                "gpt-5.6-luna",
                "gpt-5.6-sol",
                "gpt-5.6-terra",
                "grok-4.5",
                "grok-build-0.1",
                "kimi-k2.5",
                "kimi-k2.6",
                "kimi-k2.7-code",
                "minimax-m3",
                "qwen3.5-plus",
                "qwen3.6-plus",
            },
        ),
        (
            "github-copilot",
            {
                "claude-fable-5",
                "claude-haiku-4.5",
                "claude-opus-4.5",
                "claude-opus-4.6",
                "claude-opus-4.7",
                "claude-opus-4.8",
                "claude-sonnet-4",
                "claude-sonnet-4.5",
                "claude-sonnet-4.6",
                "claude-sonnet-5",
                "gemini-2.5-pro",
                "gemini-3-flash-preview",
                "gemini-3.1-pro-preview",
                "gemini-3.5-flash",
                "gpt-4.1",
                "gpt-5-mini",
                "gpt-5.2",
                "gpt-5.2-codex",
                "gpt-5.3-codex",
                "gpt-5.4",
                "gpt-5.4-mini",
                "gpt-5.4-nano",
                "gpt-5.5",
                "gpt-5.6-luna",
                "gpt-5.6-sol",
                "gpt-5.6-terra",
                "kimi-k2.7-code",
            },
        ),
    ],
)
def test_sparse_provider_catalogs_declare_model_input_modalities(
    provider_name: str, vision_models: set[str]
) -> None:
    provider = builtin_provider_entry(provider_name)

    assert provider is not None
    assert set(provider.model_metadata) == set(provider.models)
    actual_vision_models = {
        model for model, metadata in provider.model_metadata.items() if "image" in metadata.input
    }
    assert vision_models <= actual_vision_models


def test_builtin_catalog_copilot_claude_max_tokens() -> None:
    entry = builtin_provider_entry("github-copilot")
    assert entry is not None

    expected = {
        "claude-haiku-4.5": 64_000,
        "claude-opus-4.5": 32_000,
        "claude-opus-4.6": 32_000,
        "claude-opus-4.7": 32_000,
        "claude-opus-4.8": 64_000,
        "claude-sonnet-4": 16_000,
        "claude-sonnet-4.5": 32_000,
        "claude-sonnet-4.6": 32_000,
        "claude-sonnet-5": 128_000,
    }

    for model, max_tokens in expected.items():
        metadata = entry.model_metadata[model]
        assert metadata.api == "anthropic-messages"
        assert metadata.max_tokens == max_tokens


def assert_model_metadata_schema(model: str, metadata: ModelCatalogMetadata) -> None:
    """Check one catalog metadata record's shape without pinning upstream values."""
    assert isinstance(metadata, ModelCatalogMetadata), model
    assert metadata.api is None or metadata.api in get_args(ProviderApi), model
    assert metadata.base_url is None or isinstance(metadata.base_url, str), model
    assert metadata.reasoning is None or isinstance(metadata.reasoning, bool), model
    assert isinstance(metadata.input, tuple), model
    assert all(value in get_args(ModelInput) for value in metadata.input), model
    assert metadata.max_tokens is None or isinstance(metadata.max_tokens, int), model
    assert isinstance(metadata.headers, dict), model
    assert all(isinstance(value, str) for value in metadata.headers.values()), model
    assert isinstance(metadata.compat, dict), model
    assert isinstance(metadata.thinking_level_map, dict), model
    for level, mapped in metadata.thinking_level_map.items():
        assert level in THINKING_LEVELS, model
        assert mapped is None or isinstance(mapped, str), model


def test_builtin_catalog_golden_nvidia_entry() -> None:
    entry = builtin_provider_entry("nvidia")
    assert entry is not None
    assert entry.base_url == "https://integrate.api.nvidia.com/v1"
    assert entry.api_key_env == "NVIDIA_API_KEY"
    assert entry.credential_name == "nvidia"
    assert entry.api == "openai-completions"
    assert entry.thinking_levels == ("off", "minimal", "low", "medium", "high")
    assert entry.thinking_models == ()
    assert entry.thinking_default == "medium"
    assert entry.thinking_parameter == "reasoning_effort"

    # Upstream models.dev churns model ids, so assert the catalog's shape rather
    # than any particular model surviving a refresh.
    assert entry.models
    assert all(isinstance(model, str) for model in entry.models)
    assert entry.default_model in entry.models
    assert set(entry.model_metadata) == set(entry.models)
    for model, metadata in entry.model_metadata.items():
        assert_model_metadata_schema(model, metadata)


def test_builtin_catalog_huggingface_model_expansion() -> None:
    entry = builtin_provider_entry("huggingface")
    assert entry is not None
    assert entry.models
    assert all(isinstance(model, str) for model in entry.models)
    assert entry.default_model in entry.models
    assert set(entry.model_metadata) == set(entry.models)
    for model, metadata in entry.model_metadata.items():
        assert_model_metadata_schema(model, metadata)


def test_builtin_catalog_golden_kimi_entries() -> None:
    moonshot = builtin_provider_entry("moonshotai")
    assert moonshot is not None
    assert moonshot.default_model == "kimi-k2.7-code"
    assert "kimi-k2.7-code" in moonshot.models

    moonshot_cn = builtin_provider_entry("moonshotai-cn")
    assert moonshot_cn is not None
    assert moonshot_cn.default_model == "kimi-k2.7-code"
    assert "kimi-k2.7-code" in moonshot_cn.models

    k2_7 = moonshot.model_metadata["kimi-k2.7-code"]
    assert k2_7.reasoning is True
    assert k2_7.input == ("text", "image")
    assert k2_7.max_tokens == 32_768
    assert k2_7.thinking_level_map == {
        "off": None,
        "minimal": None,
        "low": None,
        "high": None,
    }

    coding = builtin_provider_entry("kimi-code")
    assert coding is not None
    assert coding.base_url == "https://api.kimi.com/coding/v1"
    assert coding.api_key_env == "KIMI_CODE_API_KEY"
    assert coding.credential_name == "kimi-code"
    assert {"k3", "kimi-for-coding"} <= set(coding.models)
    assert coding.default_model == "kimi-for-coding"
    assert coding.thinking_default == "max"

    k3 = coding.model_metadata["k3"]
    assert k3.reasoning is True
    assert k3.input == ("text", "image")

    latest = coding.model_metadata["kimi-for-coding"]
    assert latest.reasoning is True


def test_builtin_catalog_entries_are_internally_consistent() -> None:
    for entry in builtin_catalog():
        assert entry.default_model in entry.models
        assert set(entry.thinking_models) <= set(entry.models)
        if entry.thinking_default is not None:
            assert entry.thinking_levels is not None
            assert entry.thinking_default in entry.thinking_levels


def test_builtin_catalog_resource_is_packaged() -> None:
    assert "[[providers]]" in builtin_catalog_resource_text()


def test_effective_catalog_without_user_file_is_builtin(tmp_path: Path) -> None:
    paths = RioPaths(home=tmp_path / ".rio")
    assert effective_catalog(paths) == builtin_catalog()


def test_user_catalog_adds_new_provider(tmp_path: Path) -> None:
    paths = _write_user_catalog(tmp_path / ".rio", VALID_PROVIDER)
    catalog = effective_catalog(paths)
    assert [entry.name for entry in catalog[:-1]] == [e.name for e in builtin_catalog()]
    entry = catalog[-1]
    assert entry.name == "nebius"
    assert entry.default_model == "deepseek-ai/DeepSeek-V4-Pro"
    assert entry.thinking_levels == ("off", "low", "medium", "high")


def test_user_catalog_accepts_minimal_provider(tmp_path: Path) -> None:
    paths = _write_user_catalog(
        tmp_path / ".rio",
        '[[providers]]\nname = "local-llama"\nbase_url = "http://localhost:8080/v1"\n',
    )
    entry = effective_catalog(paths)[-1]
    assert entry.api is None
    assert entry.api_key_env == ""
    assert entry.models == ("local-llama",)
    assert entry.default_model == "local-llama"
    provider = provider_config_from_entry(entry)
    settings = ProviderSettings(default_provider="local-llama", providers=(provider,))
    assert resolve_provider_selection(settings).model == "local-llama"
    assert openai_compatible_config_from_provider(provider).api_key == ""


def test_user_catalog_defaults_to_first_model(tmp_path: Path) -> None:
    paths = _write_user_catalog(
        tmp_path / ".rio",
        '[[providers]]\nname = "local-llama"\nbase_url = "http://localhost:8080/v1"\n'
        'models = ["qwen", "llama"]\n',
    )
    assert effective_catalog(paths)[-1].default_model == "qwen"


def test_user_catalog_overlays_builtin_provider(tmp_path: Path) -> None:
    paths = _write_user_catalog(
        tmp_path / ".rio",
        """
[[providers]]
name = "anthropic"
models = ["claude-next-1"]
default_model = "claude-next-1"

[providers.context_windows]
"claude-next-1" = 500000
""",
    )
    entry = next(e for e in effective_catalog(paths) if e.name == "anthropic")
    assert entry.models[0] == "claude-next-1"
    assert "claude-sonnet-4-6" in entry.models
    assert entry.default_model == "claude-next-1"
    # Untouched fields come from the builtin entry.
    assert entry.base_url == "https://api.anthropic.com"
    assert entry.thinking_parameter == "anthropic.thinking"


def test_builtin_tombstone_removes_model_from_user_catalog_overlay(tmp_path: Path) -> None:
    paths = _write_user_catalog(
        tmp_path / ".rio",
        """
[[providers]]
name = "openai-codex"
models = ["gpt-5.6"]
default_model = "gpt-5.6"
thinking_models = ["gpt-5.6"]

[providers.context_windows]
"gpt-5.6" = 272000

[providers.model_metadata."gpt-5.6"]
name = "GPT-5.6"
""",
    )

    entry = next(e for e in effective_catalog(paths) if e.name == "openai-codex")

    assert entry.default_model == "gpt-5.5"
    assert "gpt-5.6" not in entry.models
    assert "gpt-5.6" not in entry.thinking_models
    assert "gpt-5.6" not in entry.model_metadata
    assert entry.removed_models == ("gpt-5.6",)


def test_user_catalog_thinking_fields_replace_as_group(tmp_path: Path) -> None:
    paths = _write_user_catalog(
        tmp_path / ".rio",
        """
[[providers]]
name = "anthropic"
thinking_levels = ["off", "high"]
thinking_default = "high"
""",
    )
    entry = next(e for e in effective_catalog(paths) if e.name == "anthropic")
    assert entry.thinking_levels == ("off", "high")
    assert entry.thinking_default == "high"
    assert entry.thinking_models == ()
    assert entry.thinking_parameter is None


def test_user_catalog_rejects_unknown_keys(tmp_path: Path) -> None:
    paths = _write_user_catalog(tmp_path / ".rio", VALID_PROVIDER.replace("docs_url", "docs_ur1"))
    with pytest.raises(CatalogError, match=r"providers\.nebius"):
        effective_catalog(paths)


def test_user_catalog_rejects_default_model_not_in_models(tmp_path: Path) -> None:
    paths = _write_user_catalog(
        tmp_path / ".rio",
        VALID_PROVIDER.replace(
            'default_model = "deepseek-ai/DeepSeek-V4-Pro"', 'default_model = "missing"'
        ),
    )
    with pytest.raises(CatalogError, match=r"providers\.nebius\.default_model"):
        effective_catalog(paths)


def test_user_catalog_rejects_bad_api(tmp_path: Path) -> None:
    paths = _write_user_catalog(
        tmp_path / ".rio",
        VALID_PROVIDER.replace('thinking_parameter = "reasoning_effort"\n', 'api = "grpc"\n'),
    )
    with pytest.raises(CatalogError, match="api"):
        effective_catalog(paths)


def test_user_catalog_rejects_removed_kind(tmp_path: Path) -> None:
    paths = _write_user_catalog(
        tmp_path / ".rio",
        VALID_PROVIDER.replace("[[providers]]\n", '[[providers]]\nkind = "anthropic"\n'),
    )
    with pytest.raises(CatalogError, match="kind"):
        effective_catalog(paths)


def test_user_catalog_rejects_malformed_toml(tmp_path: Path) -> None:
    paths = _write_user_catalog(tmp_path / ".rio", "[[providers]\nname =")
    with pytest.raises(CatalogError, match="invalid TOML"):
        effective_catalog(paths)


@pytest.mark.parametrize(
    "fields",
    [
        "thinking_levels = []",
        'thinking_levels = ["low", "low"]',
        'thinking_parameter = "reasoning_effort"',
        'models = [""]',
        "[providers.model_metadata.local]\nmax_tokens = 0",
        "[providers.model_metadata.local]\nmax_tokens = true",
    ],
)
def test_user_catalog_validates_runtime_fields(tmp_path, fields):
    paths = _write_user_catalog(
        tmp_path / ".rio",
        '[[providers]]\nname = "local"\nbase_url = "http://localhost"\n' + fields,
    )
    with pytest.raises(CatalogError):
        effective_catalog(paths)


def test_obsolete_cache_does_not_override_catalog(tmp_path):
    paths = RioPaths(home=tmp_path)
    document = models_dev_catalog_document(
        _fixture(), builtin_source_catalog(), generated_at=10**15
    )
    (tmp_path / "models-store.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "checked_at": 10**15,
                "catalog": document,
            }
        )
    )
    assert effective_catalog(paths) == builtin_catalog()
