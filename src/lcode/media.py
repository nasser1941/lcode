"""Image and video mode: a request becomes media, made by ComfyUI (lcode.comfy) with no language model in between.

Image mode makes one image per request with the image model chosen from the media catalog (lcode.media_catalog);
sizes, variations and a gallery come with #151, video with #153.
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from rich.text import Text

from lcode import comfy, imageopts, preview
from lcode import media_catalog as catalog

if TYPE_CHECKING:
    from lcode.agent import Agent

SOON = {"video": "Video models come with video generation (#153)."}
IGNORED = "# lcode's images and videos: not part of the project unless you add them\n*\n"


class MediaError(Exception):
    pass


def installed_lister(agent: Agent) -> catalog.Installed | None:
    """What ComfyUI has installed: asked from it when it's running, else read from its folders; None: unknown."""
    server = comfy.Server(agent.settings.comfyui_url)
    if server.stats():
        return catalog.lister(server)
    install = comfy.find_install(agent.settings.comfyui_path)
    return catalog.lister(install=install) if install else None


def current(agent: Agent) -> catalog.MediaModel:
    """The image model the agent uses (the image_model setting, or /model in image mode)."""
    cat = catalog.load()
    return cat.find(agent.settings.image_model) or cat.models[catalog.DEFAULT_IMAGE]


@dataclass
class Made:
    files: list[Path]
    model: str
    seed: int
    width: int
    height: int
    seconds: float
    prompt: str = ""
    numbers: list[int] = field(default_factory=list)  # in the session's gallery
    thumbnail: bytes = b""  # a small PNG, for the half-block preview


# ----------------------------------------------------------------------------- ComfyUI, started and stopped


def engine(agent: Agent, ask: Callable[[str], bool] | None = None, cancelled=None) -> comfy.Server:
    """ComfyUI, answering: one that's already running, or the install lcode finds (or installs), started now."""
    server = comfy.Server(agent.settings.comfyui_url)
    if server.stats():
        return server
    proc = agent.comfy_process
    if proc is None or proc.poll() is not None:
        install = comfy.find_install(agent.settings.comfyui_path) or offer_install(agent, server, ask)
        proc = agent.comfy_process = comfy.launch(install, server.url)
    with agent.console.status("Starting ComfyUI (the first start takes a while)…", spinner="dots"):
        try:
            stats = server.wait(proc, cancelled=cancelled)
        except comfy.ComfyError as e:
            comfy.stop(proc)
            agent.comfy_process = None
            raise MediaError(str(e)) from e
    version = (stats.get("system") or {}).get("comfyui_version", "")
    agent.say(f"ComfyUI {version} is running at {server.url}; lcode stops it when you quit or go back to code mode.")
    return server


def offer_install(agent: Agent, server: comfy.Server, ask: Callable[[str], bool] | None) -> comfy.Install:
    folder = comfy.own_folder()
    question = (
        f"ComfyUI runs the image and video models, and there's none here. Install it into {folder} with "
        "comfy-cli? It downloads PyTorch (several GB)."
    )
    if ask is None or not ask(question):
        raise MediaError(f"No ComfyUI at {server.url}, and no install found. {comfy.install_hint()}")
    from lcode.hardware import detect

    try:
        install = comfy.install(detect())
    except comfy.ComfyError as e:
        raise MediaError(f"{e}. {comfy.install_hint()}") from e
    agent.say(f"Installed ComfyUI into {install.path}.")
    return install


def warm(agent: Agent) -> str | None:
    """Start ComfyUI in the background on entering image or video mode, so the first request waits less."""
    if agent.comfy_process is not None and agent.comfy_process.poll() is None:
        return None
    if comfy.Server(agent.settings.comfyui_url).stats():
        return None
    install = comfy.find_install(agent.settings.comfyui_path)
    if install is None:
        return "ComfyUI isn't installed; lcode offers to install it when you make your first image."
    agent.comfy_process = comfy.launch(install, agent.settings.comfyui_url)
    return f"Starting ComfyUI ({install.path}) in the background."


def stop_engine(agent: Agent) -> bool:
    """Stop the ComfyUI lcode started (one you started yourself keeps running). Whether there was one."""
    proc, agent.comfy_process = agent.comfy_process, None
    if proc is None or proc.poll() is not None:
        return False
    comfy.stop(proc)
    return True


