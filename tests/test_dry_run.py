"""Tests verifying that --dry-run causes zero filesystem, state, or metadata mutations."""

import os
from pathlib import Path
import pytest

from personal_tideway.cli.main import main
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import ExitCode
from personal_tideway.core.mcp import save_mcp_server
from personal_tideway.models import MCPServer
from personal_tideway.utils import hash_dir, hash_file


def take_fs_snapshot(root_dirs: list[Path]) -> dict[str, tuple[int, int, str | None]]:
    """Capture (size, mtime_ns, hash) for every file under given root directories."""
    snapshot: dict[str, tuple[int, int, str | None]] = {}
    for r in root_dirs:
        if not r.exists():
            continue
        for root, dirs, files in os.walk(r):
            for f in files:
                p = Path(root) / f
                stat = p.stat()
                h = hash_file(p)
                snapshot[str(p)] = (stat.st_size, stat.st_mtime_ns, h)
    return snapshot


def test_sync_dry_run_zero_mutation(personal_tideway_config: PersonalTidewayConfig):
    """Test that 'ptw sync --dry-run' modifies zero bytes and creates zero backups or state changes."""
    # Setup an MCP server in Personal Tideway that would normally trigger client file creation
    srv = MCPServer(
        name="test-server",
        command="echo",
        args=["hello"],
        targets=["codex", "agy"],
    )
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    # Roots to monitor
    monitored_roots = [
        personal_tideway_config.home,
        personal_tideway_config.codex_home,
        personal_tideway_config.gemini_home,
    ]

    before_snapshot = take_fs_snapshot(monitored_roots)

    # Run sync with --dry-run
    code = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync", "--dry-run",
    ])

    after_snapshot = take_fs_snapshot(monitored_roots)

    assert code == ExitCode.SUCCESS
    # Ensure every file path, size, mtime, and hash is 100% identical
    assert before_snapshot == after_snapshot, "Dry run mutated filesystem state!"
    # Ensure no backups were created
    assert not list(personal_tideway_config.backups_dir.glob("*"))
    # Ensure client files were not created
    assert not personal_tideway_config.codex_config.exists()
    assert not personal_tideway_config.agy_config.exists()
