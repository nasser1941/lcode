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
import os
import re
import time
from collections import deque
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
GONE = (  # a removed file read: firm, because models answer from the stub instead of reading it again
    "earlier; its content was removed to save context and isn't in this conversation any more. Read it again "
    "before answering anything about it.]"
)
KEEP_TOOLS = {"agent", "skill", "memory", "todo_write", "present_plan"}  # short, or still guiding the work
ERROR_LINE = re.compile(
    r"error|exception|traceback|failed|failure|fatal|panic|assert|denied|not found|cannot|can't|undefined", re.I
)
MAX_ERROR_LINES = 40
OUTPUT_DAYS = 7  # saved full outputs are deleted after this
MAX_SAVED = 20_000_000  # characters of one saved output: a runaway command doesn't fill the disk
MAX_SAVED_TOTAL = 200_000_000  # bytes of saved outputs in all, the oldest go first


# ----------------------------------------------------------------------------- the user's requests

REQUESTS_HEADER = "[The user's requests in this conversation, word for word, oldest first]"
MAX_REQUESTS = 8  # the most recent ones are kept through a summary
MAX_REQUEST_CHARS = 1500
# What lcode appends to the user's words: attached files, images and folders, and its own notes.
ATTACHED = re.compile(
    r"\n\n(?:<file path=|<image path=|<directory path=|\[lcode[:\]] |\[The user wants the )"
)  # also the sheet


def request_text(content: str) -> str:
    """The user's own words in a message, without what lcode attached to it."""
    cut = ATTACHED.search(content)
    return (content[: cut.start()] if cut else content).strip()


def user_requests(messages: list[dict]) -> list[str]:
    """What the user asked in the conversation, oldest first, including the requests an earlier summary kept."""
    requests: list[str] = []
    for message in messages:
        content = message.get("content") or ""
        if message.get("role") != "user":
            continue
        if content.startswith(SUMMARY_PREFIX):
            start = content.find(REQUESTS_HEADER)
            if start >= 0:
                rest = content[start + len(REQUESTS_HEADER) :]
                try:
                    kept, _ = json.JSONDecoder().raw_decode(rest[rest.index("[") :])
                    requests += [str(r) for r in kept]
                except ValueError:
                    pass
            continue
        if content.startswith("[lcode]"):
            continue  # lcode's own nudges
        text = request_text(content)
        if text:
            requests.append(text)
    return requests


def requests_block(requests: list[str]) -> str:
    """The user's latest requests, word for word, to keep the goal through a summary."""
    if not requests:
        return ""
    kept = [r if len(r) <= MAX_REQUEST_CHARS else f"{r[: MAX_REQUEST_CHARS - 200]} […] {r[-150:]}" for r in requests]
    older = len(kept) - MAX_REQUESTS
    note = f" (and {older} earlier, in the summary)" if older > 0 else ""
    return f"\n\n{REQUESTS_HEADER}{note}\n{json.dumps(kept[-MAX_REQUESTS:], ensure_ascii=False, indent=1)}"


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
        return f"{PRUNED}read {args.get('path', 'a file')}{span} {GONE}"
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


def prune(messages: list[dict], keep: int = KEEP_RECENT_REQUESTS, removed: list[str] | None = None) -> int:
    """Replace stale tool results with stubs, in place. Returns the number of characters saved; the files and
    pages whose only copy went are added to `removed`."""
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
            new = new.replace(
                GONE, "earlier (read again later); removed to save context. Read it again if you need it.]"
            )
        elif removed is not None and name in ("read_file", "web_fetch"):
            what = str(args.get("path") or args.get("url") or "")
            if what and what not in removed:
                removed.append(what)
        messages[i]["content"] = new
        saved += len(content) - len(new)
    return saved


SHEET_HEADER = "[lcode] Task sheet: the plan, notes and changes so far, kept when old messages are removed"
MAX_SHEET = 3000  # characters


def task_sheet(agent: Agent) -> str:
    """The state of the work, from lcode's own records: the todo list and the model's notes (todo_write), the
    files changed in the session and the last check. Pinned where old messages go: in summaries and next to the
    request when tool output is removed."""
    tools = agent.tools
    parts = []
    if tools.todos:
        marks = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}
        plan = "\n".join(f"{marks.get(t.get('status', ''), '[ ]')} {t.get('content', '')}" for t in tools.todos)
        parts.append(f"Plan:\n{plan}")
    if tools.notes:
        parts.append("Notes:\n" + "\n".join(f"- {note}" for note in tools.notes))
    if tools.changed:
        files = list(tools.changed)
        more = f" and {len(files) - 30} more" if len(files) > 30 else ""
        parts.append("Files changed in this session: " + ", ".join(files[-30:]) + more)
    if agent.check_results:
        last = agent.check_results[-1]
        code = last.get("exit_code")
        state = "passed" if code == 0 else "didn't run" if code is None else f"failed (exit code {code})"
        parts.append(f"Last check: `{last.get('command')}` {state}")
    if not parts:
        return ""
    sheet = "\n\n".join(parts)
    if len(sheet) > MAX_SHEET:
        sheet = sheet[: MAX_SHEET - 20] + "\n[…cut]"
    return f"{SHEET_HEADER}\n{sheet}"


