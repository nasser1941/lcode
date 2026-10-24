"""Image mode's engine: lcode drives ComfyUI (a stand-in here): queue, progress, cancel, outputs, recovery."""

import contextlib
import io
import json
import os
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

import pytest

from comfy_standin import FLUX, StandIn, tiny_png
from conftest import FakeOllama, output
from lcode import catalog, cli, comfy, config, media
from lcode.comfy import cli_workspace as real_cli_workspace  # bound before conftest replaces them for each test
from lcode.comfy import launch as real_launch
from lcode.hardware import Hardware
from lcode.repl import handle_command, respond

LAPTOP = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)


def text(agent):
    return " ".join(output(agent).split())


def squeezed(value: str) -> str:
    return "".join(value.split())


def chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    """A PNG's chunks, each checked against its CRC."""
    found, i = [], 8
    while i < len(png):
        (n,) = struct.unpack(">I", png[i : i + 4])
        kind, body = png[i + 4 : i + 8], png[i + 8 : i + 8 + n]
        (crc,) = struct.unpack(">I", png[i + 8 + n : i + 12 + n])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        found.append((kind, body))
        i += 12 + n
    return found


@pytest.fixture
def standin():
    with StandIn() as server:
        yield server


@pytest.fixture
def image_agent(make_agent, standin):
    agent = make_agent(comfyui_url=standin.url)
    agent.settings.mode = "image"
    return agent


# ----------------------------------------------------------------------------- a request becomes an image


def test_a_request_becomes_an_image_in_the_project(image_agent, standin, repo):
    made = media.generate_image(image_agent, "A red fox in fresh snow, at dusk")
    [path] = made.files
    assert path.parent == repo / ".lcode" / "media" and path.name.endswith("-a-red-fox-in-fresh-snow-at-dusk.png")
    assert (path.parent / ".gitignore").read_text().endswith("*\n")  # lcode's folder stays out of git
    graph = standin.prompts[0]
    assert graph["4"]["inputs"]["text"] == "A red fox in fresh snow, at dusk"
    assert graph["1"]["inputs"]["unet_name"] == "flux-2-klein-4b-fp8.safetensors"
    assert graph["10"]["inputs"]["noise_seed"] == made.seed and graph["8"]["inputs"]["steps"] == 4
    sidecar = json.loads(path.with_suffix(".json").read_text())
    assert sidecar["prompt"] == "A red fox in fresh snow, at dusk" and sidecar["seed"] == made.seed
    assert sidecar["model"] == "FLUX.2 klein 4B" and sidecar["file"] == path.name
    [embedded] = [body for kind, body in chunks(path.read_bytes()) if kind == b"iTXt"]
    assert embedded.startswith(b"lcode\0") and json.loads(embedded.split(b"\0", 5)[5])["seed"] == made.seed
    assert path.name in text(image_agent) and "FLUX.2 klein 4B" in text(image_agent)
    assert image_agent.ollama.unloaded == ["lcode-qwen3.6-35b"]  # the coding model gave the GPU up
    assert comfy._read_jobs() == {}  # nothing left to collect


def test_the_job_is_followed_on_the_websocket(standin):
    standin.ahead = 2
    events = []
    server = comfy.Server(standin.url)
    graph = comfy.fill(comfy.load_workflow("flux2-klein-4b-t2i").graph, {**stand_values(), "prompt": "a fox"})
    prompt_id, outputs = comfy.run(server, graph, events.append, lambda: False)
    kinds = [e.kind for e in events]
    assert kinds[0] == "queued" and events[0].prompt_id == prompt_id
    assert "waiting" in kinds and max(e.value for e in events if e.kind == "waiting") == 2
    assert [(e.value, e.total) for e in events if e.kind == "progress"] == [(1, 4), (2, 4), (3, 4), (4, 4)]
    assert {e.node for e in events if e.kind == "node"} >= {"1", "11", "13"}
    assert any(e.kind == "preview" and e.data.startswith(b"\x89PNG") for e in events)
    assert outputs["13"]["images"][0]["filename"] == "lcode/image_00001_.png"


def stand_values() -> dict:
    files = {"model": "f.safetensors", "text_encoder": "t.safetensors", "vae": "v.safetensors"}
    return {**files, "width": 512, "height": 512, "steps": 4, "batch": 1, "seed": 1}


