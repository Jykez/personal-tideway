"""Codex CLI adapter for TOML config, AGENTS.md rules, and skills."""

import os
from pathlib import Path
import shutil
from typing import Any
import tomlkit
from tomlkit.items import Table

from personal_tideway.adapters.base import BaseClientAdapter
from personal_tideway.backup import create_backup_if_changed
from personal_tideway.constants import (
    CLIENT_CODEX,
    RULE_MARKER_END,
    RULE_MARKER_START,
    SKILL_LINK_SYMLINK,
    TRANSPORT_HTTP,
    TRANSPORT_STDIO,
)
from personal_tideway.exceptions import ValidationError
from personal_tideway.models import SkillInfo
from personal_tideway.utils import (
    atomic_write_text,
    ensure_safe_path,
    safe_remove_link_or_dir,
)


class CodexAdapter(BaseClientAdapter):
    """Adapter for Codex CLI (~/.codex/config.toml, AGENTS.md, skills)."""

    name = CLIENT_CODEX

    def __init__(
        self,
        config_path: Path,
        rules_path: Path,
        skills_path: Path,
        backups_dir: Path,
    ):
        self.config_path = config_path
        self.rules_path = rules_path
        self.skills_path = skills_path
        self.backups_dir = backups_dir

    def read_mcp_servers(self) -> dict[str, dict[str, Any]]:
        """Read [mcp_servers.<name>] tables from config.toml."""
        if not self.config_path.is_file():
            return {}

        try:
            content = self.config_path.read_text(encoding="utf-8")
            doc = tomlkit.parse(content)
        except Exception as e:
            raise ValidationError(f"Failed to parse Codex config at {self.config_path}: {e}")

        mcp_servers_table = doc.get("mcp_servers")
        if not isinstance(mcp_servers_table, dict):
            return {}

        result: dict[str, dict[str, Any]] = {}
        for name, entry in mcp_servers_table.items():
            if not isinstance(entry, dict):
                continue

            name_str = str(name)
            raw_command = entry.get("command")
            command = str(raw_command) if raw_command is not None else None
            raw_args = entry.get("args", [])
            args = [str(x) for x in raw_args] if isinstance(raw_args, (list, tuple)) else []
            raw_env = entry.get("env", {})
            env = {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, dict) else {}
            raw_url = entry.get("url")
            url = str(raw_url) if raw_url is not None else None

            transport = TRANSPORT_HTTP if url else TRANSPORT_STDIO
            result[name_str] = {
                "name": name_str,
                "transport": transport,
                "command": command,
                "args": args,
                "url": url,
                "enabled": bool(entry.get("enabled", True)),
                "env": env,
            }

        return result

    def write_mcp_servers(
        self,
        servers: dict[str, dict[str, Any]],
        managed_names: set[str],
        dry_run: bool = False,
    ) -> bool:
        """Update managed MCP servers in config.toml, preserving comments and other tables."""
        if self.config_path.is_file():
            content = self.config_path.read_text(encoding="utf-8")
            doc = tomlkit.parse(content)
        else:
            content = ""
            doc = tomlkit.document()

        # Ensure mcp_servers table exists if we have servers to write
        if "mcp_servers" not in doc:
            doc["mcp_servers"] = tomlkit.table()

        mcp_table = doc["mcp_servers"]

        # 1. Update/add servers that target codex
        for name, s_cfg in sorted(servers.items()):
            existing = mcp_table.get(name)
            server_tbl = existing if isinstance(existing, dict) else tomlkit.table()
            for managed_key in ("command", "args", "url", "env", "enabled"):
                if managed_key in server_tbl:
                    del server_tbl[managed_key]
            transport = s_cfg.get("transport", TRANSPORT_STDIO)
            if transport == TRANSPORT_STDIO:
                server_tbl["command"] = s_cfg.get("command", "")
                args = s_cfg.get("args", [])
                server_tbl["args"] = args
            else:
                server_tbl["url"] = s_cfg.get("url", "")

            env = s_cfg.get("env", {})
            if env:
                env_tbl = tomlkit.table()
                for k in sorted(env.keys()):
                    env_tbl[k] = env[k]
                server_tbl["env"] = env_tbl

            if not bool(s_cfg.get("enabled", True)):
                server_tbl["enabled"] = False

            mcp_table[name] = server_tbl

        # 2. Remove managed servers that are no longer targeted to codex or disabled/removed
        for name in managed_names:
            if name not in servers and name in mcp_table:
                del mcp_table[name]

        new_content = tomlkit.dumps(doc)
        if new_content == content:
            return False

        if not dry_run:
            create_backup_if_changed(self.config_path, new_content, self.backups_dir, dry_run=False)
            atomic_write_text(self.config_path, new_content)

        return True

    def read_rules_block(self) -> tuple[str | None, str | None, str | None]:
        """Read AGENTS.md and split into (prefix, managed_content, suffix)."""
        if not self.rules_path.is_file():
            return None, None, None

        content = self.rules_path.read_text(encoding="utf-8")
        start_count = content.count(RULE_MARKER_START)
        end_count = content.count(RULE_MARKER_END)

        if start_count == 0 and end_count == 0:
            return content, None, None

        if start_count != end_count:
            raise ValidationError(
                f"Malformed rule markers in {self.rules_path}: "
                f"unbalanced start ({start_count}) and end ({end_count}) markers."
            )

        if start_count > 1:
            raise ValidationError(
                f"Malformed rule markers in {self.rules_path}: duplicate marker blocks detected."
            )

        start_idx = content.find(RULE_MARKER_START)
        end_idx = content.find(RULE_MARKER_END)

        if end_idx < start_idx:
            raise ValidationError(
                f"Malformed rule markers in {self.rules_path}: end marker appears before start marker."
            )

        prefix = content[:start_idx]
        managed = content[start_idx + len(RULE_MARKER_START):end_idx].strip("\r\n")
        suffix = content[end_idx + len(RULE_MARKER_END):]

        return prefix, managed, suffix

    def write_rules_block(
        self,
        new_rules: str,
        dry_run: bool = False,
        adopt_existing: bool = False,
    ) -> bool:
        """Write rules into managed marker block preserving outer text byte-for-byte."""
        prefix, managed, suffix = self.read_rules_block()

        block_body = new_rules.strip("\r\n")
        managed_block = f"{RULE_MARKER_START}\n{block_body}\n{RULE_MARKER_END}" if block_body else f"{RULE_MARKER_START}\n{RULE_MARKER_END}"

        if prefix is None:
            # File did not exist
            new_content = f"{managed_block}\n"
            old_content = ""
        elif managed is None and adopt_existing:
            old_content = prefix
            new_content = f"{managed_block}\n"
        elif managed is None:
            # File existed but had no markers
            old_content = prefix
            sep = "\n\n" if prefix and not prefix.endswith("\n") else ("\n" if prefix and not prefix.endswith("\n\n") else "")
            new_content = f"{prefix}{sep}{managed_block}\n"
        else:
            old_content = f"{prefix}{RULE_MARKER_START}{managed}{RULE_MARKER_END}{suffix}"
            new_content = f"{prefix}{managed_block}{suffix}"

        if self.rules_path.is_file() and self.rules_path.read_text(encoding="utf-8") == new_content:
            return False

        if not dry_run:
            create_backup_if_changed(self.rules_path, new_content, self.backups_dir, dry_run=False)
            atomic_write_text(self.rules_path, new_content)

        return True

    def list_installed_skills(self) -> list[str]:
        """List skill names installed in Codex skills directory."""
        if not self.skills_path.is_dir():
            return []
        skills = []
        for entry in self.skills_path.iterdir():
            if (entry.is_dir() or entry.is_symlink()) and (entry / "SKILL.md").exists():
                skills.append(entry.name)
        skills.sort()
        return skills

    def install_skill(
        self,
        skill_name: str,
        source_dir: Path,
        mode: str,
        dry_run: bool = False,
    ) -> bool:
        """Expose skill to Codex via symlink or copy."""
        target = self.skills_path / skill_name
        ensure_safe_path(target, self.skills_path, "Codex skill installation")

        if dry_run:
            return True

        self.skills_path.mkdir(parents=True, exist_ok=True)

        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)

        if mode == SKILL_LINK_SYMLINK:
            # Create symlink pointing directly to source_dir
            target.symlink_to(source_dir.resolve(), target_is_directory=True)
        else:
            shutil.copytree(source_dir, target)

        return True

    def uninstall_skill(self, skill_name: str, dry_run: bool = False) -> bool:
        """Remove installed skill from Codex skills directory."""
        target = self.skills_path / skill_name
        ensure_safe_path(target, self.skills_path, "Codex skill uninstall")
        if not (target.exists() or target.is_symlink()):
            return False

        return safe_remove_link_or_dir(target, self.skills_path, dry_run=dry_run)

    def validate_skill_support(self, skill_dir: Path) -> tuple[bool, str]:
        """Verify that skill contains a valid SKILL.md for Codex."""
        skill = SkillInfo(name=skill_dir.name, path=skill_dir, scope="check")
        return skill.is_valid()
