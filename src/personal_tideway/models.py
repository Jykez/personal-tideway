"""Data models for MCP servers, skills, rules, memory, and conflicts."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any
import yaml

from personal_tideway.constants import (
    CLIENT_AGY,
    CLIENT_CODEX,
    SCHEMA_VERSION,
    SUPPORTED_CLIENTS,
    SUPPORTED_TRANSPORTS,
    TRANSPORT_HTTP,
    TRANSPORT_STDIO,
)
from personal_tideway.exceptions import ValidationError


NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")


@dataclass
class MCPServer:
    """Canonical MCP Server representation stored in mcp/<name>.yaml."""
    name: str
    transport: str = TRANSPORT_STDIO
    version: int = SCHEMA_VERSION
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    targets: list[str] = field(default_factory=lambda: [CLIENT_CODEX, CLIENT_AGY])
    enabled: bool = True
    profiles: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.name = str(self.name)
        self.transport = str(self.transport)
        self.version = int(self.version)
        if self.command is not None:
            self.command = str(self.command)
        if self.url is not None:
            self.url = str(self.url)
        if self.args is not None:
            self.args = [str(a) for a in self.args]
        if self.targets is not None:
            self.targets = [str(t) for t in self.targets]
        if self.profiles is not None:
            self.profiles = [str(p) for p in self.profiles]
        if self.tags is not None:
            self.tags = [str(t) for t in self.tags]
        if self.env is not None:
            self.env = {str(k): str(v) for k, v in self.env.items()}

    def validate(self) -> None:
        """Validate mutually required and forbidden fields."""
        if not self.name or not NAME_PATTERN.match(self.name):
            raise ValidationError(
                f"Invalid MCP server name '{self.name}'. Must be non-empty alphanumeric with '-' or '_'."
            )

        if self.transport not in SUPPORTED_TRANSPORTS:
            raise ValidationError(
                f"Invalid transport '{self.transport}' for MCP server '{self.name}'. "
                f"Supported: {SUPPORTED_TRANSPORTS}"
            )

        if not self.targets:
            raise ValidationError(f"MCP server '{self.name}' must specify at least one target.")

        for t in self.targets:
            if t not in SUPPORTED_CLIENTS:
                raise ValidationError(
                    f"Unsupported target '{t}' in server '{self.name}'. Supported: {SUPPORTED_CLIENTS}"
                )

        if self.transport == TRANSPORT_STDIO:
            if not self.command or not self.command.strip():
                raise ValidationError(
                    f"MCP server '{self.name}' uses 'stdio' transport but 'command' is missing or empty."
                )
            if self.url is not None and self.url != "":
                raise ValidationError(
                    f"MCP server '{self.name}' uses 'stdio' transport; 'url' is forbidden."
                )
        elif self.transport == TRANSPORT_HTTP:
            if not self.url or not self.url.strip():
                raise ValidationError(
                    f"MCP server '{self.name}' uses 'http' transport but 'url' is missing or empty."
                )
            if self.command is not None and self.command != "":
                raise ValidationError(
                    f"MCP server '{self.name}' uses 'http' transport; 'command' is forbidden."
                )
            if self.args:
                raise ValidationError(
                    f"MCP server '{self.name}' uses 'http' transport; 'args' are forbidden."
                )

        if not isinstance(self.env, dict):
            raise ValidationError(f"MCP server '{self.name}' env must be a mapping.")
        for k, v in self.env.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValidationError(f"MCP server '{self.name}' env keys and values must be strings.")

        if not isinstance(self.overrides, dict):
            raise ValidationError(f"MCP server '{self.name}' overrides must be a mapping.")
        for client_name in self.overrides:
            if client_name not in SUPPORTED_CLIENTS:
                raise ValidationError(
                    f"Invalid client override '{client_name}' in server '{self.name}'. Supported: {SUPPORTED_CLIENTS}"
                )

    @property
    def is_portable(self) -> bool:
        """Portable subset check: targeted to both codex and agy."""
        return set(self.targets) >= {CLIENT_CODEX, CLIENT_AGY}

    def get_effective_config(self, client: str) -> dict[str, Any]:
        """Return effective client configuration taking client overrides into account."""
        cfg = {
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "url": self.url,
            "enabled": self.enabled,
            "env": dict(self.env),
        }
        if client in self.overrides:
            override = self.overrides[client]
            if "transport" in override:
                cfg["transport"] = override["transport"]
            if "command" in override:
                cfg["command"] = override["command"]
            if "args" in override:
                cfg["args"] = list(override["args"])
            if "url" in override:
                cfg["url"] = override["url"]
            if "enabled" in override:
                cfg["enabled"] = bool(override["enabled"])
            if "env" in override and isinstance(override["env"], dict):
                cfg["env"].update(override["env"])
        return cfg

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for YAML serialization."""
        data: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "transport": self.transport,
            "targets": list(self.targets),
            "enabled": self.enabled,
        }
        if self.transport == TRANSPORT_STDIO:
            data["command"] = self.command
            data["args"] = list(self.args)
        elif self.transport == TRANSPORT_HTTP:
            data["url"] = self.url

        if self.profiles:
            data["profiles"] = list(self.profiles)
        if self.tags:
            data["tags"] = list(self.tags)
        if self.env:
            data["env"] = dict(self.env)
        if self.overrides:
            data["overrides"] = self.overrides

        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MCPServer":
        """Construct and validate from dictionary."""
        if not isinstance(data, dict):
            raise ValidationError("MCP server configuration must be a dictionary.")
        server = cls(
            name=str(data.get("name", "")),
            version=int(data.get("version", SCHEMA_VERSION)),
            transport=str(data.get("transport", TRANSPORT_STDIO)),
            command=data.get("command"),
            args=list(data.get("args", [])) if data.get("args") is not None else [],
            url=data.get("url"),
            targets=list(data.get("targets", [CLIENT_CODEX, CLIENT_AGY])),
            enabled=bool(data.get("enabled", True)),
            profiles=list(data.get("profiles", [])),
            tags=list(data.get("tags", [])),
            env=dict(data.get("env", {})),
            overrides=dict(data.get("overrides", {})),
        )
        server.validate()
        return server


