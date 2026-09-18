"""Acceptance tests for Migration 5A: deterministic read-only 'ptw migrate plan'.

NOTE: Per execution instructions, these tests are WRITTEN but NOT RUN by the agent.
Codex will execute every test afterward in an isolated test environment.
"""

import json
from pathlib import Path

import pytest
import yaml

from personal_tideway.cli.main import main
from personal_tideway.constants import (
    DEFAULT_CONFIG_YAML,
    SCHEMA_VERSION,
    ExitCode,
)
from personal_tideway.core.migration import (
    STATUS_BLOCKED,
    STATUS_NOT_REQUIRED,
    STATUS_READY,
    format_migration_plan_text,
    plan_migration,
)


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch):
    """Ensure no test consults live host environment variables."""
    for env_k in [
        "PERSONAL_TIDEWAY_HOME",
        "CODEX_HOME",
        "GEMINI_HOME",
        "AGY_HOME",
        "PERSONAL_TIDEWAY_PROJECT",
    ]:
        monkeypatch.delenv(env_k, raising=False)


# ============================================================================
# 1. Valid v2 -> not_required
# ============================================================================

def test_valid_v2_not_required(tmp_path: Path):
    """Valid v2 workspace yields status='not_required' with zero actions/backups."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text(
        yaml.safe_dump({"version": SCHEMA_VERSION, "client_paths": {}}),
        encoding="utf-8",
    )

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert plan.status == STATUS_NOT_REQUIRED
    assert plan.source_schema_version == SCHEMA_VERSION
    assert plan.target_schema_version == SCHEMA_VERSION
    assert plan.blockers == []
    assert plan.actions == []
    assert plan.backups == []


# ============================================================================
# 2. Valid synthetic v1 -> ready
# ============================================================================

def test_valid_synthetic_v1_ready(tmp_path: Path):
    """Valid synthetic v1 workspace produces status='ready' with ordered actions."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert plan.status == STATUS_READY
    assert plan.source_schema_version == 1
    assert plan.target_schema_version == SCHEMA_VERSION
    assert plan.blockers == []
    assert len(plan.actions) > 0
    assert "ptw:config_yaml" in plan.backups


# ============================================================================
# 3. Identical repeat result (determinism)
# ============================================================================

def test_identical_repeat_result(tmp_path: Path):
    """Multiple plan executions on the same state produce bit-for-bit identical results."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    plan1 = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    plan2 = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert plan1.to_dict() == plan2.to_dict()
    assert format_migration_plan_text(plan1) == format_migration_plan_text(plan2)


# ============================================================================
# 4. Human output format (Global flags before subcommand)
# ============================================================================

def test_human_output_format(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Human output format exposes all 12 properties clearly without omissions."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    code = main([
        "--home",
        str(home),
        "--codex-home",
        str(codex_home),
        "--gemini-home",
        str(gemini_home),
        "migrate",
        "plan",
    ])

    assert code == ExitCode.SUCCESS
    out = capsys.readouterr().out

    assert "Migration Plan" in out
    assert "Status: ready" in out
    assert "Source Schema Version: 1" in out
    assert f"Target Schema Version: {SCHEMA_VERSION}" in out
    assert "Blockers:" in out
    assert "Actions:" in out
    assert "Backups:" in out
    assert "Preserve:" in out
    assert "Legacy Paths:" in out
    assert "Current Paths:" in out
    assert "Canonical MCP:" in out
    assert "Client Files:" in out
    assert "Warnings:" in out


# ============================================================================
# 5. CLI --json flag (Global flags before subcommand)
# ============================================================================

def test_cli_json_flag(tmp_path: Path, capsys: pytest.CaptureFixture):
    """CLI --json flag produces valid JSON with strict schema adherence."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    code = main([
        "--home",
        str(home),
        "--codex-home",
        str(codex_home),
        "--gemini-home",
        str(gemini_home),
        "migrate",
        "plan",
        "--json",
    ])

    assert code == ExitCode.SUCCESS
    out = capsys.readouterr().out
    data = json.loads(out)

    expected_keys = {
        "status",
        "source_schema_version",
        "target_schema_version",
        "actions",
        "backups",
        "blockers",
        "warnings",
        "preserve",
        "legacy_paths",
        "current_paths",
        "canonical_mcp",
        "client_files",
    }
    assert set(data.keys()) == expected_keys
    assert data["status"] == STATUS_READY
    assert data["source_schema_version"] == 1
    assert data["target_schema_version"] == SCHEMA_VERSION
    assert isinstance(data["actions"], list)
    assert isinstance(data["backups"], list)


