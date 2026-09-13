"""Typed continuity assurance evaluator and diagnostic models.

Phase 4B implementation:
- Levels are exactly: hooked, instructed, manual, unavailable.
- This slice implements no native lifecycle hook, so it must never report hooked.
- instructed requires evidence that both exact managed continuity rule and skill are available to that client.
- manual means the Phase 4A CLI bridge remains the only usable path.
- unavailable means continuity/backend is disabled, broken, or uninitialized.
"""

from __future__ import annotations

from personal_tideway.adapters.agy import AgyAdapter
from personal_tideway.adapters.codex import CodexAdapter
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import (
    CLIENT_AGY,
    CLIENT_CODEX,
    CONTINUITY_RULE_FILENAME,
    CONTINUITY_SKILL_NAME,
    SUPPORTED_CLIENTS,
)
from personal_tideway.core.basic_memory_installer import validate_isolated_executable
from personal_tideway.core.basic_memory_runtime import get_basic_memory_layout
from personal_tideway.exceptions import (
    BoundaryError,
    ConfigError,
    RuntimeProbeError,
    ValidationError,
)
from personal_tideway.models import (
    ClientContinuityAssurance,
    ContinuityAssuranceLevel,
    ContinuityAssuranceReport,
    SkillInfo,
)


def is_backend_usable(cfg: PersonalTidewayConfig) -> tuple[bool, str]:
    """Check safe structural availability without claiming runtime health."""
    if not cfg.is_initialized():
        return False, "Personal Tideway workspace is not initialized"

    try:
        layout = get_basic_memory_layout(cfg)
    except (BoundaryError, ConfigError) as e:
        return False, f"Invalid backend layout: {e}"

    if not layout.service_root.is_dir():
        return False, "Basic Memory service directory is missing"

    if not cfg.projects_dir.is_dir():
        return False, "Projects directory is missing"

    if not cfg.projects_yaml.is_file():
        return False, "Projects registry is missing"

    try:
        executable_available = validate_isolated_executable(
            layout.primary_executable,
            service_root=layout.service_root,
            home_boundary=cfg.home,
        )
    except (BoundaryError, RuntimeProbeError):
        return False, "Basic Memory executable is unavailable or unsafe"

    if not executable_available:
        return False, "Basic Memory executable is unavailable or unsafe"

    return True, "Continuity backend executable is available; runtime health is not verified"


def check_client_continuity_rule(cfg: PersonalTidewayConfig, client: str) -> bool:
    """Verify that exact canonical continuity rule content is present in the client's managed rules block."""
    canonical_rule_path = cfg.rules_shared / CONTINUITY_RULE_FILENAME
    if not canonical_rule_path.is_file():
        return False

    try:
        canonical_text = canonical_rule_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return False

    if not canonical_text:
        return False

    if client == CLIENT_CODEX:
        if not cfg.codex_rules.is_file():
            return False
        try:
            codex_adapter = CodexAdapter(cfg.codex_config, cfg.codex_rules, cfg.codex_skills, cfg.backups_dir)
            _, managed, _ = codex_adapter.read_rules_block()
            if not managed or not managed.strip():
                return False
            return canonical_text in managed.strip()
        except (OSError, UnicodeDecodeError, ValidationError):
            return False

    elif client == CLIENT_AGY:
        if not cfg.agy_rules.is_file():
            return False
        try:
            agy_adapter = AgyAdapter(cfg.agy_config, cfg.agy_rules, cfg.agy_skills, cfg.backups_dir)
            _, managed, _ = agy_adapter.read_rules_block()
            if not managed or not managed.strip():
                return False
            return canonical_text in managed.strip()
        except (OSError, UnicodeDecodeError, ValidationError):
            return False

    return False


def check_client_continuity_skill(cfg: PersonalTidewayConfig, client: str) -> bool:
    """Verify that the projected skill matches the canonical shared skill content exactly."""
    canonical_skill_md = cfg.skills_shared / CONTINUITY_SKILL_NAME / "SKILL.md"
    if not canonical_skill_md.is_file():
        return False

    try:
        canonical_text = canonical_skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    if not canonical_text.strip():
        return False

    skills_dir = cfg.codex_skills if client == CLIENT_CODEX else cfg.agy_skills
    skill_path = skills_dir / CONTINUITY_SKILL_NAME

    if not skill_path.exists():
        return False

    try:
        skill = SkillInfo(name=CONTINUITY_SKILL_NAME, path=skill_path, scope=client)
        valid, _ = skill.is_valid()
        if not valid:
            return False

        client_skill_md = skill_path / "SKILL.md"
        if not client_skill_md.is_file():
            return False

        client_text = client_skill_md.read_text(encoding="utf-8")
        return client_text == canonical_text
    except (OSError, UnicodeDecodeError, ValidationError):
        return False


def evaluate_client_assurance(
    cfg: PersonalTidewayConfig,
    client: str,
) -> ClientContinuityAssurance:
    """Evaluate continuity assurance level for a specific client with strict evidence checks."""
    backend_ok, backend_reason = is_backend_usable(cfg)
    if not backend_ok:
        return ClientContinuityAssurance(
            client=client,
            level=ContinuityAssuranceLevel.UNAVAILABLE,
            rule_available=False,
            skill_available=False,
            details=backend_reason,
            hook_verified=False,
        )

    rule_available = check_client_continuity_rule(cfg, client)
    skill_available = check_client_continuity_skill(cfg, client)

    # Phase 4B implements no native lifecycle hook evidence provider; hooked is never emitted.
    hook_verified = False

    if rule_available and skill_available:
        level = ContinuityAssuranceLevel.INSTRUCTED
        details = "Managed continuity rule and skill available"
    else:
        level = ContinuityAssuranceLevel.MANUAL
        if not rule_available and not skill_available:
            details = "Continuity CLI bridge available; rule and skill not projected"
        elif not rule_available:
            details = "Continuity CLI bridge available; managed rule missing or stale"
        else:
            details = "Continuity CLI bridge available; managed skill missing or stale"

    return ClientContinuityAssurance(
        client=client,
        level=level,
        rule_available=rule_available,
        skill_available=skill_available,
        details=details,
        hook_verified=hook_verified,
    )


def evaluate_assurance(
    cfg: PersonalTidewayConfig,
) -> ContinuityAssuranceReport:
    """Evaluate continuity assurance for all supported clients."""
    backend_ok, _ = is_backend_usable(cfg)
    clients_map: dict[str, ClientContinuityAssurance] = {}

    for client in SUPPORTED_CLIENTS:
        clients_map[client] = evaluate_client_assurance(cfg, client)

    levels_summary = ", ".join(f"{c}: {clients_map[c].level}" for c in SUPPORTED_CLIENTS)
    summary = f"Continuity assurance: {levels_summary}"

    return ContinuityAssuranceReport(
        clients=clients_map,
        backend_usable=backend_ok,
        summary=summary,
    )
