"""Image and video mode: a request becomes media, made by ComfyUI (lcode.comfy) with no language model in between.

Image mode makes one image per request with the image model chosen from the media catalog (lcode.media_catalog);
sizes, variations and a gallery come with #151, video with #153.
"""

from __future__ import annotations

import os
import random
import re
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

from lcode import comfy, edits, imageopts, preview
from lcode import media_catalog as catalog

if TYPE_CHECKING:
    from lcode.agent import Agent

SOON: dict[str, str] = {}
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


def current(agent: Agent, mode: str | None = None) -> catalog.MediaModel:
    """The model the agent uses in image or video mode (image_model, video_model, or /model there)."""
    cat = catalog.load()
    if (mode or agent.settings.mode) == "video":
        return cat.find(agent.settings.video_model) or cat.models[catalog.DEFAULT_VIDEO]
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
    poster: bytes = b""  # a video's first frame, for terminals that show pictures


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
POSTER_NODES = ("9003", "9004")  # a video's first frame, picked and kept (not saved) for the preview


def add_thumbnail(graph: dict, width: int, height: int) -> None:
    """Have ComfyUI also make a small copy of the image, or of a video's first frame (not saved, a preview)."""
    save = next((nid for nid, node in graph.items() if node.get("class_type") == "SaveImage"), None)
    video = next((nid for nid, node in graph.items() if node.get("class_type") == "CreateVideo"), None)
    if save is not None:
        frames = graph[save]["inputs"]["images"]
    elif video is not None:
        first, keep = POSTER_NODES
        graph[first] = {
            "class_type": "ImageFromBatch",
            "inputs": {"image": graph[video]["inputs"]["images"], "batch_index": 0, "length": 1},
        }
        graph[keep] = {"class_type": "PreviewImage", "inputs": {"images": [first, 0]}}
        frames = [first, 0]
    else:
        return
    tw, th = preview.thumbnail_size(width, height)
    scale, show = THUMB_NODES
    graph[scale] = {
        "class_type": "ImageScale",
        "inputs": {"image": frames, "upscale_method": "area", "width": tw, "height": th, "crop": "disabled"},
    }
    graph[show] = {"class_type": "PreviewImage", "inputs": {"images": [scale, 0]}}


def thumbnail(server: comfy.Server, outputs: dict, node: str = THUMB_NODES[1]) -> bytes:
    for item in (outputs.get(node) or {}).get("images", []):
        try:
            return server.fetch(item)
        except (comfy.ComfyError, OSError, requests.RequestException):
            return b""
    return b""


SIZE_FLAG = re.compile(r"(?:^|\s)--?(ar|aspect|size)\b")
MASK_FLAG = re.compile(r'(?:^|\s)--mask\s+("[^"]+"|\S+)')


def request(
    agent: Agent, text: str, cancelled: Callable[[], bool] = lambda: False, ask: Callable[[str], bool] | None = None
) -> list[Made]:
    """A typed request: its @references (images to edit or follow), --mask, and flags (--n 4 --ar 16:9 …, over
    the session's /options), then the images."""
    mask = None
    m = MASK_FLAG.search(text)
    if m:
        mask = resolve(agent, m.group(1).strip('"'))
        text = MASK_FLAG.sub(" ", text)
    text, refs = edits.split_references(text)
    try:
        prompt, options = imageopts.parse(text, agent.image_options)
    except imageopts.OptionError as e:
        raise MediaError(str(e)) from e
    sources = [resolve(agent, ref) for ref in refs] + [Path(p) for p in agent.image_refs if Path(p).exists()]
    if mask and not sources:
        raise MediaError("--mask needs the image it masks: @photo.png --mask mask.png make the sky purple")
    if agent.settings.mode == "video":
        if mask:
            raise MediaError("Video mode doesn't take a mask: @photo.png starts a video from a picture.")
        job = {"kind": "video", "prompt": prompt, "options": options, "sources": [str(p) for p in sources[:1]],
               "mask": None, "pads": None, "keep_shape": not SIZE_FLAG.search(text)}  # fmt: skip
        return redo(agent, job, cancelled, ask)
    job = {"kind": "edit" if sources else "image", "prompt": prompt, "options": options,
           "sources": [str(p) for p in sources], "mask": str(mask) if mask else None, "pads": None,
           "keep_shape": not SIZE_FLAG.search(text)}  # fmt: skip
    return redo(agent, job, cancelled, ask)


