"""Run traces (lcode.traces, lcode.replay): what a request records, replaying it without a model, exporting it
masked."""

from __future__ import annotations

import functools
import json
import os
import shutil
import time

import pytest

from conftest import call, output, reply
from lcode import __version__, cli, config, replay, traces
from lcode.ollama import OllamaError
from lcode.tools import Toolbox


class Raises:
    """A scripted model call that fails the way Ollama does."""

    def __init__(self, message: str) -> None:
        self.message = message

    def __iter__(self):
        raise OllamaError(self.message)


@pytest.fixture
def original(repo, tmp_path_factory):
    """The demo repository as it was before the request changed it."""
    copy = tmp_path_factory.mktemp("original") / "repo"
    shutil.copytree(repo, copy)
    return copy


def fresh_copy(original, tmp_path_factory, name="replay"):
    copy = tmp_path_factory.mktemp(name) / "repo"
    shutil.copytree(original, copy)
    return copy


def only_trace() -> traces.Info:
    [info] = traces.list_traces()
    return info


def test_a_request_is_recorded_step_by_step(make_agent, repo):
    agent = make_agent(
        [reply(tool_calls=[call("read_file", path="src/pkg/math.py")]), reply("It has add and sub.")], trace=True
    )
    agent.run_turn("What's in the math module?")
    info = only_trace()
    assert info.id == f"{agent.session_id}-1"
    assert info.status == "success"
    assert info.path.parent == config.STATE_DIR / "traces"
    events = traces.load(info.path)
    assert events[0]["type"] == "start" and events[-1]["type"] == "end"
    start = events[0]
    assert start["text"] == "What's in the math module?"
    assert (start["model"], start["context"], start["lcode"]) == ("lcode-qwen3.6-35b", 65536, __version__)
    assert start["cwd"] == str(repo) and start["options"]["num_ctx"] == 65536
    [request] = [e for e in events if e["type"] == "message"]
    assert (request["by"], request["step"], request["content"]) == ("user", 1, "What's in the math module?")
    first, second = [e for e in events if e["type"] == "model"]
    assert (first["step"], second["step"]) == (1, 2)
    assert first["reply"]["tool_calls"][0]["function"]["name"] == "read_file"
    assert second["reply"]["content"] == "It has add and sub."
    assert (first["tokens_in"], first["tokens_out"], first["gen_seconds"]) == (1000, 50, 1.0)
    assert first["first_token"] is not None and first["seconds"] >= first["first_token"]
    assert first["prompt_sha"] == traces.digest(agent.messages[0]["content"])
    [tool] = [e for e in events if e["type"] == "tool"]
    assert (tool["name"], tool["step"], tool["index"], tool["arguments"]) == (
        "read_file",
        1,
        1,
        {"path": "src/pkg/math.py"},
    )
    assert "def add" in tool["result"] and tool["sha"] == traces.digest(agent.messages[3]["content"])
    end = events[-1]
    assert (end["status"], end["steps"], end["model_calls"], end["tool_calls"]) == ("success", 2, 2, 1)
    assert (end["tokens_in"], end["tokens_out"]) == (2000, 100)
    assert "read_file" in end["tools"] and end["prompt_sha"] == first["prompt_sha"]
    assert 0 <= end["trace_seconds"] < 1
    # The model client is lcode's own again once the request ends.
    assert agent.trace is None and not isinstance(agent.ollama, traces.Recording)


def test_nothing_is_recorded_with_the_setting_off(make_agent):
    agent = make_agent([reply("Hello.")])  # what the bench and subagents get: no trace of their own
    agent.run_turn("hi")
    assert traces.list_traces() == []
    assert config.SETTINGS["trace"][0] is True  # lcode itself records unless you turn it off


def test_each_request_of_a_session_gets_the_next_number(make_agent):
    agent = make_agent([reply("One."), reply("Two.")], trace=True)
    agent.run_turn("first")
    agent.run_turn("second")
    assert sorted(i.id for i in traces.list_traces()) == [f"{agent.session_id}-1", f"{agent.session_id}-2"]
    assert traces.list_traces()[0].text == "second"  # newest first


