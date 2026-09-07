"""Tests for Personal Tideway v2 configuration schema, precedence, and path boundaries."""

import os
from pathlib import Path
import pytest
import yaml

from personal_tideway.config import PersonalTidewayConfig, validate_owned_path
from personal_tideway.constants import (
    DEFAULT_CONFIG_YAML,
    DEFAULT_PERSONAL_TIDEWAY_DIR,
    SCHEMA_VERSION,
    SKILL_LINK_COPY,
    SKILL_LINK_SYMLINK,
)
from personal_tideway.exceptions import BoundaryError, ConfigError, ValidationError


# ============================================================================
# 1. Precedence tests: CLI > Env > File > Default
# ============================================================================

def test_precedence_home_cli_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Explicit CLI argument takes precedence over environment variable."""
    env_home = tmp_path / "env_home"
    cli_home = tmp_path / "cli_home"
    monkeypatch.setenv("PERSONAL_TIDEWAY_HOME", str(env_home))

    cfg = PersonalTidewayConfig.resolve(home=cli_home)
    assert cfg.home == cli_home.resolve()


def test_precedence_home_env_over_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Environment variable takes precedence over default location."""
    env_home = tmp_path / "env_home"
    monkeypatch.setenv("PERSONAL_TIDEWAY_HOME", str(env_home))

    cfg = PersonalTidewayConfig.resolve()
    assert cfg.home == env_home.resolve()


def test_precedence_home_default(monkeypatch: pytest.MonkeyPatch):
    """When neither arg nor env is given, default ~/.personal-tideway is used."""
    monkeypatch.delenv("PERSONAL_TIDEWAY_HOME", raising=False)
    expected_default = (Path.home() / DEFAULT_PERSONAL_TIDEWAY_DIR).resolve()

    cfg = PersonalTidewayConfig.resolve()
    assert cfg.home == expected_default


def test_precedence_codex_home_full_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Precedence chain for codex_home: CLI > env > config.yaml > default."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    file_codex = tmp_path / "file_codex"
    env_codex = tmp_path / "env_codex"
    cli_codex = tmp_path / "cli_codex"

    # Step 1: Default
    monkeypatch.delenv("CODEX_HOME", raising=False)
    cfg_default = PersonalTidewayConfig.resolve(home=home)
    assert cfg_default.codex_home == (Path.home() / ".codex").resolve()

    # Step 2: In config.yaml
    config_file = home / DEFAULT_CONFIG_YAML
    config_file.write_text(yaml.safe_dump({
        "version": SCHEMA_VERSION,
        "client_paths": {"codex_home": str(file_codex)},
    }), encoding="utf-8")
    cfg_file = PersonalTidewayConfig.resolve(home=home)
    assert cfg_file.codex_home == file_codex.resolve()

    # Step 3: Env overrides config.yaml
    monkeypatch.setenv("CODEX_HOME", str(env_codex))
    cfg_env = PersonalTidewayConfig.resolve(home=home)
    assert cfg_env.codex_home == env_codex.resolve()

    # Step 4: CLI overrides env
    cfg_cli = PersonalTidewayConfig.resolve(home=home, codex_home=cli_codex)
    assert cfg_cli.codex_home == cli_codex.resolve()


def test_precedence_gemini_home_and_agy_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Precedence chain for gemini_home including AGY_HOME alias."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    file_gemini = tmp_path / "file_gemini"
    agy_env_gemini = tmp_path / "agy_env_gemini"
    gemini_env = tmp_path / "gemini_env"
    cli_gemini = tmp_path / "cli_gemini"

    # Default
    monkeypatch.delenv("GEMINI_HOME", raising=False)
    monkeypatch.delenv("AGY_HOME", raising=False)
    cfg_default = PersonalTidewayConfig.resolve(home=home)
    assert cfg_default.gemini_home == (Path.home() / ".gemini").resolve()

    # Config.yaml
    config_file = home / DEFAULT_CONFIG_YAML
    config_file.write_text(yaml.safe_dump({
        "version": SCHEMA_VERSION,
        "client_paths": {"gemini_home": str(file_gemini)},
    }), encoding="utf-8")
    cfg_file = PersonalTidewayConfig.resolve(home=home)
    assert cfg_file.gemini_home == file_gemini.resolve()

    # AGY_HOME alias overrides file
    monkeypatch.setenv("AGY_HOME", str(agy_env_gemini))
    cfg_agy = PersonalTidewayConfig.resolve(home=home)
    assert cfg_agy.gemini_home == agy_env_gemini.resolve()

    # GEMINI_HOME takes precedence over AGY_HOME
    monkeypatch.setenv("GEMINI_HOME", str(gemini_env))
    cfg_gem = PersonalTidewayConfig.resolve(home=home)
    assert cfg_gem.gemini_home == gemini_env.resolve()

    # CLI overrides all
    cfg_cli = PersonalTidewayConfig.resolve(home=home, gemini_home=cli_gemini)
    assert cfg_cli.gemini_home == cli_gemini.resolve()


