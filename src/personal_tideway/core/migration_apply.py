"""Migration 5B apply engine and rollback mechanism for Personal Tideway.

Provides:
- Deterministic dry-run preview (ptw migrate apply --dry-run [--json])
- Precondition verification, concurrency locking, and drift detection
- Pre-mutation collision-safe backups and transaction manifest
- Safe mutations: schema upgrade, canonical directories, managed rules/skills relocation,
  lossless memory archiving, and unmanaged text/skills preservation
- Fail-closed rollback engine: automatic on apply error or via Python API
"""

import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from personal_tideway.constants import (
    CONTINUITY_RULE_FILENAME,
    CONTINUITY_SKILL_NAME,
    DEFAULT_CONFIG_YAML,
    DEFAULT_PERSONAL_TIDEWAY_DIR,
    RULE_MARKER_END,
    RULE_MARKER_START,
    SCHEMA_VERSION,
    ExitCode,
)
from personal_tideway.core.migration import (
    STATUS_BLOCKED,
    STATUS_NOT_REQUIRED,
    STATUS_READY,
    UniqueKeyLoader,
    _has_symlink_component,
    _is_path_traversal,
    _safe_is_symlink,
    plan_migration,
)
from personal_tideway.core.workspace import (
    DEFAULT_CONTINUITY_RULE_TEMPLATE,
    DEFAULT_CONTINUITY_SKILL_TEMPLATE,
    DEFAULT_PROJECTS_REGISTRY_TEMPLATE,
)
from personal_tideway.exceptions import (
    PersonalTidewayError,
    ValidationError,
)
from personal_tideway.utils import (
    atomic_write_bytes,
    atomic_write_text,
    ensure_safe_path,
    hash_file,
    is_safe_path,
    safe_remove_link_or_dir,
)

MANIFEST_VERSION = 1
STATE_PREPARING = "preparing"
STATE_APPLYING = "applying"
STATE_COMPLETED = "completed"
STATE_ROLLED_BACK = "rolled_back"
STATE_ROLLBACK_FAILED = "rollback_failed"

VALID_MANIFEST_STATES = frozenset({
    STATE_PREPARING,
    STATE_APPLYING,
    STATE_COMPLETED,
    STATE_ROLLED_BACK,
    STATE_ROLLBACK_FAILED,
})

MAX_MANIFEST_BYTES = 1024 * 1024  # 1 MiB cap for manifest


class MigrationError(PersonalTidewayError):
    """Base error for migration failures."""
    exit_code: ExitCode = ExitCode.CONFIG_ERROR


class MigrationLockError(MigrationError):
    """Raised when migration lock cannot be acquired."""
    exit_code: ExitCode = ExitCode.CONFIG_ERROR


class MigrationRollbackError(MigrationError):
    """Raised when rollback operation fails."""
    exit_code: ExitCode = ExitCode.CONFIG_ERROR


class TamperedBackupError(MigrationRollbackError):
    """Raised when backup file hash does not match manifest."""
    exit_code: ExitCode = ExitCode.CONFIG_ERROR


@dataclass(frozen=True)
class MigrationApplyResult:
    """Encapsulates the result of a migration apply or dry-run execution."""

    status: str
    dry_run: bool
    source_schema_version: int | None
    target_schema_version: int
    mutations: list[str]
    backups: list[str]
    rollback_evidence: dict[str, Any]
    blockers: list[str]
    warnings: list[str]
    preserved: list[str]
    bundle_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.status,
            "dry_run": self.dry_run,
            "source_schema_version": self.source_schema_version,
            "target_schema_version": self.target_schema_version,
            "mutations": list(self.mutations),
            "backups": list(self.backups),
            "rollback_evidence": dict(self.rollback_evidence),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "preserved": list(self.preserved),
        }
        if self.bundle_id is not None:
            d["bundle_id"] = self.bundle_id
        if self.error is not None:
            d["error"] = self.error
        return d


@dataclass(frozen=True)
class MigrationRollbackResult:
    """Encapsulates the result of a rollback execution."""

    status: str
    manifest_path: str
    restored_entries: list[str]
    cleaned_destinations: list[str]
    preserved_memory: list[str]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.status,
            "manifest_path": self.manifest_path,
            "restored_entries": list(self.restored_entries),
            "cleaned_destinations": list(self.cleaned_destinations),
            "preserved_memory": list(self.preserved_memory),
        }
        if self.error is not None:
            d["error"] = self.error
        return d


def sanitize_error_message(message: Any, roots: tuple[Path, ...] = ()) -> str:
    """Sanitize error messages by stripping absolute host paths."""
    sanitized = str(message)
    for r in roots:
        try:
            r_res = str(r.resolve())
            if r_res in sanitized:
                sanitized = sanitized.replace(r_res, "[WORKSPACE_ROOT]")
            r_raw = str(r)
            if r_raw in sanitized:
                sanitized = sanitized.replace(r_raw, "[WORKSPACE_ROOT]")
        except (OSError, RuntimeError):
            continue
    home_str = str(Path.home())
    if home_str in sanitized:
        sanitized = sanitized.replace(home_str, "~")
    return sanitized


def format_migration_apply_text(result: MigrationApplyResult) -> str:
    """Format migration apply result for human consumption with complete semantic parity."""
    lines: list[str] = [
        "Migration Apply Report",
        "======================",
        f"Status: {result.status}",
        f"Dry Run: {'true' if result.dry_run else 'false'}",
        f"Source Schema Version: {result.source_schema_version if result.source_schema_version is not None else 'none'}",
        f"Target Schema Version: {result.target_schema_version}",
    ]
    if result.bundle_id is not None:
        lines.append(f"Bundle ID: {result.bundle_id}")
    if result.error is not None:
        lines.append(f"Error: {result.error}")

    lines.append("")
    lines.append("Blockers:")
    if result.blockers:
        for b in sorted(result.blockers):
            lines.append(f"  - {b}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Mutations:")
    if result.mutations:
        for m in sorted(result.mutations):
            lines.append(f"  - {m}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Backups:")
    if result.backups:
        for b in sorted(result.backups):
            lines.append(f"  - {b}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Preserved:")
    if result.preserved:
        for p in sorted(result.preserved):
            lines.append(f"  - {p}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Warnings:")
    if result.warnings:
        for w in sorted(result.warnings):
            lines.append(f"  - {w}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Rollback Evidence:")
    if result.rollback_evidence:
        for k in sorted(result.rollback_evidence.keys()):
            v = result.rollback_evidence[k]
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (none)")

    return "\n".join(lines)


def format_migration_rollback_text(result: MigrationRollbackResult) -> str:
    """Format migration rollback result for human consumption."""
    lines: list[str] = [
        "Migration Rollback Report",
        "========================",
        f"Status: {result.status}",
        f"Manifest: {result.manifest_path}",
    ]
    if result.error is not None:
        lines.append(f"Error: {result.error}")

    lines.append("")
    lines.append("Restored Entries:")
    if result.restored_entries:
        for r in sorted(result.restored_entries):
            lines.append(f"  - {r}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Cleaned Destinations:")
    if result.cleaned_destinations:
        for c in sorted(result.cleaned_destinations):
            lines.append(f"  - {c}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Preserved Memory:")
    if result.preserved_memory:
        for m in sorted(result.preserved_memory):
            lines.append(f"  - {m}")
    else:
        lines.append("  (none)")

    return "\n".join(lines)


