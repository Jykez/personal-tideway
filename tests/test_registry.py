"""Tests for Personal Tideway v2 project registry schema, validation, and persistence."""

from datetime import datetime, timezone
import os
from pathlib import Path
import pytest
import yaml

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import (
    DEFAULT_MEMORY_BACKEND,
    PROJECT_KIND_DIRECTORY,
    PROJECT_KIND_EXTERNAL,
    PROJECT_KIND_GIT,
    SCHEMA_VERSION,
)
from personal_tideway.core.registry import (
    ProjectBindings,
    ProjectMemory,
    ProjectRecord,
    ProjectRegistry,
    load_registry,
    save_registry,
)
from personal_tideway.exceptions import BoundaryError, ConfigError, ValidationError


def make_fake_project(
    project_id: str = "550e8400-e29b-41d4-a716-446655440000",
    slug: str = "infra-core",
    display_name: str = "Core Infrastructure",
    kind: str = PROJECT_KIND_GIT,
    aliases: list[str] | None = None,
    paths: list[str] | None = None,
    git_common_dirs: list[str] | None = None,
    git_remotes: list[str] | None = None,
    created_at: str = "2026-09-09T12:00:00Z",
    updated_at: str = "2026-09-09T12:00:00Z",
) -> ProjectRecord:
    """Helper to create a valid ProjectRecord with fake data."""
    clean_paths = paths if paths is not None else ["/repos/infra-core"]
    clean_gcds = git_common_dirs if git_common_dirs is not None else ["/repos/infra-core/.git"]
    clean_remotes = git_remotes if git_remotes is not None else ["github.com/org/infra-core.git"]
    clean_aliases = aliases if aliases is not None else ["infra", "core"]

    if kind == PROJECT_KIND_EXTERNAL:
        clean_paths = []
        clean_gcds = []
        clean_remotes = []

    return ProjectRecord.create(
        project_id=project_id,
        slug=slug,
        display_name=display_name,
        kind=kind,
        aliases=clean_aliases,
        paths=clean_paths,
        git_common_dirs=clean_gcds,
        git_remotes=clean_remotes,
        created_at=created_at,
        updated_at=updated_at,
    )


# ---------------------------------------------------------------------------
# 1. Valid Round-Trip and Idempotence
# ---------------------------------------------------------------------------


def test_valid_round_trip_dict_and_yaml(tmp_path: Path):
    """Test full round-trip serialization between object, dict, and YAML."""
    p1 = make_fake_project(
        project_id="c0ffee00-1111-4444-8888-000000000001",
        slug="proj-alpha",
        display_name="Project Alpha",
        aliases=["alpha-one", "alpha-main"],
        paths=["/repos/alpha"],
        git_common_dirs=["/repos/alpha/.git"],
        git_remotes=["github.com/org/alpha.git"],
    )
    p2 = make_fake_project(
        project_id="c0ffee00-2222-4444-8888-000000000002",
        slug="proj-external",
        display_name="External Switch",
        kind=PROJECT_KIND_EXTERNAL,
        aliases=["switch-core"],
    )

    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p1, p2])
    data = reg.to_dict()

    assert data["version"] == 2
    assert len(data["projects"]) == 2
    assert data["projects"][0]["slug"] == "proj-alpha"
    assert data["projects"][1]["slug"] == "proj-external"

    # from_dict roundtrip
    restored = ProjectRegistry.from_dict(data)
    assert len(restored) == 2
    assert restored.projects[0].slug == "proj-alpha"
    assert restored.projects[1].kind == PROJECT_KIND_EXTERNAL

    # to_yaml roundtrip
    yaml_text = reg.to_yaml()
    assert "version: 2" in yaml_text
    assert "slug: proj-alpha" in yaml_text

    loaded = yaml.safe_load(yaml_text)
    restored_yaml = ProjectRegistry.from_dict(loaded)
    assert restored_yaml.to_dict() == data


