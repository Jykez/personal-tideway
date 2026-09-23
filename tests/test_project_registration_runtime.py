"""Transactional project-registration runtime completion tests."""

from pathlib import Path

import pytest

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import PROJECT_KIND_DIRECTORY, PROJECT_KIND_EXTERNAL
from personal_tideway.core.basic_memory_reconciliation import (
    reconcile_basic_memory_projects,
)
from personal_tideway.core.project_registration_runtime import (
    capture_project_registration_snapshot,
    complete_project_registration,
)
from personal_tideway.core.registry import ProjectRecord, ProjectRegistry, save_registry
from personal_tideway.core.workspace import init_workspace
from personal_tideway.exceptions import RuntimeProbeError


def _record() -> ProjectRecord:
    return ProjectRecord.create(
        project_id="12345678-1234-4234-8234-123456789abc",
        slug="runtime-probe",
        display_name="Runtime Probe",
        kind=PROJECT_KIND_EXTERNAL,
    )


def _mutate_registration_state(
    cfg: PersonalTidewayConfig,
    registry: ProjectRegistry,
    project: ProjectRecord,
) -> None:
    registry.add_project(project)
    save_registry(registry, cfg)
    reconcile_basic_memory_projects(cfg, registry=registry)


def test_complete_project_registration_success_calls_runtime_initializer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = PersonalTidewayConfig.resolve(home=tmp_path / "ptw")
    init_workspace(cfg)
    registry = ProjectRegistry.empty()
    snapshot = capture_project_registration_snapshot(cfg, registry)
    project = _record()
    _mutate_registration_state(cfg, registry, project)
    observed: dict[str, object] = {}

    def initialize(*args: object, **kwargs: object) -> object:
        observed["registry"] = kwargs["registry"]
        observed["runner"] = kwargs["runner"]
        return object()

    runner = object()
    monkeypatch.setattr(
        "personal_tideway.core.project_registration_runtime.sync_and_initialize_basic_memory_backend",
        initialize,
    )

    complete_project_registration(
        cfg,
        snapshot=snapshot,
        registry=registry,
        project=project,
        created=True,
        mutated=True,
        runner=runner,  # type: ignore[arg-type]
    )

    assert observed == {"registry": registry, "runner": runner}


def test_failed_new_registration_restores_exact_files_and_removes_empty_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = PersonalTidewayConfig.resolve(home=tmp_path / "ptw")
    init_workspace(cfg)
    registry = ProjectRegistry.empty()
    snapshot = capture_project_registration_snapshot(cfg, registry)
    registry_before = cfg.projects_yaml.read_bytes()
    config_existed_before = (cfg.basic_memory_dir / "config" / "config.json").exists()
    project = _record()
    _mutate_registration_state(cfg, registry, project)
    memory_root = cfg.home / project.memory.path
    assert memory_root.is_dir()

    def fail_initialize(*args: object, **kwargs: object) -> object:
        raise RuntimeProbeError("simulated backend failure")

    removed: list[str] = []

    def remove_runtime(
        _cfg: PersonalTidewayConfig,
        project_name: str,
        **kwargs: object,
    ) -> bool:
        removed.append(project_name)
        return True

    monkeypatch.setattr(
        "personal_tideway.core.project_registration_runtime.sync_and_initialize_basic_memory_backend",
        fail_initialize,
    )
    monkeypatch.setattr(
        "personal_tideway.core.project_registration_runtime.remove_basic_memory_runtime_project",
        remove_runtime,
    )

    with pytest.raises(RuntimeProbeError, match="failed and was rolled back") as exc_info:
        complete_project_registration(
            cfg,
            snapshot=snapshot,
            registry=registry,
            project=project,
            created=True,
            mutated=True,
        )

    assert exc_info.value.__cause__ is None
    assert removed == [project.memory.project_name]
    assert cfg.projects_yaml.read_bytes() == registry_before
    assert (cfg.basic_memory_dir / "config" / "config.json").exists() is config_existed_before
    assert registry.projects == []
    assert not memory_root.exists()
    assert not memory_root.parent.exists()


def test_runtime_cleanup_failure_still_restores_source_of_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = PersonalTidewayConfig.resolve(home=tmp_path / "ptw")
    init_workspace(cfg)
    registry = ProjectRegistry.empty()
    snapshot = capture_project_registration_snapshot(cfg, registry)
    registry_before = cfg.projects_yaml.read_bytes()
    project = _record()
    _mutate_registration_state(cfg, registry, project)

    monkeypatch.setattr(
        "personal_tideway.core.project_registration_runtime.sync_and_initialize_basic_memory_backend",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeProbeError("backend failed")),
    )
    monkeypatch.setattr(
        "personal_tideway.core.project_registration_runtime.remove_basic_memory_runtime_project",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeProbeError("cleanup failed")),
    )

    with pytest.raises(RuntimeProbeError, match="automatic rollback was incomplete") as exc_info:
        complete_project_registration(
            cfg,
            snapshot=snapshot,
            registry=registry,
            project=project,
            created=True,
            mutated=True,
        )

    assert exc_info.value.__cause__ is None
    assert cfg.projects_yaml.read_bytes() == registry_before
    assert registry.projects == []
    assert (cfg.home / project.memory.path).is_dir()


