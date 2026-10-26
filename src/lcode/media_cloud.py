"""Image and video mode in the cloud: models that run at a provider with your API key (xAI's Grok Imagine first).

For what a laptop can't run, or when speed matters. A cloud model is chosen like any other (`/model` in image or video
mode; `/models` and `lcode media` mark it "cloud"), and then:

- **nothing goes without asking.** Before each request lcode shows what goes where (the prompt, any picture) and about
  what it costs, from the prices in media.toml and the date they were checked, and waits for a yes;
- **the key comes from an environment variable only** (XAI_API_KEY) and goes only to the provider's API, in the
  Authorization header. Results are downloaded from the address the API gives, without it;
- what it makes lands in the media folder and the gallery like anything else, with a JSON file next to it saying what
  was sent to which model and about what it cost.

What a cloud model can't do here (masks, several pictures at once, clips between two frames, extending or restyling a
clip) is made by a local model instead, and lcode says which.
"""

from __future__ import annotations

import base64
import json
import math
import mimetypes
import os
import re
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import requests
from rich.panel import Panel
from rich.text import Text

from lcode import comfy, edits, imageopts, media
from lcode import media_catalog as catalog

if TYPE_CHECKING:
    from lcode.agent import Agent

TIMEOUT = 180  # seconds for one API call (a picture takes a few)
VIDEO_WAIT = 900  # seconds to wait for a clip before giving up on it
POLL = 4  # seconds between asking whether a clip is done
IMAGE_RATIOS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "2:1", "1:2")
VIDEO_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3")
RESOLUTION = {
    "image": {"fast": "1k", "balanced": "1k", "best": "2k"},
    "video": {"fast": "480p", "balanced": "720p", "best": "1080p"},
}
MAX_VIDEO_SECONDS = 15
KEPT = ("kind", "prompt", "sources", "mask", "pads", "keep_shape", "extend", "size", "refs", "restyle")


class CloudError(Exception):
    pass


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    api: str
    key_env: str  # the environment variable with the user's API key
    base_env: str  # another address for the API (a proxy; the tests' stand-in)
    keys_url: str
    prices_url: str

    def api_key(self) -> str:
        return os.environ.get(self.key_env, "").strip()

    def base(self) -> str:
        return (os.environ.get(self.base_env, "").strip() or self.api).rstrip("/")

    def host(self) -> str:
        return urlsplit(self.base()).netloc


PROVIDERS = {
    "xai": Provider(
        "xai", "xAI", "https://api.x.ai/v1", "XAI_API_KEY", "XAI_API_BASE", "https://console.x.ai",
        "https://docs.x.ai/developers/pricing",
    ),
}  # fmt: skip


def provider(model: catalog.MediaModel) -> Provider:
    return PROVIDERS[model.cloud]


def describe(model: catalog.MediaModel) -> list[str]:
    """For lists (/models, lcode media): where it runs, what it costs, whether the key is set."""
    p = provider(model)
    key = f"{p.key_env} is set" if p.api_key() else f"needs {p.key_env}"
    return [f"cloud: {p.name}", price_text(model), key]


def price_text(model: catalog.MediaModel) -> str:
    if model.mode == "video":
        rates = [(r, model.price[r]) for r in RESOLUTION["video"].values() if r in model.price]
        if not rates:
            return "price not listed"
        low, high = rates[0], rates[-1]
        return f"${low[1]:.2f}–{high[1]:.2f} per second ({low[0]}–{high[0]})"
    return f"${model.price['image']:.2f} per image" if "image" in model.price else "price not listed"


def ready_note(model: catalog.MediaModel) -> str:
    """What to know after choosing a cloud model."""
    p = provider(model)
    note = (
        f"{model.name} runs at {p.name}: each request shows what goes there and about what it costs ("
        f"{price_text(model)}), and asks first."
    )
    if not p.api_key():
        note += f" Set your key first: export {p.key_env}=… (from {p.keys_url}), then start lcode again."
    return note


# ----------------------------------------------------------------------------- what it can do, and what it costs


def cannot(model: catalog.MediaModel, job: dict) -> str:
    """What in the job the cloud model can't do here (a local model makes it then), or empty."""
    kind, sources = job["kind"], job.get("sources") or []
    if kind == "edit":
        if job.get("mask"):
            return "masks"
        if job.get("pads"):
            return "extending a picture"
        if len(sources) > 1:
            return "several pictures at once"
    elif kind == "video":
        options = job.get("options")
        if options is not None and options.seconds > MAX_VIDEO_SECONDS:
            return f"clips longer than {MAX_VIDEO_SECONDS} s"
        if job.get("extend"):
            return "extending a clip"
        if job.get("restyle"):
            return "restyling a clip"
        if job.get("refs"):
            return "reference pictures"
        if len(sources) > 1:
            return "a clip between two frames"
    elif kind != "image":
        return kind
    return ""


