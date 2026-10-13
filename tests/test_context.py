import pytest

from conftest import call, output, reply
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


def test_a_large_window_is_summarized_at_the_ceiling(make_agent):
    assert make_agent(context=262144, compact_at=65536).compact_limit() == 65536
    assert make_agent(context=262144, compact_at=0).compact_limit() == int(0.85 * 262144)
    assert make_agent(context=32768, compact_at=65536).compact_limit() == int(0.85 * 32768)
    agent = make_agent([reply("Summary.")], context=262144, compact_at=8192)
    agent.messages = conversation()
    agent.ctx_used = 9000  # 3% of the window, but past the ceiling
    agent.maybe_compact()
    assert agent.pruned == 1 and agent.compacted == 1
    assert "The conversation reached 8K tokens (compact_at)" in output(agent)
    agent.ctx_used = 5000
    agent.maybe_compact()  # under the ceiling again: nothing to do
    assert agent.compacted == 1


def test_summaries_keep_the_users_requests_word_for_word(make_agent):
    agent = make_agent([reply("Summary one."), reply("ok"), reply("Summary two.")], context=16384)
    agent.messages = conversation()
    agent.messages[1]["content"] = 'make it 2x3\n\n<file path="config.json">\n{"grid": "3x3"}\n</file>'
    agent.compact()
    summary = agent.messages[1]["content"]
    assert summary.startswith("[Summary of our conversation so far]\n\nSummary one.")
    assert context.REQUESTS_HEADER in summary and '"make it 2x3"' in summary and "config.json" not in summary
    agent.run_turn("now commit it")
    agent.compact()
    assert context.user_requests(agent.messages)[:1] == ["make it 2x3"]
    assert context.user_requests(agent.messages)[-1] == "now commit it"
    assert agent.messages[1]["content"].count(context.REQUESTS_HEADER) == 1


def test_only_the_latest_requests_are_kept(make_agent):
    requests = [f"request {n}" for n in range(12)] + ["x" * 5000]
    block = context.requests_block(requests)
    assert "(and 5 earlier, in the summary)" in block and '"request 4"' not in block and '"request 5"' in block
    assert "[…]" in block and len(block) < 3000


def test_context_shows_when_it_makes_room(make_agent):
    agent = make_agent(context=262144, compact_at=65536)
    handle_command(agent, "/context 262144", HW)
    assert "makes room at 64K (compact_at)" in output(agent)


def test_a_fresh_summary_is_not_summarized_again_right_away(make_agent):
    agent = make_agent([reply("Summary."), reply("Summary again.")], context=262144, compact_at=16384)
    agent.messages = conversation()
    agent.ctx_used = 17000
    agent.maybe_compact()
    assert agent.compacted == 1
    agent.summarized_size = 15000  # the summary and the requests it keeps are large
    agent.ctx_used = 17000  # past the ceiling, but it grew only 2K since
    agent.maybe_compact()
    assert agent.compacted == 1
    agent.ctx_used = 15000 + 16384 // 4 + 1  # it grew by a quarter of the ceiling: summarize again
    agent.maybe_compact()
    assert agent.compacted == 2
    agent.summarized_size, agent.ctx_used = 200000, 230000  # the window itself is full: always
    agent.ollama.scripts.append(reply("Summary three."))
    agent.maybe_compact()
    assert agent.compacted == 3


def test_compact_at_has_a_floor():
    from lcode import config
    from lcode.config import ConfigError

    assert config.coerce("compact_at", "64k") == 65536 and config.coerce("compact_at", "off") == 0
    with pytest.raises(ConfigError, match="at least 16k"):
        config.coerce("compact_at", "8k")


def test_the_model_is_told_next_to_its_request_what_it_no_longer_has(make_agent):
    agent = make_agent([reply("ok")], context=16384)
    agent.messages = conversation(BIG * 10)
    agent.messages[12]["tool_calls"] = [tool("read_file", path="c.py"), tool("read_file", path="b.py")]
    agent.ctx_used = int(0.9 * 16384)
    agent.maybe_compact()
    assert agent.messages[11]["content"].startswith(
        "third request\n\n[lcode] To make room, lcode removed what you read earlier from: a.py. It isn't in this "
        "conversation any more: read it again"
    )
    assert context.user_requests(agent.messages)[-1] == "third request"


TODOS = [
    {"content": "Port the parser", "status": "completed"},
    {"content": "Keep the old flags", "status": "in_progress"},
]


def test_the_task_sheet_survives_a_summary_and_a_resume(make_agent, repo):
    agent = make_agent(
        [
            reply(tool_calls=[call("read_file", path="README.md")]),
            reply(tool_calls=[call("edit_file", path="README.md", old_string="Demo", new_string="Demo app")]),
            reply(tool_calls=[call("todo_write", todos=TODOS, notes=["Decided: keep --legacy as an alias"])]),
            reply("Working on it."),
            reply("Summary."),
        ]
    )
    agent.run_turn("port the parser")
    agent.save()
    agent.compact()
    summary = agent.messages[1]["content"]
    assert context.SHEET_HEADER in summary and "[>] Keep the old flags" in summary
    assert "- Decided: keep --legacy as an alias" in summary and "Files changed in this session: README.md" in summary
    other = make_agent()
    other.load_latest()
    assert other.tools.todos == TODOS and other.tools.notes == ["Decided: keep --legacy as an alias"]
    assert list(other.tools.changed) == ["README.md"]
    other.new_session()
    assert other.tools.todos == [] and other.tools.notes == [] and not other.tools.changed


def test_the_sheet_is_pinned_next_to_the_request_when_output_is_removed(make_agent):
    agent = make_agent([reply("ok")], context=16384)
    agent.messages = conversation(BIG * 10)
    agent.tools.todos = TODOS
    agent.ctx_used = int(0.9 * 16384)
    agent.maybe_compact()
    agent.maybe_compact()  # pinned again: replaced, not added
    latest = agent.messages[11]["content"]
    assert latest.startswith("third request") and latest.count(context.SHEET_HEADER) == 1
    assert context.user_requests(agent.messages)[-1] == "third request"  # the sheet isn't the user's words


def test_status_shows_the_sheet(make_agent):
    from lcode.repl import handle_command

    agent = make_agent([reply(tool_calls=[call("todo_write", todos=TODOS, notes=["Open: --strict?"])]), reply("ok")])
    agent.run_turn("port the parser")
    handle_command(agent, "/status", HW)
    text = output(agent)
    assert "Requests (latest)" in text and "port the parser" in text and "Keep the old flags" in text
    assert "Open: --strict?" in text
