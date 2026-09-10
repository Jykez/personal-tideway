"""Safe, testable execution of the Basic Memory 0.23.2 install plan.

Work Package 9b implementation for Personal Tideway v2:
- Immutable runner contract, safe subprocess execution, and bounded output capture.
- Minimal subprocess environment isolation excluding sensitive host variables.
- Strict boundary revalidation, mode 0700 directories, and atomic mode 0600 bootstrap config.
- Pre-existing config security policy: rejects symlinks, non-regular files, hardlinks, oversize, invalid schemas.
- Safe convergence execution flow: pre-health skip, single-shot install with --force, and post-health verification.
- Fixed secret-safe exceptions with suppressed chaining preventing information leaks.
"""

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from types import MappingProxyType
from typing import Any
import uuid

from personal_tideway.config import PersonalTidewayConfig, validate_owned_path
from personal_tideway.core.basic_memory_runtime import (
    BASIC_MEMORY_PINNED_VERSION,
    BasicMemoryBootstrapConfig,
    BasicMemoryInstallPlan,
    BasicMemoryInstallPlanPreview,
    BasicMemoryLayout,
    build_basic_memory_install_plan,
)
from personal_tideway.exceptions import BoundaryError, ConfigError, RuntimeProbeError

# Subprocess timeouts (seconds)
DEFAULT_INSTALL_TIMEOUT: float = 300.0
DEFAULT_HEALTH_TIMEOUT: float = 15.0

# Bounded output and file limits
MAX_CONFIG_SIZE_BYTES: int = 64 * 1024  # 64KiB
MAX_SUBPROCESS_OUTPUT_BYTES: int = 64 * 1024  # 64KiB

# Allowlisted host environment keys permitted in isolated child processes
ALLOWED_HOST_ENV_KEYS: frozenset[str] = frozenset({
    # PATH
    "PATH",
    # System essentials if present
    "SYSTEMROOT",
    # Locale
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LC_COLLATE",
    "LC_NUMERIC",
    "LC_TIME",
    "LC_MONETARY",
    # Certificates / SSL
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    # Proxies
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
})


def truncate_utf8_bytes(text: str, max_bytes: int) -> str:
    """Truncate text to at most max_bytes UTF-8 encoded size without breaking Unicode characters."""
    raw_bytes = text.encode("utf-8")
    if len(raw_bytes) <= max_bytes:
        return text
    return raw_bytes[:max_bytes].decode("utf-8", errors="ignore")


@dataclass(frozen=True)
class BasicMemoryRunnerResult:
    """Immutable result of a low-level command invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "stdout", truncate_utf8_bytes(str(self.stdout), MAX_SUBPROCESS_OUTPUT_BYTES))
        object.__setattr__(self, "stderr", truncate_utf8_bytes(str(self.stderr), MAX_SUBPROCESS_OUTPUT_BYTES))


BasicMemoryRunner = Callable[[tuple[str, ...], Mapping[str, str], float], Any]


@dataclass(frozen=True)
class BasicMemoryInstallResult:
    """Immutable result of Basic Memory install or health evaluation."""

    dry_run: bool
    config_created: bool
    install_attempted: bool
    already_healthy: bool
    healthy: bool
    version: str | None = None
    preview: BasicMemoryInstallPlanPreview | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert result to safe dictionary without exposing secrets or raw host environment."""
        return {
            "dry_run": self.dry_run,
            "config_created": self.config_created,
            "install_attempted": self.install_attempted,
            "already_healthy": self.already_healthy,
            "healthy": self.healthy,
            "version": self.version,
            "preview": self.preview.to_dict() if self.preview is not None else None,
        }


