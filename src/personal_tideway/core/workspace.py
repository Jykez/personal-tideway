"""Workspace initialization and structure verification."""

import json
import os
from pathlib import Path
import yaml

from personal_tideway.config import PersonalTidewayConfig, validate_owned_path
from personal_tideway.constants import (
    CONTINUITY_RULE_FILENAME,
    CONTINUITY_SKILL_NAME,
    DEFAULT_CONFIG_YAML,
    DEFAULT_SECRETS_ENV,
    SCHEMA_VERSION,
)
from personal_tideway.exceptions import ConfigError
from personal_tideway.utils import atomic_write_text


DEFAULT_SECRETS_TEMPLATE = """# Personal Tideway Secrets
# Strict KEY=VALUE lines only. Mode 0600.
# Example:
# OPENAI_API_KEY=sk-...
"""

DEFAULT_PROJECTS_REGISTRY_TEMPLATE = f"""# Personal Tideway Projects Registry
version: {SCHEMA_VERSION}
projects: []
"""

DEFAULT_CONTINUITY_RULE_TEMPLATE = """# Personal Tideway Continuity Policy

1. Context resolution & recall:
   - At session or task start on a registered project, recall bounded current state using `ptw context show`.
   - Search memory (`ptw context search QUERY`) before guessing about prior work, decisions, or project conventions.

2. Checkpoint triggers:
   - Create a checkpoint (`ptw checkpoint`) when meaningful work is completed, key architectural decisions are made, external system state changes, the task ends with pending items or blockers, or context compaction is imminent.

3. Concise retention:
   - Store only curated facts, verified outcomes, active blockers, and the next safe action.
   - Never store raw conversation transcripts, verbose logs, or full file dumps.

4. No secrets:
   - Never record credentials, tokens, API keys, passwords, or private environment variables in checkpoints.

5. Verified persistence:
   - Ensure checkpoints are explicitly recorded and verified through the Personal Tideway CLI (`written` or `unchanged`).
   - Do not assume unverified background capture or native lifecycle hooks without explicit CLI confirmation.

6. On-demand skill:
   - Consult the `continuity` skill for command usage, schema parameters, character budgets, and structured checkpoint templates.
"""

DEFAULT_CONTINUITY_SKILL_TEMPLATE = """# Personal Tideway Continuity Skill

## Description
Shared continuity skill for Codex and Antigravity. Guides context retrieval and bounded, idempotent project checkpoints via the Personal Tideway CLI.

## Bounded Context Retrieval
Retrieve current project context before planning or implementing changes:

```bash
# Show current-state briefing for active project (bounded by character cap, default 8000)
ptw context show [--project <project-id>] [--budget <chars>] [--json]

# Search project notes for prior decisions or facts before guessing
ptw context search "<query>" [--project <project-id>] [--budget <chars>] [--limit <items>] [--json]
```

Guidelines:
- Default to current working directory resolution; pass `--project` when targeting a specific registered project.
- Respect budget limits to avoid token saturation.
- Ambiguous project resolution fails closed; register projects with `ptw project register` if needed.

## Checkpoint Guidance
Persist concise project state when milestone tasks finish, blockers arise, or context compacts:

```bash
# Pass structured checkpoint JSON via file or stdin
ptw checkpoint [--project <project-id>] [--dry-run] (--file <path> | --stdin) [--json]
```

### Checkpoint Schema (max 64 KiB)
The payload must be a JSON object containing:
- `condition`: String describing current project state/environment.
- `objective`: String describing the milestone or immediate goal.
- `completed`: Array of strings detailing verified completed work.
- `blockers`: Array of strings detailing active blockers (empty if none).
- `verification_status`: One of `"verified"`, `"partial"`, `"planned"`, `"blocked"`.
- `next_safe_action`: String specifying the immediate next action to take.
- `source_client`: String, `"codex"` or `"agy"`.
- `evidence`: Array of strings referencing relative paths or test outcomes (no absolute paths or `..`).

### Example Checkpoint Payload
```json
{
  "condition": "Development workspace configured, core models implemented",
  "objective": "Complete Phase 4B continuity policy and assurance reporting",
  "completed": [
    "Provisioned canonical continuity rule and skill on workspace init",
    "Implemented typed assurance evaluator and CLI status reporting"
  ],
  "blockers": [],
  "verification_status": "verified",
  "next_safe_action": "Run focused test suite to verify idempotency and non-overwrite",
  "source_client": "codex",
  "evidence": [
    "tests/test_continuity_assurance.py:45",
    "docs/PERSONAL_TIDEWAY_V2_SPEC.md#L525"
  ]
}
```

### Operational Rules
- **Idempotency**: Submitting an identical checkpoint payload is safely skipped (`unchanged`).
- **Dry-run**: Test persistence with `--dry-run` to preview changes without mutating memory.
- **Safety**: Do NOT embed secrets, tokens, API keys, or raw chat transcripts. Payloads containing secret patterns are rejected.
"""


