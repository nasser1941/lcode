"""/models and /model: a numbered list to switch from, without typing the model's name."""

import pytest

from conftest import output
from lcode import catalog, cli
from lcode.hardware import Hardware
from lcode.repl import handle_command

LAPTOP = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)


def answer(monkeypatch, text):
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return text

    monkeypatch.setattr("builtins.input", ask)
    return asked


def number_of(name: str) -> str:
    keys = [spec.key for spec in catalog.load()]
    return str(keys.index(name) + 1) if name in keys else str(len(keys) + 1)  # custom:7b comes after the catalog


def test_models_lists_numbers_and_switches_to_the_one_picked(agent, monkeypatch):
    asked = answer(monkeypatch, number_of("custom:7b"))
    handle_command(agent, "/models", LAPTOP)
    out = output(agent)
    assert "Switch to which model? (number or name, Enter to keep lcode-qwen3.6-35b)" in asked[0]
    [row] = [line for line in out.splitlines() if "custom:7b" in line and "│" in line]
    assert row.split("│")[1].strip() == number_of("custom:7b") and "catalog" in out
    assert agent.settings.model == "custom:7b" and "Switched to custom:7b" in output(agent)


def test_model_with_no_name_shows_the_same_list(agent, monkeypatch):
    answer(monkeypatch, "")
    handle_command(agent, "/model", LAPTOP)
    assert "Models for this machine" in output(agent) and "Keeping lcode-qwen3.6-35b." in output(agent)
    assert agent.settings.model == "lcode-qwen3.6-35b"


def test_a_number_or_a_name_switches_without_the_list(agent, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("should not ask"))
    handle_command(agent, f"/model {number_of('custom:7b')}", LAPTOP)
    assert agent.settings.model == "custom:7b" and "Models for this machine" not in output(agent)
    handle_command(agent, "/model qwen3.6-35b", LAPTOP)
    assert agent.settings.model == "lcode-qwen3.6-35b" and agent.settings.context == 262144
    handle_command(agent, "/models 1", LAPTOP)
    assert "Already using lcode-qwen3.6-35b" in output(agent)


def test_a_model_that_isnt_installed_says_how_to_get_it(agent, monkeypatch):
    answer(monkeypatch, number_of("qwen3.8-27b"))
    handle_command(agent, "/models", LAPTOP)
    assert "lcode setup qwen3.8-27b" in output(agent) and agent.settings.model == "lcode-qwen3.6-35b"
    handle_command(agent, "/model 99", LAPTOP)
    assert "There's no model 99 in the list" in " ".join(output(agent).split())


def test_installed_models_that_cant_call_tools_are_left_out(agent):
    installed = {"lcode-qwen3.6-35b", "custom:7b", "embed:1", "embed:1:latest", "chat-only:3b"}
    agent.ollama.installed_names = lambda: installed
    agent.ollama.capabilities["embed:1"] = ["tools", "thinking", "embedding"]  # what Ollama says of qwen3-embedding
    agent.ollama.capabilities["chat-only:3b"] = ["completion"]
    choices = cli.model_choices(agent.ollama)
    assert choices[-1] == "custom:7b" and "embed:1" not in choices and "chat-only:3b" not in choices
    assert choices[: len(catalog.load())] == [spec.key for spec in catalog.load()]


def test_the_shell_command_keeps_its_table_without_numbers(monkeypatch):
    import io

    from rich.console import Console

    from conftest import FakeOllama

    wide = Console(file=io.StringIO(), width=160)
    names = cli.print_models(FakeOllama(), LAPTOP, "qwen3.6-35b", out=wide)
    out = wide.file.getvalue()
    assert names[0] == "qwen3.6-35b" and "Install one with" in out and " # " not in out.splitlines()[2]


def test_context_says_why_larger_windows_are_not_offered(agent, monkeypatch):
    answer(monkeypatch, "")
    handle_command(agent, "/context", LAPTOP)
    text = " ".join(output(agent).split())
    assert "Not offered: 512K and 1M. lcode-qwen3.6-35b was trained on up to 256K tokens" in text
    agent.ollama.max_ctx = 1048576
    handle_command(agent, "/context", LAPTOP)
    assert " ".join(output(agent).split()).count("Not offered") == 1
