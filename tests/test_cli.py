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


def test_models_table_fits_narrow_terminals(monkeypatch):
    import io

    from rich.console import Console

    narrow = Console(file=io.StringIO(), width=80)
    monkeypatch.setattr(cli, "console", narrow)
    cli.print_models(FakeOllama(), HW, "qwen3.6-35b")
    out = narrow.file.getvalue()
    assert "Fits here" in out and "qwen3.6-35b" in out and "recommended" in out
    assert "…" not in out


def test_other_model_gets_its_own_context(tmp_path, monkeypatch):
    """A context saved for the default model must not be forced on a model picked with --model."""
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    config.save({"model": "qwen3.6-35b", "context": 262144})
    requested = []

    def fake_choose_context(ollama, model, spec, requested_ctx, hw):
        requested.append((model, requested_ctx))
        return 131072, ""

    monkeypatch.setattr(cli, "Ollama", lambda host: FakeOllama())
    monkeypatch.setattr(cli, "detect", lambda: HW)
    monkeypatch.setattr(cli, "check_ollama", lambda *args: "0.32.0")
    monkeypatch.setattr(cli, "resolve_model", lambda ollama, name: (name, catalog.find(name)))
    monkeypatch.setattr(cli, "choose_context", fake_choose_context)
    monkeypatch.setattr("lcode.repl.repl", lambda *args, **kwargs: None)
    cli.main(["--model", "qwen3.5-9b"])
    cli.main(["--model", "qwen3.6-35b"])
    cli.main(["--model", "qwen3.5-9b", "--context", "64k"])
    assert requested == [("qwen3.5-9b", None), ("qwen3.6-35b", 262144), ("qwen3.5-9b", 65536)]


def test_learned_limits_cap_the_automatic_context(tmp_path, monkeypatch):
    from lcode import limits

    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    spec = catalog.find("nemotron-3.5-lightning")
    big = Hardware("linux", "x", 64, "GPU", 12)
    assert cli.choose_context(FakeOllama(max_ctx=1048576), "nemo", spec, None, big)[0] == 1048576
    limits.record("nemo", 524288)
    assert cli.choose_context(FakeOllama(max_ctx=1048576), "nemo", spec, None, big)[0] == 524288
    # An explicit --context is respected (and falls back at runtime if it doesn't fit).
    assert cli.choose_context(FakeOllama(max_ctx=1048576), "nemo", spec, 1048576, big)[0] == 1048576


def test_bench_warns_only_about_models_it_isnt_running(tmp_path, monkeypatch):
    import io

    from rich.console import Console

    from lcode import bench

    class Busy(FakeOllama):
        def running(self):
            return [{"name": "lcode-qwen3.6-35b:latest"}, {"name": "qwen2.5-coder:14b"}]

    out = Console(file=io.StringIO(), width=200)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(cli, "console", out)
    monkeypatch.setattr(cli, "Ollama", lambda host: Busy())
    monkeypatch.setattr(cli, "detect", lambda: HW)
    monkeypatch.setattr(cli, "check_ollama", lambda *args: "0.32.0")
    ran = []
    monkeypatch.setattr(bench, "run_model", lambda ollama, name, *args: ran.append(name) or bench.ModelRun(name, name))
    cli.main(["bench", "qwen3.6-35b", "gpt-oss-20b"])
    text = out.file.getvalue()
    assert "Already loaded in Ollama: qwen2.5-coder:14b." in text
    assert "Skipping gpt-oss-20b" in text and ran == ["qwen3.6-35b"]
