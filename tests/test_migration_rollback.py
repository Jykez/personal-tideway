"""Acceptance tests for Migration 5B rollback engine.

Tests verify:
- Automatic rollback on fault injection after each mutation boundary
- Accurate restoration of bytes, modes, and removal of transaction-created destinations
- Preservation and reporting of new post-migration central memory
- Tampered backup hash detection and fail-closed rejection
- Idempotent repeated rollback
- Directory traversal and malicious manifest boundaries rejection

NOTE: Per Codex execution instructions, all tests in this suite are WRITTEN but NOT RUN
by the agent. Codex will run every test in the verification phase.
"""

import json
import stat
from pathlib import Path

import pytest

from personal_tideway.constants import (
    DEFAULT_CONFIG_YAML,
    RULE_MARKER_END,
    RULE_MARKER_START,
)
from personal_tideway.core.migration_apply import (
    MigrationRollbackError,
    TamperedBackupError,
    apply_migration,
    rollback_migration,
)
from personal_tideway.exceptions import ValidationError


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
# 16. fault injection после каждого mutation boundary вызывает полный rollback
# ============================================================================

@pytest.mark.parametrize(
    "fault_step",
    [
        "02_init_dirs",
        "03_update_config",
        "04_relocate_rules",
        "05_relocate_skills",
        "06_archive_memory",
    ],
)
def test_fault_injection_at_each_boundary_triggers_full_rollback(tmp_path: Path, fault_step: str):
    """Fault injection at any mutation boundary triggers automatic rollback to original state."""
    home = tmp_path / f"ptw_home_{fault_step}"
    codex_home = tmp_path / f"codex_home_{fault_step}"
    gemini_home = tmp_path / f"gemini_home_{fault_step}"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    # 1. Setup v1 state
    cfg_file = home / DEFAULT_CONFIG_YAML
    original_cfg_text = "version: 1\nclient_paths: {}\nsetting: original_val\n"
    cfg_file.write_text(original_cfg_text, encoding="utf-8")

    legacy_gemini = gemini_home / "GEMINI.md"
    original_rules_text = f"{RULE_MARKER_START}\nmanaged rules\n{RULE_MARKER_END}\n"
    legacy_gemini.write_text(original_rules_text, encoding="utf-8")

    legacy_skills = gemini_home / "skills"
    legacy_skills.mkdir(parents=True)
    managed_sk = legacy_skills / "my_skill"
    managed_sk.mkdir()
    (managed_sk / "SKILL.md").write_text("# my_skill\n\nPersonal Tideway managed skill.\n", encoding="utf-8")

    legacy_memory = home / "memory"
    legacy_memory.mkdir(parents=True)
    (legacy_memory / "note.md").write_text("# Legacy Note\n", encoding="utf-8")

    # 2. Run apply with fault injection
    result = apply_migration(
        home=home,
        codex_home=codex_home,
        gemini_home=gemini_home,
        fault_after_step=fault_step,
        dry_run=False,
    )

    # Every injected failure here occurs after the first filesystem mutation.
    assert result.status == "rolled_back"
    assert result.error is not None
    assert "Automatic rollback succeeded" in result.error

    # Verify original files are restored exactly
    assert cfg_file.read_text(encoding="utf-8") == original_cfg_text
    assert legacy_gemini.read_text(encoding="utf-8") == original_rules_text
    assert (legacy_skills / "my_skill" / "SKILL.md").is_file()
    assert (legacy_memory / "note.md").read_text(encoding="utf-8") == "# Legacy Note\n"

    # Verify transaction-created destinations are cleaned up
    assert not (gemini_home / "config" / "GEMINI.md").exists()
    assert not (gemini_home / "config" / "skills" / "my_skill").exists()
    assert not (home / "archive" / "legacy_memory").exists()


# ============================================================================
# 17. rollback восстанавливает bytes, modes и отсутствие исходно отсутствовавших paths
# ============================================================================

