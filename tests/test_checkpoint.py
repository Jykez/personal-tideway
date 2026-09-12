"""Comprehensive automated tests for Personal Tideway v2 checkpoint writes.

Work Package 12 verification:
1. Deterministic schema, render, SHA-256 fingerprint, and controlled frontmatter.
2. Dry-run byte-for-byte non-mutation and zero runner/clock/probe/lock calls.
3. First write exact argv plus stdin, project scoping, no content in argv.
4. Identical repeat skips write and preserves timestamp.
5. Changed payload overwrites exactly once with a later injected UTC timestamp.
6. Missing current-state, malformed/untrusted fingerprint, and legacy note behavior.
7. Write nonzero, crash, timeout, oversize output all fail closed and leak nothing.
8. Known token, private-key, password, auth patterns rejected before any side effect.
9. Unresolved/ambiguous projects and unsafe paths fail closed.
10. Lock contention and hostile lock path tests (symlink, directory, hardlink).
11. Bounded Unicode and collection limits, invalid statuses, clients, timestamps.
12. No writes into a registered source tree.
"""

import fcntl
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.core.basic_memory_installer import BasicMemoryRunnerResult
from personal_tideway.core.basic_memory_runtime import get_basic_memory_layout
from personal_tideway.core.checkpoint import (
    CURRENT_STATE_FOLDER,
    CURRENT_STATE_PERMALINK,
    CURRENT_STATE_TITLE,
    HARD_MAX_CHECKPOINT_CHARS,
    MAX_CHECKPOINT_OUTPUT_BYTES,
    MAX_COLLECTION_ITEMS,
    MAX_FIELD_CHARS,
    MIN_CHECKPOINT_CHARS,
    CheckpointLockContentionError,
    CheckpointPayload,
    CheckpointRequest,
    CheckpointRunnerResult,
    CheckpointSecretError,
    acquire_project_checkpoint_lock,
    compute_semantic_fingerprint,
    plan_checkpoint_write,
    render_checkpoint_markdown,
    write_checkpoint,
)
from personal_tideway.core.registry import ProjectRecord, ProjectRegistry
from personal_tideway.exceptions import (
    BoundaryError,
    ConflictError,
    RuntimeProbeError,
    SecretsError,
    ValidationError,
)

NULL_NOTE_STDOUT: str = json.dumps({
    "title": None,
    "permalink": None,
    "file_path": None,
    "content": None,
    "frontmatter": None,
})


def synthetic_secret(*parts: str) -> str:
    """Assemble detector fixtures without committing provider-shaped credentials."""
    return "".join(parts)


def snapshot_filesystem(root: Path) -> dict[str, tuple[int, int, int]]:
    """Capture snapshot of directory entries: (size, mtime_ns, mode)."""
    if not root.exists():
        return {}
    entries: dict[str, tuple[int, int, int]] = {}
    for p in sorted(root.rglob("*")):
        try:
            st = p.lstat()
            entries[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, st.st_mode)
        except OSError:
            pass
    return entries


def create_fake_executable(target: Path) -> Path:
    """Create a minimal executable file with mode 0755."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    return target


def create_test_projects(
    tmp_path: Path,
) -> tuple[ProjectRegistry, ProjectRecord, ProjectRecord, ProjectRecord]:
    """Create sample registry with git, directory, and external projects."""
    git_dir = tmp_path / "git_repo"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "README.md").write_text("# Git Repo Source\n", encoding="utf-8")

    dir_dir = tmp_path / "local_docs"
    dir_dir.mkdir(parents=True, exist_ok=True)
    (dir_dir / "index.md").write_text("# Local Docs Source\n", encoding="utf-8")

    p_git = ProjectRecord.create(
        slug="alpha-repo",
        display_name="Alpha Repo",
        kind="git",
        paths=[str(git_dir)],
        aliases=["alpha", "arepo"],
    )
    p_dir = ProjectRecord.create(
        slug="beta-docs",
        display_name="Beta Docs",
        kind="directory",
        paths=[str(dir_dir)],
        aliases=["beta"],
    )
    p_ext = ProjectRecord.create(
        slug="gamma-ext",
        display_name="Gamma External",
        kind="external",
        aliases=["gamma"],
    )
    reg = ProjectRegistry(projects=[p_git, p_dir, p_ext])
    return reg, p_git, p_dir, p_ext


# ===========================================================================
# 1. Deterministic Schema, Render, Fingerprint, and Controlled Frontmatter
# ===========================================================================


def test_deterministic_schema_render_fingerprint_and_controlled_frontmatter(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test deterministic Markdown rendering, controlled frontmatter, and SHA-256 fingerprint."""
    _reg, p_git, _, _ = create_test_projects(tmp_path)

    payload = CheckpointPayload(
        condition="Clean build passes; database migration applied.",
        objective="Integrate contextual checkpoint writing.",
        completed=["Refactored project resolver", "Added lock boundaries"],
        blockers=["Pending upstream API freeze"],
        verification_status="verified",
        next_safe_action="Implement test suite and run unit tests",
        source_client="codex",
        evidence=["decisions/db-001", "specs/checkpoint-v2"],
    )

    # Immutability
    with pytest.raises((FrozenInstanceError, AttributeError)):
        payload.condition = "mutation"  # type: ignore

    fp1 = compute_semantic_fingerprint(payload)
    fp2 = compute_semantic_fingerprint(payload)
    assert len(fp1) == 64
    assert fp1 == fp2
    assert fp1.isalnum()

    # Changing any semantic field changes the fingerprint
    payload_modified = CheckpointPayload(
        condition="Clean build passes; database migration applied.",
        objective="Integrate contextual checkpoint writing with optimizations.",
        completed=["Refactored project resolver", "Added lock boundaries"],
        blockers=["Pending upstream API freeze"],
        verification_status="verified",
        next_safe_action="Implement test suite and run unit tests",
        source_client="codex",
        evidence=["decisions/db-001", "specs/checkpoint-v2"],
    )
    assert compute_semantic_fingerprint(payload_modified) != fp1

    ts = "2026-09-11T12:00:00Z"
    md1 = render_checkpoint_markdown(
        payload,
        project_name=p_git.memory.project_name,
        timestamp=ts,
        fingerprint=fp1,
    )
    md2 = render_checkpoint_markdown(
        payload,
        project_name=p_git.memory.project_name,
        timestamp=ts,
        fingerprint=fp1,
    )
    assert md1 == md2

    # Verify controlled frontmatter structure
    assert md1.startswith("---\n")
    assert f"title: {CURRENT_STATE_TITLE}\n" in md1
    assert "type: state\n" in md1
    assert "note_type: state\n" in md1
    assert "status: verified\n" in md1
    assert f"updated_at: {ts}\n" in md1
    assert "source_client: codex\n" in md1
    assert f"fingerprint: {fp1}\n" in md1

    # Verify curated sections
    assert f"# {CURRENT_STATE_TITLE}: {p_git.memory.project_name}" in md1
    assert "## Current Condition" in md1
    assert "Clean build passes; database migration applied." in md1
    assert "## Active Objective" in md1
    assert "Integrate contextual checkpoint writing." in md1
    assert "## Completed & Decisions" in md1
    assert "- Refactored project resolver" in md1
    assert "- Added lock boundaries" in md1
    assert "## Blockers" in md1
    assert "- Pending upstream API freeze" in md1
    assert "## Next Safe Action" in md1
    assert "Implement test suite and run unit tests" in md1
    assert "## Evidence & References" in md1
    assert "- decisions/db-001" in md1
    assert "- specs/checkpoint-v2" in md1


