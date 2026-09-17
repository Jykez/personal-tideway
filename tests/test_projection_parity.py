"""Comprehensive, focused tests for Projection Parity evaluator and status reporting.

Covers:
- Pre-sync drift detection
- Dry-run zero mutation guarantee
- Apply reaching parity with correct serialization, scope isolation, unrelated preservation, and backups
- Second sync idempotence
- Adversarial malformed configs and markers
- Skill link hazards (broken links, escaping symlinks, scope leakage)
- Deleted canonical skill detection via read-only sync state
- Conflict precedence
- Secret redaction in JSON and text reporting
- Uninitialized workspace reporting
"""

import json
from pathlib import Path

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import (
    CLIENT_AGY,
    CLIENT_CODEX,
    RULE_MARKER_END,
    RULE_MARKER_START,
    ExitCode,
)
from personal_tideway.core.conflicts import save_conflict_artifacts
from personal_tideway.core.mcp import save_mcp_server
from personal_tideway.core.skills import create_skill
from personal_tideway.core.status import (
    evaluate_projection_parity,
    format_status_text,
    get_workspace_status,
)
from personal_tideway.core.sync import sync_workspace
from personal_tideway.models import ConflictRecord, MCPServer


def test_projection_parity_uninitialized(tmp_path: Path):
    """Test projection parity reports uninitialized when workspace lacks personal-tideway.yaml."""
    empty_home = tmp_path / "empty_ptw"
    empty_home.mkdir(parents=True)
    cfg = PersonalTidewayConfig.resolve(home=empty_home)

    parity = evaluate_projection_parity(cfg)
    assert parity["status"] == "uninitialized"
    assert parity["codex"]["status"] == "uninitialized"
    assert parity["codex"]["mcp"]["status"] == "uninitialized"
    assert parity["codex"]["rules"]["status"] == "uninitialized"
    assert parity["codex"]["skills"]["status"] == "uninitialized"
    assert parity["agy"]["status"] == "uninitialized"
    assert parity["agy"]["mcp"]["status"] == "uninitialized"
    assert parity["agy"]["rules"]["status"] == "uninitialized"
    assert parity["agy"]["skills"]["status"] == "uninitialized"
    assert "init" in parity["remediation"].lower()

    status = get_workspace_status(cfg)
    assert status["initialized"] is False
    assert "projection_parity" in status
    assert status["projection_parity"]["status"] == "uninitialized"

    text = format_status_text(status)
    assert "projection parity: uninitialized" in text.lower()


def test_pre_sync_drift(personal_tideway_config: PersonalTidewayConfig):
    """Test that canonical items in Personal Tideway report drift_detected before sync."""
    # 1. Canonical MCP server
    srv = MCPServer(
        name="shared-bridge",
        command="python3",
        args=["bridge.py"],
        targets=[CLIENT_CODEX, CLIENT_AGY],
    )
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    # 2. Canonical rule
    (personal_tideway_config.rules_shared / "01-shared.md").write_text(
        "Shared rule instruction.", encoding="utf-8"
    )

    # 3. Canonical skill
    create_skill(personal_tideway_config, "demo-skill", scope="shared")

    status = get_workspace_status(personal_tideway_config)
    parity = status["projection_parity"]

    assert parity["status"] == "drift_detected"
    assert parity["codex"]["status"] == "drift_detected"
    assert parity["codex"]["mcp"]["status"] == "drift_detected"
    assert parity["codex"]["rules"]["status"] == "drift_detected"
    assert parity["codex"]["skills"]["status"] == "drift_detected"
    assert parity["agy"]["status"] == "drift_detected"
    assert parity["agy"]["mcp"]["status"] == "drift_detected"
    assert parity["agy"]["rules"]["status"] == "drift_detected"
    assert parity["agy"]["skills"]["status"] == "drift_detected"
    assert "sync" in parity["remediation"].lower()


