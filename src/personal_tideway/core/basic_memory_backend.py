"""Safe Basic Memory 0.23.2 project-database initialization and parity verification.

Work Package 10b implementation for Personal Tideway v2:
- Immutable backend plan, preview, project status, and execution result models.
- Pure deterministic planner aligning with validated ProjectRegistry ordering.
- Strict plan validation rejecting forged argv, injected env overrides, or invalid timeouts.
- Explicit reindex initialization to reconcile config.json into DB without main fallback.
- Explicit per-project status probing capturing safe counts without raw path leakage.
- Strict child environment isolation using allowlist and controlled overrides.
- Fail-closed execution requiring installed isolated service executable and canonical layout.
- Secret-safe error translations with suppressed cause chaining.
- Safe orchestration linking config reconciliation with backend verification.
"""

import json
import math
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.core.basic_memory_installer import (
    BasicMemoryRunner,
    BasicMemoryRunnerResult,
    build_subprocess_env,
    default_subprocess_runner,
    validate_isolated_executable,
)
from personal_tideway.core.basic_memory_reconciliation import (
    BasicMemoryProjectReconciliationResult,
    reconcile_basic_memory_projects,
    validate_registry_for_basic_memory,
)
from personal_tideway.core.basic_memory_runtime import (
    BASIC_MEMORY_AUTO_UPDATE_VALUE,
    BASIC_MEMORY_NO_PROMOS_VALUE,
    ENV_BASIC_MEMORY_AUTO_UPDATE,
    ENV_BASIC_MEMORY_CONFIG_DIR,
    ENV_BASIC_MEMORY_NO_PROMOS,
    ENV_UV_CACHE_DIR,
    ENV_UV_TOOL_BIN_DIR,
    ENV_UV_TOOL_DIR,
    BasicMemoryLayout,
    get_basic_memory_layout,
)
from personal_tideway.core.registry import ProjectRegistry, load_registry
from personal_tideway.exceptions import (
    BoundaryError,
    RuntimeProbeError,
    ValidationError,
)

# Default timeouts for backend operations (seconds)
DEFAULT_REINDEX_TIMEOUT: float = 120.0
DEFAULT_STATUS_TIMEOUT: float = 15.0

# Dedicated bounded status limit for realistic observed_files JSON arrays (4 MiB)
MAX_STATUS_OUTPUT_BYTES: int = 4 * 1024 * 1024


def compute_canonical_basic_memory_env_overrides(layout: BasicMemoryLayout) -> dict[str, str]:
    """Compute the canonical six environment overrides for an isolated Basic Memory layout."""
    return {
        ENV_UV_TOOL_DIR: str(layout.uv_tool_dir),
        ENV_UV_TOOL_BIN_DIR: str(layout.bin_dir),
        ENV_UV_CACHE_DIR: str(layout.cache_dir),
        ENV_BASIC_MEMORY_CONFIG_DIR: str(layout.config_dir),
        ENV_BASIC_MEMORY_AUTO_UPDATE: BASIC_MEMORY_AUTO_UPDATE_VALUE,
        ENV_BASIC_MEMORY_NO_PROMOS: BASIC_MEMORY_NO_PROMOS_VALUE,
    }


@dataclass(frozen=True)
class BasicMemoryProjectStatus:
    """Safe, bounded status summary for an explicit Basic Memory project."""

    project_name: str
    total_files: int
    observed_files_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.project_name, str) or not self.project_name.strip():
            raise ValidationError("Project name must be a non-empty string.")
        if type(self.total_files) is not int or self.total_files < 0:
            raise ValidationError("total_files must be a non-negative integer.")
        if type(self.observed_files_count) is not int or self.observed_files_count < 0:
            raise ValidationError("observed_files_count must be a non-negative integer.")

    @property
    def files_count(self) -> int:
        """Alias for total_files."""
        return self.total_files

    @property
    def observed_count(self) -> int:
        """Alias for observed_files_count."""
        return self.observed_files_count

    def to_dict(self) -> dict[str, Any]:
        """Return safe dictionary representation without leaking filesystem paths."""
        return {
            "project_name": self.project_name,
            "total_files": self.total_files,
            "observed_files_count": self.observed_files_count,
        }


