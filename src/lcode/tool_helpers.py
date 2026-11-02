"""What the tools share: their error, output limits, and reading and writing text files as they were
(encoding, line endings)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


NO_NETWORK = re.compile(
    r"Network is unreachable|Temporary failure in name resolution|Could not resolve host|getaddrinfo|ENOTFOUND|"
    r"EAI_AGAIN|Name or service not known|network is unreachable|No route to host"
)


MAX_TOOL_OUTPUT = 30_000  # characters returned to the model per tool call


PENDING_LINES = 5000  # output lines waiting to be read, at most


LEFTOVER_GRACE = 0.5  # seconds to wait for output after a command ends, before stopping what it left running


IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".idea", ".vscode", "dist", "build", ".next", "target", ".cache", ".gradle",
    "site-packages", ".eggs", ".DS_Store",
}  # fmt: skip


def truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit * 2 // 3], text[-limit // 3 :]
    return f"{head}\n\n... [{len(text) - len(head) - len(tail)} characters truncated] ...\n\n{tail}"


@dataclass
class TextFormat:
    """How a file stores its text, so an edit writes it back the same way."""

    encoding: str = "utf-8"
    crlf: bool = False  # Windows line endings
    bom: bool = False  # a UTF-8 byte order mark


def decode(raw: bytes) -> tuple[str, TextFormat]:
    """A file's text with \n line endings, and its format: UTF-8, else Windows-1252 or Latin-1 (which always
    decode), never with characters replaced. Writing a replaced character back used to corrupt the whole file."""
    fmt = TextFormat(bom=raw.startswith(b"\xef\xbb\xbf"))
    text = ""
    for encoding in ("utf-8-sig" if fmt.bom else "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            fmt.encoding = "utf-8" if encoding == "utf-8-sig" else encoding
            break
        except UnicodeDecodeError:
            continue
    crlf = text.count("\r\n")
    if crlf and crlf == text.count("\n"):  # only CRLF: mixed files are left as they are
        fmt.crlf = True
        text = text.replace("\r\n", "\n")
    return text, fmt


def encode(text: str, fmt: TextFormat) -> bytes:
    if fmt.crlf:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    try:
        data = text.encode(fmt.encoding)
    except UnicodeEncodeError as e:
        bad = text[e.start : e.end]
        raise ToolError(
            f"the file is in {fmt.encoding}, which can't hold {bad!r}: write it without that character, or ask the "
            "user to convert the file to UTF-8"
        ) from e
    return (b"\xef\xbb\xbf" if fmt.bom else b"") + data


def is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\0" in f.read(8192)
    except OSError:
        return False


def walk_files(base: Path):
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS and not d.endswith(".egg-info"))
        for name in sorted(files):
            if name not in IGNORE_DIRS:
                yield Path(root) / name


def tree(base: Path, depth: int = 2, limit: int = 400) -> str:
    lines, count = [f"{base}/"], 0

    def walk(directory: Path, prefix: str, level: int) -> None:
        nonlocal count
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        entries = [e for e in entries if e.name not in IGNORE_DIRS]
        for i, entry in enumerate(entries):
            if count >= limit:
                lines.append(f"{prefix}... (truncated)")
                return
            count += 1
            last = i == len(entries) - 1
            lines.append(f"{prefix}{'└── ' if last else '├── '}{entry.name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir() and level < depth:
                walk(entry, prefix + ("    " if last else "│   "), level + 1)

    walk(base, "", 1)
    return "\n".join(lines)


class ToolError(Exception):
    pass
