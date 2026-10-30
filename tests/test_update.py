import io
import json
import shlex
import subprocess
import sys

import pytest
from rich.console import Console

from lcode import __version__, api, cli, config, update
from lcode.hardware import Hardware
from lcode.ollama import OllamaError
from lcode.update import Container, Install, UpdateError

COMPOSE = """\
name: lab
services:
  db:
    image: postgres:16
  ollama:
    # the model server
    image: "ollama/ollama:latest"   # keep in sync
    container_name: lab-ollama
    ports:
      - "11434:11434"
  web:
    build: .
volumes:
  models: {}
"""


def hub(*names, next_page=None):
    return {"results": [{"name": n} for n in names], "next": next_page}


def inspect(name="lab-ollama", image="ollama/ollama:latest", labels=None, network="lab_default"):
    return {
        "Id": "ce403d03b71e" + "0" * 52,
        "Name": f"/{name}",
        "Image": "sha256:" + "3886" * 16,
        "Config": {
            "Image": image,
            "Env": ["OLLAMA_CONTEXT_LENGTH=32768", "OLLAMA_HOST=0.0.0.0:11434", "PATH=/usr/bin"],
            "Cmd": ["serve"],
            "Entrypoint": ["/bin/ollama"],
            "Labels": {"org.opencontainers.image.version": "24.04", "team": "ml", **(labels or {})},
        },
        "HostConfig": {
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "PortBindings": {"11434/tcp": [{"HostIp": "", "HostPort": "11434"}]},
            "DeviceRequests": [{"Driver": "", "Count": -1, "DeviceIDs": None, "Capabilities": [["gpu"]]}],
            "Runtime": "runc",
            "NetworkMode": network,
        },
        "NetworkSettings": {"Networks": {network: {"Aliases": [name, "ollama", "ce403d03b71e"]}}},
        "Mounts": [{"Type": "volume", "Name": "models", "Destination": "/root/.ollama", "RW": True}],
    }


IMAGE_INFO = {
    "Config": {
        "Env": ["PATH=/usr/bin", "OLLAMA_HOST=0.0.0.0:11434"],
        "Cmd": ["serve"],
        "Entrypoint": ["/bin/ollama"],
        "Labels": {"org.opencontainers.image.version": "24.04"},
    }
}


# ----------------------------------------------------------------------------- versions


def test_the_newest_release_comes_from_docker_hub(monkeypatch):
    pages = {
        update.HUB_TAGS: hub(
            "0.40.0-rc5-rocm", "0.40.0-rc5", "rocm", "latest", "0.35.1-rocm", "0.35.1", next_page="p2"
        ),
        "p2": hub("0.35.1-rc2", "0.35.0", "0.34.4", "0.9.10"),
    }
    monkeypatch.setattr(update, "get_json", lambda url, **params: pages[url])
    assert update.latest_ollama() == "0.35.1"  # not the release candidates, not "latest"
    assert update.latest_ollama("rocm") == "0.35.1-rocm"
    pages[update.HUB_TAGS] = hub("latest", "rocm")
    pages["p2"] = hub()
    with pytest.raises(UpdateError, match="lists no released ollama/ollama versions"):
        update.latest_ollama()
    assert update.version_key("0.35.1") > update.version_key("0.9.10")
    assert (
        update.image_flavor("ollama/ollama:rocm") == "rocm"
        and update.image_flavor("ollama/ollama:0.35.1-rocm") == "rocm"
    )
    assert update.image_flavor("ollama/ollama") == "" and update.image_flavor("ollama/ollama:latest") == ""


