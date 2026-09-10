"""Comprehensive tests for explicit directory registration."""

import os
from pathlib import Path
import subprocess
import pytest

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import (
    DEFAULT_MEMORY_BACKEND,
    PROJECT_KIND_DIRECTORY,
)
from personal_tideway.core.project_resolver import (
    DirectoryRegistrationResult,
    DirectoryRegistrationService,
    GitProbeResult,
    probe_git,
    register_directory,
)
from personal_tideway.core.registry import ProjectRegistry, load_registry
from personal_tideway.exceptions import ConfigError, ValidationError


def make_fake_non_git_runner():
    """Runner returning confirmed non-git status (standard exit code 128 with non-repo diagnostic)."""
    def fake_runner(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str, str]:
        return 128, "", "fatal: not a git repository (or any of the parent directories): .git\n"
    return fake_runner


def test_register_directory_successful_defaults(tmp_path: Path):
    """Register an ordinary directory with default metadata and verify created record."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    work_dir = tmp_path / "my-plain-project"
    work_dir.mkdir()

    runner = make_fake_non_git_runner()
    res = register_directory(work_dir, cfg=cfg, git_runner=runner)

    assert isinstance(res, DirectoryRegistrationResult)
    assert res.status == "resolved"
    assert res.created is True
    assert res.requires_confirmation is False
    assert res.evidence == ["directory"]
    assert res.action == "register_directory"

    record = res.record
    assert record is not None
    assert res.proposal is None
    assert res.project_id == record.id
    assert record.kind == PROJECT_KIND_DIRECTORY
    assert record.slug == "my-plain-project"
    assert record.display_name == "my-plain-project"
    assert record.aliases == []
    assert record.bindings.paths == [str(work_dir.resolve())]
    assert record.bindings.git_common_dirs == []
    assert record.bindings.git_remotes == []
    assert record.memory.backend == DEFAULT_MEMORY_BACKEND
    assert record.memory.project_name == f"ptw-my-plain-project-{record.id[:8]}"
    assert record.memory.path == f"projects/{record.id}/memory"
    assert record.created_at == record.updated_at
    assert record.created_at.endswith("Z")

    # Verify saved file
    assert cfg.projects_yaml.is_file()
    saved_reg = load_registry(cfg.projects_yaml)
    assert len(saved_reg) == 1
    assert saved_reg.projects[0].id == record.id
    assert saved_reg.projects[0].kind == PROJECT_KIND_DIRECTORY

    # Verify to_dict output
    as_dict = res.to_dict()
    assert as_dict["status"] == "resolved"
    assert as_dict["created"] is True
    assert as_dict["action"] == "register_directory"
    assert as_dict["record"]["id"] == record.id


def test_register_directory_custom_metadata(tmp_path: Path):
    """Register a directory providing explicit display_name, slug, aliases, clock, and id_factory."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    work_dir = tmp_path / "custom-raw-folder"
    work_dir.mkdir()

    fixed_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    fixed_ts = "2026-09-10T12:00:00Z"
    runner = make_fake_non_git_runner()

    res = register_directory(
        work_dir,
        cfg=cfg,
        display_name="My Special App",
        slug="special-app",
        aliases=["my-app", "app-v2"],
        id_factory=lambda: fixed_id,
        clock=lambda: fixed_ts,
        git_runner=runner,
    )

    assert res.created is True
    assert res.project_id == fixed_id
    record = res.record
    assert record is not None
    assert record.id == fixed_id
    assert record.slug == "special-app"
    assert record.display_name == "My Special App"
    assert record.aliases == ["my-app", "app-v2"]
    assert record.created_at == fixed_ts
    assert record.updated_at == fixed_ts
    assert record.memory.project_name == f"ptw-special-app-{fixed_id[:8]}"