def free_gpu(agent: Agent) -> None:
    """Unload lcode's coding model if Ollama still has it: the image or video model needs the GPU."""
    if getattr(agent.ollama, "kind", "ollama") != "ollama":
        return
    try:
        loaded = {name for m in agent.ollama.running() for name in (m.get("name"), m.get("model"))}
    except Exception:
        return
    model = agent.settings.model
    if loaded & {model, f"{model}:latest"}:
        try:
            agent.ollama.unload(model)
            agent.say(f"Unloaded {model} to free the GPU.")
        except Exception:
            pass


# ----------------------------------------------------------------------------- where the files go


def folder(agent: Agent) -> Path:
    """The media folder: `media_dir`, in the project unless it's an absolute path."""
    path = Path(agent.settings.media_dir).expanduser()
    path = path if path.is_absolute() else agent.workspace / path
    path.mkdir(parents=True, exist_ok=True)
    ignore = path / ".gitignore"
    if ".lcode" in path.parts and not ignore.exists():  # lcode's own folder: kept out of git, like screenshots
        ignore.write_text(IGNORED)
    return path


def shown(agent: Agent, path: Path) -> str:
    try:
        return str(path.relative_to(agent.cwd))
    except ValueError:
        return str(path)


# ----------------------------------------------------------------------------- a job


class Line:
    """The status line while ComfyUI works: what it's doing, the step, time left. Re-rendered on every frame."""

    def __init__(self, name: str, hint: str = "") -> None:
        self.name, self.hint = name, hint
        self.started = time.monotonic()
        self.doing = "waiting for ComfyUI"
        self.step: tuple[int, int] | None = None
        self.first: tuple[float, int] | None = None  # (when, step) of the first step, to estimate the rest

    def progress(self, value: int, total: int) -> None:
        now = time.monotonic()
        if self.first is None or value < self.first[1]:
            self.first = (now, value)
        self.step = (value, total)

    def left(self) -> float | None:
        if not self.step or not self.first or self.step[0] <= self.first[1]:
            return None
        spent, done = time.monotonic() - self.first[0], self.step[0] - self.first[1]
        return spent / done * (self.step[1] - self.step[0])

    def __rich__(self) -> Text:
        parts = [self.doing]
        if self.step:
            parts.append(f"step {self.step[0]}/{self.step[1]}")
            left = self.left()
            if left is not None and self.step[0] < self.step[1]:
                parts.append(f"~{left:.0f} s left")
        parts.append(f"{time.monotonic() - self.started:.0f} s")
        text = Text(f"{self.name}: ", style="bold") + Text(" · ".join(parts))
        if self.hint:
            text += Text(f"  ({self.hint})", style="dim")
        return text


def ensure_installed(
    agent: Agent, model: catalog.MediaModel, server: comfy.Server, ask: Callable[[str], bool] | None
) -> dict[str, str]:
    """The model's files (role -> installed name), downloading the missing ones first when the user agrees."""
    files, missing = catalog.chosen_files(model, catalog.lister(server))
    if not missing:
        return files
    size = sum(f.gb for f in missing)
    if ask is None:
        raise MediaError(f"{model.name} isn't installed ({size:.1f} GB to download): lcode media install {model.key}")
    install = comfy.find_install(agent.settings.comfyui_path)
    try:
        catalog.install(model, server=server, install=install, console=agent.console, ask=ask)
    except catalog.CatalogError as e:
        raise MediaError(str(e)) from e
    files, missing = catalog.chosen_files(model, catalog.lister(server))
    if missing:
        raise MediaError(f"ComfyUI doesn't see {', '.join(f.name for f in missing)} yet: restart it and try again")
    agent.say(f"Installed {model.name}.")
    return files


THUMB_NODES = ("9001", "9002")  # the thumbnail lcode adds to a workflow, for the half-block preview


def add_thumbnail(graph: dict, width: int, height: int) -> None:
    """Have ComfyUI also make a small copy of the image (not saved, a preview), for the half-block preview."""
    save = next((nid for nid, node in graph.items() if node.get("class_type") == "SaveImage"), None)
    if save is None:
        return
    tw, th = preview.thumbnail_size(width, height)
    scale, show = THUMB_NODES
    graph[scale] = {
        "class_type": "ImageScale",
        "inputs": {"image": graph[save]["inputs"]["images"], "upscale_method": "area", "width": tw, "height": th,
                   "crop": "disabled"},
    }  # fmt: skip
    graph[show] = {"class_type": "PreviewImage", "inputs": {"images": [scale, 0]}}


