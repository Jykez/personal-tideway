"""Workspace initialization and structure verification."""

import json
import os
from pathlib import Path
import yaml

from personal_tideway.config import PersonalTidewayConfig, validate_owned_path
from personal_tideway.constants import (
    DEFAULT_CONFIG_YAML,
    DEFAULT_SECRETS_ENV,
    SCHEMA_VERSION,
)
from personal_tideway.exceptions import ConfigError
from personal_tideway.utils import atomic_write_text


DEFAULT_SECRETS_TEMPLATE = """# Personal Tideway Secrets
# Strict KEY=VALUE lines only. Mode 0600.
# Example:
# OPENAI_API_KEY=sk-...
"""

DEFAULT_PROJECTS_REGISTRY_TEMPLATE = f"""# Personal Tideway Projects Registry
version: {SCHEMA_VERSION}
projects: []
"""


def init_workspace(cfg: PersonalTidewayConfig) -> None:
    """Initialize canonical Personal Tideway v2 central directory tree, secrets.env, config.yaml, and registry."""
    if cfg.version != SCHEMA_VERSION:
        raise ConfigError(
            f"Cannot initialize workspace with version {cfg.version}. Workspace requires migration to version {SCHEMA_VERSION}."
        )

    # List of all canonical directories
    canonical_dirs = [
        (cfg.home, True),
        (cfg.registry_dir, False),
        (cfg.projects_dir, False),
        (cfg.knowledge_dir, False),
        (cfg.knowledge_personal_dir, False),
        (cfg.mcp_dir, False),
        (cfg.rules_dir, False),
        (cfg.rules_shared, False),
        (cfg.rules_codex, False),
        (cfg.rules_agy, False),
        (cfg.skills_dir, False),
        (cfg.skills_shared, False),
        (cfg.skills_codex, False),
        (cfg.skills_agy, False),
        (cfg.services_dir, False),
        (cfg.basic_memory_dir, False),
        (cfg.state_dir, False),
        (cfg.conflicts_dir, False),
        (cfg.backups_dir, False),
        (cfg.locks_dir, False),
    ]

    # List of all canonical files
    canonical_files = [
        cfg.secrets_env,
        cfg.config_yaml,
        cfg.projects_yaml,
        cfg.state_file,
    ]

    # --- Preflight Phase ---
    # Validate cfg.home and every canonical directory target
    for d, allow_root in canonical_dirs:
        validate_owned_path(d, root=cfg.home, allow_root=allow_root)

        # Check existing path: workspace root itself can be a symlink, but child layout must not be a symlink
        if not allow_root and d.is_symlink():
            raise ConfigError(f"Directory target '{d}' is a symlink, which is not permitted in child layout.")

        if d.exists() and not d.is_dir():
            raise ConfigError(f"Directory target '{d}' exists but is not a directory.")

    # Validate every canonical file target
    for f in canonical_files:
        validate_owned_path(f, root=cfg.home, allow_root=False)

        if f.is_symlink():
            raise ConfigError(f"File target '{f}' is a symlink, which is not permitted in child layout.")

        if f.exists() and not f.is_file():
            raise ConfigError(f"File target '{f}' exists but is not a regular file.")

    # --- Mutation Phase ---
    # 1. Create canonical v2 directory tree
    for d, _ in canonical_dirs:
        d.mkdir(parents=True, exist_ok=True)

    # 2. Initialize secrets.env with mode 0600 (NEVER overwritten by init)
    if not cfg.secrets_env.exists():
        atomic_write_text(cfg.secrets_env, DEFAULT_SECRETS_TEMPLATE, mode=0o600)
    else:
        # Ensure existing secrets.env maintains strict 0600 permissions
        try:
            os.chmod(cfg.secrets_env, 0o600)
        except OSError:
            pass

    # 3. Write config.yaml if missing
    if not cfg.config_yaml.exists():
        yaml_content = yaml.safe_dump(cfg.to_dict(), sort_keys=False)
        atomic_write_text(cfg.config_yaml, yaml_content)

    # 4. Initialize registry/projects.yaml if missing
    if not cfg.projects_yaml.exists():
        atomic_write_text(cfg.projects_yaml, DEFAULT_PROJECTS_REGISTRY_TEMPLATE)

    # 5. Initialize state file if missing
    if not cfg.state_file.exists():
        initial_state = {
            "version": SCHEMA_VERSION,
            "objects": {},
        }
        atomic_write_text(
            cfg.state_file,
            json.dumps(initial_state, indent=2, sort_keys=True) + "\n",
        )
