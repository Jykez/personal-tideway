"""Tests for backup creation, avoiding duplicate backups, and restoring."""

from pathlib import Path
import pytest

from personal_tideway.backup import create_backup_if_changed, list_backups, restore_backup
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.utils import atomic_write_text


def test_backup_created_only_when_content_changes(personal_tideway_config: PersonalTidewayConfig):
    """Verify backups are created when target bytes differ, and avoided when identical."""
    target_file = personal_tideway_config.codex_home / "config.toml"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target_file, "original content\n")

    # 1. Content differs -> backup MUST be created
    backup1 = create_backup_if_changed(target_file, "new content\n", personal_tideway_config.backups_dir)
    assert backup1 is not None
    assert backup1.is_file()
    assert backup1.read_text(encoding="utf-8") == "original content\n"

    # 2. Content is identical -> backup MUST NOT be created
    backup2 = create_backup_if_changed(target_file, "original content\n", personal_tideway_config.backups_dir)
    assert backup2 is None
    # No second backup file
    assert len(list_backups(personal_tideway_config.backups_dir)) == 1


def test_restore_backup(personal_tideway_config: PersonalTidewayConfig):
    """Verify restoring a backup returns target file to original state."""
    target_file = personal_tideway_config.gemini_home / "GEMINI.md"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target_file, "v1_original")

    backup = create_backup_if_changed(target_file, "v2_updated", personal_tideway_config.backups_dir)
    assert backup is not None

    # Simulate overwrite of target
    atomic_write_text(target_file, "corrupted_state")

    # Restore from backup
    restore_backup(backup, target_file)
    assert target_file.read_text(encoding="utf-8") == "v1_original"


def test_atomic_write_preserves_existing_mode(tmp_path: Path):
    """Verify that atomic replacement preserves existing file mode when mode is None."""
    target = tmp_path / "protected_file.conf"
    atomic_write_text(target, "secret initial content\n", mode=0o600)
    assert (target.stat().st_mode & 0o777) == 0o600

    # Overwrite without specifying mode -> must retain 0o600
    atomic_write_text(target, "updated content\n")
    assert (target.stat().st_mode & 0o777) == 0o600
    assert target.read_text(encoding="utf-8") == "updated content\n"