def test_register_directory_rejects_git_repository(tmp_path: Path):
    """A directory inside a Git repository must be rejected directing to Git registration."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    git_dir = tmp_path / "git-repo"
    git_dir.mkdir()

    git_probe = GitProbeResult(is_git=True, root=git_dir, status="git")

    with pytest.raises(ValidationError, match=r"inside a Git repository.*Use Git project registration instead"):
        register_directory(git_dir, cfg=cfg, probe=git_probe)

    assert not cfg.projects_yaml.exists()


def test_register_directory_rejects_probe_uncertainty(tmp_path: Path):
    """Probe failures or timeouts must be rejected without mutation."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    work_dir = tmp_path / "uncertain-folder"
    work_dir.mkdir()

    # Case 1: Injected probe with probe_error status
    uncertain_probe = GitProbeResult(is_git=False, error="Git probe timed out.", status="probe_error")
    with pytest.raises(ValidationError, match="Git probe failed or status uncertain.*timed out"):
        register_directory(work_dir, cfg=cfg, probe=uncertain_probe)

    assert not cfg.projects_yaml.exists()

    # Case 2: Injected runner raising TimeoutExpired
    def timeout_runner(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str, str]:
        raise subprocess.TimeoutExpired(cmd, timeout)

    with pytest.raises(ValidationError, match="Git probe failed or status uncertain"):
        register_directory(work_dir, cfg=cfg, git_runner=timeout_runner)

    assert not cfg.projects_yaml.exists()


def test_probe_git_never_exposes_secret_bearing_stderr_to_callers(tmp_path: Path):
    """Security check: probe_git and register_directory must never leak raw or sanitized stderr."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    work_dir = tmp_path / "secret-leak-test"
    work_dir.mkdir()

    secret_string = "SUPER_SECRET_TOKEN_XYZ_987654321"
    secret_stderr = f"fatal: authentication failed for https://user:{secret_string}@host.example.com/repo.git\n"

    def leaking_runner(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str, str]:
        return 1, "", secret_stderr

    # Assert secret is absent from probe_git result
    probe = probe_git(work_dir, runner=leaking_runner)
    assert probe.is_git is False
    assert probe.status == "probe_error"
    assert secret_string not in (probe.error or "")
    assert probe.error == "Git probe failed."

    # Assert secret is absent from register_directory ValidationError
    with pytest.raises(ValidationError) as exc_info:
        register_directory(work_dir, cfg=cfg, git_runner=leaking_runner)

    error_msg = str(exc_info.value)
    assert secret_string not in error_msg
    assert "Git probe failed or status uncertain" in error_msg
    assert not cfg.projects_yaml.exists()


def test_probe_git_exit_128_blank_stderr_is_uncertain_probe_error(tmp_path: Path):
    """Exit code 128 with blank stderr is uncertain (not confirmed non-Git) and treated as probe_error."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    work_dir = tmp_path / "blank-stderr-test"
    work_dir.mkdir()

    def blank_128_runner(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str, str]:
        return 128, "", ""

    probe = probe_git(work_dir, runner=blank_128_runner)
    assert probe.is_git is False
    assert probe.status == "probe_error"
    assert probe.error == "Git probe failed."

    with pytest.raises(ValidationError, match="Git probe failed or status uncertain"):
        register_directory(work_dir, cfg=cfg, git_runner=blank_128_runner)

    assert not cfg.projects_yaml.exists()


def test_register_directory_display_name_validation(tmp_path: Path):
    """Supplied display_name must be a non-blank string, while omitted defaults to basename."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    work_dir = tmp_path / "dir-name-test"
    work_dir.mkdir()
    runner = make_fake_non_git_runner()

    # Reject integer display_name
    with pytest.raises(ValidationError, match="Field 'display_name' must be a non-empty string"):
        register_directory(work_dir, cfg=cfg, display_name=123, git_runner=runner)  # type: ignore

    # Reject empty string display_name
    with pytest.raises(ValidationError, match="Field 'display_name' must be a non-empty string"):
        register_directory(work_dir, cfg=cfg, display_name="", git_runner=runner)

    # Reject whitespace-only display_name
    with pytest.raises(ValidationError, match="Field 'display_name' must be a non-empty string"):
        register_directory(work_dir, cfg=cfg, display_name="   ", git_runner=runner)

    # Omitted display_name defaults cleanly to physical directory basename
    res = register_directory(work_dir, cfg=cfg, display_name=None, git_runner=runner)
    assert res.record is not None
    assert res.record.display_name == "dir-name-test"


def test_register_directory_rejects_nonexistent_path(tmp_path: Path):
    """Non-existent directory path must be rejected."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    missing = tmp_path / "missing-folder"
    with pytest.raises(ValidationError, match="does not exist"):
        register_directory(missing, cfg=cfg)


