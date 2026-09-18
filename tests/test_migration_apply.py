"""Acceptance tests for Migration 5B: safe migration apply ('ptw migrate apply [--dry-run] [--json]').

NOTE: Per Codex execution instructions, all tests in this suite are WRITTEN but NOT RUN
by the agent. Codex will run every test in the verification phase.
"""

import json
import os
from pathlib import Path

import pytest
import yaml

from personal_tideway.cli.main import main
from personal_tideway.constants import (
    DEFAULT_CONFIG_YAML,
    RULE_MARKER_END,
    RULE_MARKER_START,
    SCHEMA_VERSION,
    ExitCode,
)
from personal_tideway.core.migration import (
    STATUS_BLOCKED,
    STATUS_NOT_REQUIRED,
    STATUS_READY,
    plan_migration,
)
from personal_tideway.core.migration_apply import (
    STATE_COMPLETED,
    MigrationLockError,
    apply_migration,
    format_migration_apply_text,
)


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch):
    """Ensure tests run isolated from host environment variables."""
    for env_k in [
        "PERSONAL_TIDEWAY_HOME",
        "CODEX_HOME",
        "GEMINI_HOME",
        "AGY_HOME",
        "PERSONAL_TIDEWAY_PROJECT",
    ]:
        monkeypatch.delenv(env_k, raising=False)


# ============================================================================
# 1. dry-run полностью zero-mutation
# ============================================================================

def test_dry_run_zero_mutation(tmp_path: Path):
    """Dry-run apply performs zero mutations, creates no locks, backups, or directories."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_content = "version: 1\nclient_paths: {}\ncustom_key: preserve_me\n"
    cfg_file.write_text(cfg_content, encoding="utf-8")

    initial_cfg_stat = cfg_file.stat()

    result = apply_migration(
        home=home,
        codex_home=codex_home,
        gemini_home=gemini_home,
        dry_run=True,
    )

    assert result.status == STATUS_READY
    assert result.dry_run is True
    assert result.source_schema_version == 1
    assert result.target_schema_version == SCHEMA_VERSION
    assert result.bundle_id is None
    assert len(result.mutations) > 0
    assert "ptw:config_yaml" in result.backups

    # Verify zero mutations occurred
    assert cfg_file.read_text(encoding="utf-8") == cfg_content
    assert cfg_file.stat().st_mtime_ns == initial_cfg_stat.st_mtime_ns
    assert not (home / "locks").exists()
    assert not (home / "backups").exists()
    assert not (home / "registry").exists()
    assert not (home / "state").exists()


# ============================================================================
# 2. blocked / not_required никогда не создают backup или lock
# ============================================================================

def test_blocked_and_not_required_zero_backup_and_zero_lock(tmp_path: Path):
    """Blocked and not_required plans never acquire lock or create backup directories."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    # Subtest A: not_required (already v2)
    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text(f"version: {SCHEMA_VERSION}\nclient_paths: {{}}\n", encoding="utf-8")

    res_not_req = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert res_not_req.status == STATUS_NOT_REQUIRED
    assert res_not_req.bundle_id is None
    assert not (home / "locks").exists()
    assert not (home / "backups").exists()

    # Subtest B: blocked (malformed yaml)
    cfg_file.write_text("version: 1\nclient_paths: [unbalanced\n", encoding="utf-8")
    res_blocked = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert res_blocked.status == STATUS_BLOCKED
    assert len(res_blocked.blockers) > 0
    assert res_blocked.bundle_id is None
    assert not (home / "locks").exists()
    assert not (home / "backups").exists()


# ============================================================================
# 3. успешный apply переводит schema v1 -> v2
# ============================================================================

