"""Modes: code (the coding agent, code only), image and video (their own models, the GPU to themselves)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from conftest import FakeOllama, output
from lcode import catalog, cli, config, modes
from lcode.hardware import Hardware
from lcode.mcp import config as mcp_config
from lcode.repl import banner, handle_command, respond

LAPTOP = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)


def text(agent):
    return " ".join(output(agent).split())


# ----------------------------------------------------------------------------- switching


def test_mode_shows_itself_and_switches(agent, monkeypatch):
    freed = []
    monkeypatch.setattr(modes, "free_comfyui", lambda url: freed.append(url) or True)
    monkeypatch.setattr("lcode.repl.preload", lambda agent, session=None: None)
    handle_command(agent, "/mode", LAPTOP)
    assert "Mode: code (local coding agent)" in text(agent)
    handle_command(agent, "/mode image", LAPTOP)
    assert agent.settings.mode == "image" and agent.ollama.unloaded == ["lcode-qwen3.6-35b"]
    assert "Unloaded lcode-qwen3.6-35b to free the GPU for images" in text(agent)
    assert "ComfyUI isn't installed; lcode offers to install it when you make your first image" in text(agent)
    assert "Describe an image and press Enter: FLUX.2 klein 4B makes it on your GPU, into .lcode/media" in text(agent)
    handle_command(agent, "/mode video", LAPTOP)  # no coding model to unload from image mode
    assert agent.settings.mode == "video" and agent.ollama.unloaded == ["lcode-qwen3.6-35b"]
    assert "Describe a video and press Enter (@photo.png starts it from a picture): Wan 2.2 TI2V 5B" in text(agent)
    handle_command(agent, "/mode code", LAPTOP)
    assert agent.settings.mode == "code" and freed == [agent.settings.comfyui_url]
    assert "ComfyUI gave the GPU back" in text(agent)
    handle_command(agent, "/mode cinema", LAPTOP)
    assert "Unknown mode 'cinema'" in text(agent) and agent.settings.mode == "code"


def test_a_request_in_image_or_video_mode_never_reaches_the_coding_model(make_agent):
    agent = make_agent([], mode="yolo")  # no scripted replies: a request to the model would fail
    agent.interactive = False
    agent.settings.mode = "image"
    respond(agent, "a red fox in the snow")  # no ComfyUI here: image mode says how to get it
    assert "no install found" in text(agent) and agent.ollama.payloads == []
    agent.settings.mode = "video"
    respond(agent, "a red fox running")  # video mode too: ComfyUI's, never the coding model's
    assert text(agent).count("no install found") == 2 and agent.ollama.payloads == []
    handle_command(agent, "/model 1", LAPTOP)
    assert "Video model: Wan 2.2 TI2V 5B" in text(agent) and agent.settings.video_model == "wan22-ti2v-5b"


def test_the_permission_mode_has_its_own_command_now(agent):
    handle_command(agent, "/permissions plan", LAPTOP)
    assert agent.perms.mode == "plan" and "Permission mode: plan" in text(agent)
    handle_command(agent, "/mode yolo", LAPTOP)  # what /mode used to set still works, with a note
    assert agent.perms.mode == "yolo" and agent.settings.mode == "code"
    assert "/permissions sets it now" in text(agent)


def test_the_banner_says_the_mode(agent):
    banner(agent)
    assert "local coding agent" in text(agent) and "mode code" in text(agent) and "perms yolo" in text(agent)
    agent.settings.mode = "video"
    banner(agent)
    assert "local video generation" in text(agent)


def test_a_session_keeps_its_mode(make_agent):
    agent = make_agent()
    agent.settings.mode = "image"
    agent.messages.append({"role": "user", "content": "hi"})
    agent.save()
    from lcode import sessions

    other = make_agent()
    other.load(sessions.list_sessions(agent.workspace)[0])
    assert other.settings.mode == "image"


# ----------------------------------------------------------------------------- the GPU handover


class Comfy(BaseHTTPRequestHandler):
    calls: ClassVar[list] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Comfy.calls.append((self.path, body))
        self.send_response(200)
        self.end_headers()


def test_comfyui_is_asked_to_give_the_gpu_back():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Comfy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert modes.free_comfyui(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()
    assert Comfy.calls == [("/free", {"unload_models": True, "free_memory": True})]
    assert not modes.free_comfyui("http://127.0.0.1:9")  # nothing there: nothing to free


# ----------------------------------------------------------------------------- code mode makes code only


def test_code_mode_leaves_out_the_servers_that_make_images_and_videos():
    servers = [
        mcp_config.parse("comfyui", {"command": "uvx", "args": ["--from", "comfy-mcp", "comfy-mcp"]}),
        mcp_config.parse("grok", {"command": "uvx", "args": ["--from", "lcode-cli", "lcode-mcp-xai"]}),
        mcp_config.parse("github", {"url": "https://api.githubcopilot.com/mcp/"}),
    ]
    kept, left_out = modes.code_mode_servers(servers)
    assert [s.name for s in kept] == ["github"] and left_out == ["comfyui", "grok"]
    assert "aren't loaded here any more" in modes.note_once(left_out)
    assert modes.note_once(left_out) == ""  # once
    assert modes.note_once([]) == ""


def test_the_catalog_no_longer_offers_them():
    from lcode.mcp import catalog as mcp_catalog

    assert not {"comfyui", "xai"} & set(mcp_catalog.load())


# ----------------------------------------------------------------------------- settings and the command line


def test_the_mode_setting(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    assert config.load()["mode"] == "code" and config.load()["comfyui_url"] == "http://127.0.0.1:8188"
    with pytest.raises(SystemExit):
        cli.main(["config", "set", "mode", "cinema"])
    cli.main(["config", "set", "mode", "video"])
    assert config.load()["mode"] == "video"


def test_a_request_from_the_command_line_without_comfyui_says_how_to_get_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    cli.main(["config", "set", "comfyui_url", "http://127.0.0.1:9"])
    monkeypatch.setattr(cli, "Ollama", lambda host: FakeOllama())
    monkeypatch.setattr(cli, "detect", lambda: LAPTOP)
    monkeypatch.setattr("lcode.api.check_server", lambda *args: "0.35.1")
    monkeypatch.setattr(cli, "resolve_model", lambda ollama, name: (name, catalog.find(name)))
    monkeypatch.setattr(cli, "choose_context", lambda *args: (32768, ""))
    for mode in ("video", "image"):  # both make media now, given a ComfyUI (tests/test_comfy.py, test_video.py)
        with pytest.raises(SystemExit) as done:
            cli.main(["--mode", mode, "-p", "a red fox running", "--output", "json", "--no-mcp"])
        assert done.value.code == 1
        result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert result["status"] == "error" and "no install found" in result["error"]
