"""Comprehensive tests for Git identity probing, project resolution, and auto-registration."""

import os
from pathlib import Path
import subprocess
import uuid

import pytest
import yaml

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import PROJECT_KIND_GIT, SCHEMA_VERSION
from personal_tideway.core.project_resolver import (
    BindingReconciliationResult,
    GitBindingReconciliationService,
    GitProbeResult,
    ProjectResolutionResult,
    ProposedBindingChanges,
    derive_safe_slug,
    is_token_available,
    normalize_git_remote,
    probe_git,
    reconcile_git_bindings,
    resolve_or_register_git,
    resolve_project,
    sanitize_error_text,
    sanitize_remote_url,
)
from personal_tideway.core.registry import (
    ProjectRecord,
    ProjectRegistry,
    load_registry,
    save_registry,
)
from personal_tideway.exceptions import ConfigError, ValidationError


def make_test_project(
    project_id: str = "11111111-2222-4333-8444-555555555555",
    slug: str = "test-repo",
    display_name: str = "Test Repository",
    aliases: list[str] | None = None,
    paths: list[str] | None = None,
    git_common_dirs: list[str] | None = None,
    git_remotes: list[str] | None = None,
) -> ProjectRecord:
    """Helper to create a valid ProjectRecord for testing."""
    return ProjectRecord.create(
        project_id=project_id,
        slug=slug,
        display_name=display_name,
        kind=PROJECT_KIND_GIT,
        aliases=aliases or [],
        paths=paths or ["/repos/test-repo"],
        git_common_dirs=git_common_dirs or ["/repos/test-repo/.git"],
        git_remotes=git_remotes or ["github.com/org/test-repo"],
    )


def make_fake_git_runner(
    root: str | None = None,
    common_dir: str | None = None,
    remotes: list[str] | None = None,
    retcode: int = 0,
    stderr: str = "",
    timeout_on_rev_parse: bool = False,
    timeout_on_config: bool = False,
    raise_exc: Exception | None = None,
):
    """Factory for injectable no-shell subprocess runners."""
    def fake_runner(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str, str]:
        assert isinstance(cmd, list)
        assert not any(" " in arg and arg.startswith("git ") for arg in cmd), "Commands must be list of args, not shell strings"
        assert isinstance(cwd, Path)

        if raise_exc is not None:
            raise raise_exc

        if "rev-parse" in cmd:
            if timeout_on_rev_parse:
                raise subprocess.TimeoutExpired(cmd, timeout)
            if retcode != 0:
                return retcode, "", stderr
            out_root = root or str(cwd)
            out_common = common_dir or f"{out_root}/.git"
            return 0, f"{out_root}\n{out_common}\n", ""

        if "config" in cmd:
            if timeout_on_config:
                raise subprocess.TimeoutExpired(cmd, timeout)
            if remotes:
                stdout_lines = [f"remote.origin{i}.url {r}" for i, r in enumerate(remotes)]
                return 0, "\n".join(stdout_lines) + "\n", ""
            return 1, "", ""

        return 0, "", ""

    return fake_runner


# ===========================================================================
# 1. URL Normalization and Credential Stripping Tests
# ===========================================================================


def test_normalize_git_remote_common_formats():
    """Test remote normalization across HTTPS, SSH, SCP-like, and Git protocols."""
    # HTTPS
    assert normalize_git_remote("https://github.com/owner/repo.git") == "github.com/owner/repo"
    assert normalize_git_remote("https://github.com/owner/repo") == "github.com/owner/repo"
    assert normalize_git_remote("https://github.com/owner/repo/") == "github.com/owner/repo"
    assert normalize_git_remote("https://github.com/owner/repo.git/") == "github.com/owner/repo"

    # HTTPS with port
    assert normalize_git_remote("https://gitlab.example.com:8443/group/subgroup/repo.git") == "gitlab.example.com/group/subgroup/repo"

    # SSH URL
    assert normalize_git_remote("ssh://git@github.com/owner/repo.git") == "github.com/owner/repo"
    assert normalize_git_remote("ssh://git@github.com:22/owner/repo.git") == "github.com/owner/repo"

    # SCP-like
    assert normalize_git_remote("git@github.com:owner/repo.git") == "github.com/owner/repo"
    assert normalize_git_remote("git@gitlab.com:group/subgroup/repo.git") == "gitlab.com/group/subgroup/repo"
    assert normalize_git_remote("host.xz:/path/to/repo.git") == "host.xz/path/to/repo"

    # Git protocol
    assert normalize_git_remote("git://github.com/owner/repo.git") == "github.com/owner/repo"

    # Query and fragment stripping
    assert normalize_git_remote("https://github.com/owner/repo.git?auth=token#branch") == "github.com/owner/repo"
    assert normalize_git_remote("git@github.com:owner/repo.git?query=val") == "github.com/owner/repo"

    sanitized_query = sanitize_remote_url("https://github.com/owner/repo.git?token=secret-value#branch")
    assert "secret-value" not in sanitized_query
    assert sanitized_query == "https://github.com/owner/repo.git"


def test_normalize_git_remote_lowercase_host():
    """Host is converted to lowercase, while path case is preserved."""
    assert normalize_git_remote("https://GITHUB.COM/MyOrg/MyRepo.git") == "github.com/MyOrg/MyRepo"
    assert normalize_git_remote("git@GITLAB.COM:Group/Repo.git") == "gitlab.com/Group/Repo"


def test_normalize_git_remote_rejects_local_and_malformed():
    """Local directories, file:// URLs, and malformed inputs return None."""
    assert normalize_git_remote("") is None
    assert normalize_git_remote("   ") is None
    assert normalize_git_remote(None) is None
    assert normalize_git_remote("/local/path/to/repo") is None
    assert normalize_git_remote("./local/relative") is None
    assert normalize_git_remote("../parent/relative") is None
    assert normalize_git_remote("~/home/repo") is None
    assert normalize_git_remote("file:///var/git/repo.git") is None
    assert normalize_git_remote("not_a_valid_url") is None
    assert normalize_git_remote("https:///no-host/repo.git") is None
    assert normalize_git_remote("https://github.com/") is None
    assert normalize_git_remote("https://github.com/../traversal/repo") is None


def test_credential_stripping_and_no_secret_leakage():
    """Ensure passwords and tokens never appear in results or sanitized errors."""
    super_secret = "ghp_SecretToken987654321Password"

    # HTTPS with user:password
    url_with_pass = f"https://user:{super_secret}@github.com/owner/repo.git"
    norm = normalize_git_remote(url_with_pass)
    assert norm == "github.com/owner/repo"
    assert super_secret not in norm

    sanitized = sanitize_remote_url(url_with_pass)
    assert super_secret not in sanitized
    assert "user" not in sanitized
    assert sanitized == "https://github.com/owner/repo.git"

    # SCP-like with password
    scp_with_pass = f"user:{super_secret}@gitlab.example.com:team/project.git"
    norm_scp = normalize_git_remote(scp_with_pass)
    assert norm_scp == "gitlab.example.com/team/project"
    assert super_secret not in norm_scp

    sanitized_scp = sanitize_remote_url(scp_with_pass)
    assert super_secret not in sanitized_scp

    # Raw stderr sanitization
    raw_stderr = f"fatal: Authentication failed for 'https://bot:{super_secret}@github.com/repo.git'"
    cleaned_stderr = sanitize_error_text(raw_stderr)
    assert super_secret not in cleaned_stderr
    assert "bot" not in cleaned_stderr
    assert "https://github.com/repo.git" in cleaned_stderr