def test_a_failed_request_still_ends_its_trace(make_agent):
    agent = make_agent([Raises("Ollama error: the model crashed")], trace=True)
    with pytest.raises(OllamaError):
        agent.run_turn("go")
    info = only_trace()
    assert info.status == "error" and "the model crashed" in info.end["error"]
    [model] = [e for e in traces.load(info.path) if e["type"] == "model"]
    assert model["error"] == "Ollama error: the model crashed"
    assert agent.trace is None and not isinstance(agent.ollama, traces.Recording)


def test_a_trace_replays_without_a_model_server(make_agent, original, tmp_path_factory):
    agent = make_agent(
        [
            reply(tool_calls=[call("read_file", path="src/pkg/math.py")]),
            reply(
                tool_calls=[
                    call("edit_file", path="src/pkg/math.py", old_string="return a + b", new_string="return b + a"),
                    call("bash", command="echo checked"),
                ]
            ),
            reply("Swapped them."),
        ],
        trace=True,
    )
    agent.run_turn("Swap the operands in add")
    copy = fresh_copy(original, tmp_path_factory)
    report = replay.run(traces.load(only_trace().path), copy)
    assert report.ok, report.differences
    assert report.status == ("success", "success") and report.steps == (3, 3)
    # read_file and edit_file ran again, with the same results; the shell command got its recorded result
    assert (report.rerun, report.same, report.served) == (2, 2, 1)
    assert "return b + a" in (copy / "src/pkg/math.py").read_text()


def test_replay_shows_where_a_tool_now_answers_differently(make_agent, original, tmp_path_factory, monkeypatch):
    agent = make_agent([reply(tool_calls=[call("grep", pattern="def add")]), reply("In math.py.")], trace=True)
    agent.run_turn("where is add?")
    grep = Toolbox.t_grep

    @functools.wraps(grep)
    def changed(self, *args, **kwargs):
        return grep(self, *args, **kwargs) + "\n(a harness change)"

    monkeypatch.setattr(Toolbox, "t_grep", changed)
    report = replay.run(traces.load(only_trace().path), fresh_copy(original, tmp_path_factory))
    assert not report.ok
    [difference] = report.differences
    assert (difference.lane, difference.step, difference.what) == ("main", 1, "grep def add")
    assert "(a harness change)" in difference.now and "(a harness change)" not in difference.recorded
    shown = "\n".join(replay.describe_difference(difference))
    assert "+(a harness change)" in shown


def test_replay_runs_commands_only_when_asked(make_agent, repo, tmp_path_factory):
    agent = make_agent([reply(tool_calls=[call("bash", command="touch made-by-bash")]), reply("Made it.")], trace=True)
    agent.run_turn("make a file")
    assert (repo / "made-by-bash").exists()
    events = traces.load(only_trace().path)
    quiet = tmp_path_factory.mktemp("quiet")
    report = replay.run(events, quiet)
    assert report.ok and report.served == 1 and report.rerun == 0
    assert not (quiet / "made-by-bash").exists()
    loud = tmp_path_factory.mktemp("loud")
    report = replay.run(events, loud, commands=True)
    assert report.ok, report.differences
    assert report.rerun == 1 and (loud / "made-by-bash").exists()


def test_subagents_replay_in_lanes_of_their_own(make_agent, original, tmp_path_factory):
    explore = call("agent", type="explore", task="Find where add is defined", description="find add")
    agent = make_agent(
        [
            reply(tool_calls=[explore]),
            reply(tool_calls=[call("grep", pattern="def add")]),
            reply("`add` is in src/pkg/math.py:1."),
            reply("It's in src/pkg/math.py."),
        ],
        trace=True,
        subagents=True,
    )
    agent.run_turn("where is add?")
    events = traces.load(only_trace().path)
    assert {e["lane"] for e in events if e["type"] in ("model", "tool")} == {"main", "agent 1"}
    [task] = [e for e in events if e["type"] == "message" and e["lane"] == "agent 1"]
    assert task["content"] == "Find where add is defined"
    report = replay.run(events, fresh_copy(original, tmp_path_factory))
    assert report.ok, report.differences
    assert report.rerun == 2  # the agent call, and the subagent's grep


