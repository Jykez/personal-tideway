"""Workspace status reporting: pending changes, conflicts, deletions, and portable MCP parity."""

import json
import os
from typing import Any

import yaml

from personal_tideway.adapters.agy import AgyAdapter
from personal_tideway.adapters.codex import CodexAdapter
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import CLIENT_AGY, CLIENT_CODEX
from personal_tideway.core.assurance import evaluate_assurance
from personal_tideway.core.conflicts import load_all_conflicts
from personal_tideway.core.discovery import (
    discover_agy,
    discover_codex,
    format_client_agy_text,
    format_client_codex_text,
)
from personal_tideway.core.hooks import get_agy_hook_status, get_codex_hook_status
from personal_tideway.core.memory import list_memories
from personal_tideway.core.rules import get_composed_rules_for_client
from personal_tideway.core.skills import find_skill, list_all_skills
from personal_tideway.core.sync import load_state, normalize_mcp_dict
from personal_tideway.exceptions import ValidationError
from personal_tideway.models import MCPServer, SkillInfo
from personal_tideway.utils import calculate_hash, hash_dir


def evaluate_projection_parity(
    cfg: PersonalTidewayConfig,
    ptw_servers: dict[str, MCPServer] | None = None,
    codex_mcp: dict[str, dict[str, Any]] | None = None,
    agy_mcp: dict[str, dict[str, Any]] | None = None,
    codex_mcp_invalid: bool = False,
    agy_mcp_invalid: bool = False,
    mcp_canonical_invalid: bool = False,
) -> dict[str, Any]:
    """Evaluate projection parity across MCP, rules, and skills for Codex and agy.

    Read-only evaluator:
    - Never mutates files, state, backups, or permissions.
    - Never executes host commands or probes.
    - Completely redacts secrets and raw configuration dumps.
    - Compares only managed rules blocks; handles malformed markers and conflicts.
    - Validates skills against shared/client scopes, link integrity, and scope leaks.
    - Preserves independent unrelated client configurations.
    """
    if not cfg.is_initialized():
        uninit_client = {
            "status": "uninitialized",
            "mcp": {"status": "uninitialized", "servers": []},
            "rules": {"status": "uninitialized"},
            "skills": {"status": "uninitialized", "skills": []},
        }
        return {
            "status": "uninitialized",
            "remediation": "Run 'ptw init' to initialize workspace.",
            "clients": {
                CLIENT_CODEX: dict(uninit_client),
                CLIENT_AGY: dict(uninit_client),
            },
            CLIENT_CODEX: dict(uninit_client),
            CLIENT_AGY: dict(uninit_client),
        }

    conflicts = load_all_conflicts(cfg.conflicts_dir)
    state = load_state(cfg.state_file)
    objects_state: dict[str, Any] = state.get("objects", {})

    codex_adapter = CodexAdapter(cfg.codex_config, cfg.codex_rules, cfg.codex_skills, cfg.backups_dir)
    agy_adapter = AgyAdapter(cfg.agy_config, cfg.agy_rules, cfg.agy_skills, cfg.backups_dir)

    # 1. Canonical MCP Servers
    if ptw_servers is None:
        ptw_servers = {}
        if cfg.mcp_dir.is_dir():
            for f in sorted(cfg.mcp_dir.glob("*.yaml")):
                try:
                    with open(f, "r", encoding="utf-8") as yf:
                        data = yaml.safe_load(yf)
                    if not isinstance(data, dict):
                        mcp_canonical_invalid = True
                        continue
                    if "name" not in data:
                        data["name"] = f.stem
                    srv = MCPServer.from_dict(data)
                    srv.validate()
                    ptw_servers[srv.name] = srv
                except (yaml.YAMLError, ValidationError, OSError):
                    mcp_canonical_invalid = True

    clients_result: dict[str, dict[str, Any]] = {}

    for client in (CLIENT_CODEX, CLIENT_AGY):
        adapter = codex_adapter if client == CLIENT_CODEX else agy_adapter
        client_is_invalid = codex_mcp_invalid if client == CLIENT_CODEX else agy_mcp_invalid
        client_servers = codex_mcp if client == CLIENT_CODEX else agy_mcp

        # --- MCP Evaluation ---
        if client_servers is None and not client_is_invalid:
            try:
                client_servers = adapter.read_mcp_servers()
            except (ValidationError, OSError):
                client_is_invalid = True
                client_servers = {}

        if mcp_canonical_invalid or client_is_invalid:
            mcp_status = "invalid"
            mcp_servers_details: list[dict[str, Any]] = []
        else:
            targeted = [srv for srv in ptw_servers.values() if client in srv.targets]
            targeted.sort(key=lambda s: s.name)
            if not targeted:
                mcp_status = "empty"
                mcp_servers_details = []
            else:
                mcp_servers_details = []
                has_mcp_conflict = False
                has_mcp_drift = False

                for srv in targeted:
                    obj_id = f"mcp:{srv.name}"
                    is_conflicted = any(
                        c.object_id == obj_id and c.client == client
                        for c in conflicts.values()
                    )
                    if is_conflicted:
                        srv_status = "conflict"
                        has_mcp_conflict = True
                    else:
                        expected_norm = normalize_mcp_dict(srv.get_effective_config(client))
                        actual_raw = client_servers.get(srv.name) if client_servers else None
                        if actual_raw is None:
                            srv_status = "drift_detected"
                            has_mcp_drift = True
                        else:
                            actual_norm = normalize_mcp_dict(actual_raw)
                            if actual_norm == expected_norm:
                                srv_status = "in_sync"
                            else:
                                srv_status = "drift_detected"
                                has_mcp_drift = True

                    # Secret redaction: only server name and status, never raw config/env
                    mcp_servers_details.append({
                        "name": srv.name,
                        "status": srv_status,
                        "in_sync": srv_status == "in_sync",
                    })

                if has_mcp_conflict:
                    mcp_status = "conflict"
                elif has_mcp_drift:
                    mcp_status = "drift_detected"
                else:
                    mcp_status = "in_sync"

        # --- Rules Evaluation ---
        rules_invalid = False
        managed = None
        try:
            _prefix, managed, _suffix = adapter.read_rules_block()
        except (ValidationError, OSError):
            rules_invalid = True

        if rules_invalid:
            rules_status = "invalid"
        else:
            obj_id = f"rules:{client}"
            is_rules_conflicted = any(
                c.object_id == obj_id and c.client == client
                for c in conflicts.values()
            )
            if is_rules_conflicted:
                rules_status = "conflict"
            else:
                composed = get_composed_rules_for_client(cfg, client).strip()
                client_managed = (managed or "").strip() if managed is not None else None

                if not composed and client_managed is None:
                    rules_status = "empty"
                elif bool(composed) != bool(client_managed):
                    rules_status = "drift_detected"
                elif composed == client_managed:
                    rules_status = "in_sync"
                else:
                    rules_status = "drift_detected"

        # --- Skills Evaluation ---
        canonical_skills: dict[str, SkillInfo] = {}
        skills_invalid = False

        if cfg.skills_shared.is_dir():
            for entry in sorted(cfg.skills_shared.iterdir()):
                if entry.is_dir():
                    sk = SkillInfo(name=entry.name, path=entry, scope="shared")
                    if not sk.is_valid()[0]:
                        skills_invalid = True
                    canonical_skills[entry.name] = sk

        client_canon_dir = cfg.skills_codex if client == CLIENT_CODEX else cfg.skills_agy
        if client_canon_dir.is_dir():
            for entry in sorted(client_canon_dir.iterdir()):
                if entry.is_dir():
                    sk = SkillInfo(name=entry.name, path=entry, scope=client)
                    if not sk.is_valid()[0]:
                        skills_invalid = True
                    canonical_skills[entry.name] = sk

        foreign_dir = cfg.skills_agy if client == CLIENT_CODEX else cfg.skills_codex
        foreign_skill_names: set[str] = set()
        if foreign_dir.is_dir():
            for entry in sorted(foreign_dir.iterdir()):
                if entry.is_dir() and entry.name not in canonical_skills:
                    foreign_skill_names.add(entry.name)

        # Collect state-managed skills targeted to this client
        state_managed_skills: dict[str, dict[str, Any]] = {}
        for obj_id, b_data in sorted(objects_state.items()):
            if obj_id.startswith("skill:") and isinstance(b_data, dict):
                sk_name = obj_id[6:]
                sk_scope = b_data.get("scope", "shared")
                if sk_scope in ("shared", client):
                    state_managed_skills[sk_name] = b_data

        if skills_invalid:
            skills_status = "invalid"
            skills_items: list[dict[str, Any]] = []
        else:
            client_installed_dir = cfg.codex_skills if client == CLIENT_CODEX else cfg.agy_skills
            skills_items = []
            has_skills_conflict = False
            has_skills_drift = False

            # Check scope leakage (skills from foreign client scope found in this client's skills dir)
            if client_installed_dir.is_dir():
                for entry in sorted(client_installed_dir.iterdir()):
                    if entry.name in foreign_skill_names:
                        has_skills_drift = True
                        skills_items.append({
                            "name": entry.name,
                            "status": "drift_detected",
                            "in_sync": False,
                            "hazard": "scope_leakage",
                        })

            # Check state-managed skills deleted in PTW (leaving orphaned client links or missing canonical definitions)
            for sk_name in sorted(state_managed_skills.keys()):
                if sk_name not in canonical_skills:
                    has_skills_drift = True
                    skills_items.append({
                        "name": sk_name,
                        "status": "drift_detected",
                        "in_sync": False,
                        "hazard": "deleted_in_ptw",
                    })

            # Check canonical skills targeted for this client
            for name, sk in sorted(canonical_skills.items()):
                obj_id = f"skill:{name}"
                is_conflicted = any(
                    c.object_id == obj_id and c.client == client
                    for c in conflicts.values()
                )
                if is_conflicted:
                    sk_status = "conflict"
                    has_skills_conflict = True
                else:
                    dest = client_installed_dir / name
                    if not dest.exists():
                        sk_status = "drift_detected"
                        has_skills_drift = True
                    elif dest.is_symlink():
                        try:
                            canonical_resolved = sk.path.resolve()
                            raw_target = os.readlink(dest)
                            resolved_dest = (dest.parent / raw_target).resolve()
                            if resolved_dest != canonical_resolved or cfg.skill_link_mode != "symlink":
                                sk_status = "drift_detected"
                                has_skills_drift = True
                            else:
                                sk_status = "in_sync"
                        except OSError:
                            sk_status = "drift_detected"
                            has_skills_drift = True
                    elif dest.is_dir():
                        if cfg.skill_link_mode == "symlink":
                            sk_status = "drift_detected"
                            has_skills_drift = True
                        else:
                            h_canon = hash_dir(sk.path)
                            h_dest = hash_dir(dest)
                            if h_canon == h_dest:
                                sk_status = "in_sync"
                            else:
                                sk_status = "drift_detected"
                                has_skills_drift = True
                    else:
                        sk_status = "drift_detected"
                        has_skills_drift = True

                skills_items.append({
                    "name": name,
                    "status": sk_status,
                    "in_sync": sk_status == "in_sync",
                })

            skills_items.sort(key=lambda s: s["name"])

            if not canonical_skills and not skills_items:
                skills_status = "empty"
            elif has_skills_conflict:
                skills_status = "conflict"
            elif has_skills_drift:
                skills_status = "drift_detected"
            else:
                skills_status = "in_sync"

        # Client-level status
        comp_statuses = [mcp_status, rules_status, skills_status]
        if "invalid" in comp_statuses:
            client_status = "invalid"
        elif "conflict" in comp_statuses:
            client_status = "conflict"
        elif "drift_detected" in comp_statuses:
            client_status = "drift_detected"
        elif all(s == "empty" for s in comp_statuses):
            client_status = "empty"
        else:
            client_status = "in_sync"

        clients_result[client] = {
            "status": client_status,
            "mcp": {
                "status": mcp_status,
                "servers": mcp_servers_details,
            },
            "rules": {
                "status": rules_status,
            },
            "skills": {
                "status": skills_status,
                "skills": skills_items,
            },
        }

    all_client_statuses = [clients_result[CLIENT_CODEX]["status"], clients_result[CLIENT_AGY]["status"]]
    if "invalid" in all_client_statuses:
        overall_status = "invalid"
        remediation = "Fix syntax errors or malformed markers in configuration files."
    elif "conflict" in all_client_statuses:
        overall_status = "conflict"
        remediation = "Resolve active conflicts with 'ptw resolve <object> --take ptw|codex|agy' before sync."
    elif "drift_detected" in all_client_statuses:
        overall_status = "drift_detected"
        remediation = "Run 'ptw sync' to safely apply projections."
    elif all(s == "empty" for s in all_client_statuses):
        overall_status = "empty"
        remediation = "No projections configured."
    else:
        overall_status = "in_sync"
        remediation = "All projections are in sync."

    return {
        "status": overall_status,
        "remediation": remediation,
        "clients": clients_result,
        CLIENT_CODEX: clients_result[CLIENT_CODEX],
        CLIENT_AGY: clients_result[CLIENT_AGY],
    }


