"""Tests for Phase 4B: canonical continuity policy, skill provisioning, projection, and truthful assurance reporting."""

import inspect
import json
import shutil
from pathlib import Path

from personal_tideway.cli.main import main
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import (
    ASSURANCE_LEVEL_HOOKED,
    ASSURANCE_LEVEL_INSTRUCTED,
    ASSURANCE_LEVEL_MANUAL,
    ASSURANCE_LEVEL_UNAVAILABLE,
    CLIENT_AGY,
    CLIENT_CODEX,
    CONTINUITY_RULE_FILENAME,
    CONTINUITY_SKILL_NAME,
    RULE_MARKER_END,
    RULE_MARKER_START,
    ExitCode,
)
from personal_tideway.core.assurance import (
    check_client_continuity_rule,
    check_client_continuity_skill,
    evaluate_assurance,
    evaluate_client_assurance,
    is_backend_usable,
)
from personal_tideway.core.basic_memory_runtime import get_basic_memory_layout
from personal_tideway.core.workspace import (
    LEGACY_CONTINUITY_SKILL_TEMPLATE,
    init_workspace,
)
from personal_tideway.models import ContinuityAssuranceLevel, SkillInfo
from personal_tideway.utils import atomic_write_text


def make_backend_executable_available(cfg: PersonalTidewayConfig) -> None:
    """Create a harmless executable fixture without claiming a successful health probe."""
    executable = get_basic_memory_layout(cfg).primary_executable
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)


def test_fresh_init_provisions_canonical_policy_and_skill(tmp_path: Path):
    """Fresh disposable init creates valid canonical policy and skill only inside the configured PTW home."""
    ptw_home = tmp_path / "fresh_ptw"
    project_source = tmp_path / "user_repo"
    project_source.mkdir(parents=True, exist_ok=True)

    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    init_workspace(cfg)

    # 1. Check canonical rule
    rule_file = cfg.rules_shared / CONTINUITY_RULE_FILENAME
    assert rule_file.is_file(), f"Expected continuity rule at {rule_file}"
    rule_text = rule_file.read_text(encoding="utf-8")

    # Policy contents validation
    assert "Personal Tideway Continuity Policy" in rule_text
    assert "ptw context show" in rule_text
    assert "ptw context search" in rule_text
    assert "ptw checkpoint" in rule_text
    assert "concise" in rule_text.lower()
    assert "no secrets" in rule_text.lower()
    assert "continuity" in rule_text.lower()

    # 2. Check canonical skill
    skill_dir = cfg.skills_shared / CONTINUITY_SKILL_NAME
    assert skill_dir.is_dir(), f"Expected skill dir at {skill_dir}"
    skill = SkillInfo(name=CONTINUITY_SKILL_NAME, path=skill_dir, scope="shared")
    valid, reason = skill.is_valid()
    assert valid, f"Canonical skill is not valid: {reason}"

    skill_text = skill.skill_md_path.read_text(encoding="utf-8")
    assert skill_text.startswith("---\nname: continuity\n")
    assert "description:" in skill_text.split("---", 2)[1]
    assert "Personal Tideway Continuity Skill" in skill_text
    assert "ptw context show" in skill_text
    assert "ptw context search" in skill_text
    assert "ptw checkpoint" in skill_text
    assert "--source-client" not in skill_text
    assert "--file" in skill_text
    assert "--stdin" in skill_text
    assert "condition" in skill_text
    assert "objective" in skill_text
    assert "completed" in skill_text
    assert "blockers" in skill_text
    assert "verification_status" in skill_text
    assert "next_safe_action" in skill_text
    assert "source_client" in skill_text
    assert "evidence" in skill_text

    # 3. Verify zero contamination in project_source
    assert list(project_source.iterdir()) == []


