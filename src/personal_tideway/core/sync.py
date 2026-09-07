"""Three-way synchronization engine and conflict resolution."""

import json
import os
from pathlib import Path
import shutil
from typing import Any, Literal

from personal_tideway.adapters.agy import AgyAdapter
from personal_tideway.adapters.codex import CodexAdapter
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import (
    CLIENT_AGY,
    CLIENT_CODEX,
    ExitCode,
    SCHEMA_VERSION,
    SUPPORTED_CLIENTS,
    TRANSPORT_HTTP,
    TRANSPORT_STDIO,
)
from personal_tideway.core.conflicts import (
    load_all_conflicts,
    load_conflict,
    remove_conflict_artifacts,
    save_conflict_artifacts,
)
from personal_tideway.core.mcp import load_all_mcp_servers, load_mcp_server, save_mcp_server
from personal_tideway.core.rules import compose_rules_content, get_composed_rules_for_client
from personal_tideway.core.skills import find_skill, get_skill_scopes, list_all_skills
from personal_tideway.exceptions import ConflictError, ValidationError
from personal_tideway.models import ConflictRecord, MCPServer, SkillInfo
from personal_tideway.utils import atomic_write_text, calculate_hash, hash_dir, hash_file


def load_state(state_file: Path) -> dict[str, Any]:
    """Load base synchronization snapshots from state.json."""
    if not state_file.is_file():
        return {"version": SCHEMA_VERSION, "objects": {}}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "objects" in data:
            return data
    except Exception:
        pass
    return {"version": SCHEMA_VERSION, "objects": {}}


def save_state(state_file: Path, state: dict[str, Any], dry_run: bool = False) -> None:
    """Save base synchronization snapshots atomically."""
    if not dry_run:
        content = json.dumps(state, indent=2, sort_keys=True) + "\n"
        if state_file.is_file() and state_file.read_text(encoding="utf-8") == content:
            return
        atomic_write_text(state_file, content)


def normalize_mcp_dict(cfg: dict[str, Any]) -> dict[str, Any]:
    """Normalize MCP server dictionary for deterministic comparison."""
    transport = str(cfg.get("transport", TRANSPORT_STDIO))
    cmd = cfg.get("command")
    url = cfg.get("url") or cfg.get("serverUrl")
    args = cfg.get("args") or []
    env = cfg.get("env") or {}
    norm: dict[str, Any] = {
        "transport": transport,
        "command": str(cmd) if cmd is not None else None,
        "args": [str(x) for x in args],
        "url": str(url) if url is not None else None,
        "enabled": bool(cfg.get("enabled", not cfg.get("disabled", False))),
        "env": {str(k): str(v) for k, v in env.items()},
    }
    return norm


