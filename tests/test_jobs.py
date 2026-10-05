import shlex
import sys
import time
from pathlib import Path

import pytest

from conftest import call, output, reply
from lcode import api, jobs, notify, planning
from lcode.hardware import Hardware
from lcode.repl import handle_command

HW = Hardware("linux", "x", 31, "GPU", 12)
TICKER = f"{shlex.quote(sys.executable)} -u -c " + shlex.quote(
    "import time\nfor i in range(600):\n    print('tick', i)\n    time.sleep(0.05)"
)


def gone(pid: int) -> bool:
    """The process ended (or is a zombie waiting for its parent)."""
    status = Path(f"/proc/{pid}/status")  # Linux; elsewhere there's nothing to check
    return not status.exists() or "zombie" in status.read_text().lower()


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
    assert "background=true" in first
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
