"""Antigravity CLI (agy) lifecycle hook management and PreInvocation handler.

Phase 4C-A implementation:
- Exact managed named hook definition 'personal-tideway' for agy's PreInvocation event.
- PreInvocation fires before model calls; invocationNum == 0 is the first model invocation in the current invocation sequence/execution loop.
- Lifecycle management: plan, install, remove, and status.
- Idempotency, non-overwrite, preserving unrelated hooks and keys semantically.
- Strict symlink and path-boundary protections.
- Safe PreInvocation handler: retrieves bounded context on invocationNum == 0, returns empty JSON on later invocations.
- Fail-closed behavior with sanitized error messages and zero leaks of host paths, transcripts, or secrets.
"""

from __future__ import annotations

import copy
import json
import stat
import sys
from pathlib import Path
from typing import Any

from personal_tideway.adapters.agy import AgyAdapter, inspect_hooks_path
from personal_tideway.cli.bridge import format_context_result, resolve_cli_project
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import (
    AGY_HOOK_COMMAND,
    AGY_HOOK_EVENT,
    AGY_HOOK_NAME,
    AGY_HOOK_TIMEOUT,
    CLIENT_AGY,
    MAX_HOOK_INPUT_BYTES,
    ExitCode,
)
from personal_tideway.core.basic_memory_installer import BasicMemoryRunner
from personal_tideway.core.context_retrieval import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_ITEMS,
    ContextRetrievalRequest,
    retrieve_context,
)
from personal_tideway.exceptions import (
    BoundaryError,
    ValidationError,
)
from personal_tideway.models import AgyHookEvidence, HookStatus
from personal_tideway.utils import ensure_safe_path


def get_canonical_agy_hook_entry() -> dict[str, Any]:
    """Return canonical JSON dictionary for the personal-tideway agy hook."""
    return {
        AGY_HOOK_EVENT: [
            {
                "type": "command",
                "command": AGY_HOOK_COMMAND,
                "timeout": AGY_HOOK_TIMEOUT,
            }
        ]
    }


def is_canonical_agy_hook_entry(entry: Any) -> bool:
    """Verify if a hook definition strictly matches the canonical definition."""
    if not isinstance(entry, dict):
        return False

    # Optional "enabled" field must be True if present
    if "enabled" in entry and entry["enabled"] is not True:
        return False

    allowed_keys = {AGY_HOOK_EVENT}
    if "enabled" in entry:
        allowed_keys.add("enabled")

    if set(entry.keys()) != allowed_keys:
        return False

    handlers = entry.get(AGY_HOOK_EVENT)
    if not isinstance(handlers, list) or len(handlers) != 1:
        return False

    h = handlers[0]
    if not isinstance(h, dict):
        return False

    # Command must match exactly
    if h.get("command") != AGY_HOOK_COMMAND:
        return False

    # Type defaults to "command"
    if h.get("type", "command") != "command":
        return False

    # Timeout defaults to 30 or must be 30
    return h.get("timeout", AGY_HOOK_TIMEOUT) == AGY_HOOK_TIMEOUT


def get_agy_adapter(cfg: PersonalTidewayConfig) -> AgyAdapter:
    """Create AgyAdapter with configured paths."""
    return AgyAdapter(
        config_path=cfg.agy_config,
        rules_path=cfg.agy_rules,
        skills_path=cfg.agy_skills,
        backups_dir=cfg.backups_dir,
        hooks_path=cfg.agy_hooks,
    )


def validate_hooks_file_safety(hooks_path: Path, gemini_home: Path) -> None:
    """Validate path boundary and symlink safety without modifying files."""
    try:
        lstat_res = hooks_path.lstat()
        if stat.S_ISLNK(lstat_res.st_mode):
            raise ValidationError(f"Agy hooks path cannot be a symlink: {hooks_path}")
    except FileNotFoundError:
        pass
    except OSError:
        raise ValidationError(f"Failed to read agy hooks file {hooks_path}.")

    # Check that parent directory does not escape through symlinks
    ensure_safe_path(hooks_path, gemini_home, label="Agy hooks path", follow_symlinks=False)