def default_subprocess_runner(
    argv: tuple[str, ...] | Sequence[str],
    env: Mapping[str, str],
    timeout: float = DEFAULT_INSTALL_TIMEOUT,
) -> BasicMemoryRunnerResult:
    """Default no-shell subprocess runner with fixed timeouts and bounded temporary file output capture."""
    try:
        with tempfile.TemporaryFile(mode="w+b") as stdout_f, tempfile.TemporaryFile(mode="w+b") as stderr_f:
            proc = subprocess.run(
                list(argv),
                env=dict(env),
                stdout=stdout_f,
                stderr=stderr_f,
                timeout=timeout,
                shell=False,
                check=False,
            )
            stdout_f.seek(0)
            stderr_f.seek(0)
            stdout_bytes = stdout_f.read(MAX_SUBPROCESS_OUTPUT_BYTES + 1)
            stderr_bytes = stderr_f.read(MAX_SUBPROCESS_OUTPUT_BYTES + 1)

            stdout_str = stdout_bytes[:MAX_SUBPROCESS_OUTPUT_BYTES].decode("utf-8", errors="ignore")
            stderr_str = stderr_bytes[:MAX_SUBPROCESS_OUTPUT_BYTES].decode("utf-8", errors="ignore")

            return BasicMemoryRunnerResult(
                returncode=proc.returncode,
                stdout=stdout_str,
                stderr=stderr_str,
            )
    except subprocess.TimeoutExpired:
        raise TimeoutError("Subprocess timed out during execution.") from None
    except TimeoutError:
        raise
    except Exception:
        raise RuntimeProbeError("Subprocess execution failed.") from None