def test_save_and_load_registry_round_trip_is_idempotent(tmp_path: Path):
    """Test save_registry and load_registry round-trip preserves byte-for-byte YAML."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir(parents=True)
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    p1 = make_fake_project(
        project_id="a1111111-2222-4333-8444-555555555555",
        slug="frontend-app",
        display_name="Frontend Application",
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p1])

    # First save
    target_path = save_registry(reg, cfg)
    assert target_path == cfg.projects_yaml
    assert target_path.is_file()

    first_content = target_path.read_text(encoding="utf-8")

    # Load back
    loaded_reg = load_registry(target_path)
    assert len(loaded_reg) == 1
    assert loaded_reg.projects[0].id == p1.id

    # Second save (idempotency check)
    save_registry(loaded_reg, cfg)
    second_content = target_path.read_text(encoding="utf-8")

    assert first_content == second_content


# ---------------------------------------------------------------------------
# 2. Unicode Display Name and Spaces in Absolute Paths
# ---------------------------------------------------------------------------


def test_unicode_display_name_and_spaces_in_paths(tmp_path: Path):
    """Test Unicode display name and spaces in absolute normalized paths."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir(parents=True)
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    unicode_name = "Управление инфраструктурой 🚀 / Café René"
    spaced_path = str(tmp_path / "my spaces in folder" / "worktree repo")

    proj = make_fake_project(
        project_id="b2222222-3333-4444-8555-666666666666",
        slug="unicode-spaces",
        display_name=unicode_name,
        paths=[spaced_path],
        git_common_dirs=[f"{spaced_path}/.git"],
    )

    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[proj])
    save_registry(reg, cfg)

    # Verify directly in YAML file that Unicode is not escaped as \uXXXX
    yaml_text = cfg.projects_yaml.read_text(encoding="utf-8")
    assert unicode_name in yaml_text
    assert spaced_path in yaml_text

    # Verify loaded registry preserves them identically
    loaded = load_registry(cfg.projects_yaml)
    assert loaded.projects[0].display_name == unicode_name
    assert loaded.projects[0].bindings.paths[0] == os.path.normpath(spaced_path)


# ---------------------------------------------------------------------------
# 3. Validation Families: UUID4, Kinds, Slugs, Aliases, Timestamps
# ---------------------------------------------------------------------------


def test_reject_invalid_uuid4():
    """Reject missing, non-UUID, non-v4, or non-canonical UUID string."""
    with pytest.raises(ValidationError, match="valid UUID"):
        make_fake_project(project_id="not-a-uuid")

    # UUID v1 (timestamp-based)
    uuid_v1 = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
    with pytest.raises(ValidationError, match="UUID version 4"):
        make_fake_project(project_id=uuid_v1)

    # Non-canonical UUID without hyphens
    uuid_no_hyphens = "550e8400e29b41d4a716446655440000"
    with pytest.raises(ValidationError, match="canonical formatted"):
        make_fake_project(project_id=uuid_no_hyphens)


def test_reject_invalid_kinds_and_external_bindings():
    """Reject unknown kinds and filesystem/git bindings on external projects."""
    with pytest.raises(ValidationError, match="Invalid project kind"):
        make_fake_project(kind="unknown_kind")

    # External project must not have path bindings
    with pytest.raises(ValidationError, match="External project .* must not have path"):
        ProjectRecord.create(
            project_id="550e8400-e29b-41d4-a716-446655440000",
            slug="ext-proj",
            display_name="External",
            kind=PROJECT_KIND_EXTERNAL,
            paths=["/some/path"],
        )

    # External project must not have git bindings
    with pytest.raises(ValidationError, match="External project .* must not have git"):
        ProjectRecord.create(
            project_id="550e8400-e29b-41d4-a716-446655440000",
            slug="ext-proj",
            display_name="External",
            kind=PROJECT_KIND_EXTERNAL,
            git_common_dirs=["/some/path/.git"],
        )


def test_reject_invalid_slugs_and_aliases():
    """Reject malformed slugs/aliases and internal token collisions."""
    # Invalid characters
    with pytest.raises(ValidationError, match="Must contain only letters"):
        make_fake_project(slug="invalid slug with spaces")

    with pytest.raises(ValidationError, match="Must contain only letters"):
        make_fake_project(slug="invalid/slash")

    with pytest.raises(ValidationError, match="Must contain only letters"):
        make_fake_project(aliases=["good-alias", "bad@alias"])

    # Duplicate alias within project
    with pytest.raises(ValidationError, match="Duplicate alias"):
        make_fake_project(aliases=["alias-one", "alias-one"])

    # Duplicate alias case-insensitively
    with pytest.raises(ValidationError, match="Duplicate alias"):
        make_fake_project(aliases=["my-alias", "MY-ALIAS"])

    # Slug matches alias
    with pytest.raises(ValidationError, match="cannot also be declared as an alias"):
        make_fake_project(slug="my-proj", aliases=["my-proj"])


