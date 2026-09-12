"""Concise idempotent checkpoint writes for Personal Tideway v2 context loop.

Work Package 12 implementation:
- Immutable validated request/payload models for curated canonical current-state.
- Structured fields: condition, objective, completed, blockers, verification_status,
  next_safe_action, source_client, and bounded evidence references.
- Pre-execution secret and credential scanning rejecting sensitive patterns without echoing values.
- Semantic SHA-256 fingerprinting embedded in controlled YAML frontmatter.
- Strict project resolution through central registry; fails closed on missing/ambiguous projects.
- Safe per-project file locking below cfg.locks_dir rejecting symlinks and hardlinks.
- Idempotence: reads existing current-state inside the critical section, skips write and preserves
  timestamp when fingerprint matches.
- Clock and runner injection for deterministic testing.
- Bounded runner subprocess execution with temp-file capture, shell=False, and sanitized environment.
- Dry-run mode performing zero probes, zero clock calls, zero locks, and zero filesystem mutations.
"""

import fcntl
import hashlib
import inspect
import json
import math
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from personal_tideway.config import PersonalTidewayConfig, validate_owned_path
from personal_tideway.constants import CLIENT_CODEX, SUPPORTED_CLIENTS
from personal_tideway.core.basic_memory_backend import (
    compute_canonical_basic_memory_env_overrides,
)
from personal_tideway.core.basic_memory_installer import (
    BasicMemoryRunner,
    BasicMemoryRunnerResult,
    build_subprocess_env,
    validate_isolated_executable,
)
from personal_tideway.core.basic_memory_runtime import (
    BasicMemoryLayout,
    get_basic_memory_layout,
)
from personal_tideway.core.context_retrieval import (
    CURRENT_STATE_PERMALINK,
    build_read_note_argv,
    resolve_registered_project,
)
from personal_tideway.core.registry import (
    ProjectRecord,
    ProjectRegistry,
    load_registry,
)
from personal_tideway.exceptions import (
    BoundaryError,
    ConflictError,
    RuntimeProbeError,
    SecretsError,
    ValidationError,
)

# Canonical note title, folder, and permalink
CURRENT_STATE_TITLE: str = "Current State"
CURRENT_STATE_FOLDER: str = "."

# Default timeouts and bounding limits
DEFAULT_CHECKPOINT_TIMEOUT: float = 15.0
MAX_CHECKPOINT_OUTPUT_BYTES: int = 2 * 1024 * 1024  # 2 MiB bounded output limit
MAX_CHECKPOINT_INPUT_BYTES: int = 64 * 1024  # 64 KiB bounded input limit

MIN_CHECKPOINT_CHARS: int = 600
DEFAULT_MAX_CHECKPOINT_CHARS: int = 8000
HARD_MAX_CHECKPOINT_CHARS: int = 32000

MAX_FIELD_CHARS: int = 4000
MAX_ITEM_CHARS: int = 500
MAX_COLLECTION_ITEMS: int = 50

# Valid status and source-client sets
CHECKPOINT_VALID_STATUSES: frozenset[str] = frozenset(
    {"verified", "partial", "planned", "blocked"}
)
CHECKPOINT_VALID_CLIENTS: frozenset[str] = frozenset(SUPPORTED_CLIENTS)

# Control character detection regex (excluding valid \n and \t for multiline bodies)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Patterns for detecting credentials and secrets before any runner/probe execution
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"-----BEGIN (?:[A-Z0-9_-]+ )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{16,}\b", re.IGNORECASE),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", re.IGNORECASE),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b", re.IGNORECASE),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z_\-]{10,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret[_-]?key|private[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]{6,}",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bauthorization\s*[:=]\s*['\"]?(?:bearer\s+)?[^\s'\"]{10,}",
        re.IGNORECASE,
    ),
    re.compile(r"\bbearer\s+[A-Za-z0-9_\-\.]{15,}\b", re.IGNORECASE),
]

_SAFE_CREDENTIAL_PROSE: frozenset[str] = frozenset({
    "rotated",
    "system",
    "header",
    "none",
    "null",
    "unset",
    "configured",
    "enabled",
    "disabled",
    "required",
    "optional",
    "present",
    "missing",
    "default",
    "placeholder",
    "omitted",
    "redacted",
    "verified",
    "env",
})

_CREDENTIAL_KEYWORD_RE = re.compile(
    r"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret[_-]?key|private[_-]?key)\s*[:=]\s*(.+)",
    re.IGNORECASE,
)


def _is_benign_credential_prose(matched_text: str) -> bool:
    """Determine if a credential assignment match is benign operational prose."""
    kw_match = _CREDENTIAL_KEYWORD_RE.match(matched_text)
    if not kw_match:
        return False
    val_part = kw_match.group(1).strip()
    if (val_part.startswith("'") and val_part.endswith("'")) or (
        val_part.startswith('"') and val_part.endswith('"')
    ):
        val_part = val_part[1:-1].strip()
    elif val_part.startswith(("'", '"')):
        val_part = val_part[1:].strip()
    token = re.split(r"[\s,;]+", val_part)[0].strip().lower()
    token = token.rstrip(".,;:!?")
    return token in _SAFE_CREDENTIAL_PROSE


class CheckpointSecretError(ValidationError, SecretsError):
    """Raised when potential credentials or secrets are detected in a checkpoint payload."""


class CheckpointLockContentionError(ConflictError, RuntimeProbeError):
    """Raised when a per-project checkpoint lock cannot be acquired due to contention."""


