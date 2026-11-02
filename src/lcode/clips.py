"""Video files as they are: whether a file is one, and its length, size and frame rate (ffprobe, or the MP4 itself)."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


VIDEO_TYPES = (".mp4", ".webm", ".mkv", ".mov")


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


def mp4_size(path: Path) -> tuple[int, int]:
    """An MP4's picture size from its first video track header (tkhd), or (0, 0)."""
    try:
        data = path.read_bytes()[: 1 << 22]
    except OSError:
        return 0, 0
    i = data.find(b"tkhd")
    while i >= 4:
        size = 8 + (96 if data[i + 4 : i + 5] == b"\x01" else 84)  # the box, with its size and type
        end = i - 4 + size  # width and height end it, as 16.16 fixed-point numbers
        if struct.unpack(">I", data[i - 4 : i])[0] == size and end <= len(data):
            width, height = struct.unpack(">II", data[end - 8 : end])
            if width and height:
                return width >> 16, height >> 16
        i = data.find(b"tkhd", i + 4)
    return 0, 0
