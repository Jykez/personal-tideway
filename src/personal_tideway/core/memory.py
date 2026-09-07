"""Curated memory management with markdown files and YAML front matter."""

from datetime import datetime, timezone
from pathlib import Path
import re

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.exceptions import ValidationError
from personal_tideway.models import MemoryEntry
from personal_tideway.utils import atomic_write_text


def slugify(text: str) -> str:
    """Convert text to a safe filename slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")


def add_memory(
    cfg: PersonalTidewayConfig,
    title: str,
    content: str,
    tags: list[str] | None = None,
    entry_id: str | None = None,
    dry_run: bool = False,
) -> MemoryEntry:
    """Add a new curated markdown memory entry."""
    if not title or not title.strip():
        raise ValidationError("Memory title cannot be empty")

    tag_list = [t.strip() for t in (tags or []) if t.strip()]
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not entry_id:
        base_slug = slugify(title)
        entry_id = f"{now_iso}-{base_slug}" if base_slug else f"{now_iso}-entry"

    # Avoid filename collision
    filename = f"{entry_id}.md"
    target_path = cfg.memory_dir / filename
    counter = 1
    while target_path.exists():
        counter += 1
        target_path = cfg.memory_dir / f"{entry_id}-{counter}.md"

    actual_id = target_path.stem
    entry = MemoryEntry(
        id=actual_id,
        title=title.strip(),
        tags=tag_list,
        created_at=datetime.now(timezone.utc).isoformat(),
        content=content.strip(),
    )

    if not dry_run:
        cfg.memory_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target_path, entry.to_markdown())

    return entry


def list_memories(cfg: PersonalTidewayConfig) -> list[MemoryEntry]:
    """List all curated memory entries sorted by creation/filename."""
    entries: list[MemoryEntry] = []
    if not cfg.memory_dir.is_dir():
        return entries

    for p in sorted(cfg.memory_dir.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
            entry = MemoryEntry.from_markdown(text)
            entries.append(entry)
        except Exception:
            continue

    entries.sort(key=lambda e: e.created_at, reverse=True)
    return entries


def search_memories(cfg: PersonalTidewayConfig, query: str) -> list[MemoryEntry]:
    """Search memories by query string across title, tags, and content."""
    all_entries = list_memories(cfg)
    if not query or not query.strip():
        return all_entries

    q = query.lower().strip()
    matches: list[MemoryEntry] = []
    for entry in all_entries:
        if (
            q in entry.title.lower()
            or any(q in t.lower() for t in entry.tags)
            or q in entry.content.lower()
            or q in entry.id.lower()
        ):
            matches.append(entry)

    return matches
