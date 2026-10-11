"""Canonical filesystem paths for rio user and project data."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RioPaths:
    """Resolved rio filesystem locations.

    rio keeps durable application data under the user's home directory while also
    loading project-local resources from the active working directory.
    """

    home: Path = field(default_factory=lambda: Path.home() / ".rio")

    @property
    def sessions_dir(self) -> Path:
        """Return the user-level session directory."""
        return self.home / "sessions"

    @property
    def models_store_path(self) -> Path:
        """Return the persisted remote model-catalog cache path."""
        return self.home / "models-store.json"

    @property
    def extension_state_dir(self) -> Path:
        """Return the user-level state directory owned by built-in extensions."""
        return self.home / "state" / "extensions"

    @property
    def llama_cpp_state_path(self) -> Path:
        """Return the safe built-in llama.cpp integration state path."""
        return self.extension_state_dir / "llama.cpp.json"

    @property
    def mcp_config_path(self) -> Path:
        """Return the user-level MCP server configuration."""
        return self.home / "mcp.json"

    @property
    def mcp_auth_path(self) -> Path:
        """Return the stored OAuth credentials of MCP servers."""
        return self.home / "mcp-auth.json"

    def project_rio_dir(self, cwd: Path) -> Path:
        """Return the project-local rio resource directory."""
        return cwd / ".rio"

    def project_mcp_config_path(self, cwd: Path) -> Path:
        """Return the project-local MCP server configuration."""
        return self.project_rio_dir(cwd) / "mcp.json"