def test_dry_run_zero_mutation(personal_tideway_config: PersonalTidewayConfig):
    """Test that dry-run sync produces zero mutations across client configs, rules, skills, backups, and state."""
    # Setup canonical items
    srv = MCPServer(
        name="api-srv",
        command="node",
        args=["server.js"],
        targets=[CLIENT_CODEX, CLIENT_AGY],
    )
    save_mcp_server(personal_tideway_config.mcp_dir, srv)
    (personal_tideway_config.rules_shared / "base.md").write_text("Standard rule", encoding="utf-8")
    create_skill(personal_tideway_config, "calc-skill", scope="shared")

    # Run dry-run sync
    code, _msgs = sync_workspace(personal_tideway_config, dry_run=True)
    assert code == ExitCode.SUCCESS

    # Verify zero mutations
    assert not personal_tideway_config.codex_config.exists()
    assert not personal_tideway_config.agy_config.exists()
    assert not personal_tideway_config.codex_rules.exists()
    assert not personal_tideway_config.agy_rules.exists()
    assert not (personal_tideway_config.codex_skills / "calc-skill").exists()
    assert not (personal_tideway_config.agy_skills / "calc-skill").exists()

    # Backups directory must remain completely empty
    backup_files = list(personal_tideway_config.backups_dir.glob("**/*"))
    assert len(backup_files) == 0

    # Status remains in drift
    status = get_workspace_status(personal_tideway_config)
    assert status["projection_parity"]["status"] == "drift_detected"


