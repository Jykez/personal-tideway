"""Tests for three-way synchronization: propagation, idempotency, and conflicts."""

import json
from pathlib import Path
import pytest
import tomlkit

from personal_tideway.cli.main import main
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import CLIENT_AGY, CLIENT_CODEX, ExitCode
from personal_tideway.core.mcp import load_mcp_server, save_mcp_server
from personal_tideway.models import MCPServer
from personal_tideway.utils import atomic_write_text


def test_sync_idempotency_no_changes(personal_tideway_config: PersonalTidewayConfig):
    """Test that repeated sync with no changes produces zero modifications."""
    # 1. First sync to establish base
    code1 = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code1 == ExitCode.SUCCESS

    # Take snapshot of mtime
    state_mtime = personal_tideway_config.state_file.stat().st_mtime_ns
    backups_before = list(personal_tideway_config.backups_dir.glob("*"))

    # 2. Second sync
    code2 = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code2 == ExitCode.SUCCESS

    # Ensure no new backups or state modifications
    backups_after = list(personal_tideway_config.backups_dir.glob("*"))
    assert len(backups_before) == len(backups_after)


def test_one_sided_propagation_ptw_to_clients(personal_tideway_config: PersonalTidewayConfig):
    """Test one-sided change in Personal Tideway propagates to both clients."""
    srv = MCPServer(
        name="test-server",
        command="node",
        args=["main.js"],
        targets=[CLIENT_CODEX, CLIENT_AGY],
        env={"PORT": "8080"},
    )
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    code = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code == ExitCode.SUCCESS

    # Check Codex TOML
    assert personal_tideway_config.codex_config.is_file()
    codex_data = tomlkit.parse(personal_tideway_config.codex_config.read_text(encoding="utf-8"))
    assert "test-server" in codex_data["mcp_servers"]
    assert codex_data["mcp_servers"]["test-server"]["command"] == "node"
    assert codex_data["mcp_servers"]["test-server"]["env"]["PORT"] == "8080"

    # Check agy JSON
    assert personal_tideway_config.agy_config.is_file()
    with open(personal_tideway_config.agy_config, "r", encoding="utf-8") as f:
        agy_data = json.load(f)
    assert "test-server" in agy_data["mcpServers"]
    assert agy_data["mcpServers"]["test-server"]["command"] == "node"
    assert agy_data["mcpServers"]["test-server"]["env"]["PORT"] == "8080"


def test_one_sided_propagation_client_to_ptw(personal_tideway_config: PersonalTidewayConfig):
    """Test one-sided change in a client propagates back into Personal Tideway canonical YAML."""
    srv = MCPServer(
        name="echo-srv",
        command="echo",
        args=["v1"],
        targets=[CLIENT_CODEX],
    )
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    # First sync establishes base
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])

    # Now simulate client modifying its config.toml
    codex_doc = tomlkit.parse(personal_tideway_config.codex_config.read_text(encoding="utf-8"))
    codex_doc["mcp_servers"]["echo-srv"]["args"] = ["v2-client-updated"]
    atomic_write_text(personal_tideway_config.codex_config, tomlkit.dumps(codex_doc))

    # Sync again
    code = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code == ExitCode.SUCCESS

    # Verify Personal Tideway YAML got updated with client change
    updated_server = load_mcp_server(personal_tideway_config.mcp_dir / "echo-srv.yaml")
    assert updated_server.args == ["v2-client-updated"]


def test_dual_identical_changes_accepted(personal_tideway_config: PersonalTidewayConfig):
    """Test identical dual changes on both sides are accepted without conflict."""
    srv = MCPServer(
        name="dual-srv",
        command="python",
        args=["base.py"],
        targets=[CLIENT_CODEX],
    )
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    # Base sync
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])

    # Both Personal Tideway and Codex update to the exact same args
    srv.args = ["dual.py"]
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    codex_doc = tomlkit.parse(personal_tideway_config.codex_config.read_text(encoding="utf-8"))
    codex_doc["mcp_servers"]["dual-srv"]["args"] = ["dual.py"]
    atomic_write_text(personal_tideway_config.codex_config, tomlkit.dumps(codex_doc))

    code = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code == ExitCode.SUCCESS
    assert not list(personal_tideway_config.conflicts_dir.glob("*.json"))


def test_dual_divergence_generates_conflict_without_blocking_unrelated(personal_tideway_config: PersonalTidewayConfig):
    """Test divergent dual changes generate conflict and continue syncing unrelated objects."""
    # Server 1: will diverge
    srv1 = MCPServer(
        name="diverge-srv",
        command="tool",
        args=["base"],
        targets=[CLIENT_CODEX],
    )
    save_mcp_server(personal_tideway_config.mcp_dir, srv1)

    # Server 2: unrelated normal server
    srv2 = MCPServer(
        name="normal-srv",
        command="echo",
        args=["initial"],
        targets=[CLIENT_CODEX],
    )
    save_mcp_server(personal_tideway_config.mcp_dir, srv2)

    # Base sync
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])

    # Diverge Server 1
    srv1.args = ["ptw-branch"]
    save_mcp_server(personal_tideway_config.mcp_dir, srv1)

    codex_doc = tomlkit.parse(personal_tideway_config.codex_config.read_text(encoding="utf-8"))
    codex_doc["mcp_servers"]["diverge-srv"]["args"] = ["codex-branch"]

    # Modify Server 2 in Personal Tideway only
    srv2.args = ["updated-by-ptw"]
    save_mcp_server(personal_tideway_config.mcp_dir, srv2)

    atomic_write_text(personal_tideway_config.codex_config, tomlkit.dumps(codex_doc))

    # Sync: should return ExitCode.CONFLICT (3)
    code = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code == ExitCode.CONFLICT

    # Conflict artifact created for server 1
    assert (personal_tideway_config.conflicts_dir / "mcp_diverge-srv.json").is_file()
    assert (personal_tideway_config.conflicts_dir / "mcp_diverge-srv.txt").is_file()

    # Neither side overwritten for server 1
    assert load_mcp_server(personal_tideway_config.mcp_dir / "diverge-srv.yaml").args == ["ptw-branch"]
    reloaded_codex = tomlkit.parse(personal_tideway_config.codex_config.read_text(encoding="utf-8"))
    assert reloaded_codex["mcp_servers"]["diverge-srv"]["args"] == ["codex-branch"]

    # Unrelated server 2 was successfully propagated despite conflict in server 1!
    assert reloaded_codex["mcp_servers"]["normal-srv"]["args"] == ["updated-by-ptw"]


def test_clean_second_sync_does_not_rewrite_state(personal_tideway_config: PersonalTidewayConfig):
    args = [
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ]
    assert main(args) == ExitCode.SUCCESS
    before_bytes = personal_tideway_config.state_file.read_bytes()
    before_mtime = personal_tideway_config.state_file.stat().st_mtime_ns

    assert main(args) == ExitCode.SUCCESS
    assert personal_tideway_config.state_file.read_bytes() == before_bytes
    assert personal_tideway_config.state_file.stat().st_mtime_ns == before_mtime