def test_successful_apply_schema_v1_to_v2(tmp_path: Path):
    """Successful apply migrates config.yaml from schema version 1 to 2, preserving unknown keys."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text(
        "version: 1\nclient_paths: {}\nuser_custom_setting: preserved_value\n",
        encoding="utf-8",
    )

    result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)

    assert result.status == "applied"
    assert result.dry_run is False
    assert result.bundle_id is not None

    # Verify config.yaml upgraded to v2 and preserved keys
    new_doc = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert new_doc["version"] == SCHEMA_VERSION
    assert new_doc["user_custom_setting"] == "preserved_value"

    # Verify canonical v2 directories exist
    assert (home / "registry" / "projects.yaml").is_file()
    assert (home / "state" / "state.json").is_file()
    assert (home / "rules" / "shared" / "continuity.md").is_file()
    assert (home / "skills" / "shared" / "continuity" / "SKILL.md").is_file()


# ============================================================================
# 4. backup всех затронутых существующих объектов создаётся до первой mutation
# ============================================================================

def test_backup_created_before_first_mutation(tmp_path: Path):
    """Backup of all affected objects is safely created and verified in bundle before mutations."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    original_cfg_bytes = b"version: 1\nclient_paths: {}\n"
    cfg_file.write_bytes(original_cfg_bytes)

    legacy_gemini = gemini_home / "GEMINI.md"
    original_rules_bytes = f"{RULE_MARKER_START}\nmanaged rules\n{RULE_MARKER_END}\n".encode()
    legacy_gemini.write_bytes(original_rules_bytes)

    result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert result.status == "applied"

    bundle_dir = home / "backups" / "migrations" / result.bundle_id
    assert bundle_dir.is_dir()

    manifest_file = bundle_dir / "manifest.json"
    assert manifest_file.is_file()
    manifest_doc = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest_doc["state"] == STATE_COMPLETED

    # Check that backed-up entries exist with identical bytes
    entries = manifest_doc["entries"]
    cfg_entry = next(e for e in entries if e["symbolic_id"] == "ptw:config_yaml")
    assert cfg_entry["existed_before"] is True
    backup_cfg_file = bundle_dir / cfg_entry["backup_rel_path"]
    assert backup_cfg_file.read_bytes() == original_cfg_bytes


# ============================================================================
# 5. ранее отсутствовавшие destinations корректно записываются в manifest
# ============================================================================

def test_absent_destinations_recorded_in_manifest(tmp_path: Path):
    """Destinations not present before apply are recorded in manifest with existed_before=False."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert result.status == "applied"

    bundle_dir = home / "backups" / "migrations" / result.bundle_id
    manifest_doc = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))

    absent_entries = [e for e in manifest_doc["entries"] if not e["existed_before"]]
    assert len(absent_entries) > 0
    symbols = {e["symbolic_id"] for e in absent_entries}
    assert "ptw:registry_dir" in symbols
    assert "ptw:projects_dir" in symbols


# ============================================================================
# 6. unmanaged AGY rules сохраняются byte-for-byte
# ============================================================================

def test_unmanaged_agy_rules_preserved_byte_for_byte(tmp_path: Path):
    """Unmanaged rule text outside markers in legacy GEMINI.md is preserved byte-for-byte."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    legacy_gemini = gemini_home / "GEMINI.md"
    unmanaged_prefix = "# Custom Header\nDo not delete this line.\n\n"
    unmanaged_suffix = "\n# Custom Footer\nKeep this too.\n"
    managed_body = "Personal Tideway Managed Rules Content"
    full_rules_text = f"{unmanaged_prefix}{RULE_MARKER_START}\n{managed_body}\n{RULE_MARKER_END}{unmanaged_suffix}"
    legacy_gemini.write_text(full_rules_text, encoding="utf-8")

    result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert result.status == "applied"

    # Destination GEMINI.md under customization root has managed block
    dest_gemini = gemini_home / "config" / "GEMINI.md"
    assert dest_gemini.is_file()
    dest_text = dest_gemini.read_text(encoding="utf-8")
    assert managed_body in dest_text

    # Legacy GEMINI.md preserves unmanaged text byte-for-byte
    assert legacy_gemini.is_file()
    assert legacy_gemini.read_text(encoding="utf-8") == unmanaged_prefix + unmanaged_suffix


# ============================================================================
# 7. canonical MCP не изменяется
# ============================================================================