# ===========================================================================
# 2. Git Probing Tests (File input, non-Git, timeout, failure)
# ===========================================================================


def test_probe_git_directory_input(tmp_path: Path):
    """Probing a directory resolves root, common-dir, and remotes."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()

    runner = make_fake_git_runner(
        root=str(repo_dir),
        common_dir=str(repo_dir / ".git"),
        remotes=["https://user:secret123@github.com/org/my-repo.git"],
    )

    probe = probe_git(repo_dir, runner=runner)
    assert probe.is_git is True
    assert probe.root == repo_dir.resolve()
    assert probe.common_dir == (repo_dir / ".git").resolve()
    assert probe.normalized_remotes == ["github.com/org/my-repo"]
    assert "secret123" not in str(probe.remotes)
    assert probe.error is None


def test_probe_git_file_input(tmp_path: Path):
    """Probing a file safely resolves the working directory to its parent."""
    repo_dir = tmp_path / "my-repo"
    src_dir = repo_dir / "src"
    src_dir.mkdir(parents=True)
    code_file = src_dir / "main.py"
    code_file.write_text("print('hello')", encoding="utf-8")

    runner = make_fake_git_runner(
        root=str(repo_dir),
        common_dir=str(repo_dir / ".git"),
    )

    probe = probe_git(code_file, runner=runner)
    assert probe.is_git is True
    assert probe.root == repo_dir.resolve()
    assert probe.common_dir == (repo_dir / ".git").resolve()
    assert probe.error is None


def test_probe_git_non_git_path_is_negative_result_not_traceback(tmp_path: Path):
    """A non-Git path returns is_git=False without raising exceptions."""
    non_git = tmp_path / "plain-dir"
    non_git.mkdir()

    runner = make_fake_git_runner(
        retcode=128,
        stderr="fatal: not a git repository (or any of the parent directories): .git",
    )

    probe = probe_git(non_git, runner=runner)
    assert probe.is_git is False
    assert probe.root is None
    assert probe.common_dir is None
    assert "not a git repository" in (probe.error or "")


def test_probe_git_nonexistent_path(tmp_path: Path):
    """A nonexistent path returns is_git=False gracefully."""
    missing = tmp_path / "does_not_exist"
    probe = probe_git(missing)
    assert probe.is_git is False
    assert "does not exist" in (probe.error or "")


def test_probe_git_timeout_handling(tmp_path: Path):
    """Subprocess timeout returns bounded is_git=False without crashing."""
    repo_dir = tmp_path / "timed-repo"
    repo_dir.mkdir()

    runner = make_fake_git_runner(timeout_on_rev_parse=True)
    probe = probe_git(repo_dir, runner=runner)

    assert probe.is_git is False
    assert probe.error == "Git probe timed out."


def test_probe_git_failure_never_exposes_secrets_in_stderr(tmp_path: Path):
    """Command failure sanitizes raw stderr to prevent credential exposure."""
    repo_dir = tmp_path / "secret-fail-repo"
    repo_dir.mkdir()

    secret = "TopSecretTokenXYZ99"
    runner = make_fake_git_runner(
        retcode=128,
        stderr=f"fatal: remote error at https://user:{secret}@host.com/repo.git",
    )
    probe = probe_git(repo_dir, runner=runner)

    assert probe.is_git is False
    assert secret not in (probe.error or "")


# ===========================================================================
# 3. Deterministic Resolution Order Tests (Tiers 1 to 7)
# ===========================================================================


def test_resolution_tier1_explicit_id_slug_and_alias():
    """Tier 1: Explicit project selection by ID, slug, or unique alias."""
    p1 = make_test_project(
        project_id="aaaaaaaa-1111-4111-8111-111111111111",
        slug="alpha-service",
        aliases=["alpha", "svc-a"],
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p1])

    # By ID
    res_id = resolve_project(reg, explicit_project="aaaaaaaa-1111-4111-8111-111111111111")
    assert res_id.status == "resolved"
    assert res_id.project_id == p1.id
    assert res_id.evidence == ["explicit"]
    assert res_id.requires_confirmation is False

    # By slug
    res_slug = resolve_project(reg, explicit_project="alpha-service")
    assert res_slug.status == "resolved"
    assert res_slug.project_id == p1.id
    assert res_slug.evidence == ["explicit"]

    # By alias
    res_alias = resolve_project(reg, explicit_project="svc-a")
    assert res_alias.status == "resolved"
    assert res_alias.project_id == p1.id
    assert res_alias.evidence == ["explicit"]

    # Unknown explicit project
    res_unknown = resolve_project(reg, explicit_project="non-existent")
    assert res_unknown.status == "unresolved"
    assert res_unknown.project_id is None
    assert res_unknown.evidence == ["explicit"]


def test_explicit_resolution_does_not_probe_git():
    """An explicit match is resolved without invoking the Git subprocess runner."""
    project = make_test_project()
    registry = ProjectRegistry(version=SCHEMA_VERSION, projects=[project])

    def forbidden_runner(cmd, cwd, timeout):
        raise AssertionError("Git runner must not be called for an explicit project")

    result = resolve_or_register_git(
        path="/path/that/does/not/exist",
        registry=registry,
        explicit_project=project.slug,
        git_runner=forbidden_runner,
    )
    assert result.project_id == project.id


def test_resolution_tier2_exact_registered_canonical_path(tmp_path: Path):
    """Tier 2: Exact registered canonical path match."""
    repo_path = (tmp_path / "alpha-repo").resolve()
    p1 = make_test_project(
        project_id="bbbbbbbb-2222-4222-8222-222222222222",
        slug="alpha-repo",
        paths=[str(repo_path)],
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p1])

    res = resolve_project(reg, path=repo_path)
    assert res.status == "resolved"
    assert res.project_id == p1.id
    assert res.evidence == ["path"]
    assert res.requires_confirmation is False


def test_resolution_tier3_longest_registered_ancestor_path(tmp_path: Path):
    """Tier 3: Longest registered ancestor path wins over shallower ancestor."""
    base_dir = (tmp_path / "workspace").resolve()
    sub_project_dir = (base_dir / "nested" / "my-service").resolve()

    deep_file = sub_project_dir / "pkg" / "module" / "file.py"

    p_shallow = make_test_project(
        project_id="11111111-aaaa-4aaa-8aaa-111111111111",
        slug="workspace-root",
        paths=[str(base_dir)],
    )
    p_deep = make_test_project(
        project_id="22222222-bbbb-4bbb-8bbb-222222222222",
        slug="my-service",
        paths=[str(sub_project_dir)],
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p_shallow, p_deep])

    res = resolve_project(reg, path=deep_file)
    assert res.status == "resolved"
    # Longest ancestor path must match p_deep, not p_shallow
    assert res.project_id == p_deep.id
    assert res.evidence == ["path"]


def test_resolution_tier4_git_common_dir_identity(tmp_path: Path):
    """Tier 4: Worktree or alternate path resolves via Git common dir identity."""
    repo_dir = (tmp_path / "main-repo").resolve()
    worktree_dir = (tmp_path / "worktree-repo").resolve()
    common_dir = repo_dir / ".git"

    p1 = make_test_project(
        project_id="cccccccc-3333-4333-8333-333333333333",
        slug="main-repo",
        paths=[str(repo_dir)],
        git_common_dirs=[str(common_dir)],
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p1])

    # Worktree path does not match paths in registry, but common-dir does
    fake_probe = GitProbeResult(
        is_git=True,
        root=worktree_dir,
        common_dir=common_dir,
        remotes=[],
        normalized_remotes=[],
    )

    res = resolve_project(reg, path=worktree_dir, probe=fake_probe)
    assert res.status == "resolved"
    assert res.project_id == p1.id
    assert res.evidence == ["git-common-dir"]


def test_resolution_tier5_normalized_git_remote_unique(tmp_path: Path):
    """Tier 5: New clone with unknown path/common-dir resolves via unique remote."""
    clone_dir = (tmp_path / "new-clone").resolve()
    p1 = make_test_project(
        project_id="dddddddd-4444-4444-8444-444444444444",
        slug="remote-repo",
        paths=["/other/path/repo"],
        git_common_dirs=["/other/path/repo/.git"],
        git_remotes=["github.com/my-org/shared-tool"],
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p1])

    fake_probe = GitProbeResult(
        is_git=True,
        root=clone_dir,
        common_dir=clone_dir / ".git",
        normalized_remotes=["github.com/my-org/shared-tool"],
    )

    res = resolve_project(reg, path=clone_dir, probe=fake_probe)
    assert res.status == "resolved"
    assert res.project_id == p1.id
    assert res.evidence == ["git-remote"]


def test_resolution_tier5_ambiguous_remote_never_chooses_arbitrarily(tmp_path: Path):
    """Tier 5: Multiple projects matching the same remote return ambiguous without arbitrary selection."""
    clone_dir = (tmp_path / "ambiguous-clone").resolve()
    shared_remote = "github.com/my-org/multi-clone"

    p1 = make_test_project(
        project_id="eeeeeeee-5555-4555-8555-555555555551",
        slug="clone-one",
        paths=["/repos/clone-one"],
        git_remotes=[shared_remote],
    )
    p2 = make_test_project(
        project_id="eeeeeeee-5555-4555-8555-555555555552",
        slug="clone-two",
        paths=["/repos/clone-two"],
        git_remotes=[shared_remote],
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p1, p2])

    fake_probe = GitProbeResult(
        is_git=True,
        root=clone_dir,
        common_dir=clone_dir / ".git",
        normalized_remotes=[shared_remote],
    )

    res = resolve_project(reg, path=clone_dir, probe=fake_probe)
    assert res.status == "ambiguous"
    assert res.project_id is None
    assert res.evidence == ["git-remote"]
    assert res.requires_confirmation is True
    assert sorted(res.ambiguous_project_ids) == sorted([p1.id, p2.id])


def test_resolution_tier6_unregistered_git_candidate(tmp_path: Path):
    """Tier 6: Valid Git repository with no matches in registry returns candidate."""
    fresh_repo = (tmp_path / "fresh-repo").resolve()
    reg = ProjectRegistry.empty()

    fake_probe = GitProbeResult(
        is_git=True,
        root=fresh_repo,
        common_dir=fresh_repo / ".git",
        normalized_remotes=["github.com/new-org/fresh-repo"],
    )

    res = resolve_project(reg, path=fresh_repo, probe=fake_probe)
    assert res.status == "candidate"
    assert res.project_id is None
    assert res.evidence == ["git-root"]
    assert res.requires_confirmation is True
    assert res.candidate_metadata is not None
    assert res.candidate_metadata["root"] == str(fresh_repo)
    assert res.candidate_metadata["suggested_slug"] == "fresh-repo"


def test_resolution_tier7_unresolved_non_git(tmp_path: Path):
    """Tier 7: Non-Git directory returns unresolved."""
    plain_dir = (tmp_path / "plain-folder").resolve()
    reg = ProjectRegistry.empty()

    fake_probe = GitProbeResult(is_git=False)
    res = resolve_project(reg, path=plain_dir, probe=fake_probe)

    assert res.status == "unresolved"
    assert res.project_id is None
    assert res.evidence == []
    assert res.requires_confirmation is False


# ===========================================================================
# 4. Auto-Registration Tests (on, off, dry-run, collisions, central writes)
# ===========================================================================


def test_auto_register_git_creates_record_and_persists(tmp_path: Path):
    """When auto_register_git=True, unregistered Git root is registered in registry and projects.yaml."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    repo_dir = tmp_path / "my-new-service"
    repo_dir.mkdir()

    fixed_id = "12345678-1234-4234-8234-1234567890ab"
    runner = make_fake_git_runner(
        root=str(repo_dir),
        common_dir=str(repo_dir / ".git"),
        remotes=["https://user:token@github.com/corp/my-new-service.git"],
    )

    res = resolve_or_register_git(
        path=repo_dir,
        cfg=cfg,
        auto_register_git=True,
        dry_run=False,
        id_factory=lambda: fixed_id,
        git_runner=runner,
    )

    assert res.status == "resolved"
    assert res.project_id == fixed_id
    assert res.project is not None
    assert res.project.slug == "my-new-service"
    assert res.project.display_name == "my-new-service"
    assert res.project.kind == PROJECT_KIND_GIT
    assert res.project.bindings.paths == [str(repo_dir.resolve())]
    assert res.project.bindings.git_common_dirs == [str((repo_dir / ".git").resolve())]
    assert res.project.bindings.git_remotes == ["github.com/corp/my-new-service"]
    assert res.project.memory.project_name == f"ptw-my-new-service-{fixed_id[:8]}"
    assert res.evidence == ["auto-registered"]

    # Verify persistence to cfg.projects_yaml only
    assert cfg.projects_yaml.is_file()
    saved_reg = load_registry(cfg.projects_yaml)
    assert len(saved_reg) == 1
    assert saved_reg.projects[0].id == fixed_id