def redo(
    agent: Agent, job: dict, cancelled: Callable[[], bool] = lambda: False, ask: Callable[[str], bool] | None = None
) -> list[Made]:
    """Make what `job` describes (also what /again and /vary repeat): images, or edits of its sources."""
    prompt, options = job["prompt"], job["options"]
    if not prompt.strip() and job["kind"] in ("image", "edit", "video") and not job.get("pads"):
        what = {"image": "Describe the image you want.", "edit": "Say what to change."}
        raise MediaError(what.get(job["kind"], "Describe the video you want."))
    model = current(agent, "video" if job["kind"] == "video" else "image")
    if options.negative and not model.negative:
        agent.say(f"{model.name} doesn't use a negative prompt: describe what you want instead (--no is ignored).")
    agent.last_request = job
    made = []
    for i in range(options.n):
        seed = None if options.seed is None else options.seed + i
        label = f"{i + 1} of {options.n}" if options.n > 1 else ""
        one = replace(options, seed=seed, n=1)
        if job["kind"] == "image":
            made.append(generate_image(agent, prompt, cancelled, ask, one, label))
        elif job["kind"] == "video":
            made.append(generate_video(agent, job, one, cancelled, ask, label))
        else:
            made.append(edit_image(agent, job, one, cancelled, ask, label))
    return made


def make(
    agent: Agent,
    prompt: str,
    options: imageopts.Options,
    cancelled: Callable[[], bool] = lambda: False,
    ask: Callable[[str], bool] | None = None,
) -> list[Made]:
    """`options.n` images of the prompt; a fixed seed counts up from image to image."""
    job = {"kind": "image", "prompt": prompt, "options": options, "sources": [], "mask": None, "pads": None}
    return redo(agent, job, cancelled, ask)


def resolve(agent: Agent, ref: str) -> Path:
    """An image the user points at: #3 (a result) or a file path."""
    ref = ref.strip()
    if re.fullmatch(r"#?\d+", ref) and ref.startswith("#"):
        return Path(result(agent, ref)["path"])
    path = Path(ref).expanduser()
    path = path if path.is_absolute() else agent.cwd / path
    if not path.is_file():
        raise MediaError(f"There's no image {ref}: give a file (@photo.png) or a result's number (@#3).")
    if not edits.is_image(path):
        raise MediaError(f"{ref} isn't an image lcode can use: PNG, JPEG, WebP or BMP.")
    return path


@dataclass
class Plan:
    """A job ready for ComfyUI: the graph, what to call it, and what goes in its JSON file."""

    model: catalog.MediaModel
    workflow: comfy.Workflow
    graph: dict
    meta: dict
    prompt: str
    seed: int
    options: imageopts.Options
    kind: str = "image"
    sources: list[str] = field(default_factory=list)
    job: dict = field(default_factory=dict)  # what made it, for /vary (prompt, sources, mask, pads)
    stem: str = ""  # the file name without its extension, when not the time and the prompt


def prepare(agent: Agent, ask, cancelled) -> comfy.Server:
    server = engine(agent, ask, cancelled)
    for path in comfy.collect(server):
        agent.say(f"Saved {shown(agent, path)}: it finished while lcode wasn't watching.")
    return server


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
    server = prepare(agent, ask, cancelled)
    model = current(agent, "image")
    files = ensure_installed(agent, model, server, ask)
    workflow = comfy.load_workflow(model.workflow)
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
    meta = describe_job(model, workflow, prompt, text, seed, values, options, files)
    return run_plan(agent, server, Plan(model, workflow, graph, meta, prompt, seed, options), cancelled, label)


