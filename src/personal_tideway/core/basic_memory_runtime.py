"""Basic Memory isolated runtime layout, safe bootstrap config model, and pure install planning.

Work Package 9a implementation for Personal Tideway v2:
- Pinned upstream version 0.23.2 and exact requirement specification.
- Typed immutable layout, plan, and bootstrap configuration dataclasses.
- Strict workspace boundary validation and rejection of symlink escapes.
- Pure build_basic_memory_install_plan with zero filesystem side-effects.
- Deterministic minimal safe bootstrap config.json model without default fallback.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Any

from personal_tideway.config import PersonalTidewayConfig, validate_owned_path
from personal_tideway.exceptions import BoundaryError, ConfigError, ValidationError

# Verified upstream exact version pin and install requirement
BASIC_MEMORY_PINNED_VERSION: str = "0.23.2"
BASIC_MEMORY_REQUIREMENT: str = f"basic-memory=={BASIC_MEMORY_PINNED_VERSION}"

# Environment isolation keys and values
ENV_UV_TOOL_DIR: str = "UV_TOOL_DIR"
ENV_UV_TOOL_BIN_DIR: str = "UV_TOOL_BIN_DIR"
ENV_UV_CACHE_DIR: str = "UV_CACHE_DIR"
ENV_BASIC_MEMORY_CONFIG_DIR: str = "BASIC_MEMORY_CONFIG_DIR"
ENV_BASIC_MEMORY_AUTO_UPDATE: str = "BASIC_MEMORY_AUTO_UPDATE"
ENV_BASIC_MEMORY_NO_PROMOS: str = "BASIC_MEMORY_NO_PROMOS"

BASIC_MEMORY_AUTO_UPDATE_VALUE: str = "false"
BASIC_MEMORY_NO_PROMOS_VALUE: str = "1"


@dataclass(frozen=True)
class BasicMemoryLayout:
    """Isolated filesystem layout for Basic Memory under Personal Tideway services."""

    service_root: Path
    uv_tool_dir: Path
    bin_dir: Path
    config_dir: Path
    config_file: Path
    primary_executable: Path
    alias_executable: Path
    executable_candidates: tuple[Path, ...]
    cache_dir: Path = field(default=Path())
    services_dir: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "executable_candidates", tuple(self.executable_candidates))
        if not str(self.cache_dir) or self.cache_dir == Path():
            object.__setattr__(self, "cache_dir", self.service_root / "cache")
        if self.services_dir is None:
            object.__setattr__(self, "services_dir", self.service_root.parent)

    def to_dict(self) -> dict[str, Any]:
        """Convert layout to safe dictionary representation without leaking sensitive environment data."""
        res = {
            "service_root": str(self.service_root),
            "uv_tool_dir": str(self.uv_tool_dir),
            "bin_dir": str(self.bin_dir),
            "config_dir": str(self.config_dir),
            "cache_dir": str(self.cache_dir),
            "config_file": str(self.config_file),
            "primary_executable": str(self.primary_executable),
            "alias_executable": str(self.alias_executable),
            "executable_candidates": [str(p) for p in self.executable_candidates],
        }
        if self.services_dir is not None:
            res["services_dir"] = str(self.services_dir)
        return res


@dataclass(frozen=True)
class BasicMemoryBootstrapConfig:
    """Deterministic, minimal safe bootstrap configuration model for Basic Memory.

    Isolates Basic Memory:
    - auto_update is disabled (false)
    - default_project is null (no implicit fallback to main or accidental project)
    - projects mapping is empty
    - zero external paths or note paths
    """

    auto_update: bool = False
    default_project: str | None = None
    projects: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.auto_update is not False:
            raise ValidationError("BasicMemoryBootstrapConfig requires auto_update=False")
        if self.default_project is not None:
            raise ValidationError("BasicMemoryBootstrapConfig requires default_project=None")
        if self.projects:
            raise ValidationError("BasicMemoryBootstrapConfig requires empty projects mapping")
        # Enforce true immutability via defensive copy and MappingProxyType
        object.__setattr__(self, "projects", MappingProxyType(dict(self.projects)))

    def to_dict(self) -> dict[str, Any]:
        """Return deterministic dictionary matching Basic Memory config schema."""
        return {
            "auto_update": self.auto_update,
            "default_project": self.default_project,
            "projects": dict(self.projects),
        }

    def to_json(self) -> str:
        """Render deterministic JSON string ending with a newline."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class BasicMemoryInstallPlanPreview:
    """Preview of directories, configuration, and commands planned for execution."""

    intended_directories: tuple[Path, ...]
    config_path: Path
    config_content: str
    install_argv: tuple[str, ...]
    health_argv: tuple[str, ...]
    env_overrides: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "intended_directories", tuple(self.intended_directories))
        object.__setattr__(self, "install_argv", tuple(self.install_argv))
        object.__setattr__(self, "health_argv", tuple(self.health_argv))
        object.__setattr__(self, "env_overrides", MappingProxyType(dict(self.env_overrides)))

    def to_dict(self) -> dict[str, Any]:
        """Convert preview to safe dictionary representation."""
        return {
            "intended_directories": [str(d) for d in self.intended_directories],
            "config_path": str(self.config_path),
            "config_content": self.config_content,
            "install_argv": list(self.install_argv),
            "health_argv": list(self.health_argv),
            "env_overrides": dict(self.env_overrides),
        }


