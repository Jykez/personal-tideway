"""Migration 5C public rollback CLI and sanitized end-to-end acceptance tests."""

import json
from pathlib import Path

import yaml

from personal_tideway.cli.main import main
from personal_tideway.constants import (
    DEFAULT_CONFIG_YAML,
    RULE_MARKER_END,
    RULE_MARKER_START,
    ExitCode,
)
from personal_tideway.core.migration_apply import apply_migration


def _make_v1_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    home = tmp_path / "ptw"
    codex_home = tmp_path / "codex"
    gemini_home = tmp_path / "gemini"
    source_repo = tmp_path / "source-project"
    for path in (home, codex_home, gemini_home, source_repo):
        path.mkdir()
    (source_repo / ".git").mkdir()
    (source_repo / "README.md").write_text("fixture repository\n", encoding="utf-8")

    (home / DEFAULT_CONFIG_YAML).write_text(
        yaml.safe_dump({"version": 1, "client_paths": {}, "fixture": "migration-5c"}),
        encoding="utf-8",
    )
    (home / DEFAULT_CONFIG_YAML).chmod(0o640)
    (home / "memory").mkdir()
    (home / "memory" / "decision.md").write_text("# Sanitized legacy decision\n", encoding="utf-8")

    (gemini_home / "GEMINI.md").write_text(
        f"Unmanaged prefix\n{RULE_MARKER_START}\nManaged fixture rule\n{RULE_MARKER_END}\nUnmanaged suffix\n",
        encoding="utf-8",
    )
    (gemini_home / "GEMINI.md").chmod(0o600)
    skill = gemini_home / "skills" / "fixture-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# fixture-skill\n\nPersonal Tideway managed skill.\n", encoding="utf-8")
    return home, codex_home, gemini_home, source_repo


def test_public_rollback_dry_run_accepts_apply_manifest_path(tmp_path: Path, capsys) -> None:
    home, codex_home, gemini_home, _ = _make_v1_fixture(tmp_path)
    applied = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    manifest = home / applied.rollback_evidence["manifest_path"]
    before = manifest.read_bytes()
    new_memory = home / "memory" / "after-apply.md"
    new_memory.parent.mkdir(parents=True)
    new_memory.write_text("# Created after apply\n", encoding="utf-8")
    colliding_memory = home / "memory" / "decision.md"
    colliding_memory.write_text("# New decision with legacy path\n", encoding="utf-8")

    code = main([
        "--home", str(home),
        "--codex-home", str(codex_home),
        "--gemini-home", str(gemini_home),
        "migrate", "rollback", applied.rollback_evidence["manifest_path"], "--dry-run", "--json",
    ])

    assert code == ExitCode.SUCCESS
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ready"
    assert report["dry_run"] is True
    assert report["manifest_path"] == applied.rollback_evidence["manifest_path"]
    assert "memory/after-apply.md" in report["preserved_memory"]
    assert "memory/decision.md.post_migration" in report["preserved_memory"]
    assert manifest.read_bytes() == before
    assert new_memory.read_text(encoding="utf-8") == "# Created after apply\n"
    assert colliding_memory.read_text(encoding="utf-8") == "# New decision with legacy path\n"
    assert yaml.safe_load((home / DEFAULT_CONFIG_YAML).read_text(encoding="utf-8"))["version"] == 2


def test_public_rollback_accepts_bundle_id_and_is_idempotent(tmp_path: Path, capsys) -> None:
    home, codex_home, gemini_home, _ = _make_v1_fixture(tmp_path)
    original = (home / DEFAULT_CONFIG_YAML).read_bytes()
    applied = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)

    argv = [
        "--home", str(home),
        "--codex-home", str(codex_home),
        "--gemini-home", str(gemini_home),
        "migrate", "rollback", applied.bundle_id, "--json",
    ]
    assert main(argv) == ExitCode.SUCCESS
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "rolled_back"
    assert first["dry_run"] is False
    assert (home / DEFAULT_CONFIG_YAML).read_bytes() == original

    assert main(argv) == ExitCode.SUCCESS
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "rolled_back"


