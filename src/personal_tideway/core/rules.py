"""Composition and application of rules into AGENTS.md and GEMINI.md."""

from pathlib import Path
from typing import Literal

from personal_tideway.adapters.agy import AgyAdapter
from personal_tideway.adapters.codex import CodexAdapter
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import CLIENT_AGY, CLIENT_CODEX
from personal_tideway.exceptions import ValidationError


def compose_rules_content(shared_dir: Path, client_dir: Path) -> str:
    """Compose shared and client-specific rule markdown fragments in deterministic order.

    Order:
    1. shared/*.md (sorted by filename)
    2. client/*.md (sorted by filename)
    """
    fragments: list[str] = []

    # 1. Shared rules
    if shared_dir.is_dir():
        for f in sorted(shared_dir.glob("*.md")):
            text = f.read_text(encoding="utf-8").strip()
            if text:
                fragments.append(text)

    # 2. Client rules
    if client_dir.is_dir():
        for f in sorted(client_dir.glob("*.md")):
            text = f.read_text(encoding="utf-8").strip()
            if text:
                fragments.append(text)

    return "\n\n".join(fragments)


def get_composed_rules_for_client(cfg: PersonalTidewayConfig, client: str) -> str:
    """Get the composed rules text for codex or agy."""
    if client == CLIENT_CODEX:
        return compose_rules_content(cfg.rules_shared, cfg.rules_codex)
    elif client == CLIENT_AGY:
        return compose_rules_content(cfg.rules_shared, cfg.rules_agy)
    else:
        raise ValidationError(f"Unknown client '{client}'")


def apply_rules(
    cfg: PersonalTidewayConfig,
    client: Literal["codex", "agy"],
    dry_run: bool = False,
) -> bool:
    """Compose rules and update the client's rules file marker block atomically.

    Preserves text outside markers byte-for-byte.
    """
    composed = get_composed_rules_for_client(cfg, client)

    if client == CLIENT_CODEX:
        adapter = CodexAdapter(cfg.codex_config, cfg.codex_rules, cfg.codex_skills, cfg.backups_dir)
    elif client == CLIENT_AGY:
        adapter = AgyAdapter(cfg.agy_config, cfg.agy_rules, cfg.agy_skills, cfg.backups_dir)
    else:
        raise ValidationError(f"Unknown client '{client}'")

    return adapter.write_rules_block(composed, dry_run=dry_run)
