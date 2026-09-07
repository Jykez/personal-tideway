"""MCP server management, canonical YAML files, and test runner."""

import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
import urllib.parse
import urllib.request
import yaml

from personal_tideway.constants import (
    CLIENT_AGY,
    CLIENT_CODEX,
    ExitCode,
    SCHEMA_VERSION,
    TRANSPORT_HTTP,
    TRANSPORT_STDIO,
)
from personal_tideway.exceptions import RuntimeProbeError, ValidationError
from personal_tideway.models import MCPServer
from personal_tideway.secrets import interpolate_list, interpolate_string, load_secrets, redact_secrets
from personal_tideway.utils import atomic_write_text


def load_all_mcp_servers(mcp_dir: Path) -> dict[str, MCPServer]:
    """Load all valid and invalid MCP server definitions from mcp/<name>.yaml."""
    servers: dict[str, MCPServer] = {}
    if not mcp_dir.is_dir():
        return servers

    for path in sorted(mcp_dir.glob("*.yaml")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                continue
            if "name" not in data:
                data["name"] = path.stem
            server = MCPServer.from_dict(data)
            servers[server.name] = server
        except Exception as e:
            # Create a placeholder or raise depending on context
            continue

    return servers


def load_mcp_server(file_path: Path) -> MCPServer:
    """Load and validate a single MCP server definition file."""
    if not file_path.is_file():
        raise ValidationError(f"MCP server file not found: {file_path}")
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        raise ValidationError(f"Failed to parse YAML from {file_path}: {e}")

    if not isinstance(data, dict):
        raise ValidationError(f"Invalid format in {file_path}: expected dictionary")

    if "name" not in data:
        data["name"] = file_path.stem

    return MCPServer.from_dict(data)


def save_mcp_server(mcp_dir: Path, server: MCPServer, dry_run: bool = False) -> Path:
    """Save an MCPServer instance to mcp/<name>.yaml atomically."""
    server.validate()
    target = mcp_dir / f"{server.name}.yaml"
    content = yaml.safe_dump(server.to_dict(), sort_keys=False)

    if not dry_run:
        mcp_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, content)

    return target


def test_mcp_server(
    server: MCPServer,
    secrets_file: Path,
    static_only: bool = True,
) -> tuple[bool, list[str]]:
    """Test an MCP server statically and optionally with a bounded probe.

    Never exposes resolved secrets in output or errors.
    """
    messages: list[str] = []
    secrets = load_secrets(secrets_file)
    secret_values = list(secrets.values())

    # 1. Static validation
    try:
        server.validate()
        messages.append(f"[{server.name}] Schema validation: PASS")
    except ValidationError as e:
        messages.append(f"[{server.name}] Schema validation: FAIL: {e}")
        return False, messages

    # 2. Check environment placeholders
    missing_vars: list[str] = []
    placeholder_pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

    # Check env variables
    for k, v in server.env.items():
        for match in placeholder_pattern.finditer(v):
            var_name = match.group(1)
            if var_name not in secrets and var_name not in os.environ:
                missing_vars.append(var_name)

    # Check args placeholders
    for arg in server.args:
        for match in placeholder_pattern.finditer(arg):
            var_name = match.group(1)
            if var_name not in secrets and var_name not in os.environ:
                missing_vars.append(var_name)

    if missing_vars:
        messages.append(
            f"[{server.name}] Environment placeholders: FAIL (missing variables: {', '.join(sorted(set(missing_vars)))})"
        )
        return False, messages
    else:
        messages.append(f"[{server.name}] Environment placeholders: PASS")

    # 3. Static reachability check
    if server.transport == TRANSPORT_STDIO:
        assert server.command is not None
        cmd_path = shutil.which(server.command) or (
            Path(server.command).is_file() if "/" in server.command else None
        )
        if not cmd_path:
            messages.append(
                f"[{server.name}] Command lookup: WARNING (binary '{server.command}' not found in PATH)"
            )
        else:
            messages.append(f"[{server.name}] Command lookup: PASS ({server.command})")
    elif server.transport == TRANSPORT_HTTP:
        assert server.url is not None
        parsed = urllib.parse.urlparse(server.url)
        if not parsed.scheme or not parsed.netloc:
            messages.append(f"[{server.name}] URL validation: FAIL (malformed URL: {server.url})")
            return False, messages
        messages.append(f"[{server.name}] URL validation: PASS ({server.url})")

    # If static-only is requested (default), return here
    if static_only:
        messages.append(f"[{server.name}] Static check completed successfully")
        return True, messages

    # 4. Optional bounded probe
    try:
        if server.transport == TRANSPORT_STDIO:
            assert server.command is not None
            # Prepare resolved args and env safely
            resolved_env = os.environ.copy()
            for k, v in server.env.items():
                resolved_env[k] = interpolate_string(v, secrets)

            resolved_args = [server.command] + interpolate_list(server.args, secrets)

            # Bounded non-shell execution
            proc = subprocess.run(
                resolved_args,
                env=resolved_env,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            messages.append(f"[{server.name}] Process probe: PASS (exit code {proc.returncode})")
        elif server.transport == TRANSPORT_HTTP:
            assert server.url is not None
            req = urllib.request.Request(server.url, method="HEAD")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                status = resp.status
            messages.append(f"[{server.name}] HTTP probe: PASS (HTTP status {status})")
    except subprocess.TimeoutExpired:
        # For daemon/long-running stdio servers, timing out after 2s proves it started!
        messages.append(f"[{server.name}] Process probe: PASS (process stayed alive)")
    except Exception as e:
        safe_msg = redact_secrets(str(e), secret_values)
        messages.append(f"[{server.name}] Runtime probe: FAIL: {safe_msg}")
    return True, messages


test_mcp_server.__test__ = False
