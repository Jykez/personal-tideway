"""Comprehensive unit tests for Basic Memory 0.23.2 read-only context retrieval adapter.

Work Package Context Loop 1 verification:
- Exact argv construction and explicit project/local/json flags.
- Pure planning and immutable previews without filesystem mutation.
- Dry-run mode produces zero runner calls and zero filesystem effects.
- Successful current-state-only, query-only, and combined context bundles.
- Deterministic ordering, case-insensitive permalink deduplication, max_items, max_chars, Unicode boundaries.
- Separate tests for exact semantic duplicate deduplication vs conflicting duplicate rejection.
- Missing current-state and zero results handling.
- External project kind support with robust argument inspection.
- Project resolution failure modes: not found, ambiguous, invalid record, forgery rejection, projects_yaml fallback.
- Executable missing, symlink escape, wrong layout, and replacement race revalidation.
- Child environment isolation excluding sensitive host variables.
- Subprocess timeouts, nonzero exit codes, and runner exceptions with fixed safe messages.
- Bounded output byte cap, invalid UTF-8/JSON, wrong scalar/list types, booleans-as-integers, negative counts.
- Strict status and confidence validation rejecting unknown values without upgrading to verified.
- Zero secret, path, query, note content, or stderr leakage in exceptions or to_dict serialization.
- Whole-tree filesystem snapshot verification proving zero mutations on owned state or source roots.
- read_project_note helper execution, frontmatter parsing, and schema validation.
- Small max_chars budgeting invariants guaranteeing rendered_text <= max_chars.
"""

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.core.basic_memory_installer import (
    BasicMemoryRunnerResult,
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
    get_basic_memory_layout,
)
from personal_tideway.core.context_retrieval import (
    CURRENT_STATE_PERMALINK_PATTERN,
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_ITEMS,
    DEFAULT_RETRIEVAL_TIMEOUT,
    HARD_MAX_CHARS,
    HARD_MAX_ITEMS,
    MAX_RETRIEVAL_OUTPUT_BYTES,
    VALID_STATUSES,
    ContextBundle,
    ContextItem,
    ContextRetrievalPlan,
    ContextRetrievalPlanPreview,
    ContextRetrievalRequest,
    ContextRetrievalResult,
    RetrievalRunnerResult,
    build_bounded_context_bundle,
    build_current_state_search_argv,
    build_read_note_argv,
    build_search_notes_argv,
    execute_context_retrieval_plan,
    is_current_state_permalink,
    parse_read_note_response,
    parse_search_notes_response,
    plan_context_retrieval,
    read_project_note,
    resolve_registered_project,
    retrieval_subprocess_runner,
    retrieve_context,
    validate_context_retrieval_plan,
)
from personal_tideway.core.registry import (
    ProjectMemory,
    ProjectRecord,
    ProjectRegistry,
    save_registry,
)
from personal_tideway.exceptions import (
    BoundaryError,
    RuntimeProbeError,
    ValidationError,
)


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


def create_test_projects(tmp_path: Path) -> tuple[ProjectRegistry, ProjectRecord, ProjectRecord, ProjectRecord]:
    """Create sample registry with git, directory, and external projects."""
    git_dir = tmp_path / "git_repo"
    git_dir.mkdir(parents=True, exist_ok=True)
    dir_dir = tmp_path / "local_docs"
    dir_dir.mkdir(parents=True, exist_ok=True)

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
# 1. Exact argv and explicit project/local/json flags
# ===========================================================================


