"""Exception classes for Personal Tideway with mapped exit codes."""

from personal_tideway.constants import ExitCode


class PersonalTidewayError(Exception):
    """Base exception for all Personal Tideway errors."""
    exit_code: ExitCode = ExitCode.VALIDATION_ERROR

    def __init__(self, message: str, exit_code: ExitCode | None = None):
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class ValidationError(PersonalTidewayError):
    """Raised when data or arguments fail validation."""
    exit_code = ExitCode.VALIDATION_ERROR


class BoundaryError(ValidationError):
    """Raised when a path escapes or violates Personal Tideway workspace boundaries."""


class ConfigError(PersonalTidewayError):
    """Raised when configuration is missing or invalid."""
    exit_code = ExitCode.CONFIG_ERROR


class ConflictError(PersonalTidewayError):
    """Raised when synchronization encounters unresolved conflicts."""
    exit_code = ExitCode.CONFLICT


class RuntimeProbeError(PersonalTidewayError):
    """Raised when runtime probing or execution fails."""
    exit_code = ExitCode.RUNTIME_PROBE_ERROR


class SecretsError(PersonalTidewayError):
    """Raised when secret parsing or variable interpolation fails."""
    exit_code = ExitCode.VALIDATION_ERROR
