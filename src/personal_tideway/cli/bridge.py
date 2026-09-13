"""Safe CLI bridge module for Personal Tideway context retrieval and checkpoints.

Phase 4A implementation:
- Strict project resolution precedence: command-local --project > global/config > strict cwd resolution.
- Fail-closed cwd resolution refusing auto-registration for candidates, ambiguous, or unresolved projects.
- Safe, bounded reading of checkpoint input from regular file or standard input.
- File validation protecting against symlinks, non-regular files, path races, and TOCTOU hazards.
- Input bounds (64 KiB conservative cap) checked before JSON decoding to prevent memory exhaustion.
- Strict payload schema validation rejecting unknown fields and verifying domain constraints.
- Output formatting for human-readable text and safe single-document JSON representation.
- Absolute prevention of secret, host path, raw stderr, or payload leakage in errors.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
from pathlib import Path

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import CLIENT_CODEX
from personal_tideway.core.checkpoint import (
    MAX_CHECKPOINT_INPUT_BYTES,
    CheckpointPayload,
    CheckpointResult,
)
from personal_tideway.core.context_retrieval import (
    ContextRetrievalResult,
    resolve_registered_project,
)
from personal_tideway.core.project_resolver import (
    GitProbeResult,
    probe_git,
    resolve_project,
)
from personal_tideway.core.registry import ProjectRecord, ProjectRegistry, load_registry
from personal_tideway.exceptions import ValidationError

ALLOWED_CHECKPOINT_FIELDS: frozenset[str] = frozenset({
    "condition",
    "objective",
    "completed",
    "blockers",
    "verification_status",
    "next_safe_action",
    "source_client",
    "evidence",
})


def resolve_cli_project(
    cfg: PersonalTidewayConfig,
    command_project: str | None = None,
    registry: ProjectRegistry | None = None,
    cwd: Path | None = None,
) -> ProjectRecord:
    """Resolve project record for CLI operations following strict precedence:

    1. Command-local explicit --project PROJECT
    2. Global/config selection (cfg.project)
    3. Strict current working directory resolution (never auto-registering)

    Fails closed on missing, candidate, ambiguous, probe uncertainty, or unresolved projects.
    """
    reg = registry if registry is not None else load_registry(cfg.projects_yaml)

    # 1. Command-local --project
    if command_project is not None:
        clean = command_project.strip()
        if not clean:
            raise ValidationError("Project identifier cannot be empty.")
        return resolve_registered_project(clean, reg)

    # 2. Global/config selection (cfg.project)
    if cfg.project is not None and cfg.project.strip():
        return resolve_registered_project(cfg.project.strip(), reg)

    # 3. Strict current working directory resolution
    target_path = (cwd if cwd is not None else Path.cwd()).resolve()
    # Resolve path bindings through the canonical resolver without invoking Git.
    pre_resolution = resolve_project(
        registry=reg,
        path=target_path,
        probe=GitProbeResult(is_git=False),
    )
    if pre_resolution.status == "resolved" and pre_resolution.project is not None:
        return resolve_registered_project(pre_resolution.project.id, reg)

    # Path bindings did not resolve. Perform one bounded/read-only Git probe.
    probe = probe_git(target_path, timeout=3.0)

    # Fail closed on probe uncertainty
    if probe.status == "probe_error":
        raise ValidationError("Git probe failed for current directory.")

    res = resolve_project(registry=reg, path=target_path, probe=probe)

    if res.status == "resolved" and res.project is not None:
        return resolve_registered_project(res.project.id, reg)
    elif res.status == "ambiguous":
        matches_str = f" ({', '.join(sorted(res.ambiguous_project_ids))})" if res.ambiguous_project_ids else ""
        raise ValidationError(
            f"Ambiguous project resolution for current directory: matches multiple projects{matches_str}."
        )
    elif res.status == "candidate":
        raise ValidationError(
            "Current directory is an unregistered project candidate. Auto-registration is not allowed."
        )
    else:  # "unresolved" or anything else
        raise ValidationError("No registered project found for current directory.")


def read_checkpoint_file(
    file_path: str | Path,
    max_bytes: int = MAX_CHECKPOINT_INPUT_BYTES,
) -> str:
    """Safely read and validate a regular checkpoint file without TOCTOU or symlink hazards.

    Binds reading to max_bytes before decoding to prevent memory exhaustion.
    Rejects symlinks, directories, non-regular files, hard links > 1, and malformed UTF-8.
    Never leaks host paths in exceptions.
    """
    if not file_path:
        raise ValidationError("Checkpoint input file path cannot be empty.")

    p = Path(file_path)

    # 1. Pre-open symlink check
    try:
        if p.is_symlink():
            raise ValidationError("Checkpoint input file cannot be a symlink.")
    except (OSError, ValueError):
        raise ValidationError("Checkpoint input file cannot be a symlink.")

    # 2. Open non-blocking with O_RDONLY, O_CLOEXEC, O_NONBLOCK, and O_NOFOLLOW
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    fd: int | None = None
    try:
        try:
            fd = os.open(p, flags)
        except FileNotFoundError:
            raise ValidationError("Checkpoint input file does not exist.") from None
        except PermissionError:
            raise ValidationError("Checkpoint input file cannot be accessed due to permissions.") from None
        except OSError as e:
            if e.errno in (errno.ELOOP, getattr(errno, "EMLINK", 31)):
                raise ValidationError("Checkpoint input file cannot be a symlink.") from None
            raise ValidationError("Checkpoint input file cannot be opened safely.") from None

        # 3. Descriptor stat
        try:
            st_fd = os.fstat(fd)
        except OSError:
            raise ValidationError("Checkpoint input file cannot be inspected safely.") from None

        if not stat.S_ISREG(st_fd.st_mode):
            raise ValidationError("Checkpoint input file must be a regular file.")

        if st_fd.st_nlink > 1:
            raise ValidationError("Checkpoint input file cannot have multiple hard links.")

        # 4. Post-open path lstat comparison to prevent TOCTOU race
        try:
            st_path = os.lstat(p)
        except OSError:
            raise ValidationError("Checkpoint input file changed during verification.") from None

        if stat.S_ISLNK(st_path.st_mode):
            raise ValidationError("Checkpoint input file cannot be a symlink.")

        if st_fd.st_dev != st_path.st_dev or st_fd.st_ino != st_path.st_ino:
            raise ValidationError("Checkpoint input file changed during verification.")

        if st_path.st_nlink > 1:
            raise ValidationError("Checkpoint input file cannot have multiple hard links.")

        if st_fd.st_size > max_bytes:
            raise ValidationError(
                f"Checkpoint input file exceeds maximum allowed size of {max_bytes} bytes."
            )

        with os.fdopen(fd, "rb") as f:
            fd = None  # fdopen owns the fd now
            raw_bytes = f.read(max_bytes + 1)
            if len(raw_bytes) > max_bytes:
                raise ValidationError(
                    f"Checkpoint input file exceeds maximum allowed size of {max_bytes} bytes."
                )
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    if not raw_bytes or not raw_bytes.strip():
        raise ValidationError("Checkpoint input file is empty.")

    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("Checkpoint input file is not valid UTF-8.") from None


def read_checkpoint_stdin(
    max_bytes: int = MAX_CHECKPOINT_INPUT_BYTES,
) -> str:
    """Safely read bounded UTF-8 checkpoint input from standard input once.

    Hard byte limit enforced before JSON decoding to prevent memory exhaustion.
    Never leaks payloads or input in exceptions.
    """
    stdin = sys.stdin
    has_buffer = hasattr(stdin, "buffer") and stdin.buffer is not None

    if has_buffer:
        try:
            raw_bytes = stdin.buffer.read(max_bytes + 1)
        except (AttributeError, OSError, ValueError):
            raise ValidationError("Failed to read checkpoint input from stdin.") from None

        if len(raw_bytes) > max_bytes:
            raise ValidationError(
                f"Checkpoint input exceeds maximum allowed size of {max_bytes} bytes."
            )
        if not raw_bytes or not raw_bytes.strip():
            raise ValidationError("Checkpoint input cannot be empty.")
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise ValidationError("Checkpoint input is not valid UTF-8.") from None

    # Text stream fallback (e.g. mocked in unit tests without a buffer attribute)
    try:
        text = stdin.read(max_bytes + 1)
    except (AttributeError, OSError, ValueError):
        raise ValidationError("Failed to read checkpoint input from stdin.") from None

    if not text or not text.strip():
        raise ValidationError("Checkpoint input cannot be empty.")

    try:
        raw = text.encode("utf-8")
    except (UnicodeEncodeError, UnicodeError):
        raise ValidationError("Checkpoint input contains invalid Unicode encoding.") from None

    if len(raw) > max_bytes:
        raise ValidationError(
            f"Checkpoint input exceeds maximum allowed size of {max_bytes} bytes."
        )
    return text


def parse_checkpoint_payload(raw_json: str) -> CheckpointPayload:
    """Parse JSON string and validate CheckpointPayload schema, rejecting unknown fields."""
    if not isinstance(raw_json, str) or not raw_json.strip():
        raise ValidationError("Checkpoint input cannot be empty.")

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        raise ValidationError("Checkpoint input is not valid JSON.") from None

    if not isinstance(data, dict):
        raise ValidationError("Checkpoint JSON input must be a JSON object.")

    unknown_fields = sorted(set(data.keys()) - ALLOWED_CHECKPOINT_FIELDS)
    if unknown_fields:
        raise ValidationError(
            f"Unknown field(s) in checkpoint input: {', '.join(unknown_fields)}."
        )

    if "condition" not in data:
        raise ValidationError("Missing required field 'condition' in checkpoint input.")
    if "objective" not in data:
        raise ValidationError("Missing required field 'objective' in checkpoint input.")

    return CheckpointPayload(
        condition=data["condition"],
        objective=data["objective"],
        completed=data.get("completed", ()),
        blockers=data.get("blockers", ()),
        verification_status=data.get("verification_status", "verified"),
        next_safe_action=data.get("next_safe_action", ""),
        source_client=data.get("source_client", CLIENT_CODEX),
        evidence=data.get("evidence", ()),
    )


def format_context_result(
    result: ContextRetrievalResult,
    *,
    as_json: bool = False,
    query: str | None = None,
) -> str:
    """Format ContextRetrievalResult for stdout display."""
    if as_json:
        return json.dumps(result.to_dict(), indent=2, ensure_ascii=False)

    if result.bundle.rendered_text:
        return result.bundle.rendered_text

    if query:
        return "No matching context notes found."
    return f"No current-state context found for project '{result.bundle.project_name}'."


def format_checkpoint_result(
    result: CheckpointResult,
    *,
    as_json: bool = False,
    source_client: str | None = None,
) -> str:
    """Format CheckpointResult for stdout display."""
    if as_json:
        doc = result.to_dict()
        if source_client is not None:
            doc["source_client"] = source_client
        return json.dumps(doc, indent=2, ensure_ascii=False)

    if result.dry_run:
        return (
            f"[DRY RUN] Checkpoint preview for project {result.project_name} "
            f"({result.project_id}): fingerprint={result.fingerprint}"
        )
    elif result.written:
        return (
            f"Checkpoint written for project {result.project_name} "
            f"({result.project_id}): fingerprint={result.fingerprint}"
        )
    else:
        return (
            f"Checkpoint unchanged for project {result.project_name} "
            f"({result.project_id}): fingerprint={result.fingerprint}"
        )


__all__ = [
    "ALLOWED_CHECKPOINT_FIELDS",
    "MAX_CHECKPOINT_INPUT_BYTES",
    "format_checkpoint_result",
    "format_context_result",
    "parse_checkpoint_payload",
    "read_checkpoint_file",
    "read_checkpoint_stdin",
    "resolve_cli_project",
]
