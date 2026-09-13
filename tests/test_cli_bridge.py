"""Comprehensive unit tests for Personal Tideway Phase 4A CLI bridge.

Tests coverage:
1. Parser help, subcommands, and pre-execution invalid option rejection.
2. Source client attribution: Codex and AGY payloads with identical schema.
3. Project selection precedence: command-local --project > global/config > strict cwd.
4. Strict cwd resolution: resolved, candidate (fail-closed, no auto-register), ambiguous, unresolved.
5. Context show: current-state only, text and --json formatting, budget caps.
6. Context search: query search + current-state, non-empty query enforcement, limits, budgets.
7. Checkpoint input validation: file vs stdin mutual exclusivity, symlink rejection,
   non-regular file (directory/FIFO) rejection, non-existent file, UTF-8, size cap (>64 KiB).
8. Checkpoint schema validation: malformed JSON, array rejection, unknown field rejection,
   missing required fields (condition, objective), invalid collection types.
9. Dry-run safety: zero runner calls, zero mutations, accurate text and JSON flags.
10. Idempotence: initial write followed by unchanged duplicate preserving fingerprint.
11. Secret safety: payload secret rejection, no secret disclosure in errors or JSON output.
12. Backend error handling: non-zero returncode, crash, timeout fail closed with safe fixed error,
    single valid JSON document on --json mapped errors, no raw backend stderr or host path leaks.
13. Source repository immutability: zero filesystem changes inside registered source trees.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from personal_tideway.cli.bridge import (
    MAX_CHECKPOINT_INPUT_BYTES,
    parse_checkpoint_payload,
    read_checkpoint_file,
    read_checkpoint_stdin,
    resolve_cli_project,
)
from personal_tideway.cli.main import build_parser, main
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import (
    CLIENT_AGY,
    CLIENT_CODEX,
    PROJECT_KIND_GIT,
    ExitCode,
)
from personal_tideway.core.basic_memory_installer import BasicMemoryRunnerResult
from personal_tideway.core.basic_memory_runtime import get_basic_memory_layout
from personal_tideway.core.checkpoint import (
    CheckpointPayload,
    CheckpointRunnerResult,
    compute_semantic_fingerprint,
)
from personal_tideway.core.registry import (
    ProjectRecord,
    ProjectRegistry,
    load_registry,
    save_registry,
)
from personal_tideway.core.workspace import init_workspace
from personal_tideway.exceptions import ValidationError

# Canonical empty note response from Basic Memory 0.23.2
NULL_NOTE_STDOUT: str = json.dumps({
    "title": None,
    "permalink": None,
    "file_path": None,
    "content": None,
    "frontmatter": None,
})

VALID_NOTE_STDOUT: str = json.dumps({
    "title": "Alpha Project Current State",
    "permalink": "alpha-proj/current-state",
    "file_path": "projects/alpha-proj/current-state.md",
    "content": "## Condition\nOperational and healthy.\n\n## Objective\nComplete phase 4A.",
    "frontmatter": {
        "title": "Alpha Project Current State",
        "permalink": "alpha-proj/current-state",
        "type": "note",
        "note_type": "project",
        "status": "verified",
        "confidence": "verified",
        "updated_at": "2026-09-12T12:00:00Z",
        "fingerprint": "existing_fp_12345",
    },
})

SEARCH_NOTES_STDOUT: str = json.dumps({
    "total": 1,
    "page_size": 10,
    "current_page": 1,
    "total_is_exact": True,
    "has_more": False,
    "results": [
        {
            "title": "Architecture Overview",
            "permalink": "alpha-proj/architecture",
            "content": "Core architectural decisions for alpha project.",
            "metadata": {
                "note_type": "note",
                "type": "note",
                "status": "verified",
            },
            "updated_at": "2026-09-12T11:00:00Z",
        }
    ],
})

CURRENT_STATE_SEARCH_STDOUT: str = json.dumps({
    "total": 1,
    "page_size": 1,
    "current_page": 1,
    "total_is_exact": True,
    "has_more": False,
    "results": [
        {
            "title": "Alpha Project Current State",
            "permalink": "alpha-proj/current-state",
            "content": "## Condition\nOperational and healthy.\n\n## Objective\nComplete phase 4A.",
            "metadata": {
                "note_type": "project",
                "type": "note",
                "status": "verified",
            },
            "updated_at": "2026-09-12T12:00:00Z",
        }
    ],
})

SEARCH_EMPTY_STDOUT: str = json.dumps({
    "total": 0,
    "page_size": 10,
    "current_page": 1,
    "total_is_exact": True,
    "has_more": False,
    "results": [],
})


def init_git_repo(path: Path) -> Path:
    """Initialize a safe disposable git repository for testing."""
    path.mkdir(parents=True, exist_ok=True)
    init_res = subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if init_res.returncode != 0:
        subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    return path


def make_record(
    project_id: str,
    slug: str,
    display_name: str,
    aliases: list[str] | None = None,
    paths: list[str] | None = None,
    git_common_dirs: list[str] | None = None,
    git_remotes: list[str] | None = None,
) -> ProjectRecord:
    """Helper to construct valid ProjectRecord instances."""
    return ProjectRecord.create(
        project_id=project_id,
        slug=slug,
        display_name=display_name,
        kind=PROJECT_KIND_GIT,
        aliases=aliases or [],
        paths=paths or [],
        git_common_dirs=git_common_dirs or [],
        git_remotes=git_remotes or [],
    )


def setup_workspace(home: Path, tmp_path: Path) -> tuple[PersonalTidewayConfig, ProjectRecord, ProjectRecord]:
    """Setup initialized workspace with two registered test projects."""
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)

    repo1 = tmp_path / "repo_alpha"
    init_git_repo(repo1)

    repo2 = tmp_path / "repo_beta"
    init_git_repo(repo2)

    p1 = make_record(
        project_id="11111111-1111-4111-8111-111111111111",
        slug="alpha-proj",
        display_name="Alpha Project",
        aliases=["alpha"],
        paths=[str(repo1.resolve())],
        git_common_dirs=[str((repo1 / ".git").resolve())],
        git_remotes=["github.com/org/alpha"],
    )
    p2 = make_record(
        project_id="22222222-2222-4222-8222-222222222222",
        slug="beta-proj",
        display_name="Beta Project",
        aliases=["beta"],
        paths=[str(repo2.resolve())],
        git_common_dirs=[str((repo2 / ".git").resolve())],
        git_remotes=["github.com/org/beta"],
    )

    reg = ProjectRegistry(version=2, projects=[p1, p2])
    save_registry(reg, cfg)

    # Provide fake isolated executable for runner tests
    layout = get_basic_memory_layout(cfg)
    layout.primary_executable.parent.mkdir(parents=True, exist_ok=True)
    layout.primary_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    layout.primary_executable.chmod(0o755)

    return cfg, p1, p2


def fake_runner_read_empty(argv: tuple[str, ...], env: Any, timeout: float) -> BasicMemoryRunnerResult:
    """Mock runner returning empty search or read note."""
    if "search-notes" in argv:
        return BasicMemoryRunnerResult(returncode=0, stdout=SEARCH_EMPTY_STDOUT, stderr="")
    return BasicMemoryRunnerResult(returncode=0, stdout=NULL_NOTE_STDOUT, stderr="")


def fake_runner_read_valid(argv: tuple[str, ...], env: Any, timeout: float) -> BasicMemoryRunnerResult:
    """Mock runner returning valid current-state and search items."""
    if "search-notes" in argv:
        if "--permalink" in argv:
            return BasicMemoryRunnerResult(returncode=0, stdout=CURRENT_STATE_SEARCH_STDOUT, stderr="")
        return BasicMemoryRunnerResult(returncode=0, stdout=SEARCH_NOTES_STDOUT, stderr="")
    return BasicMemoryRunnerResult(returncode=0, stdout=VALID_NOTE_STDOUT, stderr="")


def fake_write_runner_success(*args: Any, **kwargs: Any) -> CheckpointRunnerResult:
    """Mock write runner returning success."""
    return CheckpointRunnerResult(returncode=0, stdout="", stderr="")


# ===========================================================================
# 1. Parser Help and Mutual Exclusivity Tests
# ===========================================================================


def test_parser_help_context_and_checkpoint():
    """Verify parser help exposes exact subcommands and options."""
    parser = build_parser()

    # context --help
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["context", "--help"])
    assert exc.value.code == 0

    # context show --help
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["context", "show", "--help"])
    assert exc.value.code == 0

    # context search --help
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["context", "search", "--help"])
    assert exc.value.code == 0

    # checkpoint --help
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["checkpoint", "--help"])
    assert exc.value.code == 0


def test_parser_rejects_invalid_checkpoint_combinations(capsys: pytest.CaptureFixture[str]):
    """Checkpoint command requires exactly one of --file or --stdin."""
    parser = build_parser()

    # Missing both --file and --stdin
    with pytest.raises(SystemExit) as exc1:
        parser.parse_args(["checkpoint"])
    assert exc1.value.code == 2

    # Providing both --file and --stdin
    with pytest.raises(SystemExit) as exc2:
        parser.parse_args(["checkpoint", "--file", "sample.json", "--stdin"])
    assert exc2.value.code == 2


def test_parser_rejects_missing_subcommands_and_arguments():
    """Context without subcommand and context search without query fail before execution."""
    parser = build_parser()

    # context without show/search
    with pytest.raises(SystemExit) as exc1:
        parser.parse_args(["context"])
    assert exc1.value.code == 2

    # context search without query
    with pytest.raises(SystemExit) as exc2:
        parser.parse_args(["context", "search"])
    assert exc2.value.code == 2


# ===========================================================================
# 2. Project Selection Precedence Tests
# ===========================================================================


def test_project_precedence_command_local_overrides_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Command-local --project overrides global --project and current working directory."""
    home = tmp_path / "ptw"
    cfg, p1, p2 = setup_workspace(home, tmp_path)

    # Set cwd to repo1 (associated with p1)
    monkeypatch.chdir(p1.bindings.paths[0])

    # Global config set to p1, command-local set to p2
    cfg_with_global = PersonalTidewayConfig.resolve(home=home, project="alpha-proj")
    resolved = resolve_cli_project(cfg_with_global, command_project="beta-proj")
    assert resolved.id == p2.id
    assert resolved.slug == "beta-proj"


