"""Image options: how many, what shape, which seed, how good, what to avoid, which style.

They're typed in the request (`a red fox --n 4 --ar 16:9 --seed 42 --quality best --style photo --no "people"`), or
set for the session with `/options n=4 ar=16:9`. Flags in a request win over the session's options for that request.
"""

from __future__ import annotations

import math
import re
import shlex
from dataclasses import asdict, dataclass, replace

QUALITIES = ("fast", "balanced", "best")
MAX_COUNT = 8
MAX_SECONDS = 8  # video: longer clips need more memory than a laptop GPU has, and models drift
SIZES = {  # names for aspect ratios
    "square": "1:1",
    "portrait": "3:4",
    "landscape": "4:3",
    "wide": "16:9",
    "tall": "9:16",
    "photo": "3:2",
    "cinema": "21:9",
}
STYLES = {
    "photo": "professional photograph, natural light, sharp focus, 35mm lens",
    "cinematic": "cinematic film still, dramatic lighting, shallow depth of field, color graded",
    "anime": "anime illustration, clean line art, vibrant cel shading",
    "watercolor": "watercolor painting, soft washes, visible paper texture",
    "oil": "oil painting, visible brush strokes, rich colors",
    "pixel": "pixel art, 16-bit, crisp pixels, limited palette",
    "3d": "3D render, soft global illumination, studio lighting",
    "sketch": "pencil sketch, graphite, cross-hatching, on paper",
    "comic": "comic book art, bold ink outlines, halftone shading",
    "flat": "flat vector illustration, minimal, bold shapes",
}
CAMERA = {  # video: --camera name -> the camera model's pose
    "static": "Static",
    "pan-left": "Pan Left",
    "pan-right": "Pan Right",
    "pan-up": "Pan Up",
    "pan-down": "Pan Down",
    "tilt-up": "Pan Up",
    "tilt-down": "Pan Down",
    "zoom-in": "Zoom In",
    "zoom-out": "Zoom Out",
    "dolly-in": "Zoom In",
    "dolly-out": "Zoom Out",
    "rotate-left": "Anti Clockwise (ACW)",
    "rotate-right": "ClockWise (CW)",
}
ALIASES = {
    "n": "n",
    "count": "n",
    "ar": "size",
    "aspect": "size",
    "size": "size",
    "seed": "seed",
    "quality": "quality",
    "q": "quality",
    "no": "negative",
    "negative": "negative",
    "style": "style",
    "seconds": "seconds",
    "duration": "seconds",
    "camera": "camera",
}
FLAG = re.compile(r"(?:^|\s)--?(" + "|".join(sorted(ALIASES, key=len, reverse=True)) + r")\b(?=[\s=]|$)")
DIMENSIONS = re.compile(r"^(\d{2,4})\s*[x×]\s*(\d{2,4})$")
RATIO = re.compile(r"^(\d{1,2})\s*:\s*(\d{1,2})$")


class OptionError(ValueError):
    pass


@dataclass
class Options:
    n: int = 1
    size: str = "1:1"  # an aspect ratio (16:9), a name (wide) or pixels (1344x768)
    seed: int | None = None  # None: a new random seed for each image
    quality: str = "balanced"
    negative: str = ""
    style: str = ""
    seconds: float = 5.0  # video only
    camera: str = ""  # video only: a move from CAMERA, made by a camera-control model

    def describe(self, video: bool = False) -> str:
        seed = "random" if self.seed is None else str(self.seed)
        parts = [f"n={self.n}", f"size={self.size}", f"seed={seed}", f"quality={self.quality}"]
        if video:
            parts.append(f"seconds={self.seconds:g}")
            if self.camera:
                parts.append(f"camera={self.camera}")
        if self.style:
            parts.append(f"style={self.style}")
        if self.negative:
            parts.append(f"no={self.negative!r}")
        return " ".join(parts)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> Options:
        known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        try:
            return set_values(cls(), known)
        except OptionError:
            return cls()


