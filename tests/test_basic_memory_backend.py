"""Tests for safe Basic Memory 0.23.2 project-database initialization and parity verification.

Work Package 10b verification suite with isolated test doubles:
1. Pure planner determinism, registry ordering, and exact argv construction.
2. Planner and dry-run zero filesystem mutation and zero runner invocations.
3. Empty registry explicit no-op plan and zero runner invocations.
4. All project kinds (git, directory, external) handled uniformly by memory.project_name.
5. Strict child environment isolation excluding host tokens and secrets.
6. Fail-closed isolated executable validation.
7. Runner sequence stops on first failure with no subsequent commands called.
8. Status JSON parsing: schema validation, bool/negative rejection, non-truncating >64 KiB support, and safe count preservation.
9. Fixed secret-safe exceptions suppressing cause chaining across timeout/nonzero/runtime errors.
10. Deep immutability of plans, previews, statuses, and results against caller mutation.
11. High-level orchestration contract, single-snapshot coherent registry loading, and concurrency boundary honesty.
12. Strict avoidance of sync-config and private SQLite schema access.
13. Tampered plan rejection: forged argv, extra secret env keys, cardinality mismatch, invalid timeouts.
14. Hardened runner normalization: strict int returncode, bytes/None stream decoding, exact 3-tuple length, fixed safe errors.
15. BoundaryError preservation in plan validation on symlink boundary violations.
16. Public to_dict serialization methods and canonical aliases coverage.
17. N=1 single project command boundary validation.
"""

import json
import os
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any

import pytest

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.core.basic_memory_backend import (
    DEFAULT_REINDEX_TIMEOUT,
    DEFAULT_STATUS_TIMEOUT,
    MAX_STATUS_OUTPUT_BYTES,
    BasicMemoryBackendResult,
    BasicMemoryOrchestrationResult,
    BasicMemoryProjectStatus,
    execute_backend_plan,
    execute_basic_memory_backend_plan,
    parse_basic_memory_status,
    plan_backend,
    plan_basic_memory_backend,
    sync_and_initialize_basic_memory_backend,
    validate_basic_memory_backend_plan,
)
from personal_tideway.core.basic_memory_installer import (
    BasicMemoryRunnerResult,
)
from personal_tideway.core.basic_memory_runtime import (
    BASIC_MEMORY_AUTO_UPDATE_VALUE,
    BASIC_MEMORY_NO_PROMOS_VALUE,
    ENV_BASIC_MEMORY_AUTO_UPDATE,
    ENV_BASIC_MEMORY_CONFIG_DIR,
    ENV_BASIC_MEMORY_NO_PROMOS,
    ENV_UV_CACHE_DIR,
    ENV_UV_TOOL_BIN_DIR,
    ENV_UV_TOOL_DIR,
    get_basic_memory_layout,
)
from personal_tideway.core.registry import (
    ProjectRecord,
    ProjectRegistry,
    load_registry,
    save_registry,
)
from personal_tideway.exceptions import (
    BoundaryError,
    RuntimeProbeError,
    ValidationError,
)


def snapshot_filesystem(root: Path) -> dict[str, tuple[int, int, int]]:
    """Capture snapshot of directory entries: (size, mtime_ns, mode)."""
    if not root.exists():
        return {}
    entries: dict[str, tuple[int, int, int]] = {}
    for p in sorted(root.rglob("*")):
        try:
            st = p.lstat()
            entries[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, st.st_mode)
        except OSError:
            pass
    return entries


