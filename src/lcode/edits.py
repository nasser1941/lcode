"""Image edits as changes to a ComfyUI workflow: reference images, inpainting through a mask, outpainting.

A model that edits with reference latents (FLUX.2 klein) takes the images through its own text-to-image workflow:
each image is scaled, encoded by the VAE and attached to both conditionings before the guider, and the instruction is
the prompt. On top of that:

- **Inpainting** keeps the original outside a mask: the edited picture is pasted into the original only where the
  mask is white (with a soft edge).
- **Outpainting** puts the image on a bigger canvas, lets the model fill the new border, and pastes the original back,
  so it stays exactly as it was.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

BASE = 8000  # node ids lcode adds start here (the workflows' own are small numbers)
SIZE_NODE = str(BASE + 9)
IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
EXTEND_HINT = (
    "Replace the blurry border around the sharp picture with sharp, detailed scenery that continues the picture "
    "outward, matching its light, perspective and style. Keep the sharp middle exactly as it is. No frame, no bars."
)


class EditError(ValueError):
    pass


def node_of(graph: dict, class_type: str) -> str | None:
    return next((nid for nid, node in graph.items() if node.get("class_type") == class_type), None)


def save_node(graph: dict) -> str:
    nid = node_of(graph, "SaveImage")
    if nid is None:
        raise EditError("the workflow saves no image")
    return nid


def load_images(graph: dict, names: list[str]) -> list[list]:
    """LoadImage nodes for uploaded images; their IMAGE outputs."""
    links = []
    for k, name in enumerate(names):
        nid = str(BASE + 100 + k)
        graph[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        links.append([nid, 0])
    return links


def add_references(graph: dict, images: list[list], megapixels: float, size_from_first: bool) -> list:
    """Attach the images as reference latents to the guider's positive and negative conditionings.

    Each is scaled to `megapixels` first. With `size_from_first`, the picture made has the first image's size
    (its width and height go to every node of the workflow that takes them). Returns the first scaled image.
    """
    guider = node_of(graph, "CFGGuider")
    vae = node_of(graph, "VAELoader")
    if guider is None or vae is None:
        raise EditError("this workflow can't take reference images")
    positive, negative = graph[guider]["inputs"]["positive"], graph[guider]["inputs"]["negative"]
    first = None
    for k, image in enumerate(images):
        scale, encode, ref_pos, ref_neg = (str(BASE + 10 * (k + 1) + i) for i in range(4))
        graph[scale] = {
            "class_type": "ImageScaleToTotalPixels",
            "inputs": {"image": image, "upscale_method": "lanczos", "megapixels": megapixels, "resolution_steps": 16},
        }
        graph[encode] = {"class_type": "VAEEncode", "inputs": {"pixels": [scale, 0], "vae": [vae, 0]}}
        graph[ref_pos] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": positive, "latent": [encode, 0]}}
        graph[ref_neg] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": negative, "latent": [encode, 0]}}
        positive, negative = [ref_pos, 0], [ref_neg, 0]
        first = first or [scale, 0]
    graph[guider]["inputs"]["positive"], graph[guider]["inputs"]["negative"] = positive, negative
    if size_from_first and first:
        set_size(graph, first)
    return first


def set_size(graph: dict, image: list) -> None:
    """The picture made takes `image`'s size: its width and height go to every workflow node that takes them."""
    graph[SIZE_NODE] = {"class_type": "GetImageSize", "inputs": {"image": image}}
    for nid, node in graph.items():
        if nid.isdigit() and int(nid) >= BASE:
            continue  # lcode's own nodes keep their sizes
        inputs = node.get("inputs", {})
        for key, out in (("width", 0), ("height", 1)):
            if key in inputs and not isinstance(inputs[key], list):
                inputs[key] = [SIZE_NODE, out]


def scale_to(graph: dict, image: list, megapixels: float) -> list:
    nid = str(BASE + 301)
    graph[nid] = {
        "class_type": "ImageScaleToTotalPixels",
        "inputs": {"image": image, "upscale_method": "lanczos", "megapixels": megapixels, "resolution_steps": 16},
    }
    return [nid, 0]


