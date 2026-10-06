"""An MCP server for xAI's Grok Imagine and voice APIs: images, image edits, videos and speech.

xAI has an HTTP API but no MCP server, so lcode ships this one. It needs an API key (XAI_API_KEY,
from https://console.x.ai) and saves what it makes in the project, in `generated/` unless a path is
given. The prompts, and any image it edits or animates, go to xAI, which bills per image, per
second of video and per character of speech, and applies its own usage policies.

Run: lcode-mcp-xai   (lcode mcp add xai sets it up)
"""

from __future__ import annotations

import argparse
import base64
import mimetypes
import os
import re
import time
from pathlib import Path

import requests

from lcode import __version__
from lcode.mcp.server import Server, ToolFailure

API = "https://api.x.ai/v1"
TIMEOUT = 120
IMAGE_MODELS = ["grok-imagine-image-2.0", "grok-imagine-image-quality", "grok-imagine-image"]
VIDEO_MODELS = ["grok-imagine-video-1.5", "grok-imagine-video-1.5-lite", "grok-imagine-video"]
ASPECT_RATIOS = ["auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "2:1", "1:2", "21:9"]
VOICES = ["eve", "ara", "leo", "rex", "sal", "luna", "iris", "orion", "helix", "zagan", "carina", "altair", "zenith",
          "perseus", "helios", "lux", "kepler", "rigel", "cosmo", "celeste", "ursa", "sirius", "lumen", "castor",
          "naksh", "atlas"]  # fmt: skip
AUDIO_FORMATS = {"mp3": ".mp3", "wav": ".wav"}
VIDEO_WAIT = 600  # seconds generate_video waits before handing back the request id
POLL = 5


class XAI:
    def __init__(self, key: str, base: str = API, output: Path | None = None):
        self.base = base.rstrip("/")
        self.output = output or Path("generated")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {key}", "Content-Type": "application/json"})

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            r = self.session.request(method, f"{self.base}{path}", timeout=TIMEOUT, **kwargs)
        except requests.RequestException as e:
            raise ToolFailure(f"can't reach xAI: {e}") from e
        if r.status_code in (401, 403):
            raise ToolFailure(
                f"xAI refused the request ({r.status_code}): {_error(r)}. Check XAI_API_KEY (console.x.ai)."
            )
        if r.status_code == 429:
            raise ToolFailure(f"xAI's rate limit or credit limit was reached: {_error(r)}")
        if r.status_code >= 400:
            raise ToolFailure(f"xAI error {r.status_code}: {_error(r)}")
        return r

    def post(self, path: str, body: dict) -> dict:
        return self.request("POST", path, json=body).json()

    def download(self, url: str) -> bytes:
        try:
            r = requests.get(url, timeout=TIMEOUT)
            r.raise_for_status()
        except requests.RequestException as e:
            raise ToolFailure(f"couldn't download the result from xAI: {e}") from e
        return r.content

    def target(self, path: str, stem: str, suffix: str) -> Path:
        """Where to save: the given path, or a new file in the output folder."""
        if path:
            target = Path(path).expanduser()
            if not target.suffix:
                target = target.with_suffix(suffix)
        else:
            name = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:40] or "xai"
            target = self.output / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        n, unique = 2, target
        while unique.exists():
            unique = target.with_name(f"{target.stem}-{n}{target.suffix}")
            n += 1
        return unique


def _error(r: requests.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return r.text[:300]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or data)[:300]


def data_url(path: str) -> str:
    """A local image as a data: URL, for edits and image-to-video."""
    file = Path(path).expanduser()
    if not file.is_file():
        raise ToolFailure(f"{path} doesn't exist")
    mime = mimetypes.guess_type(file.name)[0] or "image/png"
    if mime not in ("image/png", "image/jpeg", "image/webp"):
        raise ToolFailure(f"{path} isn't a PNG, JPEG or WebP image")
    return f"data:{mime};base64,{base64.b64encode(file.read_bytes()).decode()}"