def test_how_lcode_was_installed(tmp_path):
    editable = json.dumps({"url": "file:///home/me/lcode", "dir_info": {"editable": True}})
    assert update.lcode_install(tmp_path, editable).path.as_posix() == "/home/me/lcode"
    (tmp_path / "uv-receipt.toml").write_text("[tool]\n")
    assert update.lcode_install(tmp_path, "").command == ["uv", "tool", "upgrade", "lcode-cli"]
    brew = tmp_path / "Cellar" / "lcode" / "0.20.0" / "libexec"
    brew.mkdir(parents=True)
    assert update.lcode_install(brew, "").kind == "brew"
    pipx = tmp_path / "pipx" / "venvs" / "lcode-cli"
    pipx.mkdir(parents=True)
    assert update.lcode_install(pipx, "").command == ["pipx", "upgrade", "lcode-cli"]
    uvx = tmp_path / ".cache" / "uv" / "archive-v0" / "abc"
    uvx.mkdir(parents=True)
    assert update.lcode_install(uvx, "").kind == "uvx" and update.lcode_install(uvx, "").command is None
    plain = tmp_path / "venv"
    plain.mkdir()
    assert update.lcode_install(plain, "").command[-3:] == ["install", "--upgrade", "lcode-cli"]


# ----------------------------------------------------------------------------- Docker Compose


def test_the_compose_file_gets_the_version_tag():
    text, old = update.set_compose_image(COMPOSE, "ollama", "ollama/ollama:0.35.1")
    assert old == "ollama/ollama:latest"
    assert '    image: "ollama/ollama:0.35.1"   # keep in sync\n' in text
    assert text.replace('"ollama/ollama:0.35.1"', '"ollama/ollama:latest"') == COMPOSE  # nothing else changed
    with pytest.raises(UpdateError, match="no service llama in it"):
        update.set_compose_image(COMPOSE, "llama", "x")
    with pytest.raises(UpdateError, match="service web has no image: line"):
        update.set_compose_image(COMPOSE, "web", "x")
    variable = COMPOSE.replace('"ollama/ollama:latest"', "ollama/ollama:${OLLAMA_VERSION:-latest}")
    with pytest.raises(UpdateError, match="the image comes from a variable"):
        update.set_compose_image(variable, "ollama", "ollama/ollama:0.35.1")


def compose_container(tmp_path):
    path = tmp_path / "compose.yaml"
    path.write_text(COMPOSE)
    labels = {
        "com.docker.compose.project": "lab",
        "com.docker.compose.service": "ollama",
        "com.docker.compose.project.config_files": str(path),
        "com.docker.compose.project.working_dir": str(tmp_path),
    }
    return Container("ce403d03b71e", "lab-ollama", "ollama/ollama:latest", inspect(labels=labels)), path


@pytest.fixture
def docker(monkeypatch):
    """Ollama 0.32.15 in Docker, 0.35.1 on Docker Hub, and a record of the commands lcode runs."""
    calls = {"run": [], "stream": [], "stream_codes": {}}

    def fake_run(argv, cwd=None, timeout=120):
        calls["run"].append(argv)
        out = ""
        if argv[:3] == ["docker", "image", "inspect"]:
            out = json.dumps([IMAGE_INFO]) if "--format" not in argv else "3500000000"
        return subprocess.CompletedProcess(argv, 0, out, "")

    def fake_stream(argv, cwd=None):
        calls["stream"].append(argv)
        return calls["stream_codes"].get(argv[1], 0)

    monkeypatch.setattr(update, "run", fake_run)
    monkeypatch.setattr(update, "stream", fake_stream)
    monkeypatch.setattr(update, "docker_available", lambda: True)
    monkeypatch.setattr(update, "latest_ollama", lambda flavor="": "0.35.1")
    monkeypatch.setattr(update, "wait_for_version", lambda host, wanted, timeout=0: True)
    monkeypatch.setattr("lcode.ollama.Ollama.version", lambda self: "0.32.15")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    return calls


def output_of(console):
    return console.file.getvalue()


