"""Main CLI entrypoint for Personal Tideway."""

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Sequence

from personal_tideway import __version__
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import (
    CLIENT_AGY,
    CLIENT_CODEX,
    ExitCode,
    SUPPORTED_CLIENTS,
    SUPPORTED_SKILL_LINK_MODES,
    SUPPORTED_TRANSPORTS,
    TRANSPORT_HTTP,
    TRANSPORT_STDIO,
)
from personal_tideway.core.conflicts import load_all_conflicts
from personal_tideway.core.mcp import (
    load_all_mcp_servers,
    load_mcp_server,
    save_mcp_server,
    test_mcp_server,
)
from personal_tideway.core.memory import add_memory, list_memories, search_memories
from personal_tideway.core.project import init_project
from personal_tideway.core.skills import (
    create_skill,
    find_skill,
    link_skill,
    list_all_skills,
    share_skill,
    unlink_skill,
)
from personal_tideway.core.status import format_status_text, get_workspace_status
from personal_tideway.core.sync import resolve_conflict, sync_workspace
from personal_tideway.core.workspace import init_workspace
from personal_tideway.exceptions import (
    ConfigError,
    ConflictError,
    PersonalTidewayError,
    RuntimeProbeError,
    ValidationError,
)
from personal_tideway.models import MCPServer


def build_parser() -> argparse.ArgumentParser:
    """Build root argument parser with all commands and options."""
    parser = argparse.ArgumentParser(
        prog="ptw",
        description="Personal Tideway manager and CLI",
    )
    # Global options
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--home", help="Path to Personal Tideway home directory (overrides PERSONAL_TIDEWAY_HOME)")
    parser.add_argument("--codex-home", help="Path to Codex home directory (overrides CODEX_HOME)")
    parser.add_argument("--gemini-home", "--agy-home", dest="gemini_home", help="Path to agy home directory")

    subparsers = parser.add_subparsers(dest="top_command", required=True)

    # 1. init
    p_init = subparsers.add_parser("init", help="Initialize canonical Personal Tideway directory tree")

    # 2. sync
    p_sync = subparsers.add_parser("sync", help="Synchronize Personal Tideway with clients (3-way sync)")
    p_sync.add_argument("--dry-run", action="store_true", help="Preview changes without modifying files")

    # 3. status
    p_status = subparsers.add_parser("status", help="Show workspace status, pending changes, and parity")
    p_status.add_argument("--json", action="store_true", help="Output status in JSON format")

    # 4. resolve
    p_resolve = subparsers.add_parser("resolve", help="Resolve active conflict for an object")
    p_resolve.add_argument("object", help="Object ID with conflict (e.g. mcp:name, rules:codex)")
    p_resolve.add_argument(
        "--take",
        required=True,
        choices=["ptw", CLIENT_CODEX, CLIENT_AGY],
        help="Version to accept and propagate",
    )
    p_resolve.add_argument("--dry-run", action="store_true", help="Simulate resolution without modifying files")

    # 5. mcp
    p_mcp = subparsers.add_parser("mcp", help="Manage canonical MCP server definitions")
    mcp_subs = p_mcp.add_subparsers(dest="mcp_command", required=True)

    # mcp list
    p_mcp_list = mcp_subs.add_parser("list", help="List all MCP servers")
    p_mcp_list.add_argument("--json", action="store_true", help="Output in JSON format")

    # mcp add
    p_mcp_add = mcp_subs.add_parser("add", help="Add or update an MCP server definition")
    p_mcp_add.add_argument("name", help="Name of server")
    p_mcp_add.add_argument("--transport", required=True, choices=SUPPORTED_TRANSPORTS, help="Server transport")
    p_mcp_add.add_argument("--command", dest="server_command", help="Executable command (stdio transport)")
    p_mcp_add.add_argument("--arg", action="append", dest="arg_items", default=[], help="Repeatable command argument (e.g. --arg -m --arg server)")
    p_mcp_add.add_argument("--args", nargs="*", default=[], help="Command arguments (stdio transport)")
    p_mcp_add.add_argument("--url", help="Endpoint URL (http transport)")
    p_mcp_add.add_argument("--targets", nargs="+", default=[CLIENT_CODEX, CLIENT_AGY], choices=SUPPORTED_CLIENTS)
    p_mcp_add.add_argument("--disabled", action="store_true", help="Create server in disabled state")
    p_mcp_add.add_argument("--env", nargs="*", default=[], help="Environment variables as KEY=VALUE pairs")
    p_mcp_add.add_argument("--profiles", nargs="*", default=[], help="Profile tags")
    p_mcp_add.add_argument("--tags", nargs="*", default=[], help="Server tags")

    # mcp enable / disable
    p_mcp_enable = mcp_subs.add_parser("enable", help="Enable an MCP server")
    p_mcp_enable.add_argument("name", help="Name of server")
    p_mcp_disable = mcp_subs.add_parser("disable", help="Disable an MCP server")
    p_mcp_disable.add_argument("name", help="Name of server")

    # mcp test
    p_mcp_test = mcp_subs.add_parser("test", help="Validate and test MCP servers")
    p_mcp_test.add_argument("name", nargs="?", help="Optional specific server name to test")
    p_mcp_test.add_argument("--static-only", action="store_true", default=True, help="Static validation only (default)")
    p_mcp_test.add_argument("--probe", action="store_true", help="Run bounded process or HTTP probe")

    # 6. skill
    p_skill = subparsers.add_parser("skill", help="Manage skills")
    skill_subs = p_skill.add_subparsers(dest="skill_command", required=True)

    p_skill_list = skill_subs.add_parser("list", help="List all skills")
    p_skill_list.add_argument("--json", action="store_true", help="Output in JSON format")

    p_skill_create = skill_subs.add_parser("create", help="Create a new skill")
    p_skill_create.add_argument("name", help="Skill name")
    p_skill_create.add_argument("--scope", default="shared", choices=["shared", CLIENT_CODEX, CLIENT_AGY])

    p_skill_link = skill_subs.add_parser("link", help="Expose skill to relevant clients")
    p_skill_link.add_argument("name", help="Skill name")

    p_skill_unlink = skill_subs.add_parser("unlink", help="Unlink skill from clients")
    p_skill_unlink.add_argument("name", help="Skill name")

    p_skill_share = skill_subs.add_parser("share", help="Promote client-specific skill to shared")
    p_skill_share.add_argument("name", help="Skill name")

    # 7. memory
    p_memory = subparsers.add_parser("memory", help="Curated markdown memory")
    mem_subs = p_memory.add_subparsers(dest="memory_command", required=True)

    p_mem_add = mem_subs.add_parser("add", help="Add curated memory entry")
    p_mem_add.add_argument("title", help="Memory title")
    p_mem_add.add_argument("--content", default="", help="Inline markdown content")
    p_mem_add.add_argument("--file", help="Path to markdown file to use as content")
    p_mem_add.add_argument("--tags", nargs="*", default=[], help="Tags for memory entry")

    p_mem_list = mem_subs.add_parser("list", help="List memory entries")
    p_mem_list.add_argument("--json", action="store_true", help="Output in JSON format")

    p_mem_search = mem_subs.add_parser("search", help="Search memory entries")
    p_mem_search.add_argument("query", help="Search query")
    p_mem_search.add_argument("--json", action="store_true", help="Output in JSON format")

    # 8. project
    p_proj = subparsers.add_parser("project", help="Project manifest operations")
    proj_subs = p_proj.add_subparsers(dest="project_command", required=True)

    p_proj_init = proj_subs.add_parser("init", help="Initialize .personal-tideway.yaml in project directory")
    p_proj_init.add_argument("path", nargs="?", default=".", help="Project root directory (default: current dir)")
    p_proj_init.add_argument("--template", help="Name of template from Personal Tideway templates directory")
    p_proj_init.add_argument("--force", action="store_true", help="Overwrite existing .personal-tideway.yaml manifest")
    p_proj_init.add_argument("--dry-run", action="store_true", help="Preview without writing manifest")

    return parser