def test_reject_invalid_timestamps():
    """Reject non-UTC, naive, or malformed RFC3339 timestamps."""
    # Naive timestamp without timezone
    with pytest.raises(ValidationError, match="in UTC timezone"):
        make_fake_project(created_at="2026-09-09T12:00:00")

    # Non-UTC offset (+03:00)
    with pytest.raises(ValidationError, match="in UTC timezone"):
        make_fake_project(created_at="2026-09-09T12:00:00+03:00")

    # RFC3339 -00:00 means an unknown local offset, not confirmed UTC.
    with pytest.raises(ValidationError, match="in UTC timezone"):
        make_fake_project(created_at="2026-09-09T12:00:00-00:00")

    # Date-only without time and T separator
    with pytest.raises(ValidationError, match="with 'T' separator"):
        make_fake_project(created_at="2026-09-09Z")

    # Garbage string
    with pytest.raises(ValidationError, match="RFC3339"):
        make_fake_project(created_at="not-a-timestamp")


# ---------------------------------------------------------------------------
# 4. Filesystem Bindings: Relative Rejection and Normalization
# ---------------------------------------------------------------------------


def test_reject_relative_filesystem_bindings():
    """Reject relative paths in bindings.paths and bindings.git_common_dirs."""
    with pytest.raises(ValidationError, match="must be an absolute path, got relative path"):
        make_fake_project(paths=["relative/path/to/repo"])

    with pytest.raises(ValidationError, match="must be an absolute path, got relative path"):
        make_fake_project(git_common_dirs=["relative/.git"])

    with pytest.raises(ValidationError, match="path binding must be a non-empty string"):
        make_fake_project(paths=[123])


def test_filesystem_bindings_are_normalized_and_duplicate_paths_rejected():
    """Paths are normalized and duplicates within one project are rejected."""
    # Normalization
    p = make_fake_project(paths=["/repos/foo/bar/../bar/"])
    assert p.bindings.paths == ["/repos/foo/bar"]

    # Duplicate paths within same project
    with pytest.raises(ValidationError, match="Duplicate path"):
        make_fake_project(paths=["/repos/foo", "/repos/foo/"])


# ---------------------------------------------------------------------------
# 5. Memory Path Safety
# ---------------------------------------------------------------------------


def test_memory_path_safety_and_backend():
    """Reject unsafe, non-conforming, or escaping memory paths and unknown backends."""
    valid_id = "550e8400-e29b-41d4-a716-446655440000"
    p = make_fake_project(project_id=valid_id)
    assert p.memory.path == f"projects/{valid_id}/memory"

    # Backend must be basic-memory
    p.memory.backend = "sqlite-memory"
    with pytest.raises(ValidationError, match="Unsupported memory backend"):
        p.validate()
    p.memory.backend = DEFAULT_MEMORY_BACKEND

    # Escaping memory path via ..
    p.memory.path = f"projects/{valid_id}/memory/../../escape"
    with pytest.raises(ValidationError, match="Invalid memory path"):
        p.validate()

    # Absolute memory path
    p.memory.path = f"/projects/{valid_id}/memory"
    with pytest.raises(ValidationError, match="Invalid memory path"):
        p.validate()

    # Mismatched project ID in memory path
    p.memory.path = "projects/00000000-0000-4000-8000-000000000000/memory"
    with pytest.raises(ValidationError, match="Invalid memory path"):
        p.validate()


# ---------------------------------------------------------------------------
# 6. Missing and Unknown Fields, Wrong Types
# ---------------------------------------------------------------------------


