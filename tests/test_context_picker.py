import pytest

from conftest import output, reply
from lcode import limits
from lcode.hardware import Hardware
from lcode.repl import context_options, handle_command

LAPTOP = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)


def answer(monkeypatch, text):
    monkeypatch.setattr("builtins.input", lambda _: text)


def test_options_show_fit_current_and_recommended(agent):
    options = {o["size"]: o for o in context_options(agent, LAPTOP)}
    assert sorted(options) == [16384, 32768, 65536, 131072, 262144]  # capped at the model's 256K
    assert options[65536]["current"] and options[262144]["recommended"]
    assert options[262144]["note"] == "~28 GB · GPU + RAM"


def test_options_warn_about_sizes_that_ran_out_of_memory(agent):
    limits.record(agent.settings.model, 131072)
    options = {o["size"]: o for o in context_options(agent, LAPTOP)}
    assert "ran out of memory here before" in options[262144]["note"]
    assert "ran out of memory" not in options[131072]["note"]
    assert options[131072]["recommended"]


def test_options_for_models_outside_the_catalog(make_agent):
    agent = make_agent(model="custom:7b", context=8192)
    options = context_options(agent, LAPTOP)
    assert options[0]["size"] == 8192 and options[0]["current"]
    assert options[0]["note"] == "no memory estimate for this model"


def test_pick_by_number(agent, monkeypatch):
    answer(monkeypatch, "4")  # 16K, 32K, 64K, 128K, ...
    handle_command(agent, "/context", LAPTOP)
    assert agent.settings.context == 131072
    assert "Context window set to 128K" in output(agent)


def test_pick_by_size_and_keep(agent, monkeypatch):
    answer(monkeypatch, "96k")
    handle_command(agent, "/context", LAPTOP)
    assert agent.settings.context == 98304
    answer(monkeypatch, "")
    handle_command(agent, "/ctx", LAPTOP)
    assert agent.settings.context == 98304 and "Keeping 96K" in output(agent)


def test_bad_answer_changes_nothing(agent, monkeypatch):
    answer(monkeypatch, "lots")
    handle_command(agent, "/context", LAPTOP)
    assert agent.settings.context == 65536 and "invalid context size" in output(agent)


def test_direct_size_skips_the_list(agent, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("should not ask"))
    handle_command(agent, "/context 32k", LAPTOP)
    assert agent.settings.context == 32768


def test_shrinking_below_the_conversation_summarizes_first(make_agent, monkeypatch):
    agent = make_agent([reply("first answer"), reply("SUMMARY of the work")])
    agent.run_turn("a long task")
    agent.ctx_used = 60000  # more than 85% of 32K
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("should not ask"))
    handle_command(agent, "/context 32k", LAPTOP)
    assert agent.settings.context == 32768
    assert "SUMMARY of the work" in agent.messages[1]["content"]
    assert "summarizing it first" in output(agent)