def get_agy_hook_status(cfg: PersonalTidewayConfig) -> AgyHookEvidence:
    """Inspect agy hooks.json and return typed structural evidence."""
    hooks_path = cfg.agy_hooks
    try:
        validate_hooks_file_safety(hooks_path, cfg.gemini_home)
    except (BoundaryError, ValidationError) as e:
        return AgyHookEvidence(
            status=HookStatus.CONFLICT,
            event=AGY_HOOK_EVENT,
            command=AGY_HOOK_COMMAND,
            target_path=hooks_path,
            installed=False,
            details=f"Path boundary or symlink hazard: {e}",
            conflict_reason=str(e),
        )

    try:
        exists, _ = inspect_hooks_path(hooks_path)
    except ValidationError as e:
        return AgyHookEvidence(
            status=HookStatus.CONFLICT,
            event=AGY_HOOK_EVENT,
            command=AGY_HOOK_COMMAND,
            target_path=hooks_path,
            installed=False,
            details=f"Invalid hooks configuration: {e}",
            conflict_reason=str(e),
        )

    if not exists:
        return AgyHookEvidence(
            status=HookStatus.NOT_INSTALLED,
            event=AGY_HOOK_EVENT,
            command=AGY_HOOK_COMMAND,
            target_path=hooks_path,
            installed=False,
            details=f"Agy hooks file does not exist at {hooks_path}",
            conflict_reason=None,
        )

    adapter = get_agy_adapter(cfg)
    try:
        doc = adapter.read_hooks()
    except ValidationError as e:
        return AgyHookEvidence(
            status=HookStatus.CONFLICT,
            event=AGY_HOOK_EVENT,
            command=AGY_HOOK_COMMAND,
            target_path=hooks_path,
            installed=False,
            details=f"Invalid hooks configuration: {e}",
            conflict_reason=str(e),
        )

    if AGY_HOOK_NAME not in doc:
        return AgyHookEvidence(
            status=HookStatus.NOT_INSTALLED,
            event=AGY_HOOK_EVENT,
            command=AGY_HOOK_COMMAND,
            target_path=hooks_path,
            installed=False,
            details=f"Hook '{AGY_HOOK_NAME}' is not present in {hooks_path}",
            conflict_reason=None,
        )

    entry = doc[AGY_HOOK_NAME]
    if is_canonical_agy_hook_entry(entry):
        return AgyHookEvidence(
            status=HookStatus.INSTALLED,
            event=AGY_HOOK_EVENT,
            command=AGY_HOOK_COMMAND,
            target_path=hooks_path,
            installed=True,
            details=f"Managed PreInvocation hook '{AGY_HOOK_NAME}' is installed and matches canonical definition.",
            conflict_reason=None,
        )
    else:
        return AgyHookEvidence(
            status=HookStatus.CONFLICT,
            event=AGY_HOOK_EVENT,
            command=AGY_HOOK_COMMAND,
            target_path=hooks_path,
            installed=False,
            details=f"Conflicting modified '{AGY_HOOK_NAME}' hook entry detected in {hooks_path}",
            conflict_reason="Hook entry exists but does not match canonical definition",
        )


def plan_agy_hook(cfg: PersonalTidewayConfig) -> dict[str, Any]:
    """Preview actions required to install the managed agy hook without modifying files."""
    evidence = get_agy_hook_status(cfg)
    hooks_path = cfg.agy_hooks

    if evidence.status == HookStatus.INSTALLED:
        return {
            "client": CLIENT_AGY,
            "target_path": str(hooks_path),
            "status": evidence.status.value,
            "action": "none",
            "message": f"Managed PreInvocation hook '{AGY_HOOK_NAME}' is already installed in {hooks_path}. No changes needed.",
        }
    elif evidence.status == HookStatus.CONFLICT:
        return {
            "client": CLIENT_AGY,
            "target_path": str(hooks_path),
            "status": evidence.status.value,
            "action": "conflict",
            "message": f"CONFLICT: Conflicting hook configuration detected in {hooks_path}: {evidence.details}. Installation blocked.",
            "conflict_reason": evidence.conflict_reason,
        }
    else:
        try:
            file_exists, _ = inspect_hooks_path(hooks_path)
        except ValidationError as e:
            return {
                "client": CLIENT_AGY,
                "target_path": str(hooks_path),
                "status": HookStatus.CONFLICT.value,
                "action": "conflict",
                "message": f"CONFLICT: Conflicting hook configuration detected in {hooks_path}: {e}. Installation blocked.",
                "conflict_reason": str(e),
            }
        action = "update" if file_exists else "create"
        msg = (
            f"Would add managed PreInvocation hook '{AGY_HOOK_NAME}' to existing {hooks_path} (preserving unrelated keys)."
            if file_exists
            else f"Would create {hooks_path} and install managed PreInvocation hook '{AGY_HOOK_NAME}'."
        )
        return {
            "client": CLIENT_AGY,
            "target_path": str(hooks_path),
            "status": evidence.status.value,
            "action": action,
            "message": msg,
        }


