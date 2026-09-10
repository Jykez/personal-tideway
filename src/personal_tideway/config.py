"""Configuration loading, path resolution, and boundary validation for Personal Tideway."""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any
import yaml

from personal_tideway.constants import (
    CLIENT_AGY,
    CLIENT_CODEX,
    DEFAULT_CONFIG_YAML,
    DEFAULT_PERSONAL_TIDEWAY_DIR,
    DEFAULT_SECRETS_ENV,
    DEFAULT_STATE_FILE,
    SCHEMA_VERSION,
    SKILL_LINK_SYMLINK,
    SUPPORTED_SKILL_LINK_MODES,
)
from personal_tideway.exceptions import BoundaryError, ConfigError, ValidationError


def validate_owned_path(
    path: str | Path,
    root: str | Path,
    allow_root: bool = False,
) -> Path:
    """Validate that path is safely contained within root without side-effects.

    Rejects:
    - None or empty paths
    - the workspace root itself (when allow_root=False)
    - paths escaping the workspace via '..'
    - paths escaping the workspace via symlinks

    Supports paths that do not yet exist.
    Returns the resolved Path if valid, otherwise raises BoundaryError.
    """
    if path is None:
        raise ValidationError("Path cannot be None")

    raw_str = str(path).strip()
    if not raw_str:
        raise ValidationError("Path cannot be empty")

    root_resolved = Path(root).expanduser().resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root_resolved / candidate

    resolved_candidate = candidate.resolve()

    if not resolved_candidate.is_relative_to(root_resolved):
        raise BoundaryError(
            f"Path '{path}' resolves to '{resolved_candidate}', which is outside workspace root '{root_resolved}'"
        )

    if resolved_candidate == root_resolved and not allow_root:
        raise BoundaryError(
            f"Path '{path}' resolves to workspace root '{root_resolved}', but a descendant is required"
        )

    return resolved_candidate


