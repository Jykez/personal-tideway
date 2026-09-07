"""Utility functions for atomic file I/O, hashing, and path security."""

import hashlib
import os
from pathlib import Path
import shutil
import uuid
from personal_tideway.exceptions import ValidationError


def atomic_write_bytes(path: Path, content: bytes, mode: int | None = None) -> None:
    """Write bytes atomically using a temporary file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None and path.exists():
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            pass
    tmp_path = path.parent / f".{path.name}.tmp.{uuid.uuid4().hex}"
    try:
        with open(tmp_path, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8", mode: int | None = None) -> None:
    """Write text atomically using a temporary file in the same directory."""
    atomic_write_bytes(path, content.encode(encoding), mode=mode)


def calculate_hash(content: str | bytes) -> str:
    """Calculate SHA-256 hash for string or bytes."""
    if isinstance(content, str):
        data = content.encode("utf-8")
    else:
        data = content
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> str | None:
    """Calculate SHA-256 hash of a file if it exists, else None."""
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def hash_dir(dir_path: Path) -> str | None:
    """Calculate a deterministic hash of a directory tree and its contents."""
    if not dir_path.is_dir():
        return None
    h = hashlib.sha256()
    # Walk deterministically sorted paths
    for root, dirs, files in os.walk(dir_path, followlinks=False):
        dirs.sort()
        files.sort()
        rel_root = Path(root).relative_to(dir_path)
        for d in dirs:
            h.update(f"dir:{rel_root / d}".encode("utf-8"))
        for f in files:
            file_path = Path(root) / f
            rel_file = rel_root / f
            h.update(f"file:{rel_file}".encode("utf-8"))
            if file_path.is_file():
                file_hash = hash_file(file_path) or ""
                h.update(file_hash.encode("utf-8"))
    return h.hexdigest()


def is_safe_path(target: Path, base: Path, follow_symlinks: bool = False) -> bool:
    """Verify that target is inside base (prevent directory traversal).

    If follow_symlinks is False, checks that the path itself is inside base,
    allowing symlinks within base to point outside base.
    """
    try:
        resolved_base = base.resolve()
        if follow_symlinks:
            resolved_target = target.resolve()
        else:
            resolved_target = target.parent.resolve() / target.name
        return resolved_target == resolved_base or resolved_base in resolved_target.parents
    except Exception:
        return False


def ensure_safe_path(target: Path, base: Path, label: str = "Path", follow_symlinks: bool = False) -> Path:
    """Ensure path is within base or raise ValidationError."""
    if not is_safe_path(target, base, follow_symlinks=follow_symlinks):
        raise ValidationError(f"{label} '{target}' traverses outside allowed root '{base}'")
    return target


def safe_remove_link_or_dir(target: Path, allowed_root: Path, dry_run: bool = False) -> bool:
    """Remove a symlink or directory safely inside allowed_root."""
    ensure_safe_path(target, allowed_root, "Removal target", follow_symlinks=False)
    if dry_run:
        return target.exists() or target.is_symlink()

    if target.is_symlink():
        target.unlink()
        return True
    elif target.is_dir():
        shutil.rmtree(target)
        return True
    elif target.is_file():
        target.unlink()
        return True
    return False
