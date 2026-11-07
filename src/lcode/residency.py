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
from dataclasses import dataclass, replace
from statistics import median
from urllib.parse import urlsplit

from lcode import catalog
from lcode.cloud import is_cloud
from lcode.config import format_tokens
from lcode.hardware import GIB, Hardware
from lcode.ollama import OllamaError

GB = 1e9
ALL = 0.995  # this share of a model on the GPU counts as all of it, and 1 - ALL as none of it
RELEASE_WAIT = 15.0  # seconds to wait for Ollama to give an unloaded model's GPU memory back
SPLIT_WARNING = 0.95  # a dense model with less than this on the GPU runs much slower
SHORTFALL = 0.25  # this much less on the GPU than expected: something else is using GPU memory
SYSTEM_RAM_GIB = 2.0  # what the system itself keeps, with every other program closed
SLOW_REPLIES = 3  # this many replies in a row...
SLOW_SHARE = 0.5  # ...below this share of the session's earlier speed: something changed
SPEED_MIN_TOKENS = 50  # a shorter reply says little about speed
CONFIG_COMMANDS = ("lcode config set context {}", "lcode setup {}", "lcode models")  # a size, a model, the list
SESSION_COMMANDS = ("/ctx {}", "/model {}", "/models")


@dataclass
class Placement:
    """A loaded model, from Ollama's /api/ps (see `with_estimate` for what that leaves out)."""

    name: str
    size: float  # bytes in memory, as Ollama counts them
    vram: float  # bytes of it on the GPU
    context: int = 0  # the context window it was loaded with
    estimate: float = 0.0  # bytes the model takes by the catalog's estimate, when Ollama's count leaves part out

    @property
    def total(self) -> float:
        return max(self.size, self.estimate)

    @property
    def gpu_share(self) -> float:
        return self.vram / self.total if self.total else 0.0

    def describe(self) -> str:
        """'6.6 GB, all on the GPU', or with the catalog's estimate: '~23 GB, 42% on the GPU and the rest in system
        RAM'. The estimate is shown as `lcode setup` and `lcode doctor` show it."""
        size = f"~{self.estimate / GIB:.0f} GB" if self.estimate > self.size else f"{self.size / GB:.1f} GB"
        share = self.gpu_share
        if share >= ALL:
            return f"{size}, all on the GPU"
        if share <= 1 - ALL:
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


def release(ollama, model: str, wait: float | None = None) -> bool:
    """Unload a model and wait until its GPU memory is free again; whether it was loaded.

    Ollama answers the unload at once and frees the memory a moment later: a program that starts on the GPU in
    that moment finds it still full. So this waits until Ollama no longer lists the model and, on NVIDIA GPUs, until
    most of what it held shows as free.
    """
    from lcode import hardware

    held = loaded(ollama, model)
    if not held:
        return False
    before = hardware.gpu_used_gib()
    ollama.unload(model)
    deadline = time.monotonic() + (RELEASE_WAIT if wait is None else wait)
    while loaded(ollama, model) and time.monotonic() < deadline:
        time.sleep(0.2)
    if before is not None:  # NVIDIA: until most of it is free, or the memory stops going down for a second
        target = before - 0.75 * sum(p.vram for p in held) / 2**30
        last, steady = before, time.monotonic()
        while time.monotonic() < deadline:
            used = hardware.gpu_used_gib()
            if used is None or used <= target:
                break
            if abs(used - last) > 0.1:
                last, steady = used, time.monotonic()
            elif time.monotonic() - steady >= 1.0 and used < before - 0.1:
                break
            time.sleep(0.2)
    return True


def placement(ollama, model: str) -> Placement | None:
    found = loaded(ollama, model)
    return found[0] if found else None


def with_estimate(where: Placement, model: str, context: int) -> Placement:
    """`where`, counting the whole model when part of it is off the GPU: by the catalog's estimate (models outside
    the catalog stay as Ollama counts them).

    /api/ps counts the memory Ollama's runner allocates. The weights it keeps in system RAM aren't allocated: they
    stay mapped from the model's file. So with Ollama 0.40, qwen3.6-35b at 32K on a 12 GB GPU shows as 10.5 GB, 98%
    of it on the GPU, while ~12 GB of its experts are in RAM. A model all on the GPU is counted in full.
    """
    spec = catalog.find(model)
    if spec is None or where.vram >= ALL * where.size:
        return where
    return replace(where, estimate=spec.memory_gib(where.context or context) * GIB)


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
    line = f"{'Reloaded' if reload else 'Loaded'} {model}{took}"
    if where is None:
        return f"{line}."
    where = with_estimate(where, model, context)
    line += f": {where.describe()}."
    expected = expected_share(model, context, hardware)
    if expected is not None and where.gpu_share < expected - SHORTFALL:
        fits = "all of it fits" if expected >= ALL else f"up to {expected:.0%} fits"
        line += (
            f" Less of it is on the GPU than usual ({fits}): another program or model is using GPU memory, so it "
            "runs slower until that's closed."
        )
    elif dense and where.gpu_share < SPLIT_WARNING:
        line += " It's a dense model split between GPU and CPU, so expect it to be slow."
    return line


def on_this_machine(ollama) -> bool:
    """Whether Ollama runs here (in Docker too), so that this machine's RAM is the one it uses."""
    from lcode.update import LOCAL_HOSTS

    host = getattr(ollama, "host", None)  # a replay has none
    return getattr(ollama, "kind", "ollama") == "ollama" and bool(host) and urlsplit(host).hostname in LOCAL_HOSTS


