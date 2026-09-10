"""Tests for Basic Memory isolated runtime layout, safe bootstrap config, and pure install planning.

Work Package 9a verification suite:
- Exact isolated paths under Personal Tideway services/basic-memory.
- Exact pinned version, requirement, install argv, and env overrides.
- Deterministic bootstrap config JSON ending with a newline.
- Default project is null, no main project fallback, no external or note paths.
- Rejection of uninitialized workspace without mutation.
- Strict validation of uv executable: relative, missing, non-executable, symlink.
- Boundary error and symlink escape rejection under cfg.home.
- Deterministic repeated planning.
- Isolation from host environment and prevention of secret leaks in to_dict.
- Zero filesystem mutation verified via pre/post filesystem snapshots.
- Immutability of MappingProxyType attributes and mutation rejection.
- Safety model rejection for auto_update, default_project, or non-empty projects.
- Fixed safe error messages asserting secret sentinel absence in exceptions.
"""

from dataclasses import replace
import json
import os
from pathlib import Path
from types import MappingProxyType
import pytest

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.core.basic_memory_runtime import (
    BASIC_MEMORY_AUTO_UPDATE_VALUE,
    BASIC_MEMORY_NO_PROMOS_VALUE,
    BASIC_MEMORY_PINNED_VERSION,
    BASIC_MEMORY_REQUIREMENT,
    ENV_BASIC_MEMORY_AUTO_UPDATE,
    ENV_BASIC_MEMORY_CONFIG_DIR,
    ENV_BASIC_MEMORY_NO_PROMOS,
    ENV_UV_CACHE_DIR,
    ENV_UV_TOOL_BIN_DIR,
    ENV_UV_TOOL_DIR,
    BasicMemoryBootstrapConfig,
    BasicMemoryInstallPlan,
    BasicMemoryInstallPlanPreview,
    BasicMemoryLayout,
    build_basic_memory_bootstrap_config,
    build_basic_memory_install_plan,
    get_basic_memory_layout,
    validate_uv_executable,
)
from personal_tideway.exceptions import BoundaryError, ConfigError, ValidationError


def create_mock_uv(tmp_path: Path) -> Path:
    """Create a mock executable file representing uv."""
    bin_dir = tmp_path / "mock_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    uv_file = bin_dir / "uv"
    uv_file.write_text("#!/bin/sh\necho uv 0.5.0\n", encoding="utf-8")
    uv_file.chmod(0o755)
    return uv_file


def test_exact_isolated_paths(personal_tideway_config: PersonalTidewayConfig):
    """Assert exact directory and file layout under canonical services/basic-memory."""
    layout = get_basic_memory_layout(personal_tideway_config)

    assert layout.service_root == personal_tideway_config.basic_memory_dir
    assert layout.service_root == personal_tideway_config.home / "services" / "basic-memory"
    assert layout.uv_tool_dir == layout.service_root / "tool"
    assert layout.bin_dir == layout.service_root / "bin"
    assert layout.config_dir == layout.service_root / "config"
    assert layout.cache_dir == layout.service_root / "cache"
    assert layout.config_file == layout.config_dir / "config.json"
    assert layout.primary_executable == layout.bin_dir / "basic-memory"
    assert layout.alias_executable == layout.bin_dir / "bm"
    assert layout.executable_candidates == (layout.primary_executable, layout.alias_executable)

    # All paths must be within workspace home
    for p in (
        layout.service_root,
        layout.uv_tool_dir,
        layout.bin_dir,
        layout.config_dir,
        layout.cache_dir,
        layout.config_file,
        layout.primary_executable,
        layout.alias_executable,
    ):
        assert p.is_relative_to(personal_tideway_config.home)

    data = layout.to_dict()
    assert data["service_root"] == str(layout.service_root)
    assert data["uv_tool_dir"] == str(layout.uv_tool_dir)
    assert data["bin_dir"] == str(layout.bin_dir)
    assert data["config_dir"] == str(layout.config_dir)
    assert data["cache_dir"] == str(layout.cache_dir)
    assert data["config_file"] == str(layout.config_file)
    assert data["primary_executable"] == str(layout.primary_executable)
    assert data["alias_executable"] == str(layout.alias_executable)
    assert data["executable_candidates"] == [str(layout.primary_executable), str(layout.alias_executable)]


