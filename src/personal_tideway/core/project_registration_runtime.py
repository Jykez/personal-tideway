"""Transactional completion of project registration in Basic Memory runtime state."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from personal_tideway.config import PersonalTidewayConfig, validate_owned_path
from personal_tideway.core.basic_memory_backend import (
    remove_basic_memory_runtime_project,
    sync_and_initialize_basic_memory_backend,
)
from personal_tideway.core.basic_memory_installer import BasicMemoryRunner
from personal_tideway.core.basic_memory_runtime import get_basic_memory_layout
from personal_tideway.core.registry import ProjectRecord, ProjectRegistry
from personal_tideway.exceptions import ConfigError, RuntimeProbeError
from personal_tideway.utils import atomic_write_bytes

MAX_REGISTRATION_SNAPSHOT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class _FileSnapshot:
    path: Path
    existed: bool
    content: bytes = b""
    mode: int = 0o600


@dataclass(frozen=True)
class ProjectRegistrationSnapshot:
    """Exact pre-registration state needed for automatic rollback."""

    registry_file: _FileSnapshot
    basic_memory_config: _FileSnapshot
    registry_projects: tuple[ProjectRecord, ...]
    existing_memory_roots: frozenset[Path]


def _capture_owned_file(path: Path, *, root: Path) -> _FileSnapshot:
    validate_owned_path(path, root=root, allow_root=False)
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return _FileSnapshot(path=path, existed=False)
    except OSError:
        raise ConfigError("Project registration state cannot be inspected safely.") from None

    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise ConfigError("Project registration state is not a safe regular file.")
        if file_stat.st_size > MAX_REGISTRATION_SNAPSHOT_BYTES:
            raise ConfigError("Project registration state exceeds the snapshot size limit.")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_REGISTRATION_SNAPSHOT_BYTES:
            chunk = os.read(fd, (MAX_REGISTRATION_SNAPSHOT_BYTES + 1) - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_REGISTRATION_SNAPSHOT_BYTES:
            raise ConfigError("Project registration state exceeds the snapshot size limit.")
        return _FileSnapshot(
            path=path,
            existed=True,
            content=payload,
            mode=stat.S_IMODE(file_stat.st_mode),
        )
    except ConfigError:
        raise
    except OSError:
        raise ConfigError("Project registration state cannot be inspected safely.") from None
    finally:
        os.close(fd)


def capture_project_registration_snapshot(
    cfg: PersonalTidewayConfig,
    registry: ProjectRegistry,
) -> ProjectRegistrationSnapshot:
    """Capture exact source-of-truth bytes before a registration mutation."""
    registry.validate()
    layout = get_basic_memory_layout(cfg)
    roots = frozenset(
        cfg.home / project.memory.path
        for project in registry.projects
        if (cfg.home / project.memory.path).is_dir()
    )
    return ProjectRegistrationSnapshot(
        registry_file=_capture_owned_file(cfg.projects_yaml, root=cfg.home),
        basic_memory_config=_capture_owned_file(layout.config_file, root=cfg.home),
        registry_projects=tuple(registry.projects),
        existing_memory_roots=roots,
    )


def _restore_file_snapshot(snapshot: _FileSnapshot, *, root: Path) -> None:
    validate_owned_path(snapshot.path, root=root, allow_root=False)
    if snapshot.existed:
        atomic_write_bytes(snapshot.path, snapshot.content, mode=snapshot.mode)
        return
    parent_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        parent_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        parent_flags |= os.O_NOFOLLOW
    try:
        parent_fd = os.open(snapshot.path.parent, parent_flags)
    except OSError:
        raise ConfigError("Project registration rollback could not inspect a created file.") from None
    file_fd: int | None = None
    try:
        file_flags = os.O_RDONLY | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        try:
            file_fd = os.open(snapshot.path.name, file_flags, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        current_stat = os.fstat(file_fd)
        path_stat = os.stat(snapshot.path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(current_stat.st_mode)
            or current_stat.st_nlink != 1
            or (current_stat.st_dev, current_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise ConfigError("Project registration rollback refused an unsafe created file.")
        os.unlink(snapshot.path.name, dir_fd=parent_fd)
    except ConfigError:
        raise
    except OSError:
        raise ConfigError("Project registration rollback could not remove a created file.") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent_fd)


def _remove_new_empty_memory_root(cfg: PersonalTidewayConfig, project: ProjectRecord) -> None:
    memory_root = cfg.home / project.memory.path
    validate_owned_path(memory_root, root=cfg.home, allow_root=False)
    project_root = memory_root.parent
    for candidate in (memory_root, project_root):
        try:
            candidate.rmdir()
        except FileNotFoundError:
            continue
        except OSError:
            # Never delete non-empty memory or an unexpected object during rollback.
            break


def complete_project_registration(
    cfg: PersonalTidewayConfig,
    *,
    snapshot: ProjectRegistrationSnapshot,
    registry: ProjectRegistry,
    project: ProjectRecord,
    created: bool,
    mutated: bool,
    runner: BasicMemoryRunner | None = None,
) -> None:
    """Initialize runtime state and roll back registry mutations on failure."""
    try:
        sync_and_initialize_basic_memory_backend(cfg, registry=registry, runner=runner)
        return
    except Exception:
        if not mutated:
            raise

    rollback_failed = False
    runtime_cleanup_safe = not created
    if created:
        try:
            remove_basic_memory_runtime_project(cfg, project.memory.project_name, runner=runner)
            runtime_cleanup_safe = True
        except Exception:  # noqa: BLE001 - rollback must continue after runtime cleanup failure
            rollback_failed = True

    try:
        _restore_file_snapshot(snapshot.registry_file, root=cfg.home)
        _restore_file_snapshot(snapshot.basic_memory_config, root=cfg.home)
        registry.projects[:] = list(snapshot.registry_projects)
        if (
            created
            and runtime_cleanup_safe
            and (cfg.home / project.memory.path) not in snapshot.existing_memory_roots
        ):
            _remove_new_empty_memory_root(cfg, project)
    except Exception:  # noqa: BLE001 - report incomplete rollback after all restoration attempts
        rollback_failed = True

    if not created and not rollback_failed:
        try:
            restored_registry = ProjectRegistry(
                version=registry.version,
                projects=list(snapshot.registry_projects),
            )
            sync_and_initialize_basic_memory_backend(
                cfg,
                registry=restored_registry,
                runner=runner,
            )
        except Exception:  # noqa: BLE001 - failed runtime restoration makes rollback incomplete
            rollback_failed = True

    if rollback_failed:
        raise RuntimeProbeError(
            "Project registration failed and automatic rollback was incomplete."
        ) from None
    raise RuntimeProbeError("Project registration failed and was rolled back.") from None


__all__ = [
    "MAX_REGISTRATION_SNAPSHOT_BYTES",
    "ProjectRegistrationSnapshot",
    "capture_project_registration_snapshot",
    "complete_project_registration",
]