def _assert_no_secrets(value: str) -> None:
    """Scan string for known token, private key, password, or auth patterns without echoing."""
    for pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(value):
            if _is_benign_credential_prose(match.group(0)):
                continue
            raise CheckpointSecretError(
                "Potential secret or credential pattern detected in checkpoint payload."
            )


def _validate_clean_string(
    val: Any,
    field_name: str,
    *,
    max_len: int = MAX_FIELD_CHARS,
    allow_multiline: bool = False,
    allow_empty: bool = False,
) -> str:
    """Strictly validate a scalar text field."""
    if not isinstance(val, str):
        raise ValidationError(f"{field_name} must be a string.")

    # Reject invalid Unicode / unpaired surrogates
    try:
        val.encode("utf-8")
    except (UnicodeEncodeError, UnicodeError, ValueError):
        raise ValidationError(f"{field_name} contains invalid Unicode encoding.") from None

    if _CONTROL_CHARS_RE.search(val):
        raise ValidationError(f"{field_name} contains disallowed control characters.")

    if not allow_multiline and ("\n" in val or "\r" in val):
        raise ValidationError(f"{field_name} must be a single line without newlines.")

    clean = val.strip()
    if not allow_empty and not clean:
        raise ValidationError(f"{field_name} cannot be empty.")

    if len(clean) > max_len:
        raise ValidationError(
            f"{field_name} exceeds maximum length of {max_len} characters."
        )

    _assert_no_secrets(clean)
    return clean


def _validate_evidence_reference(ref: Any) -> str:
    """Validate a bounded evidence reference string."""
    if not isinstance(ref, str):
        raise ValidationError("Evidence reference must be a string.")

    try:
        ref.encode("utf-8")
    except (UnicodeEncodeError, UnicodeError, ValueError):
        raise ValidationError("Evidence reference contains invalid Unicode encoding.") from None

    if _CONTROL_CHARS_RE.search(ref):
        raise ValidationError("Evidence reference contains disallowed control characters.")

    if "\n" in ref or "\r" in ref:
        raise ValidationError("Evidence reference must be a single line.")

    clean = ref.strip()
    if not clean:
        raise ValidationError("Evidence reference cannot be empty.")

    if clean.startswith("-"):
        raise ValidationError("Evidence reference cannot begin with a dash.")

    if clean.startswith(("/", "\\")) or Path(clean).is_absolute():
        raise ValidationError("Evidence reference cannot be an absolute path.")

    norm_parts = clean.replace("\\", "/").split("/")
    if any(p == ".." for p in norm_parts):
        raise ValidationError("Evidence reference cannot contain '..' path traversal.")

    if len(clean) > MAX_ITEM_CHARS:
        raise ValidationError(
            f"Evidence reference exceeds maximum length of {MAX_ITEM_CHARS} characters."
        )

    _assert_no_secrets(clean)
    return clean


@dataclass(frozen=True)
class CheckpointPayload:
    """Immutable, validated curated current-state payload for a project checkpoint."""

    condition: str
    objective: str
    completed: tuple[str, ...] = field(default_factory=tuple)
    blockers: tuple[str, ...] = field(default_factory=tuple)
    verification_status: str = "verified"
    next_safe_action: str = ""
    source_client: str = CLIENT_CODEX
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def __init__(
        self,
        condition: str,
        objective: str,
        completed: Sequence[str] = (),
        blockers: Sequence[str] = (),
        verification_status: str = "verified",
        next_safe_action: str = "",
        source_client: str = CLIENT_CODEX,
        evidence: Sequence[str] = (),
        # Field aliases for flexibility
        current_condition: str | None = None,
        active_objective: str | None = None,
        status: str | None = None,
        client: str | None = None,
        evidence_references: Sequence[str] | None = None,
    ) -> None:
        raw_cond = current_condition if current_condition is not None else condition
        raw_obj = active_objective if active_objective is not None else objective
        raw_status = status if status is not None else verification_status
        raw_client = client if client is not None else source_client
        raw_ev = evidence_references if evidence_references is not None else evidence

        clean_condition = _validate_clean_string(
            raw_cond, "condition", max_len=MAX_FIELD_CHARS, allow_multiline=True
        )
        clean_objective = _validate_clean_string(
            raw_obj, "objective", max_len=MAX_FIELD_CHARS, allow_multiline=True
        )

        # Status validation
        if not isinstance(raw_status, str):
            raise ValidationError("verification_status must be a string.")
        try:
            raw_status.encode("utf-8")
        except (UnicodeEncodeError, UnicodeError, ValueError):
            raise ValidationError("verification_status contains invalid Unicode encoding.") from None
        _assert_no_secrets(raw_status)
        clean_status = raw_status.strip().lower()
        if clean_status not in CHECKPOINT_VALID_STATUSES:
            raise ValidationError(
                f"verification_status must be one of {sorted(CHECKPOINT_VALID_STATUSES)}."
            )

        # Source client validation
        if not isinstance(raw_client, str):
            raise ValidationError("source_client must be a string.")
        try:
            raw_client.encode("utf-8")
        except (UnicodeEncodeError, UnicodeError, ValueError):
            raise ValidationError("source_client contains invalid Unicode encoding.") from None
        _assert_no_secrets(raw_client)
        clean_client = raw_client.strip().lower()
        if clean_client not in CHECKPOINT_VALID_CLIENTS:
            raise ValidationError(
                f"source_client must be one of {sorted(CHECKPOINT_VALID_CLIENTS)}."
            )

        clean_next_action = _validate_clean_string(
            next_safe_action,
            "next_safe_action",
            max_len=MAX_FIELD_CHARS,
            allow_multiline=False,
            allow_empty=True,
        )

        # Completed items collection validation
        if not isinstance(completed, (list, tuple)):
            raise ValidationError("completed must be a sequence of strings.")
        if len(completed) > MAX_COLLECTION_ITEMS:
            raise ValidationError(
                f"completed collection exceeds maximum limit of {MAX_COLLECTION_ITEMS} items."
            )
        clean_completed: list[str] = []
        for i, item in enumerate(completed):
            clean_item = _validate_clean_string(
                item, f"completed[{i}]", max_len=MAX_ITEM_CHARS, allow_multiline=False
            )
            clean_completed.append(clean_item)

        # Blockers collection validation
        if not isinstance(blockers, (list, tuple)):
            raise ValidationError("blockers must be a sequence of strings.")
        if len(blockers) > MAX_COLLECTION_ITEMS:
            raise ValidationError(
                f"blockers collection exceeds maximum limit of {MAX_COLLECTION_ITEMS} items."
            )
        clean_blockers: list[str] = []
        for i, item in enumerate(blockers):
            clean_item = _validate_clean_string(
                item, f"blockers[{i}]", max_len=MAX_ITEM_CHARS, allow_multiline=False
            )
            clean_blockers.append(clean_item)

        # Evidence references collection validation
        if not isinstance(raw_ev, (list, tuple)):
            raise ValidationError("evidence must be a sequence of strings.")
        if len(raw_ev) > MAX_COLLECTION_ITEMS:
            raise ValidationError(
                f"evidence collection exceeds maximum limit of {MAX_COLLECTION_ITEMS} items."
            )
        clean_evidence: list[str] = []
        for item in raw_ev:
            clean_evidence.append(_validate_evidence_reference(item))

        object.__setattr__(self, "condition", clean_condition)
        object.__setattr__(self, "objective", clean_objective)
        object.__setattr__(self, "completed", tuple(clean_completed))
        object.__setattr__(self, "blockers", tuple(clean_blockers))
        object.__setattr__(self, "verification_status", clean_status)
        object.__setattr__(self, "next_safe_action", clean_next_action)
        object.__setattr__(self, "source_client", clean_client)
        object.__setattr__(self, "evidence", tuple(clean_evidence))

    @property
    def current_condition(self) -> str:
        """Alias for condition."""
        return self.condition

    @property
    def active_objective(self) -> str:
        """Alias for objective."""
        return self.objective

    @property
    def status(self) -> str:
        """Alias for verification_status."""
        return self.verification_status

    def to_dict(self) -> dict[str, Any]:
        """Convert payload to safe dictionary without leaking secrets."""
        return {
            "condition": self.condition,
            "objective": self.objective,
            "completed": list(self.completed),
            "blockers": list(self.blockers),
            "verification_status": self.verification_status,
            "next_safe_action": self.next_safe_action,
            "source_client": self.source_client,
            "evidence": list(self.evidence),
        }


