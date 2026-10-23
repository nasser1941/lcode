"""Show an image in the terminal: the picture itself where the terminal can (kitty's graphics protocol, iTerm2's
inline images), otherwise a small colour preview drawn with half blocks.

The half-block preview comes from a thumbnail ComfyUI makes along with the image (a few dozen pixels wide), decoded
here: lcode needs no imaging library for it. A PNG decoder for 8-bit, non-interlaced images is all that takes.
"""

from __future__ import annotations

import base64
import os
import struct
import zlib
from collections.abc import Mapping

from rich.text import Text

PROTOCOLS = ("auto", "kitty", "iterm", "blocks", "off")
COLUMNS = 40  # how wide a preview is, in terminal columns
THUMB_WIDTH = 64  # pixels: the thumbnail ComfyUI makes for half-block previews
KITTY_CHUNK = 4096


def protocol(setting: str = "auto", env: Mapping[str, str] = os.environ) -> str:
    """kitty, iterm, blocks or off: what this terminal can show (or what the `preview` setting says)."""
    if setting != "auto":
        return setting
    if env.get("TMUX") or env.get("TERM", "").startswith("screen"):
        return "blocks"  # multiplexers need passthrough for images: keep to text
    if env.get("KITTY_WINDOW_ID") or env.get("TERM") == "xterm-kitty" or env.get("TERM_PROGRAM") == "ghostty":
        return "kitty"
    if env.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm") or env.get("LC_TERMINAL") == "iTerm2":
        return "iterm"
    return "blocks"


def kitty(png: bytes, columns: int = COLUMNS) -> str:
    """The escape sequences that show a PNG in kitty (and Ghostty), `columns` wide."""
    data = base64.b64encode(png).decode()
    chunks = [data[i : i + KITTY_CHUNK] for i in range(0, len(data), KITTY_CHUNK)] or [""]
    out = []
    for i, chunk in enumerate(chunks):
        more = 1 if i < len(chunks) - 1 else 0
        head = f"a=T,f=100,c={columns},q=2,m={more}" if i == 0 else f"m={more}"
        out.append(f"\x1b_G{head};{chunk}\x1b\\")
    return "".join(out) + "\n"


def iterm(png: bytes, columns: int = COLUMNS) -> str:
    """The escape sequence that shows an image in iTerm2 (and WezTerm), `columns` wide."""
    data = base64.b64encode(png).decode()
    return f"\x1b]1337;File=inline=1;size={len(png)};width={columns};preserveAspectRatio=1:{data}\x07\n"


# ----------------------------------------------------------------------------- PNG, decoded


class PngError(ValueError):
    pass


def decode_png(data: bytes) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
    """(width, height, rows of RGB pixels) of an 8-bit, non-interlaced PNG (grey, RGB, palette, with alpha)."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PngError("not a PNG")
    i, idat, palette = 8, [], b""
    width = height = depth = color = interlace = 0
    while i + 8 <= len(data):
        (n,) = struct.unpack(">I", data[i : i + 4])
        kind, body = data[i + 4 : i + 8], data[i + 8 : i + 8 + n]
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        i += 12 + n
    if depth != 8 or interlace or color not in (0, 2, 3, 4, 6):
        raise PngError(f"unsupported PNG (bit depth {depth}, colour type {color}, interlace {interlace})")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    rows, previous = [], bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind, line = raw[start], bytearray(raw[start + 1 : start + 1 + stride])
        unfilter(kind, line, previous, channels)
        rows.append(pixels(line, color, channels, palette))
        previous = line
    return width, height, rows


def unfilter(kind: int, line: bytearray, previous: bytearray, bpp: int) -> None:
    if kind == 0:
        return
    for x in range(len(line)):
        left = line[x - bpp] if x >= bpp else 0
        up = previous[x]
        if kind == 1:
            line[x] = (line[x] + left) & 0xFF
        elif kind == 2:
            line[x] = (line[x] + up) & 0xFF
        elif kind == 3:
            line[x] = (line[x] + (left + up) // 2) & 0xFF
        elif kind == 4:
            corner = previous[x - bpp] if x >= bpp else 0
            p = left + up - corner
            pa, pb, pc = abs(p - left), abs(p - up), abs(p - corner)
            line[x] = (line[x] + (left if pa <= pb and pa <= pc else up if pb <= pc else corner)) & 0xFF
        else:
            raise PngError(f"unknown PNG filter {kind}")


def pixels(line: bytearray, color: int, channels: int, palette: bytes) -> list[tuple[int, int, int]]:
    out = []
    for x in range(0, len(line), channels):
        if color in (0, 4):
            out.append((line[x], line[x], line[x]))
        elif color == 3:
            j = line[x] * 3
            out.append(tuple(palette[j : j + 3]) if j + 3 <= len(palette) else (0, 0, 0))
        else:
            out.append((line[x], line[x + 1], line[x + 2]))
    return out


def blocks(png: bytes, columns: int = COLUMNS) -> Text:
    """A colour preview `columns` wide: each character shows two pixels, one above the other (▀)."""
    width, height, rows = decode_png(png)
    columns = max(1, min(columns, width))
    lines = max(1, round(height * columns / width / 2))
    text = Text()
    for row in range(lines):
        for col in range(columns):
            x = min(width - 1, col * width // columns)
            top = rows[min(height - 1, (2 * row) * height // (2 * lines))][x]
            bottom = rows[min(height - 1, (2 * row + 1) * height // (2 * lines))][x]
            text.append("▀", style=f"{hex_color(top)} on {hex_color(bottom)}")
        if row < lines - 1:
            text.append("\n")
    return text


def hex_color(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def thumbnail_size(width: int, height: int, thumb: int = THUMB_WIDTH) -> tuple[int, int]:
    """The thumbnail ComfyUI makes for the half-block preview: `thumb` pixels wide, the image's shape."""
    return thumb, max(2, round(thumb * height / width))