@dataclass(frozen=True)
class BasicMemoryInstallPlan:
    """Immutable plan for installing and health-checking isolated Basic Memory runtime."""

    layout: BasicMemoryLayout
    uv_executable: Path
    install_argv: tuple[str, ...]
    health_argv: tuple[str, ...]
    env_overrides: Mapping[str, str]
    bootstrap_config: BasicMemoryBootstrapConfig

    def __post_init__(self) -> None:
        object.__setattr__(self, "install_argv", tuple(self.install_argv))
        object.__setattr__(self, "health_argv", tuple(self.health_argv))
        object.__setattr__(self, "env_overrides", MappingProxyType(dict(self.env_overrides)))

    def to_dict(self) -> dict[str, Any]:
        """Convert plan to safe dictionary without exposing host environment or secrets."""
        return {
            "layout": self.layout.to_dict(),
            "uv_executable": str(self.uv_executable),
            "install_argv": list(self.install_argv),
            "health_argv": list(self.health_argv),
            "env_overrides": dict(self.env_overrides),
            "bootstrap_config": self.bootstrap_config.to_dict(),
        }

    def preview(self) -> BasicMemoryInstallPlanPreview:
        """Generate a dry-run preview describing intended directories, config, and argv."""
        dirs: list[Path] = []
        if self.layout.services_dir is not None:
            dirs.append(self.layout.services_dir)
        dirs.extend([
            self.layout.service_root,
            self.layout.uv_tool_dir,
            self.layout.bin_dir,
            self.layout.config_dir,
            self.layout.cache_dir,
        ])
        return BasicMemoryInstallPlanPreview(
            intended_directories=tuple(dirs),
            config_path=self.layout.config_file,
            config_content=self.bootstrap_config.to_json(),
            install_argv=self.install_argv,
            health_argv=self.health_argv,
            env_overrides=self.env_overrides,
        )


