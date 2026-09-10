"""Read-only client discovery and diagnostics for Codex and related clients."""

from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib
from typing import Any

from personal_tideway.constants import ExitCode


@dataclass
class DiagnosticCheck:
    """A single diagnostic health check result."""

    id: str
    status: str  # "ok", "warning", "error"
    message: str
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "message": self.message,
        }
        if self.remediation is not None:
            data["remediation"] = self.remediation
        return data


@dataclass
class CodexDiscoveryResult:
    """Structured result of read-only Codex discovery."""

    installed: bool
    executable: str | None
    version: str | None
    paths: dict[str, str | None]
    checks: list[DiagnosticCheck]
    status: str  # "ok", "warning", "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "executable": self.executable,
            "version": self.version,
            "paths": dict(self.paths),
            "checks": [c.to_dict() for c in self.checks],
            "status": self.status,
        }


@dataclass
class AgyDiscoveryResult:
    """Structured result of read-only AGY (Antigravity CLI) discovery."""

    installed: bool
    executable: str | None
    version: str | None
    paths: dict[str, Any]
    checks: list[DiagnosticCheck]
    status: str  # "ok", "warning", "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "executable": self.executable,
            "version": self.version,
            "paths": dict(self.paths),
            "checks": [c.to_dict() for c in self.checks],
            "status": self.status,
        }


def _is_non_empty_file(path: Path) -> bool:
    """Check whether a file exists and contains non-whitespace content without loading full contents."""
    if not path.is_file():
        return False
    try:
        if path.stat().st_size == 0:
            return False
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    return True
    except OSError:
        return False
    return False


def _validate_codex_config(config_path: Path) -> tuple[str, str, str | None]:
    """Validate config.toml without leaking contents or raw exception text."""
    if not config_path.is_file():
        return "ok", "Codex configuration file does not exist (using default configuration)", None

    try:
        with open(config_path, "rb") as f:
            tomllib.load(f)
        return "ok", f"Codex configuration file is valid TOML at {config_path}", None
    except Exception:
        # Never expose raw exception string or file contents
        return (
            "error",
            f"Codex configuration file contains invalid TOML syntax at {config_path}",
            f"Fix syntax errors in {config_path}",
        )


def _validate_json_syntax(path: Path, label: str) -> tuple[str, str, str | None]:
    """Validate JSON file syntax without leaking keys, values, or raw exception text."""
    if not path.is_file():
        return "ok", f"{label} file does not exist (optional)", None

    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
        return "ok", f"{label} file is valid JSON at {path}", None
    except Exception:
        # Never expose raw exception string, file contents, keys, or values
        return (
            "error",
            f"{label} file contains invalid JSON syntax at {path}",
            f"Fix syntax errors in {path}",
        )


