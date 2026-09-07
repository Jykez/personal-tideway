"""Secrets parsing, placeholder interpolation, and redaction."""

import os
from pathlib import Path
import re
from typing import Mapping

from personal_tideway.exceptions import SecretsError


# Regex for valid environment variable name: letters, digits, underscore, not starting with digit
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Regex to match ${VAR_NAME} placeholders
PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def parse_secrets_env(content: str) -> dict[str, str]:
    """Parse strict KEY=VALUE lines without shell evaluation.

    Supports:
    - Empty lines and lines starting with '#' are ignored.
    - Format: KEY=VALUE, KEY="VALUE", KEY='VALUE'.
    - Trailing whitespace after closing quote or value is stripped.
    - Invalid syntax raises SecretsError.
    """
    secrets: dict[str, str] = {}
    for line_num, line in enumerate(content.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            raise SecretsError(f"Line {line_num}: Invalid secret line (missing '='): {line}")

        key, raw_val = line.split("=", 1)
        key = key.strip()
        raw_val = raw_val.strip()

        if not ENV_KEY_RE.match(key):
            raise SecretsError(f"Line {line_num}: Invalid secret key name '{key}'")

        # Strip surrounding quotes if properly matched
        if len(raw_val) >= 2:
            if (raw_val.startswith('"') and raw_val.endswith('"')) or (
                raw_val.startswith("'") and raw_val.endswith("'")
            ):
                raw_val = raw_val[1:-1]

        secrets[key] = raw_val

    return secrets


def load_secrets(secrets_file: Path) -> dict[str, str]:
    """Load secrets dictionary from secrets.env file if present."""
    if not secrets_file.is_file():
        return {}
    try:
        content = secrets_file.read_text(encoding="utf-8")
        return parse_secrets_env(content)
    except Exception as e:
        if isinstance(e, SecretsError):
            raise
        raise SecretsError(f"Error reading secrets from {secrets_file}: {e}")


def interpolate_string(text: str, context: Mapping[str, str]) -> str:
    """Expand ${NAME} placeholders using context or os.environ.

    Fails with SecretsError if any variable is missing.
    """
    def _replace(match: re.Match) -> str:
        var_name = match.group(1)
        if var_name in context:
            return context[var_name]
        if var_name in os.environ:
            return os.environ[var_name]
        raise SecretsError(f"Missing required environment variable: '{var_name}'")

    return PLACEHOLDER_RE.sub(_replace, text)


def interpolate_list(items: list[str], context: Mapping[str, str]) -> list[str]:
    """Expand ${NAME} placeholders in a list of string arguments."""
    return [interpolate_string(item, context) for item in items]


def interpolate_dict(items: dict[str, str], context: Mapping[str, str]) -> dict[str, str]:
    """Expand ${NAME} placeholders in dictionary values."""
    return {k: interpolate_string(v, context) for k, v in items.items()}


def redact_secrets(text: str, secrets: list[str] | set[str] | None) -> str:
    """Redact secret values from output or error messages."""
    if not secrets or not text:
        return text

    # Filter out empty or whitespace-only values
    clean_secrets = {s for s in secrets if s and len(s.strip()) > 0}
    if not clean_secrets:
        return text

    # Sort descending by length to avoid partial replacements
    sorted_secrets = sorted(clean_secrets, key=len, reverse=True)
    redacted = text
    for secret in sorted_secrets:
        redacted = redacted.replace(secret, "[REDACTED]")

    return redacted
