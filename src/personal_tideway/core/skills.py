"""Skill management: list, create, link, unlink, and share."""

from pathlib import Path
import shutil
from typing import Any

from personal_tideway.adapters.agy import AgyAdapter
from personal_tideway.adapters.codex import CodexAdapter
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import CLIENT_AGY, CLIENT_CODEX
from personal_tideway.exceptions import ValidationError
from personal_tideway.models import SkillInfo
from personal_tideway.utils import atomic_write_text, ensure_safe_path


DEFAULT_SKILL_TEMPLATE = """# {name}

## Description
Personal Tideway managed skill.

## Usage
Instructions for agents on how to use this skill.
"""


def get_skill_scopes(cfg: PersonalTidewayConfig) -> dict[str, Path]:
    """Return map of scope name to its directory."""
    return {
        "shared": cfg.skills_shared,
        CLIENT_CODEX: cfg.skills_codex,
        CLIENT_AGY: cfg.skills_agy,
    }


def list_all_skills(cfg: PersonalTidewayConfig) -> list[dict[str, Any]]:
    """Enumerate all skills in shared, codex, and agy directories."""
    scopes = get_skill_scopes(cfg)
    results: list[dict[str, Any]] = []

    codex_adapter = CodexAdapter(cfg.codex_config, cfg.codex_rules, cfg.codex_skills, cfg.backups_dir)
    agy_adapter = AgyAdapter(cfg.agy_config, cfg.agy_rules, cfg.agy_skills, cfg.backups_dir)

    codex_installed = set(codex_adapter.list_installed_skills())
    agy_installed = set(agy_adapter.list_installed_skills())

    for scope_name, scope_dir in sorted(scopes.items()):
        if not scope_dir.is_dir():
            continue
        for entry in sorted(scope_dir.iterdir()):
            if not entry.is_dir():
                continue
            skill = SkillInfo(name=entry.name, path=entry, scope=scope_name)
            valid, reason = skill.is_valid()
            results.append({
                "name": entry.name,
                "scope": scope_name,
                "path": str(entry),
                "valid": valid,
                "error": reason if not valid else None,
                "installed_codex": entry.name in codex_installed,
                "installed_agy": entry.name in agy_installed,
            })

    results.sort(key=lambda s: (s["scope"], s["name"]))
    return results


def find_skill(cfg: PersonalTidewayConfig, name: str) -> SkillInfo | None:
    """Find a skill by name across shared, codex, and agy scopes."""
    scopes = get_skill_scopes(cfg)
    # Check shared first, then client-specific
    for scope_name in ["shared", CLIENT_CODEX, CLIENT_AGY]:
        skill_dir = scopes[scope_name] / name
        if skill_dir.is_dir():
            return SkillInfo(name=name, path=skill_dir, scope=scope_name)
    return None


def create_skill(
    cfg: PersonalTidewayConfig,
    name: str,
    scope: str = "shared",
    dry_run: bool = False,
) -> Path:
    """Create a new skill directory with a valid default SKILL.md."""
    scopes = get_skill_scopes(cfg)
    if scope not in scopes:
        raise ValidationError(f"Invalid scope '{scope}'. Supported scopes: {list(scopes.keys())}")

    target_dir = scopes[scope] / name
    ensure_safe_path(target_dir, scopes[scope], "Skill creation")

    if target_dir.exists():
        raise ValidationError(f"Skill '{name}' already exists in scope '{scope}' at {target_dir}")

    # Check for name collisions across other scopes
    existing = find_skill(cfg, name)
    if existing:
        raise ValidationError(
            f"Skill '{name}' already exists in scope '{existing.scope}' at {existing.path}"
        )

    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)
        skill_md = target_dir / "SKILL.md"
        content = DEFAULT_SKILL_TEMPLATE.format(name=name)
        atomic_write_text(skill_md, content)

    return target_dir