def test_auto_register_git_false_writes_nothing(tmp_path: Path):
    """When auto_register_git=False, returns candidate requiring confirmation and writes nothing."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    repo_dir = tmp_path / "unregistered-repo"
    repo_dir.mkdir()

    runner = make_fake_git_runner(root=str(repo_dir))

    res = resolve_or_register_git(
        path=repo_dir,
        cfg=cfg,
        auto_register_git=False,
        dry_run=False,
        git_runner=runner,
    )

    assert res.status == "candidate"
    assert res.project_id is None
    assert res.requires_confirmation is True
    assert res.proposed_record is not None
    assert res.proposed_record.slug == "unregistered-repo"

    # Must write nothing
    assert not cfg.projects_yaml.exists()


def test_auto_register_dry_run_writes_nothing(tmp_path: Path):
    """dry_run=True returns proposed candidate metadata and writes nothing."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    repo_dir = tmp_path / "dry-run-repo"
    repo_dir.mkdir()

    runner = make_fake_git_runner(root=str(repo_dir))

    res = resolve_or_register_git(
        path=repo_dir,
        cfg=cfg,
        auto_register_git=True,
        dry_run=True,
        git_runner=runner,
    )

    assert res.status == "candidate"
    assert res.requires_confirmation is True
    assert res.proposed_record is not None
    assert res.candidate_metadata is not None
    assert "proposed_id" in res.candidate_metadata

    # Must write nothing
    assert not cfg.projects_yaml.exists()