def test_without_a_websocket_lcode_polls(standin):
    standin.websocket = False
    graph = comfy.fill(comfy.load_workflow("flux2-klein-4b-t2i").graph, {**stand_values(), "prompt": "a fox"})
    _, outputs = comfy.run(comfy.Server(standin.url), graph, lambda e: None, lambda: False)
    assert outputs["13"]["images"]


def test_esc_cancels_the_job_in_comfyui_too(image_agent, standin):
    standin.hold = True
    with pytest.raises(media.MediaError, match="Cancelled"):
        media.generate_image(image_agent, "a fox", cancelled=lambda: standin.running is not None)
    assert len(standin.cancelled) == 1 and comfy._read_jobs() == {}
    assert not list((image_agent.workspace / ".lcode" / "media").glob("*.png"))


def test_a_failed_job_says_which_node_and_why(image_agent, standin):
    standin.fail = "CUDA out of memory. Tried to allocate 2.00 GiB"
    with pytest.raises(media.MediaError, match="SamplerCustomAdvanced failed: CUDA out of memory"):
        media.generate_image(image_agent, "a fox")
    assert comfy._read_jobs() == {}


def test_a_refused_workflow_says_what_comfyui_objected_to(standin):
    standin.reject = True
    with pytest.raises(comfy.ComfyError, match=r"UNETLoader \(1\): Value not in list: unet_name: 'x' not in \[\]"):
        comfy.Server(standin.url).queue({}, "client")


def test_a_missing_model_is_named(make_agent):
    with StandIn(models={"vae": ["flux2-vae.safetensors"]}) as server:
        agent = make_agent(comfyui_url=server.url)
        with pytest.raises(media.MediaError) as e:
            media.generate_image(agent, "a fox")
    assert "FLUX.2 klein 4B isn't installed (12.1 GB to download): lcode media install flux2-klein-4b" in str(e.value)


def test_results_that_finished_while_lcode_was_away_are_collected(image_agent, standin, repo):
    graph = comfy.fill(comfy.load_workflow("flux2-klein-4b-t2i").graph, {**stand_values(), "prompt": "a cat"})
    prompt_id, _ = comfy.run(comfy.Server(standin.url), graph, lambda e: None, lambda: False)
    comfy.remember(prompt_id, standin.url, repo / "gallery", "earlier-a-cat", {"prompt": "a cat"})  # lcode crashed
    comfy.remember("gone", standin.url, repo / "gallery", "lost", {})  # ComfyUI restarted: no history for it
    jobs = comfy._read_jobs()
    jobs["gone"]["started"] -= 2 * 86400
    comfy._write_jobs(jobs)
    media.generate_image(image_agent, "a fox")
    assert (repo / "gallery" / "earlier-a-cat.png").exists()
    assert "gallery/earlier-a-cat.png: it finished while lcode wasn't watching" in text(image_agent)
    assert comfy._read_jobs() == {}


# ----------------------------------------------------------------------------- finding, starting, installing


def test_no_comfyui_says_how_to_get_it(make_agent):
    agent = make_agent()
    with pytest.raises(media.MediaError, match=r"no install found.*comfyui_path"):
        media.generate_image(agent, "a fox")
    asked = []
    with pytest.raises(media.MediaError, match="no install found"):
        media.generate_image(agent, "a fox", ask=lambda q: asked.append(q) or False)
    assert "Install it into" in asked[0] and "several GB" in asked[0]


class Proc:
    pid = 0

    def __init__(self):
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None


def test_lcode_starts_comfyui_and_stops_what_it_started(make_agent, monkeypatch, tmp_path, standin):
    install = tmp_path / "ComfyUI"
    (install / "comfy").mkdir(parents=True)
    (install / "main.py").write_text("")
    started, stopped = [], []
    proc = Proc()
    monkeypatch.setattr(comfy, "launch", lambda inst, url: started.append((inst.path, url)) or proc)
    monkeypatch.setattr(comfy, "stop", lambda p: stopped.append(p) or setattr(p, "stopped", True))
    agent = make_agent(comfyui_path=str(install))
    assert media.warm(agent).startswith("Starting ComfyUI") and started == [
        (install.resolve(), agent.settings.comfyui_url)
    ]
    assert media.warm(agent) is None  # already starting
    agent.settings.comfyui_url = standin.url  # it answers now
    media.generate_image(agent, "a fox")
    assert media.stop_engine(agent) and stopped == [proc] and agent.comfy_process is None
    assert not media.stop_engine(agent)  # one you started yourself keeps running