def create_fake_executable(target: Path) -> Path:
    """Create a minimal executable file with mode 0755."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    return target


def create_sample_registry(tmp_path: Path) -> tuple[ProjectRegistry, list[str]]:
    """Create sample registry with git, directory, and external projects."""
    git_dir = tmp_path / "git_repo"
    git_dir.mkdir(parents=True, exist_ok=True)
    dir_dir = tmp_path / "local_docs"
    dir_dir.mkdir(parents=True, exist_ok=True)

    p_git = ProjectRecord.create(
        slug="alpha-repo",
        display_name="Alpha Repo",
        kind="git",
        paths=[str(git_dir)],
    )
    p_dir = ProjectRecord.create(
        slug="beta-docs",
        display_name="Beta Docs",
        kind="directory",
        paths=[str(dir_dir)],
    )
    p_ext = ProjectRecord.create(
        slug="gamma-ext",
        display_name="Gamma External",
        kind="external",
    )
    reg = ProjectRegistry(projects=[p_git, p_dir, p_ext])
    names = [p_git.memory.project_name, p_dir.memory.project_name, p_ext.memory.project_name]
    return reg, names


def test_pure_planner_exact_argv_and_registry_order(personal_tideway_config: PersonalTidewayConfig, tmp_path: Path):
    """Invariant 1: Pure planner constructs exact argv sequence following registry order without disk mutation."""
    reg, names = create_sample_registry(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)

    before = snapshot_filesystem(personal_tideway_config.home)
    plan = plan_basic_memory_backend(personal_tideway_config, reg)
    after = snapshot_filesystem(personal_tideway_config.home)

    assert before == after, "Pure planner must not alter filesystem"
    assert not plan.is_noop
    assert plan.project_names == tuple(names)
    assert plan.executable == layout.primary_executable

    exec_str = str(layout.primary_executable)
    # First reindex acts as initial config reconciliation into DB
    assert plan.initialization_argv == (exec_str, "reindex", "--search", "--project", names[0])

    expected_reindex = (
        (exec_str, "reindex", "--search", "--project", names[0]),
        (exec_str, "reindex", "--search", "--project", names[1]),
        (exec_str, "reindex", "--search", "--project", names[2]),
    )
    assert plan.reindex_argvs == expected_reindex

    expected_status = (
        (exec_str, "status", "--project", names[0], "--local", "--json"),
        (exec_str, "status", "--project", names[1], "--local", "--json"),
        (exec_str, "status", "--project", names[2], "--local", "--json"),
    )
    assert plan.status_argvs == expected_status

    # Ensure no shell strings or sync-config
    for argv in plan.reindex_argvs + plan.status_argvs:
        assert isinstance(argv, tuple)
        assert "sync-config" not in argv
        assert "sh" not in argv and "-c" not in argv


def test_planner_and_executor_empty_registry_safe_noop(personal_tideway_config: PersonalTidewayConfig):
    """Invariant 2: Empty registry creates safe no-op plan and executes with zero runner invocations."""
    reg = ProjectRegistry.empty()
    plan = plan_basic_memory_backend(personal_tideway_config, reg)

    assert plan.is_noop
    assert plan.project_names == ()
    assert plan.reindex_argvs == ()
    assert plan.status_argvs == ()
    assert plan.initialization_argv is None
    assert plan.remaining_reindex_argvs == ()

    # Execute empty plan: runner must never be called even if binary is missing
    runner_calls: list[tuple[str, ...]] = []

    def mock_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        runner_calls.append(argv)
        return BasicMemoryRunnerResult(returncode=0, stdout="", stderr="")

    result = execute_basic_memory_backend_plan(
        plan,
        dry_run=False,
        runner=mock_runner,
        cfg=personal_tideway_config,
    )
    assert result.skipped is True
    assert result.dry_run is False
    assert result.reindexed_projects == ()
    assert result.statuses == ()
    assert len(runner_calls) == 0, "Empty registry must not invoke runner"


def test_dry_run_zero_mutation_and_zero_runner_calls(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Invariant 3: Dry run executes zero runner calls, mutates zero files, and does not claim projects reindexed."""
    reg, names = create_sample_registry(tmp_path)
    plan = plan_basic_memory_backend(personal_tideway_config, reg)

    runner_calls: list[tuple[str, ...]] = []

    def mock_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        runner_calls.append(argv)
        return BasicMemoryRunnerResult(returncode=0, stdout="", stderr="")

    before = snapshot_filesystem(personal_tideway_config.home)
    result = execute_basic_memory_backend_plan(
        plan,
        dry_run=True,
        runner=mock_runner,
        cfg=personal_tideway_config,
    )
    after = snapshot_filesystem(personal_tideway_config.home)

    assert before == after, "Dry run must perform zero filesystem mutations"
    assert len(runner_calls) == 0, "Dry run must invoke runner zero times"
    assert result.dry_run is True
    # Fix 6: Dry run must NOT claim projects were actually reindexed
    assert result.reindexed_projects == ()
    assert result.statuses == ()
    assert result.preview is not None
    assert result.preview.is_noop is False
    assert result.preview.project_names == tuple(names)


