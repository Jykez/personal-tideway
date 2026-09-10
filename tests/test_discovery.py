"""Tests for Codex client discovery, pre-init status, diagnostics, and doctor command."""

import json
import os
from pathlib import Path
import subprocess
import pytest

from personal_tideway.cli.main import main
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import ExitCode
from personal_tideway.core.discovery import (
    discover_agy,
    discover_codex,
    format_client_agy_text,
    format_client_codex_text,
    format_doctor_text,
    run_doctor,
)
from personal_tideway.core.status import get_workspace_status


def test_discovery_missing_executable(tmp_path: Path):
    """Test discovery when Codex executable is not present in PATH."""
    res = discover_codex(
        codex_home=tmp_path,
        executable_resolver=lambda _: None,
    )
    assert res.installed is False
    assert res.executable is None
    assert res.version is None

    check_ids = {c.id for c in res.checks}
    assert "codex_executable" in check_ids
    assert "codex_version" in check_ids

    exe_check = next(c for c in res.checks if c.id == "codex_executable")
    assert exe_check.status == "warning"
    assert exe_check.remediation is not None
    assert "PATH" in exe_check.message

    ver_check = next(c for c in res.checks if c.id == "codex_version")
    assert ver_check.status == "warning"

    # Overall status should be warning
    assert res.status == "warning"


def test_discovery_successful_version(tmp_path: Path):
    """Test discovery with present executable and successful version probe."""
    fake_exe = "/opt/tools/codex"

    def fake_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
        assert cmd == [fake_exe, "--version"]
        assert timeout > 0
        return 0, "codex-cli 0.45.2 (build 20260901)\n", ""

    res = discover_codex(
        codex_home=tmp_path,
        executable_resolver=lambda _: fake_exe,
        version_runner=fake_runner,
    )
    assert res.installed is True
    assert res.executable == fake_exe
    assert res.version == "codex-cli 0.45.2 (build 20260901)"

    ver_check = next(c for c in res.checks if c.id == "codex_version")
    assert ver_check.status == "ok"
    assert "0.45.2" in ver_check.message


def test_discovery_version_timeout(tmp_path: Path):
    """Test version probe timeout produces a bounded error check."""
    fake_exe = "/opt/tools/codex"

    def fake_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    res = discover_codex(
        codex_home=tmp_path,
        executable_resolver=lambda _: fake_exe,
        version_runner=fake_runner,
        timeout=1.5,
    )
    assert res.installed is True
    assert res.version is None
    assert res.status == "error"

    ver_check = next(c for c in res.checks if c.id == "codex_version")
    assert ver_check.status == "error"
    assert "Timeout" in ver_check.message
    assert ver_check.remediation is not None


def test_discovery_version_nonzero_failure(tmp_path: Path):
    """Test version probe non-zero exit code produces an error check."""
    fake_exe = "/opt/tools/codex"

    def fake_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
        return 127, "", "command not found"

    res = discover_codex(
        codex_home=tmp_path,
        executable_resolver=lambda _: fake_exe,
        version_runner=fake_runner,
    )
    assert res.installed is True
    assert res.version is None
    assert res.status == "error"

    ver_check = next(c for c in res.checks if c.id == "codex_version")
    assert ver_check.status == "error"
    assert "exit code 127" in ver_check.message


def test_discovery_valid_config_no_value_leakage(tmp_path: Path):
    """Test valid config.toml parses without leaking keys or values."""
    secret_value = "secret_auth_token_987654321"
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[mcp_servers.demo]\ncommand = "run"\napi_key = "{secret_value}"\n',
        encoding="utf-8",
    )

    res = discover_codex(
        codex_home=tmp_path,
        executable_resolver=lambda _: None,
    )
    cfg_check = next(c for c in res.checks if c.id == "codex_config")
    assert cfg_check.status == "ok"
    assert "valid TOML" in cfg_check.message

    dumped = json.dumps(res.to_dict())
    assert secret_value not in dumped
    assert "demo" not in dumped


def test_discovery_malformed_config_no_value_leakage(tmp_path: Path):
    """Test malformed config.toml produces error without exposing file contents or raw trace."""
    secret_value = "confidential_credential_val"
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'api_token = "{secret_value}"\nMALFORMED TOML SYNTAX === [[]]\n',
        encoding="utf-8",
    )

    res = discover_codex(
        codex_home=tmp_path,
        executable_resolver=lambda _: None,
    )
    cfg_check = next(c for c in res.checks if c.id == "codex_config")
    assert cfg_check.status == "error"
    assert "invalid TOML syntax" in cfg_check.message
    assert cfg_check.remediation is not None

    dumped = json.dumps(res.to_dict())
    assert secret_value not in dumped
    assert "MALFORMED TOML SYNTAX" not in dumped
    assert "[[]]" not in dumped