# ============================================================================
# 6. Zero mutation guarantee
# ============================================================================

def test_zero_mutation_guarantee(tmp_path: Path):
    """Migration planning guarantees zero mutations: paths, bytes, modes, mtimes, registry, backups."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    mcp_dir = home / "mcp"
    mcp_dir.mkdir()
    srv_file = mcp_dir / "srv1.yaml"
    srv_file.write_text("name: srv1\ntransport: stdio\n", encoding="utf-8")

    memory_dir = home / "memory"
    memory_dir.mkdir()
    mem_file = memory_dir / "note.md"
    mem_file.write_text("# Note\n", encoding="utf-8")

    def snapshot(root: Path) -> dict[str, tuple[int, int, bytes]]:
        res: dict[str, tuple[int, int, bytes]] = {}
        for p in sorted(root.rglob("*")):
            st = p.stat()
            content = p.read_bytes() if p.is_file() else b""
            res[str(p.relative_to(root))] = (st.st_mode, st.st_mtime_ns, content)
        return res

    snap_before = snapshot(tmp_path)

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert plan.status == STATUS_READY

    snap_after = snapshot(tmp_path)

    assert snap_before == snap_after
    assert not (home / "registry").exists()
    assert not (home / "state").exists()
    assert not (home / "backups").exists()


# ============================================================================
# 7. Malformed v1 YAML -> blocked (No duplicate calls)
# ============================================================================

def test_malformed_v1_yaml_blocked(tmp_path: Path):
    """Malformed YAML produces status='blocked' with sanitized generic error."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\n  invalid: [syntax\n", encoding="utf-8")

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert plan.status == STATUS_BLOCKED
    assert plan.source_schema_version is None
    assert len(plan.blockers) > 0
    assert any("YAML syntax" in b for b in plan.blockers)
    for b in plan.blockers:
        assert str(home) not in b


# ============================================================================
# 8. Future schema -> blocked
# ============================================================================

def test_future_schema_blocked(tmp_path: Path):
    """Future schema version (e.g. 3) yields status='blocked' with captured version number."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 3\nclient_paths: {}\n", encoding="utf-8")

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert plan.status == STATUS_BLOCKED
    assert plan.source_schema_version == 3
    assert any("Unsupported future schema version 3" in b for b in plan.blockers)


# ============================================================================
# 9. Legacy AGY rules detected
# ============================================================================

def test_legacy_agy_rules_detected(tmp_path: Path):
    """Legacy AGY GEMINI.md is detected and added to legacy_paths, backups, and actions."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    legacy_gemini_md = gemini_home / "GEMINI.md"
    legacy_gemini_md.write_text("# Old Rules\n", encoding="utf-8")

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert plan.status == STATUS_READY
    assert "agy:legacy_gemini_md" in plan.legacy_paths
    assert "agy:legacy_gemini_md" in plan.backups
    assert any("relocate_agy_rules" in a for a in plan.actions)


# ============================================================================
# 10. Legacy AGY skills detected
# ============================================================================

