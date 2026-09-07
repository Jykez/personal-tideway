"""Tests verifying preservation of unrelated TOML tables and JSON keys."""

import json
from pathlib import Path
import pytest
import tomlkit

from personal_tideway.cli.main import main
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import CLIENT_AGY, CLIENT_CODEX, ExitCode, TRANSPORT_HTTP
from personal_tideway.core.mcp import save_mcp_server
from personal_tideway.models import MCPServer
from personal_tideway.utils import atomic_write_text


def test_codex_toml_unrelated_sections_and_comments_preserved(personal_tideway_config: PersonalTidewayConfig):
    """Test that Codex config.toml preserves comments and unrelated tables."""
    initial_toml = """# Crucial configuration header
[general]
theme = "dark"
analytics = false

# Unmanaged server configured manually by user
[mcp_servers.manual_server]
command = "custom_tool"
args = ["--quiet"]
"""
    personal_tideway_config.codex_config.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(personal_tideway_config.codex_config, initial_toml)

    # Add a Personal Tideway-managed server
    srv = MCPServer(
        name="managed_srv",
        command="managed_tool",
        args=["--opt"],
        targets=[CLIENT_CODEX],
    )
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    # Sync
    code = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code == ExitCode.SUCCESS

    content = personal_tideway_config.codex_config.read_text(encoding="utf-8")
    doc = tomlkit.parse(content)

    # Unrelated tables survive semantically
    assert doc["general"]["theme"] == "dark"
    assert doc["general"]["analytics"] is False

    # Unmanaged server survived
    assert doc["mcp_servers"]["manual_server"]["command"] == "custom_tool"

    # Managed server added
    assert doc["mcp_servers"]["managed_srv"]["command"] == "managed_tool"

    # Comments preserved
    assert "# Crucial configuration header" in content
    assert "# Unmanaged server configured manually by user" in content


def test_agy_json_unrelated_keys_survive_and_http_uses_serverurl(personal_tideway_config: PersonalTidewayConfig):
    """Test agy mcp_config.json preserves unrelated keys and HTTP maps to serverUrl."""
    initial_json = {
        "globalSetting": True,
        "clientVersion": "1.2.3",
        "mcpServers": {
            "unmanaged_agy_srv": {
                "command": "custom_agy_cmd",
                "args": ["-x"],
            }
        },
    }
    personal_tideway_config.agy_config.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(personal_tideway_config.agy_config, json.dumps(initial_json, indent=2))

    # Add HTTP MCP server in Personal Tideway
    http_srv = MCPServer(
        name="remote_service",
        transport=TRANSPORT_HTTP,
        url="https://mcp.internal.net/v1",
        targets=[CLIENT_AGY],
    )
    save_mcp_server(personal_tideway_config.mcp_dir, http_srv)

    code = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code == ExitCode.SUCCESS

    # Validate JSON remains valid
    with open(personal_tideway_config.agy_config, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Unrelated top-level keys survive
    assert data["globalSetting"] is True
    assert data["clientVersion"] == "1.2.3"

    # Unmanaged server survived
    assert "unmanaged_agy_srv" in data["mcpServers"]
    assert data["mcpServers"]["unmanaged_agy_srv"]["command"] == "custom_agy_cmd"

    # HTTP URL serialized specifically as serverUrl
    assert "remote_service" in data["mcpServers"]
    assert data["mcpServers"]["remote_service"]["serverUrl"] == "https://mcp.internal.net/v1"
    assert "command" not in data["mcpServers"]["remote_service"]


def test_managed_mcp_preserves_unknown_fields_and_disabled_state(personal_tideway_config: PersonalTidewayConfig):
    personal_tideway_config.codex_config.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        personal_tideway_config.codex_config,
        '[mcp_servers.node_repl]\ncommand = "old"\nstartup_timeout_sec = 30\n',
    )
    save_mcp_server(
        personal_tideway_config.mcp_dir,
        MCPServer(
            name="node_repl",
            command="new",
            enabled=False,
            targets=[CLIENT_CODEX],
        ),
    )

    assert main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ]) == ExitCode.CONFLICT
    assert main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "resolve", "mcp:node_repl", "--take", "ptw",
    ]) == ExitCode.SUCCESS

    after = tomlkit.parse(personal_tideway_config.codex_config.read_text(encoding="utf-8"))
    assert after["mcp_servers"]["node_repl"]["command"] == "new"
    assert after["mcp_servers"]["node_repl"]["enabled"] is False
    assert after["mcp_servers"]["node_repl"]["startup_timeout_sec"] == 30


def test_unchanged_managed_mcp_is_not_reserialized(personal_tideway_config: PersonalTidewayConfig):
    original = (
        '[mcp_servers.keep_exact]\n'
        'args = []\n'
        'command = "tool"\n'
        'startup_timeout_sec = 120\n'
    )
    personal_tideway_config.codex_config.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(personal_tideway_config.codex_config, original)
    save_mcp_server(
        personal_tideway_config.mcp_dir,
        MCPServer(name="keep_exact", command="tool", targets=[CLIENT_CODEX]),
    )

    args = [
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ]
    assert main(args) == ExitCode.SUCCESS
    assert personal_tideway_config.codex_config.read_text(encoding="utf-8") == original
    before_mtime = personal_tideway_config.codex_config.stat().st_mtime_ns
    assert main(args) == ExitCode.SUCCESS
    assert personal_tideway_config.codex_config.read_text(encoding="utf-8") == original
    assert personal_tideway_config.codex_config.stat().st_mtime_ns == before_mtime