def test_switching_modes_starts_and_stops_comfyui(make_agent, monkeypatch, tmp_path):
    install = tmp_path / "ComfyUI"
    (install / "comfy").mkdir(parents=True)
    (install / "main.py").write_text("")
    proc = Proc()
    monkeypatch.setattr(comfy, "launch", lambda inst, url: proc)
    monkeypatch.setattr(comfy, "stop", lambda p: setattr(p, "stopped", True))
    monkeypatch.setattr("lcode.repl.preload", lambda agent, session=None: None)
    agent = make_agent(comfyui_path=str(install))
    handle_command(agent, "/mode image", LAPTOP)
    assert agent.comfy_process is proc and "Starting ComfyUI" in text(agent)
    assert "Describe an image and press Enter: FLUX.2 klein 4B" in text(agent)
    handle_command(agent, "/mode code", LAPTOP)
    assert proc.stopped and "Stopped ComfyUI to give the GPU back" in text(agent)


def test_comfyui_that_dies_while_starting_points_at_its_log(make_agent, monkeypatch, tmp_path):
    install = tmp_path / "ComfyUI"
    (install / "comfy").mkdir(parents=True)
    (install / "main.py").write_text("")
    proc = Proc()
    proc.stopped = True  # exited at once
    monkeypatch.setattr(comfy, "launch", lambda inst, url: proc)
    agent = make_agent(comfyui_path=str(install))
    with pytest.raises(media.MediaError, match=r"stopped while starting: see .*comfyui\.log"):
        media.generate_image(agent, "a fox")
    assert agent.comfy_process is None


def test_where_lcode_looks_for_comfyui(tmp_path, monkeypatch):
    mine = tmp_path / "mine"
    (mine / "comfy").mkdir(parents=True)
    (mine / "main.py").write_text("")
    (mine / ".venv" / "bin").mkdir(parents=True)
    (mine / ".venv" / "bin" / "python").write_text("")
    found = comfy.find_install(str(mine))
    assert found.path == mine.resolve() and found.python == str(mine / ".venv" / "bin" / "python")
    assert comfy.find_install(str(tmp_path / "nothing")) is None
    monkeypatch.setattr(comfy, "cli_workspace", lambda: str(mine))  # the comfy CLI's workspace
    assert comfy.find_install().path == mine.resolve()


def test_the_comfy_clis_workspace(monkeypatch):
    envelope = {"ok": True, "command": "which", "data": {"workspace_path": "/somewhere/ComfyUI"}}
    monkeypatch.setattr(comfy.shutil, "which", lambda name: "/bin/comfy")
    monkeypatch.setattr(
        comfy.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(envelope) + "\n", "")
    )
    assert real_cli_workspace() == "/somewhere/ComfyUI"
    monkeypatch.setattr(comfy.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "oops", ""))
    assert real_cli_workspace() is None
    monkeypatch.setattr(comfy.shutil, "which", lambda name: None)
    assert real_cli_workspace() is None


def test_comfyui_runs_in_its_own_process_group_and_stops_with_lcode(tmp_path):
    install = tmp_path / "ComfyUI"
    install.mkdir()
    (install / "main.py").write_text(
        "import json, sys, time\nopen('args.json', 'w').write(json.dumps(sys.argv[1:]))\ntime.sleep(60)\n"
    )
    proc = real_launch(comfy.Install(install, sys.executable), "http://127.0.0.1:8190")
    try:
        for _ in range(100):
            if (install / "args.json").exists():
                break
            time.sleep(0.05)
        args = json.loads((install / "args.json").read_text())
        assert args[:4] == ["--listen", "127.0.0.1", "--port", "8190"] and "--preview-method" in args
    finally:
        comfy.stop(proc)
    assert proc.poll() is not None


def test_installing_comfyui_picks_the_gpus_pytorch(monkeypatch, tmp_path):
    monkeypatch.setattr(comfy.shutil, "which", lambda name: None)
    folder = comfy.own_folder()
    command = comfy.install_command(folder, LAPTOP)
    assert command[:4] == ["uvx", "--from", "comfy-cli", "comfy"] and "--nvidia" in command
    assert command[command.index("--workspace") + 1] == str(folder)
    assert "--m-series" in comfy.install_command(folder, Hardware("macos", "M3", 36, "Apple M3", 0, True))
    assert "--cpu" in comfy.install_command(folder, Hardware("linux", "x", 16))
    monkeypatch.setattr(comfy.shutil, "which", lambda name: "/usr/bin/" + name)

    def fake_install(command, **kwargs):
        (folder / "comfy").mkdir(parents=True)
        (folder / "main.py").write_text("")
        return subprocess.CompletedProcess(command, 0)

    assert comfy.install(LAPTOP, run=fake_install).path == folder.resolve()
    with pytest.raises(comfy.ComfyError, match="couldn't install"):
        comfy.install(LAPTOP, run=lambda command, **kwargs: subprocess.CompletedProcess(command, 1))