def test_sanitized_v1_apply_and_cli_rollback_end_to_end(tmp_path: Path, capsys) -> None:
    home, codex_home, gemini_home, source_repo = _make_v1_fixture(tmp_path)
    tracked = {
        "config": ((home / DEFAULT_CONFIG_YAML).read_bytes(), (home / DEFAULT_CONFIG_YAML).stat().st_mode & 0o777),
        "rules": ((gemini_home / "GEMINI.md").read_bytes(), (gemini_home / "GEMINI.md").stat().st_mode & 0o777),
        "memory": (home / "memory" / "decision.md").read_bytes(),
        "repo": sorted((str(path.relative_to(source_repo)), path.read_bytes() if path.is_file() else None) for path in source_repo.rglob("*")),
    }

    applied = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert applied.status == "applied"
    assert yaml.safe_load((home / DEFAULT_CONFIG_YAML).read_text(encoding="utf-8"))["version"] == 2
    assert (gemini_home / "config" / "GEMINI.md").is_file()
    assert (gemini_home / "config" / "skills" / "fixture-skill" / "SKILL.md").is_file()
    assert (home / "archive" / "legacy_memory" / "decision.md").is_file()

    post_migration = home / "memory" / "new-decision.md"
    post_migration.parent.mkdir(parents=True)
    post_migration.write_text("# Preserve after rollback\n", encoding="utf-8")

    code = main([
        "--home", str(home),
        "--codex-home", str(codex_home),
        "--gemini-home", str(gemini_home),
        "migrate", "rollback", applied.rollback_evidence["manifest_path"],
    ])
    assert code == ExitCode.SUCCESS
    output = capsys.readouterr().out
    assert "Status: rolled_back" in output
    assert "Dry Run: false" in output
    assert "memory/new-decision.md" in output

    assert (home / DEFAULT_CONFIG_YAML).read_bytes() == tracked["config"][0]
    assert (home / DEFAULT_CONFIG_YAML).stat().st_mode & 0o777 == tracked["config"][1]
    assert (gemini_home / "GEMINI.md").read_bytes() == tracked["rules"][0]
    assert (gemini_home / "GEMINI.md").stat().st_mode & 0o777 == tracked["rules"][1]
    assert (home / "memory" / "decision.md").read_bytes() == tracked["memory"]
    assert post_migration.read_text(encoding="utf-8") == "# Preserve after rollback\n"
    assert not (gemini_home / "config" / "GEMINI.md").exists()
    assert not (gemini_home / "config" / "skills" / "fixture-skill").exists()
    assert sorted((str(path.relative_to(source_repo)), path.read_bytes() if path.is_file() else None) for path in source_repo.rglob("*")) == tracked["repo"]


def test_public_rollback_error_is_sanitized(tmp_path: Path, capsys) -> None:
    home, codex_home, gemini_home, _ = _make_v1_fixture(tmp_path)
    outside = tmp_path / "private" / "manifest.json"

    code = main([
        "--home", str(home),
        "--codex-home", str(codex_home),
        "--gemini-home", str(gemini_home),
        "migrate", "rollback", str(outside), "--json",
    ])

    assert code == ExitCode.CONFIG_ERROR
    captured = capsys.readouterr()
    assert str(tmp_path) not in captured.out
    assert str(tmp_path) not in captured.err
    assert "exit_code" in captured.out


def test_public_rollback_does_not_echo_untrusted_manifest_paths(tmp_path: Path, capsys) -> None:
    home, codex_home, gemini_home, _ = _make_v1_fixture(tmp_path)
    applied = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    manifest_path = home / applied.rollback_evidence["manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    private_sentinel = "/private/fixture/credential-location"
    manifest["entries"][0]["rel_path"] = private_sentinel
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    code = main([
        "--home", str(home),
        "--codex-home", str(codex_home),
        "--gemini-home", str(gemini_home),
        "migrate", "rollback", applied.bundle_id, "--json",
    ])

    assert code == ExitCode.CONFIG_ERROR
    captured = capsys.readouterr()
    assert private_sentinel not in captured.out
    assert private_sentinel not in captured.err
    assert "Unsafe path in manifest entry" in captured.out


def test_already_rolled_back_manifest_still_validates_entries(tmp_path: Path, capsys) -> None:
    home, codex_home, gemini_home, _ = _make_v1_fixture(tmp_path)
    applied = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert main([
        "--home", str(home),
        "--codex-home", str(codex_home),
        "--gemini-home", str(gemini_home),
        "migrate", "rollback", applied.bundle_id, "--json",
    ]) == ExitCode.SUCCESS
    capsys.readouterr()

    manifest_path = home / applied.rollback_evidence["manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0]["symbolic_id"] = None
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    code = main([
        "--home", str(home),
        "--codex-home", str(codex_home),
        "--gemini-home", str(gemini_home),
        "migrate", "rollback", applied.bundle_id, "--json",
    ])
    assert code == ExitCode.CONFIG_ERROR
    captured = capsys.readouterr()
    assert "Invalid symbolic_id in manifest entry" in captured.out
    assert "unexpected" not in captured.err.lower()


def test_already_rolled_back_manifest_validates_destination_report(tmp_path: Path, capsys) -> None:
    home, codex_home, gemini_home, _ = _make_v1_fixture(tmp_path)
    applied = apply_migration(home=home, codex_home=codex_home, gemini_home=gemini_home)
    assert main([
        "--home", str(home),
        "--codex-home", str(codex_home),
        "--gemini-home", str(gemini_home),
        "migrate", "rollback", applied.bundle_id, "--json",
    ]) == ExitCode.SUCCESS
    capsys.readouterr()

    manifest_path = home / applied.rollback_evidence["manifest_path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["destinations_created"] = ["registry", {"not": "a path"}]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    code = main([
        "--home", str(home),
        "--codex-home", str(codex_home),
        "--gemini-home", str(gemini_home),
        "migrate", "rollback", applied.bundle_id, "--json",
    ])
    assert code == ExitCode.CONFIG_ERROR
    captured = capsys.readouterr()
    assert "Malformed destinations_created in manifest" in captured.out