def handle_mcp(cfg: PersonalTidewayConfig, args: argparse.Namespace) -> int:
    """Handler for 'ptw mcp' subcommands."""
    if args.mcp_command == "list":
        servers = load_all_mcp_servers(cfg.mcp_dir)
        if args.json:
            out = [s.to_dict() for s in sorted(servers.values(), key=lambda x: x.name)]
            print(json.dumps(out, indent=2))
        else:
            if not servers:
                print("No MCP servers configured.")
                return ExitCode.SUCCESS
            print(f"{'NAME':<20} {'TRANSPORT':<10} {'ENABLED':<8} {'TARGETS':<15} {'PORTABLE'}")
            print("-" * 65)
            for s in sorted(servers.values(), key=lambda x: x.name):
                portable_str = "YES" if s.is_portable else "NO"
                print(
                    f"{s.name:<20} {s.transport:<10} {str(s.enabled):<8} "
                    f"{','.join(s.targets):<15} {portable_str}"
                )
        return ExitCode.SUCCESS

    elif args.mcp_command == "add":
        env_dict: dict[str, str] = {}
        for item in args.env:
            if "=" not in item:
                raise ValidationError(f"Invalid env argument '{item}'. Expected KEY=VALUE")
            k, v = item.split("=", 1)
            env_dict[k.strip()] = v.strip()

        combined_args = list(args.arg_items) if hasattr(args, "arg_items") and args.arg_items else []
        if getattr(args, "args", None):
            combined_args.extend(args.args)

        server = MCPServer(
            name=args.name,
            transport=args.transport,
            command=args.server_command,
            args=combined_args,
            url=args.url,
            targets=list(args.targets),
            enabled=not args.disabled,
            profiles=list(args.profiles),
            tags=list(args.tags),
            env=env_dict,
        )
        save_path = save_mcp_server(cfg.mcp_dir, server)
        print(f"Saved MCP server definition to {save_path}")
        return ExitCode.SUCCESS

    elif args.mcp_command in ("enable", "disable"):
        target_file = cfg.mcp_dir / f"{args.name}.yaml"
        server = load_mcp_server(target_file)
        server.enabled = (args.mcp_command == "enable")
        save_mcp_server(cfg.mcp_dir, server)
        print(f"MCP server '{args.name}' is now {'enabled' if server.enabled else 'disabled'}.")
        return ExitCode.SUCCESS

    elif args.mcp_command == "test":
        static_only = not args.probe
        servers = load_all_mcp_servers(cfg.mcp_dir)
        if args.name:
            if args.name not in servers:
                raise ValidationError(f"MCP server '{args.name}' not found")
            test_targets = [servers[args.name]]
        else:
            test_targets = list(servers.values())

        if not test_targets:
            print("No MCP servers to test.")
            return ExitCode.SUCCESS

        all_passed = True
        probe_failure = False

        for s in test_targets:
            passed, messages = test_mcp_server(
                s,
                cfg.secrets_env,
                static_only=static_only,
            )
            for m in messages:
                print(m)
            if not passed:
                all_passed = False
                if "Runtime probe: FAIL" in " ".join(messages):
                    probe_failure = True

        if not all_passed:
            return ExitCode.RUNTIME_PROBE_ERROR if probe_failure else ExitCode.VALIDATION_ERROR
        return ExitCode.SUCCESS

    return ExitCode.SUCCESS


