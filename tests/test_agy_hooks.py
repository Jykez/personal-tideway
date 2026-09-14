"""Comprehensive automated tests for Phase 4C-A: Antigravity CLI lifecycle hook provisioning and handler support.

Coverage:
- Handler payload validation and bounded context injection (invocationNum == 0).
- Later invocations (invocationNum > 0) returning valid no-op JSON without context reads.
- Bounded context and fail-closed error handling (unregistered/candidate/ambiguous projects, backend failures, invalid payloads).
- Dry-run zero mutation across all operations.
- Install idempotency, non-overwrite, semantic preservation of unrelated keys and hooks, and backup creation.
- Exact removal, idempotency, and non-overwrite.
- Conflict detection and refusal to overwrite/remove modified hook entries.
- Refusal of malformed JSON, duplicate keys, oversized files, invalid UTF-8, unreadable files, non-object roots, and symlink/path-boundary hazards.
- CLI subcommands (status, plan, install, remove, agy-preinvocation) in text and JSON modes.
- Assurance evaluator impossibility of emitting 'hooked' from structural evidence alone.
- Complete isolation: all tests use tmp_path and fake runners; no live client config touched.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from personal_tideway.cli.main import main
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import (
    AGY_HOOK_COMMAND,
    AGY_HOOK_EVENT,
    AGY_HOOK_NAME,
    AGY_HOOK_TIMEOUT,
    ASSURANCE_LEVEL_HOOKED,
    CLIENT_AGY,
    MAX_HOOK_INPUT_BYTES,
    MAX_HOOKS_JSON_BYTES,
    ExitCode,
)
from personal_tideway.core.assurance import (
    evaluate_assurance,
    evaluate_client_assurance,
)
from personal_tideway.core.basic_memory_installer import BasicMemoryRunnerResult
from personal_tideway.core.basic_memory_runtime import get_basic_memory_layout
from personal_tideway.core.hooks import (
    get_agy_adapter,
    get_agy_hook_status,
    get_canonical_agy_hook_entry,
    handle_agy_preinvocation,
    install_agy_hook,
    is_canonical_agy_hook_entry,
    plan_agy_hook,
    read_hook_payload_from_stream,
    remove_agy_hook,
)
from personal_tideway.core.project_resolver import register_directory
from personal_tideway.core.registry import load_registry
from personal_tideway.exceptions import RuntimeProbeError, ValidationError
from personal_tideway.models import ContinuityAssuranceLevel, HookStatus


def make_backend_fixture(cfg: PersonalTidewayConfig) -> None:
    """Create harmless executable fixture for Basic Memory backend without real host execution."""
    executable = get_basic_memory_layout(cfg).primary_executable
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)


def register_test_project(cfg: PersonalTidewayConfig, project_dir: Path, name: str = "test-proj") -> str:
    """Register a local directory project in the workspace registry."""
    project_dir.mkdir(parents=True, exist_ok=True)
    runner = lambda cmd, cwd, timeout: (128, "", "fatal: not a git repository\n")
    res = register_directory(
        project_dir,
        cfg=cfg,
        display_name=name,
        git_runner=runner,
    )
    return res.project_id


# ============================================================================
# 1. Canonical Hook Definition & Matching Tests
# ============================================================================

def test_canonical_agy_hook_entry_shape():
    """Verify canonical agy hook shape strictly matches agy documentation."""
    entry = get_canonical_agy_hook_entry()
    assert AGY_HOOK_EVENT in entry
    assert isinstance(entry[AGY_HOOK_EVENT], list)
    assert len(entry[AGY_HOOK_EVENT]) == 1
    handler = entry[AGY_HOOK_EVENT][0]
    assert handler["type"] == "command"
    assert handler["command"] == AGY_HOOK_COMMAND
    assert handler["timeout"] == AGY_HOOK_TIMEOUT
    assert is_canonical_agy_hook_entry(entry) is True


def test_is_canonical_agy_hook_entry_rejection():
    """Reject modified or conflicting hook definitions."""
    assert is_canonical_agy_hook_entry(None) is False
    assert is_canonical_agy_hook_entry([]) is False
    assert is_canonical_agy_hook_entry({}) is False
    assert is_canonical_agy_hook_entry({"enabled": False, AGY_HOOK_EVENT: [{"command": AGY_HOOK_COMMAND}]}) is False
    assert is_canonical_agy_hook_entry({AGY_HOOK_EVENT: []}) is False
    assert is_canonical_agy_hook_entry({AGY_HOOK_EVENT: [{"command": "other command"}]}) is False
    assert is_canonical_agy_hook_entry({AGY_HOOK_EVENT: [{"command": AGY_HOOK_COMMAND, "timeout": 999}]}) is False
    assert is_canonical_agy_hook_entry({AGY_HOOK_EVENT: [{"command": AGY_HOOK_COMMAND}], "PostToolUse": []}) is False


# ============================================================================
# 2. Lifecycle Operations: Install, Dry-Run, Idempotency & Preservation
# ============================================================================

def test_install_fresh_in_disposable_path(personal_tideway_config: PersonalTidewayConfig):
    """Fresh install creates hooks.json with exact canonical entry and reports installed status."""
    cfg = personal_tideway_config
    hooks_file = cfg.agy_hooks
    assert not hooks_file.exists()

    status_before = get_agy_hook_status(cfg)
    assert status_before.status == HookStatus.NOT_INSTALLED
    assert status_before.installed is False

    changed, msg = install_agy_hook(cfg, dry_run=False)
    assert changed is True
    assert "Installed managed PreInvocation hook" in msg
    assert hooks_file.is_file()

    # Verify JSON content
    doc = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert AGY_HOOK_NAME in doc
    assert is_canonical_agy_hook_entry(doc[AGY_HOOK_NAME]) is True

    status_after = get_agy_hook_status(cfg)
    assert status_after.status == HookStatus.INSTALLED
    assert status_after.installed is True
    assert status_after.conflict_reason is None


def test_install_idempotency_zero_mutation(personal_tideway_config: PersonalTidewayConfig):
    """Subsequent install calls are no-ops that do not mutate the file or create backups."""
    cfg = personal_tideway_config
    install_agy_hook(cfg, dry_run=False)
    content_before = cfg.agy_hooks.read_text(encoding="utf-8")

    # Clear any existing backups
    if cfg.backups_dir.exists():
        for b in cfg.backups_dir.iterdir():
            b.unlink()

    changed, msg = install_agy_hook(cfg, dry_run=False)
    assert changed is False
    assert "already installed" in msg
    content_after = cfg.agy_hooks.read_text(encoding="utf-8")
    assert content_before == content_after

    # No backup should have been created on no-op
    backups = list(cfg.backups_dir.glob("*.bak")) if cfg.backups_dir.exists() else []
    assert len(backups) == 0


def test_install_dry_run_zero_mutation(personal_tideway_config: PersonalTidewayConfig):
    """Dry-run install produces no filesystem mutation or backup."""
    cfg = personal_tideway_config
    assert not cfg.agy_hooks.exists()

    changed, msg = install_agy_hook(cfg, dry_run=True)
    assert changed is True
    assert "[DRY RUN]" in msg
    assert not cfg.agy_hooks.exists()


def test_install_preserves_unrelated_hooks_and_unknown_keys(personal_tideway_config: PersonalTidewayConfig):
    """Install preserves other hooks, top-level keys ($schema), and creates a backup."""
    cfg = personal_tideway_config
    cfg.agy_hooks.parent.mkdir(parents=True, exist_ok=True)

    initial_doc = {
        "$schema": "https://example.com/custom-hooks.schema.json",
        "linter": {
            "PostToolUse": [
                {
                    "matcher": "run_command",
                    "hooks": [{"type": "command", "command": "./lint.sh"}],
                }
            ]
        },
        "custom_metadata": {"author": "test", "version": 42},
    }
    cfg.agy_hooks.write_text(json.dumps(initial_doc, indent=2) + "\n", encoding="utf-8")

    changed, _msg = install_agy_hook(cfg, dry_run=False)
    assert changed is True

    doc_after = json.loads(cfg.agy_hooks.read_text(encoding="utf-8"))
    assert doc_after["$schema"] == initial_doc["$schema"]
    assert doc_after["linter"] == initial_doc["linter"]
    assert doc_after["custom_metadata"] == initial_doc["custom_metadata"]
    assert AGY_HOOK_NAME in doc_after
    assert is_canonical_agy_hook_entry(doc_after[AGY_HOOK_NAME]) is True

    # Backup should exist
    backups = list(cfg.backups_dir.glob("*.bak"))
    assert len(backups) >= 1


# ============================================================================
# 3. Lifecycle Operations: Remove, Dry-Run & Idempotency
# ============================================================================

def test_remove_canonical_hook(personal_tideway_config: PersonalTidewayConfig):
    """Remove deletes exact managed entry while preserving unrelated entries."""
    cfg = personal_tideway_config
    cfg.agy_hooks.parent.mkdir(parents=True, exist_ok=True)
    initial_doc = {
        "unrelated": {"enabled": True},
        AGY_HOOK_NAME: get_canonical_agy_hook_entry(),
    }
    cfg.agy_hooks.write_text(json.dumps(initial_doc, indent=2) + "\n", encoding="utf-8")

    changed, msg = remove_agy_hook(cfg, dry_run=False)
    assert changed is True
    assert "Removed managed PreInvocation hook" in msg

    doc_after = json.loads(cfg.agy_hooks.read_text(encoding="utf-8"))
    assert AGY_HOOK_NAME not in doc_after
    assert doc_after["unrelated"] == {"enabled": True}

    status = get_agy_hook_status(cfg)
    assert status.status == HookStatus.NOT_INSTALLED
    assert status.installed is False


def test_remove_idempotency_zero_mutation(personal_tideway_config: PersonalTidewayConfig):
    """Remove on non-existent or uninstalled hook is a safe no-op."""
    cfg = personal_tideway_config
    assert not cfg.agy_hooks.exists()

    changed, msg = remove_agy_hook(cfg, dry_run=False)
    assert changed is False
    assert "not installed" in msg
    assert not cfg.agy_hooks.exists()


def test_remove_dry_run(personal_tideway_config: PersonalTidewayConfig):
    """Dry-run remove does not modify existing hook definition."""
    cfg = personal_tideway_config
    install_agy_hook(cfg, dry_run=False)
    content_before = cfg.agy_hooks.read_text(encoding="utf-8")

    changed, msg = remove_agy_hook(cfg, dry_run=True)
    assert changed is True
    assert "[DRY RUN]" in msg
    assert cfg.agy_hooks.read_text(encoding="utf-8") == content_before


# ============================================================================
# 4. Conflict Detection & Rejection (Refuse to Overwrite or Modify)
# ============================================================================

def test_conflict_detection_and_install_refusal(personal_tideway_config: PersonalTidewayConfig):
    """Conflicting modified personal-tideway entry reports CONFLICT and install raises ValidationError without modifying file."""
    cfg = personal_tideway_config
    cfg.agy_hooks.parent.mkdir(parents=True, exist_ok=True)
    conflict_doc = {
        AGY_HOOK_NAME: {
            "PreInvocation": [{"type": "command", "command": "malicious_or_custom_script.sh"}],
        }
    }
    cfg.agy_hooks.write_text(json.dumps(conflict_doc, indent=2) + "\n", encoding="utf-8")
    content_before = cfg.agy_hooks.read_text(encoding="utf-8")

    status = get_agy_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT
    assert status.installed is False
    assert status.conflict_reason is not None

    with pytest.raises(ValidationError, match="Conflicting modified 'personal-tideway'"):
        install_agy_hook(cfg, dry_run=False)

    # Ensure zero mutation
    assert cfg.agy_hooks.read_text(encoding="utf-8") == content_before


def test_conflict_remove_refusal(personal_tideway_config: PersonalTidewayConfig):
    """Conflicting modified personal-tideway entry refuses to be removed automatically."""
    cfg = personal_tideway_config
    cfg.agy_hooks.parent.mkdir(parents=True, exist_ok=True)
    conflict_doc = {
        AGY_HOOK_NAME: {
            "PreInvocation": [{"type": "command", "command": "user_custom_tool.sh"}],
        }
    }
    cfg.agy_hooks.write_text(json.dumps(conflict_doc, indent=2) + "\n", encoding="utf-8")
    content_before = cfg.agy_hooks.read_text(encoding="utf-8")

    with pytest.raises(ValidationError, match="Conflicting modified 'personal-tideway'"):
        remove_agy_hook(cfg, dry_run=False)

    # Ensure zero mutation
    assert cfg.agy_hooks.read_text(encoding="utf-8") == content_before


# ============================================================================
# 5. Malformed JSON, Shape & Path-Boundary/Symlink Safety
# ============================================================================

def test_refuse_malformed_json(personal_tideway_config: PersonalTidewayConfig):
    """Refuse to overwrite or modify malformed JSON."""
    cfg = personal_tideway_config
    cfg.agy_hooks.parent.mkdir(parents=True, exist_ok=True)
    cfg.agy_hooks.write_text("{malformed: json, [unclosed", encoding="utf-8")
    content_before = cfg.agy_hooks.read_text(encoding="utf-8")

    status = get_agy_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT
    assert "Invalid hooks configuration" in status.details

    with pytest.raises(ValidationError, match="Malformed JSON in agy hooks file"):
        install_agy_hook(cfg, dry_run=False)

    with pytest.raises(ValidationError, match="Malformed JSON in agy hooks file"):
        remove_agy_hook(cfg, dry_run=False)

    assert cfg.agy_hooks.read_text(encoding="utf-8") == content_before


def test_refuse_wrong_shape_root_array(personal_tideway_config: PersonalTidewayConfig):
    """Refuse to overwrite when root JSON is a list instead of a dict."""
    cfg = personal_tideway_config
    cfg.agy_hooks.parent.mkdir(parents=True, exist_ok=True)
    cfg.agy_hooks.write_text("[\"item1\", \"item2\"]\n", encoding="utf-8")

    status = get_agy_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT

    with pytest.raises(ValidationError, match="must contain a JSON object at the root"):
        install_agy_hook(cfg, dry_run=False)

    with pytest.raises(ValidationError, match="must contain a JSON object at the root"):
        remove_agy_hook(cfg, dry_run=False)


def test_refuse_symlink_hooks_file(personal_tideway_config: PersonalTidewayConfig, tmp_path: Path):
    """Refuse to touch or resolve symlinked hooks.json files."""
    cfg = personal_tideway_config
    cfg.agy_hooks.parent.mkdir(parents=True, exist_ok=True)

    external_target = tmp_path / "external_target.json"
    external_target.write_text("{}", encoding="utf-8")

    cfg.agy_hooks.symlink_to(external_target)

    status = get_agy_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT
    assert "cannot be a symlink" in status.details

    with pytest.raises(ValidationError, match="cannot be a symlink"):
        install_agy_hook(cfg, dry_run=False)

    with pytest.raises(ValidationError, match="cannot be a symlink"):
        remove_agy_hook(cfg, dry_run=False)


def test_plan_output_states(personal_tideway_config: PersonalTidewayConfig):
    """Plan produces correct preview messages without modifying files."""
    cfg = personal_tideway_config

    # 1. Plan when not installed
    plan1 = plan_agy_hook(cfg)
    assert plan1["status"] == "not-installed"
    assert plan1["action"] == "create"
    assert "Would create" in plan1["message"]

    # 2. Plan when already installed
    install_agy_hook(cfg, dry_run=False)
    plan2 = plan_agy_hook(cfg)
    assert plan2["status"] == "installed"
    assert plan2["action"] == "none"
    assert "No changes needed" in plan2["message"]

    # 3. Plan on conflict
    cfg.agy_hooks.write_text(json.dumps({AGY_HOOK_NAME: {"bad": True}}), encoding="utf-8")
    plan3 = plan_agy_hook(cfg)
    assert plan3["status"] == "conflict"
    assert plan3["action"] == "conflict"
    assert "CONFLICT" in plan3["message"]


# ============================================================================
# 6. Handler Payload Parsing & Execution (PreInvocation)
# ============================================================================

def test_handler_invocation_zero_injects_bounded_context(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """On invocationNum == 0, handler retrieves bounded context and returns single ephemeralMessage."""
    cfg = personal_tideway_config
    make_backend_fixture(cfg)

    # Register project
    proj_dir = tmp_path / "workspace_repo"
    register_test_project(cfg, proj_dir, name="my-workspace")

    payload = {
        "invocationNum": 0,
        "initialNumSteps": 1,
        "conversationId": "test-uuid",
        "workspacePaths": [str(proj_dir)],
        "transcriptPath": "/fake/transcript.jsonl",
        "artifactDirectoryPath": "/fake/artifacts",
        "modelName": "auto",
    }

    def fake_context_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        # Return valid Basic Memory 0.23.2 search notes response
        data = {
            "total": 1,
            "page_size": 1,
            "current_page": 1,
            "total_is_exact": True,
            "has_more": False,
            "results": [
                {
                    "title": "Current State Note",
                    "permalink": "current-state",
                    "content": "Active goal: Complete Phase 4C-A implementation.",
                    "status": "verified",
                    "updated_at": "2026-09-13T20:00:00Z",
                }
            ],
        }
        return BasicMemoryRunnerResult(
            returncode=0,
            stdout=json.dumps(data),
            stderr="",
        )

    code, resp = handle_agy_preinvocation(
        cfg,
        raw_payload=json.dumps(payload),
        runner=fake_context_runner,
    )
    assert code == ExitCode.SUCCESS
    assert "injectSteps" in resp
    assert len(resp["injectSteps"]) == 1
    step = resp["injectSteps"][0]
    assert "ephemeralMessage" in step
    msg = step["ephemeralMessage"]
    assert "Current State Note" in msg
    assert "Active goal" in msg
    # Never leak internal transcript or artifact paths from payload
    assert "/fake/transcript.jsonl" not in msg
    assert "/fake/artifacts" not in msg


def test_handler_invocation_later_returns_noop(personal_tideway_config: PersonalTidewayConfig):
    """On invocationNum > 0, handler immediately returns empty injectSteps without reading context."""
    cfg = personal_tideway_config

    def failing_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        raise AssertionError("Runner must NOT be invoked when invocationNum > 0")

    for inv in (1, 2, 10):
        payload = {
            "invocationNum": inv,
            "conversationId": "test-uuid",
            "workspacePaths": ["/any/path"],
        }
        code, resp = handle_agy_preinvocation(
            cfg,
            raw_payload=json.dumps(payload),
            runner=failing_runner,
        )
        assert code == ExitCode.SUCCESS
        assert resp == {"injectSteps": []}


def test_handler_unregistered_candidate_fails_closed(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Unregistered candidate directory fails closed without auto-registration or host path leaks."""
    cfg = personal_tideway_config
    make_backend_fixture(cfg)

    unregistered_dir = tmp_path / "candidate_dir"
    unregistered_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "invocationNum": 0,
        "conversationId": "test-uuid",
        "workspacePaths": [str(unregistered_dir)],
    }

    with pytest.raises(ValidationError) as exc:
        handle_agy_preinvocation(cfg, raw_payload=json.dumps(payload))

    err_msg = str(exc.value)
    # Must fail closed with sanitized error
    assert "No registered project found for hook workspace context." in err_msg or "unregistered project candidate" in err_msg
    # Must NOT leak host paths
    assert str(unregistered_dir) not in err_msg

    # Registry must remain empty (zero auto-registration)
    reg = load_registry(cfg.projects_yaml)
    assert len(reg.projects) == 0


