"""Tests for skill management, scope isolation, share validation, and symlinks."""

from pathlib import Path
import pytest

from personal_tideway.cli.main import main
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import ExitCode
from personal_tideway.core.skills import create_skill, find_skill, link_skill
from personal_tideway.utils import atomic_write_text


def test_shared_skill_link_and_unlink(personal_tideway_config: PersonalTidewayConfig):
    """Test creating and linking a shared skill to both clients."""
    code_create = main([
        "--home", str(personal_tideway_config.home),
        "skill", "create", "shared-helper", "--scope", "shared",
    ])
    assert code_create == ExitCode.SUCCESS
    assert (personal_tideway_config.skills_shared / "shared-helper" / "SKILL.md").is_file()

    # Link skill
    code_link = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "skill", "link", "shared-helper",
    ])
    assert code_link == ExitCode.SUCCESS

    # Check both clients received exposure
    assert (personal_tideway_config.codex_skills / "shared-helper" / "SKILL.md").is_file()
    assert (personal_tideway_config.agy_skills / "shared-helper" / "SKILL.md").is_file()

    # Unlink skill
    code_unlink = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "skill", "unlink", "shared-helper",
    ])
    assert code_unlink == ExitCode.SUCCESS

    assert not (personal_tideway_config.codex_skills / "shared-helper").exists()
    assert not (personal_tideway_config.agy_skills / "shared-helper").exists()
    # Canonical skill is preserved
    assert (personal_tideway_config.skills_shared / "shared-helper" / "SKILL.md").is_file()


def test_client_specific_skills_do_not_leak(personal_tideway_config: PersonalTidewayConfig):
    """Test client-specific skills only expose to their designated client."""
    # Create codex-only skill
    code_codex = main([
        "--home", str(personal_tideway_config.home),
        "skill", "create", "codex-repl", "--scope", "codex",
    ])
    assert code_codex == ExitCode.SUCCESS

    # Link
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "skill", "link", "codex-repl",
    ])

    assert (personal_tideway_config.codex_skills / "codex-repl").exists()
    assert not (personal_tideway_config.agy_skills / "codex-repl").exists()


def test_skill_share_validates_and_rejects_collisions(personal_tideway_config: PersonalTidewayConfig):
    """Test skill share promotes to shared after validation and rejects collisions."""
    # Create client skill
    main([
        "--home", str(personal_tideway_config.home),
        "skill", "create", "cad-tool", "--scope", "codex",
    ])

    # Promote to shared
    code_share = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "skill", "share", "cad-tool",
    ])
    assert code_share == ExitCode.SUCCESS

    # It must now be in shared and removed from codex scope
    assert (personal_tideway_config.skills_shared / "cad-tool").is_dir()
    assert not (personal_tideway_config.skills_codex / "cad-tool").exists()

    # Both clients must have received it
    assert (personal_tideway_config.codex_skills / "cad-tool").exists()
    assert (personal_tideway_config.agy_skills / "cad-tool").exists()

    # Collision test: create another skill in agy with the same name
    agy_dup = personal_tideway_config.skills_agy / "cad-tool"
    agy_dup.mkdir(parents=True)
    atomic_write_text(agy_dup / "SKILL.md", "# Dup")

    # Attempting to share must fail due to collision
    code_coll = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "skill", "share", "cad-tool",
    ])
    assert code_coll == ExitCode.VALIDATION_ERROR


def test_skills_three_way_sync_bidirectional_propagation(personal_tideway_config: PersonalTidewayConfig):
    """Test 3-way skill sync: client modification propagates to Personal Tideway, and Personal Tideway propagates to client."""
    # 1. Create skill in Personal Tideway and establish base sync
    main([
        "--home", str(personal_tideway_config.home),
        "skill", "create", "sync-skill", "--scope", "codex",
    ])
    code_base = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code_base == ExitCode.SUCCESS

    # Ensure codex has skill and agy does NOT (client isolation)
    codex_skill = personal_tideway_config.codex_skills / "sync-skill"
    assert codex_skill.exists()
    assert not (personal_tideway_config.agy_skills / "sync-skill").exists()

    # 2. Client modifies skill content (replace symlink with dir if needed)
    if codex_skill.is_symlink():
        codex_skill.unlink()
        codex_skill.mkdir(parents=True)
    atomic_write_text(codex_skill / "SKILL.md", "# Modified by client\nClient details\n")

    code_sync1 = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code_sync1 == ExitCode.SUCCESS

    # Canonical skill in Personal Tideway received client change
    canonical_md = personal_tideway_config.skills_codex / "sync-skill" / "SKILL.md"
    assert "Modified by client" in canonical_md.read_text(encoding="utf-8")
    # Did NOT leak to agy
    assert not (personal_tideway_config.agy_skills / "sync-skill").exists()

    # 3. Personal Tideway modifies skill content
    atomic_write_text(canonical_md, "# Modified by Personal Tideway\nPersonal Tideway details\n")

    code_sync2 = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code_sync2 == ExitCode.SUCCESS
    assert "Modified by Personal Tideway" in (codex_skill / "SKILL.md").read_text(encoding="utf-8")
