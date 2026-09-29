import io

import pytest
from rich.console import Console

from conftest import call, output, reply
from lcode.ollama import OllamaError
from lcode.render import MarkdownStreamer
from lcode.tools import parse_text_tool_calls


def test_tool_loop_runs_tools_until_the_model_answers(make_agent):
    agent = make_agent(
        [
            reply(thinking="I should read it", tool_calls=[call("read_file", path="src/pkg/math.py")]),
            reply("`add` is defined in `src/pkg/math.py:1`."),
        ]
    )
    agent.run_turn("where is add defined?")
    roles = [m["role"] for m in agent.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assert agent.messages[3]["tool_name"] == "read_file"
    assert "def add(a, b):" in agent.messages[3]["content"]
    assert agent.messages[2]["thinking"] == "I should read it"
    payload = agent.ollama.payloads[0]
    assert payload["options"] == {"num_ctx": 65536}
    assert {t["function"]["name"] for t in payload["tools"]} >= {"read_file", "edit_file", "bash"}
    assert agent.ctx_used == 1050
    assert "src/pkg/math.py:1" in output(agent)


def test_num_batch_is_sent_when_configured(make_agent):
    agent = make_agent([reply("ok")], num_batch=1024)
    agent.run_turn("hi")
    assert agent.ollama.payloads[0]["options"] == {"num_ctx": 65536, "num_batch": 1024}


def test_text_tool_calls_are_recovered(make_agent):
    agent = make_agent([reply('{"name": "list_dir", "arguments": {"path": "src"}}'), reply("done")])
    agent.run_turn("list src")
    assert agent.messages[3]["role"] == "tool" and "pkg/" in agent.messages[3]["content"]


def test_model_without_reasoning_falls_back(make_agent):
    agent = make_agent()

    def chat_stream(payload):
        agent.ollama.payloads.append(payload)
        if payload["think"]:
            raise OllamaError('Ollama error 400: {"error":"model does not support thinking"}')
        yield from reply("fine")

    agent.ollama.chat_stream = chat_stream
    agent.run_turn("hi")
    assert agent.settings.think is False
    assert agent.messages[-1]["content"] == "fine"


def test_out_of_memory_gets_a_hint(make_agent):
    agent = make_agent()

    def chat_stream(payload):
        raise OllamaError("Ollama error: CUDA error: out of memory")
        yield

    agent.ollama.chat_stream = chat_stream
    with pytest.raises(OllamaError, match="/ctx 128k"):
        agent.run_turn("hi")


def test_gpu_memory_error_retries_with_a_smaller_batch(make_agent):
    agent = make_agent(num_batch=1024)

    def chat_stream(payload):
        agent.ollama.payloads.append(payload)
        if payload["options"].get("num_batch") == 1024:
            raise OllamaError("Ollama error 500: CUDA error: an illegal memory access was encountered")
        yield from reply("recovered")

    agent.ollama.chat_stream = chat_stream
    agent.run_turn("hi")
    assert agent.settings.num_batch == 512
    assert agent.messages[-1]["content"] == "recovered"
    assert "retrying with 512" in output(agent)


def test_errors_after_output_are_not_retried(make_agent):
    agent = make_agent(num_batch=1024)

    def chat_stream(payload):
        agent.ollama.payloads.append(payload)
        yield {"message": {"content": "partial answer"}, "done": False}
        raise OllamaError("Ollama error: CUDA error: out of memory")

    agent.ollama.chat_stream = chat_stream
    with pytest.raises(OllamaError, match="smaller context"):
        agent.run_turn("hi")
    assert len(agent.ollama.payloads) == 1  # no duplicate answer from a retry


def test_malformed_tool_calls_are_retried(make_agent):
    agent = make_agent([reply("fixed it")])
    calls = {"n": 0}
    real = agent.ollama.chat_stream

    def chat_stream(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OllamaError(
                'Ollama error: error parsing tool call: raw=\'{"path":"a.py"\', err=unexpected end of JSON input'
            )
        yield from real(payload)

    agent.ollama.chat_stream = chat_stream
    agent.run_turn("write a.py")
    assert agent.messages[-1]["content"] == "fixed it"
    nudge = agent.messages[-2]
    assert nudge["role"] == "user" and "unexpected end of JSON input" in nudge["content"]


def test_malformed_tool_calls_give_up_eventually(make_agent):
    agent = make_agent()

    def chat_stream(payload):
        raise OllamaError("Ollama error: error parsing tool call: raw='{', err=unexpected end of JSON input")
        yield

    agent.ollama.chat_stream = chat_stream
    with pytest.raises(OllamaError, match="error parsing tool call"):
        agent.run_turn("write a.py")


def test_mentions_attach_files(agent):
    text = agent.expand_mentions("explain @src/pkg/math.py please, mail me@example.com")
    assert '<file path="src/pkg/math.py">' in text
    assert "example.com" in text and text.count("<file") == 1


def test_set_context_caps_at_model_maximum(agent):
    agent.ollama.max_ctx = 131072
    assert "capped" in agent.set_context(262144)
    assert agent.settings.context == 131072


def test_compact_replaces_history(make_agent):
    agent = make_agent([reply("first answer"), reply("SUMMARY: user asked things")])
    agent.run_turn("question")
    agent.compact()
    assert [m["role"] for m in agent.messages] == ["system", "user", "assistant"]
    assert "SUMMARY" in agent.messages[1]["content"]


def test_system_prompt_includes_project_instructions(make_agent, repo):
    (repo / "AGENTS.md").write_text("Always use tabs.")
    agent = make_agent()
    assert "Always use tabs." in agent.messages[0]["content"]


@pytest.mark.parametrize(
    "text",
    [
        "<tool_call>\n<function=read_file>\n<parameter=path>\na.py\n</parameter>\n</function>\n</tool_call>",
        '<tool_call>{"name": "read_file", "arguments": {"path": "a.py"}}</tool_call>',
        'Sure:\n```json\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```',
    ],
)
def test_parse_text_tool_calls(text):
    assert parse_text_tool_calls(text) == [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]


def test_markdown_streamer_waits_for_complete_blocks():
    console = Console(file=io.StringIO(), width=60)
    md = MarkdownStreamer(console)
    md.feed("Intro paragraph.\n\n```py\nx = 1\n\n")
    assert "Intro paragraph." in console.file.getvalue()
    assert "x = 1" not in console.file.getvalue()  # the code block is still open
    md.feed("y = 2\n```\n\nDone.")
    assert "y = 2" in console.file.getvalue()
    md.flush()
    assert "Done." in console.file.getvalue()
