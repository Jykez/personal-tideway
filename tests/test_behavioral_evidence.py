"""Candidate evidence is bounded and cannot by itself promote assurance."""

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import CLIENT_AGY, CLIENT_CODEX
from personal_tideway.core import assurance
from personal_tideway.core.behavioral_evidence import (
    configuration_fingerprint,
    load_candidate_evidence,
    store_candidate_evidence,
)


def _setup(tmp_path: Path) -> PersonalTidewayConfig:
    cfg = PersonalTidewayConfig.resolve(
        home=tmp_path / "ptw", codex_home=tmp_path / "codex", gemini_home=tmp_path / "gemini"
    )
    cfg.state_dir.mkdir(parents=True)
    cfg.codex_home.mkdir()
    cfg.codex_hooks.write_text('{"hooks":{}}')
    cfg.codex_config.write_text("[features]\nhooks = true\n")
    cfg.agy_hooks.parent.mkdir(parents=True)
    cfg.agy_hooks.write_text('{"personal-tideway":{}}')
    cfg.agy_config.parent.mkdir(parents=True, exist_ok=True)
    cfg.agy_config.write_text("{}")
    return cfg


def _store(cfg: PersonalTidewayConfig, client: str, when: datetime | None = None) -> Path:
    return store_candidate_evidence(
        cfg, client, checked_at=when or datetime.now(UTC),
        marker_sha256="a" * 64, client_trace_sha256="b" * 64,
        trust_trace_sha256="c" * 64,
    )


def test_current_candidate_is_client_bound_and_contains_no_raw_marker(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    path = _store(cfg, CLIENT_CODEX)
    record = load_candidate_evidence(cfg, CLIENT_CODEX)
    assert record is not None
    assert record["configuration_sha256"] == configuration_fingerprint(cfg, CLIENT_CODEX)
    assert load_candidate_evidence(cfg, CLIENT_AGY) is None
    assert path.stat().st_mode & 0o077 == 0
    assert b"PTW_" not in path.read_bytes()


@pytest.mark.parametrize("mutation", ["config", "hook", "seal", "payload", "schema", "client"])
def test_changed_or_tampered_candidate_fails_closed(tmp_path: Path, mutation: str) -> None:
    cfg = _setup(tmp_path)
    path = _store(cfg, CLIENT_CODEX)
    if mutation == "config":
        cfg.codex_config.write_text("[features]\nhooks = false\n")
    elif mutation == "hook":
        cfg.codex_hooks.write_text('{"hooks":{"changed":true}}')
    else:
        record = json.loads(path.read_text())
        field = {"seal": "seal", "payload": "marker_sha256", "schema": "schema", "client": "client"}[mutation]
        record[field] = "0" * 64 if field not in ("schema", "client") else "agy"
        path.write_text(json.dumps(record))
    assert load_candidate_evidence(cfg, CLIENT_CODEX) is None


def test_expired_future_corrupt_and_symlink_records_fail_closed(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    checked = datetime.now(UTC)
    path = _store(cfg, CLIENT_CODEX, checked)
    assert load_candidate_evidence(cfg, CLIENT_CODEX, now=checked + timedelta(days=31)) is None
    assert load_candidate_evidence(cfg, CLIENT_CODEX, now=checked - timedelta(minutes=6)) is None
    path.write_bytes(b"{")
    assert load_candidate_evidence(cfg, CLIENT_CODEX) is None
    path.unlink()
    path.symlink_to(cfg.codex_config)
    assert load_candidate_evidence(cfg, CLIENT_CODEX) is None


def test_untrusted_inputs_and_linked_config_are_rejected(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    with pytest.raises(ValueError):
        _store(cfg, "unknown")
    with pytest.raises(ValueError):
        _store(cfg, CLIENT_CODEX, datetime.now(UTC) - timedelta(days=31))
    cfg.codex_hooks.unlink()
    cfg.codex_hooks.symlink_to(cfg.codex_config)
    with pytest.raises(ValueError):
        _store(cfg, CLIENT_CODEX)


def test_current_candidate_cannot_promote_assurance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _setup(tmp_path)
    _store(cfg, CLIENT_CODEX)
    monkeypatch.setattr(assurance, "is_backend_usable", lambda _cfg: (True, "available"))
    monkeypatch.setattr(assurance, "check_client_continuity_rule", lambda _cfg, _client: True)
    monkeypatch.setattr(assurance, "check_client_continuity_skill", lambda _cfg, _client: True)
    result = assurance.evaluate_client_assurance(cfg, CLIENT_CODEX)
    assert result.level.value == "instructed"
    assert result.hook_verified is False
    assert "candidate probe record" in result.details


def test_private_permissions_and_linked_evidence_directory_fail_closed(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    path = _store(cfg, CLIENT_CODEX)
    path.chmod(0o644)
    assert load_candidate_evidence(cfg, CLIENT_CODEX) is None
    path.chmod(0o600)
    key = path.parent / "seal.key"
    key.chmod(0o644)
    assert load_candidate_evidence(cfg, CLIENT_CODEX) is None

    other = _setup(tmp_path / "other")
    external = tmp_path / "external"
    external.mkdir()
    (other.state_dir / "behavioral-evidence").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError):
        _store(other, CLIENT_CODEX)
    assert list(external.iterdir()) == []


def test_existing_record_symlink_is_replaced_without_following_target(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    path = _store(cfg, CLIENT_CODEX)
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged")
    path.unlink()
    path.symlink_to(outside)
    _store(cfg, CLIENT_CODEX)
    assert outside.read_text() == "unchanged"
    assert not path.is_symlink()


def test_aware_offset_is_normalized_and_naive_time_is_rejected(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    msk = timezone(timedelta(hours=3))
    _store(cfg, CLIENT_CODEX, datetime.now(msk))
    record = load_candidate_evidence(cfg, CLIENT_CODEX)
    assert record is not None
    assert record["checked_at"].endswith("+00:00")
    with pytest.raises(ValueError):
        _store(cfg, CLIENT_CODEX, datetime.now(UTC).replace(tzinfo=None))
