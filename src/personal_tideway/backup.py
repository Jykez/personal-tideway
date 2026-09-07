"""File backup operations before client or rule mutations."""

from datetime import datetime, timezone
import hashlib
from pathlib import Path

from personal_tideway.utils import atomic_write_bytes, ensure_safe_path


def create_backup_if_changed(
    target_file: Path,
    new_content: bytes | str,
    backups_dir: Path,
    dry_run: bool = False,
) -> Path | None:
    """Create a timestamped backup in backups_dir if target_file exists and content differs.

    Returns the backup Path if a backup was created (or would be created in dry-run),
    or None if target does not exist or target bytes are identical to new_content.
    """
    if not target_file.is_file():
        return None

    target_bytes = target_file.read_bytes()
    new_bytes = new_content.encode("utf-8") if isinstance(new_content, str) else new_content

    if target_bytes == new_bytes:
        # Avoid duplicate backups when the target bytes would not change
        return None

    # Deterministic timestamp format
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    content_hash = hashlib.sha256(target_bytes).hexdigest()[:8]
    backup_filename = f"{target_file.name}.{ts}.{content_hash}.bak"
    backup_path = backups_dir / backup_filename

    if not dry_run:
        backups_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(backup_path, target_bytes)

    return backup_path


def list_backups(backups_dir: Path, filename_prefix: str | None = None) -> list[Path]:
    """List all backup files in backups_dir, optionally filtered by original filename prefix."""
    if not backups_dir.is_dir():
        return []
    backups = [p for p in backups_dir.iterdir() if p.is_file() and p.name.endswith(".bak")]
    if filename_prefix:
        backups = [p for p in backups if p.name.startswith(filename_prefix)]
    backups.sort(key=lambda p: p.name, reverse=True)
    return backups


def restore_backup(backup_file: Path, target_file: Path, dry_run: bool = False) -> None:
    """Restore target_file from a backup file."""
    if not backup_file.is_file():
        raise FileNotFoundError(f"Backup file not found: {backup_file}")
    if not dry_run:
        target_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target_file, backup_file.read_bytes())