def get_basic_memory_layout(cfg: PersonalTidewayConfig) -> BasicMemoryLayout:
    """Inspect and resolve canonical isolated Basic Memory layout paths without mutating filesystem.

    Validates all paths against workspace boundary rules and rejects symlink escapes.
    """
    if cfg.home.is_symlink():
        raise BoundaryError(
            "Workspace root is a symlink, which is not permitted."
        )

    service_root = cfg.basic_memory_dir
    uv_tool_dir = service_root / "tool"
    bin_dir = service_root / "bin"
    config_dir = service_root / "config"
    cache_dir = service_root / "cache"
    config_file = config_dir / "config.json"
    primary_executable = bin_dir / "basic-memory"
    alias_executable = bin_dir / "bm"
    executable_candidates = (primary_executable, alias_executable)

    strict_targets = (
        service_root,
        uv_tool_dir,
        bin_dir,
        config_dir,
        cache_dir,
        config_file,
    )
    launcher_targets = (
        primary_executable,
        alias_executable,
    )

    for target in strict_targets:
        curr: Path = target
        while curr != cfg.home and curr != curr.parent:
            if curr.is_symlink():
                raise BoundaryError(
                    "Layout target component is a symlink, which is not permitted in child layout."
                )
            curr = curr.parent

        validate_owned_path(target, root=cfg.home, allow_root=False)

    canonical_home = cfg.home.resolve()

    for target in launcher_targets:
        curr = target.parent
        while curr != cfg.home and curr != curr.parent:
            if curr.is_symlink():
                raise BoundaryError(
                    "Layout target component is a symlink, which is not permitted in child layout."
                )
            curr = curr.parent

        try:
            st = os.lstat(target)
            target_exists = True
            is_symlink = stat.S_ISLNK(st.st_mode)
        except FileNotFoundError:
            target_exists = False
            is_symlink = False
        except (PermissionError, OSError):
            raise BoundaryError(
                "Layout target component is a symlink, which is not permitted in child layout."
            ) from None

        if target_exists:
            if is_symlink:
                try:
                    resolved_non_strict = target.resolve(strict=False)
                except (RuntimeError, PermissionError, OSError):
                    raise BoundaryError(
                        "Layout target component is a symlink, which is not permitted in child layout."
                    ) from None

                if not (resolved_non_strict.is_relative_to(cfg.home) or resolved_non_strict.is_relative_to(canonical_home)):
                    raise BoundaryError(
                        "Layout target component is a symlink, which is not permitted in child layout."
                    )

                try:
                    resolved_strict = target.resolve(strict=True)
                except FileNotFoundError:
                    # Ordinary broken internal launcher: allowed for later self-healing
                    pass
                except (RuntimeError, OSError, PermissionError):
                    raise BoundaryError(
                        "Layout target component is a symlink, which is not permitted in child layout."
                    ) from None
                else:
                    if not (resolved_strict.is_relative_to(cfg.home) or resolved_strict.is_relative_to(canonical_home)):
                        raise BoundaryError(
                            "Layout target component is a symlink, which is not permitted in child layout."
                        )
            else:
                try:
                    resolved_strict = target.resolve(strict=True)
                except (RuntimeError, OSError, PermissionError):
                    raise BoundaryError(
                        "Layout target component is a symlink, which is not permitted in child layout."
                    ) from None
                if not (resolved_strict.is_relative_to(cfg.home) or resolved_strict.is_relative_to(canonical_home)):
                    raise BoundaryError(
                        "Layout target component is a symlink, which is not permitted in child layout."
                    )

        validate_owned_path(target.parent, root=cfg.home, allow_root=False)

    return BasicMemoryLayout(
        service_root=service_root,
        uv_tool_dir=uv_tool_dir,
        bin_dir=bin_dir,
        config_dir=config_dir,
        config_file=config_file,
        primary_executable=primary_executable,
        alias_executable=alias_executable,
        executable_candidates=executable_candidates,
        cache_dir=cache_dir,
        services_dir=cfg.services_dir,
    )


def validate_uv_executable(uv_executable: str | Path | None) -> Path:
    """Validate that uv_executable is an explicit, absolute, existing regular executable file.

    Accepts system and package manager symlinks and resolves strictly to canonical executable.
    Rejects:
    - None or empty values
    - Unsupported input types (non-str, non-Path)
    - Relative paths
    - Non-existent paths or broken symlinks
    - Non-regular files (directories, fifos)
    - Files without executable bit
    Uses fixed error messages to avoid leaking sensitive paths.
    """
    if uv_executable is None:
        raise ValidationError("uv_executable must be explicitly provided; host discovery is disabled")

    if not isinstance(uv_executable, (str, Path)):
        raise ValidationError("uv_executable must be a string or Path")

    try:
        raw_str = str(uv_executable).strip()
        if not raw_str:
            raise ValidationError("uv_executable cannot be empty")
        path = Path(uv_executable)
    except TypeError:
        raise ValidationError("uv_executable must be a string or Path") from None

    if not path.is_absolute():
        raise ValidationError("uv_executable must be an absolute path")

    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        raise ValidationError("uv_executable does not exist") from None
    except (OSError, RuntimeError):
        raise ValidationError("uv_executable does not exist") from None

    try:
        st = os.lstat(resolved)
    except OSError:
        raise ValidationError("uv_executable does not exist") from None

    if not stat.S_ISREG(st.st_mode):
        raise ValidationError("uv_executable must be a regular file")

    if not os.access(resolved, os.X_OK):
        raise ValidationError("uv_executable is not executable")

    return resolved