def test_a_malformed_call_and_its_retry_replay_the_same(make_agent, original, tmp_path_factory):
    broken = "Ollama error: error parsing tool call: raw='{\"path\": ' err=unexpected end of JSON input"
    agent = make_agent(
        [Raises(broken), reply(tool_calls=[call("read_file", path="src/pkg/math.py")]), reply("Read it.")],
        trace=True,
    )
    agent.run_turn("read the math module")
    events = traces.load(only_trace().path)
    assert any(e["type"] == "message" and e["by"] == "lcode" for e in events)  # "your last tool call…"
    report = replay.run(events, fresh_copy(original, tmp_path_factory))
    assert report.ok, report.differences
    assert report.steps == (3, 3)


def test_an_approved_plan_replays_out_of_plan_mode(make_agent, original, tmp_path_factory, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "a")  # the user approves: auto-edit
    agent = make_agent(
        [
            reply(tool_calls=[call("present_plan", title="New file", plan="1. Write notes.txt")]),
            reply(tool_calls=[call("write_file", path="notes.txt", content="notes\n")]),
            reply("Wrote it."),
        ],
        mode="plan",
        trace=True,
    )
    agent.run_turn("plan, then write notes.txt")
    assert agent.perms.mode == "auto-edit"
    copy = fresh_copy(original, tmp_path_factory)
    report = replay.run(traces.load(only_trace().path), copy)
    assert report.ok, report.differences
    assert (copy / "notes.txt").read_text() == "notes\n"


def test_the_checks_are_recorded_and_replayed(make_agent, original, tmp_path_factory):
    agent = make_agent(
        [reply(tool_calls=[call("write_file", path="new.txt", content="hi\n")]), reply("Done.")],
        trace=True,
        checks="python -c pass",
    )
    agent.run_turn("write new.txt")
    events = traces.load(only_trace().path)
    [check] = [e for e in events if e["type"] == "check"]
    assert (check["command"], check["exit_code"]) == ("python -c pass", 0)
    report = replay.run(events, fresh_copy(original, tmp_path_factory))
    assert report.ok, report.differences
    report = replay.run(events, fresh_copy(original, tmp_path_factory, "loud"), commands=True)
    assert report.ok, report.differences


def test_an_exported_trace_has_no_secrets_home_paths_or_names(make_agent, tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home") / "jdoe"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", "jdoe")
    monkeypatch.setenv("LOGNAME", "jdoe")
    (home / ".gitconfig").write_text("[user]\n\tname = Jane Q Tester\n\temail = jane@example.org\n")
    # Built here, so secret scanners don't take this file for a leak.
    token = "ghp_" + "Ab1" * 12
    aws = "AKIA" + "Z7Q2" * 4
    monkeypatch.setenv("SERVICE_TOKEN", "s3cr3t-" + "value-42")
    leaked = [token, aws, "s3cr3t-value-42", str(home), "jdoe", "Jane Q Tester", "jane@example.org", "10.1.2.3"]
    notes = (
        f"GITHUB_TOKEN={token}\naws_access_key = {aws}\npassword: 'hunter2hunter2'\nservice s3cr3t-value-42\n"
        f"by Jane Q Tester (jane@example.org) from 10.1.2.3, see {home}/proj and /home/otheruser/x\n"
    )
    agent = make_agent(
        [reply(tool_calls=[call("write_file", path="notes.txt", content=notes)]), reply(f"Saved {token} for jdoe.")],
        trace=True,
    )
    agent.run_turn(f"save the notes from {home}/proj, I'm jdoe")
    info = only_trace()
    assert all(any(s in line for line in info.path.read_text().splitlines()) for s in (token, str(home)))
    out = tmp_path_factory.mktemp("export") / "trace.jsonl"
    counts = traces.export(info, path=out)
    exported = out.read_text()
    for secret in [*leaked, "hunter2hunter2", "otheruser"]:
        assert secret not in exported, secret
    events = [json.loads(line) for line in exported.splitlines()]  # still a trace, one JSON object per line
    assert events[0]["type"] == "start" and events[0]["exported"]["lcode"] == __version__
    assert "~/proj" in exported and "<secret>" in exported and "<user>" in exported
    assert counts["secrets"] >= 4 and counts["home folder paths"] >= 2 and counts["user names"] >= 2
    assert counts["email addresses"] >= 1 and counts["IP addresses"] >= 1


