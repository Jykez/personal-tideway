"""Compact automated tests for Phase 4C-B: Codex lifecycle hook provisioning and SessionStart handler support.

Coverage:
- Canonical Codex SessionStart hook definition and shape matching.
- Lifecycle operations: install, idempotency, dry-run, removal, and semantic preservation.
- Backup creation on mutation and zero mutation on dry-run / no-op.
- Conflict detection: modified groups, duplicate commands, misplaced events, inline config.toml collisions.
- config.toml safety: allowing unrelated inline hooks, detecting syntax errors as conflicts, zero config.toml writes.
- File integrity: symlinks, non-regular files, unreadable files, oversized files, malformed JSON, duplicate keys.
- Structural schema: hooks non-object and SessionStart non-array rejection.
- Handler validation: hook_event_name SessionStart, valid sources, absolute cwd without NUL bytes.
- Handler isolation: never reading transcript_path, zero runner calls on validation failure, single runner call on success.
- Exact handler output schema: {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": rendered}}.
- CLI dispatch: ptw hook status/plan/install/remove with --client {agy, codex}, ptw hook codex-session-start, global --codex-hooks.
- Assurance evaluator: impossibility of emitting 'hooked' from structural evidence alone.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from personal_tideway.cli.main import main
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import (
    ASSURANCE_LEVEL_HOOKED,
    CLIENT_CODEX,
    CODEX_HOOK_ADDITIONAL_CONTEXT_LIMIT,
    CODEX_HOOK_COMMAND,
    CODEX_HOOK_EVENT,
    CODEX_HOOK_MATCHER,
    CODEX_HOOK_TIMEOUT,
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
from personal_tideway.core.discovery import (
    format_doctor_text,
    run_doctor,
)
from personal_tideway.core.hooks import (
    check_codex_config_for_inline_hooks,
    get_canonical_codex_hook_group,
    get_codex_hook_status,
    handle_codex_session_start,
    install_codex_hook,
    is_canonical_codex_hook_group,
    plan_codex_hook,
    remove_codex_hook,
)
from personal_tideway.core.project_resolver import register_directory
from personal_tideway.core.registry import load_registry
from personal_tideway.exceptions import PersonalTidewayError, ValidationError
from personal_tideway.models import ContinuityAssuranceLevel, HookStatus


def make_backend_fixture(cfg: PersonalTidewayConfig) -> None:
    """Create harmless executable fixture for Basic Memory backend."""
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

def test_canonical_codex_hook_group_shape():
    """Verify canonical Codex hook group shape matches requirements."""
    group = get_canonical_codex_hook_group()
    assert group["matcher"] == CODEX_HOOK_MATCHER
    assert isinstance(group["hooks"], list)
    assert len(group["hooks"]) == 1
    h = group["hooks"][0]
    assert h["type"] == "command"
    assert h["command"] == CODEX_HOOK_COMMAND
    assert h["timeout"] == CODEX_HOOK_TIMEOUT
    assert h["additionalContextLimit"] == CODEX_HOOK_ADDITIONAL_CONTEXT_LIMIT
    assert is_canonical_codex_hook_group(group) is True


def test_is_canonical_codex_hook_group_rejections():
    """Reject modified or invalid Codex hook groups."""
    assert is_canonical_codex_hook_group(None) is False
    assert is_canonical_codex_hook_group([]) is False
    assert is_canonical_codex_hook_group({}) is False
    assert is_canonical_codex_hook_group({"matcher": "startup", "hooks": []}) is False
    assert is_canonical_codex_hook_group({
        "matcher": CODEX_HOOK_MATCHER,
        "hooks": [{"type": "command", "command": "other command", "timeout": 30, "additionalContextLimit": 2500}],
    }) is False
    assert is_canonical_codex_hook_group({
        "matcher": CODEX_HOOK_MATCHER,
        "hooks": [{"type": "command", "command": CODEX_HOOK_COMMAND, "timeout": 10, "additionalContextLimit": 2500}],
    }) is False
    assert is_canonical_codex_hook_group({
        "matcher": CODEX_HOOK_MATCHER,
        "hooks": [{"type": "command", "command": CODEX_HOOK_COMMAND, "timeout": 30, "additionalContextLimit": 1000}],
    }) is False
    assert is_canonical_codex_hook_group({
        "matcher": CODEX_HOOK_MATCHER,
        "hooks": [{"type": "prompt", "command": CODEX_HOOK_COMMAND, "timeout": 30, "additionalContextLimit": 2500}],
    }) is False
    assert is_canonical_codex_hook_group({
        "matcher": CODEX_HOOK_MATCHER,
        "hooks": [{"type": "command", "command": CODEX_HOOK_COMMAND, "timeout": 30, "additionalContextLimit": 2500}],
        "extra_key": True,
    }) is False


# ============================================================================
# 2. Lifecycle Operations: Install, Dry-Run, Idempotency & Preservation
# ============================================================================

def test_codex_install_fresh_lifecycle(personal_tideway_config: PersonalTidewayConfig):
    """Fresh install creates hooks.json with canonical group appended under hooks.SessionStart."""
    cfg = personal_tideway_config
    hooks_file = cfg.codex_hooks
    assert not hooks_file.exists()

    status_before = get_codex_hook_status(cfg)
    assert status_before.status == HookStatus.NOT_INSTALLED
    assert status_before.installed is False

    changed, msg = install_codex_hook(cfg, dry_run=False)
    assert changed is True
    assert "Installed managed SessionStart hook" in msg
    assert hooks_file.is_file()

    doc = json.loads(hooks_file.read_text(encoding="utf-8"))
    assert "hooks" in doc
    assert "SessionStart" in doc["hooks"]
    assert len(doc["hooks"]["SessionStart"]) == 1
    assert is_canonical_codex_hook_group(doc["hooks"]["SessionStart"][0]) is True

    status_after = get_codex_hook_status(cfg)
    assert status_after.status == HookStatus.INSTALLED
    assert status_after.installed is True
    assert status_after.conflict_reason is None


def test_codex_install_idempotency_zero_mutation(personal_tideway_config: PersonalTidewayConfig):
    """Subsequent install calls are no-ops without mutation or backup creation."""
    cfg = personal_tideway_config
    install_codex_hook(cfg, dry_run=False)
    content_before = cfg.codex_hooks.read_text(encoding="utf-8")

    if cfg.backups_dir.exists():
        for b in cfg.backups_dir.iterdir():
            b.unlink()

    changed, msg = install_codex_hook(cfg, dry_run=False)
    assert changed is False
    assert "already installed" in msg
    assert cfg.codex_hooks.read_text(encoding="utf-8") == content_before

    backups = list(cfg.backups_dir.glob("*.bak")) if cfg.backups_dir.exists() else []
    assert len(backups) == 0


def test_codex_install_dry_run_zero_mutation(personal_tideway_config: PersonalTidewayConfig):
    """Dry-run install produces no filesystem mutation or backup."""
    cfg = personal_tideway_config
    assert not cfg.codex_hooks.exists()

    changed, msg = install_codex_hook(cfg, dry_run=True)
    assert changed is True
    assert "[DRY RUN]" in msg
    assert not cfg.codex_hooks.exists()


def test_codex_install_preserves_unrelated_hooks_and_order(personal_tideway_config: PersonalTidewayConfig):
    """Install appends to SessionStart and preserves other events and keys with backup creation."""
    cfg = personal_tideway_config
    cfg.codex_hooks.parent.mkdir(parents=True, exist_ok=True)

    initial_doc = {
        "$schema": "https://example.com/hooks.schema.json",
        "hooks": {
            "SessionEnd": [
                {
                    "matcher": ".*",
                    "hooks": [{"type": "command", "command": "echo cleanup"}],
                }
            ],
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [{"type": "command", "command": "echo preexisting"}],
                }
            ],
        },
        "metadata": {"version": 1},
    }
    cfg.codex_hooks.write_text(json.dumps(initial_doc, indent=2) + "\n", encoding="utf-8")

    changed, _msg = install_codex_hook(cfg, dry_run=False)
    assert changed is True

    doc_after = json.loads(cfg.codex_hooks.read_text(encoding="utf-8"))
    assert doc_after["$schema"] == initial_doc["$schema"]
    assert doc_after["metadata"] == initial_doc["metadata"]
    assert doc_after["hooks"]["SessionEnd"] == initial_doc["hooks"]["SessionEnd"]
    assert len(doc_after["hooks"]["SessionStart"]) == 2
    # Pre-existing hook preserved in position 0
    assert doc_after["hooks"]["SessionStart"][0] == initial_doc["hooks"]["SessionStart"][0]
    # Canonical hook appended in position 1
    assert is_canonical_codex_hook_group(doc_after["hooks"]["SessionStart"][1]) is True

    backups = list(cfg.backups_dir.glob("*.bak"))
    assert len(backups) >= 1


# ============================================================================
# 3. Lifecycle Operations: Remove, Dry-Run & Idempotency
# ============================================================================

def test_codex_remove_canonical_group(personal_tideway_config: PersonalTidewayConfig):
    """Remove deletes exact unique canonical group while preserving other definitions."""
    cfg = personal_tideway_config
    cfg.codex_hooks.parent.mkdir(parents=True, exist_ok=True)
    initial_doc = {
        "hooks": {
            "SessionStart": [
                {"matcher": "startup", "hooks": [{"type": "command", "command": "echo keep"}]},
                get_canonical_codex_hook_group(),
            ]
        }
    }
    cfg.codex_hooks.write_text(json.dumps(initial_doc, indent=2) + "\n", encoding="utf-8")

    changed, msg = remove_codex_hook(cfg, dry_run=False)
    assert changed is True
    assert "Removed managed SessionStart hook" in msg

    doc_after = json.loads(cfg.codex_hooks.read_text(encoding="utf-8"))
    assert len(doc_after["hooks"]["SessionStart"]) == 1
    assert doc_after["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo keep"

    status = get_codex_hook_status(cfg)
    assert status.status == HookStatus.NOT_INSTALLED


def test_codex_remove_idempotency_zero_mutation(personal_tideway_config: PersonalTidewayConfig):
    """Remove on non-existent or uninstalled hook is a safe no-op."""
    cfg = personal_tideway_config
    assert not cfg.codex_hooks.exists()

    changed, msg = remove_codex_hook(cfg, dry_run=False)
    assert changed is False
    assert "not installed" in msg
    assert not cfg.codex_hooks.exists()


def test_codex_remove_dry_run(personal_tideway_config: PersonalTidewayConfig):
    """Dry-run remove does not mutate existing hook definition."""
    cfg = personal_tideway_config
    install_codex_hook(cfg, dry_run=False)
    content_before = cfg.codex_hooks.read_text(encoding="utf-8")

    changed, msg = remove_codex_hook(cfg, dry_run=True)
    assert changed is True
    assert "[DRY RUN]" in msg
    assert cfg.codex_hooks.read_text(encoding="utf-8") == content_before


# ============================================================================
# 4. Conflict Detection & Rejection
# ============================================================================

def test_codex_conflict_modified_hook_group(personal_tideway_config: PersonalTidewayConfig):
    """Modified Tideway hook group reports CONFLICT and refuses install/remove."""
    cfg = personal_tideway_config
    cfg.codex_hooks.parent.mkdir(parents=True, exist_ok=True)
    conflict_doc = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",  # Modified matcher
                    "hooks": [{"type": "command", "command": CODEX_HOOK_COMMAND, "timeout": 30, "additionalContextLimit": 2500}],
                }
            ]
        }
    }
    cfg.codex_hooks.write_text(json.dumps(conflict_doc, indent=2) + "\n", encoding="utf-8")
    content_before = cfg.codex_hooks.read_text(encoding="utf-8")

    status = get_codex_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT
    assert status.installed is False
    assert status.conflict_reason is not None

    with pytest.raises(ValidationError, match="Conflicting hook configuration"):
        install_codex_hook(cfg, dry_run=False)

    with pytest.raises(ValidationError, match="Conflicting hook configuration"):
        remove_codex_hook(cfg, dry_run=False)

    assert cfg.codex_hooks.read_text(encoding="utf-8") == content_before


def test_codex_conflict_duplicate_command_occurrences(personal_tideway_config: PersonalTidewayConfig):
    """Duplicate occurrences of CODEX_HOOK_COMMAND report CONFLICT and refuse mutation."""
    cfg = personal_tideway_config
    cfg.codex_hooks.parent.mkdir(parents=True, exist_ok=True)
    dup_doc = {
        "hooks": {
            "SessionStart": [
                get_canonical_codex_hook_group(),
                get_canonical_codex_hook_group(),
            ]
        }
    }
    cfg.codex_hooks.write_text(json.dumps(dup_doc, indent=2) + "\n", encoding="utf-8")

    status = get_codex_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT
    assert "Duplicate occurrences" in status.details

    with pytest.raises(ValidationError, match="Conflicting hook configuration"):
        install_codex_hook(cfg, dry_run=False)


def test_codex_conflict_misplaced_event_occurrence(personal_tideway_config: PersonalTidewayConfig):
    """Tideway command placed outside SessionStart reports CONFLICT."""
    cfg = personal_tideway_config
    cfg.codex_hooks.parent.mkdir(parents=True, exist_ok=True)
    misplaced_doc = {
        "hooks": {
            "SessionEnd": [
                {"matcher": ".*", "hooks": [{"type": "command", "command": CODEX_HOOK_COMMAND}]}
            ]
        }
    }
    cfg.codex_hooks.write_text(json.dumps(misplaced_doc, indent=2) + "\n", encoding="utf-8")

    status = get_codex_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT
    assert status.installed is False


# ============================================================================
# 5. config.toml Safety & Collision Detection
# ============================================================================

def test_codex_config_toml_inline_collision_is_conflict(personal_tideway_config: PersonalTidewayConfig):
    """Tideway command in config.toml below [hooks] is a conflict; config.toml is never written."""
    cfg = personal_tideway_config
    cfg.codex_config.parent.mkdir(parents=True, exist_ok=True)

    toml_content = f"""[hooks.SessionStart]
