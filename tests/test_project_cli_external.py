"""CLI tests for explicit external-project registration."""

import json
from pathlib import Path

import pytest

from personal_tideway.cli.main import build_parser, main, preprocess_cli_args
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import (
    ExitCode,
    PROJECT_KIND_DIRECTORY,
    PROJECT_KIND_EXTERNAL,
    PROJECT_KIND_GIT,
)
from personal_tideway.core.project_resolver import GitProbeResult
from personal_tideway.core.registry import ProjectRecord, ProjectRegistry, load_registry, save_registry
from personal_tideway.core.workspace import init_workspace


def registry_bytes(home: Path) -> bytes | None:
    path = home / "registry" / "projects.yaml"
    return path.read_bytes() if path.is_file() else None


def test_add_external_parser_and_help(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["--project", "selected", "project", "add-external", "Edge Router", "--alias", "r1", "--alias", "r2"]
    )
    assert args.selected_project == "selected"
    assert args.name == "Edge Router"
    assert args.aliases == ["r1", "r2"]
    assert not hasattr(args, "project_token")

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["project", "add-external", "--help"])
    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out.lower()
    assert "confirm" in help_text
    assert "--alias" in help_text

    assert preprocess_cli_args(
        ["mcp", "add", "server", "--args", "--alias", "value", "--project", "remote", "--name", "srv", "--kind", "std"]
    ) == [
        "mcp",
        "add",
        "server",
        "--arg=--alias",
        "--arg=value",
        "--arg=--project",
        "--arg=remote",
        "--arg=--name",
        "--arg=srv",
        "--arg=--kind",
        "--arg=std",
    ]


def test_add_external_refuses_before_init_without_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "missing-home"
    code = main(["--home", str(home), "project", "add-external", "Home Router"])
    stdout, stderr = capsys.readouterr()

    assert code == ExitCode.CONFIG_ERROR
    assert stdout == ""
    assert "not initialized" in stderr
    assert not home.exists()


def test_add_external_persists_and_is_visible_to_read_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)

    code = main(
        ["--home", str(home), "project", "add-external", "Proxmox Cluster", "--alias", "pve", "--alias", "cluster"]
    )
    output = capsys.readouterr().out
    assert code == ExitCode.SUCCESS

    registry = load_registry(cfg.projects_yaml)
    assert len(registry.projects) == 1
    record = registry.projects[0]
    assert record.kind == PROJECT_KIND_EXTERNAL
    assert record.display_name == "Proxmox Cluster"
    assert record.slug == "proxmox-cluster"
    assert record.aliases == ["pve", "cluster"]
    assert record.bindings.to_dict() == {"paths": [], "git_common_dirs": [], "git_remotes": []}
    assert record.id in output

    assert main(["--home", str(home), "project", "list", "--json"]) == ExitCode.SUCCESS
    listed = json.loads(capsys.readouterr().out)
    assert listed["projects"] == [record.to_dict()]
    assert main(["--home", str(home), "project", "show", "PVE", "--json"]) == ExitCode.SUCCESS
    assert json.loads(capsys.readouterr().out) == record.to_dict()


def test_add_external_passes_exact_proposal_and_literal_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)

    import personal_tideway.cli.main as cli_main

    real_propose = cli_main.propose_external_project
    real_confirm = cli_main.confirm_external_project
    observed: dict[str, object] = {}

    def propose_spy(*args: object, **kwargs: object):
        result = real_propose(*args, **kwargs)
        observed["proposed"] = result
        return result

    def confirm_spy(proposal: object, *, confirmed: bool = False, **kwargs: object):
        observed["confirmed_proposal"] = proposal
        observed["confirmed"] = confirmed
        return real_confirm(proposal, confirmed=confirmed, **kwargs)

    monkeypatch.setattr(cli_main, "propose_external_project", propose_spy)
    monkeypatch.setattr(cli_main, "confirm_external_project", confirm_spy)

    assert main(["--home", str(home), "project", "add-external", "Storage Node"]) == ExitCode.SUCCESS
    capsys.readouterr()
    assert observed["confirmed_proposal"] is observed["proposed"]
    assert observed["confirmed"] is True
    assert type(observed["confirmed"]) is bool