def test_slug_collision_resolution_suffix(tmp_path: Path):
    """Slug collision deterministically resolves with ID short suffix without overwriting."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    # Existing project with slug "my-app"
    p_existing = make_test_project(
        project_id="aaaaaaaa-0000-4000-8000-000000000000",
        slug="my-app",
        display_name="Existing App",
        paths=["/repos/existing-my-app"],
        git_common_dirs=["/repos/existing-my-app/.git"],
        git_remotes=["github.com/org/existing-app"],
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p_existing])
    save_registry(reg, cfg)

    # New distinct repo with same folder name "my-app"
    new_repo = tmp_path / "my-app"
    new_repo.mkdir()

    new_id = "fedcba98-7654-4321-8234-567890abcdef"
    runner = make_fake_git_runner(
        root=str(new_repo),
        common_dir=str(new_repo / ".git"),
        remotes=["github.com/other-org/different-app"],
    )

    res = resolve_or_register_git(
        path=new_repo,
        cfg=cfg,
        registry=reg,
        auto_register_git=True,
        id_factory=lambda: new_id,
        git_runner=runner,
    )

    assert res.status == "resolved"
    assert res.project_id == new_id
    # Collision resolved: slug has ID short suffix
    expected_slug = f"my-app-{new_id[:8]}"
    assert res.project.slug == expected_slug
    assert res.project.slug != "my-app"

    # Verify both projects coexist in registry without overwrite
    saved_reg = load_registry(cfg.projects_yaml)
    assert len(saved_reg) == 2
    assert saved_reg.projects[0].slug == "my-app"
    assert saved_reg.projects[0].id == p_existing.id
    assert saved_reg.projects[1].slug == expected_slug
    assert saved_reg.projects[1].id == new_id


def test_idempotent_repeated_registration_creates_no_duplicates(tmp_path: Path):
    """Repeated registration on same path resolves existing record and creates no duplicates."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    repo_dir = tmp_path / "repeat-repo"
    repo_dir.mkdir()

    first_id = "11111111-2222-4333-8444-555555555555"
    runner = make_fake_git_runner(root=str(repo_dir))

    # First call: registers
    res1 = resolve_or_register_git(
        path=repo_dir,
        cfg=cfg,
        auto_register_git=True,
        id_factory=lambda: first_id,
        git_runner=runner,
    )
    assert res1.status == "resolved"
    assert res1.project_id == first_id

    # Second call: resolves via exact path
    second_id = "99999999-9999-4999-8999-999999999999"
    res2 = resolve_or_register_git(
        path=repo_dir,
        cfg=cfg,
        auto_register_git=True,
        id_factory=lambda: second_id,
        git_runner=runner,
    )
    assert res2.status == "resolved"
    assert res2.project_id == first_id  # same project
    assert res2.evidence == ["path"]

    # Verify registry length is still 1
    saved_reg = load_registry(cfg.projects_yaml)
    assert len(saved_reg) == 1
    assert saved_reg.projects[0].id == first_id


def test_central_only_writes_source_unchanged(tmp_path: Path):
    """Auto-registration leaves source repository completely untouched."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    repo_dir = tmp_path / "source-repo"
    repo_dir.mkdir()
    file_a = repo_dir / "file_a.txt"
    file_a.write_text("content a", encoding="utf-8")

    # Snapshot source directory contents before registration
    entries_before = sorted(os.listdir(repo_dir))
    mtime_before = file_a.stat().st_mtime_ns

    runner = make_fake_git_runner(root=str(repo_dir))

    resolve_or_register_git(
        path=repo_dir,
        cfg=cfg,
        auto_register_git=True,
        git_runner=runner,
    )

    # Snapshot source directory contents after registration
    entries_after = sorted(os.listdir(repo_dir))
    mtime_after = file_a.stat().st_mtime_ns

    # Invariant: source directory is byte-for-byte unchanged, zero pollution
    assert entries_before == entries_after
    assert mtime_before == mtime_after
    assert not (repo_dir / ".personal-tideway").exists()
    assert not (repo_dir / ".personal-tideway.yaml").exists()
    assert not (repo_dir / ".ptw").exists()
    assert not (repo_dir / ".basic-memory").exists()


def test_failed_registry_save_does_not_mutate_caller_registry(tmp_path: Path):
    """A rejected persistence attempt leaves the supplied in-memory registry unchanged."""
    ptw_home = tmp_path / "ptw_home"
    (ptw_home / "registry").mkdir(parents=True)
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    cfg.projects_yaml.write_text("version: 3\nprojects: []\n", encoding="utf-8")
    registry = ProjectRegistry.empty()
    repo_dir = tmp_path / "candidate-repo"
    repo_dir.mkdir()

    with pytest.raises(ConfigError, match="newer schema version"):
        resolve_or_register_git(
            path=repo_dir,
            cfg=cfg,
            registry=registry,
            git_runner=make_fake_git_runner(root=str(repo_dir)),
        )

    assert len(registry) == 0


def test_auto_register_git_config_round_trip_and_type_validation(tmp_path: Path):
    """The v2 config persists a strict projects.auto_register_git boolean."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home, auto_register_git=False)
    assert cfg.to_dict()["projects"] == {"auto_register_git": False}

    cfg.config_yaml.write_text(
        "version: 2\nclient_paths: {}\nskill_link_mode: symlink\nprojects:\n  auto_register_git: false\n",
        encoding="utf-8",
    )
    assert PersonalTidewayConfig.resolve(home=ptw_home).auto_register_git is False

    cfg.config_yaml.write_text(
        "version: 2\nprojects:\n  auto_register_git: 'false'\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must be a boolean"):
        PersonalTidewayConfig.resolve(home=ptw_home)


# ===========================================================================
# 5. Disposable Real-Git Integration Test (Never uses host repo)
# ===========================================================================


def test_disposable_real_git_integration(tmp_path: Path):
    """Integration test using a real disposable Git repo created in tmp_path."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    # Initialize a disposable git repository in tmp_path
    disposable_repo = tmp_path / "disposable_repo"
    disposable_repo.mkdir()

    init_res = subprocess.run(
        ["git", "init", "-b", "main", str(disposable_repo)],
        capture_output=True,
        text=True,
        check=False,
    )
    if init_res.returncode != 0:
        # Fallback for older git without -b
        subprocess.run(["git", "init", str(disposable_repo)], capture_output=True, check=True)

    # Set configured remote
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/disposable-org/disposable-repo.git"],
        cwd=str(disposable_repo),
        capture_output=True,
        check=True,
    )

    # Probe real disposable repo
    probe = probe_git(disposable_repo)
    assert probe.is_git is True
    assert probe.root == disposable_repo.resolve()
    assert probe.normalized_remotes == ["github.com/disposable-org/disposable-repo"]

    # Auto-register real disposable repo
    res = resolve_or_register_git(path=disposable_repo, cfg=cfg)
    assert res.status == "resolved"
    assert res.project is not None
    assert res.project.slug == "disposable-repo"
    assert res.project.bindings.git_remotes == ["github.com/disposable-org/disposable-repo"]

    # Verify central-only persistence
    assert cfg.projects_yaml.is_file()
    loaded = load_registry(cfg.projects_yaml)
    assert len(loaded) == 1
    assert loaded.projects[0].slug == "disposable-repo"


# ===========================================================================
# 6. Git Worktree and Moved-Repository Reconciliation Tests
# ===========================================================================


def test_reconcile_existing_exact_path_no_change(tmp_path: Path):
    """Reconciling an existing registered path returns resolved with zero changes."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    repo_dir = (tmp_path / "existing-repo").resolve()
    repo_dir.mkdir()

    p = make_test_project(
        project_id="11111111-1111-4111-8111-111111111111",
        slug="existing-repo",
        paths=[str(repo_dir)],
        git_common_dirs=[str(repo_dir / ".git")],
        git_remotes=["github.com/org/existing-repo"],
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p])
    save_registry(reg, cfg)

    fake_probe = GitProbeResult(
        is_git=True,
        root=repo_dir,
        common_dir=repo_dir / ".git",
        normalized_remotes=["github.com/org/existing-repo"],
    )

    res = reconcile_git_bindings(
        path=repo_dir,
        cfg=cfg,
        registry=reg,
        probe=fake_probe,
    )

    assert res.status == "resolved"
    assert res.project_id == p.id
    assert res.project is not None
    assert res.bindings_changed is False
    assert res.requires_confirmation is False
    assert res.evidence == ["path"]
    assert res.proposed_additions.is_empty() is True
    assert res.proposed_removals.is_empty() is True


