"""Tests for MCP server schema validation, CLI commands, and test runner."""

from dataclasses import asdict
from pathlib import Path
import pytest

from personal_tideway.cli.main import build_parser, main, preprocess_cli_args
from personal_tideway.config import PersonalTidewayConfig
from personal_tideway.constants import ExitCode, TRANSPORT_HTTP, TRANSPORT_STDIO
from personal_tideway.core.mcp import load_mcp_server, save_mcp_server, test_mcp_server
from personal_tideway.exceptions import ValidationError
from personal_tideway.models import MCPServer
from personal_tideway.utils import atomic_write_text


def test_mcp_schema_mutually_exclusive_fields():
    """Verify stdio and http transport field constraints."""
    # Valid stdio
    s_stdio = MCPServer(name="good-stdio", transport=TRANSPORT_STDIO, command="ls", args=["-l"])
    s_stdio.validate()

    # Invalid stdio with url
    with pytest.raises(ValidationError, match="url' is forbidden"):
        s_bad_stdio = MCPServer(name="bad-stdio", transport=TRANSPORT_STDIO, command="ls", url="http://bad")
        s_bad_stdio.validate()

    # Valid http
    s_http = MCPServer(name="good-http", transport=TRANSPORT_HTTP, url="https://api.test/mcp")
    s_http.validate()

    # Invalid http with command
    with pytest.raises(ValidationError, match="command' is forbidden"):
        s_bad_http = MCPServer(name="bad-http", transport=TRANSPORT_HTTP, url="https://api.test", command="python")
        s_bad_http.validate()

    # Invalid http with args
    with pytest.raises(ValidationError, match="args' are forbidden"):
        s_bad_http_args = MCPServer(name="bad-http-args", transport=TRANSPORT_HTTP, url="https://api.test", args=["--flag"])
        s_bad_http_args.validate()


def test_mcp_cli_lifecycle(personal_tideway_config: PersonalTidewayConfig):
    """Test mcp add, list, disable, and enable lifecycle via CLI using repeatable --arg."""
    # Add stdio server using repeatable --arg
    code_add = main([
        "--home", str(personal_tideway_config.home),
        "mcp", "add", "my-server",
        "--transport", "stdio",
        "--command", "python",
        "--arg", "-m",
        "--arg", "server",
        "--env", "ENV_VAR=hello",
    ])
    assert code_add == ExitCode.SUCCESS
    srv_file = personal_tideway_config.mcp_dir / "my-server.yaml"
    assert srv_file.is_file()
    srv_added = load_mcp_server(srv_file)
    assert srv_added.args == ["-m", "server"]

    # Also test backward-compatibility with --args syntax
    code_add_compat = main([
        "--home", str(personal_tideway_config.home),
        "mcp", "add", "compat-server",
        "--transport", "stdio",
        "--command", "python",
        "--args", "-m", "compat",
    ])
    assert code_add_compat == ExitCode.SUCCESS
    srv_compat = load_mcp_server(personal_tideway_config.mcp_dir / "compat-server.yaml")
    assert srv_compat.args == ["-m", "compat"]

    # Disable
    code_dis = main(["--home", str(personal_tideway_config.home), "mcp", "disable", "my-server"])
    assert code_dis == ExitCode.SUCCESS
    srv_dis = load_mcp_server(personal_tideway_config.mcp_dir / "my-server.yaml")
    assert srv_dis.enabled is False

    # Enable
    code_en = main(["--home", str(personal_tideway_config.home), "mcp", "enable", "my-server"])
    assert code_en == ExitCode.SUCCESS
    srv_en = load_mcp_server(personal_tideway_config.mcp_dir / "my-server.yaml")
    assert srv_en.enabled is True


def test_test_mcp_server_not_collected_by_pytest():
    """Regression test ensuring test_mcp_server helper is not collected by pytest."""
    assert getattr(test_mcp_server, "__test__", None) is False


