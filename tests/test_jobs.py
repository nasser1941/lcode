import shlex
import sys
import time
from pathlib import Path

import pytest

from conftest import call, output, reply
from lcode import api, config, jobs, notify, planning
from lcode.hardware import Hardware
from lcode.permissions import bash_key
from lcode.repl import handle_command
from lcode.tools_shell import TRAILING_AMPERSAND

HW = Hardware("linux", "x", 31, "GPU", 12)
TICKER = f"{shlex.quote(sys.executable)} -u -c " + shlex.quote(
    "import time\nfor i in range(600):\n    print('tick', i)\n    time.sleep(0.05)"
)


def gone(pid: int) -> bool:
    """The process ended (or is a zombie waiting for its parent)."""
    status = Path(f"/proc/{pid}/status")  # Linux; elsewhere there's nothing to check
    try:
        return not status.exists() or "zombie" in status.read_text().lower()
    except OSError:  # it ended between the two looks (ENOENT or ESRCH, by timing)
        return True


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


# ----------------------------------------------------------------------------- background commands


def test_a_job_runs_until_stopped_with_its_children(tmp_path):
    pool = jobs.Jobs()
    job = pool.start(["bash", "-c", f"sleep 60 & echo $! > child.pid; {TICKER}"], tmp_path, "ticker")
    assert job.running and job.id == "1" and "tick 0" in job.new_output(10_000)
    assert wait_for(lambda: "tick" in job.output[job.read :])
    newer = job.new_output(10_000)
    assert newer and "tick 0\n" not in newer  # only what's new
    child = int((tmp_path / "child.pid").read_text())
    stopped = pool.stop("1")
    assert stopped.status() == "stopped" and not stopped.running
    assert wait_for(lambda: gone(child))
    with pytest.raises(jobs.JobError, match=r"there's no background job 9 \(jobs: 1\)"):
        pool.get("9")


def test_output_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "KEEP", 2000)
    pool = jobs.Jobs()
    job = pool.start(["bash", "-c", "for i in $(seq 1 2000); do echo line $i; done"], tmp_path, "lines")
    assert wait_for(lambda: not job.running and job.ended)
    assert len(job.output) <= 2000 and job.dropped > 0 and job.output.rstrip().endswith("line 2000")
    text = job.new_output(100)
    assert text.startswith("[… ") and text.rstrip().endswith("line 2000") and job.status() == "exited with code 0"


def test_the_model_runs_reads_and_stops_a_background_command(make_agent, repo):
    agent = make_agent(
        [
            reply(tool_calls=[call("bash", command=TICKER, background=True)]),
            reply(tool_calls=[call("bash_output", id="1")]),
            reply(tool_calls=[call("bash_stop", id="1")]),
            reply(tool_calls=[call("bash", command="echo done", background=True)]),
            reply("All done."),
        ]
    )
    agent.run_turn("start the ticker, check it, stop it")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert results[0].startswith("Started background job 1: it keeps running") and "tick 0" in results[0]
    assert results[1].startswith("[job 1 (running, ") and "tick" in results[1]
    assert results[2].startswith("[job 1 (stopped, ")
    assert results[3].startswith("The command ended right away (exited with code 0)") and "done" in results[3]
    assert "(in the background)" in output(agent)
    handle_command(agent, "/jobs", HW)
    assert "job 1 (stopped" in output(agent) and "job 2 (exited with code 0" in output(agent)


def test_jobs_stop_with_the_session_and_from_jobs(make_agent, repo):
    agent = make_agent([reply(tool_calls=[call("bash", command=TICKER, background=True)]), reply("ok")])
    agent.run_turn("start it")
    handle_command(agent, "/jobs", HW)
    assert "job 1 (running" in output(agent) and "/jobs stop <id>" in output(agent)
    handle_command(agent, "/jobs stop 1", HW)
    assert "Stopped job 1 (stopped" in output(agent) and not agent.jobs.running()
    agent.jobs.start(["bash", "-c", TICKER], repo, "again")
    api.close_agent(agent)
    assert not agent.jobs.running() and "Stopped 1 background job(s)." in output(agent)


