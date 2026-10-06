import io
import json
import subprocess

import pytest
from rich.console import Console

from lcode import __version__, update
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
    console = Console(file=io.StringIO(), width=2000)
    assert update.command(console, "ollama", yes=True) == 0
    assert "Ollama 0.32.15 → 0.35.1 is out" in output_of(console) and docker["stream"] == []


# ----------------------------------------------------------------------------- lcode itself


def test_lcode_updates_the_way_it_was_installed(docker, monkeypatch):
    monkeypatch.setattr(update, "lcode_install", lambda: Install("uv", ["uv", "tool", "upgrade", "lcode-cli"]))
    monkeypatch.setattr(update, "latest_lcode", lambda: "99.0.0")
    console = Console(file=io.StringIO(), width=2000)
    assert update.command(console, "lcode", yes=True) == 0
    assert docker["stream"] == [["uv", "tool", "upgrade", "lcode-cli"]]
    monkeypatch.setattr(update, "latest_lcode", lambda: __version__)
    update.command(console, "lcode", yes=True)
    assert f"lcode {__version__} is the newest version (uv tool)" in output_of(console)


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
