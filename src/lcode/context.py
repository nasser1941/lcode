"""Keeping the context window for what matters, which counts most at 32K.

Most of a long session's context is old tool output: files read many turns ago, command output that
was already acted on. Before lcode summarizes a conversation (which loses detail), it prunes that:
old tool results become one-line stubs that say what was there and how to get it back, and earlier
reads of a file that was read again later go too. Recent requests are left alone.

Long tool output is cut smarter: the head, the tail and the lines that look like errors stay, and the
full output is saved to a file the model can read if it needs more.

`breakdown` shows what uses the context, by category, for /context.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lcode import config
from lcode.sessions import SUMMARY_PREFIX

if TYPE_CHECKING:
    from lcode.agent import Agent

KEEP_RECENT_REQUESTS = 2  # tool results of the last requests stay as they are
MIN_PRUNE_CHARS = 400  # shorter results aren't worth replacing
PRUNED = "[lcode: "  # how stubs start, so they're never pruned twice
KEEP_TOOLS = {"agent", "skill", "memory", "todo_write", "present_plan"}  # short, or still guiding the work
ERROR_LINE = re.compile(
    r"error|exception|traceback|failed|failure|fatal|panic|assert|denied|not found|cannot|can't|undefined", re.I
)
MAX_ERROR_LINES = 40
OUTPUT_DAYS = 7  # saved full outputs are deleted after this


# ----------------------------------------------------------------------------- pruning


def calls_for_results(messages: list[dict]) -> dict[int, tuple[str, dict]]:
    """For each tool message (by index), the name and arguments of the call it answers."""
    from lcode.agent import call_arguments

    found: dict[int, tuple[str, dict]] = {}
    pending: list[tuple[str, dict]] = []
    for i, message in enumerate(messages):
        if message.get("role") == "assistant":
            pending = [
                (c.get("function", {}).get("name", ""), call_arguments(c)) for c in message.get("tool_calls") or []
            ]
        elif message.get("role") == "tool":
            found[i] = pending.pop(0) if pending else (message.get("tool_name", ""), {})
    return found


def stub(name: str, args: dict, content: str) -> str:
    """A one-line stand-in for an old tool result."""
    if name == "read_file":
        span = ""
        if args.get("offset") or args.get("limit"):
            start = int(args.get("offset") or 1)
            span = f" from line {start}"
        return f"{PRUNED}read {args.get('path', 'a file')}{span} earlier; removed to save context. Read it again if you need it.]"
    if name == "bash":
        command = " ".join(str(args.get("command", "")).split())
        command = command if len(command) <= 80 else command[:79] + "…"
        code = re.search(r"\[exit code: (-?\d+)\]\s*$", content)
        status = f", exit code {code.group(1)}" if code else ""
        return f"{PRUNED}ran `{command}` earlier{status}; its output was removed to save context.]"
    if name in ("grep", "glob", "list_dir"):
        what = args.get("pattern") or args.get("path") or ""
        return f"{PRUNED}{name}({what}) results removed to save context; run it again if needed.]"
    if name == "web_fetch":
        return f"{PRUNED}fetched {args.get('url', 'a page')} earlier; its text was removed to save context.]"
    if name == "web_search":
        return f"{PRUNED}search results for {args.get('query', '')!r} removed to save context.]"
    if name == "view_image":
        return f"{PRUNED}the description of {args.get('path', 'an image')} was removed to save context.]"
    return f"{PRUNED}{name} result removed to save context.]"


def boundary(messages: list[dict], keep: int = KEEP_RECENT_REQUESTS) -> int:
    """The index of the oldest of the last `keep` requests from the user (results after it stay)."""
    requests = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "user" and not str(m.get("content", "")).startswith(("[lcode]", SUMMARY_PREFIX))
    ]
    return requests[-keep] if len(requests) >= keep else 0


def prune(messages: list[dict], keep: int = KEEP_RECENT_REQUESTS) -> int:
    """Replace stale tool results with stubs, in place. Returns the number of characters saved."""
    calls = calls_for_results(messages)
    edge = boundary(messages, keep)
    # A read is superseded by a later read of the whole file, or of the same lines: not by a later
    # read of other lines, which would leave the model without the part it read first.
    last_full: dict[str, int] = {}
    last_same: dict[tuple, int] = {}
    for i, (name, args) in calls.items():
        if name == "read_file" and not str(messages[i].get("content", "")).startswith("Error"):
            path = str(args.get("path", ""))
            span = (path, int(args.get("offset") or 1), int(args.get("limit") or 0))
            last_same[span] = i
            if span[1:] == (1, 0):
                last_full[path] = i
    saved = 0
    for i, (name, args) in calls.items():
        content = str(messages[i].get("content", ""))
        if name in KEEP_TOOLS or content.startswith(PRUNED) or len(content) < MIN_PRUNE_CHARS:
            continue
        span = (str(args.get("path", "")), int(args.get("offset") or 1), int(args.get("limit") or 0))
        reread = name == "read_file" and (last_full.get(span[0], i) > i or last_same.get(span, i) > i)
        if i >= edge and not reread:
            continue
        new = stub(name, args, content)
        if reread:
            new = new.replace("earlier; removed", "earlier (read again later); removed")
        messages[i]["content"] = new
        saved += len(content) - len(new)
    return saved


# ----------------------------------------------------------------------------- long output


def outputs_dir() -> Path:
    return config.STATE_DIR / "outputs"


def save_output(text: str, label: str) -> Path | None:
    """Keep the full text of a long result where the model can read it; old ones are cleaned up."""
    folder = outputs_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - OUTPUT_DAYS * 86400
        for old in folder.glob("*.txt"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^a-z0-9]+', '-', label.lower())[:30]}.txt"
        path.write_text(text)
        return path
    except OSError:
        return None


def shorten(text: str, limit: int, label: str = "output") -> str:
    """Head, tail and the error-looking lines in between; the full text goes to a file."""
    if len(text) <= limit:
        return text
    head, tail = text[: limit * 3 // 5], text[-limit // 4 :]
    middle = text[len(head) : len(text) - len(tail)]
    errors, budget = [], limit // 6
    for line in middle.splitlines():
        if ERROR_LINE.search(line) and len(errors) < MAX_ERROR_LINES:
            line = line.strip()[:300]
            if budget - len(line) < 0:
                break
            errors.append(line)
            budget -= len(line) + 1
    cut = len(middle) - sum(len(e) + 1 for e in errors)
    note = f"\n\n... [{cut} characters cut"
    if errors:
        note += f"; {len(errors)} line(s) from them that look like errors:]\n" + "\n".join(errors) + "\n[...]\n\n"
    else:
        note += "] ...\n\n"
    saved = save_output(text, label)
    where = (
        f"\n[The full {label} ({len(text)} characters) is in {saved}: read parts of it with read_file if you need them.]"
        if saved
        else ""
    )
    return f"{head}{note}{tail}{where}"


# ----------------------------------------------------------------------------- what uses the context


@dataclass
class Part:
    name: str
    chars: int
    detail: str = ""

    @property
    def tokens(self) -> int:
        return self.chars // 3


def breakdown(agent: Agent) -> list[Part]:
    """What the next request sends, by category (sizes are estimates: ~3 characters per token)."""
    from lcode import extensions

    system = agent.messages[0]["content"] if agent.messages else ""
    pieces = {
        "AGENTS.md": len(next((system[system.find(h) :] for h in ["\n# Project instructions"] if h in system), "")),
        "memory notes": len(agent.memory().prompt(agent.settings.context)) if agent.settings.memory != "off" else 0,
        "skills": len(extensions.skills_prompt(agent.extensions().skills, agent.settings.context)),
        "MCP servers": len(agent._mcp_prompt),
    }
    if pieces["AGENTS.md"]:  # it runs to the end of the prompt, which may include memory and MCP after it
        pieces["AGENTS.md"] = max(0, pieces["AGENTS.md"] - pieces["memory notes"] - pieces["MCP servers"])
    parts = [Part("system prompt", len(system) - sum(pieces.values()), "instructions, environment, repository layout")]
    parts += [Part(name, size) for name, size in pieces.items() if size]
    schemas = agent.tool_schemas()
    mcp = [s for s in schemas if agent.mcp and agent.mcp.owns(s["function"]["name"])]
    parts.append(
        Part(
            "tool definitions",
            len(json.dumps([s for s in schemas if s not in mcp])),
            f"{len(schemas) - len(mcp)} tools",
        )
    )
    if mcp:
        parts.append(Part("MCP tool definitions", len(json.dumps(mcp)), f"{len(mcp)} tools"))
    yours = files = replies = 0
    results: dict[str, int] = {}
    calls = calls_for_results(agent.messages)
    for i, m in enumerate(agent.messages[1:], 1):
        content = str(m.get("content") or "")
        if m.get("role") == "user":
            attached = sum(
                len(block)
                for block in re.findall(r"<(?:file|directory|image) path=.*?</(?:file|directory|image)>", content, re.S)
            )
            files += attached
            yours += len(content) - attached
        elif m.get("role") == "assistant":
            replies += len(content) + len(json.dumps(m.get("tool_calls") or []))
        elif m.get("role") == "tool":
            name = calls.get(i, (m.get("tool_name", "?"), {}))[0]
            name = "MCP tools" if name.startswith("mcp") else name
            results[name] = results.get(name, 0) + len(content)
    parts.append(Part("your messages", yours))
    if files:
        parts.append(Part("attached files", files))
    parts.append(Part("model's replies", replies, "answers and tool calls"))
    for name, size in sorted(results.items(), key=lambda kv: -kv[1]):
        parts.append(Part(f"tool results: {name}", size))
    return parts