def set_values(options: Options, values: dict[str, object]) -> Options:
    """A copy of `options` with `values` (by name or alias) applied. Raises OptionError saying what's wrong."""
    out = replace(options)
    for raw_key, raw in values.items():
        key = ALIASES.get(str(raw_key).lower().lstrip("-"))
        if key is None:
            raise OptionError(
                f"unknown option {raw_key!r}: use n, ar (or size), seed, quality, style, no, seconds or camera"
            )
        value = str(raw).strip() if raw is not None else ""
        if key == "n":
            if not value.isdigit() or not 1 <= int(value) <= MAX_COUNT:
                raise OptionError(f"n is how many images: 1 to {MAX_COUNT}")
            out.n = int(value)
        elif key == "size":
            out.size = check_size(value)
        elif key == "seed":
            if value.lower() in ("random", "none", ""):
                out.seed = None
            elif value.isdigit() and int(value) < 2**63:
                out.seed = int(value)
            else:
                raise OptionError("seed is a whole number, or random")
        elif key == "quality":
            if value.lower() not in QUALITIES:
                raise OptionError(f"quality is {', '.join(QUALITIES)}")
            out.quality = value.lower()
        elif key == "style":
            if value.lower() in ("none", "off", ""):
                out.style = ""
            elif value.lower() in STYLES:
                out.style = value.lower()
            else:
                raise OptionError(f"style is one of {', '.join(STYLES)} (or none)")
        elif key == "negative":
            out.negative = "" if value.lower() in ("none", "off") else value
        elif key == "camera":
            if value.lower() in ("none", "off", ""):
                out.camera = ""
            elif value.lower() in CAMERA:
                out.camera = value.lower()
            else:
                raise OptionError(f"camera is one of {', '.join(CAMERA)} (or none)")
        elif key == "seconds":
            try:
                seconds = float(value.rstrip("s"))
            except ValueError:
                seconds = 0
            if not 1 <= seconds <= MAX_SECONDS:
                raise OptionError(f"seconds is how long a video is: 1 to {MAX_SECONDS}")
            out.seconds = seconds
    return out


def check_size(value: str) -> str:
    v = value.lower().replace(" ", "")
    if v in SIZES:
        return v
    if RATIO.match(v):
        w, h = (int(x) for x in RATIO.match(v).groups())
        if w and h and 1 / 4 <= w / h <= 4:
            return f"{w}:{h}"
    m = DIMENSIONS.match(v)
    if m:
        w, h = (int(x) for x in m.groups())
        if 256 <= w <= 2048 and 256 <= h <= 2048:
            return f"{w}x{h}"
        raise OptionError("a size in pixels is 256 to 2048 on each side, e.g. 1344x768")
    raise OptionError(f"size is a ratio (16:9), pixels (1344x768) or one of {', '.join(SIZES)}")


def parse(text: str, defaults: Options) -> tuple[str, Options]:
    """The prompt without its flags, and the options for this request. Raises OptionError."""
    matches = list(FLAG.finditer(text))
    if not matches:
        return text.strip(), defaults
    prompt = text[: matches[0].start()]
    values: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        rest = text[m.end() : end].strip().lstrip("=").strip()
        try:
            words = shlex.split(rest)
        except ValueError:
            words = rest.split()
        key = ALIASES[m.group(1).lower()]
        if key == "negative":
            values[key] = " ".join(words)
        else:
            if not words:
                raise OptionError(f"--{m.group(1)} needs a value")
            values[key] = words[0]
            if len(words) > 1:  # words after a flag's value belong to the prompt
                prompt += " " + " ".join(words[1:])
    return prompt.strip(), set_values(defaults, values)


def parse_assignments(text: str) -> dict[str, str]:
    """`n=4 ar=16:9 no="blurry, text"` -> {"n": "4", "ar": "16:9", "no": "blurry, text"}."""
    try:
        words = shlex.split(text)
    except ValueError as e:
        raise OptionError(f"can't read {text!r}: {e}") from e
    out = {}
    for word in words:
        key, sep, value = word.partition("=")
        if not sep:
            raise OptionError(f"write options as name=value, e.g. n=4 ar=16:9 (not {word!r})")
        out[key] = value
    return out


def dimensions(size: str, megapixels: float, multiple: int = 16) -> tuple[int, int]:
    """Width and height in pixels: from a ratio and the quality's megapixels, or exact pixels as given."""
    m = DIMENSIONS.match(size)
    if m:
        w, h = (int(x) for x in m.groups())
        return round_to(w, multiple), round_to(h, multiple)
    ratio = SIZES.get(size, size)
    w, h = (int(x) for x in ratio.split(":"))
    area = megapixels * 1024 * 1024
    width = math.sqrt(area * w / h)
    return round_to(width, multiple), round_to(width * h / w, multiple)


def round_to(value: float, multiple: int) -> int:
    return max(multiple, round(value / multiple) * multiple)


def replace_seed(options: Options, n: int | None = None) -> Options:
    """The options with a random seed again (for /again and /vary), and optionally another count."""
    return replace(options, seed=None, n=n or options.n)


def frames(seconds: float, fps: int) -> int:
    """Frames for a clip: a multiple of 4 plus 1, as video models that compress time 4 to 1 need."""
    return max(1, round(seconds * fps / 4)) * 4 + 1


def styled(prompt: str, style: str) -> str:
    return f"{prompt}, {STYLES[style]}" if style else prompt
