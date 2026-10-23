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
    assert "Image mode doesn't generate yet" in text(agent)
    handle_command(agent, "/mode video", LAPTOP)  # no coding model to unload from image mode
    assert agent.settings.mode == "video" and agent.ollama.unloaded == ["lcode-qwen3.6-35b"]
    handle_command(agent, "/mode code", LAPTOP)
    assert agent.settings.mode == "code" and freed == ["http://127.0.0.1:8188"]
    assert "ComfyUI gave the GPU back" in text(agent)
    handle_command(agent, "/mode cinema", LAPTOP)
    assert "Unknown mode 'cinema'" in text(agent) and agent.settings.mode == "code"


def test_a_request_in_image_mode_doesnt_reach_the_coding_model(make_agent):
    agent = make_agent([], mode="yolo")  # no scripted replies: a request to the model would fail
    agent.settings.mode = "image"
    respond(agent, "a red fox in the snow")
    assert "Image mode doesn't generate yet" in text(agent) and agent.ollama.payloads == []
    handle_command(agent, "/models", LAPTOP)
    assert text(agent).count("Image mode doesn't generate yet") == 2


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


def test_a_request_from_the_command_line_in_image_mode_says_whats_coming(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(cli, "Ollama", lambda host: FakeOllama())
    monkeypatch.setattr(cli, "detect", lambda: LAPTOP)
    monkeypatch.setattr("lcode.api.check_server", lambda *args: "0.35.1")
    monkeypatch.setattr(cli, "resolve_model", lambda ollama, name: (name, catalog.find(name)))
    monkeypatch.setattr(cli, "choose_context", lambda *args: (32768, ""))
    with pytest.raises(SystemExit) as done:
        cli.main(["--mode", "image", "-p", "a red fox", "--output", "json", "--no-mcp"])
    assert done.value.code == 2
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["status"] == "error" and "Image mode doesn't generate yet" in result["error"]
