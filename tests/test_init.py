"""Tests for Personal Tideway workspace initialization and idempotency."""

import os
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from personal_tideway.cli.main import main
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import (
    DEFAULT_CONFIG_YAML,
    DEFAULT_SECRETS_ENV,
    SCHEMA_VERSION,
    ExitCode,
)
from personal_tideway.core.basic_memory_installer import BasicMemoryRunnerResult
from personal_tideway.core.basic_memory_runtime import (
    BASIC_MEMORY_PINNED_VERSION,
    ENV_BASIC_MEMORY_CONFIG_DIR,
    get_basic_memory_layout,
)
from personal_tideway.core.mcp import load_mcp_server
from personal_tideway.core.workspace import init_workspace


def _create_mock_uv(tmp_path: Path) -> Path:
    uv_executable = tmp_path / "uv"
    uv_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    uv_executable.chmod(0o755)
    return uv_executable


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
    assert (cfg.rules_shared / "continuity.md").is_file()
    assert (cfg.skills_shared / "continuity" / "SKILL.md").is_file()

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


def test_init_idempotency_does_not_chmod_converged_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A converged repeated init must not touch secrets.env metadata."""
    home = tmp_path / "ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)
    real_chmod = os.chmod
    chmod_targets: list[Path] = []

    def chmod_spy(path: str | os.PathLike[str], mode: int) -> None:
        chmod_targets.append(Path(path))
        real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", chmod_spy)
    init_workspace(cfg)

    assert cfg.secrets_env not in chmod_targets


def test_cli_init_command(tmp_path: Path):
    """Test 'ptw init' command via CLI entrypoint creating v2 files."""
    home = tmp_path / "cli_ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)
    mock_uv = _create_mock_uv(tmp_path)
    layout = get_basic_memory_layout(cfg)

    def runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        del env, timeout
        if argv[0] == str(mock_uv.resolve()):
            layout.primary_executable.parent.mkdir(parents=True, exist_ok=True)
            layout.primary_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            layout.primary_executable.chmod(0o755)
            return BasicMemoryRunnerResult(returncode=0)
        return BasicMemoryRunnerResult(
            returncode=0,
            stdout=f"basic-memory {BASIC_MEMORY_PINNED_VERSION}\n",
        )

    code = main(
        ["--home", str(home), "init", "--uv-executable", str(mock_uv)],
        runner=runner,
    )
    assert code == ExitCode.SUCCESS
    assert (home / DEFAULT_CONFIG_YAML).is_file()
    assert (home / DEFAULT_SECRETS_ENV).is_file()
    assert (home / "registry" / "projects.yaml").is_file()
    assert layout.config_file.is_file()
    assert layout.primary_executable.is_file()
    mcp_server = load_mcp_server(cfg.mcp_dir / "basic-memory.yaml")
    assert mcp_server.command == str(layout.primary_executable)
    assert mcp_server.args == ["mcp", "--transport", "stdio"]
    assert mcp_server.targets == ["codex", "agy"]
    assert mcp_server.tags == ["core-managed", "basic-memory"]
    assert mcp_server.env[ENV_BASIC_MEMORY_CONFIG_DIR] == str(layout.config_dir)


def test_cli_init_dry_run_is_exact_and_non_mutating(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Fresh-init preview сообщает центральные actions и не создаёт ни одного path."""
    home = tmp_path / "cli_ptw_dry_run"
    mock_uv = _create_mock_uv(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        del env, timeout
        calls.append(argv)
        return BasicMemoryRunnerResult(returncode=0)

    code = main(
        [
            "--home",
            str(home),
            "init",
            "--dry-run",
            "--uv-executable",
            str(mock_uv),
        ],
        runner=runner,
    )

    assert code == ExitCode.SUCCESS
    assert not home.exists()
    assert calls == []
    output = capsys.readouterr().out
    assert "[DRY RUN] Personal Tideway initialization" in output
    assert "create_directory:." in output
    assert "create_file:config.yaml" in output
    assert "create_file:secrets.env" in output
    assert "[DRY RUN] Basic Memory isolated runtime" in output
    assert "install_if_health_check_fails:" in output
    assert "basic-memory==0.23.2" in output
    assert "health_check:" in output
    assert "create_mcp:" in output


def test_cli_init_rejects_invalid_uv_before_workspace_mutation(tmp_path: Path):
    """A failed Basic Memory preflight must leave a fresh workspace absent."""
    home = tmp_path / "cli_ptw_invalid_uv"
    missing_uv = tmp_path / "missing-uv"

    code = main(
        [
            "--home",
            str(home),
            "init",
            "--uv-executable",
            str(missing_uv),
        ]
    )

    assert code == ExitCode.VALIDATION_ERROR
    assert not home.exists()


def test_init_dry_run_reports_mode_repair_without_changing_it(tmp_path: Path):
    """Preview видит небезопасный mode secrets.env, но не исправляет его сам."""
    home = tmp_path / "ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)
    cfg.secrets_env.chmod(0o644)

    actions = init_workspace(cfg, dry_run=True)

    assert actions == ["set_mode:secrets.env:0600"]
    assert stat.S_IMODE(cfg.secrets_env.stat().st_mode) == 0o644


def test_init_rejects_hardlinked_canonical_file_without_mutation(tmp_path: Path):
    """Canonical files with ambiguous inode ownership must fail before chmod or writes."""
    from personal_tideway.exceptions import ConfigError

    home = tmp_path / "ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)
    cfg.secrets_env.chmod(0o644)
    external_link = tmp_path / "external-secrets-link"
    os.link(cfg.secrets_env, external_link)
    before = cfg.secrets_env.read_bytes()

    with pytest.raises(ConfigError, match="invalid link count"):
        init_workspace(cfg, dry_run=True)

    assert cfg.secrets_env.read_bytes() == before
    assert stat.S_IMODE(cfg.secrets_env.stat().st_mode) == 0o644
    assert external_link.read_bytes() == before


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink"])
def test_cli_init_rejects_unsafe_existing_basic_memory_mcp(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    unsafe_kind: str,
):
    """Init must not trust a linked core-managed MCP definition or expose its bytes."""
    home = tmp_path / "ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)
    mock_uv = _create_mock_uv(tmp_path)
    target = cfg.mcp_dir / "basic-memory.yaml"
    outside = tmp_path / "outside-definition.yaml"
    secret = "PRIVATE_SENTINEL_MUST_NOT_APPEAR"
    outside.write_text(secret, encoding="utf-8")
    if unsafe_kind == "symlink":
        target.symlink_to(outside)
    else:
        os.link(outside, target)

    code = main(
        [
            "--home",
            str(home),
            "init",
            "--dry-run",
            "--uv-executable",
            str(mock_uv),
        ]
    )

    out, err = capsys.readouterr()
    assert code == ExitCode.VALIDATION_ERROR
    assert secret not in out
    assert secret not in err
    assert outside.read_text(encoding="utf-8") == secret
    if unsafe_kind == "symlink":
        assert target.is_symlink()
    else:
        assert target.stat().st_ino == outside.stat().st_ino


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
