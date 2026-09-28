"""Terminal rendering helpers."""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown


class MarkdownStreamer:
    """Render streamed markdown block by block, at blank lines outside code fences.

    Rendering whole blocks (instead of re-rendering a live region) keeps scrollback clean and
    works in every terminal.
    """

    def __init__(self, console: Console):
        self.console = console
        self.buf = ""

    def feed(self, text: str) -> None:
        self.buf += text
        cut, offset, in_fence = 0, 0, False
        for line in self.buf.split("\n")[:-1]:  # the last line may still be incomplete
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                in_fence = not in_fence
                if not in_fence:
                    cut = offset + len(line) + 1
            elif not in_fence and stripped == "":
                cut = offset + len(line) + 1
            offset += len(line) + 1
        if cut:
            self._render(self.buf[:cut])
            self.buf = self.buf[cut:]

    def flush(self) -> None:
        self._render(self.buf)
        self.buf = ""

    def _render(self, text: str) -> None:
        if text.strip():
            self.console.print(Markdown(text.strip("\n"), code_theme="monokai"))
            self.console.print()