def test_discovery_override_shadowing(tmp_path: Path):
    """Test non-empty AGENTS.override.md is effective and emits a shadowing warning."""
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("# Managed rules\nDo things safely.\n", encoding="utf-8")

    override_md = tmp_path / "AGENTS.override.md"
    override_md.write_text("# Custom Override\nDo whatever.\n", encoding="utf-8")

    res = discover_codex(
        codex_home=tmp_path,
        executable_resolver=lambda _: None,
    )
    assert res.paths["effective_global_rules"] == str(override_md.resolve())

    rules_check = next(c for c in res.checks if c.id == "codex_rules")
    assert rules_check.status == "warning"
    assert "shadows managed AGENTS.md" in rules_check.message
    assert rules_check.remediation is not None

    dumped = json.dumps(res.to_dict())
    assert "Do things safely" not in dumped
    assert "Do whatever" not in dumped


def test_discovery_empty_override_fallback(tmp_path: Path):
    """Test empty AGENTS.override.md falls back to AGENTS.md without shadowing warning."""
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("# Canonical Rules\nFollow guidelines.\n", encoding="utf-8")

    override_md = tmp_path / "AGENTS.override.md"
    # Write only whitespace
    override_md.write_text("   \n\n\t  \n", encoding="utf-8")

    res = discover_codex(
        codex_home=tmp_path,
        executable_resolver=lambda _: None,
    )
    assert res.paths["effective_global_rules"] == str(agents_md.resolve())

    rules_check = next(c for c in res.checks if c.id == "codex_rules")
    assert rules_check.status == "ok"
    assert "AGENTS.override.md is empty; using AGENTS.md" in rules_check.message


def test_discovery_canonical_and_legacy_skills(tmp_path: Path):
    """Test diagnosis of canonical vs legacy skills paths."""
    codex_home = tmp_path / "codex"
    codex_home.mkdir(parents=True)
    canonical_skills = tmp_path / "canonical_skills"

    # Case 1: Legacy skills directory exists -> warning
    legacy_skills = codex_home / "skills"
    legacy_skills.mkdir(parents=True)

    res = discover_codex(
        codex_home=codex_home,
        canonical_user_skills=canonical_skills,
        executable_resolver=lambda _: None,
    )
    assert res.paths["canonical_user_skills"] == str(canonical_skills.resolve())
    assert res.paths["legacy_skills"] == str(legacy_skills.resolve())

    skill_check = next(c for c in res.checks if c.id == "codex_skills")
    assert skill_check.status == "warning"
    assert "Legacy skills directory found" in skill_check.message
    assert skill_check.remediation is not None

    # Case 2: No legacy skills directory -> ok
    other_home = tmp_path / "other_codex"
    other_home.mkdir(parents=True)
    res2 = discover_codex(
        codex_home=other_home,
        canonical_user_skills=canonical_skills,
        executable_resolver=lambda _: None,
    )
    skill_check2 = next(c for c in res2.checks if c.id == "codex_skills")
    assert skill_check2.status == "ok"


def test_discovery_never_opens_auth_json(tmp_path: Path, monkeypatch):
    """Verify that discovery never attempts to open or inspect auth.json."""
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"token": "super_secret_never_read"}', encoding="utf-8")

    real_open = open

    def guarded_open(file, *args, **kwargs):
        if "auth.json" in str(file):
            raise AssertionError("Security violation: auth.json must never be opened!")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)

    res = discover_codex(
        codex_home=tmp_path,
        executable_resolver=lambda _: None,
    )
    assert res is not None