def test_a_compose_container_is_updated_through_its_compose_file(docker, tmp_path, monkeypatch):
    container, path = compose_container(tmp_path)
    monkeypatch.setattr(update, "find_container", lambda host: container)
    console = Console(file=io.StringIO(), width=2000)
    assert update.command(console, "ollama", yes=True) == 0
    assert docker["stream"][0] == ["docker", "pull", "ollama/ollama:0.35.1"]
    assert docker["stream"][1][:4] == ["docker", "compose", "-p", "lab"]
    assert docker["stream"][1][-4:] == ["up", "-d", "--no-deps", "ollama"]
    assert 'image: "ollama/ollama:0.35.1"' in path.read_text()
    text = output_of(console)
    assert f"in {path}, service ollama: image ollama/ollama:latest → ollama/ollama:0.35.1" in text
    assert "✓ Ollama 0.32.15 → 0.35.1, running as ollama/ollama:0.35.1." in text
    assert "The previous image is still on disk (3.5 GB). To remove it: docker image rm 388638863886" in text


def test_a_failed_compose_update_puts_the_file_back(docker, tmp_path, monkeypatch):
    container, path = compose_container(tmp_path)
    monkeypatch.setattr(update, "find_container", lambda host: container)
    docker["stream_codes"]["compose"] = 1
    console = Console(file=io.StringIO(), width=2000)
    assert update.command(console, "ollama", yes=True) == 1
    assert path.read_text() == COMPOSE and "✗ docker compose up failed" in output_of(console)


def test_check_and_no_terminal_change_nothing(docker, tmp_path, monkeypatch):
    container, path = compose_container(tmp_path)
    monkeypatch.setattr(update, "find_container", lambda host: container)
    console = Console(file=io.StringIO(), width=2000)
    assert update.command(console, "ollama", check=True) == 0
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert update.command(console, "ollama") == 0
    assert docker["stream"] == [] and path.read_text() == COMPOSE
    assert "Not asking without a terminal: run lcode update -y" in output_of(console)


# ----------------------------------------------------------------------------- a plain container


def test_the_same_settings_for_the_new_container():
    args = update.run_args(inspect(), IMAGE_INFO, "ollama/ollama:0.35.1")
    assert args == [
        "docker", "run", "-d", "--name", "lab-ollama", "--restart", "unless-stopped",
        "-p", "11434:11434", "-v", "models:/root/.ollama", "-e", "OLLAMA_CONTEXT_LENGTH=32768",
        "--gpus", "all", "--network", "lab_default", "--network-alias", "ollama",
        "--label", "team=ml", "ollama/ollama:0.35.1",
    ]  # fmt: skip
    info = inspect(network="default")
    info["HostConfig"]["DeviceRequests"][0].update(Count=0, DeviceIDs=["1"])
    info["HostConfig"]["PortBindings"]["11434/tcp"][0]["HostIp"] = "127.0.0.1"
    info["Config"]["Cmd"] = ["serve", "--verbose"]
    args = update.run_args(info, IMAGE_INFO, "ollama/ollama:0.35.1")
    assert "--network" not in args and args[args.index("--gpus") + 1] == "device=1"
    assert "127.0.0.1:11434:11434" in args and args[-3:] == ["ollama/ollama:0.35.1", "serve", "--verbose"]


def test_a_plain_container_is_recreated_and_put_back_if_needed(docker, monkeypatch):
    container = Container("ce403d03b71e", "my-ollama", "ollama/ollama:latest", inspect(name="my-ollama"))
    monkeypatch.setattr(update, "find_container", lambda host: container)
    console = Console(file=io.StringIO(), width=2000)
    assert update.command(console, "ollama", yes=True) == 0
    commands = [c[:3] for c in docker["run"] if c[0] == "docker"]
    assert ["docker", "stop", "my-ollama"] in commands
    assert ["docker", "rename", "my-ollama"] in commands and [
        "docker",
        "rm",
        "my-ollama-before-lcode-update",
    ] in commands
    created = next(c for c in docker["run"] if c[:2] == ["docker", "run"])
    assert created[created.index("--name") + 1] == "my-ollama" and "ollama/ollama:0.35.1" in created
    docker["run"].clear()
    monkeypatch.setattr(update, "wait_for_version", lambda host, wanted, timeout=0: False)
    assert update.command(console, "ollama", yes=True) == 1
    assert docker["run"][-3:] == [
        ["docker", "rm", "-f", "my-ollama"],
        ["docker", "rename", "my-ollama-before-lcode-update", "my-ollama"],
        ["docker", "start", "my-ollama"],
    ]
    assert "the old one is running again" in output_of(console)