def test_mcp_test_static_validation_and_placeholder_checking(personal_tideway_config: PersonalTidewayConfig):
    """Test static validation of environment placeholders without printing secret values."""
    # Create server referencing ${SECRET_TOKEN}
    srv = MCPServer(
        name="placeholder-srv",
        command="sh",
        args=["-c", "echo ok"],
        env={"AUTH": "${SECRET_TOKEN}"},
    )
    save_mcp_server(personal_tideway_config.mcp_dir, srv)

    # Missing placeholder fails static check
    ok, msgs = test_mcp_server(srv, personal_tideway_config.secrets_env, static_only=True)
    assert not ok
    assert any("missing variables: SECRET_TOKEN" in m for m in msgs)

    # Provide secret in secrets.env
    atomic_write_text(personal_tideway_config.secrets_env, "SECRET_TOKEN=classified_super_secret\n", mode=0o600)

    # Static check passes and classified_super_secret is NEVER printed
    ok2, msgs2 = test_mcp_server(srv, personal_tideway_config.secrets_env, static_only=True)
    assert ok2
    full_output = " ".join(msgs2)
    assert "classified_super_secret" not in full_output
    assert "Schema validation: PASS" in full_output


def test_mcp_parser_exact_argv_and_direct_arg_regression(personal_tideway_config: PersonalTidewayConfig):
    """Focused regression test for parsing and preserving repeatable args and name."""
    # Exact argv specified in requirements
    raw_argv = [
        "--home", str(personal_tideway_config.home),
        "mcp", "add", "my-server",
        "--transport", "stdio",
        "--command", "python",
        "--arg", "-m",
        "--arg", "server",
        "--env", "ENV_VAR=hello",
    ]
    clean_argv = preprocess_cli_args(raw_argv)
    parser = build_parser()
    args = parser.parse_args(clean_argv)
    assert args.top_command == "mcp"
    assert args.mcp_command == "add"
    assert args.name == "my-server"
    assert args.transport == "stdio"
    assert args.server_command == "python"
    assert args.arg_items == ["-m", "server"]
    assert args.env == ["ENV_VAR=hello"]

    code = main(raw_argv)
    assert code == ExitCode.SUCCESS
    srv = load_mcp_server(personal_tideway_config.mcp_dir / "my-server.yaml")
    assert srv.args == ["-m", "server"]
    assert srv.env == {"ENV_VAR": "hello"}

    # Direct --arg=-m form test
    raw_direct = [
        "--home", str(personal_tideway_config.home),
        "mcp", "add", "direct-server",
        "--transport", "stdio",
        "--command", "python",
        "--arg=-m",
        "--arg", "server",
        "--env", "ENV_VAR=hello",
    ]
    code_direct = main(raw_direct)
    assert code_direct == ExitCode.SUCCESS
    srv_direct = load_mcp_server(personal_tideway_config.mcp_dir / "direct-server.yaml")
    assert srv_direct.args == ["-m", "server"]


def test_mcp_server_constructor_round_trip_with_env():
    """Verify MCPServer constructor accepts env, serializes, deserializes, and computes effective configs."""
    # 1. Constructor with env and primitive conversion
    srv = MCPServer(
        name="roundtrip-server",
        transport=TRANSPORT_STDIO,
        command="python",
        args=["-m", "srv"],
        env={"STRING_KEY": "val", "INT_KEY": 123},
        overrides={"codex": {"env": {"STRING_KEY": "override_val", "NEW_KEY": "extra"}}},
    )
    srv.validate()
    assert srv.env == {"STRING_KEY": "val", "INT_KEY": "123"}

    # 2. Dataclass asdict serialization includes env
    raw_asdict = asdict(srv)
    assert raw_asdict["env"] == {"STRING_KEY": "val", "INT_KEY": "123"}

    # 3. to_dict serialization includes env
    dumped = srv.to_dict()
    assert dumped["env"] == {"STRING_KEY": "val", "INT_KEY": "123"}

    # 4. from_dict deserialization restores env
    loaded = MCPServer.from_dict(dumped)
    assert loaded.env == {"STRING_KEY": "val", "INT_KEY": "123"}
    assert loaded.name == srv.name

    # 5. get_effective_config preserves env and applies client overrides
    eff_agy = srv.get_effective_config("agy")
    assert eff_agy["env"] == {"STRING_KEY": "val", "INT_KEY": "123"}

    eff_codex = srv.get_effective_config("codex")
    assert eff_codex["env"] == {"STRING_KEY": "override_val", "INT_KEY": "123", "NEW_KEY": "extra"}