def compute_semantic_fingerprint(payload: CheckpointPayload) -> str:
    """Deterministically compute SHA-256 hex digest of canonical semantic checkpoint fields."""
    canonical_dict = {
        "blockers": list(payload.blockers),
        "completed": list(payload.completed),
        "condition": payload.condition,
        "evidence": list(payload.evidence),
        "next_safe_action": payload.next_safe_action,
        "objective": payload.objective,
        "source_client": payload.source_client,
        "verification_status": payload.verification_status,
    }
    canonical_json = json.dumps(
        canonical_dict,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def render_checkpoint_markdown(
    payload: CheckpointPayload,
    *,
    project_name: str,
    timestamp: str,
    fingerprint: str,
) -> str:
    """Deterministically render canonical current-state Markdown with controlled frontmatter."""
    # Frontmatter header
    lines: list[str] = [
        "---",
        f"title: {CURRENT_STATE_TITLE}",
        "type: state",
        "note_type: state",
        f"status: {payload.verification_status}",
        f"updated_at: {timestamp}",
        f"source_client: {payload.source_client}",
        f"fingerprint: {fingerprint}",
        "---",
        "",
        f"# {CURRENT_STATE_TITLE}: {project_name}",
        "",
        f"**Status:** {payload.verification_status} | **Updated:** {timestamp} | **Client:** {payload.source_client}",
        "",
        "## Current Condition",
        payload.condition,
        "",
        "## Active Objective",
        payload.objective,
        "",
        "## Completed & Decisions",
    ]

    if payload.completed:
        for item in payload.completed:
            lines.append(f"- {item}")
    else:
        lines.append("- (none)")

    lines.extend([
        "",
        "## Blockers",
    ])

    if payload.blockers:
        for item in payload.blockers:
            lines.append(f"- {item}")
    else:
        lines.append("- None")

    lines.extend([
        "",
        "## Next Safe Action",
        payload.next_safe_action if payload.next_safe_action else "- None",
        "",
        "## Evidence & References",
    ])

    if payload.evidence:
        for item in payload.evidence:
            lines.append(f"- {item}")
    else:
        lines.append("- None")

    lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True)