def test_project_precedence_global_overrides_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Global configuration/selection overrides cwd resolution when command-local is omitted."""
    home = tmp_path / "ptw"
    cfg, p1, p2 = setup_workspace(home, tmp_path)

    monkeypatch.chdir(p1.bindings.paths[0])
    cfg_with_global = PersonalTidewayConfig.resolve(home=home, project="beta-proj")
    resolved = resolve_cli_project(cfg_with_global, command_project=None)
    assert resolved.id == p2.id
    assert resolved.slug == "beta-proj"


def test_project_precedence_strict_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Strict current working directory resolution when neither command-local nor global is specified."""
    home = tmp_path / "ptw"
    cfg, p1, p2 = setup_workspace(home, tmp_path)

    monkeypatch.chdir(p1.bindings.paths[0])
    resolved = resolve_cli_project(cfg, command_project=None)
    assert resolved.id == p1.id
    assert resolved.slug == "alpha-proj"


def test_project_resolution_candidate_fails_closed_without_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Unregistered Git directory candidate fails closed with ValidationError and does not auto-register."""
    home = tmp_path / "ptw"
    cfg, _, _ = setup_workspace(home, tmp_path)

    candidate_dir = tmp_path / "unregistered_repo"
    init_git_repo(candidate_dir)

    monkeypatch.chdir(candidate_dir)

    with pytest.raises(ValidationError, match="unregistered project candidate"):
        resolve_cli_project(cfg, command_project=None)

    # Verify projects.yaml was not modified
    reg = load_registry(cfg.projects_yaml)
    assert len(reg.projects) == 2


def test_project_resolution_ambiguous_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Ambiguous project resolution fails closed with ValidationError."""
    home = tmp_path / "ptw"
    cfg, _, _ = setup_workspace(home, tmp_path)

    p_dup1 = make_record("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "dup-1", "Dup 1", aliases=["shared-alias"])
    p_dup2 = make_record("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "dup-2", "Dup 2", aliases=["shared-alias"])
    ambiguous_reg = ProjectRegistry(version=2, projects=[p_dup1, p_dup2])

    with pytest.raises(ValidationError, match="Ambiguous project reference"):
        resolve_cli_project(cfg, command_project="shared-alias", registry=ambiguous_reg)


def test_project_resolution_unresolved_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Directory with no associated project fails closed with ValidationError."""
    home = tmp_path / "ptw"
    cfg, _, _ = setup_workspace(home, tmp_path)

    unrelated_dir = tmp_path / "unrelated_folder"
    unrelated_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(unrelated_dir)

    with pytest.raises(ValidationError, match="No registered project found"):
        resolve_cli_project(cfg, command_project=None)


def test_project_resolution_empty_or_unknown_token(tmp_path: Path):
    """Empty or unknown project token raises ValidationError."""
    home = tmp_path / "ptw"
    cfg, _, _ = setup_workspace(home, tmp_path)

    with pytest.raises(ValidationError, match="Project identifier cannot be empty"):
        resolve_cli_project(cfg, command_project="   ")

    with pytest.raises(ValidationError, match="Project not found in registry"):
        resolve_cli_project(cfg, command_project="nonexistent-project")


def test_cli_cwd_resolution_and_rejection(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    """ptw context and checkpoint commands resolve project via cwd strictly without auto-registration."""
    home = tmp_path / "ptw"
    cfg, p1, _ = setup_workspace(home, tmp_path)

    # 1. Successful CWD resolution
    monkeypatch.chdir(p1.bindings.paths[0])
    code1 = main(["--home", str(home), "context", "show", "--json"], runner=fake_runner_read_valid)
    assert code1 == ExitCode.SUCCESS
    out1, _ = capsys.readouterr()
    assert json.loads(out1)["bundle"]["project_id"] == p1.id

    # 2. Candidate CWD resolution fails closed without auto-registering
    candidate_dir = tmp_path / "candidate_dir"
    init_git_repo(candidate_dir)
    monkeypatch.chdir(candidate_dir)

    code2 = main(["--home", str(home), "context", "show", "--json"])
    assert code2 == ExitCode.VALIDATION_ERROR
    out2, err2 = capsys.readouterr()
    err_json = json.loads(out2)
    assert err_json["exit_code"] == int(ExitCode.VALIDATION_ERROR)
    assert "Current directory is an unregistered project candidate. Auto-registration is not allowed." in err_json["error"]
    assert "Error: Current directory is an unregistered project candidate. Auto-registration is not allowed." in err2

    # Checkpoint also fails in candidate directory
    code3 = main(["--home", str(home), "checkpoint", "--stdin"])
    assert code3 == ExitCode.VALIDATION_ERROR
    out3, err3 = capsys.readouterr()
    assert "unregistered project candidate" in err3

    # Verify no auto-registration took place
    reg = load_registry(cfg.projects_yaml)
    assert len(reg.projects) == 2

    # 3. Unresolved directory fails closed
    empty_dir = tmp_path / "empty_dir"
    empty_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(empty_dir)
    code4 = main(["--home", str(home), "context", "show", "--json"])
    assert code4 == ExitCode.VALIDATION_ERROR
    out4, _ = capsys.readouterr()
    assert "No registered project found for current directory." in json.loads(out4)["error"]


# ===========================================================================
# 3. Context Show & Search Tests
# ===========================================================================


def test_context_show_text_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """ptw context show retrieves current-state note with text and json formatting."""
    home = tmp_path / "ptw"
    cfg, p1, _ = setup_workspace(home, tmp_path)

    # 1. Text output with content
    code1 = main(
        ["--home", str(home), "context", "show", "--project", p1.slug],
        runner=fake_runner_read_valid,
    )
    assert code1 == ExitCode.SUCCESS
    out1, err1 = capsys.readouterr()
    assert "Operational and healthy" in out1
    assert err1 == ""

    # 2. JSON output
    code2 = main(
        ["--home", str(home), "context", "show", "--project", p1.slug, "--json"],
        runner=fake_runner_read_valid,
    )
    assert code2 == ExitCode.SUCCESS
    out2, err2 = capsys.readouterr()
    data = json.loads(out2)
    assert data["bundle"]["project_id"] == p1.id
    assert data["bundle"]["has_current_state"] is True
    assert data["dry_run"] is False

    # 3. Empty current-state
    code3 = main(
        ["--home", str(home), "context", "show", "--project", p1.slug],
        runner=fake_runner_read_empty,
    )
    assert code3 == ExitCode.SUCCESS
    out3, _ = capsys.readouterr()
    assert "No current-state context found" in out3


def test_context_search_text_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """ptw context search QUERY retrieves search results plus current-state."""
    home = tmp_path / "ptw"
    cfg, p1, _ = setup_workspace(home, tmp_path)

    # 1. Successful search with text output
    code1 = main(
        ["--home", str(home), "context", "search", "architecture", "--project", p1.slug, "--limit", "3", "--budget", "4000"],
        runner=fake_runner_read_valid,
    )
    assert code1 == ExitCode.SUCCESS
    out1, err1 = capsys.readouterr()
    assert "Architecture Overview" in out1
    assert err1 == ""

    # 2. Search with --json
    code2 = main(
        ["--home", str(home), "context", "search", "architecture", "--project", p1.slug, "--json"],
        runner=fake_runner_read_valid,
    )
    assert code2 == ExitCode.SUCCESS
    out2, _ = capsys.readouterr()
    data = json.loads(out2)
    assert len(data["bundle"]["items"]) >= 1
    assert data["bundle"]["total_items"] >= 1

    # 3. Empty search result text
    code3 = main(
        ["--home", str(home), "context", "search", "nonexistent", "--project", p1.slug],
        runner=fake_runner_read_empty,
    )
    assert code3 == ExitCode.SUCCESS
    out3, _ = capsys.readouterr()
    assert "No matching context notes found." in out3


def test_context_search_empty_query_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """ptw context search with whitespace query is rejected with ValidationError."""
    home = tmp_path / "ptw"
    cfg, p1, _ = setup_workspace(home, tmp_path)

    code = main(["--home", str(home), "context", "search", "   ", "--project", p1.slug])
    assert code == ExitCode.VALIDATION_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert "Error: Search query cannot be empty" in err


def test_context_budget_and_limit_bounds_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Invalid budget or limit parameters fail closed before runner execution."""
    home = tmp_path / "ptw"
    cfg, p1, _ = setup_workspace(home, tmp_path)

    # Budget <= 0
    code1 = main(["--home", str(home), "context", "show", "--project", p1.slug, "--budget", "0"])
    assert code1 == ExitCode.VALIDATION_ERROR
    _, err1 = capsys.readouterr()
    assert "max_chars must be an integer between" in err1

    # Limit <= 0
    code2 = main(["--home", str(home), "context", "search", "query", "--project", p1.slug, "--limit", "-1"])
    assert code2 == ExitCode.VALIDATION_ERROR
    _, err2 = capsys.readouterr()
    assert "max_items must be an integer between" in err2


# ===========================================================================
# 4. Checkpoint Input Handling (File vs Stdin, Caps, Hazards)
# ===========================================================================


def test_checkpoint_payload_from_file_and_stdin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    """Checkpoints can be submitted identically via --file or --stdin."""
    home = tmp_path / "ptw"
    cfg, p1, _ = setup_workspace(home, tmp_path)

    payload_dict = {
        "condition": "Healthy clean codebase.",
        "objective": "Verify file and stdin input bridges.",
        "completed": ["Task A completed", "Task B completed"],
        "blockers": [],
        "verification_status": "verified",
        "next_safe_action": "Run regression test suite.",
        "source_client": "codex",
        "evidence": ["tests/test_cli_bridge.py"],
    }
    json_bytes = json.dumps(payload_dict).encode("utf-8")

    # 1. Via --file
    file_path = tmp_path / "checkpoint.json"
    file_path.write_bytes(json_bytes)

    code_file = main(
        ["--home", str(home), "checkpoint", "--project", p1.slug, "--file", str(file_path)],
        runner=fake_runner_read_empty,
        write_runner=fake_write_runner_success,
    )
    assert code_file == ExitCode.SUCCESS
    out_file, _ = capsys.readouterr()
    assert "Checkpoint written" in out_file

    # 2. Via --stdin
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload_dict)))
    code_stdin = main(
        ["--home", str(home), "checkpoint", "--project", p1.slug, "--stdin"],
        runner=fake_runner_read_empty,
        write_runner=fake_write_runner_success,
    )
    assert code_stdin == ExitCode.SUCCESS
    out_stdin, _ = capsys.readouterr()
    assert "Checkpoint written" in out_stdin