def test_kept_jobs_log_their_output_and_are_left_out_of_stop_all(tmp_path):
    pool = jobs.Jobs()
    server = pool.start(["bash", "-c", "echo first; sleep 60"], tmp_path, "server", log=tmp_path / "logs" / "s.log")
    watcher = pool.start(["bash", "-c", "sleep 60"], tmp_path, "watcher")
    try:
        assert server.kept and not watcher.kept and (tmp_path / "logs" / "s.log").read_text() == "first\n"
        assert "first" in server.new_output(1000) and jobs.describe(server).endswith(", kept): server")
        assert pool.stop_all() == 1 and server.running and not watcher.running
        assert pool.kept() == [server]
    finally:
        assert pool.stop_all(kept=True) == 1
    assert not server.running and pool.kept() == []
    quick = pool.start(["bash", "-c", "echo last words"], tmp_path, "quick", log=tmp_path / "quick.log")
    assert wait_for(lambda: quick.ended) and "last words" in quick.output  # read after it ended, too
    split = "printf '\\xc3'; sleep 0.3; printf '\\xa9t\\xc3\\xa9\\n'"  # "été", its first "é" in two writes
    halves = pool.start(["bash", "-c", split], tmp_path, "halves", log=tmp_path / "halves.log")
    assert wait_for(lambda: halves.ended) and halves.output == "été\n"


def test_a_kept_job_outlives_the_session(make_agent, repo):
    """A service the user asked to leave running (keep=true) writes to its own log, not to lcode, and keeps running
    when the session ends: lcode says how to stop it."""
    (repo / "tick.sh").write_text("i=0; while true; do echo tick $i; i=$((i+1)); sleep 0.05; done\n")
    agent = make_agent(
        [
            reply(tool_calls=[call("bash", command="bash tick.sh", background=True, keep=True)]),
            reply(tool_calls=[call("bash_output", id="1")]),
            reply("It's running, and stays up."),
        ]
    )
    agent.run_turn("start the ticker and leave it running")
    started, read = [m["content"] for m in agent.messages if m["role"] == "tool"]
    job = agent.jobs.get("1")
    try:
        assert job.kept and job.log is not None and job.log.parent == config.STATE_DIR / "jobs"
        assert "and after lcode exits" in started and str(job.log) in started
        assert read.startswith("[job 1 (running, ") and ", kept): bash tick.sh" in read and "tick" in read
        assert "(in the background, kept after lcode exits)" in output(agent)
        api.close_agent(agent)
        text = " ".join(output(agent).split())
        assert job.running and "Stopped" not in text
        assert "Left job 1 running, as asked: bash tick.sh" in text and f"kill -- -{job.proc.pid}" in text
        size = job.log.stat().st_size
        assert wait_for(lambda: job.log.stat().st_size > size)  # still ticking
    finally:
        agent.jobs.stop_all(kept=True)
    assert not job.running


def test_keep_alone_starts_a_kept_background_job(make_agent, repo):
    agent = make_agent()
    result = agent.tools.run("bash", {"command": "sleep 30", "keep": "true"})
    try:
        assert result.startswith("Started background job 1: it keeps running while you work, and after lcode exits")
        assert agent.jobs.get("1").kept
    finally:
        agent.jobs.stop_all(kept=True)


@pytest.mark.parametrize(
    ("command", "runs"),
    [
        ("serve &", "serve"),
        ("serve  &  \n", "serve"),
        ("cd web && serve &", "cd web && serve"),
        ("a && b", "a && b"),
        ('echo "a &"', 'echo "a &"'),
        ("echo a \\&", "echo a \\&"),
        ("serve", "serve"),
    ],
)
def test_only_a_last_ampersand_comes_off_a_background_command(command, runs):
    assert TRAILING_AMPERSAND.sub("", command) == runs


@pytest.mark.parametrize("keep", [False, True])
def test_a_background_command_with_an_ampersand_is_a_job_all_the_same(make_agent, repo, keep):
    """`cmd &` would end the shell at once, and leave cmd running where lcode can't see or stop it."""
    agent = make_agent()
    result = agent.tools.run("bash", {"command": f"{TICKER} &", "background": True, "keep": keep})
    job = agent.jobs.get("1")
    try:
        assert result.startswith("Started background job 1") and job.running
        assert "&" not in job.command and job.kept == keep
    finally:
        agent.jobs.stop_all(kept=True)
    assert not job.running and gone(job.proc.pid)


@pytest.mark.parametrize("keep", [False, True])
def test_what_a_background_command_leaves_behind_is_stopped(make_agent, repo, keep):
    """The shell ended, so there's no job to see or stop: it doesn't stay behind unnoticed, and the model hears why."""
    agent = make_agent()
    command = f"{TICKER} > ticks.log 2>&1 & echo started"
    result = agent.tools.run("bash", {"command": command, "background": True, "keep": keep})
    assert "ended right away" in result and "started" in result
    assert "lcode stopped what this command left running" in result and not agent.jobs.running()
    size = (repo / "ticks.log").stat().st_size
    time.sleep(0.3)
    assert (repo / "ticks.log").stat().st_size == size  # it really stopped


def test_a_background_command_that_just_ends_says_only_that(make_agent, repo):
    result = make_agent().tools.run("bash", {"command": "echo done", "background": True})
    assert "ended right away (exited with code 0)" in result and "done" in result
    assert "lcode stopped" not in result