def sync_workspace(cfg: PersonalTidewayConfig, dry_run: bool = False) -> tuple[int, list[str]]:
    """Execute 3-way synchronization across MCP definitions, rules, and skills.

    Returns (exit_code, summary_messages).
    Exit code is ExitCode.SUCCESS (0) or ExitCode.CONFLICT (3) if divergences occur.
    """
    messages: list[str] = []
    has_conflicts = False

    codex_adapter = CodexAdapter(cfg.codex_config, cfg.codex_rules, cfg.codex_skills, cfg.backups_dir)
    agy_adapter = AgyAdapter(cfg.agy_config, cfg.agy_rules, cfg.agy_skills, cfg.backups_dir)

    state = load_state(cfg.state_file)
    objects_state: dict[str, Any] = state.setdefault("objects", {})

    # -------------------------------------------------------------
    # 1. MCP Synchronisation
    # -------------------------------------------------------------
    ptw_servers = load_all_mcp_servers(cfg.mcp_dir)
    codex_servers = codex_adapter.read_mcp_servers()
    agy_servers = agy_adapter.read_mcp_servers()

    # Track managed servers to write
    codex_to_write: dict[str, dict[str, Any]] = {}
    agy_to_write: dict[str, dict[str, Any]] = {}
    managed_codex_names: set[str] = set()
    managed_agy_names: set[str] = set()
    conflicted_client_servers: dict[str, set[str]] = {CLIENT_CODEX: set(), CLIENT_AGY: set()}

    # Union of all server names across Personal Tideway, state, and clients
    all_server_names = set(ptw_servers.keys())
    for obj_id in objects_state.keys():
        if obj_id.startswith("mcp:"):
            all_server_names.add(obj_id[4:])

    for name in sorted(all_server_names):
        obj_id = f"mcp:{name}"
        base_obj = objects_state.get(obj_id)
        server = ptw_servers.get(name)

        # Check for deletion
        if server is None:
            # Server was deleted in Personal Tideway (requires explicit resolution, do NOT delete automatically)
            messages.append(f"Notice: MCP server '{name}' was deleted in Personal Tideway (requires explicit resolution).")
            continue

        # Target clients
        if CLIENT_CODEX in server.targets:
            managed_codex_names.add(name)
        if CLIENT_AGY in server.targets:
            managed_agy_names.add(name)

        ptw_dict = server.to_dict()
        ptw_norm = normalize_mcp_dict(ptw_dict)
        ptw_hash = calculate_hash(json.dumps(ptw_norm, sort_keys=True))

        server_conflicted = False

        # Sync with each targeted client
        for client_name in server.targets:
            adapter = codex_adapter if client_name == CLIENT_CODEX else agy_adapter
            client_servers = codex_servers if client_name == CLIENT_CODEX else agy_servers

            client_raw = client_servers.get(name)
            client_norm = normalize_mcp_dict(client_raw) if client_raw else None
            client_hash = calculate_hash(json.dumps(client_norm, sort_keys=True)) if client_norm else None

            base_client_hashes = base_obj.get("client_hashes", {}) if base_obj else {}
            base_ptw_hash = base_obj.get("ptw_hash") if base_obj else None
            b_client_hash = base_client_hashes.get(client_name)

            if base_obj is None:
                # First sync
                if client_norm is None:
                    # Missing client object -> propagate. An identical existing
                    # object is already correct and must not be reserialized.
                    if client_name == CLIENT_CODEX:
                        codex_to_write[name] = server.get_effective_config(CLIENT_CODEX)
                    else:
                        agy_to_write[name] = server.get_effective_config(CLIENT_AGY)
                elif client_hash == ptw_hash:
                    pass
                else:
                    # Initial divergence
                    server_conflicted = True
                    conflicted_client_servers[client_name].add(name)
                    has_conflicts = True
                    rec = ConflictRecord(
                        object_id=obj_id,
                        object_type="mcp",
                        client=client_name,
                        base_hash=None,
                        ptw_hash=ptw_hash,
                        client_hash=client_hash,
                        ptw_content=json.dumps(ptw_norm, indent=2),
                        client_content=json.dumps(client_norm, indent=2),
                        message=f"Initial divergence for MCP server '{name}' on client '{client_name}'.",
                    )
                    save_conflict_artifacts(cfg.conflicts_dir, rec, dry_run=dry_run)
                    messages.append(f"Conflict: MCP server '{name}' diverged on {client_name}")
            else:
                ptw_changed = (ptw_hash != base_ptw_hash)
                client_changed = (client_hash != b_client_hash)

                if not ptw_changed and not client_changed:
                    # Preserve an existing byte-identical client object. A
                    # missing target still needs to be materialized.
                    if client_norm is None:
                        if client_name == CLIENT_CODEX:
                            codex_to_write[name] = server.get_effective_config(CLIENT_CODEX)
                        else:
                            agy_to_write[name] = server.get_effective_config(CLIENT_AGY)
                elif ptw_changed and not client_changed:
                    # One-sided change: Personal Tideway changed -> propagate to client
                    if client_name == CLIENT_CODEX:
                        codex_to_write[name] = server.get_effective_config(CLIENT_CODEX)
                    else:
                        agy_to_write[name] = server.get_effective_config(CLIENT_AGY)
                    messages.append(f"Propagated MCP '{name}' from Personal Tideway to {client_name}")
                elif not ptw_changed and client_changed:
                    # One-sided change: Client changed -> propagate to Personal Tideway
                    if client_norm is not None:
                        # Update Personal Tideway server from client
                        server.command = client_norm.get("command")
                        server.args = list(client_norm.get("args", []))
                        server.url = client_norm.get("url")
                        server.transport = client_norm.get("transport", TRANSPORT_STDIO)
                        if client_norm.get("env"):
                            server.env.update(client_norm["env"])
                        save_mcp_server(cfg.mcp_dir, server, dry_run=dry_run)
                        # Re-calculate hash
                        ptw_norm = normalize_mcp_dict(server.to_dict())
                        ptw_hash = calculate_hash(json.dumps(ptw_norm, sort_keys=True))
                        messages.append(f"Propagated MCP '{name}' from {client_name} to Personal Tideway")
                        if client_name == CLIENT_CODEX:
                            codex_to_write[name] = server.get_effective_config(CLIENT_CODEX)
                        else:
                            agy_to_write[name] = server.get_effective_config(CLIENT_AGY)
                else:
                    # Both changed!
                    if ptw_hash == client_hash:
                        # Identical dual changes: accept and update base
                        messages.append(f"Accepted identical dual changes for MCP '{name}' on {client_name}")
                    else:
                        # Diverged! Do not overwrite either side
                        server_conflicted = True
                        conflicted_client_servers[client_name].add(name)
                        has_conflicts = True
                        rec = ConflictRecord(
                            object_id=obj_id,
                            object_type="mcp",
                            client=client_name,
                            base_hash=base_ptw_hash,
                            ptw_hash=ptw_hash,
                            client_hash=client_hash,
                            ptw_content=json.dumps(ptw_norm, indent=2),
                            client_content=json.dumps(client_norm, indent=2),
                            message=f"Divergent changes for MCP server '{name}' on client '{client_name}'.",
                        )
                        save_conflict_artifacts(cfg.conflicts_dir, rec, dry_run=dry_run)
                        messages.append(f"Conflict: MCP server '{name}' diverged on {client_name}")

        # Update base state if no conflicts
        if not server_conflicted:
            client_hashes: dict[str, str] = {}
            for c in server.targets:
                eff = normalize_mcp_dict(server.get_effective_config(c))
                client_hashes[c] = calculate_hash(json.dumps(eff, sort_keys=True))

            objects_state[obj_id] = {
                "ptw_hash": ptw_hash,
                "client_hashes": client_hashes,
                "normalized": ptw_norm,
            }

    # Deletions and retargeting require explicit resolution, so adapters only
    # receive the update set and never infer removals from its absence.
    codex_adapter.write_mcp_servers(codex_to_write, set(), dry_run=dry_run)
    agy_adapter.write_mcp_servers(agy_to_write, set(), dry_run=dry_run)

    # -------------------------------------------------------------
    # 2. Rules Synchronisation
    # -------------------------------------------------------------
    for client_name in (CLIENT_CODEX, CLIENT_AGY):
        obj_id = f"rules:{client_name}"
        adapter = codex_adapter if client_name == CLIENT_CODEX else agy_adapter
        composed_rules = get_composed_rules_for_client(cfg, client_name).strip()
        ptw_rules_hash = calculate_hash(composed_rules)

        try:
            prefix, client_rules, suffix = adapter.read_rules_block()
        except ValidationError as e:
            has_conflicts = True
            rec = ConflictRecord(
                object_id=obj_id,
                object_type="rules",
                client=client_name,
                base_hash=None,
                ptw_hash=ptw_rules_hash,
                client_hash=None,
                ptw_content=composed_rules,
                client_content=None,
                message=str(e),
            )
            save_conflict_artifacts(cfg.conflicts_dir, rec, dry_run=dry_run)
            messages.append(f"Conflict: Malformed rule markers on {client_name}: {e}")
            continue

        base_obj = objects_state.get(obj_id)
        base_hash = base_obj.get("hash") if base_obj else None
        client_rules_clean = (client_rules or "").strip()
        client_rules_hash = calculate_hash(client_rules_clean) if client_rules is not None else None

        if base_obj is None:
            # First sync
            if client_rules is None or client_rules_hash == ptw_rules_hash:
                adopt_existing = (
                    client_rules is None
                    and prefix is not None
                    and prefix.strip() == composed_rules
                    and bool(composed_rules)
                )
                adapter.write_rules_block(
                    composed_rules,
                    dry_run=dry_run,
                    adopt_existing=adopt_existing,
                )
                objects_state[obj_id] = {"hash": ptw_rules_hash}
                messages.append(f"Initialized rules block on {client_name}")
            else:
                has_conflicts = True
                rec = ConflictRecord(
                    object_id=obj_id,
                    object_type="rules",
                    client=client_name,
                    base_hash=None,
                    ptw_hash=ptw_rules_hash,
                    client_hash=client_rules_hash,
                    ptw_content=composed_rules,
                    client_content=client_rules_clean,
                    message=f"Initial divergence for rules on {client_name}.",
                )
                save_conflict_artifacts(cfg.conflicts_dir, rec, dry_run=dry_run)
                messages.append(f"Conflict: Rules diverged on {client_name}")
        else:
            ptw_changed = (ptw_rules_hash != base_hash)
            client_changed = (client_rules_hash != base_hash)

            if not ptw_changed and not client_changed:
                pass
            elif ptw_changed and not client_changed:
                adapter.write_rules_block(composed_rules, dry_run=dry_run)
                objects_state[obj_id] = {"hash": ptw_rules_hash}
                messages.append(f"Propagated rules from Personal Tideway to {client_name}")
            elif not ptw_changed and client_changed:
                # Client-only change: persist into client-specific canonical fragment
                client_rules_dir = cfg.rules_codex if client_name == CLIENT_CODEX else cfg.rules_agy
                shared_rules = compose_rules_content(cfg.rules_shared, Path("/nonexistent")).strip()

                if shared_rules and client_rules_clean.startswith(shared_rules):
                    client_override = client_rules_clean[len(shared_rules):].strip()
                else:
                    client_override = client_rules_clean

                if not dry_run:
                    client_rules_dir.mkdir(parents=True, exist_ok=True)
                    for old_f in client_rules_dir.glob("*.md"):
                        if old_f.name != "client-managed.md":
                            old_f.unlink()
                    atomic_write_text(client_rules_dir / "client-managed.md", client_override + ("\n" if client_override else ""))

                new_composed = get_composed_rules_for_client(cfg, client_name).strip()
                new_hash = calculate_hash(new_composed)
                objects_state[obj_id] = {"hash": new_hash}
                if new_composed != client_rules_clean:
                    adapter.write_rules_block(new_composed, dry_run=dry_run)
                messages.append(f"Propagated rules from {client_name} to Personal Tideway")
            else:
                if ptw_rules_hash == client_rules_hash:
                    objects_state[obj_id] = {"hash": ptw_rules_hash}
                    messages.append(f"Accepted identical dual changes for rules on {client_name}")
                else:
                    has_conflicts = True
                    rec = ConflictRecord(
                        object_id=obj_id,
                        object_type="rules",
                        client=client_name,
                        base_hash=base_hash,
                        ptw_hash=ptw_rules_hash,
                        client_hash=client_rules_hash,
                        ptw_content=composed_rules,
                        client_content=client_rules_clean,
                        message=f"Divergent rules changes on {client_name}.",
                    )
                    save_conflict_artifacts(cfg.conflicts_dir, rec, dry_run=dry_run)
                    messages.append(f"Conflict: Rules diverged on {client_name}")

    # -------------------------------------------------------------
    # 3. Skills Synchronisation
    # -------------------------------------------------------------
    all_skill_names = {s["name"] for s in list_all_skills(cfg)}
    for obj_id in objects_state:
        if obj_id.startswith("skill:"):
            all_skill_names.add(obj_id[6:])
    for s_name in codex_adapter.list_installed_skills():
        all_skill_names.add(s_name)
    for s_name in agy_adapter.list_installed_skills():
        all_skill_names.add(s_name)

    scopes = get_skill_scopes(cfg)

    for s_name in sorted(all_skill_names):
        obj_id = f"skill:{s_name}"
        base_obj = objects_state.get(obj_id)

        skill_info = find_skill(cfg, s_name)
        if skill_info:
            scope = skill_info.scope
            canonical_path = skill_info.path
        elif base_obj and "scope" in base_obj:
            scope = base_obj["scope"]
            canonical_path = scopes[scope] / s_name
        elif s_name in codex_adapter.list_installed_skills():
            scope = CLIENT_CODEX
            canonical_path = scopes[scope] / s_name
        elif s_name in agy_adapter.list_installed_skills():
            scope = CLIENT_AGY
            canonical_path = scopes[scope] / s_name
        else:
            scope = "shared"
            canonical_path = scopes[scope] / s_name

        applicable_clients = [CLIENT_CODEX, CLIENT_AGY] if scope == "shared" else [scope]

        ptw_exists = canonical_path.is_dir()
        ptw_hash = hash_dir(canonical_path) if ptw_exists else None

        base_ptw_hash = base_obj.get("ptw_hash") if base_obj else None
        base_client_hashes = base_obj.get("client_hashes", {}) if base_obj else {}

        # Check deletion in Personal Tideway
        if base_ptw_hash is not None and not ptw_exists:
            messages.append(f"Notice: Skill '{s_name}' was deleted in Personal Tideway (requires explicit resolution).")
            continue

        skill_conflicted = False

        for client_name in applicable_clients:
            client_adapter = codex_adapter if client_name == CLIENT_CODEX else agy_adapter
            client_skills_dir = cfg.codex_skills if client_name == CLIENT_CODEX else cfg.agy_skills
            client_skill_dir = client_skills_dir / s_name
            client_exists = client_skill_dir.is_dir() or client_skill_dir.is_symlink()
            client_hash = hash_dir(client_skill_dir) if client_exists else None
            b_client_hash = base_client_hashes.get(client_name)

            if base_obj is None:
                # First sync
                if ptw_exists and not client_exists:
                    client_adapter.install_skill(s_name, canonical_path, mode=cfg.skill_link_mode, dry_run=dry_run)
                elif not ptw_exists and client_exists:
                    # Client-only skill imported into designated client scope
                    if not dry_run:
                        canonical_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(client_skill_dir, canonical_path)
                    ptw_hash = hash_dir(canonical_path)
                    ptw_exists = True
                elif ptw_exists and client_exists:
                    if client_hash == ptw_hash:
                        pass
                    else:
                        skill_conflicted = True
                        has_conflicts = True
                        rec = ConflictRecord(
                            object_id=obj_id,
                            object_type="skill",
                            client=client_name,
                            base_hash=None,
                            ptw_hash=ptw_hash,
                            client_hash=client_hash,
                            ptw_content=f"Directory hash: {ptw_hash}",
                            client_content=f"Directory hash: {client_hash}",
                            message=f"Initial divergence for skill '{s_name}' on client '{client_name}'.",
                        )
                        save_conflict_artifacts(cfg.conflicts_dir, rec, dry_run=dry_run)
                        messages.append(f"Conflict: Skill '{s_name}' diverged on {client_name}")
            else:
                ptw_changed = (ptw_hash is not None and ptw_hash != base_ptw_hash)
                client_changed = (client_hash is not None and client_hash != b_client_hash)

                if b_client_hash is not None and not client_exists:
                    messages.append(f"Notice: Skill '{s_name}' was deleted on {client_name} (requires explicit resolution).")
                    continue

                if not ptw_changed and not client_changed:
                    if not client_exists and ptw_exists:
                        client_adapter.install_skill(s_name, canonical_path, mode=cfg.skill_link_mode, dry_run=dry_run)
                elif ptw_changed and not client_changed:
                    client_adapter.install_skill(s_name, canonical_path, mode=cfg.skill_link_mode, dry_run=dry_run)
                    messages.append(f"Propagated skill '{s_name}' from Personal Tideway to {client_name}")
                elif not ptw_changed and client_changed:
                    if not dry_run:
                        if canonical_path.exists():
                            shutil.rmtree(canonical_path)
                        shutil.copytree(client_skill_dir, canonical_path)
                    ptw_hash = hash_dir(canonical_path)
                    messages.append(f"Propagated skill '{s_name}' from {client_name} to Personal Tideway")
                    for other_c in applicable_clients:
                        if other_c != client_name:
                            other_adapter = codex_adapter if other_c == CLIENT_CODEX else agy_adapter
                            other_adapter.install_skill(s_name, canonical_path, mode=cfg.skill_link_mode, dry_run=dry_run)
                else:
                    if ptw_hash == client_hash:
                        messages.append(f"Accepted identical dual changes for skill '{s_name}' on {client_name}")
                    else:
                        skill_conflicted = True
                        has_conflicts = True
                        rec = ConflictRecord(
                            object_id=obj_id,
                            object_type="skill",
                            client=client_name,
                            base_hash=base_ptw_hash,
                            ptw_hash=ptw_hash,
                            client_hash=client_hash,
                            ptw_content=f"Directory hash: {ptw_hash}",
                            client_content=f"Directory hash: {client_hash}",
                            message=f"Divergent changes for skill '{s_name}' on {client_name}.",
                        )
                        save_conflict_artifacts(cfg.conflicts_dir, rec, dry_run=dry_run)
                        messages.append(f"Conflict: Skill '{s_name}' diverged on {client_name}")

        if not skill_conflicted and ptw_exists:
            new_client_hashes: dict[str, str] = {}
            for c in applicable_clients:
                c_path = (cfg.codex_skills if c == CLIENT_CODEX else cfg.agy_skills) / s_name
                if c_path.exists():
                    new_client_hashes[c] = hash_dir(c_path) or ""
            objects_state[obj_id] = {
                "ptw_hash": ptw_hash,
                "scope": scope,
                "client_hashes": new_client_hashes,
            }

    # Save state if not dry_run
    save_state(cfg.state_file, state, dry_run=dry_run)

    exit_code = ExitCode.CONFLICT if has_conflicts else ExitCode.SUCCESS
    return int(exit_code), messages