@dataclass(frozen=True)
class BasicMemoryBackendPlanPreview:
    """Immutable preview of intended backend command invocations."""

    executable: Path
    project_names: tuple[str, ...]
    reindex_argvs: tuple[tuple[str, ...], ...]
    status_argvs: tuple[tuple[str, ...], ...]
    env_overrides: Mapping[str, str]
    is_noop: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_names", tuple(self.project_names))
        object.__setattr__(self, "reindex_argvs", tuple(tuple(arg) for arg in self.reindex_argvs))
        object.__setattr__(self, "status_argvs", tuple(tuple(arg) for arg in self.status_argvs))
        object.__setattr__(self, "env_overrides", MappingProxyType(dict(self.env_overrides)))

    @property
    def initialization_argv(self) -> tuple[str, ...] | None:
        """First reindex command used for initial configuration sync and indexing."""
        return self.reindex_argvs[0] if self.reindex_argvs else None

    def to_dict(self) -> dict[str, Any]:
        """Convert preview to safe dictionary representation."""
        return {
            "executable": str(self.executable),
            "project_names": list(self.project_names),
            "initialization_argv": list(self.initialization_argv) if self.initialization_argv is not None else None,
            "reindex_argvs": [list(argv) for argv in self.reindex_argvs],
            "status_argvs": [list(argv) for argv in self.status_argvs],
            "env_overrides": dict(self.env_overrides),
            "is_noop": self.is_noop,
        }