def test_init_idempotency_and_non_overwrite_of_user_edits(tmp_path: Path):
    """Repeat init is byte-stable and existing valid user edits are preserved without overwrite."""
    ptw_home = tmp_path / "ptw_idempotency"
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    init_workspace(cfg)

    rule_file = cfg.rules_shared / CONTINUITY_RULE_FILENAME
    skill_file = cfg.skills_shared / CONTINUITY_SKILL_NAME / "SKILL.md"

    # User customizes the policy and skill
    custom_rule = "# Custom Team Continuity Policy\nDo not overwrite.\n"
    custom_skill = "# Custom Team Continuity Skill\nUser modified content.\n"
    atomic_write_text(rule_file, custom_rule)
    atomic_write_text(skill_file, custom_skill)

    mtime_rule_before = rule_file.stat().st_mtime_ns
    mtime_skill_before = skill_file.stat().st_mtime_ns

    # Re-run init
    init_workspace(cfg)

    # Custom content preserved
    assert rule_file.read_text(encoding="utf-8") == custom_rule
    assert skill_file.read_text(encoding="utf-8") == custom_skill
    assert rule_file.stat().st_mtime_ns == mtime_rule_before
    assert skill_file.stat().st_mtime_ns == mtime_skill_before


def test_init_upgrades_only_exact_legacy_continuity_skill(tmp_path: Path):
    """The shipped pre-frontmatter template is upgraded without replacing user edits."""
    cfg = PersonalTidewayConfig.resolve(home=tmp_path / "ptw_upgrade")
    init_workspace(cfg)
    skill_file = cfg.skills_shared / CONTINUITY_SKILL_NAME / "SKILL.md"
    atomic_write_text(skill_file, LEGACY_CONTINUITY_SKILL_TEMPLATE)

    dry_actions = init_workspace(cfg, dry_run=True)
    assert "upgrade_file:skills/shared/continuity/SKILL.md" in dry_actions
    assert skill_file.read_text(encoding="utf-8") == LEGACY_CONTINUITY_SKILL_TEMPLATE

    init_workspace(cfg)
    upgraded = skill_file.read_text(encoding="utf-8")
    assert upgraded.startswith("---\nname: continuity\n")
    assert upgraded.endswith(LEGACY_CONTINUITY_SKILL_TEMPLATE)


def test_policy_and_skill_bounded_and_no_native_hook_claims(tmp_path: Path):
    """Always-on policy stays compact and contains no native hook claims or secret exposures."""
    ptw_home = tmp_path / "ptw_bounded"
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    init_workspace(cfg)

    rule_file = cfg.rules_shared / CONTINUITY_RULE_FILENAME
    rule_lines = rule_file.read_text(encoding="utf-8").splitlines()

    # Compactness invariant
    assert len(rule_lines) <= 50, f"Policy is too verbose ({len(rule_lines)} lines)"
    assert len(rule_file.read_text(encoding="utf-8")) <= 2500

    rule_lower = rule_file.read_text(encoding="utf-8").lower()
    # No false claims of guaranteed automatic hooks
    assert "hook installed" not in rule_lower
    assert "guaranteed auto-capture" not in rule_lower
    assert "automatic background" not in rule_lower


def test_sync_dry_run_zero_mutation_with_continuity(personal_tideway_config: PersonalTidewayConfig):
    """ptw sync --dry-run reports intended projections with zero filesystem mutations."""
    assert not personal_tideway_config.codex_rules.exists()
    assert not personal_tideway_config.agy_rules.exists()
    assert not (personal_tideway_config.codex_skills / CONTINUITY_SKILL_NAME).exists()
    assert not (personal_tideway_config.agy_skills / CONTINUITY_SKILL_NAME).exists()

    state_before = personal_tideway_config.state_file.read_text(encoding="utf-8")

    code = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync", "--dry-run",
    ])
    assert code == ExitCode.SUCCESS

    # Zero mutation in client directories
    assert not personal_tideway_config.codex_rules.exists()
    assert not personal_tideway_config.agy_rules.exists()
    assert not (personal_tideway_config.codex_skills / CONTINUITY_SKILL_NAME).exists()
    assert not (personal_tideway_config.agy_skills / CONTINUITY_SKILL_NAME).exists()

    # Zero mutation in backups or state
    assert list(personal_tideway_config.backups_dir.glob("*")) == []
    assert personal_tideway_config.state_file.read_text(encoding="utf-8") == state_before