def test_old_traces_go_after_30_days_and_the_oldest_when_they_take_too_much(tmp_path, monkeypatch):
    folder = tmp_path / "traces"
    folder.mkdir()
    now = time.time()
    old, older_big, newer_big = folder / "old-1.jsonl", folder / "a-1.jsonl", folder / "b-1.jsonl"
    old.write_text("{}\n")
    os.utime(old, (now - 31 * 86400, now - 31 * 86400))
    older_big.write_text("x" * 600)
    os.utime(older_big, (now - 100, now - 100))
    newer_big.write_text("x" * 600)
    monkeypatch.setattr(traces, "MAX_TOTAL", 1000)
    traces.clean(folder)
    assert not old.exists() and not older_big.exists() and newer_big.exists()


def test_a_trace_stops_growing_at_its_size_limit(make_agent, monkeypatch):
    monkeypatch.setattr(traces, "MAX_TRACE", 4000)
    agent = make_agent(
        [reply(tool_calls=[call("write_file", path="big.txt", content="x" * 6000)]), reply("Wrote it.")], trace=True
    )
    agent.run_turn("write a big file")
    info = only_trace()
    events = traces.load(info.path)
    assert any(e["type"] == "full" for e in events)
    assert info.end["full"] and info.end["status"] == "success" and info.size < 6000
    assert replay.Recorded(events).full


def test_long_texts_keep_their_start_and_end():
    text = "a" * 300 + "b" * 300
    shortened = traces.cut(text, 200)
    assert shortened.startswith("a" * 150) and shortened.endswith("b" * 50) and "400 characters left out" in shortened


def test_sampling_settings_come_from_the_model():
    parsed = traces.parse_parameters('stop "<|im_end|>"\nstop "<|endoftext|>"\ntemperature 0.6\ntop_k 20\ntop_p 0.95')
    assert parsed == {"stop": ["<|im_end|>", "<|endoftext|>"], "temperature": 0.6, "top_k": 20, "top_p": 0.95}


def test_lcode_trace_lists_shows_exports_and_replays(make_agent, repo, tmp_path, capsys, monkeypatch):
    agent = make_agent(
        [reply(tool_calls=[call("read_file", path="src/pkg/math.py")]), reply("It has add and sub.")], trace=True
    )
    agent.run_turn("What's in the math module?")
    output(agent)

    def lcode(*args: str) -> tuple[int, str]:
        with pytest.raises(SystemExit) as done:
            cli.main(["trace", *args])
        return done.value.code, " ".join(capsys.readouterr().out.split())  # the console wraps long lines

    code, out = lcode()
    assert code == 0 and "What's in the math module?" in out and "success" in out and "1 trace(s)" in out
    code, out = lcode("show", "1")
    assert code == 0 and "model 1" in out and "read_file src/pkg/math.py" in out and "→ read_file" in out
    assert lcode("1")[1] == out  # the number alone shows it
    code, out = lcode("show", "nope")
    assert code == 1 and "No trace nope" in out
    code, out = lcode("path")
    assert out == str(config.STATE_DIR / "traces")
    monkeypatch.chdir(tmp_path)
    code, out = lcode("export", "1")
    exported = tmp_path / f"lcode-trace-{agent.session_id}-1.jsonl"
    assert code == 0 and exported.is_file() and "Masked:" in out
    # The recorded folder isn't a git repository: the replay runs in a copy of it as it is now.
    code, out = lcode("replay", "1")
    assert code == 0 and "No differences" in out and "as it is now" in out
    # An exported trace file replays too; its folder is masked, so say which one.
    code, out = lcode("replay", str(exported), "--repo", str(repo))
    assert code == 0 and "No differences" in out and "a copy of" in out