def test_discovery_performs_no_writes(tmp_path: Path):
    """Verify that discover_codex and run_doctor perform no write side-effects."""
    codex_home = tmp_path / "codex"
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text('model = "o3"\n', encoding="utf-8")
    (codex_home / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")

    # Capture initial tree state
    def get_snapshot(root: Path):
        snapshot = {}
        for p in root.rglob("*"):
            stat = p.stat()
            snapshot[str(p.relative_to(root))] = (stat.st_size, stat.st_mtime_ns)
        return snapshot

    before = get_snapshot(tmp_path)

    # Execute discovery and doctor multiple times
    res = discover_codex(codex_home=codex_home)
    report = run_doctor(codex_home=codex_home)

    after = get_snapshot(tmp_path)
    assert before == after


def test_cli_pre_init_status_text_and_json(tmp_path: Path, capsys, monkeypatch):
    """Test 'ptw status [--json]' works before PTW initialization and reports clients.codex."""
    uninit_home = tmp_path / "empty_ptw"
    uninit_home.mkdir(parents=True)
    codex_home = tmp_path / "codex"
    codex_home.mkdir(parents=True)

    fake_exe = "/usr/local/bin/codex"
    monkeypatch.setattr("shutil.which", lambda _: fake_exe)
    monkeypatch.setattr(
        "personal_tideway.core.discovery._default_version_runner",
        lambda cmd, timeout: (0, "codex 0.88.0\n", ""),
    )

    # 1. Test Text output before init
    code = main([
        "--home", str(uninit_home),
        "--codex-home", str(codex_home),
        "status",
    ])
    assert code == ExitCode.SUCCESS

    captured = capsys.readouterr()
    assert "Personal Tideway workspace is NOT initialized" in captured.out
    assert "=== Client: Codex ===" in captured.out
    assert "Installed: True" in captured.out
    assert "codex 0.88.0" in captured.out

    # 2. Test JSON output before init
    code_json = main([
        "--home", str(uninit_home),
        "--codex-home", str(codex_home),
        "status",
        "--json",
    ])
    assert code_json == ExitCode.SUCCESS

    captured_json = capsys.readouterr()
    data = json.loads(captured_json.out)
    assert data["initialized"] is False
    assert "clients" in data
    assert "codex" in data["clients"]
    assert "agy" in data["clients"]
    codex_data = data["clients"]["codex"]
    assert codex_data["installed"] is True
    assert codex_data["version"] == "codex 0.88.0"
    assert codex_data["paths"]["codex_home"] == str(codex_home.resolve())
    assert len(codex_data["checks"]) >= 5


def test_cli_pre_init_doctor_text_and_json(tmp_path: Path, capsys, monkeypatch):
    """Test read-only 'ptw doctor [--json]' before initialization."""
    uninit_home = tmp_path / "empty_ptw"
    uninit_home.mkdir(parents=True)
    codex_home = tmp_path / "codex"
    codex_home.mkdir(parents=True)

    monkeypatch.setattr("shutil.which", lambda _: None)

    # 1. Text output
    code = main([
        "--home", str(uninit_home),
        "--codex-home", str(codex_home),
        "doctor",
    ])
    # Warnings do not fail doctor
    assert code == ExitCode.SUCCESS

    captured = capsys.readouterr()
    assert "=== Personal Tideway Doctor ===" in captured.out
    assert "Coverage: Codex + AGY" in captured.out
    assert "Overall Status: WARNING" in captured.out
    assert "[WARNING] codex_executable" in captured.out
    assert "[WARNING] agy_executable" in captured.out

    # 2. JSON output
    code_json = main([
        "--home", str(uninit_home),
        "--codex-home", str(codex_home),
        "doctor",
        "--json",
    ])
    assert code_json == ExitCode.SUCCESS

    captured_json = capsys.readouterr()
    data = json.loads(captured_json.out)
    assert data["coverage"] == "Codex + AGY"
    assert data["status"] == "warning"
    assert "clients" in data
    assert "codex" in data["clients"]
    assert "agy" in data["clients"]
    assert isinstance(data["checks"], list)


def test_cli_doctor_runtime_probe_error_exit_code(tmp_path: Path, monkeypatch):
    """Test that actual probe or syntax errors cause doctor to return RUNTIME_PROBE_ERROR."""
    uninit_home = tmp_path / "empty_ptw"
    uninit_home.mkdir(parents=True)
    codex_home = tmp_path / "codex"
    codex_home.mkdir(parents=True)

    fake_exe = "/usr/bin/codex"
    monkeypatch.setattr("shutil.which", lambda _: fake_exe)
    monkeypatch.setattr(
        "personal_tideway.core.discovery._default_version_runner",
        lambda cmd, timeout: (1, "", "probe failed"),
    )

    code = main([
        "--home", str(uninit_home),
        "--codex-home", str(codex_home),
        "doctor",
    ])
    assert code == ExitCode.RUNTIME_PROBE_ERROR


def test_status_initialized_includes_codex_clients(personal_tideway_config: PersonalTidewayConfig):
    """Test get_workspace_status on an initialized workspace contains clients.codex and clients.agy."""
    status = get_workspace_status(personal_tideway_config)
    assert status["initialized"] is True
    assert "clients" in status
    assert "codex" in status["clients"]
    assert "agy" in status["clients"]
    assert "paths" in status["clients"]["codex"]
    assert "paths" in status["clients"]["agy"]
    assert status["clients"]["codex"]["paths"]["codex_home"] == str(personal_tideway_config.codex_home.resolve())
    assert status["clients"]["agy"]["paths"]["gemini_home"] == str(personal_tideway_config.gemini_home.resolve())


def test_agy_discovery_missing_executable(tmp_path: Path):
    """Test AGY discovery when executable is not present in PATH."""
    res = discover_agy(
        gemini_home=tmp_path,
        executable_resolver=lambda _: None,
    )
    assert res.installed is False
    assert res.executable is None
    assert res.version is None

    check_ids = {c.id for c in res.checks}
    assert "agy_executable" in check_ids
    assert "agy_version" in check_ids
    assert "agy_cli_settings" in check_ids
    assert "agy_mcp_config" in check_ids
    assert "agy_rules" in check_ids
    assert "agy_skills" in check_ids
    assert "agy_portable_skills_alias" in check_ids
    assert "agy_legacy_gemini_md" in check_ids
    assert "agy_legacy_skills" in check_ids

    exe_check = next(c for c in res.checks if c.id == "agy_executable")
    assert exe_check.status == "warning"
    assert exe_check.remediation is not None
    assert "PATH" in exe_check.message

    ver_check = next(c for c in res.checks if c.id == "agy_version")
    assert ver_check.status == "warning"

    assert res.status == "warning"


def test_agy_discovery_successful_version(tmp_path: Path):
    """Test AGY discovery with present executable and successful version probe."""
    fake_exe = "/opt/tools/agy"

    def fake_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
        assert cmd == [fake_exe, "--version"]
        assert timeout > 0
        return 0, "agy 1.1.28 (build 20260901)\n", ""

    res = discover_agy(
        gemini_home=tmp_path,
        executable_resolver=lambda _: fake_exe,
        version_runner=fake_runner,
    )
    assert res.installed is True
    assert res.executable == fake_exe
    assert res.version == "agy 1.1.28 (build 20260901)"

    ver_check = next(c for c in res.checks if c.id == "agy_version")
    assert ver_check.status == "ok"
    assert "1.1.28" in ver_check.message


def test_agy_discovery_version_timeout_and_nonzero(tmp_path: Path):
    """Test AGY version probe timeout and non-zero exit produce bounded error checks."""
    fake_exe = "/opt/tools/agy"

    def timeout_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    res_t = discover_agy(
        gemini_home=tmp_path,
        executable_resolver=lambda _: fake_exe,
        version_runner=timeout_runner,
        timeout=1.0,
    )
    assert res_t.installed is True
    assert res_t.version is None
    assert res_t.status == "error"
    ver_t = next(c for c in res_t.checks if c.id == "agy_version")
    assert ver_t.status == "error"
    assert "Timeout" in ver_t.message

    def nonzero_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
        return 127, "", "command not found"

    res_nz = discover_agy(
        gemini_home=tmp_path,
        executable_resolver=lambda _: fake_exe,
        version_runner=nonzero_runner,
    )
    assert res_nz.installed is True
    assert res_nz.version is None
    assert res_nz.status == "error"
    ver_nz = next(c for c in res_nz.checks if c.id == "agy_version")
    assert ver_nz.status == "error"
    assert "exit code 127" in ver_nz.message


def test_agy_discovery_valid_and_malformed_json_no_leakage(tmp_path: Path):
    """Test valid and malformed JSON validation without leaking keys, values, or raw exceptions."""
    secret_key = "super_secret_agy_token_456"
    gemini_home = tmp_path / "gemini"
    settings_file = gemini_home / "antigravity-cli" / "settings.json"
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps({"token": secret_key, "nested": {"key": "value"}}), encoding="utf-8")

    mcp_file = gemini_home / "config" / "mcp_config.json"
    mcp_file.parent.mkdir(parents=True, exist_ok=True)
    mcp_file.write_text(json.dumps({"mcpServers": {"demo": {"key": secret_key}}}), encoding="utf-8")

    res = discover_agy(
        gemini_home=gemini_home,
        executable_resolver=lambda _: None,
    )
    st_check = next(c for c in res.checks if c.id == "agy_cli_settings")
    assert st_check.status == "ok"
    assert "valid JSON" in st_check.message

    mcp_check = next(c for c in res.checks if c.id == "agy_mcp_config")
    assert mcp_check.status == "ok"
    assert "valid JSON" in mcp_check.message

    dumped = json.dumps(res.to_dict())
    assert secret_key not in dumped
    assert "nested" not in dumped
    assert "mcpServers" not in dumped

    # Now test malformed JSON
    bad_secret = "confidential_syntax_token"
    settings_file.write_text(f'{{"auth": "{bad_secret}", MALFORMED JSON ::: [', encoding="utf-8")
    mcp_file.write_text(f'{{"auth": "{bad_secret}", BAD SYNTAX === {{', encoding="utf-8")

    res_bad = discover_agy(
        gemini_home=gemini_home,
        executable_resolver=lambda _: None,
    )
    assert res_bad.status == "error"

    bad_st = next(c for c in res_bad.checks if c.id == "agy_cli_settings")
    assert bad_st.status == "error"
    assert "invalid JSON syntax" in bad_st.message
    assert bad_st.remediation is not None

    bad_mcp = next(c for c in res_bad.checks if c.id == "agy_mcp_config")
    assert bad_mcp.status == "error"
    assert "invalid JSON syntax" in bad_mcp.message
    assert bad_mcp.remediation is not None

    dumped_bad = json.dumps(res_bad.to_dict())
    assert bad_secret not in dumped_bad
    assert "MALFORMED JSON" not in dumped_bad
    assert "BAD SYNTAX" not in dumped_bad


