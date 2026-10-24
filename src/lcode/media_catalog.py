"""The image and video models lcode can run (media.toml): what they need, whether they fit, installing them.

A model is a workflow plus the files it uses in ComfyUI's model folders. Files are listed once, because models share
text encoders and VAEs, and a file counts as installed under its own name or one of its alternatives (an fp8 or a
full-precision build of the same weights). Installing downloads the missing files into ComfyUI's model folders:
resumably (a `.part` file grows until it's complete), checked against Hugging Face's SHA-256, after a disk check.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

import requests

from lcode.hardware import GIB, Hardware

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

DEFAULT_IMAGE = "flux2-klein-4b"
DISK_MARGIN_GB = 2.0  # left free after a download
GPU_MARGIN_GIB = 0.5
WORKING_GB = 1.5  # activations and buffers on top of the largest file, for models not measured yet
CHUNK = 1 << 20
http_get = requests.get  # what downloads use (tests replace it)


class CatalogError(Exception):
    pass


@dataclass(frozen=True)
class File:
    key: str
    folder: str  # ComfyUI's model folder
    name: str
    url: str
    gb: float
    alternatives: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.alternatives)


@dataclass(frozen=True)
class MediaModel:
    key: str
    name: str
    mode: str  # image or video
    publisher: str
    license: str
    tasks: tuple[str, ...]
    workflow: str
    files: dict[str, File] = field(hash=False)  # role -> file
    gpu_gb: float = 0.0  # measured peak GPU memory; 0 when not measured yet
    measured: str = ""
    notes: str = ""
    quality: dict[str, tuple[int, float]] = field(default_factory=dict, hash=False)  # preset -> (steps, megapixels)
    negative: bool = False  # whether it uses a negative prompt
    edit: str = ""  # how it edits images ("reference-latent"), or empty: it doesn't

    def preset(self, quality: str) -> tuple[int | None, float]:
        """(steps, megapixels) for a quality preset: None steps keeps the workflow's."""
        steps, megapixels = self.quality.get(quality) or self.quality.get("balanced") or (None, 1.0)
        return (int(steps) if steps else None), float(megapixels)

    @property
    def size_gb(self) -> float:
        return sum(f.gb for f in {f.key: f for f in self.files.values()}.values())

    @property
    def need_gb(self) -> float:
        """GPU memory at the peak: measured, or estimated. ComfyUI loads the text encoder, the model and the VAE
        one after another and moves what it doesn't need off the GPU, so the largest file sets the peak."""
        return self.gpu_gb or max(f.gb for f in self.files.values()) + WORKING_GB

    def fit(self, hw: Hardware) -> tuple[str, str]:
        """(fits | slower | too big | cpu, a short note) on this machine."""
        weights_gib = self.size_gb * 1e9 / GIB
        need_gib = self.need_gb * 1e9 / GIB
        if hw.unified:
            if need_gib <= hw.budget_gib - GPU_MARGIN_GIB:
                return "fits", "fits in unified memory"
            return "too big", f"needs ~{need_gib:.0f} GB, ~{hw.budget_gib:.0f} GB available"
        if not hw.gpu or not hw.vram_gib:
            return "cpu", "no GPU lcode can use: very slow on the CPU"
        if need_gib <= hw.vram_gib - GPU_MARGIN_GIB:
            return "fits", f"~{need_gib:.0f} GB of {hw.vram_gib:.0f} GB GPU memory"
        if weights_gib <= hw.vram_gib + 0.6 * hw.ram_gib:  # ComfyUI keeps the rest in RAM and swaps it in
            return "slower", f"part runs from RAM ({need_gib:.0f} GB needed, {hw.vram_gib:.0f} GB GPU)"
        return "too big", f"needs ~{weights_gib:.0f} GB, more than GPU and RAM together"