def get_workspace_status(cfg: PersonalTidewayConfig) -> dict[str, Any]:
    """Collect complete, scriptable workspace status."""
    codex_diag = discover_codex(
        codex_home=cfg.codex_home,
        canonical_user_skills=cfg.canonical_user_skills,
    )
    agy_diag = discover_agy(
        gemini_home=cfg.gemini_home,
        customization_root=cfg.agy_customization_root,
        portable_skills_alias=cfg.canonical_user_skills,
    )
    initialized = cfg.is_initialized()
    assurance_report = evaluate_assurance(cfg)
    agy_hook_evidence = get_agy_hook_status(cfg)
    codex_hook_evidence = get_codex_hook_status(cfg)

    if not initialized:
        proj_parity = evaluate_projection_parity(cfg)
        return {
            "initialized": False,
            "home": str(cfg.home),
            "clients": {
                "codex": codex_diag.to_dict(),
                "agy": agy_diag.to_dict(),
            },
            "continuity_assurance": assurance_report.to_dict(),
            "agy_hook": agy_hook_evidence.to_dict(),
            "codex_hook": codex_hook_evidence.to_dict(),
            "pending_changes": [],
            "conflicts": [],
            "deletions": [],
            "invalid_objects": [],
            "portable_mcp_parity": {
                "status": "uninitialized",
                "servers": [],
            },
            "projection_parity": proj_parity,
            "skills": [],
            "memory_count": 0,
        }

    conflicts = load_all_conflicts(cfg.conflicts_dir)
    conflict_list = [c.to_dict() for c in sorted(conflicts.values(), key=lambda x: x.object_id)]

    state = load_state(cfg.state_file)
    objects_state: dict[str, Any] = state.get("objects", {})

    codex_adapter = CodexAdapter(cfg.codex_config, cfg.codex_rules, cfg.codex_skills, cfg.backups_dir)
    agy_adapter = AgyAdapter(cfg.agy_config, cfg.agy_rules, cfg.agy_skills, cfg.backups_dir)

    invalid_objects: list[dict[str, Any]] = []

    codex_mcp: dict[str, dict[str, Any]] = {}
    codex_mcp_invalid = False
    try:
        codex_mcp = codex_adapter.read_mcp_servers()
    except ValidationError:
        codex_mcp_invalid = True
        invalid_objects.append({"object_id": "config:codex", "error": "Malformed Codex configuration file"})

    agy_mcp: dict[str, dict[str, Any]] = {}
    agy_mcp_invalid = False
    try:
        agy_mcp = agy_adapter.read_mcp_servers()
    except ValidationError:
        agy_mcp_invalid = True
        invalid_objects.append({"object_id": "config:agy", "error": "Malformed agy configuration file"})

    pending_changes: list[dict[str, Any]] = []
    deletions: list[dict[str, Any]] = []

    # 1. MCP Servers inspection
    ptw_servers: dict[str, MCPServer] = {}
    mcp_canonical_invalid = False
    if cfg.mcp_dir.is_dir():
        for f in sorted(cfg.mcp_dir.glob("*.yaml")):
            try:
                with open(f, "r", encoding="utf-8") as yf:
                    data = yaml.safe_load(yf)
                if not isinstance(data, dict):
                    invalid_objects.append({"object_id": f"mcp:{f.stem}", "error": "YAML is not a dict"})
                    mcp_canonical_invalid = True
                    continue
                if "name" not in data:
                    data["name"] = f.stem
                srv = MCPServer.from_dict(data)
                srv.validate()
                ptw_servers[srv.name] = srv
            except (yaml.YAMLError, ValidationError, OSError):
                invalid_objects.append({"object_id": f"mcp:{f.stem}", "error": "Invalid canonical MCP configuration"})
                mcp_canonical_invalid = True

    proj_parity = evaluate_projection_parity(
        cfg,
        ptw_servers=ptw_servers,
        codex_mcp=codex_mcp,
        agy_mcp=agy_mcp,
        codex_mcp_invalid=codex_mcp_invalid,
        agy_mcp_invalid=agy_mcp_invalid,
        mcp_canonical_invalid=mcp_canonical_invalid,
    )

    # Detect deleted servers and skills
    for obj_id in objects_state:
        if obj_id.startswith("mcp:"):
            s_name = obj_id[4:]
            if s_name not in ptw_servers:
                deletions.append({
                    "object_id": obj_id,
                    "type": "mcp",
                    "status": "deleted_in_ptw",
                    "name": s_name,
                })
        elif obj_id.startswith("skill:"):
            s_name = obj_id[6:]
            if not find_skill(cfg, s_name):
                deletions.append({
                    "object_id": obj_id,
                    "type": "skill",
                    "status": "deleted_in_ptw",
                    "name": s_name,
                })

    portable_servers: list[dict[str, Any]] = []

    for name, srv in sorted(ptw_servers.items()):
        obj_id = f"mcp:{name}"
        base_obj = objects_state.get(obj_id)
        norm_ptw = normalize_mcp_dict(srv.to_dict())
        h_ptw = calculate_hash(json.dumps(norm_ptw, sort_keys=True))

        for target in srv.targets:
            c_servers = codex_mcp if target == CLIENT_CODEX else agy_mcp
            client_srv = c_servers.get(name)
            norm_client = normalize_mcp_dict(client_srv) if client_srv else None
            h_client = calculate_hash(json.dumps(norm_client, sort_keys=True)) if norm_client else None

            base_h = base_obj.get("client_hashes", {}).get(target) if base_obj else None
            base_p_h = base_obj.get("ptw_hash") if base_obj else None

            if base_obj is None:
                pending_changes.append({
                    "object_id": obj_id,
                    "target": target,
                    "change": "new_object",
                })
            else:
                if h_ptw != base_p_h:
                    pending_changes.append({
                        "object_id": obj_id,
                        "target": target,
                        "change": "ptw_modified",
                    })
                if h_client != base_h:
                    pending_changes.append({
                        "object_id": obj_id,
                        "target": target,
                        "change": f"{target}_modified",
                    })

        # Check portable MCP parity (subset targeted to both codex and agy)
        if srv.is_portable:
            c_entry = codex_mcp.get(name)
            a_entry = agy_mcp.get(name)
            c_norm = normalize_mcp_dict(c_entry) if c_entry else None
            a_norm = normalize_mcp_dict(a_entry) if a_entry else None
            expected_codex = normalize_mcp_dict(srv.get_effective_config(CLIENT_CODEX))
            expected_agy = normalize_mcp_dict(srv.get_effective_config(CLIENT_AGY))
            in_sync = (
                c_norm is not None
                and a_norm is not None
                and c_norm == expected_codex
                and a_norm == expected_agy
            )

            portable_servers.append({
                "name": name,
                "in_codex": c_entry is not None,
                "in_agy": a_entry is not None,
                "in_sync": in_sync,
                "status": "in_sync" if in_sync else "drift_detected",
            })

    # 2. Rules inspection
    for client_name in (CLIENT_CODEX, CLIENT_AGY):
        obj_id = f"rules:{client_name}"
        adapter = codex_adapter if client_name == CLIENT_CODEX else agy_adapter
        base_obj = objects_state.get(obj_id)
        base_h = base_obj.get("hash") if base_obj else None

        composed = get_composed_rules_for_client(cfg, client_name).strip()
        h_ptw = calculate_hash(composed)

        try:
            _, client_managed, _ = adapter.read_rules_block()
            h_client = calculate_hash((client_managed or "").strip()) if client_managed is not None else None
        except ValidationError as e:
            invalid_objects.append({"object_id": obj_id, "error": str(e)})
            continue

        if base_h is None:
            pending_changes.append({"object_id": obj_id, "change": "rules_uninitialized"})
        else:
            if h_ptw != base_h:
                pending_changes.append({"object_id": obj_id, "change": "ptw_rules_modified"})
            if h_client != base_h:
                pending_changes.append({"object_id": obj_id, "change": f"{client_name}_rules_modified"})

    # 3. Skills inspection
    skills = list_all_skills(cfg)
    for s in skills:
        if not s["valid"]:
            invalid_objects.append({"object_id": f"skill:{s['name']}", "error": s["error"]})

    # Parity status
    if codex_mcp_invalid or agy_mcp_invalid or mcp_canonical_invalid:
        parity_status = "invalid"
    elif not portable_servers:
        parity_status = "empty"
    elif all(p["in_sync"] for p in portable_servers):
        parity_status = "in_sync"
    else:
        parity_status = "drift_detected"

    return {
        "initialized": True,
        "home": str(cfg.home),
        "clients": {
            "codex": codex_diag.to_dict(),
            "agy": agy_diag.to_dict(),
        },
        "continuity_assurance": assurance_report.to_dict(),
        "agy_hook": agy_hook_evidence.to_dict(),
        "codex_hook": codex_hook_evidence.to_dict(),
        "pending_changes": pending_changes,
        "conflicts": conflict_list,
        "deletions": deletions,
        "invalid_objects": invalid_objects,
        "portable_mcp_parity": {
            "status": parity_status,
            "servers": portable_servers,
        },
        "projection_parity": proj_parity,
        "skills_count": len(skills),
        "memory_count": len(list_memories(cfg)),
    }


