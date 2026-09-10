"""Git probing, deterministic project resolution, and central auto-registration for Personal Tideway v2.

This module implements read-only Git identity probing, deterministic resolution
against the ProjectRegistry, and safe auto-registration of previously unseen Git roots.
Zero writes inside the source repository; only canonical projects.yaml is updated.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import subprocess
from typing import Any
import urllib.parse
import uuid

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import PROJECT_KIND_DIRECTORY, PROJECT_KIND_GIT
from personal_tideway.core.registry import (
    ProjectRecord,
    ProjectRegistry,
    load_registry,
    now_rfc3339,
    save_registry,
    validate_rfc3339_utc,
    validate_token_format,
    validate_uuid4,
)
from personal_tideway.exceptions import ValidationError


MAX_ERROR_CHARS = 2048


# ---------------------------------------------------------------------------
# Sanitization and Remote URL Normalization
# ---------------------------------------------------------------------------


def sanitize_error_text(text: str, max_chars: int = MAX_ERROR_CHARS) -> str:
    """Sanitize error messages or command output by stripping credentials and truncating."""
    if not text:
        return ""
    # Strip user:password@ or token@ from standard URLs
    cleaned = re.sub(r"://[^/@\s]+:[^/@\s]+@", "://", text)
    cleaned = re.sub(r"://[^/@\s]+@", "://", cleaned)
    # Strip user:password@ from SCP-like syntax
    cleaned = re.sub(r"\b[a-zA-Z0-9_\-\.]+:[^/@\s]+@([a-zA-Z0-9_\-\.]+):", r"\1:", cleaned)
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "... [truncated]"
    return cleaned


def sanitize_remote_url(raw_url: str) -> str:
    """Strip embedded passwords and tokens from remote URLs while preserving overall form."""
    if not raw_url or not isinstance(raw_url, str):
        return ""
    clean = raw_url.strip()
    if "://" in clean:
        try:
            parsed = urllib.parse.urlsplit(clean)
            host_part = parsed.hostname or ""
            if parsed.port:
                host_part = f"{host_part}:{parsed.port}"
            clean = urllib.parse.urlunsplit((parsed.scheme, host_part, parsed.path, "", ""))
        except Exception:
            clean = re.sub(r"://[^/@\s]+@", "://", clean)
    else:
        # SCP-like: discard all userinfo before @, including nonstandard user:password.
        if "@" in clean:
            _userinfo, host_and_path = clean.rsplit("@", 1)
            clean = host_and_path
    return clean


def normalize_git_remote(raw_url: str) -> str | None:
    """Normalize remote identities into credential-free 'host/owner/repo' strings.

    Supports:
    - HTTPS / HTTP: https://user:token@github.com:8443/owner/repo.git -> github.com/owner/repo
    - SSH URLs: ssh://git@github.com/owner/repo.git -> github.com/owner/repo
    - SCP-like: git@github.com:owner/repo.git -> github.com/owner/repo
    - Git protocol: git://github.com/owner/repo.git -> github.com/owner/repo

    Rejects / ignores:
    - Local directory / file paths (e.g., /path/to/repo, ./repo, file:///...)
    - Malformed or empty URLs
    """
    if not raw_url or not isinstance(raw_url, str):
        return None

    clean = raw_url.strip()
    if not clean:
        return None

    # Reject local file paths and Windows drive paths
    if clean.startswith(("/", "./", "../", "~")):
        return None
    if clean.startswith("file://"):
        return None
    if re.match(r"^[a-zA-Z]:[\\/]", clean):
        return None

    host: str = ""
    path_part: str = ""

    if "://" in clean:
        try:
            parsed = urllib.parse.urlsplit(clean)
        except Exception:
            return None

        if parsed.scheme.lower() not in {"https", "http", "ssh", "git"}:
            return None

        raw_hostname = parsed.hostname
        if not raw_hostname:
            return None
        host = raw_hostname.lower().strip()
        path_part = parsed.path
    else:
        # Check for SCP-like format: [userinfo@]host:path.
        host_and_path = clean.rsplit("@", 1)[-1]
        if ":" not in host_and_path:
            return None
        host_part, path_part = host_and_path.split(":", 1)
        if not host_part or "/" in host_part or "\\" in host_part:
            return None

        host_clean = host_part.strip("[]").lower().strip()
        if not host_clean:
            return None
        host = host_clean

    # Strip query and fragment if any
    path_part = path_part.split("?")[0].split("#")[0].strip()

    # Normalize path: strip leading and trailing slashes
    path_part = path_part.lstrip("/")
    path_part = path_part.rstrip("/")

    # Strip trailing .git
    if path_part.endswith(".git"):
        path_part = path_part[:-4].rstrip("/")

    if not host or not path_part:
        return None

    # Reject directory traversal attempts
    normalized_path_segments = [seg for seg in path_part.split("/") if seg]
    if any(seg in {".", ".."} for seg in normalized_path_segments):
        return None

    clean_path = "/".join(normalized_path_segments)
    return f"{host}/{clean_path}"


# ---------------------------------------------------------------------------
# Git Identity Probing
# ---------------------------------------------------------------------------


def default_git_runner(
    cmd: list[str],
    cwd: Path,
    timeout: float = 3.0,
) -> tuple[int, str, str]:
    """Default no-shell subprocess runner with timeout and bounded output."""
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


@dataclass
class GitProbeResult:
    """Structured result of read-only Git repository probing."""
    is_git: bool
    root: Path | None = None
    common_dir: Path | None = None
    remotes: list[str] = field(default_factory=list)
    normalized_remotes: list[str] = field(default_factory=list)
    error: str | None = None
    status: str = "not_git"

    def __post_init__(self) -> None:
        if self.is_git and self.status == "not_git":
            self.status = "git"


def probe_git(
    path: Path | str,
    runner: Callable[[list[str], Path, float], tuple[int, str, str]] | None = None,
    timeout: float = 3.0,
    max_output_chars: int = MAX_ERROR_CHARS,
) -> GitProbeResult:
    """Safely probe Git repository identity without modifying files or exposing credentials.

    Accepts a file or directory path, resolves the working directory, and invokes
    no-shell Git commands to find repo root, common dir, and normalized remote URLs.
    Non-Git paths return is_git=False rather than raising exceptions.
    """
    try:
        candidate = Path(path).expanduser().resolve()
    except Exception as exc:
        return GitProbeResult(
            is_git=False,
            error=f"Git path resolution failed ({type(exc).__name__}).",
            status="probe_error",
        )

    if not candidate.exists():
        return GitProbeResult(
            is_git=False,
            error=sanitize_error_text(f"Path '{candidate}' does not exist.", max_output_chars),
            status="probe_error",
        )

    if candidate.is_file():
        working_dir = candidate.parent
    elif candidate.is_dir():
        working_dir = candidate
    else:
        return GitProbeResult(
            is_git=False,
            error=sanitize_error_text(f"Path '{candidate}' is not a regular file or directory.", max_output_chars),
            status="probe_error",
        )

    run_cmd = runner or default_git_runner

    # 1. Probe repository root and common dir
    try:
        ret, stdout, stderr = run_cmd(
            ["git", "rev-parse", "--show-toplevel", "--git-common-dir"],
            working_dir,
            timeout,
        )
    except subprocess.TimeoutExpired:
        return GitProbeResult(is_git=False, error="Git probe timed out.", status="probe_error")
    except Exception as exc:
        return GitProbeResult(is_git=False, error=f"Git probe failed ({type(exc).__name__}).", status="probe_error")

    if ret != 0:
        is_standard_not_git = "not a git repository" in (stderr or "").lower()
        if is_standard_not_git:
            return GitProbeResult(
                is_git=False,
                error="Path is not a git repository.",
                status="not_git",
            )
        return GitProbeResult(
            is_git=False,
            error="Git probe failed.",
            status="probe_error",
        )

    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return GitProbeResult(is_git=False, error="Empty output from git rev-parse.", status="probe_error")

    raw_root = lines[0]
    root_path = Path(raw_root).resolve()

    raw_common_dir = lines[1] if len(lines) > 1 else str(root_path / ".git")
    common_dir_obj = Path(raw_common_dir)
    if common_dir_obj.is_absolute():
        common_dir_path = common_dir_obj.resolve()
    else:
        common_dir_path = (working_dir / common_dir_obj).resolve()

    # 2. Probe configured remote URLs
    remote_urls: list[str] = []
    try:
        ret_rem, stdout_rem, _ = run_cmd(
            ["git", "config", "--get-regexp", r"^remote\..*\.url$"],
            working_dir,
            timeout,
        )
        if ret_rem == 0 and stdout_rem:
            for line in stdout_rem.splitlines():
                line_clean = line.strip()
                if not line_clean:
                    continue
                parts = line_clean.split(None, 1)
                if len(parts) == 2:
                    remote_urls.append(parts[1].strip())
    except subprocess.TimeoutExpired:
        # Non-fatal for remote discovery
        pass
    except Exception:
        pass

    sanitized_remotes: list[str] = []
    normalized_remotes: list[str] = []

    for raw_rem in remote_urls:
        s_rem = sanitize_remote_url(raw_rem)
        if s_rem and s_rem not in sanitized_remotes:
            sanitized_remotes.append(s_rem)
        n_rem = normalize_git_remote(raw_rem)
        if n_rem and n_rem not in normalized_remotes:
            normalized_remotes.append(n_rem)

    return GitProbeResult(
        is_git=True,
        root=root_path,
        common_dir=common_dir_path,
        remotes=sanitized_remotes,
        normalized_remotes=normalized_remotes,
        error=None,
        status="git",
    )

# ---------------------------------------------------------------------------
# Project Resolution
# ---------------------------------------------------------------------------


@dataclass
class ProposedBindingChanges:
    """Structured collection of proposed binding additions or removals."""
    paths: list[str] = field(default_factory=list)
    git_common_dirs: list[str] = field(default_factory=list)
    git_remotes: list[str] = field(default_factory=list)

    def __getitem__(self, key: str) -> list[str]:
        if key == "paths":
            return self.paths
        if key == "git_common_dirs":
            return self.git_common_dirs
        if key == "git_remotes":
            return self.git_remotes
        raise KeyError(key)

    def __setitem__(self, key: str, value: list[str]) -> None:
        if key == "paths":
            self.paths = list(value)
        elif key == "git_common_dirs":
            self.git_common_dirs = list(value)
        elif key == "git_remotes":
            self.git_remotes = list(value)
        else:
            raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "paths": list(self.paths),
            "git_common_dirs": list(self.git_common_dirs),
            "git_remotes": list(self.git_remotes),
        }

    def is_empty(self) -> bool:
        return not self.paths and not self.git_common_dirs and not self.git_remotes


@dataclass
class ProjectResolutionResult:
    """Structured result of deterministic project resolution."""
    status: str  # "resolved" | "candidate" | "ambiguous" | "unresolved"
    project_id: str | None = None
    project: ProjectRecord | None = None
    evidence: list[str] = field(default_factory=list)
    requires_confirmation: bool = False
    candidate_metadata: dict[str, Any] | None = None
    ambiguous_project_ids: list[str] = field(default_factory=list)
    proposed_record: ProjectRecord | None = None
    bindings_changed: bool = False
    proposed_additions: ProposedBindingChanges = field(default_factory=ProposedBindingChanges)
    proposed_removals: ProposedBindingChanges = field(default_factory=ProposedBindingChanges)

    def to_dict(self) -> dict[str, Any]:
        """Convert resolution result to structured dictionary matching v2 schema."""
        data: dict[str, Any] = {
            "status": self.status,
            "project_id": self.project_id,
            "evidence": list(self.evidence),
            "requires_confirmation": self.requires_confirmation,
        }
        if self.candidate_metadata is not None:
            data["candidate_metadata"] = dict(self.candidate_metadata)
        if self.ambiguous_project_ids:
            data["ambiguous_project_ids"] = list(self.ambiguous_project_ids)
        if self.bindings_changed or not self.proposed_additions.is_empty() or not self.proposed_removals.is_empty():
            data["bindings_changed"] = self.bindings_changed
            data["proposed_additions"] = self.proposed_additions.to_dict()
            data["proposed_removals"] = self.proposed_removals.to_dict()
        return data


@dataclass
class BindingReconciliationResult(ProjectResolutionResult):
    """Structured result of Git binding reconciliation."""
    bindings_changed: bool = False
    proposed_additions: ProposedBindingChanges = field(default_factory=ProposedBindingChanges)
    proposed_removals: ProposedBindingChanges = field(default_factory=ProposedBindingChanges)

    def to_dict(self) -> dict[str, Any]:
        """Convert reconciliation result to structured dictionary without secret leakage."""
        data: dict[str, Any] = {
            "status": self.status,
            "project_id": self.project_id,
            "evidence": list(self.evidence),
            "requires_confirmation": self.requires_confirmation,
            "bindings_changed": self.bindings_changed,
            "proposed_additions": self.proposed_additions.to_dict(),
            "proposed_removals": self.proposed_removals.to_dict(),
        }
        if self.candidate_metadata is not None:
            data["candidate_metadata"] = dict(self.candidate_metadata)
        if self.ambiguous_project_ids:
            data["ambiguous_project_ids"] = list(self.ambiguous_project_ids)
        return data



def derive_safe_slug(name: str) -> str:
    """Derive a valid token format slug from directory basename."""
    clean = name.strip().lower()
    # Use portable lowercase kebab-case for automatically generated slugs.
    slug = re.sub(r"[^a-z0-9]+", "-", clean)
    slug = re.sub(r"-+", "-", slug)
    slug = slug.strip("-")
    return slug or "project"


def is_token_available(token: str, registry: ProjectRegistry) -> bool:
    """Check whether a token (slug, id, alias) is completely unassigned in registry."""
    target = token.strip().lower()
    for p in registry.projects:
        if p.id.lower() == target or p.slug.lower() == target:
            return False
        if any(a.lower() == target for a in p.aliases):
            return False
    return True


def resolve_project(
    registry: ProjectRegistry,
    path: Path | str | None = None,
    explicit_project: str | None = None,
    probe: GitProbeResult | None = None,
    git_runner: Callable[[list[str], Path, float], tuple[int, str, str]] | None = None,
    timeout: float = 3.0,
) -> ProjectResolutionResult:
    """Deterministically resolve a project following the v2 resolution precedence.

    Order:
    1. Explicit ID/slug/alias.
    2. Exact registered canonical path.
    3. Longest registered ancestor path.
    4. Git common directory identity.
    5. Normalized Git remote identity (only when exactly one project matches).
    6. Unregistered Git candidate.
    7. Unresolved.
    """
    # Tier 1: Explicit project specification
    if explicit_project is not None and explicit_project.strip():
        explicit_target = explicit_project.strip()
        matched = registry.get(explicit_target)
        if matched is not None:
            return ProjectResolutionResult(
                status="resolved",
                project_id=matched.id,
                project=matched,
                evidence=["explicit"],
                requires_confirmation=False,
            )
        return ProjectResolutionResult(
            status="unresolved",
            project_id=None,
            evidence=["explicit"],
            requires_confirmation=False,
        )

    target_path = Path(path).expanduser().resolve() if path is not None else Path.cwd().resolve()
    target_norm = os.path.normpath(str(target_path))

    # Tier 2: Exact registered canonical path
    for p in registry.projects:
        for bound_path in p.bindings.paths:
            if os.path.normpath(bound_path) == target_norm:
                return ProjectResolutionResult(
                    status="resolved",
                    project_id=p.id,
                    project=p,
                    evidence=["path"],
                    requires_confirmation=False,
                )

    # Tier 3: Longest registered ancestor path
    best_ancestor_project: ProjectRecord | None = None
    best_ancestor_length: int = -1

    for p in registry.projects:
        for bound_path in p.bindings.paths:
            bound_obj = Path(bound_path).resolve()
            if target_path.is_relative_to(bound_obj) and target_path != bound_obj:
                depth = len(bound_obj.parts)
                if depth > best_ancestor_length:
                    best_ancestor_length = depth
                    best_ancestor_project = p

    if best_ancestor_project is not None:
        return ProjectResolutionResult(
            status="resolved",
            project_id=best_ancestor_project.id,
            project=best_ancestor_project,
            evidence=["path"],
            requires_confirmation=False,
        )

    # Probe Git if probe not provided
    git_probe = probe if probe is not None else probe_git(target_path, runner=git_runner, timeout=timeout)

    if not git_probe.is_git:
        return ProjectResolutionResult(
            status="unresolved",
            project_id=None,
            evidence=[],
            requires_confirmation=False,
        )

    # Tier 4: Git common directory identity
    if git_probe.common_dir is not None:
        probe_gcd_norm = os.path.normpath(str(git_probe.common_dir))
        for p in registry.projects:
            for bound_gcd in p.bindings.git_common_dirs:
                if os.path.normpath(bound_gcd) == probe_gcd_norm:
                    return ProjectResolutionResult(
                        status="resolved",
                        project_id=p.id,
                        project=p,
                        evidence=["git-common-dir"],
                        requires_confirmation=False,
                    )

    # Tier 5: Normalized Git remote identity (unambiguous only)
    if git_probe.normalized_remotes:
        remote_matches: list[ProjectRecord] = []
        for p in registry.projects:
            p_remotes = {normalize_git_remote(r) or r for r in p.bindings.git_remotes}
            if any(r in p_remotes for r in git_probe.normalized_remotes):
                remote_matches.append(p)

        if len(remote_matches) == 1:
            matched_project = remote_matches[0]
            return ProjectResolutionResult(
                status="resolved",
                project_id=matched_project.id,
                project=matched_project,
                evidence=["git-remote"],
                requires_confirmation=False,
            )
        elif len(remote_matches) > 1:
            return ProjectResolutionResult(
                status="ambiguous",
                project_id=None,
                evidence=["git-remote"],
                requires_confirmation=True,
                ambiguous_project_ids=[p.id for p in remote_matches],
            )

    # Tier 6: Unregistered Git root candidate
    assert git_probe.root is not None
    base_slug = derive_safe_slug(git_probe.root.name)
    candidate_metadata: dict[str, Any] = {
        "root": str(git_probe.root),
        "common_dir": str(git_probe.common_dir) if git_probe.common_dir else None,
        "remotes": list(git_probe.normalized_remotes),
        "suggested_slug": base_slug,
        "suggested_name": git_probe.root.name,
    }

    return ProjectResolutionResult(
        status="candidate",
        project_id=None,
        evidence=["git-root"],
        requires_confirmation=True,
        candidate_metadata=candidate_metadata,
    )


# ---------------------------------------------------------------------------
# Auto-Registration
# ---------------------------------------------------------------------------


def resolve_or_register_git(
    path: Path | str | None = None,
    cfg: PersonalTidewayConfig | None = None,
    registry: ProjectRegistry | None = None,
    auto_register_git: bool | None = None,
    dry_run: bool = False,
    explicit_project: str | None = None,
    id_factory: Callable[[], str] | None = None,
    git_runner: Callable[[list[str], Path, float], tuple[int, str, str]] | None = None,
    timeout: float = 3.0,
    reconcile_bindings: bool = False,
    clock: Callable[[], str] | None = None,
    probe: GitProbeResult | None = None,
    display_name: str | None = None,
) -> ProjectResolutionResult:
    """Resolve a project or auto-register a previously unseen Git root.

    Behavior:
    - If already resolved (Tier 1-5): returns existing project without duplicate creation.
    - If ambiguous: returns ambiguous result and writes nothing.
    - If unresolved (non-Git): returns unresolved and writes nothing.
    - If candidate:
      - When auto_register_git=True and dry_run=False: creates ProjectRecord,
        adds to registry, atomically saves cfg.projects_yaml, returns resolved result.
      - When auto_register_git=False: returns candidate with requires_confirmation=True.
      - When dry_run=True: returns candidate with proposed_record and writes nothing.
    - All writes are strictly confined to PERSONAL_TIDEWAY_HOME. The source repository
      is never modified or polluted.
    """
    eff_display_name: str | None = None
    if display_name is not None:
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValidationError("Field 'display_name' must be a non-empty string when provided.")
        eff_display_name = display_name.strip()

    active_cfg = cfg or PersonalTidewayConfig.resolve()
    active_registry = registry if registry is not None else load_registry(active_cfg.projects_yaml)

    if auto_register_git is not None:
        effective_auto_register = bool(auto_register_git)
    elif hasattr(active_cfg, "auto_register_git"):
        effective_auto_register = bool(active_cfg.auto_register_git)
    else:
        effective_auto_register = True

    # Resolve explicit and path-based matches before invoking Git at all.
    target_path = Path(path).expanduser().resolve() if path is not None else Path.cwd().resolve()
    pre_resolution = resolve_project(
        registry=active_registry,
        path=target_path,
        explicit_project=explicit_project,
        probe=GitProbeResult(is_git=False),
    )
    if pre_resolution.status == "resolved" or (explicit_project is not None and explicit_project.strip()):
        return pre_resolution

    # Perform the Git probe once so later tiers can reuse it.
    git_probe = probe if probe is not None else probe_git(target_path, runner=git_runner, timeout=timeout)

    if reconcile_bindings:
        recon_service = GitBindingReconciliationService(
            cfg=active_cfg,
            registry=active_registry,
            git_runner=git_runner,
            timeout=timeout,
            clock=clock,
        )
        recon_res = recon_service.reconcile(
            path=target_path,
            dry_run=dry_run,
            explicit_project=explicit_project,
            probe=git_probe,
        )
        if recon_res.status != "candidate":
            return recon_res

    resolution = resolve_project(
        registry=active_registry,
        path=target_path,
        explicit_project=explicit_project,
        probe=git_probe,
        git_runner=git_runner,
        timeout=timeout,
    )

    if resolution.status != "candidate":
        return resolution

    # Candidate found: create proposed record
    new_id = (id_factory or (lambda: str(uuid.uuid4())))()
    root_path = git_probe.root
    assert root_path is not None

    base_slug = derive_safe_slug(root_path.name)
    if is_token_available(base_slug, active_registry):
        final_slug = base_slug
    else:
        # Deterministic collision resolution using ID short suffix
        short_suffix = new_id[:8]
        final_slug = f"{base_slug}-{short_suffix}"
        if not is_token_available(final_slug, active_registry):
            final_slug = f"{base_slug}-{new_id.replace('-', '')[:12]}"
        if not is_token_available(final_slug, active_registry):
            final_slug = f"{base_slug}-{new_id.replace('-', '')}"
        if not is_token_available(final_slug, active_registry):
            raise ValidationError("Unable to derive a unique project slug from the generated project ID.")

    common_dir_str = str(git_probe.common_dir) if git_probe.common_dir else str(root_path / ".git")

    record = ProjectRecord.create(
        project_id=new_id,
        slug=final_slug,
        display_name=eff_display_name if eff_display_name is not None else root_path.name,
        kind=PROJECT_KIND_GIT,
        paths=[str(root_path)],
        git_common_dirs=[common_dir_str],
        git_remotes=git_probe.normalized_remotes,
    )

    if not effective_auto_register:
        resolution.proposed_record = record
        resolution.requires_confirmation = True
        return resolution

    if dry_run:
        resolution.proposed_record = record
        resolution.requires_confirmation = True
        if resolution.candidate_metadata is not None:
            resolution.candidate_metadata["proposed_id"] = record.id
            resolution.candidate_metadata["proposed_slug"] = record.slug
            resolution.candidate_metadata["proposed_display_name"] = record.display_name
        return resolution

    # Validate and persist a copy first so a failed write cannot mutate caller state.
    persisted_registry = ProjectRegistry(
        version=active_registry.version,
        projects=[*active_registry.projects, record],
    )
    persisted_registry.validate()
    save_registry(persisted_registry, active_cfg)
    if registry is not None:
        active_registry.add_project(record)

    return ProjectResolutionResult(
        status="resolved",
        project_id=record.id,
        project=record,
        evidence=["auto-registered"],
        requires_confirmation=False,
    )


# ---------------------------------------------------------------------------
# Git Binding Reconciliation
# ---------------------------------------------------------------------------


class GitBindingReconciliationService:
    """Service that probes once, resolves deterministically, and reconciles Git bindings."""

    def __init__(
        self,
        cfg: PersonalTidewayConfig | None = None,
        registry: ProjectRegistry | None = None,
        git_runner: Callable[[list[str], Path, float], tuple[int, str, str]] | None = None,
        timeout: float = 3.0,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.registry = registry
        self.git_runner = git_runner
        self.timeout = timeout
        self.clock = clock

    @staticmethod
    def _create_updated_registry_copy(
        registry: ProjectRegistry,
        updated_project: ProjectRecord,
    ) -> ProjectRegistry:
        """Create a detached copy of registry containing the updated project record."""
        new_projects = []
        for p in registry.projects:
            if p.id.lower() == updated_project.id.lower():
                new_projects.append(updated_project)
            else:
                new_projects.append(p)
        return ProjectRegistry(
            version=registry.version,
            projects=new_projects,
        )

    @staticmethod
    def _path_exists_with_certainty(path: str) -> bool | None:
        """Return True/False for known state and None when access errors make it uncertain."""
        try:
            Path(path).stat()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return None

    def reconcile(
        self,
        path: Path | str | None = None,
        dry_run: bool = False,
        explicit_project: str | None = None,
        probe: GitProbeResult | None = None,
    ) -> BindingReconciliationResult:
        """Probe Git once, resolve deterministically, and reconcile project bindings.

        Policies:
        - existing exact/ancestor path: no change;
        - same git-common-dir + new worktree root: add normalized root path, preserve existing roots/common-dir;
        - remote-only obvious move: add new root/common-dir and remove only stale non-existing old root/common-dir bindings;
        - remote-only additional live clone: no write, proposal requiring confirmation;
        - dry-run: return exact proposal, zero mutation/write;
        - repeated calls are byte-idempotent.
        """
        active_cfg = self.cfg or PersonalTidewayConfig.resolve()
        active_registry = (
            self.registry
            if self.registry is not None
            else load_registry(active_cfg.projects_yaml)
        )
        utc_clock = self.clock or now_rfc3339

        try:
            target_path = (
                Path(path).expanduser().resolve()
                if path is not None
                else Path.cwd().resolve()
            )
        except Exception:
            return BindingReconciliationResult(
                status="unresolved",
                project_id=None,
                evidence=[],
                requires_confirmation=False,
                bindings_changed=False,
            )

        if not target_path.exists():
            return BindingReconciliationResult(
                status="unresolved",
                project_id=None,
                evidence=[],
                requires_confirmation=False,
                bindings_changed=False,
            )

        matched_explicit: ProjectRecord | None = None
        if explicit_project is not None and explicit_project.strip():
            matched_explicit = active_registry.get(explicit_project.strip())
            if matched_explicit is None:
                return BindingReconciliationResult(
                    status="unresolved",
                    project_id=None,
                    evidence=["explicit"],
                    requires_confirmation=False,
                    bindings_changed=False,
                )
        projects_to_consider = [matched_explicit] if matched_explicit is not None else active_registry.projects

        # Single Git probe
        git_probe = (
            probe
            if probe is not None
            else probe_git(target_path, runner=self.git_runner, timeout=self.timeout)
        )

        if not git_probe.is_git or git_probe.root is None:
            return BindingReconciliationResult(
                status="unresolved",
                project_id=None,
                evidence=[],
                requires_confirmation=False,
                bindings_changed=False,
            )

        root_norm = os.path.normpath(str(git_probe.root.resolve()))
        target_norm = os.path.normpath(str(target_path))
        gcd_norm = (
            os.path.normpath(str(git_probe.common_dir.resolve()))
            if git_probe.common_dir is not None
            else os.path.normpath(str((git_probe.root / ".git").resolve()))
        )

        # 1. Exact registered canonical path or ancestor path: no change
        for p in projects_to_consider:
            bound_paths_norm = [os.path.normpath(b) for b in p.bindings.paths]
            if root_norm in bound_paths_norm or target_norm in bound_paths_norm:
                return BindingReconciliationResult(
                    status="resolved",
                    project_id=p.id,
                    project=p,
                    evidence=["path"],
                    requires_confirmation=False,
                    bindings_changed=False,
                    proposed_additions=ProposedBindingChanges(),
                    proposed_removals=ProposedBindingChanges(),
                )

        best_ancestor_project: ProjectRecord | None = None
        best_ancestor_length = -1
        for p in projects_to_consider:
            for bound_path in p.bindings.paths:
                bound_obj = Path(bound_path).resolve()
                if target_path.is_relative_to(bound_obj) and target_path != bound_obj:
                    depth = len(bound_obj.parts)
                    if depth > best_ancestor_length:
                        best_ancestor_length = depth
                        best_ancestor_project = p

        if best_ancestor_project is not None:
            return BindingReconciliationResult(
                status="resolved",
                project_id=best_ancestor_project.id,
                project=best_ancestor_project,
                evidence=["path"],
                requires_confirmation=False,
                bindings_changed=False,
                proposed_additions=ProposedBindingChanges(),
                proposed_removals=ProposedBindingChanges(),
            )

        # 2. Same git common-dir + new worktree root: add normalized root path, preserve existing roots/common-dir
        common_dir_match: ProjectRecord | None = None
        for p in projects_to_consider:
            bound_gcds_norm = [os.path.normpath(gcd) for gcd in p.bindings.git_common_dirs]
            if gcd_norm in bound_gcds_norm:
                common_dir_match = p
                break

        if common_dir_match is not None:
            # New worktree root of known repo
            known_remotes = {
                normalize_git_remote(remote) or remote
                for remote in common_dir_match.bindings.git_remotes
            }
            missing_remotes = [
                remote for remote in git_probe.normalized_remotes if remote not in known_remotes
            ]
            proposed_additions = ProposedBindingChanges(
                paths=[root_norm],
                git_remotes=missing_remotes,
            )
            proposed_removals = ProposedBindingChanges()

            if dry_run:
                return BindingReconciliationResult(
                    status="resolved",
                    project_id=common_dir_match.id,
                    project=common_dir_match,
                    evidence=["git-common-dir"],
                    requires_confirmation=False,
                    bindings_changed=False,
                    proposed_additions=proposed_additions,
                    proposed_removals=proposed_removals,
                )

            new_paths = list(common_dir_match.bindings.paths)
            if root_norm not in [os.path.normpath(p) for p in new_paths]:
                new_paths.append(root_norm)

            new_updated_at = validate_rfc3339_utc(utc_clock(), "updated_at")
            updated_record = common_dir_match.with_bindings(
                paths=new_paths,
                git_remotes=[*common_dir_match.bindings.git_remotes, *missing_remotes],
                updated_at=new_updated_at,
            )
            persisted_registry = self._create_updated_registry_copy(
                active_registry, updated_record
            )
            persisted_registry.validate()
            save_registry(persisted_registry, active_cfg)

            # Successfully persisted -> update in-memory registry state
            common_dir_match.bindings.paths = new_paths
            common_dir_match.bindings.git_remotes = [
                *common_dir_match.bindings.git_remotes,
                *missing_remotes,
            ]
            common_dir_match.updated_at = new_updated_at
            active_registry.update_project(common_dir_match)

            return BindingReconciliationResult(
                status="resolved",
                project_id=common_dir_match.id,
                project=common_dir_match,
                evidence=["git-common-dir"],
                requires_confirmation=False,
                bindings_changed=True,
                proposed_additions=proposed_additions,
                proposed_removals=proposed_removals,
            )

        # 3. Normalized Git remote identity (only when exactly one project matches)
        if git_probe.normalized_remotes:
            remote_matches: list[ProjectRecord] = []
            for p in projects_to_consider:
                p_remotes = {normalize_git_remote(r) or r for r in p.bindings.git_remotes}
                if any(r in p_remotes for r in git_probe.normalized_remotes):
                    remote_matches.append(p)

            if len(remote_matches) > 1:
                return BindingReconciliationResult(
                    status="ambiguous",
                    project_id=None,
                    evidence=["git-remote"],
                    requires_confirmation=True,
                    ambiguous_project_ids=[p.id for p in remote_matches],
                    bindings_changed=False,
                    proposed_additions=ProposedBindingChanges(),
                    proposed_removals=ProposedBindingChanges(),
                )

            if len(remote_matches) == 1:
                matched_project = remote_matches[0]
                # Check whether any old bound path or common-dir still exists on filesystem
                path_states = {
                    path: self._path_exists_with_certainty(path)
                    for path in matched_project.bindings.paths
                }
                gcd_states = {
                    path: self._path_exists_with_certainty(path)
                    for path in matched_project.bindings.git_common_dirs
                }
                any_old_alive_or_unknown = any(
                    state is not False for state in [*path_states.values(), *gcd_states.values()]
                )
                known_remotes = {
                    normalize_git_remote(remote) or remote
                    for remote in matched_project.bindings.git_remotes
                }
                missing_remotes = [
                    remote for remote in git_probe.normalized_remotes if remote not in known_remotes
                ]

                if any_old_alive_or_unknown:
                    # Additional live clone: resolve project, do not mutate bindings, proposal requiring confirmation
                    add_gcds = (
                        [gcd_norm]
                        if gcd_norm not in [os.path.normpath(g) for g in matched_project.bindings.git_common_dirs]
                        else []
                    )
                    proposed_additions = ProposedBindingChanges(
                        paths=[root_norm],
                        git_common_dirs=add_gcds,
                        git_remotes=missing_remotes,
                    )
                    return BindingReconciliationResult(
                        status="resolved",
                        project_id=matched_project.id,
                        project=matched_project,
                        evidence=["git-remote"],
                        requires_confirmation=True,
                        bindings_changed=False,
                        proposed_additions=proposed_additions,
                        proposed_removals=ProposedBindingChanges(),
                    )

                # Obvious move: all old bound paths and common-dirs missing
                stale_paths = [path for path, state in path_states.items() if state is False]
                stale_gcds = [path for path, state in gcd_states.items() if state is False]
                proposed_additions = ProposedBindingChanges(
                    paths=[root_norm],
                    git_common_dirs=[gcd_norm],
                    git_remotes=missing_remotes,
                )
                proposed_removals = ProposedBindingChanges(
                    paths=stale_paths,
                    git_common_dirs=stale_gcds,
                )

                if dry_run:
                    return BindingReconciliationResult(
                        status="resolved",
                        project_id=matched_project.id,
                        project=matched_project,
                        evidence=["git-remote"],
                        requires_confirmation=False,
                        bindings_changed=False,
                        proposed_additions=proposed_additions,
                        proposed_removals=proposed_removals,
                    )

                new_paths = [p for p in matched_project.bindings.paths if p not in stale_paths]
                if root_norm not in [os.path.normpath(p) for p in new_paths]:
                    new_paths.append(root_norm)

                new_gcds = [gcd for gcd in matched_project.bindings.git_common_dirs if gcd not in stale_gcds]
                if gcd_norm not in [os.path.normpath(g) for g in new_gcds]:
                    new_gcds.append(gcd_norm)

                new_updated_at = validate_rfc3339_utc(utc_clock(), "updated_at")
                updated_record = matched_project.with_bindings(
                    paths=new_paths,
                    git_common_dirs=new_gcds,
                    git_remotes=[*matched_project.bindings.git_remotes, *missing_remotes],
                    updated_at=new_updated_at,
                )
                persisted_registry = self._create_updated_registry_copy(
                    active_registry, updated_record
                )
                persisted_registry.validate()
                save_registry(persisted_registry, active_cfg)

                # Successfully persisted -> update in-memory registry state
                matched_project.bindings.paths = new_paths
                matched_project.bindings.git_common_dirs = new_gcds
                matched_project.bindings.git_remotes = [
                    *matched_project.bindings.git_remotes,
                    *missing_remotes,
                ]
                matched_project.updated_at = new_updated_at
                active_registry.update_project(matched_project)

                return BindingReconciliationResult(
                    status="resolved",
                    project_id=matched_project.id,
                    project=matched_project,
                    evidence=["git-remote"],
                    requires_confirmation=False,
                    bindings_changed=True,
                    proposed_additions=proposed_additions,
                    proposed_removals=proposed_removals,
                )

        if matched_explicit is not None:
            return BindingReconciliationResult(
                status="resolved",
                project_id=matched_explicit.id,
                project=matched_explicit,
                evidence=["explicit"],
                requires_confirmation=True,
                bindings_changed=False,
                proposed_additions=ProposedBindingChanges(
                    paths=[root_norm],
                    git_common_dirs=[gcd_norm],
                    git_remotes=list(git_probe.normalized_remotes),
                ),
                proposed_removals=ProposedBindingChanges(),
            )

        # 4. Unregistered Git root candidate
        base_slug = derive_safe_slug(git_probe.root.name)
        candidate_metadata: dict[str, Any] = {
            "root": str(git_probe.root),
            "common_dir": str(git_probe.common_dir) if git_probe.common_dir else None,
            "remotes": list(git_probe.normalized_remotes),
            "suggested_slug": base_slug,
            "suggested_name": git_probe.root.name,
        }
        return BindingReconciliationResult(
            status="candidate",
            project_id=None,
            evidence=["git-root"],
            requires_confirmation=True,
            bindings_changed=False,
            candidate_metadata=candidate_metadata,
            proposed_additions=ProposedBindingChanges(),
            proposed_removals=ProposedBindingChanges(),
        )


def reconcile_git_bindings(
    path: Path | str | None = None,
    cfg: PersonalTidewayConfig | None = None,
    registry: ProjectRegistry | None = None,
    dry_run: bool = False,
    explicit_project: str | None = None,
    git_runner: Callable[[list[str], Path, float], tuple[int, str, str]] | None = None,
    timeout: float = 3.0,
    clock: Callable[[], str] | None = None,
    probe: GitProbeResult | None = None,
) -> BindingReconciliationResult:
    """Convenience function to probe once, resolve deterministically, and reconcile Git bindings."""
    service = GitBindingReconciliationService(
        cfg=cfg,
        registry=registry,
        git_runner=git_runner,
        timeout=timeout,
        clock=clock,
    )
    return service.reconcile(
        path=path,
        dry_run=dry_run,
        explicit_project=explicit_project,
        probe=probe,
    )


# ---------------------------------------------------------------------------
# Directory Registration
# ---------------------------------------------------------------------------


@dataclass
class DirectoryRegistrationResult(ProjectResolutionResult):
    """Structured result of explicit directory registration."""
    created: bool = False
    action: str = ""

    def __init__(
        self,
        status: str,
        project_id: str | None = None,
        project: ProjectRecord | None = None,
        record: ProjectRecord | None = None,
        proposed_record: ProjectRecord | None = None,
        proposal: ProjectRecord | None = None,
        evidence: list[str] | None = None,
        created: bool = False,
        requires_confirmation: bool = False,
        action: str = "",
        candidate_metadata: dict[str, Any] | None = None,
        ambiguous_project_ids: list[str] | None = None,
        bindings_changed: bool = False,
        proposed_additions: ProposedBindingChanges | None = None,
        proposed_removals: ProposedBindingChanges | None = None,
    ) -> None:
        eff_project = record if record is not None else project
        eff_proposal = proposal if proposal is not None else proposed_record
        super().__init__(
            status=status,
            project_id=project_id,
            project=eff_project,
            evidence=evidence or [],
            requires_confirmation=requires_confirmation,
            candidate_metadata=candidate_metadata,
            ambiguous_project_ids=ambiguous_project_ids or [],
            proposed_record=eff_proposal,
            bindings_changed=bindings_changed,
            proposed_additions=proposed_additions or ProposedBindingChanges(),
            proposed_removals=proposed_removals or ProposedBindingChanges(),
        )
        self.created = created
        self.action = action

    @property
    def record(self) -> ProjectRecord | None:
        return self.project

    @record.setter
    def record(self, val: ProjectRecord | None) -> None:
        self.project = val

    @property
    def proposal(self) -> ProjectRecord | None:
        return self.proposed_record

    @proposal.setter
    def proposal(self, val: ProjectRecord | None) -> None:
        self.proposed_record = val

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["created"] = self.created
        if self.action:
            data["action"] = self.action
        if self.record is not None:
            data["record"] = self.record.to_dict()
        if self.proposed_record is not None:
            data["proposed_record"] = self.proposed_record.to_dict()
        return data


def register_directory(
    path: Path | str,
    *,
    cfg: PersonalTidewayConfig | None = None,
    registry: ProjectRegistry | None = None,
    display_name: str | None = None,
    slug: str | None = None,
    aliases: list[str] | None = None,
    id_factory: Callable[[], str] | None = None,
    clock: Callable[[], str] | None = None,
    probe: GitProbeResult | None = None,
    git_runner: Callable[[list[str], Path, float], tuple[int, str, str]] | None = None,
    timeout: float = 3.0,
    dry_run: bool = False,
) -> DirectoryRegistrationResult:
    """Explicitly register an ordinary local non-Git directory in Personal Tideway v2.

    Enforces policies:
    - Ordinary directories are never silently registered.
    - Zero modification to files below the registered directory; only cfg.projects_yaml is written.
    - Paths inside Git repositories are rejected with clear ValidationError directing to Git registration.
    - Exact already-registered directory is idempotent (returns existing project, created=False).
    - Path covered by a registered ancestor resolves to that ancestor project (created=False).
    - Rejects non-existent paths, files/symlinks-to-files, PTW home itself and its descendants.
    - Resolves token collisions deterministically with the ID suffix.
    - Persistence is transactional: failed save leaves supplied in-memory registry unchanged.
    """
    if path is None or (isinstance(path, str) and not path.strip()):
        raise ValidationError("Path must be a non-empty directory path.")

    active_cfg = cfg or PersonalTidewayConfig.resolve()
    active_registry = registry if registry is not None else load_registry(active_cfg.projects_yaml)

    candidate = Path(path).expanduser()
    if not candidate.exists():
        raise ValidationError(f"Directory '{candidate}' does not exist.")

    if not candidate.is_dir():
        raise ValidationError(f"Path '{candidate}' is not a directory.")

    target_path = candidate.resolve()
    if not target_path.is_dir():
        raise ValidationError(f"Resolved path '{target_path}' is not a directory.")

    ptw_home = active_cfg.home.resolve()
    if target_path == ptw_home or target_path.is_relative_to(ptw_home):
        raise ValidationError(
            f"Cannot register Personal Tideway home ('{ptw_home}') or any of its descendants as a directory project: '{target_path}'."
        )

    target_norm = os.path.normpath(str(target_path))

    # Tier 2 check: Exact registered canonical path (idempotency)
    for p in active_registry.projects:
        for bound_path in p.bindings.paths:
            if os.path.normpath(bound_path) == target_norm:
                return DirectoryRegistrationResult(
                    status="resolved",
                    project_id=p.id,
                    project=p,
                    evidence=["path"],
                    created=False,
                    requires_confirmation=False,
                    action="noop_exact",
                )

    # Tier 3 check: Longest registered ancestor path (avoid nested duplicates)
    best_ancestor_project: ProjectRecord | None = None
    best_ancestor_length: int = -1

    for p in active_registry.projects:
        for bound_path in p.bindings.paths:
            bound_obj = Path(bound_path).resolve()
            if target_path.is_relative_to(bound_obj) and target_path != bound_obj:
                depth = len(bound_obj.parts)
                if depth > best_ancestor_length:
                    best_ancestor_length = depth
                    best_ancestor_project = p

    if best_ancestor_project is not None:
        return DirectoryRegistrationResult(
            status="resolved",
            project_id=best_ancestor_project.id,
            project=best_ancestor_project,
            evidence=["path"],
            created=False,
            requires_confirmation=False,
            action="noop_ancestor",
        )

    # Git probe
    git_probe = probe if probe is not None else probe_git(target_path, runner=git_runner, timeout=timeout)

    if git_probe.is_git:
        repo_location = git_probe.root or target_path
        raise ValidationError(
            f"Path '{target_path}' is inside a Git repository ('{repo_location}'). "
            f"Directory registration rejected. Use Git project registration instead."
        )

    if git_probe.status != "not_git":
        err_msg = git_probe.error or "status uncertain"
        raise ValidationError(
            f"Git probe failed or status uncertain for '{target_path}': {err_msg}. "
            f"Directory cannot be registered without confirmed non-Git status."
        )

    # Generate or validate project ID
    new_id = (id_factory or (lambda: str(uuid.uuid4())))()
    new_id = validate_uuid4(new_id, "project_id")

    # Display name
    if display_name is not None:
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValidationError(
                f"Field 'display_name' must be a non-empty string when provided, got {display_name!r}."
            )
        eff_display_name = display_name.strip()
    else:
        eff_display_name = target_path.name

    # Slug derivation and collision resolution
    if slug is not None:
        clean_slug = validate_token_format(slug, "slug")
        if not is_token_available(clean_slug, active_registry):
            raise ValidationError(f"Project slug '{clean_slug}' is already in use in registry.")
        final_slug = clean_slug
    else:
        base_slug = derive_safe_slug(target_path.name)
        if is_token_available(base_slug, active_registry):
            final_slug = base_slug
        else:
            short_suffix = new_id[:8]
            final_slug = f"{base_slug}-{short_suffix}"
            if not is_token_available(final_slug, active_registry):
                final_slug = f"{base_slug}-{new_id.replace('-', '')[:12]}"
            if not is_token_available(final_slug, active_registry):
                final_slug = f"{base_slug}-{new_id.replace('-', '')}"
            if not is_token_available(final_slug, active_registry):
                raise ValidationError("Unable to derive a unique project slug from the generated project ID.")

    # Aliases validation
    clean_aliases: list[str] = []
    if aliases is not None:
        if not isinstance(aliases, list):
            raise ValidationError("Aliases must be a list of strings.")
        seen_aliases: set[str] = set()
        for a in aliases:
            clean_a = validate_token_format(a, "alias")
            lower_a = clean_a.lower()
            if lower_a in seen_aliases:
                raise ValidationError(f"Duplicate alias '{clean_a}' in aliases list.")
            seen_aliases.add(lower_a)
            if not is_token_available(clean_a, active_registry):
                raise ValidationError(f"Alias '{clean_a}' is already in use in registry.")
            if lower_a == final_slug.lower() or lower_a == new_id.lower():
                raise ValidationError(f"Alias '{clean_a}' cannot be identical to project slug or ID.")
            clean_aliases.append(clean_a)

    # Timestamp
    c_func = clock or now_rfc3339
    ts = c_func()
    validate_rfc3339_utc(ts, "created_at")

    # Create ProjectRecord
    record = ProjectRecord.create(
        project_id=new_id,
        slug=final_slug,
        display_name=eff_display_name,
        kind=PROJECT_KIND_DIRECTORY,
        aliases=clean_aliases,
        paths=[str(target_path)],
        git_common_dirs=[],
        git_remotes=[],
        created_at=ts,
        updated_at=ts,
    )

    # Dry-run handling
    if dry_run:
        return DirectoryRegistrationResult(
            status="candidate",
            project_id=record.id,
            proposed_record=record,
            evidence=["directory"],
            created=False,
            requires_confirmation=True,
            action="register_directory",
            candidate_metadata={
                "path": str(target_path),
                "proposed_id": record.id,
                "proposed_slug": record.slug,
                "proposed_display_name": record.display_name,
            },
        )

    # Transactional persistence: validate copy first, write to disk, then update caller registry
    persisted_registry = ProjectRegistry(
        version=active_registry.version,
        projects=[*active_registry.projects, record],
    )
    persisted_registry.validate()
    save_registry(persisted_registry, active_cfg)
    if registry is not None:
        active_registry.add_project(record)

    return DirectoryRegistrationResult(
        status="resolved",
        project_id=record.id,
        record=record,
        evidence=["directory"],
        created=True,
        requires_confirmation=False,
        action="register_directory",
    )


class DirectoryRegistrationService:
    """Service to explicitly register ordinary local non-Git directories in Personal Tideway."""

    def __init__(
        self,
        cfg: PersonalTidewayConfig | None = None,
        registry: ProjectRegistry | None = None,
        git_runner: Callable[[list[str], Path, float], tuple[int, str, str]] | None = None,
        timeout: float = 3.0,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.registry = registry
        self.git_runner = git_runner
        self.timeout = timeout
        self.id_factory = id_factory
        self.clock = clock

    def register(
        self,
        path: Path | str,
        *,
        display_name: str | None = None,
        slug: str | None = None,
        aliases: list[str] | None = None,
        probe: GitProbeResult | None = None,
        dry_run: bool = False,
    ) -> DirectoryRegistrationResult:
        return register_directory(
            path=path,
            cfg=self.cfg,
            registry=self.registry,
            display_name=display_name,
            slug=slug,
            aliases=aliases,
            id_factory=self.id_factory,
            clock=self.clock,
            probe=probe,
            git_runner=self.git_runner,
            timeout=self.timeout,
            dry_run=dry_run,
        )