def build_subprocess_env(
    env_overrides: Mapping[str, str],
    host_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Construct minimal subprocess environment from allowlisted host keys and controlled overrides.

    Explicitly excludes unrelated host variables such as tokens, API keys, virtualenv, and arbitrary UV flags.
    """
    source_env = os.environ if host_env is None else host_env
    env: dict[str, str] = {}
    for key in ALLOWED_HOST_ENV_KEYS:
        val = source_env.get(key)
        if val is not None:
            env[key] = str(val)

    # Apply controlled overrides defensively
    for k, v in env_overrides.items():
        env[str(k)] = str(v)

    return env


BASIC_MEMORY_VERSION_PATTERN = re.compile(
    r"(?i)\b(?:basic[-_ ]memory(?:\s+cli)?|bm)\b[,\s:-]*(?:version\b[,\s:-]*(?:v)?|v)?(\d+\.\d+\.\d+(?:[a-zA-Z0-9._-]+)?)\b"
)


def parse_basic_memory_version(stdout: str, stderr: str = "") -> str | None:
    """Safely extract Basic Memory version from combined stdout and stderr.

    Prefers a Basic Memory-labelled version across combined stdout+stderr, ignoring unrelated
    Python/uv versions. Accepts realistic variants such as 'Basic Memory version: 0.23.2',
    'basic-memory 0.23.2', 'basic-memory, version 0.23.2', 'Basic Memory CLI v0.23.2', 'bm 0.23.2'.
    Rejects standalone or unrelated versions.
    """
    combined = f"{stdout}\n{stderr}"
    if not combined.strip():
        return None
    bounded = truncate_utf8_bytes(combined, MAX_SUBPROCESS_OUTPUT_BYTES)
    match = BASIC_MEMORY_VERSION_PATTERN.search(bounded)
    if match:
        return match.group(1)
    return None


def validate_isolated_executable(
    executable_path: Path,
    service_root: Path,
    home_boundary: Path,
) -> bool:
    """Validate that executable_path is a safe regular executable or a safe launcher symlink inside service_root.

    Accepts regular executables and launcher symlinks whose fully resolved target remains inside service_root.
    Rejects outside or nested unsafe links with fixed errors.
    Returns True if valid. Raises RuntimeProbeError or BoundaryError on unsafe structure.
    Returns False if executable does not exist or internal launcher target is missing/unhealthy for self-healing.
    """
    try:
        canonical_service_root = service_root.resolve()
        canonical_home_boundary = home_boundary.resolve()
    except (OSError, RuntimeError):
        raise BoundaryError("Layout target component is outside workspace boundary.") from None

    if not (executable_path.is_relative_to(home_boundary) or executable_path.is_relative_to(canonical_home_boundary)):
        raise BoundaryError("Layout target component is outside workspace boundary.")

    if home_boundary.is_symlink():
        raise BoundaryError("Workspace root is a symlink, which is not permitted.")

    curr = executable_path.parent
    while curr != home_boundary and curr != canonical_home_boundary and curr != curr.parent:
        if curr.is_symlink():
            raise BoundaryError(
                "Layout target component is a symlink, which is not permitted in child layout."
            )
        curr = curr.parent

    try:
        st = os.lstat(executable_path)
    except FileNotFoundError:
        return False
    except OSError:
        raise RuntimeProbeError("Basic Memory executable missing or invalid.") from None

    if stat.S_ISLNK(st.st_mode):
        try:
            resolved_non_strict = executable_path.resolve(strict=False)
        except (OSError, RuntimeError):
            raise RuntimeProbeError("Basic Memory executable launcher resolution failed.") from None

        is_under_service = (
            resolved_non_strict.is_relative_to(canonical_service_root)
            or resolved_non_strict.is_relative_to(service_root)
        )
        is_under_home = (
            resolved_non_strict.is_relative_to(canonical_home_boundary)
            or resolved_non_strict.is_relative_to(home_boundary)
        )

        if not is_under_service:
            raise RuntimeProbeError("Basic Memory executable target is outside the service boundary.")
        if not is_under_home:
            raise RuntimeProbeError("Basic Memory executable target is outside workspace boundary.")

        try:
            resolved = executable_path.resolve(strict=True)
        except FileNotFoundError:
            return False
        except (OSError, RuntimeError):
            raise RuntimeProbeError("Basic Memory executable launcher resolution failed.") from None

        try:
            st_target = os.lstat(resolved)
        except FileNotFoundError:
            return False
        except OSError:
            raise RuntimeProbeError("Basic Memory executable target cannot be inspected.") from None

        if not stat.S_ISREG(st_target.st_mode):
            return False

        if not os.access(resolved, os.X_OK):
            return False

        return True

    if not stat.S_ISREG(st.st_mode):
        raise RuntimeProbeError("Basic Memory executable is not a regular file.")

    if not os.access(executable_path, os.X_OK):
        raise RuntimeProbeError("Basic Memory executable is not executable.")

    return True


def is_safe_isolated_executable(executable_path: Path, root_boundary: Path) -> bool:
    """Compatibility wrapper checking if executable_path is a safe regular or symlink executable."""
    try:
        return validate_isolated_executable(
            executable_path,
            service_root=executable_path.parent.parent,
            home_boundary=root_boundary,
        )
    except Exception:
        return False


def setup_isolated_directories(cfg: PersonalTidewayConfig, layout: BasicMemoryLayout) -> None:
    """Validate boundaries and reject symlinks, then create or normalize planned layout directories with mode 0700."""
    if cfg.home.is_symlink():
        raise BoundaryError("Workspace root is a symlink, which is not permitted.")

    planned_dirs = (
        cfg.services_dir,
        layout.service_root,
        layout.uv_tool_dir,
        layout.bin_dir,
        layout.config_dir,
        layout.cache_dir,
    )

    for target in planned_dirs:
        curr: Path = target
        while curr != cfg.home and curr != curr.parent:
            if curr.is_symlink():
                raise BoundaryError(
                    "Layout target component is a symlink, which is not permitted in child layout."
                )
            curr = curr.parent

    for target in planned_dirs:
        validate_owned_path(target, root=cfg.home, allow_root=False)

    for d in planned_dirs:
        try:
            st = os.lstat(d)
            exists = True
        except FileNotFoundError:
            exists = False
        except OSError:
            raise ConfigError("Failed to inspect layout directory.") from None

        if exists:
            if stat.S_ISLNK(st.st_mode):
                raise BoundaryError("Layout target component is a symlink, which is not permitted in child layout.")
            if not stat.S_ISDIR(st.st_mode):
                raise ConfigError("Existing layout path is not a directory.")
            current_mode = stat.S_IMODE(st.st_mode)
            if current_mode != 0o700:
                try:
                    os.chmod(d, 0o700)
                except OSError:
                    raise ConfigError("Failed to normalize permissions on layout directory.") from None
        else:
            try:
                d.mkdir(mode=0o700, parents=False, exist_ok=False)
                os.chmod(d, 0o700)
            except OSError:
                raise ConfigError("Failed to create isolated runtime directory.") from None


@contextmanager
def acquire_basic_memory_lock(cfg: PersonalTidewayConfig):
    """Serialize non-dry execution with a dedicated 0600 lock file under cfg.locks_dir using Linux fcntl.flock.

    Validates lock parent directory and path against symlink, non-regular, and hardlink attacks.
    Uses fixed safe errors without echoing paths.
    Ensures release on every exit.
    """
    locks_dir = cfg.locks_dir
    if not locks_dir.is_relative_to(cfg.home):
        raise BoundaryError("Workspace root boundary violation.")
    if cfg.home.is_symlink():
        raise BoundaryError("Workspace root is a symlink, which is not permitted.")

    curr = locks_dir
    while curr != cfg.home and curr != curr.parent:
        if curr.is_symlink():
            raise BoundaryError("Layout target component is a symlink, which is not permitted in child layout.")
        curr = curr.parent

    while True:
        try:
            st_dir = os.lstat(locks_dir)
            if stat.S_ISLNK(st_dir.st_mode):
                raise BoundaryError("Layout target component is a symlink, which is not permitted in child layout.")
            if not stat.S_ISDIR(st_dir.st_mode):
                raise ConfigError("Existing layout path is not a directory.")
            if stat.S_IMODE(st_dir.st_mode) != 0o700:
                try:
                    os.chmod(locks_dir, 0o700)
                except OSError:
                    raise ConfigError("Failed to normalize permissions on layout directory.") from None
            break
        except FileNotFoundError:
            try:
                locks_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
                try:
                    os.chmod(locks_dir, 0o700)
                except OSError:
                    pass
                break
            except FileExistsError:
                continue
            except OSError as err:
                if err.errno == errno.EEXIST:
                    continue
                raise ConfigError("Failed to create isolated runtime directory.") from None
        except OSError:
            raise ConfigError("Failed to inspect layout directory.") from None

    lock_file = locks_dir / "basic-memory-install.lock"

    open_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW

    try:
        lock_fd = os.open(lock_file, open_flags, 0o600)
    except OSError as err:
        if err.errno == getattr(errno, "ELOOP", 40):
            raise ConfigError("Lock file is a symlink, which is not permitted.") from None
        raise ConfigError("Failed to open lock file.") from None

    try:
        st = os.fstat(lock_fd)
        if stat.S_ISLNK(st.st_mode):
            raise ConfigError("Lock file is a symlink, which is not permitted.")
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError("Lock file is not a regular file.")
        if st.st_nlink != 1:
            raise ConfigError("Lock file has invalid link count.")

        try:
            os.fchmod(lock_fd, 0o600)
        except OSError:
            pass

        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except OSError:
            raise ConfigError("Failed to acquire lock.") from None

        try:
            st_post = os.fstat(lock_fd)
            try:
                st_disk = os.lstat(lock_file)
            except OSError:
                raise ConfigError("Lock file was replaced or removed while acquiring lock.") from None

            if (st_post.st_dev, st_post.st_ino) != (st_disk.st_dev, st_disk.st_ino) or st_post.st_nlink != 1:
                raise ConfigError("Lock file was replaced or removed while acquiring lock.")

            yield
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


def read_safe_basic_memory_config(
    config_file: Path,
    *,
    normalize_permissions: bool = False,
) -> dict[str, Any] | None:
    """Safely inspect and read an existing Basic Memory configuration file.

    Guarantees:
    - Safe descriptor handling without TOCTOU or FIFO blocking using O_NONBLOCK + O_NOFOLLOW.
    - Pure read-only by default (normalize_permissions=False performs zero filesystem mutations).
    - Rejects symlinks, non-regular files (FIFOs, sockets, dirs), hardlinks (st_nlink != 1), and oversize (> 64KiB).
    - Requires valid UTF-8 and valid JSON dictionary matching operational invariants.
    - Requires mandatory keys 'auto_update', 'default_project', 'projects'.
    - Enforces auto_update is False, default_project is None, and projects is a dictionary.
    - Normalizes file mode to 0600 on valid inspection ONLY when normalize_permissions=True.
    - Preserves upstream extra top-level keys.
    - Returns parsed dictionary if file exists and is valid, or None if file does not exist.
    - Raises fixed ConfigError without leaking paths or content on unsafe or invalid configuration.
    """
    open_flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW

    fd: int | None = None
    try:
        fd = os.open(config_file, open_flags)
    except FileNotFoundError:
        return None
    except OSError as err:
        if err.errno == getattr(errno, "ELOOP", 40):
            raise ConfigError("Existing config file is a symlink, which is not permitted.") from None
        raise ConfigError("Failed to inspect existing config file.") from None

    try:
        st = os.fstat(fd)

        # 1. Reject symlinks
        if stat.S_ISLNK(st.st_mode):
            raise ConfigError("Existing config file is a symlink, which is not permitted.")

        # 2. Reject non-regular files (FIFOs, sockets, directories, device nodes)
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError("Existing config file is not a regular file.")

        # 3. Reject multiple hard links (st_nlink != 1)
        if st.st_nlink != 1:
            raise ConfigError("Existing config file has invalid link count.")

        # 4. Reject oversized files (> 64KiB)
        if st.st_size > MAX_CONFIG_SIZE_BYTES:
            raise ConfigError("Existing config file exceeds size limit.")

        # 5. Read bounded bytes directly from fd (loop until EOF or limit)
        chunks: list[bytes] = []
        total_read = 0
        while total_read <= MAX_CONFIG_SIZE_BYTES:
            try:
                chunk = os.read(fd, (MAX_CONFIG_SIZE_BYTES + 1) - total_read)
            except BlockingIOError:
                break
            except OSError:
                raise ConfigError("Failed to read existing config file.") from None
            if not chunk:
                break
            chunks.append(chunk)
            total_read += len(chunk)

        raw_bytes = b"".join(chunks)
        if len(raw_bytes) > MAX_CONFIG_SIZE_BYTES:
            raise ConfigError("Existing config file exceeds size limit.")

        try:
            content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise ConfigError("Existing config file contains invalid UTF-8.") from None

        try:
            parsed = json.loads(content)
        except Exception:
            raise ConfigError("Existing config file contains invalid JSON syntax.") from None

        if not isinstance(parsed, dict):
            raise ConfigError("Existing config file does not match safe bootstrap configuration.")

        # 6. Operational config idempotency:
        # Require mandatory keys as a subset, accepting documented upstream extra settings
        mandatory_keys = {"auto_update", "default_project", "projects"}
        if not mandatory_keys.issubset(parsed.keys()):
            raise ConfigError("Existing config file does not match safe bootstrap configuration.")

        if parsed["auto_update"] is not False:
            raise ConfigError("Existing config file does not match safe bootstrap configuration.")

        if parsed["default_project"] is not None:
            raise ConfigError("Existing config file does not match safe bootstrap configuration.")

        projects_val = parsed["projects"]
        if not isinstance(projects_val, dict):
            raise ConfigError("Existing config file does not match safe bootstrap configuration.")

        # Normalize permissions on existing config file using open fd ONLY if explicitly requested
        if normalize_permissions:
            current_mode = stat.S_IMODE(st.st_mode)
            if current_mode != 0o600:
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    raise ConfigError("Failed to normalize permissions on existing config file.") from None

        return parsed
    finally:
        os.close(fd)


def ensure_safe_bootstrap_config(
    config_file: Path,
    bootstrap_config: BasicMemoryBootstrapConfig,
) -> bool:
    """Ensure safe bootstrap config exists, rejecting unsafe existing configurations without leaking paths or content.

    Safely reads existing configuration without TOCTOU or FIFO blocking using O_NONBLOCK + O_NOFOLLOW on fd.
    Accepts valid operational configs where auto_update is false, default_project is null, and projects is a dict.
    Returns True if a new config was created, False if an existing valid config was safely reused.
    """
    existing = read_safe_basic_memory_config(config_file, normalize_permissions=True)
    if existing is not None:
        return False

    # Create bootstrap config atomically with mode 0600
    config_dir = config_file.parent
    tmp_file = config_dir / f".tmp_config_{uuid.uuid4().hex}"
    content_str = bootstrap_config.to_json()
    content_bytes = content_str.encode("utf-8")

    try:
        tmp_fd = os.open(
            tmp_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            total_written = 0
            while total_written < len(content_bytes):
                written = os.write(tmp_fd, content_bytes[total_written:])
                if written <= 0:
                    raise ConfigError("Failed to create safe bootstrap config file.")
                total_written += written
            os.fsync(tmp_fd)
            try:
                os.fchmod(tmp_fd, 0o600)
            except OSError:
                pass
        finally:
            os.close(tmp_fd)

        # Atomic publication via hard link (POSIX O_EXCL semantics)
        try:
            os.link(tmp_file, config_file)
            published = True
        except FileExistsError:
            published = False
        finally:
            try:
                os.unlink(tmp_file)
            except OSError:
                pass

        if not published:
            return ensure_safe_bootstrap_config(config_file, bootstrap_config)

        try:
            os.chmod(config_file, 0o600)
        except OSError:
            pass

        return True
    except ConfigError:
        raise
    except Exception:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except OSError:
                pass
        raise ConfigError("Failed to create safe bootstrap config file.") from None


def _invoke_runner(
    runner: BasicMemoryRunner,
    argv: tuple[str, ...],
    env: Mapping[str, str],
    timeout: float,
) -> BasicMemoryRunnerResult:
    """Safely invoke runner callable exactly once with explicit timeout and return immutable BasicMemoryRunnerResult."""
    safe_env = MappingProxyType(dict(env))
    raw_res = runner(argv, safe_env, timeout)

    if isinstance(raw_res, BasicMemoryRunnerResult):
        return raw_res
    if isinstance(raw_res, tuple) and len(raw_res) >= 3:
        return BasicMemoryRunnerResult(
            returncode=int(raw_res[0]),
            stdout=str(raw_res[1]),
            stderr=str(raw_res[2]),
        )
    if hasattr(raw_res, "returncode"):
        return BasicMemoryRunnerResult(
            returncode=int(getattr(raw_res, "returncode")),
            stdout=str(getattr(raw_res, "stdout", "") or ""),
            stderr=str(getattr(raw_res, "stderr", "") or ""),
        )
    raise RuntimeProbeError("Runner returned an unexpected result type.")


def install_basic_memory(
    cfg: PersonalTidewayConfig,
    uv_executable: str | Path,
    *,
    dry_run: bool = False,
    runner: BasicMemoryRunner | None = None,
) -> BasicMemoryInstallResult:
    """Safe, idempotent executor for Basic Memory 0.23.2 install plan.

    Validates configuration and uv_executable internally to prevent forged path smuggling.
    Serializes non-dry execution with dedicated 0600 lock file under cfg.locks_dir.
    Performs boundary checks, sets up mode 0700 directories, ensures safe bootstrap config (mode 0600),
    checks pre-health to skip install when already converged, executes install with --force on divergence,
    verifies isolated binary and version 0.23.2, and raises fixed safe errors without secret leakage.
    """
    # 1. Build and validate install plan internally
    plan = build_basic_memory_install_plan(cfg, uv_executable=uv_executable)
    layout = plan.layout

    # 2. If dry_run: return immutable preview with zero filesystem mutation and zero runner calls
    if dry_run:
        return BasicMemoryInstallResult(
            dry_run=True,
            config_created=False,
            install_attempted=False,
            already_healthy=False,
            healthy=False,
            version=None,
            preview=plan.preview(),
        )

    # 3. Serialize non-dry execution with dedicated 0600 lock file under cfg.locks_dir
    with acquire_basic_memory_lock(cfg):
        # 4. Revalidate boundaries and create planned layout directories with mode 0700
        setup_isolated_directories(cfg, layout)

        # 5. Ensure safe bootstrap config at config/config.json with mode 0600
        config_created = ensure_safe_bootstrap_config(layout.config_file, plan.bootstrap_config)

        # 6. Build minimal subprocess environment from allowlist and plan overrides
        subprocess_env = build_subprocess_env(plan.env_overrides)

        # Resolve runner
        active_runner = runner if runner is not None else default_subprocess_runner

        # 7. Optional pre-health check if isolated primary executable exists safely
        if validate_isolated_executable(layout.primary_executable, layout.service_root, cfg.home):
            try:
                pre_health_res = _invoke_runner(
                    active_runner,
                    plan.health_argv,
                    subprocess_env,
                    timeout=DEFAULT_HEALTH_TIMEOUT,
                )
                if pre_health_res.returncode == 0:
                    parsed_ver = parse_basic_memory_version(pre_health_res.stdout, pre_health_res.stderr)
                    if parsed_ver == BASIC_MEMORY_PINNED_VERSION:
                        return BasicMemoryInstallResult(
                            dry_run=False,
                            config_created=config_created,
                            install_attempted=False,
                            already_healthy=True,
                            healthy=True,
                            version=parsed_ver,
                            preview=plan.preview(),
                        )
            except Exception:
                # Pre-health failures trigger convergence via install
                pass

        # 8. Execute install once with --force
        try:
            install_res = _invoke_runner(
                active_runner,
                plan.install_argv,
                subprocess_env,
                timeout=DEFAULT_INSTALL_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, TimeoutError):
            raise RuntimeProbeError("Basic Memory installation timed out.") from None
        except Exception:
            raise RuntimeProbeError("Basic Memory installation failed.") from None

        if install_res.returncode != 0:
            raise RuntimeProbeError("Basic Memory installation failed with non-zero exit code.") from None

        # 9. Require isolated executable
        if not validate_isolated_executable(layout.primary_executable, layout.service_root, cfg.home):
            raise RuntimeProbeError("Basic Memory executable missing or invalid after installation.") from None

        # 10. Health check once: require return code 0 and exact pinned version boundary
        try:
            health_res = _invoke_runner(
                active_runner,
                plan.health_argv,
                subprocess_env,
                timeout=DEFAULT_HEALTH_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, TimeoutError):
            raise RuntimeProbeError("Basic Memory health check timed out.") from None
        except Exception:
            raise RuntimeProbeError("Basic Memory health check failed.") from None

        if health_res.returncode != 0:
            raise RuntimeProbeError("Basic Memory health check failed with non-zero exit code.") from None

        parsed_post_ver = parse_basic_memory_version(health_res.stdout, health_res.stderr)
        if parsed_post_ver != BASIC_MEMORY_PINNED_VERSION:
            raise RuntimeProbeError("Basic Memory health check reported incorrect version.") from None

        return BasicMemoryInstallResult(
            dry_run=False,
            config_created=config_created,
            install_attempted=True,
            already_healthy=False,
            healthy=True,
            version=parsed_post_ver,
            preview=plan.preview(),
        )


# Canonical alias
execute_basic_memory_install_plan = install_basic_memory

__all__ = [
    "ALLOWED_HOST_ENV_KEYS",
    "BASIC_MEMORY_VERSION_PATTERN",
    "DEFAULT_HEALTH_TIMEOUT",
    "DEFAULT_INSTALL_TIMEOUT",
    "MAX_CONFIG_SIZE_BYTES",
    "MAX_SUBPROCESS_OUTPUT_BYTES",
    "BasicMemoryInstallResult",
    "BasicMemoryRunner",
    "BasicMemoryRunnerResult",
    "acquire_basic_memory_lock",
    "build_subprocess_env",
    "default_subprocess_runner",
    "ensure_safe_bootstrap_config",
    "execute_basic_memory_install_plan",
    "install_basic_memory",
    "is_safe_isolated_executable",
    "parse_basic_memory_version",
    "read_safe_basic_memory_config",
    "setup_isolated_directories",
    "truncate_utf8_bytes",
    "validate_isolated_executable",
]