def test_successful_execution_order_and_parity_capture(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Invariant 4: Sequential execution runs all reindexes then all statuses, capturing bounded counts."""
    reg, names = create_sample_registry(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    plan = plan_basic_memory_backend(personal_tideway_config, reg)

    invoked_argvs: list[tuple[str, ...]] = []
    invoked_timeouts: list[float] = []

    def mock_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        invoked_argvs.append(argv)
        invoked_timeouts.append(timeout)
        if "reindex" in argv:
            return BasicMemoryRunnerResult(returncode=0, stdout="Indexed successfully", stderr="")
        if "status" in argv:
            payload = {
                "total_files": 42,
                "observed_files": ["secret_note1.md", "secret_note2.md"],
            }
            return BasicMemoryRunnerResult(returncode=0, stdout=json.dumps(payload), stderr="")
        return BasicMemoryRunnerResult(returncode=1, stdout="", stderr="Unknown command")

    result = execute_basic_memory_backend_plan(
        plan,
        dry_run=False,
        runner=mock_runner,
        cfg=personal_tideway_config,
    )

    assert result.dry_run is False
    assert result.skipped is False
    assert result.reindexed_projects == tuple(names)
    assert len(result.statuses) == 3

    for st, name in zip(result.statuses, names):
        assert st.project_name == name
        assert st.total_files == 42
        assert st.observed_files_count == 2
        st_dict = st.to_dict()
        assert "secret_note1.md" not in json.dumps(st_dict)

    # Order check: all reindex commands first, then all status commands
    assert len(invoked_argvs) == 6
    assert [argv[1] for argv in invoked_argvs[:3]] == ["reindex", "reindex", "reindex"]
    assert [argv[1] for argv in invoked_argvs[3:]] == ["status", "status", "status"]
    assert invoked_timeouts[:3] == [DEFAULT_REINDEX_TIMEOUT] * 3
    assert invoked_timeouts[3:] == [DEFAULT_STATUS_TIMEOUT] * 3


def test_child_environment_isolation(personal_tideway_config: PersonalTidewayConfig, tmp_path: Path):
    """Invariant 5: Child environment strictly excludes sensitive host variables and includes overrides."""
    reg, names = create_sample_registry(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    plan = plan_basic_memory_backend(personal_tideway_config, reg)

    captured_env: dict[str, str] = {}

    def mock_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        if not captured_env:
            captured_env.update(env)
        return BasicMemoryRunnerResult(
            returncode=0,
            stdout=json.dumps({"total_files": 0, "observed_files": []}),
            stderr="",
        )

    os.environ["SUPER_SECRET_TOKEN_XYZ"] = "SECRET_123"
    os.environ["OPENAI_API_KEY"] = "sk-leaked"
    os.environ["VIRTUAL_ENV"] = "/home/user/.venv"

    try:
        execute_basic_memory_backend_plan(
            plan,
            dry_run=False,
            runner=mock_runner,
            cfg=personal_tideway_config,
        )
    finally:
        os.environ.pop("SUPER_SECRET_TOKEN_XYZ", None)
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("VIRTUAL_ENV", None)

    assert "SUPER_SECRET_TOKEN_XYZ" not in captured_env
    assert "OPENAI_API_KEY" not in captured_env
    assert "VIRTUAL_ENV" not in captured_env

    assert captured_env[ENV_UV_TOOL_DIR] == str(layout.uv_tool_dir)
    assert captured_env[ENV_UV_TOOL_BIN_DIR] == str(layout.bin_dir)
    assert captured_env[ENV_UV_CACHE_DIR] == str(layout.cache_dir)
    assert captured_env[ENV_BASIC_MEMORY_CONFIG_DIR] == str(layout.config_dir)
    assert captured_env[ENV_BASIC_MEMORY_AUTO_UPDATE] == BASIC_MEMORY_AUTO_UPDATE_VALUE
    assert captured_env[ENV_BASIC_MEMORY_NO_PROMOS] == BASIC_MEMORY_NO_PROMOS_VALUE


def test_fail_closed_on_missing_or_invalid_executable(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Invariant 6: Fails closed with RuntimeProbeError when executable is missing, non-regular, or not executable."""
    reg, names = create_sample_registry(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)

    plan = plan_basic_memory_backend(personal_tideway_config, reg)

    # Case A: Executable does not exist
    if layout.primary_executable.exists():
        layout.primary_executable.unlink()

    with pytest.raises(RuntimeProbeError) as exc_a:
        execute_basic_memory_backend_plan(plan, dry_run=False, cfg=personal_tideway_config)
    assert exc_a.value.__cause__ is None
    assert str(exc_a.value) == "Basic Memory executable missing or invalid."

    # Case B: Executable exists but lacks execute permissions
    layout.bin_dir.mkdir(parents=True, exist_ok=True)
    layout.primary_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    layout.primary_executable.chmod(0o600)

    with pytest.raises(RuntimeProbeError) as exc_b:
        execute_basic_memory_backend_plan(plan, dry_run=False, cfg=personal_tideway_config)
    assert exc_b.value.__cause__ is None

    # Case C: Executable is a directory
    layout.primary_executable.unlink()
    layout.primary_executable.mkdir(parents=True, exist_ok=True)

    try:
        with pytest.raises(RuntimeProbeError) as exc_c:
            execute_basic_memory_backend_plan(plan, dry_run=False, cfg=personal_tideway_config)
        assert exc_c.value.__cause__ is None
    finally:
        layout.primary_executable.rmdir()


def test_runner_stops_on_first_reindex_failure(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Invariant 7: Reindex failure on first project halts execution immediately without calling later commands."""
    reg, names = create_sample_registry(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    plan = plan_basic_memory_backend(personal_tideway_config, reg)

    calls: list[tuple[str, ...]] = []
    sentinel_stderr = "CANARY_FATAL_REINDEX_ERROR_SECRET"

    def failing_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        calls.append(argv)
        return BasicMemoryRunnerResult(returncode=2, stdout="", stderr=sentinel_stderr)

    with pytest.raises(RuntimeProbeError) as exc_info:
        execute_basic_memory_backend_plan(plan, dry_run=False, runner=failing_runner, cfg=personal_tideway_config)

    assert exc_info.value.__cause__ is None
    assert sentinel_stderr not in str(exc_info.value)
    assert str(exc_info.value) == "Basic Memory reindex failed with non-zero exit code."
    assert len(calls) == 1, "Must stop immediately on first reindex failure"


def test_runner_stops_on_first_status_failure(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Invariant 8: Status failure on first project halts execution immediately without calling subsequent statuses."""
    reg, names = create_sample_registry(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    plan = plan_basic_memory_backend(personal_tideway_config, reg)

    calls: list[tuple[str, ...]] = []

    def fail_on_first_status(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        calls.append(argv)
        if "reindex" in argv:
            return BasicMemoryRunnerResult(returncode=0, stdout="", stderr="")
        if "status" in argv:
            return BasicMemoryRunnerResult(returncode=1, stdout="", stderr="status failed")
        return BasicMemoryRunnerResult(returncode=0, stdout="", stderr="")

    with pytest.raises(RuntimeProbeError) as exc_info:
        execute_basic_memory_backend_plan(plan, dry_run=False, runner=fail_on_first_status, cfg=personal_tideway_config)

    assert exc_info.value.__cause__ is None
    assert str(exc_info.value) == "Basic Memory status failed with non-zero exit code."
    assert len(calls) == 4


def test_timeout_translations_and_suppressed_chaining(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Invariant 9: Timeouts in reindex and status are mapped to fixed RuntimeProbeError with __cause__ is None."""
    reg, names = create_sample_registry(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    plan = plan_basic_memory_backend(personal_tideway_config, reg)
    canary = "SECRET_TIMEOUT_CANARY_VALUE"

    # Subcase A: Timeout on reindex
    def reindex_timeout_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        raise TimeoutError(f"Operation timed out: {canary}")

    with pytest.raises(RuntimeProbeError) as exc_reindex:
        execute_basic_memory_backend_plan(
            plan,
            dry_run=False,
            runner=reindex_timeout_runner,
            cfg=personal_tideway_config,
        )
    assert exc_reindex.value.__cause__ is None
    assert canary not in str(exc_reindex.value)
    assert str(exc_reindex.value) == "Basic Memory reindex timed out."

    # Subcase B: Timeout on status
    def status_timeout_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        if "reindex" in argv:
            return BasicMemoryRunnerResult(returncode=0, stdout="", stderr="")
        raise TimeoutError(f"Status timed out: {canary}")

    with pytest.raises(RuntimeProbeError) as exc_status:
        execute_basic_memory_backend_plan(
            plan,
            dry_run=False,
            runner=status_timeout_runner,
            cfg=personal_tideway_config,
        )
    assert exc_status.value.__cause__ is None
    assert canary not in str(exc_status.value)
    assert str(exc_status.value) == "Basic Memory status timed out."


def test_status_parsing_edge_cases_and_schema_enforcement():
    """Invariant 10: Status parsing rejects invalid JSON, non-objects, booleans, negative counts, and non-lists."""
    canary = "CANARY_SENSITIVE_DATA_BLOB"

    # Invalid JSON
    with pytest.raises(RuntimeProbeError) as exc1:
        parse_basic_memory_status(f"{{not json: {canary}}}", "my-project")
    assert exc1.value.__cause__ is None
    assert canary not in str(exc1.value)
    assert str(exc1.value) == "Basic Memory status output must be valid JSON."

    # Non-object JSON: array
    with pytest.raises(RuntimeProbeError) as exc2:
        parse_basic_memory_status("[1, 2, 3]", "my-project")
    assert exc2.value.__cause__ is None
    assert str(exc2.value) == "Basic Memory status output schema is invalid."

    # Non-object JSON: number, string, null
    for primitive in ("42", '"status ok"', "null"):
        with pytest.raises(RuntimeProbeError):
            parse_basic_memory_status(primitive, "my-project")

    # bool total_files (True / False) rejected
    with pytest.raises(RuntimeProbeError) as exc_bool_t:
        parse_basic_memory_status('{"total_files": true, "observed_files": []}', "my-project")
    assert str(exc_bool_t.value) == "Basic Memory status output schema is invalid."

    with pytest.raises(RuntimeProbeError) as exc_bool_f:
        parse_basic_memory_status('{"total_files": false, "observed_files": []}', "my-project")
    assert str(exc_bool_f.value) == "Basic Memory status output schema is invalid."

    # Negative total_files
    with pytest.raises(RuntimeProbeError) as exc_neg:
        parse_basic_memory_status('{"total_files": -1, "observed_files": []}', "my-project")
    assert str(exc_neg.value) == "Basic Memory status output schema is invalid."

    # String total_files
    with pytest.raises(RuntimeProbeError) as exc_str_cnt:
        parse_basic_memory_status('{"total_files": "5", "observed_files": []}', "my-project")
    assert str(exc_str_cnt.value) == "Basic Memory status output schema is invalid."

    # Missing total_files
    with pytest.raises(RuntimeProbeError) as exc_miss_cnt:
        parse_basic_memory_status('{"observed_files": []}', "my-project")
    assert str(exc_miss_cnt.value) == "Basic Memory status output schema is invalid."

    # Non-list observed_files (string, dict, None)
    with pytest.raises(RuntimeProbeError) as exc_obs_str:
        parse_basic_memory_status('{"total_files": 0, "observed_files": "invalid"}', "my-project")
    assert str(exc_obs_str.value) == "Basic Memory status output schema is invalid."

    with pytest.raises(RuntimeProbeError) as exc_obs_dict:
        parse_basic_memory_status('{"total_files": 0, "observed_files": {"a": 1}}', "my-project")
    assert str(exc_obs_dict.value) == "Basic Memory status output schema is invalid."

    with pytest.raises(RuntimeProbeError) as exc_obs_none:
        parse_basic_memory_status('{"total_files": 0}', "my-project")
    assert str(exc_obs_none.value) == "Basic Memory status output schema is invalid."


def test_status_parsing_large_valid_json_and_over_limit():
    """Invariant 10b: Valid status JSON >64 KiB parses completely without truncation; >4 MiB fails closed."""
    # 1. Valid large JSON > 64 KiB (approx 120 KiB)
    large_files = [f"notes/category_{i % 10}/secret_document_{i:05d}.md" for i in range(1500)]
    large_payload = json.dumps({
        "total_files": len(large_files),
        "observed_files": large_files,
    })
    assert len(large_payload.encode("utf-8")) > 64 * 1024

    st = parse_basic_memory_status(large_payload, "large-proj")
    assert st.project_name == "large-proj"
    assert st.total_files == 1500
    assert st.observed_files_count == 1500
    assert not hasattr(st, "observed_files")
    assert "secret_document" not in json.dumps(st.to_dict())

    # 2. Oversized output exceeding MAX_STATUS_OUTPUT_BYTES (4 MiB)
    canary = "CANARY_MASSIVE_LEAK_SECRET_STRING"
    oversized_padding = "a" * (MAX_STATUS_OUTPUT_BYTES + 1024)
    oversized_output = f'{{"total_files": 1, "observed_files": ["{canary}"], "pad": "{oversized_padding}"}}'

    with pytest.raises(RuntimeProbeError) as exc_over:
        parse_basic_memory_status(oversized_output, "large-proj")
    assert exc_over.value.__cause__ is None
    assert str(exc_over.value) == "Basic Memory status output exceeded maximum allowable size."
    assert canary not in str(exc_over.value)


def test_deep_immutability_of_plan_and_results(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Invariant 11: Plan and result dataclasses are frozen and protect collections against caller mutation."""
    reg, names = create_sample_registry(tmp_path)
    plan = plan_basic_memory_backend(personal_tideway_config, reg)

    with pytest.raises(FrozenInstanceError):
        plan.executable = Path("/new/path")  # type: ignore

    with pytest.raises(FrozenInstanceError):
        plan.project_names = ()  # type: ignore

    with pytest.raises(TypeError):
        plan.env_overrides["NEW_KEY"] = "val"  # type: ignore

    res = BasicMemoryBackendResult(
        dry_run=False,
        skipped=False,
        reindexed_projects=tuple(names),
        statuses=(BasicMemoryProjectStatus(project_name="alpha", total_files=0, observed_files_count=0),),
    )

    with pytest.raises(FrozenInstanceError):
        res.dry_run = True  # type: ignore

    with pytest.raises(FrozenInstanceError):
        res.reindexed_projects = ()  # type: ignore


def test_orchestration_function_dry_run_and_apply(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Invariant 12: High-level orchestration function executes reconciliation then backend verification."""
    reg, names = create_sample_registry(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    # 1. Dry run orchestration: zero mutations
    before = snapshot_filesystem(personal_tideway_config.home)
    orch_dry = sync_and_initialize_basic_memory_backend(
        personal_tideway_config,
        registry=reg,
        dry_run=True,
    )
    after = snapshot_filesystem(personal_tideway_config.home)

    assert before == after
    assert orch_dry.dry_run is True
    assert orch_dry.reconciliation.dry_run is True
    assert orch_dry.backend.dry_run is True
    assert orch_dry.backend.reindexed_projects == ()

    # 2. Apply run orchestration
    def mock_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        if "reindex" in argv:
            return BasicMemoryRunnerResult(returncode=0, stdout="Indexed", stderr="")
        if "status" in argv:
            return BasicMemoryRunnerResult(
                returncode=0,
                stdout=json.dumps({"total_files": 0, "observed_files": []}),
                stderr="",
            )
        return BasicMemoryRunnerResult(returncode=1, stdout="", stderr="")

    orch_res = sync_and_initialize_basic_memory_backend(
        personal_tideway_config,
        registry=reg,
        dry_run=False,
        runner=mock_runner,
    )

    assert orch_res.dry_run is False
    assert orch_res.reconciliation.config_written is True
    assert orch_res.backend.skipped is False
    assert len(orch_res.backend.statuses) == 3
    assert layout.config_file.is_file()


def test_orchestration_coherent_single_registry_snapshot(
    personal_tideway_config: PersonalTidewayConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """Invariant 12b: sync_and_initialize loads registry exactly once when registry=None, ensuring coherent snapshot."""
    reg, names = create_sample_registry(tmp_path)
    save_registry(reg, personal_tideway_config)

    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    load_call_count = 0
    real_load = load_registry

    def counting_load_registry(path: Path) -> ProjectRegistry:
        nonlocal load_call_count
        load_call_count += 1
        return real_load(path)

    # Monkeypatch load_registry in basic_memory_backend
    monkeypatch.setattr("personal_tideway.core.basic_memory_backend.load_registry", counting_load_registry)

    def mock_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        if "reindex" in argv:
            return BasicMemoryRunnerResult(returncode=0, stdout="Indexed", stderr="")
        if "status" in argv:
            return BasicMemoryRunnerResult(
                returncode=0,
                stdout=json.dumps({"total_files": 0, "observed_files": []}),
                stderr="",
            )
        return BasicMemoryRunnerResult(returncode=1, stdout="", stderr="")

    orch_res = sync_and_initialize_basic_memory_backend(
        personal_tideway_config,
        registry=None,
        dry_run=False,
        runner=mock_runner,
    )

    # Must be loaded exactly once at the entry of sync_and_initialize_basic_memory_backend
    assert load_call_count == 1
    assert orch_res.backend.skipped is False
    assert len(orch_res.backend.statuses) == 3


def test_orchestration_missing_executable_fails_before_reconciliation_mutation(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """A missing backend must not leave a partially reconciled Basic Memory config."""
    reg, _ = create_sample_registry(tmp_path)
    before = snapshot_filesystem(personal_tideway_config.home)

    with pytest.raises(RuntimeProbeError) as exc_info:
        sync_and_initialize_basic_memory_backend(
            personal_tideway_config,
            registry=reg,
            dry_run=False,
        )

    assert str(exc_info.value) == "Basic Memory executable missing or invalid."
    assert exc_info.value.__cause__ is None
    assert snapshot_filesystem(personal_tideway_config.home) == before


def test_tampered_plan_forged_argv_fails_closed_zero_runner_calls(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Invariant 13: Tampered plans with forged reindex or status argv fail closed before invoking runner."""
    reg, names = create_sample_registry(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    canonical_plan = plan_basic_memory_backend(personal_tideway_config, reg)

    runner_calls: list[tuple[str, ...]] = []

    def mock_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        runner_calls.append(argv)
        return BasicMemoryRunnerResult(returncode=0, stdout="{}", stderr="")

    # Subcase A: Forged reindex argv with injected command
    forged_reindex = (
        ("sh", "-c", "echo canary_injection_secret"),
        canonical_plan.reindex_argvs[1],
        canonical_plan.reindex_argvs[2],
    )
    tampered_reindex_plan = replace(canonical_plan, reindex_argvs=forged_reindex)

    with pytest.raises(ValidationError) as exc_reindex:
        execute_basic_memory_backend_plan(
            tampered_reindex_plan,
            dry_run=False,
            runner=mock_runner,
            cfg=personal_tideway_config,
        )
    assert exc_reindex.value.__cause__ is None
    assert "canary_injection_secret" not in str(exc_reindex.value)
    assert len(runner_calls) == 0

    with pytest.raises(ValidationError):
        tampered_reindex_plan.preview()

    # Subcase B: Forged status argv with unsupported command
    forged_status = (
        canonical_plan.status_argvs[0],
        ("basic-memory", "sync-config"),
        canonical_plan.status_argvs[2],
    )
    tampered_status_plan = replace(canonical_plan, status_argvs=forged_status)

    with pytest.raises(ValidationError) as exc_status:
        execute_basic_memory_backend_plan(
            tampered_status_plan,
            dry_run=False,
            runner=mock_runner,
            cfg=personal_tideway_config,
        )
    assert exc_status.value.__cause__ is None
    assert len(runner_calls) == 0

    with pytest.raises(ValidationError):
        tampered_status_plan.preview()


def test_tampered_plan_extra_secret_env_key_fails_closed_zero_runner_calls(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Invariant 14: Tampered plans with extra or altered env_overrides fail closed without runner calls."""
    reg, names = create_sample_registry(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    canonical_plan = plan_basic_memory_backend(personal_tideway_config, reg)

    runner_calls: list[tuple[str, ...]] = []

    def mock_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        runner_calls.append(argv)
        return BasicMemoryRunnerResult(returncode=0, stdout="{}", stderr="")

    canary = "CANARY_INJECTED_ENV_SECRET_777"

    # Extra injected secret env key
    tampered_env = dict(canonical_plan.env_overrides)
    tampered_env["FORGED_SECRET_VARIABLE"] = canary
    tampered_plan = replace(canonical_plan, env_overrides=tampered_env)

    with pytest.raises(ValidationError) as exc_info:
        execute_basic_memory_backend_plan(
            tampered_plan,
            dry_run=False,
            runner=mock_runner,
            cfg=personal_tideway_config,
        )
    assert exc_info.value.__cause__ is None
    assert canary not in str(exc_info.value)
    assert len(runner_calls) == 0

    with pytest.raises(ValidationError):
        tampered_plan.preview()

    # Altered value of canonical key
    altered_env = dict(canonical_plan.env_overrides)
    altered_env[ENV_BASIC_MEMORY_AUTO_UPDATE] = "true"
    altered_plan = replace(canonical_plan, env_overrides=altered_env)

    with pytest.raises(ValidationError):
        execute_basic_memory_backend_plan(
            altered_plan,
            dry_run=False,
            runner=mock_runner,
            cfg=personal_tideway_config,
        )
    assert len(runner_calls) == 0

    # Missing canonical key
    missing_env = dict(canonical_plan.env_overrides)
    del missing_env[ENV_BASIC_MEMORY_NO_PROMOS]
    missing_plan = replace(canonical_plan, env_overrides=missing_env)

    with pytest.raises(ValidationError):
        execute_basic_memory_backend_plan(
            missing_plan,
            dry_run=False,
            runner=mock_runner,
            cfg=personal_tideway_config,
        )
    assert len(runner_calls) == 0


def test_tampered_plan_cardinality_and_project_names_validation(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Invariant 15: Rejects cardinality mismatch, duplicate project names (case-insensitive), and blank names."""
    reg, names = create_sample_registry(tmp_path)
    canonical_plan = plan_basic_memory_backend(personal_tideway_config, reg)

    # 1. Cardinality mismatch
    mismatched_reindex = replace(canonical_plan, reindex_argvs=canonical_plan.reindex_argvs[:2])
    with pytest.raises(ValidationError):
        execute_basic_memory_backend_plan(mismatched_reindex, cfg=personal_tideway_config)

    # 2. Duplicate project names: exact
    dup_names = ("project-one", "project-one")
    dup_exec = str(canonical_plan.executable)
    dup_reindex = (
        (dup_exec, "reindex", "--search", "--project", "project-one"),
        (dup_exec, "reindex", "--search", "--project", "project-one"),
    )
    dup_status = (
        (dup_exec, "status", "--project", "project-one", "--local", "--json"),
        (dup_exec, "status", "--project", "project-one", "--local", "--json"),
    )
    dup_plan = replace(
        canonical_plan,
        project_names=dup_names,
        reindex_argvs=dup_reindex,
        status_argvs=dup_status,
    )
    with pytest.raises(ValidationError) as exc_dup1:
        validate_basic_memory_backend_plan(dup_plan)
    assert str(exc_dup1.value) == "Plan contains duplicate project names."

    # 3. Duplicate project names: case-insensitive
    dup_case_names = ("MyProject", "myproject")
    dup_case_reindex = (
        (dup_exec, "reindex", "--search", "--project", "MyProject"),
        (dup_exec, "reindex", "--search", "--project", "myproject"),
    )
    dup_case_status = (
        (dup_exec, "status", "--project", "MyProject", "--local", "--json"),
        (dup_exec, "status", "--project", "myproject", "--local", "--json"),
    )
    dup_case_plan = replace(
        canonical_plan,
        project_names=dup_case_names,
        reindex_argvs=dup_case_reindex,
        status_argvs=dup_case_status,
    )
    with pytest.raises(ValidationError) as exc_dup2:
        validate_basic_memory_backend_plan(dup_case_plan)
    assert str(exc_dup2.value) == "Plan contains duplicate project names."

    # 4. Blank or whitespace project names
    blank_names = ("   ",)
    blank_reindex = ((dup_exec, "reindex", "--search", "--project", "   "),)
    blank_status = ((dup_exec, "status", "--project", "   ", "--local", "--json"),)
    blank_plan = replace(
        canonical_plan,
        project_names=blank_names,
        reindex_argvs=blank_reindex,
        status_argvs=blank_status,
    )
    with pytest.raises(ValidationError) as exc_blank:
        validate_basic_memory_backend_plan(blank_plan)
    assert str(exc_blank.value) == "Plan project name must be a non-empty string."


def test_tampered_plan_invalid_timeouts_fails_closed_zero_runner_calls(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Invariant 16: Invalid timeouts (bool, NaN, +/-inf, non-positive, non-numeric) fail closed."""
    reg, names = create_sample_registry(tmp_path)
    canonical_plan = plan_basic_memory_backend(personal_tideway_config, reg)

    runner_calls: list[tuple[str, ...]] = []

    def mock_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        runner_calls.append(argv)
        return BasicMemoryRunnerResult(returncode=0, stdout="{}", stderr="")

    invalid_timeouts = [
        True,
        False,
        float("nan"),
        float("inf"),
        float("-inf"),
        0,
        0.0,
        -1,
        -15.5,
        "120",
        None,
    ]

    for bad_t in invalid_timeouts:
        bad_reindex_plan = replace(canonical_plan, reindex_timeout=bad_t)  # type: ignore
        with pytest.raises(ValidationError) as exc_r:
            execute_basic_memory_backend_plan(
                bad_reindex_plan,
                dry_run=False,
                runner=mock_runner,
                cfg=personal_tideway_config,
            )
        assert exc_r.value.__cause__ is None
        assert str(exc_r.value) == "Plan timeout must be a positive finite number."
        assert len(runner_calls) == 0

        bad_status_plan = replace(canonical_plan, status_timeout=bad_t)  # type: ignore
        with pytest.raises(ValidationError) as exc_s:
            execute_basic_memory_backend_plan(
                bad_status_plan,
                dry_run=False,
                runner=mock_runner,
                cfg=personal_tideway_config,
            )
        assert exc_s.value.__cause__ is None
        assert str(exc_s.value) == "Plan timeout must be a positive finite number."
        assert len(runner_calls) == 0


def test_tampered_plan_noncanonical_executable_and_layout_fails_closed(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Invariant 17: Non-canonical executable or layout mismatch fails closed with ValidationError."""
    reg, names = create_sample_registry(tmp_path)
    canonical_plan = plan_basic_memory_backend(personal_tideway_config, reg)

    runner_calls: list[tuple[str, ...]] = []

    def mock_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        runner_calls.append(argv)
        return BasicMemoryRunnerResult(returncode=0, stdout="{}", stderr="")

    # Subcase A: Executable does not match layout primary_executable
    forged_exec_plan = replace(canonical_plan, executable=Path("/usr/bin/bash"))
    with pytest.raises(ValidationError) as exc_exec:
        execute_basic_memory_backend_plan(
            forged_exec_plan,
            dry_run=False,
            runner=mock_runner,
            cfg=personal_tideway_config,
        )
    assert exc_exec.value.__cause__ is None
    assert len(runner_calls) == 0

    # Subcase B: Execution without cfg for non-noop non-dry-run plan
    with pytest.raises(ValidationError) as exc_no_cfg:
        execute_basic_memory_backend_plan(
            canonical_plan,
            dry_run=False,
            runner=mock_runner,
            cfg=None,
        )
    assert exc_no_cfg.value.__cause__ is None
    assert "PersonalTidewayConfig is required" in str(exc_no_cfg.value)
    assert len(runner_calls) == 0

    # Subcase C: Plan layout from foreign workspace
    foreign_cfg = PersonalTidewayConfig.resolve(
        home=tmp_path / "foreign_home",
        codex_home=tmp_path / "foreign_codex",
        gemini_home=tmp_path / "foreign_gemini",
    )
    foreign_layout = get_basic_memory_layout(foreign_cfg)
    foreign_plan = replace(
        canonical_plan,
        layout=foreign_layout,
        executable=foreign_layout.primary_executable,
    )

    with pytest.raises(ValidationError) as exc_foreign:
        execute_basic_memory_backend_plan(
            foreign_plan,
            dry_run=False,
            runner=mock_runner,
            cfg=personal_tideway_config,
        )
    assert exc_foreign.value.__cause__ is None
    assert len(runner_calls) == 0


def test_plan_validation_preserves_boundary_error_and_suppresses_unexpected(
    personal_tideway_config: PersonalTidewayConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """Invariant 18: BoundaryError is preserved exactly on symlink boundaries; unexpected errors raise ValidationError."""
    reg, names = create_sample_registry(tmp_path)
    plan = plan_basic_memory_backend(personal_tideway_config, reg)

    # 1. Symlink root boundary preserves BoundaryError
    symlink_home = tmp_path / "symlink_home"
    symlink_home.symlink_to(personal_tideway_config.home)
    # Preserve the lexical symlink path: resolve() intentionally canonicalizes it.
    symlink_cfg = replace(personal_tideway_config, home=symlink_home)

    with pytest.raises(BoundaryError):
        validate_basic_memory_backend_plan(plan, cfg=symlink_cfg)

    # 2. Unexpected error during layout resolution is translated to fixed ValidationError with from None
    canary = "CANARY_UNEXPECTED_DISK_CRASH"

    def crashing_layout(cfg: PersonalTidewayConfig):
        raise RuntimeError(f"Unexpected kernel failure: {canary}")

    monkeypatch.setattr("personal_tideway.core.basic_memory_backend.get_basic_memory_layout", crashing_layout)

    with pytest.raises(ValidationError) as exc_unexp:
        validate_basic_memory_backend_plan(plan, cfg=personal_tideway_config)

    assert exc_unexp.value.__cause__ is None
    assert str(exc_unexp.value) == "Failed to resolve canonical workspace layout."
    assert canary not in str(exc_unexp.value)


def test_hardened_runner_normalization_and_error_conversions(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Invariant 19: Strict runner normalization enforces int returncode, exact 3-tuple, safe byte streams, fixed errors."""
    reg, names = create_sample_registry(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    plan = plan_basic_memory_backend(personal_tideway_config, reg)

    class ObjectWithBadReturncode:
        returncode = "0"
        stdout = ""
        stderr = ""

    class ObjectWithBoolReturncode:
        returncode = True
        stdout = ""
        stderr = ""

    class ObjectWithBadStdout:
        returncode = 0
        stdout = 12345
        stderr = ""

    class ObjectWithValidNoneStream:
        returncode = 0
        stdout = json.dumps({"total_files": 0, "observed_files": []})
        stderr = None

    # 1. Rejected malformed returns
    rejected_returns = [
        ("0", "out", "err"),  # numeric string returncode
        (True, "out", "err"),  # bool returncode
        (False, "out", "err"),  # bool returncode
        (0, "out"),  # tuple length 2
        (0, "out", "err", "extra"),  # tuple length 4
        (0, 123, "err"),  # non-string non-bytes stdout
        (0, "out", ["err"]),  # non-string non-bytes stderr
        ObjectWithBadReturncode(),
        ObjectWithBoolReturncode(),
        ObjectWithBadStdout(),
        None,
        42,
        "raw_string",
    ]

    def make_bad_runner(result: Any):
        def bad_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> Any:
            return result

        return bad_runner

    for bad_ret in rejected_returns:
        bad_runner = make_bad_runner(bad_ret)

        with pytest.raises(RuntimeProbeError) as exc_info:
            execute_basic_memory_backend_plan(
                plan,
                dry_run=False,
                runner=bad_runner,
                cfg=personal_tideway_config,
            )
        assert exc_info.value.__cause__ is None
        assert str(exc_info.value) == "Runner returned an unexpected result type."

    # 2. Accepted valid variations: bytes stdout, bytearray stderr, None stream
    def bytes_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> Any:
        if "reindex" in argv:
            return (0, b"indexed", bytearray(b"warnings"))
        if "status" in argv:
            status_bytes = json.dumps({"total_files": 5, "observed_files": []}).encode("utf-8")
            return (0, status_bytes, None)
        return (1, "", "")

    res_bytes = execute_basic_memory_backend_plan(
        plan,
        dry_run=False,
        runner=bytes_runner,
        cfg=personal_tideway_config,
    )
    assert len(res_bytes.statuses) == 3
    assert res_bytes.statuses[0].total_files == 5


def test_n_equals_one_single_project_command_boundary(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Invariant 20: Exactly N=1 project registry constructs initialization argv, zero remaining reindex, 1 status."""
    p1 = ProjectRecord.create(slug="single-proj", display_name="Single Project", kind="directory", paths=[str(tmp_path)])
    reg = ProjectRegistry(projects=[p1])
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    plan = plan_basic_memory_backend(personal_tideway_config, reg)

    assert len(plan.project_names) == 1
    assert len(plan.reindex_argvs) == 1
    assert len(plan.status_argvs) == 1
    assert plan.initialization_argv == plan.reindex_argvs[0]
    assert plan.remaining_reindex_argvs == ()

    exec_str = str(layout.primary_executable)
    p_name = p1.memory.project_name
    assert plan.initialization_argv == (exec_str, "reindex", "--search", "--project", p_name)
    assert plan.status_argvs[0] == (exec_str, "status", "--project", p_name, "--local", "--json")

    invoked_argvs: list[tuple[str, ...]] = []

    def mock_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        invoked_argvs.append(argv)
        if "reindex" in argv:
            return BasicMemoryRunnerResult(returncode=0, stdout="Indexed", stderr="")
        if "status" in argv:
            return BasicMemoryRunnerResult(
                returncode=0,
                stdout=json.dumps({"total_files": 7, "observed_files": []}),
                stderr="",
            )
        return BasicMemoryRunnerResult(returncode=1, stdout="", stderr="")

    result = execute_basic_memory_backend_plan(
        plan,
        dry_run=False,
        runner=mock_runner,
        cfg=personal_tideway_config,
    )

    assert len(invoked_argvs) == 2
    assert invoked_argvs[0][1] == "reindex"
    assert invoked_argvs[1][1] == "status"
    assert len(result.statuses) == 1
    assert result.statuses[0].total_files == 7


def test_public_to_dict_methods_and_aliases(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Invariant 21: Public to_dict serialization methods and canonical aliases work consistently."""
    reg, names = create_sample_registry(tmp_path)
    plan = plan_basic_memory_backend(personal_tideway_config, reg)

    # 1. Aliases identity
    assert plan_backend is plan_basic_memory_backend
    assert execute_backend_plan is execute_basic_memory_backend_plan

    # 2. Plan to_dict
    plan_dict = plan.to_dict()
    assert plan_dict["project_names"] == list(names)
    assert plan_dict["is_noop"] is False
    assert plan_dict["reindex_timeout"] == DEFAULT_REINDEX_TIMEOUT
    assert plan_dict["status_timeout"] == DEFAULT_STATUS_TIMEOUT
    assert "layout" in plan_dict
    assert "initialization_argv" in plan_dict

    # 3. Preview to_dict
    prev = plan.preview()
    prev_dict = prev.to_dict()
    assert prev_dict["project_names"] == list(names)
    assert prev_dict["is_noop"] is False
    assert prev.initialization_argv == plan.initialization_argv

    # 4. Status to_dict & aliases
    st = BasicMemoryProjectStatus(project_name="proj-x", total_files=12, observed_files_count=4)
    assert st.files_count == 12
    assert st.observed_count == 4
    st_dict = st.to_dict()
    assert st_dict == {
        "project_name": "proj-x",
        "total_files": 12,
        "observed_files_count": 4,
    }

    # 5. Result to_dict
    res = BasicMemoryBackendResult(
        dry_run=False,
        skipped=False,
        reindexed_projects=tuple(names),
        statuses=(st,),
        preview=prev,
    )
    res_dict = res.to_dict()
    assert res_dict["dry_run"] is False
    assert res_dict["skipped"] is False
    assert res_dict["reindexed_projects"] == list(names)
    assert len(res_dict["statuses"]) == 1
    assert res_dict["preview"] is not None

    # 6. Orchestration to_dict & dry_run alias
    from personal_tideway.core.basic_memory_reconciliation import (
        BasicMemoryProjectReconciliationResult,
    )

    recon_mock = BasicMemoryProjectReconciliationResult(
        dry_run=False,
        changed=True,
        config_written=True,
    )
    orch = BasicMemoryOrchestrationResult(reconciliation=recon_mock, backend=res)
    assert orch.dry_run is False
    orch_dict = orch.to_dict()
    assert "reconciliation" in orch_dict
    assert "backend" in orch_dict