def thumbnail(server: comfy.Server, outputs: dict) -> bytes:
    for item in (outputs.get(THUMB_NODES[1]) or {}).get("images", []):
        try:
            return server.fetch(item)
        except (comfy.ComfyError, OSError, requests.RequestException):
            return b""
    return b""


def request(
    agent: Agent, text: str, cancelled: Callable[[], bool] = lambda: False, ask: Callable[[str], bool] | None = None
) -> list[Made]:
    """A typed request: the prompt and its flags (--n 4 --ar 16:9 …, over the session's /options), then the images."""
    try:
        prompt, options = imageopts.parse(text, agent.image_options)
    except imageopts.OptionError as e:
        raise MediaError(str(e)) from e
    return make(agent, prompt, options, cancelled, ask)


def make(
    agent: Agent,
    prompt: str,
    options: imageopts.Options,
    cancelled: Callable[[], bool] = lambda: False,
    ask: Callable[[str], bool] | None = None,
) -> list[Made]:
    """`options.n` images of the prompt; a fixed seed counts up from image to image."""
    if not prompt.strip():
        raise MediaError("Describe the image you want.")
    model = current(agent)
    if options.negative and not model.negative:
        agent.say(f"{model.name} doesn't use a negative prompt: describe what you want instead (--no is ignored).")
    agent.last_request = (prompt, options)
    made = []
    for i in range(options.n):
        seed = None if options.seed is None else options.seed + i
        label = f"{i + 1} of {options.n}" if options.n > 1 else ""
        made.append(generate_image(agent, prompt, cancelled, ask, replace(options, seed=seed, n=1), label))
    return made