def ratio(size: str, choices: tuple[str, ...]) -> str:
    """The provider's aspect ratio closest to an option's size (a ratio, a name or pixels)."""
    value = imageopts.SIZES.get(size, size)
    m = re.fullmatch(r"(\d+)\s*[:x]\s*(\d+)", value)
    if not m or not int(m.group(2)):
        return choices[0]
    want = math.log(int(m.group(1)) / int(m.group(2)))

    def distance(r: str) -> float:
        w, h = (int(x) for x in r.split(":"))
        return abs(math.log(w / h) - want)

    return min(choices, key=distance)


def video_seconds(options: imageopts.Options) -> int:
    return max(1, min(MAX_VIDEO_SECONDS, round(options.seconds)))


def cost(model: catalog.MediaModel, kind: str, options: imageopts.Options, pictures: int) -> tuple[float, str]:
    """About what the request costs, in dollars, and how that adds up. `pictures`: how many go with it."""
    sent_price = model.price.get("input_image", 0.0)
    if kind == "video":
        resolution = RESOLUTION["video"][options.quality]
        if resolution not in model.price:
            raise CloudError(f"{model.name} has no price for {resolution} in lcode: choose another --quality")
        seconds, per = video_seconds(options), model.price[resolution]
        total = options.n * seconds * per
        parts = [f"{options.n} × " * (options.n > 1) + f"{seconds} s at {resolution} × ${per:.2f}/s"]
    else:
        per = model.price.get("image", 0.0)
        total = options.n * per
        parts = [f"{options.n} image{'s' * (options.n > 1)} × ${per:.2f}"]
    if pictures and sent_price:
        total += pictures * sent_price
        parts.append(f"{pictures} picture{'s' * (pictures > 1)} sent × ${sent_price:.2f}")
    return total, " + ".join(parts)


def confirm(agent: Agent, model: catalog.MediaModel, text: str, sources: list[Path], dollars: float, how: str,
            ask: Callable[[str], bool] | None) -> None:  # fmt: skip
    """Show what goes where and what it costs, and ask. Raises MediaError when the answer is no (or nobody's there)."""
    p = provider(model)
    if ask is None:
        raise media.MediaError(
            f"{model.name} runs at {p.name} and costs money, so lcode asks before each request, and nobody can be "
            "asked here: run lcode in a terminal, or choose a local model (/model)."
        )
    pictures = ", ".join(media.shown(agent, s) for s in sources)
    goes = "the prompt" + (f" and {len(sources)} picture{'s' * (len(sources) > 1)} ({pictures})" if sources else "")
    shown_text = text if len(text) <= 400 else text[:399] + "…"
    body = Text.assemble(
        ("Where: ", "bold"), f"{model.name} ({model.api_model}) at {p.name}, {p.host()}\n",
        ("What goes: ", "bold"), f"{goes}. Your {p.key_env} goes with it, to {p.host()} only.\n",
        ("Prompt: ", "bold"), f"{shown_text}\n",
        ("Cost: ", "bold"), f"about ${dollars:.2f} ({how}), billed to your {p.name} account; prices as of "
        f"{model.priced or 'unknown'}, the real ones: {p.prices_url}",
    )  # fmt: skip
    agent.console.print(Panel(body, title=f"Make it at {p.name}?", title_align="left", border_style="magenta"))
    agent.waiting(f"send it to {p.name}?")
    if not ask(f"Send it to {p.name} (about ${dollars:.2f})?"):
        raise media.MediaError(f"Nothing was sent to {p.name}.")


# ----------------------------------------------------------------------------- xAI's Grok Imagine API