def test_add_external_failure_preserves_registry_and_unrelated_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)
    existing = ProjectRecord.create(
        project_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        slug="gateway-device",
        display_name="Gateway Device",
        kind=PROJECT_KIND_EXTERNAL,
        aliases=["gw-main"],
    )
    save_registry(ProjectRegistry(projects=[existing]), cfg)
    before = registry_bytes(home)

    unrelated = tmp_path / "outside"
    unrelated.mkdir()
    marker = unrelated / "marker.txt"
    marker.write_text("unchanged", encoding="utf-8")

    code = main(["--home", str(home), "project", "add-external", "New Gateway", "--alias", "gw-main"])
    capsys.readouterr()
    assert code == ExitCode.VALIDATION_ERROR
    assert registry_bytes(home) == before
    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert list(unrelated.iterdir()) == [marker]


# ===========================================================================
# ptw project add tests
# ===========================================================================


@pytest.fixture(autouse=True)
def guard_git_runner(monkeypatch: pytest.MonkeyPatch):
    """Guard against unintended real host git execution across all tests in this module."""
    def guarded_git(cmd, cwd, timeout=3.0):
        raise AssertionError(f"Host subprocess execution blocked in tests: {cmd} at {cwd}")

    monkeypatch.setattr(
        "personal_tideway.core.project_resolver.default_git_runner",
        guarded_git,
    )


def test_project_add_parser_defaults_and_options():
    """Parser tests for project add: default '.', --name, and --kind."""
    parser = build_parser()
    args_default = parser.parse_args(["project", "add"])
    assert args_default.path == "."
    assert args_default.name is None
    assert args_default.kind is None

    args_custom = parser.parse_args(["project", "add", "my/repo", "--name", "My Name", "--kind", "git"])
    assert args_custom.path == "my/repo"
    assert args_custom.name == "My Name"
    assert args_custom.kind == "git"

    args_dir = parser.parse_args(["project", "add", "--kind", "directory"])
    assert args_dir.path == "."
    assert args_dir.kind == "directory"