def test_precedence_skill_link_mode(tmp_path: Path):
    """Precedence chain for skill_link_mode: CLI > config.yaml > default."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)

    # Default is symlink
    cfg_default = PersonalTidewayConfig.resolve(home=home)
    assert cfg_default.skill_link_mode == SKILL_LINK_SYMLINK

    # File overrides default
    config_file = home / DEFAULT_CONFIG_YAML
    config_file.write_text(yaml.safe_dump({
        "version": SCHEMA_VERSION,
        "skill_link_mode": SKILL_LINK_COPY,
    }), encoding="utf-8")
    cfg_file = PersonalTidewayConfig.resolve(home=home)
    assert cfg_file.skill_link_mode == SKILL_LINK_COPY

    # CLI overrides file
    cfg_cli = PersonalTidewayConfig.resolve(home=home, skill_link_mode=SKILL_LINK_SYMLINK)
    assert cfg_cli.skill_link_mode == SKILL_LINK_SYMLINK


def test_precedence_optional_project_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """PERSONAL_TIDEWAY_PROJECT precedence: CLI > env > file > absence (None)."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)

    # Absence distinctly preserved
    monkeypatch.delenv("PERSONAL_TIDEWAY_PROJECT", raising=False)
    cfg_none = PersonalTidewayConfig.resolve(home=home)
    assert cfg_none.project is None

    # File specifies project
    config_file = home / DEFAULT_CONFIG_YAML
    config_file.write_text(yaml.safe_dump({
        "version": SCHEMA_VERSION,
        "project": "file-project",
    }), encoding="utf-8")
    cfg_file = PersonalTidewayConfig.resolve(home=home)
    assert cfg_file.project == "file-project"

    # Env overrides file
    monkeypatch.setenv("PERSONAL_TIDEWAY_PROJECT", "env-project")
    cfg_env = PersonalTidewayConfig.resolve(home=home)
    assert cfg_env.project == "env-project"

    # CLI overrides env
    cfg_cli = PersonalTidewayConfig.resolve(home=home, project="cli-project")
    assert cfg_cli.project == "cli-project"

    # Empty env preserves absence distinctly
    monkeypatch.setenv("PERSONAL_TIDEWAY_PROJECT", "   ")
    cfg_empty_env = PersonalTidewayConfig.resolve(home=home)
    # File project is used if env is blank
    assert cfg_empty_env.project == "file-project"

    # Blank file project preserves absence
    config_file.write_text(yaml.safe_dump({
        "version": SCHEMA_VERSION,
        "project": None,
    }), encoding="utf-8")
    monkeypatch.delenv("PERSONAL_TIDEWAY_PROJECT", raising=False)
    cfg_blank_file = PersonalTidewayConfig.resolve(home=home)
    assert cfg_blank_file.project is None


# ============================================================================
# 2. Schema Validation & Integrity (Fail without mutation)
# ============================================================================

