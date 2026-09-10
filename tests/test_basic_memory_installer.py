"""Tests for safe, testable execution of the Basic Memory 0.23.2 install plan.

Work Package 9b verification suite with fake runners only:
1. Dry-run snapshot zero filesystem mutation and zero runner calls.
2. Fresh success order and exact argv, minimal environment, and exact timeout sequence.
3. Healthy second call skips install and leaves config bytes and mtime unchanged.
4. Wrong version triggers single-shot convergence install with --force and timeout sequence.
5. Nonzero, timeout, exception, missing binary, and wrong post-version fixed safe failures without secret stderr leakage.
6. All unsafe existing config forms (hardlink, FIFO, symlink, oversized, invalid JSON/schema) rejected without modifying target.
7. Symlink directory race and setup boundary rejections with secret-sentinel target regression.
8. Unrelated host variables and secrets excluded from child process environment.
9. Source mapping mutation resistance for runner environment and frozen result.
10. Strict exclusion of status, doctor, index, and notes verbs across all execution paths.
11. Atomic config creation race collision preserves destination.
12. Runner side-effect then TypeError invoked strictly once.
13. Bounded output limits enforced in UTF-8 bytes without breaking Unicode.
"""

from collections.abc import Mapping
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any
import pytest

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.core.basic_memory_installer import (
    DEFAULT_HEALTH_TIMEOUT,
    DEFAULT_INSTALL_TIMEOUT,
    MAX_SUBPROCESS_OUTPUT_BYTES,
    BasicMemoryInstallResult,
    BasicMemoryRunnerResult,
    acquire_basic_memory_lock,
    build_subprocess_env,
    default_subprocess_runner,
    ensure_safe_bootstrap_config,
    install_basic_memory,
    parse_basic_memory_version,
    truncate_utf8_bytes,
    validate_isolated_executable,
)
from personal_tideway.core.basic_memory_runtime import (
    BASIC_MEMORY_PINNED_VERSION,
    BASIC_MEMORY_REQUIREMENT,
    BasicMemoryBootstrapConfig,
    get_basic_memory_layout,
)
from personal_tideway.exceptions import BoundaryError, ConfigError, RuntimeProbeError


def create_mock_uv(tmp_path: Path) -> Path:
    """Create a mock executable file representing uv."""
    bin_dir = tmp_path / "mock_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    uv_file = bin_dir / "uv"
    uv_file.write_text("#!/bin/sh\necho uv 0.5.0\n", encoding="utf-8")
    uv_file.chmod(0o755)
    return uv_file


def snapshot_filesystem(root: Path) -> dict[str, tuple[int, int, int]]:
    """Capture snapshot of directory entries: (size, mtime_ns, mode)."""
    if not root.exists():
        return {}
    entries = {}
    for p in sorted(root.rglob("*")):
        try:
            st = p.lstat()
            entries[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, st.st_mode)
        except OSError:
            pass
    return entries