def test_saying_yes_installs_it(make_agent, monkeypatch, tmp_path):
    proc = Proc()
    installed = []

    def fake_install(hardware):
        installed.append(hardware)
        return comfy.Install(tmp_path, "python")

    monkeypatch.setattr(comfy, "install", fake_install)
    monkeypatch.setattr(comfy, "launch", lambda inst, url: proc)
    monkeypatch.setattr("lcode.hardware.detect", lambda: LAPTOP)
    agent = make_agent()
    monkeypatch.setattr(comfy.Server, "wait", lambda self, p, cancelled=None: {"system": {"comfyui_version": "0.38"}})
    with pytest.raises(media.MediaError):  # it "started", but nothing answers on port 9 for the job itself
        media.generate_image(agent, "a fox", ask=lambda q: True)
    assert installed == [LAPTOP] and agent.comfy_process is proc
    assert squeezed(f"Installed ComfyUI into {tmp_path}") in squeezed(output(agent))  # long paths wrap mid-word


# ----------------------------------------------------------------------------- the REPL, -p and doctor


def test_a_request_in_image_mode_makes_an_image(image_agent, standin):
    image_agent.interactive = False
    respond(image_agent, "a lighthouse in a storm")
    assert "a-lighthouse-in-a-storm.png" in text(image_agent) and image_agent.ollama.payloads == []


def test_an_image_from_the_command_line(tmp_path, monkeypatch, capsys, standin, repo):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    cli.main(["config", "set", "comfyui_url", standin.url])
    monkeypatch.setattr(cli, "Ollama", lambda host: FakeOllama())
    monkeypatch.setattr(cli, "detect", lambda: LAPTOP)
    monkeypatch.setattr("lcode.api.check_server", lambda *args: "0.35.1")
    monkeypatch.setattr(cli, "resolve_model", lambda ollama, name: (name, catalog.find(name)))
    monkeypatch.setattr(cli, "choose_context", lambda *args: (32768, ""))
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit) as done:
        cli.main(["--mode", "image", "-p", "a red fox", "--output", "json", "--no-mcp"])
    assert done.value.code == 0
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["status"] == "success" and result["model"] == "FLUX.2 klein 4B"
    assert Path(result["files"][0]).is_file() and result["files"][0].endswith("-a-red-fox.png")


def test_doctor_reports_comfyui(standin, tmp_path):
    cfg = {"comfyui_url": standin.url, "comfyui_path": None}
    assert cli.comfyui_status(cfg) == (f"0.38.0 running at {standin.url} · image model: FLUX.2 klein 4B", True)
    install = tmp_path / "ComfyUI"
    (install / "comfy").mkdir(parents=True)
    (install / "main.py").write_text("")
    cfg = {"comfyui_url": "http://127.0.0.1:9", "comfyui_path": str(install)}
    status, good = cli.comfyui_status(cfg)
    assert "lcode starts it in image mode" in status and good is None
    assert "FLUX.2 klein 4B not installed: lcode media install flux2-klein-4b (12.4 GB)" in status
    for folder, names in FLUX.items():
        (install / "models" / folder).mkdir(parents=True)
        (install / "models" / folder / names[0]).write_text("")
    assert cli.comfyui_status(cfg)[1] is True
    status, good = cli.comfyui_status({"comfyui_url": "http://127.0.0.1:9", "comfyui_path": str(tmp_path / "none")})
    assert status.startswith("not found") and good is None


# ----------------------------------------------------------------------------- small parts