def acquire_migration_lock(home: Path) -> int:
    """Acquire exclusive non-blocking migration lock under home/locks/migration.lock.

    Fails closed if locks directory or file is a symlink or held by another process.
    """
    if _has_symlink_component(home):
        raise MigrationLockError("Symlink escape detected: workspace home is a symbolic link")

    locks_dir = home / "locks"
    if _safe_is_symlink(locks_dir):
        raise MigrationLockError("Symlink escape detected: locks directory is a symbolic link")

    if locks_dir.exists() and not locks_dir.is_dir():
        raise MigrationLockError("Unsafe layout: locks target exists but is not a directory")

    locks_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(locks_dir, 0o700)
    except OSError:
        pass

    lock_file = locks_dir / "migration.lock"
    if _safe_is_symlink(lock_file):
        raise MigrationLockError("Symlink escape detected: migration lock file is a symbolic link")

    open_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW

    try:
        fd = os.open(lock_file, open_flags, 0o600)
    except OSError as err:
        if err.errno == getattr(errno, "ELOOP", 40):
            raise MigrationLockError("Symlink escape detected: migration lock file is a symbolic link") from None
        raise MigrationLockError("Failed to open migration lock file") from None

    try:
        st = os.fstat(fd)
        if stat.S_ISLNK(st.st_mode):
            os.close(fd)
            raise MigrationLockError("Symlink escape detected: migration lock file is a symbolic link")
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise MigrationLockError("Migration lock target is not a regular file")
        if st.st_nlink != 1:
            os.close(fd)
            raise MigrationLockError("Migration lock file has invalid link count")

        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as err:
            os.close(fd)
            raise MigrationLockError("Migration lock is currently held by another process") from err

        return fd
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def release_migration_lock(fd: int) -> None:
    """Release exclusive migration lock."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def check_rules_ownership(legacy_rules_path: Path) -> tuple[bool, str | None, str | None]:
    """Check ownership of legacy rules file.

    Returns:
        (is_managed, managed_block_body, error_message)
    """
    if not legacy_rules_path.exists():
        return False, None, None
    if _safe_is_symlink(legacy_rules_path):
        return False, None, "Symlink escape detected: legacy rules is a symbolic link"
    try:
        lstat_res = legacy_rules_path.lstat()
        if not stat.S_ISREG(lstat_res.st_mode):
            return False, None, "Unsafe non-regular file detected: legacy rules exists but is not a regular file"
    except OSError as exc:
        return False, None, f"Failed to inspect legacy rules file: {exc}"

    try:
        content = legacy_rules_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return False, None, f"Failed to read legacy rules file: {exc}"

    start_count = content.count(RULE_MARKER_START)
    end_count = content.count(RULE_MARKER_END)

    if start_count == 0 and end_count == 0:
        if content.strip():
            return False, None, "Ambiguous ownership: legacy AGY rules file contains text without Personal Tideway markers"
        return False, None, None

    if start_count != end_count or start_count > 1:
        return False, None, "Ambiguous or malformed rule markers in legacy AGY rules file"

    start_idx = content.find(RULE_MARKER_START)
    end_idx = content.find(RULE_MARKER_END)
    if end_idx < start_idx:
        return False, None, "Malformed rule markers: end marker appears before start marker"

    managed = content[start_idx + len(RULE_MARKER_START) : end_idx].strip("\r\n")
    return True, managed, None


def check_skills_ownership(
    legacy_skills_dir: Path, canonical_skills_dir: Path | None = None
) -> tuple[dict[str, bool], list[str]]:
    """Check ownership of entries in legacy skills directory.

    Returns:
        (skill_ownership_map, blockers)
    """
    if not legacy_skills_dir.exists():
        return {}, []
    if _safe_is_symlink(legacy_skills_dir):
        return {}, ["Symlink escape detected: legacy skills directory is a symbolic link"]
    if not legacy_skills_dir.is_dir():
        return {}, ["Legacy skills directory exists but is not a directory"]

    ownership: dict[str, bool] = {}
    blockers: list[str] = []

    canonical_names: set[str] = set()
    if canonical_skills_dir and canonical_skills_dir.is_dir():
        for scope in ("shared", "agy", "codex"):
            s_dir = canonical_skills_dir / scope
            if s_dir.is_dir():
                for c_entry in s_dir.iterdir():
                    if c_entry.is_dir():
                        canonical_names.add(c_entry.name)

    try:
        entries = sorted(legacy_skills_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return {}, ["Permission error reading legacy skills directory"]

    for entry in entries:
        if _safe_is_symlink(entry):
            blockers.append(f"Symlink escape detected: legacy skill '{entry.name}' is a symbolic link")
            continue
        try:
            st = entry.lstat()
            if not stat.S_ISDIR(st.st_mode):
                blockers.append(f"Unsafe non-regular file detected: skill entry '{entry.name}' is not a directory")
                continue
        except OSError:
            blockers.append(f"Permission error inspecting skill entry '{entry.name}'")
            continue

        skill_md = entry / "SKILL.md"
        if _safe_is_symlink(skill_md):
            blockers.append(f"Symlink escape detected: SKILL.md in '{entry.name}' is a symbolic link")
            continue
        if not skill_md.exists():
            blockers.append(f"Ambiguous ownership: skill '{entry.name}' missing valid SKILL.md")
            continue
        try:
            st_md = skill_md.lstat()
            if not stat.S_ISREG(st_md.st_mode):
                blockers.append(f"Unsafe non-regular file detected: SKILL.md in '{entry.name}' is not a regular file")
                continue
        except OSError:
            blockers.append(f"Permission error inspecting SKILL.md in '{entry.name}'")
            continue

        try:
            skill_text = skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            blockers.append(f"Ambiguous ownership: unable to read SKILL.md in '{entry.name}'")
            continue

        is_managed = False
        if "Personal Tideway managed skill." in skill_text or entry.name in canonical_names or RULE_MARKER_START in skill_text:
            is_managed = True

        ownership[entry.name] = is_managed

    return ownership, blockers


def _copy_tree_without_symlinks(src_dir: Path, dst_dir: Path) -> list[tuple[Path, Path]]:
    """Copy directory tree verifying no symlinks or non-regular files exist, returning (src, dst) pairs."""
    copied: list[tuple[Path, Path]] = []
    dst_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(dst_dir, src_dir.stat().st_mode & 0o777)
    except OSError:
        pass

    for root, dirs, files in os.walk(src_dir, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(src_dir)
        target_root = dst_dir / rel_root

        for d in sorted(dirs):
            d_path = root_path / d
            try:
                st_d = d_path.lstat()
            except OSError as err:
                raise ValidationError(f"Permission error inspecting directory: {d_path.name}") from err
            if stat.S_ISLNK(st_d.st_mode):
                raise ValidationError(f"Symlink escape detected inside directory: {d_path.name}")
            if not stat.S_ISDIR(st_d.st_mode):
                raise ValidationError(f"Unsafe non-directory entry detected: {d_path.name}")
            t_d = target_root / d
            t_d.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(t_d, st_d.st_mode & 0o777)
            except OSError:
                pass

        for f in sorted(files):
            f_path = root_path / f
            try:
                st_f = f_path.lstat()
            except OSError as err:
                raise ValidationError(f"Permission error inspecting file: {f_path.name}") from err
            if stat.S_ISLNK(st_f.st_mode):
                raise ValidationError(f"Symlink escape detected inside directory: {f_path.name}")
            if not stat.S_ISREG(st_f.st_mode):
                raise ValidationError(f"Unsafe non-regular file detected: {f_path.name}")
            if st_f.st_nlink != 1:
                raise ValidationError(f"Unsafe hard-linked file detected: {f_path.name}")
            t_f = target_root / f
            f_bytes = f_path.read_bytes()
            mode = st_f.st_mode & 0o777
            atomic_write_bytes(t_f, f_bytes, mode=mode)
            if hash_file(t_f) != hash_file(f_path):
                raise MigrationError(f"Backup copy hash verification failed for {f_path.name}")
            copied.append((f_path, t_f))

    return copied


def _safe_tree_hash(root: Path) -> str:
    """Hash a tree without following links or opening non-regular files."""
    digest = hashlib.sha256()
    for current_root, dirs, files in os.walk(root, followlinks=False):
        current = Path(current_root)
        rel_root = current.relative_to(root)
        for directory_name in sorted(dirs):
            directory = current / directory_name
            directory_stat = directory.lstat()
            if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
                raise ValidationError(f"Unsafe directory entry detected: {directory_name}")
            digest.update(f"dir:{rel_root / directory_name}:{stat.S_IMODE(directory_stat.st_mode)}".encode())
        for file_name in sorted(files):
            file_path = current / file_name
            file_stat = file_path.lstat()
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValidationError(f"Unsafe non-regular file detected: {file_name}")
            if file_stat.st_nlink != 1:
                raise ValidationError(f"Unsafe hard-linked file detected: {file_name}")
            digest.update(f"file:{rel_root / file_name}:{file_stat.st_size}".encode())
            file_hash = hash_file(file_path)
            if file_hash is None:
                raise ValidationError(f"Unable to hash migration source file: {file_name}")
            digest.update(file_hash.encode())
    return digest.hexdigest()


def capture_source_snapshot(
    home: Path,
    codex_home: Path,
    gemini_home: Path,
) -> dict[str, str | None]:
    """Capture hashes and states of all relevant migration source files to detect drift."""
    snapshot: dict[str, str | None] = {}
    config_yaml = home / DEFAULT_CONFIG_YAML
    snapshot["config_yaml"] = hash_file(config_yaml) if config_yaml.is_file() else None

    legacy_gemini = gemini_home / "GEMINI.md"
    snapshot["legacy_gemini"] = hash_file(legacy_gemini) if legacy_gemini.is_file() else None

    legacy_skills = gemini_home / "skills"
    if legacy_skills.is_dir():
        snapshot["legacy_skills"] = _safe_tree_hash(legacy_skills)
    else:
        snapshot["legacy_skills"] = None

    legacy_memory = home / "memory"
    if legacy_memory.is_dir():
        snapshot["legacy_memory"] = _safe_tree_hash(legacy_memory)
    else:
        snapshot["legacy_memory"] = None

    for client_p in (
        codex_home / "config.toml",
        codex_home / "AGENTS.md",
        gemini_home / "config" / "mcp_config.json",
    ):
        if client_p.is_file():
            snapshot[str(client_p.name)] = hash_file(client_p)

    return snapshot


def apply_migration(
    home: str | Path | None = None,
    codex_home: str | Path | None = None,
    gemini_home: str | Path | None = None,
    codex_hooks: str | Path | None = None,
    agy_hooks: str | Path | None = None,
    *,
    dry_run: bool = False,
    fault_after_step: str | None = None,
) -> MigrationApplyResult:
    """Safely apply Migration 5A plan with transactional backups and automatic rollback."""
    # Reject raw roots before resolving them so a symlink cannot disappear at
    # the apply boundary before Migration 5A gets a chance to inspect it.
    env_home = os.environ.get("PERSONAL_TIDEWAY_HOME")
    raw_home = Path(home or env_home or (Path.home() / DEFAULT_PERSONAL_TIDEWAY_DIR)).expanduser()
    raw_codex_home = Path(
        codex_home or os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    ).expanduser()
    raw_gemini_home = Path(
        gemini_home
        or os.environ.get("GEMINI_HOME")
        or os.environ.get("AGY_HOME")
        or (Path.home() / ".gemini")
    ).expanduser()
    if any(_has_symlink_component(root) for root in (raw_home, raw_codex_home, raw_gemini_home)):
        return MigrationApplyResult(
            status=STATUS_BLOCKED,
            dry_run=dry_run,
            source_schema_version=None,
            target_schema_version=SCHEMA_VERSION,
            mutations=[],
            backups=[],
            rollback_evidence={"status": STATUS_BLOCKED},
            blockers=["Symlink escape detected: root directory is a symbolic link"],
            warnings=[],
            preserved=[],
        )
    resolved_home = raw_home.resolve()
    resolved_codex_home = raw_codex_home.resolve()
    resolved_gemini_home = raw_gemini_home.resolve()

    # Capture initial source snapshot before planning to detect any subsequent drift
    try:
        initial_snapshot = capture_source_snapshot(
            resolved_home, resolved_codex_home, resolved_gemini_home
        )
    except ValidationError as exc:
        return MigrationApplyResult(
            status=STATUS_BLOCKED,
            dry_run=dry_run,
            source_schema_version=None,
            target_schema_version=SCHEMA_VERSION,
            mutations=[],
            backups=[],
            rollback_evidence={"status": STATUS_BLOCKED},
            blockers=[sanitize_error_message(exc, (resolved_home, resolved_codex_home, resolved_gemini_home))],
            warnings=[],
            preserved=[],
        )

    # 1. Build initial migration plan
    initial_plan = plan_migration(
        home=resolved_home,
        codex_home=resolved_codex_home,
        gemini_home=resolved_gemini_home,
        codex_hooks=codex_hooks,
        agy_hooks=agy_hooks,
    )

    if initial_plan.status == STATUS_BLOCKED:
        return MigrationApplyResult(
            status=STATUS_BLOCKED,
            dry_run=dry_run,
            source_schema_version=initial_plan.source_schema_version,
            target_schema_version=initial_plan.target_schema_version,
            mutations=[],
            backups=[],
            rollback_evidence={"status": STATUS_BLOCKED},
            blockers=initial_plan.blockers,
            warnings=initial_plan.warnings,
            preserved=[],
            bundle_id=None,
        )

    if initial_plan.status == STATUS_NOT_REQUIRED:
        return MigrationApplyResult(
            status=STATUS_NOT_REQUIRED,
            dry_run=dry_run,
            source_schema_version=initial_plan.source_schema_version,
            target_schema_version=initial_plan.target_schema_version,
            mutations=[],
            backups=[],
            rollback_evidence={"status": STATUS_NOT_REQUIRED},
            blockers=[],
            warnings=initial_plan.warnings,
            preserved=initial_plan.preserve,
            bundle_id=None,
        )

    config_yaml = resolved_home / DEFAULT_CONFIG_YAML
    agy_customization_root = resolved_gemini_home / "config"
    agy_current_skills = agy_customization_root / "skills"
    legacy_gemini_md = resolved_gemini_home / "GEMINI.md"
    legacy_skills = resolved_gemini_home / "skills"
    legacy_memory = resolved_home / "memory"

    has_legacy_rules = "agy:legacy_gemini_md" in initial_plan.legacy_paths
    has_legacy_skills = "agy:legacy_skills" in initial_plan.legacy_paths

    # Expected exact mutation steps
    planned_mutations = [
        "02_initialize_canonical_v2_directories",
        "03_migrate_config_schema_to_v2",
    ]
    if has_legacy_rules and legacy_gemini_md.exists():
        planned_mutations.append("04_relocate_agy_rules_to_customization_root")
    if has_legacy_skills and legacy_skills.exists():
        planned_mutations.append("05_relocate_agy_skills_to_customization_root")
    if legacy_memory.exists():
        planned_mutations.append("06_archive_legacy_memory_to_v2_storage")
    planned_mutations.sort()

    # In dry-run mode: return exact deterministic preview without modifying anything or using locks
    if dry_run:
        deterministic_rollback_evidence = {
            "backup_root": "backups/migrations",
            "collision_prevention": "scoped_symbolic_identifiers",
            "restore_guarantees": [
                "byte_identical_restoration",
                "mode_preservation",
                "destination_cleanup",
                "post_migration_memory_preservation",
            ],
            "transaction_states": [
                STATE_PREPARING,
                STATE_APPLYING,
                STATE_COMPLETED,
                STATE_ROLLED_BACK,
                STATE_ROLLBACK_FAILED,
            ],
        }
        return MigrationApplyResult(
            status="ready",
            dry_run=True,
            source_schema_version=initial_plan.source_schema_version,
            target_schema_version=initial_plan.target_schema_version,
            mutations=planned_mutations,
            backups=sorted(initial_plan.backups),
            rollback_evidence=deterministic_rollback_evidence,
            blockers=[],
            warnings=initial_plan.warnings,
            preserved=initial_plan.preserve,
            bundle_id=None,
        )

    # 2. Acquire migration lock
    lock_fd = acquire_migration_lock(resolved_home)
    manifest_path: Path | None = None
    bundle_dir: Path | None = None
    bundle_id: str | None = None

    try:
        # 3. Under lock: re-verify plan and check for drift
        locked_plan = plan_migration(
            home=resolved_home,
            codex_home=resolved_codex_home,
            gemini_home=resolved_gemini_home,
            codex_hooks=codex_hooks,
            agy_hooks=agy_hooks,
        )

        if locked_plan.status != STATUS_READY:
            return MigrationApplyResult(
                status=locked_plan.status,
                dry_run=False,
                source_schema_version=locked_plan.source_schema_version,
                target_schema_version=locked_plan.target_schema_version,
                mutations=[],
                backups=[],
                rollback_evidence={"status": locked_plan.status},
                blockers=locked_plan.blockers,
                warnings=locked_plan.warnings,
                preserved=locked_plan.preserve,
                bundle_id=None,
            )

        # Re-check drift by actions, backups, and file content hashes
        locked_snapshot = capture_source_snapshot(resolved_home, resolved_codex_home, resolved_gemini_home)
        if (
            locked_plan.actions != initial_plan.actions
            or locked_plan.backups != initial_plan.backups
            or locked_snapshot != initial_snapshot
        ):
            return MigrationApplyResult(
                status=STATUS_BLOCKED,
                dry_run=False,
                source_schema_version=locked_plan.source_schema_version,
                target_schema_version=locked_plan.target_schema_version,
                mutations=[],
                backups=[],
                rollback_evidence={"status": "drift_detected"},
                blockers=["Source drift detected between planning and locked execution"],
                warnings=locked_plan.warnings,
                preserved=locked_plan.preserve,
                bundle_id=None,
            )

        # Check rules ownership
        has_legacy_rules = "agy:legacy_gemini_md" in locked_plan.legacy_paths
        managed_rules_body: str | None = None
        if has_legacy_rules:
            _is_managed_rules, managed_rules_body, rules_err = check_rules_ownership(legacy_gemini_md)
            if rules_err:
                return MigrationApplyResult(
                    status=STATUS_BLOCKED,
                    dry_run=False,
                    source_schema_version=locked_plan.source_schema_version,
                    target_schema_version=locked_plan.target_schema_version,
                    mutations=[],
                    backups=[],
                    rollback_evidence={"status": STATUS_BLOCKED},
                    blockers=[rules_err],
                    warnings=locked_plan.warnings,
                    preserved=locked_plan.preserve,
                    bundle_id=None,
                )

        # Check skills ownership
        has_legacy_skills = "agy:legacy_skills" in locked_plan.legacy_paths
        skills_ownership: dict[str, bool] = {}
        if has_legacy_skills:
            skills_ownership, skills_errs = check_skills_ownership(legacy_skills, resolved_home / "skills")
            if skills_errs:
                return MigrationApplyResult(
                    status=STATUS_BLOCKED,
                    dry_run=False,
                    source_schema_version=locked_plan.source_schema_version,
                    target_schema_version=locked_plan.target_schema_version,
                    mutations=[],
                    backups=[],
                    rollback_evidence={"status": STATUS_BLOCKED},
                    blockers=skills_errs,
                    warnings=locked_plan.warnings,
                    preserved=locked_plan.preserve,
                    bundle_id=None,
                )

        # 4. Phase A: Create backup bundle before any mutation
        backups_dir = resolved_home / "backups"
        migrations_backups_dir = backups_dir / "migrations"
        backups_dir_existed = backups_dir.exists()
        migrations_dir_existed = migrations_backups_dir.exists()
        migrations_backups_dir.mkdir(parents=True, exist_ok=True)

        now_utc = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        bundle_id = f"bundle_{now_utc}_{uuid.uuid4().hex[:8]}"
        bundle_dir = migrations_backups_dir / bundle_id
        bundle_dir.mkdir(mode=0o700, parents=True, exist_ok=False)

        entries_dir = bundle_dir / "entries"
        entries_dir.mkdir(mode=0o700, parents=True, exist_ok=False)

        manifest_path = bundle_dir / "manifest.json"
        manifest_entries: list[dict[str, Any]] = []

        # Target definitions to inspect and backup
        targets_to_backup: list[tuple[str, str, Path, str]] = [
            ("ptw:config_yaml", "ptw_home", config_yaml, "file"),
        ]
        if has_legacy_rules and legacy_gemini_md.exists():
            targets_to_backup.append(("agy:legacy_gemini_md", "gemini_home", legacy_gemini_md, "file"))
        if has_legacy_skills and legacy_skills.exists():
            targets_to_backup.append(("agy:legacy_skills", "gemini_home", legacy_skills, "directory"))
        if legacy_memory.exists():
            targets_to_backup.append(("ptw:memory_dir", "ptw_home", legacy_memory, "directory"))

        # Client files from locked_plan.backups
        agy_config = agy_customization_root / "mcp_config.json"
        if "agy:config" in locked_plan.backups and agy_config.exists() and agy_config.is_file():
            targets_to_backup.append(("agy:config", "gemini_home", agy_config, "file"))

        codex_config = resolved_codex_home / "config.toml"
        if "codex:config" in locked_plan.backups and codex_config.exists() and codex_config.is_file():
            targets_to_backup.append(("codex:config", "codex_home", codex_config, "file"))

        codex_rules = resolved_codex_home / "AGENTS.md"
        if "codex:rules" in locked_plan.backups and codex_rules.exists() and codex_rules.is_file():
            targets_to_backup.append(("codex:rules", "codex_home", codex_rules, "file"))

        # Write initial manifest in PREPARING state
        initial_manifest_doc = {
            "manifest_version": MANIFEST_VERSION,
            "state": STATE_PREPARING,
            "bundle_id": bundle_id,
            "source_schema_version": locked_plan.source_schema_version,
            "target_schema_version": locked_plan.target_schema_version,
            "entries": [],
            "destinations_created": [],
            "preserved": list(locked_plan.preserve),
        }
        atomic_write_text(manifest_path, json.dumps(initial_manifest_doc, indent=2) + "\n")

        # Create backups inside protected Phase A
        try:
            for idx, (sym_id, root_key, target_p, t_type) in enumerate(targets_to_backup, start=1):
                if _safe_is_symlink(target_p):
                    raise ValidationError(f"Symlink escape detected: backup target {sym_id} is a symbolic link")
                if not target_p.exists():
                    continue

                try:
                    lst = target_p.lstat()
                    if t_type == "file" and not stat.S_ISREG(lst.st_mode):
                        raise ValidationError(f"Unsafe non-regular file detected: {sym_id}")
                    if t_type == "file" and lst.st_nlink != 1:
                        raise ValidationError(f"Unsafe hard-linked file detected: {sym_id}")
                    if t_type == "directory" and not stat.S_ISDIR(lst.st_mode):
                        raise ValidationError(f"Unsafe non-directory detected: {sym_id}")
                except OSError as err:
                    raise ValidationError(f"Failed to inspect backup target {sym_id}: {err}") from err

                root_dir = resolved_home if root_key == "ptw_home" else (
                    resolved_codex_home if root_key == "codex_home" else resolved_gemini_home
                )
                rel_target = str(target_p.relative_to(root_dir))
                clean_sym = sym_id.replace(":", "__")

                if t_type == "file":
                    f_bytes = target_p.read_bytes()
                    f_mode = target_p.stat().st_mode & 0o777
                    f_hash = hash_file(target_p)
                    backup_rel = f"entries/{idx:03d}_{clean_sym}.bin"
                    backup_target = bundle_dir / backup_rel
                    atomic_write_bytes(backup_target, f_bytes, mode=0o600)

                    # Verify written backup hash
                    if hash_file(backup_target) != f_hash:
                        raise MigrationError(f"Backup verification failed for {sym_id}")

                    manifest_entries.append({
                        "symbolic_id": sym_id,
                        "root_key": root_key,
                        "rel_path": rel_target,
                        "existed_before": True,
                        "entry_type": "file",
                        "mode": f_mode,
                        "sha256": f_hash,
                        "backup_rel_path": backup_rel,
                    })
                elif t_type == "directory":
                    backup_rel = f"entries/{idx:03d}_{clean_sym}"
                    backup_target = bundle_dir / backup_rel
                    _copy_tree_without_symlinks(target_p, backup_target)

                    dir_mode = target_p.stat().st_mode & 0o777
                    manifest_entries.append({
                        "symbolic_id": sym_id,
                        "root_key": root_key,
                        "rel_path": rel_target,
                        "existed_before": True,
                        "entry_type": "directory",
                        "mode": dir_mode,
                        "sha256": None,
                        "backup_rel_path": backup_rel,
                    })

                    # Record individual regular files inside directory
                    for root_w, dirs_w, files_w in os.walk(target_p, followlinks=False):
                        r_p = Path(root_w)
                        for d_w in sorted(dirs_w):
                            src_d = r_p / d_w
                            st_d = src_d.lstat()
                            if stat.S_ISLNK(st_d.st_mode) or not stat.S_ISDIR(st_d.st_mode):
                                raise ValidationError(f"Unsafe directory entry detected: {src_d.name}")
                            sub_rel_d = src_d.relative_to(root_dir)
                            b_rel_d = (backup_target / src_d.relative_to(target_p)).relative_to(bundle_dir)
                            manifest_entries.append({
                                "symbolic_id": f"{sym_id}:dir:{src_d.relative_to(target_p)}",
                                "root_key": root_key,
                                "rel_path": str(sub_rel_d),
                                "existed_before": True,
                                "entry_type": "directory",
                                "mode": stat.S_IMODE(st_d.st_mode),
                                "sha256": None,
                                "backup_rel_path": str(b_rel_d),
                            })
                        for f_w in sorted(files_w):
                            src_f = r_p / f_w
                            st_f = src_f.lstat()
                            if stat.S_ISLNK(st_f.st_mode):
                                raise ValidationError(f"Symlink escape detected: {src_f.name}")
                            if not stat.S_ISREG(st_f.st_mode):
                                raise ValidationError(f"Unsafe non-regular file detected: {src_f.name}")
                            if st_f.st_nlink != 1:
                                raise ValidationError(f"Unsafe hard-linked file detected: {src_f.name}")
                            sub_rel = src_f.relative_to(root_dir)
                            b_rel = (backup_target / src_f.relative_to(target_p)).relative_to(bundle_dir)
                            manifest_entries.append({
                                "symbolic_id": f"{sym_id}:{f_w}",
                                "root_key": root_key,
                                "rel_path": str(sub_rel),
                                "existed_before": True,
                                "entry_type": "file",
                                "mode": st_f.st_mode & 0o777,
                                "sha256": hash_file(src_f),
                                "backup_rel_path": str(b_rel),
                            })

            # Mark manifest as APPLYING once all backups are verified
            applying_manifest_doc = {
                "manifest_version": MANIFEST_VERSION,
                "state": STATE_APPLYING,
                "bundle_id": bundle_id,
                "source_schema_version": locked_plan.source_schema_version,
                "target_schema_version": locked_plan.target_schema_version,
                "entries": manifest_entries,
                "destinations_created": [],
                "preserved": list(locked_plan.preserve),
            }
            atomic_write_text(manifest_path, json.dumps(applying_manifest_doc, indent=2) + "\n")

        except Exception as backup_exc:  # noqa: BLE001 - phase boundary must fail closed
            # Clean up incomplete bundle directory fail-closed
            if bundle_dir and bundle_dir.exists():
                shutil.rmtree(bundle_dir, ignore_errors=True)
            if not migrations_dir_existed and migrations_backups_dir.is_dir():
                try:
                    migrations_backups_dir.rmdir()
                except OSError:
                    pass
            if not backups_dir_existed and backups_dir.is_dir():
                try:
                    backups_dir.rmdir()
                except OSError:
                    pass
            sanitized_err = sanitize_error_message(
                backup_exc, (resolved_home, resolved_codex_home, resolved_gemini_home)
            )
            return MigrationApplyResult(
                status=STATUS_BLOCKED,
                dry_run=False,
                source_schema_version=locked_plan.source_schema_version,
                target_schema_version=locked_plan.target_schema_version,
                mutations=[],
                backups=[],
                rollback_evidence={"status": "backup_failed"},
                blockers=[f"Backup preparation failed: {sanitized_err}"],
                warnings=locked_plan.warnings,
                preserved=locked_plan.preserve,
                bundle_id=None,
            )

        # 5. Phase B: Execute mutations with automatic rollback on error
        mutations_performed: list[str] = []
        destinations_created_records: list[str] = []

        if fault_after_step == "01_backup":
            if bundle_dir.exists():
                shutil.rmtree(bundle_dir)
            if not migrations_dir_existed and migrations_backups_dir.is_dir():
                migrations_backups_dir.rmdir()
            if not backups_dir_existed and backups_dir.is_dir():
                backups_dir.rmdir()
            return MigrationApplyResult(
                status=STATUS_BLOCKED,
                dry_run=False,
                source_schema_version=locked_plan.source_schema_version,
                target_schema_version=locked_plan.target_schema_version,
                mutations=[],
                backups=[],
                rollback_evidence={"status": "backup_failed"},
                blockers=["Fault injected after backup preparation before the first mutation"],
                warnings=locked_plan.warnings,
                preserved=locked_plan.preserve,
            )

        try:

            # Mutation 1: Canonical v2 directories
            canonical_v2_dirs = [
                resolved_home / "registry",
                resolved_home / "projects",
                resolved_home / "knowledge",
                resolved_home / "knowledge" / "personal",
                resolved_home / "rules",
                resolved_home / "rules" / "shared",
                resolved_home / "rules" / "codex",
                resolved_home / "rules" / "agy",
                resolved_home / "skills",
                resolved_home / "skills" / "shared",
                resolved_home / "skills" / "codex",
                resolved_home / "skills" / "agy",
                resolved_home / "services",
                resolved_home / "services" / "basic-memory",
                resolved_home / "state",
                resolved_home / "conflicts",
                resolved_home / "backups",
                resolved_home / "locks",
            ]
            for v2_d in canonical_v2_dirs:
                if not v2_d.exists():
                    v2_d.mkdir(mode=0o700, parents=True, exist_ok=True)
                    rel_d = str(v2_d.relative_to(resolved_home))
                    destinations_created_records.append(rel_d)
                    symbolic_id = {
                        "registry": "ptw:registry_dir",
                        "projects": "ptw:projects_dir",
                    }.get(rel_d, f"ptw:{rel_d}")
                    manifest_entries.append({
                        "symbolic_id": symbolic_id,
                        "root_key": "ptw_home",
                        "rel_path": rel_d,
                        "existed_before": False,
                        "entry_type": "directory",
                        "mode": 0o700,
                        "sha256": None,
                        "backup_rel_path": None,
                    })

            # Initialize canonical registry/projects.yaml if missing
            projects_yaml = resolved_home / "registry" / "projects.yaml"
            if not projects_yaml.exists():
                atomic_write_text(projects_yaml, DEFAULT_PROJECTS_REGISTRY_TEMPLATE)
                destinations_created_records.append("registry/projects.yaml")
                manifest_entries.append({
                    "symbolic_id": "ptw:projects_yaml",
                    "root_key": "ptw_home",
                    "rel_path": "registry/projects.yaml",
                    "existed_before": False,
                    "entry_type": "file",
                    "mode": 0o644,
                    "sha256": None,
                    "backup_rel_path": None,
                })

            # Initialize canonical state/state.json if missing
            state_file = resolved_home / "state" / "state.json"
            if not state_file.exists():
                init_state = {"version": SCHEMA_VERSION, "objects": {}}
                atomic_write_text(state_file, json.dumps(init_state, indent=2) + "\n")
                destinations_created_records.append("state/state.json")
                manifest_entries.append({
                    "symbolic_id": "ptw:state_file",
                    "root_key": "ptw_home",
                    "rel_path": "state/state.json",
                    "existed_before": False,
                    "entry_type": "file",
                    "mode": 0o644,
                    "sha256": None,
                    "backup_rel_path": None,
                })

            # Initialize canonical continuity rule if missing
            continuity_rule = resolved_home / "rules" / "shared" / CONTINUITY_RULE_FILENAME
            if not continuity_rule.exists():
                atomic_write_text(continuity_rule, DEFAULT_CONTINUITY_RULE_TEMPLATE)
                destinations_created_records.append(f"rules/shared/{CONTINUITY_RULE_FILENAME}")
                manifest_entries.append({
                    "symbolic_id": "ptw:continuity_rule",
                    "root_key": "ptw_home",
                    "rel_path": f"rules/shared/{CONTINUITY_RULE_FILENAME}",
                    "existed_before": False,
                    "entry_type": "file",
                    "mode": 0o644,
                    "sha256": None,
                    "backup_rel_path": None,
                })

            # Initialize canonical continuity skill if missing
            continuity_skill_dir = resolved_home / "skills" / "shared" / CONTINUITY_SKILL_NAME
            continuity_skill = continuity_skill_dir / "SKILL.md"
            if not continuity_skill.exists():
                if not continuity_skill_dir.exists():
                    continuity_skill_dir.mkdir(parents=True, exist_ok=True)
                    destinations_created_records.append(f"skills/shared/{CONTINUITY_SKILL_NAME}")
                    manifest_entries.append({
                        "symbolic_id": f"ptw:skill_dir_{CONTINUITY_SKILL_NAME}",
                        "root_key": "ptw_home",
                        "rel_path": f"skills/shared/{CONTINUITY_SKILL_NAME}",
                        "existed_before": False,
                        "entry_type": "directory",
                        "mode": 0o700,
                        "sha256": None,
                        "backup_rel_path": None,
                    })
                atomic_write_text(continuity_skill, DEFAULT_CONTINUITY_SKILL_TEMPLATE)
                destinations_created_records.append(f"skills/shared/{CONTINUITY_SKILL_NAME}/SKILL.md")
                manifest_entries.append({
                    "symbolic_id": f"ptw:skill_file_{CONTINUITY_SKILL_NAME}",
                    "root_key": "ptw_home",
                    "rel_path": f"skills/shared/{CONTINUITY_SKILL_NAME}/SKILL.md",
                    "existed_before": False,
                    "entry_type": "file",
                    "mode": 0o644,
                    "sha256": None,
                    "backup_rel_path": None,
                })

            mutations_performed.append("02_initialize_canonical_v2_directories")
            if fault_after_step == "02_init_dirs":
                raise RuntimeError("Fault injected after step '02_init_dirs'")

            # Mutation 2: Update config.yaml schema v1 -> v2
            config_text = config_yaml.read_text(encoding="utf-8")
            config_data = yaml.load(config_text, Loader=UniqueKeyLoader)
            config_data["version"] = SCHEMA_VERSION
            new_config_text = yaml.safe_dump(config_data, sort_keys=False)
            atomic_write_text(config_yaml, new_config_text)
            mutations_performed.append("03_migrate_config_schema_to_v2")
            if fault_after_step == "03_update_config":
                raise RuntimeError("Fault injected after step '03_update_config'")

            # Mutation 3: Relocate AGY rules
            if has_legacy_rules and legacy_gemini_md.exists():
                raw_gemini_text = legacy_gemini_md.read_text(encoding="utf-8")
                start_idx = raw_gemini_text.find(RULE_MARKER_START)
                end_idx = raw_gemini_text.find(RULE_MARKER_END)

                prefix = raw_gemini_text[:start_idx]
                suffix = raw_gemini_text[end_idx + len(RULE_MARKER_END) :]

                managed_block_str = f"{RULE_MARKER_START}\n{managed_rules_body}\n{RULE_MARKER_END}\n"
                dest_rules = agy_customization_root / "GEMINI.md"
                dest_rules_existed = dest_rules.exists()
                dest_rules.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(dest_rules, managed_block_str)

                if not dest_rules_existed:
                    rel_dest = str(dest_rules.relative_to(resolved_gemini_home))
                    destinations_created_records.append(rel_dest)
                    manifest_entries.append({
                        "symbolic_id": "agy:customization_rules",
                        "root_key": "gemini_home",
                        "rel_path": rel_dest,
                        "existed_before": False,
                        "entry_type": "file",
                        "mode": 0o644,
                        "sha256": None,
                        "backup_rel_path": None,
                    })

                # Check if legacy GEMINI.md has unmanaged text outside markers
                unmanaged_text = prefix + suffix
                if unmanaged_text.strip():
                    atomic_write_text(legacy_gemini_md, unmanaged_text)
                else:
                    legacy_gemini_md.unlink()

                mutations_performed.append("04_relocate_agy_rules_to_customization_root")
                if fault_after_step == "04_relocate_rules":
                    raise RuntimeError("Fault injected after step '04_relocate_rules'")

            # Mutation 4: Relocate AGY skills
            if has_legacy_skills and legacy_skills.exists():
                if not agy_current_skills.exists():
                    agy_current_skills.mkdir(parents=True, exist_ok=True)
                    rel_sk_root = str(agy_current_skills.relative_to(resolved_gemini_home))
                    destinations_created_records.append(rel_sk_root)
                    manifest_entries.append({
                        "symbolic_id": "agy:current_skills_root",
                        "root_key": "gemini_home",
                        "rel_path": rel_sk_root,
                        "existed_before": False,
                        "entry_type": "directory",
                        "mode": 0o700,
                        "sha256": None,
                        "backup_rel_path": None,
                    })

                for skill_name, is_managed in skills_ownership.items():
                    skill_src = legacy_skills / skill_name
                    if is_managed:
                        skill_dst = agy_current_skills / skill_name
                        skill_dst_existed = skill_dst.exists()
                        _copy_tree_without_symlinks(skill_src, skill_dst)
                        shutil.rmtree(skill_src)

                        if not skill_dst_existed:
                            rel_dst_sk = str(skill_dst.relative_to(resolved_gemini_home))
                            destinations_created_records.append(rel_dst_sk)
                            manifest_entries.append({
                                "symbolic_id": f"agy:skill_{skill_name}",
                                "root_key": "gemini_home",
                                "rel_path": rel_dst_sk,
                                "existed_before": False,
                                "entry_type": "directory",
                                "mode": 0o700,
                                "sha256": None,
                                "backup_rel_path": None,
                            })

                # Remove legacy skills dir only if empty of unmanaged skills
                try:
                    remaining = list(legacy_skills.iterdir())
                    if not remaining:
                        legacy_skills.rmdir()
                except OSError:
                    pass

                mutations_performed.append("05_relocate_agy_skills_to_customization_root")
                if fault_after_step == "05_relocate_skills":
                    raise RuntimeError("Fault injected after step '05_relocate_skills'")

            # Mutation 5: Archive legacy memory losslessly
            if legacy_memory.exists():
                archive_dir = resolved_home / "archive" / "legacy_memory"
                archive_parent_existed = archive_dir.parent.exists()
                archive_dir.parent.mkdir(parents=True, exist_ok=True)
                if not archive_parent_existed:
                    destinations_created_records.append("archive")
                    manifest_entries.append({
                        "symbolic_id": "ptw:archive_dir",
                        "root_key": "ptw_home",
                        "rel_path": "archive",
                        "existed_before": False,
                        "entry_type": "directory",
                        "mode": 0o700,
                        "sha256": None,
                        "backup_rel_path": None,
                    })

                _copy_tree_without_symlinks(legacy_memory, archive_dir)
                destinations_created_records.append("archive/legacy_memory")
                manifest_entries.append({
                    "symbolic_id": "ptw:memory_archive",
                    "root_key": "ptw_home",
                    "rel_path": "archive/legacy_memory",
                    "existed_before": False,
                    "entry_type": "directory",
                    "mode": 0o700,
                    "sha256": None,
                    "backup_rel_path": None,
                })

                # Verify copy
                for src_f, dst_f in [(s, archive_dir / s.relative_to(legacy_memory)) for s in legacy_memory.rglob("*") if s.is_file()]:
                    if hash_file(dst_f) != hash_file(src_f):
                        raise MigrationError("Memory archive integrity verification failed")

                shutil.rmtree(legacy_memory)
                mutations_performed.append("06_archive_legacy_memory_to_v2_storage")
                if fault_after_step == "06_archive_memory":
                    raise RuntimeError("Fault injected after step '06_archive_memory'")

            # Mark completed in manifest
            completed_manifest_doc = {
                "manifest_version": MANIFEST_VERSION,
                "state": STATE_COMPLETED,
                "bundle_id": bundle_id,
                "source_schema_version": locked_plan.source_schema_version,
                "target_schema_version": locked_plan.target_schema_version,
                "entries": manifest_entries,
                "destinations_created": destinations_created_records,
                "preserved": list(locked_plan.preserve),
            }
            atomic_write_text(manifest_path, json.dumps(completed_manifest_doc, indent=2) + "\n")

            evidence_summary = {
                "manifest_path": str(manifest_path.relative_to(resolved_home)),
                "state": STATE_COMPLETED,
                "backup_entries_count": len(manifest_entries),
                "rollback_supported": True,
            }

            return MigrationApplyResult(
                status="applied",
                dry_run=False,
                source_schema_version=locked_plan.source_schema_version,
                target_schema_version=locked_plan.target_schema_version,
                mutations=sorted(mutations_performed),
                backups=sorted(locked_plan.backups),
                rollback_evidence=evidence_summary,
                blockers=[],
                warnings=locked_plan.warnings,
                preserved=locked_plan.preserve,
                bundle_id=bundle_id,
            )

        except Exception as exc:  # noqa: BLE001 - any mutation failure requires rollback
            # AUTOMATIC ROLLBACK TRIGGERED!
            sanitized_exc_msg = sanitize_error_message(
                exc, (resolved_home, resolved_codex_home, resolved_gemini_home)
            )
            try:
                # Update manifest with collected destinations so rollback knows what to delete
                applying_manifest_doc["entries"] = manifest_entries
                applying_manifest_doc["destinations_created"] = destinations_created_records
                atomic_write_text(manifest_path, json.dumps(applying_manifest_doc, indent=2) + "\n")

                rb_result = rollback_migration(
                    manifest_path,
                    home=resolved_home,
                    codex_home=resolved_codex_home,
                    gemini_home=resolved_gemini_home,
                    lock_fd=lock_fd,
                )
                return MigrationApplyResult(
                    status="rolled_back",
                    dry_run=False,
                    source_schema_version=locked_plan.source_schema_version,
                    target_schema_version=locked_plan.target_schema_version,
                    mutations=sorted(mutations_performed),
                    backups=sorted(locked_plan.backups),
                    rollback_evidence=rb_result.to_dict(),
                    blockers=[],
                    warnings=locked_plan.warnings,
                    preserved=locked_plan.preserve,
                    bundle_id=bundle_id,
                    error=f"Apply failed: {sanitized_exc_msg}. Automatic rollback succeeded.",
                )
            except Exception as rb_exc:  # noqa: BLE001 - report every partial rollback failure
                sanitized_rb_err = sanitize_error_message(
                    rb_exc, (resolved_home, resolved_codex_home, resolved_gemini_home)
                )
                return MigrationApplyResult(
                    status="rollback_failed",
                    dry_run=False,
                    source_schema_version=locked_plan.source_schema_version,
                    target_schema_version=locked_plan.target_schema_version,
                    mutations=sorted(mutations_performed),
                    backups=sorted(locked_plan.backups),
                    rollback_evidence={"rollback_error": sanitized_rb_err},
                    blockers=[],
                    warnings=locked_plan.warnings,
                    preserved=locked_plan.preserve,
                    bundle_id=bundle_id,
                    error=f"Apply failed: {sanitized_exc_msg}. Rollback also failed: {sanitized_rb_err}",
                )

    finally:
        release_migration_lock(lock_fd)


def rollback_migration(
    manifest_path: str | Path,
    home: str | Path | None = None,
    codex_home: str | Path | None = None,
    gemini_home: str | Path | None = None,
    *,
    dry_run: bool = False,
    lock_fd: int | None = None,
) -> MigrationRollbackResult:
    """Safely restore a completed or failed migration transaction using its manifest."""
    env_home = os.environ.get("PERSONAL_TIDEWAY_HOME")
    resolved_home = Path(home or env_home or (Path.home() / DEFAULT_PERSONAL_TIDEWAY_DIR)).expanduser().resolve()
    resolved_codex_home = (
        Path(codex_home or os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser().resolve()
    )
    resolved_gemini_home = (
        Path(
            gemini_home
            or os.environ.get("GEMINI_HOME")
            or os.environ.get("AGY_HOME")
            or (Path.home() / ".gemini")
        )
        .expanduser()
        .resolve()
    )

    manifest_p = Path(manifest_path).expanduser()
    if not manifest_p.is_absolute():
        manifest_p = resolved_home / "backups" / "migrations" / manifest_p

    # Validate manifest boundary
    allowed_backup_root = resolved_home / "backups" / "migrations"
    ensure_safe_path(manifest_p, allowed_backup_root, "Migration manifest", follow_symlinks=False)

    if _has_symlink_component(manifest_p):
        raise MigrationRollbackError("Symlink escape detected: manifest is a symbolic link")

    if not manifest_p.is_file():
        raise MigrationRollbackError("Migration manifest file does not exist or is not a regular file")

    st = manifest_p.stat()
    if st.st_size > MAX_MANIFEST_BYTES:
        raise MigrationRollbackError("Manifest file exceeds maximum permitted size")

    try:
        manifest_data = json.loads(manifest_p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise MigrationRollbackError(f"Malformed manifest JSON: {exc}") from exc

    if not isinstance(manifest_data, dict):
        raise MigrationRollbackError("Malformed manifest: root must be a JSON object")

    manifest_version = manifest_data.get("manifest_version")
    if manifest_version != MANIFEST_VERSION:
        raise MigrationRollbackError(f"Unsupported manifest version: {manifest_version}")

    state = manifest_data.get("state")
    if state not in VALID_MANIFEST_STATES:
        raise MigrationRollbackError(f"Invalid manifest state: {state}")

    if state == STATE_PREPARING:
        raise MigrationRollbackError("Incomplete transaction manifest (state='preparing') cannot be rolled back")

    bundle_dir = manifest_p.parent
    entries = manifest_data.get("entries", [])
    if not isinstance(entries, list):
        raise MigrationRollbackError("Malformed manifest: entries must be a list")

    # Idempotent skip if already rolled back
    if state == STATE_ROLLED_BACK:
        return MigrationRollbackResult(
            status="rolled_back",
            manifest_path=str(manifest_p.relative_to(resolved_home)),
            restored_entries=[e.get("symbolic_id", "") for e in entries if e.get("existed_before")],
            cleaned_destinations=manifest_data.get("destinations_created", []),
            preserved_memory=[],
        )

    # Validate all entries before mutating anything
    for entry in entries:
        if not isinstance(entry, dict):
            raise MigrationRollbackError("Malformed entry in manifest")

        root_key = entry.get("root_key")
        if root_key not in ("ptw_home", "codex_home", "gemini_home"):
            raise MigrationRollbackError(f"Invalid root_key '{root_key}' in manifest entry")

        rel_path = entry.get("rel_path")
        if (
            not rel_path
            or rel_path.strip() in (".", "/", "\\")
            or _is_path_traversal(rel_path)
            or Path(rel_path).is_absolute()
        ):
            raise MigrationRollbackError(f"Unsafe path in manifest entry: '{rel_path}'")

        root_dir = resolved_home if root_key == "ptw_home" else (
            resolved_codex_home if root_key == "codex_home" else resolved_gemini_home
        )
        target_check = root_dir / rel_path
        if target_check.resolve() == root_dir.resolve():
            raise MigrationRollbackError(f"Manifest entry '{rel_path}' resolves to root directory '{root_dir}'")
        if _has_symlink_component(target_check):
            raise MigrationRollbackError("Symlink escape detected in manifest target path")
        if not is_safe_path(target_check, root_dir, follow_symlinks=True):
            raise MigrationRollbackError("Manifest target escapes its allowed root")

        if entry.get("existed_before"):
            b_rel = entry.get("backup_rel_path")
            if not b_rel or _is_path_traversal(b_rel) or Path(b_rel).is_absolute():
                raise MigrationRollbackError(f"Unsafe backup_rel_path in manifest entry: '{b_rel}'")

            b_file = bundle_dir / b_rel
            if _has_symlink_component(b_file):
                raise MigrationRollbackError("Symlink escape detected: backup entry is a symbolic link")
            if not (b_file.is_file() or b_file.is_dir()):
                raise MigrationRollbackError(f"Missing backup target: {b_rel}")

            if entry.get("entry_type") == "file":
                expected_hash = entry.get("sha256")
                actual_hash = hash_file(b_file)
                if expected_hash and actual_hash != expected_hash:
                    raise TamperedBackupError(
                        f"Tampered backup detected for entry '{entry.get('symbolic_id')}': "
                        f"expected hash {expected_hash} does not match {actual_hash}"
                    )

    # Acquire lock if not already provided by caller
    owns_lock = False
    active_fd = lock_fd
    if active_fd is None:
        active_fd = acquire_migration_lock(resolved_home)
        owns_lock = True

    try:
        restored_entries: list[str] = []
        cleaned_destinations: list[str] = []
        preserved_memory: list[str] = []

        # 1. Clean up transaction-created destinations
        for entry in entries:
            if not entry.get("existed_before"):
                root_key = entry["root_key"]
                rel_path = entry["rel_path"]
                root_dir = resolved_home if root_key == "ptw_home" else (
                    resolved_codex_home if root_key == "codex_home" else resolved_gemini_home
                )
                target_p = root_dir / rel_path

                if target_p.resolve() == root_dir.resolve():
                    raise MigrationRollbackError(f"Refusing to remove root directory '{target_p}'")

                # Special preservation rule for central memory
                if root_key == "ptw_home" and (rel_path == "memory" or rel_path.startswith("memory/")):
                    if target_p.exists():
                        preserved_memory.append(rel_path)
                    continue

                if target_p.exists() or target_p.is_symlink():
                    safe_remove_link_or_dir(target_p, root_dir)
                    cleaned_destinations.append(rel_path)

        # Also clean up empty parent archive dir if legacy memory archive was cleaned
        archive_parent = resolved_home / "archive"
        if archive_parent.is_dir():
            try:
                if not any(archive_parent.iterdir()):
                    archive_parent.rmdir()
            except OSError:
                pass

        # 2. Restore backed up entries
        for entry in entries:
            if entry.get("existed_before"):
                root_key = entry["root_key"]
                rel_path = entry["rel_path"]
                b_rel = entry["backup_rel_path"]
                mode = entry.get("mode") or 0o644

                root_dir = resolved_home if root_key == "ptw_home" else (
                    resolved_codex_home if root_key == "codex_home" else resolved_gemini_home
                )
                target_p = root_dir / rel_path
                b_file = bundle_dir / b_rel

                # Check if target is inside central memory and collides with post-migration notes
                if (
                    root_key == "ptw_home"
                    and (rel_path == "memory" or rel_path.startswith("memory/"))
                    and target_p.is_file()
                ):
                    current_hash = hash_file(target_p)
                    if current_hash != entry.get("sha256"):
                        # Post-migration file has different content; preserve it safely!
                        post_mig_path = target_p.parent / f"{target_p.name}.post_migration"
                        counter = 1
                        while post_mig_path.exists():
                            post_mig_path = target_p.parent / f"{target_p.name}.post_migration.{counter}"
                            counter += 1
                        atomic_write_bytes(post_mig_path, target_p.read_bytes(), mode=target_p.stat().st_mode & 0o777)
                        preserved_memory.append(str(post_mig_path.relative_to(resolved_home)))

                if entry.get("entry_type") == "file":
                    target_p.parent.mkdir(parents=True, exist_ok=True)
                    b_bytes = b_file.read_bytes()
                    atomic_write_bytes(target_p, b_bytes, mode=mode)
                    try:
                        os.chmod(target_p, mode)
                    except OSError:
                        pass
                    restored_entries.append(entry.get("symbolic_id", rel_path))
                elif entry.get("entry_type") == "directory":
                    target_p.mkdir(parents=True, exist_ok=True)
                    try:
                        os.chmod(target_p, mode)
                    except OSError:
                        pass

        # Check for any other new files in central memory that were not in original backup
        central_memory_dir = resolved_home / "memory"
        if central_memory_dir.is_dir():
            backed_up_rel_paths = {
                e["rel_path"] for e in entries if e.get("root_key") == "ptw_home" and e.get("existed_before")
            }
            for root_m, _, files_m in os.walk(central_memory_dir, followlinks=False):
                r_p = Path(root_m)
                for f_m in files_m:
                    m_file = r_p / f_m
                    rel_to_ptw = str(m_file.relative_to(resolved_home))
                    if rel_to_ptw not in backed_up_rel_paths and rel_to_ptw not in preserved_memory:
                        preserved_memory.append(rel_to_ptw)

        # Mark manifest as rolled_back
        manifest_data["state"] = STATE_ROLLED_BACK
        atomic_write_text(manifest_p, json.dumps(manifest_data, indent=2) + "\n")

        return MigrationRollbackResult(
            status="rolled_back",
            manifest_path=str(manifest_p.relative_to(resolved_home)),
            restored_entries=sorted(restored_entries),
            cleaned_destinations=sorted(cleaned_destinations),
            preserved_memory=sorted(preserved_memory),
        )

    except Exception as exc:
        manifest_data["state"] = STATE_ROLLBACK_FAILED
        try:
            atomic_write_text(manifest_p, json.dumps(manifest_data, indent=2) + "\n")
        except OSError:
            pass
        sanitized_err = sanitize_error_message(
            exc, (resolved_home, resolved_codex_home, resolved_gemini_home)
        )
        raise MigrationRollbackError(f"Rollback execution failed: {sanitized_err}") from exc

    finally:
        if owns_lock and active_fd is not None:
            release_migration_lock(active_fd)