def test_agy_discovery_current_paths_and_rules_candidates(tmp_path: Path):
    """Test paths reporting and rule candidates detection under customization root."""
    gemini_home = tmp_path / "gemini"
    custom_root = gemini_home / "config"
    rules_dir = custom_root / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "my_rule.md").write_text("# Custom Rule\n", encoding="utf-8")
    (custom_root / "AGENTS.md").write_text("# Standalone Agents\n", encoding="utf-8")

    portable_skills = tmp_path / "portable_skills"
    portable_skills.mkdir(parents=True, exist_ok=True)

    res = discover_agy(
        gemini_home=gemini_home,
        customization_root=custom_root,
        portable_skills_alias=portable_skills,
        executable_resolver=lambda _: None,
    )

    assert res.paths["gemini_home"] == str(gemini_home.resolve())
    assert res.paths["customization_root"] == str(custom_root.resolve())
    assert res.paths["cli_settings"] == str((gemini_home / "antigravity-cli" / "settings.json").resolve())
    assert res.paths["mcp_config"] == str((custom_root / "mcp_config.json").resolve())
    assert res.paths["rules_root"] == str(rules_dir.resolve())
    assert str(rules_dir.resolve()) in res.paths["rule_candidates"]
    assert str((custom_root / "AGENTS.md").resolve()) in res.paths["rule_candidates"]
    assert str((custom_root / "GEMINI.md").resolve()) in res.paths["rule_candidates"]
    assert res.paths["current_skills"] == str((custom_root / "skills").resolve())
    assert res.paths["portable_skills_alias"] == str(portable_skills.resolve())
    assert res.paths["legacy_gemini_md"] == str((gemini_home / "GEMINI.md").resolve())
    assert res.paths["legacy_skills"] == str((gemini_home / "skills").resolve())

    # Rules check must report candidates without claiming one is effective
    rules_check = next(c for c in res.checks if c.id == "agy_rules")
    assert rules_check.status == "ok"
    assert "rules/" in rules_check.message
    assert "AGENTS.md" in rules_check.message
    assert "no safe precedence" in rules_check.message.lower()

    dumped = json.dumps(res.to_dict())
    assert "Custom Rule" not in dumped
    assert "Standalone Agents" not in dumped