def pin(messages: list[dict], text: str) -> None:
    """Put a note next to the request the model is working on, replacing an earlier sheet there."""
    if not text:
        return
    for message in reversed(messages):
        content = str(message.get("content", ""))
        if message.get("role") == "user" and not content.startswith(("[lcode]", SUMMARY_PREFIX)):
            content = content.split("\n\n" + SHEET_HEADER, 1)[0]
            message["content"] = f"{content}\n\n{text}"
            return


def note_removed(messages: list[dict], removed: list[str]) -> None:
    """Tell the model, next to the request it's working on, which files it no longer has: a stub deep in the
    conversation is easy to overlook, and models then answer from memory instead of reading again."""
    if not removed:
        return
    for message in reversed(messages):
        content = str(message.get("content", ""))
        if message.get("role") == "user" and not content.startswith(("[lcode]", SUMMARY_PREFIX)):
            names = ", ".join(removed[:10]) + (f" and {len(removed) - 10} more" if len(removed) > 10 else "")
            message["content"] = (
                f"{content}\n\n[lcode] To make room, lcode removed what you read earlier from: {names}. It isn't in "
                "this conversation any more: read it again before answering anything about it, don't answer from memory."
            )
            return


# ----------------------------------------------------------------------------- long output


def outputs_dir() -> Path:
    return config.STATE_DIR / "outputs"


def save_output(text: str, label: str) -> Path | None:
    """Keep the full text of a long result where the model can read it; old ones are cleaned up."""
    folder = outputs_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        clean_outputs(folder, room=min(len(text), MAX_SAVED))
        path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^a-z0-9]+', '-', label.lower())[:30]}.txt"
        path.write_text(text if len(text) <= MAX_SAVED else text[:MAX_SAVED] + CUT_SAVED)
        return path
    except OSError:
        return None


CUT_SAVED = "\n[lcode stopped saving here: the output went on, but a saved output is kept to 20 MB]\n"


def clean_outputs(folder: Path, room: int = 0) -> None:
    """Delete saved outputs older than OUTPUT_DAYS, then the oldest until they take MAX_SAVED_TOTAL with `room`."""
    cutoff = time.time() - OUTPUT_DAYS * 86400
    files = []
    for old in folder.glob("*.txt"):
        try:
            info = old.stat()
        except OSError:
            continue
        if info.st_mtime < cutoff:
            old.unlink(missing_ok=True)
        else:
            files.append((info.st_mtime, info.st_size, old))
    total = sum(size for _, size, _ in files) + room
    for _, size, old in sorted(files):
        if total <= MAX_SAVED_TOTAL:
            break
        old.unlink(missing_ok=True)
        total -= size


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


class OutputSpool:
    """Output collected line by line with bounded memory: once it's long, everything goes to a file, and only the
    start, the end and the error-looking lines stay in memory. What `shorten` returns comes out the same."""

    def __init__(self, limit: int, label: str = "output") -> None:
        self.limit, self.label = limit, label
        self.head: list[str] = []  # everything, until the output gets long; then its start
        self.tail: deque[str] = deque()
        self.tail_chars = 0
        self.errors: list[str] = []
        self.chars = 0
        self.file = None
        self.path: Path | None = None
        self.saved = 0  # characters written to the file

    def add(self, line: str) -> None:
        self.chars += len(line)
        if self.file is None:
            self.head.append(line)
            if self.chars > self.limit * 2:
                self._spool()
            return
        if self.saved <= MAX_SAVED:  # past it, only the end and the error lines are kept (in memory)
            try:
                self.file.write(line if self.saved + len(line) <= MAX_SAVED else CUT_SAVED)
            except OSError:
                pass
            self.saved += len(line)
        if ERROR_LINE.search(line) and len(self.errors) < MAX_ERROR_LINES:
            self.errors.append(line.strip()[:300])
        self.tail.append(line)
        self.tail_chars += len(line)
        while self.tail_chars > self.limit // 4 and len(self.tail) > 1:
            self.tail_chars -= len(self.tail.popleft())

    def _spool(self) -> None:
        text = "".join(self.head)
        self.path = save_output(text, self.label)
        self.saved = len(text)
        if self.path is not None:
            try:
                self.file = self.path.open("a")
            except OSError:
                self.file = None
        if self.file is None:  # nowhere to keep it: keep only the start and carry on counting
            self.file = open(os.devnull, "w")  # noqa: SIM115
        head = text[: self.limit * 3 // 5]
        self.head = [head]

    def text(self) -> str:
        if self.file is None:
            return shorten("".join(self.head), self.limit, self.label)
        self.file.close()
        head, tail = "".join(self.head), "".join(self.tail)
        cut = self.chars - len(head) - len(tail)
        note = f"\n\n... [{cut} characters cut"
        if self.errors:
            note += f"; {len(self.errors)} line(s) from them that look like errors:]\n" + "\n".join(self.errors)
            note += "\n[...]\n\n"
        else:
            note += "] ...\n\n"
        whole = f"The full {self.label} ({self.chars} characters)"
        if self.saved > MAX_SAVED:
            whole = f"The first {MAX_SAVED // 1_000_000} MB of the {self.label} ({self.chars} characters in all)"
        where = f"\n[{whole} is in {self.path}: read parts of it with read_file if you need them.]" if self.path else ""
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