def test_legacy_agy_skills_detected(tmp_path: Path):
    """Legacy AGY skills directory is detected and planned for migration."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    legacy_skills = gemini_home / "skills"
    legacy_skills.mkdir(parents=True)
    (legacy_skills / "sample_skill").mkdir()

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert plan.status == STATUS_READY
    assert "agy:legacy_skills" in plan.legacy_paths
    assert "agy:legacy_skills" in plan.backups
    assert any("relocate_agy_skills" in a for a in plan.actions)


# ============================================================================
# 11. Legacy memory detected without file-content import (Isolated client homes)
# ============================================================================

def test_legacy_memory_detected_without_file_content_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Legacy memory presence is detected without reading, opening, or importing note contents."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    memory_dir = home / "memory"
    memory_dir.mkdir()
    secret_note = memory_dir / "private_note.md"
    sentinel_secret = "SENTINEL_MEMORY_SECRET_CONTENT_998877"
    secret_note.write_text(sentinel_secret, encoding="utf-8")

    original_open = Path.open

    def guarded_open(self, *args, **kwargs):
        if "memory" in self.parts and self.name.endswith(".md"):
            raise AssertionError(f"Security violation: opened legacy memory file: {self}")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert plan.status == STATUS_READY
    assert "ptw:memory_dir" in plan.legacy_paths
    assert "ptw:legacy_memory" in plan.preserve
    assert "ptw:memory_dir" in plan.backups
    assert any("archive_legacy_memory" in a for a in plan.actions)

    plan_json = json.dumps(plan.to_dict())
    assert sentinel_secret not in plan_json
    assert sentinel_secret not in format_migration_plan_text(plan)


# ============================================================================
# 12. Canonical MCP preservation without raw contents/env (Isolated client homes)
# ============================================================================

def test_canonical_mcp_preservation_without_raw_contents_or_env(tmp_path: Path):
    """Canonical MCP definitions (.yaml) are preserved by symbolic ID without dumping env or secrets."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    mcp_dir = home / "mcp"
    mcp_dir.mkdir()
    secret_srv = mcp_dir / "vault_server.yaml"
    secret_token = "SUPER_SECRET_VAULT_TOKEN_ABCD1234"
    secret_srv.write_text(
        yaml.safe_dump({
            "name": "vault_server",
            "transport": "stdio",
            "command": "run_vault",
            "env": {"VAULT_TOKEN": secret_token},
        }),
        encoding="utf-8",
    )

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert plan.status == STATUS_READY
    assert "mcp:vault_server" in plan.canonical_mcp
    assert "mcp:vault_server" in plan.preserve

    plan_str = json.dumps(plan.to_dict())
    assert secret_token not in plan_str
    assert "VAULT_TOKEN" not in plan_str
    assert "run_vault" not in plan_str
    assert secret_token not in format_migration_plan_text(plan)


# ============================================================================
# 13. Unmanaged rules/skills/foreign files preserved
# ============================================================================

def test_unmanaged_rules_skills_foreign_files_preserved(tmp_path: Path):
    """Unmanaged user rules, skills, and client files are marked for preservation without leak."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    codex_rules = codex_home / "AGENTS.md"
    sentinel_rules = "UNMANAGED_USER_CODEX_RULES_CUSTOM_CONTENT"
    codex_rules.write_text(sentinel_rules, encoding="utf-8")

    foreign_file = gemini_home / "config" / "user_tool.json"
    foreign_file.parent.mkdir(parents=True)
    sentinel_foreign = "UNMANAGED_FOREIGN_CLIENT_FILE_CUSTOM"
    foreign_file.write_text(sentinel_foreign, encoding="utf-8")

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert plan.status == STATUS_READY
    assert "codex:unmanaged_rules" in plan.preserve
    assert "agy:unmanaged_rules" in plan.preserve
    assert "client:unmanaged_files" in plan.preserve

    plan_json = json.dumps(plan.to_dict())
    assert sentinel_rules not in plan_json
    assert sentinel_foreign not in plan_json


# ============================================================================
# 14. Symlink escape and configured '../' traversal -> blocked
# ============================================================================

def test_symlink_escape_and_traversal_blocked(tmp_path: Path):
    """Configured path traversal ('..') and escaping symlinks cause status='blocked'."""
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    # Subtest 1: Path traversal in configured client path
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text(
        yaml.safe_dump({"version": 1, "client_paths": {"codex_home": "../../escaped_dir"}}),
        encoding="utf-8",
    )

    plan_traversal = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert plan_traversal.status == STATUS_BLOCKED
    assert any("Path traversal detected" in b for b in plan_traversal.blockers)

    # Subtest 2: Escaping symlink in memory directory
    home2 = tmp_path / "ptw_home2"
    home2.mkdir(parents=True)
    cfg_file2 = home2 / DEFAULT_CONFIG_YAML
    cfg_file2.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    symlink_mem = home2 / "memory"
    symlink_mem.symlink_to(outside_dir, target_is_directory=True)

    plan_symlink = plan_migration(home=home2, codex_home=codex_home, gemini_home=gemini_home)
    assert plan_symlink.status == STATUS_BLOCKED
    assert any("Symlink escape detected" in b for b in plan_symlink.blockers)


# ============================================================================
# 15. Missing optional legacy artifacts does not block
# ============================================================================

def test_missing_optional_legacy_artifacts_does_not_block(tmp_path: Path):
    """Absence of optional legacy artifacts (memory, GEMINI.md, skills) reports warnings but status='ready'."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert plan.status == STATUS_READY
    assert plan.blockers == []
    assert len(plan.warnings) > 0
    assert any("Optional legacy artifact absent" in w for w in plan.warnings)