class Imagine:
    """xAI's image and video API, as image and video mode use it."""

    def __init__(self, p: Provider, key: str):
        self.p, self.key, self.base = p, key, p.base()
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {key}", "Content-Type": "application/json"})

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            r = self.session.request(method, self.base + path, json=body, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise CloudError(self.scrub(f"Can't reach {self.p.name} ({self.p.host()}): {e}")) from e
        name, why = self.p.name, error_text(r)
        if r.status_code in (401, 403):
            check = f"Check {self.p.key_env} ({self.p.keys_url})."
            raise CloudError(self.scrub(f"{name} refused the request ({r.status_code}: {why}). {check}"))
        if r.status_code == 429:
            raise CloudError(self.scrub(f"{name}'s rate limit or your credit limit was reached: {why}"))
        if r.status_code >= 400:
            raise CloudError(self.scrub(f"{name} didn't make it ({r.status_code}): {why}"))
        try:
            return r.json()
        except ValueError:
            raise CloudError(f"{self.p.name} answered with something that isn't JSON: {r.text[:200]}") from None

    def scrub(self, message: str) -> str:
        return message.replace(self.key, "…") if self.key else message

    def images(self, model: str, prompt: str, n: int, aspect_ratio: str, resolution: str) -> list[bytes]:
        body = {"model": model, "prompt": prompt, "n": n, "response_format": "b64_json", "aspect_ratio": aspect_ratio}
        if resolution != "1k":
            body["resolution"] = resolution
        return self.pictures(self.call("POST", "/images/generations", body))

    def edit(self, model: str, prompt: str, picture: Path) -> list[bytes]:
        body = {"model": model, "prompt": prompt, "image": {"url": data_url(picture), "type": "image_url"},
                "response_format": "b64_json"}  # fmt: skip
        return self.pictures(self.call("POST", "/images/edits", body))

    def pictures(self, reply: dict) -> list[bytes]:
        items = reply.get("data") or []
        if not items:
            raise CloudError(f"{self.p.name} returned no image: {str(reply)[:300]}")
        return [base64.b64decode(i["b64_json"]) if i.get("b64_json") else download(i["url"], self.p) for i in items]

    def start_video(self, model: str, prompt: str, seconds: int, aspect_ratio: str, resolution: str,
                    start: Path | None) -> str:  # fmt: skip
        body: dict = {"model": model, "prompt": prompt, "duration": seconds, "aspect_ratio": aspect_ratio,
                      "resolution": resolution}  # fmt: skip
        if start is not None:
            body["image"] = {"url": data_url(start)}
        request_id = self.call("POST", "/videos/generations", body).get("request_id")
        if not request_id:
            raise CloudError(f"{self.p.name} didn't start the video")
        return str(request_id)

    def video(self, request_id: str) -> dict:
        return self.call("GET", f"/videos/{request_id}")


def error_text(r: requests.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return r.text[:300] or r.reason
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or data)[:300]


def download(url: str, p: Provider) -> bytes:
    """A result from the address the API gave: without the key, which goes to the API only."""
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        raise CloudError(f"{p.name} made it, but lcode couldn't download it: {e}") from e
    return r.content


def data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    if mime not in ("image/png", "image/jpeg", "image/webp"):
        raise CloudError(f"{path.name} isn't a PNG, JPEG or WebP picture, which is what the provider takes")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def image_suffix(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


# ----------------------------------------------------------------------------- a request, made in the cloud


def make(
    agent: Agent,
    model: catalog.MediaModel,
    job: dict,
    options: imageopts.Options,
    cancelled: Callable[[], bool] = lambda: False,
    ask: Callable[[str], bool] | None = None,
) -> list[media.Made]:
    """The job's `options.n` images, edits or clips, made at the model's provider once the user says yes to the
    cost; saved, numbered and shown like local results."""
    p = provider(model)
    key = p.api_key()
    if not key:
        raise media.MediaError(
            f"{model.name} runs at {p.name} with your API key: export {p.key_env}=… (from {p.keys_url}) and start "
            "lcode again, or choose a local model with /model."
        )
    kind = job["kind"]
    sources = [Path(s) for s in job.get("sources") or []]
    text = imageopts.styled(job.get("expanded") or job["prompt"], options.style)
    if kind == "video" and options.camera and options.camera != "static":
        text = f"{text} Camera: {options.camera.replace('-', ' ')}."
    try:
        dollars, how = cost(model, kind, options, options.n * len(sources))
    except CloudError as e:
        raise media.MediaError(str(e)) from e
    confirm(agent, model, text, sources, dollars, how, ask)
    api = Imagine(p, key)
    out = media.folder(agent)
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{comfy.slug(job['prompt'] or kind)}"
    resolution = RESOLUTION["video" if kind == "video" else "image"][options.quality]
    meta = {"prompt": job["prompt"], "model": model.name, "model_key": model.key, "provider": p.name,
            "api_model": model.api_model, "full_prompt": text, "quality": options.quality, "style": options.style,
            "resolution": resolution, "cost_usd": round(dollars / options.n, 4), "cost_is": "an estimate",
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}  # fmt: skip
    if sources:
        meta["edit_of" if kind == "edit" else "start_image"] = [media.shown(agent, s) for s in sources]
    line = media.Line(model.name, "Esc or Ctrl+C stops waiting" if agent.interactive else "")
    line.doing = f"waiting for {p.name}"
    made: list[media.Made] = []
    try:
        with agent.console.status(line, spinner="dots"):
            if kind == "image":
                shape = ratio(options.size, IMAGE_RATIOS)
                pictures = api.images(model.api_model, text, options.n, shape, resolution)
                for i, data in enumerate(pictures, 1):
                    made.append(keep(agent, model, job, options, {**meta, "aspect_ratio": shape}, data, out,
                                     stem if len(pictures) == 1 else f"{stem}-{i}", line))  # fmt: skip
            elif kind == "edit":
                for i in range(options.n):
                    data = api.edit(model.api_model, text, sources[0])[0]
                    made.append(keep(agent, model, job, options, meta, data, out,
                                     stem if options.n == 1 else f"{stem}-{i + 1}", line))  # fmt: skip
            else:
                shape = video_shape(job, options, sources[0] if sources else None)
                seconds = video_seconds(options)
                for i in range(options.n):
                    request_id = api.start_video(model.api_model, text, seconds, shape, resolution,
                                                 sources[0] if sources else None)  # fmt: skip
                    state = wait(api, line, request_id, cancelled)
                    video = state.get("video") or {}
                    if not video.get("url"):
                        raise CloudError(f"{p.name} finished the video but gave no address for it: {str(state)[:200]}")
                    data = download(video["url"], p)
                    clip_meta = {**meta, "aspect_ratio": shape, "seconds": video.get("duration") or seconds,
                                 "request_id": request_id}  # fmt: skip
                    made.append(keep(agent, model, job, options, clip_meta, data, out,
                                     stem if options.n == 1 else f"{stem}-{i + 1}", line))  # fmt: skip
    except CloudError as e:
        raise media.MediaError(str(e)) from e
    finally:
        spent = dollars * len(made) / options.n
        agent.cloud_spent += spent
    for one in made:
        media.show(agent, one)
    agent.console.print(Text(f"  about ${spent:.2f} at {p.name} (this session: about ${agent.cloud_spent:.2f})",
                             style="dim"))  # fmt: skip
    return made


def video_shape(job: dict, options: imageopts.Options, start: Path | None) -> str:
    """16:9, or 9:16 from an upright start picture, unless the request gave a shape (as local video does)."""
    if job.get("keep_shape", True) and options.size == imageopts.Options().size:
        if start is not None:
            try:
                w, h = edits.image_size(start)
                return "9:16" if h > w else "16:9"
            except (OSError, edits.EditError):
                pass
        return "16:9"
    return ratio(options.size, VIDEO_RATIOS)


def wait(api: Imagine, line: media.Line, request_id: str, cancelled: Callable[[], bool]) -> dict:
    """The clip's state once it's done. Esc or Ctrl+C stops waiting (the provider may still make it, and bill it)."""
    stopped = media.MediaError(
        f"Stopped waiting: {api.p.name} may still make the video and bill it (request {request_id})."
    )
    deadline, next_poll = time.monotonic() + VIDEO_WAIT, 0.0
    try:
        while True:
            if cancelled():
                raise stopped
            if time.monotonic() >= next_poll:
                state = api.video(request_id)
                status = state.get("status")
                if status == "done":
                    return state
                if status in ("failed", "expired"):
                    reason = state.get("error") or ""
                    reason = reason.get("message", reason) if isinstance(reason, dict) else reason
                    because = f": {reason}" if reason else ""
                    raise CloudError(f"{api.p.name} couldn't make the video ({status}){because}")
                line.doing = f"{api.p.name} is making it ({status or 'queued'})"
                next_poll = time.monotonic() + POLL
            if time.monotonic() > deadline:
                raise CloudError(
                    f"{api.p.name} hasn't finished the video after {VIDEO_WAIT // 60} minutes (request {request_id}); "
                    "it may still finish it, and bill it."
                )
            time.sleep(0.2)
    except KeyboardInterrupt:
        raise stopped from None


def keep(agent: Agent, model: catalog.MediaModel, job: dict, options: imageopts.Options, meta: dict, data: bytes,
         out: Path, stem: str, line: media.Line) -> media.Made:  # fmt: skip
    """Save one result with its JSON, number it in the gallery."""
    video = job["kind"] == "video"
    path = comfy.unique(out / f"{stem}{'.mp4' if video else image_suffix(data)}")
    if path.suffix == ".png":
        data = comfy.with_text(data, "lcode", json.dumps(meta, ensure_ascii=False))
    path.write_bytes(data)
    width = height = 0
    try:
        width, height = media.mp4_size(path) if video else edits.image_size(path)
    except (OSError, edits.EditError, ValueError, struct.error):
        pass  # the gallery shows no size
    meta = {**meta, "width": width, "height": height}
    path.with_suffix(".json").write_text(json.dumps({**meta, "file": path.name}, indent=2, ensure_ascii=False) + "\n")
    sources = [str(s) for s in job.get("sources") or []]
    remembered = {k: job.get(k) for k in KEPT}  # for /again and /vary
    one = replace(options, n=1)
    plan = media.Plan(model, None, {}, meta, job["prompt"], 0, one, job["kind"], sources, remembered)  # type: ignore[arg-type]
    made = media.Made([path], model.name, 0, width, height, time.monotonic() - line.started, job["prompt"])
    made.numbers = media.record(agent, made, plan)
    return made
