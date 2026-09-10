"""Tests for Personal Tideway v2 read-only project CLI commands."""

import json
import os
from pathlib import Path
import pytest

from personal_tideway.cli.main import main
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import ExitCode, PROJECT_KIND_GIT
from personal_tideway.core.project_resolver import GitProbeResult
from personal_tideway.core.registry import (
    ProjectRecord,
    ProjectRegistry,
    save_registry,
)
from personal_tideway.core.workspace import init_workspace


@pytest.fixture(autouse=True)
def guard_git_runner(monkeypatch: pytest.MonkeyPatch):
    """Guard against unintended real host git execution across all tests in this module."""
    def guarded_git(cmd, cwd, timeout=3.0):
        raise AssertionError(f"Host subprocess execution blocked in tests: {cmd} at {cwd}")

    monkeypatch.setattr(
        "personal_tideway.core.project_resolver.default_git_runner",
        guarded_git,
    )


def get_projects_yaml_bytes(home: Path) -> bytes | None:
    """Return raw bytes of projects.yaml if it exists, or None if absent."""
    p = home / "registry" / "projects.yaml"
    return p.read_bytes() if p.is_file() else None


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


def setup_populated_workspace(home: Path) -> tuple[PersonalTidewayConfig, list[ProjectRecord]]:
    """Initialize a workspace populated with deterministic test project records."""
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)

    p1 = make_record(
        project_id="11111111-1111-4111-8111-111111111111",
        slug="alpha-proj",
        display_name="Alpha Project",
        aliases=["alpha", "a-alias"],
        paths=[str(home / "repos" / "alpha-repo")],
        git_common_dirs=[str(home / "repos" / "alpha-repo" / ".git")],
        git_remotes=["github.com/myorg/alpha"],
    )
    p2 = make_record(
        project_id="22222222-2222-4222-8222-222222222222",
        slug="beta-proj",
        display_name="Beta Project",
        aliases=["beta", "b-alias"],
        paths=[str(home / "repos" / "beta-repo")],
        git_common_dirs=[str(home / "repos" / "beta-repo" / ".git")],
        git_remotes=["github.com/myorg/shared-remote"],
    )
    p3 = make_record(
        project_id="33333333-3333-4333-8333-333333333333",
        slug="gamma-proj",
        display_name="Gamma Project",
        aliases=["gamma"],
        paths=[str(home / "repos" / "gamma-repo")],
        git_common_dirs=[str(home / "repos" / "gamma-repo" / ".git")],
        git_remotes=["github.com/myorg/shared-remote"],
    )

    reg = ProjectRegistry(version=2, projects=[p1, p2, p3])
    save_registry(reg, cfg)
    return cfg, [p1, p2, p3]


# ===========================================================================
# 1. ptw project list tests
# ===========================================================================


def test_project_list_empty_before_init(tmp_path: Path, capsys: pytest.CaptureFixture):
    """ptw project list before init handles missing registry cleanly and creates no files."""
    home = tmp_path / "empty_ptw"
    assert get_projects_yaml_bytes(home) is None

    # Text output
    code = main(["--home", str(home), "project", "list"])
    assert code == ExitCode.SUCCESS
    out, err = capsys.readouterr()
    assert "No projects registered." in out
    assert err == ""
    assert get_projects_yaml_bytes(home) is None

    # JSON output
    code_json = main(["--home", str(home), "project", "list", "--json"])
    assert code_json == ExitCode.SUCCESS
    out_json, err_json = capsys.readouterr()
    data = json.loads(out_json)
    assert data == {"version": 2, "projects": []}
    assert err_json == ""
    assert get_projects_yaml_bytes(home) is None