def test_register_directory_rejects_file_and_symlink_to_file(tmp_path: Path):
    """Files and symlinks pointing to files must be rejected."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    plain_file = tmp_path / "file.txt"
    plain_file.write_text("content", encoding="utf-8")

    with pytest.raises(ValidationError, match="is not a directory"):
        register_directory(plain_file, cfg=cfg)

    symlink_file = tmp_path / "symlink-file"
    symlink_file.symlink_to(plain_file)

    with pytest.raises(ValidationError, match="is not a directory"):
        register_directory(symlink_file, cfg=cfg)


def test_register_directory_rejects_ptw_home_and_descendants(tmp_path: Path):
    """Personal Tideway home itself or any descendant path must be rejected."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    # PTW home itself
    with pytest.raises(ValidationError, match="Cannot register Personal Tideway home"):
        register_directory(ptw_home, cfg=cfg)

    # Descendant within PTW home
    sub = ptw_home / "inner-folder"
    sub.mkdir()
    with pytest.raises(ValidationError, match="Cannot register Personal Tideway home"):
        register_directory(sub, cfg=cfg)


def test_register_directory_exact_idempotence(tmp_path: Path):
    """Registering an already registered directory returns existing project without duplication."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    work_dir = tmp_path / "idempotent-dir"
    work_dir.mkdir()
    runner = make_fake_non_git_runner()

    # Initial registration
    res1 = register_directory(work_dir, cfg=cfg, git_runner=runner)
    assert res1.created is True
    first_id = res1.project_id

    # Second registration of the exact same path
    res2 = register_directory(work_dir, cfg=cfg, git_runner=runner)
    assert res2.status == "resolved"
    assert res2.created is False
    assert res2.project_id == first_id
    assert res2.evidence == ["path"]
    assert res2.action == "noop_exact"

    # Only 1 project in registry
    saved_reg = load_registry(cfg.projects_yaml)
    assert len(saved_reg) == 1


def test_register_directory_ancestor_idempotence(tmp_path: Path):
    """A directory covered by an already registered ancestor resolves to that ancestor."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    parent_dir = tmp_path / "parent-workspace"
    parent_dir.mkdir()
    child_dir = parent_dir / "modules" / "submodule"
    child_dir.mkdir(parents=True)
    runner = make_fake_non_git_runner()

    # Register parent
    res_parent = register_directory(parent_dir, cfg=cfg, git_runner=runner)
    assert res_parent.created is True

    # Register child
    res_child = register_directory(child_dir, cfg=cfg, git_runner=runner)
    assert res_child.status == "resolved"
    assert res_child.created is False
    assert res_child.project_id == res_parent.project_id
    assert res_child.record.id == res_parent.project_id
    assert res_child.evidence == ["path"]
    assert res_child.action == "noop_ancestor"

    # Registry has only the parent project
    saved_reg = load_registry(cfg.projects_yaml)
    assert len(saved_reg) == 1
    assert saved_reg.projects[0].id == res_parent.project_id


def test_register_directory_slug_collision_resolution(tmp_path: Path):
    """Colliding slugs resolve deterministically with ID suffix."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    dir1 = tmp_path / "work" / "common-name"
    dir1.mkdir(parents=True)
    dir2 = tmp_path / "other" / "common-name"
    dir2.mkdir(parents=True)
    runner = make_fake_non_git_runner()

    id1 = "11111111-1111-4111-8111-111111111111"
    id2 = "22222222-2222-4222-8222-222222222222"

    res1 = register_directory(dir1, cfg=cfg, id_factory=lambda: id1, git_runner=runner)
    assert res1.record.slug == "common-name"

    res2 = register_directory(dir2, cfg=cfg, id_factory=lambda: id2, git_runner=runner)
    assert res2.record.slug == f"common-name-{id2[:8]}"

    saved_reg = load_registry(cfg.projects_yaml)
    assert len(saved_reg) == 2
    assert saved_reg.projects[0].slug == "common-name"
    assert saved_reg.projects[1].slug == f"common-name-{id2[:8]}"


def test_register_directory_explicit_slug_collision_error(tmp_path: Path):
    """Explicitly specified slug colliding with an existing project raises ValidationError."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    dir1 = tmp_path / "dir1"
    dir1.mkdir()
    dir2 = tmp_path / "dir2"
    dir2.mkdir()
    runner = make_fake_non_git_runner()

    register_directory(dir1, cfg=cfg, slug="shared-slug", git_runner=runner)

    with pytest.raises(ValidationError, match="already in use in registry"):
        register_directory(dir2, cfg=cfg, slug="shared-slug", git_runner=runner)


