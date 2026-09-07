"""Conflict artifacts generation, reading, and resolution removal."""

import difflib
import json
from pathlib import Path
from typing import Any

from personal_tideway.exceptions import ValidationError
from personal_tideway.models import ConflictRecord
from personal_tideway.utils import atomic_write_text


def get_safe_conflict_name(object_id: str) -> str:
    """Generate safe filename from object_id."""
    return object_id.replace(":", "_").replace("/", "_").replace("\\", "_")


def format_human_conflict_report(record: ConflictRecord) -> str:
    """Format human-readable conflict report with unified diff if strings."""
    lines = [
        f"=== CONFLICT REPORT: {record.object_id} ===",
        f"Object Type: {record.object_type}",
        f"Target Client: {record.client}",
        f"Created At: {record.created_at}",
        f"Message: {record.message}",
        "",
        "--- [Base Version Hash] ---",
        str(record.base_hash or "None"),
        "",
        "--- [Personal Tideway Canonical Version Hash] ---",
        str(record.ptw_hash or "None"),
        "",
        "--- [Client Version Hash] ---",
        str(record.client_hash or "None"),
        "",
    ]

    p_str = record.ptw_content if isinstance(record.ptw_content, str) else json.dumps(record.ptw_content, indent=2, sort_keys=True) if record.ptw_content else ""
    c_str = record.client_content if isinstance(record.client_content, str) else json.dumps(record.client_content, indent=2, sort_keys=True) if record.client_content else ""

    if p_str or c_str:
        lines.append("--- Unified Diff (Personal Tideway -> Client) ---")
        diff = difflib.unified_diff(
            p_str.splitlines(keepends=True),
            c_str.splitlines(keepends=True),
            fromfile="Personal Tideway Canonical",
            tofile=f"Client ({record.client})",
        )
        diff_text = "".join(diff)
        lines.append(diff_text if diff_text else "(Values differ in structure or metadata)")

    lines.append("\n=== RESOLUTION INSTRUCTIONS ===")
    lines.append(f"To resolve, run:")
    lines.append(f"  ptw resolve {record.object_id} --take ptw")
    if record.client in ("codex", "agy"):
        lines.append(f"  ptw resolve {record.object_id} --take {record.client}")
    lines.append("")
    return "\n".join(lines)


def save_conflict_artifacts(
    conflicts_dir: Path,
    record: ConflictRecord,
    dry_run: bool = False,
) -> tuple[Path, Path]:
    """Save machine-readable JSON and human-readable TXT conflict artifacts."""
    safe_name = get_safe_conflict_name(record.object_id)
    json_path = conflicts_dir / f"{safe_name}.json"
    txt_path = conflicts_dir / f"{safe_name}.txt"

    if dry_run:
        return json_path, txt_path

    # Check if identical conflict record already exists to preserve mtimes
    if json_path.is_file():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
            substantive_keys = [
                "object_id", "object_type", "client", "base_hash",
                "ptw_hash", "client_hash", "ptw_content", "client_content", "message"
            ]
            if all(existing_data.get(k) == getattr(record, k) for k in substantive_keys):
                return json_path, txt_path
        except Exception:
            pass

    json_content = json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n"
    txt_content = format_human_conflict_report(record)

    conflicts_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(json_path, json_content)
    atomic_write_text(txt_path, txt_content)

    return json_path, txt_path


def load_all_conflicts(conflicts_dir: Path) -> dict[str, ConflictRecord]:
    """Load all active conflict records from conflicts directory."""
    conflicts: dict[str, ConflictRecord] = {}
    if not conflicts_dir.is_dir():
        return conflicts

    for path in sorted(conflicts_dir.glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            rec = ConflictRecord.from_dict(data)
            conflicts[rec.object_id] = rec
        except Exception:
            continue

    return conflicts


def load_conflict(conflicts_dir: Path, object_id: str) -> ConflictRecord | None:
    """Find and load conflict record for a specific object_id."""
    safe_name = get_safe_conflict_name(object_id)
    json_path = conflicts_dir / f"{safe_name}.json"
    if not json_path.is_file():
        # Also try searching all conflicts if name mapping was different
        all_c = load_all_conflicts(conflicts_dir)
        return all_c.get(object_id)

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return ConflictRecord.from_dict(data)
    except Exception:
        return None


def remove_conflict_artifacts(
    conflicts_dir: Path,
    object_id: str,
    dry_run: bool = False,
) -> bool:
    """Remove resolved conflict artifacts for the given object_id."""
    safe_name = get_safe_conflict_name(object_id)
    json_path = conflicts_dir / f"{safe_name}.json"
    txt_path = conflicts_dir / f"{safe_name}.txt"

    removed = False
    if not dry_run:
        if json_path.exists():
            json_path.unlink()
            removed = True
        if txt_path.exists():
            txt_path.unlink()
            removed = True

    return removed
