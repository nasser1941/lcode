import os
import subprocess
import time

import pytest

from conftest import call, output, reply
from lcode import config, sandbox
from lcode.sandbox import Sandbox, SandboxError

IMAGE = "python:3.12-slim"  # small, has bash and setsid; the default image takes minutes to build
docker = pytest.mark.skipif(sandbox.engine_problem("docker") is not None, reason="Docker isn't available")


def test_settings():
    assert config.coerce("sandbox", "docker") == "docker"
    assert config.coerce("sandbox", "none") == "off"
    with pytest.raises(config.ConfigError):
        config.coerce("sandbox", "vmware")
    with pytest.raises(SandboxError, match="unknown sandbox"):
        Sandbox("vmware")


def test_missing_engine_explains_the_fix(monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    assert "docker isn't installed" in sandbox.engine_problem("docker")


def test_file_tools_stay_inside_the_project(make_agent, repo):
    agent = make_agent(sandbox="docker")
    assert agent.sandbox_root() == repo
    assert agent.tools.run("read_file", {"path": "/etc/hostname"}).startswith(
        "Error: /etc/hostname is outside the project"
    )
    assert agent.tools.run("list_dir", {"path": ".."}).startswith("Error:")
    assert "def add" in agent.tools.run("read_file", {"path": "src/pkg/math.py"})
    plain = make_agent()
    assert plain.sandbox_root() is None


def fake_container(monkeypatch):
    """Run "sandboxed" commands with the local bash, to test lcode's side without Docker."""
    monkeypatch.setattr(Sandbox, "ensure", lambda self, cwd: setattr(self, "root", cwd))
    monkeypatch.setattr(Sandbox, "exec_argv", lambda self, cwd, script: (["bash", "-c", script], "t"))
    monkeypatch.setattr(Sandbox, "kill", lambda self, token: None)


def test_auto_edit_runs_sandboxed_commands_without_asking(make_agent, monkeypatch):
    fake_container(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("should not ask"))
    agent = make_agent(
        [reply(tool_calls=[call("bash", command="echo built > out.txt")]), reply("ok")],
        mode="auto-edit",
        sandbox="docker",
    )
    agent.run_turn("build it")
    assert (agent.cwd / "out.txt").read_text() == "built\n"


def test_commands_still_ask_without_the_sandbox_in_auto_edit(make_agent, monkeypatch):
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "n")
    agent = make_agent([reply(tool_calls=[call("bash", command="echo hi > out.txt")]), reply("ok")], mode="auto-edit")
    agent.run_turn("do it")
    assert asked and not (agent.cwd / "out.txt").exists()


def test_network_errors_get_a_hint(make_agent, monkeypatch):
    fake_container(monkeypatch)
    agent = make_agent(sandbox="docker")
    result = agent.tools.run("bash", {"command": "echo 'curl: (6) Could not resolve host: pypi.org'; exit 6"})
    assert "[exit code: 6]" in result and "/sandbox network on" in result


def test_a_sandbox_that_cant_start_runs_nothing(make_agent, monkeypatch):
    def broken(self, cwd):
        raise SandboxError("docker isn't running")

    monkeypatch.setattr(Sandbox, "ensure", broken)
    agent = make_agent(sandbox="docker")
    result = agent.tools.run("bash", {"command": "touch nope.txt"})
    assert result.startswith("Error: the sandbox can't start") and not (agent.cwd / "nope.txt").exists()


def test_the_home_folder_is_never_mounted(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox.Path, "home", lambda: tmp_path)
    with pytest.raises(SandboxError, match="won't mount"):
        sandbox.project_root(tmp_path)


# ----------------------------------------------------------------------------- with Docker


@pytest.fixture
def sandboxed(make_agent, tmp_path):
    agent = make_agent(sandbox="docker", sandbox_image=IMAGE)
    yield agent
    agent.sandbox.stop()


def bash(agent, command: str, timeout: int = 60) -> str:
    return agent.tools.run("bash", {"command": command, "timeout": timeout})


@docker
def test_commands_only_see_the_project(sandboxed, repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("secret")
    assert "No such file" in bash(sandboxed, f"cat {outside}")
    assert "No such file" in bash(sandboxed, f"ls {os.path.expanduser('~')}") or os.path.expanduser("~") == "/root"
    assert "No such file" in bash(sandboxed, "ls /var/run/docker.sock")
    assert "def add" in bash(sandboxed, "cat src/pkg/math.py")  # the project is mounted at the same path


@docker
def test_no_network_unless_allowed(sandboxed):
    probe = "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\""
    result = bash(sandboxed, probe)
    assert "Network is unreachable" in result and "/sandbox network on" in result


@docker
def test_files_belong_to_the_user_and_cd_persists(sandboxed, repo):
    result = bash(sandboxed, "mkdir -p build && cd build && echo ok > made.txt && id -u")
    assert result.splitlines()[0] == str(os.getuid())
    assert (repo / "build" / "made.txt").stat().st_uid == os.getuid()
    assert sandboxed.cwd == repo / "build"
    assert bash(sandboxed, "pwd").splitlines()[0] == str(repo / "build")


@docker
def test_timeouts_stop_the_command_and_the_container_lives_on(sandboxed):
    started = time.monotonic()
    result = bash(sandboxed, "sleep 60 & sleep 60", timeout=2)
    assert "timed out after 2s" in result and time.monotonic() - started < 15
    count = "for f in /proc/[0-9]*/cmdline; do tr '\\0' ' ' < $f 2>/dev/null; echo; done | grep -c '^sleep 60 $'"
    assert bash(sandboxed, count).splitlines()[0] == "0"  # the command and what it started in the background
    assert "still here" in bash(sandboxed, "echo still here")


@docker
def test_containers_are_removed(make_agent):
    agent = make_agent(sandbox="docker", sandbox_image=IMAGE)
    bash(agent, "true")
    name = agent.sandbox.container
    agent.sandbox.stop()
    listed = subprocess.run(["docker", "ps", "-a", "--filter", f"name={name}", "-q"], capture_output=True, text=True)
    assert listed.stdout.strip() == ""

    orphan = subprocess.run(
        ["docker", "run", "-d", "--label", sandbox.LABEL, "--label", "dev.lcode.pid=999999", IMAGE, "sleep", "60"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    sandbox.remove_orphans("docker")  # its lcode session (pid 999999) is gone
    listed = subprocess.run(["docker", "ps", "-a", "--filter", f"id={orphan}", "-q"], capture_output=True, text=True)
    assert listed.stdout.strip() == ""


@docker
def test_the_session_shows_the_sandbox(sandboxed):
    from lcode.repl import handle_command

    bash(sandboxed, "true")
    handle_command(sandboxed, "/sandbox", None)
    assert "docker · python:3.12-slim · no network" in output(sandboxed)
