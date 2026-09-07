"""Constants and exit codes for Personal Tideway."""

from enum import IntEnum


class ExitCode(IntEnum):
    """Documented exit codes for Personal Tideway CLI."""
    SUCCESS = 0
    VALIDATION_ERROR = 1
    CONFIG_ERROR = 2
    CONFLICT = 3
    RUNTIME_PROBE_ERROR = 4


SCHEMA_VERSION = 2

RULE_MARKER_START = "<!-- PERSONAL-TIDEWAY:START -->"
RULE_MARKER_END = "<!-- PERSONAL-TIDEWAY:END -->"

CLIENT_CODEX = "codex"
CLIENT_AGY = "agy"
SUPPORTED_CLIENTS = (CLIENT_CODEX, CLIENT_AGY)

TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"
SUPPORTED_TRANSPORTS = (TRANSPORT_STDIO, TRANSPORT_HTTP)

SKILL_LINK_SYMLINK = "symlink"
SKILL_LINK_COPY = "copy"
SUPPORTED_SKILL_LINK_MODES = (SKILL_LINK_SYMLINK, SKILL_LINK_COPY)

DEFAULT_PERSONAL_TIDEWAY_DIR = ".personal-tideway"
DEFAULT_CONFIG_YAML = "config.yaml"
DEFAULT_SECRETS_ENV = "secrets.env"  # pragma: allowlist secret
DEFAULT_STATE_FILE = "state.json"