@dataclass
class SkillInfo:
    """Represents a skill located in shared or client-specific folder."""
    name: str
    path: Path
    scope: str  # "shared", "codex", or "agy"

    @property
    def skill_md_path(self) -> Path:
        return self.path / "SKILL.md"

    def is_valid(self) -> tuple[bool, str]:
        """Validate that the skill folder contains a readable SKILL.md."""
        if not self.path.is_dir():
            return False, f"Directory does not exist: {self.path}"
        if not self.skill_md_path.is_file():
            return False, f"Missing SKILL.md in {self.path}"
        try:
            content = self.skill_md_path.read_text(encoding="utf-8")
            if not content.strip():
                return False, f"SKILL.md in {self.path} is empty"
            return True, ""
        except Exception as e:
            return False, f"Cannot read SKILL.md in {self.path}: {e}"


@dataclass
class MemoryEntry:
    """Curated Markdown memory entry with YAML front matter."""
    id: str
    title: str
    tags: list[str]
    created_at: str
    content: str

    def to_markdown(self) -> str:
        """Serialize entry to markdown with front matter."""
        fm = {
            "id": self.id,
            "title": self.title,
            "tags": self.tags,
            "created_at": self.created_at,
        }
        fm_yaml = yaml.safe_dump(fm, sort_keys=False).strip()
        return f"---\n{fm_yaml}\n---\n\n{self.content.strip()}\n"

    @classmethod
    def from_markdown(cls, text: str) -> "MemoryEntry":
        """Parse markdown with YAML front matter into MemoryEntry."""
        if not text.startswith("---"):
            raise ValidationError("Memory entry must start with YAML front matter '---'")

        parts = text.split("---", 2)
        if len(parts) < 3:
            raise ValidationError("Malformed YAML front matter in memory entry")

        fm_text = parts[1]
        body = parts[2].strip()

        try:
            fm = yaml.safe_load(fm_text) or {}
        except Exception as e:
            raise ValidationError(f"Invalid YAML in front matter: {e}")

        entry_id = str(fm.get("id", "")).strip()
        title = str(fm.get("title", "")).strip()
        tags = list(fm.get("tags", []))
        created_at = str(fm.get("created_at", datetime.now(timezone.utc).isoformat()))

        if not entry_id:
            raise ValidationError("Memory front matter missing 'id'")
        if not title:
            raise ValidationError("Memory front matter missing 'title'")

        return cls(
            id=entry_id,
            title=title,
            tags=tags,
            created_at=created_at,
            content=body,
        )


@dataclass
class ConflictRecord:
    """Record of a 3-way synchronization divergence."""
    object_id: str
    object_type: str  # "mcp", "skill", "rules"
    client: str       # "codex", "agy", or "all"
    base_hash: str | None
    ptw_hash: str | None
    client_hash: str | None
    ptw_content: str | None
    client_content: str | None
    message: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConflictRecord":
        return cls(**data)