# ============================================================================
# 16. Output excludes secrets/OAuth/host paths/raw config (Global flags before subcommand)
# ============================================================================

def test_output_excludes_secrets_oauth_host_paths_raw_config(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Migration plan output never exposes secrets.env, OAuth tokens, host paths, or raw YAML."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\nraw_yaml_marker: true\n", encoding="utf-8")

    secrets_file = home / "secrets.env"
    secret_val = "SUPER_SECRET_VALUE_999999"
    secrets_file.write_text(f"API_KEY={secret_val}\n", encoding="utf-8")

    auth_file = codex_home / "auth.json"
    oauth_val = "OAUTH_TOKEN_SECRET_888888"
    auth_file.write_text(json.dumps({"token": oauth_val}), encoding="utf-8")

    code = main([
        "--home",
        str(home),
        "--codex-home",
        str(codex_home),
        "--gemini-home",
        str(gemini_home),
        "migrate",
        "plan",
        "--json",
    ])

    assert code == ExitCode.SUCCESS
    out = capsys.readouterr().out

    assert secret_val not in out
    assert oauth_val not in out
    assert "raw_yaml_marker" not in out
    assert str(home) not in out


# ============================================================================
# 17. Stable ordering for every list (.yaml files)
# ============================================================================

def test_stable_ordering_for_every_list(tmp_path: Path):
    """All plan lists are deterministically sorted to prevent diff jitter."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    mcp_dir = home / "mcp"
    mcp_dir.mkdir()
    (mcp_dir / "z_srv.yaml").write_text("name: z\n", encoding="utf-8")
    (mcp_dir / "a_srv.yaml").write_text("name: a\n", encoding="utf-8")
    (mcp_dir / "m_srv.yaml").write_text("name: m\n", encoding="utf-8")

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert plan.actions == sorted(plan.actions)
    assert plan.backups == sorted(plan.backups)
    assert plan.blockers == sorted(plan.blockers)
    assert plan.warnings == sorted(plan.warnings)
    assert plan.preserve == sorted(plan.preserve)
    assert plan.legacy_paths == sorted(plan.legacy_paths)
    assert plan.current_paths == sorted(plan.current_paths)
    assert plan.canonical_mcp == sorted(plan.canonical_mcp)
    assert plan.client_files == sorted(plan.client_files)


# ============================================================================
# 18. Works before normal initialized check (Global flags before subcommand)
# ============================================================================

def test_works_before_normal_initialized_check(tmp_path: Path):
    """'ptw migrate plan' operates safely on uninitialized workspace without throwing ConfigError."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    code = main([
        "--home",
        str(home),
        "--codex-home",
        str(codex_home),
        "--gemini-home",
        str(gemini_home),
        "migrate",
        "plan",
    ])
    assert code == ExitCode.SUCCESS


# ============================================================================
# 19. Broken symlinks and non-regular artifacts blocked
# ============================================================================

def test_broken_symlinks_and_non_regular_blocked(tmp_path: Path):
    """Broken symlinks are caught by lstat checks prior to exists() and blocked."""
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    # Subtest 1: Broken symlink in config.yaml
    home1 = tmp_path / "ptw_home1"
    home1.mkdir(parents=True)
    broken_cfg = home1 / DEFAULT_CONFIG_YAML
    broken_cfg.symlink_to(tmp_path / "non_existent_target.yaml")

    plan1 = plan_migration(home=home1, codex_home=codex_home, gemini_home=gemini_home)
    assert plan1.status == STATUS_BLOCKED
    assert any("is a symbolic link" in b for b in plan1.blockers)

    # Subtest 2: Broken symlink in mcp directory
    home2 = tmp_path / "ptw_home2"
    home2.mkdir(parents=True)
    (home2 / DEFAULT_CONFIG_YAML).write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")
    mcp_dir = home2 / "mcp"
    mcp_dir.mkdir()
    broken_mcp = mcp_dir / "broken.yaml"
    broken_mcp.symlink_to(tmp_path / "non_existent_mcp.yaml")

    plan2 = plan_migration(home=home2, codex_home=codex_home, gemini_home=gemini_home)
    assert plan2.status == STATUS_BLOCKED
    assert any("is a symbolic link" in b for b in plan2.blockers)

    # Subtest 3: Broken symlink in legacy GEMINI.md
    home3 = tmp_path / "ptw_home3"
    gemini_home3 = tmp_path / "gemini_home3"
    home3.mkdir(parents=True)
    gemini_home3.mkdir(parents=True)
    (home3 / DEFAULT_CONFIG_YAML).write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")
    broken_gemini = gemini_home3 / "GEMINI.md"
    broken_gemini.symlink_to(tmp_path / "ghost_rules.md")

    plan3 = plan_migration(home=home3, codex_home=codex_home, gemini_home=gemini_home3)
    assert plan3.status == STATUS_BLOCKED
    assert any("is a symbolic link" in b for b in plan3.blockers)