def test_rollback_restores_bytes_modes_and_absent_paths(tmp_path: Path):
    """Rollback restores exact file bytes, permissions, and deletes created destinations."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    original_cfg_bytes = b"version: 1\nclient_paths: {}\nflag: test\n"
    cfg_file.write_bytes(original_cfg_bytes)
    cfg_file.chmod(0o640)

    legacy_gemini = gemini_home / "GEMINI.md"
    original_rules_bytes = f"{RULE_MARKER_START}\nmanaged\n{RULE_MARKER_END}\n".encode()
    legacy_gemini.write_bytes(original_rules_bytes)
    legacy_gemini.chmod(0o600)

    # Successful apply
    apply_res = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert apply_res.status == "applied"

    manifest_rel = apply_res.rollback_evidence["manifest_path"]
    manifest_file = home / manifest_rel

    # Execute rollback via Python API
    rb_res = rollback_migration(manifest_file, home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert rb_res.status == "rolled_back"

    # Verify bytes and permissions
    assert cfg_file.read_bytes() == original_cfg_bytes
    assert (cfg_file.stat().st_mode & 0o777) == 0o640

    assert legacy_gemini.read_bytes() == original_rules_bytes
    assert (legacy_gemini.stat().st_mode & 0o777) == 0o600

    # Verify created destination was cleaned up
    assert not (gemini_home / "config" / "GEMINI.md").exists()


# ============================================================================
# 18. rollback сохраняет новую post-migration central memory
# ============================================================================

def test_rollback_preserves_post_migration_central_memory(tmp_path: Path):
    """New central memory created after successful migration is preserved and reported during rollback."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    # Legacy memory
    legacy_mem = home / "memory"
    legacy_mem.mkdir(parents=True)
    (legacy_mem / "old_note.md").write_text("# Old Note\n", encoding="utf-8")

    # Apply migration
    apply_res = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert apply_res.status == "applied"
    assert not legacy_mem.exists()

    # User works in v2 and creates new central memory
    central_memory = home / "memory"
    central_memory.mkdir(parents=True)
    new_note = central_memory / "post_migration_decision.md"
    new_note_content = "# Verified Decision\nKeep this note after rollback.\n"
    new_note.write_text(new_note_content, encoding="utf-8")

    manifest_rel = apply_res.rollback_evidence["manifest_path"]
    manifest_file = home / manifest_rel

    # Run rollback
    rb_res = rollback_migration(manifest_file, home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert rb_res.status == "rolled_back"

    # Old memory restored
    assert (home / "memory" / "old_note.md").is_file()

    # Post-migration note is PRESERVED and reported
    assert new_note.is_file()
    assert new_note.read_text(encoding="utf-8") == new_note_content
    assert any("post_migration_decision.md" in p for p in rb_res.preserved_memory)


# ============================================================================
# 19. tampered backup hash блокирует rollback
# ============================================================================

def test_tampered_backup_hash_blocks_rollback(tmp_path: Path):
    """Tampering with backup bytes causes rollback to abort with TamperedBackupError."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    apply_res = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert apply_res.status == "applied"

    bundle_dir = home / "backups" / "migrations" / apply_res.bundle_id
    manifest_file = bundle_dir / "manifest.json"

    # Tamper with backup file
    backup_cfg = bundle_dir / "entries" / "001_ptw__config_yaml.bin"
    backup_cfg.write_bytes(b"TAMPERED_CONTENT_CORRUPTED_HASH")

    with pytest.raises(TamperedBackupError) as exc_info:
        rollback_migration(manifest_file, home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert "Tampered backup detected" in str(exc_info.value)


# ============================================================================
# 20. repeated rollback idempotent
# ============================================================================

def test_repeated_rollback_idempotent(tmp_path: Path):
    """Executing rollback multiple times on the same transaction succeeds idempotently."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    original_text = "version: 1\nclient_paths: {}\nkey: stable\n"
    cfg_file.write_text(original_text, encoding="utf-8")

    apply_res = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert apply_res.status == "applied"

    manifest_file = home / apply_res.rollback_evidence["manifest_path"]

    # First rollback
    rb_res1 = rollback_migration(manifest_file, home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert rb_res1.status == "rolled_back"
    assert cfg_file.read_text(encoding="utf-8") == original_text

    # Second rollback on same manifest
    rb_res2 = rollback_migration(manifest_file, home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert rb_res2.status == "rolled_back"
    assert cfg_file.read_text(encoding="utf-8") == original_text


# ============================================================================
# Malicious manifest and path boundary rejection
# ============================================================================

def test_malicious_manifest_traversal_rejected(tmp_path: Path):
    """Manifest attempting directory traversal outside backup root or target roots is rejected."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)

    bundle_dir = home / "backups" / "migrations" / "bundle_evil"
    bundle_dir.mkdir(parents=True)
    manifest_file = bundle_dir / "manifest.json"

    # Traversal in rel_path
    evil_manifest = {
        "manifest_version": 1,
        "state": "completed",
        "entries": [
            {
                "symbolic_id": "evil_target",
                "root_key": "ptw_home",
                "rel_path": "../../etc/evil_target",
                "existed_before": False,
            }
        ],
    }
    manifest_file.write_text(json.dumps(evil_manifest), encoding="utf-8")

    with pytest.raises(MigrationRollbackError) as exc_info:
        rollback_migration(manifest_file, home=home)
    assert "Unsafe path in manifest entry" in str(exc_info.value)


def test_manifest_outside_migration_backup_root_rejected(tmp_path: Path):
    """Manifest placed outside home/backups/migrations is rejected."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)

    outside_manifest = tmp_path / "outside_manifest.json"
    outside_manifest.write_text(
        json.dumps({"manifest_version": 1, "state": "completed", "entries": []}),
        encoding="utf-8",
    )

    with pytest.raises((ValidationError, MigrationRollbackError)):
        rollback_migration(outside_manifest, home=home)


# ============================================================================
# Regression: P0-1 Циклический импорт
# ============================================================================

def test_isolated_migration_apply_import():
    """Verify that importing migration_apply has no circular dependency on migration."""
    import sys

    saved_modules = {k: v for k, v in sys.modules.items() if "personal_tideway" in k}
    for k in list(saved_modules.keys()):
        del sys.modules[k]
    try:
        from personal_tideway.core.migration import plan_migration  # noqa: F401
        from personal_tideway.core.migration_apply import (
            rollback_migration,  # noqa: F401
        )
    finally:
        sys.modules.update(saved_modules)


# ============================================================================
# Regression: P0-2 Защита от удаления корня при rel_path: '.'
# ============================================================================

def test_manifest_with_dot_rel_path_rejected_without_deletion(tmp_path: Path):
    """Manifest with rel_path '.' or '' cannot trigger root workspace deletion (P0-2)."""
    home = tmp_path / "ptw_home"
    home.mkdir(parents=True)
    (home / "important_file.txt").write_text("keep this safe", encoding="utf-8")

    bundle_dir = home / "backups" / "migrations" / "bundle_dot"
    bundle_dir.mkdir(parents=True)
    manifest_file = bundle_dir / "manifest.json"

    dot_manifest = {
        "manifest_version": 1,
        "state": "completed",
        "entries": [
            {
                "symbolic_id": "root_threat",
                "root_key": "ptw_home",
                "rel_path": ".",
                "existed_before": False,
            }
        ],
    }
    manifest_file.write_text(json.dumps(dot_manifest), encoding="utf-8")

    with pytest.raises(MigrationRollbackError) as exc_info:
        rollback_migration(manifest_file, home=home)
    assert "Unsafe path in manifest entry" in str(exc_info.value)
    # Ensure home root was NOT deleted
    assert home.is_dir()
    assert (home / "important_file.txt").is_file()


# ============================================================================
# Regression: P1-1 Коллизии post-migration memory не затираются
# ============================================================================

def test_rollback_preserves_colliding_post_migration_central_memory_file(tmp_path: Path):
    """Colliding post-migration central memory file is preserved under .post_migration name (P1-1)."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    # Legacy memory
    legacy_mem = home / "memory"
    legacy_mem.mkdir(parents=True)
    old_file = legacy_mem / "notes.md"
    old_file.write_text("# Original v1 Notes\n", encoding="utf-8")

    apply_res = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert apply_res.status == "applied"
    assert not legacy_mem.exists()

    # User creates memory/notes.md post-migration with different content
    legacy_mem.mkdir(parents=True)
    v2_content = "# V2 Post-Migration Notes that must not be destroyed\n"
    old_file.write_text(v2_content, encoding="utf-8")

    manifest_rel = apply_res.rollback_evidence["manifest_path"]
    manifest_file = home / manifest_rel

    rb_res = rollback_migration(manifest_file, home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert rb_res.status == "rolled_back"

    # Original v1 note restored at canonical path
    assert old_file.read_text(encoding="utf-8") == "# Original v1 Notes\n"

    # Colliding post-migration file preserved
    preserved_file = legacy_mem / "notes.md.post_migration"
    assert preserved_file.is_file()
    assert preserved_file.read_text(encoding="utf-8") == v2_content
    assert any("notes.md.post_migration" in p for p in rb_res.preserved_memory)


# ============================================================================
# Regression: P1-2 Удаление перемещённых навыков при существовавшем target parent
# ============================================================================

def test_rollback_removes_relocated_skill_when_target_parent_existed(tmp_path: Path):
    """Relocated skill destination is removed on rollback even if target parent dir existed before (P1-2)."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    # Target parent exists prior to migration
    dest_skills_dir = gemini_home / "config" / "skills"
    dest_skills_dir.mkdir(parents=True)
    preexisting_skill = dest_skills_dir / "preexisting"
    preexisting_skill.mkdir()
    (preexisting_skill / "SKILL.md").write_text("# preexisting\n", encoding="utf-8")

    # Legacy skill to migrate
    legacy_skills = gemini_home / "skills"
    legacy_skills.mkdir(parents=True)
    migrated_skill = legacy_skills / "my_migrated_skill"
    migrated_skill.mkdir()
    (migrated_skill / "SKILL.md").write_text("# my_migrated_skill\nPersonal Tideway managed skill.\n", encoding="utf-8")

    apply_res = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert apply_res.status == "applied"

    # Verify skill was relocated
    assert (dest_skills_dir / "my_migrated_skill" / "SKILL.md").is_file()
    assert not migrated_skill.exists()

    manifest_file = home / apply_res.rollback_evidence["manifest_path"]
    rb_res = rollback_migration(manifest_file, home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert rb_res.status == "rolled_back"

    # Verify restored to legacy location
    assert (migrated_skill / "SKILL.md").is_file()

    # Verify deleted from relocated location to prevent duplicate skills!
    assert not (dest_skills_dir / "my_migrated_skill").exists()

    # Preexisting skill in target parent was preserved untouched
    assert (preexisting_skill / "SKILL.md").is_file()


# ============================================================================
# Regression: P2-5 & P3-1 Очистка archive и сохранение прав директорий
# ============================================================================

def test_rollback_cleans_empty_archive_directory_and_restores_dir_mode(tmp_path: Path):
    """Rollback cleans up empty archive directory and restores directory permissions (P2-5, P3-1)."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    home.mkdir(parents=True)
    codex_home.mkdir(parents=True)
    gemini_home.mkdir(parents=True)

    cfg_file = home / DEFAULT_CONFIG_YAML
    cfg_file.write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")

    legacy_mem = home / "memory"
    legacy_mem.mkdir(parents=True)
    (legacy_mem / "old.md").write_text("content", encoding="utf-8")
    legacy_mem.chmod(0o750)

    apply_res = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home, dry_run=False)
    assert apply_res.status == "applied"
    assert (home / "archive" / "legacy_memory").is_dir()

    manifest_file = home / apply_res.rollback_evidence["manifest_path"]
    rb_res = rollback_migration(manifest_file, home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert rb_res.status == "rolled_back"

    # Restored legacy memory
    assert legacy_mem.is_dir()
    assert (legacy_mem.stat().st_mode & 0o777) == 0o750

    # Archive directory cleaned up
    assert not (home / "archive").exists()


def test_rollback_restores_empty_nested_directories_and_modes(tmp_path: Path):
    """Directory-only legacy structure is restored exactly, including nested modes."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    for path in (home, codex_home, gemini_home):
        path.mkdir()
    (home / DEFAULT_CONFIG_YAML).write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")
    empty_dir = home / "memory" / "nested" / "empty"
    empty_dir.mkdir(parents=True)
    empty_dir.chmod(0o710)

    apply_result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    manifest = home / apply_result.rollback_evidence["manifest_path"]
    rollback_migration(manifest, home=home, codex_home=codex_home, gemini_home=gemini_home)

    assert empty_dir.is_dir()
    assert stat.S_IMODE(empty_dir.stat().st_mode) == 0o710


def test_rollback_rejects_symlinked_backup_component(tmp_path: Path):
    """A replaced intermediate backup directory cannot redirect rollback reads."""
    home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"
    for path in (home, codex_home, gemini_home):
        path.mkdir()
    (home / DEFAULT_CONFIG_YAML).write_text("version: 1\nclient_paths: {}\n", encoding="utf-8")
    apply_result = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    bundle = home / "backups" / "migrations" / apply_result.bundle_id
    entries = bundle / "entries"
    displaced = bundle / "entries-real"
    entries.rename(displaced)
    entries.symlink_to(displaced, target_is_directory=True)

    with pytest.raises(MigrationRollbackError, match="Symlink escape"):
        rollback_migration(
            bundle / "manifest.json",
            home=home,
            codex_home=codex_home,
            gemini_home=gemini_home,
        )