def test_up_to_date_and_pinned(docker, monkeypatch):
    container = Container("ce403d03b71e", "my-ollama", "ollama/ollama:0.35.1", inspect(image="ollama/ollama:0.35.1"))
    monkeypatch.setattr(update, "find_container", lambda host: container)
    monkeypatch.setattr("lcode.ollama.Ollama.version", lambda self: "0.35.1")
    console = Console(file=io.StringIO(), width=2000)
    assert update.command(console, "ollama", yes=True) == 0
    assert docker["stream"] == [] and "✓ Ollama is up to date, on ollama/ollama:0.35.1." in output_of(console)


def test_ollama_outside_docker_gets_instructions(docker, monkeypatch):
    monkeypatch.setattr(update, "find_container", lambda host: None)
    monkeypatch.setattr("lcode.hardware.detect", lambda: Hardware("linux", "x", 31, "RTX 4080 Laptop", 12))
    console = Console(file=io.StringIO(), width=2000)
    assert update.command(console, "ollama", yes=True) == 0
    assert "Ollama 0.32.15 → 0.35.1 is out" in output_of(console) and docker["stream"] == []
    assert "curl -fsSL https://ollama.com/install.sh | sh" in output_of(console)
    monkeypatch.setattr("lcode.hardware.detect", lambda: Hardware("macos", "Apple M4 Max", 64, "Apple GPU", None, True))
    console = Console(file=io.StringIO(), width=2000)
    assert update.command(console, "ollama", yes=True) == 0
    assert "update it with:\n  brew upgrade ollama" in output_of(console) and "curl" not in output_of(console)


# ----------------------------------------------------------------------------- when Ollama doesn't answer


def docker_with(monkeypatch, *infos, fails=""):
    """Docker with these containers (as `docker inspect` describes them), or whose `docker ps` fails with `fails`."""
    calls = []

    def fake_run(argv, cwd=None, timeout=120):
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            if fails:
                return subprocess.CompletedProcess(argv, 1, "", f"{fails}\n")
            return subprocess.CompletedProcess(argv, 0, "".join(f"{i['Id'][:12]}\n" for i in infos), "")
        assert argv[:2] == ["docker", "inspect"], argv
        return subprocess.CompletedProcess(argv, 0, json.dumps(list(infos)), "")

    monkeypatch.setattr(update, "run", fake_run)
    monkeypatch.setattr(update, "docker_installed", lambda: True)
    return calls


def test_a_stopped_compose_container_is_started_through_its_compose_file(monkeypatch, tmp_path):
    container, path = compose_container(tmp_path)
    calls = docker_with(monkeypatch, container.info)
    assert api.ollama_down("http://localhost:11434", "linux") == (
        "Ollama runs in Docker, in the container lab-ollama, which is stopped. Start it with:\n"
        f"  docker compose -p lab --project-directory {shlex.quote(str(tmp_path))} -f {shlex.quote(str(path))} "
        "up -d --no-deps ollama"
    )
    assert calls[0] == ["docker", "ps", "-aq"]  # stopped containers too
    path.unlink()  # the compose file moved: Docker can still start the container itself
    assert api.ollama_down("http://localhost:11434", "linux").endswith("Start it with:\n  docker start lab-ollama")