# ============================================================================
# 20. Dangerous symlinks block for schema v2 as well
# ============================================================================

def test_dangerous_symlinks_block_v2(tmp_path: Path):
    """Schema v2 is not automatically 'not_required' if unsafe symlinks exist in MCP."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    (home / DEFAULT_CONFIG_YAML).write_text(f"version: {SCHEMA_VERSION}\n", encoding="utf-8")
    mcp_dir = home / "mcp"
    mcp_dir.mkdir()
    evil_mcp = mcp_dir / "evil.yaml"
    evil_mcp.symlink_to(tmp_path)

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert plan.status == STATUS_BLOCKED
    assert plan.source_schema_version == SCHEMA_VERSION
    assert any("is a symbolic link" in b for b in plan.blockers)


# ============================================================================
# 21. Nested containment overlap across roots blocked
# ============================================================================

def test_nested_containment_overlap_blocked(tmp_path: Path):
    """Ancestorship/descendant containment across PTW, Codex, and AGY roots is blocked."""
    gemini_home = tmp_path / "gemini_home"
    gemini_home.mkdir(parents=True)

    # Subtest 1: Codex inside PTW home
    home1 = tmp_path / "ptw_home1"
    codex_inside = home1 / "codex_client"
    home1.mkdir(parents=True)
    codex_inside.mkdir(parents=True)
    (home1 / DEFAULT_CONFIG_YAML).write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    plan1 = plan_migration(home=home1, codex_home=codex_inside, gemini_home=gemini_home)
    assert plan1.status == STATUS_BLOCKED
    assert any("overlaps or contains" in b for b in plan1.blockers)

    # Subtest 2: AGY inside Codex home
    home2 = tmp_path / "ptw_home2"
    codex2 = tmp_path / "codex2"
    agy_inside = codex2 / "nested_gemini"
    home2.mkdir(parents=True)
    codex2.mkdir(parents=True)
    agy_inside.mkdir(parents=True)
    (home2 / DEFAULT_CONFIG_YAML).write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    plan2 = plan_migration(home=home2, codex_home=codex2, gemini_home=agy_inside)
    assert plan2.status == STATUS_BLOCKED
    assert any("overlap or contain one another" in b for b in plan2.blockers)


# ============================================================================
# 22. Source/Destination conflicts: Rules and Skills
# ============================================================================

def test_source_destination_conflicts_blocked(tmp_path: Path):
    """Conflicts where legacy and current destination artifacts both exist are blocked."""
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir(parents=True)

    # Subtest 1: Rules conflict (legacy GEMINI.md AND current config/GEMINI.md)
    home1 = tmp_path / "ptw_home1"
    gemini1 = tmp_path / "gemini1"
    home1.mkdir(parents=True)
    gemini1.mkdir(parents=True)
    (home1 / DEFAULT_CONFIG_YAML).write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    (gemini1 / "GEMINI.md").write_text("# Old\n", encoding="utf-8")
    cfg_dir = gemini1 / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "GEMINI.md").write_text("# New\n", encoding="utf-8")

    plan1 = plan_migration(home=home1, codex_home=codex_home, gemini_home=gemini1)
    assert plan1.status == STATUS_BLOCKED
    assert any("legacy AGY rules and current customization rules both exist" in b for b in plan1.blockers)

    # Subtest 2: Skills conflict (skill with same name in legacy and current)
    home2 = tmp_path / "ptw_home2"
    gemini2 = tmp_path / "gemini2"
    home2.mkdir(parents=True)
    gemini2.mkdir(parents=True)
    (home2 / DEFAULT_CONFIG_YAML).write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    legacy_skill_dir = gemini2 / "skills" / "overlap_skill"
    legacy_skill_dir.mkdir(parents=True)
    current_skill_dir = gemini2 / "config" / "skills" / "overlap_skill"
    current_skill_dir.mkdir(parents=True)

    plan2 = plan_migration(home=home2, codex_home=codex_home, gemini_home=gemini2)
    assert plan2.status == STATUS_BLOCKED
    assert any("skills exist in both legacy and current roots" in b for b in plan2.blockers)


# ============================================================================
# 23. Duplicate YAML keys and size limits fail closed
# ============================================================================

def test_duplicate_yaml_keys_and_size_limits(tmp_path: Path):
    """Duplicate keys in config.yaml and files exceeding 64 KiB fail closed."""
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    # Subtest 1: Duplicate keys
    home1 = tmp_path / "ptw_home1"
    home1.mkdir(parents=True)
    (home1 / DEFAULT_CONFIG_YAML).write_text(
        "version: 1\nversion: 3\nclient_paths: {}\n",
        encoding="utf-8",
    )

    plan1 = plan_migration(home=home1, codex_home=codex_home, gemini_home=gemini_home)
    assert plan1.status == STATUS_BLOCKED
    assert any("duplicate key error" in b for b in plan1.blockers)

    # Subtest 2: File exceeding size limit
    home2 = tmp_path / "ptw_home2"
    home2.mkdir(parents=True)
    large_cfg = home2 / DEFAULT_CONFIG_YAML
    large_cfg.write_text("version: 1\n# " + ("X" * 70000) + "\n", encoding="utf-8")

    plan2 = plan_migration(home=home2, codex_home=codex_home, gemini_home=gemini_home)
    assert plan2.status == STATUS_BLOCKED
    assert any("exceeds maximum permitted size" in b for b in plan2.blockers)


# ============================================================================
# 24. Schema v2 with client symlink blocked
# ============================================================================

def test_v2_client_symlink_blocked(tmp_path: Path):
    """Schema v2 config with unsafe client artifact symlink fails closed with blocked."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text(f"version: {SCHEMA_VERSION}\nclient_paths: {{}}\n", encoding="utf-8")

    # Unsafe symlink in codex rules
    outside_rules = tmp_path / "outside_rules.md"
    outside_rules.write_text("# Outside\n", encoding="utf-8")
    codex_rules = codex_home / "AGENTS.md"
    codex_rules.symlink_to(outside_rules)

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert plan.status == STATUS_BLOCKED
    assert plan.source_schema_version == SCHEMA_VERSION
    assert any("client artifact is a symbolic link" in b for b in plan.blockers)