def mask_from_file(graph: dict, mask_name: str) -> list:
    """The user's mask image (white: change), at the picture's size, with a soft edge."""
    if SIZE_NODE not in graph:
        raise EditError("inpainting needs the picture at the original's size")
    load, to_image, scale, blur, to_mask = (str(BASE + 200 + i) for i in range(5))
    graph[load] = {"class_type": "LoadImageMask", "inputs": {"image": mask_name, "channel": "red"}}
    graph[to_image] = {"class_type": "MaskToImage", "inputs": {"mask": [load, 0]}}
    graph[scale] = {
        "class_type": "ImageScale",
        "inputs": {"image": [to_image, 0], "upscale_method": "bilinear", "width": [SIZE_NODE, 0],
                   "height": [SIZE_NODE, 1], "crop": "disabled"},
    }  # fmt: skip
    graph[blur] = {"class_type": "ImageBlur", "inputs": {"image": [scale, 0], "blur_radius": 8, "sigma": 4.0}}
    graph[to_mask] = {"class_type": "ImageToMask", "inputs": {"image": [blur, 0], "channel": "red"}}
    return [to_mask, 0]


def mask_scaled(graph: dict, mask: list, megapixels: float) -> list:
    """A mask brought to the same size as an image scaled to `megapixels` (outpainting's border)."""
    to_image, scale, to_mask = (str(BASE + 310 + i) for i in range(3))
    graph[to_image] = {"class_type": "MaskToImage", "inputs": {"mask": mask}}
    graph[scale] = {
        "class_type": "ImageScaleToTotalPixels",
        "inputs": {"image": [to_image, 0], "upscale_method": "bilinear", "megapixels": megapixels,
                   "resolution_steps": 16},
    }  # fmt: skip
    graph[to_mask] = {"class_type": "ImageToMask", "inputs": {"image": [scale, 0], "channel": "red"}}
    return [to_mask, 0]


def sample_inside(graph: dict, image: list, mask: list) -> None:
    """Start from the image itself and let the sampler change only what the mask covers."""
    sampler = node_of(graph, "SamplerCustomAdvanced") or node_of(graph, "KSampler")
    vae = node_of(graph, "VAELoader")
    if sampler is None or vae is None:
        raise EditError("this workflow can't paint inside a mask")
    encode, masked = str(BASE + 400), str(BASE + 401)
    graph[encode] = {"class_type": "VAEEncode", "inputs": {"pixels": image, "vae": [vae, 0]}}
    graph[masked] = {"class_type": "SetLatentNoiseMask", "inputs": {"samples": [encode, 0], "mask": mask}}
    graph[sampler]["inputs"]["latent_image"] = [masked, 0]


def paste(graph: dict, destination: list, source: list, mask: list) -> None:
    """The saved picture becomes `destination` with `source` pasted where the mask is white."""
    save = save_node(graph)
    nid = str(BASE + 500)
    graph[nid] = {
        "class_type": "ImageCompositeMasked",
        "inputs": {"destination": destination, "source": source, "x": 0, "y": 0, "resize_source": False,
                   "mask": mask},
    }  # fmt: skip
    graph[save]["inputs"]["images"] = [nid, 0]


def inpaint(graph: dict, original: list, mask_name: str) -> None:
    """Change only where the mask image is white: sampled there from the original, then pasted into it."""
    mask = mask_from_file(graph, mask_name)
    sample_inside(graph, original, mask)
    paste(graph, original, graph[save_node(graph)]["inputs"]["images"], mask)


def pad_canvas(graph: dict, image: list, pads: tuple[int, int, int, int]) -> tuple[list, list]:
    """Outpainting's canvas, bigger by `pads` (left, top, right, bottom); (canvas, mask of the new border).

    The border starts as a blurred, stretched copy of the image rather than flat grey: a fast model keeps a little of
    what it starts from, and grey would show.
    """
    pad, size, stretched, blurred, canvas = (str(BASE + 300 + i) for i in (0, 2, 3, 4, 5))
    left, top, right, bottom = pads
    graph[pad] = {
        "class_type": "ImagePadForOutpaint",
        "inputs": {"image": image, "left": left, "top": top, "right": right, "bottom": bottom, "feathering": 48},
    }
    graph[size] = {"class_type": "GetImageSize", "inputs": {"image": [pad, 0]}}
    graph[stretched] = {
        "class_type": "ImageScale",
        "inputs": {"image": image, "upscale_method": "bilinear", "width": [size, 0], "height": [size, 1],
                   "crop": "disabled"},
    }  # fmt: skip
    graph[blurred] = {"class_type": "ImageBlur", "inputs": {"image": [stretched, 0], "blur_radius": 31, "sigma": 10.0}}
    graph[canvas] = {
        "class_type": "ImageCompositeMasked",
        "inputs": {"destination": [blurred, 0], "source": image, "x": left, "y": top, "resize_source": False},
    }
    return [canvas, 0], [pad, 1]