def test_project_list_deterministic_order_and_json(tmp_path: Path, capsys: pytest.CaptureFixture):
    """ptw project list preserves deterministic insertion order and produces valid JSON."""
    home = tmp_path / "populated_ptw"
    _cfg, projects = setup_populated_workspace(home)
    initial_bytes = get_projects_yaml_bytes(home)
    assert initial_bytes is not None

    # Text output
    code = main(["--home", str(home), "project", "list"])
    assert code == ExitCode.SUCCESS
    out, err = capsys.readouterr()
    assert err == ""
    lines = [line.strip() for line in out.splitlines() if line.strip() and not line.startswith("-") and not line.startswith("SLUG")]
    assert len(lines) == 3
    assert lines[0].startswith("alpha-proj")
    assert lines[1].startswith("beta-proj")
    assert lines[2].startswith("gamma-proj")
    assert get_projects_yaml_bytes(home) == initial_bytes

    # JSON output
    code_json = main(["--home", str(home), "project", "list", "--json"])
    assert code_json == ExitCode.SUCCESS
    out_json, err_json = capsys.readouterr()
    assert err_json == ""
    data = json.loads(out_json)
    assert data["version"] == 2
    assert len(data["projects"]) == 3
    assert [p["slug"] for p in data["projects"]] == ["alpha-proj", "beta-proj", "gamma-proj"]
    assert [p["id"] for p in data["projects"]] == [p.id for p in projects]
    assert get_projects_yaml_bytes(home) == initial_bytes


# ===========================================================================
# 2. ptw project show tests
# ===========================================================================


def test_project_show_by_id_slug_alias_case_insensitive(tmp_path: Path, capsys: pytest.CaptureFixture):
    """ptw project show resolves by id, slug, and alias case-insensitively with full details."""
    home = tmp_path / "populated_ptw"
    _cfg, projects = setup_populated_workspace(home)
    p1 = projects[0]
    initial_bytes = get_projects_yaml_bytes(home)

    tokens = [
        p1.id,
        p1.id.upper(),
        p1.slug,
        p1.slug.upper(),
        "alpha",
        "A-ALIAS",
    ]

    for token in tokens:
        code = main(["--home", str(home), "project", "show", token])
        assert code == ExitCode.SUCCESS
        out, err = capsys.readouterr()
        assert err == ""
        assert f"ID:           {p1.id}" in out
        assert f"Slug:         {p1.slug}" in out
        assert f"Display Name: {p1.display_name}" in out
        assert f"Kind:         {p1.kind}" in out
        assert "alpha" in out
        assert "Bindings:" in out
        assert p1.bindings.paths[0] in out
        assert get_projects_yaml_bytes(home) == initial_bytes

    # JSON output must exactly match ProjectRecord dict
    code_json = main(["--home", str(home), "project", "show", p1.slug, "--json"])
    assert code_json == ExitCode.SUCCESS
    out_json, err_json = capsys.readouterr()
    assert err_json == ""
    data = json.loads(out_json)
    assert data == p1.to_dict()
    assert get_projects_yaml_bytes(home) == initial_bytes


def test_project_show_missing_raises_validation_error(tmp_path: Path, capsys: pytest.CaptureFixture):
    """ptw project show with unknown token raises safe ValidationError with clean stderr."""
    home = tmp_path / "populated_ptw"
    setup_populated_workspace(home)
    initial_bytes = get_projects_yaml_bytes(home)

    code = main(["--home", str(home), "project", "show", "nonexistent-token"])
    assert code == ExitCode.VALIDATION_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert "Error: Project 'nonexistent-token' not found in registry." in err
    assert get_projects_yaml_bytes(home) == initial_bytes


def test_project_show_before_init_fails_safely(tmp_path: Path, capsys: pytest.CaptureFixture):
    """ptw project show before init fails safely without creating registry."""
    home = tmp_path / "uninitialized_ptw"
    assert get_projects_yaml_bytes(home) is None

    code = main(["--home", str(home), "project", "show", "any-proj"])
    assert code == ExitCode.VALIDATION_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert "Error: Project 'any-proj' not found in registry." in err
    assert get_projects_yaml_bytes(home) is None


# ===========================================================================
# 3. ptw project resolve tests
# ===========================================================================