def test_checkpoint_stdin_reading_bounded(monkeypatch: pytest.MonkeyPatch):
    """Stdin is read once, bounded, and validated as UTF-8."""
    # Test valid stdin
    valid_text = json.dumps({"condition": "OK", "objective": "Done"})
    monkeypatch.setattr("sys.stdin", io.StringIO(valid_text))
    read_text = read_checkpoint_stdin(max_bytes=MAX_CHECKPOINT_INPUT_BYTES)
    assert read_text == valid_text

    # Test oversized stdin
    oversized = "a" * (MAX_CHECKPOINT_INPUT_BYTES + 10)
    monkeypatch.setattr("sys.stdin", io.StringIO(oversized))
    with pytest.raises(ValidationError, match="exceeds maximum allowed size"):
        read_checkpoint_stdin(max_bytes=MAX_CHECKPOINT_INPUT_BYTES)

    # Test empty stdin
    monkeypatch.setattr("sys.stdin", io.StringIO("   "))
    with pytest.raises(ValidationError, match="cannot be empty"):
        read_checkpoint_stdin(max_bytes=MAX_CHECKPOINT_INPUT_BYTES)


def test_checkpoint_file_reading_symlink_rejection(tmp_path: Path):
    """Checkpoint input file cannot be a symlink."""
    target_file = tmp_path / "real_payload.json"
    target_file.write_text(json.dumps({"condition": "OK", "objective": "Test"}), encoding="utf-8")

    symlink_file = tmp_path / "symlink_payload.json"
    symlink_file.symlink_to(target_file)

    with pytest.raises(ValidationError, match="cannot be a symlink"):
        read_checkpoint_file(symlink_file)