@dataclass
class PersonalTidewayConfig:
    """Encapsulates all resolved paths and configuration for Personal Tideway."""
    home: Path
    codex_home: Path
    gemini_home: Path
    skill_link_mode: str = SKILL_LINK_SYMLINK
    custom_codex_config: Path | None = None
    custom_codex_rules: Path | None = None
    custom_agy_config: Path | None = None
    custom_agy_rules: Path | None = None
    project: str | None = None
    auto_register_git: bool = True

    # Version
    version: int = SCHEMA_VERSION

    # Flags to indicate explicit custom configuration vs platform default
    _explicit_codex_home: bool = False
    _explicit_gemini_home: bool = False

    # Canonical central layout paths (v2)
    @property
    def config_yaml(self) -> Path:
        """Canonical configuration file path."""
        return self.home / DEFAULT_CONFIG_YAML

    @property
    def personal_tideway_yaml(self) -> Path:
        """Compatibility alias for canonical config_yaml."""
        return self.config_yaml

    @property
    def secrets_env(self) -> Path:
        """Canonical secrets environment file path."""
        return self.home / DEFAULT_SECRETS_ENV

    @property
    def registry_dir(self) -> Path:
        """Canonical registry directory path."""
        return self.home / "registry"

    @property
    def projects_yaml(self) -> Path:
        """Canonical projects registry manifest path."""
        return self.registry_dir / "projects.yaml"

    @property
    def projects_registry_file(self) -> Path:
        """Compatibility alias for projects_yaml."""
        return self.projects_yaml

    @property
    def projects_dir(self) -> Path:
        """Canonical projects root directory path."""
        return self.home / "projects"

    @property
    def knowledge_dir(self) -> Path:
        """Canonical knowledge directory path."""
        return self.home / "knowledge"

    @property
    def knowledge_personal_dir(self) -> Path:
        """Canonical personal knowledge directory path."""
        return self.knowledge_dir / "personal"

    @property
    def personal_knowledge_dir(self) -> Path:
        """Compatibility alias for knowledge_personal_dir."""
        return self.knowledge_personal_dir

    @property
    def mcp_dir(self) -> Path:
        """Canonical MCP server definitions directory path."""
        return self.home / "mcp"

    @property
    def rules_dir(self) -> Path:
        """Canonical rules root directory path."""
        return self.home / "rules"

    @property
    def rules_shared(self) -> Path:
        """Canonical shared rules directory path."""
        return self.rules_dir / "shared"

    @property
    def rules_codex(self) -> Path:
        """Canonical Codex rules directory path."""
        return self.rules_dir / CLIENT_CODEX

    @property
    def rules_agy(self) -> Path:
        """Canonical AGY rules directory path."""
        return self.rules_dir / CLIENT_AGY

    @property
    def skills_dir(self) -> Path:
        """Canonical skills root directory path."""
        return self.home / "skills"

    @property
    def skills_shared(self) -> Path:
        """Canonical shared skills directory path."""
        return self.skills_dir / "shared"

    @property
    def skills_codex(self) -> Path:
        """Canonical Codex skills directory path."""
        return self.skills_dir / CLIENT_CODEX

    @property
    def skills_agy(self) -> Path:
        """Canonical AGY skills directory path."""
        return self.skills_dir / CLIENT_AGY

    @property
    def services_dir(self) -> Path:
        """Canonical services directory path."""
        return self.home / "services"

    @property
    def basic_memory_dir(self) -> Path:
        """Canonical Basic Memory service directory path."""
        return self.services_dir / "basic-memory"

    @property
    def services_basic_memory_dir(self) -> Path:
        """Compatibility alias for basic_memory_dir."""
        return self.basic_memory_dir

    @property
    def state_dir(self) -> Path:
        """Canonical state directory path."""
        return self.home / "state"

    @property
    def state_file(self) -> Path:
        """Canonical sync state file path."""
        return self.state_dir / DEFAULT_STATE_FILE

    @property
    def conflicts_dir(self) -> Path:
        """Canonical conflicts directory path."""
        return self.home / "conflicts"

    @property
    def backups_dir(self) -> Path:
        """Canonical backups directory path."""
        return self.home / "backups"

    @property
    def locks_dir(self) -> Path:
        """Canonical concurrency locks directory path."""
        return self.home / "locks"

    # Compatibility properties for legacy components
    @property
    def memory_dir(self) -> Path:
        """Compatibility alias for legacy memory directory."""
        return self.home / "memory"

    @property
    def templates_dir(self) -> Path:
        """Compatibility alias for legacy templates directory."""
        return self.home / "templates"

    # Client-specific paths
    @property
    def codex_config(self) -> Path:
        if self.custom_codex_config:
            return self.custom_codex_config
        return self.codex_home / "config.toml"

    @property
    def codex_rules(self) -> Path:
        if self.custom_codex_rules:
            return self.custom_codex_rules
        return self.codex_home / "AGENTS.md"

    @property
    def codex_override_rules(self) -> Path:
        return self.codex_home / "AGENTS.override.md"

    @property
    def codex_skills(self) -> Path:
        return self.codex_home / "skills"

    @property
    def canonical_user_skills(self) -> Path:
        return Path.home() / ".agents" / "skills"

    @property
    def agy_config(self) -> Path:
        if self.custom_agy_config:
            return self.custom_agy_config
        return self.gemini_home / "config" / "mcp_config.json"

    @property
    def agy_customization_root(self) -> Path:
        return self.gemini_home / "config"

    @property
    def agy_cli_settings(self) -> Path:
        return self.gemini_home / "antigravity-cli" / "settings.json"

    @property
    def agy_current_skills(self) -> Path:
        return self.agy_customization_root / "skills"

    @property
    def agy_rules(self) -> Path:
        if self.custom_agy_rules:
            return self.custom_agy_rules
        return self.gemini_home / "GEMINI.md"

    @property
    def agy_skills(self) -> Path:
        return self.gemini_home / "skills"

    @property
    def agy_legacy_gemini_md(self) -> Path:
        return self.gemini_home / "GEMINI.md"

    @property
    def agy_legacy_skills(self) -> Path:
        return self.gemini_home / "skills"

    def validate_owned_path(self, path: str | Path, allow_root: bool = False) -> Path:
        """Validate that path is safely contained within this workspace root without side-effects."""
        return validate_owned_path(path, root=self.home, allow_root=allow_root)

    def is_owned_path(self, path: str | Path, allow_root: bool = False) -> bool:
        """Check whether path is safely contained within this workspace root."""
        try:
            self.validate_owned_path(path, allow_root=allow_root)
            return True
        except (ValidationError, ConfigError):
            return False

    def is_initialized(self) -> bool:
        """Check if canonical Personal Tideway workspace is initialized."""
        return self.config_yaml.is_file() and self.secrets_env.is_file()

    def to_dict(self) -> dict[str, Any]:
        """Convert configuration to dictionary for config.yaml without user-specific fixture paths."""
        client_paths: dict[str, str] = {}
        if self._explicit_codex_home:
            client_paths["codex_home"] = str(self.codex_home)
        if self._explicit_gemini_home:
            client_paths["gemini_home"] = str(self.gemini_home)
        if self.custom_codex_config:
            client_paths["codex_config"] = str(self.custom_codex_config)
        if self.custom_codex_rules:
            client_paths["codex_rules"] = str(self.custom_codex_rules)
        if self.custom_agy_config:
            client_paths["agy_config"] = str(self.custom_agy_config)
        if self.custom_agy_rules:
            client_paths["agy_rules"] = str(self.custom_agy_rules)

        data: dict[str, Any] = {
            "version": self.version,
            "client_paths": client_paths,
            "skill_link_mode": self.skill_link_mode,
            "projects": {"auto_register_git": self.auto_register_git},
        }
        if self.project is not None:
            data["project"] = self.project
        return data

    @classmethod
    def resolve(
        cls,
        home: str | Path | None = None,
        codex_home: str | Path | None = None,
        gemini_home: str | Path | None = None,
        codex_config: str | Path | None = None,
        codex_rules: str | Path | None = None,
        agy_config: str | Path | None = None,
        agy_rules: str | Path | None = None,
        skill_link_mode: str | None = None,
        project: str | None = None,
        auto_register_git: bool | None = None,
    ) -> "PersonalTidewayConfig":
        """Resolve all configuration paths following strict precedence.

        Precedence: explicit argument > task-specific environment variable > config.yaml > platform default.
        """
        # 1. Resolve PERSONAL_TIDEWAY_HOME
        env_home = os.environ.get("PERSONAL_TIDEWAY_HOME")
        raw_home = home or env_home or (Path.home() / DEFAULT_PERSONAL_TIDEWAY_DIR)
        resolved_home = Path(raw_home).expanduser().resolve()

        # 2. Check config.yaml (canonical)
        yaml_path = resolved_home / DEFAULT_CONFIG_YAML

        file_data: dict[str, Any] = {}
        file_version = SCHEMA_VERSION
        file_client_paths: dict[str, Any] = {}
        file_skill_link_mode: str | None = None
        file_project: str | None = None

        if yaml_path.is_file():
            try:
                with open(yaml_path, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f)
            except Exception as e:
                raise ConfigError(f"Failed to parse {yaml_path}: {e}")

            if not isinstance(loaded, dict):
                raise ConfigError(
                    f"Config file {yaml_path} must have a mapping at root, got {type(loaded).__name__}"
                )
            file_data = loaded

            # Validate version (required in existing config.yaml)
            if "version" not in file_data:
                raise ConfigError(
                    f"Missing required 'version' field in {yaml_path}"
                )
            v = file_data["version"]
            if isinstance(v, bool) or not isinstance(v, int):
                raise ConfigError(
                    f"Invalid schema version in {yaml_path}: version must be an integer, got {type(v).__name__}"
                )
            if v > SCHEMA_VERSION:
                raise ConfigError(
                    f"Unsupported schema version {v} in {yaml_path}: newer than supported version {SCHEMA_VERSION}"
                )
            if v < 1:
                raise ConfigError(
                    f"Invalid schema version {v} in {yaml_path}: version must be >= 1"
                )
            file_version = v

            # Validate client_paths
            if "client_paths" in file_data:
                cp = file_data["client_paths"]
                if not isinstance(cp, dict):
                    raise ConfigError(
                        f"'client_paths' in {yaml_path} must be a mapping, got {type(cp).__name__}"
                    )
                for cp_key, cp_val in cp.items():
                    if cp_val is not None and not isinstance(cp_val, (str, Path)):
                        raise ConfigError(
                            f"Invalid path for '{cp_key}' in {yaml_path}: expected string, got {type(cp_val).__name__}"
                        )
                file_client_paths = cp

            # Validate skill_link_mode
            if "skill_link_mode" in file_data:
                mode_val = file_data["skill_link_mode"]
                if not isinstance(mode_val, str) or mode_val not in SUPPORTED_SKILL_LINK_MODES:
                    raise ConfigError(
                        f"Unsupported skill_link_mode '{mode_val}' in {yaml_path}. Expected one of: {SUPPORTED_SKILL_LINK_MODES}"
                    )
                file_skill_link_mode = mode_val

            # Validate project
            if "project" in file_data:
                proj_val = file_data["project"]
                if proj_val is not None and not isinstance(proj_val, str):
                    raise ConfigError(
                        f"Invalid 'project' in {yaml_path}: expected string or null, got {type(proj_val).__name__}"
                    )
                file_project = proj_val

        # 3. Resolve codex_home with precedence
        explicit_codex_home = False
        if codex_home is not None:
            raw_codex_home = codex_home
            explicit_codex_home = True
        elif os.environ.get("CODEX_HOME"):
            raw_codex_home = os.environ["CODEX_HOME"]
            explicit_codex_home = True
        elif file_client_paths.get("codex_home"):
            raw_codex_home = file_client_paths["codex_home"]
            explicit_codex_home = True
        else:
            raw_codex_home = Path.home() / ".codex"
            explicit_codex_home = False
        resolved_codex_home = Path(raw_codex_home).expanduser().resolve()

        # 4. Resolve gemini_home with precedence (AGY_HOME is a documented alias)
        explicit_gemini_home = False
        env_gemini_home = os.environ.get("GEMINI_HOME") or os.environ.get("AGY_HOME")
        if gemini_home is not None:
            raw_gemini_home = gemini_home
            explicit_gemini_home = True
        elif env_gemini_home:
            raw_gemini_home = env_gemini_home
            explicit_gemini_home = True
        elif file_client_paths.get("gemini_home"):
            raw_gemini_home = file_client_paths["gemini_home"]
            explicit_gemini_home = True
        else:
            raw_gemini_home = Path.home() / ".gemini"
            explicit_gemini_home = False
        resolved_gemini_home = Path(raw_gemini_home).expanduser().resolve()

        # 5. Resolve custom overrides
        c_config = (
            Path(codex_config).expanduser().resolve()
            if codex_config is not None
            else (
                Path(file_client_paths["codex_config"]).expanduser().resolve()
                if file_client_paths.get("codex_config")
                else None
            )
        )
        c_rules = (
            Path(codex_rules).expanduser().resolve()
            if codex_rules is not None
            else (
                Path(file_client_paths["codex_rules"]).expanduser().resolve()
                if file_client_paths.get("codex_rules")
                else None
            )
        )
        a_config = (
            Path(agy_config).expanduser().resolve()
            if agy_config is not None
            else (
                Path(file_client_paths["agy_config"]).expanduser().resolve()
                if file_client_paths.get("agy_config")
                else None
            )
        )
        a_rules = (
            Path(agy_rules).expanduser().resolve()
            if agy_rules is not None
            else (
                Path(file_client_paths["agy_rules"]).expanduser().resolve()
                if file_client_paths.get("agy_rules")
                else None
            )
        )

        # 6. Resolve skill_link_mode with precedence
        if skill_link_mode is not None:
            if skill_link_mode not in SUPPORTED_SKILL_LINK_MODES:
                raise ValidationError(
                    f"Unsupported skill_link_mode '{skill_link_mode}'. Expected one of: {SUPPORTED_SKILL_LINK_MODES}"
                )
            resolved_mode = skill_link_mode
        elif file_skill_link_mode is not None:
            resolved_mode = file_skill_link_mode
        else:
            resolved_mode = SKILL_LINK_SYMLINK

        # 7. Resolve optional project with precedence (preserve absence distinctly)
        env_project_raw = os.environ.get("PERSONAL_TIDEWAY_PROJECT")
        env_project_val = env_project_raw.strip() if env_project_raw is not None else None
        if project is not None:
            resolved_project = project.strip() if project.strip() else None
        elif env_project_val:
            resolved_project = env_project_val
        elif file_project is not None:
            file_proj_clean = file_project.strip() if isinstance(file_project, str) else None
            resolved_project = file_proj_clean if file_proj_clean else None
        else:
            resolved_project = None

        # 8. Resolve optional auto_register_git with precedence
        if auto_register_git is not None:
            if not isinstance(auto_register_git, bool):
                raise ValidationError("auto_register_git must be a boolean.")
            resolved_auto_reg = auto_register_git
        elif (
            "projects" in file_data
            and isinstance(file_data["projects"], dict)
            and "auto_register_git" in file_data["projects"]
        ):
            file_auto_register = file_data["projects"]["auto_register_git"]
            if not isinstance(file_auto_register, bool):
                raise ConfigError(f"'projects.auto_register_git' in {yaml_path} must be a boolean.")
            resolved_auto_reg = file_auto_register
        elif "projects" in file_data and not isinstance(file_data["projects"], dict):
            raise ConfigError(f"'projects' in {yaml_path} must be a mapping.")
        else:
            resolved_auto_reg = True

        return cls(
            home=resolved_home,
            codex_home=resolved_codex_home,
            gemini_home=resolved_gemini_home,
            skill_link_mode=resolved_mode,
            custom_codex_config=c_config,
            custom_codex_rules=c_rules,
            custom_agy_config=a_config,
            custom_agy_rules=a_rules,
            project=resolved_project,
            auto_register_git=resolved_auto_reg,
            version=file_version,
            _explicit_codex_home=explicit_codex_home,
            _explicit_gemini_home=explicit_gemini_home,
        )