def test_project_resolve_exact_registered_path(tmp_path: Path, capsys: pytest.CaptureFixture):
    """ptw project resolve matches exact registered path deterministically."""
    home = tmp_path / "populated_ptw"
    _cfg, projects = setup_populated_workspace(home)
    p1 = projects[0]
    target_path = p1.bindings.paths[0]
    initial_bytes = get_projects_yaml_bytes(home)

    # Text mode
    code = main(["--home", str(home), "project", "resolve", target_path])
    assert code == ExitCode.SUCCESS
    out, err = capsys.readouterr()
    assert err == ""
    assert "Status:   resolved" in out
    assert f"Project:  {p1.id}" in out
    assert f"Slug:     {p1.slug}" in out
    assert "Evidence: path" in out
    assert get_projects_yaml_bytes(home) == initial_bytes

    # JSON mode
    code_json = main(["--home", str(home), "project", "resolve", target_path, "--json"])
    assert code_json == ExitCode.SUCCESS
    out_json, err_json = capsys.readouterr()
    assert err_json == ""
    data = json.loads(out_json)
    assert data["status"] == "resolved"
    assert data["project_id"] == p1.id
    assert data["evidence"] == ["path"]
    assert data["requires_confirmation"] is False
    assert get_projects_yaml_bytes(home) == initial_bytes


def test_project_resolve_explicit_env_selection(tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch):
    """ptw project resolve honors explicit selection from PERSONAL_TIDEWAY_PROJECT/config."""
    home = tmp_path / "populated_ptw"
    _cfg, projects = setup_populated_workspace(home)
    p2 = projects[1]
    initial_bytes = get_projects_yaml_bytes(home)

    # 1. Valid explicit project in env
    monkeypatch.setenv("PERSONAL_TIDEWAY_PROJECT", "beta-proj")
    code = main(["--home", str(home), "project", "resolve"])
    assert code == ExitCode.SUCCESS
    out, err = capsys.readouterr()
    assert err == ""
    assert "Status:   resolved" in out
    assert f"Project:  {p2.id}" in out
    assert "Evidence: explicit" in out
    assert get_projects_yaml_bytes(home) == initial_bytes

    # 2. Unknown explicit project in env -> valid unresolved query result
    monkeypatch.setenv("PERSONAL_TIDEWAY_PROJECT", "no-such-project")
    code_unres = main(["--home", str(home), "project", "resolve", "--json"])
    assert code_unres == ExitCode.SUCCESS
    out_unres, err_unres = capsys.readouterr()
    assert err_unres == ""
    data = json.loads(out_unres)
    assert data["status"] == "unresolved"
    assert data["project_id"] is None
    assert data["evidence"] == ["explicit"]
    assert get_projects_yaml_bytes(home) == initial_bytes

    # 3. Global --project overrides conflicting PERSONAL_TIDEWAY_PROJECT without probing Git
    monkeypatch.setenv("PERSONAL_TIDEWAY_PROJECT", "alpha-proj")
    code_override = main(["--home", str(home), "--project", "beta-proj", "project", "resolve", "--json"])
    assert code_override == ExitCode.SUCCESS
    out_override, err_override = capsys.readouterr()
    assert err_override == ""
    data_override = json.loads(out_override)
    assert data_override["status"] == "resolved"
    assert data_override["project_id"] == p2.id
    assert data_override["evidence"] == ["explicit"]
    assert get_projects_yaml_bytes(home) == initial_bytes

    # 4. show by slug does not influence cfg.project through namespace collision
    monkeypatch.delenv("PERSONAL_TIDEWAY_PROJECT", raising=False)
    code_show = main(["--home", str(home), "project", "show", "alpha-proj"])
    assert code_show == ExitCode.SUCCESS
    _out_show, err_show = capsys.readouterr()
    assert err_show == ""
    assert get_projects_yaml_bytes(home) == initial_bytes