def _default_version_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    """Default no-shell subprocess runner with bounded timeout."""
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def discover_codex(
    codex_home: Path | str | None = None,
    canonical_user_skills: Path | str | None = None,
    executable_resolver: Callable[[str], str | None] | None = None,
    version_runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
    timeout: float = 2.0,
    max_output_bytes: int = 1024,
) -> CodexDiscoveryResult:
    """Perform deterministic, read-only discovery of Codex client.

    This function performs no writes, never inspects auth.json, boundedly probes version,
    and safely validates config.toml without leaking secrets or raw values.
    """
    # 1. Resolve paths
    if codex_home is not None:
        resolved_codex_home = Path(codex_home).expanduser().resolve()
    elif "CODEX_HOME" in os.environ and os.environ["CODEX_HOME"].strip():
        resolved_codex_home = Path(os.environ["CODEX_HOME"]).expanduser().resolve()
    else:
        resolved_codex_home = (Path.home() / ".codex").resolve()

    if canonical_user_skills is not None:
        resolved_canonical_skills = Path(canonical_user_skills).expanduser().resolve()
    else:
        resolved_canonical_skills = (Path.home() / ".agents" / "skills").resolve()

    config_path = resolved_codex_home / "config.toml"
    agents_override_path = resolved_codex_home / "AGENTS.override.md"
    agents_path = resolved_codex_home / "AGENTS.md"
    legacy_skills_path = resolved_codex_home / "skills"

    checks: list[DiagnosticCheck] = []

    # 2. Executable discovery
    resolver = executable_resolver if executable_resolver is not None else shutil.which
    executable_path = resolver("codex")
    installed = executable_path is not None
    version: str | None = None

    if installed and executable_path:
        checks.append(
            DiagnosticCheck(
                id="codex_executable",
                status="ok",
                message=f"Codex executable found at {executable_path}",
            )
        )

        # 3. Version probe (no-shell subprocess, bounded timeout and bounded output)
        runner = version_runner if version_runner is not None else _default_version_runner
        try:
            retcode, stdout, stderr = runner([executable_path, "--version"], timeout)
            if retcode != 0:
                checks.append(
                    DiagnosticCheck(
                        id="codex_version",
                        status="error",
                        message=f"Version probe returned non-zero exit code {retcode}",
                        remediation=f"Run '{executable_path} --version' manually to diagnose",
                    )
                )
            else:
                raw_out = (stdout or stderr or "")[:max_output_bytes].strip()
                lines = [line.strip() for line in raw_out.splitlines() if line.strip()]
                version = lines[0] if lines else "unknown"
                checks.append(
                    DiagnosticCheck(
                        id="codex_version",
                        status="ok",
                        message=f"Codex version: {version}",
                    )
                )
        except (subprocess.TimeoutExpired, TimeoutError):
            checks.append(
                DiagnosticCheck(
                    id="codex_version",
                    status="error",
                    message=f"Timeout while probing Codex version (exceeded {timeout:.1f}s)",
                    remediation="Verify Codex CLI is responsive and not hanging",
                )
            )
        except Exception as e:
            checks.append(
                DiagnosticCheck(
                    id="codex_version",
                    status="error",
                    message=f"Version probe failed: {type(e).__name__}",
                    remediation=f"Verify execution permissions for {executable_path}",
                )
            )
    else:
        checks.append(
            DiagnosticCheck(
                id="codex_executable",
                status="warning",
                message="Codex executable not found in PATH",
                remediation="Install Codex CLI or ensure 'codex' is available in PATH",
            )
        )
        checks.append(
            DiagnosticCheck(
                id="codex_version",
                status="warning",
                message="Codex version probe skipped (executable not found)",
                remediation="Install Codex CLI to enable version probe",
            )
        )

    # 4. Config validation (never opens auth.json, never leaks values or exception strings)
    cfg_status, cfg_msg, cfg_rem = _validate_codex_config(config_path)
    checks.append(
        DiagnosticCheck(
            id="codex_config",
            status=cfg_status,
            message=cfg_msg,
            remediation=cfg_rem,
        )
    )

    # 5. Global rules & shadowing diagnosis
    override_non_empty = _is_non_empty_file(agents_override_path)
    agents_non_empty = _is_non_empty_file(agents_path)
    effective_global_rules: Path | None = None

    if override_non_empty:
        effective_global_rules = agents_override_path
        checks.append(
            DiagnosticCheck(
                id="codex_rules",
                status="warning",
                message="AGENTS.override.md is active and shadows managed AGENTS.md",
                remediation=f"Remove or merge {agents_override_path} if managed rules in AGENTS.md should apply",
            )
        )
    elif agents_override_path.is_file() and not override_non_empty:
        # Override file exists but is empty -> fallback to AGENTS.md
        if agents_non_empty:
            effective_global_rules = agents_path
            checks.append(
                DiagnosticCheck(
                    id="codex_rules",
                    status="ok",
                    message="AGENTS.override.md is empty; using AGENTS.md as effective global rules",
                )
            )
        else:
            effective_global_rules = None
            checks.append(
                DiagnosticCheck(
                    id="codex_rules",
                    status="ok",
                    message="No effective global rules found (AGENTS.override.md is empty and AGENTS.md is missing or empty)",
                )
            )
    elif agents_non_empty:
        effective_global_rules = agents_path
        checks.append(
            DiagnosticCheck(
                id="codex_rules",
                status="ok",
                message="Using AGENTS.md as effective global rules",
            )
        )
    else:
        effective_global_rules = None
        checks.append(
            DiagnosticCheck(
                id="codex_rules",
                status="ok",
                message="No global rules found (neither AGENTS.override.md nor AGENTS.md exists)",
            )
        )

    # 6. Canonical vs legacy skills diagnosis
    if legacy_skills_path.exists():
        checks.append(
            DiagnosticCheck(
                id="codex_skills",
                status="warning",
                message=f"Legacy skills directory found at {legacy_skills_path}",
                remediation=f"Migrate skills from {legacy_skills_path} to canonical user skills directory {resolved_canonical_skills} (manual migration required; no automatic migration performed)",
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                id="codex_skills",
                status="ok",
                message=f"No legacy skills directory at {legacy_skills_path}",
            )
        )

    # 7. Aggregate overall status
    has_error = any(c.status == "error" for c in checks)
    has_warning = any(c.status == "warning" for c in checks)
    overall_status = "error" if has_error else ("warning" if has_warning else "ok")

    paths_map = {
        "codex_home": str(resolved_codex_home),
        "config": str(config_path),
        "agents_md": str(agents_path),
        "agents_override_md": str(agents_override_path),
        "effective_global_rules": str(effective_global_rules) if effective_global_rules else None,
        "canonical_user_skills": str(resolved_canonical_skills),
        "legacy_skills": str(legacy_skills_path),
    }

    return CodexDiscoveryResult(
        installed=installed,
        executable=executable_path,
        version=version,
        paths=paths_map,
        checks=checks,
        status=overall_status,
    )