# ===========================================================================
# 2. Dry-Run Byte-For-Byte Non-Mutation and Zero Calls
# ===========================================================================


def test_dry_run_byte_for_byte_non_mutation_and_zero_calls(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test dry-run mode makes zero runner calls, zero clock reads, zero locks, and zero disk changes."""
    reg, p_git, _, _ = create_test_projects(tmp_path)

    # Executable intentionally does not exist on disk
    layout = get_basic_memory_layout(personal_tideway_config)
    assert not layout.primary_executable.exists()

    req = CheckpointRequest(
        project=p_git,
        condition="System healthy.",
        objective="Verify dry-run isolation.",
    )

    def guarded_runner(*_args, **_kwargs):
        raise AssertionError("Runner must not be invoked during dry-run!")

    def guarded_clock():
        raise AssertionError("Clock must not be read during dry-run!")

    before_ptw = snapshot_filesystem(personal_tideway_config.home)
    before_git = snapshot_filesystem(Path(p_git.bindings.paths[0]))

    res = write_checkpoint(
        personal_tideway_config,
        req,
        dry_run=True,
        runner=guarded_runner,
        clock=guarded_clock,
        registry=reg,
    )

    assert res.written is False
    assert res.dry_run is True
    assert res.timestamp == ""
    assert res.preview is not None
    assert res.preview.is_noop is False
    assert res.preview.operation == "checkpoint_write"

    # Explicitly assert the per-project checkpoint lock file was not created
    lock_file = personal_tideway_config.locks_dir / f"checkpoint-{p_git.id}.lock"
    assert not lock_file.exists()
    assert not list(personal_tideway_config.locks_dir.glob("*.lock"))

    # Verify byte-for-byte non-mutation
    after_ptw = snapshot_filesystem(personal_tideway_config.home)
    after_git = snapshot_filesystem(Path(p_git.bindings.paths[0]))
    assert before_ptw == after_ptw
    assert before_git == after_git


# ===========================================================================
# 3. First Write Exact Argv Plus Stdin, Project Scoping, No Content in Argv
# ===========================================================================


def test_first_write_exact_argv_plus_stdin_no_content_in_argv(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test initial checkpoint write sends exact argv and supplies content via stdin only."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    condition_text = "Initial setup complete; tests pending."
    objective_text = "Deploy initial checkpoint adapter."
    req = CheckpointRequest(
        project=p_git,
        condition=condition_text,
        objective=objective_text,
        verification_status="planned",
        next_safe_action="Verify runner stdin isolation",
        source_client="codex",
    )

    read_calls: list[tuple[str, ...]] = []
    write_calls: list[dict[str, Any]] = []

    def mock_read_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float):
        read_calls.append(argv)
        # Note does not exist yet (Basic Memory 0.23.2 returns rc=0 with null content)
        return BasicMemoryRunnerResult(returncode=0, stdout=NULL_NOTE_STDOUT, stderr="")

    def mock_write_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
        stdin: str = "",
    ):
        write_calls.append({"argv": argv, "env": env, "timeout": timeout, "stdin": stdin})
        return CheckpointRunnerResult(
            returncode=0,
            stdout=json.dumps({"status": "success", "title": "Current State"}),
            stderr="",
        )

    injected_time = "2026-09-11T13:00:00Z"
    clock_calls: list[int] = []

    def mock_clock():
        clock_calls.append(1)
        return injected_time

    res = write_checkpoint(
        personal_tideway_config,
        req,
        read_runner=mock_read_runner,
        write_runner=mock_write_runner,
        clock=mock_clock,
        registry=reg,
    )

    assert res.written is True
    assert res.timestamp == injected_time
    assert len(clock_calls) == 1

    # Verify read argv
    assert len(read_calls) == 1
    assert read_calls[0] == (
        str(layout.primary_executable),
        "tool",
        "read-note",
        CURRENT_STATE_PERMALINK,
        "--json",
        "--project",
        p_git.memory.project_name,
        "--local",
    )

    # Verify write call: exact argv, scoped project, NO note content in argv
    assert len(write_calls) == 1
    write_info = write_calls[0]
    expected_write_argv = (
        str(layout.primary_executable),
        "tool",
        "write-note",
        "--title",
        CURRENT_STATE_TITLE,
        "--folder",
        CURRENT_STATE_FOLDER,
        "--project",
        p_git.memory.project_name,
        "--overwrite",
        "--local",
    )
    assert write_info["argv"] == expected_write_argv

    # Assert no content leaked into argv
    for arg in write_info["argv"]:
        assert condition_text not in arg
        assert objective_text not in arg
        assert "Initial setup" not in arg

    # Verify content delivered via stdin
    stdin_content = write_info["stdin"]
    assert condition_text in stdin_content
    assert objective_text in stdin_content
    assert f"updated_at: {injected_time}" in stdin_content
    assert f"fingerprint: {res.fingerprint}" in stdin_content


# ===========================================================================
# 4. Identical Repeat Skips Write and Preserves Timestamp
# ===========================================================================


def test_identical_repeat_skips_write_and_preserves_timestamp(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test repeat checkpoint with same semantic content skips write and preserves existing timestamp."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    payload = CheckpointPayload(
        condition="Identical state test.",
        objective="Verify idempotent skip.",
        completed=["Task 1", "Task 2"],
        verification_status="verified",
        source_client="agy",
    )
    target_fp = compute_semantic_fingerprint(payload)
    original_timestamp = "2026-09-11T09:15:30Z"

    existing_note_stdout = json.dumps({
        "title": "Current State",
        "permalink": "current-state",
        "content": render_checkpoint_markdown(
            payload,
            project_name=p_git.memory.project_name,
            timestamp=original_timestamp,
            fingerprint=target_fp,
        ),
        "frontmatter": {
            "title": "Current State",
            "type": "state",
            "note_type": "state",
            "status": "verified",
            "updated_at": original_timestamp,
            "source_client": "agy",
            "fingerprint": target_fp,
        },
    })

    read_calls: list[tuple[str, ...]] = []

    def mock_read_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float):
        read_calls.append(argv)
        return BasicMemoryRunnerResult(returncode=0, stdout=existing_note_stdout, stderr="")

    def guarded_write_runner(*_args, **_kwargs):
        raise AssertionError("Write runner must NOT be called for identical repeat!")

    def guarded_clock():
        raise AssertionError("Clock must NOT be called for identical repeat!")

    req = CheckpointRequest(project=p_git, payload=payload)
    res = write_checkpoint(
        personal_tideway_config,
        req,
        read_runner=mock_read_runner,
        write_runner=guarded_write_runner,
        clock=guarded_clock,
        registry=reg,
    )

    assert res.written is False
    assert res.unchanged is True
    assert res.timestamp == original_timestamp
    assert res.fingerprint == target_fp
    assert len(read_calls) == 1


# ===========================================================================
# 5. Changed Payload Overwrites Exactly Once With Later Timestamp
# ===========================================================================


def test_changed_payload_overwrites_exactly_once_with_later_timestamp(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test modified checkpoint overwrites existing note and advances timestamp."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    old_payload = CheckpointPayload(
        condition="State v1.",
        objective="Step 1 in progress.",
        completed=["Step 0"],
    )
    old_fp = compute_semantic_fingerprint(old_payload)
    old_timestamp = "2026-09-11T10:00:00Z"

    existing_note_stdout = json.dumps({
        "title": "Current State",
        "permalink": "current-state",
        "frontmatter": {
            "title": "Current State",
            "status": "verified",
            "updated_at": old_timestamp,
            "fingerprint": old_fp,
        },
    })

    new_payload = CheckpointPayload(
        condition="State v2.",
        objective="Step 2 in progress.",
        completed=["Step 0", "Step 1"],
    )
    new_fp = compute_semantic_fingerprint(new_payload)
    assert new_fp != old_fp

    later_timestamp = "2026-09-11T14:30:00Z"
    clock_calls: list[int] = []

    def mock_clock():
        clock_calls.append(1)
        return later_timestamp

    write_calls: list[dict[str, Any]] = []

    def mock_read_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float):
        return BasicMemoryRunnerResult(returncode=0, stdout=existing_note_stdout, stderr="")

    def mock_write_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
        stdin: str = "",
    ):
        write_calls.append({"argv": argv, "stdin": stdin})
        return CheckpointRunnerResult(returncode=0, stdout="{}", stderr="")

    req = CheckpointRequest(project=p_git, payload=new_payload)
    res = write_checkpoint(
        personal_tideway_config,
        req,
        read_runner=mock_read_runner,
        write_runner=mock_write_runner,
        clock=mock_clock,
        registry=reg,
    )

    assert res.written is True
    assert res.timestamp == later_timestamp
    assert res.fingerprint == new_fp
    assert len(clock_calls) == 1
    assert len(write_calls) == 1
    assert f"fingerprint: {new_fp}" in write_calls[0]["stdin"]
    assert f"updated_at: {later_timestamp}" in write_calls[0]["stdin"]


# ===========================================================================
# 6. Missing Current-State, Malformed Fingerprint, and Legacy Note Behavior
# ===========================================================================


@pytest.mark.parametrize(
    "existing_read_stdout,should_write",
    [
        # Missing note (rc=0 with null fields in Basic Memory 0.23.2)
        (NULL_NOTE_STDOUT, True),
        # Malformed fingerprint (not a 64-char hex string)
        (
            json.dumps({
                "title": "Current State",
                "frontmatter": {"fingerprint": "corrupted-or-malformed-hash"},
            }),
            True,
        ),
        # Empty fingerprint in frontmatter
        (
            json.dumps({
                "title": "Current State",
                "frontmatter": {"fingerprint": ""},
            }),
            True,
        ),
        # Legacy note: note exists but lacks frontmatter or fingerprint entirely
        (
            json.dumps({
                "title": "Current State",
                "content": "# Legacy Note Without Fingerprint\nOld content.",
            }),
            True,
        ),
    ],
)
def test_missing_current_state_malformed_untrusted_fingerprint_and_legacy_note(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
    existing_read_stdout: str | None,
    should_write: bool,
):
    """Test that missing note, malformed hash, or legacy note all safely trigger fresh write."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    req = CheckpointRequest(
        project=p_git,
        condition="Condition test.",
        objective="Objective test.",
    )

    write_calls: list[dict[str, Any]] = []

    def mock_read_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float):
        return BasicMemoryRunnerResult(returncode=0, stdout=existing_read_stdout, stderr="")

    def mock_write_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
        stdin: str = "",
    ):
        write_calls.append({"argv": argv, "stdin": stdin})
        return CheckpointRunnerResult(returncode=0, stdout="{}", stderr="")

    res = write_checkpoint(
        personal_tideway_config,
        req,
        read_runner=mock_read_runner,
        write_runner=mock_write_runner,
        clock=lambda: "2026-09-11T15:00:00Z",
        registry=reg,
    )

    if should_write:
        assert res.written is True
        assert len(write_calls) == 1
    else:
        assert res.written is False
        assert len(write_calls) == 0


