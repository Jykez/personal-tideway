"""Tests for curated memory management and project init with templates."""

from pathlib import Path
import pytest
import yaml

from personal_tideway.cli.main import main
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import ExitCode
from personal_tideway.core.memory import list_memories, search_memories
from personal_tideway.utils import atomic_write_text


def test_memory_add_list_search(personal_tideway_config: PersonalTidewayConfig):
    """Test adding curated memory, listing, and searching entries."""
    code_add = main([
        "--home", str(personal_tideway_config.home),
        "memory", "add", "Python Typing Guide",
        "--content", "Use PEP 484 and dataclasses for typed code.",
        "--tags", "python", "types",
    ])
    assert code_add == ExitCode.SUCCESS

    # List memories
    entries = list_memories(personal_tideway_config)
    assert len(entries) == 1
    assert entries[0].title == "Python Typing Guide"
    assert "python" in entries[0].tags

    # Search positive
    matches = search_memories(personal_tideway_config, "dataclasses")
    assert len(matches) == 1
    assert matches[0].id == entries[0].id

    # Search negative
    no_matches = search_memories(personal_tideway_config, "nonexistent-query-string")
    assert len(no_matches) == 0


def test_project_init_refuses_overwrite_without_force(personal_tideway_config: PersonalTidewayConfig, tmp_path: Path):
    """Test project init creates .personal-tideway.yaml and refuses overwrite unless forced."""
    proj_dir = tmp_path / "my_project"
    proj_dir.mkdir(parents=True)

    # First init succeeds
    code1 = main([
        "--home", str(personal_tideway_config.home),
        "project", "init", str(proj_dir),
    ])
    assert code1 == ExitCode.SUCCESS
    manifest = proj_dir / ".personal-tideway.yaml"
    assert manifest.is_file()

    # Second init without --force fails with validation error
    code2 = main([
        "--home", str(personal_tideway_config.home),
        "project", "init", str(proj_dir),
    ])
    assert code2 == ExitCode.VALIDATION_ERROR

    # Second init with --force succeeds
    code3 = main([
        "--home", str(personal_tideway_config.home),
        "project", "init", str(proj_dir), "--force",
    ])
    assert code3 == ExitCode.SUCCESS


def test_project_init_with_custom_template(personal_tideway_config: PersonalTidewayConfig, tmp_path: Path):
    """Test project init applying a named template from templates/ directory."""
    custom_tpl = personal_tideway_config.templates_dir / "microservice.yaml"
    tpl_content = """version: 1
project:
  name: "{{project_name}}"
  type: "microservice"
"""
    atomic_write_text(custom_tpl, tpl_content)

    proj_dir = tmp_path / "order_service"
    proj_dir.mkdir(parents=True)

    code = main([
        "--home", str(personal_tideway_config.home),
        "project", "init", str(proj_dir),
        "--template", "microservice",
    ])
    assert code == ExitCode.SUCCESS

    manifest_text = (proj_dir / ".personal-tideway.yaml").read_text(encoding="utf-8")
    assert "order_service" in manifest_text
    assert 'type: "microservice"' in manifest_text