def test_sync_projections_for_both_clients(personal_tideway_config: PersonalTidewayConfig):
    """sync applies through current adapters, and a second sync is clean."""
    # 1. First sync applies rules and skills to both clients
    code1 = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code1 == ExitCode.SUCCESS

    # Check Codex rule projection
    assert personal_tideway_config.codex_rules.is_file()
    codex_rules_text = personal_tideway_config.codex_rules.read_text(encoding="utf-8")
    assert RULE_MARKER_START in codex_rules_text
    assert "Personal Tideway Continuity Policy" in codex_rules_text
    assert RULE_MARKER_END in codex_rules_text

    # Check AGY rule projection
    assert personal_tideway_config.agy_rules.is_file()
    agy_rules_text = personal_tideway_config.agy_rules.read_text(encoding="utf-8")
    assert RULE_MARKER_START in agy_rules_text
    assert "Personal Tideway Continuity Policy" in agy_rules_text
    assert RULE_MARKER_END in agy_rules_text

    # Check Codex skill projection
    codex_skill = personal_tideway_config.codex_skills / CONTINUITY_SKILL_NAME
    assert codex_skill.exists()
    codex_skill_info = SkillInfo(name=CONTINUITY_SKILL_NAME, path=codex_skill, scope="codex")
    valid_c, _ = codex_skill_info.is_valid()
    assert valid_c

    # Check AGY skill projection
    agy_skill = personal_tideway_config.agy_skills / CONTINUITY_SKILL_NAME
    assert agy_skill.exists()
    agy_skill_info = SkillInfo(name=CONTINUITY_SKILL_NAME, path=agy_skill, scope="agy")
    valid_a, _ = agy_skill_info.is_valid()
    assert valid_a

    # 2. Second sync is clean and produces no mutations
    state_after_first = personal_tideway_config.state_file.read_text(encoding="utf-8")
    codex_rules_mtime = personal_tideway_config.codex_rules.stat().st_mtime_ns
    agy_rules_mtime = personal_tideway_config.agy_rules.stat().st_mtime_ns

    code2 = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert code2 == ExitCode.SUCCESS
    assert personal_tideway_config.state_file.read_text(encoding="utf-8") == state_after_first
    assert personal_tideway_config.codex_rules.stat().st_mtime_ns == codex_rules_mtime
    assert personal_tideway_config.agy_rules.stat().st_mtime_ns == agy_rules_mtime


def test_assurance_levels_instructed_after_sync(personal_tideway_config: PersonalTidewayConfig):
    """Assurance output reports instructed for both clients after sync with verified evidence."""
    make_backend_executable_available(personal_tideway_config)
    report_pre = evaluate_assurance(personal_tideway_config)
    assert report_pre.backend_usable is True
    assert report_pre.clients[CLIENT_CODEX].level == ASSURANCE_LEVEL_MANUAL
    assert report_pre.clients[CLIENT_AGY].level == ASSURANCE_LEVEL_MANUAL

    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])

    report_post = evaluate_assurance(personal_tideway_config)
    codex_ass = report_post.clients[CLIENT_CODEX]
    agy_ass = report_post.clients[CLIENT_AGY]

    assert codex_ass.level == ASSURANCE_LEVEL_INSTRUCTED
    assert codex_ass.rule_available is True
    assert codex_ass.skill_available is True
    assert codex_ass.hook_verified is False
    assert "Managed continuity rule and skill available" in codex_ass.details

    assert agy_ass.level == ASSURANCE_LEVEL_INSTRUCTED
    assert agy_ass.rule_available is True
    assert agy_ass.skill_available is True
    assert agy_ass.hook_verified is False
    assert "Managed continuity rule and skill available" in agy_ass.details


def test_exact_rule_evidence_and_stale_downgrade(personal_tideway_config: PersonalTidewayConfig):
    """Rule evidence requires exact canonical rule content; modified canonical rule downgrades to manual until sync."""
    make_backend_executable_available(personal_tideway_config)
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert check_client_continuity_rule(personal_tideway_config, CLIENT_CODEX) is True

    # User modifies canonical policy in workspace (stale client projection)
    canonical_rule = personal_tideway_config.rules_shared / CONTINUITY_RULE_FILENAME
    atomic_write_text(canonical_rule, "# Updated Canonical Continuity Policy\nNew mandatory guidance.\n")

    # Client projection does not match exact new canonical content -> downgrade to manual
    assert check_client_continuity_rule(personal_tideway_config, CLIENT_CODEX) is False
    ass = evaluate_client_assurance(personal_tideway_config, CLIENT_CODEX)
    assert ass.level == ASSURANCE_LEVEL_MANUAL
    assert ass.rule_available is False
    assert "managed rule missing or stale" in ass.details

    # Re-syncing brings client projection up to date
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert check_client_continuity_rule(personal_tideway_config, CLIENT_CODEX) is True
    assert evaluate_client_assurance(personal_tideway_config, CLIENT_CODEX).level == ASSURANCE_LEVEL_INSTRUCTED