def test_project_add_refuses_uninitialized_zero_writes(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """project add refuses on uninitialized workspace without creating anything."""
    home = tmp_path / "uninitialized_ptw"
    target = tmp_path / "target_dir"
    target.mkdir()

    code = main(["--home", str(home), "project", "add", str(target)])
    out, err = capsys.readouterr()
    assert code == ExitCode.CONFIG_ERROR
    assert not home.exists()
    assert "not initialized" in err
    assert list(target.iterdir()) == []


def test_project_add_autodetected_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """Auto-detected non-Git path registers as directory project."""
    home = tmp_path / "ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)

    target = tmp_path / "my_plain_dir"
    target.mkdir()

    fake_probe = GitProbeResult(is_git=False, status="not_git")
    monkeypatch.setattr("personal_tideway.cli.main.probe_git", lambda _p: fake_probe)

    code = main(["--home", str(home), "project", "add", str(target)])
    out, err = capsys.readouterr()
    assert code == ExitCode.SUCCESS
    assert "Registered new directory project: my_plain_dir" in out
    assert err == ""

    reg = load_registry(cfg.projects_yaml)
    assert len(reg.projects) == 1
    assert reg.projects[0].kind == PROJECT_KIND_DIRECTORY
    assert reg.projects[0].display_name == "my_plain_dir"


def test_project_add_autodetected_git_with_custom_name_and_single_probe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """Auto-detected Git path registers with custom name and calls probe_git exactly once."""
    home = tmp_path / "ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)

    repo_dir = tmp_path / "my_git_repo"
    repo_dir.mkdir()

    probe_calls = []
    fake_probe = GitProbeResult(
        is_git=True,
        root=repo_dir,
        common_dir=repo_dir / ".git",
        remotes=["https://github.com/myorg/repo.git"],
        normalized_remotes=["github.com/myorg/repo"],
        status="git",
    )

    def probe_spy(p):
        probe_calls.append(p)
        return fake_probe

    monkeypatch.setattr("personal_tideway.cli.main.probe_git", probe_spy)

    code = main(["--home", str(home), "project", "add", str(repo_dir), "--name", "Custom Project Name"])
    out, err = capsys.readouterr()
    assert code == ExitCode.SUCCESS
    assert len(probe_calls) == 1
    assert "Registered new git project: Custom Project Name" in out
    assert err == ""

    reg = load_registry(cfg.projects_yaml)
    assert len(reg.projects) == 1
    assert reg.projects[0].kind == PROJECT_KIND_GIT
    assert reg.projects[0].display_name == "Custom Project Name"


def test_project_add_explicit_kind_mismatch_both_ways(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """Explicit kind mismatch is rejected without writes in both directions."""
    home = tmp_path / "ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)
    before = registry_bytes(home)

    git_target = tmp_path / "git_target"
    git_target.mkdir()
    non_git_target = tmp_path / "dir_target"
    non_git_target.mkdir()

    # 1. kind=directory on Git repo
    monkeypatch.setattr(
        "personal_tideway.cli.main.probe_git",
        lambda _p: GitProbeResult(is_git=True, root=git_target, status="git"),
    )
    code_dir_fail = main(["--home", str(home), "project", "add", str(git_target), "--kind", "directory"])
    _, err1 = capsys.readouterr()
    assert code_dir_fail == ExitCode.VALIDATION_ERROR
    assert "inside a Git repository" in err1
    assert registry_bytes(home) == before

    # 2. kind=git on non-Git directory
    monkeypatch.setattr(
        "personal_tideway.cli.main.probe_git",
        lambda _p: GitProbeResult(is_git=False, status="not_git"),
    )
    code_git_fail = main(["--home", str(home), "project", "add", str(non_git_target), "--kind", "git"])
    _, err2 = capsys.readouterr()
    assert code_git_fail == ExitCode.VALIDATION_ERROR
    assert "not a Git repository" in err2
    assert registry_bytes(home) == before


def test_project_add_probe_error_secret_stderr_not_echoed_and_bytes_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """Probe error fails safely with fixed message, no secret stderr, and bytes unchanged."""
    home = tmp_path / "ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)
    before = registry_bytes(home)

    target = tmp_path / "err_target"
    target.mkdir()

    secret = "TopSecretToken_123456789"
    monkeypatch.setattr(
        "personal_tideway.cli.main.probe_git",
        lambda _p: GitProbeResult(
            is_git=False,
            error=f"fatal: auth failed for https://bot:{secret}@host",
            status="probe_error",
        ),
    )

    code = main(["--home", str(home), "project", "add", str(target)])
    out, err = capsys.readouterr()
    assert code == ExitCode.VALIDATION_ERROR
    assert secret not in out
    assert secret not in err
    assert "Git probe failed or status uncertain" in err
    assert registry_bytes(home) == before


def test_project_add_idempotent_repeated_add(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """Repeated add on registered project reports already registered and does not duplicate."""
    home = tmp_path / "ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)

    target = tmp_path / "idempotent_dir"
    target.mkdir()

    monkeypatch.setattr(
        "personal_tideway.cli.main.probe_git",
        lambda _p: GitProbeResult(is_git=False, status="not_git"),
    )

    # First add
    assert main(["--home", str(home), "project", "add", str(target)]) == ExitCode.SUCCESS
    out1, _ = capsys.readouterr()
    assert "Registered new directory project" in out1

    # Second add
    assert main(["--home", str(home), "project", "add", str(target)]) == ExitCode.SUCCESS
    out2, _ = capsys.readouterr()
    assert "Project already registered" in out2

    reg = load_registry(cfg.projects_yaml)
    assert len(reg.projects) == 1


def test_project_add_target_tree_untouched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    """project add never writes any files into target project directory."""
    home = tmp_path / "ptw"
    cfg = PersonalTidewayConfig.resolve(home=home)
    init_workspace(cfg)

    target = tmp_path / "untouched_target"
    target.mkdir()
    source_file = target / "app.py"
    source_file.write_text("print('hello')", encoding="utf-8")
    before_files = sorted(p.name for p in target.iterdir())
    before_mtime = source_file.stat().st_mtime_ns

    monkeypatch.setattr(
        "personal_tideway.cli.main.probe_git",
        lambda _p: GitProbeResult(is_git=False, status="not_git"),
    )

    assert main(["--home", str(home), "project", "add", str(target)]) == ExitCode.SUCCESS
    capsys.readouterr()

    after_files = sorted(p.name for p in target.iterdir())
    assert before_files == after_files
    assert source_file.stat().st_mtime_ns == before_mtime
    assert not (target / ".personal-tideway.yaml").exists()
    assert not (target / ".ptw").exists()