def init_workspace(cfg: PersonalTidewayConfig) -> None:
    """Initialize canonical Personal Tideway v2 central directory tree, secrets.env, config.yaml, and registry."""
    if cfg.version != SCHEMA_VERSION:
        raise ConfigError(
            f"Cannot initialize workspace with version {cfg.version}. Workspace requires migration to version {SCHEMA_VERSION}."
        )

    # List of all canonical directories
    canonical_dirs = [
        (cfg.home, True),
        (cfg.registry_dir, False),
        (cfg.projects_dir, False),
        (cfg.knowledge_dir, False),
        (cfg.knowledge_personal_dir, False),
        (cfg.mcp_dir, False),
        (cfg.rules_dir, False),
        (cfg.rules_shared, False),
        (cfg.rules_codex, False),
        (cfg.rules_agy, False),
        (cfg.skills_dir, False),
        (cfg.skills_shared, False),
        (cfg.skills_codex, False),
        (cfg.skills_agy, False),
        (cfg.services_dir, False),
        (cfg.basic_memory_dir, False),
        (cfg.state_dir, False),
        (cfg.conflicts_dir, False),
        (cfg.backups_dir, False),
        (cfg.locks_dir, False),
        (cfg.skills_shared / CONTINUITY_SKILL_NAME, False),
    ]

    continuity_rule_path = cfg.rules_shared / CONTINUITY_RULE_FILENAME
    continuity_skill_dir = cfg.skills_shared / CONTINUITY_SKILL_NAME
    continuity_skill_md = continuity_skill_dir / "SKILL.md"

    # List of all canonical files
    canonical_files = [
        cfg.secrets_env,
        cfg.config_yaml,
        cfg.projects_yaml,
        cfg.state_file,
        continuity_rule_path,
        continuity_skill_md,
    ]

    # --- Preflight Phase ---
    # Validate cfg.home and every canonical directory target
    for d, allow_root in canonical_dirs:
        validate_owned_path(d, root=cfg.home, allow_root=allow_root)

        # Check existing path: workspace root itself can be a symlink, but child layout must not be a symlink
        if not allow_root and d.is_symlink():
            raise ConfigError(f"Directory target '{d}' is a symlink, which is not permitted in child layout.")

        if d.exists() and not d.is_dir():
            raise ConfigError(f"Directory target '{d}' exists but is not a directory.")

    # Validate every canonical file target
    for f in canonical_files:
        validate_owned_path(f, root=cfg.home, allow_root=False)

        if f.is_symlink():
            raise ConfigError(f"File target '{f}' is a symlink, which is not permitted in child layout.")

        if f.exists() and not f.is_file():
            raise ConfigError(f"File target '{f}' exists but is not a regular file.")

    # --- Mutation Phase ---
    # 1. Create canonical v2 directory tree
    for d, _ in canonical_dirs:
        d.mkdir(parents=True, exist_ok=True)

    # 2. Initialize secrets.env with mode 0600 (NEVER overwritten by init)
    if not cfg.secrets_env.exists():
        atomic_write_text(cfg.secrets_env, DEFAULT_SECRETS_TEMPLATE, mode=0o600)
    else:
        # Ensure existing secrets.env maintains strict 0600 permissions
        try:
            os.chmod(cfg.secrets_env, 0o600)
        except OSError:
            pass

    # 3. Write config.yaml if missing
    if not cfg.config_yaml.exists():
        yaml_content = yaml.safe_dump(cfg.to_dict(), sort_keys=False)
        atomic_write_text(cfg.config_yaml, yaml_content)

    # 4. Initialize registry/projects.yaml if missing
    if not cfg.projects_yaml.exists():
        atomic_write_text(cfg.projects_yaml, DEFAULT_PROJECTS_REGISTRY_TEMPLATE)

    # 5. Initialize state file if missing
    if not cfg.state_file.exists():
        initial_state = {
            "version": SCHEMA_VERSION,
            "objects": {},
        }
        atomic_write_text(
            cfg.state_file,
            json.dumps(initial_state, indent=2, sort_keys=True) + "\n",
        )

    # 6. Initialize canonical continuity rule if missing (preserve existing user edits)
    if not continuity_rule_path.exists():
        atomic_write_text(continuity_rule_path, DEFAULT_CONTINUITY_RULE_TEMPLATE)

    # 7. Initialize canonical continuity skill if missing (preserve existing user edits)
    if not continuity_skill_md.exists():
        atomic_write_text(continuity_skill_md, DEFAULT_CONTINUITY_SKILL_TEMPLATE)
