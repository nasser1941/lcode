"""Detect the machine's GPU and memory to decide which models and context sizes fit."""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path

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


def gpu_used_gib() -> float | None:
    """GPU memory in use on the NVIDIA GPUs, in GiB; None without nvidia-smi."""
    out = _run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"])
    try:
        return sum(float(v) for v in out.split()) / 1024 if out else None
    except ValueError:
        return None


def gpu_users() -> list[tuple[str, float]]:
    """The programs holding NVIDIA GPU memory, largest first: (a short name, GiB). Empty without nvidia-smi."""
    out = _run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"])
    users: dict[str, float] = {}
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            gib = float(parts[2]) / 1024
        except ValueError:
            continue
        name = program_name(parts[0], parts[1])
        users[name] = users.get(name, 0.0) + gib
    return sorted(users.items(), key=lambda item: -item[1])


def program_name(pid: str, process: str) -> str:
    """A GPU program's short name, from its command line (/proc) or else nvidia-smi's process name."""
    try:
        argv = [a.decode(errors="replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
    except OSError:
        argv = []
    return name_from(argv or [process], process)


def name_from(argv: list[str], process: str = "") -> str:
    """'render.py' for `python3 render.py`, 'ComfyUI' for its main.py, 'Ollama', else the program."""
    program = Path(argv[0]).name if argv else process
    if program.startswith("python") and len(argv) > 1:
        script = next((a for a in argv[1:] if not a.startswith("-")), argv[1])
        if Path(script).name == "main.py" and "comfyui" in " ".join(argv[:2]).lower():
            return "ComfyUI"
        return Path(script).name
    if program in ("llama-server", "ollama") or "/ollama/" in (argv[0] if argv else ""):
        return "Ollama"
    return program or process


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