def test_exact_skill_evidence_and_stale_tampered_downgrade(personal_tideway_config: PersonalTidewayConfig):
    """Skill evidence requires byte-equal canonical content; tampered or stale SKILL.md downgrades to manual."""
    make_backend_executable_available(personal_tideway_config)
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    assert check_client_continuity_skill(personal_tideway_config, CLIENT_CODEX) is True

    # Tamper with client SKILL.md
    client_dir = personal_tideway_config.codex_skills / CONTINUITY_SKILL_NAME
    client_skill_file = client_dir / "SKILL.md"
    if client_dir.is_symlink():
        client_dir.unlink()
        client_dir.mkdir(parents=True)
        client_skill_file = client_dir / "SKILL.md"
    atomic_write_text(client_skill_file, "# Tampered or outdated skill\nNot matching canonical.\n")

    assert check_client_continuity_skill(personal_tideway_config, CLIENT_CODEX) is False
    ass = evaluate_client_assurance(personal_tideway_config, CLIENT_CODEX)
    assert ass.level == ASSURANCE_LEVEL_MANUAL
    assert ass.skill_available is False
    assert "managed skill missing or stale" in ass.details


def test_direct_impossibility_of_hooked_in_phase_4b(personal_tideway_config: PersonalTidewayConfig):
    """In Phase 4B evaluate_client_assurance has no hook bypass and cannot emit hooked."""
    make_backend_executable_available(personal_tideway_config)
    sig = inspect.signature(evaluate_client_assurance)
    # Must only accept (cfg, client) without any hook_verified or shortcut parameters
    assert list(sig.parameters.keys()) == ["cfg", "client"]

    # Even with all components present, level must not be hooked
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])

    report = evaluate_assurance(personal_tideway_config)
    for client_name in (CLIENT_CODEX, CLIENT_AGY):
        ass = report.clients[client_name]
        assert ass.level != ASSURANCE_LEVEL_HOOKED
        assert ass.level != ContinuityAssuranceLevel.HOOKED.value
        assert ass.hook_verified is False

    # Enum defines hooked for future phases, but evaluator never produces it
    assert ContinuityAssuranceLevel.HOOKED.value == "hooked"


def test_assurance_downgrade_to_manual_when_skill_or_rule_missing(personal_tideway_config: PersonalTidewayConfig):
    """Assurance downgrades to manual when either rule or skill is missing from a client."""
    make_backend_executable_available(personal_tideway_config)
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])

    # 1. Remove skill from Codex
    codex_skill_link = personal_tideway_config.codex_skills / CONTINUITY_SKILL_NAME
    if codex_skill_link.is_symlink() or codex_skill_link.is_file():
        codex_skill_link.unlink()
    elif codex_skill_link.is_dir():
        shutil.rmtree(codex_skill_link)

    report1 = evaluate_assurance(personal_tideway_config)
    assert report1.clients[CLIENT_CODEX].level == ASSURANCE_LEVEL_MANUAL
    assert report1.clients[CLIENT_CODEX].rule_available is True
    assert report1.clients[CLIENT_CODEX].skill_available is False
    assert "managed skill missing" in report1.clients[CLIENT_CODEX].details

    # AGY remains instructed
    assert report1.clients[CLIENT_AGY].level == ASSURANCE_LEVEL_INSTRUCTED

    # 2. Corrupt/remove rules block from AGY
    atomic_write_text(personal_tideway_config.agy_rules, "# Unmanaged AGY rules without PTW block\n")

    report2 = evaluate_assurance(personal_tideway_config)
    assert report2.clients[CLIENT_AGY].level == ASSURANCE_LEVEL_MANUAL
    assert report2.clients[CLIENT_AGY].rule_available is False
    assert report2.clients[CLIENT_AGY].skill_available is True
    assert "managed rule missing" in report2.clients[CLIENT_AGY].details