def test_reconcile_existing_ancestor_path_no_change(tmp_path: Path):
    """Reconciling a subpath within an existing registered repo returns resolved with zero changes."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    repo_dir = (tmp_path / "ancestor-repo").resolve()
    repo_dir.mkdir()
    sub_dir = (repo_dir / "src" / "deep").resolve()
    sub_dir.mkdir(parents=True)

    p = make_test_project(
        project_id="22222222-2222-4222-8222-222222222222",
        slug="ancestor-repo",
        paths=[str(repo_dir)],
        git_common_dirs=[str(repo_dir / ".git")],
        git_remotes=["github.com/org/ancestor-repo"],
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p])
    save_registry(reg, cfg)

    fake_probe = GitProbeResult(
        is_git=True,
        root=repo_dir,
        common_dir=repo_dir / ".git",
        normalized_remotes=["github.com/org/ancestor-repo"],
    )

    res = reconcile_git_bindings(
        path=sub_dir,
        cfg=cfg,
        registry=reg,
        probe=fake_probe,
    )

    assert res.status == "resolved"
    assert res.project_id == p.id
    assert res.bindings_changed is False
    assert res.requires_confirmation is False
    assert res.evidence == ["path"]
    assert res.proposed_additions.is_empty() is True
    assert res.proposed_removals.is_empty() is True


def test_reconcile_same_git_common_dir_new_worktree_root(tmp_path: Path):
    """A new root sharing git-common-dir is automatically added to bindings preserving existing roots."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    main_root = (tmp_path / "main-root").resolve()
    main_root.mkdir()
    common_dir = (main_root / ".git").resolve()
    common_dir.mkdir()

    wt_root = (tmp_path / "worktree-root").resolve()
    wt_root.mkdir()

    fixed_id = "33333333-3333-4333-8333-333333333333"
    initial_updated_at = "2026-09-01T00:00:00Z"
    clock_updated_at = "2026-09-10T12:34:56Z"

    p = ProjectRecord.create(
        project_id=fixed_id,
        slug="shared-repo",
        display_name="Shared Repo",
        paths=[str(main_root)],
        git_common_dirs=[str(common_dir)],
        git_remotes=["github.com/org/shared-repo"],
        created_at=initial_updated_at,
        updated_at=initial_updated_at,
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p])
    save_registry(reg, cfg)

    fake_probe = GitProbeResult(
        is_git=True,
        root=wt_root,
        common_dir=common_dir,
        normalized_remotes=["github.com/org/shared-repo"],
    )

    res = reconcile_git_bindings(
        path=wt_root,
        cfg=cfg,
        registry=reg,
        probe=fake_probe,
        clock=lambda: clock_updated_at,
    )

    assert res.status == "resolved"
    assert res.project_id == fixed_id
    assert res.bindings_changed is True
    assert res.requires_confirmation is False
    assert res.evidence == ["git-common-dir"]
    assert res.proposed_additions.paths == [str(wt_root)]
    assert res.proposed_removals.is_empty() is True

    # Check project in memory
    assert len(res.project.bindings.paths) == 2
    assert str(main_root) in res.project.bindings.paths
    assert str(wt_root) in res.project.bindings.paths
    assert res.project.bindings.git_common_dirs == [str(common_dir)]
    assert res.project.updated_at == clock_updated_at
    assert res.project.id == fixed_id
    assert res.project.memory.project_name == f"ptw-shared-repo-{fixed_id[:8]}"

    # Check persisted registry in projects.yaml
    saved_reg = load_registry(cfg.projects_yaml)
    assert len(saved_reg) == 1
    persisted_p = saved_reg.projects[0]
    assert persisted_p.id == fixed_id
    assert persisted_p.updated_at == clock_updated_at
    assert len(persisted_p.bindings.paths) == 2
    assert str(main_root) in persisted_p.bindings.paths
    assert str(wt_root) in persisted_p.bindings.paths


def test_reconcile_remote_only_obvious_move(tmp_path: Path):
    """When all old bound paths/common-dirs are missing, unique remote match replaces stale bindings automatically."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    old_root = tmp_path / "non-existent-old-repo"
    old_gcd = old_root / ".git"
    assert not old_root.exists()

    new_root = (tmp_path / "moved-repo").resolve()
    new_root.mkdir()
    new_gcd = (new_root / ".git").resolve()
    new_gcd.mkdir()

    fixed_id = "44444444-4444-4444-8444-444444444444"
    initial_updated_at = "2026-09-01T00:00:00Z"
    clock_updated_at = "2026-09-10T14:00:00Z"

    p = ProjectRecord.create(
        project_id=fixed_id,
        slug="moved-project",
        display_name="Moved Project",
        paths=[str(old_root)],
        git_common_dirs=[str(old_gcd)],
        git_remotes=["github.com/org/unique-moved-repo"],
        created_at=initial_updated_at,
        updated_at=initial_updated_at,
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p])
    save_registry(reg, cfg)

    fake_probe = GitProbeResult(
        is_git=True,
        root=new_root,
        common_dir=new_gcd,
        normalized_remotes=["github.com/org/unique-moved-repo"],
    )

    res = reconcile_git_bindings(
        path=new_root,
        cfg=cfg,
        registry=reg,
        probe=fake_probe,
        clock=lambda: clock_updated_at,
    )

    assert res.status == "resolved"
    assert res.project_id == fixed_id
    assert res.bindings_changed is True
    assert res.requires_confirmation is False
    assert res.evidence == ["git-remote"]
    assert res.proposed_additions.paths == [str(new_root)]
    assert res.proposed_additions.git_common_dirs == [str(new_gcd)]
    assert res.proposed_removals.paths == [str(old_root)]
    assert res.proposed_removals.git_common_dirs == [str(old_gcd)]

    # In-memory record updated
    assert res.project.bindings.paths == [str(new_root)]
    assert res.project.bindings.git_common_dirs == [str(new_gcd)]
    assert res.project.updated_at == clock_updated_at
    assert res.project.id == fixed_id

    # Persisted to file
    saved_reg = load_registry(cfg.projects_yaml)
    assert len(saved_reg) == 1
    persisted_p = saved_reg.projects[0]
    assert persisted_p.id == fixed_id
    assert persisted_p.bindings.paths == [str(new_root)]
    assert persisted_p.bindings.git_common_dirs == [str(new_gcd)]
    assert persisted_p.updated_at == clock_updated_at


def test_reconcile_remote_only_additional_live_clone(tmp_path: Path):
    """When an old bound path still exists, unique remote match returns proposal requiring confirmation without mutating."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    old_root = (tmp_path / "original-clone").resolve()
    old_root.mkdir()
    old_gcd = (old_root / ".git").resolve()
    old_gcd.mkdir()

    new_clone = (tmp_path / "second-clone").resolve()
    new_clone.mkdir()
    new_gcd = (new_clone / ".git").resolve()
    new_gcd.mkdir()

    fixed_id = "55555555-5555-4555-8555-555555555555"
    initial_updated_at = "2026-09-01T00:00:00Z"

    p = ProjectRecord.create(
        project_id=fixed_id,
        slug="cloned-project",
        display_name="Cloned Project",
        paths=[str(old_root)],
        git_common_dirs=[str(old_gcd)],
        git_remotes=["github.com/org/cloned-repo"],
        created_at=initial_updated_at,
        updated_at=initial_updated_at,
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p])
    save_registry(reg, cfg)

    fake_probe = GitProbeResult(
        is_git=True,
        root=new_clone,
        common_dir=new_gcd,
        normalized_remotes=["github.com/org/cloned-repo"],
    )

    res = reconcile_git_bindings(
        path=new_clone,
        cfg=cfg,
        registry=reg,
        probe=fake_probe,
    )

    assert res.status == "resolved"
    assert res.project_id == fixed_id
    assert res.bindings_changed is False
    assert res.requires_confirmation is True
    assert res.evidence == ["git-remote"]
    assert res.proposed_additions.paths == [str(new_clone)]
    assert res.proposed_removals.is_empty() is True

    # No writes performed!
    assert res.project.bindings.paths == [str(old_root)]
    assert res.project.updated_at == initial_updated_at

    saved_reg = load_registry(cfg.projects_yaml)
    assert len(saved_reg.projects[0].bindings.paths) == 1
    assert saved_reg.projects[0].bindings.paths[0] == str(old_root)
    assert saved_reg.projects[0].updated_at == initial_updated_at


