"""Base client adapter abstract class."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class BaseClientAdapter(ABC):
    """Abstract interface for Codex and Antigravity (agy) clients."""

    name: str

    @abstractmethod
    def read_mcp_servers(self) -> dict[str, dict[str, Any]]:
        """Read existing MCP server configurations from client config file.

        Returns a dictionary mapping server name to normalized dictionary:
        {
            "transport": "stdio" | "http",
            "command": str | None,
            "args": list[str],
            "url": str | None,
            "env": dict[str, str],
        }
        """
        pass

    @abstractmethod
    def write_mcp_servers(
        self,
        servers: dict[str, dict[str, Any]],
        managed_names: set[str],
        dry_run: bool = False,
    ) -> bool:
        """Write managed MCP servers to client config, preserving unmanaged keys/servers.

        Returns True if changes were (or would be) written.
        """
        pass

    @abstractmethod
    def read_rules_block(self) -> tuple[str | None, str | None, str | None]:
        """Read rules file and extract (prefix, managed_content, suffix).

        If file doesn't exist, returns (None, None, None).
        If file exists but has no markers, returns (full_content, None, None).
        If malformed markers exist, raises ValidationError.
        """
        pass

    @abstractmethod
    def write_rules_block(
        self,
        new_rules: str,
        dry_run: bool = False,
        adopt_existing: bool = False,
    ) -> bool:
        """Write composed rules into managed marker block, preserving outer text byte-for-byte.

        Returns True if changes were (or would be) written.
        """
        pass

    @abstractmethod
    def list_installed_skills(self) -> list[str]:
        """List names of skills installed in client's skill directory."""
        pass

    @abstractmethod
    def install_skill(
        self,
        skill_name: str,
        source_dir: Path,
        mode: str,
        dry_run: bool = False,
    ) -> bool:
        """Expose skill to client via symlink or copy."""
        pass

    @abstractmethod
    def uninstall_skill(self, skill_name: str, dry_run: bool = False) -> bool:
        """Remove installed skill from client directory safely."""
        pass

    @abstractmethod
    def validate_skill_support(self, skill_dir: Path) -> tuple[bool, str]:
        """Verify that client adapter can expose and support this skill."""
        pass