def test_exact_argv_and_explicit_project_local_json_flags(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test exact argv generation with explicit --project, --local, and --json flags."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    exec_str = str(layout.primary_executable)

    # 1. Helper function for search-notes argv with query
    argv_search = build_search_notes_argv(
        executable=layout.primary_executable,
        project_name=p_git.memory.project_name,
        query="architecture design",
        page_size=5,
    )
    expected_search = (
        exec_str,
        "tool",
        "search-notes",
        "architecture design",
        "--page-size",
        "5",
        "--json",
        "--project",
        p_git.memory.project_name,
        "--local",
    )
    assert argv_search == expected_search

    # 2. Helper function for current-state search-notes argv
    argv_cs = build_current_state_search_argv(
        executable=layout.primary_executable,
        project_name=p_git.memory.project_name,
        page_size=1,
    )
    expected_cs = (
        exec_str,
        "tool",
        "search-notes",
        "--permalink",
        CURRENT_STATE_PERMALINK_PATTERN,
        "--page-size",
        "1",
        "--json",
        "--project",
        p_git.memory.project_name,
        "--local",
    )
    assert argv_cs == expected_cs

    # 3. Helper function for read-note argv
    argv_read = build_read_note_argv(
        executable=layout.primary_executable,
        project_name=p_git.memory.project_name,
        identifier="decisions/001-auth",
    )
    expected_read = (
        exec_str,
        "tool",
        "read-note",
        "decisions/001-auth",
        "--json",
        "--project",
        p_git.memory.project_name,
        "--local",
    )
    assert argv_read == expected_read

    # 4. Plan contains exact argv sequence for both current-state and query search
    req = ContextRetrievalRequest(
        project=p_git,
        query="database schema",
        max_items=7,
        include_current_state=True,
    )
    plan = plan_context_retrieval(personal_tideway_config, req, registry=reg)

    expected_q = (
        exec_str,
        "tool",
        "search-notes",
        "database schema",
        "--page-size",
        "7",
        "--json",
        "--project",
        p_git.memory.project_name,
        "--local",
    )

    assert plan.current_state_argv == expected_cs
    assert plan.query_argv == expected_q
    assert plan.search_argvs == (expected_cs, expected_q)

    # Every command explicitly includes --project, project_name, --local, and --json
    for argv in plan.search_argvs:
        assert "--project" in argv
        assert p_git.memory.project_name in argv
        assert "--local" in argv
        assert "--json" in argv
        assert argv[0] == exec_str


# ===========================================================================
# 2. Pure planning and immutable previews
# ===========================================================================


def test_pure_planning_and_immutable_previews(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test pure planning performs zero filesystem mutation and produces immutable preview."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    save_registry(reg, personal_tideway_config)

    before_snapshot = snapshot_filesystem(personal_tideway_config.home)

    req = ContextRetrievalRequest(project=p_git.slug, query="auth")
    plan = plan_context_retrieval(personal_tideway_config, req, registry=reg)
    assert isinstance(plan, ContextRetrievalPlan)
    validate_context_retrieval_plan(plan, cfg=personal_tideway_config)

    after_snapshot = snapshot_filesystem(personal_tideway_config.home)
    assert before_snapshot == after_snapshot, "Pure planner must not alter filesystem"

    # Immutability check on plan and request
    with pytest.raises(FrozenInstanceError):
        plan.timeout = 999.0  # type: ignore

    with pytest.raises(FrozenInstanceError):
        req.query = "mutated"  # type: ignore

    preview = plan.preview()
    assert isinstance(preview, ContextRetrievalPlanPreview)
    assert preview.project_id == p_git.id
    assert preview.project_name == p_git.memory.project_name
    assert preview.operation == "context_retrieval"
    assert len(preview.commands) == 2

    with pytest.raises(FrozenInstanceError):
        preview.project_id = "hacked"  # type: ignore

    p_dict = preview.to_dict()
    assert p_dict["project_id"] == p_git.id
    assert p_dict["project_name"] == p_git.memory.project_name
    assert p_dict["operation"] == "context_retrieval"
    assert "commands" in p_dict
    assert "<query_redacted>" in json.dumps(p_dict)
    # Ensure raw env_overrides and full host executable paths are not leaked in preview
    assert "env_overrides" not in p_dict
    assert "search_argvs" not in p_dict


# ===========================================================================
# 3. Dry-run has no runner/filesystem effects
# ===========================================================================


def test_dry_run_has_no_runner_or_filesystem_effects(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test dry-run returns safe preview, makes zero runner calls, and creates zero files."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    def failing_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        raise AssertionError("Runner must never be invoked during dry-run!")

    req = ContextRetrievalRequest(project=p_git, query="security")
    plan = plan_context_retrieval(personal_tideway_config, req, registry=reg)

    before_snapshot = snapshot_filesystem(personal_tideway_config.home)

    res = execute_context_retrieval_plan(
        plan,
        dry_run=True,
        runner=failing_runner,
        cfg=personal_tideway_config,
    )

    after_snapshot = snapshot_filesystem(personal_tideway_config.home)
    assert before_snapshot == after_snapshot, "Dry run must make no filesystem modifications"

    assert isinstance(res, ContextRetrievalResult)
    assert res.dry_run is True
    assert res.preview is not None
    assert len(res.preview.commands) == len(plan.search_argvs)
    assert res.bundle.total_items == 0
    assert len(res.bundle.items) == 0


# ===========================================================================
# 4. Successful current-state-only, query-only, and combined bundles
# ===========================================================================


def test_successful_bundles_current_state_only_query_only_and_combined(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test retrieval for current-state-only, query-only, and combined note bundles."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    cs_json = json.dumps({
        "results": [{
            "title": "Current Project State",
            "permalink": "current-state",
            "content": "Active objective: implement WP10. Status is verified.",
            "type": "state",
            "status": "verified",
            "updated_at": "2026-09-11T12:00:00Z",
            "file_path": "/tmp/notes/current-state.md",
        }],
        "total": 1,
        "page_size": 1,
        "current_page": 1,
    })

    query_json = json.dumps({
        "results": [
            {
                "title": "Decision 001 - Auth Architecture",
                "permalink": "decisions/001-auth",
                "content": "We chose JWT with RS256 for stateless validation.",
                "type": "decision",
                "status": "partial",
                "updated_at": "2026-09-10T10:00:00Z",
                "file_path": "/tmp/notes/001.md",
            },
            {
                "title": "Runbook - Deploy Service",
                "permalink": "runbooks/deploy",
                "content": "Steps to deploy to local docker compose.",
                "type": "runbook",
                "status": "planned",
                "updated_at": "2026-09-09T09:00:00Z",
                "file_path": "/tmp/notes/deploy.md",
            },
        ],
        "total": 2,
        "page_size": 5,
        "current_page": 1,
    })

    def mock_runner(
        argv: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
    ) -> BasicMemoryRunnerResult:
        if "--permalink" in argv:
            return BasicMemoryRunnerResult(returncode=0, stdout=cs_json, stderr="")
        return BasicMemoryRunnerResult(returncode=0, stdout=query_json, stderr="")

    # Subcase A: Current-state only (query=None)
    req_cs_only = ContextRetrievalRequest(project=p_git, query=None, include_current_state=True)
    res_cs = retrieve_context(personal_tideway_config, req_cs_only, runner=mock_runner, registry=reg)
    assert res_cs.bundle.total_items == 1
    assert res_cs.bundle.has_current_state is True
    assert res_cs.bundle.items[0].is_current_state is True
    assert res_cs.bundle.items[0].permalink == "current-state"
    assert res_cs.bundle.items[0].status == "verified"

    # Subcase B: Query only (include_current_state=False)
    req_q_only = ContextRetrievalRequest(
        project=p_git, query="auth deployment", include_current_state=False
    )
    res_q = retrieve_context(personal_tideway_config, req_q_only, runner=mock_runner, registry=reg)
    assert res_q.bundle.total_items == 2
    assert res_q.bundle.has_current_state is False
    assert res_q.bundle.items[0].permalink == "decisions/001-auth"
    assert res_q.bundle.items[1].permalink == "runbooks/deploy"

    # Subcase C: Combined bundle (current-state + query notes)
    req_combined = ContextRetrievalRequest(
        project=p_git, query="auth deployment", include_current_state=True
    )
    res_comb = retrieve_context(personal_tideway_config, req_combined, runner=mock_runner, registry=reg)
    assert res_comb.bundle.total_items == 3
    assert res_comb.bundle.has_current_state is True
    # Invariant: current-state MUST be ordered first!
    assert res_comb.bundle.items[0].is_current_state is True
    assert res_comb.bundle.items[0].permalink == "current-state"
    assert res_comb.bundle.items[1].permalink == "decisions/001-auth"
    assert res_comb.bundle.items[2].permalink == "runbooks/deploy"


# ===========================================================================
# 5. Deterministic ordering, deduplication, max_items, max_chars, Unicode
# ===========================================================================


def test_deterministic_ordering_deduplication_max_items_max_chars_and_unicode(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test case-insensitive deduplication, max_items, max_chars, and Unicode handling."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    # Current-state note with uppercase in permalink
    cs_json = json.dumps({
        "results": [{
            "title": "Current State Note",
            "permalink": "Current-State",
            "content": "Current state content with unicode: 🚀 Привет мир! Übergröße.",
            "status": "verified",
        }],
        "total": 1,
    })

    # Query results returning exact duplicate of current-state (case variation) and duplicate note Alpha
    query_json = json.dumps({
        "results": [
            {
                "title": "Current State Note",
                "permalink": "CURRENT-STATE",  # Exact duplicate of current state
                "content": "Current state content with unicode: 🚀 Привет мир! Übergröße.",
                "status": "verified",
            },
            {
                "title": "Note Alpha",
                "permalink": "notes/Alpha",
                "content": "Alpha content with emojis 🔑 🛡️ and Cyrillic текст.",
            },
            {
                "title": "Note Alpha",
                "permalink": "notes/alpha",  # Exact semantic duplicate of Alpha with different case
                "content": "Alpha content with emojis 🔑 🛡️ and Cyrillic текст.",
            },
            {
                "title": "Note Beta",
                "permalink": "notes/Beta",
                "content": "Beta content text.",
            },
        ],
        "total": 4,
    })

    def runner_with_dups(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        if "--permalink" in argv:
            return BasicMemoryRunnerResult(returncode=0, stdout=cs_json, stderr="")
        return BasicMemoryRunnerResult(returncode=0, stdout=query_json, stderr="")

    # 1. Deduplication test
    req = ContextRetrievalRequest(project=p_git, query="notes", max_items=10)
    res = retrieve_context(personal_tideway_config, req, runner=runner_with_dups, registry=reg)

    permalinks = [it.permalink for it in res.bundle.items]
    assert permalinks == ["Current-State", "notes/Alpha", "notes/Beta"]
    assert res.bundle.total_items == 3

    # 2. max_items budget test
    req_capped = ContextRetrievalRequest(project=p_git, query="notes", max_items=2)
    res_capped = retrieve_context(personal_tideway_config, req_capped, runner=runner_with_dups, registry=reg)
    assert res_capped.bundle.total_items == 2
    assert res_capped.bundle.truncated is True
    assert [it.permalink for it in res_capped.bundle.items] == ["Current-State", "notes/Alpha"]

    # 3. max_chars budget test with Unicode preservation
    req_char_bounded = ContextRetrievalRequest(
        project=p_git, query="notes", max_items=10, max_chars=120
    )
    res_char = retrieve_context(personal_tideway_config, req_char_bounded, runner=runner_with_dups, registry=reg)
    assert res_char.bundle.truncated is True
    assert len(res_char.bundle.rendered_text) <= 120
    assert "... [truncated]" in res_char.bundle.rendered_text
    assert res_char.bundle.total_chars == len(res_char.bundle.rendered_text)


# ===========================================================================
# 6. Separate tests: conflicting duplicate rejection vs exact deduplication
# ===========================================================================


def test_conflicting_duplicates_rejected():
    """Test that conflicting duplicate records for the same permalink are rejected."""
    # Case 1: Conflicting content
    conflict_content_json = json.dumps({
        "results": [
            {"title": "Note A", "permalink": "note-1", "content": "Content Alpha"},
            {"title": "Note A", "permalink": "note-1", "content": "Content Beta"},
        ]
    })
    with pytest.raises(RuntimeProbeError) as exc1:
        parse_search_notes_response(conflict_content_json, "pid", "pname")
    assert "contradictory or ambiguous results" in str(exc1.value)

    # Case 2: Conflicting title with case difference in permalink
    conflict_title_json = json.dumps({
        "results": [
            {"title": "Note A", "permalink": "note-1", "content": "Content"},
            {"title": "Note B", "permalink": "NOTE-1", "content": "Content"},
        ]
    })
    with pytest.raises(RuntimeProbeError) as exc2:
        parse_search_notes_response(conflict_title_json, "pid", "pname")
    assert "contradictory or ambiguous results" in str(exc2.value)

    # Case 3: Conflicting note_type
    conflict_type_json = json.dumps({
        "results": [
            {"title": "Note A", "permalink": "note-1", "content": "Content", "type": "decision"},
            {"title": "Note A", "permalink": "note-1", "content": "Content", "type": "runbook"},
        ]
    })
    with pytest.raises(RuntimeProbeError) as exc3:
        parse_search_notes_response(conflict_type_json, "pid", "pname")
    assert "contradictory or ambiguous results" in str(exc3.value)

    # Case 4: Conflicting status
    conflict_status_json = json.dumps({
        "results": [
            {"title": "Note A", "permalink": "note-1", "content": "Content", "status": "verified"},
            {"title": "Note A", "permalink": "note-1", "content": "Content", "status": "blocked"},
        ]
    })
    with pytest.raises(RuntimeProbeError) as exc4:
        parse_search_notes_response(conflict_status_json, "pid", "pname")
    assert "contradictory or ambiguous results" in str(exc4.value)

    # Case 5: Conflicting updated_at timestamp
    conflict_updated_json = json.dumps({
        "results": [
            {"title": "Note A", "permalink": "note-1", "content": "Content", "updated_at": "2026-09-10T10:00:00Z"},
            {"title": "Note A", "permalink": "note-1", "content": "Content", "updated_at": "2026-09-11T12:00:00Z"},
        ]
    })
    with pytest.raises(RuntimeProbeError) as exc5:
        parse_search_notes_response(conflict_updated_json, "pid", "pname")
    assert "contradictory or ambiguous results" in str(exc5.value)


def test_exact_semantic_duplicates_deduplicated():
    """Test that exact semantic duplicates with case variation in permalink are deduplicated."""
    exact_dup_json = json.dumps({
        "results": [
            {
                "title": "Architecture Note",
                "permalink": "notes/arch",
                "content": "System design details.",
                "type": "architecture",
                "status": "verified",
                "updated_at": "2026-09-11T12:00:00Z",
            },
            {
                "title": "Architecture Note",
                "permalink": "NOTES/ARCH",
                "content": "System design details.",
                "type": "architecture",
                "status": "verified",
                "updated_at": "2026-09-11T12:00:00Z",
            },
        ]
    })
    items = parse_search_notes_response(exact_dup_json, "pid", "pname")
    assert len(items) == 1
    assert items[0].permalink == "notes/arch"
    assert items[0].title == "Architecture Note"
    assert items[0].status == "verified"
    assert items[0].updated_at == "2026-09-11T12:00:00Z"


# ===========================================================================
# 7. Missing current-state and zero results
# ===========================================================================


def test_missing_current_state_and_zero_results(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test missing current-state safely returns zero results without failing closed."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    empty_cs_json = json.dumps({"results": [], "total": 0, "page_size": 1, "current_page": 1})

    def zero_results_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(returncode=0, stdout=empty_cs_json, stderr="")

    # Current-state search returns 0 results
    req = ContextRetrievalRequest(project=p_git, query=None, include_current_state=True)
    res = retrieve_context(personal_tideway_config, req, runner=zero_results_runner, registry=reg)

    assert res.bundle.total_items == 0
    assert res.bundle.has_current_state is False
    assert res.bundle.rendered_text == ""
    assert res.bundle.truncated is False


# ===========================================================================
# 8. External project support
# ===========================================================================


def test_external_project_support(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test that external projects are valid and support context retrieval without brittle argv index."""
    reg, _, _, p_ext = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    cs_json = json.dumps({
        "results": [{
            "title": "External State",
            "permalink": "current-state",
            "content": "External project tracking active notes.",
            "status": "verified",
        }],
        "total": 1,
    })

    def mock_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        assert "--project" in argv
        proj_idx = argv.index("--project")
        assert argv[proj_idx + 1] == p_ext.memory.project_name
        assert "--local" in argv
        assert "--json" in argv
        return BasicMemoryRunnerResult(returncode=0, stdout=cs_json, stderr="")

    req = ContextRetrievalRequest(project=p_ext.slug, include_current_state=True)
    res = retrieve_context(personal_tideway_config, req, runner=mock_runner, registry=reg)

    assert res.bundle.project_id == p_ext.id
    assert res.bundle.project_name == p_ext.memory.project_name
    assert res.bundle.total_items == 1
    assert res.bundle.items[0].is_current_state is True


# ===========================================================================
# 9. Project resolution failures, forgery rejection, and registry fallback
# ===========================================================================


def test_project_resolution_failures_fail_closed(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test resolution fails closed on missing, ambiguous, or invalid project records."""
    reg, p_git, _, _ = create_test_projects(tmp_path)

    # 1. Project not found
    req_missing = ContextRetrievalRequest(project="non-existent-project")
    with pytest.raises(ValidationError) as exc_missing:
        plan_context_retrieval(personal_tideway_config, req_missing, registry=reg)
    assert "Project not found in registry." in str(exc_missing.value)

    # 2. Empty project identifier
    with pytest.raises(ValidationError):
        ContextRetrievalRequest(project="   ")

    # 3. Invalid project type
    with pytest.raises(ValidationError):
        ContextRetrievalRequest(project=12345)  # type: ignore

    # 4. Ambiguous project lookup (two projects matching same alias)
    p_dup_alias = ProjectRecord.create(
        slug="collision-proj",
        display_name="Collision",
        kind="git",
        aliases=["alpha"],  # Conflicts with p_git alias
    )
    ambig_reg = ProjectRegistry(projects=[p_git, p_dup_alias])
    with pytest.raises(ValidationError) as exc_ambig:
        resolve_registered_project("alpha", ambig_reg)
    assert "Ambiguous project reference" in str(exc_ambig.value)

    # 5. Project record with missing or invalid memory
    broken_rec = ProjectRecord.create(slug="broken", display_name="Broken")
    object.__setattr__(broken_rec.memory, "project_name", "")
    with pytest.raises(ValidationError):
        resolve_registered_project(broken_rec, reg)


def test_forged_project_record_rejected(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test that forged or unregistered ProjectRecord instances fail closed."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    save_registry(reg, personal_tideway_config)

    # 1. Unknown ID
    fake_rec1 = ProjectRecord.create(slug="fake-slug", display_name="Fake Project")
    req1 = ContextRetrievalRequest(project=fake_rec1)
    with pytest.raises(ValidationError) as exc1:
        plan_context_retrieval(personal_tideway_config, req1, registry=reg)
    assert "Project record not found in registry." in str(exc1.value)

    # 2. Matching ID but mismatched slug
    forged_rec2 = ProjectRecord(
        id=p_git.id,
        slug="tampered-slug",
        display_name=p_git.display_name,
        kind=p_git.kind,
        bindings=p_git.bindings,
        memory=p_git.memory,
    )
    req2 = ContextRetrievalRequest(project=forged_rec2)
    with pytest.raises(ValidationError) as exc2:
        plan_context_retrieval(personal_tideway_config, req2, registry=reg)
    assert "Project record does not match registered project." in str(exc2.value)

    # 3. Matching ID but mismatched memory project_name
    forged_rec3 = ProjectRecord(
        id=p_git.id,
        slug=p_git.slug,
        display_name=p_git.display_name,
        kind=p_git.kind,
        bindings=p_git.bindings,
        memory=ProjectMemory(
            backend=p_git.memory.backend,
            project_name="tampered-memory-name",
            path=f"projects/{p_git.id}/memory",
        ),
    )
    req3 = ContextRetrievalRequest(project=forged_rec3)
    with pytest.raises(ValidationError) as exc3:
        plan_context_retrieval(personal_tideway_config, req3, registry=reg)
    assert "Project record does not match registered project." in str(exc3.value)


def test_registry_none_resolves_from_projects_yaml(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test that passing registry=None loads the registry from cfg.projects_yaml."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    save_registry(reg, personal_tideway_config)

    req = ContextRetrievalRequest(project="alpha-repo", query="test")
    plan = plan_context_retrieval(personal_tideway_config, req)
    assert plan.project_record.id == p_git.id
    assert plan.project_record.slug == p_git.slug


# ===========================================================================
# 10. Executable missing, symlink escape, wrong layout, and race revalidation
# ===========================================================================


def test_executable_validation_missing_symlink_escape_and_race_revalidation(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test fail-closed behavior on missing executable, symlink escape, and replacement race."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)

    req = ContextRetrievalRequest(project=p_git)
    plan = plan_context_retrieval(personal_tideway_config, req, registry=reg)

    # 1. Executable does not exist on disk
    if layout.primary_executable.exists():
        layout.primary_executable.unlink()

    with pytest.raises(RuntimeProbeError) as exc_missing:
        execute_context_retrieval_plan(plan, dry_run=False, cfg=personal_tideway_config)
    assert str(exc_missing.value) == "Basic Memory executable missing or invalid."

    # 2. Executable exists but is not executable
    create_fake_executable(layout.primary_executable)
    layout.primary_executable.chmod(0o644)
    with pytest.raises(RuntimeProbeError):
        execute_context_retrieval_plan(plan, dry_run=False, cfg=personal_tideway_config)

    # 3. Executable symlink escaping workspace raises BoundaryError from get_basic_memory_layout
    outside_bin = tmp_path / "outside_bin"
    create_fake_executable(outside_bin)
    layout.primary_executable.unlink()
    layout.primary_executable.symlink_to(outside_bin)

    with pytest.raises(BoundaryError):
        execute_context_retrieval_plan(plan, dry_run=False, cfg=personal_tideway_config)

    # 4. Replacement race revalidation: binary exists at plan time, deleted right before execution
    layout.primary_executable.unlink()
    create_fake_executable(layout.primary_executable)
    fresh_plan = plan_context_retrieval(personal_tideway_config, req, registry=reg)
    layout.primary_executable.unlink()

    with pytest.raises(RuntimeProbeError) as exc_race:
        execute_context_retrieval_plan(fresh_plan, dry_run=False, cfg=personal_tideway_config)
    assert str(exc_race.value) == "Basic Memory executable missing or invalid."


# ===========================================================================
# 11. Isolated environment excludes sensitive host variables
# ===========================================================================


def test_isolated_environment_excludes_sensitive_host_variables(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test that subprocess environment excludes host secrets, API keys, and virtualenvs."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    captured_env: dict[str, str] = {}

    def spy_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        captured_env.update(dict(env))
        return BasicMemoryRunnerResult(
            returncode=0,
            stdout=json.dumps({"results": [], "total": 0}),
            stderr="",
        )

    # Inject sensitive variables into host environment
    os.environ["OPENAI_API_KEY"] = "sk-secret-leaked-key"
    os.environ["GITHUB_TOKEN"] = "ghp_super_secret_token"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "aws_secret_key"
    os.environ["VIRTUAL_ENV"] = "/home/user/.venv"
    os.environ["UV_PROJECT_ENVIRONMENT"] = "/home/user/.uv"

    try:
        req = ContextRetrievalRequest(project=p_git)
        retrieve_context(
            personal_tideway_config,
            req,
            dry_run=False,
            runner=spy_runner,
            registry=reg,
        )
    finally:
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
        os.environ.pop("VIRTUAL_ENV", None)
        os.environ.pop("UV_PROJECT_ENVIRONMENT", None)

    # Assert none of the sensitive variables leaked into child environment
    assert "OPENAI_API_KEY" not in captured_env
    assert "GITHUB_TOKEN" not in captured_env
    assert "AWS_SECRET_ACCESS_KEY" not in captured_env
    assert "VIRTUAL_ENV" not in captured_env
    assert "UV_PROJECT_ENVIRONMENT" not in captured_env

    # Assert canonical overrides are present
    assert captured_env[ENV_UV_TOOL_DIR] == str(layout.uv_tool_dir)
    assert captured_env[ENV_UV_TOOL_BIN_DIR] == str(layout.bin_dir)
    assert captured_env[ENV_UV_CACHE_DIR] == str(layout.cache_dir)
    assert captured_env[ENV_BASIC_MEMORY_CONFIG_DIR] == str(layout.config_dir)
    assert captured_env[ENV_BASIC_MEMORY_AUTO_UPDATE] == BASIC_MEMORY_AUTO_UPDATE_VALUE
    assert captured_env[ENV_BASIC_MEMORY_NO_PROMOS] == BASIC_MEMORY_NO_PROMOS_VALUE


# ===========================================================================
# 12. Timeouts, nonzero exit, and runner exceptions with safe messages
# ===========================================================================


def test_timeouts_nonzero_and_runner_exceptions_fail_closed_safe_messages(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test timeout, nonzero exit, and runner exception handling with suppressed causes."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    # 1. Timeout failure
    def timeout_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        raise TimeoutError("Command timed out after 15s")

    req = ContextRetrievalRequest(project=p_git)
    with pytest.raises(RuntimeProbeError) as exc_timeout:
        retrieve_context(personal_tideway_config, req, runner=timeout_runner, registry=reg)

    assert str(exc_timeout.value) == "Basic Memory search timed out."
    assert exc_timeout.value.__cause__ is None

    # 2. Nonzero exit code
    def nonzero_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(returncode=1, stdout="", stderr="error details")

    with pytest.raises(RuntimeProbeError) as exc_nonzero:
        retrieve_context(personal_tideway_config, req, runner=nonzero_runner, registry=reg)

    assert str(exc_nonzero.value) == "Basic Memory search failed with non-zero exit code."
    assert exc_nonzero.value.__cause__ is None

    # 3. Runner exception
    def crashing_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        raise RuntimeError("Subprocess crashed with internal OS error")

    with pytest.raises(RuntimeProbeError) as exc_crash:
        retrieve_context(personal_tideway_config, req, runner=crashing_runner, registry=reg)

    assert str(exc_crash.value) == "Basic Memory search execution failed."
    assert exc_crash.value.__cause__ is None

    # 4. Immediate halt on first failure (no second command executed)
    calls: list[tuple[str, ...]] = []

    def fail_first_call(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        calls.append(argv)
        return BasicMemoryRunnerResult(returncode=2, stdout="", stderr="fail")

    req_two_calls = ContextRetrievalRequest(project=p_git, query="query2", include_current_state=True)
    with pytest.raises(RuntimeProbeError):
        retrieve_context(personal_tideway_config, req_two_calls, runner=fail_first_call, registry=reg)

    assert len(calls) == 1, "Execution must halt immediately on first command failure"


# ===========================================================================
# 13. Output byte cap, invalid UTF-8/JSON, wrong types, scalar validation
# ===========================================================================


def test_output_byte_cap_malformed_json_and_wrong_types():
    """Test schema validation rejecting oversized, malformed, or wrong typed responses."""
    # 1. Output byte cap exceeded
    huge_stdout = "x" * (MAX_RETRIEVAL_OUTPUT_BYTES + 10)
    with pytest.raises(RuntimeProbeError) as exc_huge:
        parse_search_notes_response(huge_stdout, "p-id", "p-name")
    assert "exceeded maximum allowed size" in str(exc_huge.value)

    # 2. Malformed JSON
    with pytest.raises(RuntimeProbeError) as exc_malformed:
        parse_search_notes_response("{not valid json", "p-id", "p-name")
    assert "returned malformed JSON" in str(exc_malformed.value)

    # 3. Root is not JSON object
    for bad_root in ["[1, 2, 3]", "\"string_root\"", "12345", "true", "null"]:
        with pytest.raises(RuntimeProbeError) as exc_root:
            parse_search_notes_response(bad_root, "p-id", "p-name")
        assert "response must be a JSON object" in str(exc_root.value)

    # 4. results field is not a list
    bad_results_json = json.dumps({"results": "not_a_list"})
    with pytest.raises(RuntimeProbeError) as exc_results:
        parse_search_notes_response(bad_results_json, "p-id", "p-name")
    assert "results field must be a list" in str(exc_results.value)

    # 5. Booleans as integers rejected
    bool_as_int_json = json.dumps({"results": [], "total": True})
    with pytest.raises(RuntimeProbeError) as exc_bool:
        parse_search_notes_response(bool_as_int_json, "p-id", "p-name")
    assert "field total is invalid" in str(exc_bool.value)

    # 6. Negative counts rejected
    neg_count_json = json.dumps({"results": [], "total": -5})
    with pytest.raises(RuntimeProbeError) as exc_neg:
        parse_search_notes_response(neg_count_json, "p-id", "p-name")
    assert "field total is invalid" in str(exc_neg.value)

    # 7. Result item is not a dict
    bad_item_json = json.dumps({"results": ["string_item"]})
    with pytest.raises(RuntimeProbeError) as exc_item:
        parse_search_notes_response(bad_item_json, "p-id", "p-name")
    assert "result item must be a JSON object" in str(exc_item.value)

    # 8. Result item title is not a string
    bad_title_json = json.dumps({"results": [{"title": 12345, "permalink": "ok"}]})
    with pytest.raises(RuntimeProbeError) as exc_title:
        parse_search_notes_response(bad_title_json, "p-id", "p-name")
    assert "result title is invalid" in str(exc_title.value)

    # 9. Result item permalink is empty or non-string
    bad_permalink_json = json.dumps({"results": [{"title": "title", "permalink": "  "}]})
    with pytest.raises(RuntimeProbeError) as exc_p:
        parse_search_notes_response(bad_permalink_json, "p-id", "p-name")
    assert "result permalink is invalid" in str(exc_p.value)


def test_invalid_encoding_or_surrogates_raises_runtime_probe_error():
    """Test that surrogate characters or encoding issues raise RuntimeProbeError."""
    surrogate_stdout = "{\x00\ud800}"
    with pytest.raises(RuntimeProbeError) as exc_surr:
        parse_search_notes_response(surrogate_stdout, "pid", "pname")
    assert "invalid encoding" in str(exc_surr.value)

    with pytest.raises(RuntimeProbeError) as exc_read_surr:
        parse_read_note_response(surrogate_stdout, "pid", "pname")
    assert "invalid encoding" in str(exc_read_surr.value)


def test_metadata_note_type_priority_over_entity_type():
    """Test that metadata.note_type takes precedence over root type in search response."""
    payload = json.dumps({
        "results": [{
            "title": "Note Title",
            "permalink": "notes/item",
            "type": "entity_type_fallback",
            "metadata": {"note_type": "specific_note_type"},
        }]
    })
    items = parse_search_notes_response(payload, "pid", "pname")
    assert items[0].note_type == "specific_note_type"


# ===========================================================================
# 14. Unknown status and confidence handling
# ===========================================================================


def test_unknown_status_and_confidence_handling():
    """Test status validation preserves allowed values and rejects unknown without upgrading."""
    # 1. Allowed statuses are preserved and lowercased
    for s in VALID_STATUSES:
        payload = json.dumps({
            "results": [{
                "title": f"Note {s}",
                "permalink": f"notes/{s}",
                "content": "content",
                "status": s.upper(),
            }]
        })
        items = parse_search_notes_response(payload, "pid", "pname")
        assert items[0].status == s

    # 2. Unknown status must remain None, not upgraded to verified
    for unknown_status in ["high", "in_progress", "done", "unknown", "experimental"]:
        payload = json.dumps({
            "results": [{
                "title": "Note",
                "permalink": "notes/item",
                "content": "content",
                "status": unknown_status,
            }]
        })
        items = parse_search_notes_response(payload, "pid", "pname")
        assert items[0].status is None, f"Unknown status '{unknown_status}' must not be upgraded"

    # 3. Missing status remains None
    payload_no_status = json.dumps({
        "results": [{
            "title": "Note",
            "permalink": "notes/item",
            "content": "content",
        }]
    })
    items = parse_search_notes_response(payload_no_status, "pid", "pname")
    assert items[0].status is None


# ===========================================================================
# 15. No leakage of path, query, note content, stderr, or secrets
# ===========================================================================


def test_no_leakage_of_path_query_content_stderr_or_secrets(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Assert error messages and to_dict representations never leak secrets or sensitive paths."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    sentinel_secret = "SUPER_SECRET_TOKEN_XYZ_987654321"
    sentinel_path = "/home/private/secret/path/to/note.md"
    sentinel_query = "CLASSIFIED_PROJECT_QUERY"
    sentinel_content = "TOP_SECRET_NOTE_BODY_CONTENT"
    sentinel_stderr = "FATAL: sensitive host credential leaked in stderr"

    # 1. Nonzero error does not leak stderr or query
    def leaky_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(returncode=1, stdout="", stderr=sentinel_stderr)

    req = ContextRetrievalRequest(project=p_git, query=sentinel_query)
    with pytest.raises(RuntimeProbeError) as exc_nonzero:
        retrieve_context(personal_tideway_config, req, runner=leaky_runner, registry=reg)

    err_msg = str(exc_nonzero.value)
    assert sentinel_secret not in err_msg
    assert sentinel_path not in err_msg
    assert sentinel_query not in err_msg
    assert sentinel_stderr not in err_msg
    assert exc_nonzero.value.__cause__ is None

    # 2. Malformed JSON with secrets does not leak stdout in error
    def leaky_json_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(
            returncode=0,
            stdout=f"{{ malformed json with secret: {sentinel_secret}, content: {sentinel_content} }}",
            stderr="",
        )

    with pytest.raises(RuntimeProbeError) as exc_malformed:
        retrieve_context(personal_tideway_config, req, runner=leaky_json_runner, registry=reg)

    assert sentinel_secret not in str(exc_malformed.value)
    assert sentinel_content not in str(exc_malformed.value)
    assert exc_malformed.value.__cause__ is None

    # 3. to_dict serialization never exposes file_path, raw stderr, or arbitrary metadata
    backend_payload = json.dumps({
        "results": [{
            "title": "Public Note",
            "permalink": "notes/pub",
            "content": "Safe content",
            "file_path": sentinel_path,
            "metadata": {"leaked_key": sentinel_secret},
            "entity_id": "secret-entity-id",
        }]
    })

    def normal_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(returncode=0, stdout=backend_payload, stderr=sentinel_stderr)

    res = retrieve_context(personal_tideway_config, req, runner=normal_runner, registry=reg)
    res_dict = res.to_dict()

    # Verify no file_path or metadata in serialized items
    item_dict = res_dict["bundle"]["items"][0]
    assert "file_path" not in item_dict
    assert "metadata" not in item_dict
    assert "entity_id" not in item_dict
    assert sentinel_path not in json.dumps(res_dict)
    assert sentinel_secret not in json.dumps(res_dict)
    assert sentinel_stderr not in json.dumps(res_dict)


def test_redacted_serializations_leak_no_query_paths_or_env(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test that to_dict representations never include raw query, host paths, or env overrides."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    secret_query = "CLASSIFIED_SEARCH_KEYWORD_XYZ"
    req = ContextRetrievalRequest(
        project=p_git,
        query=secret_query,
        max_items=3,
        max_chars=500,
    )
    req_dict = req.to_dict()
    assert "query" not in req_dict
    assert req_dict["has_query"] is True
    assert secret_query not in json.dumps(req_dict)

    plan = plan_context_retrieval(personal_tideway_config, req, registry=reg)
    plan_dict = plan.to_dict()
    assert "env_overrides" not in plan_dict
    assert "search_argvs" not in plan_dict
    assert secret_query not in json.dumps(plan_dict)
    assert "<query_redacted>" in json.dumps(plan_dict)

    preview = plan.preview()
    preview_dict = preview.to_dict()
    assert "env_overrides" not in preview_dict
    assert "search_argvs" not in preview_dict
    assert secret_query not in json.dumps(preview_dict)
    assert "<query_redacted>" in json.dumps(preview_dict)


# ===========================================================================
# 16. Whole-tree snapshot proves zero mutation on owned state or source roots
# ===========================================================================


def test_whole_tree_snapshot_proves_no_mutations_on_owned_state_or_source_roots(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test that context retrieval mutates zero files in owned state or registered source roots."""
    reg, p_git, p_dir, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    # Populate dummy files in source roots
    git_root = Path(p_git.bindings.paths[0])
    (git_root / "app.py").write_text("print('hello')", encoding="utf-8")
    (git_root / "README.md").write_text("# Project", encoding="utf-8")

    dir_root = Path(p_dir.bindings.paths[0])
    (dir_root / "spec.txt").write_text("specifications", encoding="utf-8")

    # Populate dummy memory notes in personal_tideway_config
    mem_dir = personal_tideway_config.projects_dir / p_git.id / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "current-state.md").write_text("verified state", encoding="utf-8")

    save_registry(reg, personal_tideway_config)

    # Capture initial snapshots
    home_before = snapshot_filesystem(personal_tideway_config.home)
    git_before = snapshot_filesystem(git_root)
    dir_before = snapshot_filesystem(dir_root)

    backend_json = json.dumps({
        "results": [{
            "title": "State Note",
            "permalink": "current-state",
            "content": "Verified state note content.",
            "status": "verified",
        }],
        "total": 1,
    })

    def mock_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(returncode=0, stdout=backend_json, stderr="")

    req = ContextRetrievalRequest(project=p_git, query="test query")

    # 1. Non-dry-run retrieval
    res = retrieve_context(personal_tideway_config, req, dry_run=False, runner=mock_runner, registry=reg)
    assert res.bundle.total_items == 1

    assert snapshot_filesystem(personal_tideway_config.home) == home_before
    assert snapshot_filesystem(git_root) == git_before
    assert snapshot_filesystem(dir_root) == dir_before

    # 2. Dry-run retrieval
    res_dry = retrieve_context(personal_tideway_config, req, dry_run=True, runner=mock_runner, registry=reg)
    assert res_dry.dry_run is True

    assert snapshot_filesystem(personal_tideway_config.home) == home_before
    assert snapshot_filesystem(git_root) == git_before
    assert snapshot_filesystem(dir_root) == dir_before


# ===========================================================================
# 17. read_project_note helper and frontmatter parsing
# ===========================================================================


def test_read_project_note_execution_and_schema(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test individual note retrieval via read_project_note with frontmatter parsing."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    read_stdout = json.dumps({
        "title": "Database Architecture",
        "permalink": "decisions/db",
        "content": "Chose PostgreSQL for transactional integrity.",
        "file_path": "/tmp/notes/decisions/db.md",
        "frontmatter": {
            "type": "decision",
            "status": "verified",
            "updated_at": "2026-09-11T11:00:00Z",
        },
    })

    calls: list[tuple[str, ...]] = []

    def read_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        calls.append(argv)
        return BasicMemoryRunnerResult(returncode=0, stdout=read_stdout, stderr="")

    item = read_project_note(
        personal_tideway_config,
        project=p_git,
        identifier="decisions/db",
        runner=read_runner,
        registry=reg,
    )

    assert len(calls) == 1
    assert calls[0] == (
        str(layout.primary_executable),
        "tool",
        "read-note",
        "decisions/db",
        "--json",
        "--project",
        p_git.memory.project_name,
        "--local",
    )

    assert item.title == "Database Architecture"
    assert item.permalink == "decisions/db"
    assert item.content == "Chose PostgreSQL for transactional integrity."
    assert item.note_type == "decision"
    assert item.status == "verified"
    assert item.updated_at == "2026-09-11T11:00:00Z"
    assert item.is_current_state is False

    # Verify safe to_dict output
    i_dict = item.to_dict()
    assert "file_path" not in i_dict
    assert "frontmatter" not in i_dict
    assert i_dict["permalink"] == "decisions/db"


def test_read_note_frontmatter_schema_validation():
    """Test frontmatter schema validation in parse_read_note_response."""
    # Frontmatter is not a dict
    bad_fm = json.dumps({"title": "T", "permalink": "P", "frontmatter": "not_a_dict"})
    with pytest.raises(RuntimeProbeError) as exc_fm:
        parse_read_note_response(bad_fm, "pid", "pname")
    assert "frontmatter must be a JSON object" in str(exc_fm.value)

    # Frontmatter type is not a string
    bad_type = json.dumps({"title": "T", "permalink": "P", "frontmatter": {"type": 123}})
    with pytest.raises(RuntimeProbeError) as exc_type:
        parse_read_note_response(bad_type, "pid", "pname")
    assert "frontmatter type is invalid" in str(exc_type.value)

    # Frontmatter status is not a string
    bad_status = json.dumps({"title": "T", "permalink": "P", "frontmatter": {"status": True}})
    with pytest.raises(RuntimeProbeError) as exc_status:
        parse_read_note_response(bad_status, "pid", "pname")
    assert "frontmatter status is invalid" in str(exc_status.value)


# ===========================================================================
# 18. Budgeting invariants and small max_chars
# ===========================================================================


@pytest.mark.parametrize("max_chars", [1, 5, 10, 25, 50, 80, 150, 500])
def test_tiny_max_chars_budgeting_invariants(max_chars: int):
    """Test that rendered_text length <= max_chars unconditionally for all small max_chars."""
    item = ContextItem(
        project_id="test-p",
        project_name="test-proj",
        title="Sample Note Title",
        permalink="notes/sample",
        content="This is the content of the note that will be budgeted and potentially truncated.",
        is_current_state=True,
        note_type="decision",
        status="verified",
        updated_at="2026-09-11T12:00:00Z",
    )
    bundle = build_bounded_context_bundle(
        project_id="test-p",
        project_name="test-proj",
        candidate_items=[item],
        max_items=5,
        max_chars=max_chars,
    )
    assert len(bundle.rendered_text) <= max_chars
    assert bundle.total_chars == len(bundle.rendered_text)

    item2 = ContextItem(
        project_id="test-p",
        project_name="test-proj",
        title="Second Note",
        permalink="notes/second",
        content="Second content.",
    )
    bundle2 = build_bounded_context_bundle(
        project_id="test-p",
        project_name="test-proj",
        candidate_items=[item, item2],
        max_items=5,
        max_chars=max_chars,
    )
    assert len(bundle2.rendered_text) <= max_chars
    assert bundle2.total_chars == len(bundle2.rendered_text)


# ===========================================================================
# 19. Helpers and Request/Bundle validation
# ===========================================================================


def test_is_current_state_permalink():
    """Test recognition of canonical current-state permalinks."""
    assert is_current_state_permalink("current-state") is True
    assert is_current_state_permalink("CURRENT-STATE") is True
    assert is_current_state_permalink("prefix/current-state") is True
    assert is_current_state_permalink("notes/CURRENT-STATE") is True
    assert is_current_state_permalink("notes/current-state-extra") is False
    assert is_current_state_permalink("not-current-state") is False


def test_request_and_bundle_validation():
    """Test boundary validation for ContextRetrievalRequest, ContextBundle, and ContextItem."""
    req_default = ContextRetrievalRequest(project="p")
    assert req_default.max_items == DEFAULT_MAX_ITEMS
    assert req_default.max_chars == DEFAULT_MAX_CHARS
    assert req_default.timeout == DEFAULT_RETRIEVAL_TIMEOUT

    bundle_empty = ContextBundle(project_id="pid", project_name="pname")
    assert isinstance(bundle_empty, ContextBundle)
    assert bundle_empty.total_items == 0
    assert bundle_empty.total_chars == 0
    assert bundle_empty.has_current_state is False

    # max_items boundaries
    with pytest.raises(ValidationError):
        ContextRetrievalRequest(project="p", max_items=0)
    with pytest.raises(ValidationError):
        ContextRetrievalRequest(project="p", max_items=HARD_MAX_ITEMS + 1)
    with pytest.raises(ValidationError):
        ContextRetrievalRequest(project="p", max_items=True)  # type: ignore

    # max_chars boundaries
    with pytest.raises(ValidationError):
        ContextRetrievalRequest(project="p", max_chars=0)
    with pytest.raises(ValidationError):
        ContextRetrievalRequest(project="p", max_chars=HARD_MAX_CHARS + 1)
    with pytest.raises(ValidationError):
        ContextRetrievalRequest(project="p", max_chars=True)  # type: ignore

    # timeout boundaries
    with pytest.raises(ValidationError):
        ContextRetrievalRequest(project="p", timeout=-1.0)
    with pytest.raises(ValidationError):
        ContextRetrievalRequest(project="p", timeout=float("nan"))
    with pytest.raises(ValidationError):
        ContextRetrievalRequest(project="p", timeout=float("inf"))

    # ContextItem status validation
    with pytest.raises(ValidationError):
        ContextItem(
            project_id="pid",
            project_name="pname",
            title="Title",
            permalink="link",
            content="text",
            status="invalid_status",
        )


# ===========================================================================
# 20. Staged Context Loop 1 Hardening Follow-up Tests
# ===========================================================================


def test_current_state_regression_unrelated_note_not_promoted(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Regression test: unrelated note (e.g. notes/x) must NEVER be promoted to current-state.

    When --permalink */current-state returns items but none end in 'current-state',
    current_state_item must remain None.
    """
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    # Subcase A: Backend returns only unrelated note 'notes/x'
    unrelated_json = json.dumps({
        "results": [{
            "title": "Random Unrelated Note",
            "permalink": "notes/x",
            "content": "This is note x, not current-state.",
            "type": "note",
            "status": "verified",
        }],
        "total": 1,
        "page_size": 1,
        "current_page": 1,
    })

    def unrelated_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(returncode=0, stdout=unrelated_json, stderr="")

    req_cs_only = ContextRetrievalRequest(project=p_git, query=None, include_current_state=True)
    res = retrieve_context(personal_tideway_config, req_cs_only, runner=unrelated_runner, registry=reg)

    assert res.bundle.has_current_state is False
    assert res.bundle.total_items == 0
    assert len(res.bundle.items) == 0
    assert res.bundle.rendered_text == ""

    # Subcase B: Multiple items returned where only the second is genuine current-state
    mixed_json = json.dumps({
        "results": [
            {
                "title": "Unrelated Note",
                "permalink": "notes/x",
                "content": "Not current state.",
            },
            {
                "title": "Actual State Note",
                "permalink": "projects/alpha/current-state",
                "content": "Real current state content.",
                "status": "verified",
            },
        ],
        "total": 2,
    })

    def mixed_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(returncode=0, stdout=mixed_json, stderr="")

    res_mixed = retrieve_context(personal_tideway_config, req_cs_only, runner=mixed_runner, registry=reg)
    assert res_mixed.bundle.has_current_state is True
    assert res_mixed.bundle.total_items == 1
    assert res_mixed.bundle.items[0].permalink == "projects/alpha/current-state"
    assert res_mixed.bundle.items[0].is_current_state is True


def test_read_project_note_identifier_rejection_before_runner_or_probe(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test read_project_note rejects CLI injection, absolute paths, and '..' traversal before runner/probe."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)

    # Remove executable so any probe would fail with RuntimeProbeError
    if layout.primary_executable.exists():
        layout.primary_executable.unlink()

    def failing_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        raise AssertionError("Runner must NEVER be invoked for an unsafe note identifier!")

    unsafe_identifiers = [
        "-option",
        "--all",
        " -leading-space-dash",
        "/etc/passwd",
        "/root/note",
        r"\windows\path",
        "..",
        "../traversal",
        "notes/../escape",
        "notes/..",
        r"notes\..\escape",
    ]

    for bad_id in unsafe_identifiers:
        # 1. Validation in read_project_note occurs before runner/probe
        with pytest.raises(ValidationError) as exc:
            read_project_note(
                personal_tideway_config,
                project=p_git,
                identifier=bad_id,
                runner=failing_runner,
                registry=reg,
            )
        assert exc.value is not None

        # 2. build_read_note_argv rejects unsafe identifiers without building unsafe argv
        with pytest.raises(ValidationError):
            build_read_note_argv(layout.primary_executable, p_git.memory.project_name, bad_id)

    # 3. Legitimate identifiers remain completely usable
    safe_identifiers = [
        "notes/x",
        "decisions/001-auth",
        "current-state",
        "projects/notes/architecture",
        "note-1..2",  # Contains dots but not a '..' path component
        "my-note.md",
    ]
    for safe_id in safe_identifiers:
        argv = build_read_note_argv(layout.primary_executable, p_git.memory.project_name, safe_id)
        assert safe_id.strip() in argv


def test_context_retrieval_request_query_rejects_leading_dash():
    """Test ContextRetrievalRequest rejects query beginning with '-' before any runner call."""
    with pytest.raises(ValidationError) as exc1:
        ContextRetrievalRequest(project="valid-p", query="-option-injection")
    assert "cannot begin with a dash" in str(exc1.value)

    with pytest.raises(ValidationError) as exc2:
        ContextRetrievalRequest(project="valid-p", query="   --inspect-flag")
    assert "cannot begin with a dash" in str(exc2.value)

    # Non-leading dashes are permitted
    req1 = ContextRetrievalRequest(project="valid-p", query="search - with middle dash")
    assert req1.query == "search - with middle dash"

    req2 = ContextRetrievalRequest(project="valid-p", query=None)
    assert req2.query is None


def test_build_search_notes_argv_rejects_query_with_permalink_and_dashes():
    """Test build_search_notes_argv rejects incompatible query+permalink and CLI dash injection."""
    # 1. Reject incompatible query and permalink pair
    with pytest.raises(ValidationError) as exc_pair:
        build_search_notes_argv(
            executable="/bin/basic-memory",
            project_name="test_proj",
            query="auth",
            permalink="notes/auth",
        )
    assert "Cannot specify both query and permalink" in str(exc_pair.value)

    # 2. Reject leading dash in query
    with pytest.raises(ValidationError) as exc_q:
        build_search_notes_argv(
            executable="/bin/basic-memory",
            project_name="test_proj",
            query="-cli-flag",
        )
    assert "cannot begin with a dash" in str(exc_q.value)

    # 3. Reject leading dash in permalink
    with pytest.raises(ValidationError) as exc_p:
        build_search_notes_argv(
            executable="/bin/basic-memory",
            project_name="test_proj",
            permalink="--permalink-injection",
        )
    assert "cannot begin with a dash" in str(exc_p.value)

    # 4. Legitimate individual query and permalink work as expected
    argv_q = build_search_notes_argv(
        executable="/bin/basic-memory",
        project_name="test_proj",
        query="architecture",
    )
    assert "architecture" in argv_q
    assert "--permalink" not in argv_q

    argv_p = build_search_notes_argv(
        executable="/bin/basic-memory",
        project_name="test_proj",
        permalink="notes/architecture",
    )
    assert "--permalink" in argv_p
    assert "notes/architecture" in argv_p


def test_invoke_runner_error_messages_name_actual_operation(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test _invoke_runner error messages explicitly name 'search' vs 'read-note' operation."""
    reg, p_git, _, _ = create_test_projects(tmp_path)
    layout = get_basic_memory_layout(personal_tideway_config)
    create_fake_executable(layout.primary_executable)

    # 1. read_project_note: timeout error message
    def timeout_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        raise TimeoutError("timed out")

    with pytest.raises(RuntimeProbeError) as exc_read_timeout:
        read_project_note(
            personal_tideway_config,
            project=p_git,
            identifier="decisions/001",
            runner=timeout_runner,
            registry=reg,
        )
    assert str(exc_read_timeout.value) == "Basic Memory read-note timed out."

    # 2. read_project_note: crash error message
    def crash_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        raise RuntimeError("crash")

    with pytest.raises(RuntimeProbeError) as exc_read_crash:
        read_project_note(
            personal_tideway_config,
            project=p_git,
            identifier="decisions/001",
            runner=crash_runner,
            registry=reg,
        )
    assert str(exc_read_crash.value) == "Basic Memory read-note execution failed."

    # 3. read_project_note: nonzero exit code error message
    def nonzero_runner(argv: tuple[str, ...], env: Mapping[str, str], timeout: float) -> BasicMemoryRunnerResult:
        return BasicMemoryRunnerResult(returncode=1, stdout="", stderr="error")

    with pytest.raises(RuntimeProbeError) as exc_read_nonzero:
        read_project_note(
            personal_tideway_config,
            project=p_git,
            identifier="decisions/001",
            runner=nonzero_runner,
            registry=reg,
        )
    assert str(exc_read_nonzero.value) == "Basic Memory read-note failed with non-zero exit code."

    # 4. Search operation continues to name 'search'
    req = ContextRetrievalRequest(project=p_git, query="auth")
    with pytest.raises(RuntimeProbeError) as exc_search_timeout:
        retrieve_context(personal_tideway_config, req, runner=timeout_runner, registry=reg)
    assert str(exc_search_timeout.value) == "Basic Memory search timed out."

    with pytest.raises(RuntimeProbeError) as exc_search_crash:
        retrieve_context(personal_tideway_config, req, runner=crash_runner, registry=reg)
    assert str(exc_search_crash.value) == "Basic Memory search execution failed."

    with pytest.raises(RuntimeProbeError) as exc_search_nonzero:
        retrieve_context(personal_tideway_config, req, runner=nonzero_runner, registry=reg)
    assert str(exc_search_nonzero.value) == "Basic Memory search failed with non-zero exit code."


def test_validate_context_retrieval_plan_bidirectional_is_noop(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test bidirectional consistency: plan.is_noop must be True exactly iff search_argvs is empty."""
    reg, p_git, _, _ = create_test_projects(tmp_path)

    # 1. Genuine no-op plan
    req_noop = ContextRetrievalRequest(project=p_git, query=None, include_current_state=False)
    plan_noop = plan_context_retrieval(personal_tideway_config, req_noop, registry=reg)
    assert plan_noop.is_noop is True
    assert len(plan_noop.search_argvs) == 0
    validate_context_retrieval_plan(plan_noop, cfg=personal_tideway_config)

    # 2. Genuine non-noop plan
    req_active = ContextRetrievalRequest(project=p_git, query="database", include_current_state=True)
    plan_active = plan_context_retrieval(personal_tideway_config, req_active, registry=reg)
    assert plan_active.is_noop is False
    assert len(plan_active.search_argvs) == 2
    validate_context_retrieval_plan(plan_active, cfg=personal_tideway_config)

    # 3. Inconsistent: is_noop is False, but search_argvs is empty
    inconsistent_plan_false_empty = ContextRetrievalPlan(
        request=req_noop,
        layout=plan_noop.layout,
        executable=plan_noop.executable,
        project_record=plan_noop.project_record,
        current_state_argv=None,
        query_argv=None,
        search_argvs=(),
        env_overrides=plan_noop.env_overrides,
        timeout=plan_noop.timeout,
        is_noop=False,  # Inconsistent!
    )
    with pytest.raises(ValidationError) as exc1:
        validate_context_retrieval_plan(inconsistent_plan_false_empty, cfg=personal_tideway_config)
    assert "Plan with no search commands must be marked as no-op." in str(exc1.value)

    # 4. Inconsistent: is_noop is True, but search_argvs is non-empty
    inconsistent_plan_true_nonempty = ContextRetrievalPlan(
        request=req_active,
        layout=plan_active.layout,
        executable=plan_active.executable,
        project_record=plan_active.project_record,
        current_state_argv=plan_active.current_state_argv,
        query_argv=plan_active.query_argv,
        search_argvs=plan_active.search_argvs,
        env_overrides=plan_active.env_overrides,
        timeout=plan_active.timeout,
        is_noop=True,  # Inconsistent!
    )
    with pytest.raises(ValidationError) as exc2:
        validate_context_retrieval_plan(inconsistent_plan_true_nonempty, cfg=personal_tideway_config)
    assert "No-op plan must not have search commands." in str(exc2.value)


def test_retrieval_bounded_subprocess_runner_and_output_cap(
    personal_tideway_config: PersonalTidewayConfig,
    tmp_path: Path,
):
    """Test retrieval_subprocess_runner captures 65 KiB - 2 MiB, caps >2 MiB, and handles timeouts.

    Does not spawn external Basic Memory; uses Python subprocess execution to test runner mechanics.
    """
    # 1. Verify response from 65 KiB through 2 MiB parses correctly without 64 KiB truncation
    items_count = 100
    # Python script constructs ~120 KiB valid JSON compactly at runtime without huge argv
    py_code_120k = (
        "import json, sys; "
        "items = ["
        "    {"
        "        'title': f'Note {i}',"
        "        'permalink': f'notes/{i}',"
        "        'content': 'X' * 1200,"
        "        'type': 'note',"
        "        'status': 'verified',"
        "    }"
        "    for i in range(100)"
        "]; "
        "sys.stdout.write(json.dumps({'results': items, 'total': 100, 'page_size': 100, 'current_page': 1}))"
    )
    res_120k = retrieval_subprocess_runner(
        [sys.executable, "-c", py_code_120k],
        env={},
        timeout=10.0,
    )
    assert isinstance(res_120k, RetrievalRunnerResult)
    assert res_120k.returncode == 0
    assert len(res_120k.stdout.encode("utf-8")) > 65 * 1024
    assert len(res_120k.stdout.encode("utf-8")) <= MAX_RETRIEVAL_OUTPUT_BYTES

    # Successfully parsed by parse_search_notes_response
    items = parse_search_notes_response(res_120k.stdout, "p-id", "p-name")
    assert len(items) == items_count
    assert items[0].title == "Note 0"

    # 2. Verify response > 2 MiB fails explicitly as oversized, NOT as malformed JSON
    # A Python script generating 2.5 MiB of valid JSON
    py_code_oversized = (
        "import json, sys; "
        "large_str = 'A' * (2 * 1024 * 1024 + 100_000); "
        "sys.stdout.write(json.dumps({'results': [{'title': 'Big', 'permalink': 'p', 'content': large_str}]}))"
    )
    res_huge = retrieval_subprocess_runner(
        [sys.executable, "-c", py_code_oversized],
        env={},
        timeout=10.0,
    )
    assert isinstance(res_huge, RetrievalRunnerResult)
    # Runner safely bounded output capture to MAX_RETRIEVAL_OUTPUT_BYTES + 1
    assert len(res_huge.stdout.encode("utf-8")) == MAX_RETRIEVAL_OUTPUT_BYTES + 1

    # Must fail explicitly with 'exceeded maximum allowed size', not 'malformed JSON'
    with pytest.raises(RuntimeProbeError) as exc_oversized:
        parse_search_notes_response(res_huge.stdout, "p-id", "p-name")
    assert "exceeded maximum allowed size" in str(exc_oversized.value)
    assert "malformed JSON" not in str(exc_oversized.value)

    # 3. Timeout handling and process cleanup
    with pytest.raises(TimeoutError) as exc_timeout:
        retrieval_subprocess_runner(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            env={},
            timeout=0.1,
        )
    assert "timed out" in str(exc_timeout.value)