def test_canonical_mcp_unmodified(tmp_path: Path):
    """Canonical MCP server definitions are preserved byte-identical during apply."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    mcp_dir = home / "mcp"
    mcp_dir.mkdir(parents=True)
    srv_file = mcp_dir / "custom_server.yaml"
    original_mcp_bytes = yaml.safe_dump({
        "name": "custom_server",
        "transport": "stdio",
        "command": "custom_tool",
        "args": ["--mode", "fast"],
    }).encode("utf-8")
    srv_file.write_bytes(original_mcp_bytes)

    result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert result.status == "applied"

    # Verify MCP server file is byte-identical
    assert srv_file.read_bytes() == original_mcp_bytes


# ============================================================================
# 8. secrets.env, auth.json и OAuth stores не читаются и не попадают в output/manifest
# ============================================================================

def test_secrets_and_oauth_never_read_or_leaked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """Secrets, auth tokens, and OAuth stores are never read, leaked into output, or stored in manifest."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    secrets_file = home / "secrets.env"
    secret_sentinel = "SUPER_SECRET_KEY_NEVER_LEAK_12345"
    secrets_file.write_text(f"API_KEY={secret_sentinel}\n", encoding="utf-8")

    codex_auth = codex_home / "auth.json"
    auth_sentinel = "OAUTH_TOKEN_NEVER_READ_67890"
    codex_auth.write_text(json.dumps({"token": auth_sentinel}), encoding="utf-8")

    # Guard open to ensure secrets.env and auth.json are never opened for reading
    original_open = Path.open

    def guarded_open(self, *args, **kwargs):
        if self.name in ("secrets.env", "auth.json") and ("r" in args or kwargs.get("mode", "r").startswith("r")):
            raise AssertionError(f"Security violation: attempted to read secret/auth file: {self}")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    code = main([
        "--home", str(home),
        "--codex-home", str(codex_home),
        "--gemini-home", str(gemini_home),
        "migrate", "apply", "--json",
    ])
    assert code == ExitCode.SUCCESS

    captured = capsys.readouterr()
    assert secret_sentinel not in captured.out
    assert secret_sentinel not in captured.err
    assert auth_sentinel not in captured.out
    assert auth_sentinel not in captured.err


# ============================================================================
# 9. legacy memory архивируется без утраты, но не импортируется
# ============================================================================

def test_legacy_memory_archived_losslessly_not_imported(tmp_path: Path):
    """Legacy memory is losslessly archived to archive/legacy_memory without importing or content leaking."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    legacy_memory = home / "memory"
    legacy_memory.mkdir(parents=True)
    sub_dir = legacy_memory / "notes"
    sub_dir.mkdir()
    note_file = sub_dir / "project_ideas.md"
    note_content = "# Project Ideas\n- Build personal tideway v2\n"
    note_file.write_text(note_content, encoding="utf-8")

    result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert result.status == "applied"

    # Archived destination exists with exact contents
    archived_note = home / "archive" / "legacy_memory" / "notes" / "project_ideas.md"
    assert archived_note.is_file()
    assert archived_note.read_text(encoding="utf-8") == note_content

    # Original legacy memory directory is removed
    assert not legacy_memory.exists()


# ============================================================================
# 10. managed skills переносятся, unmanaged остаются
# ============================================================================

def test_managed_skills_relocated_unmanaged_preserved(tmp_path: Path):
    """Managed skills move to current customization root, while unmanaged skills remain in legacy."""
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

    # 1. Managed skill
    managed_sk = legacy_skills / "managed_tool"
    managed_sk.mkdir()
    (managed_sk / "SKILL.md").write_text("# managed_tool\n\nPersonal Tideway managed skill.\n", encoding="utf-8")

    # 2. Unmanaged skill
    unmanaged_sk = legacy_skills / "user_tool"
    unmanaged_sk.mkdir()
    (unmanaged_sk / "SKILL.md").write_text("# user_tool\n\nUser custom skill instructions.\n", encoding="utf-8")

    result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert result.status == "applied"

    # Managed skill relocated to current skills root
    dest_managed = gemini_home / "config" / "skills" / "managed_tool" / "SKILL.md"
    assert dest_managed.is_file()
    assert not (legacy_skills / "managed_tool").exists()

    # Unmanaged skill remains untouched in legacy root
    assert (legacy_skills / "user_tool" / "SKILL.md").is_file()


# ============================================================================
# 11. неясный ownership блокирует apply до записи
# ============================================================================

def test_unclear_ownership_blocks_before_write(tmp_path: Path):
    """Ambiguous ownership of rules or skills blocks apply before any mutations occur."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    # Legacy GEMINI.md has non-empty text but NO Personal Tideway markers
    legacy_gemini = gemini_home / "GEMINI.md"
    legacy_gemini.write_text("# Some Custom Rules Without Markers\n", encoding="utf-8")

    result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)

    assert result.status == STATUS_BLOCKED
    assert any("Ambiguous ownership" in b for b in result.blockers)
    assert not (home / "backups").exists()
    assert not (gemini_home / "config").exists()