@dataclass
class RamFit:
    """Whether system RAM can hold a model in a long session, as things stand now."""

    spec: catalog.ModelSpec
    context: int
    hw: Hardware
    available: float  # GiB for the model and Ollama: what's available now, and what Ollama already holds

    @property
    def in_ram(self) -> float:
        return self.spec.in_ram_gib(self.context, self.hw)

    @property
    def needed(self) -> float:
        return self.spec.ram_needed_gib(self.context, self.hw)

    @property
    def short(self) -> bool:
        return round(self.available) < round(self.needed)  # as the numbers on the screen say

    def describe(self) -> str:
        """'~15 GB available now, ~21 GB needed in a long session: ~11 GB of the model that doesn't fit on the GPU,
        up to 10 GB for Ollama's server and prompt cache'"""
        parts = [f"up to {catalog.OLLAMA_RAM_GIB:.0f} GB for Ollama's server and prompt cache"]
        if round(self.in_ram):
            model = "of the model that doesn't fit on the GPU" if self.hw.gpu else "for the model"
            parts.insert(0, f"~{self.in_ram:.0f} GB {model}")
        needed = f"~{self.needed:.0f} GB needed in a long session"
        return f"~{self.available:.0f} GB available now, {needed}: {', '.join(parts)}"

    def why(self) -> str:
        """What happens when RAM runs short."""
        if round(self.in_ram):
            return (
                "When RAM runs short, Linux drops the model from memory and keeps reading it back from disk: replies "
                "fall to about a third of their speed, often within an hour, as the prompt cache fills."
            )
        return (
            "When RAM runs short as the prompt cache fills, programs get pushed out to swap and everything slows down."
        )

    def smaller_context(self) -> tuple[int, float] | None:
        """The context to suggest, and the GiB it saves: the largest that makes everything fit, or else the smallest."""
        steps = [size for size in catalog.CONTEXT_STEPS if size < self.context]
        if not steps:
            return None
        fits = (size for size in steps if self.spec.ram_needed_gib(size, self.hw) <= self.available)
        size = next(fits, steps[-1])
        saved = self.needed - self.spec.ram_needed_gib(size, self.hw)
        return (size, saved) if round(saved) else None

    def smaller_model(self) -> catalog.ModelSpec | None:
        """The first smaller model in the catalog that fits what's available, with the context it would get. Dense
        models that would be split across GPU and CPU are left out: those are very slow."""
        for other in catalog.load():
            ctx, speed = other.fit(self.hw)
            if not ctx or other.size_gb >= self.spec.size_gb or "split" in speed:
                continue
            if other.ram_needed_gib(ctx, self.hw) <= self.available:
                return other
        return None

    def advice(self, commands: tuple[str, str, str] = CONFIG_COMMANDS) -> str:
        """What can be done about it, and only what would help. Closing programs can't make up for too little RAM, and
        with the whole model on the GPU a smaller model or context saves nothing: RAM only holds Ollama's cache then."""
        set_context, use_model, list_models = commands
        options = []
        if self.needed <= self.hw.ram_gib - SYSTEM_RAM_GIB:
            options.append(f"close other programs to free ~{max(1, round(self.needed - self.available))} GB")
        if round(self.in_ram):
            if smaller := self.smaller_context():
                size, saved = smaller
                command = set_context.format(format_tokens(size).lower())
                options.append(f"use a smaller context (saves ~{saved:.0f} GB: {command})")
            if model := self.smaller_model():
                options.append(f"pick a smaller model such as {model.key} ({use_model.format(model.key)})")
            elif any(other.size_gb < self.spec.size_gb for other in catalog.load()):
                options.append(f"pick a smaller model ({list_models})")
        if not options:
            return "This machine has too little RAM for that, so expect long sessions to slow down."
        text = options[0] if len(options) == 1 else f"{', '.join(options[:-1])}, or {options[-1]}"
        return f"{text[0].upper()}{text[1:]}."


def ram_fit(ollama, spec: catalog.ModelSpec, context: int, hw: Hardware) -> RamFit | None:
    """How RAM stands for this model; None when that can't be told: Ollama runs on another machine, or it isn't Linux
    (nothing was measured elsewhere; on a Mac the weights stay in wired memory)."""
    from lcode import hardware

    if not on_this_machine(ollama) or (available := hardware.available_ram_gib()) is None:
        return None
    # What Ollama holds already counts as available: part of what it needs is that memory.
    return RamFit(spec, context, hw, available + hardware.ollama_private_gib())


class SpeedWatch:
    """How fast a session's replies came, to notice when they fall far below what the model did earlier."""

    def __init__(self) -> None:
        self.speeds: list[float] = []
        self.setup: tuple[str, int] | None = None  # the model and context they were measured with
        self.explained = False  # a slowdown is explained once

    def add(self, model: str, context: int, tokens: int, seconds: float) -> tuple[float, float] | None:
        """Note a reply. When the last ones were all far slower than before: (the earlier speed, the speed now)."""
        if (model, context) != self.setup:
            self.setup, self.speeds = (model, context), []
        if tokens < SPEED_MIN_TOKENS or seconds <= 0:
            return None
        self.speeds.append(tokens / seconds)
        earlier, recent = self.speeds[:-SLOW_REPLIES], self.speeds[-SLOW_REPLIES:]
        if len(earlier) < SLOW_REPLIES or max(recent) >= SLOW_SHARE * median(earlier):
            return None
        return median(earlier), median(recent)


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