def test_reconcile_dry_run_zero_writes(tmp_path: Path):
    """dry_run=True returns exact proposal with zero writes and zero in-memory mutations."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    main_root = (tmp_path / "dry-main").resolve()
    main_root.mkdir()
    common_dir = (main_root / ".git").resolve()
    common_dir.mkdir()

    wt_root = (tmp_path / "dry-worktree").resolve()
    wt_root.mkdir()

    fixed_id = "66666666-6666-4666-8666-666666666666"
    initial_updated_at = "2026-09-01T00:00:00Z"

    p = ProjectRecord.create(
        project_id=fixed_id,
        slug="dry-repo",
        display_name="Dry Repo",
        paths=[str(main_root)],
        git_common_dirs=[str(common_dir)],
        git_remotes=["github.com/org/dry-repo"],
        created_at=initial_updated_at,
        updated_at=initial_updated_at,
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p])
    save_registry(reg, cfg)

    fake_probe = GitProbeResult(
        is_git=True,
        root=wt_root,
        common_dir=common_dir,
        normalized_remotes=["github.com/org/dry-repo"],
    )

    res = reconcile_git_bindings(
        path=wt_root,
        cfg=cfg,
        registry=reg,
        probe=fake_probe,
        dry_run=True,
        clock=lambda: "2026-09-10T15:00:00Z",
    )

    assert res.status == "resolved"
    assert res.project_id == fixed_id
    assert res.bindings_changed is False
    assert res.proposed_additions.paths == [str(wt_root)]
    assert res.proposed_removals.is_empty() is True

    # In-memory registry unchanged
    assert reg.projects[0].bindings.paths == [str(main_root)]
    assert reg.projects[0].updated_at == initial_updated_at

    # Persisted registry on disk unchanged
    saved_reg = load_registry(cfg.projects_yaml)
    assert len(saved_reg.projects[0].bindings.paths) == 1
    assert saved_reg.projects[0].updated_at == initial_updated_at


def test_reconcile_repeated_calls_byte_idempotent(tmp_path: Path):
    """Repeated calls produce byte-idempotent canonical projects.yaml without spurious timestamp updates."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    main_root = (tmp_path / "idempotent-main").resolve()
    main_root.mkdir()
    common_dir = (main_root / ".git").resolve()
    common_dir.mkdir()

    wt_root = (tmp_path / "idempotent-wt").resolve()
    wt_root.mkdir()

    fixed_id = "77777777-7777-4777-8777-777777777777"
    call1_time = "2026-09-10T16:00:00Z"
    call2_time = "2026-09-10T16:05:00Z"

    p = ProjectRecord.create(
        project_id=fixed_id,
        slug="idempotent-repo",
        display_name="Idempotent Repo",
        paths=[str(main_root)],
        git_common_dirs=[str(common_dir)],
        git_remotes=["github.com/org/idempotent-repo"],
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p])
    save_registry(reg, cfg)

    fake_probe = GitProbeResult(
        is_git=True,
        root=wt_root,
        common_dir=common_dir,
        normalized_remotes=["github.com/org/idempotent-repo"],
    )

    # First call: reconciles worktree and updates registry
    res1 = reconcile_git_bindings(
        path=wt_root,
        cfg=cfg,
        registry=reg,
        probe=fake_probe,
        clock=lambda: call1_time,
    )
    assert res1.bindings_changed is True
    assert res1.project.updated_at == call1_time
    yaml_bytes_1 = cfg.projects_yaml.read_bytes()

    # Second call on same path: resolved via exact path, zero mutation, byte-identical YAML
    res2 = reconcile_git_bindings(
        path=wt_root,
        cfg=cfg,
        registry=reg,
        probe=fake_probe,
        clock=lambda: call2_time,
    )
    assert res2.status == "resolved"
    assert res2.bindings_changed is False
    assert res2.evidence == ["path"]
    assert res2.project.updated_at == call1_time  # clock was not called / updated_at not bumped

    yaml_bytes_2 = cfg.projects_yaml.read_bytes()
    assert yaml_bytes_1 == yaml_bytes_2


def test_reconcile_persistence_failure_rollback(tmp_path: Path):
    """Failed persistence rolls back and leaves supplied in-memory registry completely unchanged."""
    ptw_home = tmp_path / "ptw_home"
    (ptw_home / "registry").mkdir(parents=True)
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    # Write invalid registry version to force save failure
    cfg.projects_yaml.write_text("version: 999\nprojects: []\n", encoding="utf-8")

    main_root = (tmp_path / "fail-main").resolve()
    main_root.mkdir()
    common_dir = (main_root / ".git").resolve()
    common_dir.mkdir()

    wt_root = (tmp_path / "fail-wt").resolve()
    wt_root.mkdir()

    initial_updated_at = "2026-09-01T00:00:00Z"
    p = ProjectRecord.create(
        project_id="88888888-8888-4888-8888-888888888888",
        slug="fail-repo",
        display_name="Fail Repo",
        paths=[str(main_root)],
        git_common_dirs=[str(common_dir)],
        git_remotes=["github.com/org/fail-repo"],
        created_at=initial_updated_at,
        updated_at=initial_updated_at,
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p])

    fake_probe = GitProbeResult(
        is_git=True,
        root=wt_root,
        common_dir=common_dir,
        normalized_remotes=["github.com/org/fail-repo"],
    )

    with pytest.raises(ConfigError, match="newer schema version"):
        reconcile_git_bindings(
            path=wt_root,
            cfg=cfg,
            registry=reg,
            probe=fake_probe,
            clock=lambda: "2026-09-10T17:00:00Z",
        )

    # In-memory registry must be completely unchanged!
    assert len(reg.projects[0].bindings.paths) == 1
    assert reg.projects[0].bindings.paths[0] == str(main_root)
    assert reg.projects[0].updated_at == initial_updated_at