def test_project_resolve_git_candidate_never_mutates(tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch):
    """ptw project resolve for unseen Git root reports candidate without mutating registry."""
    home = tmp_path / "populated_ptw"
    setup_populated_workspace(home)
    initial_bytes = get_projects_yaml_bytes(home)

    candidate_dir = tmp_path / "new_workspace" / "candidate-repo"
    candidate_dir.mkdir(parents=True, exist_ok=True)

    fake_probe = GitProbeResult(
        is_git=True,
        root=candidate_dir,
        common_dir=candidate_dir / ".git",
        remotes=["https://github.com/neworg/candidate-repo.git"],
        normalized_remotes=["github.com/neworg/candidate-repo"],
    )
    monkeypatch.setattr(
        "personal_tideway.core.project_resolver.probe_git",
        lambda _p, **_kwargs: fake_probe,
    )

    # Text mode
    code = main(["--home", str(home), "project", "resolve", str(candidate_dir)])
    assert code == ExitCode.SUCCESS
    out, err = capsys.readouterr()
    assert err == ""
    assert "Status:   candidate" in out
    assert "Evidence: git-root" in out
    assert f"Root:     {candidate_dir}" in out
    assert "Suggested Slug: candidate-repo" in out
    assert get_projects_yaml_bytes(home) == initial_bytes

    # JSON mode
    code_json = main(["--home", str(home), "project", "resolve", str(candidate_dir), "--json"])
    assert code_json == ExitCode.SUCCESS
    out_json, err_json = capsys.readouterr()
    assert err_json == ""
    data = json.loads(out_json)
    assert data["status"] == "candidate"
    assert data["requires_confirmation"] is True
    assert data["candidate_metadata"]["root"] == str(candidate_dir)
    assert data["candidate_metadata"]["suggested_slug"] == "candidate-repo"
    assert get_projects_yaml_bytes(home) == initial_bytes


def test_project_resolve_ambiguous_remote(tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch):
    """ptw project resolve for ambiguous remote matches reports ambiguous without arbitrary choice."""
    home = tmp_path / "populated_ptw"
    _cfg, projects = setup_populated_workspace(home)
    p2, p3 = projects[1], projects[2]
    initial_bytes = get_projects_yaml_bytes(home)

    clone_dir = tmp_path / "ambiguous_clone"
    clone_dir.mkdir(parents=True, exist_ok=True)

    fake_probe = GitProbeResult(
        is_git=True,
        root=clone_dir,
        common_dir=clone_dir / ".git",
        remotes=["git@github.com:myorg/shared-remote.git"],
        normalized_remotes=["github.com/myorg/shared-remote"],
    )
    monkeypatch.setattr(
        "personal_tideway.core.project_resolver.probe_git",
        lambda _p, **_kwargs: fake_probe,
    )

    # Text mode
    code = main(["--home", str(home), "project", "resolve", str(clone_dir)])
    assert code == ExitCode.SUCCESS
    out, err = capsys.readouterr()
    assert err == ""
    assert "Status:   ambiguous" in out
    assert "Evidence: git-remote" in out
    assert "Ambiguous Project IDs:" in out
    assert p2.id in out
    assert p3.id in out
    assert get_projects_yaml_bytes(home) == initial_bytes

    # JSON mode
    code_json = main(["--home", str(home), "project", "resolve", str(clone_dir), "--json"])
    assert code_json == ExitCode.SUCCESS
    out_json, err_json = capsys.readouterr()
    assert err_json == ""
    data = json.loads(out_json)
    assert data["status"] == "ambiguous"
    assert data["requires_confirmation"] is True
    assert sorted(data["ambiguous_project_ids"]) == sorted([p2.id, p3.id])
    assert get_projects_yaml_bytes(home) == initial_bytes