def test_dry_run_snapshot_zero_mutation_and_zero_calls(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert dry_run performs zero filesystem mutations and zero runner invocations."""
    mock_uv = create_mock_uv(tmp_path)
    before_snapshot = snapshot_filesystem(personal_tideway_config.home)

    calls = []

    def fake_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        calls.append(argv)
        return BasicMemoryRunnerResult(returncode=0, stdout="", stderr="")

    res = install_basic_memory(
        personal_tideway_config,
        uv_executable=mock_uv,
        dry_run=True,
        runner=fake_runner,
    )

    assert len(calls) == 0
    assert res.dry_run is True
    assert res.config_created is False
    assert res.install_attempted is False
    assert res.already_healthy is False
    assert res.healthy is False
    assert res.version is None
    assert res.preview is not None

    expected_config_file = personal_tideway_config.basic_memory_dir / "config" / "config.json"
    assert res.preview.config_path == expected_config_file

    after_snapshot = snapshot_filesystem(personal_tideway_config.home)
    assert before_snapshot == after_snapshot

    # Confirm no new planned subdirectories or config created
    layout = get_basic_memory_layout(personal_tideway_config)
    assert not layout.uv_tool_dir.exists()
    assert not layout.bin_dir.exists()
    assert not layout.config_dir.exists()
    assert not layout.config_file.exists()


def test_fresh_success_order_and_exact_argv_and_env(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert fresh install executes install once then health check with exact planned argv, minimal env, and timeouts."""
    mock_uv = create_mock_uv(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)

    calls: list[tuple[tuple[str, ...], dict[str, str], float]] = []

    def fake_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        calls.append((tuple(argv), dict(env), timeout))
        if len(calls) == 1:
            # Install invocation: simulate uv tool install creating isolated binary
            layout.primary_executable.parent.mkdir(parents=True, exist_ok=True)
            layout.primary_executable.write_text("#!/bin/sh\necho basic-memory 0.23.2\n", encoding="utf-8")
            layout.primary_executable.chmod(0o755)
            return BasicMemoryRunnerResult(returncode=0, stdout="Installed basic-memory\n", stderr="")
        elif len(calls) == 2:
            # Health check invocation
            return BasicMemoryRunnerResult(returncode=0, stdout="basic-memory 0.23.2\n", stderr="")
        return BasicMemoryRunnerResult(returncode=1, stdout="", stderr="unexpected invocation")

    res = install_basic_memory(
        personal_tideway_config,
        uv_executable=mock_uv,
        dry_run=False,
        runner=fake_runner,
    )

    assert res.dry_run is False
    assert res.config_created is True
    assert res.install_attempted is True
    assert res.already_healthy is False
    assert res.healthy is True
    assert res.version == BASIC_MEMORY_PINNED_VERSION

    # Verify exactly 2 calls: install followed by health check
    assert len(calls) == 2

    # 1. Exact install argv and timeout (DEFAULT_INSTALL_TIMEOUT = 300.0)
    expected_install_argv = (
        str(mock_uv),
        "--no-config",
        "tool",
        "install",
        "--force",
        "--prerelease=allow",
        BASIC_MEMORY_REQUIREMENT,
    )
    assert calls[0][0] == expected_install_argv
    assert calls[0][2] == DEFAULT_INSTALL_TIMEOUT

    # 2. Exact health argv and timeout (DEFAULT_HEALTH_TIMEOUT = 15.0)
    expected_health_argv = (str(layout.primary_executable), "--version")
    assert calls[1][0] == expected_health_argv
    assert calls[1][2] == DEFAULT_HEALTH_TIMEOUT

    # Verify controlled environment overrides for both calls
    for _, call_env, _ in calls:
        assert call_env["UV_TOOL_DIR"] == str(layout.uv_tool_dir)
        assert call_env["UV_TOOL_BIN_DIR"] == str(layout.bin_dir)
        assert call_env["UV_CACHE_DIR"] == str(layout.cache_dir)
        assert call_env["BASIC_MEMORY_CONFIG_DIR"] == str(layout.config_dir)
        assert call_env["BASIC_MEMORY_AUTO_UPDATE"] == "false"
        assert call_env["BASIC_MEMORY_NO_PROMOS"] == "1"

    # Verify directory modes (0700) and config file mode (0600)
    assert (layout.service_root.stat().st_mode & 0o777) == 0o700
    assert (layout.uv_tool_dir.stat().st_mode & 0o777) == 0o700
    assert (layout.bin_dir.stat().st_mode & 0o777) == 0o700
    assert (layout.config_dir.stat().st_mode & 0o777) == 0o700
    assert (layout.cache_dir.stat().st_mode & 0o777) == 0o700
    assert (layout.config_file.stat().st_mode & 0o777) == 0o600

    config_data = json.loads(layout.config_file.read_text(encoding="utf-8"))
    assert config_data == {"auto_update": False, "default_project": None, "projects": {}}


def test_healthy_second_call_skips_install_and_config_bytes_mtime_unchanged(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert second call on healthy runtime skips install and leaves config bytes and mtime unchanged."""
    mock_uv = create_mock_uv(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)

    # Initial successful install
    def initial_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        if "--version" not in argv:
            layout.primary_executable.parent.mkdir(parents=True, exist_ok=True)
            layout.primary_executable.write_text("#!/bin/sh\necho basic-memory 0.23.2\n", encoding="utf-8")
            layout.primary_executable.chmod(0o755)
        return BasicMemoryRunnerResult(returncode=0, stdout="basic-memory 0.23.2\n", stderr="")

    res1 = install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=initial_runner)
    assert res1.healthy is True
    assert res1.config_created is True

    # Record config file state before second call
    config_bytes_before = layout.config_file.read_bytes()
    config_st_before = layout.config_file.stat()
    config_mtime_before = config_st_before.st_mtime_ns

    # Second call with runner tracking
    calls2: list[tuple[tuple[str, ...], float]] = []

    def second_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        calls2.append((tuple(argv), timeout))
        return BasicMemoryRunnerResult(returncode=0, stdout="basic-memory 0.23.2\n", stderr="")

    res2 = install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=second_runner)

    # Only pre-health check was executed (1 call), install was skipped
    assert len(calls2) == 1
    assert calls2[0][0] == (str(layout.primary_executable), "--version")
    assert calls2[0][1] == DEFAULT_HEALTH_TIMEOUT

    assert res2.dry_run is False
    assert res2.config_created is False
    assert res2.install_attempted is False
    assert res2.already_healthy is True
    assert res2.healthy is True
    assert res2.version == BASIC_MEMORY_PINNED_VERSION

    # Config bytes and mtime are strictly identical
    assert layout.config_file.read_bytes() == config_bytes_before
    assert layout.config_file.stat().st_mtime_ns == config_mtime_before


def test_wrong_version_triggers_force_install(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert pre-health detecting mismatched version triggers single-shot convergence install with --force."""
    mock_uv = create_mock_uv(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)

    # Pre-populate isolated binary reporting an older version 0.22.0
    layout.primary_executable.parent.mkdir(parents=True, exist_ok=True)
    layout.primary_executable.write_text("#!/bin/sh\necho basic-memory 0.22.0\n", encoding="utf-8")
    layout.primary_executable.chmod(0o755)

    calls: list[tuple[tuple[str, ...], float]] = []

    def convergence_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        calls.append((tuple(argv), timeout))
        if len(calls) == 1:
            # 1. pre-health check: reports old version
            return BasicMemoryRunnerResult(returncode=0, stdout="basic-memory 0.22.0\n", stderr="")
        elif len(calls) == 2:
            # 2. install with --force
            return BasicMemoryRunnerResult(returncode=0, stdout="Upgraded basic-memory\n", stderr="")
        elif len(calls) == 3:
            # 3. post-health check: reports pinned 0.23.2
            return BasicMemoryRunnerResult(returncode=0, stdout="basic-memory 0.23.2\n", stderr="")
        return BasicMemoryRunnerResult(returncode=1, stdout="", stderr="extra call")

    res = install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=convergence_runner)

    assert len(calls) == 3
    # Call 1: pre-health with DEFAULT_HEALTH_TIMEOUT
    assert calls[0][0] == (str(layout.primary_executable), "--version")
    assert calls[0][1] == DEFAULT_HEALTH_TIMEOUT

    # Call 2: install with --force and DEFAULT_INSTALL_TIMEOUT
    assert calls[1][0] == (
        str(mock_uv),
        "--no-config",
        "tool",
        "install",
        "--force",
        "--prerelease=allow",
        BASIC_MEMORY_REQUIREMENT,
    )
    assert calls[1][1] == DEFAULT_INSTALL_TIMEOUT

    # Call 3: post-health with DEFAULT_HEALTH_TIMEOUT
    assert calls[2][0] == (str(layout.primary_executable), "--version")
    assert calls[2][1] == DEFAULT_HEALTH_TIMEOUT

    assert res.already_healthy is False
    assert res.install_attempted is True
    assert res.healthy is True
    assert res.version == BASIC_MEMORY_PINNED_VERSION


def test_fixed_safe_failures_and_sentinel_stderr_not_leaked(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert all failure modes raise fixed RuntimeProbeError without leaking sensitive stderr sentinels."""
    mock_uv = create_mock_uv(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    sentinel = "SECRET_SENTINEL_TOKEN_XYZ_999"

    # Case A: Nonzero return code on install
    def fail_install_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(returncode=1, stdout="", stderr=f"Install fatal error: {sentinel}")

    with pytest.raises(RuntimeProbeError) as exc_a:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=fail_install_runner)
    assert sentinel not in str(exc_a.value)
    assert exc_a.value.__cause__ is None
    assert str(exc_a.value) == "Basic Memory installation failed with non-zero exit code."

    # Inspectable partial state: config and dirs remain intact
    assert layout.service_root.exists()
    assert layout.config_file.exists()

    # Case B: Timeout on install
    def timeout_install_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        raise TimeoutError(f"Install timed out with {sentinel}")

    with pytest.raises(RuntimeProbeError) as exc_b:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=timeout_install_runner)
    assert sentinel not in str(exc_b.value)
    assert exc_b.value.__cause__ is None
    assert str(exc_b.value) == "Basic Memory installation timed out."

    # Case C: Runner exception on install
    def exception_install_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        raise RuntimeError(f"Network failure with {sentinel}")

    with pytest.raises(RuntimeProbeError) as exc_c:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=exception_install_runner)
    assert sentinel not in str(exc_c.value)
    assert exc_c.value.__cause__ is None
    assert str(exc_c.value) == "Basic Memory installation failed."

    # Case D: Missing binary after install
    def missing_binary_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(returncode=0, stdout="success", stderr="")

    with pytest.raises(RuntimeProbeError) as exc_d:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=missing_binary_runner)
    assert sentinel not in str(exc_d.value)
    assert exc_d.value.__cause__ is None
    assert str(exc_d.value) == "Basic Memory executable missing or invalid after installation."

    # Create binary on disk for post-health failure testing
    layout.primary_executable.parent.mkdir(parents=True, exist_ok=True)
    layout.primary_executable.write_text("#!/bin/sh\necho 1\n", encoding="utf-8")
    layout.primary_executable.chmod(0o755)

    # Case E: Nonzero return code on post-health
    def fail_health_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        if "--version" in argv:
            return BasicMemoryRunnerResult(returncode=2, stdout="", stderr=f"Health error: {sentinel}")
        return BasicMemoryRunnerResult(returncode=0, stdout="installed", stderr="")

    with pytest.raises(RuntimeProbeError) as exc_e:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=fail_health_runner)
    assert sentinel not in str(exc_e.value)
    assert exc_e.value.__cause__ is None
    assert str(exc_e.value) == "Basic Memory health check failed with non-zero exit code."

    # Case F: Timeout on post-health
    def timeout_health_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        if "--version" in argv:
            raise TimeoutError(f"Health timed out with {sentinel}")
        return BasicMemoryRunnerResult(returncode=0, stdout="installed", stderr="")

    with pytest.raises(RuntimeProbeError) as exc_f:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=timeout_health_runner)
    assert sentinel not in str(exc_f.value)
    assert exc_f.value.__cause__ is None
    assert str(exc_f.value) == "Basic Memory health check timed out."

    # Case G: Runner exception on post-health
    def exception_health_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        if "--version" in argv:
            raise RuntimeError(f"Health crash with {sentinel}")
        return BasicMemoryRunnerResult(returncode=0, stdout="installed", stderr="")

    with pytest.raises(RuntimeProbeError) as exc_g:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=exception_health_runner)
    assert sentinel not in str(exc_g.value)
    assert exc_g.value.__cause__ is None
    assert str(exc_g.value) == "Basic Memory health check failed."

    # Case H: Wrong version on post-health
    def wrong_version_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        if "--version" in argv:
            return BasicMemoryRunnerResult(returncode=0, stdout=f"basic-memory 0.99.9 {sentinel}\n", stderr="")
        return BasicMemoryRunnerResult(returncode=0, stdout="installed", stderr="")

    with pytest.raises(RuntimeProbeError) as exc_h:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=wrong_version_runner)
    assert sentinel not in str(exc_h.value)
    assert exc_h.value.__cause__ is None
    assert str(exc_h.value) == "Basic Memory health check reported incorrect version."


def test_all_unsafe_existing_config_forms_rejected(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert existing config policy rejects symlink, FIFO, hardlink, oversize, and non-conforming JSON without modifying target."""
    mock_uv = create_mock_uv(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    layout.config_dir.mkdir(parents=True, exist_ok=True)
    config_file = layout.config_file

    canary_target = tmp_path / "protected_target.txt"
    canary_content = "DO_NOT_MODIFY_CANARY_SECRET_12345"
    canary_target.write_text(canary_content, encoding="utf-8")

    def clean_config():
        if config_file.is_symlink() or config_file.exists():
            config_file.unlink()

    # 1. Hardlink rejection: st_nlink != 1
    clean_config()
    os.link(canary_target, config_file)
    with pytest.raises(ConfigError) as exc_hl:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)
    assert "invalid link count" in str(exc_hl.value).lower()
    assert canary_content not in str(exc_hl.value)
    assert str(canary_target) not in str(exc_hl.value)
    assert canary_target.read_text(encoding="utf-8") == canary_content

    # 2. FIFO rejection: not a regular file
    clean_config()
    os.mkfifo(config_file)
    with pytest.raises(ConfigError) as exc_fifo:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)
    assert "not a regular file" in str(exc_fifo.value).lower()

    # 3. Symlink rejection (caught earlier as BoundaryError by get_basic_memory_layout)
    clean_config()
    config_file.symlink_to(canary_target)
    with pytest.raises((BoundaryError, ConfigError)) as exc_sym:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)
    assert "symlink" in str(exc_sym.value).lower()
    assert canary_content not in str(exc_sym.value)
    assert str(canary_target) not in str(exc_sym.value)
    assert canary_target.read_text(encoding="utf-8") == canary_content

    # 4. Oversized rejection (> 64KiB)
    clean_config()
    config_file.write_text("x" * (65 * 1024), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_over:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)
    assert "exceeds size limit" in str(exc_over.value).lower()

    # 5. Invalid JSON syntax
    clean_config()
    config_file.write_text("{ unclosed invalid json", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_json:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)
    assert "invalid json" in str(exc_json.value).lower()

    # 6. Unsafe auto_update=True
    clean_config()
    config_file.write_text(json.dumps({"auto_update": True, "default_project": None, "projects": {}}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_m1:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)
    assert "does not match safe bootstrap" in str(exc_m1.value).lower()

    # 7. Unsafe default_project != None
    clean_config()
    config_file.write_text(json.dumps({"auto_update": False, "default_project": "default", "projects": {}}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_m2:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)
    assert "does not match safe bootstrap" in str(exc_m2.value).lower()

    # 8. Non-dict projects mapping
    clean_config()
    config_file.write_text(json.dumps({"auto_update": False, "default_project": None, "projects": ["not", "a", "dict"]}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_m3:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)
    assert "does not match safe bootstrap" in str(exc_m3.value).lower()

    # 9. Documented upstream extra settings are accepted and reused byte-for-byte/mtime unchanged
    clean_config()
    upstream_extras_data = {
        "auto_update": False,
        "default_project": None,
        "projects": {},
        "semantic_search_enabled": True,
        "semantic_min_similarity": 0.75,
        "update_check_interval": 86400,
    }
    upstream_extras_json = json.dumps(upstream_extras_data, indent=2, sort_keys=True) + "\n"
    config_file.write_text(upstream_extras_json, encoding="utf-8")
    os.chmod(config_file, 0o600)
    bytes_before_extra = config_file.read_bytes()
    mtime_before_extra = config_file.stat().st_mtime_ns

    dummy_bootstrap = BasicMemoryBootstrapConfig()
    created = ensure_safe_bootstrap_config(config_file, dummy_bootstrap)
    assert created is False
    assert config_file.read_bytes() == bytes_before_extra
    assert config_file.stat().st_mtime_ns == mtime_before_extra
    assert json.loads(config_file.read_text(encoding="utf-8")) == upstream_extras_data

    # 10. Missing mandatory keys (e.g. missing 'auto_update' or missing 'projects')
    clean_config()
    config_file.write_text(json.dumps({"default_project": None, "projects": {}}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_m4_no_update:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)
    assert "does not match safe bootstrap" in str(exc_m4_no_update.value).lower()

    clean_config()
    config_file.write_text(json.dumps({"auto_update": False, "default_project": None}), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_m4_no_proj:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)
    assert "does not match safe bootstrap" in str(exc_m4_no_proj.value).lower()

    # 11. Non-dict JSON
    clean_config()
    config_file.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_m5:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)
    assert "does not match safe bootstrap" in str(exc_m5.value).lower()
    clean_config()


def test_symlink_directory_race_and_setup_rejected(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert symlink directories in runtime hierarchy are rejected with fixed BoundaryError."""
    mock_uv = create_mock_uv(tmp_path)
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()

    def recreate_clean_services():
        if personal_tideway_config.services_dir.is_symlink() or personal_tideway_config.services_dir.is_file():
            personal_tideway_config.services_dir.unlink()
        elif personal_tideway_config.services_dir.is_dir():
            shutil.rmtree(personal_tideway_config.services_dir)
        personal_tideway_config.services_dir.mkdir(mode=0o700, exist_ok=True)
        personal_tideway_config.basic_memory_dir.mkdir(mode=0o700, exist_ok=True)

    # 1. services directory itself is a symlink
    # Safely remove existing services tree created by init fixture
    if personal_tideway_config.services_dir.is_symlink() or personal_tideway_config.services_dir.is_file():
        personal_tideway_config.services_dir.unlink()
    elif personal_tideway_config.services_dir.is_dir():
        shutil.rmtree(personal_tideway_config.services_dir)

    personal_tideway_config.services_dir.symlink_to(outside_dir)
    with pytest.raises(BoundaryError) as exc1:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)
    assert "symlink" in str(exc1.value).lower()

    # Reconstruct clean services between cases
    recreate_clean_services()

    # 2. basic_memory_dir is a symlink
    if personal_tideway_config.basic_memory_dir.is_symlink() or personal_tideway_config.basic_memory_dir.is_file():
        personal_tideway_config.basic_memory_dir.unlink()
    elif personal_tideway_config.basic_memory_dir.is_dir():
        shutil.rmtree(personal_tideway_config.basic_memory_dir)

    personal_tideway_config.basic_memory_dir.symlink_to(outside_dir)
    with pytest.raises(BoundaryError) as exc2:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)
    assert "symlink" in str(exc2.value).lower()

    # Reconstruct clean services between cases
    recreate_clean_services()

    # 3. config_dir is a symlink
    layout = get_basic_memory_layout(personal_tideway_config)
    layout.config_dir.symlink_to(outside_dir)
    with pytest.raises(BoundaryError) as exc3:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)
    assert "symlink" in str(exc3.value).lower()

    recreate_clean_services()


def test_symlink_directory_does_not_leak_secret_sentinel_target(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert symlink rejection before path resolution never leaks sensitive external target path."""
    mock_uv = create_mock_uv(tmp_path)
    sentinel_secret = "CANARY_SECRET_EXTERNAL_TARGET_PATH_112233"
    secret_target = tmp_path / f"outside_secret_{sentinel_secret}"
    secret_target.mkdir()

    # Symlink basic_memory_dir to sensitive external target
    if personal_tideway_config.basic_memory_dir.exists():
        if personal_tideway_config.basic_memory_dir.is_dir():
            shutil.rmtree(personal_tideway_config.basic_memory_dir)
        else:
            personal_tideway_config.basic_memory_dir.unlink()

    personal_tideway_config.services_dir.mkdir(parents=True, exist_ok=True)
    personal_tideway_config.basic_memory_dir.symlink_to(secret_target)

    with pytest.raises(BoundaryError) as exc:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)

    assert str(exc.value) == "Layout target component is a symlink, which is not permitted in child layout."
    assert sentinel_secret not in str(exc.value)
    assert str(secret_target) not in str(exc.value)

    # Clean up symlink
    personal_tideway_config.basic_memory_dir.unlink()
    personal_tideway_config.basic_memory_dir.mkdir(mode=0o700, exist_ok=True)

    # Symlink workspace root itself
    symlink_home = tmp_path / f"home_symlink_{sentinel_secret}"
    symlink_home.symlink_to(personal_tideway_config.home)
    sym_cfg = replace(personal_tideway_config, home=symlink_home)

    with pytest.raises(BoundaryError) as exc_root:
        install_basic_memory(sym_cfg, uv_executable=mock_uv)

    assert str(exc_root.value) == "Workspace root is a symlink, which is not permitted."
    assert sentinel_secret not in str(exc_root.value)
    assert str(symlink_home) not in str(exc_root.value)


def test_atomic_config_creation_race_collision_preserves_destination(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Assert atomic hard-link publication race does not overwrite or corrupt concurrently created destination."""
    mock_uv = create_mock_uv(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    layout.config_dir.mkdir(parents=True, exist_ok=True)
    config_file = layout.config_file

    valid_bootstrap_data = {
        "auto_update": False,
        "default_project": None,
        "projects": {},
    }
    concurrent_content = json.dumps(valid_bootstrap_data, indent=2, sort_keys=True) + "\n"

    real_link = os.link

    def race_simulating_link(src: str | Path, dst: str | Path) -> None:
        if Path(dst) == config_file:
            # Concurrently write the file before os.link is invoked
            config_file.write_text(concurrent_content, encoding="utf-8")
            os.chmod(config_file, 0o600)
        real_link(src, dst)

    monkeypatch.setattr(os, "link", race_simulating_link)

    def fake_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        layout.primary_executable.parent.mkdir(parents=True, exist_ok=True)
        layout.primary_executable.write_text("#!/bin/sh\necho basic-memory 0.23.2\n", encoding="utf-8")
        layout.primary_executable.chmod(0o755)
        return BasicMemoryRunnerResult(returncode=0, stdout="basic-memory 0.23.2\n", stderr="")

    # Execution detects existing safe destination upon collision and reuses it without overwriting
    res = install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=fake_runner)

    assert res.config_created is False
    assert config_file.read_text(encoding="utf-8") == concurrent_content
    # Temporary files are cleaned up
    assert len(list(layout.config_dir.glob(".tmp_config_*"))) == 0

    # Test collision when concurrent file is unsafe
    config_file.unlink()
    unsafe_content = "UNSAFE_CONCURRENT_CORRUPTED_JSON"

    def unsafe_race_link(src: str | Path, dst: str | Path) -> None:
        if Path(dst) == config_file:
            config_file.write_text(unsafe_content, encoding="utf-8")
        real_link(src, dst)

    monkeypatch.setattr(os, "link", unsafe_race_link)

    with pytest.raises(ConfigError) as exc_unsafe:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=fake_runner)

    assert "invalid json" in str(exc_unsafe.value).lower()
    # Concurrently created destination is preserved (not truncated or overwritten)
    assert config_file.read_text(encoding="utf-8") == unsafe_content
    assert len(list(layout.config_dir.glob(".tmp_config_*"))) == 0


def test_runner_side_effect_then_type_error_called_only_once(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert a runner performing a side-effect then raising TypeError is invoked strictly once without retry."""
    mock_uv = create_mock_uv(tmp_path)
    calls_count = 0

    def side_effect_type_error_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        nonlocal calls_count
        calls_count += 1
        raise TypeError("simulated runner side-effect followed by TypeError")

    with pytest.raises(RuntimeProbeError) as exc:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=side_effect_type_error_runner)

    assert calls_count == 1
    assert str(exc.value) == "Basic Memory installation failed."
    assert exc.value.__cause__ is None


def test_subprocess_output_bounded_by_utf8_bytes_without_breaking_unicode():
    """Assert runner result stdout and stderr are bounded by UTF-8 byte length without breaking multi-byte characters."""
    # 2-byte UTF-8 Cyrillic character: 'ж' is 2 bytes
    char = "ж"
    # String of 40,000 'ж' characters = 80,000 bytes > 65,536 bytes
    large_text = char * 40000
    res = BasicMemoryRunnerResult(returncode=0, stdout=large_text, stderr=large_text)

    stdout_bytes = res.stdout.encode("utf-8")
    stderr_bytes = res.stderr.encode("utf-8")
    assert len(stdout_bytes) <= MAX_SUBPROCESS_OUTPUT_BYTES
    assert len(stderr_bytes) <= MAX_SUBPROCESS_OUTPUT_BYTES

    # Ensure result is valid Unicode that decodes cleanly
    assert res.stdout.encode("utf-8").decode("utf-8") == res.stdout
    assert res.stderr.encode("utf-8").decode("utf-8") == res.stderr


def test_host_secret_env_excluded(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Assert unrelated host variables and secrets are completely stripped from child process environment."""
    mock_uv = create_mock_uv(tmp_path)

    # Inject sensitive and forbidden host variables
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-openai-token-1234")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-token-5678")
    monkeypatch.setenv("PTW_SECRET_KEY", "ptw-secret-val-999")
    monkeypatch.setenv("PYTHONPATH", "/malicious/pythonpath")
    monkeypatch.setenv("PYTHONHOME", "/malicious/pythonhome")
    monkeypatch.setenv("VIRTUAL_ENV", "/malicious/venv")
    monkeypatch.setenv("UV_CUSTOM_FLAG", "injected_flag")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.com:8080")
    monkeypatch.setenv("LANG", "en_US.UTF-8")

    recorded_envs: list[dict[str, str]] = []

    def capturing_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        recorded_envs.append(dict(env))
        layout = get_basic_memory_layout(personal_tideway_config)
        layout.primary_executable.parent.mkdir(parents=True, exist_ok=True)
        layout.primary_executable.write_text("#!/bin/sh\necho basic-memory 0.23.2\n", encoding="utf-8")
        layout.primary_executable.chmod(0o755)
        return BasicMemoryRunnerResult(returncode=0, stdout="basic-memory 0.23.2\n", stderr="")

    install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=capturing_runner)

    assert len(recorded_envs) > 0
    for env_dict in recorded_envs:
        # Sensitive and unlisted variables must be absent
        assert "OPENAI_API_KEY" not in env_dict
        assert "ANTHROPIC_API_KEY" not in env_dict
        assert "PTW_SECRET_KEY" not in env_dict
        assert "PYTHONPATH" not in env_dict
        assert "PYTHONHOME" not in env_dict
        assert "VIRTUAL_ENV" not in env_dict
        assert "UV_CUSTOM_FLAG" not in env_dict

        # Allowlisted proxy and locale are preserved
        assert env_dict.get("HTTP_PROXY") == "http://proxy.example.com:8080"
        assert env_dict.get("LANG") == "en_US.UTF-8"

        # Controlled overrides are present
        assert env_dict.get("BASIC_MEMORY_AUTO_UPDATE") == "false"
        assert env_dict.get("BASIC_MEMORY_NO_PROMOS") == "1"


def test_source_mapping_mutation_cannot_alter_runner_or_result(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert MappingProxyType and frozen dataclasses resist mutation attempts from caller or runner."""
    mock_uv = create_mock_uv(tmp_path)

    def mutating_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        # Attempt to mutate runner env
        with pytest.raises(TypeError):
            env["INJECTED_KEY"] = "val"  # type: ignore[index]
        layout = get_basic_memory_layout(personal_tideway_config)
        layout.primary_executable.parent.mkdir(parents=True, exist_ok=True)
        layout.primary_executable.write_text("#!/bin/sh\necho basic-memory 0.23.2\n", encoding="utf-8")
        layout.primary_executable.chmod(0o755)
        return BasicMemoryRunnerResult(returncode=0, stdout="basic-memory 0.23.2\n", stderr="")

    res = install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=mutating_runner)
    assert isinstance(res, BasicMemoryInstallResult)

    # Attempt to mutate frozen result fields
    with pytest.raises(Exception):
        res.version = "mutated"  # type: ignore[misc]
    with pytest.raises(Exception):
        res.healthy = False  # type: ignore[misc]

    # Attempt to mutate preview env_overrides
    assert res.preview is not None
    with pytest.raises(TypeError):
        res.preview.env_overrides["INJECTED"] = "1"  # type: ignore[index]

    # to_dict returns fresh defensive copy
    res_dict = res.to_dict()
    res_dict["version"] = "tampered"
    assert res.version == BASIC_MEMORY_PINNED_VERSION


def test_no_status_or_doctor_or_notes_argv(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert runner is never invoked with status, doctor, index, or notes commands."""
    mock_uv = create_mock_uv(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)

    all_argvs: list[tuple[str, ...]] = []

    def tracking_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        all_argvs.append(tuple(argv))
        if "--version" not in argv:
            layout.primary_executable.parent.mkdir(parents=True, exist_ok=True)
            layout.primary_executable.write_text("#!/bin/sh\necho basic-memory 0.23.2\n", encoding="utf-8")
            layout.primary_executable.chmod(0o755)
        return BasicMemoryRunnerResult(returncode=0, stdout="basic-memory 0.23.2\n", stderr="")

    install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=tracking_runner)

    for argv in all_argvs:
        # Inspect command subcommands and verbs, skipping argv[0] which contains filesystem path
        for arg in argv[1:]:
            lower = arg.lower()
            assert "status" not in lower
            assert "doctor" not in lower
            assert "index" not in lower
            assert "notes" not in lower


def test_operational_nonempty_projects_config_unchanged(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert existing config with non-empty projects mapping is accepted and bytes/mtime are unchanged."""
    mock_uv = create_mock_uv(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    layout.config_dir.mkdir(parents=True, exist_ok=True)
    config_file = layout.config_file

    custom_config_data = {
        "auto_update": False,
        "default_project": None,
        "projects": {
            "proj_alpha": {"path": "/foo/bar", "active": True},
            "proj_beta": {"path": "/baz/qux"},
        },
    }
    content = json.dumps(custom_config_data, indent=2, sort_keys=True) + "\n"
    config_file.write_text(content, encoding="utf-8")
    os.chmod(config_file, 0o600)

    # Pre-populate healthy binary
    layout.primary_executable.parent.mkdir(parents=True, exist_ok=True)
    layout.primary_executable.write_text("#!/bin/sh\necho basic-memory 0.23.2\n", encoding="utf-8")
    layout.primary_executable.chmod(0o755)

    bytes_before = config_file.read_bytes()
    mtime_before = config_file.stat().st_mtime_ns

    def healthy_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(returncode=0, stdout="basic-memory 0.23.2\n", stderr="")

    res = install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=healthy_runner)

    assert res.healthy is True
    assert res.config_created is False
    assert res.already_healthy is True
    assert config_file.read_bytes() == bytes_before
    assert config_file.stat().st_mtime_ns == mtime_before
    assert json.loads(config_file.read_text(encoding="utf-8")) == custom_config_data


def test_default_subprocess_runner_contracts(monkeypatch: pytest.MonkeyPatch):
    """Assert default_subprocess_runner enforces shell=False, exact timeout/env, and temporary file capture."""
    received_kwargs: dict[str, Any] = {}

    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        received_kwargs.update(kwargs)
        stdout_handle = kwargs.get("stdout")
        stderr_handle = kwargs.get("stderr")
        if stdout_handle is not None:
            stdout_handle.write("test stdout output\n".encode("utf-8"))
        if stderr_handle is not None:
            stderr_handle.write("test stderr output\n".encode("utf-8"))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=None, stderr=None)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    test_argv = ("uv", "--version")
    test_env = {"UV_TEST_KEY": "test_val"}
    test_timeout = 42.0

    res = default_subprocess_runner(test_argv, test_env, timeout=test_timeout)

    assert received_kwargs["shell"] is False
    assert received_kwargs["timeout"] == 42.0
    assert received_kwargs["env"] == test_env
    assert received_kwargs["check"] is False
    assert hasattr(received_kwargs["stdout"], "read")
    assert hasattr(received_kwargs["stderr"], "read")
    assert res.returncode == 0
    assert "test stdout output" in res.stdout
    assert "test stderr output" in res.stderr


def test_default_subprocess_runner_timeout_raises_timeout_error(monkeypatch: pytest.MonkeyPatch):
    """Assert default_subprocess_runner maps subprocess.TimeoutExpired to TimeoutError from None."""
    def timing_out_subprocess_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 10.0))

    monkeypatch.setattr(subprocess, "run", timing_out_subprocess_run)

    with pytest.raises(TimeoutError) as exc_info:
        default_subprocess_runner(("uv", "install"), {}, timeout=5.0)

    assert exc_info.value.__cause__ is None
    assert "timed out" in str(exc_info.value).lower()


def test_default_subprocess_runner_generic_exception_suppresses_cause(monkeypatch: pytest.MonkeyPatch):
    """Assert generic exceptions in default_subprocess_runner raise fixed RuntimeProbeError without secret leakage."""
    secret = "SUPER_SECRET_SUBPROCESS_TOKEN_999"

    def crashing_subprocess_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        raise OSError(f"Low level IO failure with secret: {secret}")

    monkeypatch.setattr(subprocess, "run", crashing_subprocess_run)

    with pytest.raises(RuntimeProbeError) as exc_info:
        default_subprocess_runner(("uv", "install"), {}, timeout=5.0)

    assert secret not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert str(exc_info.value) == "Subprocess execution failed."


def test_symlink_launcher_reaches_pre_health_and_skips_install(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert internal uv launcher symlink is accepted, passes pre-health, and skips install."""
    mock_uv = create_mock_uv(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)

    # 1. Realistic target inside layout.uv_tool_dir
    real_target_dir = layout.uv_tool_dir / "basic-memory" / "bin"
    real_target_dir.mkdir(parents=True, exist_ok=True)
    real_target_bin = real_target_dir / "basic-memory"
    real_target_bin.write_text("#!/bin/sh\necho basic-memory 0.23.2\n", encoding="utf-8")
    real_target_bin.chmod(0o755)

    # 2. Launcher symlink in layout.bin_dir pointing to real_target_bin
    layout.bin_dir.mkdir(parents=True, exist_ok=True)
    layout.primary_executable.symlink_to(real_target_bin)

    calls = []

    def tracking_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        calls.append(argv)
        return BasicMemoryRunnerResult(returncode=0, stdout="basic-memory 0.23.2\n", stderr="")

    res = install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=tracking_runner)

    assert res.already_healthy is True
    assert res.install_attempted is False
    assert res.healthy is True
    assert res.version == BASIC_MEMORY_PINNED_VERSION
    assert len(calls) == 1
    assert calls[0] == (str(layout.primary_executable), "--version")


def test_symlink_launcher_outside_target_rejected(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert launcher symlink pointing outside service_root is rejected with fixed error and runner is never invoked."""
    mock_uv = create_mock_uv(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)

    outside_bin = tmp_path / "outside_bin"
    outside_bin.write_text("#!/bin/sh\necho basic-memory 0.23.2\n", encoding="utf-8")
    outside_bin.chmod(0o755)

    layout.bin_dir.mkdir(parents=True, exist_ok=True)
    layout.primary_executable.symlink_to(outside_bin)

    calls: list[tuple[str, ...]] = []

    def runner_never_called(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        calls.append(tuple(argv))
        return BasicMemoryRunnerResult(returncode=0, stdout="", stderr="")

    with pytest.raises((RuntimeProbeError, BoundaryError)) as exc_info:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=runner_never_called)

    assert "outside" in str(exc_info.value).lower() or "symlink" in str(exc_info.value).lower()
    assert len(calls) == 0


def test_symlink_launcher_broken_target_rejected(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert broken internal launcher symlink is treated as unhealthy, self-heals via --force install, and asserts call order."""
    mock_uv = create_mock_uv(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)

    # Target in tool dir that does not exist yet
    target_in_tool = layout.uv_tool_dir / "bin" / "basic-memory"
    layout.bin_dir.mkdir(parents=True, exist_ok=True)
    layout.primary_executable.symlink_to(target_in_tool)
    assert not target_in_tool.exists()

    calls: list[tuple[str, ...]] = []

    def healing_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        calls.append(tuple(argv))
        if "--force" in argv:
            # Simulate repairing launcher/target inside tool dir during install
            target_in_tool.parent.mkdir(parents=True, exist_ok=True)
            target_in_tool.write_text("#!/bin/sh\necho basic-memory 0.23.2\n", encoding="utf-8")
            target_in_tool.chmod(0o755)
            return BasicMemoryRunnerResult(returncode=0, stdout="Installed basic-memory\n", stderr="")
        elif "--version" in argv:
            return BasicMemoryRunnerResult(returncode=0, stdout="basic-memory 0.23.2\n", stderr="")
        return BasicMemoryRunnerResult(returncode=1, stdout="", stderr="unexpected invocation")

    res = install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=healing_runner)

    assert res.healthy is True
    assert res.install_attempted is True
    assert res.already_healthy is False
    assert res.version == BASIC_MEMORY_PINNED_VERSION

    # Assert pre-health is skipped and install --force is invoked once, followed by post-health
    assert len(calls) == 2
    expected_install_argv = (
        str(mock_uv),
        "--no-config",
        "tool",
        "install",
        "--force",
        "--prerelease=allow",
        BASIC_MEMORY_REQUIREMENT,
    )
    assert calls[0] == expected_install_argv
    assert calls[1] == (str(layout.primary_executable), "--version")


def test_symlink_launcher_broken_target_post_install_still_invalid_fails(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert that if post-install launcher remains broken, installation fails with fixed safe error."""
    mock_uv = create_mock_uv(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)

    target_in_tool = layout.uv_tool_dir / "bin" / "basic-memory"
    layout.bin_dir.mkdir(parents=True, exist_ok=True)
    layout.primary_executable.symlink_to(target_in_tool)

    def failing_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(returncode=0, stdout="Done", stderr="")

    with pytest.raises(RuntimeProbeError) as exc_info:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=failing_runner)

    assert "missing or invalid after installation" in str(exc_info.value).lower()


def test_concurrency_lock_acquired_and_released_on_exit(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert execution serializes via 0600 lock file and releases lock on exit even upon exceptions."""
    mock_uv = create_mock_uv(tmp_path)
    lock_file = personal_tideway_config.locks_dir / "basic-memory-install.lock"

    lock_was_held = False

    def runner_checking_lock(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        nonlocal lock_was_held
        assert lock_file.exists()
        fd2 = os.open(lock_file, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_was_held = False
            except (BlockingIOError, OSError):
                lock_was_held = True
        finally:
            os.close(fd2)
        raise RuntimeError("Simulated runner failure to check lock release on exception")

    with pytest.raises(RuntimeProbeError):
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv, runner=runner_checking_lock)

    assert lock_was_held is True
    # Lock is now fully released: non-blocking lock should succeed
    fd3 = os.open(lock_file, os.O_RDWR)
    try:
        fcntl.flock(fd3, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd3, fcntl.LOCK_UN)
    finally:
        os.close(fd3)

    assert (lock_file.stat().st_mode & 0o777) == 0o600


def test_concurrency_lock_dry_run_no_lock_and_no_mutation(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert dry-run acquires no lock, does not create the lock file, and causes zero filesystem mutation."""
    mock_uv = create_mock_uv(tmp_path)
    lock_file = personal_tideway_config.locks_dir / "basic-memory-install.lock"

    before_snapshot = snapshot_filesystem(personal_tideway_config.home)

    res = install_basic_memory(personal_tideway_config, uv_executable=mock_uv, dry_run=True)
    assert res.dry_run is True

    after_snapshot = snapshot_filesystem(personal_tideway_config.home)
    assert before_snapshot == after_snapshot
    assert not lock_file.exists()


def test_existing_config_fifo_and_symlink_safe_handling(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert existing config FIFO and symlink are safely rejected without blocking, modifying target, or leaking sentinels."""
    mock_uv = create_mock_uv(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    layout.config_dir.mkdir(parents=True, exist_ok=True)
    config_file = layout.config_file

    # 1. FIFO: rejected as non-regular file without blocking
    if config_file.exists():
        config_file.unlink()
    os.mkfifo(config_file)
    with pytest.raises(ConfigError) as exc_fifo:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)
    assert "not a regular file" in str(exc_fifo.value).lower()

    # 2. Symlink: rejected safely as BoundaryError or ConfigError without leaking sentinel or modifying target
    config_file.unlink()
    sentinel = "SECRET_CANARY_CONFIG_TARGET_12345"
    dummy = tmp_path / f"dummy_{sentinel}.json"
    dummy_content = f'{{"canary": "{sentinel}"}}'
    dummy.write_text(dummy_content, encoding="utf-8")
    config_file.symlink_to(dummy)

    with pytest.raises((BoundaryError, ConfigError)) as exc_sym:
        install_basic_memory(personal_tideway_config, uv_executable=mock_uv)

    assert "symlink" in str(exc_sym.value).lower()
    assert sentinel not in str(exc_sym.value)
    assert str(dummy) not in str(exc_sym.value)
    assert dummy.read_text(encoding="utf-8") == dummy_content


def test_parse_basic_memory_version_variants_and_unrelated_ignored():
    """Assert version parsing extracts Basic Memory versions, handles variants, and ignores unrelated tools."""
    # 1. Standard format
    assert parse_basic_memory_version("basic-memory 0.23.2\n") == "0.23.2"

    # 2. Comma version format
    assert parse_basic_memory_version("basic-memory, version 0.23.2\n") == "0.23.2"

    # 3. CLI v format
    assert parse_basic_memory_version("Basic Memory CLI v0.23.2\n") == "0.23.2"

    # 4. Short alias format
    assert parse_basic_memory_version("bm 0.23.2\n") == "0.23.2"

    # 5. Combined output with unrelated tools earlier
    combined = "Python 3.12.2\nuv 0.5.1\nbasic-memory 0.23.2\n"
    assert parse_basic_memory_version(combined) == "0.23.2"

    # 6. Combined split across stdout and stderr
    assert parse_basic_memory_version("Python 3.12.2\n", "basic-memory 0.23.2\n") == "0.23.2"

    # 7. Unrelated standalone version without label is rejected
    assert parse_basic_memory_version("0.23.2\n") is None

    # 8. Unrelated tool with version is rejected
    assert parse_basic_memory_version("uv 0.23.2\n") is None
    assert parse_basic_memory_version("python 0.23.2\n") is None


def test_parse_basic_memory_version_exact_real_smoke_output_regression():
    """Regression test: parse exact real output from Codex smoke test and nearby punctuation variants."""
    # Exact real output from Basic Memory 0.23.2 install smoke test
    exact_output = "Basic Memory version: 0.23.2"
    assert parse_basic_memory_version(exact_output) == "0.23.2"
    assert parse_basic_memory_version(f"{exact_output}\n") == "0.23.2"

    # Reasonable nearby punctuation variants
    assert parse_basic_memory_version("Basic Memory version: v0.23.2\n") == "0.23.2"
    assert parse_basic_memory_version("Basic Memory, version: 0.23.2\n") == "0.23.2"
    assert parse_basic_memory_version("Basic Memory: version: 0.23.2\n") == "0.23.2"
    assert parse_basic_memory_version("Basic Memory version - 0.23.2\n") == "0.23.2"
    assert parse_basic_memory_version("basic-memory version: 0.23.2\n") == "0.23.2"
    assert parse_basic_memory_version("basic-memory - 0.23.2\n") == "0.23.2"
    assert parse_basic_memory_version("bm version: 0.23.2\n") == "0.23.2"

    # Ensure rejection of unrelated versions remains strict and label-anchored
    assert parse_basic_memory_version("version: 0.23.2\n") is None
    assert parse_basic_memory_version("uv version: 0.23.2\n") is None
    assert parse_basic_memory_version("python version: 0.23.2\n") is None


def test_install_basic_memory_accepts_exact_real_version_output(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert install_basic_memory succeeds when binary emits exact real version output."""
    mock_uv = create_mock_uv(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)

    def fake_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        if "--version" in argv:
            return BasicMemoryRunnerResult(returncode=0, stdout="Basic Memory version: 0.23.2\n", stderr="")
        layout.primary_executable.parent.mkdir(parents=True, exist_ok=True)
        layout.primary_executable.write_text("#!/bin/sh\necho 'Basic Memory version: 0.23.2'\n", encoding="utf-8")
        layout.primary_executable.chmod(0o755)
        return BasicMemoryRunnerResult(returncode=0, stdout="Installed basic-memory\n", stderr="")

    res = install_basic_memory(
        personal_tideway_config,
        uv_executable=mock_uv,
        dry_run=False,
        runner=fake_runner,
    )

    assert res.healthy is True
    assert res.version == BASIC_MEMORY_PINNED_VERSION


def test_validate_isolated_executable_symlinked_ancestor_path_regression(tmp_path: Path):
    """Assert validate_isolated_executable resolves canonical boundaries when home_boundary has symlinked ancestor."""
    real_ancestor = tmp_path / "real_ancestor"
    real_ancestor.mkdir()

    sym_ancestor = tmp_path / "sym_ancestor"
    sym_ancestor.symlink_to(real_ancestor)

    # Home boundary path constructed through symlink ancestor
    home_boundary = sym_ancestor / "ptw_home"
    home_boundary.mkdir()

    service_root = home_boundary / "services" / "basic-memory"
    bin_dir = service_root / "bin"
    tool_bin_dir = service_root / "tool" / "bin"
    tool_bin_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    real_bin = tool_bin_dir / "basic-memory"
    real_bin.write_text("#!/bin/sh\necho basic-memory 0.23.2\n", encoding="utf-8")
    real_bin.chmod(0o755)

    launcher = bin_dir / "basic-memory"
    launcher.symlink_to(real_bin)

    # validate_isolated_executable must succeed through canonical boundary comparison
    assert validate_isolated_executable(launcher, service_root=service_root, home_boundary=home_boundary) is True

    # Outside target through symlink ancestor is still rejected with fixed error
    outside_bin = tmp_path / "outside_tool" / "bin" / "basic-memory"
    outside_bin.parent.mkdir(parents=True, exist_ok=True)
    outside_bin.write_text("#!/bin/sh\necho 1\n", encoding="utf-8")
    outside_bin.chmod(0o755)

    bad_launcher = bin_dir / "bad_bm"
    bad_launcher.symlink_to(outside_bin)
    with pytest.raises(RuntimeProbeError) as exc_outside:
        validate_isolated_executable(bad_launcher, service_root=service_root, home_boundary=home_boundary)
    assert "outside" in str(exc_outside.value).lower()
    assert str(tmp_path) not in str(exc_outside.value)


def test_concurrency_lock_mkdir_race_converges(
    personal_tideway_config: PersonalTidewayConfig,
    monkeypatch: pytest.MonkeyPatch,
):
    """Assert concurrent FileExists during lock directory creation converges to inspecting directory and acquiring lock."""
    real_mkdir = Path.mkdir
    race_simulated = False

    def race_mkdir(self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False):
        nonlocal race_simulated
        if self == personal_tideway_config.locks_dir and not race_simulated:
            race_simulated = True
            real_mkdir(self, mode=0o700, parents=parents, exist_ok=True)
            raise FileExistsError(f"Directory already exists: {self}")
        return real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", race_mkdir)

    if personal_tideway_config.locks_dir.exists():
        shutil.rmtree(personal_tideway_config.locks_dir)

    with acquire_basic_memory_lock(personal_tideway_config):
        lock_file = personal_tideway_config.locks_dir / "basic-memory-install.lock"
        assert lock_file.exists()
        assert (personal_tideway_config.locks_dir.stat().st_mode & 0o777) == 0o700

    assert race_simulated is True


def test_concurrency_lock_post_flock_replacement_rejected_and_released(
    personal_tideway_config: PersonalTidewayConfig,
    monkeypatch: pytest.MonkeyPatch,
):
    """Assert lock file replacement or unlinking during flock acquisition is rejected with fixed ConfigError and releases lock."""
    lock_file = personal_tideway_config.locks_dir / "basic-memory-install.lock"
    personal_tideway_config.locks_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    original_flock = fcntl.flock
    replacement_triggered = False

    def mutating_flock(fd: int, operation: int):
        nonlocal replacement_triggered
        original_flock(fd, operation)
        if operation == fcntl.LOCK_EX and not replacement_triggered:
            replacement_triggered = True
            if lock_file.exists():
                lock_file.unlink()
            new_fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
            os.close(new_fd)

    monkeypatch.setattr(fcntl, "flock", mutating_flock)

    with pytest.raises(ConfigError) as exc_info:
        with acquire_basic_memory_lock(personal_tideway_config):
            pytest.fail("Should not yield when lock file was replaced on disk")

    assert "replaced or removed" in str(exc_info.value).lower()
    assert replacement_triggered is True

    # Verify lock on new file is not blocked (lock was properly released)
    test_fd = os.open(lock_file, os.O_RDWR)
    try:
        fcntl.flock(test_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(test_fd, fcntl.LOCK_UN)
    finally:
        os.close(test_fd)