# ===========================================================================
# 7. Write Nonzero, Crash, Timeout, and Oversize Output Fail Closed & Leak Nothing
# ===========================================================================


def test_write_nonzero_exit_code_fails_closed_and_leaks_nothing(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test write runner non-zero exit code fails closed with safe fixed error."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    req = CheckpointRequest(
        project=p_git,
        condition="Condition text.",
        objective="Objective text.",
    )

    def failing_write_runner(*_args, **_kwargs):
        return CheckpointRunnerResult(
            returncode=1,
            stdout="",
            stderr="internal DB error: /secret/path/to/db.sqlite: permission denied",
        )

    with pytest.raises(RuntimeProbeError) as exc_info:
        write_checkpoint(
            personal_tideway_config,
            req,
            read_runner=lambda *a, **kw: BasicMemoryRunnerResult(returncode=0, stdout=NULL_NOTE_STDOUT, stderr=""),
            write_runner=failing_write_runner,
            registry=reg,
        )

    msg = str(exc_info.value)
    assert msg in (
        "Basic Memory write-note failed with non-zero exit code.",
        "Basic Memory write failed with non-zero exit code.",
    )
    assert "internal DB error" not in msg
    assert "/secret/path" not in msg


def test_write_crash_and_timeout_fail_closed_and_leak_nothing(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test write runner crash and timeout raise fixed safe errors."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    req = CheckpointRequest(
        project=p_git,
        condition="Condition text.",
        objective="Objective text.",
    )

    def crash_write_runner(*_args, **_kwargs):
        raise RuntimeError("Subprocess segfault at 0xDEADBEEF in /usr/local/bin")

    with pytest.raises(RuntimeProbeError) as exc_crash:
        write_checkpoint(
            personal_tideway_config,
            req,
            read_runner=lambda *a, **kw: BasicMemoryRunnerResult(returncode=0, stdout=NULL_NOTE_STDOUT, stderr=""),
            write_runner=crash_write_runner,
            registry=reg,
        )

    assert str(exc_crash.value) == "Basic Memory write-note execution failed."
    assert "DEADBEEF" not in str(exc_crash.value)

    def timeout_write_runner(*_args, **_kwargs):
        raise TimeoutError("Timed out after 15s")

    with pytest.raises(RuntimeProbeError) as exc_timeout:
        write_checkpoint(
            personal_tideway_config,
            req,
            read_runner=lambda *a, **kw: BasicMemoryRunnerResult(returncode=0, stdout=NULL_NOTE_STDOUT, stderr=""),
            write_runner=timeout_write_runner,
            registry=reg,
        )

    assert str(exc_timeout.value) == "Basic Memory write-note timed out."


