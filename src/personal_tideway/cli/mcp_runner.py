"""Secrets wrapper executable `ptw-mcp-run`."""

import os
from pathlib import Path
import sys
from typing import Sequence

from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import ExitCode
from personal_tideway.exceptions import SecretsError
from personal_tideway.secrets import interpolate_string, load_secrets, redact_secrets


def run_wrapper(argv: Sequence[str] | None = None) -> int:
    """Execute command with injected secrets and expanded placeholders."""
    args = list(sys.argv[1:] if argv is None else argv)

    if args in (["-h"], ["--help"]):
        print("Usage: ptw-mcp-run [--home PATH] [--] <command> [args...]")
        return ExitCode.SUCCESS

    # Optional flags before command
    home_override: str | None = None
    while args and args[0].startswith("--"):
        if args[0] == "--":
            args.pop(0)
            break
        elif args[0] == "--home":
            args.pop(0)
            if not args:
                sys.stderr.write("Error: --home requires a path argument\n")
                return ExitCode.VALIDATION_ERROR
            home_override = args.pop(0)
        elif args[0].startswith("--home="):
            home_override = args.pop(0).split("=", 1)[1]
        else:
            break

    if not args:
        sys.stderr.write("Usage: ptw-mcp-run [--home PATH] [--] <command> [args...]\n")
        return ExitCode.VALIDATION_ERROR

    cfg = PersonalTidewayConfig.resolve(home=home_override)
    secret_values: list[str] = []

    try:
        secrets = load_secrets(cfg.secrets_env)
        secret_values = list(secrets.values())

        # Prepare execution environment
        exec_env = os.environ.copy()
        for k, v in secrets.items():
            # Interpolate any cross-referenced secrets without shell
            exec_env[k] = interpolate_string(v, secrets)

        raw_cmd = args[0]
        raw_args = args[1:]

        # Expand ${VAR} placeholders in command and args
        expanded_cmd = interpolate_string(raw_cmd, secrets)
        expanded_args = [interpolate_string(arg, secrets) for arg in raw_args]

        full_cmd_args = [expanded_cmd] + expanded_args

        # Replace current process with target command
        os.execvpe(expanded_cmd, full_cmd_args, exec_env)

    except SecretsError as e:
        safe_msg = redact_secrets(str(e), secret_values)
        sys.stderr.write(f"Error: {safe_msg}\n")
        return ExitCode.VALIDATION_ERROR
    except FileNotFoundError as e:
        safe_msg = redact_secrets(f"Command not found: {e.filename}", secret_values)
        sys.stderr.write(f"Error: {safe_msg}\n")
        return ExitCode.RUNTIME_PROBE_ERROR
    except Exception as e:
        safe_msg = redact_secrets(f"Execution failed: {e}", secret_values)
        sys.stderr.write(f"Error: {safe_msg}\n")
        return ExitCode.RUNTIME_PROBE_ERROR

    return ExitCode.SUCCESS


def main() -> None:
    code = run_wrapper()
    sys.exit(code)


if __name__ == "__main__":
    main()
