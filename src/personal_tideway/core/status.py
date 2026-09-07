"""Workspace status reporting: pending changes, conflicts, deletions, and portable MCP parity."""

import json
from pathlib import Path
from typing import Any
import yaml

from personal_tideway.adapters.agy import AgyAdapter
from personal_tideway.adapters.codex import CodexAdapter
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import CLIENT_AGY, CLIENT_CODEX
from personal_tideway.core.conflicts import load_all_conflicts
from personal_tideway.core.mcp import load_all_mcp_servers
from personal_tideway.core.memory import list_memories
from personal_tideway.core.rules import get_composed_rules_for_client
from personal_tideway.core.skills import find_skill, list_all_skills
from personal_tideway.core.sync import load_state, normalize_mcp_dict
from personal_tideway.exceptions import ValidationError
from personal_tideway.models import MCPServer
from personal_tideway.utils import calculate_hash


def get_workspace_status(cfg: PersonalTidewayConfig) -> dict[str, Any]:
    """Collect complete, scriptable workspace status."""
    initialized = cfg.is_initialized()
    if not initialized:
        return {
            "initialized": False,
            "home": str(cfg.home),
            "pending_changes": [],
            "conflicts": [],
            "deletions": [],
            "invalid_objects": [],
            "portable_mcp_parity": {
                "status": "uninitialized",
                "servers": [],
            },
            "skills": [],
            "memory_count": 0,
        }

    conflicts = load_all_conflicts(cfg.conflicts_dir)
    conflict_list = [c.to_dict() for c in sorted(conflicts.values(), key=lambda x: x.object_id)]

    state = load_state(cfg.state_file)
    objects_state: dict[str, Any] = state.get("objects", {})

    codex_adapter = CodexAdapter(cfg.codex_config, cfg.codex_rules, cfg.codex_skills, cfg.backups_dir)
    agy_adapter = AgyAdapter(cfg.agy_config, cfg.agy_rules, cfg.agy_skills, cfg.backups_dir)

    codex_mcp = codex_adapter.read_mcp_servers()
    agy_mcp = agy_adapter.read_mcp_servers()

    pending_changes: list[dict[str, Any]] = []
    deletions: list[dict[str, Any]] = []
    invalid_objects: list[dict[str, Any]] = []

    # 1. MCP Servers inspection
    ptw_servers: dict[str, MCPServer] = {}
    if cfg.mcp_dir.is_dir():
        for f in sorted(cfg.mcp_dir.glob("*.yaml")):
            try:
                with open(f, "r", encoding="utf-8") as yf:
                    data = yaml.safe_load(yf)
                if not isinstance(data, dict):
                    invalid_objects.append({"object_id": f"mcp:{f.stem}", "error": "YAML is not a dict"})
                    continue
                if "name" not in data:
                    data["name"] = f.stem
                srv = MCPServer.from_dict(data)
                ptw_servers[srv.name] = srv
            except Exception as e:
                invalid_objects.append({"object_id": f"mcp:{f.stem}", "error": str(e)})

    # Detect deleted servers and skills
    for obj_id, b_data in objects_state.items():
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
    if not portable_servers:
        parity_status = "empty"
    elif all(p["in_sync"] for p in portable_servers):
        parity_status = "in_sync"
    else:
        parity_status = "drift_detected"

    return {
        "initialized": True,
        "home": str(cfg.home),
        "pending_changes": pending_changes,
        "conflicts": conflict_list,
        "deletions": deletions,
        "invalid_objects": invalid_objects,
        "portable_mcp_parity": {
            "status": parity_status,
            "servers": portable_servers,
        },
        "skills_count": len(skills),
        "memory_count": len(list_memories(cfg)),
    }


def format_status_text(status: dict[str, Any]) -> str:
    """Format status dictionary as a clean, human-readable terminal report."""
    if not status.get("initialized"):
        return f"Personal Tideway workspace is NOT initialized at {status.get('home')}.\nRun 'ptw init' to create workspace."

    lines = [
        "=== Personal Tideway Status ===",
        f"Personal Tideway Home: {status['home']}",
        f"Memory Entries: {status['memory_count']}",
        f"Managed Skills: {status['skills_count']}",
        "",
    ]

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

    return "\n".join(lines)