def test_a_stopped_container_is_started_and_a_running_one_is_looked_into(monkeypatch):
    stopped = inspect()
    docker_with(monkeypatch, stopped)
    assert api.ollama_down("http://127.0.0.1:11434", "linux") == (
        "Ollama runs in Docker, in the container lab-ollama, which is stopped. Start it with:\n"
        "  docker start lab-ollama"
    )
    running = {**inspect(), "State": {"Status": "running", "Running": True}}
    docker_with(monkeypatch, running)
    assert api.ollama_down("http://localhost:11434", "linux") == (
        "Ollama runs in Docker, in the container lab-ollama, but doesn't answer. If it has just started, try again "
        "in a moment; if not, see why:\n  docker logs --tail 20 lab-ollama"
    )
    docker_with(monkeypatch, stopped, inspect(name="old-ollama"))
    assert api.ollama_down("http://localhost:11434", "linux") == (
        "Ollama runs in Docker here, in one of these containers: lab-ollama, old-ollama. Start it with:\n"
        "  docker start <name>"
    )


def test_without_an_ollama_container_lcode_says_how_to_get_one(monkeypatch):
    other_port = inspect()
    other_port["HostConfig"]["PortBindings"]["11434/tcp"][0]["HostPort"] = "11435"
    docker_with(monkeypatch, inspect(name="db", image="postgres:16"), other_port)
    assert api.ollama_down("http://localhost:11434", "linux") == (
        "Is Ollama installed and running? Install it with:\n"
        "  curl -fsSL https://ollama.com/install.sh | sh\n"
        "If you ran Ollama in Docker, its container is gone: create it again (with Docker Compose: "
        "docker compose up -d, in the folder of its compose file)."
    )
    docker_with(monkeypatch, fails="Cannot connect to the Docker daemon at unix:///var/run/docker.sock.")
    assert api.ollama_down("http://localhost:11434", "linux").endswith(
        "\nIf you run Ollama in Docker, start Docker first. It says: Cannot connect to the Docker daemon at "
        "unix:///var/run/docker.sock."
    )


def test_without_docker_or_for_another_machine_lcode_says_how_to_install_ollama(monkeypatch):
    calls = docker_with(monkeypatch, inspect())
    assert api.ollama_down("http://gpu-box:11434", "linux") == (
        "Is Ollama installed and running? Install it with:\n  curl -fsSL https://ollama.com/install.sh | sh"
    )
    monkeypatch.setattr(update, "docker_installed", lambda: False)
    assert api.ollama_down("http://localhost:11434", "macos") == (
        "Is Ollama installed and running? Install it with:\n"
        "  brew install ollama && brew services start ollama   (or download the app from https://ollama.com/download)"
    )
    assert calls == []


class Down:
    host = "http://localhost:11434"

    def version(self):
        raise OllamaError("cannot reach Ollama at http://localhost:11434: Connection refused")


def test_lcode_and_its_doctor_say_how_to_start_the_container(monkeypatch, tmp_path):
    docker_with(monkeypatch, inspect())
    hw = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)
    with pytest.raises(api.SetupError) as error:
        api.check_server(Down(), hw)
    assert str(error.value) == (
        "cannot reach Ollama at http://localhost:11434: Connection refused\n\n"
        "Ollama runs in Docker, in the container lab-ollama, which is stopped. Start it with:\n"
        "  docker start lab-ollama"
    )
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(cli, "Ollama", lambda host: Down())
    monkeypatch.setattr(cli, "detect", lambda: hw)
    monkeypatch.setattr(cli, "console", Console(file=io.StringIO(), width=2000, highlight=False))
    with pytest.raises(SystemExit):
        cli.main(["doctor"])
    lines = cli.console.file.getvalue().splitlines()
    assert lines[-3:] == [
        "✗ Ollama     cannot reach Ollama at http://localhost:11434: Connection refused",
        "!            Ollama runs in Docker, in the container lab-ollama, which is stopped. Start it with:",
        "!            docker start lab-ollama",
    ]


# ----------------------------------------------------------------------------- lcode itself


class Replaced(Console):
    """A console whose rich is gone once a command replaces lcode's own environment, as when uv makes a new one: rich
    imports some modules only when first needed. From then on lcode can only write what it rendered before."""

    gone = False

    def print(self, *objects, **kwargs):
        assert not self.gone, "rich used after lcode's environment was replaced"
        super().print(*objects, **kwargs)