def image_suffix(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def save_images(api: XAI, reply: dict, prompt: str, path: str) -> list[Path]:
    saved = []
    items = reply.get("data") or []
    if not items:
        raise ToolFailure(f"xAI returned no image: {str(reply)[:300]}")
    for i, item in enumerate(items):
        data = base64.b64decode(item["b64_json"]) if item.get("b64_json") else api.download(item["url"])
        many = path and len(items) > 1
        target = api.target(f"{Path(path).with_suffix('')}-{i + 1}" if many else path, prompt, image_suffix(data))
        target.write_bytes(data)
        saved.append(target)
    return saved


def build(api: XAI) -> Server:
    server = Server(
        "xai",
        __version__,
        "xAI's Grok Imagine and voice: generate and edit images, generate videos (from text or a start image) "
        "and speech. Results are saved as files in the project; each call costs money on the user's xAI account. "
        "xAI has no music generation.",
    )
    path_arg = {"type": "string", "description": "Where to save it (default: a new file in generated/)"}

    @server.tool(
        "generate_image",
        "Generate images from a description with Grok Imagine and save them. Costs $0.02–0.05 per image.",
        {
            "prompt": {"type": "string", "description": "What the image shows, in detail"},
            "n": {"type": "integer", "description": "How many images, 1–10 (default 1)"},
            "aspect_ratio": {"type": "string", "enum": ASPECT_RATIOS, "description": "Default auto"},
            "resolution": {"type": "string", "enum": ["1k", "2k"], "description": "Default 1k"},
            "model": {"type": "string", "enum": IMAGE_MODELS, "description": f"Default {IMAGE_MODELS[0]}"},
            "path": path_arg,
        },
        ["prompt"],
    )
    def generate_image(prompt: str, n: int = 1, aspect_ratio: str = "auto", resolution: str = "1k",
                       model: str = IMAGE_MODELS[0], path: str = "") -> str:  # fmt: skip
        body = {"model": model, "prompt": prompt, "n": max(1, min(10, int(n))), "response_format": "b64_json"}
        if aspect_ratio and aspect_ratio != "auto":
            body["aspect_ratio"] = aspect_ratio
        if resolution and resolution != "1k":
            body["resolution"] = resolution
        saved = save_images(api, api.post("/images/generations", body), prompt, path)
        return f"Saved {len(saved)} image(s) from {model}:\n" + "\n".join(f"- {p}" for p in saved)

    @server.tool(
        "edit_image",
        "Change an existing image (a local file) as described, with Grok Imagine, and save the result.",
        {
            "image": {"type": "string", "description": "Path of the image to edit (PNG, JPEG or WebP)"},
            "prompt": {"type": "string", "description": "What to change"},
            "model": {"type": "string", "enum": IMAGE_MODELS, "description": f"Default {IMAGE_MODELS[0]}"},
            "path": path_arg,
        },
        ["image", "prompt"],
    )
    def edit_image(image: str, prompt: str, model: str = IMAGE_MODELS[0], path: str = "") -> str:
        body = {"model": model, "prompt": prompt, "image": {"url": data_url(image), "type": "image_url"},
                "response_format": "b64_json"}  # fmt: skip
        saved = save_images(api, api.post("/images/edits", body), prompt, path)
        return f"Saved the edited image: {saved[0]}"

    def finish_video(request_id: str, prompt: str, path: str, wait: float) -> str:
        deadline = time.monotonic() + wait
        while True:
            state = api.request("GET", f"/videos/{request_id}").json()
            status = state.get("status")
            if status == "done":
                video = state.get("video") or {}
                target = api.target(path, prompt or request_id, ".mp4")
                target.write_bytes(api.download(video["url"]))
                return f"Saved the video ({video.get('duration', '?')} s, {state.get('model', '')}): {target}"
            if status in ("failed", "expired"):
                raise ToolFailure(f"xAI couldn't make the video ({status}): {state.get('error') or state}")
            if time.monotonic() > deadline:
                return (
                    f"The video isn't ready yet (request {request_id}). Check later with "
                    f'video_status(request_id="{request_id}"), which downloads it when it\'s done.'
                )
            time.sleep(POLL)

    @server.tool(
        "generate_video",
        "Generate a video with Grok Imagine, from a description and optionally a start image, and save it as "
        "MP4. Takes a minute or more. Costs $0.02–0.08 per second of video.",
        {
            "prompt": {"type": "string", "description": "What happens in the video"},
            "image": {"type": "string", "description": "A local image to start from (optional)"},
            "duration": {"type": "integer", "description": "Seconds, 1–15 (default 8)"},
            "aspect_ratio": {
                "type": "string",
                "enum": [r for r in ASPECT_RATIOS if r not in ("auto", "2:1", "1:2", "21:9")],
            },
            "resolution": {"type": "string", "enum": ["480p", "720p", "1080p"], "description": "Default 720p"},
            "model": {"type": "string", "enum": VIDEO_MODELS, "description": f"Default {VIDEO_MODELS[0]}"},
            "path": path_arg,
        },
        ["prompt"],
    )
    def generate_video(prompt: str, image: str = "", duration: int = 8, aspect_ratio: str = "16:9",
                       resolution: str = "720p", model: str = VIDEO_MODELS[0], path: str = "") -> str:  # fmt: skip
        body: dict = {"model": model, "prompt": prompt, "duration": max(1, min(15, int(duration))),
                      "aspect_ratio": aspect_ratio, "resolution": resolution}  # fmt: skip
        if image:
            body["image"] = {"url": data_url(image)}
        request_id = api.post("/videos/generations", body).get("request_id")
        if not request_id:
            raise ToolFailure("xAI didn't start the video")
        return finish_video(str(request_id), prompt, path, VIDEO_WAIT)

    @server.tool(
        "video_status",
        "Check a video that generate_video started and that wasn't ready yet; saves it when it's done.",
        {"request_id": {"type": "string"}, "path": path_arg},
        ["request_id"],
    )
    def video_status(request_id: str, path: str = "") -> str:
        return finish_video(request_id, "", path, 0)

    @server.tool(
        "text_to_speech",
        "Turn text into speech with an xAI voice and save it as an audio file. Billed per character.",
        {
            "text": {"type": "string", "description": "What to say (up to 60,000 characters)"},
            "voice": {"type": "string", "enum": VOICES, "description": "Default eve"},
            "language": {"type": "string", "description": "BCP-47 code such as en, de, fr, ja, or auto (default)"},
            "format": {"type": "string", "enum": list(AUDIO_FORMATS), "description": "Default mp3"},
            "speed": {"type": "number", "description": "1.0 is normal"},
            "path": path_arg,
        },
        ["text"],
    )
    def text_to_speech(text: str, voice: str = "eve", language: str = "auto", format: str = "mp3",
                       speed: float = 1.0, path: str = "") -> str:  # fmt: skip
        codec = format if format in AUDIO_FORMATS else "mp3"
        body = {"text": text, "voice_id": voice.lower(), "language": language or "auto",
                "output_format": {"codec": codec}, "speed": float(speed or 1.0)}  # fmt: skip
        r = api.request("POST", "/tts", json=body)
        duration = ""
        if "json" in r.headers.get("Content-Type", ""):
            data = r.json()
            audio = base64.b64decode(data.get("audio", ""))
            duration = f", {data['duration']:.1f} s" if isinstance(data.get("duration"), (int, float)) else ""
        else:
            audio = r.content
        if not audio:
            raise ToolFailure("xAI returned no audio")
        target = api.target(path, text[:40], AUDIO_FORMATS[codec])
        target.write_bytes(audio)
        return f"Saved the speech ({voice}{duration}): {target}"

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="lcode-mcp-xai", description="MCP server for xAI's image, video and speech APIs (stdio)."
    )
    parser.add_argument("--output", default=os.environ.get("LCODE_XAI_OUTPUT", "generated"),
                        help="folder for the files it makes (default: generated/ in the project)")  # fmt: skip
    args = parser.parse_args(argv)
    key = os.environ.get("XAI_API_KEY")
    if not key:
        parser.error("set XAI_API_KEY to an xAI API key (https://console.x.ai)")
    build(XAI(key, os.environ.get("XAI_API_BASE", API), Path(args.output))).serve()


if __name__ == "__main__":
    main()