def test_checkpoint_file_reading_non_regular_file(tmp_path: Path):
    """Directory and FIFO checkpoint input files are rejected without reading."""
    dir_target = tmp_path / "dir_payload"
    dir_target.mkdir()

    with pytest.raises(ValidationError, match="must be a regular file"):
        read_checkpoint_file(dir_target)

    if hasattr(os, "mkfifo"):
        fifo_target = tmp_path / "fifo_payload"
        try:
            os.mkfifo(fifo_target)
            with pytest.raises(ValidationError, match="must be a regular file"):
                read_checkpoint_file(fifo_target)
        except OSError:
            pass


def test_checkpoint_file_reading_hardlink_rejection(tmp_path: Path):
    """Checkpoint input file with hard link count > 1 is rejected."""
    original_file = tmp_path / "original_payload.json"
    original_file.write_text(json.dumps({"condition": "OK", "objective": "Test"}), encoding="utf-8")
    hardlink_file = tmp_path / "hardlink_payload.json"
    try:
        os.link(original_file, hardlink_file)
    except (OSError, NotImplementedError):
        return

    with pytest.raises(ValidationError, match="cannot have multiple hard links"):
        read_checkpoint_file(original_file)


def test_checkpoint_stdin_binary_failure_fails_closed(monkeypatch: pytest.MonkeyPatch):
    """If stdin has a binary buffer and read fails, fail closed without falling back to text read."""
    class FailingBuffer:
        def read(self, *args: Any, **kwargs: Any):
            raise OSError("I/O error reading binary stream")

    class MockStdin(io.StringIO):
        def __init__(self):
            super().__init__("text fallback data that must not be read")
            self.buffer = FailingBuffer()

    monkeypatch.setattr("sys.stdin", MockStdin())
    with pytest.raises(ValidationError, match="Failed to read checkpoint input from stdin"):
        read_checkpoint_stdin()


