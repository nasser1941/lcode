import pytest

from conftest import FakeOllama
from lcode import __version__, catalog, cli, config
from lcode.hardware import Hardware

HW = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)


def test_resolve_model_prefers_text_only_variant():
    assert cli.resolve_model(FakeOllama(), "qwen3.6-35b") == ("lcode-qwen3.6-35b", catalog.find("qwen3.6-35b"))
    assert cli.resolve_model(FakeOllama(), "custom:7b") == ("custom:7b", None)
    with pytest.raises(cli.NotInstalled, match=r"lcode setup qwen3\.5-9b"):
        cli.resolve_model(FakeOllama(), "qwen3.5-9b")
    with pytest.raises(cli.NotInstalled, match="ollama pull other"):
        cli.resolve_model(FakeOllama(), "other")


def test_choose_context():
    spec = catalog.find("qwen3.6-35b")
    assert cli.choose_context(FakeOllama(), "m", spec, None, HW) == (262144, "")
    ctx, note = cli.choose_context(FakeOllama(max_ctx=131072), "m", spec, 262144, HW)
    assert ctx == 131072 and "capped" in note
    assert cli.choose_context(FakeOllama(), "custom:7b", None, None, HW) == (32768, "")
    ctx, note = cli.choose_context(
        FakeOllama(max_ctx=1048576), "m", catalog.find("qwen3.6-27b"), 262144, Hardware("linux", "x", 16, "GPU", 8)
    )
    assert "out-of-memory" in note


def test_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.strip() == f"lcode {__version__}"


def test_config_command(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    cli.main(["config", "set", "context", "128k"])
    assert "context = 131072" in path.read_text()
    cli.main(["config", "unset", "context"])
    assert "context" not in path.read_text()
    with pytest.raises(SystemExit):
        cli.main(["config", "set", "permission_mode", "never"])
