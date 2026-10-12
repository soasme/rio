"""Built-in OAuth providers by stable ID."""

from __future__ import annotations

from rio.coding.oauth import OAuthProvider
from rio.coding.oauth_anthropic import AnthropicOAuthProvider
from rio.coding.oauth_github_copilot import GitHubCopilotOAuthProvider
from rio.coding.oauth_openai_codex import OpenAICodexOAuthProvider

_registry: dict[str, OAuthProvider] = {
    provider.id: provider
    for provider in (
        AnthropicOAuthProvider(),
        GitHubCopilotOAuthProvider(),
        OpenAICodexOAuthProvider(),
    )
}


def get_oauth_provider(provider_id: str) -> OAuthProvider | None:
    """Return a registered OAuth provider by stable provider ID."""
    return _registry.get(provider_id)
