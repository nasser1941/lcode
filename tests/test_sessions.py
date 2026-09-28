import datetime as dt
import json
import os

from conftest import output, reply
from lcode import cli, sessions
from lcode.hardware import Hardware
from lcode.repl import choose_session, handle_command

HW = Hardware("linux", "x", 31, "GPU", 12)


def run(agent, text):
    agent.ollama.scripts.append(reply(f"answer to {text}"))
    agent.run_turn(text)
    agent.save()


def touch(agent, seconds_ago):
    """Pretend the session file was last written `seconds_ago`."""
    f = agent.session_file()
    t = f.stat().st_mtime - seconds_ago
    os.utime(f, (t, t))


def test_sessions_are_saved_with_title_and_name(make_agent):
    agent = make_agent()
    run(agent, "Explain @src/pkg/math.py and    the tests please")
    agent.rename("  math   deep dive ")
    data = json.loads(agent.session_file().read_text())
    assert data["name"] == "math deep dive"
    assert data["title"] == "Explain @src/pkg/math.py and the tests please"  # attachment stripped
    info = sessions.list_sessions(agent.cwd)[0]
    assert (info.label, info.turns) == ("math deep dive", 1)


def test_empty_sessions_are_not_saved(make_agent):
    agent = make_agent()
    agent.save()
    assert not agent.session_file().exists()
    assert sessions.list_sessions() == []


def test_list_orders_by_last_use_and_filters_by_folder(make_agent, tmp_path_factory):
    first = make_agent()
    run(first, "first task")
    touch(first, 300)
    second = make_agent()
    run(second, "second task")
    touch(second, 100)
    elsewhere = make_agent()
    elsewhere.cwd = tmp_path_factory.mktemp("elsewhere").resolve()
    run(elsewhere, "other folder")
    assert [s.title for s in sessions.list_sessions(first.cwd)] == ["second task", "first task"]
    assert len(sessions.list_sessions()) == 3
    # Continuing the older session makes it the most recent one.
    run(first, "more work")
    assert sessions.list_sessions(first.cwd)[0].id == first.session_id


def test_find_by_number_name_id_and_title():
    def info(i, name, title):
        return sessions.SessionInfo(f"2026092{i}-120000-abc{i}", None, "/p", name, title, "m", 0.0, 1)

    found = [info(1, "Auth refactor", "rewrite login"), info(2, "", "fix the flaky test"), info(3, "", "fix docs")]
    assert sessions.find("2", found) is found[1]
    assert sessions.find("auth REFACTOR", found) is found[0]
    assert sessions.find("20260923", found) is found[2]
    assert sessions.find("flaky", found) is found[1]
    assert sessions.find("fix", found) is None  # ambiguous
    assert sessions.find("9", found) is None


def test_age():
    now = dt.datetime(2026, 9, 28, 18, 0)
    ts = lambda **kw: (now - dt.timedelta(**kw)).timestamp()  # noqa: E731
    assert sessions.age(ts(seconds=10), now) == "just now"
    assert sessions.age(ts(minutes=5), now) == "5 min ago"
    assert sessions.age(ts(hours=3), now) == "3 h ago"
    assert sessions.age(ts(days=1), now) == "yesterday"
    assert sessions.age(ts(days=10), now) == "Sep 18"


def test_rename_and_resume_commands(make_agent, monkeypatch):
    old = make_agent()
    run(old, "build the parser")
    handle_command(old, "/rename parser work", HW)
    touch(old, 60)

    agent = make_agent()
    run(agent, "something else")
    # Pick from the list by number: the parser session is second (older).
    monkeypatch.setattr("builtins.input", lambda _: "2")
    handle_command(agent, "/resume", HW)
    assert agent.session_id == old.session_id and agent.session_name == "parser work"
    assert [m["content"] for m in agent.messages if m["role"] == "user"] == ["build the parser"]
    assert "Where you left off" in output(agent)
    # Resume directly by name, without a prompt.
    other = make_agent()
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("should not ask")))
    handle_command(other, "/resume Parser Work", HW)
    assert other.session_id == old.session_id


def test_resumed_session_needs_fresh_reads(make_agent):
    agent = make_agent()
    agent.tools.run("read_file", {"path": "src/pkg/math.py"})
    run(agent, "look at math")
    agent.new_session()
    agent.load(sessions.list_sessions(agent.cwd)[0])
    edit = {"path": "src/pkg/math.py", "old_string": "a - b", "new_string": "b - a"}
    assert "must read_file" in agent.tools.run("edit_file", edit)


def test_resume_switches_to_the_session_folder(make_agent, tmp_path_factory):
    other = tmp_path_factory.mktemp("other").resolve()
    agent = make_agent()
    agent.cwd = other
    run(agent, "work in another folder")
    here = make_agent()
    assert choose_session(here, "all work in another") is not None
    note = here.load(sessions.list_sessions()[0])
    assert here.cwd == other and "Working directory is now" in note


def test_resume_with_nothing_saved(make_agent, monkeypatch):
    agent = make_agent()
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("should not ask")))
    assert choose_session(agent, "") is None
    assert "No saved sessions yet" in output(agent)


def test_clear_starts_a_new_unnamed_session(make_agent):
    agent = make_agent()
    run(agent, "task one")
    agent.rename("one")
    first_id = agent.session_id
    handle_command(agent, "/clear", HW)
    assert agent.session_id != first_id and agent.session_name == ""
    assert sessions.list_sessions(agent.cwd)[0].id == first_id  # the old one is kept


def test_cli_resume_flag():
    assert cli.build_parser().parse_args(["--resume"]).resume == ""
    assert cli.build_parser().parse_args(["--resume", "parser work"]).resume == "parser work"
    assert cli.build_parser().parse_args([]).resume is None