def test_reconcile_ambiguous_remote_matches(tmp_path: Path):
    """Ambiguous remote matches never mutate or choose a project."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    target_dir = (tmp_path / "ambig-repo").resolve()
    target_dir.mkdir()

    shared_remote = "github.com/shared-org/ambiguous-repo"
    p1 = make_test_project(
        project_id="aaaaaaaa-1111-4111-8111-111111111111",
        slug="p1-ambig",
        paths=["/repos/p1"],
        git_common_dirs=["/repos/p1/.git"],
        git_remotes=[shared_remote],
    )
    p2 = make_test_project(
        project_id="bbbbbbbb-2222-4222-8222-222222222222",
        slug="p2-ambig",
        paths=["/repos/p2"],
        git_common_dirs=["/repos/p2/.git"],
        git_remotes=[shared_remote],
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p1, p2])
    save_registry(reg, cfg)

    fake_probe = GitProbeResult(
        is_git=True,
        root=target_dir,
        common_dir=target_dir / ".git",
        normalized_remotes=[shared_remote],
    )

    res = reconcile_git_bindings(
        path=target_dir,
        cfg=cfg,
        registry=reg,
        probe=fake_probe,
    )

    assert res.status == "ambiguous"
    assert res.project_id is None
    assert res.project is None
    assert res.bindings_changed is False
    assert res.requires_confirmation is True
    assert sorted(res.ambiguous_project_ids) == sorted([p1.id, p2.id])
    assert res.proposed_additions.is_empty() is True
    assert res.proposed_removals.is_empty() is True


def test_reconcile_explicit_project_never_switches_to_another_match(tmp_path: Path):
    """Explicit selection constrains reconciliation even if another project matches Git identity."""
    target_dir = tmp_path / "explicit-target"
    target_dir.mkdir()
    first = make_test_project(
        project_id="12121212-1212-4212-8212-121212121212",
        slug="explicit-one",
        paths=["/missing/explicit-one"],
        git_common_dirs=["/missing/explicit-one/.git"],
        git_remotes=["github.com/org/explicit-one"],
    )
    second = make_test_project(
        project_id="34343434-3434-4434-8434-343434343434",
        slug="other-match",
        paths=["/missing/other-match"],
        git_common_dirs=["/missing/other-match/.git"],
        git_remotes=["github.com/org/other-match"],
    )
    registry = ProjectRegistry(version=SCHEMA_VERSION, projects=[first, second])
    probe = GitProbeResult(
        is_git=True,
        root=target_dir,
        common_dir=Path(second.bindings.git_common_dirs[0]),
        normalized_remotes=list(second.bindings.git_remotes),
    )

    result = reconcile_git_bindings(
        path=target_dir,
        registry=registry,
        explicit_project=first.slug,
        probe=probe,
        dry_run=True,
    )

    assert result.project_id == first.id
    assert result.evidence == ["explicit"]
    assert result.requires_confirmation is True


def test_uncertain_old_binding_is_never_removed(tmp_path: Path, monkeypatch):
    """Permission or I/O uncertainty is treated like a live binding, never as a stale path."""
    target_dir = tmp_path / "uncertain-target"
    target_dir.mkdir()
    project = make_test_project(
        paths=["/unreadable/old-root"],
        git_common_dirs=["/unreadable/old-root/.git"],
        git_remotes=["github.com/org/test-repo"],
    )
    registry = ProjectRegistry(version=SCHEMA_VERSION, projects=[project])
    probe = GitProbeResult(
        is_git=True,
        root=target_dir,
        common_dir=target_dir / ".git",
        normalized_remotes=["github.com/org/test-repo"],
    )
    monkeypatch.setattr(
        GitBindingReconciliationService,
        "_path_exists_with_certainty",
        staticmethod(lambda _path: None),
    )

    result = reconcile_git_bindings(path=target_dir, registry=registry, probe=probe)

    assert result.requires_confirmation is True
    assert result.bindings_changed is False
    assert result.proposed_removals.is_empty()


def test_reconcile_no_source_writes(tmp_path: Path):
    """Reconciliation leaves the source repository completely untouched with zero writes."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    main_root = (tmp_path / "source-main").resolve()
    main_root.mkdir()
    common_dir = (main_root / ".git").resolve()
    common_dir.mkdir()

    wt_root = (tmp_path / "source-wt").resolve()
    wt_root.mkdir()
    file_wt = wt_root / "code.py"
    file_wt.write_text("print('hello')", encoding="utf-8")

    p = ProjectRecord.create(
        project_id="cccccccc-3333-4333-8333-333333333333",
        slug="source-repo",
        display_name="Source Repo",
        paths=[str(main_root)],
        git_common_dirs=[str(common_dir)],
        git_remotes=["github.com/org/source-repo"],
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p])
    save_registry(reg, cfg)

    entries_before = sorted(os.listdir(wt_root))
    mtime_before = file_wt.stat().st_mtime_ns

    fake_probe = GitProbeResult(
        is_git=True,
        root=wt_root,
        common_dir=common_dir,
        normalized_remotes=["github.com/org/source-repo"],
    )

    reconcile_git_bindings(
        path=wt_root,
        cfg=cfg,
        registry=reg,
        probe=fake_probe,
    )

    entries_after = sorted(os.listdir(wt_root))
    mtime_after = file_wt.stat().st_mtime_ns

    assert entries_before == entries_after
    assert mtime_before == mtime_after
    assert not (wt_root / ".personal-tideway").exists()
    assert not (wt_root / ".ptw").exists()