def install_agy_hook(
    cfg: PersonalTidewayConfig,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Install canonical agy PreInvocation hook into hooks.json.

    Idempotent: returns (False, msg) if already installed.
    Refuses malformed JSON, symlinks, or conflicting entries.
    Preserves all other top-level keys and hooks.
    """
    hooks_path = cfg.agy_hooks
    validate_hooks_file_safety(hooks_path, cfg.gemini_home)

    adapter = get_agy_adapter(cfg)
    doc = adapter.read_hooks()

    if AGY_HOOK_NAME in doc:
        if is_canonical_agy_hook_entry(doc[AGY_HOOK_NAME]):
            return False, f"Managed PreInvocation hook '{AGY_HOOK_NAME}' is already installed in {hooks_path} (unchanged)."
        else:
            raise ValidationError(
                f"Conflicting modified '{AGY_HOOK_NAME}' hook entry detected in {hooks_path}; refusing to overwrite."
            )

    # Clone document to preserve existing keys semantically
    new_doc = copy.deepcopy(doc)
    new_doc[AGY_HOOK_NAME] = get_canonical_agy_hook_entry()

    prefix = "[DRY RUN] " if dry_run else ""
    changed = adapter.write_hooks(new_doc, dry_run=dry_run)
    action_verb = "Would install" if dry_run else "Installed"
    return changed, f"{prefix}{action_verb} managed PreInvocation hook '{AGY_HOOK_NAME}' in {hooks_path}."


def remove_agy_hook(
    cfg: PersonalTidewayConfig,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Remove managed agy PreInvocation hook from hooks.json.

    Idempotent: returns (False, msg) if not installed.
    Refuses malformed JSON, symlinks, or conflicting entries.
    Preserves all other top-level keys and hooks.
    """
    hooks_path = cfg.agy_hooks
    validate_hooks_file_safety(hooks_path, cfg.gemini_home)

    exists, _ = inspect_hooks_path(hooks_path)
    if not exists:
        prefix = "[DRY RUN] " if dry_run else ""
        return False, f"{prefix}Managed PreInvocation hook '{AGY_HOOK_NAME}' is not installed in {hooks_path} (unchanged)."

    adapter = get_agy_adapter(cfg)
    doc = adapter.read_hooks()

    if AGY_HOOK_NAME not in doc:
        prefix = "[DRY RUN] " if dry_run else ""
        return False, f"{prefix}Managed PreInvocation hook '{AGY_HOOK_NAME}' is not installed in {hooks_path} (unchanged)."

    if not is_canonical_agy_hook_entry(doc[AGY_HOOK_NAME]):
        raise ValidationError(
            f"Conflicting modified '{AGY_HOOK_NAME}' hook entry detected in {hooks_path}; refusing to remove."
        )

    new_doc = copy.deepcopy(doc)
    del new_doc[AGY_HOOK_NAME]

    prefix = "[DRY RUN] " if dry_run else ""
    changed = adapter.write_hooks(new_doc, dry_run=dry_run)
    action_verb = "Would remove" if dry_run else "Removed"
    return changed, f"{prefix}{action_verb} managed PreInvocation hook '{AGY_HOOK_NAME}' from {hooks_path}."


def read_hook_payload_from_stream(stream: Any) -> str:
    """Read bounded UTF-8 hook payload from a stream."""
    raw_bytes = bytearray()
    raw_buffer = getattr(stream, "buffer", stream)

    while True:
        chunk = raw_buffer.read(min(4096, MAX_HOOK_INPUT_BYTES - len(raw_bytes) + 1))
        if not chunk:
            break
        raw_bytes.extend(chunk if isinstance(chunk, (bytes, bytearray)) else chunk.encode("utf-8"))
        if len(raw_bytes) > MAX_HOOK_INPUT_BYTES:
            raise ValidationError("Hook payload exceeds maximum allowed size.")

    if not raw_bytes:
        raise ValidationError("Hook payload is empty.")

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("Malformed UTF-8 in hook payload.")

    if not text.strip():
        raise ValidationError("Hook payload is empty.")

    return text


def handle_agy_preinvocation(
    cfg: PersonalTidewayConfig,
    *,
    raw_payload: str | None = None,
    stdin_stream: Any = None,
    runner: BasicMemoryRunner | None = None,
) -> tuple[int, dict[str, Any]]:
    """Execute agy PreInvocation hook handler.

    PreInvocation fires before model calls; invocationNum == 0 is the first model invocation
    in the current invocation sequence/execution loop.

    - Reads agy protojson camelCase payload from stdin or raw_payload (strictly bounded).
    - If invocationNum > 0: returns empty injectSteps (no-op) immediately without reading context.
    - If invocationNum == 0: requires workspacePaths to be a non-empty JSON array containing exactly one
      non-empty absolute path, resolves workspace context project (failing closed on unregistered/candidate/ambiguous),
      retrieves bounded Personal Tideway context, and returns injectSteps with single ephemeralMessage.
    - Fails closed on any errors with sanitized messages.
    """
    if raw_payload is not None:
        if not isinstance(raw_payload, str):
            raise ValidationError("Hook payload must be a string.")
        if not raw_payload.strip():
            raise ValidationError("Hook payload is empty.")
        raw_bytes = raw_payload.encode("utf-8")
        if len(raw_bytes) > MAX_HOOK_INPUT_BYTES:
            raise ValidationError("Hook payload exceeds maximum allowed size.")
        payload_text = raw_payload
    else:
        stream = stdin_stream if stdin_stream is not None else sys.stdin
        payload_text = read_hook_payload_from_stream(stream)

    try:
        data = json.loads(payload_text)
    except json.JSONDecodeError as e:
        raise ValidationError(f"Malformed JSON in hook payload: {e}") from e

    if not isinstance(data, dict):
        raise ValidationError("Hook payload must be a JSON object.")

    if "invocationNum" not in data:
        raise ValidationError("Missing required 'invocationNum' in hook payload.")

    inv_num = data["invocationNum"]
    if isinstance(inv_num, bool) or not isinstance(inv_num, int) or inv_num < 0:
        raise ValidationError("Invalid 'invocationNum' in hook payload.")

    # invocationNum > 0: Valid no-op response
    if inv_num > 0:
        return ExitCode.SUCCESS, {"injectSteps": []}

    # invocationNum == 0: retrieve bounded initial context
    # 1. Require workspacePaths to be a non-empty JSON array containing exactly one non-empty absolute path.
    # Reject missing, null, wrong-type, empty, relative, malformed, or multiple paths before project resolution or backend call.
    if "workspacePaths" not in data or data["workspacePaths"] is None:
        raise ValidationError("Missing required 'workspacePaths' in hook payload.")

    ws_paths = data["workspacePaths"]
    if not isinstance(ws_paths, list):
        raise ValidationError("Invalid 'workspacePaths' in hook payload: expected a JSON array.")

    if len(ws_paths) == 0:
        raise ValidationError("Invalid 'workspacePaths' in hook payload: workspacePaths cannot be empty.")

    if len(ws_paths) != 1:
        raise ValidationError("Multi-root workspaces are not supported: exactly one workspace path is required.")

    raw_path = ws_paths[0]
    if not isinstance(raw_path, str):
        raise ValidationError("Invalid workspace path in hook payload: path must be a string.")

    cleaned_path = raw_path.strip()
    if not cleaned_path:
        raise ValidationError("Invalid workspace path in hook payload: path cannot be empty.")

    if "\0" in raw_path or "\0" in cleaned_path:
        raise ValidationError("Invalid workspace path in hook payload: malformed path contains null bytes.")

    try:
        candidate_path = Path(cleaned_path)
    except (ValueError, OSError) as e:
        raise ValidationError(f"Invalid workspace path in hook payload: {e}") from e

    # Do not expanduser; path must already be absolute
    if not candidate_path.is_absolute():
        raise ValidationError("Workspace path must be an absolute path.")

    target_cwd = candidate_path.resolve()

    # 2. Strict project resolution (refusing auto-registration)
    try:
        project_rec = resolve_cli_project(
            cfg=cfg,
            command_project=None,
            cwd=target_cwd,
        )
    except ValidationError as e:
        # Sanitize error to avoid leaking host paths
        err_msg = str(e)
        if "unregistered project candidate" in err_msg:
            raise ValidationError("Hook workspace context is an unregistered project candidate. Auto-registration is not allowed.")
        elif "Ambiguous project resolution" in err_msg:
            raise ValidationError("Ambiguous project resolution for hook workspace context.")
        elif "No registered project found" in err_msg:
            raise ValidationError("No registered project found for hook workspace context.")
        elif "Git probe failed" in err_msg:
            raise ValidationError("Git probe failed for hook workspace directory.")
        else:
            raise ValidationError("Project resolution failed for hook workspace context.")

    # 3. Retrieve bounded context
    req = ContextRetrievalRequest(
        project=project_rec,
        max_items=DEFAULT_MAX_ITEMS,
        max_chars=DEFAULT_MAX_CHARS,
        include_current_state=True,
    )
    result = retrieve_context(cfg, request=req, runner=runner)
    rendered = format_context_result(result)

    response: dict[str, Any] = {
        "injectSteps": [
            {
                "ephemeralMessage": rendered,
            }
        ]
    }
    return ExitCode.SUCCESS, response