class CheckpointRequest:
    """Immutable specification for a project checkpoint write operation."""

    project: str | ProjectRecord
    payload: CheckpointPayload
    max_chars: int = DEFAULT_MAX_CHECKPOINT_CHARS
    timeout: float = DEFAULT_CHECKPOINT_TIMEOUT

    def __init__(
        self,
        project: str | ProjectRecord,
        payload: CheckpointPayload | None = None,
        max_chars: int = DEFAULT_MAX_CHECKPOINT_CHARS,
        timeout: float = DEFAULT_CHECKPOINT_TIMEOUT,
        # Allow constructing directly with payload keyword arguments
        condition: str | None = None,
        objective: str | None = None,
        completed: Sequence[str] = (),
        blockers: Sequence[str] = (),
        verification_status: str = "verified",
        next_safe_action: str = "",
        source_client: str = CLIENT_CODEX,
        evidence: Sequence[str] = (),
        current_condition: str | None = None,
        active_objective: str | None = None,
        status: str | None = None,
        client: str | None = None,
        evidence_references: Sequence[str] | None = None,
    ) -> None:
        if isinstance(project, str):
            clean_proj = project.strip()
            if not clean_proj:
                raise ValidationError("Project identifier cannot be empty.")
            _assert_no_secrets(clean_proj)
            object.__setattr__(self, "project", clean_proj)
        elif isinstance(project, ProjectRecord):
            project.validate()
            object.__setattr__(self, "project", project)
        else:
            raise ValidationError("Project must be a string identifier or ProjectRecord.")

        if payload is not None:
            if not isinstance(payload, CheckpointPayload):
                raise ValidationError("payload must be a CheckpointPayload instance.")
            final_payload = payload
        else:
            cond = current_condition if current_condition is not None else condition
            obj = active_objective if active_objective is not None else objective
            if cond is None or obj is None:
                raise ValidationError(
                    "Either a CheckpointPayload or condition and objective arguments must be provided."
                )
            final_payload = CheckpointPayload(
                condition=cond,
                objective=obj,
                completed=completed,
                blockers=blockers,
                verification_status=status if status is not None else verification_status,
                next_safe_action=next_safe_action,
                source_client=client if client is not None else source_client,
                evidence=evidence_references if evidence_references is not None else evidence,
            )

        object.__setattr__(self, "payload", final_payload)

        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or max_chars < MIN_CHECKPOINT_CHARS
            or max_chars > HARD_MAX_CHECKPOINT_CHARS
        ):
            raise ValidationError(
                f"max_chars must be an integer between {MIN_CHECKPOINT_CHARS} and {HARD_MAX_CHECKPOINT_CHARS}."
            )
        object.__setattr__(self, "max_chars", max_chars)

        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or math.isnan(timeout)
            or math.isinf(timeout)
            or timeout <= 0
        ):
            raise ValidationError("timeout must be a positive finite number.")
        object.__setattr__(self, "timeout", float(timeout))

    def to_dict(self) -> dict[str, Any]:
        """Convert request to safe dictionary representation without leaking secrets."""
        project_val = (
            self.project.id
            if isinstance(self.project, ProjectRecord)
            else self.project
        )
        return {
            "project": project_val,
            "max_chars": self.max_chars,
            "timeout": self.timeout,
            "payload": self.payload.to_dict(),
        }


@dataclass(frozen=True)
class CheckpointPlanPreview:
    """Safe, immutable preview of planned checkpoint execution with redacted content."""

    project_id: str
    project_name: str
    operation: str
    fingerprint: str
    is_noop: bool
    command: str
    limits: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "limits", MappingProxyType(dict(self.limits)))

    def to_dict(self) -> dict[str, Any]:
        """Convert preview to safe dictionary without exposing host paths or note content."""
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "operation": self.operation,
            "fingerprint": self.fingerprint,
            "is_noop": self.is_noop,
            "command": self.command,
            "limits": dict(self.limits),
        }