def format_status_text(status: dict[str, Any]) -> str:
    """Format status dictionary as a clean, human-readable terminal report."""
    if not status.get("initialized"):
        lines = [
            f"Personal Tideway workspace is NOT initialized at {status.get('home')}.",
            "Run 'ptw init' to create workspace.",
        ]
        assurance = status.get("continuity_assurance", {})
        if assurance and "clients" in assurance:
            lines.append("")
            lines.append("Continuity Assurance:")
            for c_name, c_data in sorted(assurance["clients"].items()):
                lines.append(f"  - {c_name}: {c_data.get('level', 'unavailable')} ({c_data.get('details', '')})")
        codex_hook = status.get("codex_hook")
        if codex_hook:
            lines.append(f"  - codex hook: {codex_hook.get('status', 'unknown')}")
        agy_hook = status.get("agy_hook")
        if agy_hook:
            lines.append(f"  - agy hook: {agy_hook.get('status', 'unknown')}")
        proj_parity = status.get("projection_parity")
        if proj_parity:
            lines.append(f"  - projection parity: {proj_parity.get('status', 'uninitialized')}")
            remediation = proj_parity.get("remediation")
            if remediation:
                lines.append(f"  Remediation: {remediation}")
        if "clients" in status:
            if "codex" in status["clients"]:
                lines.append("")
                lines.append(format_client_codex_text(status["clients"]["codex"]))
            if "agy" in status["clients"]:
                lines.append("")
                lines.append(format_client_agy_text(status["clients"]["agy"]))
        return "\n".join(lines)

    lines = [
        "=== Personal Tideway Status ===",
        f"Personal Tideway Home: {status['home']}",
        f"Memory Entries: {status['memory_count']}",
        f"Managed Skills: {status['skills_count']}",
    ]

    codex_hook = status.get("codex_hook")
    if codex_hook:
        lines.append(f"Codex Lifecycle Hook: {codex_hook.get('status', 'unknown')}")
    agy_hook = status.get("agy_hook")
    if agy_hook:
        lines.append(f"AGY Lifecycle Hook: {agy_hook.get('status', 'unknown')}")

    assurance = status.get("continuity_assurance", {})
    if assurance and "clients" in assurance:
        lines.append("")
        lines.append("Continuity Assurance:")
        for c_name, c_data in sorted(assurance["clients"].items()):
            lines.append(f"  - {c_name}: {c_data.get('level', 'unknown')} ({c_data.get('details', '')})")

    lines.append("")

    # Conflicts
    conflicts = status.get("conflicts", [])
    if conflicts:
        lines.append(f"Conflicts ({len(conflicts)} ACTIVE):")
        for c in conflicts:
            lines.append(f"  - [CONFLICT] {c['object_id']} ({c['client']}): {c['message']}")
        lines.append("  Use 'ptw resolve OBJECT --take ptw|codex|agy' to resolve.")
    else:
        lines.append("Conflicts: None")

    # Deletions
    deletions = status.get("deletions", [])
    if deletions:
        lines.append(f"\nUnresolved Deletions ({len(deletions)}):")
        for d in deletions:
            lines.append(f"  - [DELETED] {d['object_id']} ({d['status']})")
    else:
        lines.append("Deletions: None")

    # Invalid objects
    invalid = status.get("invalid_objects", [])
    if invalid:
        lines.append(f"\nInvalid Objects ({len(invalid)}):")
        for inv in invalid:
            lines.append(f"  - [INVALID] {inv['object_id']}: {inv['error']}")

    # Pending changes
    pending = status.get("pending_changes", [])
    if pending:
        lines.append(f"\nPending Changes ({len(pending)}):")
        for p in pending:
            lines.append(f"  - {p['object_id']}: {p['change']}")
        lines.append("  Run 'ptw sync' to synchronize.")
    else:
        lines.append("\nPending Changes: None (in sync)")

    # Portable MCP parity
    parity = status.get("portable_mcp_parity", {})
    lines.append(f"\nPortable MCP Parity: {parity.get('status', 'unknown')}")
    servers = parity.get("servers", [])
    if servers:
        for s in servers:
            status_str = "IN_SYNC" if s["in_sync"] else "DRIFT"
            lines.append(f"  - {s['name']}: {status_str} (Codex: {s['in_codex']}, agy: {s['in_agy']})")
    else:
        lines.append("  No shared portable MCP servers defined.")

    # Projection parity
    proj_parity = status.get("projection_parity", {})
    if proj_parity:
        lines.append(f"\nProjection Parity: {proj_parity.get('status', 'unknown')}")
        clients_data = proj_parity.get("clients", {})
        for c_name in (CLIENT_CODEX, CLIENT_AGY):
            c_data = clients_data.get(c_name)
            if not c_data:
                continue
            display_name = "Codex" if c_name == CLIENT_CODEX else "AGY"
            mcp_st = c_data.get("mcp", {}).get("status", "unknown")
            rules_st = c_data.get("rules", {}).get("status", "unknown")
            skills_st = c_data.get("skills", {}).get("status", "unknown")
            lines.append(f"  {display_name}:")
            lines.append(f"    - MCP:    {mcp_st}")
            lines.append(f"    - Rules:  {rules_st}")
            lines.append(f"    - Skills: {skills_st}")
        remediation = proj_parity.get("remediation")
        if remediation:
            lines.append(f"  Remediation: {remediation}")

    if "clients" in status:
        if "codex" in status["clients"]:
            lines.append("")
            lines.append(format_client_codex_text(status["clients"]["codex"]))
        if "agy" in status["clients"]:
            lines.append("")
            lines.append(format_client_agy_text(status["clients"]["agy"]))

    return "\n".join(lines)
