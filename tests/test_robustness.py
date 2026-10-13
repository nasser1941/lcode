"""Bugs that real-world tests found (issue #118), one test each."""

import json
import time
from pathlib import Path

import pytest

from conftest import call, output, reply
from lcode.context import OutputSpool
from lcode.permissions import bash_key, is_read_only
from lcode.tools import parse_text_tool_calls


@pytest.mark.parametrize(
    "command",
    [
        "sort -o out.txt in.txt",
        "sort --output=out.txt in.txt",
        "uniq in.txt out.txt",
        "tree -o out.txt",
        "git branch -D topic",
        "git branch -m old new",
        "git branch new-branch",
        "git remote remove origin",
        "git remote add fork https://example.com/x.git",
        "git log --output=log.txt",
        "git diff --output x.patch",
        "git -c core.pager=less log",
        "rg --pre ./script.sh needle",
        "find . -fls out.txt",
        "date -s 12:00",
        "nvidia-smi -pm 1",
        "cat a && rm b",
        "echo $(rm x)",
    ],
)
def test_commands_that_change_things_are_not_read_only(command):
    assert not is_read_only(command)


@pytest.mark.parametrize(
    "command",
    [
        "sort in.txt",
        "git branch",
        "git branch -a -v",
        'git branch --list "feat/*"',
        "git branch --contains HEAD",
        "git remote -v",
        "git log --oneline -5",
        "cd src && git status",
        "cat a.txt 2>&1 | head -20",
        'grep "a|b" file.txt',
        "nvidia-smi --query-gpu=name --format=csv",
        "ls > /dev/null",
    ],
)
def test_reading_commands_still_run_without_asking(command):
    assert is_read_only(command)


def test_always_allow_covers_only_the_same_programs():
    assert bash_key("npm test") == "bash:npm test"
    assert bash_key("cd app && npm test 2>&1 | tail -20") == "bash:npm test + tail"
    assert bash_key("npm test && rm -rf build") != bash_key("npm test")
    assert bash_key("npm test > log.txt") == "bash:npm test (redirected)"
    assert bash_key("echo $(whoami)") == "bash:echo $(whoami)"  # can't be read: only this exact command


def test_always_for_one_command_doesnt_run_a_chain(make_agent, repo, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "a")  # "always" for touch
    agent = make_agent(mode="ask")
    agent.tools.run("bash", {"command": "touch first.txt"})
    asked = []
    agent.perms.approve = lambda request: asked.append(request["target"]) or False
    agent.tools.run("bash", {"command": "touch second.txt"})
    assert asked == [] and (repo / "second.txt").exists()  # "always" covers touch
    agent.tools.run("bash", {"command": "touch third.txt && rm first.txt"})
    assert asked == ["touch third.txt && rm first.txt"]  # but not touch followed by rm
    assert (repo / "first.txt").exists() and not (repo / "third.txt").exists()


def test_json_in_an_answer_is_not_run(make_agent, repo):
    answer = (
        "Done. For reference, a tool call in the stream-json output looks like this:\n\n"
        '```json\n{"name": "bash", "arguments": {"command": "touch PROSE_RAN"}}\n```\n'
    )
    agent = make_agent([reply(answer)])
    agent.run_turn("explain the event format")
    assert not (repo / "PROSE_RAN").exists()
    assert parse_text_tool_calls('Here: {"name": "bash", "arguments": {"command": "ls"}} and so on') == []
    call_only = parse_text_tool_calls('Let me look:\n{"name": "read_file", "arguments": {"path": "a.py"}}')
    assert call_only == [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]


def test_an_edit_made_while_asking_is_not_overwritten(make_agent, repo, monkeypatch):
    target = repo / "src" / "pkg" / "math.py"
    agent = make_agent(mode="ask")
    agent.tools.run("read_file", {"path": "src/pkg/math.py"})

    def the_user_edits_meanwhile(prompt=""):
        target.write_text(target.read_text() + "\n# the user's own line\n")
        return "y"

    monkeypatch.setattr("builtins.input", the_user_edits_meanwhile)
    result = agent.tools.run("edit_file", {"path": "src/pkg/math.py", "old_string": "a - b", "new_string": "b - a"})
    assert result.startswith("Error: src/pkg/math.py changed while the user was being asked")
    assert "the user's own line" in target.read_text() and "a - b" in target.read_text()
    result = agent.tools.run("write_file", {"path": "src/pkg/math.py", "content": "x = 1\n"})
    assert result.startswith("Error:") and "the user's own line" in target.read_text()


def test_text_values_get_their_declared_type(make_agent, repo):
    (repo / "twice.txt").write_text("a = 1\na = 1\n")
    agent = make_agent()
    agent.tools.run("read_file", {"path": "twice.txt"})
    result = agent.tools.run(
        "edit_file", {"path": "twice.txt", "old_string": "a = 1", "new_string": "b = 2", "replace_all": "false"}
    )
    assert "occurs 2 times" in result and (repo / "twice.txt").read_text() == "a = 1\na = 1\n"
    agent.tools.run(
        "edit_file", {"path": "twice.txt", "old_string": "a = 1", "new_string": "b = 2", "replace_all": "true"}
    )
    assert (repo / "twice.txt").read_text() == "b = 2\nb = 2\n"
    assert agent.tools.run("read_file", {"path": "twice.txt", "limit": "1"}).count("\n") <= 3