def outpaint(graph: dict, canvas_scaled: list, border: list, megapixels: float) -> None:
    """Fill only the new border, sampled around the original, and keep the original exactly as it was."""
    mask = mask_scaled(graph, border, megapixels)
    sample_inside(graph, canvas_scaled, mask)
    invert = str(BASE + 501)
    graph[invert] = {"class_type": "InvertMask", "inputs": {"mask": mask}}
    paste(graph, graph[save_node(graph)]["inputs"]["images"], canvas_scaled, [invert, 0])


# ----------------------------------------------------------------------------- sizes and arguments


def image_size(path: Path) -> tuple[int, int]:
    """Width and height from a PNG, JPEG or WebP file's header."""
    data = path.read_bytes()[:65536]
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            length = struct.unpack(">H", data[i + 2 : i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return w, h
            i += 2 + length
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8X":
            return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
        if kind == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if kind == b"VP8L":
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    raise EditError(f"can't read the size of {path.name}: use a PNG, JPEG or WebP image")


def pads_for_ratio(width: int, height: int, ratio: str) -> tuple[int, int, int, int]:
    """Borders that bring the image to an aspect ratio (a:b), split evenly, in steps of 8 pixels."""
    m = re.fullmatch(r"(\d{1,2}):(\d{1,2})", ratio)
    if not m or not int(m.group(1)) or not int(m.group(2)):
        raise EditError("the shape to extend to is a ratio, e.g. --ar 16:9")
    a, b = int(m.group(1)), int(m.group(2))
    if abs(width / height - a / b) < 0.01:
        raise EditError(f"it's {a}:{b} already: choose another shape, or --left/--right/--top/--bottom pixels")
    if width / height < a / b:  # wider
        extra = round(height * a / b) - width
        left = (extra // 2) // 8 * 8
        return left, 0, (extra - left + 7) // 8 * 8, 0
    extra = round(width * b / a) - height
    top = (extra // 2) // 8 * 8
    return 0, top, 0, (extra - top + 7) // 8 * 8


EXTEND_FLAG = re.compile(r"--(left|top|right|bottom)\s+(\d+)")


def parse_extend(text: str, width: int, height: int) -> tuple[tuple[int, int, int, int], str]:
    """`/extend` arguments: --ar 16:9, or --left/--top/--right/--bottom pixels; the rest describes the new part."""
    sides = {k: int(v) for k, v in EXTEND_FLAG.findall(text)}
    text = EXTEND_FLAG.sub(" ", text)
    ratio = re.search(r"--(?:ar|size)\s+(\S+)", text)
    text = re.sub(r"--(?:ar|size)\s+\S+", " ", text)
    if ratio and sides:
        raise EditError("extend by a shape (--ar 16:9) or by sides (--left 256), not both")
    if ratio:
        from lcode.imageopts import SIZES

        pads = pads_for_ratio(width, height, SIZES.get(ratio.group(1), ratio.group(1)))
    elif sides:
        pads = tuple((sides.get(k, 0) + 7) // 8 * 8 for k in ("left", "top", "right", "bottom"))
        if any(p > 2048 for p in pads):
            raise EditError("a side grows by at most 2048 pixels")
    else:
        pads = pads_for_ratio(width, height, "16:9" if width <= height * 16 / 9 - 1 else "1:1")
    return pads, " ".join(text.split())


REFERENCE = re.compile(r'(?:^|(?<=\s))@(#\d+|"[^"]+"|\S+)')


def split_references(text: str) -> tuple[str, list[str]]:
    """The prompt without its @references, and the references (#3, or file paths) in order."""
    refs = [m.group(1).strip('"') for m in REFERENCE.finditer(text)]
    return " ".join(REFERENCE.sub(" ", text).split()), refs


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_TYPES