def test_agy_discovery_legacy_warnings(tmp_path: Path):
    """Test warning emitted when obsolete v1 GEMINI.md or skills paths exist."""
    gemini_home = tmp_path / "gemini"
    gemini_home.mkdir(parents=True, exist_ok=True)
    legacy_gemini = gemini_home / "GEMINI.md"
    legacy_gemini.write_text("# Legacy global\n", encoding="utf-8")
    legacy_skills = gemini_home / "skills"
    legacy_skills.mkdir(parents=True, exist_ok=True)

    res = discover_agy(
        gemini_home=gemini_home,
        executable_resolver=lambda _: None,
    )
    assert res.status == "warning"

    g_check = next(c for c in res.checks if c.id == "agy_legacy_gemini_md")
    assert g_check.status == "warning"
    assert "Legacy global GEMINI.md found" in g_check.message
    assert "manual" in g_check.remediation.lower()
    assert "not performed" in g_check.remediation.lower()

    s_check = next(c for c in res.checks if c.id == "agy_legacy_skills")
    assert s_check.status == "warning"
    assert "Legacy skills directory found" in s_check.message
    assert "manual" in s_check.remediation.lower()
    assert "not performed" in s_check.remediation.lower()


def test_agy_discovery_no_writes_no_auth_access(tmp_path: Path, monkeypatch):
    """Verify that discover_agy performs no filesystem writes and never opens auth files."""
    gemini_home = tmp_path / "gemini"
    gemini_home.mkdir(parents=True, exist_ok=True)
    (gemini_home / "auth.json").write_text('{"token": "secret"}', encoding="utf-8")
    cli_dir = gemini_home / "antigravity-cli"
    cli_dir.mkdir(parents=True, exist_ok=True)
    (cli_dir / "auth.json").write_text('{"token": "secret2"}', encoding="utf-8")
    (cli_dir / "settings.json").write_text('{"initialized": true}', encoding="utf-8")

    real_open = open

    def guarded_open(file, *args, **kwargs):
        if "auth.json" in str(file):
            raise AssertionError("Security violation: auth.json must never be opened!")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)

    def get_snapshot(root: Path):
        snapshot = {}
        for p in root.rglob("*"):
            stat = p.stat()
            snapshot[str(p.relative_to(root))] = (stat.st_size, stat.st_mtime_ns)
        return snapshot

    before = get_snapshot(tmp_path)
    res = discover_agy(gemini_home=gemini_home, executable_resolver=lambda _: None)
    after = get_snapshot(tmp_path)

    assert before == after
    assert res is not None


