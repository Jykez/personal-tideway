"""Tests for Personal Tideway workspace initialization and idempotency."""

import os
from pathlib import Path
import pytest
import yaml

from personal_tideway.cli.main import main
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import DEFAULT_CONFIG_YAML, DEFAULT_SECRETS_ENV, ExitCode
from personal_tideway.core.workspace import init_workspace


def test_init_canonical_tree(tmp_path: Path):
    """Test full v2 directory structure created under configurable PERSONAL_TIDEWAY_HOME."""
    home = tmp_path / "custom_ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)

    init_workspace(cfg)

    # Check v2 canonical directories
    assert cfg.home.is_dir()
    assert cfg.registry_dir.is_dir()
    assert cfg.projects_dir.is_dir()
    assert cfg.knowledge_dir.is_dir()
    assert cfg.knowledge_personal_dir.is_dir()
    assert cfg.personal_knowledge_dir.is_dir()
    assert cfg.mcp_dir.is_dir()
    assert cfg.rules_dir.is_dir()
    assert cfg.rules_shared.is_dir()
    assert cfg.rules_codex.is_dir()
    assert cfg.rules_agy.is_dir()
    assert cfg.skills_dir.is_dir()
    assert cfg.skills_shared.is_dir()
    assert cfg.skills_codex.is_dir()
    assert cfg.skills_agy.is_dir()
    assert cfg.services_dir.is_dir()
    assert cfg.basic_memory_dir.is_dir()
    assert cfg.services_basic_memory_dir.is_dir()
    assert cfg.state_dir.is_dir()
    assert cfg.conflicts_dir.is_dir()
    assert cfg.backups_dir.is_dir()
    assert cfg.locks_dir.is_dir()

    # Legacy directories must not be created
    assert not (cfg.home / "memory").exists()
    assert not (cfg.home / "templates").exists()
    assert not (cfg.home / "personal-tideway.yaml").exists()

    # Check files
    assert cfg.config_yaml.is_file()
    assert cfg.personal_tideway_yaml.is_file()
    assert cfg.secrets_env.is_file()
    assert cfg.projects_yaml.is_file()
    assert cfg.state_file.is_file()

    # Check secrets.env permissions mode 0600
    stat_mode = cfg.secrets_env.stat().st_mode & 0o777
    assert stat_mode == 0o600, f"Expected 0600 mode on secrets.env, got {oct(stat_mode)}"

    # Check config.yaml contents (v2 schema without user-specific paths)
    with open(cfg.config_yaml, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data["version"] == 2
    assert "client_paths" in data
    assert data["client_paths"] == {}
    assert data["skill_link_mode"] == "symlink"

    # Check projects.yaml contents
    with open(cfg.projects_yaml, "r", encoding="utf-8") as f:
        proj_data = yaml.safe_load(f)
    assert proj_data["version"] == 2
    assert "projects" in proj_data


def test_init_idempotency_preserves_secrets(tmp_path: Path):
    """Test that secrets.env is never overwritten by init and permissions preserved."""
    home = tmp_path / "ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)

    # First init
    init_workspace(cfg)

    # Put custom secret in secrets.env
    secret_line = "CUSTOM_API_KEY=secret_value_12345\n"  # pragma: allowlist secret
    cfg.secrets_env.write_text(secret_line, encoding="utf-8")

    # Second init
    init_workspace(cfg)

    # Verify secret is intact and mode is still 0600
    content = cfg.secrets_env.read_text(encoding="utf-8")
    assert secret_line in content
    assert (cfg.secrets_env.stat().st_mode & 0o777) == 0o600


def test_cli_init_command(tmp_path: Path):
    """Test 'ptw init' command via CLI entrypoint creating v2 files."""
    home = tmp_path / "cli_ptw"
    code = main(["--home", str(home), "init"])
    assert code == ExitCode.SUCCESS
    assert (home / DEFAULT_CONFIG_YAML).is_file()
    assert (home / DEFAULT_SECRETS_ENV).is_file()
    assert (home / "registry" / "projects.yaml").is_file()


def test_init_regression_creates_no_forbidden_artifacts_in_source_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Regression test: Central workspace initialization must never create artifacts in project/source root."""
    ptw_home = tmp_path / "central_workspace"
    project_source = tmp_path / "my_source_repo"
    project_source.mkdir(parents=True, exist_ok=True)

    # Change current working directory to project/source root to verify execution context safety
    monkeypatch.chdir(project_source)

    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    init_workspace(cfg)

    # Check forbidden project-local artifacts
    assert not (project_source / ".personal-tideway").exists()
    assert not (project_source / ".personal-tideway.yaml").exists()
    assert not (project_source / ".basic-memory").exists()
    assert not (project_source / "AGENTS.md").exists()
    assert not (project_source / "GEMINI.md").exists()
    assert list(project_source.iterdir()) == []


def test_init_symlink_escape_fails_and_leaves_outside_directory_unchanged(tmp_path: Path):
    """Pre-existing symlink in workspace pointing outside must be rejected and outside must not be modified."""
    from personal_tideway.exceptions import BoundaryError

    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    outside = tmp_path / "outside_dir"
    outside.mkdir(parents=True)
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("sentinel_data", encoding="utf-8")

    # Create pre-existing symlink escaping workspace
    os.symlink(outside, home / "registry")

    cfg = PersonalTidewayConfig.resolve(home=home)
    with pytest.raises(BoundaryError) as exc_info:
        init_workspace(cfg)
    assert "outside workspace root" in str(exc_info.value)

    # Assert outside directory remains completely unchanged
    assert list(outside.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "sentinel_data"
    assert not (outside / "projects.yaml").exists()


def test_init_refuses_older_or_mismatched_version_without_mutation(tmp_path: Path):
    """init_workspace must refuse to mutate when cfg.version != SCHEMA_VERSION with ConfigError."""
    from personal_tideway.exceptions import ConfigError

    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    cfg = PersonalTidewayConfig.resolve(home=home)
    assert cfg.version == 1

    with pytest.raises(ConfigError) as exc_info:
        init_workspace(cfg)
    assert "requires migration" in str(exc_info.value)
    assert "version 2" in str(exc_info.value) or f"version {SCHEMA_VERSION}" in str(exc_info.value)

    # Ensure no mutation
    assert cfg_file.read_text(encoding="utf-8") == "version: 1\nclient_paths: {}\n"
    assert not (home / "registry").exists()
    assert not (home / DEFAULT_SECRETS_ENV).exists()


def test_init_generates_schema_version_in_projects_and_state(tmp_path: Path):
    """projects.yaml and state.json schema versions are generated from SCHEMA_VERSION."""
    import json
    from personal_tideway.constants import SCHEMA_VERSION

    home = tmp_path / "ptw_home"
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)

    with open(cfg.projects_yaml, "r", encoding="utf-8") as f:
        proj_data = yaml.safe_load(f)
    assert proj_data["version"] == SCHEMA_VERSION

    with open(cfg.state_file, "r", encoding="utf-8") as f:
        state_data = json.load(f)
    assert state_data["version"] == SCHEMA_VERSION


def test_init_preflight_escaping_symlink_leaves_layout_unmodified(tmp_path: Path):
    """If a late target escapes via symlink, preflight rejects before creating any directories or files."""
    from personal_tideway.exceptions import BoundaryError

    home = tmp_path / "ptw_home"
    outside = tmp_path / "outside_escape"
    outside.mkdir(parents=True)

    # State directory is symlinked to outside
    # Create home directory first (or let it be absent; here home doesn't exist yet, but let's test with parent created)
    home.mkdir(parents=True)
    os.symlink(outside, home / "state")

    cfg = PersonalTidewayConfig.resolve(home=home)
    with pytest.raises(BoundaryError):
        init_workspace(cfg)

    # Assert earlier absent directories/files were not created
    assert not (home / "registry").exists()
    assert not (home / "projects").exists()
    assert not (home / "knowledge").exists()
    assert not (home / "mcp").exists()
    assert not (home / "rules").exists()
    assert not (home / "skills").exists()
    assert not (home / "services").exists()
    assert not (home / DEFAULT_CONFIG_YAML).exists()
    assert not (home / DEFAULT_SECRETS_ENV).exists()


def test_init_preflight_rejects_wrong_object_types(tmp_path: Path):
    """Preflight rejects wrong existing object types and child symlinks with ConfigError without mutation."""
    from personal_tideway.exceptions import ConfigError

    # 1. Existing secrets.env as directory must be rejected without chmod or mutation
    home = tmp_path / "ptw_secrets_dir"
    home.mkdir(parents=True)
    secrets_as_dir = home / DEFAULT_SECRETS_ENV
    secrets_as_dir.mkdir(parents=True)

    cfg = PersonalTidewayConfig.resolve(home=home)
    with pytest.raises(ConfigError) as exc_info:
        init_workspace(cfg)
    assert "not a regular file" in str(exc_info.value)
    assert secrets_as_dir.is_dir()
    assert not (home / "registry").exists()
    assert not (home / DEFAULT_CONFIG_YAML).exists()

    # 2. Existing child directory target as regular file
    home2 = tmp_path / "ptw_reg_file"
    home2.mkdir(parents=True)
    reg_as_file = home2 / "registry"
    reg_as_file.write_text("not a dir", encoding="utf-8")

    cfg2 = PersonalTidewayConfig.resolve(home=home2)
    with pytest.raises(ConfigError) as exc_info:
        init_workspace(cfg2)
    assert "not a directory" in str(exc_info.value)
    assert not (home2 / "projects").exists()
    assert not (home2 / DEFAULT_CONFIG_YAML).exists()

    # 3. Child symlink pointing inside workspace is rejected
    home3 = tmp_path / "ptw_child_symlink"
    home3.mkdir(parents=True)
    dummy_dir = home3 / "dummy"
    dummy_dir.mkdir()
    os.symlink(dummy_dir, home3 / "skills")

    cfg3 = PersonalTidewayConfig.resolve(home=home3)
    with pytest.raises(ConfigError) as exc_info:
        init_workspace(cfg3)
    assert "is a symlink, which is not permitted" in str(exc_info.value)
    assert not (home3 / "registry").exists()
    assert not (home3 / DEFAULT_CONFIG_YAML).exists()