def build_basic_memory_bootstrap_config() -> BasicMemoryBootstrapConfig:
    """Create minimal safe bootstrap configuration instance."""
    return BasicMemoryBootstrapConfig(
        auto_update=False,
        default_project=None,
        projects={},
    )


def build_basic_memory_install_plan(
    cfg: PersonalTidewayConfig,
    uv_executable: str | Path | None = None,
    uv_resolver: Callable[[], str | Path] | None = None,
) -> BasicMemoryInstallPlan:
    """Construct an immutable, verified Basic Memory installation plan.

    Pure builder: performs zero writes, creates zero files, and executes zero child processes.
    Validates that cfg is initialized and uv_executable meets all security constraints.
    """
    if cfg.home.is_symlink():
        raise BoundaryError(
            "Workspace root is a symlink, which is not permitted."
        )

    if not cfg.is_initialized():
        raise ConfigError(
            "Personal Tideway workspace is not initialized. Run 'ptw init' before creating an install plan."
        )

    if not cfg.home.is_dir():
        raise ConfigError(
            "Personal Tideway workspace directory does not exist or is not a directory."
        )

    # Resolve uv executable without host discovery and without raw exception leakage
    resolved_uv = uv_executable
    if resolved_uv is None and uv_resolver is not None:
        try:
            resolved_uv = uv_resolver()
        except Exception:
            raise ValidationError("Failed to resolve uv executable via resolver") from None

    validated_uv = validate_uv_executable(resolved_uv)
    layout = get_basic_memory_layout(cfg)
    bootstrap_config = build_basic_memory_bootstrap_config()

    # Exact argv without shell execution: [uv, --no-config, tool, install, --force, --prerelease=allow, basic-memory==0.23.2]
    install_argv: tuple[str, ...] = (
        str(validated_uv),
        "--no-config",
        "tool",
        "install",
        "--force",
        "--prerelease=allow",
        BASIC_MEMORY_REQUIREMENT,
    )

    # Health check argv: absolute isolated binary --version only (avoid status/doctor before indexing)
    health_argv: tuple[str, ...] = (
        str(layout.primary_executable),
        "--version",
    )

    # Isolated environment overrides
    env_overrides: dict[str, str] = {
        ENV_UV_TOOL_DIR: str(layout.uv_tool_dir),
        ENV_UV_TOOL_BIN_DIR: str(layout.bin_dir),
        ENV_UV_CACHE_DIR: str(layout.cache_dir),
        ENV_BASIC_MEMORY_CONFIG_DIR: str(layout.config_dir),
        ENV_BASIC_MEMORY_AUTO_UPDATE: BASIC_MEMORY_AUTO_UPDATE_VALUE,
        ENV_BASIC_MEMORY_NO_PROMOS: BASIC_MEMORY_NO_PROMOS_VALUE,
    }

    return BasicMemoryInstallPlan(
        layout=layout,
        uv_executable=validated_uv,
        install_argv=install_argv,
        health_argv=health_argv,
        env_overrides=env_overrides,
        bootstrap_config=bootstrap_config,
    )


__all__ = [
    "BASIC_MEMORY_AUTO_UPDATE_VALUE",
    "BASIC_MEMORY_NO_PROMOS_VALUE",
    "BASIC_MEMORY_PINNED_VERSION",
    "BASIC_MEMORY_REQUIREMENT",
    "BasicMemoryBootstrapConfig",
    "BasicMemoryInstallPlan",
    "BasicMemoryInstallPlanPreview",
    "BasicMemoryLayout",
    "ENV_BASIC_MEMORY_AUTO_UPDATE",
    "ENV_BASIC_MEMORY_CONFIG_DIR",
    "ENV_BASIC_MEMORY_NO_PROMOS",
    "ENV_UV_CACHE_DIR",
    "ENV_UV_TOOL_BIN_DIR",
    "ENV_UV_TOOL_DIR",
    "build_basic_memory_bootstrap_config",
    "build_basic_memory_install_plan",
    "get_basic_memory_layout",
    "validate_uv_executable",
]