def test_the_status_line_estimates_the_time_left(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(media.time, "monotonic", lambda: now[0])
    line = media.Line("FLUX.2 klein 4B", "Esc or Ctrl+C cancels")
    line.doing = "drawing"
    line.progress(1, 10)
    now[0] = 102.0
    line.progress(3, 10)
    rendered = line.__rich__().plain
    assert rendered.startswith("FLUX.2 klein 4B: drawing · step 3/10 · ~7 s left · 2 s")
    assert "Esc or Ctrl+C cancels" in rendered


def test_a_png_keeps_its_settings_and_anything_else_is_left_alone():
    png = comfy.with_text(tiny_png(), "lcode", '{"seed": 7}')
    assert [kind for kind, _ in chunks(png)] == [b"IHDR", b"IDAT", b"iTXt", b"IEND"]
    assert comfy.with_text(b"not a png", "lcode", "{}") == b"not a png"


def test_media_dir_can_be_anywhere(make_agent, tmp_path):
    agent = make_agent(media_dir=str(tmp_path / "gallery"))
    assert media.folder(agent) == tmp_path / "gallery" and not (tmp_path / "gallery" / ".gitignore").exists()
    agent = make_agent(media_dir="assets/generated")
    assert media.folder(agent) == agent.workspace / "assets" / "generated"
    assert not (agent.workspace / "assets" / "generated" / ".gitignore").exists()  # your folder: you decide


# ----------------------------------------------------------------------------- lcode never leaves ComfyUI behind


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the parent-death signal is Linux's")
def test_comfyui_stops_when_lcode_is_killed(tmp_path):
    install = tmp_path / "ComfyUI"
    install.mkdir()
    (install / "main.py").write_text("import time\ntime.sleep(60)\n")
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\nfrom pathlib import Path\nfrom lcode import comfy\n"
            "p = comfy.launch(comfy.Install(Path(sys.argv[1]), sys.executable), 'http://127.0.0.1:8191')\n"
            "print(p.pid, flush=True)\ntime.sleep(60)\n",
            str(install),
        ],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "LCODE_HOME": str(tmp_path / "state")},
    )
    child = int(parent.stdout.readline())
    parent.kill()  # kill -9: no cleanup in lcode at all
    parent.wait()
    for _ in range(300):  # up to 15 s: a busy machine can be slow to deliver and reap
        if not alive(child):
            break
        time.sleep(0.05)
    try:
        assert not alive(child)
    finally:
        if alive(child):
            with contextlib.suppress(ProcessLookupError):  # it may end between the check and the kill
                os.kill(child, 9)


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return False
    return "\nState:\tZ" not in state


def test_a_closed_terminal_ends_lcode_the_usual_way():
    import signal

    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGHUP, signal.SIGTERM)}
    try:
        cli.exit_on_hangup()
        with pytest.raises(SystemExit) as e:
            signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None)
        assert e.value.code == 129
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def test_closing_stops_comfyui_even_when_the_terminal_is_gone(make_agent, monkeypatch):
    from lcode import api

    class Gone(io.StringIO):
        def write(self, text):
            raise OSError(5, "Input/output error")

    proc = Proc()
    monkeypatch.setattr(comfy, "stop", lambda p: setattr(p, "stopped", True))
    agent = make_agent()
    agent.comfy_process = proc
    agent.console.file = Gone()
    api.close_agent(agent)
    assert proc.stopped and agent.comfy_process is None


def test_what_you_type_while_an_image_is_made_waits_in_the_next_prompt(image_agent, standin, monkeypatch):
    from lcode import repl

    class Reader:
        def __init__(self, steering):
            self.steering = steering

        def start(self):
            self.steering.feed("a cat on a windowsill")  # typed during the job
            return True

        def stop(self):
            pass

    monkeypatch.setattr(repl, "KeyReader", Reader)
    respond(image_agent, "a lighthouse")
    assert image_agent.unsent == "a cat on a windowsill" and "is in the prompt below" in text(image_agent)


# ----------------------------------------------------------------------------- image mode needs no coding model


def test_image_mode_starts_without_ollama(tmp_path, monkeypatch, repo):
    from rich.console import Console

    from lcode import api, modes

    def down(*args):
        raise api.SetupError("can't reach Ollama at http://localhost:11434\n\nIs Ollama installed and running?")

    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(cli, "Ollama", lambda host: FakeOllama())
    monkeypatch.setattr(api, "check_server", down)
    console = Console(file=io.StringIO(), width=120)
    with pytest.raises(api.SetupError):  # code mode can't do without it
        api.open_agent(repo, api.Options(mcp=False, interactive=False), console, LAPTOP)
    agent = api.open_agent(repo, api.Options(mode="image", mcp=False, interactive=False), console, LAPTOP)
    assert agent.settings.mode == "image" and agent.no_coding_model.startswith("can't reach Ollama")
    from lcode.repl import banner

    banner(agent)
    assert "FLUX.2 klein 4B · ComfyUI · .lcode/media" in " ".join(console.file.getvalue().split())
    lines = " ".join(modes.switch(agent, "code"))
    assert "lcode started without its coding model (can't reach Ollama at http://localhost:11434)" in lines
    api.close_agent(agent)