def test_a_command_that_keeps_printing_still_times_out(make_agent):
    agent = make_agent()
    started = time.monotonic()
    result = agent.tools.run("bash", {"command": "while true; do echo tick; sleep 0.05; done", "timeout": 1})
    assert time.monotonic() - started < 5 and "timed out after 1s" in result


def test_a_chatty_leftover_doesnt_keep_the_command_running(make_agent):
    agent = make_agent()
    started = time.monotonic()
    result = agent.tools.run("bash", {"command": "(while true; do echo noise; sleep 0.05; done) & echo started"})
    assert time.monotonic() - started < 10 and "started" in result and "lcode stopped what this command left" in result


def test_long_output_stays_bounded_and_keeps_what_matters(tmp_path, monkeypatch):
    monkeypatch.setattr("lcode.config.STATE_DIR", tmp_path)
    spool = OutputSpool(1000, "output")
    for i in range(20000):
        spool.add(f"line {i}\n")
        if i == 10000:
            spool.add("ERROR: it broke here\n")
    assert len("".join(spool.head)) <= 600 and spool.tail_chars <= 250
    text = spool.text()
    assert (
        text.startswith("line 0\n") and "ERROR: it broke here" in text and text.rstrip().endswith("if you need them.]")
    )
    saved = text.rsplit(" is in ", 1)[1].split(":", 1)[0]
    assert "line 19999" in Path(saved).read_text()
    small = OutputSpool(1000)
    small.add("short\n")
    assert small.text() == "short\n"


def test_a_large_result_is_cut_to_a_share_of_the_window(make_agent, repo):
    (repo / "big.log").write_text("".join(f"line {i}: some log output that goes on for a while\n" for i in range(2000)))
    agent = make_agent([reply(tool_calls=[call("read_file", path="big.log")]), reply("summary")], context=16384)
    agent.run_turn("what's in big.log?")
    result = next(m["content"] for m in agent.messages if m["role"] == "tool")
    assert len(result) <= agent.result_budget() and "Read on with offset=" in result


def test_compaction_counts_what_was_added_since_the_last_answer(make_agent):
    agent = make_agent([reply("ok")], context=16384)
    agent.run_turn("hi")  # the fake server counts 1,050 tokens
    agent.messages.append({"role": "tool", "tool_name": "bash", "content": "x" * 45000})  # ~15K tokens more
    assert agent.next_request_tokens() > 15000
    agent.ollama.scripts.append(reply("Summary."))
    agent.maybe_compact()
    assert agent.pruned + agent.compacted >= 1


def test_sessions_are_saved_after_every_step_and_atomically(make_agent, repo):
    agent = make_agent(
        [reply(tool_calls=[call("read_file", path="README.md")]), reply(tool_calls=[call("glob", pattern="*")])]
    )
    agent.autosave = True
    saved_during = []
    real_save = agent.save

    def save():
        real_save()
        saved_during.append(len(json.loads(agent.session_file().read_text())["messages"]))

    agent.save = save
    with pytest.raises(IndexError):  # the fake server runs out of answers: as if lcode crashed here
        agent.run_turn("look around")
    assert saved_during and saved_during[-1] >= 4  # the read and its result are on disk
    assert not list(agent.session_file().parent.glob(".*.partial"))
    assert output(agent)


def test_windows_line_endings_survive_an_edit(make_agent, repo):
    (repo / "win.txt").write_bytes(b"first line\r\nsecond line\r\nthird line\r\n")
    agent = make_agent()
    agent.tools.run("read_file", {"path": "win.txt"})
    agent.tools.run("edit_file", {"path": "win.txt", "old_string": "second line", "new_string": "2nd line"})
    agent.tools.run("edit_file", {"path": "win.txt", "old_string": "first line\n2nd line", "new_string": "one\ntwo"})
    assert (repo / "win.txt").read_bytes() == b"one\r\ntwo\r\nthird line\r\n"


def test_other_encodings_survive_an_edit(make_agent, repo):
    (repo / "latin.txt").write_bytes("café = 1\nnaïve = 2\n".encode("cp1252"))
    (repo / "bom.txt").write_bytes(b"\xef\xbb\xbfkey = 1\n")
    agent = make_agent()
    assert "café = 1" in agent.tools.run("read_file", {"path": "latin.txt"})  # shown as it is, not as "caf�"
    agent.tools.run("edit_file", {"path": "latin.txt", "old_string": "= 2", "new_string": "= 3"})
    assert (repo / "latin.txt").read_bytes() == "café = 1\nnaïve = 3\n".encode("cp1252")
    result = agent.tools.run("edit_file", {"path": "latin.txt", "old_string": "= 3", "new_string": "= 3  # ✓"})
    assert result.startswith("Error: the file is in cp1252, which can't hold '✓'")
    assert (repo / "latin.txt").read_bytes() == "café = 1\nnaïve = 3\n".encode("cp1252")  # nothing written
    agent.tools.run("read_file", {"path": "bom.txt"})
    agent.tools.run("edit_file", {"path": "bom.txt", "old_string": "key = 1", "new_string": "key = 2"})
    assert (repo / "bom.txt").read_bytes() == b"\xef\xbb\xbfkey = 2\n"
