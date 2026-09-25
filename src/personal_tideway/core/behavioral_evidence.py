"""Bounded, fail-closed storage for candidate continuity probe evidence.

The seal detects accidental changes to local records. It is not a client
attestation: only a native-client verifier can establish marker delivery and
trust review, so these records alone never raise continuity assurance.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import CLIENT_AGY, CLIENT_CODEX

MAX_AGE = timedelta(days=30)
MAX_RECORD_BYTES = 4096
MAX_CONFIG_BYTES = 1024 * 1024
SCHEMA = 1


def _paths(cfg: PersonalTidewayConfig, client: str) -> tuple[Path, Path, Path]:
    if client == CLIENT_CODEX:
        return cfg.codex_hooks, cfg.codex_config, cfg.codex_home
    if client == CLIENT_AGY:
        return cfg.agy_hooks, cfg.agy_config, cfg.gemini_home
    raise ValueError("Unsupported client")


def _directory_fd(path: Path) -> int:
    """Open every directory component without following links."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe evidence directory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except (OSError, ValueError) as exc:
        os.close(fd)
        raise ValueError("Unsafe evidence directory") from exc


def _regular_bytes(path: Path, root: Path, limit: int, *, private: bool = False) -> bytes:
    """Read through anchored descriptors and validate the opened inode."""
    try:
        relative = path.relative_to(root)
        if len(relative.parts) < 1 or ".." in relative.parts:
            raise ValueError("Unsafe evidence path")
        parent_fd = _directory_fd(path.parent)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                    raise ValueError("Unsafe evidence file")
                if private and (metadata.st_uid != os.getuid() or metadata.st_mode & 0o077):
                    raise ValueError("Unsafe evidence permissions")
                chunks = bytearray()
                while len(chunks) <= limit:
                    piece = os.read(fd, min(65536, limit + 1 - len(chunks)))
                    if not piece:
                        break
                    chunks.extend(piece)
                if len(chunks) > limit:
                    raise ValueError("Oversized evidence file")
                return bytes(chunks)
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)
    except (OSError, ValueError) as exc:
        raise ValueError("Evidence file unavailable or unsafe") from exc


def _evidence_dir_fd(cfg: PersonalTidewayConfig, *, create: bool) -> int:
    state_fd = _directory_fd(cfg.state_dir)
    try:
        try:
            if create:
                try:
                    os.mkdir("behavioral-evidence", 0o700, dir_fd=state_fd)
                except FileExistsError:
                    pass
            fd = os.open(
                "behavioral-evidence", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=state_fd,
            )
            try:
                metadata = os.fstat(fd)
                if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                    raise ValueError("Unsafe evidence directory")
                return fd
            except (OSError, ValueError):
                os.close(fd)
                raise
        except OSError as exc:
            raise ValueError("Unsafe evidence directory") from exc
    finally:
        os.close(state_fd)


def _atomic_private_write(dir_fd: int, name: str, content: bytes) -> None:
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=dir_fd)
        except FileNotFoundError:
            pass


def configuration_fingerprint(cfg: PersonalTidewayConfig, client: str) -> str:
    """Bind a probe to both exact hook bytes and its current runtime config."""
    hooks, config, root = _paths(cfg, client)
    digest = hashlib.sha256()
    for label, path in ((b"hooks", hooks), (b"config", config)):
        content = _regular_bytes(path, root, MAX_CONFIG_BYTES)
        digest.update(label + b"\0" + len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def _record_path(cfg: PersonalTidewayConfig, client: str) -> Path:
    _paths(cfg, client)
    return cfg.state_dir / "behavioral-evidence" / f"{client}.json"


def _seal_path(cfg: PersonalTidewayConfig) -> Path:
    return cfg.state_dir / "behavioral-evidence" / "seal.key"


def _payload_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _key(cfg: PersonalTidewayConfig, *, create: bool) -> bytes:
    path = _seal_path(cfg)
    if create:
        directory_fd = _evidence_dir_fd(cfg, create=True)
        try:
            try:
                fd = os.open("seal.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(os.urandom(32))
                    stream.flush()
                    os.fsync(stream.fileno())
        finally:
            os.close(directory_fd)
    data = _regular_bytes(path, cfg.home, 32, private=True)
    if len(data) != 32:
        raise ValueError("Unsafe evidence seal")
    return data


def store_candidate_evidence(
    cfg: PersonalTidewayConfig,
    client: str,
    *,
    checked_at: datetime,
    marker_sha256: str,
    client_trace_sha256: str,
    trust_trace_sha256: str,
) -> Path:
    """Store bounded digests of a probe; no raw marker or transcript is retained.

    This is an internal candidate-record API, not a proof-issuing API. Callers
    must not interpret a successful write as verified native behavior.
    """
    now = datetime.now(UTC)
    if checked_at.tzinfo is None or not now - MAX_AGE <= checked_at <= now + timedelta(minutes=5):
        raise ValueError("Invalid evidence time")
    for value in (marker_sha256, client_trace_sha256, trust_trace_sha256):
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("Invalid evidence digest")
    payload = {
        "schema": SCHEMA,
        "client": client,
        "checked_at": checked_at.astimezone(UTC).isoformat(),
        "configuration_sha256": configuration_fingerprint(cfg, client),
        "marker_sha256": marker_sha256,
        "client_trace_sha256": client_trace_sha256,
        "trust_trace_sha256": trust_trace_sha256,
    }
    seal = hmac.new(_key(cfg, create=True), _payload_bytes(payload), hashlib.sha256).hexdigest()
    directory_fd = _evidence_dir_fd(cfg, create=False)
    try:
        _atomic_private_write(directory_fd, f"{client}.json", _payload_bytes({**payload, "seal": seal}) + b"\n")
    finally:
        os.close(directory_fd)
    return _record_path(cfg, client)


def load_candidate_evidence(
    cfg: PersonalTidewayConfig, client: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Return an intact, current record, or fail closed without leaking its data."""
    try:
        path = _record_path(cfg, client)
        raw = _regular_bytes(path, cfg.home, MAX_RECORD_BYTES, private=True)
        record = json.loads(raw)
        if not isinstance(record, dict) or set(record) != {
            "schema", "client", "checked_at", "configuration_sha256", "marker_sha256",
            "client_trace_sha256", "trust_trace_sha256", "seal",
        }:
            return None
        seal = record.pop("seal")
        if not isinstance(seal, str) or not hmac.compare_digest(
            seal, hmac.new(_key(cfg, create=False), _payload_bytes(record), hashlib.sha256).hexdigest()
        ):
            return None
        checked_at = datetime.fromisoformat(record["checked_at"])
        current = now or datetime.now(UTC)
        if (
            record["schema"] != SCHEMA or record["client"] != client
            or checked_at.tzinfo is None or current.tzinfo is None
            or not current - MAX_AGE <= checked_at <= current + timedelta(minutes=5)
            or record["configuration_sha256"] != configuration_fingerprint(cfg, client)
        ):
            return None
        return record
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None