# ============================================================================
# 25. Absent config with legacy artifacts vs clean empty home
# ============================================================================

def test_absent_config_with_legacy_artifacts_vs_clean(tmp_path: Path):
    """Missing config.yaml with legacy memory/mcp is blocked, while clean empty home is not_required."""
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    # Case 1: Clean empty home -> not_required
    clean_home = tmp_path / "clean_home"
    clean_home.mkdir(parents=True)
    plan_clean = plan_migration(home=clean_home, codex_home=codex_home, gemini_home=gemini_home)
    assert plan_clean.status == STATUS_NOT_REQUIRED
    assert plan_clean.blockers == []

    # Case 2: Missing config.yaml but legacy memory directory present -> ambiguous layout (blocked)
    legacy_home = tmp_path / "legacy_home"
    legacy_home.mkdir(parents=True)
    (legacy_home / "memory").mkdir(parents=True)
    plan_legacy = plan_migration(home=legacy_home, codex_home=codex_home, gemini_home=gemini_home)
    assert plan_legacy.status == STATUS_BLOCKED
    assert any("legacy artifacts present but config.yaml is missing" in b for b in plan_legacy.blockers)


# ============================================================================
# 26. Custom client paths, hook precedence, and unhashable keys
# ============================================================================

def test_custom_client_paths_and_hook_precedence(tmp_path: Path):
    """Custom client_paths in config.yaml and explicit hook arguments are respected safely."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    custom_codex_cfg = codex_home / "custom_codex.toml"
    custom_codex_cfg.write_text("model = 'test'\n", encoding="utf-8")

    custom_codex_hook = codex_home / "explicit_hook.json"
    custom_codex_hook.write_text("{}", encoding="utf-8")

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text(
        yaml.safe_dump({
            "version": 1,
            "client_paths": {
                "codex_config": str(custom_codex_cfg),
            },
        }),
        encoding="utf-8",
    )

    plan = plan_migration(
        home=home,
        codex_home=codex_home,
        gemini_home=gemini_home,
        codex_hooks=custom_codex_hook,
    )
    assert plan.status == STATUS_READY
    assert "codex:config" in plan.backups


# ============================================================================
# 27. Root symlink fails closed
# ============================================================================

def test_root_symlink_fails_closed(tmp_path: Path):
    """Symlinked home directory fails closed immediately."""
    real_home = tmp_path / "real_home"
    real_home.mkdir(parents=True)
    symlink_home = tmp_path / "symlink_home"
    symlink_home.symlink_to(real_home, target_is_directory=True)

    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    plan = plan_migration(home=symlink_home, codex_home=codex_home, gemini_home=gemini_home)
    assert plan.status == STATUS_BLOCKED
    assert any("root directory is a symbolic link" in b for b in plan.blockers)


# ============================================================================
# 28. Residual fail-closed regressions from independent review
# ============================================================================

def test_configured_client_root_symlink_is_blocked(tmp_path: Path):
    """A client root supplied by config.yaml must not be resolved through a symlink."""
    home = tmp_path / "ptw_home"
    real_codex_home = tmp_path / "real_codex_home"
    configured_codex_home = tmp_path / "configured_codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir()
    real_codex_home.mkdir()
    gemini_home.mkdir()
    configured_codex_home.symlink_to(real_codex_home, target_is_directory=True)
    (home / DEFAULT_CONFIG_YAML).write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "client_paths": {"codex_home": str(configured_codex_home)},
            }
        ),
        encoding="utf-8",
    )

    plan = plan_migration(home=home, gemini_home=gemini_home)

    assert plan.status == STATUS_BLOCKED
    assert any("configured client root is a symbolic link" in item for item in plan.blockers)


@pytest.mark.parametrize("version", [1, SCHEMA_VERSION])
def test_current_agy_rule_candidate_hazards_are_blocked(tmp_path: Path, version: int):
    """Broken links and wrong file types in current AGY rule candidates fail closed."""
    home = tmp_path / f"ptw_home_{version}"
    codex_home = tmp_path / f"codex_home_{version}"
    gemini_home = tmp_path / f"gemini_home_{version}"
    home.mkdir()
    codex_home.mkdir()
    gemini_home.mkdir()
    (home / DEFAULT_CONFIG_YAML).write_text(
        yaml.safe_dump({"version": version, "client_paths": {}}),
        encoding="utf-8",
    )
    customization_root = gemini_home / "config"
    customization_root.mkdir()

    broken_rules = customization_root / "rules"
    broken_rules.symlink_to(tmp_path / "missing-rules", target_is_directory=True)
    broken_plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert broken_plan.status == STATUS_BLOCKED

    broken_rules.unlink()
    (customization_root / "AGENTS.md").mkdir()
    wrong_type_plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert wrong_type_plan.status == STATUS_BLOCKED
    assert any("not a regular file" in item for item in wrong_type_plan.blockers)


def test_duplicate_custom_agy_rule_candidate_is_deduplicated(tmp_path: Path):
    """A custom rule path equal to a built-in candidate is one target, not ambiguity."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir()
    codex_home.mkdir()
    gemini_home.mkdir()
    current_rules = gemini_home / "config" / "GEMINI.md"
    current_rules.parent.mkdir()
    current_rules.write_text("# Current rules\n", encoding="utf-8")
    (home / DEFAULT_CONFIG_YAML).write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "client_paths": {"agy_rules": str(current_rules)},
            }
        ),
        encoding="utf-8",
    )

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert plan.status == STATUS_READY
    assert not any("multiple current-root rule candidates" in item for item in plan.blockers)


