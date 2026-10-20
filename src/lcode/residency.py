"""Where the model is, in words: loading it, how it fits, making room for another one.

A local model has to be in memory before it answers. On a small GPU loading takes a while, how much of
the model fits on the GPU decides how fast it runs, and a second model (the one that looks at images)
can push the first one out. lcode says so, in a line, when it happens: nothing is printed while the
model simply stays loaded.

Only Ollama is asked: other servers load and place their models themselves.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from lcode import catalog
from lcode.cloud import is_cloud
from lcode.ollama import OllamaError

GB = 1e9
SPLIT_WARNING = 0.95  # a dense model with less than this on the GPU runs much slower
SHORTFALL = 0.25  # this much less on the GPU than expected: something else is using GPU memory


@dataclass
class Placement:
    """A loaded model, from Ollama's /api/ps."""

    name: str
    size: float  # bytes in memory
    vram: float  # bytes of it on the GPU
    context: int = 0  # the context window it was loaded with

    @property
    def gpu_share(self) -> float:
        return self.vram / self.size if self.size else 0.0

    def describe(self) -> str:
        """'22.3 GB, 44% on the GPU and the rest in system RAM'"""
        size = f"{self.size / GB:.1f} GB"
        share = self.gpu_share
        if share >= 0.995:
            return f"{size}, all on the GPU"
        if share <= 0.005:
            return f"{size}, all in system RAM (on the CPU)"
        return f"{size}, {share:.0%} on the GPU and the rest in system RAM"


def same_model(a: str, b: str) -> bool:
    return a.removesuffix(":latest") == b.removesuffix(":latest")


def loaded(ollama, model: str | None = None) -> list[Placement]:
    """The models Ollama has in memory (only `model`, if given). Empty when it can't be asked."""
    if getattr(ollama, "kind", "ollama") != "ollama":
        return []
    try:
        entries = ollama.running()
    except (OllamaError, OSError):
        return []
    found = []
    for entry in entries or []:
        name = entry.get("name") or entry.get("model") or ""
        if model is None or same_model(name, model):
            size = float(entry.get("size") or 0)
            vram = float(entry.get("size_vram") if entry.get("size_vram") is not None else size)
            found.append(Placement(name.removesuffix(":latest"), size, vram, int(entry.get("context_length") or 0)))
    return found


def placement(ollama, model: str) -> Placement | None:
    found = loaded(ollama, model)
    return found[0] if found else None


def is_moe(ollama, model: str) -> bool | None:
    """Whether the model is a Mixture-of-Experts (None: unknown)."""
    spec = catalog.find(model)
    if spec:
        return spec.moe
    try:
        info = (ollama.show(model) or {}).get("model_info") or {}
    except (OllamaError, OSError):
        return None
    if not info:
        return None
    return any(key.endswith(".expert_count") and value for key, value in info.items())


def expected_share(model: str, context: int, hardware) -> float | None:
    """How much of the model should fit on the GPU, from the catalog's estimate."""
    spec = catalog.find(model)
    if not spec or hardware is None or not hardware.gpu or hardware.unified:
        return None
    return min(1.0, hardware.vram_gib / spec.memory_gib(context))


def dense_warning(ollama, model: str, context: int, hardware) -> str:
    """For a dense model that won't fit on the GPU: what that means and what to do. Empty otherwise."""
    if is_cloud(model):
        return ""  # it runs on ollama.com
    if hardware is None or hardware.unified or is_moe(ollama, model) is not False:
        return ""
    spec = catalog.find(model)
    params = f" {spec.params.split(' ')[0]}" if spec and spec.params else ""
    if not hardware.gpu:
        where = "There's no GPU here, so it runs on the CPU"
    else:
        share = expected_share(model, context, hardware)
        if share is None or share >= SPLIT_WARNING:
            return ""
        where = (
            f"Only about {share:.0%} of it fits in the {hardware.vram_gib:.0f} GB of GPU memory and the rest runs "
            "on the CPU"
        )
    return (
        f"{model} is a dense model: all{params} parameters work on every token. It's capable, but slow here. "
        f"{where}, so expect a few tokens a second. For speed, pick a Mixture-of-Experts model such as "
        "qwen3.6-35b (/model qwen3.6-35b), or a smaller context (/ctx 16k) to keep more of it on the GPU."
    )


def load_report(
    model: str, seconds: float, where: Placement | None, context: int, hardware, dense: bool, reload: bool = False
) -> str:
    """The line after a load: how long it took, how it fits, and a warning if it fits worse than it should."""
    took = f" in {seconds:.0f}s" if seconds >= 1 else ""
    line = f"{'Reloaded' if reload else 'Loaded'} {model}{took}" + (f": {where.describe()}." if where else ".")
    if where is None:
        return line
    expected = expected_share(model, context, hardware)
    if expected is not None and where.gpu_share < expected - SHORTFALL:
        line += (
            f" Less of it is on the GPU than usual ({expected:.0%}): another program or model is using GPU "
            "memory, so it runs slower until that's closed."
        )
    elif dense and where.gpu_share < SPLIT_WARNING:
        line += " It's a dense model split between GPU and CPU, so expect it to be slow."
    return line


class LoadWatch:
    """While a model loads: asks Ollama once a second, and once the model is in memory moves the status line
    on to reading the prompt, which comes next and can take a while too."""

    POLL = 1.0

    def __init__(self, ollama, model: str, context: int, progress, reading: str) -> None:
        self.ollama, self.model, self.context = ollama, model, context
        self.progress, self.reading = progress, reading
        self.started = time.monotonic()
        self.loaded_after: float | None = None
        self.reload = False  # it was in memory before: unloaded for another model, or loaded with another context
        self.stopped = threading.Event()
        threading.Thread(target=self.run, daemon=True).start()

    def run(self) -> None:
        while not self.stopped.wait(self.POLL):
            where = placement(self.ollama, self.model)
            if where and (not where.context or not self.context or where.context == self.context):
                self.loaded_after = time.monotonic() - self.started
                if not self.stopped.is_set():
                    self.progress.set_phase(self.reading)
                return

    def stop(self) -> float:
        """Seconds the load took (until the first token, when the end of the load went unseen)."""
        self.stopped.set()
        return self.loaded_after if self.loaded_after is not None else time.monotonic() - self.started
