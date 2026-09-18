"""Deterministic read-only migration planning for Personal Tideway (Schema v1 -> v2).

Migration 5A: Fully read-only planning logic that detects schema-v1 state
and produces a deterministic migration plan without side-effects.
"""

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from personal_tideway.constants import (
    DEFAULT_CONFIG_YAML,
    DEFAULT_PERSONAL_TIDEWAY_DIR,
    SCHEMA_VERSION,
)

STATUS_NOT_REQUIRED = "not_required"
STATUS_READY = "ready"
STATUS_BLOCKED = "blocked"

VALID_STATUSES = (STATUS_NOT_REQUIRED, STATUS_READY, STATUS_BLOCKED)

MAX_CONFIG_BYTES = 64 * 1024  # 64 KiB conservative cap for config.yaml

SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

SUPPORTED_CLIENT_PATH_KEYS = frozenset({
    "codex_home",
    "gemini_home",
    "codex_config",
    "codex_rules",
    "codex_hooks",
    "agy_config",
    "agy_rules",
    "agy_hooks",
})


class UniqueKeyLoader(yaml.SafeLoader):
    """YAML safe loader that rejects duplicate keys in mappings to fail closed."""


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        try:
            key = loader.construct_object(key_node, deep=deep)
        except Exception as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "invalid unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if not isinstance(key, str):
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "mapping key must be a string",
                key_node.start_mark,
            )
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found duplicate mapping key",
                key_node.start_mark,
            )
        value = loader.construct_object(value_node, deep=deep)
        mapping[key] = value
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class MigrationPlan:
    """Encapsulates deterministic migration assessment and planned operations."""

    status: str
    source_schema_version: int | None
    target_schema_version: int
    actions: list[str]
    backups: list[str]
    blockers: list[str]
    warnings: list[str]
    preserve: list[str]
    legacy_paths: list[str]
    current_paths: list[str]
    canonical_mcp: list[str]
    client_files: list[str]

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"Invalid migration plan status: {self.status}")
        if self.target_schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"Target schema version must be {SCHEMA_VERSION}, got {self.target_schema_version}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Convert plan to deterministic dictionary matching the JSON schema."""
        return {
            "status": self.status,
            "source_schema_version": self.source_schema_version,
            "target_schema_version": self.target_schema_version,
            "actions": list(self.actions),
            "backups": list(self.backups),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "preserve": list(self.preserve),
            "legacy_paths": list(self.legacy_paths),
            "current_paths": list(self.current_paths),
            "canonical_mcp": list(self.canonical_mcp),
            "client_files": list(self.client_files),
        }


def format_migration_plan_text(plan: MigrationPlan) -> str:
    """Format migration plan for human consumption with complete semantic parity to JSON."""
    lines: list[str] = [
        "Migration Plan",
        "==============",
        f"Status: {plan.status}",
        f"Source Schema Version: {plan.source_schema_version if plan.source_schema_version is not None else 'none'}",
        f"Target Schema Version: {plan.target_schema_version}",
        "",
        "Blockers:",
    ]
    if plan.blockers:
        for b in plan.blockers:
            lines.append(f"  - {b}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Actions:")
    if plan.actions:
        for a in plan.actions:
            lines.append(f"  - {a}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Backups:")
    if plan.backups:
        for b in plan.backups:
            lines.append(f"  - {b}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Preserve:")
    if plan.preserve:
        for p in plan.preserve:
            lines.append(f"  - {p}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Legacy Paths:")
    if plan.legacy_paths:
        for p in plan.legacy_paths:
            lines.append(f"  - {p}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Current Paths:")
    if plan.current_paths:
        for p in plan.current_paths:
            lines.append(f"  - {p}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Canonical MCP:")
    if plan.canonical_mcp:
        for m in plan.canonical_mcp:
            lines.append(f"  - {m}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Client Files:")
    if plan.client_files:
        for f in plan.client_files:
            lines.append(f"  - {f}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Warnings:")
    if plan.warnings:
        for w in plan.warnings:
            lines.append(f"  - {w}")
    else:
        lines.append("  (none)")

    return "\n".join(lines)


def _safe_is_symlink(path: Path) -> bool:
    """Check if path is a symlink (including broken symlinks) using lstat."""
    try:
        return os.path.islink(path)
    except OSError:
        return False


