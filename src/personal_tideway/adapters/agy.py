"""Antigravity (agy) adapter for mcp_config.json, GEMINI.md, and skills."""

import json
from pathlib import Path
import shutil
import stat
from typing import Any

from personal_tideway.adapters.base import BaseClientAdapter
from personal_tideway.backup import create_backup_if_changed
from personal_tideway.constants import (
    CLIENT_AGY,
    MAX_HOOKS_JSON_BYTES,
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


def inspect_hooks_path(hooks_path: Path) -> tuple[bool, int]:
    """Safely inspect hooks.json without letting OSError escape or treating errors as absent.

    Returns:
        tuple[bool, int]: (exists, file_size)

    Raises:
        ValidationError: If path is a symlink, not a regular file, unreadable (OSError),
            or exceeds MAX_HOOKS_JSON_BYTES.
    """
    try:
        lstat_res = hooks_path.lstat()
    except FileNotFoundError:
        return False, 0
    except OSError:
        raise ValidationError(f"Failed to read agy hooks file {hooks_path}.")

    if stat.S_ISLNK(lstat_res.st_mode):
        raise ValidationError(f"Agy hooks file cannot be a symlink: {hooks_path}")

    try:
        st = hooks_path.stat()
    except FileNotFoundError:
        return False, 0
    except OSError:
        raise ValidationError(f"Failed to read agy hooks file {hooks_path}.")

    if not stat.S_ISREG(st.st_mode):
        raise ValidationError(f"Agy hooks path is not a regular file: {hooks_path}")

    if st.st_size > MAX_HOOKS_JSON_BYTES:
        raise ValidationError(
            f"Agy hooks file {hooks_path} exceeds maximum allowed size "
            f"({st.st_size} bytes > {MAX_HOOKS_JSON_BYTES} bytes)."
        )

    return True, st.st_size


class AgyAdapter(BaseClientAdapter):
    """Adapter for Antigravity (agy) CLI (~/.gemini/config/mcp_config.json, GEMINI.md, skills)."""

    name = CLIENT_AGY

    def __init__(
        self,
        config_path: Path,
        rules_path: Path,
        skills_path: Path,
        backups_dir: Path,
        hooks_path: Path | None = None,
    ):
        self.config_path = config_path
        self.rules_path = rules_path
        self.skills_path = skills_path
        self.backups_dir = backups_dir
        self.hooks_path = hooks_path or (config_path.parent / "hooks.json")

    def read_mcp_servers(self) -> dict[str, dict[str, Any]]:
        """Read mcpServers object from mcp_config.json."""
        if not self.config_path.is_file():
            return {}

        try:
            content = self.config_path.read_text(encoding="utf-8")
            doc = json.loads(content)
        except Exception as e:
            raise ValidationError(f"Failed to parse agy config at {self.config_path}: {e}")

        mcp_servers = doc.get("mcpServers")
        if not isinstance(mcp_servers, dict):
            return {}

        result: dict[str, dict[str, Any]] = {}
        for name, entry in mcp_servers.items():
            if not isinstance(entry, dict):
                continue

            command = entry.get("command")
            raw_args = entry.get("args", [])
            args = list(raw_args) if isinstance(raw_args, list) else []
            raw_env = entry.get("env", {})
            env = {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, dict) else {}
            server_url = entry.get("serverUrl")

            transport = TRANSPORT_HTTP if server_url else TRANSPORT_STDIO
            result[name] = {
                "name": name,
                "transport": transport,
                "command": command,
                "args": args,
                "url": server_url,
                "enabled": not bool(entry.get("disabled", False)),
                "env": env,
            }

        return result

    def write_mcp_servers(
        self,
        servers: dict[str, dict[str, Any]],
        managed_names: set[str],
        dry_run: bool = False,
    ) -> bool:
        """Update managed MCP servers in mcp_config.json, preserving unrelated keys and servers."""
        if self.config_path.is_file():
            content = self.config_path.read_text(encoding="utf-8")
            doc = json.loads(content)
        else:
            content = ""
            doc = {}

        if "mcpServers" not in doc or not isinstance(doc["mcpServers"], dict):
            doc["mcpServers"] = {}

        mcp_servers = doc["mcpServers"]

        # 1. Update/add servers that target agy
        for name, s_cfg in sorted(servers.items()):
            transport = s_cfg.get("transport", TRANSPORT_STDIO)
            existing = mcp_servers.get(name)
            server_entry = dict(existing) if isinstance(existing, dict) else {}
            had_explicit_enabled = "disabled" in server_entry
            for managed_key in ("command", "args", "serverUrl", "env", "disabled"):
                server_entry.pop(managed_key, None)
            if transport == TRANSPORT_HTTP:
                # HTTP URL serializes as serverUrl in agy-specific file
                server_entry["serverUrl"] = s_cfg.get("url", "")
            else:
                server_entry["command"] = s_cfg.get("command", "")
                server_entry["args"] = s_cfg.get("args", [])
                env = s_cfg.get("env", {})
                if env:
                    server_entry["env"] = dict(env)
            if not bool(s_cfg.get("enabled", True)):
                server_entry["disabled"] = True
            elif had_explicit_enabled:
                server_entry["disabled"] = False
            mcp_servers[name] = server_entry

        # 2. Remove managed servers that are no longer targeted to agy
        for name in managed_names:
            if name not in servers and name in mcp_servers:
                del mcp_servers[name]

        new_content = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        if new_content == content:
            return False

        if not dry_run:
            create_backup_if_changed(self.config_path, new_content, self.backups_dir, dry_run=False)
            atomic_write_text(self.config_path, new_content)

        return True

    def read_rules_block(self) -> tuple[str | None, str | None, str | None]:
        """Read GEMINI.md and split into (prefix, managed_content, suffix)."""
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
        """Write rules into managed marker block in GEMINI.md preserving outer text byte-for-byte."""
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
        """List skill names installed in agy skills directory."""
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
        """Expose skill to agy via symlink or copy."""
        target = self.skills_path / skill_name
        ensure_safe_path(target, self.skills_path, "agy skill installation")

        if dry_run:
            return True

        self.skills_path.mkdir(parents=True, exist_ok=True)

        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)

        if mode == SKILL_LINK_SYMLINK:
            target.symlink_to(source_dir.resolve(), target_is_directory=True)
        else:
            shutil.copytree(source_dir, target)

        return True

    def uninstall_skill(self, skill_name: str, dry_run: bool = False) -> bool:
        """Remove installed skill from agy skills directory."""
        target = self.skills_path / skill_name
        ensure_safe_path(target, self.skills_path, "agy skill uninstall")
        if not (target.exists() or target.is_symlink()):
            return False

        return safe_remove_link_or_dir(target, self.skills_path, dry_run=dry_run)

    def validate_skill_support(self, skill_dir: Path) -> tuple[bool, str]:
        """Verify that skill contains a valid SKILL.md for agy."""
        skill = SkillInfo(name=skill_dir.name, path=skill_dir, scope="check")
        return skill.is_valid()

    def read_hooks_raw(self) -> tuple[dict[str, Any], str]:
        """Safely read and return parsed doc and raw decoded content of hooks.json."""
        exists, _ = inspect_hooks_path(self.hooks_path)
        if not exists:
            return {}, ""

        try:
            raw_bytes = self.hooks_path.read_bytes()
        except OSError:
            raise ValidationError(f"Failed to read agy hooks file {self.hooks_path}.")

        if len(raw_bytes) > MAX_HOOKS_JSON_BYTES:
            raise ValidationError(
                f"Agy hooks file {self.hooks_path} exceeds maximum allowed size "
                f"({len(raw_bytes)} bytes > {MAX_HOOKS_JSON_BYTES} bytes)."
            )

        try:
            content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ValidationError(f"Malformed UTF-8 in agy hooks file {self.hooks_path}: {e}") from e

        def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            seen: set[str] = set()
            res: dict[str, Any] = {}
            for k, v in pairs:
                if k in seen:
                    raise ValidationError(f"Duplicate key '{k}' detected in JSON object in {self.hooks_path}.")
                seen.add(k)
                res[k] = v
            return res

        try:
            doc = json.loads(content, object_pairs_hook=_reject_duplicate_keys)
        except json.JSONDecodeError as e:
            raise ValidationError(f"Malformed JSON in agy hooks file {self.hooks_path}: {e}") from e

        if not isinstance(doc, dict):
            raise ValidationError(f"Agy hooks file {self.hooks_path} must contain a JSON object at the root.")

        return doc, content

    def read_hooks(self) -> dict[str, Any]:
        """Safely read hooks.json dictionary, refusing symlinks, oversized files, duplicate keys, and non-dict roots."""
        doc, _ = self.read_hooks_raw()
        return doc

    def write_hooks(self, hooks_doc: dict[str, Any], dry_run: bool = False) -> bool:
        """Atomically write hooks.json dictionary, creating backups on mutation.

        Fails closed without overwriting if an existing hooks file cannot be read,
        decoded, stat-checked, or validated.
        """
        if not isinstance(hooks_doc, dict):
            raise ValidationError("hooks_doc must be a dictionary.")

        exists, _ = inspect_hooks_path(self.hooks_path)

        new_content = json.dumps(hooks_doc, indent=2, ensure_ascii=False) + "\n"
        if len(new_content.encode("utf-8")) > MAX_HOOKS_JSON_BYTES:
            raise ValidationError(
                f"Generated hooks configuration exceeds maximum allowed size ({MAX_HOOKS_JSON_BYTES} bytes)."
            )

        if exists:
            _existing_doc, old_content = self.read_hooks_raw()
            if old_content == new_content:
                return False

        if not dry_run:
            self.hooks_path.parent.mkdir(parents=True, exist_ok=True)
            create_backup_if_changed(self.hooks_path, new_content, self.backups_dir, dry_run=False)
            atomic_write_text(self.hooks_path, new_content)

        return True