command = "{CODEX_HOOK_COMMAND}"
"""
    cfg.codex_config.write_text(toml_content, encoding="utf-8")

    conflict = check_codex_config_for_inline_hooks(cfg.codex_config)
    assert conflict is not None
    assert "Inline Tideway hook collision" in conflict

    status = get_codex_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT
    assert "Inline Tideway hook collision" in status.details

    with pytest.raises(ValidationError, match="Conflicting Codex configuration"):
        install_codex_hook(cfg, dry_run=False)

    # Ensure config.toml is NEVER modified
    assert cfg.codex_config.read_text(encoding="utf-8") == toml_content


def test_codex_config_toml_unrelated_inline_hooks_allowed(personal_tideway_config: PersonalTidewayConfig):
    """Unrelated inline hooks in config.toml are allowed without conflict."""
    cfg = personal_tideway_config
    cfg.codex_config.parent.mkdir(parents=True, exist_ok=True)

    toml_content = """[hooks.SessionStart]
command = "echo unrelated_startup"
"""
    cfg.codex_config.write_text(toml_content, encoding="utf-8")

    conflict = check_codex_config_for_inline_hooks(cfg.codex_config)
    assert conflict is None

    status = get_codex_hook_status(cfg)
    assert status.status == HookStatus.NOT_INSTALLED


def test_codex_config_toml_malformed_is_conflict(personal_tideway_config: PersonalTidewayConfig):
    """Malformed config.toml is reported as conflict."""
    cfg = personal_tideway_config
    cfg.codex_config.parent.mkdir(parents=True, exist_ok=True)
    cfg.codex_config.write_text("[unclosed toml table\n", encoding="utf-8")

    status = get_codex_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT
    assert "malformed or unreadable" in status.details


# ============================================================================
# 6. Malformed JSON, Duplicate Keys, Schema & Path Safety
# ============================================================================

def test_codex_refuse_malformed_json(personal_tideway_config: PersonalTidewayConfig):
    """Refuse malformed JSON in hooks.json."""
    cfg = personal_tideway_config
    cfg.codex_hooks.parent.mkdir(parents=True, exist_ok=True)
    cfg.codex_hooks.write_text("{bad: json [", encoding="utf-8")

    status = get_codex_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT

    with pytest.raises(ValidationError, match="Malformed JSON"):
        install_codex_hook(cfg, dry_run=False)


def test_codex_refuse_duplicate_keys(personal_tideway_config: PersonalTidewayConfig):
    """Reject duplicate keys at any level in hooks.json."""
    cfg = personal_tideway_config
    cfg.codex_hooks.parent.mkdir(parents=True, exist_ok=True)
    dup_json = '{\n  "hooks": {},\n  "hooks": {}\n}\n'
    cfg.codex_hooks.write_text(dup_json, encoding="utf-8")

    status = get_codex_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT
    assert "Duplicate key" in status.details

    with pytest.raises(ValidationError, match="Duplicate key"):
        install_codex_hook(cfg, dry_run=False)


def test_codex_refuse_wrong_shape_root_and_fields(personal_tideway_config: PersonalTidewayConfig):
    """Reject non-dict root, non-dict hooks field, and non-list SessionStart field."""
    cfg = personal_tideway_config
    cfg.codex_hooks.parent.mkdir(parents=True, exist_ok=True)

    # 1. Root non-dict
    cfg.codex_hooks.write_text("[\"array\"]\n", encoding="utf-8")
    status1 = get_codex_hook_status(cfg)
    assert status1.status == HookStatus.CONFLICT

    # 2. hooks non-dict
    cfg.codex_hooks.write_text(json.dumps({"hooks": "invalid_string"}), encoding="utf-8")
    status2 = get_codex_hook_status(cfg)
    assert status2.status == HookStatus.CONFLICT
    assert "not a JSON object" in status2.details

    # 3. SessionStart non-list
    cfg.codex_hooks.write_text(json.dumps({"hooks": {"SessionStart": {"matcher": "bad"}}}), encoding="utf-8")
    status3 = get_codex_hook_status(cfg)
    assert status3.status == HookStatus.CONFLICT
    assert "not a JSON array" in status3.details


def test_codex_refuse_symlink_hooks_file(personal_tideway_config: PersonalTidewayConfig, tmp_path: Path):
    """Refuse symlinked hooks.json files."""
    cfg = personal_tideway_config
    cfg.codex_hooks.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "external_hooks.json"
    target.write_text("{}", encoding="utf-8")
    cfg.codex_hooks.symlink_to(target)

    status = get_codex_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT
    assert "cannot be a symlink" in status.details

    with pytest.raises(ValidationError, match="cannot be a symlink"):
        install_codex_hook(cfg, dry_run=False)


def test_codex_refuse_oversized_hooks_file(personal_tideway_config: PersonalTidewayConfig):
    """Refuse hooks.json exceeding MAX_HOOKS_JSON_BYTES."""
    cfg = personal_tideway_config
    cfg.codex_hooks.parent.mkdir(parents=True, exist_ok=True)
    oversized = "{\n  \"pad\": \"" + ("x" * (MAX_HOOKS_JSON_BYTES + 1024)) + "\"\n}\n"
    cfg.codex_hooks.write_text(oversized, encoding="utf-8")

    status = get_codex_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT
    assert "exceeds maximum allowed size" in status.details

    with pytest.raises(ValidationError, match="exceeds maximum allowed size"):
        install_codex_hook(cfg, dry_run=False)


def test_codex_plan_output_states(personal_tideway_config: PersonalTidewayConfig):
    """Plan produces correct preview messages without modifying files."""
    cfg = personal_tideway_config

    # Not installed
    p1 = plan_codex_hook(cfg)
    assert p1["status"] == "not-installed"
    assert p1["action"] == "create"
    assert "Would create" in p1["message"]

    # Installed
    install_codex_hook(cfg, dry_run=False)
    p2 = plan_codex_hook(cfg)
    assert p2["status"] == "installed"
    assert p2["action"] == "none"

    # Conflict
    cfg.codex_hooks.write_text(json.dumps({"hooks": {"SessionStart": "bad"}}), encoding="utf-8")
    p3 = plan_codex_hook(cfg)
    assert p3["status"] == "conflict"
    assert p3["action"] == "conflict"


# ============================================================================
# 7. SessionStart Handler Validation, Schema & Execution
# ============================================================================

def test_codex_handler_session_start_success(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Valid payload retrieves context once and returns exact hookSpecificOutput structure."""
    cfg = personal_tideway_config
    make_backend_fixture(cfg)

    proj_dir = tmp_path / "codex_workspace"
    register_test_project(cfg, proj_dir, name="codex-project")

    runner_calls = 0

    def fake_context_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        nonlocal runner_calls
        runner_calls += 1
        data = {
            "total": 1,
            "page_size": 1,
            "current_page": 1,
            "total_is_exact": True,
            "has_more": False,
            "results": [
                {
                    "title": "Codex State Note",
                    "permalink": "current-state",
                    "content": "Active goal: Complete Phase 4C-B.",
                    "status": "verified",
                    "updated_at": "2026-09-14T20:00:00Z",
                }
            ],
        }
        return BasicMemoryRunnerResult(returncode=0, stdout=json.dumps(data), stderr="")

    payload = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": str(proj_dir),
        "transcript_path": "/fake/transcript.jsonl",  # Must never be read
    }

    code, resp = handle_codex_session_start(
        cfg,
        raw_payload=json.dumps(payload),
        runner=fake_context_runner,
    )
    assert code == ExitCode.SUCCESS
    assert runner_calls == 1
    assert "hookSpecificOutput" in resp
    assert resp["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "additionalContext" in resp["hookSpecificOutput"]
    ctx = resp["hookSpecificOutput"]["additionalContext"]
    assert "Codex State Note" in ctx
    assert "Active goal" in ctx
    # Ensure transcript_path was never leaked
    assert "/fake/transcript.jsonl" not in ctx


def test_codex_handler_payload_validation_zero_runner_calls(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Invalid payloads fail closed with zero runner calls."""
    cfg = personal_tideway_config

    def asserting_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        raise AssertionError("Runner must NOT be called on validation failure.")

    invalid_payloads = [
        # Missing hook_event_name
        {"source": "startup", "cwd": "/safe/path"},
        # Wrong hook_event_name
        {"hook_event_name": "PreInvocation", "source": "startup", "cwd": "/safe/path"},
        {"hook_event_name": "SessionEnd", "source": "startup", "cwd": "/safe/path"},
        # Missing source
        {"hook_event_name": "SessionStart", "cwd": "/safe/path"},
        # Invalid source
        {"hook_event_name": "SessionStart", "source": "tool_use", "cwd": "/safe/path"},
        {"hook_event_name": "SessionStart", "source": "invalid", "cwd": "/safe/path"},
        # Missing cwd
        {"hook_event_name": "SessionStart", "source": "startup"},
        # null cwd
        {"hook_event_name": "SessionStart", "source": "startup", "cwd": None},
        # empty / whitespace cwd
        {"hook_event_name": "SessionStart", "source": "startup", "cwd": ""},
        {"hook_event_name": "SessionStart", "source": "startup", "cwd": "   "},
        # relative cwd
        {"hook_event_name": "SessionStart", "source": "startup", "cwd": "relative/path"},
        {"hook_event_name": "SessionStart", "source": "startup", "cwd": "./relative/path"},
        # NUL byte in cwd
        {"hook_event_name": "SessionStart", "source": "startup", "cwd": "/path/with/\0/null"},
        # non-string cwd
        {"hook_event_name": "SessionStart", "source": "startup", "cwd": 123},
    ]

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            handle_codex_session_start(cfg, raw_payload=json.dumps(payload), runner=asserting_runner)

    # Empty payload
    with pytest.raises(ValidationError, match="Hook payload is empty"):
        handle_codex_session_start(cfg, raw_payload="", runner=asserting_runner)

    # Oversized payload
    huge = json.dumps({"hook_event_name": "SessionStart", "source": "startup", "cwd": "/p", "x": "a" * (MAX_HOOK_INPUT_BYTES + 100)})
    with pytest.raises(ValidationError, match="exceeds maximum allowed size"):
        handle_codex_session_start(cfg, raw_payload=huge, runner=asserting_runner)


def test_codex_handler_unregistered_candidate_fails_closed(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Unregistered candidate directory fails closed without auto-registration or host path leaks."""
    cfg = personal_tideway_config
    make_backend_fixture(cfg)

    unregistered_dir = tmp_path / "candidate_dir"
    unregistered_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": str(unregistered_dir),
    }

    with pytest.raises(ValidationError) as exc:
        handle_codex_session_start(cfg, raw_payload=json.dumps(payload))

    err = str(exc.value)
    assert str(unregistered_dir) not in err
    reg = load_registry(cfg.projects_yaml)
    assert len(reg.projects) == 0


# ============================================================================
# 8. CLI End-to-End Tests
# ============================================================================

def test_cli_codex_hook_status_plan_install_remove(personal_tideway_config: PersonalTidewayConfig, capsys):
    """CLI hook commands support --client codex and execute full lifecycle."""
    cfg = personal_tideway_config

    # 1. status (not installed)
    code = main(["--home", str(cfg.home), "--codex-home", str(cfg.codex_home), "hook", "status", "--client", "codex"])
    assert code == ExitCode.SUCCESS
    out = capsys.readouterr().out
    assert "Codex Lifecycle Hook Status:" in out
    assert "Status: not-installed" in out

    # 2. plan
    code_plan = main(["--home", str(cfg.home), "--codex-home", str(cfg.codex_home), "hook", "plan", "--client", "codex", "--json"])
    assert code_plan == ExitCode.SUCCESS
    plan_data = json.loads(capsys.readouterr().out)
    assert plan_data["client"] == CLIENT_CODEX
    assert plan_data["status"] == "not-installed"

    # 3. install dry-run
    code_dry = main(["--home", str(cfg.home), "--codex-home", str(cfg.codex_home), "hook", "install", "--client", "codex", "--dry-run"])
    assert code_dry == ExitCode.SUCCESS
    assert "[DRY RUN]" in capsys.readouterr().out
    assert not cfg.codex_hooks.exists()

    # 4. install
    code_inst = main(["--home", str(cfg.home), "--codex-home", str(cfg.codex_home), "hook", "install", "--client", "codex"])
    assert code_inst == ExitCode.SUCCESS
    capsys.readouterr()
    assert cfg.codex_hooks.is_file()

    # 5. status (installed, json)
    code_stat = main(["--home", str(cfg.home), "--codex-home", str(cfg.codex_home), "hook", "status", "--client", "codex", "--json"])
    assert code_stat == ExitCode.SUCCESS
    stat_data = json.loads(capsys.readouterr().out)
    assert stat_data["status"] == "installed"
    assert stat_data["installed"] is True
    assert stat_data["event"] == CODEX_HOOK_EVENT

    # 6. remove
    code_rem = main(["--home", str(cfg.home), "--codex-home", str(cfg.codex_home), "hook", "remove", "--client", "codex"])
    assert code_rem == ExitCode.SUCCESS
    capsys.readouterr()

    # 7. status after remove
    main(["--home", str(cfg.home), "--codex-home", str(cfg.codex_home), "hook", "status", "--client", "codex", "--json"])
    stat_after = json.loads(capsys.readouterr().out)
    assert stat_after["status"] == "not-installed"


def test_cli_codex_session_start_dispatch(personal_tideway_config: PersonalTidewayConfig, tmp_path: Path, monkeypatch, capsys):
    """CLI hook codex-session-start reads stdin and executes handler."""
    cfg = personal_tideway_config
    make_backend_fixture(cfg)
    proj_dir = tmp_path / "codex_cli_proj"
    register_test_project(cfg, proj_dir, name="codex-cli-proj")

    payload = json.dumps({
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": str(proj_dir),
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))

    runner_calls = 0

    def fake_context_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        nonlocal runner_calls
        runner_calls += 1
        data = {
            "total": 1,
            "page_size": 1,
            "current_page": 1,
            "total_is_exact": True,
            "has_more": False,
            "results": [
                {
                    "title": "Codex State Note",
                    "permalink": "current-state",
                    "content": "Active goal: Complete Phase 4C-B.",
                    "status": "verified",
                    "updated_at": "2026-09-14T20:00:00Z",
                }
            ],
        }
        return BasicMemoryRunnerResult(returncode=0, stdout=json.dumps(data), stderr="")

    code = main([
        "--home", str(cfg.home),
        "--codex-home", str(cfg.codex_home),
        "hook", "codex-session-start",
    ], runner=fake_context_runner)
    assert code == ExitCode.SUCCESS
    assert runner_calls == 1
    resp = json.loads(capsys.readouterr().out)
    assert "hookSpecificOutput" in resp
    assert resp["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "additionalContext" in resp["hookSpecificOutput"]
    ctx = resp["hookSpecificOutput"]["additionalContext"]
    assert "Codex State Note" in ctx
    assert "Active goal" in ctx


def test_cli_custom_codex_hooks_flag(personal_tideway_config: PersonalTidewayConfig, tmp_path: Path):
    """Global --codex-hooks flag sets custom hooks path."""
    cfg = personal_tideway_config
    custom_hooks = tmp_path / "custom_codex_hooks.json"

    resolved = PersonalTidewayConfig.resolve(
        home=cfg.home,
        codex_home=cfg.codex_home,
        codex_hooks=custom_hooks,
    )
    assert resolved.codex_hooks == custom_hooks
    assert resolved.custom_codex_hooks == custom_hooks
    assert resolved.to_dict()["client_paths"]["codex_hooks"] == str(custom_hooks)


# ============================================================================
# 9. Assurance Level Impossibility of 'hooked' from Structural Evidence
# ============================================================================

def test_codex_hook_installed_does_not_elevate_assurance_to_hooked(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Installing Codex SessionStart hook does NOT elevate continuity assurance to 'hooked'.

    Phase 4C-B is strictly structural provisioning; explicit /hooks review and a real
    no-bypass behavioral probe are required before hooked assurance can ever be emitted.
    """
    cfg = personal_tideway_config
    make_backend_fixture(cfg)

    # Install both Codex and agy hooks
    install_codex_hook(cfg, dry_run=False)
    codex_ev = get_codex_hook_status(cfg)
    assert codex_ev.status == HookStatus.INSTALLED

    # Sync rules and skills
    main([
        "--home", str(cfg.home),
        "--codex-home", str(cfg.codex_home),
        "--gemini-home", str(cfg.gemini_home),
        "sync",
    ])

    codex_ass = evaluate_client_assurance(cfg, CLIENT_CODEX)
    assert codex_ass.level != ASSURANCE_LEVEL_HOOKED
    assert codex_ass.level != ContinuityAssuranceLevel.HOOKED.value
    assert codex_ass.level == ContinuityAssuranceLevel.INSTRUCTED
    assert codex_ass.hook_verified is False

    report = evaluate_assurance(cfg)
    assert report.clients[CLIENT_CODEX].level == ContinuityAssuranceLevel.INSTRUCTED
    assert report.clients[CLIENT_CODEX].hook_verified is False
    assert ASSURANCE_LEVEL_HOOKED not in [c.level.value for c in report.clients.values()]


# ============================================================================
# 10. Codex Hooks Symlink Protection & TOML Regression Suite
# ============================================================================

def test_cli_codex_hooks_symlink_within_codex_home_refused(
    personal_tideway_config: PersonalTidewayConfig,
):
    """CLI --codex-hooks pointing to a symlink within codex_home is refused with no writes or backups."""
    cfg = personal_tideway_config
    cfg.codex_home.mkdir(parents=True, exist_ok=True)
    target_hooks = cfg.codex_home / "target_hooks.json"
    target_hooks.write_text("{}", encoding="utf-8")

    link_hooks = cfg.codex_home / "link_hooks.json"
    link_hooks.symlink_to(target_hooks)

    code = main([
        "--home", str(cfg.home),
        "--codex-home", str(cfg.codex_home),
        "--codex-hooks", str(link_hooks),
        "hook", "install",
        "--client", "codex",
    ])
    assert code != ExitCode.SUCCESS
    assert target_hooks.read_text(encoding="utf-8") == "{}"
    if cfg.backups_dir.exists():
        assert len(list(cfg.backups_dir.iterdir())) == 0


def test_persisted_config_codex_hooks_symlink_refused(
    personal_tideway_config: PersonalTidewayConfig,
):
    """Persisted config client_paths codex_hooks symlink is refused with no writes or backups."""
    cfg = personal_tideway_config
    cfg.codex_home.mkdir(parents=True, exist_ok=True)
    target_hooks = cfg.codex_home / "persisted_target.json"
    target_hooks.write_text("{}", encoding="utf-8")

    link_hooks = cfg.codex_home / "persisted_link.json"
    link_hooks.symlink_to(target_hooks)

    raw_yaml = yaml.safe_load(cfg.config_yaml.read_text(encoding="utf-8")) or {}
    raw_yaml.setdefault("client_paths", {})["codex_hooks"] = str(link_hooks)
    cfg.config_yaml.write_text(yaml.safe_dump(raw_yaml), encoding="utf-8")

    persisted_cfg = PersonalTidewayConfig.resolve(
        home=cfg.home,
        codex_home=cfg.codex_home,
        gemini_home=cfg.gemini_home,
    )
    assert persisted_cfg.codex_hooks == link_hooks

    status = get_codex_hook_status(persisted_cfg)
    assert status.status == HookStatus.CONFLICT
    assert "cannot be a symlink" in status.details

    with pytest.raises(ValidationError, match="cannot be a symlink"):
        install_codex_hook(persisted_cfg, dry_run=False)

    with pytest.raises(ValidationError, match="cannot be a symlink"):
        remove_codex_hook(persisted_cfg, dry_run=False)

    assert target_hooks.read_text(encoding="utf-8") == "{}"
    if cfg.backups_dir.exists():
        assert len(list(cfg.backups_dir.iterdir())) == 0


def test_codex_hooks_parent_symlink_escape_rejected(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Hooks path under a parent directory escaping codex_home via symlink is rejected."""
    cfg = personal_tideway_config
    outside_dir = tmp_path / "outside_codex"
    outside_dir.mkdir(parents=True, exist_ok=True)
    escaped_hooks = outside_dir / "escaped_hooks.json"
    escaped_hooks.write_text("{}", encoding="utf-8")

    cfg.codex_home.mkdir(parents=True, exist_ok=True)
    symlink_dir = cfg.codex_home / "linked_dir"
    symlink_dir.symlink_to(outside_dir)

    nested_hooks = symlink_dir / "escaped_hooks.json"
    escaped_cfg = PersonalTidewayConfig.resolve(
        home=cfg.home,
        codex_home=cfg.codex_home,
        codex_hooks=nested_hooks,
    )

    status = get_codex_hook_status(escaped_cfg)
    assert status.status == HookStatus.CONFLICT

    with pytest.raises(ValidationError):
        install_codex_hook(escaped_cfg, dry_run=False)

    assert escaped_hooks.read_text(encoding="utf-8") == "{}"
    if cfg.backups_dir.exists():
        assert len(list(cfg.backups_dir.iterdir())) == 0


@pytest.mark.parametrize("hazard_type", [
    "directory",
    "symlink",
    "lstat_oserror",
    "open_oserror",
    "invalid_utf8",
    "oversized",
    "malformed_toml",
])
def test_codex_config_toml_hazards_and_regressions(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hazard_type: str,
):
    """Test TOML regressions and hazards: sanitized conflict, no writes, no backups."""
    cfg = personal_tideway_config
    cfg.codex_config.parent.mkdir(parents=True, exist_ok=True)

    sentinel_token = "SECRET_SENTINEL_TOKEN_12345"

    if hazard_type == "directory":
        cfg.codex_config.mkdir(parents=True, exist_ok=True)
    elif hazard_type == "symlink":
        target = tmp_path / "external_config.toml"
        target.write_text(f"# {sentinel_token}\nhooks = {{}}\n", encoding="utf-8")
        cfg.codex_config.symlink_to(target)
    elif hazard_type == "lstat_oserror":
        cfg.codex_config.write_text("hooks = {}\n", encoding="utf-8")
        orig_lstat = Path.lstat
        def fake_lstat(self):
            if self == cfg.codex_config:
                raise OSError(f"Simulated lstat error: {sentinel_token}")
            return orig_lstat(self)
        monkeypatch.setattr(Path, "lstat", fake_lstat)
    elif hazard_type == "open_oserror":
        cfg.codex_config.write_text("hooks = {}\n", encoding="utf-8")
        orig_open = open
        def fake_open(file, *args, **kwargs):
            if str(file) == str(cfg.codex_config):
                raise OSError(f"Simulated read error: {sentinel_token}")
            return orig_open(file, *args, **kwargs)
        monkeypatch.setattr("builtins.open", fake_open)
    elif hazard_type == "invalid_utf8":
        cfg.codex_config.write_bytes(b"\xff\xfe\x00\x00" + sentinel_token.encode("utf-8"))
    elif hazard_type == "oversized":
        cfg.codex_config.write_bytes(b"a" * (256 * 1024 + 10))
    elif hazard_type == "malformed_toml":
        cfg.codex_config.write_text(f"[malformed {sentinel_token}\n", encoding="utf-8")

    # 1. check_codex_config_for_inline_hooks returns sanitized conflict
    conflict = check_codex_config_for_inline_hooks(cfg.codex_config)
    assert conflict is not None
    assert "malformed or unreadable" in conflict
    assert sentinel_token not in conflict

    # 2. get_codex_hook_status reports CONFLICT
    status = get_codex_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT
    assert "malformed or unreadable" in status.details
    assert sentinel_token not in status.details

    # 3. install refuses with ValidationError, no writes, no backups
    with pytest.raises(ValidationError, match="malformed or unreadable"):
        install_codex_hook(cfg, dry_run=False)

    assert not cfg.codex_hooks.exists()
    if cfg.backups_dir.exists():
        assert len(list(cfg.backups_dir.iterdir())) == 0


# ============================================================================
# 11. Parameterized Sentinel, Hazard & Boundary Tests
# ============================================================================

@pytest.mark.parametrize("bad_field,bad_val", [
    ("timeout", 30.0),
    ("timeout", True),
    ("timeout", False),
    ("additionalContextLimit", 2500.0),
    ("additionalContextLimit", True),
    ("additionalContextLimit", False),
])
def test_codex_canonical_float_and_bool_reject(bad_field: str, bad_val: Any):
    """Reject float lookalikes and bools in canonical hook timeout and additionalContextLimit."""
    group = get_canonical_codex_hook_group()
    group["hooks"][0][bad_field] = bad_val
    assert is_canonical_codex_hook_group(group) is False


@pytest.mark.parametrize("error_kind", [
    "event",
    "source",
    "parse",
    "surrogate",
    "stream_io",
    "path_io",
    "backend",
])
def test_codex_handler_sentinel_errors_do_not_reveal_sentinel(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_kind: str,
):
    """Sanitized errors never reveal arbitrary payload inputs or underlying exceptions."""
    cfg = personal_tideway_config
    make_backend_fixture(cfg)
    proj_dir = tmp_path / "codex_sentinel_proj"
    register_test_project(cfg, proj_dir, name="codex-sentinel")

    sentinel = "SECRET_SENTINEL_TOKEN_98765"
    runner = None
    stream = None
    payload = None

    if error_kind == "event":
        payload = json.dumps({
            "hook_event_name": f"SessionStart_{sentinel}",
            "source": "startup",
            "cwd": str(proj_dir),
        })
    elif error_kind == "source":
        payload = json.dumps({
            "hook_event_name": "SessionStart",
            "source": f"invalid_{sentinel}",
            "cwd": str(proj_dir),
        })
    elif error_kind == "parse":
        payload = f'{{"hook_event_name": "{sentinel}", malformed'
    elif error_kind == "surrogate":
        payload = f'{{"hook_event_name": "SessionStart", "source": "startup", "cwd": "{sentinel}\\ud800"}}'
    elif error_kind == "stream_io":
        class FailingStream:
            def read(self, *args, **kwargs):
                raise OSError(f"Stream IO error with {sentinel}")
        stream = FailingStream()
    elif error_kind == "path_io":
        payload = json.dumps({
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(proj_dir),
        })
        orig_resolve = Path.resolve
        def fake_resolve(self, *args, **kwargs):
            if str(self) == str(proj_dir):
                raise OSError(f"Path resolve failure {sentinel}")
            return orig_resolve(self, *args, **kwargs)
        monkeypatch.setattr(Path, "resolve", fake_resolve)
    elif error_kind == "backend":
        payload = json.dumps({
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(proj_dir),
        })
        def failing_runner(*args, **kwargs):
            raise PersonalTidewayError(f"Backend failure with {sentinel}")
        runner = failing_runner

    with pytest.raises(ValidationError) as exc_info:
        handle_codex_session_start(
            cfg,
            raw_payload=payload,
            stdin_stream=stream,
            runner=runner,
        )

    err_msg = str(exc_info.value)
    assert sentinel not in err_msg


@pytest.mark.parametrize("payload_type", [
    "duplicate_keys",
    "constant_nan",
    "constant_infinity",
    "deep_nesting",
    "giant_integer",
])
def test_codex_handler_duplicate_nonstandard_deep_zero_backend_calls(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
    payload_type: str,
):
    """Payloads with duplicate keys, NaN/Infinity, excessive nesting, or giant ints make zero backend calls."""
    cfg = personal_tideway_config
    make_backend_fixture(cfg)
    proj_dir = tmp_path / "codex_zero_calls"
    register_test_project(cfg, proj_dir, name="codex-zero-calls")

    def asserting_runner(*args, **kwargs):
        raise AssertionError("Backend runner must not be called.")

    if payload_type == "duplicate_keys":
        payload = (
            f'{{"hook_event_name": "SessionStart", "source": "startup", '
            f'"cwd": "{proj_dir}", "cwd": "{proj_dir}"}}'
        )
    elif payload_type == "constant_nan":
        payload = (
            f'{{"hook_event_name": "SessionStart", "source": "startup", '
            f'"cwd": "{proj_dir}", "bad": NaN}}'
        )
    elif payload_type == "constant_infinity":
        payload = (
            f'{{"hook_event_name": "SessionStart", "source": "startup", '
            f'"cwd": "{proj_dir}", "bad": Infinity}}'
        )
    elif payload_type == "deep_nesting":
        deep: Any = "leaf"
        for _ in range(70):
            deep = [deep]
        payload = json.dumps({
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(proj_dir),
            "nested": deep,
        })
    elif payload_type == "giant_integer":
        payload = (
            '{"hook_event_name": "SessionStart", "source": "startup", '
            f'"cwd": "{proj_dir}", "x": ' + ("1" * 5000) + "}"
        )

    with pytest.raises(ValidationError):
        handle_codex_session_start(cfg, raw_payload=payload, runner=asserting_runner)


@pytest.mark.parametrize("hazard_kind", [
    "nan",
    "infinity",
    "duplicates",
    "deep",
    "surrogate",
    "giant_integer",
])
def test_codex_hooks_file_hazards_conflict_no_mutation_or_backups(
    personal_tideway_config: PersonalTidewayConfig,
    hazard_kind: str,
):
    """Hooks file with NaN, Infinity, duplicates, deep nesting, surrogates, or giant ints reports CONFLICT and prevents writes/backups."""
    cfg = personal_tideway_config
    cfg.codex_hooks.parent.mkdir(parents=True, exist_ok=True)

    if hazard_kind == "nan":
        content = '{"hooks": {"SessionStart": [NaN]}}\n'
    elif hazard_kind == "infinity":
        content = '{"hooks": {"SessionStart": [Infinity]}}\n'
    elif hazard_kind == "duplicates":
        content = '{\n  "hooks": {},\n  "hooks": {}\n}\n'
    elif hazard_kind == "deep":
        deep: Any = "leaf"
        for _ in range(70):
            deep = [deep]
        content = json.dumps({"hooks": {"SessionStart": deep}}) + "\n"
    elif hazard_kind == "surrogate":
        content = '{"hooks": {"SessionStart": ["\\ud800"]}}\n'
    elif hazard_kind == "giant_integer":
        content = '{"hooks": {"SessionStart": [{"x": ' + ("1" * 5000) + "}]}}\n"

    cfg.codex_hooks.write_text(content, encoding="utf-8")

    status = get_codex_hook_status(cfg)
    assert status.status == HookStatus.CONFLICT

    with pytest.raises(ValidationError):
        install_codex_hook(cfg, dry_run=False)

    with pytest.raises(ValidationError):
        remove_codex_hook(cfg, dry_run=False)

    assert cfg.codex_hooks.read_text(encoding="utf-8") == content
    if cfg.backups_dir.exists():
        assert len(list(cfg.backups_dir.iterdir())) == 0


# ============================================================================
# 12. Doctor Codex Hook Structural Diagnostics
# ============================================================================

def test_run_doctor_codex_hook_structural_evidence(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """run_doctor returns structural codex_hook evidence without elevating assurance."""
    cfg = personal_tideway_config
    skills_dir = tmp_path / "skills"
    fake_version_runner = lambda cmd, timeout: (1, "", "unused")
    no_executable_resolver = lambda name: None

    # 1. Install temporary canonical hook
    install_codex_hook(cfg, dry_run=False)

    report = run_doctor(
        cfg=cfg,
        codex_home=cfg.codex_home,
        gemini_home=cfg.gemini_home,
        canonical_user_skills=skills_dir,
        executable_resolver=no_executable_resolver,
        version_runner=fake_version_runner,
    )
    assert "codex_hook" in report
    hook_ev = report["codex_hook"]
    assert hook_ev["installed"] is True
    assert hook_ev["status"] == HookStatus.INSTALLED.value
    assert hook_ev["target_path"] == str(cfg.codex_hooks)

    codex_ass = report["continuity_assurance"]["clients"][CLIENT_CODEX]
    assert codex_ass["level"] != ASSURANCE_LEVEL_HOOKED
    assert codex_ass["hook_verified"] is False

    doc_text = format_doctor_text(report)
    assert "Codex Lifecycle Hook (structural only):" in doc_text
    assert "Status: installed" in doc_text
    assert str(cfg.codex_hooks) in doc_text

    # 2. Modified hook reports conflict
    conflict_doc = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "modified_matcher",
                    "hooks": [{"type": "command", "command": CODEX_HOOK_COMMAND, "timeout": 30, "additionalContextLimit": 2500}],
                }
            ]
        }
    }
    cfg.codex_hooks.write_text(json.dumps(conflict_doc, indent=2) + "\n", encoding="utf-8")

    report_conflict = run_doctor(
        cfg=cfg,
        codex_home=cfg.codex_home,
        gemini_home=cfg.gemini_home,
        canonical_user_skills=skills_dir,
        executable_resolver=no_executable_resolver,
        version_runner=fake_version_runner,
    )
    assert report_conflict["codex_hook"]["installed"] is False
    assert report_conflict["codex_hook"]["status"] == HookStatus.CONFLICT.value
    doc_conflict_text = format_doctor_text(report_conflict)
    assert "Status: conflict" in doc_conflict_text

    # 3. Custom codex_hooks target_path override is honored
    custom_hooks = tmp_path / "custom_codex_hooks.json"
    cfg_custom = PersonalTidewayConfig.resolve(
        home=cfg.home,
        codex_home=cfg.codex_home,
        gemini_home=cfg.gemini_home,
        codex_hooks=custom_hooks,
    )
    report_custom = run_doctor(
        cfg=cfg_custom,
        codex_home=cfg_custom.codex_home,
        gemini_home=cfg_custom.gemini_home,
        canonical_user_skills=skills_dir,
        executable_resolver=no_executable_resolver,
        version_runner=fake_version_runner,
    )
    assert report_custom["codex_hook"]["target_path"] == str(custom_hooks)
