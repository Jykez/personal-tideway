"""Tests for conflict resolution and explicit deletion handling."""

from pathlib import Path
import pytest
import tomlkit

from personal_tideway.cli.main import main
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import CLIENT_CODEX, ExitCode
from personal_tideway.core.mcp import load_mcp_server, save_mcp_server
from personal_tideway.core.status import get_workspace_status
from personal_tideway.models import MCPServer
from personal_tideway.utils import atomic_write_text


def test_resolve_take_ptw(personal_tideway_config: PersonalTidewayConfig):
    """Test resolving conflict by choosing --take ptw."""
    srv = MCPServer(name="res-srv", command="run", args=["v1"], targets=[CLIENT_CODEX])
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    # Establish base
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])

    # Cause divergence
    srv.args = ["ptw-wins"]
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    doc = tomlkit.parse(personal_tideway_config.codex_config.read_text(encoding="utf-8"))
    doc["mcp_servers"]["res-srv"]["args"] = ["client-loses"]
    atomic_write_text(personal_tideway_config.codex_config, tomlkit.dumps(doc))

    # Sync triggers conflict
    code_sync = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code_sync == ExitCode.CONFLICT
    assert (personal_tideway_config.conflicts_dir / "mcp_res-srv.json").is_file()

    # Resolve using --take ptw
    code_res = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "resolve", "mcp:res-srv", "--take", "ptw",
    ])
    assert code_res == ExitCode.SUCCESS

    # Conflict artifacts should be removed
    assert not (personal_tideway_config.conflicts_dir / "mcp_res-srv.json").exists()
    assert not (personal_tideway_config.conflicts_dir / "mcp_res-srv.txt").exists()

    # Client TOML should now have Personal Tideway's version
    doc_after = tomlkit.parse(personal_tideway_config.codex_config.read_text(encoding="utf-8"))
    assert doc_after["mcp_servers"]["res-srv"]["args"] == ["ptw-wins"]

    # Next sync should be 100% clean
    code_sync2 = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code_sync2 == ExitCode.SUCCESS


def test_resolve_take_client(personal_tideway_config: PersonalTidewayConfig):
    """Test resolving conflict by choosing --take codex."""
    srv = MCPServer(name="res-srv2", command="run", args=["v1"], targets=[CLIENT_CODEX])
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    # Base sync
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])

    # Cause divergence
    srv.args = ["ptw-loses"]
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    doc = tomlkit.parse(personal_tideway_config.codex_config.read_text(encoding="utf-8"))
    doc["mcp_servers"]["res-srv2"]["args"] = ["client-wins"]
    atomic_write_text(personal_tideway_config.codex_config, tomlkit.dumps(doc))

    # Trigger conflict
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert (personal_tideway_config.conflicts_dir / "mcp_res-srv2.json").is_file()

    # Resolve using --take codex
    code_res = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "resolve", "mcp:res-srv2", "--take", "codex",
    ])
    assert code_res == ExitCode.SUCCESS

    # Verify Personal Tideway YAML got updated with client version
    reloaded_ptw = load_mcp_server(personal_tideway_config.mcp_dir / "res-srv2.yaml")
    assert reloaded_ptw.args == ["client-wins"]


def test_deletion_requires_explicit_resolution(personal_tideway_config: PersonalTidewayConfig):
    """Test that deletion in Personal Tideway does not automatically delete from client and is reported in status."""
    srv = MCPServer(name="del-srv", command="run", args=[], targets=[CLIENT_CODEX])
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    # Base sync
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])

    # Delete server definition from Personal Tideway canonical dir
    (personal_tideway_config.mcp_dir / "del-srv.yaml").unlink()

    # Sync: should not automatically delete on client
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])

    # Client still has del-srv!
    doc = tomlkit.parse(personal_tideway_config.codex_config.read_text(encoding="utf-8"))
    assert "del-srv" in doc["mcp_servers"]

    # Status must report deletion
    status = get_workspace_status(personal_tideway_config)
    deletions = [d["name"] for d in status["deletions"]]
    assert "del-srv" in deletions