# ============================================================================
# 29. Final independent-review regressions
# ============================================================================

@pytest.mark.parametrize(
    "environment_variable",
    ["PERSONAL_TIDEWAY_HOME", "CODEX_HOME", "GEMINI_HOME", "AGY_HOME"],
)
def test_environment_root_symlink_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment_variable: str,
):
    """Roots selected through every supported environment variable fail closed on symlinks."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    real_root = tmp_path / "real_root"
    symlink_root = tmp_path / "symlink_root"
    for directory in (home, codex_home, gemini_home, real_root):
        directory.mkdir()
    (home / DEFAULT_CONFIG_YAML).write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")
    (real_root / DEFAULT_CONFIG_YAML).write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")
    symlink_root.symlink_to(real_root, target_is_directory=True)

    monkeypatch.setenv("PERSONAL_TIDEWAY_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("GEMINI_HOME", str(gemini_home))
    monkeypatch.delenv("AGY_HOME", raising=False)
    if environment_variable == "AGY_HOME":
        monkeypatch.delenv("GEMINI_HOME")
        monkeypatch.setenv("AGY_HOME", str(symlink_root))
    else:
        monkeypatch.setenv(environment_variable, str(symlink_root))

    plan = plan_migration()

    assert plan.status == STATUS_BLOCKED
    assert any("root directory is a symbolic link" in item for item in plan.blockers)


def test_configured_client_artifact_symlink_components_are_blocked(tmp_path: Path):
    """Configured client artifacts cannot hide a final or intermediate symlink."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    external = tmp_path / "external"
    for directory in (home, codex_home, gemini_home, external):
        directory.mkdir()
    external_config = external / "config.toml"
    external_config.write_text("model = 'external'\n", encoding="utf-8")
    configured_config = codex_home / "configured.toml"
    configured_config.symlink_to(external_config)
    config_yaml = home / DEFAULT_CONFIG_YAML
    config_yaml.write_text(
        yaml.safe_dump(
            {"version": 1, "client_paths": {"codex_config": "configured.toml"}}
        ),
        encoding="utf-8",
    )

    direct_plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert direct_plan.status == STATUS_BLOCKED

    configured_config.unlink()
    linked_directory = codex_home / "linked"
    linked_directory.symlink_to(external, target_is_directory=True)
    config_yaml.write_text(
        yaml.safe_dump(
            {"version": 1, "client_paths": {"codex_config": "linked/config.toml"}}
        ),
        encoding="utf-8",
    )

    nested_plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert nested_plan.status == STATUS_BLOCKED
    assert any("client artifact is a symbolic link" in item for item in nested_plan.blockers)