@dataclass
class Catalog:
    files: dict[str, File]
    models: dict[str, MediaModel]

    def for_mode(self, mode: str) -> list[MediaModel]:
        return [m for m in self.models.values() if m.mode == mode]

    def tool(self, task: str) -> MediaModel:
        """The tool for a task (upscale, remove-background)."""
        return next(m for m in self.models.values() if m.mode == "tool" and task in m.tasks)

    def editor(self, preferred: MediaModel) -> MediaModel | None:
        """A model that edits: the preferred one if it can, else the first image model that can."""
        if preferred.edit:
            return preferred
        return next((m for m in self.for_mode("image") if m.edit), None)

    def find(self, wanted: str, mode: str | None = None) -> MediaModel | None:
        """A model by key or name (any case), or by its number in the mode's list."""
        candidates = self.for_mode(mode) if mode else list(self.models.values())
        if wanted.isdigit() and 1 <= int(wanted) <= len(candidates):
            return candidates[int(wanted) - 1]
        low = wanted.strip().lower()
        return next((m for m in candidates if low in (m.key, m.name.lower())), None)


@cache
def load() -> Catalog:
    data = tomllib.loads(resources.files("lcode").joinpath("media.toml").read_text())
    files = {
        key: File(key, f["folder"], f["name"], f["url"], float(f["gb"]), tuple(f.get("alternatives", ())))
        for key, f in data.get("files", {}).items()
    }
    models = {}
    for key, m in data.get("models", {}).items():
        try:
            roles = {role: files[ref] for role, ref in m["files"].items()}
        except KeyError as e:
            raise CatalogError(f"media.toml: {key} uses the file {e}, which isn't listed") from e
        models[key] = MediaModel(
            key,
            m["name"],
            m["mode"],
            m.get("publisher", ""),
            m.get("license", ""),
            tuple(m.get("tasks", ())),
            m["workflow"],
            roles,
            float(m.get("gpu_gb", 0.0)),
            m.get("measured", ""),
            m.get("notes", ""),
            {name: (int(v[0]), float(v[1])) for name, v in (m.get("quality") or {}).items()},
            bool(m.get("negative", False)),
            m.get("edit", ""),
        )
    return Catalog(files, models)


# ----------------------------------------------------------------------------- what's installed


Installed = Callable[[str], set[str]]  # ComfyUI folder -> the file names in it


def chosen_files(model: MediaModel, installed: Installed) -> tuple[dict[str, str], list[File]]:
    """For each role, the installed file name to use; and the files that aren't installed."""
    chosen, missing = {}, []
    for role, f in model.files.items():
        found = next((n for n in f.names if n in installed(f.folder)), None)
        if found:
            chosen[role] = found
        elif f not in missing:
            missing.append(f)
    return chosen, missing


def lister(server=None, install=None) -> Installed:
    """What's in each model folder: asked from a running ComfyUI, or read from the install's folders."""
    from lcode import comfy

    seen: dict[str, set[str]] = {}

    def installed(folder: str) -> set[str]:
        if folder not in seen:
            if server is not None:
                seen[folder] = set(server.models(folder))
            elif install is not None:
                seen[folder] = set(comfy.local_models(install, folder))
            else:
                seen[folder] = set()
        return seen[folder]

    return installed


def model_folder(folder: str, server=None, install=None) -> Path | None:
    """Where ComfyUI keeps one kind of model: from the running server, or the install's models/ folder."""
    if server is not None:
        paths = server.folder_paths().get(folder)
        if paths:
            return Path(paths[0])
    if install is not None:
        return install.path / "models" / folder
    return None


# ----------------------------------------------------------------------------- downloading


def disk_problem(folder: Path, need_gb: float) -> str:
    """Why `need_gb` won't fit on the disk of `folder` (with a margin), or empty."""
    probe = folder
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free_gb = shutil.disk_usage(probe).free / 1e9
    if need_gb + DISK_MARGIN_GB > free_gb:
        return f"needs {need_gb:.1f} GB, and {probe} has {free_gb:.1f} GB free (lcode keeps {DISK_MARGIN_GB:.0f} GB)"
    return ""


def remaining_gb(f: File, folder: Path) -> float:
    part = folder / (f.name + ".part")
    have = part.stat().st_size / 1e9 if part.exists() else 0.0
    return max(f.gb - have, 0.0)


def auth_headers(url: str) -> dict[str, str]:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token and urlsplit(url).hostname == "huggingface.co":
        return {"Authorization": f"Bearer {token}"}  # requests drops it when redirected to another host
    return {}