def test_resolve_skill_conflict_and_dry_run(personal_tideway_config: PersonalTidewayConfig):
    """Test resolving skill conflict, verifying dry-run mutates nothing and real resolve clears artifacts."""
    # 1. Create skill in Personal Tideway and establish base
    main([
        "--home", str(personal_tideway_config.home),
        "skill", "create", "test-skill", "--scope", "shared",
    ])
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])

    # 2. Diverge skill on both sides
    canonical_md = personal_tideway_config.skills_shared / "test-skill" / "SKILL.md"
    atomic_write_text(canonical_md, "# Title\nPersonal Tideway changed skill\n")

    codex_md = personal_tideway_config.codex_skills / "test-skill" / "SKILL.md"
    # Ensure it's a real directory rather than symlink for independent divergence test
    if (personal_tideway_config.codex_skills / "test-skill").is_symlink():
        (personal_tideway_config.codex_skills / "test-skill").unlink()
        (personal_tideway_config.codex_skills / "test-skill").mkdir(parents=True)
    atomic_write_text(codex_md, "# Title\nCodex changed skill\n")

    # Sync triggers conflict
    code_sync = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code_sync == ExitCode.CONFLICT
    conflict_json = personal_tideway_config.conflicts_dir / "skill_test-skill.json"
    assert conflict_json.is_file()

    # 3. Dry-run resolve: must NOT remove artifacts or alter canonical skill
    code_dry = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "resolve", "skill:test-skill", "--take", "codex", "--dry-run",
    ])
    assert code_dry == ExitCode.SUCCESS
    assert conflict_json.is_file()
    assert canonical_md.read_text(encoding="utf-8") == "# Title\nPersonal Tideway changed skill\n"

    # 4. Real resolve: taking codex version
    code_real = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "resolve", "skill:test-skill", "--take", "codex",
    ])
    assert code_real == ExitCode.SUCCESS
    assert not conflict_json.exists()
    assert canonical_md.read_text(encoding="utf-8") == "# Title\nCodex changed skill\n"

    # Next sync is clean
    code_sync2 = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code_sync2 == ExitCode.SUCCESS


def test_repeated_identical_conflict_does_not_touch_mtimes(personal_tideway_config: PersonalTidewayConfig):
    """Test that repeated identical conflicts do not rewrite artifacts or update mtimes."""
    srv = MCPServer(name="mtime-srv", command="v1", args=[], targets=[CLIENT_CODEX])
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])

    # Cause divergence
    srv.command = "ptw-ver"
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    doc = tomlkit.parse(personal_tideway_config.codex_config.read_text(encoding="utf-8"))
    doc["mcp_servers"]["mtime-srv"]["command"] = "codex-ver"
    atomic_write_text(personal_tideway_config.codex_config, tomlkit.dumps(doc))

    # First conflict creation
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    conflict_json = personal_tideway_config.conflicts_dir / "mcp_mtime-srv.json"
    assert conflict_json.is_file()
    mtime1 = conflict_json.stat().st_mtime_ns

    # Second sync without any changes on either side
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    mtime2 = conflict_json.stat().st_mtime_ns
    assert mtime1 == mtime2, "Repeated identical conflict rewrote artifact mtime!"


def test_resolve_take_client_preserves_canonical_metadata(personal_tideway_config: PersonalTidewayConfig):
    """Test taking client version retains canonical metadata like tags, profiles, and targets."""
    srv = MCPServer(
        name="meta-srv",
        command="base-cmd",
        args=["arg1"],
        targets=[CLIENT_CODEX, "agy"],
        enabled=True,
        profiles=["prod", "analytics"],
        tags=["core"],
    )
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])

    # Diverge command on client and in Personal Tideway
    srv.command = "ptw-cmd"
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    doc = tomlkit.parse(personal_tideway_config.codex_config.read_text(encoding="utf-8"))
    doc["mcp_servers"]["meta-srv"]["command"] = "client-cmd"
    atomic_write_text(personal_tideway_config.codex_config, tomlkit.dumps(doc))

    # Sync generates conflict
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])

    # Resolve taking codex
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "resolve", "mcp:meta-srv", "--take", "codex",
    ])

    reloaded = load_mcp_server(personal_tideway_config.mcp_dir / "meta-srv.yaml")
    # Command updated from client
    assert reloaded.command == "client-cmd"
    # Metadata retained!
    assert reloaded.profiles == ["prod", "analytics"]
    assert reloaded.tags == ["core"]
    assert reloaded.targets == [CLIENT_CODEX, "agy"]
    assert reloaded.enabled is True