def test_explicit_hook_symlink_is_blocked(tmp_path: Path):
    """An explicit hook path remains unresolved until symlink validation."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    for directory in (home, codex_home, gemini_home):
        directory.mkdir()
    (home / DEFAULT_CONFIG_YAML).write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")
    target = tmp_path / "outside-hooks.json"
    target.write_text("{}", encoding="utf-8")
    hooks_link = codex_home / "hooks-link.json"
    hooks_link.symlink_to(target)

    plan = plan_migration(
        home=home,
        codex_home=codex_home,
        gemini_home=gemini_home,
        codex_hooks=hooks_link,
    )

    assert plan.status == STATUS_BLOCKED
    assert any("client artifact is a symbolic link" in item for item in plan.blockers)


def test_duplicate_canonical_mcp_identifier_is_blocked(tmp_path: Path):
    """The same MCP identifier in .yaml and .yml is ambiguous and appears only once."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    for directory in (home, codex_home, gemini_home):
        directory.mkdir()
    (home / DEFAULT_CONFIG_YAML).write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")
    mcp_dir = home / "mcp"
    mcp_dir.mkdir()
    (mcp_dir / "server.yaml").write_text("name: first\n", encoding="utf-8")
    (mcp_dir / "server.yml").write_text("name: second\n", encoding="utf-8")

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert plan.status == STATUS_BLOCKED
    assert plan.canonical_mcp == ["mcp:server"]
    assert any("duplicate canonical mcp definition identifier" in item for item in plan.blockers)


# ============================================================================
# 30. Semantic parity of human and JSON representations
# ============================================================================

def test_semantic_parity_of_human_and_json(tmp_path: Path):
    """Every piece of data present in JSON plan is fully reflected in human format."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    data = plan.to_dict()
    human_text = format_migration_plan_text(plan)

    assert data["status"] in human_text
    assert str(data["source_schema_version"]) in human_text
    assert str(data["target_schema_version"]) in human_text

    for a in data["actions"]:
        assert a in human_text
    for b in data["backups"]:
        assert b in human_text
    for p in data["preserve"]:
        assert p in human_text
    for lp in data["legacy_paths"]:
        assert lp in human_text
    for cp in data["current_paths"]:
        assert cp in human_text
    for m in data["canonical_mcp"]:
        assert m in human_text
    for cf in data["client_files"]:
        assert cf in human_text
    for w in data["warnings"]:
        assert w in human_text
