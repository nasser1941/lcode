"""What lcode makes: code, images or videos.

- **code** is the coding agent: a language model with tools, and nothing else. It doesn't generate images or
  videos: the MCP servers that do (`comfyui`, `xai`) aren't loaded in code mode.
- **image** and **video** generate media on the GPU with image and video models, through ComfyUI (lcode.comfy,
  lcode.media). They don't need the big coding model: lcode unloads it when you switch to them, and stops the
  ComfyUI it started (or frees the memory of yours) when you switch back, so one model has the GPU at a time.

Video mode says what's coming instead of sending the prompt anywhere until video generation lands (#153).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import requests

from lcode import config

if TYPE_CHECKING:
    from lcode.agent import Agent
    from lcode.mcp.config import ServerConfig

MODES = ("code", "image", "video")
TITLES = {"code": "local coding agent", "image": "local image generation", "video": "local video generation"}
MEDIA_SERVERS = frozenset({"comfyui", "xai"})  # the MCP catalog's image and video servers
MEDIA_COMMANDS = ("comfy-mcp", "lcode-mcp-xai")  # the same servers, under any name
NOTED_FILE = "media-servers-noted"
COMING = {
    "video": (
        "Video mode doesn't generate yet: the video models (#150) and video generation (#153) come next. "
        "/mode image makes images now; /mode code switches back."
    ),
}


def ready(agent: Agent) -> str:
    """What to do in the agent's mode, said on entering it."""
    if agent.settings.mode == "image":
        from lcode import media

        return (
            f"Describe an image and press Enter: {media.image_model()} makes it on your GPU, into "
            f"{agent.settings.media_dir}. /mode code switches back."
        )
    return COMING.get(agent.settings.mode, "")


def is_media_server(server: ServerConfig) -> bool:
    """An MCP server that generates images or videos: those belong to image and video mode, not code mode."""
    if server.name.lower() in MEDIA_SERVERS:
        return True
    target = " ".join(str(part) for part in [*server.command, server.url])
    return any(name in target for name in MEDIA_COMMANDS)


def code_mode_servers(servers: list[ServerConfig]) -> tuple[list[ServerConfig], list[str]]:
    """The servers code mode loads, and the names of the media servers it leaves out."""
    kept = [s for s in servers if not is_media_server(s)]
    return kept, [s.name for s in servers if is_media_server(s)]


def note_once(left_out: list[str]) -> str:
    """The note about media servers left out of code mode, the first time only."""
    if not left_out:
        return ""
    marker = config.STATE_DIR / NOTED_FILE
    if marker.exists():
        return ""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(sorted(left_out)) + "\n")
    except OSError:
        pass
    names = ", ".join(left_out)
    return (
        f"Code mode makes code only, so the MCP servers that make images and videos ({names}) aren't loaded "
        "here any more. Image and video generation move to their own modes: /mode image, /mode video."
    )


def free_comfyui(url: str) -> bool:
    """Ask ComfyUI at `url` to give the GPU back (unload its models, free its memory). Whether it answered."""
    try:
        r = requests.post(url.rstrip("/") + "/free", json={"unload_models": True, "free_memory": True}, timeout=3)
    except requests.RequestException:
        return False
    return r.status_code == 200


def switch(agent: Agent, new: str) -> list[str]:
    """Switch the agent to another mode, handing the GPU over. The lines to show."""
    old = agent.settings.mode
    if new == old:
        return [f"Already in {new} mode."]
    lines = []
    if old == "code" and getattr(agent.ollama, "kind", "ollama") == "ollama":
        try:
            agent.ollama.unload(agent.settings.model)  # the image or video model needs the GPU
            lines.append(f"Unloaded {agent.settings.model} to free the GPU for {new}s.")
        except Exception:  # not loaded, or Ollama gone: nothing to free
            pass
    if new == "code":
        from lcode import media

        if media.stop_engine(agent):
            lines.append("Stopped ComfyUI to give the GPU back.")
        elif free_comfyui(agent.settings.comfyui_url):
            lines.append("ComfyUI gave the GPU back.")
    agent.settings.mode = new
    lines.insert(0, f"Mode: {new} ({TITLES[new]}).")
    if new == "code" and agent.no_coding_model:
        reason = agent.no_coding_model.strip().splitlines()[0]
        lines.append(f"lcode started without its coding model ({reason}). Fix that, then restart lcode.")
    if new == "image":
        from lcode import media

        warming = media.warm(agent)
        if warming:
            lines.append(warming)
    if new != "code":
        lines.append(ready(agent))
    return lines