def test_exact_pin_argv_env_keys(personal_tideway_config: PersonalTidewayConfig, tmp_path: Path):
    """Assert exact version pin 0.23.2, install argv with --no-config, and environment overrides."""
    assert BASIC_MEMORY_PINNED_VERSION == "0.23.2"
    assert BASIC_MEMORY_REQUIREMENT == "basic-memory==0.23.2"

    mock_uv = create_mock_uv(tmp_path)
    plan = build_basic_memory_install_plan(personal_tideway_config, uv_executable=mock_uv)

    expected_install_argv = (
        str(mock_uv),
        "--no-config",
        "tool",
        "install",
        "--force",
        "--prerelease=allow",
        "basic-memory==0.23.2",
    )
    assert plan.install_argv == expected_install_argv
    assert plan.health_argv == (str(plan.layout.primary_executable), "--version")

    expected_env_keys = {
        ENV_UV_TOOL_DIR,
        ENV_UV_TOOL_BIN_DIR,
        ENV_UV_CACHE_DIR,
        ENV_BASIC_MEMORY_CONFIG_DIR,
        ENV_BASIC_MEMORY_AUTO_UPDATE,
        ENV_BASIC_MEMORY_NO_PROMOS,
    }
    assert set(plan.env_overrides.keys()) == expected_env_keys
    assert plan.env_overrides[ENV_UV_TOOL_DIR] == str(plan.layout.uv_tool_dir)
    assert plan.env_overrides[ENV_UV_TOOL_BIN_DIR] == str(plan.layout.bin_dir)
    assert plan.env_overrides[ENV_UV_CACHE_DIR] == str(plan.layout.cache_dir)
    assert plan.env_overrides[ENV_BASIC_MEMORY_CONFIG_DIR] == str(plan.layout.config_dir)
    assert plan.env_overrides[ENV_BASIC_MEMORY_AUTO_UPDATE] == BASIC_MEMORY_AUTO_UPDATE_VALUE
    assert plan.env_overrides[ENV_BASIC_MEMORY_NO_PROMOS] == BASIC_MEMORY_NO_PROMOS_VALUE


def test_deterministic_config_json_ending_newline():
    """Assert minimal bootstrap config JSON format and newline termination."""
    cfg_model = build_basic_memory_bootstrap_config()
    json_text = cfg_model.to_json()

    assert json_text.endswith("\n")
    parsed = json.loads(json_text)
    assert parsed == {
        "auto_update": False,
        "default_project": None,
        "projects": {},
    }
    # Determinism across multiple calls
    assert json_text == build_basic_memory_bootstrap_config().to_json()


def test_default_null_no_main_no_external_path():
    """Assert bootstrap config has null default project, no 'main', and zero note/external paths."""
    cfg_model = BasicMemoryBootstrapConfig()
    data = cfg_model.to_dict()

    assert data["default_project"] is None
    assert data["auto_update"] is False
    assert data["projects"] == {}
    assert "main" not in data
    assert "main" not in data["projects"]

    json_str = cfg_model.to_json()
    assert "main" not in json_str
    assert "notes" not in json_str
    assert "memory" not in json_str


def test_bootstrap_config_safety_model_rejection():
    """Assert safety model rejects auto_update=True, non-null default_project, or non-empty projects."""
    with pytest.raises(ValidationError) as exc1:
        BasicMemoryBootstrapConfig(auto_update=True)
    assert "auto_update=False" in str(exc1.value)

    with pytest.raises(ValidationError) as exc2:
        BasicMemoryBootstrapConfig(default_project="main")
    assert "default_project=None" in str(exc2.value)

    with pytest.raises(ValidationError) as exc3:
        BasicMemoryBootstrapConfig(projects={"main": {}})
    assert "empty projects mapping" in str(exc3.value)