def test_a_kept_job_says_so_when_it_asks(make_agent, repo):
    """The question says the command outlives lcode, and "always" for the program doesn't cover a kept job."""
    agent = make_agent(mode="ask")
    asked = []
    agent.perms.approve = lambda request: asked.append(request) or True
    agent.perms.always.add(bash_key("sleep 30"))
    try:
        agent.tools.run("bash", {"command": "sleep 30", "background": True})  # allowed for the session: no question
        assert not asked and not agent.jobs.get("1").kept
        result = agent.tools.run("bash", {"command": "sleep 30", "background": True, "keep": True})
        assert result.startswith("Started background job 2") and agent.jobs.get("2").kept
        assert [r["title"] for r in asked] == [f"Run command and leave it running after lcode exits (in {repo})"]
    finally:
        agent.jobs.stop_all(kept=True)


def test_a_command_that_only_reads_asks_before_it_is_kept(make_agent, repo):
    agent = make_agent(mode="ask")
    asked = []
    agent.perms.approve = lambda request: asked.append(request) or False  # refuses
    assert "exit code: 0" in agent.tools.run("bash", {"command": "ls"})  # read-only: no question
    assert not asked
    agent.tools.run("bash", {"command": "ls", "keep": True})
    assert len(asked) == 1 and not agent.jobs.items


def test_plan_mode_does_not_keep_a_command(make_agent, repo):
    agent = make_agent(mode="plan")
    assert "exit code: 0" in agent.tools.run("bash", {"command": "ls"})
    assert agent.tools.run("bash", {"command": "ls", "keep": True}).startswith("Error:")
    assert not agent.jobs.items


def test_what_a_command_leaves_running_is_stopped(make_agent, repo):
    agent = make_agent(
        [
            reply(tool_calls=[call("bash", command=f"{TICKER} > ticks.log &\necho started")]),
            reply(tool_calls=[call("bash", command="sleep 0.2 & sleep 0.1 & wait; echo both done")]),
            reply("ok"),
        ]
    )
    agent.run_turn("start a ticker")
    first, second = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "started" in first and "lcode stopped what this command left running" in first
    assert "background=true" in first and "keep=true for a service the user wants left running" in first
    size = (repo / "ticks.log").stat().st_size
    time.sleep(0.3)
    assert (repo / "ticks.log").stat().st_size == size  # it really stopped
    assert "both done" in second and "lcode stopped" not in second


def test_plan_mode_allows_reading_but_not_starting(make_agent, repo):
    agent = make_agent(
        [
            reply(tool_calls=[call("bash", command="python -m http.server", background=True)]),
            reply(tool_calls=[call("bash_output", id="1")]),
            reply("ok"),
        ],
        mode="plan",
    )
    agent.run_turn("start a server")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "Plan mode is on" in results[0] and "there's no background job 1" in results[1]


# ----------------------------------------------------------------------------- notifications


def test_notification_commands(monkeypatch, capsys):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    assert notify.command("lcode", "done") == ["notify-send", "--app-name=lcode", "lcode", "done"]
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    assert notify.command("lcode", 'say "hi"') == [
        "osascript", "-e", 'display notification "say \\"hi\\"" with title "lcode"']  # fmt: skip
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert notify.command("lcode", "done") is None
    notify.desktop("lcode", "done")
    assert capsys.readouterr().err == "\a"  # the terminal bell instead


def test_long_requests_and_waits_notify(make_agent, monkeypatch):
    shown = []
    monkeypatch.setattr(notify, "desktop", lambda title, message: shown.append((title, message)))
    agent = make_agent(notify=True, notify_after=30)
    agent.interactive = True
    agent.finished("fix the parser", 12)
    assert shown == []
    agent.finished("fix the parser", 95)
    assert shown == [("lcode is done", "fix the parser (95s)")]
    agent.turn_started = time.monotonic()
    agent.waiting("Run command")
    assert len(shown) == 1  # the user is probably still watching
    agent.turn_started = time.monotonic() - 60
    agent.waiting("Run command")
    assert shown[-1] == ("lcode needs you", "Run command")
    agent.interactive = False  # lcode -p, scripts, editors
    agent.finished("fix the parser", 95)
    agent.settings.notify, agent.interactive = False, True
    agent.finished("fix the parser", 95)
    assert len(shown) == 2


def test_a_plan_waiting_for_approval_notifies(make_agent, monkeypatch):
    seen = []
    agent = make_agent(mode="plan")
    agent.interactive = True
    agent.perms.on_prompt = seen.append
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    planning.present(agent, "Rename", "1. Rename it")
    assert seen == ["Plan: Rename"]