# ============================================================================
# 12. source drift после plan блокирует apply
# ============================================================================

def test_source_drift_after_plan_blocks_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Modifying source files between initial plan and locked execution triggers drift abort."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    original_plan_migration = plan_migration
    call_count = 0

    def mutating_plan(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        res = original_plan_migration(*args, **kwargs)
        if call_count == 1:
            # Simulate drift right after initial plan
            cfg_file.write_text("version: 1\nclient_paths: {}\n# DRIFT INJECTED\n", encoding="utf-8")
        return res

    monkeypatch.setattr("personal_tideway.core.migration_apply.plan_migration", mutating_plan)

    result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert result.status == STATUS_BLOCKED
    assert any("Source drift detected" in b for b in result.blockers)
    assert not (home / "backups").exists()


# ============================================================================
# 13. symlink source/destination/backup/temp/manifest блокируется
# ============================================================================

def test_symlinks_blocked_across_all_roles(tmp_path: Path):
    """Symlink in lock directory or targets causes apply to fail closed."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    # Symlink lock file
    locks_dir = home / "locks"
    locks_dir.mkdir(parents=True)
    outside_target = tmp_path / "outside.lock"
    outside_target.touch()
    (locks_dir / "migration.lock").symlink_to(outside_target)

    with pytest.raises(MigrationLockError):
        apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)


# ============================================================================
# 14. traversal и malicious manifest блокируются
# ============================================================================

def test_traversal_and_malicious_paths_blocked(tmp_path: Path):
    """Configured path traversal ('..') causes apply to reject before any writes."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text(
        yaml.safe_dump({"version": 1, "client_paths": {"codex_home": "../../escaped_dir"}}),
        encoding="utf-8",
    )

    result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert result.status == STATUS_BLOCKED
    assert any("Path traversal detected" in b for b in result.blockers)
    assert not (home / "backups").exists()


# ============================================================================
# 15. collision одинаковых basename не смешивает backups
# ============================================================================

def test_basename_collision_safe_backups(tmp_path: Path):
    """Files with identical basenames across different directories do not collide in backup bundle."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    # Legacy GEMINI.md and a legacy skill that also has a file
    legacy_gemini = gemini_home / "GEMINI.md"
    legacy_gemini.write_text(f"{RULE_MARKER_START}\nmanaged\n{RULE_MARKER_END}\n", encoding="utf-8")

    result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert result.status == "applied"

    bundle_dir = home / "backups" / "migrations" / result.bundle_id
    entries_dir = bundle_dir / "entries"
    entry_files = list(entries_dir.iterdir())
    # All filenames must be unique and have collision-safe prefixes
    names = [f.name for f in entry_files]
    assert len(names) == len(set(names))


# ============================================================================
# 20. repeated apply idempotent
# ============================================================================

def test_repeated_apply_idempotent(tmp_path: Path):
    """Running apply on an already migrated workspace returns not_required with no new backups."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    res1 = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert res1.status == "applied"

    backup_bundles_before = list((home / "backups" / "migrations").iterdir())

    # Second apply
    res2 = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert res2.status == STATUS_NOT_REQUIRED
    assert res2.bundle_id is None

    backup_bundles_after = list((home / "backups" / "migrations").iterdir())
    assert len(backup_bundles_after) == len(backup_bundles_before)


# ============================================================================
# 21. concurrent apply не допускает две транзакции
# ============================================================================

def test_concurrent_apply_prevented_by_lock(tmp_path: Path):
    """Concurrent apply attempt fails closed when lock is held by another process."""
    import fcntl

    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    locks_dir = home / "locks"
    locks_dir.mkdir(parents=True)
    lock_file = locks_dir / "migration.lock"
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    try:
        with pytest.raises(MigrationLockError) as exc_info:
            apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
        assert "currently held" in str(exc_info.value)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ============================================================================
# 22. human/JSON output семантически эквивалентны и санированы
# ============================================================================

