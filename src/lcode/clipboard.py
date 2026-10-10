"""Paste from the clipboard with Ctrl+V, images included.

A terminal can only paste text, so a screenshot never reaches lcode that way. On Ctrl+V lcode reads the
clipboard itself: with wl-paste on Wayland, xclip on X11, and pngpaste or osascript on macOS. An image is
saved in lcode's state folder and goes into the prompt as `@path`, so it's attached like any other image;
anything else is pasted as text.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from lcode import config

TIMEOUT = 5  # seconds for a clipboard tool
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
KEEP = 50  # pasted images kept; older ones are removed


@dataclass
class Paste:
    image: Path | None = None  # a saved image
    text: str = ""  # or text
    problem: str = ""  # why nothing could be pasted


def run(argv: list[str]) -> bytes | None:
    try:
        done = subprocess.run(argv, capture_output=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def folder() -> Path:
    return config.STATE_DIR / "pasted"


def save(data: bytes, suffix: str) -> Path:
    where = folder()
    where.mkdir(parents=True, exist_ok=True)
    old = sorted(where.glob("pasted-*"))  # the names sort by time
    for path in old[: max(0, len(old) - KEEP + 1)]:
        path.unlink(missing_ok=True)
    now = time.time_ns()
    stamp = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now / 1e9))}-{now % 1_000_000_000:09d}"
    path = where / f"pasted-{stamp}{suffix}"
    n = 1
    while path.exists():
        n += 1
        path = where / f"pasted-{stamp}-{n}{suffix}"
    path.write_bytes(data)
    return path


def pick(types: list[str]) -> str | None:
    return next((t for t in IMAGE_TYPES if t in types), None)


def paste() -> Paste:
    """Whatever is on the clipboard: an image (saved to a file) or text."""
    if sys.platform == "darwin":
        return paste_mac()
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        return paste_with(
            ["wl-paste", "--list-types"],
            lambda kind: ["wl-paste", "--no-newline", "--type", kind],
            ["wl-paste", "--no-newline"],
        )
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        return paste_with(
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            lambda kind: ["xclip", "-selection", "clipboard", "-t", kind, "-o"],
            ["xclip", "-selection", "clipboard", "-o"],
        )
    return Paste(problem=status()[0])


def status() -> tuple[str, bool | None]:
    """Whether Ctrl+V can paste images here, and if not, what to do (for the paste and `lcode doctor`)."""
    if sys.platform == "darwin":
        return "Ctrl+V pastes images" + (" (with pngpaste)" if shutil.which("pngpaste") else ""), True
    if os.environ.get("WAYLAND_DISPLAY"):
        if shutil.which("wl-paste"):
            return "Ctrl+V pastes images (with wl-paste)", True
        return "To paste images with Ctrl+V, install wl-clipboard (for example `sudo apt install wl-clipboard`).", None
    if os.environ.get("DISPLAY"):
        if shutil.which("xclip"):
            return "Ctrl+V pastes images (with xclip)", True
        return "To paste images with Ctrl+V, install xclip (for example `sudo apt install xclip`).", None
    return "There's no clipboard here (no desktop session), so give an image by its path.", None


def paste_with(list_types: list[str], get, get_text: list[str]) -> Paste:
    types = (run(list_types) or b"").decode(errors="replace").split()
    kind = pick(types)
    if kind:
        data = run(get(kind))
        if data:
            return Paste(image=save(data, SUFFIXES[kind]))
    text = run(get_text)
    if text:
        return Paste(text=text.decode(errors="replace"))
    return Paste(problem="The clipboard is empty.")


def paste_mac() -> Paste:
    if shutil.which("pngpaste"):
        data = run(["pngpaste", "-"])
        if data:
            return Paste(image=save(data, ".png"))
    else:
        target = folder() / ".clipboard.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        script = [
            "try",
            "set png to (the clipboard as «class PNGf»)",
            f'set f to open for access POSIX file "{target}" with write permission',
            "write png to f",
            "close access f",
            "end try",
        ]
        run(["osascript", *[arg for line in script for arg in ("-e", line)]])
        if target.exists() and target.stat().st_size:
            data = target.read_bytes()
            target.unlink(missing_ok=True)
            return Paste(image=save(data, ".png"))
    text = run(["pbpaste"])
    if text:
        return Paste(text=text.decode(errors="replace"))
    return Paste(problem="The clipboard is empty.")