def replacing(monkeypatch, console, code=0):
    """Commands lcode streams; a uv, brew, pipx or pip one replaces its environment and ends with `code`."""
    ran, real = [], update.stream

    def stream(argv, cwd=None):
        if argv[0] == "git":
            return real(argv, cwd)
        ran.append(argv)
        console.gone = True
        return code

    monkeypatch.setattr(update, "stream", stream)
    return ran


def test_lcode_updates_the_way_it_was_installed(docker, monkeypatch):
    monkeypatch.setattr(update, "lcode_install", lambda: Install("uv", ["uv", "tool", "upgrade", "lcode-cli"]))
    monkeypatch.setattr(update, "latest_lcode", lambda: "99.0.0")
    console = Replaced(file=io.StringIO(), width=2000)
    ran = replacing(monkeypatch, console)
    assert update.command(console, "lcode", yes=True) == 0
    assert ran == [["uv", "tool", "upgrade", "lcode-cli"]]
    assert output_of(console).endswith("✓ lcode updated; the new version runs from the next `lcode`.\n\n")
    console.gone = False
    ran = replacing(monkeypatch, console, code=1)
    assert update.command(console, "lcode", yes=True) == 1
    assert output_of(console).endswith("✗ uv tool upgrade lcode-cli failed\n\n")
    console.gone = False
    monkeypatch.setattr(update, "latest_lcode", lambda: __version__)
    update.command(console, "lcode", yes=True)
    assert f"lcode {__version__} is the newest version (uv tool)" in output_of(console)
    order = []
    monkeypatch.setattr(update, "update_ollama", lambda console, check, ask: order.append("ollama"))
    monkeypatch.setattr(update, "update_lcode", lambda console, check, ask: order.append("lcode"))
    update.command(console, yes=True)
    assert order == ["ollama", "lcode"]  # lcode last: Ollama's update needs rich and requests


def test_a_checkout_is_pulled(tmp_path, monkeypatch):
    def git(cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (tmp_path / "gitconfig").write_text("[user]\n\tname = T\n\temail = t@example.com\n")
    origin, clone = tmp_path / "origin.git", tmp_path / "lcode"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    (clone / "a.txt").write_text("1\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "one")
    git(clone, "push", "-q", "origin", "main")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(origin), str(other))
    (other / "a.txt").write_text("2\n")
    git(other, "commit", "-qam", "two")
    git(other, "push", "-q")
    monkeypatch.setattr(update, "lcode_install", lambda: Install("checkout", path=clone))
    console = Console(file=io.StringIO(), width=2000)
    assert update.command(console, "lcode", check=True) == 0
    assert "1 new commit(s) on origin/main" in output_of(console) and (clone / "a.txt").read_text() == "1\n"
    assert update.command(console, "lcode", yes=True) == 0
    assert (clone / "a.txt").read_text() == "2\n" and "lcode updated: 1 new commit(s)." in output_of(console)
    (clone / "pyproject.toml").write_text('version = "2"\n')  # new dependencies, say: its environment is reinstalled
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "three")
    git(clone, "push", "-q")
    git(clone, "reset", "-q", "--hard", "HEAD~1")
    (tmp_path / "uv-receipt.toml").write_text("[tool]\n")
    monkeypatch.setattr(update.sys, "prefix", str(tmp_path))
    console = Replaced(file=io.StringIO(), width=2000)
    ran = replacing(monkeypatch, console)
    assert update.command(console, "lcode", yes=True) == 0
    python = f"{sys.version_info.major}.{sys.version_info.minor}"  # the one it runs on, not one uv finds nearby
    assert ran == [["uv", "tool", "install", "--force", "--python", python, "--editable", str(clone)]]
    assert output_of(console).endswith("✓ lcode updated: 1 new commit(s).\n\n")