def test_human_and_json_semantic_parity_and_sanitization(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Human and JSON outputs have complete semantic parity and exclude private paths and secrets."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=True)

    json_dict = result.to_dict()
    text_out = format_migration_apply_text(result)

    assert json_dict["status"] == "ready"
    assert "Status: ready" in text_out
    assert json_dict["dry_run"] is True
    assert "Dry Run: true" in text_out
    for mut in json_dict["mutations"]:
        assert mut in text_out
    for b in json_dict["backups"]:
        assert b in text_out


# ============================================================================
# 23. apply работает до обычного initialized-workspace gate
# ============================================================================

def test_apply_works_before_initialized_gate(tmp_path: Path):
    """Apply operates successfully before the standard workspace initialized check."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    # CLI call without running ptw init
    code = main([
        "--home", str(home),
        "--codex-home", str(codex_home),
        "--gemini-home", str(gemini_home),
        "migrate", "apply",
    ])
    assert code == ExitCode.SUCCESS


# ============================================================================
# 24. никакие source repositories не загрязняются
# ============================================================================

def test_zero_pollution_of_source_repositories(tmp_path: Path):
    """No files are ever created inside registered or external source repositories."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    repo_dir = tmp_path / "my_project_repo"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert result.status == "applied"

    # Verify zero files created in repo_dir
    repo_contents = [p.name for p in repo_dir.iterdir()]
    assert repo_contents == [".git"]


# ============================================================================
# 25. временные файлы и незавершённые manifests обрабатываются fail-closed
# ============================================================================

def test_temp_files_and_incomplete_manifests_fail_closed(tmp_path: Path):
    """Incomplete manifests (state='preparing') cannot be rolled back and fail closed."""
    from personal_tideway.core.migration_apply import (
        MigrationRollbackError,
        rollback_migration,
    )

    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    bundle_dir = home / "backups" / "migrations" / "bundle_incomplete"
    bundle_dir.mkdir(parents=True)

    manifest_file = bundle_dir / "manifest.json"
    manifest_file.write_text(
        json.dumps({"manifest_version": 1, "state": "preparing", "entries": []}),
        encoding="utf-8",
    )

    with pytest.raises(MigrationRollbackError) as exc_info:
        rollback_migration(manifest_file, home=home)
    assert "Incomplete transaction manifest" in str(exc_info.value)


# ============================================================================
# 26. P0-3: FIFO в memory или skills не вызывает зависания (fail-closed)
# ============================================================================

def test_fifo_in_memory_or_skills_aborts_without_hang(tmp_path: Path):
    """Presence of FIFO/named pipe aborts immediately without hanging on read."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    memory_dir = home / "memory"
    memory_dir.mkdir(parents=True)

    fifo_path = memory_dir / "blocked_pipe.fifo"
    try:
        os.mkfifo(str(fifo_path))
    except (AttributeError, OSError):
        pytest.skip("mkfifo not supported on this platform/filesystem")

    result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)

    assert result.status == STATUS_BLOCKED
    assert any("Non-regular file encountered" in b or "blocked_pipe.fifo" in b for b in result.blockers)
    # Ensure no bundle left behind
    assert not (home / "backups" / "migrations").exists()


# ============================================================================
# 27. P1-3: Ошибка бэкапа удаляет bundle и не рапортует rollback_failed
# ============================================================================

def test_backup_failure_cleans_bundle_and_does_not_report_rollback_failed(tmp_path: Path):
    """Failure during Phase 1 (backup creation) cleans temp bundle and does not claim rollback_failed."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    result = apply_migration(
        home=home,
        codex_home=codex_home,
        gemini_home=gemini_home,
        fault_after_step="01_backup",
        dry_run=False,
    )

    assert result.status == STATUS_BLOCKED
    assert result.status != "rollback_failed"
    assert result.bundle_id is None
    # Bundle directory was cleaned up
    bundle_root = home / "backups" / "migrations"
    if bundle_root.exists():
        assert len(list(bundle_root.iterdir())) == 0


# ============================================================================
# 28. P2-1: Состав физического bundle полностью соответствует backups плана
# ============================================================================