def test_write_oversize_output_fails_closed(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test write runner output exceeding bounded byte cap fails closed."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    req = CheckpointRequest(
        project=p_git,
        condition="Condition text.",
        objective="Objective text.",
    )

    def oversize_write_runner(*_args, **_kwargs):
        return CheckpointRunnerResult(
            returncode=0,
            stdout="A" * (MAX_CHECKPOINT_OUTPUT_BYTES + 100),
            stderr="",
        )

    with pytest.raises(RuntimeProbeError) as exc_info:
        write_checkpoint(
            personal_tideway_config,
            req,
            read_runner=lambda *a, **kw: BasicMemoryRunnerResult(returncode=0, stdout=NULL_NOTE_STDOUT, stderr=""),
            write_runner=oversize_write_runner,
            registry=reg,
        )

    assert str(exc_info.value) == "Basic Memory write output exceeded maximum allowed size."


# ===========================================================================
# 8. Known Token, Private-Key, Password, and Auth Patterns Rejected Before Probing
# ===========================================================================


@pytest.mark.parametrize(
    "secret_snippet",
    [
        # Private key pattern
        synthetic_secret("-----", "BEGIN RSA ", "PRIVATE KEY-----\n", "fixture", "\n-----END-----"),
        synthetic_secret("-----", "BEGIN OPENSSH ", "PRIVATE KEY-----\n", "fixture", "\n-----END-----"),
        # GitHub tokens
        synthetic_secret("gh", "p_", "SyntheticFakeToken1234567890abcdef"),
        synthetic_secret("github_", "pat_", "SyntheticPat1234567890abcdef1234567890"),
        # GitLab token
        synthetic_secret("gl", "pat-", "SyntheticGitlabToken1234567890"),
        # Slack token
        synthetic_secret("xo", "xb-", "123456789012-123456789012-abcdef123456"),
        # AWS Key
        synthetic_secret("AK", "IA", "IOSFODNN7EXAMPLE"),
        # JWT token pattern
        synthetic_secret(
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            ".",
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
            ".",
            "synthetic_signature_1234567890",
        ),
        # Password assignment
        synthetic_secret("pass", "word = '", "SyntheticSecretPassword123!", "'"),
        synthetic_secret("pass", "wd: \"", "SecretPass999", "\""),
        # API key assignment
        synthetic_secret("api_", "key = '", "sk-SyntheticFakeKey1234567890", "'"),
        synthetic_secret("access_", "token: '", "synthetic_secret_access_value", "'"),
        # Bearer / Auth header
        synthetic_secret("Author", "ization: Bearer ", "synthetic_bearer_token_value_xyz"),
        synthetic_secret("bear", "er ", "synthetic_token_string_long_enough_123"),
    ],
)
def test_secrets_rejected_before_runner_probe_or_lock(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
    secret_snippet: str,
):
    """Verify secrets and credential assignment patterns are rejected before any runner or probe."""
    # Executable does not exist on disk; if probe ran, it would fail with RuntimeProbeError
    layout = get_basic_memory_layout(personal_tideway_config)
    assert not layout.primary_executable.exists()

    def guarded_runner(*_args, **_kwargs):
        raise AssertionError("Runner must not be called when secrets are present!")

    with pytest.raises((CheckpointSecretError, ValidationError, SecretsError)) as exc_info:
        CheckpointPayload(
            condition=f"Deploying with {secret_snippet}",
            objective="Setup auth.",
        )

    err_msg = str(exc_info.value)
    assert err_msg == "Potential secret or credential pattern detected in checkpoint payload."
    # Never echo the suspected secret value
    assert secret_snippet not in err_msg


