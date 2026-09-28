"""The model catalog (models.toml) and memory-fit estimates."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import cache
from importlib import resources

from lcode.hardware import GIB, Hardware

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

OVERHEAD_GIB = 1.0  # compute buffers and runtime; measured <1 GiB for qwen3.6-35b and qwen3.5-9b on CUDA
HEADROOM_GIB = 0.5  # keep a little memory free so estimates at the edge don't run out
CONTEXT_STEPS = [1048576, 524288, 262144, 131072, 65536, 32768]
MIN_USEFUL_CONTEXT = 32768


@dataclass(frozen=True)
class ModelSpec:
    key: str
    tag: str
    name: str
    publisher: str
    params: str
    moe: bool
    size_gb: float
    max_context: int
    kv_kib_per_token: float
    tested: bool = False
    kv_estimated: bool = False
    num_batch: int | None = None
    swe_bench_verified: float | None = None
    notes: str = ""

    @property
    def local_name(self) -> str:
        """Name of the text-only variant `lcode setup` creates for models that ship a vision projector."""
        return f"lcode-{self.key}"

    def memory_gib(self, context: int) -> float:
        return self.size_gb * 1e9 / GIB + self.kv_kib_per_token * 1024 * context / GIB + OVERHEAD_GIB

    def best_context(self, budget_gib: float) -> int | None:
        """Largest standard context window whose estimated memory fits the budget."""
        for ctx in CONTEXT_STEPS:
            if ctx <= self.max_context and self.memory_gib(ctx) <= budget_gib - HEADROOM_GIB:
                return ctx
        return None

    def fit(self, hw: Hardware) -> tuple[int | None, str]:
        """(largest context that fits, speed note) on this machine."""
        if hw.unified:
            ctx = self.best_context(hw.budget_gib)
            return ctx, "fast" if ctx else "too large"
        if not hw.gpu:
            ctx = self.best_context(hw.budget_gib)
            return ctx, "CPU only (slow)" if ctx else "too large"
        if not self.moe:
            # Dense models slow down a lot when split, so prefer a context that stays in VRAM.
            fast_ctx = self.best_context(hw.vram_gib)
            if fast_ctx and fast_ctx >= MIN_USEFUL_CONTEXT:
                return fast_ctx, "fast"
        ctx = self.best_context(hw.budget_gib)
        if ctx is None:
            return None, "too large"
        if self.memory_gib(ctx) <= hw.vram_gib:
            return ctx, "fast"
        return ctx, "good (experts in RAM)" if self.moe else "slow (split across GPU and CPU)"


@cache
def load() -> tuple[ModelSpec, ...]:
    data = tomllib.loads(resources.files("lcode").joinpath("models.toml").read_text())
    return tuple(ModelSpec(**entry) for entry in data["model"])


def find(name: str) -> ModelSpec | None:
    """Look a model up by catalog key, Ollama tag or local variant name."""
    for spec in load():
        if name in (spec.key, spec.tag, spec.local_name, f"{spec.local_name}:latest") or (
            ":" not in spec.tag and name == f"{spec.tag}:latest"
        ):
            return spec
    return None


def recommend(hw: Hardware) -> tuple[ModelSpec, int] | None:
    """Best model for this machine, in order of preference:

    1. the first tested model (catalog order) that fits with at least 32K of context at usable speed;
    2. the first model in catalog order that fits with at least 64K, then 32K, at usable speed;
    3. the smallest model that fits at all.
    """

    def usable(spec: ModelSpec, min_ctx: int) -> int | None:
        ctx, speed = spec.fit(hw)
        return ctx if ctx and ctx >= min_ctx and "slow" not in speed else None

    for spec in load():
        if spec.tested and (ctx := usable(spec, MIN_USEFUL_CONTEXT)):
            return spec, ctx
    for min_ctx in (65536, MIN_USEFUL_CONTEXT):
        for spec in load():
            if ctx := usable(spec, min_ctx):
                return spec, ctx
    for spec in sorted(load(), key=lambda s: s.size_gb):
        if ctx := spec.best_context(hw.budget_gib):
            return spec, ctx
    return None
