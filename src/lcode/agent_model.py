"""Talking to the model: requests, streaming a reply, and the vision model that looks at images."""

from __future__ import annotations

import base64
import contextlib
import time
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text

from lcode import (
    catalog,
    limits,
    residency,
    subagents,
    vision,
)
from lcode.agent_base import AgentBase
from lcode.agent_context import conversation_chars
from lcode.config import format_tokens
from lcode.ollama import OllamaError
from lcode.render import MarkdownStreamer
from lcode.repeats import Circles
from lcode.steer import Steering
from lcode.tool_helpers import ToolError
from lcode.tools import parse_text_tool_calls

if TYPE_CHECKING:
    pass


GPU_MEMORY_ERRORS = (
    "out of memory",
    "illegal memory access",
    "cudamalloc failed",
    "requires more system memory",  # Ollama: the model and its context don't fit in RAM either
)


SAFE_NUM_BATCH = 512  # Ollama's default prompt batch


class Progress:
    """The status line under a running request.

    It is re-rendered on every spinner frame, so the elapsed time keeps moving even when no text
    arrives, for example while the model writes a long file into a tool call.
    """

    def __init__(self, phase: str = "Thinking", steering: Steering | None = None, tokens: bool = True) -> None:
        self.started = self.last_chunk = time.time()
        self.phase = phase
        self.chars = 0
        self.steering = steering  # the note being typed and the notes waiting, shown on a second line
        self.tokens = tokens  # False for a command running: no tokens to count

    def update(self, phase: str, text: str) -> None:
        self.phase, self.chars, self.last_chunk = phase, self.chars + len(text), time.time()

    def set_phase(self, phase: str) -> None:
        """What happens before the first token: loading the model, reading the prompt."""
        self.phase, self.last_chunk = phase, time.time()

    def __rich__(self) -> Text:
        now = time.time()
        streaming = self.phase in ("Thinking", "Writing")  # loading and reading say what they are throughout
        phase = self.phase if now - self.last_chunk < 3 or not streaming else "Working"
        tokens = f"{self.chars // 4} tokens, " if self.tokens else ""
        keys = "Ctrl+C to stop" if self.steering is None else "type a note · Esc pauses · Ctrl+C stops"
        line = Text.from_markup(f"[cyan]{phase}…[/] [dim]({tokens}{now - self.started:.0f}s · {keys})[/]")
        below = self.steering.status() if self.steering is not None else None
        return line if below is None else Text("\n").join([line, below])