def test_handler_strict_workspace_paths_rejections(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Invocation 0 requires exactly one absolute workspace path; rejects all invalid shapes before resolution/backend."""
    cfg = personal_tideway_config
    make_backend_fixture(cfg)

    def asserting_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        raise AssertionError("Runner must NOT be invoked when workspace path validation fails.")

    invalid_payloads = [
        # Missing workspacePaths
        {"invocationNum": 0, "conversationId": "test"},
        # null workspacePaths
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": None},
        # wrong type
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": "not-a-list"},
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": 123},
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": {}},
        # empty list
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": []},
        # multi-root (more than 1 path)
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": ["/first/path", "/second/path"]},
        # wrong element type
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": [123]},
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": [None]},
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": [[]]},
        # empty or whitespace string
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": [""]},
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": ["   "]},
        # relative paths
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": ["relative/path"]},
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": ["./relative/path"]},
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": ["../relative/path"]},
        # tilde path (not expanded)
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": ["~/expanded/path"]},
        # null byte and literal escape
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": ["/path/with/\0/null"]},
        {"invocationNum": 0, "conversationId": "test", "workspacePaths": ["/path/with/\\0/null"]},
    ]

    for payload in invalid_payloads:
        with pytest.raises(ValidationError) as exc:
            handle_agy_preinvocation(
                cfg,
                raw_payload=json.dumps(payload),
                runner=asserting_runner,
            )
        # Ensure error message is sanitized and informative
        assert len(str(exc.value)) > 0
        # Ensure zero registry mutation
        reg = load_registry(cfg.projects_yaml)
        assert len(reg.projects) == 0


def test_handler_workspace_path_with_null_byte_rejected(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Ensure NUL byte in workspacePaths is rejected before Path instantiation, sanitized, zero runner, zero registry mutation."""
    cfg = personal_tideway_config
    runner_called = False

    def asserting_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        nonlocal runner_called
        runner_called = True
        raise AssertionError("Runner must NOT be invoked when workspace path contains null bytes.")

    payload = {
        "invocationNum": 0,
        "conversationId": "test-null",
        "workspacePaths": ["/path/with/\0/null"],
    }
    raw_payload = json.dumps(payload)
    assert "\\u0000" in raw_payload

    with pytest.raises(ValidationError) as exc:
        handle_agy_preinvocation(cfg, raw_payload=raw_payload, runner=asserting_runner)

    err_msg = str(exc.value)
    assert "malformed path contains null bytes" in err_msg
    assert "/path/with" not in err_msg
    assert "\0" not in err_msg
    assert runner_called is False
    reg = load_registry(cfg.projects_yaml)
    assert len(reg.projects) == 0


def test_handler_raw_payload_size_limits_and_empty_rejection(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Raw payload enforces MAX_HOOK_INPUT_BYTES UTF-8 size, rejects empty/whitespace, and protects runners."""
    cfg = personal_tideway_config

    def asserting_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        raise AssertionError("Runner must NOT be invoked on invalid payload input.")

    # 1. Empty raw_payload
    with pytest.raises(ValidationError, match="Hook payload is empty"):
        handle_agy_preinvocation(cfg, raw_payload="", runner=asserting_runner)

    # 2. Whitespace-only raw_payload
    with pytest.raises(ValidationError, match="Hook payload is empty"):
        handle_agy_preinvocation(cfg, raw_payload="   \n\t  \r\n", runner=asserting_runner)

    # 3. Oversized raw_payload
    huge_payload = json.dumps({
        "invocationNum": 0,
        "workspacePaths": ["/safe/path"],
        "extra_padding": "x" * (MAX_HOOK_INPUT_BYTES + 1024),
    })
    with pytest.raises(ValidationError, match="exceeds maximum allowed size"):
        handle_agy_preinvocation(cfg, raw_payload=huge_payload, runner=asserting_runner)

    # 4. Non-string raw_payload
    with pytest.raises(ValidationError, match="Hook payload must be a string"):
        handle_agy_preinvocation(cfg, raw_payload=123, runner=asserting_runner)  # type: ignore[arg-type]


def test_hooks_json_oversized_file_fails_closed_zero_mutation(
    personal_tideway_config: PersonalTidewayConfig,
):
    """hooks.json exceeding MAX_HOOKS_JSON_BYTES fails closed across status, install, remove, and plan."""
    cfg = personal_tideway_config
    cfg.agy_hooks.parent.mkdir(parents=True, exist_ok=True)

    # Create oversized file
    oversized_content = "{\n  \"padding\": \"" + ("a" * (MAX_HOOKS_JSON_BYTES + 2048)) + "\"\n}\n"
    cfg.agy_hooks.write_text(oversized_content, encoding="utf-8")

    # 1. Status reports CONFLICT with safe reason without crashing
    status = get_agy_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT
    assert status.installed is False
    assert "exceeds maximum allowed size" in status.details

    # 2. Install refuses to mutate
    with pytest.raises(ValidationError, match="exceeds maximum allowed size"):
        install_agy_hook(cfg, dry_run=False)
    assert cfg.agy_hooks.read_text(encoding="utf-8") == oversized_content

    # 3. Remove refuses to mutate
    with pytest.raises(ValidationError, match="exceeds maximum allowed size"):
        remove_agy_hook(cfg, dry_run=False)
    assert cfg.agy_hooks.read_text(encoding="utf-8") == oversized_content

    # 4. Plan reports conflict without mutating
    plan = plan_agy_hook(cfg)
    assert plan["status"] == "conflict"
    assert plan["action"] == "conflict"
    assert cfg.agy_hooks.read_text(encoding="utf-8") == oversized_content


def test_hooks_json_duplicate_keys_fails_closed_zero_mutation(
    personal_tideway_config: PersonalTidewayConfig,
):
    """hooks.json with duplicate object keys at any nesting level fails closed with zero mutation."""
    cfg = personal_tideway_config
    cfg.agy_hooks.parent.mkdir(parents=True, exist_ok=True)

    # Top-level duplicate keys
    dup_top_content = '{\n  "personal-tideway": {},\n  "personal-tideway": {}\n}\n'
    cfg.agy_hooks.write_text(dup_top_content, encoding="utf-8")

    status_top = get_agy_hook_status(cfg)
    assert status_top.status == HookStatus.CONFLICT
    assert "Duplicate key" in status_top.details

    with pytest.raises(ValidationError, match="Duplicate key"):
        install_agy_hook(cfg, dry_run=False)
    assert cfg.agy_hooks.read_text(encoding="utf-8") == dup_top_content

    with pytest.raises(ValidationError, match="Duplicate key"):
        remove_agy_hook(cfg, dry_run=False)
    assert cfg.agy_hooks.read_text(encoding="utf-8") == dup_top_content

    plan_top = plan_agy_hook(cfg)
    assert plan_top["status"] == "conflict"
    assert plan_top["action"] == "conflict"

    # Nested duplicate keys
    dup_nested_raw = '{\n  "unrelated": {\n    "tool": "a",\n    "tool": "b"\n  }\n}\n'
    cfg.agy_hooks.write_text(dup_nested_raw, encoding="utf-8")

    status_nested = get_agy_hook_status(cfg)
    assert status_nested.status == HookStatus.CONFLICT
    assert "Duplicate key" in status_nested.details

    with pytest.raises(ValidationError, match="Duplicate key"):
        install_agy_hook(cfg, dry_run=False)
    assert cfg.agy_hooks.read_text(encoding="utf-8") == dup_nested_raw

    with pytest.raises(ValidationError, match="Duplicate key"):
        remove_agy_hook(cfg, dry_run=False)
    assert cfg.agy_hooks.read_text(encoding="utf-8") == dup_nested_raw


def test_hooks_json_invalid_utf8_or_unreadable_fails_closed_zero_mutation(
    personal_tideway_config: PersonalTidewayConfig,
    monkeypatch: pytest.MonkeyPatch,
):
    """hooks.json with invalid UTF-8 or unreadable content fails closed with zero mutation across write, install, and remove."""
    cfg = personal_tideway_config
    cfg.agy_hooks.parent.mkdir(parents=True, exist_ok=True)
    adapter = get_agy_adapter(cfg)

    # 1. Invalid UTF-8 bytes in existing file
    invalid_utf8_bytes = b"{\xff\xfe\xfd: malformed utf-8\x80\x81}"
    cfg.agy_hooks.write_bytes(invalid_utf8_bytes)

    status = get_agy_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT
    assert "Malformed UTF-8" in status.details

    # Direct write_hooks call must fail closed
    with pytest.raises(ValidationError, match="Malformed UTF-8"):
        adapter.write_hooks({"new_hook": {}}, dry_run=False)
    assert cfg.agy_hooks.read_bytes() == invalid_utf8_bytes

    # install_agy_hook must fail closed
    with pytest.raises(ValidationError, match="Malformed UTF-8"):
        install_agy_hook(cfg, dry_run=False)
    assert cfg.agy_hooks.read_bytes() == invalid_utf8_bytes

    # remove_agy_hook must fail closed
    with pytest.raises(ValidationError, match="Malformed UTF-8"):
        remove_agy_hook(cfg, dry_run=False)
    assert cfg.agy_hooks.read_bytes() == invalid_utf8_bytes

    # Verify zero backups created
    backups = list(cfg.backups_dir.glob("*.bak")) if cfg.backups_dir.exists() else []
    assert len(backups) == 0

    # 2. Unreadable existing file (deterministic monkeypatching of OSError on read_bytes)
    valid_initial_content = b'{"preexisting": true}\n'
    cfg.agy_hooks.write_bytes(valid_initial_content)

    def mock_read_bytes_oserror(self_path: Path) -> bytes:
        if self_path == cfg.agy_hooks:
            raise OSError("Permission denied / I/O error reading hooks file")
        return Path.read_bytes(self_path)

    monkeypatch.setattr(Path, "read_bytes", mock_read_bytes_oserror)

    # write_hooks must fail closed and never overwrite
    with pytest.raises(ValidationError, match="Failed to read agy hooks file"):
        adapter.write_hooks({"new_hook": {}}, dry_run=False)

    # install_agy_hook must fail closed
    with pytest.raises(ValidationError, match="Failed to read agy hooks file"):
        install_agy_hook(cfg, dry_run=False)

    # remove_agy_hook must fail closed
    with pytest.raises(ValidationError, match="Failed to read agy hooks file"):
        remove_agy_hook(cfg, dry_run=False)

    monkeypatch.undo()
    assert cfg.agy_hooks.read_bytes() == valid_initial_content

    # 3. Unreadable existing file (deterministic monkeypatching of OSError on stat)
    orig_stat = Path.stat

    def mock_stat_oserror(self_path: Path, *args: Any, **kwargs: Any) -> Any:
        if self_path == cfg.agy_hooks:
            follow_symlinks = kwargs.get("follow_symlinks", True)
            if args:
                follow_symlinks = args[0]
            if follow_symlinks:
                raise OSError("I/O error during stat")
        return orig_stat(self_path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", mock_stat_oserror)

    # status and plan must report conflict without raising unhandled OSError
    status = get_agy_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT
    assert "Failed to read agy hooks file" in status.details

    plan = plan_agy_hook(cfg)
    assert plan["status"] == HookStatus.CONFLICT.value
    assert plan["action"] == "conflict"

    with pytest.raises(ValidationError, match="Failed to read agy hooks file"):
        adapter.write_hooks({"new_hook": {}}, dry_run=False)

    with pytest.raises(ValidationError, match="Failed to read agy hooks file"):
        install_agy_hook(cfg, dry_run=False)

    with pytest.raises(ValidationError, match="Failed to read agy hooks file"):
        remove_agy_hook(cfg, dry_run=False)

    monkeypatch.undo()
    assert cfg.agy_hooks.read_bytes() == valid_initial_content

    # Verify no backups created during failure modes
    backups_after = list(cfg.backups_dir.glob("*.bak")) if cfg.backups_dir.exists() else []
    assert len(backups_after) == 0


def test_handler_invalid_payloads_fail_closed(personal_tideway_config: PersonalTidewayConfig):
    """Handler fails closed on malformed JSON, wrong shapes, missing invocationNum, and oversized input."""
    cfg = personal_tideway_config

    # 1. Malformed JSON
    with pytest.raises(ValidationError, match="Malformed JSON in hook payload"):
        handle_agy_preinvocation(cfg, raw_payload="{not-valid-json")

    # 2. Non-object root
    with pytest.raises(ValidationError, match="Hook payload must be a JSON object"):
        handle_agy_preinvocation(cfg, raw_payload="[\"array\"]")

    # 3. Missing invocationNum
    with pytest.raises(ValidationError, match="Missing required 'invocationNum'"):
        handle_agy_preinvocation(cfg, raw_payload=json.dumps({"conversationId": "x"}))

    # 4. Invalid invocationNum types
    with pytest.raises(ValidationError, match="Invalid 'invocationNum'"):
        handle_agy_preinvocation(cfg, raw_payload=json.dumps({"invocationNum": "0"}))

    with pytest.raises(ValidationError, match="Invalid 'invocationNum'"):
        handle_agy_preinvocation(cfg, raw_payload=json.dumps({"invocationNum": True}))

    with pytest.raises(ValidationError, match="Invalid 'invocationNum'"):
        handle_agy_preinvocation(cfg, raw_payload=json.dumps({"invocationNum": -1}))

    # 5. Oversized input
    oversized = io.BytesIO(b"x" * (MAX_HOOK_INPUT_BYTES + 100))
    with pytest.raises(ValidationError, match="exceeds maximum allowed size"):
        read_hook_payload_from_stream(oversized)


def test_handler_backend_failure_fails_closed(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Backend failure fails closed without leaking raw stderr or stack traces."""
    cfg = personal_tideway_config
    make_backend_fixture(cfg)

    proj_dir = tmp_path / "proj"
    register_test_project(cfg, proj_dir, name="backend-fail-proj")

    def broken_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(
            returncode=1,
            stdout="",
            stderr="fatal: sensitive internal database error at /var/secret",
        )

    payload = {
        "invocationNum": 0,
        "workspacePaths": [str(proj_dir)],
    }

    with pytest.raises(RuntimeProbeError) as exc:
        handle_agy_preinvocation(cfg, raw_payload=json.dumps(payload), runner=broken_runner)

    # Ensure sensitive stderr string was suppressed and safe classification preserved
    assert "/var/secret" not in str(exc.value)
    assert "Basic Memory search failed" in str(exc.value)


# ============================================================================
# 7. CLI End-to-End Tests
# ============================================================================

def test_cli_hook_status_text_and_json(personal_tideway_config: PersonalTidewayConfig, capsys):
    """CLI ptw hook status works in text and --json mode."""
    cfg = personal_tideway_config

    # Text mode before install
    code = main(["--home", str(cfg.home), "--gemini-home", str(cfg.gemini_home), "hook", "status"])
    assert code == ExitCode.SUCCESS
    out = capsys.readouterr().out
    assert "AGY Lifecycle Hook Status:" in out
    assert "Status: not-installed" in out

    # Install
    main(["--home", str(cfg.home), "--gemini-home", str(cfg.gemini_home), "hook", "install"])
    capsys.readouterr()

    # JSON mode after install
    code_json = main(["--home", str(cfg.home), "--gemini-home", str(cfg.gemini_home), "hook", "status", "--json"])
    assert code_json == ExitCode.SUCCESS
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "installed"
    assert data["installed"] is True
    assert data["event"] == AGY_HOOK_EVENT
    assert data["command"] == AGY_HOOK_COMMAND


def test_cli_hook_plan_and_dry_run(personal_tideway_config: PersonalTidewayConfig, capsys):
    """CLI ptw hook plan and install/remove --dry-run produce zero mutation."""
    cfg = personal_tideway_config

    # Plan
    code_plan = main(["--home", str(cfg.home), "--gemini-home", str(cfg.gemini_home), "hook", "plan"])
    assert code_plan == ExitCode.SUCCESS
    assert not cfg.agy_hooks.exists()

    # Install dry-run
    code_dry = main(["--home", str(cfg.home), "--gemini-home", str(cfg.gemini_home), "hook", "install", "--dry-run"])
    assert code_dry == ExitCode.SUCCESS
    out_dry = capsys.readouterr().out
    assert "[DRY RUN]" in out_dry
    assert not cfg.agy_hooks.exists()


def test_cli_hook_remove_e2e(personal_tideway_config: PersonalTidewayConfig, capsys):
    """CLI ptw hook install followed by remove successfully cycles state."""
    cfg = personal_tideway_config

    # Install
    assert main(["--home", str(cfg.home), "--gemini-home", str(cfg.gemini_home), "hook", "install"]) == ExitCode.SUCCESS
    assert cfg.agy_hooks.is_file()

    # Remove
    assert main(["--home", str(cfg.home), "--gemini-home", str(cfg.gemini_home), "hook", "remove"]) == ExitCode.SUCCESS
    capsys.readouterr()

    # Verify status
    main(["--home", str(cfg.home), "--gemini-home", str(cfg.gemini_home), "hook", "status", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "not-installed"
    assert data["installed"] is False


def test_cli_hook_agy_preinvocation_later_invocation(personal_tideway_config: PersonalTidewayConfig, monkeypatch, capsys):
    """CLI hook agy-preinvocation reads stdin and returns valid empty JSON for invocationNum > 0."""
    cfg = personal_tideway_config
    payload = json.dumps({"invocationNum": 3, "conversationId": "test"})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))

    code = main([
        "--home", str(cfg.home),
        "--gemini-home", str(cfg.gemini_home),
        "hook", "agy-preinvocation",
    ])
    assert code == ExitCode.SUCCESS
    out = json.loads(capsys.readouterr().out)
    assert out == {"injectSteps": []}


# ============================================================================
# 8. Assurance Level Impossibility of 'hooked' from Structural Evidence
# ============================================================================

def test_assurance_evaluator_never_emits_hooked_with_installed_hook(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Installing the agy PreInvocation hook does NOT elevate continuity assurance to 'hooked'.

    Phase 4C-A is strictly structural provisioning; a real quota-consuming behavioral probe
    is required before hooked can ever be emitted.
    """
    cfg = personal_tideway_config
    make_backend_fixture(cfg)

    # 1. Install hook
    install_agy_hook(cfg, dry_run=False)
    hook_ev = get_agy_hook_status(cfg)
    assert hook_ev.status == HookStatus.INSTALLED
    assert hook_ev.installed is True

    # 2. Sync to project rules and skills
    main([
        "--home", str(cfg.home),
        "--codex-home", str(cfg.codex_home),
        "--gemini-home", str(cfg.gemini_home),
        "sync",
    ])

    # 3. Evaluate client assurance directly
    agy_ass = evaluate_client_assurance(cfg, CLIENT_AGY)
    assert agy_ass.level != ASSURANCE_LEVEL_HOOKED
    assert agy_ass.level != ContinuityAssuranceLevel.HOOKED.value
    # Level must remain INSTRUCTED (because rule and skill are present)
    assert agy_ass.level == ContinuityAssuranceLevel.INSTRUCTED
    assert agy_ass.hook_verified is False

    # 4. Evaluate consolidated report
    report = evaluate_assurance(cfg)
    assert report.clients[CLIENT_AGY].level == ContinuityAssuranceLevel.INSTRUCTED
    assert report.clients[CLIENT_AGY].hook_verified is False
    assert ASSURANCE_LEVEL_HOOKED not in [c.level.value for c in report.clients.values()]
