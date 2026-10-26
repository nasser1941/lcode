"""The prompt assistant: a small local language model that helps image and video mode with words.

Image and video models do better with detailed prompts in English, written the way each expects (a picture's subject,
light and composition; a clip's motion and camera). The assistant:

- **expands** a short request into such a prompt, from any language;
- **looks at a start picture** (it can see) and writes the video prompt around it;
- **reads follow-ups** ("darker", "add a hat") as changes to the last image;
- **checks a result** against the request and suggests a better prompt when it misses;
- **writes storyboards** for films (lcode.film).

It runs in Ollama, loaded for each job and unloaded right after (keep_alive 0), so the image or video model has the GPU
to itself while it works. The `prompt_assistant` setting says where it helps: `video` (the default: measured, video
models make much better clips from fuller prompts, while FLUX.2 klein didn't make better pictures), `all` (pictures
too, and follow-ups), `off`, or an Ollama model's name (everywhere, with that model).
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path

PREFERRED = ("qwen3.5:4b", "qwen3.5:9b", "qwen3:4b", "gemma3:4b", "llama3.2:3b")
MAX_PROMPT = 900  # characters: image and video models read only so much

STYLE_GUIDE = {
    "image": (
        "You write prompts for an image generation model. Rewrite the user's request as one detailed prompt in "
        "English: the subject, what it looks like, the setting, the light, the composition and the medium (photo, "
        "painting…). Keep everything the user asked for, add no new subjects or text, and keep any words in quotes "
        "exactly as written (they are text to show in the picture). 40 to 80 words, one paragraph, no lists."
    ),
    "edit": (
        "You write instructions for an image editing model. Rewrite the user's request as one short, clear "
        "instruction in English that says exactly what to change in the picture and what to keep. 10 to 30 words."
    ),
    "video": (
        "You write prompts for a video generation model. Rewrite the user's request as one prompt in English: the "
        "subject and the setting, the motion over time, the camera (a shot: slow zoom in, pan left, tracking shot, "
        "static), and the light and mood. Describe movement, not a single picture. Never mention a camera as an "
        "object in the scene. 50 to 90 words, one paragraph."
    ),
}
START_IMAGE = (
    "The video starts from the attached picture. Begin the prompt by describing what the picture shows (subject, "
    "setting, light, style) in one sentence, then the motion the user asked for."
)
FOLLOW_UP = (
    "The user made a picture from this request: {last!r}. Now they wrote: {text!r}.\n"
    "Is the new text a change to that picture (like 'darker', 'add a hat', 'make it night', 'without the car'), or "
    "a description of a new, different picture?\n"
    'Answer with JSON only: {{"change": true or false, "instruction": "the change as one clear editing '
    'instruction in English, or empty"}}'
)
CHECK = (
    "The user asked for: {request!r}.\nLook at the picture. Does it show what was asked? Answer with JSON only: "
    '{{"matches": true or false, "missing": "what is missing or wrong, or empty", '
    '"better_prompt": "a clearer prompt for the request when it does not match, or empty"}}'
)


class AssistantError(Exception):
    pass


@dataclass
class Check:
    matches: bool
    missing: str = ""
    better_prompt: str = ""


SCOPES = ("off", "video", "all", "auto")


def pick(ollama, setting: str) -> str | None:
    """The assistant's model for a setting: off, or a small installed model (video, all, auto), or a model's name."""
    if setting in ("off", "", None):
        return None
    try:
        installed = set(ollama.installed_names())
    except Exception:
        return None
    if setting not in SCOPES:
        return setting if setting in installed or f"{setting}:latest" in installed else None
    return next((m for m in PREFERRED if m in installed), None)


class Assistant:
    def __init__(self, ollama, model: str):
        self.ollama, self.model = ollama, model

    def _ask(self, system: str, user: str, images: list[Path] = (), json_answer: bool = False) -> str:
        message: dict = {"role": "user", "content": user}
        if images:
            message["images"] = [base64.b64encode(p.read_bytes()).decode() for p in images]
        return self.converse([{"role": "system", "content": system}, message], json_answer=json_answer)

    def converse(self, messages: list[dict], json_answer: bool = False, num_ctx: int = 4096) -> str:
        """The model's answer to a conversation (lcode.manual's answers use a longer one)."""
        payload = {
            "model": self.model,
            "messages": messages,
            "think": False,
            "keep_alive": 0,  # unloaded right after: the image or video model needs the GPU
            "options": {"temperature": 0.4, "num_ctx": num_ctx},
        }
        if json_answer:
            payload["format"] = "json"
        try:
            reply = self.ollama.chat(payload, timeout=180)
        except Exception as e:
            raise AssistantError(f"the prompt assistant ({self.model}) didn't answer: {e}") from e
        return ((reply.get("message") or {}).get("content") or "").strip()

    def expand(self, request: str, kind: str = "image", start: Path | None = None) -> str:
        """A detailed prompt for the model, in English, from the user's request (and a start picture)."""
        system = STYLE_GUIDE.get(kind, STYLE_GUIDE["image"])
        if start is not None and kind == "video":
            system += " " + START_IMAGE
        text = clean(self._ask(system, request, [start] if start is not None else []))
        return text[:MAX_PROMPT] if text else request

    def follow_up(self, text: str, last: str) -> str | None:
        """When `text` is a change to the last picture, the change as an editing instruction; else None."""
        answer = parse_json(
            self._ask("You answer with JSON only.", FOLLOW_UP.format(last=last, text=text), json_answer=True)
        )
        if answer.get("change") is True and str(answer.get("instruction") or "").strip():
            return str(answer["instruction"]).strip()
        return None

    def check(self, picture: Path, request: str) -> Check:
        """Whether the picture shows what was asked, and a better prompt when it doesn't."""
        answer = parse_json(
            self._ask(
                "You check pictures. You answer with JSON only.",
                CHECK.format(request=request),
                [picture],
                json_answer=True,
            )
        )
        matches = answer.get("matches")
        if not isinstance(matches, bool):
            raise AssistantError("the prompt assistant's check had no answer")
        return Check(matches, str(answer.get("missing") or "").strip(), str(answer.get("better_prompt") or "").strip())

    def write(self, system: str, request: str, json_answer: bool = False) -> str:
        """Any other writing job (storyboards)."""
        return self._ask(system, request, json_answer=json_answer)


def clean(text: str) -> str:
    """The prompt alone: without quotes around it, a 'Prompt:' label, or reasoning tags."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^(here is|here's)[^:\n]*:\s*", "", text, flags=re.I)
    text = re.sub(r"^\**\s*(prompt|rewritten prompt|instruction)\s*\**\s*:\s*\**\s*", "", text, flags=re.I)
    return " ".join(text.strip().strip('"').strip("“”").split())


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, flags=re.S)
    try:
        value = json.loads(m.group(0) if m else text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