def test_nonempty_new_memory_root_is_preserved_after_successful_runtime_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = PersonalTidewayConfig.resolve(home=tmp_path / "ptw")
    init_workspace(cfg)
    registry = ProjectRegistry.empty()
    snapshot = capture_project_registration_snapshot(cfg, registry)
    project = _record()
    _mutate_registration_state(cfg, registry, project)
    memory_root = cfg.home / project.memory.path
    note = memory_root / "preserve.md"
    note.write_text("# Preserve\n", encoding="utf-8")
    monkeypatch.setattr(
        "personal_tideway.core.project_registration_runtime.sync_and_initialize_basic_memory_backend",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeProbeError("backend failed")),
    )
    monkeypatch.setattr(
        "personal_tideway.core.project_registration_runtime.remove_basic_memory_runtime_project",
        lambda *args, **kwargs: True,
    )

    with pytest.raises(RuntimeProbeError, match="failed and was rolled back"):
        complete_project_registration(
            cfg,
            snapshot=snapshot,
            registry=registry,
            project=project,
            created=True,
            mutated=True,
        )

    assert note.read_text(encoding="utf-8") == "# Preserve\n"
    assert memory_root.is_dir()


def test_existing_registration_failure_is_not_destructively_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = PersonalTidewayConfig.resolve(home=tmp_path / "ptw")
    init_workspace(cfg)
    project = _record()
    registry = ProjectRegistry(projects=[project])
    save_registry(registry, cfg)
    reconcile_basic_memory_projects(cfg, registry=registry)
    snapshot = capture_project_registration_snapshot(cfg, registry)
    monkeypatch.setattr(
        "personal_tideway.core.project_registration_runtime.sync_and_initialize_basic_memory_backend",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeProbeError("backend failed")),
    )

    with pytest.raises(RuntimeProbeError, match="backend failed"):
        complete_project_registration(
            cfg,
            snapshot=snapshot,
            registry=registry,
            project=project,
            created=False,
            mutated=False,
        )

    assert registry.projects == [project]


def test_failed_binding_update_restores_and_reinitializes_previous_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = PersonalTidewayConfig.resolve(home=tmp_path / "ptw")
    init_workspace(cfg)
    original_root = tmp_path / "original"
    updated_root = tmp_path / "updated"
    original_root.mkdir()
    updated_root.mkdir()
    original = ProjectRecord.create(
        project_id="12345678-1234-4234-8234-123456789abc",
        slug="binding-probe",
        display_name="Binding Probe",
        kind=PROJECT_KIND_DIRECTORY,
        paths=[str(original_root)],
    )
    updated = original.with_bindings(paths=[str(updated_root)])
    registry = ProjectRegistry(projects=[original])
    save_registry(registry, cfg)
    reconcile_basic_memory_projects(cfg, registry=registry)
    snapshot = capture_project_registration_snapshot(cfg, registry)
    registry_before = cfg.projects_yaml.read_bytes()
    config_before = (cfg.basic_memory_dir / "config" / "config.json").read_bytes()
    registry.projects[:] = [updated]
    save_registry(registry, cfg)
    reconcile_basic_memory_projects(cfg, registry=registry)
    calls: list[ProjectRegistry] = []

    def initialize(
        _cfg: PersonalTidewayConfig,
        *,
        registry: ProjectRegistry,
        **kwargs: object,
    ) -> object:
        calls.append(registry)
        if len(calls) == 1:
            raise RuntimeProbeError("simulated updated binding failure")
        return object()

    monkeypatch.setattr(
        "personal_tideway.core.project_registration_runtime.sync_and_initialize_basic_memory_backend",
        initialize,
    )
    monkeypatch.setattr(
        "personal_tideway.core.project_registration_runtime.remove_basic_memory_runtime_project",
        lambda *args, **kwargs: pytest.fail("existing runtime project must not be removed"),
    )

    with pytest.raises(RuntimeProbeError, match="failed and was rolled back"):
        complete_project_registration(
            cfg,
            snapshot=snapshot,
            registry=registry,
            project=updated,
            created=False,
            mutated=True,
        )

    assert len(calls) == 2
    assert calls[1].projects == [original]
    assert registry.projects == [original]
    assert cfg.projects_yaml.read_bytes() == registry_before
    assert (cfg.basic_memory_dir / "config" / "config.json").read_bytes() == config_before