def test_replay_starts_from_the_recorded_commit(make_agent, git_repo, capsys):
    agent = make_agent(
        [
            reply(tool_calls=[call("edit_file", path="README.md", old_string="# Demo", new_string="# Demo app")]),
            reply("Renamed it."),
        ],
        trace=True,
    )
    agent.tools.read_mtimes[str(git_repo / "README.md")] = (git_repo / "README.md").stat().st_mtime
    agent.run_turn("Rename the heading")
    assert (git_repo / "README.md").read_text() == "# Demo app\n"
    info = only_trace()
    env = next(e for e in traces.load(info.path) if e["type"] == "env")
    assert env["git"]["commit"] and env["git"]["dirty"] is False
    assert info.start["read"] == ["README.md"]  # read before the request: no need to read it again
    with pytest.raises(SystemExit) as done:
        cli.main(["trace", "replay", "1"])
    out = " ".join(capsys.readouterr().out.split())  # the console wraps long lines
    assert done.value.code == 0 and "at the recorded commit" in out and "No differences" in out


def test_a_replay_writes_only_in_its_copy(make_agent, repo, original, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "elsewhere.txt"
    agent = make_agent(
        [
            reply(
                tool_calls=[
                    call("write_file", path=str(repo / "inside.txt"), content="in the project\n"),
                    call("write_file", path=str(outside), content="outside the project\n"),
                ]
            ),
            reply("Wrote both."),
        ],
        trace=True,
    )
    agent.run_turn("write two files")
    assert outside.read_text() == "outside the project\n"
    outside.unlink()
    (repo / "inside.txt").unlink()
    copy = fresh_copy(original, tmp_path_factory)
    report = replay.run(traces.load(only_trace().path), copy)
    # The recorded folder's path points into the copy; nothing outside the copy is written.
    assert (copy / "inside.txt").read_text() == "in the project\n"
    assert not (repo / "inside.txt").exists() and not outside.exists()
    [difference] = report.differences
    assert difference.what == f"write_file {outside}" and "outside the project" in difference.now


def test_requests_in_the_interactive_session_are_recorded(make_agent, monkeypatch):
    from lcode.hardware import Hardware
    from lcode.repl import repl

    agent = make_agent(
        [
            reply(tool_calls=[call("read_file", path="src/pkg/math.py")]),
            reply("add and sub."),
            reply("You're welcome."),
        ],
        trace=True,
    )
    lines = iter(["what's in math.py?", "thanks"])

    class Session:
        def prompt(self, *args, **kwargs):
            try:
                return next(lines)
            except StopIteration:
                raise EOFError from None

    monkeypatch.setattr("lcode.repl.build_session", lambda agent: Session())
    monkeypatch.setattr("lcode.repl.preload", lambda agent, session: None)
    monkeypatch.setattr("lcode.repl.start_steering", lambda agent: None)
    repl(agent, prompt=None, hardware=Hardware("linux", "x", 31, "GPU", 12))
    infos = traces.list_traces()
    assert sorted(i.text for i in infos) == ["thanks", "what's in math.py?"]
    assert all(i.status == "success" and i.start["interactive"] for i in infos)
    assert {i.id for i in infos} == {f"{agent.session_id}-1", f"{agent.session_id}-2"}


def test_recording_a_long_request_takes_milliseconds(make_agent):
    steps = [reply(tool_calls=[call("grep", pattern=f"def add{i % 2 or ''}")]) for i in range(100)]
    agent = make_agent([*steps, reply("Done.")], trace=True, repeat_limit=0)
    agent.run_turn("search a hundred times")
    end = only_trace().end
    assert end["steps"] == 101 and end["tool_calls"] == 100
    # About 0.1 ms per event here: well under 2% of any real request, whose steps take seconds.
    assert end["trace_seconds"] < 0.5


def test_an_empty_list_says_whether_recording_is_on(capsys, monkeypatch):
    with pytest.raises(SystemExit):
        cli.main(["trace"])
    assert "No traces yet. Each request in code mode is recorded in" in " ".join(capsys.readouterr().out.split())
    monkeypatch.setattr("lcode.config.load", lambda path=None: {"trace": False})
    with pytest.raises(SystemExit):
        cli.main(["trace"])
    assert "recording is off. Turn it on with: lcode config set trace true" in " ".join(capsys.readouterr().out.split())