def edit_image(
    agent: Agent,
    job: dict,
    options: imageopts.Options,
    cancelled: Callable[[], bool] = lambda: False,
    ask: Callable[[str], bool] | None = None,
    label: str = "",
) -> Made:
    """An edit of the job's source images: by instruction, through a mask (inpainting), or on a bigger canvas
    (outpainting). The first source sets the shape unless the request gave one."""
    server = prepare(agent, ask, cancelled)
    chosen = current(agent, "image")
    model = catalog.load().editor(chosen)
    if model is None:
        raise MediaError("None of lcode's image models edits pictures yet.")
    if model.key != chosen.key:
        agent.say(f"{chosen.name} doesn't edit pictures: {model.name} makes this one.")
    files = ensure_installed(agent, model, server, ask)
    workflow = comfy.load_workflow(model.workflow)
    sources = [Path(p) for p in job["sources"]]
    try:
        names = [server.upload(p) for p in sources]
        mask_name = server.upload(Path(job["mask"])) if job.get("mask") else None
        first_w, first_h = edits.image_size(sources[0])
    except (comfy.ComfyError, OSError, edits.EditError) as e:
        raise MediaError(f"Couldn't hand the images to ComfyUI: {e}") from e
    pads = job.get("pads")
    if pads:
        first_w, first_h = first_w + pads[0] + pads[2], first_h + pads[1] + pads[3]
    seed = options.seed if options.seed is not None else random.randrange(2**32)
    steps, megapixels = model.preset(options.quality)
    keep_shape = bool(job.get("keep_shape", True) or job.get("mask") or pads)
    ratio = f"{first_w}x{first_h}" if keep_shape else options.size
    width, height = shape(ratio, megapixels)
    instruction = job["prompt"] if not pads else f"{edits.EXTEND_HINT} {job['prompt']}".strip()
    text = imageopts.styled(instruction, options.style)
    values = {**workflow.defaults, **files, "prompt": text, "seed": seed, "width": width, "height": height}
    if steps:
        values["steps"] = steps
    if model.negative:
        values["negative"] = options.negative
    graph = comfy.fill(workflow.graph, values)
    try:
        links = edits.load_images(graph, names)
        if pads:  # the original is the reference; the picture is the padded canvas, painted only in its border
            canvas, border = edits.pad_canvas(graph, links[0], pads)
            canvas = edits.add_references(graph, [canvas, *links[1:]], megapixels, size_from_first=True)
            edits.outpaint(graph, canvas, border, megapixels)
        else:
            first = edits.add_references(graph, links, megapixels, size_from_first=keep_shape)
            if mask_name:
                edits.inpaint(graph, first, mask_name)
    except edits.EditError as e:
        raise MediaError(f"{model.name} can't do that edit: {e}") from e
    add_thumbnail(graph, width, height)
    meta = describe_job(model, workflow, job["prompt"], text, seed, values, options, files)
    meta.update(
        {"edit_of": [shown(agent, p) for p in sources], "mask": shown(agent, Path(job["mask"])) if job.get("mask")
         else "", "extend": list(pads) if pads else []}
    )  # fmt: skip
    kind = "extend" if pads else ("inpaint" if job.get("mask") else "edit")
    remembered = {k: job.get(k) for k in ("kind", "prompt", "sources", "mask", "pads", "keep_shape")}
    plan = Plan(model, workflow, graph, meta, job["prompt"] or "extended", seed, options, kind, job["sources"],
                remembered, f"{sources[0].stem}-extended" if pads else "")  # fmt: skip
    return run_plan(agent, server, plan, cancelled, label)