def handle_skill(cfg: PersonalTidewayConfig, args: argparse.Namespace) -> int:
    """Handler for 'ptw skill' subcommands."""
    if args.skill_command == "list":
        skills = list_all_skills(cfg)
        if args.json:
            print(json.dumps(skills, indent=2))
        else:
            if not skills:
                print("No skills found.")
                return ExitCode.SUCCESS
            print(f"{'NAME':<20} {'SCOPE':<10} {'VALID':<8} {'CODEX':<8} {'AGY':<8}")
            print("-" * 55)
            for s in skills:
                valid_str = "YES" if s["valid"] else "NO"
                c_str = "YES" if s["installed_codex"] else "NO"
                a_str = "YES" if s["installed_agy"] else "NO"
                print(f"{s['name']:<20} {s['scope']:<10} {valid_str:<8} {c_str:<8} {a_str:<8}")
        return ExitCode.SUCCESS

    elif args.skill_command == "create":
        target = create_skill(cfg, args.name, scope=args.scope)
        print(f"Created skill '{args.name}' in {target}")
        return ExitCode.SUCCESS

    elif args.skill_command == "link":
        actions = link_skill(cfg, args.name)
        for act in actions:
            print(act)
        return ExitCode.SUCCESS

    elif args.skill_command == "unlink":
        actions = unlink_skill(cfg, args.name)
        for act in actions:
            print(act)
        return ExitCode.SUCCESS

    elif args.skill_command == "share":
        target = share_skill(cfg, args.name)
        print(f"Promoted skill '{args.name}' to shared scope: {target}")
        return ExitCode.SUCCESS

    return ExitCode.SUCCESS


def handle_memory(cfg: PersonalTidewayConfig, args: argparse.Namespace) -> int:
    """Handler for 'ptw memory' subcommands."""
    if args.memory_command == "add":
        content = args.content
        if args.file:
            content = Path(args.file).read_text(encoding="utf-8")
        entry = add_memory(cfg, title=args.title, content=content, tags=args.tags)
        print(f"Created memory entry '{entry.id}' ({entry.title})")
        return ExitCode.SUCCESS

    elif args.memory_command == "list":
        entries = list_memories(cfg)
        if args.json:
            out = [
                {"id": e.id, "title": e.title, "tags": e.tags, "created_at": e.created_at}
                for e in entries
            ]
            print(json.dumps(out, indent=2))
        else:
            if not entries:
                print("No memory entries found.")
                return ExitCode.SUCCESS
            print(f"{'ID':<30} {'DATE':<12} {'TITLE':<30} {'TAGS'}")
            print("-" * 80)
            for e in entries:
                print(f"{e.id:<30} {e.created_at[:10]:<12} {e.title:<30} {','.join(e.tags)}")
        return ExitCode.SUCCESS

    elif args.memory_command == "search":
        matches = search_memories(cfg, args.query)
        if args.json:
            out = [
                {
                    "id": m.id,
                    "title": m.title,
                    "tags": m.tags,
                    "created_at": m.created_at,
                    "content": m.content,
                }
                for m in matches
            ]
            print(json.dumps(out, indent=2))
        else:
            if not matches:
                print(f"No memory entries matching '{args.query}'.")
                return ExitCode.SUCCESS
            print(f"Found {len(matches)} matching entries for '{args.query}':\n")
            for m in matches:
                print(f"[{m.id}] {m.title} (Tags: {', '.join(m.tags)})")
                snippet = m.content[:150] + ("..." if len(m.content) > 150 else "")
                print(f"  {snippet}\n")
        return ExitCode.SUCCESS

    return ExitCode.SUCCESS


