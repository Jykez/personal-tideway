"""Tests for the single-source package and CLI version."""

import pytest

from personal_tideway import __version__
from personal_tideway.cli.main import build_parser


def test_development_version() -> None:
    assert __version__ == "0.1.0.dev4"


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"ptw {__version__}"
