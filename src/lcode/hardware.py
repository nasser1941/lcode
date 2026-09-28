"""Detect the machine's GPU and memory to decide which models and context sizes fit."""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass

GIB = 1024**3


@dataclass(frozen=True)
class Hardware:
    os: str  # "linux", "macos" or other platform.system() value
    cpu: str
    ram_gib: float
    gpu: str | None = None
    vram_gib: float = 0.0  # dedicated GPU memory (0 for Apple Silicon: memory is unified)
    unified: bool = False  # Apple Silicon

    @property
    def budget_gib(self) -> float:
        """Memory available for model weights + KV cache."""
        if self.unified:
            # macOS lets the GPU wire ~2/3 of RAM on smaller Macs and ~3/4 above 36 GB.
            return self.ram_gib * (0.75 if self.ram_gib > 36 else 0.67)
        # Dedicated GPU plus system RAM for offloaded layers, keeping ~8 GiB for the OS and apps.
        return self.vram_gib + max(0.0, self.ram_gib - 8)

    @property
    def fast_gib(self) -> float:
        """Memory where a model runs at full accelerator speed."""
        return self.budget_gib if self.unified else self.vram_gib

    def describe(self) -> str:
        if self.unified:
            return f"{self.cpu}, {self.ram_gib:.0f} GB unified memory (~{self.budget_gib:.0f} GB usable by the GPU)"
        gpu = f"{self.gpu} ({self.vram_gib:.0f} GB VRAM)" if self.gpu else "no supported GPU (CPU only)"
        return f"{gpu}, {self.ram_gib:.0f} GB RAM"


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _linux_ram_gib() -> float:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024 / GIB
    except OSError:
        pass
    return 0.0


def detect() -> Hardware:
    system = platform.system()
    if system == "Darwin":
        ram = int(_run(["sysctl", "-n", "hw.memsize"]) or 0) / GIB
        cpu = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.machine()
        apple = platform.machine() == "arm64"
        return Hardware(os="macos", cpu=cpu, ram_gib=ram, gpu=f"{cpu} GPU" if apple else None, unified=apple)

    cpu = platform.processor() or platform.machine()
    try:
        with open("/proc/cpuinfo") as f:
            cpu = next((ln.split(":", 1)[1].strip() for ln in f if ln.startswith("model name")), cpu)
    except OSError:
        pass
    gpus = [
        ln.rsplit(",", 1)
        for ln in _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]).splitlines()
        if "," in ln
    ]
    names = [n.strip() for n, _ in gpus]
    vram = sum(float(m) for _, m in gpus) / 1024 if gpus else 0.0
    gpu = None
    if names:
        gpu = names[0] if len(names) == 1 else f"{len(names)}x {names[0]}"
    return Hardware(os=system.lower(), cpu=cpu, ram_gib=_linux_ram_gib(), gpu=gpu, vram_gib=vram)