def preprocess_cli_args(argv: Sequence[str] | None) -> list[str]:
    """Preprocess CLI arguments to cleanly handle flags beginning with '-' in --arg and --args."""
    if argv is None:
        raw = list(sys.argv[1:])
    else:
        raw = list(argv)

    known_flags = {
        "--home", "--codex-home", "--gemini-home", "--agy-home",
        "--dry-run", "--json", "--take", "--transport", "--command",
        "--url", "--targets", "--disabled", "--env", "--profiles",
        "--tags", "--static-only", "--probe", "--scope", "--content",
        "--file", "--template", "--force",
    }

    processed: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        token = raw[i]
        if token == "--arg":
            if i + 1 < n:
                next_tok = raw[i + 1]
                if next_tok in known_flags or next_tok == "--arg" or next_tok == "--args" or next_tok.startswith("--arg="):
                    processed.append(token)
                else:
                    processed.append(f"--arg={next_tok}")
                    i += 1
            else:
                processed.append(token)
        elif token == "--args":
            i += 1
            while i < n and raw[i] not in known_flags and not raw[i].startswith("--arg="):
                if raw[i] == "--arg":
                    break
                processed.append(f"--arg={raw[i]}")
                i += 1
            continue
        else:
            processed.append(token)
        i += 1

    return processed


def main(argv: Sequence[str] | None = None) -> int:
    """Main CLI entrypoint."""
    parser = build_parser()
    clean_argv = preprocess_cli_args(argv)
    args = parser.parse_args(clean_argv)

    try:
        # Resolve configuration
        cfg = PersonalTidewayConfig.resolve(
            home=args.home,
            codex_home=args.codex_home,
            gemini_home=args.gemini_home,
        )

        if args.top_command == "init":
            init_workspace(cfg)
            print(f"Initialized Personal Tideway at {cfg.home}")
            return ExitCode.SUCCESS

        # For commands other than init and project, verify workspace is initialized
        if args.top_command not in ("init", "project") and not cfg.is_initialized():
            raise ConfigError(
                f"Personal Tideway workspace not initialized at {cfg.home}. Run 'ptw init' first."
            )

        if args.top_command == "sync":
            code, msgs = sync_workspace(cfg, dry_run=args.dry_run)
            prefix = "[DRY RUN] " if args.dry_run else ""
            for m in msgs:
                print(f"{prefix}{m}")
            if not msgs:
                print(f"{prefix}Workspace is in sync. No changes needed.")
            return code

        elif args.top_command == "status":
            status = get_workspace_status(cfg)
            if args.json:
                print(json.dumps(status, indent=2, sort_keys=True))
            else:
                print(format_status_text(status))
            return ExitCode.SUCCESS

        elif args.top_command == "resolve":
            ok, msg = resolve_conflict(cfg, args.object, take=args.take, dry_run=args.dry_run)
            prefix = "[DRY RUN] " if args.dry_run else ""
            print(f"{prefix}{msg}")
            return ExitCode.SUCCESS

        elif args.top_command == "mcp":
            return handle_mcp(cfg, args)

        elif args.top_command == "skill":
            return handle_skill(cfg, args)

        elif args.top_command == "memory":
            return handle_memory(cfg, args)

        elif args.top_command == "project":
            if args.project_command == "init":
                target_file = init_project(
                    cfg,
                    target_path=args.path,
                    template_name=args.template,
                    force=args.force,
                    dry_run=args.dry_run,
                )
                prefix = "[DRY RUN] " if args.dry_run else ""
                print(f"{prefix}Initialized project manifest at {target_file}")
                return ExitCode.SUCCESS

    except PersonalTidewayError as e:
        sys.stderr.write(f"Error: {e}\n")
        return int(e.exit_code)
    except Exception as e:
        sys.stderr.write(f"Unexpected error: {e}\n")
        return ExitCode.VALIDATION_ERROR

    return ExitCode.SUCCESS


if __name__ == "__main__":
    sys.exit(main())
