import pytest

from lcode import config
from lcode.config import ConfigError, format_tokens, parse_context


@pytest.mark.parametrize(
    ("value", "tokens"),
    [("131072", 131072), ("128k", 131072), ("128K", 131072), ("1m", 1048576), ("0.5m", 524288), (65536, 65536)],
)
def test_parse_context(value, tokens):
    assert parse_context(value) == tokens


@pytest.mark.parametrize("value", ["lots", "12x", "100"])
def test_parse_context_rejects(value):
    with pytest.raises(ConfigError):
        parse_context(value)


def test_format_tokens():
    assert format_tokens(262144) == "256K"
    assert format_tokens(1048576) == "1M"
    assert format_tokens(14200) == "14.2K"
    assert format_tokens(719) == "719"


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    for env in config.ENV_OVERRIDES:
        monkeypatch.delenv(env, raising=False)
    path = tmp_path / "config.toml"
    config.save({"model": "qwen3.5-9b", "context": "64k", "think": "off"}, path=path)
    cfg = config.load(path)
    assert (cfg["model"], cfg["context"], cfg["think"]) == ("qwen3.5-9b", 65536, False)
    config.save({}, path=path, remove=("context",))
    assert config.load(path)["context"] is None


def test_env_overrides_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    config.save({"model": "qwen3.5-9b"}, path=path)
    monkeypatch.setenv("LCODE_MODEL", "custom:7b")
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
    cfg = config.load(path)
    assert cfg["model"] == "custom:7b"
    assert cfg["ollama_host"] == "http://localhost:11434"


def test_invalid_settings():
    with pytest.raises(ConfigError):
        config.coerce("permission_mode", "sometimes")
    with pytest.raises(ConfigError):
        config.coerce("nonsense", 1)
    with pytest.raises(ConfigError):
        config.coerce("num_batch", "many")
