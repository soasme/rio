"""Tests for rio_coding's model/provider catalog stack.

Ported from tau's test_models_dev.py, test_models_dev_store.py, and
test_provider_catalog.py. Tests that depend on tau_coding.provider_config
(a sibling slice not owned here) are intentionally not ported.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import rio_coding.models_dev_store as store
from rio_coding import models_dev
from rio_coding.catalog_loader import (
    CatalogError,
    builtin_catalog,
    builtin_catalog_resource_text,
    builtin_source_catalog,
    effective_catalog,
    save_user_catalog_entries,
    user_catalog_path,
)
from rio_coding.models_dev import (
    MODELS_DEV_URL,
    NVIDIA_MODELS_URL,
    bundled_models_dev_catalog_overlay,
    models_dev_catalog_document,
    models_dev_catalog_overlay,
    thinking_level_map_from_reasoning_options,
)
from rio_coding.models_dev_store import (
    ModelsDevRefreshError,
    cached_models_dev_catalog_document,
    refresh_models_dev_catalog,
)
from rio_coding.paths import RioPaths
from rio_coding.provider_catalog import (
    BUILTIN_PROVIDER_CATALOG,
    builtin_provider_entry,
    model_cost_for_input_tokens,
)

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
                "name": "New Tool Model",
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
kind = "openai-compatible"
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
        "name": "New Tool Model",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0.1, "output": 0.2, "cacheRead": 0.0, "cacheWrite": 0.0},
        "context_window": 65536,
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


def test_missing_bundled_catalog_falls_back_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_files(_package: str) -> None:
        raise OSError("offline package fixture")

    monkeypatch.setattr(models_dev, "files", missing_files)

    assert bundled_models_dev_catalog_overlay() is None


# ---------------------------------------------------------------------------
# models_dev_store
# ---------------------------------------------------------------------------


def _source() -> dict[str, object]:
    source = _fixture()
    source["nvidia"] = {"id": "nvidia", "models": {}}
    return source


def _client(source: dict[str, object]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == MODELS_DEV_URL:
            return httpx.Response(200, json=source, headers={"etag": '"fixture"'})
        if str(request.url) == NVIDIA_MODELS_URL:
            return httpx.Response(200, json={"data": []})
        raise AssertionError(f"unexpected request: {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_refresh_persists_catalog_and_effective_catalog_uses_newer_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RioPaths(home=tmp_path / ".rio")
    async with _client(_source()) as client:
        result = await refresh_models_dev_catalog(
            paths=paths,
            force=True,
            client=client,
            now=1000.0,
        )

    assert result.refreshed
    assert result.model_count > 0
    assert result.cache_path.exists()
    monkeypatch.setattr(
        store,
        "bundled_models_dev_catalog_document",
        lambda: {"generated_at": 2_000_000},
    )
    assert cached_models_dev_catalog_document(paths) is None
    monkeypatch.setattr(store, "bundled_models_dev_catalog_document", lambda: {"generated_at": 0})
    assert cached_models_dev_catalog_document(paths) is not None
    huggingface = next(
        provider for provider in effective_catalog(paths) if provider.name == "huggingface"
    )
    assert "example/new-tool-model" in huggingface.models


async def test_refresh_revalidates_with_etag_and_preserves_cached_body(tmp_path: Path) -> None:
    paths = RioPaths(home=tmp_path / ".rio")
    async with _client(_source()) as client:
        await refresh_models_dev_catalog(paths=paths, force=True, client=client, now=1000.0)
    result_path = paths.home / "models-store.json"
    before_catalog = json.loads(result_path.read_text(encoding="utf-8"))["catalog"]

    def not_modified(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == '"fixture"'
        return httpx.Response(304)

    async with httpx.AsyncClient(transport=httpx.MockTransport(not_modified)) as client:
        result = await refresh_models_dev_catalog(
            paths=paths,
            force=True,
            client=client,
            now=2000.0,
        )

    after = json.loads(result_path.read_text(encoding="utf-8"))
    assert result.not_modified
    assert after["checked_at"] == 2000.0
    assert after["catalog"] == before_catalog


async def test_offline_mode_uses_bundled_catalog_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RIO_OFFLINE", "1")

    def no_network(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("offline mode should avoid network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_network)) as client:
        result = await refresh_models_dev_catalog(
            paths=RioPaths(home=tmp_path / ".rio"),
            force=True,
            client=client,
        )

    assert not result.refreshed
    assert result.model_count > 0


async def test_fresh_cache_skips_network_and_failed_force_preserves_it(tmp_path: Path) -> None:
    paths = RioPaths(home=tmp_path / ".rio")
    async with _client(_source()) as client:
        first = await refresh_models_dev_catalog(paths=paths, force=True, client=client, now=1000.0)
    cached_text = first.cache_path.read_text(encoding="utf-8")

    def no_network(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("fresh cache should avoid network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_network)) as client:
        skipped = await refresh_models_dev_catalog(
            paths=paths,
            client=client,
            now=1001.0,
        )
    assert not skipped.refreshed

    def failure(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(failure)) as client:
        with pytest.raises(ModelsDevRefreshError, match="503"):
            await refresh_models_dev_catalog(
                paths=paths,
                force=True,
                client=client,
                now=2000.0,
            )
    assert first.cache_path.read_text(encoding="utf-8") == cached_text


# ---------------------------------------------------------------------------
# provider_catalog / catalog_loader
# ---------------------------------------------------------------------------


def test_builtin_catalog_matches_expected_providers() -> None:
    names = [entry.name for entry in BUILTIN_PROVIDER_CATALOG]
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
    assert entry.display_name == "Anthropic"
    assert entry.kind == "anthropic"
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
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-sonnet-4-5",
        "claude-sonnet-4-5-20250929",
        "claude-sonnet-4-6",
        "claude-sonnet-5",
    } <= set(entry.models)
    assert entry.default_model == "claude-sonnet-4-6"
    assert entry.docs_url == "https://docs.anthropic.com"
    assert entry.context_windows is not None
    assert entry.context_windows["claude-haiku-4-5"] == 200_000
    assert entry.context_windows["claude-opus-5"] == 1_000_000
    assert entry.context_windows["claude-sonnet-4-6"] == 1_000_000
    assert set(entry.context_windows) == set(entry.models)
    opus_5 = entry.model_metadata["claude-opus-5"]
    assert opus_5.context_window == 1_000_000
    assert opus_5.max_tokens == 128_000
    assert opus_5.input == ("text", "image")
    assert opus_5.cost is not None
    assert opus_5.cost["input"] == 5
    assert opus_5.cost["output"] == 25
    assert opus_5.cost["cacheWrite"] == 6.25
    assert opus_5.cost["cacheWrite1h"] == 10
    assert opus_5.compat == {"forceAdaptiveThinking": True}
    assert opus_5.thinking_level_map == {
        "off": None,
        "minimal": None,
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "xhigh",
        "max": "max",
    }
    assert entry.thinking_levels == ("off", "minimal", "low", "medium", "high", "xhigh")
    assert entry.thinking_models == ()
    assert entry.thinking_default == "medium"
    assert entry.thinking_parameter == "anthropic.thinking"
    assert entry.auth_methods == ("api_key", "oauth")


def test_builtin_catalog_separates_openai_api_and_codex_context_limits() -> None:
    openai = builtin_provider_entry("openai")
    codex = builtin_provider_entry("openai-codex")

    assert openai is not None
    assert codex is not None
    assert openai.context_windows is not None
    assert codex.context_windows is not None
    assert "gpt-5.6" in openai.models
    assert "gpt-5.6" not in codex.models
    assert "gpt-5.6" not in codex.context_windows
    assert "gpt-5.6" not in codex.model_metadata
    assert codex.removed_models == ("gpt-5.6",)
    assert openai.context_windows["gpt-5.6-sol"] == 1_050_000
    assert codex.context_windows["gpt-5.6-sol"] == 272_000
    assert codex.context_windows["gpt-5.6-terra"] == 272_000
    assert codex.context_windows["gpt-5.6-luna"] == 272_000
    assert codex.model_metadata["gpt-5.6-sol"].context_window == 272_000


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
                "mimo-v2.5-free",
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


def test_builtin_catalog_oauth_and_opencode_auth_methods() -> None:
    codex = builtin_provider_entry("openai-codex")
    copilot = builtin_provider_entry("github-copilot")
    opencode_go = builtin_provider_entry("opencode-go")
    opencode = builtin_provider_entry("opencode")

    assert codex is not None and codex.auth_methods == ("oauth",)
    assert copilot is not None and copilot.auth_methods == ("oauth",)
    assert opencode_go is not None and opencode_go.auth_methods == ("api_key",)
    assert opencode is not None and opencode.auth_methods == ("api_key",)
    assert opencode_go.api_key_env == "OPENCODE_API_KEY"
    assert opencode.api_key_env == "OPENCODE_API_KEY"


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


def test_builtin_catalog_golden_nvidia_entry() -> None:
    entry = builtin_provider_entry("nvidia")
    assert entry is not None
    assert entry.display_name == "NVIDIA NIM"
    assert entry.kind == "openai-compatible"
    assert entry.base_url == "https://integrate.api.nvidia.com/v1"
    assert entry.api_key_env == "NVIDIA_API_KEY"
    assert entry.credential_name == "nvidia"
    assert {
        "nvidia/llama-3.3-nemotron-super-49b-v1.5",
        "nvidia/nvidia-nemotron-nano-9b-v2",
        "meta/llama-3.3-70b-instruct",
        "meta/llama-3.1-8b-instruct",
        "mistralai/mistral-large-2-instruct",
        "openai/gpt-oss-120b",
    } <= set(entry.models)
    assert entry.default_model == "nvidia/llama-3.3-nemotron-super-49b-v1.5"
    assert entry.docs_url == "https://docs.api.nvidia.com/nim"
    assert entry.api == "openai-completions"
    assert entry.context_windows is not None
    assert entry.context_windows["nvidia/llama-3.3-nemotron-super-49b-v1.5"] == 131_072
    assert entry.context_windows["openai/gpt-oss-120b"] == 131_072
    assert set(entry.context_windows) == set(entry.models)
    assert entry.thinking_levels == ("off", "minimal", "low", "medium", "high")
    assert entry.thinking_models == ()
    assert entry.thinking_default == "medium"
    assert entry.thinking_parameter == "reasoning_effort"

    default_metadata = entry.model_metadata[entry.default_model]
    assert default_metadata.name == "Llama 3.3 Nemotron Super 49B v1.5"
    assert default_metadata.reasoning is True
    assert default_metadata.input == ("text",)
    assert default_metadata.context_window == 131_072
    assert default_metadata.max_tokens == 65_536
    assert default_metadata.cost == {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}

    gpt_oss_metadata = entry.model_metadata["openai/gpt-oss-120b"]
    assert gpt_oss_metadata.reasoning is True
    assert gpt_oss_metadata.context_window == 128_000
    assert gpt_oss_metadata.max_tokens == 8_192


def test_builtin_catalog_huggingface_model_expansion() -> None:
    entry = builtin_provider_entry("huggingface")
    assert entry is not None
    added_models = {
        "MiniMaxAI/MiniMax-M2",
        "MiniMaxAI/MiniMax-M3",
        "Qwen/Qwen3-235B-A22B",
        "Qwen/Qwen3-32B",
        "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        "Qwen/Qwen3.5-122B-A10B",
        "Qwen/Qwen3.5-27B",
        "Qwen/Qwen3.5-35B-A3B",
        "Qwen/Qwen3.5-9B",
        "Qwen/Qwen3.6-27B",
        "Qwen/Qwen3.6-35B-A3B",
        "XiaomiMiMo/MiMo-V2.5-Pro",
        "deepseek-ai/DeepSeek-R1",
        "deepseek-ai/DeepSeek-V4-Flash",
        "deepseek-ai/DeepSeek-V4-Pro",
        "google/gemma-4-26B-A4B-it",
        "google/gemma-4-31B-it",
        "meta-llama/Llama-3.3-70B-Instruct",
        "moonshotai/Kimi-K2.7-Code",
        "moonshotai/Kimi-K3",
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "stepfun-ai/Step-3.5-Flash",
        "stepfun-ai/Step-3.7-Flash",
        "zai-org/GLM-4.5",
        "zai-org/GLM-4.5-Air",
        "zai-org/GLM-4.5V",
        "zai-org/GLM-4.6",
        "zai-org/GLM-5.2",
    }

    assert len(entry.models) >= 47
    assert added_models <= set(entry.models)
    assert set(entry.context_windows or {}) == set(entry.models)
    assert set(entry.model_metadata) == set(entry.models)
    assert entry.default_model == "moonshotai/Kimi-K2.6"

    minimax_m3 = entry.model_metadata["MiniMaxAI/MiniMax-M3"]
    assert minimax_m3.input == ("text", "image")
    assert minimax_m3.context_window == 524_288
    assert minimax_m3.max_tokens == 128_000

    llama = entry.model_metadata["meta-llama/Llama-3.3-70B-Instruct"]
    assert llama.reasoning is False
    assert llama.context_window == 131_072

    kimi_k3 = entry.model_metadata["moonshotai/Kimi-K3"]
    assert kimi_k3.name == "Kimi K3"
    assert kimi_k3.reasoning is True
    assert kimi_k3.input == ("text", "image")
    assert kimi_k3.context_window == 1_000_000
    assert kimi_k3.cost == {"input": 3, "output": 15, "cacheRead": 0, "cacheWrite": 0}
    assert kimi_k3.thinking_level_map == {
        "off": None,
        "minimal": None,
        "low": "low",
        "medium": None,
        "high": "high",
        "xhigh": None,
        "max": "max",
    }


def test_builtin_catalog_golden_kimi_entries() -> None:
    moonshot = builtin_provider_entry("moonshotai")
    assert moonshot is not None
    assert moonshot.display_name == "Moonshot AI (Kimi)"
    assert moonshot.default_model == "kimi-k2.7-code"
    assert "kimi-k2.7-code" in moonshot.models
    assert moonshot.context_windows is not None
    assert moonshot.context_windows["kimi-k2.7-code"] == 262_144

    moonshot_cn = builtin_provider_entry("moonshotai-cn")
    assert moonshot_cn is not None
    assert moonshot_cn.default_model == "kimi-k2.7-code"
    assert "kimi-k2.7-code" in moonshot_cn.models
    assert moonshot_cn.context_windows is not None
    assert moonshot_cn.context_windows["kimi-k2.7-code"] == 262_144

    k2_7 = moonshot.model_metadata["kimi-k2.7-code"]
    assert k2_7.name == "Kimi K2.7 Code"
    assert k2_7.reasoning is True
    assert k2_7.input == ("text", "image")
    assert k2_7.context_window == 262_144
    assert k2_7.max_tokens == 262_144
    assert k2_7.thinking_level_map == {
        "off": None,
        "minimal": None,
        "low": None,
        "high": None,
    }

    coding = builtin_provider_entry("kimi-code")
    assert coding is not None
    assert coding.display_name == "Kimi Code subscription"
    assert coding.base_url == "https://api.kimi.com/coding/v1"
    assert coding.api_key_env == "KIMI_CODE_API_KEY"
    assert coding.credential_name == "kimi-code"
    assert {"k3", "kimi-for-coding", "k3-256k", "kimi-for-coding-highspeed"} == set(coding.models)
    assert coding.default_model == "kimi-for-coding"
    assert coding.thinking_default == "max"
    assert coding.context_windows is not None
    assert coding.context_windows["k3"] == 1_048_576
    assert coding.context_windows["kimi-for-coding"] == 262_144
    assert set(coding.context_windows) == set(coding.models)

    k3 = coding.model_metadata["k3"]
    assert k3.name == "Kimi K3"
    assert k3.reasoning is True
    assert k3.input == ("text", "image")
    assert k3.context_window == 1_048_576
    assert k3.thinking_level_map == {
        "off": None,
        "minimal": None,
        "low": "low",
        "medium": None,
        "high": "high",
        "xhigh": None,
        "max": "max",
    }

    latest = coding.model_metadata["kimi-for-coding"]
    assert latest.name == "Kimi K2.7 Code"
    assert latest.reasoning is True
    assert latest.context_window == 262_144
    assert latest.thinking_level_map == {
        "off": None,
        "minimal": None,
        "low": None,
        "high": None,
        "xhigh": None,
    }


def test_builtin_minimax_m3_has_tiered_pricing() -> None:
    base_cost = {"input": 0.3, "output": 1.2, "cacheRead": 0.06, "cacheWrite": 0}
    long_context_cost = {
        "input": 0.6,
        "output": 2.4,
        "cacheRead": 0.12,
        "cacheWrite": 0,
    }

    for provider_name in ("minimax", "minimax-cn"):
        entry = builtin_provider_entry(provider_name)
        assert entry is not None
        metadata = entry.model_metadata["MiniMax-M3"]
        assert metadata.input == ("text", "image")
        assert metadata.cost == base_cost
        assert model_cost_for_input_tokens(metadata, 512_000) == base_cost
        assert model_cost_for_input_tokens(metadata, 512_001) == long_context_cost


@pytest.mark.parametrize("input_tokens", [-1, True])
def test_model_cost_for_input_tokens_rejects_invalid_count(input_tokens: int) -> None:
    entry = builtin_provider_entry("minimax")
    assert entry is not None
    metadata = entry.model_metadata["MiniMax-M3"]

    with pytest.raises(ValueError, match="non-negative integer"):
        model_cost_for_input_tokens(metadata, input_tokens)


def test_builtin_catalog_entries_are_internally_consistent() -> None:
    for entry in builtin_catalog():
        assert entry.default_model in entry.models
        assert set(entry.thinking_models) <= set(entry.models)
        assert set(entry.context_windows or {}) <= set(entry.models)
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
    assert entry.context_windows == {"deepseek-ai/DeepSeek-V4-Pro": 163_840}
    assert entry.thinking_levels == ("off", "low", "medium", "high")


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
    assert entry.context_windows is not None
    assert entry.context_windows["claude-next-1"] == 500_000
    assert entry.context_windows["claude-opus-4-7"] == 1_000_000
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
    assert entry.context_windows is not None
    assert "gpt-5.6" not in entry.context_windows
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


def test_user_catalog_overlays_and_serializes_cost_tiers(tmp_path: Path) -> None:
    paths = _write_user_catalog(
        tmp_path / ".rio",
        """