def link_skill(
    cfg: PersonalTidewayConfig,
    name: str,
    dry_run: bool = False,
) -> list[str]:
    """Expose skill to relevant clients according to scope and configuration."""
    skill = find_skill(cfg, name)
    if not skill:
        raise ValidationError(f"Skill '{name}' not found in any scope")

    valid, reason = skill.is_valid()
    if not valid:
        raise ValidationError(f"Cannot link invalid skill '{name}': {reason}")

    codex_adapter = CodexAdapter(cfg.codex_config, cfg.codex_rules, cfg.codex_skills, cfg.backups_dir)
    agy_adapter = AgyAdapter(cfg.agy_config, cfg.agy_rules, cfg.agy_skills, cfg.backups_dir)

    actions: list[str] = []
    mode = cfg.skill_link_mode

    if skill.scope in ("shared", CLIENT_CODEX):
        codex_adapter.install_skill(skill.name, skill.path, mode=mode, dry_run=dry_run)
        actions.append(f"Linked skill '{name}' to Codex ({mode})")

    if skill.scope in ("shared", CLIENT_AGY):
        agy_adapter.install_skill(skill.name, skill.path, mode=mode, dry_run=dry_run)
        actions.append(f"Linked skill '{name}' to agy ({mode})")

    return actions


def unlink_skill(
    cfg: PersonalTidewayConfig,
    name: str,
    dry_run: bool = False,
) -> list[str]:
    """Unlink skill from client directories safely."""
    codex_adapter = CodexAdapter(cfg.codex_config, cfg.codex_rules, cfg.codex_skills, cfg.backups_dir)
    agy_adapter = AgyAdapter(cfg.agy_config, cfg.agy_rules, cfg.agy_skills, cfg.backups_dir)

    actions: list[str] = []
    if codex_adapter.uninstall_skill(name, dry_run=dry_run):
        actions.append(f"Unlinked skill '{name}' from Codex")

    if agy_adapter.uninstall_skill(name, dry_run=dry_run):
        actions.append(f"Unlinked skill '{name}' from agy")

    return actions


def share_skill(
    cfg: PersonalTidewayConfig,
    name: str,
    dry_run: bool = False,
) -> Path:
    """Promote a client-specific skill to shared after validating SKILL.md and both adapters."""
    # 1. Look for skill in client-specific scopes
    source_skill: SkillInfo | None = None
    for scope_name in (CLIENT_CODEX, CLIENT_AGY):
        p = get_skill_scopes(cfg)[scope_name] / name
        if p.is_dir():
            source_skill = SkillInfo(name=name, path=p, scope=scope_name)
            break

    if not source_skill:
        # Check if already shared
        shared_path = cfg.skills_shared / name
        if shared_path.is_dir():
            raise ValidationError(f"Skill '{name}' is already in shared scope")
        raise ValidationError(f"Client-specific skill '{name}' not found in codex or agy scopes")

    # 2. Reject name collisions in shared
    dest_path = cfg.skills_shared / name
    ensure_safe_path(dest_path, cfg.skills_shared, "Shared skill destination")
    if dest_path.exists():
        raise ValidationError(f"Skill collision: '{name}' already exists in shared folder")

    # 3. Validate SKILL.md
    valid, reason = source_skill.is_valid()
    if not valid:
        raise ValidationError(f"Skill '{name}' has invalid SKILL.md: {reason}")

    # 4. Confirm both adapters can expose it
    codex_adapter = CodexAdapter(cfg.codex_config, cfg.codex_rules, cfg.codex_skills, cfg.backups_dir)
    agy_adapter = AgyAdapter(cfg.agy_config, cfg.agy_rules, cfg.agy_skills, cfg.backups_dir)

    c_ok, c_msg = codex_adapter.validate_skill_support(source_skill.path)
    if not c_ok:
        raise ValidationError(f"Codex adapter rejected skill '{name}': {c_msg}")

    a_ok, a_msg = agy_adapter.validate_skill_support(source_skill.path)
    if not a_ok:
        raise ValidationError(f"agy adapter rejected skill '{name}': {a_msg}")

    # 5. Move skill to shared and re-link to both clients
    if not dry_run:
        shutil.move(str(source_skill.path), str(dest_path))
        # Re-link into both clients
        codex_adapter.install_skill(name, dest_path, mode=cfg.skill_link_mode, dry_run=False)
        agy_adapter.install_skill(name, dest_path, mode=cfg.skill_link_mode, dry_run=False)

    return dest_path