def test_assurance_unavailable_when_uninitialized_or_backend_broken(tmp_path: Path):
    """Assurance reports unavailable when workspace is uninitialized or backend service is broken."""
    empty_home = tmp_path / "empty_ptw"
    empty_home.mkdir(parents=True)
    cfg_uninit = PersonalTidewayConfig.resolve(home=empty_home)

    report_uninit = evaluate_assurance(cfg_uninit)
    assert report_uninit.backend_usable is False
    assert report_uninit.clients[CLIENT_CODEX].level == ASSURANCE_LEVEL_UNAVAILABLE
    assert report_uninit.clients[CLIENT_AGY].level == ASSURANCE_LEVEL_UNAVAILABLE
    assert "not initialized" in report_uninit.clients[CLIENT_CODEX].details

    # Initialize workspace: its directory skeleton alone is not backend evidence.
    init_workspace(cfg_uninit)
    report_init = evaluate_assurance(cfg_uninit)
    assert report_init.backend_usable is False
    assert report_init.clients[CLIENT_CODEX].level == ASSURANCE_LEVEL_UNAVAILABLE

    make_backend_executable_available(cfg_uninit)
    report_executable = evaluate_assurance(cfg_uninit)
    assert report_executable.backend_usable is True
    _, backend_details = is_backend_usable(cfg_uninit)
    assert "runtime health is not verified" in backend_details

    # Remove basic memory service dir
    shutil.rmtree(cfg_uninit.basic_memory_dir)
    report_broken = evaluate_assurance(cfg_uninit)
    assert report_broken.backend_usable is False
    assert report_broken.clients[CLIENT_CODEX].level == ASSURANCE_LEVEL_UNAVAILABLE
    assert "Basic Memory" in report_broken.clients[CLIENT_CODEX].details


def test_cli_status_and_doctor_text_and_json_assurance(personal_tideway_config: PersonalTidewayConfig, capsys):
    """status and doctor commands surface continuity assurance in text and JSON output."""
    make_backend_executable_available(personal_tideway_config)
    # 1. ptw status before sync -> manual
    code_st1 = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "status",
    ])
    assert code_st1 == ExitCode.SUCCESS
    out_st1 = capsys.readouterr().out
    assert "Continuity Assurance:" in out_st1
    assert f"- {CLIENT_CODEX}: manual" in out_st1
    assert f"- {CLIENT_AGY}: manual" in out_st1

    # 2. Sync to instructed
    main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "sync",
    ])
    capsys.readouterr()

    # 3. ptw status --json after sync -> instructed
    code_st_json = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "status", "--json",
    ])
    assert code_st_json == ExitCode.SUCCESS
    st_json = json.loads(capsys.readouterr().out)
    assert "continuity_assurance" in st_json
    ass_json = st_json["continuity_assurance"]
    assert ass_json["backend_usable"] is True
    assert ass_json["clients"][CLIENT_CODEX]["level"] == "instructed"
    assert ass_json["clients"][CLIENT_AGY]["level"] == "instructed"

    # 4. ptw doctor text
    code_doc = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "doctor",
    ])
    assert code_doc == ExitCode.SUCCESS
    out_doc = capsys.readouterr().out
    assert "Continuity Assurance:" in out_doc
    assert f"- {CLIENT_CODEX}: instructed" in out_doc
    assert f"- {CLIENT_AGY}: instructed" in out_doc

    # 5. ptw doctor --json
    code_doc_json = main([
        "--home", str(personal_tideway_config.home),
        "--codex-home", str(personal_tideway_config.codex_home),
        "--gemini-home", str(personal_tideway_config.gemini_home),
        "doctor", "--json",
    ])
    assert code_doc_json == ExitCode.SUCCESS
    doc_json = json.loads(capsys.readouterr().out)
    assert "continuity_assurance" in doc_json
    assert doc_json["continuity_assurance"]["clients"][CLIENT_CODEX]["level"] == "instructed"
    assert doc_json["continuity_assurance"]["clients"][CLIENT_AGY]["level"] == "instructed"