def download(
    url: str,
    dest: Path,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
    get=None,
) -> None:
    """Download `url` to `dest`, resuming a `.part` file left by an earlier try, and check its SHA-256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {**auth_headers(url), **({"Range": f"bytes={have}-"} if have else {})}
    try:
        r = (get or http_get)(url, headers=headers, stream=True, timeout=(15, 120), allow_redirects=True)
    except requests.RequestException as e:
        raise CatalogError(f"can't download {dest.name}: {e}") from e
    with r:
        if r.status_code == 416 and have:  # nothing left to fetch: the .part is complete
            pass
        elif r.status_code in (401, 403):
            raise CatalogError(
                f"{dest.name} needs a Hugging Face login: accept its license on huggingface.co, then set HF_TOKEN"
            )
        elif r.status_code not in (200, 206):
            raise CatalogError(f"can't download {dest.name}: HTTP {r.status_code}")
        else:
            if r.status_code == 200 and have:  # the server ignored the range: start over
                have = 0
            total = have + int(r.headers.get("Content-Length") or 0)
            with part.open("ab" if have else "wb") as out:
                done = have
                for chunk in r.iter_content(CHUNK):
                    out.write(chunk)
                    done += len(chunk)
                    on_progress(done, total)
        expected = sha256_of(r)
    if expected:
        actual = file_sha256(part)
        if actual != expected:
            part.unlink()
            raise CatalogError(f"{dest.name} arrived damaged (its SHA-256 doesn't match): it will download again")
    part.replace(dest)


def sha256_of(response) -> str:
    """The SHA-256 Hugging Face gives for a large file (its X-Linked-Etag, on the redirect), or empty."""
    for r in [*getattr(response, "history", []), response]:
        tag = (r.headers.get("X-Linked-Etag") or "").strip('"')
        if len(tag) == 64 and all(c in "0123456789abcdef" for c in tag):
            return tag
    return ""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK * 8), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install(model: MediaModel, server=None, install=None, console=None, ask=None, get=None) -> list[Path]:
    """Download the model's missing files into ComfyUI's model folders. Raises CatalogError with what to do.

    `ask(question) -> bool` confirms the download (None: go ahead, for `--yes`).
    """
    from rich.console import Console
    from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TimeRemainingColumn, TransferSpeedColumn

    console = console or Console()
    _, missing = chosen_files(model, lister(server, install))
    if not missing:
        return []
    folders = {}
    for f in missing:
        folder = model_folder(f.folder, server, install)
        if folder is None:
            raise CatalogError("lcode doesn't know where ComfyUI keeps its models: set comfyui_path")
        folders[f.key] = folder
    need = sum(remaining_gb(f, folders[f.key]) for f in missing)
    problem = disk_problem(next(iter(folders.values())), need)
    if problem:
        raise CatalogError(f"not enough disk space for {model.name}: {problem}")
    listing = ", ".join(f"{f.name} ({f.gb:.1f} GB)" for f in missing)
    if ask is not None and not ask(
        f"Download {model.name}: {listing}, {need:.1f} GB into {folders[missing[0].key].parent}?"
    ):
        raise CatalogError(f"{model.name} isn't installed (download declined)")
    saved = []
    columns = (TextColumn("{task.description}"), BarColumn(), DownloadColumn(), TransferSpeedColumn())
    with Progress(*columns, TimeRemainingColumn(), console=console, transient=False) as progress:
        for f in missing:
            task = progress.add_task(f.name, total=int(f.gb * 1e9))
            dest = folders[f.key] / f.name
            try:
                download(f.url, dest, lambda done, total, t=task: progress.update(t, completed=done, total=total), get)
            except KeyboardInterrupt:
                raise CatalogError(f"Paused {f.name}: install {model.key} again to resume where it stopped.") from None
            saved.append(dest)
    return saved


def describe(model: MediaModel, hw: Hardware, installed: Installed | None) -> str:
    """One line for a list: tasks, size, fit, installed."""
    status, note = model.fit(hw)
    parts = [", ".join(model.tasks), f"{model.size_gb:.1f} GB"]
    if installed is not None:
        _, missing = chosen_files(model, installed)
        if missing:
            parts.append(f"{sum(f.gb for f in missing):.1f} GB to download")
        else:
            parts.append("installed")
    parts.append({"fits": "fits", "slower": "slower", "too big": "too big", "cpu": "CPU only"}[status] + f" ({note})")
    return " · ".join(parts)
