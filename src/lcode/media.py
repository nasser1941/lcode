"""Image and video mode: a request becomes media, made by ComfyUI (lcode.comfy) with no language model in between.

Image mode makes one image per request with the image model chosen from the media catalog (lcode.media_catalog);
sizes, variations and a gallery come with #151, video with #153.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from rich.text import Text

from lcode import assistant, comfy, edits, imageopts, preview
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
RAW_FLAG = re.compile(r"(?:^|\s)--raw\b")
MASK_FLAG = re.compile(r'(?:^|\s)--mask\s+("[^"]+"|\S+)')
REF_FLAG = re.compile(r"(?:^|\s)--refs?\b")


def request(
    agent: Agent, text: str, cancelled: Callable[[], bool] = lambda: False, ask: Callable[[str], bool] | None = None
) -> list[Made]:
    """A typed request: its @references (images to edit or follow), --mask, and flags (--n 4 --ar 16:9 …, over
    the session's /options), then the images."""
    as_references = bool(REF_FLAG.search(text))
    text = REF_FLAG.sub(" ", text)
    raw = bool(RAW_FLAG.search(text))  # the prompt exactly as typed: no assistant
    text = RAW_FLAG.sub(" ", text)
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
        if as_references and not sources:
            raise MediaError("--ref makes the @pictures references: @cat.png @hat.png --ref the cat wears the hat")
        frames = [] if as_references else [str(p) for p in sources[:2]]
        refs = [str(p) for p in sources] if as_references else []
        job = {"kind": "video", "prompt": prompt, "options": options, "sources": frames, "mask": None, "pads": None,
               "keep_shape": not SIZE_FLAG.search(text), "refs": refs, "raw": raw}  # fmt: skip
        return redo(agent, job, cancelled, ask)
    if not sources and not mask and not raw:
        change = follow_up(agent, prompt)
        if change:  # "darker", "add a hat": the last picture, changed
            last = agent.media_results[-1]
            agent.console.print(Text(f"✎ A change to #{last['n']}: {change}", style="dim italic"))
            job = {"kind": "edit", "prompt": change, "options": options, "sources": [last["path"]], "mask": None,
                   "pads": None, "keep_shape": not SIZE_FLAG.search(text), "raw": True}  # fmt: skip
            return redo(agent, job, cancelled, ask)
    job = {"kind": "edit" if sources else "image", "prompt": prompt, "options": options,
           "sources": [str(p) for p in sources], "mask": str(mask) if mask else None, "pads": None,
           "keep_shape": not SIZE_FLAG.search(text), "raw": raw}  # fmt: skip
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
    if not job.get("raw") and job["kind"] in ("image", "edit", "video") and not job.get("pads"):
        job = {**job, "expanded": expand(agent, job)}
    made = []
    for i in range(options.n):
        seed = None if options.seed is None else options.seed + i
        label = f"{i + 1} of {options.n}" if options.n > 1 else ""
        one = replace(options, seed=seed, n=1)
        if job["kind"] == "image":
            made.append(generate_image(agent, prompt, cancelled, ask, one, label, job.get("expanded", "")))
        elif job["kind"] == "video":
            made.append(generate_video(agent, job, one, cancelled, ask, label))
        else:
            made.append(edit_image(agent, job, one, cancelled, ask, label))
        if job["kind"] in ("image", "edit"):
            check_result(agent, made[-1], prompt)
    return made


# ----------------------------------------------------------------------------- the prompt assistant


def helps(agent: Agent, kind: str) -> bool:
    """Whether the prompt assistant writes this kind of prompt: video only by default, or everything (all)."""
    setting = agent.settings.prompt_assistant
    return setting != "off" and (setting != "video" or kind == "video")


def helper(agent: Agent) -> assistant.Assistant | None:
    """The prompt assistant (lcode.assistant): the prompt_assistant setting's model, or a small installed one."""
    if getattr(agent, "_assistant_model", None) is None:
        setting = agent.settings.prompt_assistant
        model = assistant.pick(agent.ollama, "auto" if setting == "off" else setting)  # check_results may want one
        agent._assistant_model = model or ""
        if setting not in assistant.SCOPES and not model:
            agent.say(f"The prompt assistant {agent.settings.prompt_assistant} isn't installed in Ollama: ollama pull "
                      f"{agent.settings.prompt_assistant}. Prompts go as typed.", warning=True)  # fmt: skip
    return assistant.Assistant(agent.ollama, agent._assistant_model) if agent._assistant_model else None


def expand(agent: Agent, job: dict) -> str:
    """The assistant's fuller prompt for the job, shown before it's used; empty when it's off or fails."""
    kind = job["kind"]
    helper_model = helper(agent) if helps(agent, kind) else None
    if helper_model is None:
        return ""
    start = Path(job["sources"][0]) if kind == "video" and job.get("sources") else None
    try:
        with agent.console.status(
            f"The prompt assistant ({helper_model.model}) is writing the prompt…", spinner="dots"
        ):
            text = helper_model.expand(job["prompt"], kind, start if start and edits.is_image(start) else None)
    except assistant.AssistantError as e:
        agent.say(f"{e}: the prompt goes as typed.", warning=True)
        return ""
    if text and text != job["prompt"]:
        agent.console.print(Text(f"✎ {text}", style="dim italic"))
        return text
    return ""


def follow_up(agent: Agent, text: str) -> str | None:
    """A short request after an image: the assistant decides whether it's a change to it, and words it."""
    last = agent.media_results[-1] if agent.media_results else None
    if last is None or not edits.is_image(Path(last["path"])) or len(text.split()) > 10:
        return None
    if agent.settings.prompt_assistant in ("off", "video"):  # follow-ups come with prompts for pictures (all)
        return None
    helper_model = helper(agent)
    if helper_model is None:
        return None
    try:
        return helper_model.follow_up(text, last["prompt"])
    except assistant.AssistantError:
        return None


def check_result(agent: Agent, made: Made, request: str) -> None:
    """With check_results on: the assistant looks at the picture and says whether it shows what was asked."""
    if not agent.settings.check_results or not made.files or not edits.is_image(made.files[0]):
        return
    helper_model = helper(agent)
    if helper_model is None:
        return
    try:
        with agent.console.status("The prompt assistant is checking the picture…", spinner="dots"):
            result = helper_model.check(made.files[0], request)
    except assistant.AssistantError as e:
        agent.say(f"Couldn't check the picture: {e}", warning=True)
        return
    if result.matches:
        agent.console.print(Text("✓ It shows what you asked for.", style="green"))
    else:
        tip = f" Try: {result.better_prompt}" if result.better_prompt else ""
        agent.console.print(Text(f"⚠ {result.missing or 'It misses part of the request.'}{tip}", style="yellow"))


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
    expanded: str = "",
) -> Made:
    """One image from the prompt (or the assistant's fuller prompt), saved in the media folder and numbered."""
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
    text = imageopts.styled(expanded or prompt, options.style)
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
    instruction = job.get("expanded") or job["prompt"]
    instruction = instruction if not pads else f"{edits.EXTEND_HINT} {job['prompt']}".strip()
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
    sources = [Path(p) for p in job.get("sources") or []]
    start = sources[0] if sources else None
    end = sources[1] if len(sources) > 1 else None
    clip = Path(job["extend"]) if job.get("extend") else None
    refs = [Path(p) for p in job.get("refs") or []]
    restyle = Path(job["restyle"]) if job.get("restyle") else None
    task = (
        "restyle" if restyle else "references" if refs else "first-last-frame" if end
        else "camera" if options.camera else "image-to-video" if clip else ""
    )  # fmt: skip
    model = video_model(agent, task)
    files = ensure_installed(agent, model, server, ask)
    workflow = comfy.load_workflow(model.workflow)
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
    if clip is not None:  # the new part continues the clip, so it has its size
        width, height = job["size"]
    fps = int(workflow.defaults.get("fps", 24))
    if clip is not None:  # one clip, one pace: the new part plays at the clip's fps
        fps = int(clip_info(clip)["fps"] or fps)
    if restyle is not None:  # the restyled clip has the clip's size and pace (up to the model's length)
        info = clip_info(restyle)
        width, height = (info["width"] // 16 * 16 or 832), (info["height"] // 16 * 16 or 480)
        fps = int(info["fps"] or fps)
        options = replace(options, seconds=min(options.seconds if job.get("seconds_given") else 99,
                                               (min(info["frames"] or 81, 81) - 1) / fps))  # fmt: skip
    text = imageopts.styled(job.get("expanded") or prompt, options.style)
    values = {**workflow.defaults, **files, "prompt": text, "seed": seed, "width": width, "height": height,
              "length": imageopts.frames(options.seconds, fps), "fps": fps}  # fmt: skip
    if steps:
        values["steps"] = steps
    if options.negative:
        values["negative"] = options.negative
    values["camera"] = imageopts.CAMERA[options.camera or "static"]
    graph = comfy.fill(workflow.graph, values)
    latent = next((edits.node_of(graph, c) for c in VIDEO_LATENTS if edits.node_of(graph, c)), None)
    try:
        if start is not None:
            graph["8100"] = {"class_type": "LoadImage", "inputs": {"image": server.upload(start)}}
            graph[latent]["inputs"]["start_image"] = ["8100", 0]
            loader = edits.node_of(graph, "CLIPVisionLoader")
            if loader is not None:  # the camera model also looks at the start image
                look = {"clip_vision": [loader, 0], "image": ["8100", 0], "crop": "none"}
                graph["8102"] = {"class_type": "CLIPVisionEncode", "inputs": look}
                graph[latent]["inputs"]["clip_vision_output"] = ["8102", 0]
        if end is not None:
            graph["8101"] = {"class_type": "LoadImage", "inputs": {"image": server.upload(end)}}
            graph[latent]["inputs"]["end_image"] = ["8101", 0]
        if clip is not None:
            continue_clip(graph, latent, server.upload(clip), values["length"])
        vace = edits.node_of(graph, "WanVaceToVideo")
        if refs and vace is not None:
            graph[vace]["inputs"]["reference_image"] = reference_strip(graph, [server.upload(p) for p in refs])
        if restyle is not None and vace is not None:
            graph[vace]["inputs"]["control_video"] = control_from(graph, server.upload(restyle), values["length"])
    except (comfy.ComfyError, OSError) as e:
        raise MediaError(f"Couldn't hand the files to ComfyUI: {e}") from e
    add_thumbnail(graph, width, height)
    meta = describe_job(model, workflow, prompt, text, seed, values, options, files)
    meta.update({"seconds": round((values["length"] - 1) / fps, 2), "fps": fps, "frames": values["length"],
                 "start_image": shown(agent, start) if start else "", "end_image": shown(agent, end) if end else "",
                 "camera": options.camera, "extends": shown(agent, clip) if clip else "",
                 "references": [shown(agent, p) for p in refs],
                 "restyles": shown(agent, restyle) if restyle else ""})  # fmt: skip
    keys = ("kind", "prompt", "sources", "mask", "pads", "keep_shape", "extend", "size", "refs", "restyle")
    remembered = {k: job.get(k) for k in keys}
    stem = f"{clip.stem}-extended" if clip else (f"{restyle.stem}-restyled" if restyle else "")
    plan = Plan(model, workflow, graph, meta, prompt, seed, options, "video", job.get("sources") or [], remembered,
                stem)  # fmt: skip
    return run_plan(agent, server, plan, cancelled, label)


VIDEO_LATENTS = ("Wan22ImageToVideoLatent", "WanFunInpaintToVideo", "WanCameraImageToVideo", "WanVaceToVideo")
WHY = {
    "first-last-frame": "a clip between two frames",
    "camera": "a camera move",
    "image-to-video": "continuing a clip",
    "references": "following reference pictures",
    "restyle": "restyling a clip",
}


def reference_strip(graph: dict, names: list[str]) -> list:
    """The reference pictures side by side in one image, as VACE takes them."""
    graph["8300"] = {"class_type": "LoadImage", "inputs": {"image": names[0]}}
    strip = ["8300", 0]
    for k, name in enumerate(names[1:], 1):
        load, stitch = str(8300 + 2 * k - 1), str(8300 + 2 * k)
        graph[load] = {"class_type": "LoadImage", "inputs": {"image": name}}
        graph[stitch] = {
            "class_type": "ImageStitch",
            "inputs": {"image1": strip, "direction": "right", "match_image_size": True, "spacing_width": 0,
                       "spacing_color": "white", "image2": [load, 0]},
        }  # fmt: skip
        strip = [stitch, 0]
    return strip


def control_from(graph: dict, clip_name: str, length: int) -> list:
    """Restyling: the clip's outlines (Canny edges), frame by frame, for VACE to follow."""
    graph["8400"] = {"class_type": "LoadVideo", "inputs": {"file": clip_name}}
    graph["8401"] = {"class_type": "GetVideoComponents", "inputs": {"video": ["8400", 0]}}
    graph["8402"] = {
        "class_type": "ImageFromBatch",
        "inputs": {"image": ["8401", 0], "batch_index": 0, "length": length},
    }
    latent = edits.node_of(graph, "WanVaceToVideo")
    w, h = graph[latent]["inputs"]["width"], graph[latent]["inputs"]["height"]
    graph["8403"] = {
        "class_type": "ImageScale",
        "inputs": {"image": ["8402", 0], "upscale_method": "bilinear", "width": w, "height": h, "crop": "center"},
    }
    graph["8404"] = {
        "class_type": "Canny",
        "inputs": {"image": ["8403", 0], "low_threshold": 0.3, "high_threshold": 0.6},
    }
    return ["8404", 0]


def video_model(agent: Agent, task: str) -> catalog.MediaModel:
    """The video model for a job: the chosen one, or the first that does what the job needs."""
    chosen = current(agent, "video")
    if not task or task in chosen.tasks:
        return chosen
    model = catalog.load().for_task("video", task)
    if model is None:
        raise MediaError(f"None of lcode's video models does {WHY.get(task, task)} yet.")
    agent.say(f"{model.name} makes this one ({WHY.get(task, task)}).")
    return model


def continue_clip(graph: dict, latent: str, clip_name: str, length: int) -> None:
    """Extending a clip: start from its last frame, and join the new frames (without that first one) after it."""
    graph["8200"] = {"class_type": "LoadVideo", "inputs": {"file": clip_name}}
    graph["8201"] = {"class_type": "GetVideoComponents", "inputs": {"video": ["8200", 0]}}
    graph["8202"] = {"class_type": "ImageFromBatch", "inputs": {"image": ["8201", 0], "batch_index": -1, "length": 1}}
    graph[latent]["inputs"]["start_image"] = ["8202", 0]
    video = edits.node_of(graph, "CreateVideo")
    decoded = graph[video]["inputs"]["images"]
    graph["8203"] = {"class_type": "ImageFromBatch", "inputs": {"image": decoded, "batch_index": 1, "length": length}}
    graph["8204"] = {"class_type": "ImageBatch", "inputs": {"image1": ["8201", 0], "image2": ["8203", 0]}}
    graph[video]["inputs"]["images"] = ["8204", 0]


VIDEO_TYPES = (".mp4", ".webm", ".mkv", ".mov")
TOOL_NAMES = {"upscale": "upscaled", "remove-background": "nobg", "interpolate": "smooth", "sound": "sound"}


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_TYPES


def clip_info(path: Path) -> dict:
    """A clip's fps, seconds, frames, width and height: from lcode's JSON next to it, else its MP4 header."""
    info = {"fps": 24.0, "seconds": 0.0, "frames": 0, "width": 0, "height": 0}
    try:
        meta = json.loads(path.with_suffix(".json").read_text())
        info.update({k: meta[k] for k in info if isinstance(meta.get(k), (int, float))})
    except (OSError, ValueError):
        pass
    if not info["seconds"]:
        info["seconds"] = mp4_seconds(path)
    if not info["frames"] and info["seconds"]:
        info["frames"] = round(info["seconds"] * info["fps"]) + 1
    return info


def mp4_seconds(path: Path) -> float:
    """An MP4's length from its movie header (mvhd), or 0."""
    try:
        data = path.read_bytes()
    except OSError:
        return 0.0
    i = data.find(b"mvhd")
    if i < 4:
        return 0.0
    version = data[i + 4]
    if version == 1:
        scale, duration = struct.unpack(">IQ", data[i + 24 : i + 36])
    else:
        scale, duration = struct.unpack(">II", data[i + 16 : i + 24])
    return duration / scale if scale else 0.0


def apply_tool(
    agent: Agent,
    task: str,
    source: Path,
    scale: int = 2,
    cancelled: Callable[[], bool] = lambda: False,
    ask: Callable[[str], bool] | None = None,
    prompt: str = "",
) -> Made:
    """A tool model on one image or clip: /upscale (both), /nobg (pictures), /smooth and /sound (clips)."""
    video = is_video(source)
    if task == "remove-background" and video:
        raise MediaError("/nobg works on pictures.")
    if task in ("interpolate", "sound") and not video:
        raise MediaError(f"/{'smooth' if task == 'interpolate' else 'sound'} works on clips.")
    if task == "sound" and not prompt.strip():
        raise MediaError("Say what it should sound like: /sound 4 waves and seagulls")
    server = prepare(agent, ask, cancelled)
    model = catalog.load().tool(task)
    files = ensure_installed(agent, model, server, ask)
    workflow = comfy.load_workflow(model.video_workflow if video and model.video_workflow else model.workflow)
    try:
        name = server.upload(source)
        if video:
            info = clip_info(source)
            width, height = info["width"] or 864, info["height"] or 480
        else:
            width, height = edits.image_size(source)
    except (comfy.ComfyError, OSError, edits.EditError) as e:
        raise MediaError(f"Couldn't hand the file to ComfyUI: {e}") from e
    values = {**workflow.defaults, **files, ("video" if video else "image"): name}
    seed = 0
    if video:
        values["fps"] = info["fps"]
    if task == "upscale":
        values["scale_by"] = scale / 4  # Real-ESRGAN makes 4 times the size; scaled back for 2
        width, height = width * scale, height * scale
    elif task == "interpolate":
        values["multiplier"], values["fps"] = scale, info["fps"] * scale
    elif task == "sound":
        seed = random.randrange(2**32)
        values.update({"prompt": prompt.strip(), "seed": seed, "seconds": max(1.0, info["seconds"])})
    graph = comfy.fill(workflow.graph, values)
    add_thumbnail(graph, width, height)
    what = {
        "upscale": f"upscaled {scale}×",
        "remove-background": "background removed",
        "interpolate": f"{scale}× the frames",
        "sound": f"sound: {prompt.strip()}",
    }[task]
    label = f"{what} ({shown(agent, source)})"
    meta = {
        "prompt": label,
        "model": model.name,
        "model_key": model.key,
        "workflow": workflow.key,
        "source": shown(agent, source),
        "files": files,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if video:
        meta.update({"fps": values["fps"], "seconds": info["seconds"], "width": width, "height": height})
    suffix = {"upscale": f"upscaled-{scale}x", "interpolate": f"smooth-{scale}x"}.get(task, TOOL_NAMES[task])
    plan = Plan(model, workflow, graph, meta, label, seed, imageopts.Options(), task, [str(source)], {},
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
