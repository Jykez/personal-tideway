"""Pytest fixtures and test environment setup for Personal Tideway."""

import os
from pathlib import Path
import pytest

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.core.workspace import init_workspace


@pytest.fixture(autouse=True)
def prevent_real_client_probes(monkeypatch: pytest.MonkeyPatch):
    """Keep the test suite from resolving or executing real Codex or AGY installations from the host."""
    monkeypatch.setattr("personal_tideway.core.discovery.shutil.which", lambda _name: None)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    def guarded_runner(cmd: list[str], timeout: float) -> tuple[int, str, str]:
        raise AssertionError(f"Security violation: unexpected host process execution in test: {cmd}")

    monkeypatch.setattr("personal_tideway.core.discovery._default_version_runner", guarded_runner)


@pytest.fixture
def isolated_dirs(tmp_path: Path):
    """Provide isolated paths for PERSONAL_TIDEWAY_HOME, CODEX_HOME, and GEMINI_HOME."""
    ptw_home = tmp_path / "ptw_home"
    codex_home = tmp_path / "codex_home"
    gemini_home = tmp_path / "gemini_home"

    ptw_home.mkdir(parents=True, exist_ok=True)
    codex_home.mkdir(parents=True, exist_ok=True)
    gemini_home.mkdir(parents=True, exist_ok=True)

    return ptw_home, codex_home, gemini_home


@pytest.fixture
def personal_tideway_config(isolated_dirs):
    """Provide a configured and initialized PersonalTidewayConfig pointing to isolated temp dirs."""
    ptw_home, codex_home, gemini_home = isolated_dirs

    # Clean environment variables to ensure test isolation
    for env_k in ["PERSONAL_TIDEWAY_HOME", "CODEX_HOME", "GEMINI_HOME", "AGY_HOME", "PERSONAL_TIDEWAY_PROJECT"]:
        if env_k in os.environ:
            del os.environ[env_k]

    cfg = PersonalTidewayConfig.resolve(
        home=ptw_home,
        codex_home=codex_home,
        gemini_home=gemini_home,
    )
    init_workspace(cfg)
    return cfg