def resolve_conflict(
    cfg: PersonalTidewayConfig,
    object_id: str,
    take: str,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Resolve an existing conflict by applying the selected version atomically.

    Verifies active conflict, updates base state, and removes conflict artifacts.
    """
    valid_takes = ("ptw", CLIENT_CODEX, CLIENT_AGY)
    if take not in valid_takes:
        raise ValidationError(f"Invalid --take option '{take}'. Expected one of: {valid_takes}")

    conflict = load_conflict(cfg.conflicts_dir, object_id)
    if not conflict:
        raise ValidationError(f"No active conflict found for object '{object_id}' in {cfg.conflicts_dir}")

    state = load_state(cfg.state_file)
    objects_state: dict[str, Any] = state.setdefault("objects", {})

    codex_adapter = CodexAdapter(cfg.codex_config, cfg.codex_rules, cfg.codex_skills, cfg.backups_dir)
    agy_adapter = AgyAdapter(cfg.agy_config, cfg.agy_rules, cfg.agy_skills, cfg.backups_dir)

    # 1. MCP Resolution
    if conflict.object_type == "mcp":
        server_name = object_id.split(":", 1)[1] if ":" in object_id else object_id
        ptw_server = load_all_mcp_servers(cfg.mcp_dir).get(server_name)

        if take == "ptw":
            if not ptw_server:
                raise ValidationError(f"Cannot take Personal Tideway version: MCP server '{server_name}' missing in Personal Tideway")
            srv = ptw_server
            if CLIENT_CODEX in srv.targets:
                codex_adapter.write_mcp_servers(
                    {server_name: srv.get_effective_config(CLIENT_CODEX)},
                    {server_name},
                    dry_run=dry_run,
                )
            if CLIENT_AGY in srv.targets:
                agy_adapter.write_mcp_servers(
                    {server_name: srv.get_effective_config(CLIENT_AGY)},
                    {server_name},
                    dry_run=dry_run,
                )
        elif take in (CLIENT_CODEX, CLIENT_AGY):
            adapter = codex_adapter if take == CLIENT_CODEX else agy_adapter
            client_servers = adapter.read_mcp_servers()
            client_cfg = client_servers.get(server_name)
            if not client_cfg:
                raise ValidationError(f"Cannot take {take} version: server '{server_name}' missing on {take}")

            if ptw_server is not None:
                srv = ptw_server
                srv.transport = str(client_cfg.get("transport", TRANSPORT_STDIO))
                srv.command = str(client_cfg["command"]) if client_cfg.get("command") is not None else None
                srv.args = [str(x) for x in client_cfg.get("args", [])]
                srv.url = str(client_cfg["url"]) if client_cfg.get("url") is not None else None
                if client_cfg.get("env"):
                    srv.env = {str(k): str(v) for k, v in client_cfg["env"].items()}
            else:
                srv = MCPServer(
                    name=server_name,
                    transport=str(client_cfg.get("transport", TRANSPORT_STDIO)),
                    command=str(client_cfg["command"]) if client_cfg.get("command") is not None else None,
                    args=[str(x) for x in client_cfg.get("args", [])],
                    url=str(client_cfg["url"]) if client_cfg.get("url") is not None else None,
                    env={str(k): str(v) for k, v in client_cfg.get("env", {}).items()},
                    targets=[CLIENT_CODEX, CLIENT_AGY],
                )

            save_mcp_server(cfg.mcp_dir, srv, dry_run=dry_run)

            other_client = CLIENT_AGY if take == CLIENT_CODEX else CLIENT_CODEX
            if other_client in srv.targets:
                other_adapter = agy_adapter if other_client == CLIENT_AGY else codex_adapter
                other_adapter.write_mcp_servers(
                    {server_name: srv.get_effective_config(other_client)},
                    {server_name},
                    dry_run=dry_run,
                )

        client_hashes: dict[str, str] = {}
        for c in srv.targets:
            eff = normalize_mcp_dict(srv.get_effective_config(c))
            client_hashes[c] = calculate_hash(json.dumps(eff, sort_keys=True))
        norm = normalize_mcp_dict(srv.to_dict())
        h = calculate_hash(json.dumps(norm, sort_keys=True))
        if not dry_run:
            objects_state[object_id] = {
                "ptw_hash": h,
                "client_hashes": client_hashes,
                "normalized": norm,
            }

    # 2. Rules Resolution
    elif conflict.object_type == "rules":
        client_name = conflict.client
        adapter = codex_adapter if client_name == CLIENT_CODEX else agy_adapter

        if take == "ptw":
            composed = get_composed_rules_for_client(cfg, client_name).strip()
            adapter.write_rules_block(composed, dry_run=dry_run)
            h = calculate_hash(composed)
            if not dry_run:
                objects_state[object_id] = {"hash": h}
        elif take in (CLIENT_CODEX, CLIENT_AGY):
            client_rules_dir = cfg.rules_codex if take == CLIENT_CODEX else cfg.rules_agy
            shared_rules = compose_rules_content(cfg.rules_shared, Path("/nonexistent")).strip()
            client_content = (conflict.client_content or "").strip()
            if shared_rules and client_content.startswith(shared_rules):
                client_override = client_content[len(shared_rules):].strip()
            else:
                client_override = client_content

            if not dry_run:
                client_rules_dir.mkdir(parents=True, exist_ok=True)
                for old_f in client_rules_dir.glob("*.md"):
                    if old_f.name != "client-managed.md":
                        old_f.unlink()
                atomic_write_text(client_rules_dir / "client-managed.md", client_override + ("\n" if client_override else ""))

            composed = get_composed_rules_for_client(cfg, client_name).strip()
            adapter.write_rules_block(composed, dry_run=dry_run)
            h = calculate_hash(composed)
            if not dry_run:
                objects_state[object_id] = {"hash": h}

    # 3. Skills Resolution
    elif conflict.object_type == "skill":
        skill_name = object_id.split(":", 1)[1] if ":" in object_id else object_id
        skill_info = find_skill(cfg, skill_name)
        scope = skill_info.scope if skill_info else (objects_state.get(object_id, {}).get("scope") or "shared")
        canonical_dir = get_skill_scopes(cfg)[scope] / skill_name
        applicable_clients = [CLIENT_CODEX, CLIENT_AGY] if scope == "shared" else [scope]

        if take == "ptw":
            if not canonical_dir.is_dir():
                raise ValidationError(f"Cannot take Personal Tideway version: skill '{skill_name}' missing in Personal Tideway")
            for c in applicable_clients:
                adapter = codex_adapter if c == CLIENT_CODEX else agy_adapter
                adapter.install_skill(skill_name, canonical_dir, mode=cfg.skill_link_mode, dry_run=dry_run)
        elif take in (CLIENT_CODEX, CLIENT_AGY):
            c_skills_dir = cfg.codex_skills if take == CLIENT_CODEX else cfg.agy_skills
            c_skill_dir = c_skills_dir / skill_name
            if not (c_skill_dir.is_dir() or c_skill_dir.is_symlink()):
                raise ValidationError(f"Cannot take {take} version: skill '{skill_name}' missing on {take}")

            valid_skill = SkillInfo(name=skill_name, path=c_skill_dir, scope=take)
            ok, reason = valid_skill.is_valid()
            if not ok:
                raise ValidationError(f"Cannot take invalid skill '{skill_name}' from {take}: {reason}")

            if not dry_run:
                canonical_dir.parent.mkdir(parents=True, exist_ok=True)
                if canonical_dir.exists():
                    shutil.rmtree(canonical_dir)
                shutil.copytree(c_skill_dir, canonical_dir)

            for c in applicable_clients:
                if c != take:
                    adapter = codex_adapter if c == CLIENT_CODEX else agy_adapter
                    adapter.install_skill(skill_name, canonical_dir, mode=cfg.skill_link_mode, dry_run=dry_run)

        ptw_h = hash_dir(canonical_dir)
        new_client_hashes: dict[str, str] = {}
        for c in applicable_clients:
            c_p = (cfg.codex_skills if c == CLIENT_CODEX else cfg.agy_skills) / skill_name
            if c_p.exists():
                new_client_hashes[c] = hash_dir(c_p) or ""

        if not dry_run:
            objects_state[object_id] = {
                "ptw_hash": ptw_h,
                "scope": scope,
                "client_hashes": new_client_hashes,
            }

    # Save state and remove conflict artifacts only on non-dry-run
    if not dry_run:
        save_state(cfg.state_file, state, dry_run=False)
        remove_conflict_artifacts(cfg.conflicts_dir, object_id, dry_run=False)

    return True, f"Successfully resolved conflict for '{object_id}' taking '{take}'"