def discover_agy(
    gemini_home: Path | str | None = None,
    customization_root: Path | str | None = None,
    portable_skills_alias: Path | str | None = None,
    executable_resolver: Callable[[str], str | None] | None = None,
    version_runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
    timeout: float = 2.0,
    max_output_bytes: int = 1024,
) -> AgyDiscoveryResult:
    """Perform deterministic, read-only discovery of AGY (Antigravity CLI) client.

    This function performs no writes, never inspects auth files, boundedly probes version,
    and safely validates JSON files without leaking secrets, keys, or raw values.
    """
    # 1. Resolve paths
    if gemini_home is not None:
        resolved_gemini_home = Path(gemini_home).expanduser().resolve()
    elif "GEMINI_HOME" in os.environ and os.environ["GEMINI_HOME"].strip():
        resolved_gemini_home = Path(os.environ["GEMINI_HOME"]).expanduser().resolve()
    elif "AGY_HOME" in os.environ and os.environ["AGY_HOME"].strip():
        resolved_gemini_home = Path(os.environ["AGY_HOME"]).expanduser().resolve()
    else:
        resolved_gemini_home = (Path.home() / ".gemini").resolve()

    if customization_root is not None:
        resolved_customization_root = Path(customization_root).expanduser().resolve()
    else:
        resolved_customization_root = (resolved_gemini_home / "config").resolve()

    if portable_skills_alias is not None:
        resolved_portable_skills = Path(portable_skills_alias).expanduser().resolve()
    else:
        resolved_portable_skills = (Path.home() / ".agents" / "skills").resolve()

    cli_settings_path = resolved_gemini_home / "antigravity-cli" / "settings.json"
    mcp_config_path = resolved_customization_root / "mcp_config.json"
    rules_root = resolved_customization_root / "rules"
    rules_agents_md = resolved_customization_root / "AGENTS.md"
    rules_gemini_md = resolved_customization_root / "GEMINI.md"
    current_skills_path = resolved_customization_root / "skills"
    legacy_gemini_md = resolved_gemini_home / "GEMINI.md"
    legacy_skills_path = resolved_gemini_home / "skills"

    rule_candidates = [
        str(rules_root),
        str(rules_agents_md),
        str(rules_gemini_md),
    ]

    checks: list[DiagnosticCheck] = []

    # 2. Executable discovery
    resolver = executable_resolver if executable_resolver is not None else shutil.which
    executable_path = resolver("agy")
    installed = executable_path is not None
    version: str | None = None

    if installed and executable_path:
        checks.append(
            DiagnosticCheck(
                id="agy_executable",
                status="ok",
                message=f"AGY executable found at {executable_path}",
            )
        )

        # 3. Version probe (no-shell subprocess, bounded timeout and bounded output)
        runner = version_runner if version_runner is not None else _default_version_runner
        try:
            retcode, stdout, stderr = runner([executable_path, "--version"], timeout)
            if retcode != 0:
                checks.append(
                    DiagnosticCheck(
                        id="agy_version",
                        status="error",
                        message=f"Version probe returned non-zero exit code {retcode}",
                        remediation=f"Run '{executable_path} --version' manually to diagnose",
                    )
                )
            else:
                raw_out = (stdout or stderr or "")[:max_output_bytes].strip()
                lines = [line.strip() for line in raw_out.splitlines() if line.strip()]
                version = lines[0] if lines else "unknown"
                checks.append(
                    DiagnosticCheck(
                        id="agy_version",
                        status="ok",
                        message=f"AGY version: {version}",
                    )
                )
        except (subprocess.TimeoutExpired, TimeoutError):
            checks.append(
                DiagnosticCheck(
                    id="agy_version",
                    status="error",
                    message=f"Timeout while probing AGY version (exceeded {timeout:.1f}s)",
                    remediation="Verify AGY CLI is responsive and not hanging",
                )
            )
        except Exception as e:
            checks.append(
                DiagnosticCheck(
                    id="agy_version",
                    status="error",
                    message=f"Version probe failed: {type(e).__name__}",
                    remediation=f"Verify execution permissions for {executable_path}",
                )
            )
    else:
        checks.append(
            DiagnosticCheck(
                id="agy_executable",
                status="warning",
                message="AGY executable not found in PATH",
                remediation="Install Antigravity CLI or ensure 'agy' is available in PATH",
            )
        )
        checks.append(
            DiagnosticCheck(
                id="agy_version",
                status="warning",
                message="AGY version probe skipped (executable not found)",
                remediation="Install Antigravity CLI to enable version probe",
            )
        )

    # 4. Validate CLI settings JSON
    settings_status, settings_msg, settings_rem = _validate_json_syntax(cli_settings_path, "AGY CLI settings")
    checks.append(
        DiagnosticCheck(
            id="agy_cli_settings",
            status=settings_status,
            message=settings_msg,
            remediation=settings_rem,
        )
    )

    # 5. Validate MCP config JSON
    mcp_status, mcp_msg, mcp_rem = _validate_json_syntax(mcp_config_path, "AGY MCP configuration")
    checks.append(
        DiagnosticCheck(
            id="agy_mcp_config",
            status=mcp_status,
            message=mcp_msg,
            remediation=mcp_rem,
        )
    )

    # 6. Global rules candidate discovery (no safe precedence assumed)
    found_candidates: list[str] = []
    if rules_root.is_dir() and any(rules_root.iterdir()):
        found_candidates.append("rules/")
    elif rules_root.is_dir():
        found_candidates.append("rules/ (empty)")
    if _is_non_empty_file(rules_agents_md):
        found_candidates.append("AGENTS.md")
    if _is_non_empty_file(rules_gemini_md):
        found_candidates.append("GEMINI.md")

    if found_candidates:
        checks.append(
            DiagnosticCheck(
                id="agy_rules",
                status="ok",
                message=f"Current-root rules exist under {resolved_customization_root}: {', '.join(found_candidates)} (candidates: rules/, AGENTS.md, GEMINI.md; contract defines no safe precedence)",
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                id="agy_rules",
                status="ok",
                message=f"No current-root rules found under {resolved_customization_root} (candidates: rules/, AGENTS.md, GEMINI.md)",
            )
        )

    # 7. Current skills directory and portable skills alias
    checks.append(
        DiagnosticCheck(
            id="agy_skills",
            status="ok",
            message=f"AGY skills directory at {current_skills_path} (exists: {current_skills_path.exists()})",
        )
    )
    checks.append(
        DiagnosticCheck(
            id="agy_portable_skills_alias",
            status="ok",
            message=f"Portable Agent Skills alias at {resolved_portable_skills} (exists: {resolved_portable_skills.exists()})",
        )
    )

    # 8. Legacy paths diagnosis
    if legacy_gemini_md.exists():
        checks.append(
            DiagnosticCheck(
                id="agy_legacy_gemini_md",
                status="warning",
                message=f"Legacy global GEMINI.md found at {legacy_gemini_md}",
                remediation=f"Manual migration required: copy or move rules from {legacy_gemini_md} to {resolved_customization_root} (migration was not performed)",
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                id="agy_legacy_gemini_md",
                status="ok",
                message=f"No legacy global GEMINI.md at {legacy_gemini_md}",
            )
        )

    if legacy_skills_path.exists():
        checks.append(
            DiagnosticCheck(
                id="agy_legacy_skills",
                status="warning",
                message=f"Legacy skills directory found at {legacy_skills_path}",
                remediation=f"Manual migration required: copy or move skills from {legacy_skills_path} to {current_skills_path} (migration was not performed)",
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                id="agy_legacy_skills",
                status="ok",
                message=f"No legacy skills directory at {legacy_skills_path}",
            )
        )

    # 9. Aggregate status
    has_error = any(c.status == "error" for c in checks)
    has_warning = any(c.status == "warning" for c in checks)
    overall_status = "error" if has_error else ("warning" if has_warning else "ok")

    paths_map: dict[str, Any] = {
        "gemini_home": str(resolved_gemini_home),
        "customization_root": str(resolved_customization_root),
        "cli_settings": str(cli_settings_path),
        "mcp_config": str(mcp_config_path),
        "rules_root": str(rules_root),
        "rule_candidates": rule_candidates,
        "current_skills": str(current_skills_path),
        "current_agy_skills": str(current_skills_path),
        "portable_skills_alias": str(resolved_portable_skills),
        "legacy_gemini_md": str(legacy_gemini_md),
        "legacy_skills": str(legacy_skills_path),
    }

    return AgyDiscoveryResult(
        installed=installed,
        executable=executable_path,
        version=version,
        paths=paths_map,
        checks=checks,
        status=overall_status,
    )