class ModelCalls(AgentBase):
    """Agent's model calls."""

    def vision_model(self) -> str | None:
        """The model that looks at images for this session, if any (see lcode.vision)."""
        if self._vision is False:
            self._vision = vision.pick_model(self.ollama, self.settings.model, self.settings.vision_model)
        return self._vision if isinstance(self._vision, str) and self._vision else None

    def free_gpu(self) -> list[str]:
        """Unload lcode's models from Ollama so another program can use the GPU; the next request reloads.

        Only lcode's own models: the session's and the one that looks at images.
        """
        mine = {self.settings.model, *([self._vision] if isinstance(self._vision, str) else [])}
        freed = []
        for entry in self.ollama.running():
            name = entry.get("name") or entry.get("model") or ""
            if name in mine or name.removesuffix(":latest") in mine:
                try:
                    self.ollama.unload(name)
                    freed.append(name.removesuffix(":latest"))
                except OllamaError:
                    pass
        return freed

    def look(self, path: Path, question: str = "") -> str:
        """Describe an image with the vision model. Raises ToolError if it can't."""
        model = self.vision_model()
        if not model:
            raise ToolError(f"can't look at {path.name}: {vision.INSTALL_HINT}")
        try:
            image = vision.read_image(path)
            # The session's own model keeps its settings, so Ollama doesn't reload it.
            options = self.options() if model == self.settings.model else None
            with self.vision_swap(model, path.name):
                return vision.describe(self.ollama, model, image, question, options, self.settings.keep_alive)
        except vision.VisionError as e:
            raise ToolError(str(e)) from e

    @contextlib.contextmanager
    def vision_swap(self, model: str, what: str):
        """Look at an image with `model`, saying when it has to load and when the main model made room for it."""
        main = self.settings.model
        own = model == main
        main_was_loaded = not own and residency.placement(self.ollama, main) is not None
        if own or residency.placement(self.ollama, model) is not None:
            text = f"Looking at {what} with {model}…"
        else:
            text = f"Loading the vision model {model} to look at {what}" + (
                f"; {main} makes room and reloads afterwards…" if main_was_loaded else "…"
            )
        with self.console.status(text, spinner="dots"):
            yield
        if main_was_loaded and residency.placement(self.ollama, main) is None:
            self.made_room_for = f"the vision model {model}"
            self.say(f"{main} was unloaded to make room for {model}; it reloads for the next step.")

    def describe_image_data(self, data: str, mime: str) -> str:
        """For images that tools return (e.g. an MCP browser's screenshots): a description, or a note."""
        model = self.vision_model()
        if not model:
            return f"[{mime} image not shown: {vision.INSTALL_HINT}]"
        options = self.options() if model == self.settings.model else None
        try:
            with self.vision_swap(model, f"the {mime} image"):
                text = vision.describe(
                    self.ollama, model, base64.b64decode(data), "", options, self.settings.keep_alive
                )
        except (vision.VisionError, ValueError) as e:
            return f"[{mime} image not shown: {e}]"
        return f"[{mime} image, as described by {model}]\n{text}"

    # -- model calls
    def options(self) -> dict:
        opts: dict = {"num_ctx": self.settings.context}
        if self.settings.num_batch:
            opts["num_batch"] = self.settings.num_batch
        return opts

    def chat(self, messages: list[dict], tools: list | None, think: bool) -> Generator[dict, None, None]:
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "think": think,
            "keep_alive": self.settings.keep_alive,
            "options": self.options(),
        }
        if tools:
            payload["tools"] = tools
        started = False
        stream_options = {"on_open": self._opened} if self.cancel is not None else {}
        try:
            for chunk in self.ollama.chat_stream(payload, **stream_options):
                started = True
                yield chunk
        except OllamaError as e:
            error = str(e).lower()
            if not started and think and "think" in error and "support" in error:
                self.settings.think = False
                self.console.print(f"[dim]{self.settings.model} does not support reasoning; continuing without.[/]")
                yield from self.chat(messages, tools, False)
                return
            # Only Ollama loads the model with lcode's settings; other servers manage their own memory.
            if getattr(self.ollama, "kind", "ollama") == "ollama" and any(m in error for m in GPU_MEMORY_ERRORS):
                batch = self.settings.num_batch
                if not started and batch and batch > SAFE_NUM_BATCH:
                    # Larger batches read prompts faster but need extra VRAM that isn't always free.
                    self.settings.num_batch = SAFE_NUM_BATCH
                    self.say(
                        f"The GPU ran out of memory reading the prompt in batches of {batch} tokens. Retrying with "
                        f"batches of {SAFE_NUM_BATCH}: a little slower to read, but it needs less memory. To start "
                        f"there next time: lcode config set num_batch {SAFE_NUM_BATCH}",
                        warning=True,
                    )
                    yield from self.chat(messages, tools, think)
                    return
                context = self.settings.context
                if not started and context > catalog.MIN_USEFUL_CONTEXT:
                    # The context cache has to fit in memory; halve it until the model loads.
                    smaller = max(catalog.MIN_USEFUL_CONTEXT, context // 2)
                    self.settings.context = smaller
                    limits.record(self.settings.model, smaller)
                    self.say(
                        f"{self.settings.model} didn't fit in memory with a {format_tokens(context)} context: the "
                        "context needs its own memory next to the model. Reloading it with "
                        f"{format_tokens(smaller)}; lcode will start there next time on this machine.",
                        warning=True,
                    )
                    yield from self.chat(messages, tools, think)
                    return
                raise OllamaError(
                    f"{e}\n{self.settings.model} ran out of memory. Close other programs that use the GPU (another "
                    "model, a game, an image generator), use a smaller context (/ctx "
                    f"{format_tokens(max(catalog.MIN_USEFUL_CONTEXT // 2, context // 2)).lower()}), or pick a smaller "
                    "model (/models)."
                ) from e
            raise

    def _opened(self, response) -> None:
        self.response = response
        if self.cancel is not None and self.cancel.is_set():
            subagents.abort(response)

    def assistant_step(self) -> dict:
        """Stream one model response, rendering thinking/content live. Returns the assistant message."""
        content, thinking, tool_calls, final = "", "", [], {}
        md = MarkdownStreamer(self.console)
        progress = Progress(steering=self.steering)
        watch = self.before_request(progress)
        status = None
        printed_thinking = False

        def start_spinner() -> None:
            nonlocal status
            if status is None:
                status = self.console.status(progress, spinner="dots")
                status.start()

        def stop_spinner() -> None:
            nonlocal status
            if status is not None:
                status.stop()
                status = None

        start_spinner()
        circles = Circles()
        self.reply_trouble = ""
        stream = self.chat(self.messages, self.tool_schemas(), self.settings.think)
        try:
            for chunk in stream:
                if self.cancel is not None and self.cancel.is_set():
                    raise KeyboardInterrupt
                msg = chunk.get("message", {})
                if watch is not None:
                    self.after_load(watch)
                    watch = None
                if msg.get("thinking"):
                    thinking += msg["thinking"]
                    if self.on_delta:
                        self.on_delta("thinking", msg["thinking"])
                    progress.update("Thinking", msg["thinking"])
                    if self.settings.show_thinking:
                        stop_spinner()  # raw text is printed as it streams
                        self.console.print(Text(msg["thinking"], style="dim italic"), end="")
                        printed_thinking = True
                    if self.settings.repeat_limit and circles.feed(msg["thinking"]):
                        self.reply_trouble = "circles"
                        break  # closing the stream below stops the model
                if msg.get("content"):
                    if printed_thinking:
                        self.console.print("\n")
                        printed_thinking = False
                    content += msg["content"]
                    if self.on_delta:
                        self.on_delta("text", msg["content"])
                    progress.update("Writing", msg["content"])
                    # Finished markdown blocks print above the spinner, which keeps running meanwhile.
                    start_spinner()
                    md.feed(msg["content"])
                if msg.get("tool_calls"):
                    tool_calls.extend(msg["tool_calls"])
                if chunk.get("done"):
                    final = chunk
            if self.cancel is not None and self.cancel.is_set():
                raise KeyboardInterrupt  # cancelled while the answer streamed: its end never came
        except KeyboardInterrupt:
            stop_spinner()
            md.flush()
            self.console.print("[yellow]⏹ Interrupted[/]")
            self.messages.append({"role": "assistant", "content": content + "\n[interrupted by user]"})
            raise
        finally:
            stream.close()
            stop_spinner()
            if watch is not None:
                watch.stop()
        if final.get("done_reason") == "length" and not self.reply_trouble:
            self.reply_trouble = "cut_off"  # it filled the context (or hit the output limit) mid-reply
        if printed_thinking:
            self.console.print()
        md.flush()
        if not tool_calls and ("<tool_call>" in content or '"name"' in content):
            tool_calls = parse_text_tool_calls(content)
        message: dict = {"role": "assistant", "content": content}
        if thinking:
            message["thinking"] = thinking
        if tool_calls:
            message["tool_calls"] = tool_calls
        self.messages.append(message)
        if final:
            self.usage["requests"] += 1
            self.usage["prompt_tokens"] += final.get("prompt_eval_count", 0)
            self.usage["prompt_ns"] += final.get("prompt_eval_duration", 0)
            self.usage["output_tokens"] += final.get("eval_count", 0)
            self.usage["output_ns"] += final.get("eval_duration", 0)
            self.ctx_used = final.get("prompt_eval_count", 0) + final.get("eval_count", 0)
            self.sent_chars = self.counted_chars = conversation_chars(self.messages)
            seconds = final.get("eval_duration", 0) / 1e9
            self.last_speed = final.get("eval_count", 0) / seconds if seconds else 0.0
        return message