@dataclass(frozen=True)
class BasicMemoryBackendPlan:
    """Immutable plan for initializing and verifying Basic Memory project databases."""

    layout: BasicMemoryLayout
    executable: Path
    project_names: tuple[str, ...]
    reindex_argvs: tuple[tuple[str, ...], ...]
    status_argvs: tuple[tuple[str, ...], ...]
    env_overrides: Mapping[str, str]
    reindex_timeout: float = DEFAULT_REINDEX_TIMEOUT
    status_timeout: float = DEFAULT_STATUS_TIMEOUT

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_names", tuple(self.project_names))
        object.__setattr__(self, "reindex_argvs", tuple(tuple(arg) for arg in self.reindex_argvs))
        object.__setattr__(self, "status_argvs", tuple(tuple(arg) for arg in self.status_argvs))
        object.__setattr__(self, "env_overrides", MappingProxyType(dict(self.env_overrides)))

    @property
    def is_noop(self) -> bool:
        """True when there are no projects to initialize."""
        return len(self.project_names) == 0

    @property
    def initialization_argv(self) -> tuple[str, ...] | None:
        """First reindex command used for initial configuration sync and indexing, or None."""
        return self.reindex_argvs[0] if self.reindex_argvs else None

    @property
    def remaining_reindex_argvs(self) -> tuple[tuple[str, ...], ...]:
        """Subsequent reindex commands for remaining projects."""
        return self.reindex_argvs[1:] if len(self.reindex_argvs) > 1 else ()

    def preview(self) -> BasicMemoryBackendPlanPreview:
        """Generate a dry-run preview of planned invocations after validating plan invariants."""
        validate_basic_memory_backend_plan(self)
        return BasicMemoryBackendPlanPreview(
            executable=self.executable,
            project_names=self.project_names,
            reindex_argvs=self.reindex_argvs,
            status_argvs=self.status_argvs,
            env_overrides=self.env_overrides,
            is_noop=self.is_noop,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert plan to safe dictionary representation after validating plan invariants."""
        validate_basic_memory_backend_plan(self)
        return {
            "layout": self.layout.to_dict(),
            "executable": str(self.executable),
            "project_names": list(self.project_names),
            "initialization_argv": list(self.initialization_argv) if self.initialization_argv is not None else None,
            "reindex_argvs": [list(argv) for argv in self.reindex_argvs],
            "status_argvs": [list(argv) for argv in self.status_argvs],
            "env_overrides": dict(self.env_overrides),
            "reindex_timeout": self.reindex_timeout,
            "status_timeout": self.status_timeout,
            "is_noop": self.is_noop,
        }


def validate_basic_memory_backend_plan(
    plan: BasicMemoryBackendPlan,
    cfg: PersonalTidewayConfig | None = None,
) -> None:
    """Validate that plan is canonical, consistent, and safe to execute.

    Checks:
    - Plan type is BasicMemoryBackendPlan.
    - Timeouts are finite, positive numeric values (bool rejected).
    - Layout and executable match canonical Basic Memory layout.
    - If cfg is provided, layout and executable strictly match get_basic_memory_layout(cfg).
      Preserves BoundaryError exactly on symlink boundary violations; other unexpected
      resolution errors raise a fixed ValidationError from None.
    - env_overrides exactly match the canonical six environment overrides derived from layout.
    - project_names, reindex_argvs, and status_argvs have equal cardinality.
    - project_names contains no duplicate names (checked case-insensitively).
    - Every project name is a non-empty string.
    - Every argv exactly matches the supported Basic Memory 0.23.2 command for its project:
      - reindex: (executable, "reindex", "--search", "--project", name)
      - status: (executable, "status", "--project", name, "--local", "--json")
    - No-op plan has exactly zero commands.
    Raises fixed secret-safe ValidationError with suppressed cause.
    """
    if not isinstance(plan, BasicMemoryBackendPlan):
        raise ValidationError("Invalid Basic Memory backend plan type.") from None

    # 1. Validate timeouts: finite, positive numeric, bool rejected
    for timeout_val in (plan.reindex_timeout, plan.status_timeout):
        if isinstance(timeout_val, bool) or not isinstance(timeout_val, (int, float)):
            raise ValidationError("Plan timeout must be a positive finite number.") from None
        if math.isnan(timeout_val) or math.isinf(timeout_val) or timeout_val <= 0:
            raise ValidationError("Plan timeout must be a positive finite number.") from None

    # 2. Validate layout and executable
    if not isinstance(plan.layout, BasicMemoryLayout):
        raise ValidationError("Plan layout is invalid.") from None

    if not isinstance(plan.executable, Path):
        raise ValidationError("Plan executable is invalid.") from None

    if plan.executable != plan.layout.primary_executable:
        raise ValidationError("Plan executable does not match canonical primary executable.") from None

    if cfg is not None:
        try:
            canonical_layout = get_basic_memory_layout(cfg)
        except BoundaryError:
            # Preserve BoundaryError exactly without swallowing into ValidationError
            raise
        except Exception:  # noqa: BLE001 - translate unknown layout failures safely
            raise ValidationError("Failed to resolve canonical workspace layout.") from None

        if plan.layout != canonical_layout:
            raise ValidationError("Plan layout does not match canonical workspace layout.") from None

        if plan.executable != canonical_layout.primary_executable:
            raise ValidationError("Plan executable does not match canonical workspace layout.") from None

    # 3. Validate env_overrides: exact match with canonical six overrides
    canonical_env = compute_canonical_basic_memory_env_overrides(plan.layout)
    if dict(plan.env_overrides) != canonical_env:
        raise ValidationError("Plan env_overrides do not match canonical environment overrides.") from None

    # 4. Validate cardinality and command structure
    n_names = len(plan.project_names)
    n_reindex = len(plan.reindex_argvs)
    n_status = len(plan.status_argvs)

    if not (n_names == n_reindex == n_status):
        raise ValidationError(
            "Plan project_names, reindex_argvs, and status_argvs must have equal cardinality."
        ) from None

    # 5. Case-insensitive uniqueness and validity of project names
    seen_names: set[str] = set()
    for proj in plan.project_names:
        if not isinstance(proj, str) or not proj.strip():
            raise ValidationError("Plan project name must be a non-empty string.") from None
        clean_proj = proj.strip().casefold()
        if clean_proj in seen_names:
            raise ValidationError("Plan contains duplicate project names.") from None
        seen_names.add(clean_proj)

    exec_str = str(plan.executable)

    for i in range(n_names):
        proj = plan.project_names[i]
        expected_reindex = (exec_str, "reindex", "--search", "--project", proj)
        expected_status = (exec_str, "status", "--project", proj, "--local", "--json")

        if plan.reindex_argvs[i] != expected_reindex:
            raise ValidationError("Plan reindex_argv does not match expected explicit reindex command.") from None

        if plan.status_argvs[i] != expected_status:
            raise ValidationError("Plan status_argv does not match expected explicit status command.") from None


@dataclass(frozen=True)
class BasicMemoryBackendResult:
    """Immutable result of Basic Memory project initialization and parity verification."""

    dry_run: bool
    skipped: bool
    reindexed_projects: tuple[str, ...] = field(default_factory=tuple)
    statuses: tuple[BasicMemoryProjectStatus, ...] = field(default_factory=tuple)
    preview: BasicMemoryBackendPlanPreview | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reindexed_projects", tuple(self.reindexed_projects))
        object.__setattr__(self, "statuses", tuple(self.statuses))

    def to_dict(self) -> dict[str, Any]:
        """Convert result to safe dictionary representation."""
        return {
            "dry_run": self.dry_run,
            "skipped": self.skipped,
            "reindexed_projects": list(self.reindexed_projects),
            "statuses": [s.to_dict() for s in self.statuses],
            "preview": self.preview.to_dict() if self.preview is not None else None,
        }


@dataclass(frozen=True)
class BasicMemoryOrchestrationResult:
    """Combined result of project configuration reconciliation and backend verification."""

    reconciliation: BasicMemoryProjectReconciliationResult
    backend: BasicMemoryBackendResult

    @property
    def dry_run(self) -> bool:
        """True if operation ran in dry-run mode."""
        return self.backend.dry_run

    def to_dict(self) -> dict[str, Any]:
        """Convert orchestration result to safe dictionary representation."""
        return {
            "reconciliation": self.reconciliation.to_dict(),
            "backend": self.backend.to_dict(),
        }


def plan_basic_memory_backend(
    cfg: PersonalTidewayConfig,
    registry: ProjectRegistry,
    *,
    reindex_timeout: float = DEFAULT_REINDEX_TIMEOUT,
    status_timeout: float = DEFAULT_STATUS_TIMEOUT,
) -> BasicMemoryBackendPlan:
    """Construct an immutable, deterministic Basic Memory backend plan without side-effects.

    Pure builder: performs zero disk writes and spawns zero subprocesses.
    Follows registry project ordering:
    - If non-empty:
      1. First initialization argv: (executable, "reindex", "--search", "--project", first_project)
         which reconciles config.json into DB and indexes the first project.
      2. Remaining reindex argvs: one per remaining project.
      3. Status argvs: (executable, "status", "--project", name, "--local", "--json") per project.
    - If empty:
      Explicit safe no-op plan with zero commands. Avoids unconstrained reindex which
      Basic Memory 0.23.2 uses to seed a default 'main' project.
    """
    validate_registry_for_basic_memory(cfg, registry)
    layout = get_basic_memory_layout(cfg)
    resolved_exec = layout.primary_executable

    env_overrides = compute_canonical_basic_memory_env_overrides(layout)

    if not registry.projects:
        plan = BasicMemoryBackendPlan(
            layout=layout,
            executable=resolved_exec,
            project_names=(),
            reindex_argvs=(),
            status_argvs=(),
            env_overrides=env_overrides,
            reindex_timeout=reindex_timeout,
            status_timeout=status_timeout,
        )
        validate_basic_memory_backend_plan(plan, cfg=cfg)
        return plan

    names = tuple(rec.memory.project_name for rec in registry.projects)
    exec_str = str(resolved_exec)

    first_name = names[0]
    first_reindex = (exec_str, "reindex", "--search", "--project", first_name)
    remaining_reindex = tuple((exec_str, "reindex", "--search", "--project", name) for name in names[1:])
    reindex_argvs = (first_reindex,) + remaining_reindex

    status_argvs = tuple((exec_str, "status", "--project", name, "--local", "--json") for name in names)

    plan = BasicMemoryBackendPlan(
        layout=layout,
        executable=resolved_exec,
        project_names=names,
        reindex_argvs=reindex_argvs,
        status_argvs=status_argvs,
        env_overrides=env_overrides,
        reindex_timeout=reindex_timeout,
        status_timeout=status_timeout,
    )
    validate_basic_memory_backend_plan(plan, cfg=cfg)
    return plan


def parse_basic_memory_status(stdout: str, project_name: str) -> BasicMemoryProjectStatus:
    """Parse bounded status JSON output and return safe BasicMemoryProjectStatus.

    Requires:
    - stdout UTF-8 byte length does not exceed MAX_STATUS_OUTPUT_BYTES (4 MiB)
    - stdout parses as valid JSON
    - parsed root is a JSON object (dict)
    - 'total_files' is a non-negative integer (bool rejected)
    - 'observed_files' is a list
    Preserves only safe count metrics; never stores raw file paths or contents.
    Rejects output exceeding limit with fixed RuntimeProbeError without mid-JSON truncation.
    """
    if not isinstance(stdout, str):
        raise RuntimeProbeError("Basic Memory status output must be a string.") from None

    try:
        stdout_bytes = stdout.encode("utf-8")
    except UnicodeEncodeError:
        raise RuntimeProbeError("Basic Memory status output must be valid UTF-8 text.") from None
    if len(stdout_bytes) > MAX_STATUS_OUTPUT_BYTES:
        raise RuntimeProbeError("Basic Memory status output exceeded maximum allowable size.") from None

    try:
        data = json.loads(stdout)
    except Exception:  # noqa: BLE001 - normalize parser failures at the public boundary
        raise RuntimeProbeError("Basic Memory status output must be valid JSON.") from None

    if not isinstance(data, dict):
        raise RuntimeProbeError("Basic Memory status output schema is invalid.") from None

    total_files = data.get("total_files")
    if type(total_files) is not int or total_files < 0:
        raise RuntimeProbeError("Basic Memory status output schema is invalid.") from None

    observed_files = data.get("observed_files")
    if not isinstance(observed_files, list):
        raise RuntimeProbeError("Basic Memory status output schema is invalid.") from None

    return BasicMemoryProjectStatus(
        project_name=project_name,
        total_files=total_files,
        observed_files_count=len(observed_files),
    )


@dataclass(frozen=True)
class _CommandResult:
    """Internal normalized runner command execution result."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


def _normalize_returncode(rc: Any) -> int:
    """Validate returncode is strictly an integer and not bool.

    Rejects:
    - bool (True, False)
    - numeric strings ("0", "1")
    - None, float, and all other non-int types
    """
    if type(rc) is not int:
        raise RuntimeProbeError("Runner returned an unexpected result type.") from None
    return rc


def _normalize_stream_output(stream_val: Any) -> str:
    """Normalize runner stdout/stderr stream output to string safely.

    Accepts:
    - str: returned as is
    - bytes, bytearray: decoded as UTF-8 ignoring decoding errors
    - None: returns empty string
    Rejects all other types with fixed RuntimeProbeError.
    """
    if stream_val is None:
        return ""
    if isinstance(stream_val, str):
        return stream_val
    if isinstance(stream_val, (bytes, bytearray)):
        return bytes(stream_val).decode("utf-8", errors="ignore")
    raise RuntimeProbeError("Runner returned an unexpected result type.") from None


def _invoke_runner(
    runner: BasicMemoryRunner,
    argv: tuple[str, ...],
    env: Mapping[str, str],
    timeout: float,
) -> _CommandResult:
    """Safely invoke runner callable with explicit timeout and return normalized _CommandResult."""
    safe_env = MappingProxyType(dict(env))
    try:
        raw_res = runner(argv, safe_env, timeout)
    except (subprocess.TimeoutExpired, TimeoutError):
        raise
    except Exception:  # noqa: BLE001 - custom runners are an untrusted extension boundary
        raise RuntimeProbeError("Subprocess execution failed.") from None

    try:
        # 1. Tuple form: cardinality must be exactly 3
        if isinstance(raw_res, tuple):
            if len(raw_res) != 3:
                raise RuntimeProbeError("Runner returned an unexpected result type.") from None
            rc = _normalize_returncode(raw_res[0])
            out = _normalize_stream_output(raw_res[1])
            err = _normalize_stream_output(raw_res[2])
            return _CommandResult(returncode=rc, stdout=out, stderr=err)

        # 2. BasicMemoryRunnerResult form
        if isinstance(raw_res, BasicMemoryRunnerResult):
            rc = _normalize_returncode(raw_res.returncode)
            out = _normalize_stream_output(raw_res.stdout)
            err = _normalize_stream_output(raw_res.stderr)
            return _CommandResult(returncode=rc, stdout=out, stderr=err)

        # 3. Object with returncode attribute
        if hasattr(raw_res, "returncode"):
            rc = _normalize_returncode(raw_res.returncode)
            out = _normalize_stream_output(getattr(raw_res, "stdout", None))
            err = _normalize_stream_output(getattr(raw_res, "stderr", None))
            return _CommandResult(returncode=rc, stdout=out, stderr=err)
    except RuntimeProbeError:
        raise
    except Exception:  # noqa: BLE001 - malformed runner objects must not leak errors
        raise RuntimeProbeError("Runner returned an unexpected result type.") from None

    raise RuntimeProbeError("Runner returned an unexpected result type.") from None


def _require_backend_executable(plan: BasicMemoryBackendPlan, cfg: PersonalTidewayConfig) -> None:
    """Fail closed unless the plan points at the installed isolated executable."""
    try:
        is_valid = validate_isolated_executable(
            plan.executable,
            service_root=plan.layout.service_root,
            home_boundary=cfg.home,
        )
    except (BoundaryError, RuntimeProbeError):
        raise
    except Exception:  # noqa: BLE001 - translate executable probe failures safely
        raise RuntimeProbeError("Basic Memory executable missing or invalid.") from None

    if not is_valid:
        raise RuntimeProbeError("Basic Memory executable missing or invalid.") from None


def execute_basic_memory_backend_plan(
    plan: BasicMemoryBackendPlan,
    *,
    dry_run: bool = False,
    runner: BasicMemoryRunner | None = None,
    cfg: PersonalTidewayConfig | None = None,
) -> BasicMemoryBackendResult:
    """Execute a previously built Basic Memory backend plan.

    Fails closed before invoking runner:
    - Requires PersonalTidewayConfig for non-noop non-dry-run execution.
    - Validates plan invariants, canonical layout match, and exact command forms.
    - If dry_run: returns preview without runner calls or disk writes; reindexed_projects is empty.
    - If plan is a no-op (empty registry): returns skipped result, calls runner zero times.
    - Validates that executable is an installed regular executable inside the PTW Basic Memory service.
    - Builds isolated child environment via allowlist and plan overrides.
    - Runs all reindex commands sequentially with plan.reindex_timeout.
    - Runs all status commands sequentially with plan.status_timeout.
    - Halts on first failure without calling subsequent commands.
    - Enforces returncode 0 and validates bounded status JSON schema.
    - Translates errors to stable PTW exceptions with suppressed cause chaining.
    """
    # 1. Require cfg for non-noop non-dry-run execution
    if not plan.is_noop and not dry_run and cfg is None:
        raise ValidationError("PersonalTidewayConfig is required for non-dry-run execution.") from None

    # 2. Strict validation of plan invariants against canonical layout and commands
    validate_basic_memory_backend_plan(plan, cfg=cfg)

    # 3. Dry-run: returns preview without invoking runner or filesystem mutations; reindexed_projects is empty
    if dry_run:
        return BasicMemoryBackendResult(
            dry_run=True,
            skipped=plan.is_noop,
            reindexed_projects=(),
            statuses=(),
            preview=plan.preview(),
        )

    # 4. No-op plan (empty registry): returns skipped result, calls runner zero times
    if plan.is_noop:
        return BasicMemoryBackendResult(
            dry_run=False,
            skipped=True,
            reindexed_projects=(),
            statuses=(),
            preview=None,
        )

    # 5. Non-dry-run with non-empty registry
    assert cfg is not None
    _require_backend_executable(plan, cfg)

    subprocess_env = build_subprocess_env(plan.env_overrides)
    active_runner = runner if runner is not None else default_subprocess_runner

    reindexed: list[str] = []
    for i, argv in enumerate(plan.reindex_argvs):
        try:
            res = _invoke_runner(active_runner, argv, subprocess_env, plan.reindex_timeout)
        except (subprocess.TimeoutExpired, TimeoutError):
            raise RuntimeProbeError("Basic Memory reindex timed out.") from None
        except RuntimeProbeError:
            raise
        except Exception:  # noqa: BLE001 - translate runner failures safely
            raise RuntimeProbeError("Basic Memory reindex execution failed.") from None

        if res.returncode != 0:
            raise RuntimeProbeError("Basic Memory reindex failed with non-zero exit code.") from None

        reindexed.append(plan.project_names[i])

    statuses: list[BasicMemoryProjectStatus] = []
    for i, argv in enumerate(plan.status_argvs):
        proj_name = plan.project_names[i]
        try:
            res = _invoke_runner(active_runner, argv, subprocess_env, plan.status_timeout)
        except (subprocess.TimeoutExpired, TimeoutError):
            raise RuntimeProbeError("Basic Memory status timed out.") from None
        except RuntimeProbeError:
            raise
        except Exception:  # noqa: BLE001 - translate runner failures safely
            raise RuntimeProbeError("Basic Memory status execution failed.") from None

        if res.returncode != 0:
            raise RuntimeProbeError("Basic Memory status failed with non-zero exit code.") from None

        status_obj = parse_basic_memory_status(res.stdout, proj_name)
        statuses.append(status_obj)

    return BasicMemoryBackendResult(
        dry_run=False,
        skipped=False,
        reindexed_projects=tuple(reindexed),
        statuses=tuple(statuses),
        preview=None,
    )


def sync_and_initialize_basic_memory_backend(
    cfg: PersonalTidewayConfig,
    registry: ProjectRegistry | None = None,
    *,
    dry_run: bool = False,
    runner: BasicMemoryRunner | None = None,
    reindex_timeout: float = DEFAULT_REINDEX_TIMEOUT,
    status_timeout: float = DEFAULT_STATUS_TIMEOUT,
) -> BasicMemoryOrchestrationResult:
    """Orchestrate configuration reconciliation followed by backend initialization and parity verification.

    Coherent Snapshot Guarantee:
        If registry is None, load_registry is called exactly once to produce a single,
        coherent in-memory ProjectRegistry snapshot. That exact snapshot instance is
        passed to both reconcile_basic_memory_projects and plan_basic_memory_backend,
        preventing race conditions from reloading modified registry YAML between steps.

    Residual Concurrency Boundary Note:
        reconcile_basic_memory_projects acquires an exclusive filesystem lock
        (cfg.locks_dir/basic-memory-install.lock) during directory normalization and
        config.json generation. However, subsequent execution of external Basic Memory
        CLI processes (reindex, status) runs in separate child processes outside that lock.
        Upstream Basic Memory 0.23.2 does not provide a cross-process transaction across
        configuration reconciliation and CLI process execution. Callers must note that
        concurrent external CLI processes could interleave after reconciliation.
    """
    effective_registry = registry if registry is not None else load_registry(cfg.projects_yaml)

    plan = plan_basic_memory_backend(
        cfg,
        effective_registry,
        reindex_timeout=reindex_timeout,
        status_timeout=status_timeout,
    )

    # Fail before reconciliation can write config or create directories. Execution
    # validates again after reconciliation to close the replacement race window.
    if not dry_run and not plan.is_noop:
        _require_backend_executable(plan, cfg)

    recon_res = reconcile_basic_memory_projects(cfg, registry=effective_registry, dry_run=dry_run)

    backend_res = execute_basic_memory_backend_plan(
        plan,
        dry_run=dry_run,
        runner=runner,
        cfg=cfg,
    )

    return BasicMemoryOrchestrationResult(
        reconciliation=recon_res,
        backend=backend_res,
    )


# Canonical aliases
plan_backend = plan_basic_memory_backend
execute_backend_plan = execute_basic_memory_backend_plan

__all__ = [
    "DEFAULT_REINDEX_TIMEOUT",
    "DEFAULT_STATUS_TIMEOUT",
    "MAX_STATUS_OUTPUT_BYTES",
    "BasicMemoryBackendPlan",
    "BasicMemoryBackendPlanPreview",
    "BasicMemoryBackendResult",
    "BasicMemoryOrchestrationResult",
    "BasicMemoryProjectStatus",
    "compute_canonical_basic_memory_env_overrides",
    "execute_backend_plan",
    "execute_basic_memory_backend_plan",
    "parse_basic_memory_status",
    "plan_backend",
    "plan_basic_memory_backend",
    "sync_and_initialize_basic_memory_backend",
    "validate_basic_memory_backend_plan",
]