def run_doctor(
    codex_home: Path | str | None = None,
    canonical_user_skills: Path | str | None = None,
    gemini_home: Path | str | None = None,
    customization_root: Path | str | None = None,
    executable_resolver: Callable[[str], str | None] | None = None,
    version_runner: Callable[[list[str], float], tuple[int, str, str]] | None = None,
) -> dict[str, Any]:
    """Run diagnostic doctor checks across supported clients."""
    codex_diag = discover_codex(
        codex_home=codex_home,
        canonical_user_skills=canonical_user_skills,
        executable_resolver=executable_resolver,
        version_runner=version_runner,
    )
    agy_diag = discover_agy(
        gemini_home=gemini_home,
        customization_root=customization_root,
        portable_skills_alias=canonical_user_skills,
        executable_resolver=executable_resolver,
        version_runner=version_runner,
    )

    all_checks = list(codex_diag.checks) + list(agy_diag.checks)
    has_error = any(c.status == "error" for c in all_checks)
    has_warning = any(c.status == "warning" for c in all_checks)
    overall_status = "error" if has_error else ("warning" if has_warning else "ok")

    return {
        "coverage": "Codex + AGY",
        "status": overall_status,
        "clients": {
            "codex": codex_diag.to_dict(),
            "agy": agy_diag.to_dict(),
        },
        "checks": [c.to_dict() for c in all_checks],
    }