def test_reconcile_secret_free_output(tmp_path: Path):
    """Reconciliation result dictionary strips embedded passwords and tokens."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    repo_dir = (tmp_path / "secret-repo").resolve()
    repo_dir.mkdir()

    secret_raw = "https://alice:super_secret_token_98765@github.com/org/secret-repo.git"
    normalized = normalize_git_remote(secret_raw)
    assert normalized == "github.com/org/secret-repo"

    fake_probe = probe_git(
        repo_dir,
        runner=make_fake_git_runner(
            root=str(repo_dir),
            common_dir=str(repo_dir / ".git"),
            remotes=[secret_raw],
        ),
    )

    p = ProjectRecord.create(
        project_id="dddddddd-4444-4444-8444-444444444444",
        slug="secret-proj",
        display_name="Secret Project",
        paths=["/tmp/nonexistent-old-secret-path"],
        git_common_dirs=["/tmp/nonexistent-old-secret-path/.git"],
        git_remotes=[normalized],
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p])
    save_registry(reg, cfg)

    res = reconcile_git_bindings(
        path=repo_dir,
        cfg=cfg,
        registry=reg,
        probe=fake_probe,
    )

    d = res.to_dict()
    d_str = str(d)

    assert "super_secret_token" not in d_str
    assert "super_secret_token" not in str(res.proposed_additions.to_dict())
    assert "super_secret_token" not in str(res.proposed_removals.to_dict())
    assert "super_secret_token" not in str(res.evidence)


def test_reconcile_spaces_unicode_symlinks_and_bare_repo(tmp_path: Path):
    """Conservative handling of spaces, Unicode, symlinks, and bare repositories."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    # 1. Spaces and Unicode in worktree path
    main_root = (tmp_path / "папка с пробелами и юникодом" / "main").resolve()
    main_root.mkdir(parents=True)
    common_dir = (main_root / ".git").resolve()
    common_dir.mkdir()

    wt_root = (tmp_path / "папка с пробелами и юникодом" / "worktree 1").resolve()
    wt_root.mkdir()

    p = ProjectRecord.create(
        project_id="99999999-9999-4999-8999-999999999999",
        slug="unicode-repo",
        display_name="Unicode Repo",
        paths=[str(main_root)],
        git_common_dirs=[str(common_dir)],
        git_remotes=["github.com/org/unicode-repo"],
    )
    reg = ProjectRegistry(version=SCHEMA_VERSION, projects=[p])
    save_registry(reg, cfg)

    fake_probe = GitProbeResult(
        is_git=True,
        root=wt_root,
        common_dir=common_dir,
        normalized_remotes=["github.com/org/unicode-repo"],
    )

    res = reconcile_git_bindings(path=wt_root, cfg=cfg, registry=reg, probe=fake_probe)
    assert res.status == "resolved"
    assert res.bindings_changed is True
    assert str(wt_root) in res.project.bindings.paths

    # 2. Symlink to worktree
    symlink_wt = tmp_path / "symlink-wt"
    symlink_wt.symlink_to(wt_root)
    res_sym = reconcile_git_bindings(path=symlink_wt, cfg=cfg, registry=reg, probe=fake_probe)
    assert res_sym.status == "resolved"
    assert res_sym.bindings_changed is False  # already bound resolved path

    # 3. Bare repository where rev-parse --show-toplevel fails
    bare_probe = GitProbeResult(
        is_git=False,
        root=None,
        error="Path is not a git repository or is bare.",
    )
    res_bare = reconcile_git_bindings(path=tmp_path / "bare-repo", cfg=cfg, registry=reg, probe=bare_probe)
    assert res_bare.status == "unresolved"
    assert res_bare.bindings_changed is False

    # 4. Non-existent path
    res_nonexistent = reconcile_git_bindings(path=tmp_path / "does-not-exist-at-all", cfg=cfg, registry=reg)
    assert res_nonexistent.status == "unresolved"
    assert res_nonexistent.bindings_changed is False


def test_disposable_real_git_worktree_integration(tmp_path: Path):
    """Integration test using a real disposable Git repo and worktree created in tmp_path."""
    ptw_home = tmp_path / "ptw_home"
    ptw_home.mkdir()
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)

    disposable_main = (tmp_path / "real_main_repo").resolve()
    disposable_main.mkdir()

    # Initialize disposable repo
    init_res = subprocess.run(
        ["git", "init", "-b", "main", str(disposable_main)],
        capture_output=True,
        text=True,
        check=False,
    )
    if init_res.returncode != 0:
        subprocess.run(["git", "init", str(disposable_main)], capture_output=True, check=True)

    # Git identity for commit
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(disposable_main), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(disposable_main), capture_output=True, check=True)

    # Commit initial file required for worktree add
    dummy_file = disposable_main / "README.md"
    dummy_file.write_text("# Test", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(disposable_main), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(disposable_main), capture_output=True, check=True)

    # Add configured remote
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/real-org/disposable-wt-repo.git"],
        cwd=str(disposable_main),
        capture_output=True,
        check=True,
    )

    # Create disposable worktree
    disposable_wt = (tmp_path / "real_worktree_repo").resolve()
    wt_add_res = subprocess.run(
        ["git", "worktree", "add", str(disposable_wt), "-b", "feature-wt"],
        cwd=str(disposable_main),
        capture_output=True,
        text=True,
        check=False,
    )
    if wt_add_res.returncode != 0:
        pytest.skip(f"Git worktree not supported or failed on local git binary: {wt_add_res.stderr}")

    # Register main repo first
    main_res = resolve_or_register_git(path=disposable_main, cfg=cfg)
    assert main_res.status == "resolved"
    assert main_res.project is not None
    original_project_id = main_res.project.id
    original_memory_name = main_res.project.memory.project_name

    # Reconcile from worktree
    wt_res = reconcile_git_bindings(path=disposable_wt, cfg=cfg)
    assert wt_res.status == "resolved"
    assert wt_res.project_id == original_project_id
    assert wt_res.bindings_changed is True
    assert wt_res.requires_confirmation is False
    assert wt_res.evidence == ["git-common-dir"]
    assert str(disposable_wt) in wt_res.project.bindings.paths
    assert str(disposable_main) in wt_res.project.bindings.paths

    # Verify central-only persistence
    saved_reg = load_registry(cfg.projects_yaml)
    assert len(saved_reg) == 1
    assert saved_reg.projects[0].id == original_project_id
    assert saved_reg.projects[0].memory.project_name == original_memory_name
    assert str(disposable_wt) in saved_reg.projects[0].bindings.paths
    assert str(disposable_main) in saved_reg.projects[0].bindings.paths


def test_resolve_or_register_git_probe_and_display_name_reuse(tmp_path: Path):
    """resolve_or_register_git reuses injected probe, sets custom display_name, and protects existing names."""
    ptw_home = tmp_path / "ptw"
    cfg = PersonalTidewayConfig.resolve(home=ptw_home)
    repo_dir = (tmp_path / "my-custom-repo").resolve()
    repo_dir.mkdir()

    probe = GitProbeResult(
        is_git=True,
        root=repo_dir,
        common_dir=repo_dir / ".git",
        remotes=["https://github.com/org/custom.git"],
        normalized_remotes=["github.com/org/custom"],
    )

    def blocked_runner(*_args, **_kwargs):
        raise AssertionError("Runner must not be invoked when probe is injected")

    # 1. Reject invalid display_name
    with pytest.raises(ValidationError, match="Field 'display_name' must be a non-empty string"):
        resolve_or_register_git(path=repo_dir, cfg=cfg, probe=probe, display_name="   ", git_runner=blocked_runner)

    # 2. Register with injected probe and custom display_name
    res = resolve_or_register_git(
        path=repo_dir,
        cfg=cfg,
        auto_register_git=True,
        probe=probe,
        display_name="Custom Display Name",
        git_runner=blocked_runner,
    )
    assert res.status == "resolved"
    assert res.project is not None
    assert res.project.display_name == "Custom Display Name"
    assert res.project.kind == PROJECT_KIND_GIT

    # 3. Existing project display_name is NEVER mutated as side effect
    res_repeat = resolve_or_register_git(
        path=repo_dir,
        cfg=cfg,
        probe=probe,
        display_name="Attempted Name Overwrite",
        git_runner=blocked_runner,
    )
    assert res_repeat.status == "resolved"
    assert res_repeat.project is not None
    assert res_repeat.project.id == res.project.id
    assert res_repeat.project.display_name == "Custom Display Name"

    # 4. Backward compatibility: call without probe/display_name succeeds
    other_repo = (tmp_path / "compat-repo").resolve()
    other_repo.mkdir()
    runner = make_fake_git_runner(root=str(other_repo))
    res_compat = resolve_or_register_git(path=other_repo, cfg=cfg, git_runner=runner)
    assert res_compat.status == "resolved"
    assert res_compat.project is not None
    assert res_compat.project.display_name == "compat-repo"