# ===========================================================================
# 9. Unresolved/Ambiguous Projects and Unsafe Paths Fail Closed
# ===========================================================================


def test_unresolved_and_ambiguous_projects_fail_closed(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test that missing or ambiguous projects fail closed with ValidationError."""
    reg, _p_git, _p_dir, _ = create_test_projects(tmp_path)

    # 1. Non-existent project
    req_missing = CheckpointRequest(
        project="completely-unknown-project",
        condition="Clean.",
        objective="Objective.",
    )
    with pytest.raises(ValidationError, match="Project not found in registry"):
        plan_checkpoint_write(personal_tideway_config, req_missing, registry=reg)

    # 2. Ambiguous project reference (multiple projects match same query)
    p_dup1 = ProjectRecord.create(slug="proj-dup-1", display_name="Dup 1", kind="directory", aliases=["shared-alias"])
    p_dup2 = ProjectRecord.create(slug="proj-dup-2", display_name="Dup 2", kind="directory", aliases=["shared-alias"])
    ambiguous_reg = ProjectRegistry(projects=[p_dup1, p_dup2])

    req_ambiguous = CheckpointRequest(
        project="shared-alias",
        condition="Clean.",
        objective="Objective.",
    )
    with pytest.raises(ValidationError, match="Ambiguous project reference"):
        plan_checkpoint_write(personal_tideway_config, req_ambiguous, registry=ambiguous_reg)

    # 3. Forged ProjectRecord not in registry
    forged = ProjectRecord.create(slug="forged", display_name="Forged", kind="external")
    req_forged = CheckpointRequest(
        project=forged,
        condition="Clean.",
        objective="Objective.",
    )
    with pytest.raises(ValidationError, match="Project record not found in registry"):
        plan_checkpoint_write(personal_tideway_config, req_forged, registry=reg)


# ===========================================================================
# 10. Lock Contention and Hostile Lock Path Tests
# ===========================================================================


def test_lock_contention_raises_conflict_error(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test lock contention when lock file is already held by another process/fd."""
    _reg, p_git, _, _ = create_test_projects(tmp_path)
    personal_tideway_config.locks_dir.mkdir(parents=True, exist_ok=True)
    lock_file = personal_tideway_config.locks_dir / f"checkpoint-{p_git.id}.lock"

    # Pre-acquire lock externally
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    try:
        with (
            pytest.raises((CheckpointLockContentionError, ConflictError, RuntimeProbeError)),
            acquire_project_checkpoint_lock(personal_tideway_config, p_git),
        ):
            pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_hostile_lock_symlink_and_hardlink_fail_closed(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test hostile symlink, directory, and hardlink lock targets are rejected."""
    _reg, p_git, _, _ = create_test_projects(tmp_path)
    personal_tideway_config.locks_dir.mkdir(parents=True, exist_ok=True)
    lock_file = personal_tideway_config.locks_dir / f"checkpoint-{p_git.id}.lock"

    # 1. Symlink lock path
    target_file = tmp_path / "symlink_target.txt"
    target_file.write_text("evil", encoding="utf-8")
    lock_file.symlink_to(target_file)

    with (
        pytest.raises(BoundaryError),
        acquire_project_checkpoint_lock(personal_tideway_config, p_git),
    ):
        pass

    lock_file.unlink()

    # 2. Directory lock path
    lock_file.mkdir(exist_ok=True)
    with (
        pytest.raises(BoundaryError),
        acquire_project_checkpoint_lock(personal_tideway_config, p_git),
    ):
        pass

    lock_file.rmdir()

    # 3. Hardlink lock path (st_nlink > 1)
    real_file = tmp_path / "real_file.txt"
    real_file.write_text("lock data", encoding="utf-8")
    os.link(str(real_file), str(lock_file))
    assert lock_file.stat().st_nlink > 1

    with (
        pytest.raises(BoundaryError),
        acquire_project_checkpoint_lock(personal_tideway_config, p_git),
    ):
        pass

    lock_file.unlink()


# ===========================================================================
# 11. Bounded Unicode, Collection Limits, and Invalid Values
# ===========================================================================


def test_bounded_unicode_collection_limits_and_validation_errors():
    """Test finite size/count limits, control characters, and invalid values."""
    # 1. Invalid verification status
    with pytest.raises(ValidationError, match="verification_status must be one of"):
        CheckpointPayload(
            condition="Valid.",
            objective="Valid.",
            verification_status="invalid_status",
        )

    # 2. Invalid source client
    with pytest.raises(ValidationError, match="source_client must be one of"):
        CheckpointPayload(
            condition="Valid.",
            objective="Valid.",
            source_client="unknown_client",
        )

    # 3. Control characters (null byte)
    with pytest.raises(ValidationError, match="contains disallowed control characters"):
        CheckpointPayload(
            condition="Valid with \x00 null byte.",
            objective="Valid.",
        )

    # 4. Exceeding max field length
    with pytest.raises(ValidationError, match="exceeds maximum length"):
        CheckpointPayload(
            condition="A" * (MAX_FIELD_CHARS + 1),
            objective="Valid.",
        )

    # 5. Exceeding collection count
    with pytest.raises(ValidationError, match="completed collection exceeds maximum limit"):
        CheckpointPayload(
            condition="Valid.",
            objective="Valid.",
            completed=["item"] * (MAX_COLLECTION_ITEMS + 1),
        )

    # 6. Invalid evidence reference (traversal or leading dash)
    with pytest.raises(ValidationError, match="cannot begin with a dash"):
        CheckpointPayload(
            condition="Valid.",
            objective="Valid.",
            evidence=["--invalid-flag"],
        )

    with pytest.raises(ValidationError, match="cannot contain '..' path traversal"):
        CheckpointPayload(
            condition="Valid.",
            objective="Valid.",
            evidence=["notes/../../escape"],
        )

    with pytest.raises(ValidationError, match="cannot be an absolute path"):
        CheckpointPayload(
            condition="Valid.",
            objective="Valid.",
            evidence=["/etc/shadow"],
        )

    # 7. Request max_chars bounds
    with pytest.raises(ValidationError, match="max_chars must be an integer"):
        CheckpointRequest(
            project="alpha",
            condition="Cond",
            objective="Obj",
            max_chars=HARD_MAX_CHECKPOINT_CHARS + 100,
        )


# ===========================================================================
# 12. No Writes Into a Registered Source Tree
# ===========================================================================


def test_no_writes_into_registered_source_tree(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Verify that checkpoint writes never mutate files within the project's source tree."""
    reg, p_git, p_dir, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    git_source_root = Path(p_git.bindings.paths[0])
    dir_source_root = Path(p_dir.bindings.paths[0])
    before_git = snapshot_filesystem(git_source_root)
    before_dir = snapshot_filesystem(dir_source_root)

    req = CheckpointRequest(
        project=p_git,
        condition="Project in good shape.",
        objective="Preserve source tree untouched.",
        verification_status="verified",
    )

    def mock_read_runner(*_args, **_kwargs):
        return BasicMemoryRunnerResult(returncode=0, stdout=NULL_NOTE_STDOUT, stderr="")

    def mock_write_runner(*_args, **_kwargs):
        return CheckpointRunnerResult(returncode=0, stdout="{}", stderr="")

    res = write_checkpoint(
        personal_tideway_config,
        req,
        read_runner=mock_read_runner,
        write_runner=mock_write_runner,
        clock=lambda: "2026-09-11T12:00:00Z",
        registry=reg,
    )

    assert res.written is True

    # Source tree must remain byte-for-byte identical
    after_git = snapshot_filesystem(git_source_root)
    after_dir = snapshot_filesystem(dir_source_root)
    assert before_git == after_git
    assert before_dir == after_dir


# ===========================================================================
# 13. Acceptance Tests for Hardened Checkpoint Implementation
# ===========================================================================


def test_defaults_perform_read_and_idempotent_skip(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Defaults perform read; matching fingerprint produces skipped result and no write."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    payload = CheckpointPayload(
        condition="Clean build passes; database migration applied.",
        objective="Verify production defaults idempotence.",
        verification_status="verified",
    )
    req = CheckpointRequest(project=p_git, payload=payload)

    runner_calls: list[dict[str, Any]] = []
    stored_note_json: str | None = None

    def fake_subprocess_runner(
        argv: tuple[str, ...] | Sequence[str],
        env: Mapping[str, str],
        timeout: float = 15.0,
        stdin: str = "",
    ) -> CheckpointRunnerResult:
        nonlocal stored_note_json
        runner_calls.append({"argv": list(argv), "stdin": stdin})
        argv_list = list(argv)
        if "read-note" in argv_list:
            if stored_note_json is None:
                return CheckpointRunnerResult(returncode=0, stdout=NULL_NOTE_STDOUT, stderr="")
            return CheckpointRunnerResult(returncode=0, stdout=stored_note_json, stderr="")
        elif "write-note" in argv_list:
            fp = compute_semantic_fingerprint(payload)
            stored_note_json = json.dumps({
                "title": CURRENT_STATE_TITLE,
                "permalink": CURRENT_STATE_PERMALINK,
                "content": stdin,
                "frontmatter": {
                    "title": CURRENT_STATE_TITLE,
                    "type": "state",
                    "status": "verified",
                    "updated_at": "2026-09-12T10:00:00Z",
                    "source_client": "codex",
                    "fingerprint": fp,
                },
            })
            return CheckpointRunnerResult(
                returncode=0,
                stdout=json.dumps({"status": "success"}),
                stderr="",
            )
        raise AssertionError(f"Unexpected argv: {argv}")

    monkeypatch.setattr(
        "personal_tideway.core.checkpoint.checkpoint_subprocess_runner",
        fake_subprocess_runner,
    )

    # First write: calls read, then write
    res1 = write_checkpoint(personal_tideway_config, req, registry=reg)
    assert res1.written is True
    assert any("read-note" in c["argv"] for c in runner_calls)
    assert any("write-note" in c["argv"] for c in runner_calls)

    runner_calls.clear()

    # Second identical write: must perform read and skip write
    res2 = write_checkpoint(personal_tideway_config, req, registry=reg)
    assert res2.written is False
    assert res2.unchanged is True
    assert any("read-note" in c["argv"] for c in runner_calls)
    assert not any("write-note" in c["argv"] for c in runner_calls)


def test_read_rc_zero_null_note_proceeds_and_read_failures_fail_closed(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """rc=0 null note proceeds, but read rc!=0/timeout/exception fails closed before clock/write."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    req = CheckpointRequest(
        project=p_git,
        condition="Condition test.",
        objective="Objective test.",
    )

    # Case 1: rc=0 null note proceeds to write
    write_called = False

    def mock_read_null(argv: tuple[str, ...], env: Mapping[str, str], timeout: float):
        return BasicMemoryRunnerResult(returncode=0, stdout=NULL_NOTE_STDOUT, stderr="")

    def mock_write_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
        stdin: str = "",
    ):
        nonlocal write_called
        write_called = True
        return CheckpointRunnerResult(returncode=0, stdout="{}", stderr="")

    clock_called = False

    def mock_clock():
        nonlocal clock_called
        clock_called = True
        return "2026-09-12T12:00:00Z"

    res = write_checkpoint(
        personal_tideway_config,
        req,
        read_runner=mock_read_null,
        write_runner=mock_write_runner,
        clock=mock_clock,
        registry=reg,
    )
    assert res.written is True
    assert write_called is True
    assert clock_called is True

    # Case 2: rc!=0 fails closed before clock and write, and leaks no secrets
    def guarded_write(*_a, **_kw):
        raise AssertionError("Write runner must NOT be called on read failure!")

    def guarded_clock():
        raise AssertionError("Clock must NOT be called on read failure!")

    def failing_read(argv: tuple[str, ...], env: Mapping[str, str], timeout: float):
        return CheckpointRunnerResult(
            returncode=1,
            stdout="",
            stderr="secret-token-in-stderr-xyz",
        )

    with pytest.raises(RuntimeProbeError) as exc_rc:
        write_checkpoint(
            personal_tideway_config,
            req,
            read_runner=failing_read,
            write_runner=guarded_write,
            clock=guarded_clock,
            registry=reg,
        )
    assert str(exc_rc.value) == "Basic Memory read-note failed with non-zero exit code."
    assert "secret-token" not in str(exc_rc.value)

    # Case 3: timeout fails closed before clock and write
    def timeout_read(argv: tuple[str, ...], env: Mapping[str, str], timeout: float):
        raise TimeoutError("read timed out")

    with pytest.raises(RuntimeProbeError) as exc_timeout:
        write_checkpoint(
            personal_tideway_config,
            req,
            read_runner=timeout_read,
            write_runner=guarded_write,
            clock=guarded_clock,
            registry=reg,
        )
    assert str(exc_timeout.value) == "Basic Memory read-note timed out."

    # Case 4: runner exception fails closed before clock and write
    def crash_read(argv: tuple[str, ...], env: Mapping[str, str], timeout: float):
        raise RuntimeError("database /secret/path unreadable")

    with pytest.raises(RuntimeProbeError) as exc_crash:
        write_checkpoint(
            personal_tideway_config,
            req,
            read_runner=crash_read,
            write_runner=guarded_write,
            clock=guarded_clock,
            registry=reg,
        )
    assert str(exc_crash.value) == "Basic Memory read-note execution failed."
    assert "/secret/path" not in str(exc_crash.value)


def test_invalid_status_and_client_errors_do_not_echo_raw_values_or_secrets():
    """Invalid status/client errors do not contain raw value or embedded synthetic secret."""
    raw_status_secret = synthetic_secret("gh", "p_", "SyntheticSecretStatus1234567890abcdef")
    with pytest.raises((CheckpointSecretError, ValidationError, SecretsError)) as exc_stat_sec:
        CheckpointPayload(
            condition="Healthy.",
            objective="Deploy.",
            verification_status=raw_status_secret,
        )
    assert raw_status_secret not in str(exc_stat_sec.value)

    raw_client_secret = synthetic_secret("api_", "key = ", "sk_test_1234567890abcdef")
    with pytest.raises((CheckpointSecretError, ValidationError, SecretsError)) as exc_cli_sec:
        CheckpointPayload(
            condition="Healthy.",
            objective="Deploy.",
            source_client=raw_client_secret,
        )
    assert raw_client_secret not in str(exc_cli_sec.value)
    assert "sk_test" not in str(exc_cli_sec.value)

    # Ordinary invalid values (not secrets) must not echo the raw value
    raw_bad_status = "some_bogus_status_string_xyz"
    with pytest.raises(ValidationError) as exc_stat_bad:
        CheckpointPayload(
            condition="Healthy.",
            objective="Deploy.",
            verification_status=raw_bad_status,
        )
    assert raw_bad_status not in str(exc_stat_bad.value)

    raw_bad_client = "some_bogus_client_string_xyz"
    with pytest.raises(ValidationError) as exc_cli_bad:
        CheckpointPayload(
            condition="Healthy.",
            objective="Deploy.",
            source_client=raw_bad_client,
        )
    assert raw_bad_client not in str(exc_cli_bad.value)


def test_prose_examples_pass_and_synthetic_credentials_fail():
    """Ordinary prose examples pass and realistic synthetic credentials fail."""
    # The three ordinary prose examples must pass
    p1 = CheckpointPayload(condition="password: rotated", objective="Deploy v2.")
    assert p1.condition == "password: rotated"

    p2 = CheckpointPayload(condition="api_key: system", objective="Deploy v2.")
    assert p2.condition == "api_key: system"

    p3 = CheckpointPayload(condition="token: header", objective="Deploy v2.")
    assert p3.condition == "token: header"

    # Realistic synthetic assignments must fail
    with pytest.raises((CheckpointSecretError, SecretsError, ValidationError)):
        CheckpointPayload(
            condition=synthetic_secret(
                "Configured with api_",
                "key = ",
                "sk_test_1234567890abcdef",
            ),
            objective="Deploy v2.",
        )

    with pytest.raises((CheckpointSecretError, SecretsError, ValidationError)):
        CheckpointPayload(
            condition=synthetic_secret(
                "Using pass",
                "word: ",
                "CorrectHorseBatteryStaple42!",
            ),
            objective="Deploy v2.",
        )


def test_read_stdout_over_cap_fails_before_parsing_or_write(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Read stdout just over cap fails closed before parsing or write."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    req = CheckpointRequest(
        project=p_git,
        condition="Condition text.",
        objective="Objective text.",
    )

    def oversize_read_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float):
        # Use the checkpoint runner result because BasicMemoryRunnerResult itself
        # applies a smaller installer-level truncation in __post_init__.
        return CheckpointRunnerResult(
            returncode=0,
            stdout=" " * (MAX_CHECKPOINT_OUTPUT_BYTES + 1),
            stderr="",
        )

    def guarded_write(*_a, **_kw):
        raise AssertionError("Write runner must NOT be called when read exceeds cap!")

    def guarded_clock():
        raise AssertionError("Clock must NOT be called when read exceeds cap!")

    with pytest.raises(RuntimeProbeError) as exc_info:
        write_checkpoint(
            personal_tideway_config,
            req,
            read_runner=oversize_read_runner,
            write_runner=guarded_write,
            clock=guarded_clock,
            registry=reg,
        )
    assert str(exc_info.value) == "Basic Memory read output exceeded maximum allowed size."


def test_lock_nonregular_and_inode_mismatch_rejected(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Lock nonregular or inode mismatch is safely rejected with BoundaryError."""
    _reg, p_git, _, _ = create_test_projects(tmp_path)
    personal_tideway_config.locks_dir.mkdir(parents=True, exist_ok=True)
    lock_file = personal_tideway_config.locks_dir / f"checkpoint-{p_git.id}.lock"
    lock_file.write_text("lock", encoding="utf-8")

    real_fstat = os.fstat

    # 1. Inode mismatch
    def fake_fstat_inode(fd: int):
        st = real_fstat(fd)
        return os.stat_result((
            st.st_mode,
            st.st_ino + 999999,
            st.st_dev,
            st.st_nlink,
            st.st_uid,
            st.st_gid,
            st.st_size,
            st.st_atime,
            st.st_mtime,
            st.st_ctime,
        ))

    monkeypatch.setattr(os, "fstat", fake_fstat_inode)

    with (
        pytest.raises(BoundaryError, match="mismatch with lock path"),
        acquire_project_checkpoint_lock(personal_tideway_config, p_git),
    ):
        pass

    # 2. Non-regular file on fstat
    monkeypatch.undo()

    def fake_fstat_nonregular(fd: int):
        st = real_fstat(fd)
        import stat
        nonreg_mode = (st.st_mode & ~0o170000) | stat.S_IFIFO
        return os.stat_result((
            nonreg_mode,
            st.st_ino,
            st.st_dev,
            st.st_nlink,
            st.st_uid,
            st.st_gid,
            st.st_size,
            st.st_atime,
            st.st_mtime,
            st.st_ctime,
        ))

    monkeypatch.setattr(os, "fstat", fake_fstat_nonregular)

    with (
        pytest.raises(BoundaryError, match="not a regular file"),
        acquire_project_checkpoint_lock(personal_tideway_config, p_git),
    ):
        pass


def test_current_state_permalink_export_from_core():
    """CURRENT_STATE_PERMALINK is exported from core and matches checkpoint permalink."""
    from personal_tideway.core import CURRENT_STATE_PERMALINK as CORE_PERMALINK
    from personal_tideway.core.checkpoint import (
        CURRENT_STATE_PERMALINK as CHECKPOINT_PERMALINK,
    )

    assert CORE_PERMALINK == "current-state"
    assert CORE_PERMALINK == CHECKPOINT_PERMALINK


def test_min_checkpoint_chars_boundary_and_rejection_before_side_effects(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """max_chars below MIN fails before side effects; exact MIN is accepted if payload fits."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    # 1. Below MIN: rejected immediately in CheckpointRequest before any side effects
    with pytest.raises(ValidationError, match="max_chars must be an integer between"):
        CheckpointRequest(
            project=p_git,
            condition="Healthy.",
            objective="Ready.",
            max_chars=MIN_CHECKPOINT_CHARS - 1,
        )

    # 2. Exact MIN: accepted if payload fits
    req_exact = CheckpointRequest(
        project=p_git,
        condition="Healthy.",
        objective="Ready.",
        max_chars=MIN_CHECKPOINT_CHARS,
    )
    assert req_exact.max_chars == MIN_CHECKPOINT_CHARS

    write_executed = False

    def mock_read_null(argv: tuple[str, ...], env: Mapping[str, str], timeout: float):
        return BasicMemoryRunnerResult(returncode=0, stdout=NULL_NOTE_STDOUT, stderr="")

    def mock_write(argv: tuple[str, ...], env: Mapping[str, str], timeout: float, stdin: str = ""):
        nonlocal write_executed
        write_executed = True
        return CheckpointRunnerResult(returncode=0, stdout="{}", stderr="")

    res = write_checkpoint(
        personal_tideway_config,
        req_exact,
        read_runner=mock_read_null,
        write_runner=mock_write,
        clock=lambda: "2026-09-12T12:00:00Z",
        registry=reg,
    )
    assert res.written is True
    assert write_executed is True


def test_internal_type_error_runner_called_once_only(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """A runner that internally raises TypeError after a side effect is invoked exactly once."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    req = CheckpointRequest(
        project=p_git,
        condition="Healthy.",
        objective="Ready.",
    )

    write_calls = 0

    def buggy_write_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
        stdin: str = "",
    ):
        nonlocal write_calls
        write_calls += 1
        raise TypeError("internal bug inside custom runner")

    def mock_read_null(argv: tuple[str, ...], env: Mapping[str, str], timeout: float):
        return BasicMemoryRunnerResult(returncode=0, stdout=NULL_NOTE_STDOUT, stderr="")

    with pytest.raises(RuntimeProbeError) as exc_info:
        write_checkpoint(
            personal_tideway_config,
            req,
            read_runner=mock_read_null,
            write_runner=buggy_write_runner,
            clock=lambda: "2026-09-12T12:00:00Z",
            registry=reg,
        )

    assert str(exc_info.value) == "Basic Memory write-note execution failed."
    assert write_calls == 1


def test_three_argument_write_runner_is_rejected_before_invocation(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """A write runner unable to accept note stdin fails closed without being called."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    req = CheckpointRequest(
        project=p_git,
        condition="Healthy.",
        objective="Ready.",
    )
    write_calls = 0

    def incompatible_write_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> CheckpointRunnerResult:
        nonlocal write_calls
        write_calls += 1
        return CheckpointRunnerResult(returncode=0, stdout="{}", stderr="")

    def mock_read_null(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(
            returncode=0,
            stdout=NULL_NOTE_STDOUT,
            stderr="",
        )

    with pytest.raises(
        RuntimeProbeError,
        match="Write runner must accept stdin content",
    ):
        write_checkpoint(
            personal_tideway_config,
            req,
            read_runner=mock_read_null,
            write_runner=incompatible_write_runner,
            clock=lambda: "2026-09-12T12:00:00Z",
            registry=reg,
        )

    assert write_calls == 0