def test_immutable_mappings_and_mutation_rejection(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert MappingProxyType immutability and that modifying source dicts cannot alter objects."""
    # 1. Bootstrap config projects
    source_projects = {}
    cfg_model = BasicMemoryBootstrapConfig(projects=source_projects)
    assert isinstance(cfg_model.projects, MappingProxyType)
    with pytest.raises(TypeError):
        cfg_model.projects["evil"] = "val"  # type: ignore[index]

    # Mutating source after construction must not affect config
    source_projects["injected"] = "val"
    assert "injected" not in cfg_model.projects
    assert cfg_model.to_dict()["projects"] == {}

    # 2. Plan and Preview env_overrides
    mock_uv = create_mock_uv(tmp_path)
    plan = build_basic_memory_install_plan(personal_tideway_config, uv_executable=mock_uv)
    assert isinstance(plan.env_overrides, MappingProxyType)
    with pytest.raises(TypeError):
        plan.env_overrides["NEW_KEY"] = "val"  # type: ignore[index]

    preview = plan.preview()
    assert isinstance(preview.env_overrides, MappingProxyType)
    with pytest.raises(TypeError):
        preview.env_overrides["NEW_KEY"] = "val"  # type: ignore[index]

    # to_dict returns fresh mutable dictionary without mutating original
    plan_dict = plan.to_dict()
    assert type(plan_dict["env_overrides"]) is dict
    plan_dict["env_overrides"]["MUTATED"] = "true"
    assert "MUTATED" not in plan.env_overrides


def test_uninitialized_refusal_and_no_mutation(tmp_path: Path):
    """Assert uninitialized workspace refuses install plan and causes no mutation."""
    uninit_home = tmp_path / "uninit_workspace"
    uninit_cfg = PersonalTidewayConfig.resolve(home=uninit_home)
    mock_uv = create_mock_uv(tmp_path)

    with pytest.raises(ConfigError) as exc_info:
        build_basic_memory_install_plan(uninit_cfg, uv_executable=mock_uv)

    assert "not initialized" in str(exc_info.value).lower()
    assert not uninit_home.exists()

    # Pure layout inspection succeeds even before initialization
    layout = get_basic_memory_layout(uninit_cfg)
    assert layout.service_root == uninit_home / "services" / "basic-memory"
    assert not uninit_home.exists()


def test_uv_executable_validation_rejections(tmp_path: Path):
    """Assert strict uv_executable rejections: relative, missing, permission, symlink."""
    # 1. Missing / None
    with pytest.raises(ValidationError):
        validate_uv_executable(None)
    with pytest.raises(ValidationError):
        validate_uv_executable("")

    # 2. Relative path
    with pytest.raises(ValidationError):
        validate_uv_executable("uv")
    with pytest.raises(ValidationError):
        validate_uv_executable(Path("bin/uv"))

    # 3. Nonexistent absolute path
    with pytest.raises(ValidationError):
        validate_uv_executable(tmp_path / "nonexistent_uv")

    # 4. Directory instead of regular file
    a_dir = tmp_path / "uv_as_dir"
    a_dir.mkdir()
    with pytest.raises(ValidationError):
        validate_uv_executable(a_dir)

    # 5. Non-executable file
    non_exec = tmp_path / "uv_non_exec"
    non_exec.write_text("dummy", encoding="utf-8")
    non_exec.chmod(0o644)
    with pytest.raises(ValidationError):
        validate_uv_executable(non_exec)

    # 6. Symlink acceptance: resolves to canonical real executable
    real_uv = create_mock_uv(tmp_path)
    symlink_uv = tmp_path / "uv_symlink"
    symlink_uv.symlink_to(real_uv)
    assert validate_uv_executable(symlink_uv) == real_uv.resolve()

    # 7. Broken symlink rejection
    broken_uv = tmp_path / "uv_broken_symlink"
    broken_uv.symlink_to(tmp_path / "nonexistent_target")
    with pytest.raises(ValidationError) as exc_broken:
        validate_uv_executable(broken_uv)
    assert "does not exist" in str(exc_broken.value).lower()

    # 8. Non-regular symlink target (symlink to directory) rejection
    symlink_dir = tmp_path / "uv_symlink_dir"
    symlink_dir.symlink_to(a_dir)
    with pytest.raises(ValidationError) as exc_dir_sym:
        validate_uv_executable(symlink_dir)
    assert "must be a regular file" in str(exc_dir_sym.value).lower()


def test_fixed_safe_errors_do_not_leak_secrets(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert that error messages for invalid paths, resolvers, and symlinks do not echo secret sentinels."""
    secret_sentinel = "SECRET_TOKEN_XYZ_12345"

    # 1. validate_uv_executable does not leak path with sentinel
    secret_path = tmp_path / f"uv_with_{secret_sentinel}"
    with pytest.raises(ValidationError) as exc_path:
        validate_uv_executable(secret_path)
    assert secret_sentinel not in str(exc_path.value)

    # 2. uv_resolver exception text and chaining do not leak sentinel
    def faulty_resolver():
        raise RuntimeError(f"Crashing with sensitive token: {secret_sentinel}")

    with pytest.raises(ValidationError) as exc_res:
        build_basic_memory_install_plan(personal_tideway_config, uv_resolver=faulty_resolver)
    assert secret_sentinel not in str(exc_res.value)
    # Check that exception cause/__cause__ is suppressed
    assert exc_res.value.__cause__ is None

    # 3. Layout symlink error does not leak path with sentinel
    secret_target = tmp_path / f"target_{secret_sentinel}"
    secret_target.mkdir()
    bm_dir = personal_tideway_config.basic_memory_dir
    if bm_dir.exists():
        bm_dir.rmdir()
    bm_dir.symlink_to(secret_target)

    with pytest.raises(BoundaryError) as exc_symlink:
        get_basic_memory_layout(personal_tideway_config)
    assert secret_sentinel not in str(exc_symlink.value)


def test_cfg_home_symlink_escape_rejection(personal_tideway_config: PersonalTidewayConfig, tmp_path: Path):
    """Assert rejection when layout paths involve symlink escapes or intermediate symlinks."""
    outside_dir = tmp_path / "outside_world"
    outside_dir.mkdir()

    # Replace basic_memory_dir with a symlink to outside
    bm_dir = personal_tideway_config.basic_memory_dir
    if bm_dir.exists():
        bm_dir.rmdir()
    bm_dir.symlink_to(outside_dir)

    with pytest.raises(BoundaryError):
        get_basic_memory_layout(personal_tideway_config)


def test_repeated_planning_deterministic(personal_tideway_config: PersonalTidewayConfig, tmp_path: Path):
    """Assert that repeating build_basic_memory_install_plan yields deterministic results."""
    mock_uv = create_mock_uv(tmp_path)
    plan1 = build_basic_memory_install_plan(personal_tideway_config, uv_executable=mock_uv)
    plan2 = build_basic_memory_install_plan(personal_tideway_config, uv_executable=mock_uv)

    assert plan1.to_dict() == plan2.to_dict()
    assert plan1.preview().to_dict() == plan2.preview().to_dict()
    assert plan1.install_argv == plan2.install_argv
    assert plan1.health_argv == plan2.health_argv
    assert plan1.env_overrides == plan2.env_overrides


def test_to_dict_contains_no_unrelated_host_env_or_secret_sentinel(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Assert to_dict and preview contain only isolated overrides and no host env/secrets."""
    sentinel_secret = "super_secret_token_value_987654"
    monkeypatch.setenv("PTW_TEST_SECRET", sentinel_secret)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-sentinel-xyz")

    mock_uv = create_mock_uv(tmp_path)
    plan = build_basic_memory_install_plan(personal_tideway_config, uv_executable=mock_uv)

    plan_data = plan.to_dict()
    preview_data = plan.preview().to_dict()
    combined_json = json.dumps(plan_data) + json.dumps(preview_data)

    assert sentinel_secret not in combined_json
    assert "sk-live-sentinel-xyz" not in combined_json
    assert "PATH" not in plan_data["env_overrides"]
    assert "HOME" not in plan_data["env_overrides"]
    assert "USER" not in plan_data["env_overrides"]


def test_full_filesystem_snapshot_unchanged_by_planning(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert zero filesystem mutation during install plan construction and preview generation."""
    mock_uv = create_mock_uv(tmp_path)

    def snapshot(root: Path) -> dict[str, tuple[int, int]]:
        if not root.exists():
            return {}
        entries = {}
        for p in root.rglob("*"):
            st = p.stat()
            entries[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
        return entries

    before_snapshot = snapshot(personal_tideway_config.home)

    plan = build_basic_memory_install_plan(personal_tideway_config, uv_executable=mock_uv)
    _ = plan.to_dict()
    preview = plan.preview()
    _ = preview.to_dict()

    after_snapshot = snapshot(personal_tideway_config.home)
    assert before_snapshot == after_snapshot

    # Explicitly ensure planned directories and config were NOT created
    assert not plan.layout.uv_tool_dir.exists()
    assert not plan.layout.bin_dir.exists()
    assert not plan.layout.config_dir.exists()
    assert not plan.layout.config_file.exists()


def test_config_error_messages_do_not_leak_cfg_home_or_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Assert both ConfigError branches in build_basic_memory_install_plan use fixed text without raw cfg.home."""
    canary = "CANARY_TOKEN_SECRET_9999"
    mock_uv = create_mock_uv(tmp_path)

    # 1. Uninitialized workspace branch
    uninit_home = tmp_path / f"uninit_{canary}"
    uninit_cfg = PersonalTidewayConfig.resolve(home=uninit_home)
    with pytest.raises(ConfigError) as exc_uninit:
        build_basic_memory_install_plan(uninit_cfg, uv_executable=mock_uv)
    assert str(exc_uninit.value) == "Personal Tideway workspace is not initialized. Run 'ptw init' before creating an install plan."
    assert canary not in str(exc_uninit.value)
    assert str(uninit_home) not in str(exc_uninit.value)

    # 2. Workspace directory does not exist or is not a directory branch
    file_home = tmp_path / f"file_{canary}"
    file_home.write_text("not_a_directory", encoding="utf-8")
    cfg_file = PersonalTidewayConfig.resolve(home=file_home)
    # Simulate is_initialized returning True while home is not a dir
    monkeypatch.setattr(cfg_file, "is_initialized", lambda: True)

    with pytest.raises(ConfigError) as exc_dir:
        build_basic_memory_install_plan(cfg_file, uv_executable=mock_uv)
    assert str(exc_dir.value) == "Personal Tideway workspace directory does not exist or is not a directory."
    assert canary not in str(exc_dir.value)
    assert str(file_home) not in str(exc_dir.value)


def test_cfg_home_itself_symlink_rejection(personal_tideway_config: PersonalTidewayConfig, tmp_path: Path):
    """Assert cfg.home itself is rejected before child validation when it is a symlink, with fixed BoundaryError."""
    canary = "ROOT_SYMLINK_CANARY_8888"
    symlink_home = tmp_path / f"symlink_home_{canary}"
    symlink_home.symlink_to(personal_tideway_config.home)

    symlink_cfg = replace(personal_tideway_config, home=symlink_home)
    mock_uv = create_mock_uv(tmp_path)

    # Rejection in get_basic_memory_layout
    with pytest.raises(BoundaryError) as exc_layout:
        get_basic_memory_layout(symlink_cfg)
    assert str(exc_layout.value) == "Workspace root is a symlink, which is not permitted."
    assert canary not in str(exc_layout.value)
    assert str(symlink_home) not in str(exc_layout.value)
    assert str(personal_tideway_config.home) not in str(exc_layout.value)

    # Rejection in build_basic_memory_install_plan
    with pytest.raises(BoundaryError) as exc_plan:
        build_basic_memory_install_plan(symlink_cfg, uv_executable=mock_uv)
    assert str(exc_plan.value) == "Workspace root is a symlink, which is not permitted."
    assert canary not in str(exc_plan.value)
    assert str(symlink_home) not in str(exc_plan.value)


def test_sequence_snapshots_immutable_normalized_to_tuple_and_source_mutation(tmp_path: Path):
    """Assert sequence snapshots normalize to tuples in __post_init__ and resist source list mutation."""
    # 1. BasicMemoryLayout.executable_candidates
    p1 = tmp_path / "bin" / "basic-memory"
    p2 = tmp_path / "bin" / "bm"
    candidates_list = [p1, p2]
    layout = BasicMemoryLayout(
        service_root=tmp_path / "srv",
        uv_tool_dir=tmp_path / "tool",
        bin_dir=tmp_path / "bin",
        config_dir=tmp_path / "cfg",
        config_file=tmp_path / "cfg" / "config.json",
        primary_executable=p1,
        alias_executable=p2,
        executable_candidates=candidates_list,  # type: ignore[arg-type]
    )
    assert isinstance(layout.executable_candidates, tuple)
    assert layout.executable_candidates == (p1, p2)

    # Mutate source list after construction
    injected_path = tmp_path / "bin" / "injected"
    candidates_list.append(injected_path)
    candidates_list[0] = injected_path
    assert layout.executable_candidates == (p1, p2)
    assert injected_path not in layout.executable_candidates

    with pytest.raises(TypeError):
        layout.executable_candidates[0] = injected_path  # type: ignore[index]

    # 2. BasicMemoryInstallPlanPreview sequences
    dirs_list = [tmp_path / "d1", tmp_path / "d2"]
    install_argv_list = ["uv", "tool", "install"]
    health_argv_list = ["bm", "--version"]
    env_dict = {"ENV_K": "ENV_V"}

    preview = BasicMemoryInstallPlanPreview(
        intended_directories=dirs_list,  # type: ignore[arg-type]
        config_path=tmp_path / "cfg.json",
        config_content="{}",
        install_argv=install_argv_list,  # type: ignore[arg-type]
        health_argv=health_argv_list,  # type: ignore[arg-type]
        env_overrides=env_dict,
    )
    assert isinstance(preview.intended_directories, tuple)
    assert isinstance(preview.install_argv, tuple)
    assert isinstance(preview.health_argv, tuple)
    assert isinstance(preview.env_overrides, MappingProxyType)

    # Mutate source lists and dict after construction
    dirs_list.append(tmp_path / "d3")
    dirs_list[0] = tmp_path / "d_mutated"
    install_argv_list.append("--extra")
    install_argv_list[0] = "mutated"
    health_argv_list.append("--extra")
    health_argv_list[0] = "mutated"
    env_dict["NEW_KEY"] = "val"

    assert preview.intended_directories == (tmp_path / "d1", tmp_path / "d2")
    assert preview.install_argv == ("uv", "tool", "install")
    assert preview.health_argv == ("bm", "--version")
    assert "NEW_KEY" not in preview.env_overrides

    with pytest.raises(TypeError):
        preview.intended_directories[0] = tmp_path / "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        preview.install_argv[0] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        preview.health_argv[0] = "x"  # type: ignore[index]

    # 3. BasicMemoryInstallPlan sequences
    plan_install_argv = ["uv", "tool", "install", "--force", "--prerelease=allow", "basic-memory==0.23.2"]
    plan_health_argv = ["bm", "--version"]
    plan_env = {"PTW_KEY": "1"}

    plan = BasicMemoryInstallPlan(
        layout=layout,
        uv_executable=p1,
        install_argv=plan_install_argv,  # type: ignore[arg-type]
        health_argv=plan_health_argv,  # type: ignore[arg-type]
        env_overrides=plan_env,
        bootstrap_config=build_basic_memory_bootstrap_config(),
    )
    assert isinstance(plan.install_argv, tuple)
    assert isinstance(plan.health_argv, tuple)
    assert isinstance(plan.env_overrides, MappingProxyType)

    # Mutate source lists and dict after construction
    plan_install_argv.append("--extra")
    plan_install_argv[0] = "mutated"
    plan_health_argv.append("--force")
    plan_health_argv[0] = "mutated"
    plan_env["PTW_KEY"] = "2"
    plan_env["INJECTED2"] = "val2"

    assert plan.install_argv == ("uv", "tool", "install", "--force", "--prerelease=allow", "basic-memory==0.23.2")
    assert plan.health_argv == ("bm", "--version")
    assert plan.env_overrides["PTW_KEY"] == "1"
    assert "INJECTED2" not in plan.env_overrides

    with pytest.raises(TypeError):
        plan.install_argv[0] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        plan.health_argv[0] = "x"  # type: ignore[index]


def test_validate_uv_executable_symlink_parent_acceptance(tmp_path: Path):
    """Assert validate_uv_executable accepts symlinks in parent path components and returns canonical resolved executable."""
    canary = "CANARY_UV_PARENT_SECRET"
    real_parent = tmp_path / f"real_uv_{canary}"
    real_parent.mkdir(parents=True)
    real_uv = real_parent / "uv"
    real_uv.write_text("#!/bin/sh\necho 1\n", encoding="utf-8")
    real_uv.chmod(0o755)

    symlink_parent = tmp_path / "symlink_uv_dir"
    symlink_parent.symlink_to(real_parent)

    # Direct parent is a symlink: accepted and canonical path returned
    resolved_direct = validate_uv_executable(symlink_parent / "uv")
    assert resolved_direct == real_uv.resolve()

    # Grandparent is a symlink: accepted and canonical path returned
    nested_dir = real_parent / "subdir"
    nested_dir.mkdir()
    nested_uv = nested_dir / "uv"
    nested_uv.write_text("#!/bin/sh\necho 1\n", encoding="utf-8")
    nested_uv.chmod(0o755)

    resolved_nested = validate_uv_executable(symlink_parent / "subdir" / "uv")
    assert resolved_nested == nested_uv.resolve()


def test_preview_execution_directory_parity(personal_tideway_config: PersonalTidewayConfig, tmp_path: Path):
    """Assert preview.intended_directories matches every directory created by real execution."""
    mock_uv = create_mock_uv(tmp_path)
    plan = build_basic_memory_install_plan(personal_tideway_config, uv_executable=mock_uv)
    preview = plan.preview()

    expected_dirs = (
        personal_tideway_config.services_dir,
        plan.layout.service_root,
        plan.layout.uv_tool_dir,
        plan.layout.bin_dir,
        plan.layout.config_dir,
        plan.layout.cache_dir,
    )
    assert preview.intended_directories == expected_dirs


def test_validate_uv_executable_unsupported_input_types():
    """Assert validate_uv_executable raises fixed ValidationError instead of TypeError for unsupported types."""
    bad_inputs = [
        123,
        45.6,
        True,
        False,
        ["/usr/bin/uv"],
        {"path": "/usr/bin/uv"},
        object(),
        b"/usr/bin/uv",
    ]
    for bad in bad_inputs:
        with pytest.raises(ValidationError) as exc:
            validate_uv_executable(bad)  # type: ignore[arg-type]
        assert not isinstance(exc.value, TypeError)
        assert str(exc.value) == "uv_executable must be a string or Path"


def test_get_basic_memory_layout_launcher_resolution_variants(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Assert launcher resolution handles missing launcher, allows terminal internal launcher, and rejects loops/failures with BoundaryError."""
    # 1. Missing launcher: handled normally
    layout = get_basic_memory_layout(personal_tideway_config)
    assert not layout.primary_executable.exists()

    # 2. Terminal internal launcher: points to valid target under service_root that does not exist yet
    layout.bin_dir.mkdir(parents=True, exist_ok=True)
    future_target = layout.uv_tool_dir / "bin" / "basic-memory"
    layout.primary_executable.symlink_to(future_target)

    layout_with_symlink = get_basic_memory_layout(personal_tideway_config)
    assert layout_with_symlink.primary_executable == layout.primary_executable

    # 3. Symlink loop: rejected with fixed BoundaryError
    layout.primary_executable.unlink()
    loop_target = layout.bin_dir / "loop_link"
    if loop_target.is_symlink() or loop_target.exists():
        loop_target.unlink()
    loop_target.symlink_to(layout.primary_executable)
    layout.primary_executable.symlink_to(loop_target)

    with pytest.raises(BoundaryError) as exc_loop:
        get_basic_memory_layout(personal_tideway_config)
    assert str(exc_loop.value) == "Layout target component is a symlink, which is not permitted in child layout."

    # 4. Self loop: layout.alias_executable -> layout.alias_executable
    layout.primary_executable.unlink()
    loop_target.unlink()
    layout.alias_executable.symlink_to(layout.alias_executable)
    with pytest.raises(BoundaryError) as exc_self_loop:
        get_basic_memory_layout(personal_tideway_config)
    assert str(exc_self_loop.value) == "Layout target component is a symlink, which is not permitted in child layout."
    layout.alias_executable.unlink()


def test_core_package_exports_and_all_consistency():
    """Assert build_basic_memory_bootstrap_config and all environment constants are exported and __all__ is consistent."""
    from personal_tideway import core

    # 1. build_basic_memory_bootstrap_config is importable and functional
    assert hasattr(core, "build_basic_memory_bootstrap_config")
    bootstrap = core.build_basic_memory_bootstrap_config()
    assert isinstance(bootstrap, BasicMemoryBootstrapConfig)
    assert bootstrap.auto_update is False

    # 2. Public environment constants
    expected_constants = [
        "BASIC_MEMORY_AUTO_UPDATE_VALUE",
        "BASIC_MEMORY_NO_PROMOS_VALUE",
        "BASIC_MEMORY_PINNED_VERSION",
        "BASIC_MEMORY_REQUIREMENT",
        "ENV_BASIC_MEMORY_AUTO_UPDATE",
        "ENV_BASIC_MEMORY_CONFIG_DIR",
        "ENV_BASIC_MEMORY_NO_PROMOS",
        "ENV_UV_CACHE_DIR",
        "ENV_UV_TOOL_BIN_DIR",
        "ENV_UV_TOOL_DIR",
    ]
    for const_name in expected_constants:
        assert hasattr(core, const_name)
        assert getattr(core, const_name) is not None
        assert const_name in core.__all__

    # 3. __all__ consistency: every symbol in __all__ exists in core
    assert hasattr(core, "__all__")
    for name in core.__all__:
        assert hasattr(core, name), f"Symbol {name} in __all__ not found in core"

    # 4. __all__ is sorted
    assert core.__all__ == sorted(core.__all__)