def test_invalid_yaml_fails_without_mutation(tmp_path: Path):
    """Invalid YAML syntax raises ConfigError and does not rewrite the file."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    cfg_file = home / DEFAULT_CONFIG_YAML
    bad_content = "version: [unclosed list\nclient_paths: invalid:"
    cfg_file.write_text(bad_content, encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        PersonalTidewayConfig.resolve(home=home)
    assert "Failed to parse" in str(exc_info.value)
    # Ensure zero mutation
    assert cfg_file.read_text(encoding="utf-8") == bad_content


def test_non_mapping_root_fails_without_mutation(tmp_path: Path):
    """Config with non-mapping root (e.g. list, scalar, empty) raises ConfigError."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    cfg_file = home / DEFAULT_CONFIG_YAML
    list_content = "- item1\n- item2\n"
    cfg_file.write_text(list_content, encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        PersonalTidewayConfig.resolve(home=home)
    assert "must have a mapping at root" in str(exc_info.value)
    assert cfg_file.read_text(encoding="utf-8") == list_content

    # Empty file is also not a mapping
    cfg_file.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        PersonalTidewayConfig.resolve(home=home)
    assert "must have a mapping at root" in str(exc_info.value)


def test_non_integer_and_bool_version_fails_without_mutation(tmp_path: Path):
    """Version must be strictly an integer; bool, string, float must raise ConfigError."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    cfg_file = home / DEFAULT_CONFIG_YAML

    # Bool version
    bool_content = "version: true\n"
    cfg_file.write_text(bool_content, encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        PersonalTidewayConfig.resolve(home=home)
    assert "version must be an integer" in str(exc_info.value)
    assert cfg_file.read_text(encoding="utf-8") == bool_content

    # String version
    str_content = "version: '2'\n"
    cfg_file.write_text(str_content, encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        PersonalTidewayConfig.resolve(home=home)
    assert "version must be an integer" in str(exc_info.value)
    assert cfg_file.read_text(encoding="utf-8") == str_content

    # Float version
    float_content = "version: 2.5\n"
    cfg_file.write_text(float_content, encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        PersonalTidewayConfig.resolve(home=home)
    assert "version must be an integer" in str(exc_info.value)
    assert cfg_file.read_text(encoding="utf-8") == float_content


def test_version_greater_than_supported_fails_without_mutation(tmp_path: Path):
    """Schema version newer than supported raises ConfigError without rewriting."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    cfg_file = home / DEFAULT_CONFIG_YAML
    future_content = f"version: {SCHEMA_VERSION + 1}\nclient_paths: {{}}\n"
    cfg_file.write_text(future_content, encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        PersonalTidewayConfig.resolve(home=home)
    assert f"newer than supported version {SCHEMA_VERSION}" in str(exc_info.value)
    assert cfg_file.read_text(encoding="utf-8") == future_content


def test_invalid_client_paths_and_skill_mode(tmp_path: Path):
    """Non-mapping client_paths or invalid skill_link_mode raises ConfigError."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    cfg_file = home / DEFAULT_CONFIG_YAML

    # Malformed client_paths
    cfg_file.write_text("version: 2\nclient_paths: 'not-a-dict'\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        PersonalTidewayConfig.resolve(home=home)
    assert "'client_paths'" in str(exc_info.value)

    # Malformed client_paths value
    cfg_file.write_text("version: 2\nclient_paths:\n  codex_home: 12345\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        PersonalTidewayConfig.resolve(home=home)
    assert "expected string" in str(exc_info.value)

    # Malformed skill_link_mode in file
    cfg_file.write_text("version: 2\nskill_link_mode: 'unsupported_mode'\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        PersonalTidewayConfig.resolve(home=home)
    assert "Unsupported skill_link_mode" in str(exc_info.value)

    # Reset config.yaml to valid content before testing explicit CLI arg
    cfg_file.write_text("version: 2\n", encoding="utf-8")

    # Malformed skill_link_mode in CLI arg
    with pytest.raises(ValidationError) as exc_info:
        PersonalTidewayConfig.resolve(home=home, skill_link_mode="bogus")
    assert "Unsupported skill_link_mode" in str(exc_info.value)


def test_missing_version_fails_without_mutation(tmp_path: Path):
    """Missing required version field raises ConfigError."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    cfg_file = home / DEFAULT_CONFIG_YAML
    content = "client_paths: {}\n"
    cfg_file.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        PersonalTidewayConfig.resolve(home=home)
    assert "Missing required 'version' field" in str(exc_info.value)
    assert cfg_file.read_text(encoding="utf-8") == content


def test_older_supported_version_resolves_read_only(tmp_path: Path):
    """Older supported schema version (e.g. version 1) resolves cleanly in read-only mode."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    cfg = PersonalTidewayConfig.resolve(home=home)
    assert cfg.version == 1


# ============================================================================
# 3. Spaces and Non-ASCII Paths
# ============================================================================

def test_config_with_spaces_and_non_ascii_paths(tmp_path: Path):
    """Paths with spaces and Unicode/non-ASCII resolve correctly without mangling."""
    unicode_home = tmp_path / "пространство имён и папка 🚀"
    unicode_home.mkdir(parents=True)

    codex_target = tmp_path / "кодекс path 📁"
    config_file = unicode_home / DEFAULT_CONFIG_YAML
    config_file.write_text(yaml.safe_dump({
        "version": SCHEMA_VERSION,
        "client_paths": {
            "codex_home": str(codex_target),
        },
    }, allow_unicode=True), encoding="utf-8")

    cfg = PersonalTidewayConfig.resolve(home=unicode_home)
    assert cfg.home == unicode_home.resolve()
    assert cfg.codex_home == codex_target.resolve()
    assert cfg.skills_shared == unicode_home.resolve() / "skills" / "shared"
    assert cfg.projects_yaml == unicode_home.resolve() / "registry" / "projects.yaml"


# ============================================================================
# 4. Boundary Validation (Side-effect free)
# ============================================================================

def test_boundary_valid_descendants(tmp_path: Path):
    """Valid descendants inside workspace pass boundary validation."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    cfg = PersonalTidewayConfig.resolve(home=home)

    # Existing directory
    (home / "mcp").mkdir()
    res1 = cfg.validate_owned_path(home / "mcp")
    assert res1 == (home / "mcp").resolve()

    # Relative existing/future paths
    res2 = cfg.validate_owned_path("skills/shared/my_skill")
    assert res2 == (home / "skills" / "shared" / "my_skill").resolve()

    # Deeply nested non-existent path
    res3 = cfg.validate_owned_path("registry/future/deep/nonexistent.yaml")
    assert res3 == (home / "registry" / "future" / "deep" / "nonexistent.yaml").resolve()
    # Confirm side-effect-free: nothing was created
    assert not (home / "registry" / "future").exists()


def test_boundary_root_rejection_when_descendant_required(tmp_path: Path):
    """Workspace root itself is rejected when a descendant is required."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    cfg = PersonalTidewayConfig.resolve(home=home)

    # allow_root=False (default) must reject
    with pytest.raises(BoundaryError) as exc_info:
        cfg.validate_owned_path(home)
    assert "descendant is required" in str(exc_info.value)

    with pytest.raises(BoundaryError):
        cfg.validate_owned_path(".")

    with pytest.raises(BoundaryError):
        cfg.validate_owned_path(home / "subdir" / "..")

    # allow_root=True must allow
    assert cfg.validate_owned_path(home, allow_root=True) == home.resolve()
    assert cfg.validate_owned_path(".", allow_root=True) == home.resolve()


def test_boundary_dotdot_escape(tmp_path: Path):
    """Path traversal escaping via '..' is rejected."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    cfg = PersonalTidewayConfig.resolve(home=home)

    with pytest.raises(BoundaryError) as exc_info:
        cfg.validate_owned_path(home / ".." / "outside_file.txt")
    assert "outside workspace root" in str(exc_info.value)

    with pytest.raises(BoundaryError):
        cfg.validate_owned_path("../sibling")

    with pytest.raises(BoundaryError):
        cfg.validate_owned_path("a/b/../../../../escape")


def test_boundary_symlink_escape(tmp_path: Path):
    """Symlinks escaping workspace root are rejected for both existing and future targets."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    outside = tmp_path / "outside_world"
    outside.mkdir(parents=True)
    (outside / "real_file.txt").write_text("outside data", encoding="utf-8")

    # Create symlink pointing outside
    symlink_target = home / "symlink_outside"
    os.symlink(outside, symlink_target)

    cfg = PersonalTidewayConfig.resolve(home=home)

    # Direct symlink escape
    with pytest.raises(BoundaryError) as exc_info:
        cfg.validate_owned_path(symlink_target)
    assert "outside workspace root" in str(exc_info.value)

    # Existing file via symlink
    with pytest.raises(BoundaryError):
        cfg.validate_owned_path(symlink_target / "real_file.txt")

    # Non-existent target through symlink escape
    with pytest.raises(BoundaryError):
        cfg.validate_owned_path(symlink_target / "nested" / "nonexistent.txt")


def test_boundary_empty_and_is_owned_helper(tmp_path: Path):
    """Empty paths raise ValidationError, and is_owned_path returns bool cleanly."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    cfg = PersonalTidewayConfig.resolve(home=home)

    with pytest.raises(ValidationError):
        cfg.validate_owned_path("")

    with pytest.raises(ValidationError):
        cfg.validate_owned_path("   ")

    assert cfg.is_owned_path("valid/sub/path") is True
    assert cfg.is_owned_path(home) is False
    assert cfg.is_owned_path(home, allow_root=True) is True
    assert cfg.is_owned_path("../outside") is False


# ============================================================================
# 5. Serialization without User-Specific Fixture Paths
# ============================================================================

def test_to_dict_omits_platform_default_user_paths(tmp_path: Path):
    """to_dict serializes clean config without embedding machine-specific user home paths."""
    home = tmp_path / "ptw_home"
    cfg = PersonalTidewayConfig.resolve(home=home)
    data = cfg.to_dict()

    assert data["version"] == SCHEMA_VERSION
    assert data["skill_link_mode"] == SKILL_LINK_SYMLINK
    assert data["client_paths"] == {}
    assert "project" not in data

    # Explicit custom path is included
    custom_codex = tmp_path / "custom_codex"
    cfg_custom = PersonalTidewayConfig.resolve(home=home, codex_home=custom_codex, project="test-proj")
    custom_data = cfg_custom.to_dict()
    assert custom_data["client_paths"]["codex_home"] == str(custom_codex.resolve())
    assert custom_data["project"] == "test-proj"