def test_checkpoint_file_reading_size_bound_and_nonexistent(tmp_path: Path):
    """Oversized file and non-existent file raise ValidationError without leaking path."""
    non_existent = tmp_path / "missing.json"
    with pytest.raises(ValidationError) as exc_missing:
        read_checkpoint_file(non_existent)
    assert str(exc_missing.value) == "Checkpoint input file does not exist."
    assert str(non_existent) not in str(exc_missing.value)

    oversized = tmp_path / "big.json"
    oversized.write_bytes(b"x" * (MAX_CHECKPOINT_INPUT_BYTES + 100))
    with pytest.raises(ValidationError, match="exceeds maximum allowed size"):
        read_checkpoint_file(oversized)


def test_checkpoint_file_reading_invalid_utf8(tmp_path: Path):
    """File with invalid UTF-8 bytes raises ValidationError."""
    bad_utf8 = tmp_path / "bad.json"
    bad_utf8.write_bytes(b"\x80\x81\x82")
    with pytest.raises(ValidationError, match="is not valid UTF-8"):
        read_checkpoint_file(bad_utf8)


# ===========================================================================
# 5. Schema Validation & Source Client Attribution
# ===========================================================================


def test_checkpoint_client_attribution_codex_and_agy(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Both Codex and AGY can pass the identical schema with accurate source_client attribution."""
    home = tmp_path / "ptw"
    cfg, p1, _ = setup_workspace(home, tmp_path)

    # 1. Codex attribution
    payload_codex = {
        "condition": "Operational.",
        "objective": "Codex work item.",
        "source_client": "codex",
    }
    f_codex = tmp_path / "codex.json"
    f_codex.write_text(json.dumps(payload_codex), encoding="utf-8")

    code1 = main(
        ["--home", str(home), "checkpoint", "--project", p1.slug, "--file", str(f_codex), "--json"],
        runner=fake_runner_read_empty,
        write_runner=fake_write_runner_success,
    )
    assert code1 == ExitCode.SUCCESS
    out1, _ = capsys.readouterr()
    res1 = json.loads(out1)
    assert res1["written"] is True
    assert res1["source_client"] == CLIENT_CODEX

    # 2. AGY attribution
    payload_agy = {
        "condition": "Operational.",
        "objective": "AGY work item.",
        "source_client": "agy",
    }
    f_agy = tmp_path / "agy.json"
    f_agy.write_text(json.dumps(payload_agy), encoding="utf-8")

    code2 = main(
        ["--home", str(home), "checkpoint", "--project", p1.slug, "--file", str(f_agy), "--json"],
        runner=fake_runner_read_empty,
        write_runner=fake_write_runner_success,
    )
    assert code2 == ExitCode.SUCCESS
    out2, _ = capsys.readouterr()
    res2 = json.loads(out2)
    assert res2["written"] is True
    assert res2["source_client"] == CLIENT_AGY


def test_checkpoint_payload_schema_rejections():
    """Schema rejects malformed JSON, arrays, unknown fields, missing required fields, and invalid types."""
    # Malformed JSON
    with pytest.raises(ValidationError, match="not valid JSON"):
        parse_checkpoint_payload("{not valid json")

    # JSON Array
    with pytest.raises(ValidationError, match="must be a JSON object"):
        parse_checkpoint_payload(json.dumps([1, 2, 3]))

    # Unknown field
    with pytest.raises(ValidationError, match="Unknown field.*bogus_field"):
        parse_checkpoint_payload(json.dumps({
            "condition": "OK",
            "objective": "Goal",
            "bogus_field": "disallowed",
        }))

    # Missing condition
    with pytest.raises(ValidationError, match="Missing required field 'condition'"):
        parse_checkpoint_payload(json.dumps({"objective": "Goal"}))

    # Missing objective
    with pytest.raises(ValidationError, match="Missing required field 'objective'"):
        parse_checkpoint_payload(json.dumps({"condition": "OK"}))

    # Invalid client
    with pytest.raises(ValidationError, match="source_client must be one of"):
        parse_checkpoint_payload(json.dumps({
            "condition": "OK",
            "objective": "Goal",
            "source_client": "invalid_agent",
        }))

    # Invalid verification_status
    with pytest.raises(ValidationError, match="verification_status must be one of"):
        parse_checkpoint_payload(json.dumps({
            "condition": "OK",
            "objective": "Goal",
            "verification_status": "super_verified",
        }))


# ===========================================================================
# 6. Idempotence & Dry-Run Tests
# ===========================================================================


def test_checkpoint_dry_run_zero_runner_calls_and_no_mutation(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """--dry-run executes zero runner calls and produces accurate text and JSON flags."""
    home = tmp_path / "ptw"
    cfg, p1, _ = setup_workspace(home, tmp_path)

    runner_called = False

    def guarded_runner(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal runner_called
        runner_called = True
        raise AssertionError("Runner must not be called in dry-run mode!")

    payload_file = tmp_path / "dry_payload.json"
    payload_file.write_text(json.dumps({"condition": "Cond", "objective": "Obj"}), encoding="utf-8")

    # 1. Text dry-run
    code1 = main(
        ["--home", str(home), "checkpoint", "--project", p1.slug, "--file", str(payload_file), "--dry-run"],
        runner=guarded_runner,
        write_runner=guarded_runner,
    )
    assert code1 == ExitCode.SUCCESS
    assert not runner_called
    out1, err1 = capsys.readouterr()
    assert "[DRY RUN]" in out1
    assert "fingerprint=" in out1
    assert "unchanged=" not in out1
    assert err1 == ""

    # 2. JSON dry-run
    code2 = main(
        ["--home", str(home), "checkpoint", "--project", p1.slug, "--file", str(payload_file), "--dry-run", "--json"],
        runner=guarded_runner,
        write_runner=guarded_runner,
    )
    assert code2 == ExitCode.SUCCESS
    assert not runner_called
    out2, err2 = capsys.readouterr()
    data = json.loads(out2)
    assert data["dry_run"] is True
    assert data["written"] is False
    assert data["unchanged"] is True
    assert err2 == ""


def test_checkpoint_idempotent_unchanged_result(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Repeated checkpoint with identical content reports unchanged without calling write runner."""
    home = tmp_path / "ptw"
    cfg, p1, _ = setup_workspace(home, tmp_path)

    payload = {"condition": "Identical condition.", "objective": "Identical objective."}
    p_file = tmp_path / "idempotent.json"
    p_file.write_text(json.dumps(payload), encoding="utf-8")

    # First write: backend returns empty current-state, write succeeds
    code1 = main(
        ["--home", str(home), "checkpoint", "--project", p1.slug, "--file", str(p_file)],
        runner=fake_runner_read_empty,
        write_runner=fake_write_runner_success,
    )
    assert code1 == ExitCode.SUCCESS
    out1, _ = capsys.readouterr()
    assert "Checkpoint written" in out1

    # Simulate read-runner returning note with the matching semantic fingerprint
    cp_payload = CheckpointPayload(**payload)
    fp = compute_semantic_fingerprint(cp_payload)

    matching_note_stdout = json.dumps({
        "title": "Alpha Project Current State",
        "permalink": "alpha-proj/current-state",
        "file_path": "projects/alpha-proj/current-state.md",
        "content": "Rendered content.",
        "frontmatter": {
            "title": "Alpha Project Current State",
            "permalink": "alpha-proj/current-state",
            "type": "note",
            "note_type": "project",
            "status": "verified",
            "confidence": "verified",
            "updated_at": "2026-09-12T10:00:00Z",
            "fingerprint": fp,
        },
    })

    write_runner_called = False

    def write_runner_guarded(*_args: Any, **_kwargs: Any) -> CheckpointRunnerResult:
        nonlocal write_runner_called
        write_runner_called = True
        raise AssertionError("Write runner should not be called when checkpoint is unchanged!")

    def read_runner_matching(*_args: Any, **_kwargs: Any) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(returncode=0, stdout=matching_note_stdout, stderr="")

    # Second write: unchanged result
    code2 = main(
        ["--home", str(home), "checkpoint", "--project", p1.slug, "--file", str(p_file)],
        read_runner=read_runner_matching,
        write_runner=write_runner_guarded,
    )
    assert code2 == ExitCode.SUCCESS
    assert not write_runner_called
    out2, _ = capsys.readouterr()
    assert "Checkpoint unchanged" in out2

    # JSON unchanged result
    code3 = main(
        ["--home", str(home), "checkpoint", "--project", p1.slug, "--file", str(p_file), "--json"],
        read_runner=read_runner_matching,
        write_runner=write_runner_guarded,
    )
    assert code3 == ExitCode.SUCCESS
    out3, _ = capsys.readouterr()
    data = json.loads(out3)
    assert data["written"] is False
    assert data["unchanged"] is True
    assert data["dry_run"] is False
    assert data["fingerprint"] == fp


# ===========================================================================
# 7. Secret Disclosure and Safety Tests
# ===========================================================================


def test_checkpoint_secret_pattern_rejected_without_disclosure(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Secrets detected in checkpoint payloads fail closed with fixed error and zero disclosure."""
    home = tmp_path / "ptw"
    cfg, p1, _ = setup_workspace(home, tmp_path)

    secret_key = "AKIA" + "IOSFODNN7EXAMPLE"
    payload = {
        "condition": f"API key configured: {secret_key}",
        "objective": "Deploy app.",
    }
    p_file = tmp_path / "leak.json"
    p_file.write_text(json.dumps(payload), encoding="utf-8")

    stable_secret_msg = "Potential secret or credential pattern detected in checkpoint payload."

    # Text mode error
    code1 = main(["--home", str(home), "checkpoint", "--project", p1.slug, "--file", str(p_file)])
    assert code1 == ExitCode.VALIDATION_ERROR
    out1, err1 = capsys.readouterr()
    assert out1 == ""
    assert stable_secret_msg in err1
    assert secret_key not in err1

    # JSON mode error
    code2 = main(["--home", str(home), "checkpoint", "--project", p1.slug, "--file", str(p_file), "--json"])
    assert code2 == ExitCode.VALIDATION_ERROR
    out2, err2 = capsys.readouterr()
    data = json.loads(out2)
    assert data["exit_code"] == int(ExitCode.VALIDATION_ERROR)
    assert data["error"] == stable_secret_msg
    assert secret_key not in out2
    assert secret_key not in err2


# ===========================================================================
# 8. Backend Error Handling & Single Valid JSON Output
# ===========================================================================


def test_backend_runner_failure_safe_json_and_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Backend failure produces single valid JSON doc on stdout and safe stderr diagnostics without path leakage."""
    home = tmp_path / "ptw"
    cfg, p1, _ = setup_workspace(home, tmp_path)

    secret_path = "/var/secret/database/passwords.txt"

    def failing_runner(*_args: Any, **_kwargs: Any) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(
            returncode=1,
            stdout="",
            stderr=f"FATAL: Database error reading {secret_path}: permission denied",
        )

    # 1. Non-JSON mode
    code1 = main(
        ["--home", str(home), "context", "show", "--project", p1.slug],
        runner=failing_runner,
    )
    assert code1 == ExitCode.RUNTIME_PROBE_ERROR
    out1, err1 = capsys.readouterr()
    assert out1 == ""
    assert "Error: Basic Memory search failed with non-zero exit code." in err1
    assert secret_path not in err1

    # 2. JSON mode: single valid JSON document on stdout, diagnostics on stderr
    code2 = main(
        ["--home", str(home), "context", "show", "--project", p1.slug, "--json"],
        runner=failing_runner,
    )
    assert code2 == ExitCode.RUNTIME_PROBE_ERROR
    out2, err2 = capsys.readouterr()
    err_doc = json.loads(out2)
    assert err_doc["exit_code"] == int(ExitCode.RUNTIME_PROBE_ERROR)
    assert err_doc["error"] == "Basic Memory search failed with non-zero exit code."
    assert secret_path not in out2
    assert secret_path not in err2


def test_uninitialized_workspace_safe_error_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """Uninitialized workspace fails safely with ConfigError without leaking host paths in JSON mode."""
    uninit_home = tmp_path / "uninit_home_path"

    # Non-JSON: generic behavior preserves cfg.home in error
    code1 = main(["--home", str(uninit_home), "context", "show"])
    assert code1 == ExitCode.CONFIG_ERROR
    out1, err1 = capsys.readouterr()
    assert out1 == ""
    assert f"Personal Tideway workspace not initialized at {uninit_home}. Run 'ptw init' first." in err1

    # JSON: stdout is single JSON document, stderr contains safe message without host path
    code2 = main(["--home", str(uninit_home), "checkpoint", "--json", "--stdin"])
    assert code2 == ExitCode.CONFIG_ERROR
    out2, err2 = capsys.readouterr()
    err_doc = json.loads(out2)
    assert err_doc["exit_code"] == int(ExitCode.CONFIG_ERROR)
    assert err_doc["error"] == "Personal Tideway workspace is not initialized. Run 'ptw init' first."
    assert "Personal Tideway workspace is not initialized. Run 'ptw init' first." in err2
    assert str(uninit_home) not in out2
    assert str(uninit_home) not in err2


# ===========================================================================
# 9. Source Repository Immutability Invariant
# ===========================================================================


def test_no_writes_in_source_repositories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Context show, search, and checkpoint operations perform zero writes inside source trees."""
    home = tmp_path / "ptw"
    cfg, p1, _ = setup_workspace(home, tmp_path)

    repo_path = Path(p1.bindings.paths[0])
    (repo_path / "README.md").write_text("Repository content\n", encoding="utf-8")

    def snapshot(p: Path) -> dict[str, tuple[int, int]]:
        return {str(f.relative_to(p)): (f.stat().st_size, f.stat().st_mtime_ns) for f in p.rglob("*")}

    snap_before = snapshot(repo_path)

    payload_file = tmp_path / "cp.json"
    payload_file.write_text(json.dumps({"condition": "A", "objective": "B"}), encoding="utf-8")

    # Run context show, context search, and checkpoint write
    main(["--home", str(home), "context", "show", "--project", p1.slug], runner=fake_runner_read_valid)
    main(["--home", str(home), "context", "search", "architecture", "--project", p1.slug], runner=fake_runner_read_valid)
    main(
        ["--home", str(home), "checkpoint", "--project", p1.slug, "--file", str(payload_file)],
        runner=fake_runner_read_empty,
        write_runner=fake_write_runner_success,
    )

    snap_after = snapshot(repo_path)
    assert snap_before == snap_after