def test_cli_status_and_doctor_both_clients_text_and_json(tmp_path: Path, capsys, monkeypatch):
    """Test ptw status and ptw doctor output for both Codex and AGY clients."""
    uninit_home = tmp_path / "empty_ptw"
    uninit_home.mkdir(parents=True, exist_ok=True)
    codex_home = tmp_path / "codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    gemini_home = tmp_path / "gemini"
    gemini_home.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("shutil.which", lambda _: None)

    # 1. ptw status text
    code_st = main([
        "--home", str(uninit_home),
        "--codex-home", str(codex_home),
        "--gemini-home", str(gemini_home),
        "status",
    ])
    assert code_st == ExitCode.SUCCESS
    out_st = capsys.readouterr().out
    assert "=== Client: Codex ===" in out_st
    assert "=== Client: Antigravity (agy) ===" in out_st

    # 2. ptw status --json
    code_st_json = main([
        "--home", str(uninit_home),
        "--codex-home", str(codex_home),
        "--gemini-home", str(gemini_home),
        "status",
        "--json",
    ])
    assert code_st_json == ExitCode.SUCCESS
    st_json = json.loads(capsys.readouterr().out)
    assert "clients" in st_json
    assert "codex" in st_json["clients"]
    assert "agy" in st_json["clients"]
    assert st_json["clients"]["agy"]["installed"] is False

    # 3. ptw doctor text
    code_doc = main([
        "--home", str(uninit_home),
        "--codex-home", str(codex_home),
        "--gemini-home", str(gemini_home),
        "doctor",
    ])
    assert code_doc == ExitCode.SUCCESS
    out_doc = capsys.readouterr().out
    assert "=== Personal Tideway Doctor ===" in out_doc
    assert "Coverage: Codex + AGY" in out_doc
    assert "[WARNING] codex_executable" in out_doc
    assert "[WARNING] agy_executable" in out_doc

    # 4. ptw doctor --json
    code_doc_json = main([
        "--home", str(uninit_home),
        "--codex-home", str(codex_home),
        "--gemini-home", str(gemini_home),
        "doctor",
        "--json",
    ])
    assert code_doc_json == ExitCode.SUCCESS
    doc_json = json.loads(capsys.readouterr().out)
    assert doc_json["coverage"] == "Codex + AGY"
    assert "codex" in doc_json["clients"]
    assert "agy" in doc_json["clients"]
