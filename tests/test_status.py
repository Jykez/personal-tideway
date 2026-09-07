"""Tests for workspace status reporting, JSON output, and portable MCP parity."""

import json
from pathlib import Path
import pytest

from personal_tideway.cli.main import main
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import CLIENT_AGY, CLIENT_CODEX, ExitCode
from personal_tideway.core.mcp import save_mcp_server
from personal_tideway.core.status import get_workspace_status
from personal_tideway.models import MCPServer


def test_status_uninitialized(tmp_path: Path):
    """Test status reports uninitialized when workspace lacks personal-tideway.yaml."""
    empty_home = tmp_path / "empty_ptw"
    empty_home.mkdir(parents=True)
    cfg = PersonalTidewayConfig.resolve(home=empty_home)

    status = get_workspace_status(cfg)
    assert status["initialized"] is False


def test_status_reports_portable_mcp_parity(personal_tideway_config: PersonalTidewayConfig):
    """Test portable MCP parity correctly checks cross-client parity."""
    # Add portable server (both codex and agy)
    srv = MCPServer(
        name="shared-bridge",
        command="python",
        args=["bridge.py"],
        targets=[CLIENT_CODEX, CLIENT_AGY],
        overrides={
            CLIENT_AGY: {
                "command": "python3",
                "args": ["agy-bridge.py"],
            }
        },
    )
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    # Before sync, status shows drift/pending
    status_before = get_workspace_status(personal_tideway_config)
    assert len(status_before["pending_changes"]) > 0

    # Sync to synchronize both clients
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])

    status_after = get_workspace_status(personal_tideway_config)
    assert status_after["portable_mcp_parity"]["status"] == "in_sync"
    servers = status_after["portable_mcp_parity"]["servers"]
    assert len(servers) == 1
    assert servers[0]["name"] == "shared-bridge"
    assert servers[0]["in_sync"] is True


def test_status_json_cli_output(personal_tideway_config: PersonalTidewayConfig, capsys):
    """Test CLI 'ptw status --json' produces clean, parseable JSON."""
    code = main(["--home", str(personal_tideway_config.home), "status", "--json"])
    assert code == ExitCode.SUCCESS

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["initialized"] is True
    assert "conflicts" in data
    assert "deletions" in data
    assert "pending_changes" in data
    assert "portable_mcp_parity" in data
