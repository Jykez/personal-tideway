"""Tests for secrets parsing without shell evaluation, placeholder interpolation, and redaction."""

from pathlib import Path
import pytest

from personal_tideway.cli.mcp_runner import run_wrapper
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import ExitCode
from personal_tideway.exceptions import SecretsError
from personal_tideway.secrets import (
    interpolate_string,
    load_secrets,
    parse_secrets_env,
    redact_secrets,
)
from personal_tideway.utils import atomic_write_text


def test_strict_parsing_no_shell_evaluation():
    """Verify secrets are parsed strictly as literals with no shell or subshell evaluation."""
    content = """# Comments are ignored
API_TOKEN=abc123xyz
SUB_SHELL=$(touch /tmp/evil_marker)
BACKTICKS=`whoami`
DOUBLE_QUOTED="hello world"
SINGLE_QUOTED='single quoted'
"""
    parsed = parse_secrets_env(content)
    assert parsed["API_TOKEN"] == "abc123xyz"
    assert parsed["SUB_SHELL"] == "$(touch /tmp/evil_marker)"
    assert parsed["BACKTICKS"] == "`whoami`"
    assert parsed["DOUBLE_QUOTED"] == "hello world"
    assert parsed["SINGLE_QUOTED"] == "single quoted"
    # Ensure no shell command was evaluated
    assert not Path("/tmp/evil_marker").exists()


def test_invalid_syntax_raises_error():
    """Verify malformed lines raise SecretsError."""
    with pytest.raises(SecretsError):
        parse_secrets_env("INVALID_LINE_WITHOUT_EQUALS")

    with pytest.raises(SecretsError):
        parse_secrets_env("123INVALID_KEY=val")


def test_missing_variable_fails_clearly():
    """Verify missing placeholders raise clear SecretsError."""
    context = {"EXISTING": "ok"}
    assert interpolate_string("value-${EXISTING}", context) == "value-ok"

    with pytest.raises(SecretsError, match="Missing required environment variable: 'MISSING_SECRET'"):
        interpolate_string("prefix-${MISSING_SECRET}", context)


def test_redact_secrets_masks_values():
    """Verify secret values are replaced with [REDACTED] in output and errors."""
    secrets = ["very_secret_token", "super_password", "token"]
    raw_text = "Connection failed using token very_secret_token and pwd super_password!"
    redacted = redact_secrets(raw_text, secrets)

    assert "very_secret_token" not in redacted
    assert "super_password" not in redacted
    assert redacted == "Connection failed using [REDACTED] [REDACTED] and pwd [REDACTED]!"


def test_mcp_run_wrapper_fails_on_missing_var_and_redacts(personal_tideway_config: PersonalTidewayConfig, capsys):
    """Verify ptw-mcp-run handles missing variables cleanly without exposing existing secrets."""
    atomic_write_text(
        personal_tideway_config.secrets_env,
        "REAL_SECRET=super_secret_value_xyz\n",
        mode=0o600,
    )

    code = run_wrapper([
        "--home", str(personal_tideway_config.home),
        "--",
        "echo", "${REAL_SECRET}", "${UNKNOWN_SECRET}",
    ])

    assert code == ExitCode.VALIDATION_ERROR
    captured = capsys.readouterr()
    assert "super_secret_value_xyz" not in captured.err
    assert "Missing required environment variable" in captured.err


def test_runner_help_does_not_execute_a_command(capsys):
    assert run_wrapper(["--help"]) == ExitCode.SUCCESS
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("Usage: ptw-mcp-run")