def test_project_resolve_unresolved_non_git(tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch):
    """ptw project resolve for non-Git path reports unresolved cleanly."""
    home = tmp_path / "populated_ptw"
    setup_populated_workspace(home)
    initial_bytes = get_projects_yaml_bytes(home)

    plain_dir = tmp_path / "plain_dir"
    plain_dir.mkdir(parents=True, exist_ok=True)

    fake_probe = GitProbeResult(is_git=False)
    monkeypatch.setattr(
        "personal_tideway.core.project_resolver.probe_git",
        lambda _p, **_kwargs: fake_probe,
    )

    # Text mode
    code = main(["--home", str(home), "project", "resolve", str(plain_dir)])
    assert code == ExitCode.SUCCESS
    out, err = capsys.readouterr()
    assert err == ""
    assert "Status:   unresolved" in out
    assert get_projects_yaml_bytes(home) == initial_bytes

    # JSON mode
    code_json = main(["--home", str(home), "project", "resolve", str(plain_dir), "--json"])
    assert code_json == ExitCode.SUCCESS
    out_json, err_json = capsys.readouterr()
    assert err_json == ""
    data = json.loads(out_json)
    assert data["status"] == "unresolved"
    assert data["project_id"] is None
    assert get_projects_yaml_bytes(home) == initial_bytes


# ===========================================================================
# 4. Unicode, spaces, and existing init compatibility tests
# ===========================================================================


def test_spaces_and_non_ascii_paths_preserved(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Spaces and non-ASCII paths are preserved without escaping or corruption."""
    home = tmp_path / "unicode_ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)

    unicode_path = str(home / "репозитории" / "тестовый проект")
    rec = make_record(
        project_id="44444444-4444-4444-8444-444444444444",
        slug="unicode-proj",
        display_name="Проект с пробелами",
        paths=[unicode_path],
    )
    reg = ProjectRegistry(version=2, projects=[rec])
    save_registry(reg, cfg)
    initial_bytes = get_projects_yaml_bytes(home)

    # Show text
    code_show = main(["--home", str(home), "project", "show", "unicode-proj"])
    assert code_show == ExitCode.SUCCESS
    out_show, _ = capsys.readouterr()
    assert "Проект с пробелами" in out_show
    assert unicode_path in out_show

    # Show JSON
    code_show_json = main(["--home", str(home), "project", "show", "unicode-proj", "--json"])
    assert code_show_json == ExitCode.SUCCESS
    out_show_json, _ = capsys.readouterr()
    data_show = json.loads(out_show_json)
    assert data_show["display_name"] == "Проект с пробелами"
    assert data_show["bindings"]["paths"][0] == unicode_path

    # Resolve text
    code_res = main(["--home", str(home), "project", "resolve", unicode_path])
    assert code_res == ExitCode.SUCCESS
    out_res, _ = capsys.readouterr()
    assert "Status:   resolved" in out_res
    assert "Slug:     unicode-proj" in out_res

    # Resolve JSON
    code_res_json = main(["--home", str(home), "project", "resolve", unicode_path, "--json"])
    assert code_res_json == ExitCode.SUCCESS
    out_res_json, _ = capsys.readouterr()
    data_res = json.loads(out_res_json)
    assert data_res["status"] == "resolved"
    assert data_res["project_id"] == rec.id

    assert get_projects_yaml_bytes(home) == initial_bytes


def test_existing_project_init_still_parses(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Existing ptw project init continues to parse and function with --dry-run and paths."""
    home = tmp_path / "ptw_home"
    target_dir = tmp_path / "proj_dir"
    target_dir.mkdir(parents=True, exist_ok=True)

    # Dry run
    code_dry = main(["--home", str(home), "project", "init", str(target_dir), "--dry-run"])
    assert code_dry == ExitCode.SUCCESS
    out_dry, err_dry = capsys.readouterr()
    assert "[DRY RUN] Initialized project manifest" in out_dry
    assert not (target_dir / ".personal-tideway.yaml").exists()

    # Real init
    code_real = main(["--home", str(home), "project", "init", str(target_dir)])
    assert code_real == ExitCode.SUCCESS
    out_real, _ = capsys.readouterr()
    assert "Initialized project manifest" in out_real
    assert (target_dir / ".personal-tideway.yaml").is_file()