def _has_symlink_component(path: Path) -> bool:
    """Return True when any existing component of a path is a symlink."""
    absolute_path = path.absolute()
    current = Path(absolute_path.anchor)
    for part in absolute_path.parts[1:]:
        current /= part
        if _safe_is_symlink(current):
            return True
    return False


def _is_path_traversal(candidate_str: str) -> bool:
    """Check whether a path string attempts directory traversal outside its root."""
    parts = Path(candidate_str).parts
    return ".." in parts


def _is_overlapping_or_nested(path_a: Path, path_b: Path) -> bool:
    """Check whether two paths are identical or one is an ancestor of the other."""
    try:
        res_a = path_a.resolve()
        res_b = path_b.resolve()
    except OSError:
        return True
    if res_a == res_b:
        return True
    return res_a in res_b.parents or res_b in res_a.parents


def _resolve_anchored_client_path(raw_path: str, anchor_root: Path) -> Path:
    """Anchor a client path without dereferencing symlinks before validation."""
    p = Path(raw_path).expanduser()
    if not p.is_absolute():
        p = anchor_root / p
    return p


def plan_migration(
    home: str | Path | None = None,
    codex_home: str | Path | None = None,
    gemini_home: str | Path | None = None,
    codex_hooks: str | Path | None = None,
    agy_hooks: str | Path | None = None,
) -> MigrationPlan:
    """Produce a deterministic, fully read-only migration plan.

    Detects schema-v1 state, validates layout, and returns a MigrationPlan
    without modifying files, creating backups, updating registry, or reading secrets.
    """
    standard_client_files = sorted([
        "agy:config",
        "agy:current_skills",
        "agy:customization_root",
        "agy:legacy_gemini_md",
        "agy:legacy_skills",
        "codex:config",
        "codex:rules",
        "codex:skills",
    ])
    standard_current_paths = sorted([
        "agy:current_skills",
        "agy:customization_root",
        "ptw:config_yaml",
        "ptw:mcp_dir",
    ])

    # 1. Select every root source, then fail closed before dereferencing it.
    env_home = os.environ.get("PERSONAL_TIDEWAY_HOME")
    raw_home = Path(home or env_home or (Path.home() / DEFAULT_PERSONAL_TIDEWAY_DIR)).expanduser()
    if codex_home is not None:
        raw_codex_home = Path(codex_home).expanduser()
    elif os.environ.get("CODEX_HOME"):
        raw_codex_home = Path(os.environ["CODEX_HOME"]).expanduser()
    else:
        raw_codex_home = Path.home() / ".codex"

    if gemini_home is not None:
        raw_gemini_home = Path(gemini_home).expanduser()
    elif os.environ.get("GEMINI_HOME") or os.environ.get("AGY_HOME"):
        raw_env_gemini = os.environ.get("GEMINI_HOME") or os.environ.get("AGY_HOME")
        raw_gemini_home = Path(raw_env_gemini).expanduser()
    else:
        raw_gemini_home = Path.home() / ".gemini"

    if any(
        _has_symlink_component(root)
        for root in (raw_home, raw_codex_home, raw_gemini_home)
    ):
        return MigrationPlan(
            status=STATUS_BLOCKED,
            source_schema_version=None,
            target_schema_version=SCHEMA_VERSION,
            actions=[],
            backups=[],
            blockers=["Symlink escape detected: root directory is a symbolic link"],
            warnings=[],
            preserve=[],
            legacy_paths=[],
            current_paths=standard_current_paths,
            canonical_mcp=[],
            client_files=standard_client_files,
        )

    resolved_home = raw_home.resolve()
    resolved_codex_home = raw_codex_home.resolve()
    resolved_gemini_home = raw_gemini_home.resolve()

    config_yaml = resolved_home / DEFAULT_CONFIG_YAML

    # 2. Inspect config.yaml with no-follow and fstat to close TOCTOU races
    if _safe_is_symlink(config_yaml):
        return MigrationPlan(
            status=STATUS_BLOCKED,
            source_schema_version=None,
            target_schema_version=SCHEMA_VERSION,
            actions=[],
            backups=[],
            blockers=["Unsafe configuration: config.yaml is a symbolic link"],
            warnings=[],
            preserve=[],
            legacy_paths=[],
            current_paths=standard_current_paths,
            canonical_mcp=[],
            client_files=standard_client_files,
        )

    open_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW

    try:
        fd = os.open(config_yaml, open_flags)
    except FileNotFoundError:
        # Check if legacy artifacts exist under home or client roots
        legacy_under_home = False
        mem_cand = resolved_home / "memory"
        mcp_cand = resolved_home / "mcp"
        if _safe_is_symlink(mem_cand) or mem_cand.exists():
            legacy_under_home = True
        if _safe_is_symlink(mcp_cand) or mcp_cand.exists():
            legacy_under_home = True

        legacy_under_clients = False
        gemini_rules_cand = resolved_gemini_home / "GEMINI.md"
        gemini_skills_cand = resolved_gemini_home / "skills"
        if _safe_is_symlink(gemini_rules_cand) or gemini_rules_cand.exists():
            legacy_under_clients = True
        if _safe_is_symlink(gemini_skills_cand) or gemini_skills_cand.exists():
            legacy_under_clients = True

        if legacy_under_home or legacy_under_clients:
            return MigrationPlan(
                status=STATUS_BLOCKED,
                source_schema_version=None,
                target_schema_version=SCHEMA_VERSION,
                actions=[],
                backups=[],
                blockers=["Ambiguous layout: legacy artifacts present but config.yaml is missing"],
                warnings=[],
                preserve=[],
                legacy_paths=[],
                current_paths=standard_current_paths,
                canonical_mcp=[],
                client_files=standard_client_files,
            )

        return MigrationPlan(
            status=STATUS_NOT_REQUIRED,
            source_schema_version=None,
            target_schema_version=SCHEMA_VERSION,
            actions=[],
            backups=[],
            blockers=[],
            warnings=["No legacy Personal Tideway configuration found to migrate"],
            preserve=[],
            legacy_paths=[],
            current_paths=standard_current_paths,
            canonical_mcp=[],
            client_files=standard_client_files,
        )
    except OSError:
        return MigrationPlan(
            status=STATUS_BLOCKED,
            source_schema_version=None,
            target_schema_version=SCHEMA_VERSION,
            actions=[],
            backups=[],
            blockers=["Unsafe configuration: config.yaml cannot be opened safely"],
            warnings=[],
            preserve=[],
            legacy_paths=[],
            current_paths=standard_current_paths,
            canonical_mcp=[],
            client_files=standard_client_files,
        )

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return MigrationPlan(
                status=STATUS_BLOCKED,
                source_schema_version=None,
                target_schema_version=SCHEMA_VERSION,
                actions=[],
                backups=[],
                blockers=["Unsafe configuration: config.yaml exists but is not a regular file"],
                warnings=[],
                preserve=[],
                legacy_paths=[],
                current_paths=standard_current_paths,
                canonical_mcp=[],
                client_files=standard_client_files,
            )
        if st.st_size > MAX_CONFIG_BYTES:
            return MigrationPlan(
                status=STATUS_BLOCKED,
                source_schema_version=None,
                target_schema_version=SCHEMA_VERSION,
                actions=[],
                backups=[],
                blockers=["Configuration file exceeds maximum permitted size limit"],
                warnings=[],
                preserve=[],
                legacy_paths=[],
                current_paths=standard_current_paths,
                canonical_mcp=[],
                client_files=standard_client_files,
            )
        raw_bytes = os.read(fd, MAX_CONFIG_BYTES + 1)
        if len(raw_bytes) > MAX_CONFIG_BYTES:
            return MigrationPlan(
                status=STATUS_BLOCKED,
                source_schema_version=None,
                target_schema_version=SCHEMA_VERSION,
                actions=[],
                backups=[],
                blockers=["Configuration file exceeds maximum permitted size limit"],
                warnings=[],
                preserve=[],
                legacy_paths=[],
                current_paths=standard_current_paths,
                canonical_mcp=[],
                client_files=standard_client_files,
            )
        raw_text = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return MigrationPlan(
            status=STATUS_BLOCKED,
            source_schema_version=None,
            target_schema_version=SCHEMA_VERSION,
            actions=[],
            backups=[],
            blockers=["Malformed configuration: unable to read config.yaml safely"],
            warnings=[],
            preserve=[],
            legacy_paths=[],
            current_paths=standard_current_paths,
            canonical_mcp=[],
            client_files=standard_client_files,
        )
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    # 3. Safely parse YAML failing closed on duplicate keys or malformed structures
    try:
        file_data = yaml.load(raw_text, Loader=UniqueKeyLoader)
    except (yaml.YAMLError, OSError, UnicodeDecodeError, ValueError, TypeError):
        return MigrationPlan(
            status=STATUS_BLOCKED,
            source_schema_version=None,
            target_schema_version=SCHEMA_VERSION,
            actions=[],
            backups=[],
            blockers=["Malformed configuration: YAML syntax or duplicate key error in config.yaml"],
            warnings=[],
            preserve=[],
            legacy_paths=[],
            current_paths=standard_current_paths,
            canonical_mcp=[],
            client_files=standard_client_files,
        )

    if not isinstance(file_data, dict):
        return MigrationPlan(
            status=STATUS_BLOCKED,
            source_schema_version=None,
            target_schema_version=SCHEMA_VERSION,
            actions=[],
            backups=[],
            blockers=["Malformed configuration: root of config.yaml must be a mapping"],
            warnings=[],
            preserve=[],
            legacy_paths=[],
            current_paths=standard_current_paths,
            canonical_mcp=[],
            client_files=standard_client_files,
        )

    # 4. Validate schema version field
    if "version" not in file_data:
        return MigrationPlan(
            status=STATUS_BLOCKED,
            source_schema_version=None,
            target_schema_version=SCHEMA_VERSION,
            actions=[],
            backups=[],
            blockers=["Malformed configuration: missing required 'version' field in config.yaml"],
            warnings=[],
            preserve=[],
            legacy_paths=[],
            current_paths=standard_current_paths,
            canonical_mcp=[],
            client_files=standard_client_files,
        )

    v = file_data["version"]
    if isinstance(v, bool) or not isinstance(v, int):
        return MigrationPlan(
            status=STATUS_BLOCKED,
            source_schema_version=None,
            target_schema_version=SCHEMA_VERSION,
            actions=[],
            backups=[],
            blockers=["Invalid configuration: 'version' field in config.yaml must be an integer"],
            warnings=[],
            preserve=[],
            legacy_paths=[],
            current_paths=standard_current_paths,
            canonical_mcp=[],
            client_files=standard_client_files,
        )

    if v < 1:
        return MigrationPlan(
            status=STATUS_BLOCKED,
            source_schema_version=v,
            target_schema_version=SCHEMA_VERSION,
            actions=[],
            backups=[],
            blockers=[f"Invalid schema version {v} in config.yaml: version must be >= 1"],
            warnings=[],
            preserve=[],
            legacy_paths=[],
            current_paths=standard_current_paths,
            canonical_mcp=[],
            client_files=standard_client_files,
        )

    if v > SCHEMA_VERSION:
        return MigrationPlan(
            status=STATUS_BLOCKED,
            source_schema_version=v,
            target_schema_version=SCHEMA_VERSION,
            actions=[],
            backups=[],
            blockers=[
                f"Unsupported future schema version {v}: exceeds maximum supported version {SCHEMA_VERSION}"
            ],
            warnings=[],
            preserve=[],
            legacy_paths=[],
            current_paths=standard_current_paths,
            canonical_mcp=[],
            client_files=standard_client_files,
        )

    # 5. Check client_paths for unknown keys, invalid types, and traversal
    file_client_paths: dict[str, Any] = {}
    if "client_paths" in file_data:
        cp = file_data["client_paths"]
        if not isinstance(cp, dict):
            return MigrationPlan(
                status=STATUS_BLOCKED,
                source_schema_version=v,
                target_schema_version=SCHEMA_VERSION,
                actions=[],
                backups=[],
                blockers=["Malformed configuration: 'client_paths' must be a mapping"],
                warnings=[],
                preserve=[],
                legacy_paths=[],
                current_paths=standard_current_paths,
                canonical_mcp=[],
                client_files=standard_client_files,
            )
        for cp_k in cp:
            if cp_k not in SUPPORTED_CLIENT_PATH_KEYS:
                return MigrationPlan(
                    status=STATUS_BLOCKED,
                    source_schema_version=v,
                    target_schema_version=SCHEMA_VERSION,
                    actions=[],
                    backups=[],
                    blockers=["Invalid configuration: unknown key detected in client_paths"],
                    warnings=[],
                    preserve=[],
                    legacy_paths=[],
                    current_paths=standard_current_paths,
                    canonical_mcp=[],
                    client_files=standard_client_files,
                )
        file_client_paths = cp

    for cp_val in file_client_paths.values():
        if cp_val is not None:
            if not isinstance(cp_val, str):
                return MigrationPlan(
                    status=STATUS_BLOCKED,
                    source_schema_version=v,
                    target_schema_version=SCHEMA_VERSION,
                    actions=[],
                    backups=[],
                    blockers=["Invalid configuration: client_paths entry must be a string"],
                    warnings=[],
                    preserve=[],
                    legacy_paths=[],
                    current_paths=standard_current_paths,
                    canonical_mcp=[],
                    client_files=standard_client_files,
                )
            if _is_path_traversal(cp_val):
                return MigrationPlan(
                    status=STATUS_BLOCKED,
                    source_schema_version=v,
                    target_schema_version=SCHEMA_VERSION,
                    actions=[],
                    backups=[],
                    blockers=["Path traversal detected in configured client path"],
                    warnings=[],
                    preserve=[],
                    legacy_paths=[],
                    current_paths=standard_current_paths,
                    canonical_mcp=[],
                    client_files=standard_client_files,
                )

    # 6. Re-resolve client homes if configured in client_paths
    if codex_home is None and file_client_paths.get("codex_home"):
        configured_codex_home = Path(file_client_paths["codex_home"]).expanduser()
        if not configured_codex_home.is_absolute():
            configured_codex_home = resolved_home / configured_codex_home
        if _has_symlink_component(configured_codex_home):
            return MigrationPlan(
                status=STATUS_BLOCKED,
                source_schema_version=v,
                target_schema_version=SCHEMA_VERSION,
                actions=[],
                backups=[],
                blockers=["Symlink escape detected: configured client root is a symbolic link"],
                warnings=[],
                preserve=[],
                legacy_paths=[],
                current_paths=standard_current_paths,
                canonical_mcp=[],
                client_files=standard_client_files,
            )
        resolved_codex_home = configured_codex_home.resolve()

    if gemini_home is None and file_client_paths.get("gemini_home"):
        configured_gemini_home = Path(file_client_paths["gemini_home"]).expanduser()
        if not configured_gemini_home.is_absolute():
            configured_gemini_home = resolved_home / configured_gemini_home
        if _has_symlink_component(configured_gemini_home):
            return MigrationPlan(
                status=STATUS_BLOCKED,
                source_schema_version=v,
                target_schema_version=SCHEMA_VERSION,
                actions=[],
                backups=[],
                blockers=["Symlink escape detected: configured client root is a symbolic link"],
                warnings=[],
                preserve=[],
                legacy_paths=[],
                current_paths=standard_current_paths,
                canonical_mcp=[],
                client_files=standard_client_files,
            )
        resolved_gemini_home = configured_gemini_home.resolve()

    # 7. Check overlap / nested containment across PTW, Codex, and AGY roots
    if _is_overlapping_or_nested(resolved_home, resolved_codex_home):
        return MigrationPlan(
            status=STATUS_BLOCKED,
            source_schema_version=v,
            target_schema_version=SCHEMA_VERSION,
            actions=[],
            backups=[],
            blockers=[
                "Ambiguous layout: Personal Tideway home overlaps or contains Codex home directory"
            ],
            warnings=[],
            preserve=[],
            legacy_paths=[],
            current_paths=standard_current_paths,
            canonical_mcp=[],
            client_files=standard_client_files,
        )

    if _is_overlapping_or_nested(resolved_home, resolved_gemini_home):
        return MigrationPlan(
            status=STATUS_BLOCKED,
            source_schema_version=v,
            target_schema_version=SCHEMA_VERSION,
            actions=[],
            backups=[],
            blockers=[
                "Ambiguous layout: Personal Tideway home overlaps or contains Antigravity home directory"
            ],
            warnings=[],
            preserve=[],
            legacy_paths=[],
            current_paths=standard_current_paths,
            canonical_mcp=[],
            client_files=standard_client_files,
        )

    if _is_overlapping_or_nested(resolved_codex_home, resolved_gemini_home):
        return MigrationPlan(
            status=STATUS_BLOCKED,
            source_schema_version=v,
            target_schema_version=SCHEMA_VERSION,
            actions=[],
            backups=[],
            blockers=[
                "Ambiguous layout: Codex home and Antigravity home overlap or contain one another"
            ],
            warnings=[],
            preserve=[],
            legacy_paths=[],
            current_paths=standard_current_paths,
            canonical_mcp=[],
            client_files=standard_client_files,
        )

    # 8. Probe Canonical MCP files (mcp/*.yaml)
    mcp_dir = resolved_home / "mcp"
    canonical_mcp: list[str] = []
    blockers: list[str] = []
    warnings: list[str] = []

    if _safe_is_symlink(mcp_dir):
        blockers.append("Symlink escape detected: canonical mcp directory is a symbolic link")
    elif mcp_dir.exists():
        if not mcp_dir.is_dir():
            blockers.append("Unsafe artifact: canonical mcp exists but is not a directory")
        else:
            try:
                seen_mcp_identifiers: set[str] = set()
                for entry in sorted(mcp_dir.iterdir(), key=lambda p: p.name):
                    if _safe_is_symlink(entry):
                        blockers.append(
                            "Symlink escape detected: mcp entry is a symbolic link"
                        )
                    elif not entry.is_file():
                        blockers.append(
                            "Unsafe artifact: mcp entry exists but is not a regular file"
                        )
                    elif entry.name.endswith(".yaml") or entry.name.endswith(".yml"):
                        if SAFE_IDENTIFIER_RE.match(entry.stem):
                            if entry.stem in seen_mcp_identifiers:
                                blockers.append(
                                    "Ambiguous artifact: duplicate canonical mcp definition identifier"
                                )
                            else:
                                seen_mcp_identifiers.add(entry.stem)
                                canonical_mcp.append(f"mcp:{entry.stem}")
                        else:
                            blockers.append(
                                "Unsafe artifact: canonical mcp definition has invalid identifier"
                            )
            except OSError:
                blockers.append("Permission error or unreadable canonical mcp directory")

    # 9. Resolve custom client paths with documented precedence
    agy_customization_root = resolved_gemini_home / "config"
    agy_current_skills = agy_customization_root / "skills"

    # Codex config
    if file_client_paths.get("codex_config"):
        codex_config = _resolve_anchored_client_path(file_client_paths["codex_config"], resolved_codex_home)
    else:
        codex_config = resolved_codex_home / "config.toml"

    # Codex rules
    if file_client_paths.get("codex_rules"):
        codex_rules = _resolve_anchored_client_path(file_client_paths["codex_rules"], resolved_codex_home)
    else:
        codex_rules = resolved_codex_home / "AGENTS.md"

    # Codex hooks: explicit arg > config.yaml > default
    if codex_hooks is not None:
        c_hooks = _resolve_anchored_client_path(str(codex_hooks), resolved_codex_home)
    elif file_client_paths.get("codex_hooks"):
        c_hooks = _resolve_anchored_client_path(file_client_paths["codex_hooks"], resolved_codex_home)
    else:
        c_hooks = resolved_codex_home / "hooks.json"

    # AGY config
    if file_client_paths.get("agy_config"):
        agy_config = _resolve_anchored_client_path(file_client_paths["agy_config"], agy_customization_root)
    else:
        agy_config = agy_customization_root / "mcp_config.json"

    # AGY hooks: explicit arg > config.yaml > default
    if agy_hooks is not None:
        a_hooks = _resolve_anchored_client_path(str(agy_hooks), agy_customization_root)
    elif file_client_paths.get("agy_hooks"):
        a_hooks = _resolve_anchored_client_path(file_client_paths["agy_hooks"], agy_customization_root)
    else:
        a_hooks = agy_customization_root / "hooks.json"

    # AGY rules candidate under customization root
    custom_agy_rules: Path | None = None
    if file_client_paths.get("agy_rules"):
        custom_agy_rules = _resolve_anchored_client_path(file_client_paths["agy_rules"], agy_customization_root)

    # 10. Validate client file hazards for BOTH v1 and v2
    client_artifacts_to_validate: list[tuple[str, Path, bool]] = [
        ("codex:config", codex_config, True),
        ("codex:rules", codex_rules, True),
        ("codex:hooks", c_hooks, True),
        ("codex:skills", resolved_codex_home / "skills", False),
        ("agy:customization_root", agy_customization_root, False),
        ("agy:current_skills", agy_current_skills, False),
        ("agy:config", agy_config, True),
        ("agy:hooks", a_hooks, True),
    ]
    if custom_agy_rules is not None:
        client_artifacts_to_validate.append(("agy:rules", custom_agy_rules, True))

    for sym_id, art_path, is_expect_file in client_artifacts_to_validate:
        if _has_symlink_component(art_path):
            blockers.append("Symlink escape detected: client artifact is a symbolic link")
        elif art_path.exists():
            if is_expect_file and not art_path.is_file():
                blockers.append("Unsafe artifact: client artifact exists but is not a regular file")
            elif not is_expect_file and not art_path.is_dir():
                blockers.append("Unsafe artifact: client artifact exists but is not a directory")

    # Validate all supported current AGY rule candidates for both schema versions.
    rule_candidate_specs: list[tuple[Path, bool]] = [
        (agy_customization_root / "rules", False),
        (agy_customization_root / "AGENTS.md", True),
        (agy_customization_root / "GEMINI.md", True),
    ]
    if custom_agy_rules is not None:
        rule_candidate_specs.append((custom_agy_rules, True))

    unique_rule_candidates: dict[Path, tuple[Path, bool]] = {}
    for candidate, expects_file in rule_candidate_specs:
        unique_rule_candidates.setdefault(candidate, (candidate, expects_file))

    existing_current_rule_targets: list[Path] = []
    for candidate, expects_file in unique_rule_candidates.values():
        if _has_symlink_component(candidate):
            blockers.append("Symlink escape detected: current agy rules target is a symbolic link")
        elif candidate.exists():
            if expects_file and not candidate.is_file():
                blockers.append(
                    "Unsafe artifact: current agy rules target exists but is not a regular file"
                )
            elif not expects_file and not candidate.is_dir():
                blockers.append(
                    "Unsafe artifact: current agy rules target exists but is not a directory"
                )
            else:
                existing_current_rule_targets.append(candidate)

    if len(existing_current_rule_targets) > 1:
        blockers.append(
            "Ambiguous layout: multiple current-root rule candidates exist under customization root"
        )

    # If schema is v2, complete validation and return
    if v == SCHEMA_VERSION:
        if blockers:
            return MigrationPlan(
                status=STATUS_BLOCKED,
                source_schema_version=v,
                target_schema_version=SCHEMA_VERSION,
                actions=[],
                backups=[],
                blockers=sorted(blockers),
                warnings=sorted(warnings),
                preserve=[],
                legacy_paths=[],
                current_paths=standard_current_paths,
                canonical_mcp=sorted(canonical_mcp),
                client_files=standard_client_files,
            )
        return MigrationPlan(
            status=STATUS_NOT_REQUIRED,
            source_schema_version=v,
            target_schema_version=SCHEMA_VERSION,
            actions=[],
            backups=[],
            blockers=[],
            warnings=[],
            preserve=[],
            legacy_paths=[],
            current_paths=standard_current_paths,
            canonical_mcp=sorted(canonical_mcp),
            client_files=standard_client_files,
        )

    # 11. Schema v1: validate legacy memory directory
    memory_dir = resolved_home / "memory"
    legacy_memory_found = False
    if _safe_is_symlink(memory_dir):
        blockers.append("Symlink escape detected: legacy memory directory is a symbolic link")
    elif memory_dir.exists():
        if not memory_dir.is_dir():
            blockers.append("Unsafe artifact: legacy memory exists but is not a directory")
        else:
            try:
                legacy_memory_found = any(True for _ in memory_dir.iterdir())
            except OSError:
                blockers.append("Permission error or unreadable legacy memory directory")
    else:
        warnings.append("Legacy memory directory not present; no memory archive needed")

    # 12. Validate legacy AGY GEMINI.md
    agy_legacy_gemini_md = resolved_gemini_home / "GEMINI.md"
    agy_legacy_rules_found = False
    if _safe_is_symlink(agy_legacy_gemini_md):
        blockers.append("Symlink escape detected: legacy GEMINI.md is a symbolic link")
    elif agy_legacy_gemini_md.exists():
        if not agy_legacy_gemini_md.is_file():
            blockers.append("Unsafe artifact: legacy GEMINI.md exists but is not a regular file")
        else:
            agy_legacy_rules_found = True
    else:
        warnings.append("Optional legacy artifact absent: agy_legacy_gemini_md not found")

    # 13. Validate legacy AGY skills directory
    agy_legacy_skills = resolved_gemini_home / "skills"
    agy_legacy_skills_found = False
    legacy_skill_names: set[str] = set()
    if _safe_is_symlink(agy_legacy_skills):
        blockers.append("Symlink escape detected: legacy agy skills is a symbolic link")
    elif agy_legacy_skills.exists():
        if not agy_legacy_skills.is_dir():
            blockers.append("Unsafe artifact: legacy agy skills exists but is not a directory")
        else:
            agy_legacy_skills_found = True
            try:
                for entry in agy_legacy_skills.iterdir():
                    if _safe_is_symlink(entry):
                        blockers.append(
                            "Symlink escape detected: legacy skill entry is a symbolic link"
                        )
                    else:
                        legacy_skill_names.add(entry.name)
            except OSError:
                blockers.append("Permission error or unreadable legacy agy skills directory")
    else:
        warnings.append("Optional legacy artifact absent: agy_legacy_skills not found")

    # 14. Detect Source/Destination Conflict: Rules
    if agy_legacy_rules_found and existing_current_rule_targets:
        blockers.append(
            "Source and destination conflict detected: legacy AGY rules and current customization rules both exist"
        )

    # 15. Detect Source/Destination Conflict: Skills
    if agy_current_skills.exists() and agy_current_skills.is_dir():
        try:
            current_skill_names: set[str] = set()
            for entry in agy_current_skills.iterdir():
                if _safe_is_symlink(entry):
                    blockers.append(
                        "Symlink escape detected: current agy skill entry is a symbolic link"
                    )
                else:
                    current_skill_names.add(entry.name)
            if agy_legacy_skills_found and (legacy_skill_names.intersection(current_skill_names) or (legacy_skill_names and current_skill_names)):
                blockers.append(
                    "Source and destination conflict detected: skills exist in both legacy and current roots"
                )
        except OSError:
            blockers.append("Permission error inspecting current agy skills directory")

    if blockers:
        return MigrationPlan(
            status=STATUS_BLOCKED,
            source_schema_version=v,
            target_schema_version=SCHEMA_VERSION,
            actions=[],
            backups=[],
            blockers=sorted(blockers),
            warnings=sorted(warnings),
            preserve=[],
            legacy_paths=[],
            current_paths=standard_current_paths,
            canonical_mcp=sorted(canonical_mcp),
            client_files=standard_client_files,
        )

    # 16. Build Ready migration plan
    legacy_paths: list[str] = []
    if agy_legacy_rules_found:
        legacy_paths.append("agy:legacy_gemini_md")
    if agy_legacy_skills_found:
        legacy_paths.append("agy:legacy_skills")
    if legacy_memory_found or memory_dir.exists():
        legacy_paths.append("ptw:memory_dir")

    preserve_items = [
        "agy:unmanaged_rules",
        "agy:unmanaged_skills",
        "client:unmanaged_files",
        "codex:unmanaged_rules",
        "codex:unmanaged_skills",
        "ptw:canonical_mcp",
    ]
    preserve_items.extend(canonical_mcp)
    if legacy_memory_found or memory_dir.exists():
        preserve_items.append("ptw:legacy_memory")

    backup_items = ["ptw:config_yaml"]
    if legacy_memory_found or memory_dir.exists():
        backup_items.append("ptw:memory_dir")
    if agy_legacy_rules_found:
        backup_items.append("agy:legacy_gemini_md")
    if agy_legacy_skills_found:
        backup_items.append("agy:legacy_skills")
    if agy_config.exists() and agy_config.is_file():
        backup_items.append("agy:config")
    if codex_config.exists() and codex_config.is_file():
        backup_items.append("codex:config")
    if codex_rules.exists() and codex_rules.is_file():
        backup_items.append("codex:rules")

    actions = [
        "01_backup_v1_configuration_and_client_files",
        "02_initialize_canonical_v2_directories",
        "03_migrate_config_schema_to_v2",
    ]
    if agy_legacy_rules_found:
        actions.append("04_relocate_agy_rules_to_customization_root")
    if agy_legacy_skills_found:
        actions.append("05_relocate_agy_skills_to_customization_root")
    if legacy_memory_found or memory_dir.exists():
        actions.append("06_archive_legacy_memory_to_v2_storage")
    actions.extend([
        "07_preserve_canonical_mcp_definitions",
        "08_preserve_unmanaged_client_rules_and_skills",
    ])

    return MigrationPlan(
        status=STATUS_READY,
        source_schema_version=v,
        target_schema_version=SCHEMA_VERSION,
        actions=sorted(actions),
        backups=sorted(backup_items),
        blockers=[],
        warnings=sorted(warnings),
        preserve=sorted(preserve_items),
        legacy_paths=sorted(legacy_paths),
        current_paths=standard_current_paths,
        canonical_mcp=sorted(canonical_mcp),
        client_files=standard_client_files,
    )