[[providers]]
name = "minimax"

[providers.model_metadata."MiniMax-M3"]
cost_tiers = [
  { max_input_tokens = 400000, input = 0.2, output = 1.0, cacheRead = 0.04, cacheWrite = 0 },
  { input = 0.5, output = 2.0, cacheRead = 0.1, cacheWrite = 0 },
]
""",
    )
    entry = next(e for e in effective_catalog(paths) if e.name == "minimax")
    metadata = entry.model_metadata["MiniMax-M3"]
    assert model_cost_for_input_tokens(metadata, 400_000) == {
        "input": 0.2,
        "output": 1.0,
        "cacheRead": 0.04,
        "cacheWrite": 0,
    }
    long_context_cost = {
        "input": 0.5,
        "output": 2.0,
        "cacheRead": 0.1,
        "cacheWrite": 0,
    }
    assert model_cost_for_input_tokens(metadata, 400_001) == long_context_cost

    save_user_catalog_entries([entry], paths)
    reloaded = next(e for e in effective_catalog(paths) if e.name == "minimax")
    assert (
        model_cost_for_input_tokens(reloaded.model_metadata["MiniMax-M3"], 400_001)
        == long_context_cost
    )


def test_user_catalog_cost_tier_accepts_one_hour_cache_write_rate(tmp_path: Path) -> None:
    paths = _write_user_catalog(
        tmp_path / ".rio",
        """