def test_reject_unknown_and_missing_fields_and_wrong_types():
    """Reject missing fields, unknown fields, and wrong types in records and registry."""
    valid_dict = make_fake_project().to_dict()

    # Unknown field in record
    bad_dict = dict(valid_dict)
    bad_dict["unexpected_field"] = "value"
    with pytest.raises(ValidationError, match="Unknown field"):
        ProjectRecord.from_dict(bad_dict)

    # Missing field in record
    bad_dict2 = dict(valid_dict)
    del bad_dict2["slug"]
    with pytest.raises(ValidationError, match="Missing required field"):
        ProjectRecord.from_dict(bad_dict2)

    # Wrong type for aliases
    bad_dict3 = dict(valid_dict)
    bad_dict3["aliases"] = "not-a-list"
    with pytest.raises(ValidationError, match="must be a list"):
        ProjectRecord.from_dict(bad_dict3)

    # Wrong type for display_name
    bad_dict4 = dict(valid_dict)
    bad_dict4["display_name"] = 12345
    with pytest.raises(ValidationError, match="must be a string"):
        ProjectRecord.from_dict(bad_dict4)

    # Unknown root field in registry
    with pytest.raises(ConfigError, match="Unknown root field"):
        ProjectRegistry.from_dict({"version": 2, "projects": [], "extra": True})

    # Missing version in registry
    with pytest.raises(ConfigError, match="Missing required 'version'"):
        ProjectRegistry.from_dict({"projects": []})

    # Non-mapping root in registry
    with pytest.raises(ConfigError, match="Registry root must be a mapping"):
        ProjectRegistry.from_dict(["version", 2])


# ---------------------------------------------------------------------------
# 7. Collisions Across Projects
# ---------------------------------------------------------------------------


def test_registry_token_and_binding_collisions():
    """Reject duplicate IDs, slugs, aliases, paths, and git-common-dirs across projects."""
    p1 = make_fake_project(
        project_id="11111111-1111-4111-8111-111111111111",
        slug="service-api",
        aliases=["api-server", "backend"],
        paths=["/repos/service-api"],
        git_common_dirs=["/repos/service-api/.git"],
        git_remotes=["github.com/org/service.git"],
    )

    # 1. Duplicate ID
    p2_dup_id = make_fake_project(
        project_id="11111111-1111-4111-8111-111111111111",
        slug="service-other",
        aliases=[],
        paths=["/repos/other"],
        git_common_dirs=["/repos/other/.git"],
    )
    with pytest.raises(ValidationError, match="Registry collision: token '11111111-1111-4111-8111-111111111111'"):
        ProjectRegistry(version=2, projects=[p1, p2_dup_id]).validate()

    # 2. Duplicate slug case-insensitively
    p2_dup_slug = make_fake_project(
        project_id="22222222-2222-4222-8222-222222222222",
        slug="SERVICE-API",
        aliases=[],
        paths=["/repos/other"],
        git_common_dirs=["/repos/other/.git"],
    )
    with pytest.raises(ValidationError, match="Registry collision: token 'service-api'"):
        ProjectRegistry(version=2, projects=[p1, p2_dup_slug]).validate()

    # 3. Alias of project 2 collides with slug of project 1
    p2_alias_collides_slug = make_fake_project(
        project_id="33333333-3333-4333-8333-333333333333",
        slug="service-web",
        aliases=["SERVICE-API"],
        paths=["/repos/web"],
        git_common_dirs=["/repos/web/.git"],
    )
    with pytest.raises(ValidationError, match="Registry collision: token 'service-api'"):
        ProjectRegistry(version=2, projects=[p1, p2_alias_collides_slug]).validate()

    # 4. Duplicate path binding
    p2_dup_path = make_fake_project(
        project_id="44444444-4444-4444-8444-444444444444",
        slug="service-client",
        aliases=[],
        paths=["/repos/service-api/"],
        git_common_dirs=["/repos/service-client/.git"],
    )
    with pytest.raises(ValidationError, match="Duplicate path binding"):
        ProjectRegistry(version=2, projects=[p1, p2_dup_path]).validate()

    # 5. Duplicate git_common_dir binding
    p2_dup_gcd = make_fake_project(
        project_id="55555555-5555-4555-8555-555555555555",
        slug="service-worker",
        aliases=[],
        paths=["/repos/service-worker"],
        git_common_dirs=["/repos/service-api/.git"],
    )
    with pytest.raises(ValidationError, match="Duplicate git_common_dir binding"):
        ProjectRegistry(version=2, projects=[p1, p2_dup_gcd]).validate()

    # 6. Duplicate git_remotes ARE ALLOWED across projects
    p2_dup_remote = make_fake_project(
        project_id="66666666-6666-4666-8666-666666666666",
        slug="service-replica",
        aliases=[],
        paths=["/repos/service-replica"],
        git_common_dirs=["/repos/service-replica/.git"],
        git_remotes=["github.com/org/service.git"],  # identical remote
    )
    reg_ok = ProjectRegistry(version=2, projects=[p1, p2_dup_remote])
    reg_ok.validate()  # must not raise