def generate_video(
    agent: Agent,
    job: dict,
    options: imageopts.Options,
    cancelled: Callable[[], bool] = lambda: False,
    ask: Callable[[str], bool] | None = None,
    label: str = "",
) -> Made:
    """One video: from the prompt, or starting from the job's image. Its shape is 16:9, or 9:16 for an upright start
    image (the shapes video models know best; the image is cropped to it), unless the request gives one."""
    prompt = job["prompt"].strip()
    server = prepare(agent, ask, cancelled)
    model = current(agent, "video")
    files = ensure_installed(agent, model, server, ask)
    workflow = comfy.load_workflow(model.workflow)
    start = Path(job["sources"][0]) if job.get("sources") else None
    seed = options.seed if options.seed is not None else random.randrange(2**32)
    steps, megapixels = model.preset(options.quality)
    ratio = options.size
    if job.get("keep_shape", True) and options.size == imageopts.Options().size:  # no shape asked for
        ratio = "16:9"
        if start is not None:
            try:
                w, h = edits.image_size(start)
                ratio = "9:16" if h > w else "16:9"  # a square or odd shape breaks up; this crops it
            except (OSError, edits.EditError):
                pass
    width, height = imageopts.dimensions(imageopts.SIZES.get(ratio, ratio), megapixels, multiple=32)
    fps = int(workflow.defaults.get("fps", 24))
    text = imageopts.styled(prompt, options.style)
    values = {**workflow.defaults, **files, "prompt": text, "seed": seed, "width": width, "height": height,
              "length": imageopts.frames(options.seconds, fps), "fps": fps}  # fmt: skip
    if steps:
        values["steps"] = steps
    if options.negative:
        values["negative"] = options.negative
    graph = comfy.fill(workflow.graph, values)
    if start is not None:
        try:
            name = server.upload(start)
        except (comfy.ComfyError, OSError) as e:
            raise MediaError(f"Couldn't hand the image to ComfyUI: {e}") from e
        latent = edits.node_of(graph, "Wan22ImageToVideoLatent")
        if latent is None:
            raise MediaError(f"{model.name} can't start from an image.")
        graph["8100"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        graph[latent]["inputs"]["start_image"] = ["8100", 0]
    add_thumbnail(graph, width, height)
    meta = describe_job(model, workflow, prompt, text, seed, values, options, files)
    meta.update({"seconds": round((values["length"] - 1) / fps, 2), "fps": fps, "frames": values["length"],
                 "start_image": shown(agent, start) if start else ""})  # fmt: skip
    remembered = {k: job.get(k) for k in ("kind", "prompt", "sources", "mask", "pads", "keep_shape")}
    plan = Plan(model, workflow, graph, meta, prompt, seed, options, "video", job.get("sources") or [], remembered)
    return run_plan(agent, server, plan, cancelled, label)


def apply_tool(
    agent: Agent,
    task: str,
    source: Path,
    scale: int = 2,
    cancelled: Callable[[], bool] = lambda: False,
    ask: Callable[[str], bool] | None = None,
) -> Made:
    """/upscale and /nobg: a tool model on one image (no seed, no prompt)."""
    server = prepare(agent, ask, cancelled)
    model = catalog.load().tool(task)
    files = ensure_installed(agent, model, server, ask)
    workflow = comfy.load_workflow(model.workflow)
    try:
        name = server.upload(source)
        width, height = edits.image_size(source)
    except (comfy.ComfyError, OSError, edits.EditError) as e:
        raise MediaError(f"Couldn't hand the image to ComfyUI: {e}") from e
    values = {**workflow.defaults, **files, "image": name}
    if task == "upscale":
        values["scale_by"] = scale / 4  # Real-ESRGAN makes 4 times the size; scaled back for 2
        width, height = width * scale, height * scale
    graph = comfy.fill(workflow.graph, values)
    add_thumbnail(graph, width, height)
    what = f"upscaled {scale}×" if task == "upscale" else "background removed"
    prompt = f"{what}: {shown(agent, source)}"
    meta = {
        "prompt": prompt,
        "model": model.name,
        "model_key": model.key,
        "workflow": workflow.key,
        "source": shown(agent, source),
        "files": files,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    suffix = f"upscaled-{scale}x" if task == "upscale" else "nobg"
    plan = Plan(model, workflow, graph, meta, prompt, 0, imageopts.Options(), task, [str(source)], {},
                f"{source.stem}-{suffix}")  # fmt: skip
    return run_plan(agent, server, plan, cancelled, "")


def shape(size: str, megapixels: float) -> tuple[int, int]:
    """Pixels for an edit: a ratio or a source's own pixels, brought to the quality's megapixels."""
    m = edits.re.fullmatch(r"(\d+)x(\d+)", size)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        return imageopts.dimensions(f"{w}:{h}" if w < 100 and h < 100 else ratio_of(w, h), megapixels)
    return imageopts.dimensions(size, megapixels)


def ratio_of(width: int, height: int) -> str:
    """A ratio string close to width:height, with small numbers (for imageopts.dimensions)."""
    from fractions import Fraction

    f = Fraction(width, height).limit_denominator(32)
    return f"{f.numerator}:{f.denominator}" if f.numerator < 100 else f"{round(width / height * 16)}:16"


def describe_job(model, workflow, prompt, text, seed, values, options, files) -> dict:
    return {
        "prompt": prompt,
        "model": model.name,
        "model_key": model.key,
        "workflow": workflow.key,
        "seed": seed,
        "width": values["width"],
        "height": values["height"],
        "steps": values.get("steps"),
        "quality": options.quality,
        "style": options.style,
        "negative": options.negative if model.negative else "",
        "full_prompt": text,
        "files": files,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def run_plan(agent: Agent, server: comfy.Server, plan: Plan, cancelled, label: str) -> Made:
    """Queue the plan, follow it, save what it made, number it, show it."""
    free_gpu(agent)
    out = folder(agent)
    stem = plan.stem or f"{time.strftime('%Y%m%d-%H%M%S')}-{comfy.slug(plan.prompt)}"
    hint = "Esc or Ctrl+C cancels" if agent.interactive else ""
    line = Line(f"{plan.model.name} ({label})" if label else plan.model.name, hint)
    job = {"id": ""}
    labels = {**plan.workflow.labels, THUMB_NODES[0]: "making the preview", THUMB_NODES[1]: "making the preview"}

    def on_event(event: comfy.Event) -> None:
        if event.kind == "queued":
            job["id"] = event.prompt_id
            comfy.remember(event.prompt_id, server.url, out, stem, plan.meta)
        elif event.kind == "waiting":
            line.doing = f"waiting: {event.value} job(s) ahead in ComfyUI's queue"
        elif event.kind == "node":
            line.doing, line.step = labels.get(event.node, "working"), None
        elif event.kind == "progress" and event.total:
            line.doing = labels.get(event.node, line.doing)
            line.progress(event.value, event.total)

    try:
        with agent.console.status(line, spinner="dots"):
            prompt_id, outputs = comfy.run(server, plan.graph, on_event, cancelled)
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
        saved = comfy.save(server, outputs, out, stem, {**plan.meta, "comfyui_prompt_id": prompt_id})
    except (comfy.ComfyError, OSError) as e:
        raise MediaError(f"ComfyUI made it, but lcode couldn't save it: {e}") from e
    comfy.forget(prompt_id)
    if not saved:
        raise MediaError("ComfyUI finished but returned no image.")
    width, height = plan.meta.get("width") or 0, plan.meta.get("height") or 0
    if plan.kind != "image":  # an edit's or a tool's size comes from its source: read what ComfyUI made
        try:
            width, height = edits.image_size(saved[0])
        except (OSError, edits.EditError):
            pass
    made = Made(saved, plan.model.name, plan.seed, width, height, time.monotonic() - line.started, plan.prompt)
    made.thumbnail = thumbnail(server, outputs)
    if plan.kind == "video":
        made.poster = thumbnail(server, outputs, POSTER_NODES[1])
    made.numbers = record(agent, made, plan)
    show(agent, made)
    return made


# ----------------------------------------------------------------------------- results: numbered, shown, opened


def record(agent: Agent, made: Made, plan: Plan) -> list[int]:
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
                "steps": plan.meta.get("steps"),
                "seconds": round(made.seconds, 1),
                "options": plan.options.to_json(),
                "kind": plan.kind,
                "sources": plan.sources,
                "mask": plan.meta.get("mask", ""),
                "extend": plan.meta.get("extend", []),
                "job": plan.job,
            }
        )
        numbers.append(number)
    return numbers


def show(agent: Agent, made: Made) -> None:
    seed = f" · seed {made.seed}" if made.seed else ""  # tools (upscaling, cut-outs) have none
    for path, number in zip(made.files, made.numbers or [0] * len(made.files), strict=False):
        agent.console.print(
            Text("◆ ", style="green")
            + Text(f"#{number} " if number else "", style="bold green")
            + Text(shown(agent, path), style="bold")
            + Text(f"  {made.width}×{made.height} · {made.model}{seed} · {made.seconds:.1f} s", style="dim")
        )
    if made.files:
        show_preview(agent, made.files[0], made.thumbnail, made.poster)


def show_preview(agent: Agent, path: Path, thumb: bytes = b"", poster: bytes = b"") -> None:
    """The image (or a video's first frame) in the terminal: itself where the terminal can show pictures, else a
    half-block preview."""
    setting = agent.settings.preview
    how = preview.protocol(setting)
    if how == "off" or (setting == "auto" and not agent.console.is_terminal):
        return
    try:
        if how in ("kitty", "iterm"):
            data = path.read_bytes() if edits.is_image(path) else poster
            if not data:
                return
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