[[providers]]
name = "minimax"

[providers.model_metadata."MiniMax-M3"]
cost_tiers = [
  { max_input_tokens = 400000, input = 0.2, output = 1.0, cacheRead = 0.04, cacheWrite = 0.25 },
  { input = 0.5, output = 2.0, cacheRead = 0.1, cacheWrite = 0.6, cacheWrite1h = 1.0 },
]
""",
    )
    entry = next(e for e in effective_catalog(paths) if e.name == "minimax")
    metadata = entry.model_metadata["MiniMax-M3"]
    assert model_cost_for_input_tokens(metadata, 400_001) == {
        "input": 0.5,
        "output": 2.0,
        "cacheRead": 0.1,
        "cacheWrite": 0.6,
        "cacheWrite1h": 1.0,
    }
    # Tiers without the key omit it, so billing can fall back to cacheWrite.
    assert model_cost_for_input_tokens(metadata, 400_000) == {
        "input": 0.2,
        "output": 1.0,
        "cacheRead": 0.04,
        "cacheWrite": 0.25,
    }


def test_user_catalog_rejects_bounded_final_cost_tier(tmp_path: Path) -> None:
    paths = _write_user_catalog(
        tmp_path / ".rio",
        """
[[providers]]
name = "minimax"

[providers.model_metadata."MiniMax-M3"]
cost_tiers = [
  { max_input_tokens = 512000, input = 0.3, output = 1.2, cacheRead = 0.06, cacheWrite = 0 },
]
""",
    )
    with pytest.raises(CatalogError, match="final tier must omit max_input_tokens"):
        effective_catalog(paths)


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


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (
            VALID_PROVIDER.replace('display_name = "Nebius AI Studio"', 'display_name = ""'),
            r"providers\.nebius\.display_name",
        ),
        (
            VALID_PROVIDER.replace(
                'models = ["deepseek-ai/DeepSeek-V4-Pro", "Qwen/Qwen3-Coder-480B-A35B-Instruct"]',
                'models = [""]',
            ),
            r"providers\.nebius\.models",
        ),
        (
            VALID_PROVIDER.replace('"deepseek-ai/DeepSeek-V4-Pro" = 163840', '"" = 163840'),
            r"providers\.nebius\.context_windows",
        ),
        (
            VALID_PROVIDER.replace(
                '"deepseek-ai/DeepSeek-V4-Pro" = 163840',
                '"deepseek-ai/DeepSeek-V4-Pro" = 0',
            ),
            r"providers\.nebius\.context_windows",
        ),
        (
            VALID_PROVIDER.replace(
                '"deepseek-ai/DeepSeek-V4-Pro" = 163840',
                '"deepseek-ai/DeepSeek-V4-Pro" = -1',
            ),
            r"providers\.nebius\.context_windows",
        ),
        (
            VALID_PROVIDER.replace(
                '"deepseek-ai/DeepSeek-V4-Pro" = 163840',
                '"deepseek-ai/DeepSeek-V4-Pro" = true',
            ),
            r"providers\.nebius\.context_windows",
        ),
        (
            VALID_PROVIDER.replace(
                '"deepseek-ai/DeepSeek-V4-Pro" = 163840',
                '"deepseek-ai/DeepSeek-V4-Pro" = "163840"',
            ),
            r"providers\.nebius\.context_windows",
        ),
    ],
)
def test_user_catalog_rejects_empty_and_coerced_values(
    tmp_path: Path,
    body: str,
    match: str,
) -> None:
    paths = _write_user_catalog(tmp_path / ".rio", body)
    with pytest.raises(CatalogError, match=match):
        effective_catalog(paths)


def test_user_catalog_rejects_bad_kind(tmp_path: Path) -> None:
    paths = _write_user_catalog(
        tmp_path / ".rio", VALID_PROVIDER.replace("openai-compatible", "grpc")
    )
    with pytest.raises(CatalogError, match="kind"):
        effective_catalog(paths)


def test_user_catalog_rejects_malformed_toml(tmp_path: Path) -> None:
    paths = _write_user_catalog(tmp_path / ".rio", "[[providers]\nname =")
    with pytest.raises(CatalogError, match="invalid TOML"):
        effective_catalog(paths)