# ---------------------------------------------------------------------------
# 8. Missing File Read-Only Behavior
# ---------------------------------------------------------------------------


def test_missing_registry_file_read_only(tmp_path: Path):
    """Missing registry file returns empty v2 registry without writing to disk."""
    absent_file = tmp_path / "registry" / "projects.yaml"
    assert not absent_file.exists()

    reg = load_registry(absent_file)
    assert isinstance(reg, ProjectRegistry)
    assert reg.version == 2
    assert len(reg.projects) == 0

    # Ensure no directories or files were created
    assert not absent_file.exists()
    assert not absent_file.parent.exists()


# ---------------------------------------------------------------------------
# 9. Malformed YAML and Newer Schema Non-Rewrite
# ---------------------------------------------------------------------------


def test_malformed_yaml_and_newer_schema_rejection(tmp_path: Path):
    """Reject malformed YAML, unknown/newer schema versions, and refuse to overwrite newer files."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir(parents=True)
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    reg_dir = ptw_home / "registry"
    reg_dir.mkdir(parents=True)
    reg_file = reg_dir / "projects.yaml"

    # Malformed YAML
    secret_marker = "registry_secret_value_must_not_leak"
    reg_file.write_text(
        f"version: 2\nsecret: {secret_marker}\nprojects: [broken yaml: :",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="Malformed YAML") as malformed_error:
        load_registry(reg_file)
    assert secret_marker not in str(malformed_error.value)

    # Newer schema version (version: 3)
    newer_content = "version: 3\nprojects: []\n"
    reg_file.write_text(newer_content, encoding="utf-8")

    with pytest.raises(ConfigError, match="newer than supported version"):
        load_registry(reg_file)

    # Attempt to overwrite newer version with save_registry must fail and NOT modify file
    dummy_reg = ProjectRegistry.empty()
    with pytest.raises(ConfigError, match="newer schema version"):
        save_registry(dummy_reg, cfg)

    # Verify file content was completely untouched
    assert reg_file.read_text(encoding="utf-8") == newer_content


# ---------------------------------------------------------------------------
# 10. Symlink and Path Boundary Rejection
# ---------------------------------------------------------------------------


def test_symlink_and_boundary_rejection_on_save(tmp_path: Path):
    """Reject symlink target and paths escaping PERSONAL_TIDEWAY_HOME."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir(parents=True)
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    outside = tmp_path / "outside_dir"
    outside.mkdir(parents=True)

    reg = ProjectRegistry.empty()

    # 1. Target outside workspace
    outside_target = outside / "projects.yaml"
    with pytest.raises(BoundaryError, match="outside workspace root"):
        save_registry(reg, cfg, target_path=outside_target)

    # A different file inside the workspace is still not the canonical registry.
    with pytest.raises(ConfigError, match="not the configured canonical registry"):
        save_registry(reg, cfg, target_path=ptw_home / "other-projects.yaml")

    # 2. Target is a symlink
    reg_dir = ptw_home / "registry"
    reg_dir.mkdir(parents=True)
    real_file = ptw_home / "real_file.yaml"
    real_file.write_text("version: 2\nprojects: []\n", encoding="utf-8")
    symlink_target = reg_dir / "projects.yaml"
    os.symlink(real_file, symlink_target)

    with pytest.raises(ConfigError, match="is a symlink"):
        save_registry(reg, cfg, target_path=symlink_target)


# ---------------------------------------------------------------------------
# 11. Dry-Run Makes Zero Filesystem Changes
# ---------------------------------------------------------------------------


