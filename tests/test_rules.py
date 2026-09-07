"""Tests for rule composition, byte-for-byte preservation, and malformed marker detection."""

from pathlib import Path
import pytest

from personal_tideway.cli.main import main
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import (
    CLIENT_CODEX,
    ExitCode,
    RULE_MARKER_END,
    RULE_MARKER_START,
)
from personal_tideway.core.rules import get_composed_rules_for_client
from personal_tideway.utils import atomic_write_text


def test_rules_deterministic_composition_order(personal_tideway_config: PersonalTidewayConfig):
    """Test deterministic composition order: shared/*.md then client/*.md by filename."""
    # Shared fragments
    atomic_write_text(personal_tideway_config.rules_shared / "20_style.md", "Rule 20: Code style.")
    atomic_write_text(personal_tideway_config.rules_shared / "10_safety.md", "Rule 10: Safety first.")

    # Codex-specific fragment
    atomic_write_text(personal_tideway_config.rules_codex / "30_codex.md", "Rule 30: Codex specific instructions.")

    composed = get_composed_rules_for_client(personal_tideway_config, CLIENT_CODEX)

    # 10_safety must come before 20_style, followed by 30_codex
    assert composed == "Rule 10: Safety first.\n\nRule 20: Code style.\n\nRule 30: Codex specific instructions."


def test_rules_preserves_outer_text_byte_for_byte(personal_tideway_config: PersonalTidewayConfig):
    """Test that outer text is preserved byte-for-byte on legitimate Personal Tideway-to-client propagation."""
    prefix_text = "### USER CUSTOM HEADER\nKeep these exact spaces and lines.\n\n"
    suffix_text = "\n\n### USER FOOTER\nStrictly preserve this byte-identical suffix."

    # 1. Establish base first with identical initial rules
    atomic_write_text(personal_tideway_config.rules_shared / "base_rule.md", "Base initial rule.")
    initial_agents = f"{prefix_text}{RULE_MARKER_START}\nBase initial rule.\n{RULE_MARKER_END}{suffix_text}"
    personal_tideway_config.codex_rules.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(personal_tideway_config.codex_rules, initial_agents)

    code_base = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code_base == ExitCode.SUCCESS

    # 2. Add a new rule fragment in Personal Tideway (legitimate Personal Tideway-to-client propagation)
    atomic_write_text(personal_tideway_config.rules_shared / "new_rule.md", "Brand new Personal Tideway rule.")

    code_prop = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code_prop == ExitCode.SUCCESS

    content = personal_tideway_config.codex_rules.read_text(encoding="utf-8")
    assert content.startswith(prefix_text)
    assert content.endswith(suffix_text)
    assert "Brand new Personal Tideway rule." in content
    assert "Base initial rule." in content


def test_rules_initial_divergence_conflicts_without_overwrite(personal_tideway_config: PersonalTidewayConfig):
    """Test that initial divergence on first sync creates a conflict without overwriting client file."""
    prefix_text = "### USER HEADER\n"
    suffix_text = "\n### USER FOOTER\n"
    initial_agents = f"{prefix_text}{RULE_MARKER_START}\npre-existing divergent rules\n{RULE_MARKER_END}{suffix_text}"
    personal_tideway_config.codex_rules.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(personal_tideway_config.codex_rules, initial_agents)

    # Different rule in Personal Tideway
    atomic_write_text(personal_tideway_config.rules_shared / "rule.md", "Personal Tideway initial rule.")

    code = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code == ExitCode.CONFLICT

    # Client file must NOT be overwritten
    assert personal_tideway_config.codex_rules.read_text(encoding="utf-8") == initial_agents
    assert (personal_tideway_config.conflicts_dir / "rules_codex.json").is_file()


def test_rules_client_only_change_propagates_bidirectionally(personal_tideway_config: PersonalTidewayConfig):
    """Test that client-only change to managed block propagates to Personal Tideway client fragment and is idempotent."""
    # 1. Establish base
    atomic_write_text(personal_tideway_config.rules_shared / "shared.md", "Shared company policy.")
    code_base = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code_base == ExitCode.SUCCESS

    # 2. Client modifies managed block
    prefix_text = "### USER HEADER\n"
    suffix_text = "\n### USER FOOTER\n"
    new_client_managed = "Shared company policy.\n\nCodex-local custom instructions."
    updated_file_content = f"{prefix_text}{RULE_MARKER_START}\n{new_client_managed}\n{RULE_MARKER_END}{suffix_text}"
    atomic_write_text(personal_tideway_config.codex_rules, updated_file_content)

    # 3. Sync propagates client change back to Personal Tideway
    code_sync = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code_sync == ExitCode.SUCCESS

    # Check client-specific fragment was created
    client_frag = personal_tideway_config.rules_codex / "client-managed.md"
    assert client_frag.is_file()
    assert "Codex-local custom instructions." in client_frag.read_text(encoding="utf-8")

    # Outer text preserved byte-for-byte
    reloaded_client = personal_tideway_config.codex_rules.read_text(encoding="utf-8")
    assert reloaded_client.startswith(prefix_text)
    assert reloaded_client.endswith(suffix_text)

    # 4. Next sync must be completely idempotent
    code_sync2 = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code_sync2 == ExitCode.SUCCESS
    assert personal_tideway_config.codex_rules.read_text(encoding="utf-8") == reloaded_client


def test_rules_malformed_marker_stops_with_conflict(personal_tideway_config: PersonalTidewayConfig):
    """Test that duplicate or broken markers trigger conflict without overwriting."""
    malformed_content = f"# Malformed file\n{RULE_MARKER_START}\nContent 1\n{RULE_MARKER_START}\nBroken duplicate"
    personal_tideway_config.codex_rules.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(personal_tideway_config.codex_rules, malformed_content)

    code = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code == ExitCode.CONFLICT

    # File was not overwritten
    assert personal_tideway_config.codex_rules.read_text(encoding="utf-8") == malformed_content
    # Conflict artifact created
    assert (personal_tideway_config.conflicts_dir / "rules_codex.json").is_file()


def test_first_sync_adopts_identical_unmanaged_rules_without_duplication(personal_tideway_config: PersonalTidewayConfig):
    existing = "# Shared rule\n\nKeep this once.\n"
    atomic_write_text(personal_tideway_config.agy_rules, existing)
    atomic_write_text(personal_tideway_config.rules_shared / "imported.md", existing)

    code = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code == ExitCode.SUCCESS
    after = personal_tideway_config.agy_rules.read_text(encoding="utf-8")
    assert after.count("# Shared rule") == 1
    assert after.count("Keep this once.") == 1
    assert after.count(RULE_MARKER_START) == 1
    assert after.count(RULE_MARKER_END) == 1