def generate_image(
    agent: Agent,
    prompt: str,
    cancelled: Callable[[], bool] = lambda: False,
    ask: Callable[[str], bool] | None = None,
    options: imageopts.Options | None = None,
    label: str = "",
) -> Made:
    """One image from the prompt, saved in the media folder and numbered. Raises MediaError with what to do."""
    prompt = prompt.strip()
    if not prompt:
        raise MediaError("Describe the image you want.")
    options = options or imageopts.Options()
    server = engine(agent, ask, cancelled)
    for path in comfy.collect(server):
        agent.say(f"Saved {shown(agent, path)}: it finished while lcode wasn't watching.")
    model = current(agent)
    files = ensure_installed(agent, model, server, ask)
    workflow = comfy.load_workflow(model.workflow)
    free_gpu(agent)
    seed = options.seed if options.seed is not None else random.randrange(2**32)
    steps, megapixels = model.preset(options.quality)
    width, height = imageopts.dimensions(options.size, megapixels)
    text = imageopts.styled(prompt, options.style)
    values = {**workflow.defaults, **files, "prompt": text, "seed": seed, "width": width, "height": height}
    if steps:
        values["steps"] = steps
    if model.negative:
        values["negative"] = options.negative
    graph = comfy.fill(workflow.graph, values)
    add_thumbnail(graph, width, height)
    meta = {
        "prompt": prompt,
        "model": model.name,
        "model_key": model.key,
        "workflow": workflow.key,
        "seed": seed,
        "width": width,
        "height": height,
        "steps": values["steps"],
        "quality": options.quality,
        "style": options.style,
        "negative": options.negative if model.negative else "",
        "full_prompt": text,
        "files": files,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    out = folder(agent)
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{comfy.slug(prompt)}"
    line = Line(
        f"{model.name} ({label})" if label else model.name, "Esc or Ctrl+C cancels" if agent.interactive else ""
    )
    job = {"id": ""}
    labels = {**workflow.labels, THUMB_NODES[0]: "making the preview", THUMB_NODES[1]: "making the preview"}

    def on_event(event: comfy.Event) -> None:
        if event.kind == "queued":
            job["id"] = event.prompt_id
            comfy.remember(event.prompt_id, server.url, out, stem, meta)
        elif event.kind == "waiting":
            line.doing = f"waiting: {event.value} job(s) ahead in ComfyUI's queue"
        elif event.kind == "node":
            line.doing, line.step = labels.get(event.node, "working"), None
        elif event.kind == "progress" and event.total:
            line.doing = labels.get(event.node, line.doing)
            line.progress(event.value, event.total)

    try:
        with agent.console.status(line, spinner="dots"):
            prompt_id, outputs = comfy.run(server, graph, on_event, cancelled)
    except (KeyboardInterrupt, comfy.Cancelled) as e:
        if job["id"]:
            if isinstance(e, KeyboardInterrupt):  # run() cancels it in ComfyUI itself for Esc
                server.cancel(job["id"])
            comfy.forget(job["id"])
        raise MediaError("Cancelled: ComfyUI stopped the job.") from None
    except comfy.ComfyError as e:
        if job["id"]:
            comfy.forget(job["id"])
        raise MediaError(str(e)) from e
    try:
        saved = comfy.save(server, outputs, out, stem, {**meta, "comfyui_prompt_id": prompt_id})
    except (comfy.ComfyError, OSError) as e:
        raise MediaError(f"ComfyUI made it, but lcode couldn't save it: {e}") from e
    comfy.forget(prompt_id)
    if not saved:
        raise MediaError("ComfyUI finished but returned no image.")
    made = Made(saved, model.name, seed, width, height, time.monotonic() - line.started, prompt)
    made.thumbnail = thumbnail(server, outputs)
    made.numbers = record(agent, made, meta, options)
    show(agent, made)
    return made


# ----------------------------------------------------------------------------- results: numbered, shown, opened


def record(agent: Agent, made: Made, meta: dict, options: imageopts.Options) -> list[int]:
    """Number the images in the session's gallery, with what made them (for /gallery, /vary, /open)."""
    numbers = []
    for path in made.files:
        number = len(agent.media_results) + 1
        agent.media_results.append(
            {
                "n": number,
                "path": str(path),
                "prompt": made.prompt,
                "model": made.model,
                "seed": made.seed,
                "width": made.width,
                "height": made.height,
                "steps": meta["steps"],
                "seconds": round(made.seconds, 1),
                "options": options.to_json(),
            }
        )
        numbers.append(number)
    return numbers


def show(agent: Agent, made: Made) -> None:
    for path, number in zip(made.files, made.numbers or [0] * len(made.files), strict=False):
        agent.console.print(
            Text("◆ ", style="green")
            + Text(f"#{number} " if number else "", style="bold green")
            + Text(shown(agent, path), style="bold")
            + Text(
                f"  {made.width}×{made.height} · {made.model} · seed {made.seed} · {made.seconds:.1f} s", style="dim"
            )
        )
    if made.files:
        show_preview(agent, made.files[0], made.thumbnail)


def show_preview(agent: Agent, path: Path, thumb: bytes = b"") -> None:
    """The image in the terminal: itself where the terminal can show pictures, else a half-block preview."""
    setting = agent.settings.preview
    how = preview.protocol(setting)
    if how == "off" or (setting == "auto" and not agent.console.is_terminal):
        return
    try:
        if how in ("kitty", "iterm"):
            data = path.read_bytes()
            sequence = preview.kitty(data) if how == "kitty" else preview.iterm(data)
            agent.console.file.write(sequence)
            agent.console.file.flush()
        elif thumb:
            agent.console.print(preview.blocks(thumb))
    except (OSError, preview.PngError):
        pass


def result(agent: Agent, which: str) -> dict:
    """A gallery entry by its number (#3 or 3), or the last one."""
    if not agent.media_results:
        raise MediaError("No images in this session yet.")
    key = which.strip().lstrip("#")
    if not key:
        return agent.media_results[-1]
    if not key.isdigit() or not 1 <= int(key) <= len(agent.media_results):
        raise MediaError(f"There's no image #{key}: this session has #1 to #{len(agent.media_results)} (/gallery).")
    return agent.media_results[int(key) - 1]


def open_file(path: Path) -> str:
    """Open a file in the system's viewer. An error message, or empty."""
    if not path.exists():
        return f"{path} isn't there any more."
    if sys.platform == "darwin":
        command = ["open", str(path)]
    elif sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
        return ""
    else:
        opener = shutil.which("xdg-open") or shutil.which("gio")
        if not opener:
            return "No program to open it with (xdg-open isn't installed): open the file yourself."
        command = [opener, str(path)] if opener.endswith("xdg-open") else [opener, "open", str(path)]
    try:
        subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )  # fmt: skip
    except OSError as e:
        return f"Couldn't open it: {e}"
    return ""
