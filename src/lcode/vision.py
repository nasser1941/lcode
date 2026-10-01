"""Looking at images: screenshots, diagrams and photos the user attaches or the model opens.

The model that runs the conversation often can't see: lcode's text-only variants leave out the
vision projector to save GPU memory. So lcode asks a model that can see to describe the image in
detail (all text transcribed, layout, colors), with the user's question in mind, and gives the
description to the conversation. Which model looks (`vision_model = "auto"`):

1. the session's model, if it has vision;
2. otherwise the original Ollama model behind a text-only catalog variant (the same weights plus
   the vision projector; `lcode setup` already downloaded it), e.g. qwen3.6:35b-a3b-coding;
3. otherwise none, unless `vision_model` names one (for example qwen3-vl:8b).
"""

from __future__ import annotations

import base64
from pathlib import Path

from lcode import catalog
from lcode.ollama import Ollama, OllamaError

IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
DESCRIBE_CONTEXT = 16384  # enough for one image and a long description

PROMPT = """You are the eyes of a coding assistant that cannot see images. Describe this image so the \
assistant can act on it without seeing it.
- Transcribe all visible text exactly: code, error messages, terminal output, labels, menu items, numbers.
- Describe the layout and every important element: what it is, where it is, its size, color and state \
(enabled, selected, highlighted, misaligned, overlapping, cut off).
- For diagrams, charts and architecture drawings, describe the parts and how they connect.
- Say what looks wrong or unusual, if anything.
Don't guess at what isn't visible.{question}"""

INSTALL_HINT = (
    "no model that can see images is available: run `ollama pull qwen3-vl:8b` and "
    "`lcode config set vision_model qwen3-vl:8b`, or use a catalog model (lcode setup)"
)


class VisionError(Exception):
    pass


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_TYPES


def can_see(ollama: Ollama, model: str) -> bool:
    try:
        return "vision" in (ollama.show(model).get("capabilities") or [])
    except (OllamaError, AttributeError, TypeError):
        return False


def pick_model(ollama: Ollama, model: str, setting: str) -> str | None:
    """The model that looks at images for a session running `model` (see the module docstring)."""
    if setting == "off":
        return None
    if setting != "auto":
        return setting
    if can_see(ollama, model):
        return model
    spec = catalog.find(model)
    if spec and spec.tag != model and can_see(ollama, spec.tag):
        return spec.tag
    return None


def read_image(path: Path) -> bytes:
    if not path.is_file():
        raise VisionError(f"{path} doesn't exist")
    if not is_image(path):
        raise VisionError(f"{path.name} isn't an image lcode can read ({', '.join(sorted(IMAGE_TYPES))})")
    size = path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise VisionError(f"{path.name} is {size // (1024 * 1024)} MB; images up to 20 MB are supported")
    return path.read_bytes()


def describe(ollama: Ollama, model: str, image: bytes, question: str = "", options: dict | None = None,
             keep_alive: str = "30m") -> str:  # fmt: skip
    """A detailed description of the image, focused on the question if there is one."""
    focus = (
        f"\n\nThe user asks: {question.strip()}\nPay special attention to what that needs." if question.strip() else ""
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": PROMPT.format(question=focus), "images": [base64.b64encode(image).decode()]}
        ],
        "think": False,
        "keep_alive": keep_alive,
        "options": options or {"num_ctx": DESCRIBE_CONTEXT},
    }
    try:
        reply = ollama.chat(payload)
    except OllamaError as e:
        raise VisionError(f"{model} couldn't look at the image: {e}") from e
    text = ((reply.get("message") or {}).get("content") or "").strip()
    if not text:
        raise VisionError(f"{model} returned no description")
    return text