def test_register_directory_dry_run_writes_nothing(tmp_path: Path):
    """dry_run=True returns proposed record without modifying disk or supplied registry."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    work_dir = tmp_path / "dry-run-folder"
    work_dir.mkdir()
    runner = make_fake_non_git_runner()

    in_memory_reg = ProjectRegistry.empty()
    res = register_directory(work_dir, cfg=cfg, registry=in_memory_reg, dry_run=True, git_runner=runner)

    assert res.status == "candidate"
    assert res.created is False
    assert res.requires_confirmation is True
    assert res.action == "register_directory"
    assert res.proposed_record is not None
    assert res.proposed_record.slug == "dry-run-folder"
    assert res.candidate_metadata is not None
    assert "proposed_id" in res.candidate_metadata

    # In-memory registry unchanged
    assert len(in_memory_reg) == 0

    # No projects.yaml created
    assert not cfg.projects_yaml.exists()


def test_register_directory_repeated_registration(tmp_path: Path):
    """Multiple registrations persist sequentially in stable insertion order."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    runner = make_fake_non_git_runner()

    dirs = [tmp_path / f"project-{i}" for i in range(3)]
    for d in dirs:
        d.mkdir()

    for d in dirs:
        res = register_directory(d, cfg=cfg, git_runner=runner)
        assert res.created is True

    saved_reg = load_registry(cfg.projects_yaml)
    assert len(saved_reg) == 3
    for i, d in enumerate(dirs):
        assert saved_reg.projects[i].slug == d.name
        assert saved_reg.projects[i].bindings.paths == [str(d.resolve())]


def test_register_directory_persistence_rollback(tmp_path: Path):
    """Persistence failure leaves supplied in-memory registry unchanged."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    work_dir = tmp_path / "rollback-test"
    work_dir.mkdir()
    runner = make_fake_non_git_runner()

    # Ensure registry directory exists and place symlink target inside cfg.home
    cfg.projects_yaml.parent.mkdir(parents=True, exist_ok=True)
    actual_file = cfg.projects_yaml.parent / "real_registry.yaml"
    actual_file.touch()
    cfg.projects_yaml.symlink_to(actual_file)

    in_memory_reg = ProjectRegistry.empty()

    with pytest.raises(ConfigError, match="is a symlink"):
        register_directory(work_dir, cfg=cfg, registry=in_memory_reg, git_runner=runner)

    # In-memory registry must remain completely unchanged
    assert len(in_memory_reg) == 0


def test_register_directory_unicode_spaces_symlink_normalization(tmp_path: Path):
    """Supports Unicode characters, spaces, and resolves symlink to normalized physical path."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    real_dir = tmp_path / "Мой Проект (тест) 🚀"
    real_dir.mkdir()

    symlink_dir = tmp_path / "symlink_dir"
    symlink_dir.symlink_to(real_dir)

    runner = make_fake_non_git_runner()
    res = register_directory(symlink_dir, cfg=cfg, git_runner=runner)

    assert res.created is True
    # Normalized physical path
    assert res.record.bindings.paths == [os.path.normpath(str(real_dir.resolve()))]
    # Display name preserved
    assert res.record.display_name == "Мой Проект (тест) 🚀"


def test_register_directory_source_tree_unchanged(tmp_path: Path):
    """Source directory tree is completely untouched by directory registration."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    work_dir = tmp_path / "source-tree"
    work_dir.mkdir()
    (work_dir / "app.py").write_text("print('untouched')", encoding="utf-8")
    sub = work_dir / "data"
    sub.mkdir()
    (sub / "info.json").write_text('{"key": "val"}', encoding="utf-8")

    # Record files before registration
    files_before = sorted(str(p.relative_to(work_dir)) for p in work_dir.rglob("*"))

    runner = make_fake_non_git_runner()
    res = register_directory(work_dir, cfg=cfg, git_runner=runner)
    assert res.created is True

    # Record files after registration
    files_after = sorted(str(p.relative_to(work_dir)) for p in work_dir.rglob("*"))

    assert files_before == files_after
    assert not (work_dir / ".personal-tideway.yaml").exists()


def test_directory_registration_service(tmp_path: Path):
    """DirectoryRegistrationService class delegates cleanly with bound dependencies."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    work_dir = tmp_path / "service-test"
    work_dir.mkdir()

    fixed_id = "55555555-5555-4555-8555-555555555555"
    runner = make_fake_non_git_runner()

    svc = DirectoryRegistrationService(
        cfg=cfg,
        git_runner=runner,
        id_factory=lambda: fixed_id,
    )

    res = svc.register(work_dir)
    assert res.created is True
    assert res.project_id == fixed_id