def format_client_codex_text(data: dict[str, Any]) -> str:
    """Format Codex client discovery result into human-readable text."""
    lines = [
        "=== Client: Codex ===",
        f"Status: {data.get('status', 'unknown').upper()}",
        f"Installed: {data.get('installed', False)}",
    ]
    if data.get("executable"):
        lines.append(f"Executable: {data['executable']}")
    if data.get("version"):
        lines.append(f"Version: {data['version']}")

    paths = data.get("paths", {})
    lines.append("Paths:")
    lines.append(f"  Codex Home: {paths.get('codex_home')}")
    lines.append(f"  Config: {paths.get('config')}")
    lines.append(f"  Effective Rules: {paths.get('effective_global_rules') or 'None'}")
    lines.append(f"  Canonical Skills: {paths.get('canonical_user_skills')}")
    lines.append(f"  Legacy Skills: {paths.get('legacy_skills')}")

    checks = data.get("checks", [])
    if checks:
        lines.append("Diagnostics:")
        for c in checks:
            tag = c["status"].upper()
            lines.append(f"  [{tag}] {c['id']}: {c['message']}")
            if c.get("remediation"):
                lines.append(f"         Remediation: {c['remediation']}")
    return "\n".join(lines)


def format_client_agy_text(data: dict[str, Any]) -> str:
    """Format AGY client discovery result into human-readable text."""
    lines = [
        "=== Client: Antigravity (agy) ===",
        f"Status: {data.get('status', 'unknown').upper()}",
        f"Installed: {data.get('installed', False)}",
    ]
    if data.get("executable"):
        lines.append(f"Executable: {data['executable']}")
    if data.get("version"):
        lines.append(f"Version: {data['version']}")

    paths = data.get("paths", {})
    lines.append("Paths:")
    lines.append(f"  Gemini Home: {paths.get('gemini_home')}")
    lines.append(f"  Customization Root: {paths.get('customization_root')}")
    lines.append(f"  CLI Settings: {paths.get('cli_settings')}")
    lines.append(f"  MCP Config: {paths.get('mcp_config')}")
    lines.append(f"  Rules Root: {paths.get('rules_root')}")
    lines.append(f"  Current Skills: {paths.get('current_skills')}")
    lines.append(f"  Portable Skills Alias: {paths.get('portable_skills_alias')}")
    lines.append(f"  Legacy GEMINI.md: {paths.get('legacy_gemini_md')}")
    lines.append(f"  Legacy Skills: {paths.get('legacy_skills')}")

    checks = data.get("checks", [])
    if checks:
        lines.append("Diagnostics:")
        for c in checks:
            tag = c["status"].upper()
            lines.append(f"  [{tag}] {c['id']}: {c['message']}")
            if c.get("remediation"):
                lines.append(f"         Remediation: {c['remediation']}")
    return "\n".join(lines)


def format_doctor_text(report: dict[str, Any]) -> str:
    """Format doctor report into human-readable text."""
    lines = [
        "=== Personal Tideway Doctor ===",
        f"Coverage: {report.get('coverage', 'Codex + AGY')}",
        f"Overall Status: {report.get('status', 'unknown').upper()}",
        "",
        "Diagnostic Checks:",
    ]
    for c in report.get("checks", []):
        tag = c["status"].upper()
        lines.append(f"  [{tag}] {c['id']}: {c['message']}")
        if c.get("remediation"):
            lines.append(f"         Remediation: {c['remediation']}")
    return "\n".join(lines)
