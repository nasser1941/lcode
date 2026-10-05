from conftest import output, reply
from lcode import context
from lcode.hardware import Hardware
from lcode.repl import handle_command

HW = Hardware("linux", "x", 31, "GPU", 12)
BIG = "x = 1\n" * 200  # 1,200 characters


def tool(name, **arguments):
    return {"function": {"name": name, "arguments": arguments}}


def conversation(big=BIG):
    """Three requests, with tool results in each."""
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "first request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [tool("read_file", path="a.py"), tool("bash", command="pytest -q")],
        },
        {"role": "tool", "tool_name": "read_file", "content": big},
        {"role": "tool", "tool_name": "bash", "content": big + "\n[exit code: 1]"},
        {"role": "assistant", "content": "Fixed."},
        {"role": "user", "content": "[lcode] The user undid something."},
        {"role": "user", "content": "second request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [tool("agent", type="explore", task="t"), tool("grep", pattern="def")],
        },
        {"role": "tool", "tool_name": "agent", "content": BIG},
        {"role": "tool", "tool_name": "grep", "content": BIG},
        {"role": "user", "content": "third request"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [tool("read_file", path="a.py"), tool("read_file", path="b.py")],
        },
        {"role": "tool", "tool_name": "read_file", "content": BIG},
        {"role": "tool", "tool_name": "read_file", "content": "short"},
    ]


def test_old_tool_output_becomes_stubs():
    messages = conversation()
    saved = context.prune(messages)
    contents = [m["content"] for m in messages]
    assert (
        contents[3]
        == "[lcode: read a.py earlier (read again later); removed to save context. Read it again if you need it.]"
    )
    assert contents[4] == "[lcode: ran `pytest -q` earlier, exit code 1; its output was removed to save context.]"
    assert contents[9] == BIG  # subagent reports are kept
    assert contents[10] == BIG  # the last two requests are left alone
    assert contents[13] == BIG and contents[14] == "short"
    assert saved == 2 * len(BIG) + len("\n[exit code: 1]") - len(contents[3]) - len(contents[4])
    assert context.prune(messages) == 0  # nothing left to do


def test_a_file_read_again_loses_its_older_copy_even_recently():
    messages = [
        *conversation()[:1],
        {"role": "user", "content": "only request"},
        {"role": "assistant", "content": "", "tool_calls": [tool("read_file", path="a.py")]},
        {"role": "tool", "tool_name": "read_file", "content": BIG},
        {"role": "assistant", "content": "", "tool_calls": [tool("read_file", path="a.py")]},
        {"role": "tool", "tool_name": "read_file", "content": BIG},
    ]
    context.prune(messages)
    assert messages[3]["content"].startswith("[lcode: read a.py earlier (read again later)")
    assert messages[5]["content"] == BIG


def test_reading_other_lines_doesnt_supersede_a_read():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "only request"},
        {"role": "assistant", "content": "", "tool_calls": [tool("read_file", path="a.py")]},
        {"role": "tool", "tool_name": "read_file", "content": BIG},
        {"role": "assistant", "content": "", "tool_calls": [tool("read_file", path="a.py", offset=80, limit=10)]},
        {"role": "tool", "tool_name": "read_file", "content": BIG},
        {"role": "assistant", "content": "", "tool_calls": [tool("read_file", path="a.py", offset=80, limit=10)]},
        {"role": "tool", "tool_name": "read_file", "content": BIG},
    ]
    context.prune(messages)
    assert messages[3]["content"] == BIG  # the whole file stays: only lines 80-89 were read again
    assert messages[5]["content"].startswith("[lcode: read a.py from line 80 earlier (read again later)")
    assert messages[7]["content"] == BIG


def test_pruning_goes_further_when_two_requests_are_too_much(make_agent):
    agent = make_agent([reply("ok")], context=16384)
    agent.messages = conversation()
    agent.messages[13]["content"] = BIG * 20  # the last request's own output is small enough to keep…
    agent.messages[10]["content"] = BIG * 20  # …but the request before it read a lot
    agent.ctx_used = int(0.9 * 16384)
    agent.maybe_compact()
    assert agent.pruned == 1 and agent.compacted == 0
    assert agent.messages[10]["content"].startswith("[lcode: grep(def) results removed")
    assert agent.messages[13]["content"] == BIG * 20  # the latest request is never pruned


def test_near_full_context_prunes_before_summarizing(make_agent):
    agent = make_agent([reply("ok")], context=16384)
    agent.messages = conversation(BIG * 10)  # 24K characters of old output: ~8K tokens
    agent.ctx_used = int(0.9 * 16384)
    agent.maybe_compact()
    assert agent.pruned == 1 and agent.compacted == 0  # pruning freed enough: no summary
    assert agent.messages[4]["content"].startswith("[lcode: ran `pytest -q`")
    assert "Removed old tool output" in output(agent)


def test_when_pruning_isnt_enough_it_summarizes(make_agent):
    agent = make_agent([reply("Summary.")], context=16384)
    agent.messages = conversation()
    agent.ctx_used = 16000
    agent.maybe_compact()
    assert agent.pruned == 1 and agent.compacted == 1
    assert agent.messages[1]["content"].startswith("[Summary of our conversation so far]")


def test_long_output_keeps_head_tail_and_errors(tmp_path, monkeypatch):
    monkeypatch.setattr("lcode.config.STATE_DIR", tmp_path)
    lines = [f"step {i}: ok" for i in range(3000)]
    lines[1500] = "E   AssertionError: expected 3, got 4"
    lines[2000] = "Traceback (most recent call last):"
    text = "\n".join(lines)
    short = context.shorten(text, 5000, "output")
    assert short.startswith("step 0: ok") and short.rstrip().endswith(
        "read parts of it with read_file if you need them.]"
    )
    assert "AssertionError: expected 3, got 4" in short and "Traceback" in short
    assert "2 line(s) from them that look like errors" in short
    saved = next(tmp_path.joinpath("outputs").glob("*.txt"))
    assert saved.read_text() == text
    assert context.shorten("short", 5000) == "short"


def test_the_full_output_can_be_read_with_the_sandbox_on(make_agent, repo, tmp_path, monkeypatch):
    monkeypatch.setattr("lcode.config.STATE_DIR", tmp_path)
    path = context.save_output("the whole thing", "output")
    agent = make_agent()
    monkeypatch.setattr(agent, "sandbox_root", lambda: repo)
    assert "the whole thing" in agent.tools.run("read_file", {"path": str(path)})


def test_context_shows_what_uses_it(make_agent, repo, monkeypatch):
    (repo / "AGENTS.md").write_text("Use tabs.\n" * 50)
    agent = make_agent([reply(tool_calls=[tool("read_file", path="README.md")]), reply("Done.")])
    agent.run_turn("look at @src/pkg/math.py")
    parts = {p.name: p for p in context.breakdown(agent)}
    assert parts["AGENTS.md"].chars > 400 and parts["system prompt"].chars > 1000
    assert parts["attached files"].chars > 40 and parts["your messages"].chars < 40
    assert parts["tool results: read_file"].chars > 10
    assert parts["tool definitions"].detail.endswith("tools")
    monkeypatch.setattr("builtins.input", lambda prompt="": "")  # keep the window size
    handle_command(agent, "/context", HW)
    assert "What uses the context" in output(agent) and "tool results: read_file" in output(agent)