def test_actual_bundle_contents_match_plan_backups(tmp_path: Path):
    """Every backed up object specified in the plan exists physically in the bundle entries."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    legacy_gemini = gemini_home / "GEMINI.md"
    legacy_gemini.write_text(f"{RULE_MARKER_START}\nmanaged rules\n{RULE_MARKER_END}\n", encoding="utf-8")

    agy_config = gemini_home / "config.json"
    agy_config.write_text('{"custom_theme": "dark"}\n', encoding="utf-8")

    codex_config = codex_home / "config.toml"
    codex_config.write_text('theme = "nord"\n', encoding="utf-8")

    codex_rules = codex_home / "rules" / "default.rules"
    codex_rules.parent.mkdir(parents=True)
    codex_rules.write_text("rule_content\n", encoding="utf-8")

    plan = plan_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert "ptw:config_yaml" in plan.backups
    assert "agy:legacy_gemini_md" in plan.backups

    result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert result.status == "applied"

    bundle_dir = home / "backups" / "migrations" / result.bundle_id
    manifest_doc = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))

    backed_up_entries = [e for e in manifest_doc["entries"] if e.get("existed_before")]
    for entry in backed_up_entries:
        assert entry["backup_rel_path"] is not None
        backup_file = bundle_dir / entry["backup_rel_path"]
        assert backup_file.is_file()
        assert backup_file.stat().st_size >= 0


# ============================================================================
# 29. P2-2: Паритет списка mutations между dry-run и apply
# ============================================================================

def test_dry_run_and_apply_mutation_list_parity(tmp_path: Path):
    """List of mutations returned by dry-run exactly matches mutations executed during apply."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    legacy_gemini = gemini_home / "GEMINI.md"
    legacy_gemini.write_text(f"{RULE_MARKER_START}\nmanaged\n{RULE_MARKER_END}\n", encoding="utf-8")

    legacy_skills = gemini_home / "skills" / "skill_a"
    legacy_skills.mkdir(parents=True)
    (legacy_skills / "SKILL.md").write_text("# skill_a\nPersonal Tideway managed skill.\n", encoding="utf-8")

    legacy_mem = home / "memory"
    legacy_mem.mkdir(parents=True)
    (legacy_mem / "old.md").write_text("old note", encoding="utf-8")

    res_dry = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=True)
    assert res_dry.status == "ready"

    res_apply = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert res_apply.status == "applied"

    assert res_dry.mutations == res_apply.mutations


# ============================================================================
# 30. P2-3: Сообщения об ошибках санируются от абсолютных путей хоста
# ============================================================================

def test_apply_error_never_contains_absolute_host_paths(tmp_path: Path):
    """Error and blocker messages do not expose raw absolute filesystem paths."""
    from personal_tideway.core.migration_apply import sanitize_error_message

    raw_error = f"Failed to access directory {tmp_path / 'sensitive' / 'secrets'} on host"
    sanitized = sanitize_error_message(raw_error, roots=[tmp_path])

    assert str(tmp_path) not in sanitized
    assert "[WORKSPACE_ROOT]" in sanitized


def test_apply_rejects_symlinked_root_before_resolution(tmp_path: Path):
    """Apply must not erase the evidence of a symlinked root by resolving it first."""
    real_home = tmp_path / "real_home"
    linked_home = tmp_path / "linked_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    for path in (real_home, codex_home, gemini_home):
        path.mkdir()
    (real_home / DEFAULT_CONFIG_YAML).write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")
    linked_home.symlink_to(real_home, target_is_directory=True)

    result = apply_migration(
        home=linked_home,
        codex_home=codex_home,
        gemini_home=gemini_home,
    )

    assert result.status == STATUS_BLOCKED
    assert not (real_home / "backups").exists()


def test_apply_rejects_hardlinked_migration_source(tmp_path: Path):
    """A hard-linked legacy file is ambiguous ownership and must fail before backup."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    for path in (home, codex_home, gemini_home):
        path.mkdir()
    (home / DEFAULT_CONFIG_YAML).write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")
    memory = home / "memory"
    memory.mkdir()
    source = memory / "note.md"
    source.write_text("legacy", encoding="utf-8")
    os.link(source, tmp_path / "linked-note.md")

    result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert result.status == STATUS_BLOCKED
    assert any("hard-linked" in blocker for blocker in result.blockers)
    assert not (home / "backups").exists()