def test_dry_run_makes_no_filesystem_changes(tmp_path: Path):
    """dry_run=True validates target and registry but writes nothing to disk."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir(parents=True)
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    p1 = make_fake_project()
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p1])

    res = save_registry(reg, cfg, dry_run=True)
    assert res == cfg.projects_yaml
    assert not cfg.projects_yaml.exists()
    assert not cfg.registry_dir.exists()


# ---------------------------------------------------------------------------
# 12. In-Memory Lookup, Add, Update, Remove Primitives
# ---------------------------------------------------------------------------


def test_in_memory_lookup_add_update_remove():
    """Test lookup, add, update, and remove primitives."""
    reg = ProjectRegistry.empty()
    p1 = make_fake_project(
        project_id="aaaaaaaa-1111-4111-8111-111111111111",
        slug="service-auth",
        display_name="Auth Service",
        aliases=["auth-sso", "identity"],
    )
    p2 = make_fake_project(
        project_id="bbbbbbbb-2222-4222-8222-222222222222",
        slug="service-billing",
        display_name="Billing Service",
        aliases=["payments"],
        paths=["/repos/billing"],
        git_common_dirs=["/repos/billing/.git"],
    )

    # Add
    reg.add_project(p1)
    reg.add_project(p2)
    assert len(reg) == 2

    # Lookup by ID case-insensitively
    assert reg.get("AAAAAAAA-1111-4111-8111-111111111111") == p1

    # Lookup by slug case-insensitively
    assert reg.get("SERVICE-AUTH") == p1

    # Lookup by alias case-insensitively
    assert reg.get("PAYMENTS") == p2
    assert reg.get("identity") == p1

    # Lookup unknown
    assert reg.get("unknown-token") is None

    # Update
    updated_p1 = make_fake_project(
        project_id="aaaaaaaa-1111-4111-8111-111111111111",
        slug="service-auth-v2",
        display_name="Auth Service V2",
        aliases=["auth-sso"],
        paths=["/repos/service-auth"],
        git_common_dirs=["/repos/service-auth/.git"],
    )
    reg.update_project(updated_p1)
    assert reg.get("service-auth-v2") == updated_p1
    assert reg.projects[0].display_name == "Auth Service V2"
    # Position preserved at index 0
    assert reg.projects[0].id == p1.id

    # Remove
    removed = reg.remove_project("payments")
    assert removed.id == p2.id
    assert len(reg) == 1
    assert reg.get("service-billing") is None

    # Remove non-existent raises ValidationError
    with pytest.raises(ValidationError, match="not found in registry"):
        reg.remove_project("non-existent")


# ---------------------------------------------------------------------------
# 13. Ordering Determinism (Stable Insertion Order)
# ---------------------------------------------------------------------------


def test_stable_insertion_order_preserved():
    """Stable insertion order is maintained across operations and round-trips."""
    reg = ProjectRegistry.empty()
    ids = [
        "11111111-0000-4000-8000-000000000001",
        "22222222-0000-4000-8000-000000000002",
        "33333333-0000-4000-8000-000000000003",
    ]
    for i, pid in enumerate(ids):
        reg.add_project(
            make_fake_project(
                project_id=pid,
                slug=f"proj-{i}",
                paths=[f"/repos/proj-{i}"],
                git_common_dirs=[f"/repos/proj-{i}/.git"],
                aliases=[],
            )
        )

    assert [p.id for p in reg.projects] == ids

    # Update middle element preserves position
    updated_middle = make_fake_project(
        project_id=ids[1],
        slug="proj-1-renamed",
        paths=["/repos/proj-1"],
        git_common_dirs=["/repos/proj-1/.git"],
        aliases=[],
    )
    reg.update_project(updated_middle)
    assert [p.id for p in reg.projects] == ids

    # Round-trip through YAML keeps exact order
    loaded = ProjectRegistry.from_dict(yaml.safe_load(reg.to_yaml()))
    assert [p.id for p in loaded.projects] == ids


# ---------------------------------------------------------------------------
# 14. Regression: Persistence Creates Nothing in Registered Source Paths
# ---------------------------------------------------------------------------


def test_persistence_creates_nothing_below_registered_source_paths(tmp_path: Path):
    """Regression test: saving registry never writes artifacts into registered project paths."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir(parents=True)
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    source_dir = tmp_path / "user_source_repo"
    source_dir.mkdir(parents=True)
    readme = source_dir / "README.md"
    readme.write_text("# User Project\n", encoding="utf-8")

    proj = make_fake_project(
        project_id="77777777-7777-4777-8777-777777777777",
        slug="my-repo",
        paths=[str(source_dir)],
        git_common_dirs=[str(source_dir / ".git")],
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[proj])

    # Save registry
    save_registry(reg, cfg)

    # Verify source_dir has strictly untouched original content
    assert list(source_dir.iterdir()) == [readme]
    assert not (source_dir / ".personal-tideway").exists()
    assert not (source_dir / ".personal-tideway.yaml").exists()
    assert not (source_dir / ".basic-memory").exists()
    assert not (source_dir / "AGENTS.md").exists()
    assert not (source_dir / "GEMINI.md").exists()
    assert not (source_dir / "project.yaml").exists()
