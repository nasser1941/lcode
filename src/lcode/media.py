"""Image and video mode: a request becomes media, made by ComfyUI (lcode.comfy) with no language model in between.

Image mode makes one image per request with the image model chosen from the media catalog (lcode.media_catalog);
sizes, variations and a gallery come with #151, video with #153.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text

from lcode import comfy
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


def generate_image(
    agent: Agent, prompt: str, cancelled: Callable[[], bool] = lambda: False, ask: Callable[[str], bool] | None = None
) -> Made:
    """One image from the prompt, saved in the media folder. Raises MediaError with what to do."""
    prompt = prompt.strip()
    if not prompt:
        raise MediaError("Describe the image you want.")
    server = engine(agent, ask, cancelled)
    for path in comfy.collect(server):
        agent.say(f"Saved {shown(agent, path)}: it finished while lcode wasn't watching.")
    model = current(agent)
    files = ensure_installed(agent, model, server, ask)
    workflow = comfy.load_workflow(model.workflow)
    free_gpu(agent)
    seed = random.randrange(2**32)
    values = {**workflow.defaults, **files, "prompt": prompt, "seed": seed}
    graph = comfy.fill(workflow.graph, values)
    meta = {
        "prompt": prompt,
        "model": model.name,
        "model_key": model.key,
        "workflow": workflow.key,
        "seed": seed,
        "width": values["width"],
        "height": values["height"],
        "steps": values["steps"],
        "files": files,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    out = folder(agent)
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{comfy.slug(prompt)}"
    line = Line(model.name, "Esc or Ctrl+C cancels" if agent.interactive else "")
    job = {"id": ""}

    def on_event(event: comfy.Event) -> None:
        if event.kind == "queued":
            job["id"] = event.prompt_id
            comfy.remember(event.prompt_id, server.url, out, stem, meta)
        elif event.kind == "waiting":
            line.doing = f"waiting: {event.value} job(s) ahead in ComfyUI's queue"
        elif event.kind == "node":
            line.doing, line.step = workflow.labels.get(event.node, "working"), None
        elif event.kind == "progress" and event.total:
            line.doing = workflow.labels.get(event.node, line.doing)
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
    made = Made(saved, model.name, seed, values["width"], values["height"], time.monotonic() - line.started)
    for path in saved:
        agent.console.print(
            Text("◆ ", style="green")
            + Text(shown(agent, path), style="bold")
            + Text(f"  {made.width}×{made.height} · {made.model} · seed {seed} · {made.seconds:.1f} s", style="dim")
        )
    return made
