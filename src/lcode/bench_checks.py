"""What every lcode bench task is made of, and what its check uses: running Python in the task's folder,
reading files."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CHECK_TIMEOUT = 60


DEFAULT_TIMEOUT = 300  # seconds per task


@dataclass
class Check:
    passed: bool
    detail: str


@dataclass
class Task:
    id: str
    title: str
    prompt: str
    files: dict[str, str]
    check: Callable[[Path, str], Check]  # (task folder, the model's final answer) -> result
    timeout: float = DEFAULT_TIMEOUT  # seconds before it stops, unless --timeout says otherwise


def run_python(folder: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=folder,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=CHECK_TIMEOUT,
    )


def read(folder: Path, name: str) -> str | None:
    try:
        return (folder / name).read_text()
    except (OSError, UnicodeDecodeError):
        return None


def last_line(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def unittest_check(folder: Path, what: str) -> Check:
    r = run_python(folder, "-m", "unittest", "-q")
    if r.returncode == 0:
        return Check(True, f"{what} pass")
    return Check(False, f"{what} fail: {last_line(r.stderr) or last_line(r.stdout)}")