@dataclass(frozen=True)
class CheckpointPlan:
    """Pure, immutable plan for executing a checkpoint write."""

    request: CheckpointRequest
    layout: BasicMemoryLayout
    executable: Path
    project_record: ProjectRecord
    write_argv: tuple[str, ...]
    read_argv: tuple[str, ...]
    fingerprint: str
    env_overrides: Mapping[str, str]
    timeout: float
    is_noop: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "write_argv", tuple(self.write_argv))
        object.__setattr__(self, "read_argv", tuple(self.read_argv))
        object.__setattr__(
            self, "env_overrides", MappingProxyType(dict(self.env_overrides))
        )

    def preview(self) -> CheckpointPlanPreview:
        """Return safe preview of planned checkpoint execution."""
        safe_cmd = (
            f"basic-memory tool write-note --title {CURRENT_STATE_TITLE!r} "
            f"--folder {CURRENT_STATE_FOLDER!r} "
            f"--project {self.project_record.memory.project_name!r} --overwrite --local <stdin>"
        )
        return CheckpointPlanPreview(
            project_id=self.project_record.id,
            project_name=self.project_record.memory.project_name,
            operation="checkpoint_write",
            fingerprint=self.fingerprint,
            is_noop=self.is_noop,
            command=safe_cmd,
            limits={
                "max_chars": self.request.max_chars,
                "timeout": self.timeout,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert plan to safe dictionary without leaking host paths or secrets."""
        d = self.preview().to_dict()
        d["is_noop"] = self.is_noop
        return d


@dataclass(frozen=True)
class CheckpointResult:
    """Immutable result of a checkpoint write or idempotency check."""

    project_id: str
    project_name: str
    fingerprint: str
    written: bool
    dry_run: bool
    timestamp: str
    rendered_text: str = ""
    preview: CheckpointPlanPreview | None = None

    @property
    def unchanged(self) -> bool:
        """Convenience property indicating whether the checkpoint was unchanged."""
        return not self.written

    def to_dict(self) -> dict[str, Any]:
        """Convert result to safe dictionary representation."""
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "fingerprint": self.fingerprint,
            "written": self.written,
            "unchanged": self.unchanged,
            "dry_run": self.dry_run,
            "timestamp": self.timestamp,
            "preview": self.preview.to_dict() if self.preview is not None else None,
        }


@dataclass(frozen=True)
class CheckpointRunnerResult:
    """Immutable result from a checkpoint write runner."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


# ---------------------------------------------------------------------------
# Concurrency Lock
# ---------------------------------------------------------------------------


@contextmanager
def acquire_project_checkpoint_lock(
    cfg: PersonalTidewayConfig,
    project_record: ProjectRecord,
):
    """Safely acquire a non-blocking per-project file lock below cfg.locks_dir.

    Validates path containment, rejects symlinks, non-regular files, and hardlinks.
    Raises CheckpointLockContentionError on contention.
    """
    locks_dir = cfg.locks_dir
    try:
        locks_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except Exception:  # noqa: BLE001
        raise BoundaryError("Cannot create locks directory.") from None

    # Safe lock filename
    safe_slug = re.sub(r"[^A-Za-z0-9_.-]", "_", project_record.id)
    lock_file = locks_dir / f"checkpoint-{safe_slug}.lock"

    if lock_file.is_symlink():
        raise BoundaryError("Lock path is a symlink, which is not permitted.")

    validated_path = validate_owned_path(lock_file, locks_dir)

    # Rejection of hostile symlink or non-regular files
    if validated_path.is_symlink() or lock_file.is_symlink():
        raise BoundaryError("Lock path is a symlink, which is not permitted.")

    if validated_path.exists():
        if not validated_path.is_file():
            raise BoundaryError("Lock path is not a regular file.")
        st = validated_path.stat()
        if st.st_nlink > 1:
            raise BoundaryError(
                "Lock path has multiple hard links, which is not permitted."
            )

    # Open with O_NOFOLLOW
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        fd = os.open(str(validated_path), flags, 0o600)
    except OSError:
        raise BoundaryError("Failed to open lock file safely.") from None

    try:
        # Revalidate opened file descriptor against non-regular files and hard links
        f_st = os.fstat(fd)
        if not stat.S_ISREG(f_st.st_mode):
            raise BoundaryError("Lock file descriptor is not a regular file.")
        if f_st.st_nlink != 1:
            raise BoundaryError(
                "Lock file descriptor link count must be exactly one."
            )

        # Fresh no-follow stat of the lock path
        try:
            p_st = os.stat(str(validated_path), follow_symlinks=False)
        except OSError:
            raise BoundaryError("Lock path cannot be stat'd safely.") from None

        if not stat.S_ISREG(p_st.st_mode):
            raise BoundaryError("Lock path is not a regular file.")
        if p_st.st_nlink != 1:
            raise BoundaryError("Lock path link count must be exactly one.")

        if (f_st.st_dev, f_st.st_ino) != (p_st.st_dev, p_st.st_ino):
            raise BoundaryError(
                "Lock file descriptor (st_dev, st_ino) mismatch with lock path."
            )

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError, OSError):
            raise CheckpointLockContentionError(
                "Lock contention: project checkpoint lock is held by another process."
            ) from None

        try:
            yield validated_path
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Subprocess Runner & Invocation
# ---------------------------------------------------------------------------


def checkpoint_subprocess_runner(
    argv: tuple[str, ...] | Sequence[str],
    env: Mapping[str, str],
    timeout: float = DEFAULT_CHECKPOINT_TIMEOUT,
    stdin: str = "",
) -> CheckpointRunnerResult:
    """Production no-shell runner with bounded capture and bounded stdin."""
    stdin_bytes = stdin.encode("utf-8")
    if len(stdin_bytes) > MAX_CHECKPOINT_INPUT_BYTES:
        raise ValidationError("Checkpoint input content exceeded maximum allowed size.")

    try:
        with (
            tempfile.TemporaryFile(mode="w+b") as stdout_f,
            tempfile.TemporaryFile(mode="w+b") as stderr_f,
        ):
            proc = subprocess.run(
                list(argv),
                input=stdin_bytes,
                env=dict(env),
                stdout=stdout_f,
                stderr=stderr_f,
                timeout=timeout,
                shell=False,
                check=False,
            )
            stdout_f.seek(0)
            stderr_f.seek(0)
            stdout_bytes = stdout_f.read(MAX_CHECKPOINT_OUTPUT_BYTES + 1)
            stderr_bytes = stderr_f.read(MAX_CHECKPOINT_OUTPUT_BYTES + 1)

            stdout_str = stdout_bytes.decode("utf-8", errors="replace")
            stderr_str = stderr_bytes.decode("utf-8", errors="replace")

            return CheckpointRunnerResult(
                returncode=proc.returncode,
                stdout=stdout_str,
                stderr=stderr_str,
            )
    except (subprocess.TimeoutExpired, TimeoutError):
        raise TimeoutError("Subprocess timed out during execution.") from None
    except Exception:  # noqa: BLE001 - subprocess boundary
        raise RuntimeProbeError("Subprocess execution failed.") from None


def _prepare_runner_call(
    runner: Callable[..., Any],
    argv: tuple[str, ...],
    env: Mapping[str, str],
    timeout: float,
    *,
    stdin_content: str = "",
    operation: str = "write",
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Determine arguments to call runner with based on signature inspection BEFORE invocation."""
    safe_env = MappingProxyType(dict(env))

    try:
        sig = inspect.signature(runner)
    except (ValueError, TypeError):
        if operation == "read":
            return (argv, safe_env, timeout), {}
        return (argv, safe_env, timeout, stdin_content), {}

    if operation == "read":
        # Prefer explicit 3-argument read-runner contract: (argv, env, timeout)
        try:
            sig.bind(argv, safe_env, timeout)
            return (argv, safe_env, timeout), {}
        except TypeError:
            pass

        try:
            sig.bind(argv, safe_env, timeout, stdin_content)
            return (argv, safe_env, timeout, stdin_content), {}
        except TypeError:
            pass

        return (argv, safe_env, timeout), {}

    # operation == "write"
    # Prefer explicit 4-argument write-runner contract: (argv, env, timeout, stdin)
    try:
        sig.bind(argv, safe_env, timeout, stdin_content)
        return (argv, safe_env, timeout, stdin_content), {}
    except TypeError:
        pass

    try:
        sig.bind(argv, safe_env, timeout, stdin=stdin_content)
        return (argv, safe_env, timeout), {"stdin": stdin_content}
    except TypeError:
        pass

    raise RuntimeProbeError("Write runner must accept stdin content.")


def _invoke_runner(
    runner: Callable[..., Any],
    argv: tuple[str, ...],
    env: Mapping[str, str],
    timeout: float,
    *,
    stdin_content: str = "",
    operation: str = "write",
) -> tuple[int, str, str]:
    """Invoke runner callable exactly once without adaptive exception-catching retries."""
    op_label = "write-note" if operation == "write" else "read-note"
    call_args, call_kwargs = _prepare_runner_call(
        runner,
        argv,
        env,
        timeout,
        stdin_content=stdin_content,
        operation=operation,
    )

    try:
        raw_res = runner(*call_args, **call_kwargs)
    except (subprocess.TimeoutExpired, TimeoutError):
        raise RuntimeProbeError(f"Basic Memory {op_label} timed out.") from None
    except Exception:  # noqa: BLE001 - custom runner boundary
        raise RuntimeProbeError(f"Basic Memory {op_label} execution failed.") from None

    try:
        if isinstance(raw_res, tuple):
            if len(raw_res) != 3:
                raise RuntimeProbeError("Runner returned an unexpected result type.") from None
            rc = raw_res[0]
            out = raw_res[1] or ""
            err = raw_res[2] or ""
        elif isinstance(raw_res, (CheckpointRunnerResult, BasicMemoryRunnerResult)):
            rc = raw_res.returncode
            out = raw_res.stdout or ""
            err = raw_res.stderr or ""
        elif hasattr(raw_res, "returncode"):
            rc = raw_res.returncode
            out = getattr(raw_res, "stdout", "") or ""
            err = getattr(raw_res, "stderr", "") or ""
        else:
            raise RuntimeProbeError("Runner returned an unexpected result type.") from None

        if isinstance(rc, bool) or not isinstance(rc, int):
            raise RuntimeProbeError("Runner returned an unexpected result type.") from None

        raw_out_bytes = (
            bytes(out)
            if isinstance(out, (bytes, bytearray))
            else str(out).encode("utf-8")
        )
        if int(rc) == 0 and len(raw_out_bytes) > MAX_CHECKPOINT_OUTPUT_BYTES:
            if operation == "read":
                raise RuntimeProbeError(
                    "Basic Memory read output exceeded maximum allowed size."
                )
            raise RuntimeProbeError(
                "Basic Memory write output exceeded maximum allowed size."
            )

        out_str = (
            out.decode("utf-8", errors="replace")
            if isinstance(out, (bytes, bytearray))
            else str(out)
        )
        err_str = (
            err.decode("utf-8", errors="replace")
            if isinstance(err, (bytes, bytearray))
            else str(err)
        )

        return int(rc), out_str, err_str
    except RuntimeProbeError:
        raise
    except Exception:  # noqa: BLE001
        raise RuntimeProbeError("Runner returned an unexpected result type.") from None


# Alias for backwards compatibility
_invoke_runner_adaptable = _invoke_runner


# ---------------------------------------------------------------------------
# Fingerprint Extraction & Note Inspection
# ---------------------------------------------------------------------------

_FINGERPRINT_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_FRONTMATTER_FINGERPRINT_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL | re.MULTILINE
)
_FINGERPRINT_LINE_RE = re.compile(
    r"^fingerprint:\s*['\"]?([0-9a-fA-F]{64})['\"]?", re.MULTILINE
)
_UPDATED_AT_LINE_RE = re.compile(
    r"^updated_at:\s*['\"]?([^\r\n'\"]+)['\"]?", re.MULTILINE
)


def _extract_existing_fingerprint_and_timestamp(
    raw_stdout_or_data: str | dict[str, Any],
) -> tuple[str | None, str | None]:
    """Extract and validate embedded SHA-256 fingerprint and timestamp from read-note JSON stdout or dict.

    Returns (None, None) if fingerprint is missing, malformed, or untrusted.
    """
    if isinstance(raw_stdout_or_data, dict):
        data = raw_stdout_or_data
    else:
        try:
            data = json.loads(raw_stdout_or_data)
        except json.JSONDecodeError:
            return None, None

    if not isinstance(data, dict):
        return None, None

    fp_val: str | None = None
    updated_val: str | None = None

    # 1. Look in frontmatter dictionary
    frontmatter = data.get("frontmatter")
    if isinstance(frontmatter, dict):
        raw_fp = frontmatter.get("fingerprint")
        if isinstance(raw_fp, str) and _FINGERPRINT_HEX_RE.match(raw_fp.strip().lower()):
            fp_val = raw_fp.strip().lower()

        raw_up = frontmatter.get("updated_at")
        if isinstance(raw_up, str) and raw_up.strip():
            updated_val = raw_up.strip()

    # 2. Look in metadata dictionary
    if fp_val is None:
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            raw_fp = metadata.get("fingerprint")
            if isinstance(raw_fp, str) and _FINGERPRINT_HEX_RE.match(raw_fp.strip().lower()):
                fp_val = raw_fp.strip().lower()
            if updated_val is None:
                raw_up = metadata.get("updated_at")
                if isinstance(raw_up, str) and raw_up.strip():
                    updated_val = raw_up.strip()

    # 3. Fall back to parsing content body frontmatter
    content = data.get("content")
    if isinstance(content, str):
        fm_match = _FRONTMATTER_FINGERPRINT_RE.match(content.lstrip())
        if fm_match:
            fm_text = fm_match.group(1)
            if fp_val is None:
                fp_line_match = _FINGERPRINT_LINE_RE.search(fm_text)
                if fp_line_match:
                    candidate = fp_line_match.group(1).strip().lower()
                    if _FINGERPRINT_HEX_RE.match(candidate):
                        fp_val = candidate

            if updated_val is None:
                up_line_match = _UPDATED_AT_LINE_RE.search(fm_text)
                if up_line_match:
                    updated_val = up_line_match.group(1).strip()

    if updated_val is None:
        raw_up = data.get("updated_at")
        if isinstance(raw_up, str) and raw_up.strip():
            updated_val = raw_up.strip()

    return fp_val, updated_val


# ---------------------------------------------------------------------------
# Pure Planner & Execution
# ---------------------------------------------------------------------------


def plan_checkpoint_write(
    cfg: PersonalTidewayConfig,
    request: CheckpointRequest,
    registry: ProjectRegistry | None = None,
) -> CheckpointPlan:
    """Pure planner constructing an immutable CheckpointPlan.

    Performs zero filesystem mutations, zero runner calls, zero executable probes,
    and zero lock creation.
    """
    if cfg.home.is_symlink():
        raise BoundaryError("Workspace root is a symlink, which is not permitted.")

    layout = get_basic_memory_layout(cfg)
    reg = registry if registry is not None else load_registry(cfg.projects_yaml)
    project_record = resolve_registered_project(request.project, reg)

    project_name = project_record.memory.project_name
    exec_str = str(layout.primary_executable)

    # Basic Memory 0.23.2 contracts
    write_argv = (
        exec_str,
        "tool",
        "write-note",
        "--title",
        CURRENT_STATE_TITLE,
        "--folder",
        CURRENT_STATE_FOLDER,
        "--project",
        project_name,
        "--overwrite",
        "--local",
    )

    read_argv = build_read_note_argv(
        layout.primary_executable,
        project_name,
        CURRENT_STATE_PERMALINK,
    )

    fingerprint = compute_semantic_fingerprint(request.payload)
    env_overrides = compute_canonical_basic_memory_env_overrides(layout)

    return CheckpointPlan(
        request=request,
        layout=layout,
        executable=layout.primary_executable,
        project_record=project_record,
        write_argv=write_argv,
        read_argv=read_argv,
        fingerprint=fingerprint,
        env_overrides=env_overrides,
        timeout=request.timeout,
    )


def execute_checkpoint_plan(
    plan: CheckpointPlan,
    *,
    dry_run: bool = False,
    cfg: PersonalTidewayConfig,
    runner: BasicMemoryRunner | None = None,
    write_runner: BasicMemoryRunner | None = None,
    read_runner: BasicMemoryRunner | None = None,
    clock: Callable[[], str | datetime] | None = None,
) -> CheckpointResult:
    """Execute a CheckpointPlan inside a per-project lock with full idempotence."""
    project_record = plan.project_record
    project_id = project_record.id
    project_name = project_record.memory.project_name
    fingerprint = plan.fingerprint

    if dry_run:
        # Dry-run: zero probes, zero clock calls, zero locks, zero mutations
        preview = plan.preview()
        return CheckpointResult(
            project_id=project_id,
            project_name=project_name,
            fingerprint=fingerprint,
            written=False,
            dry_run=True,
            timestamp="",
            preview=preview,
        )

    # Validate executable isolation before entering write critical section
    try:
        is_valid = validate_isolated_executable(
            plan.layout.primary_executable,
            service_root=plan.layout.service_root,
            home_boundary=cfg.home,
        )
    except (BoundaryError, RuntimeProbeError):
        raise
    except Exception:  # noqa: BLE001 - probe boundary
        raise RuntimeProbeError("Basic Memory executable missing or invalid.") from None

    if not is_valid:
        raise RuntimeProbeError("Basic Memory executable missing or invalid.") from None

    active_read_runner = read_runner if read_runner is not None else runner
    if active_read_runner is None:
        active_read_runner = checkpoint_subprocess_runner

    active_write_runner = write_runner if write_runner is not None else runner
    if active_write_runner is None:
        active_write_runner = checkpoint_subprocess_runner

    subprocess_env = build_subprocess_env(plan.env_overrides)

    # Enter critical section with per-project concurrency lock
    with acquire_project_checkpoint_lock(cfg, project_record):
        # 1. Read existing canonical state inside the lock
        existing_fp: str | None = None
        existing_timestamp: str | None = None

        rc, stdout, _ = _invoke_runner(
            active_read_runner,
            plan.read_argv,
            subprocess_env,
            plan.timeout,
            operation="read",
        )

        if rc != 0:
            raise RuntimeProbeError(
                "Basic Memory read-note failed with non-zero exit code."
            )

        try:
            stdout_bytes = stdout.encode("utf-8")
        except UnicodeError:
            raise RuntimeProbeError(
                "Basic Memory read output contains invalid encoding."
            ) from None

        if len(stdout_bytes) > MAX_CHECKPOINT_OUTPUT_BYTES:
            raise RuntimeProbeError(
                "Basic Memory read output exceeded maximum allowed size."
            )

        try:
            read_data = json.loads(stdout)
        except json.JSONDecodeError:
            raise RuntimeProbeError(
                "Basic Memory read-note returned malformed JSON."
            ) from None

        if not isinstance(read_data, dict):
            raise RuntimeProbeError(
                "Basic Memory read-note response must be a JSON object."
            )

        # Verified Basic Memory 0.23.2 behavior: a missing note returns rc=0 with JSON
        # {"title":null,"permalink":null,"file_path":null,"content":null,"frontmatter":null}.
        # rc=0/null-content means missing and may proceed.
        if read_data.get("content") is not None:
            existing_fp, existing_timestamp = (
                _extract_existing_fingerprint_and_timestamp(read_data)
            )
        else:
            existing_fp = None
            existing_timestamp = None

        # 2. Idempotence check: if valid embedded fingerprint matches, skip write!
        if existing_fp is not None and existing_fp == fingerprint:
            # Identical repeat: do NOT obtain clock, do NOT invoke write
            return CheckpointResult(
                project_id=project_id,
                project_name=project_name,
                fingerprint=fingerprint,
                written=False,
                dry_run=False,
                timestamp=existing_timestamp or "",
                preview=plan.preview(),
            )

        # 3. Changed or new checkpoint: obtain clock strictly once
        if clock is not None:
            raw_time = clock()
            if isinstance(raw_time, datetime):
                # Ensure UTC
                if raw_time.tzinfo is None:
                    utc_dt = raw_time.replace(tzinfo=UTC)
                else:
                    utc_dt = raw_time.astimezone(UTC)
                timestamp_str = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            elif isinstance(raw_time, str):
                timestamp_str = raw_time.strip()
            else:
                raise ValidationError("Clock must return a datetime or ISO timestamp string.")
        else:
            timestamp_str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Validate timestamp format
        if not timestamp_str or any(c in timestamp_str for c in ("\n", "\r", "\x00")):
            raise ValidationError("Invalid timestamp generated for checkpoint.")

        # 4. Render Markdown deterministically
        rendered = render_checkpoint_markdown(
            plan.request.payload,
            project_name=project_name,
            timestamp=timestamp_str,
            fingerprint=fingerprint,
        )

        # Strict character budget check
        if len(rendered) > plan.request.max_chars:
            raise ValidationError(
                f"Rendered checkpoint exceeds character budget of {plan.request.max_chars} characters."
            )

        # 5. Execute write runner with bounded stdin
        try:
            rc, stdout, _stderr = _invoke_runner(
                active_write_runner,
                plan.write_argv,
                subprocess_env,
                plan.timeout,
                stdin_content=rendered,
                operation="write",
            )
        except RuntimeProbeError:
            raise
        except Exception:  # noqa: BLE001 - runner boundary
            raise RuntimeProbeError("Basic Memory write-note execution failed.") from None

        if rc != 0:
            raise RuntimeProbeError(
                "Basic Memory write-note failed with non-zero exit code."
            )

        # Check stdout size limit
        try:
            stdout_bytes = stdout.encode("utf-8")
        except UnicodeError:
            raise RuntimeProbeError("Basic Memory write output contains invalid encoding.") from None

        if len(stdout_bytes) > MAX_CHECKPOINT_OUTPUT_BYTES:
            raise RuntimeProbeError(
                "Basic Memory write output exceeded maximum allowed size."
            )

        return CheckpointResult(
            project_id=project_id,
            project_name=project_name,
            fingerprint=fingerprint,
            written=True,
            dry_run=False,
            timestamp=timestamp_str,
            rendered_text=rendered,
            preview=plan.preview(),
        )


def write_checkpoint(
    cfg: PersonalTidewayConfig,
    request: CheckpointRequest,
    *,
    dry_run: bool = False,
    runner: BasicMemoryRunner | None = None,
    write_runner: BasicMemoryRunner | None = None,
    read_runner: BasicMemoryRunner | None = None,
    clock: Callable[[], str | datetime] | None = None,
    registry: ProjectRegistry | None = None,
) -> CheckpointResult:
    """Plan and execute a concise idempotent checkpoint write in one step."""
    plan = plan_checkpoint_write(cfg, request, registry=registry)
    return execute_checkpoint_plan(
        plan,
        dry_run=dry_run,
        cfg=cfg,
        runner=runner,
        write_runner=write_runner,
        read_runner=read_runner,
        clock=clock,
    )


__all__ = [
    "CHECKPOINT_VALID_CLIENTS",
    "CHECKPOINT_VALID_STATUSES",
    "CURRENT_STATE_FOLDER",
    "CURRENT_STATE_PERMALINK",
    "CURRENT_STATE_TITLE",
    "DEFAULT_CHECKPOINT_TIMEOUT",
    "DEFAULT_MAX_CHECKPOINT_CHARS",
    "HARD_MAX_CHECKPOINT_CHARS",
    "MAX_CHECKPOINT_INPUT_BYTES",
    "MAX_CHECKPOINT_OUTPUT_BYTES",
    "MAX_COLLECTION_ITEMS",
    "MAX_FIELD_CHARS",
    "MAX_ITEM_CHARS",
    "MIN_CHECKPOINT_CHARS",
    "CheckpointLockContentionError",
    "CheckpointPayload",
    "CheckpointPlan",
    "CheckpointPlanPreview",
    "CheckpointRequest",
    "CheckpointResult",
    "CheckpointRunnerResult",
    "CheckpointSecretError",
    "acquire_project_checkpoint_lock",
    "checkpoint_subprocess_runner",
    "compute_semantic_fingerprint",
    "execute_checkpoint_plan",
    "plan_checkpoint_write",
    "render_checkpoint_markdown",
    "write_checkpoint",
]