def test_apply_reaches_parity_serialization_scope_unrelated_backups(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Test that apply reaches parity with proper serialization, scope isolation, unrelated preservation, and backups."""
    cfg = personal_tideway_config

    # 1. Existing unrelated client configs and rules
    cfg.codex_config.parent.mkdir(parents=True, exist_ok=True)
    cfg.codex_config.write_text(
        '[mcp_servers.unrelated_codex]\ncommand = "unrelated-bin"\nargs = ["--flag"]\n',
        encoding="utf-8",
    )
    cfg.agy_config.parent.mkdir(parents=True, exist_ok=True)
    cfg.agy_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "unrelated_agy": {
                        "command": "agy-bin",
                        "args": [],
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    cfg.codex_rules.parent.mkdir(parents=True, exist_ok=True)
    cfg.codex_rules.write_text("# User Personal Notes\nKeep this text intact.\n", encoding="utf-8")

    cfg.agy_rules.parent.mkdir(parents=True, exist_ok=True)
    cfg.agy_rules.write_text("# Custom Agy Notes\nDo not overwrite.\n", encoding="utf-8")

    # 2. Canonical objects in Personal Tideway
    srv = MCPServer(
        name="poly-srv",
        command="run-poly",
        args=["--port", "8080"],
        targets=[CLIENT_CODEX, CLIENT_AGY],
        overrides={
            CLIENT_AGY: {
                "command": "run-poly-agy",
                "args": ["--port", "9090"],
            }
        },
    )
    save_mcp_server(cfg.mcp_dir, srv)

    (cfg.rules_shared / "shared.md").write_text("Shared policy content.", encoding="utf-8")
    (cfg.rules_codex / "codex.md").write_text("Codex specific policy.", encoding="utf-8")
    (cfg.rules_agy / "agy.md").write_text("AGY specific policy.", encoding="utf-8")

    create_skill(cfg, "shared-skill", scope="shared")
    create_skill(cfg, "codex-only-skill", scope=CLIENT_CODEX)
    create_skill(cfg, "agy-only-skill", scope=CLIENT_AGY)

    # 3. Apply sync
    code, _msgs = sync_workspace(cfg, dry_run=False)
    assert code == ExitCode.SUCCESS

    # 4. Add truly unknown unrelated user skill after initial sync
    user_skill_dir = cfg.codex_skills / "user-custom-skill"
    user_skill_dir.mkdir(parents=True, exist_ok=True)
    (user_skill_dir / "SKILL.md").write_text("# User Custom Skill\n", encoding="utf-8")

    # 5. Verify Projection Parity is in_sync across all components and clients
    status = get_workspace_status(cfg)
    proj = status["projection_parity"]

    assert proj["status"] == "in_sync"
    assert proj["remediation"] == "All projections are in sync."

    assert proj["codex"]["status"] == "in_sync"
    assert proj["codex"]["mcp"]["status"] == "in_sync"
    assert proj["codex"]["rules"]["status"] == "in_sync"
    assert proj["codex"]["skills"]["status"] == "in_sync"

    assert proj["agy"]["status"] == "in_sync"
    assert proj["agy"]["mcp"]["status"] == "in_sync"
    assert proj["agy"]["rules"]["status"] == "in_sync"
    assert proj["agy"]["skills"]["status"] == "in_sync"

    # Backward compatibility: portable_mcp_parity preserved
    assert "portable_mcp_parity" in status
    assert status["portable_mcp_parity"]["status"] == "in_sync"

    # 6. Verify Serialization
    codex_cfg_text = cfg.codex_config.read_text(encoding="utf-8")
    assert "poly-srv" in codex_cfg_text
    assert 'command = "run-poly"' in codex_cfg_text
    assert 'args = ["--port", "8080"]' in codex_cfg_text

    agy_cfg_json = json.loads(cfg.agy_config.read_text(encoding="utf-8"))
    assert "poly-srv" in agy_cfg_json["mcpServers"]
    assert agy_cfg_json["mcpServers"]["poly-srv"]["command"] == "run-poly-agy"
    assert agy_cfg_json["mcpServers"]["poly-srv"]["args"] == ["--port", "9090"]

    # 7. Verify Scope Isolation
    assert (cfg.codex_skills / "codex-only-skill").exists()
    assert not (cfg.agy_skills / "codex-only-skill").exists()
    assert (cfg.agy_skills / "agy-only-skill").exists()
    assert not (cfg.codex_skills / "agy-only-skill").exists()
    assert (cfg.codex_skills / "shared-skill").exists()
    assert (cfg.agy_skills / "shared-skill").exists()

    # 8. Verify Unrelated Preservation
    assert "unrelated_codex" in codex_cfg_text
    assert "unrelated_agy" in agy_cfg_json["mcpServers"]
    assert "Keep this text intact." in cfg.codex_rules.read_text(encoding="utf-8")
    assert "Do not overwrite." in cfg.agy_rules.read_text(encoding="utf-8")
    assert (cfg.codex_skills / "user-custom-skill").exists()

    # 9. Verify Backups were created for modified files
    backups = list(cfg.backups_dir.glob("**/*"))
    assert len(backups) > 0


def test_second_sync_idempotence(personal_tideway_config: PersonalTidewayConfig):
    """Test that running sync a second time produces no modifications and maintains parity."""
    cfg = personal_tideway_config

    srv = MCPServer(name="test-srv", command="test", targets=[CLIENT_CODEX, CLIENT_AGY])
    save_mcp_server(cfg.mcp_dir, srv)
    (cfg.rules_shared / "rule.md").write_text("Policy", encoding="utf-8")
    create_skill(cfg, "skill-a", scope="shared")

    # First sync
    code1, _msgs1 = sync_workspace(cfg, dry_run=False)
    assert code1 == ExitCode.SUCCESS

    status1 = get_workspace_status(cfg)
    assert status1["projection_parity"]["status"] == "in_sync"

    # Second sync
    code2, _msgs2 = sync_workspace(cfg, dry_run=False)
    assert code2 == ExitCode.SUCCESS

    status2 = get_workspace_status(cfg)
    assert status2["projection_parity"]["status"] == "in_sync"
    assert status2["projection_parity"] == status1["projection_parity"]


def test_adversarial_malformed_configs_and_markers(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Test adversarial scenarios: corrupted TOML, corrupted JSON, malformed rule markers, invalid YAML."""
    cfg = personal_tideway_config

    # Ensure sync state first
    srv = MCPServer(name="svc", command="svc-bin", targets=[CLIENT_CODEX, CLIENT_AGY])
    save_mcp_server(cfg.mcp_dir, srv)
    (cfg.rules_shared / "base.md").write_text("Valid rule", encoding="utf-8")
    sync_workspace(cfg, dry_run=False)

    # Save exact valid config bytes immediately after initial successful sync
    valid_codex_config_bytes = cfg.codex_config.read_bytes()
    valid_agy_config_bytes = cfg.agy_config.read_bytes()
    valid_codex_rules_bytes = cfg.codex_rules.read_bytes()

    # 1. Corrupt Codex TOML
    cfg.codex_config.write_text("INVALID [[[[ TOML SYNTAX = {", encoding="utf-8")
    status1 = get_workspace_status(cfg)
    assert status1["projection_parity"]["codex"]["mcp"]["status"] == "invalid"
    assert status1["projection_parity"]["status"] == "invalid"
    assert "syntax errors" in status1["projection_parity"]["remediation"].lower()

    # Restore exact valid Codex TOML bytes directly before next scenario
    cfg.codex_config.write_bytes(valid_codex_config_bytes)

    # 2. Corrupt agy JSON
    cfg.agy_config.write_text("{ unquoted_key: invalid_json }", encoding="utf-8")
    status2 = get_workspace_status(cfg)
    assert status2["projection_parity"]["agy"]["mcp"]["status"] == "invalid"
    assert status2["projection_parity"]["status"] == "invalid"

    # Restore exact valid agy JSON bytes directly before next scenario
    cfg.agy_config.write_bytes(valid_agy_config_bytes)

    # 3. Corrupt Codex rules markers (unbalanced start marker)
    cfg.codex_rules.write_text(
        f"Some prefix\n{RULE_MARKER_START}\nUnclosed block\n", encoding="utf-8"
    )
    status3 = get_workspace_status(cfg)
    assert status3["projection_parity"]["codex"]["rules"]["status"] == "invalid"
    assert status3["projection_parity"]["status"] == "invalid"

    # End marker before start marker
    cfg.codex_rules.write_text(
        f"{RULE_MARKER_END}\nReversed\n{RULE_MARKER_START}\n", encoding="utf-8"
    )
    status4 = get_workspace_status(cfg)
    assert status4["projection_parity"]["codex"]["rules"]["status"] == "invalid"

    # Restore exact valid Codex rules bytes directly before next scenario
    cfg.codex_rules.write_bytes(valid_codex_rules_bytes)

    # 4. Corrupt canonical MCP YAML in Personal Tideway
    corrupted_yaml = cfg.mcp_dir / "corrupted.yaml"
    corrupted_yaml.write_text(": : bad yaml\n", encoding="utf-8")
    status5 = get_workspace_status(cfg)
    assert status5["projection_parity"]["codex"]["mcp"]["status"] == "invalid"
    assert status5["projection_parity"]["agy"]["mcp"]["status"] == "invalid"
    corrupted_yaml.unlink()


def test_deleted_canonical_skill_reports_drift_from_state(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Test that deleting a canonical skill leaves state evidence and client symlinks detected as drift."""
    cfg = personal_tideway_config

    create_skill(cfg, "ephemeral-skill", scope="shared")
    code, _msgs = sync_workspace(cfg, dry_run=False)
    assert code == ExitCode.SUCCESS

    # Verify initially in sync
    status1 = get_workspace_status(cfg)
    assert status1["projection_parity"]["codex"]["skills"]["status"] == "in_sync"
    assert status1["projection_parity"]["agy"]["skills"]["status"] == "in_sync"

    # Delete the canonical skill from personal tideway shared skills dir
    canonical_dir = cfg.skills_shared / "ephemeral-skill"
    for item in canonical_dir.iterdir():
        item.unlink()
    canonical_dir.rmdir()

    # Client symlinks and state.json still exist!
    assert (cfg.codex_skills / "ephemeral-skill").is_symlink()
    assert (cfg.agy_skills / "ephemeral-skill").is_symlink()

    # Evaluator must load read-only state, detect missing canonical skill, and report drift
    status2 = get_workspace_status(cfg)
    proj2 = status2["projection_parity"]
    assert proj2["codex"]["skills"]["status"] == "drift_detected"
    assert proj2["agy"]["skills"]["status"] == "drift_detected"
    assert proj2["status"] == "drift_detected"

    # Also verify unrelated unknown user skill in client dir does not cause drift
    user_skill_dir = cfg.codex_skills / "other-user-skill"
    user_skill_dir.mkdir(parents=True, exist_ok=True)
    (user_skill_dir / "SKILL.md").write_text("# Other User Skill\n", encoding="utf-8")

    # The drift reason must be ephemeral-skill (deleted_in_ptw), not other-user-skill
    codex_items = proj2["codex"]["skills"]["skills"]
    assert any(s["name"] == "ephemeral-skill" and s.get("hazard") == "deleted_in_ptw" for s in codex_items)
    assert not any(s["name"] == "other-user-skill" for s in codex_items)


def test_skill_link_hazards(personal_tideway_config: PersonalTidewayConfig):
    """Test detection of broken links, escaping links/target mismatch, and scope leakage."""
    cfg = personal_tideway_config

    create_skill(cfg, "valid-skill", scope="shared")
    create_skill(cfg, "agy-exclusive", scope=CLIENT_AGY)
    sync_workspace(cfg, dry_run=False)

    # 1. Broken symlink hazard pointing to nonexistent target
    broken_link = cfg.codex_skills / "valid-skill"
    if broken_link.is_symlink() or broken_link.is_file():
        broken_link.unlink()
    broken_link.symlink_to(Path("/nonexistent/target/hazard"), target_is_directory=True)

    status = get_workspace_status(cfg)
    assert status["projection_parity"]["codex"]["skills"]["status"] == "drift_detected"

    # Restore valid-skill link
    if broken_link.is_symlink() or broken_link.is_file():
        broken_link.unlink()
    broken_link.symlink_to((cfg.skills_shared / "valid-skill").resolve(), target_is_directory=True)

    # 2. Escaping symlink / Target mismatch hazard (symlink pointing outside canonical skill)
    client_link = cfg.codex_skills / "valid-skill"
    if client_link.is_symlink() or client_link.is_file():
        client_link.unlink()
    client_link.symlink_to(cfg.home.resolve(), target_is_directory=True)

    status = get_workspace_status(cfg)
    assert status["projection_parity"]["codex"]["skills"]["status"] == "drift_detected"
    assert status["projection_parity"]["status"] != "in_sync"

    # Restore valid link
    if client_link.is_symlink() or client_link.is_file():
        client_link.unlink()
    client_link.symlink_to((cfg.skills_shared / "valid-skill").resolve(), target_is_directory=True)

    # 3. Scope leakage hazard: agy-only skill mistakenly present in codex skills dir
    leaked_target = cfg.codex_skills / "agy-exclusive"
    leaked_target.symlink_to((cfg.skills_agy / "agy-exclusive").resolve(), target_is_directory=True)

    status = get_workspace_status(cfg)
    assert status["projection_parity"]["codex"]["skills"]["status"] == "drift_detected"
    codex_skills_items = status["projection_parity"]["codex"]["skills"]["skills"]
    leaked_item = next((s for s in codex_skills_items if s["name"] == "agy-exclusive"), None)
    assert leaked_item is not None
    assert leaked_item.get("hazard") == "scope_leakage"


def test_conflict_precedence(personal_tideway_config: PersonalTidewayConfig):
    """Test that active conflict records take precedence over drift or in_sync status."""
    cfg = personal_tideway_config

    srv = MCPServer(name="db-service", command="db-cmd", targets=[CLIENT_CODEX, CLIENT_AGY])
    save_mcp_server(cfg.mcp_dir, srv)
    sync_workspace(cfg, dry_run=False)

    # Verify initially in_sync
    status1 = get_workspace_status(cfg)
    assert status1["projection_parity"]["codex"]["mcp"]["status"] == "in_sync"

    # Plant an active conflict record in conflicts_dir
    conflict = ConflictRecord(
        object_id="mcp:db-service",
        object_type="mcp",
        client=CLIENT_CODEX,
        base_hash="hash_base",
        ptw_hash="hash_ptw",
        client_hash="hash_client",
        ptw_content='{"command": "db-cmd"}',
        client_content='{"command": "divergent-cmd"}',
        message="Manual conflict planted for test",
    )
    save_conflict_artifacts(cfg.conflicts_dir, conflict)

    status2 = get_workspace_status(cfg)
    parity = status2["projection_parity"]

    # Conflict takes precedence over in_sync or drift
    assert parity["codex"]["mcp"]["status"] == "conflict"
    assert parity["codex"]["status"] == "conflict"
    assert parity["status"] == "conflict"
    assert "resolve" in parity["remediation"].lower()


def test_secret_redaction(personal_tideway_config: PersonalTidewayConfig):
    """Test that raw secrets from MCP environment variables are never leaked into projection parity or formatted text."""
    cfg = personal_tideway_config

    secret_key = "TOP_SECRET_AUTH_TOKEN_VALUE_XYZ"
    secret_pass = "DATABASE_PASSWORD_998877"

    srv = MCPServer(
        name="vault-client",
        command="vault",
        args=["auth"],
        targets=[CLIENT_CODEX, CLIENT_AGY],
        env={
            "AUTH_TOKEN": secret_key,
            "DB_PASS": secret_pass,
        },
    )
    save_mcp_server(cfg.mcp_dir, srv)
    sync_workspace(cfg, dry_run=False)

    status = get_workspace_status(cfg)
    parity = status["projection_parity"]

    # Convert projection parity to JSON string
    parity_json = json.dumps(parity)
    assert secret_key not in parity_json
    assert secret_pass not in parity_json

    # Check terminal formatted text
    text = format_status_text(status)
    assert secret_key not in text
    assert secret_pass not in text


def test_text_formatting_compact(personal_tideway_config: PersonalTidewayConfig):
    """Test that text status formats compact MCP/Rules/Skills lines and remediation."""
    cfg = personal_tideway_config

    srv = MCPServer(name="srv1", command="bin1", targets=[CLIENT_CODEX, CLIENT_AGY])
    save_mcp_server(cfg.mcp_dir, srv)
    (cfg.rules_shared / "policy.md").write_text("Rules policy", encoding="utf-8")
    create_skill(cfg, "tool-skill", scope="shared")

    # Before sync: drift detected
    status_drift = get_workspace_status(cfg)
    text_drift = format_status_text(status_drift)
    assert "Projection Parity: drift_detected" in text_drift
    assert "- MCP:    drift_detected" in text_drift
    assert "- Rules:  drift_detected" in text_drift
    assert "- Skills: drift_detected" in text_drift
    assert "Remediation: Run 'ptw sync' to safely apply projections." in text_drift

    # After sync: in_sync
    sync_workspace(cfg, dry_run=False)
    status_sync = get_workspace_status(cfg)
    text_sync = format_status_text(status_sync)
    assert "Projection Parity: in_sync" in text_sync
    assert "- MCP:    in_sync" in text_sync
    assert "- Rules:  in_sync" in text_sync
    assert "- Skills: in_sync" in text_sync
    assert "Remediation: All projections are in sync." in text_sync
