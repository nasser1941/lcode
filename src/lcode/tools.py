"""The tools the model can call: file reading/editing, search, shell and a todo list."""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import inspect
import itertools
import json
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from lcode import matching, screenshots, secretscan, web
from lcode.context import OutputSpool, shorten
from lcode.jobs import JobError, describe, stop_leftovers
from lcode.permissions import bash_key, is_read_only
from lcode.planning import BLOCKED, PLAN_MODE_TOOLS
from lcode.sandbox import SandboxError
from lcode.vision import is_image

if TYPE_CHECKING:
    from lcode.agent import Agent

NO_NETWORK = re.compile(
    r"Network is unreachable|Temporary failure in name resolution|Could not resolve host|getaddrinfo|ENOTFOUND|"
    r"EAI_AGAIN|Name or service not known|network is unreachable|No route to host"
)
MAX_TOOL_OUTPUT = 30_000  # characters returned to the model per tool call
PENDING_LINES = 5000  # output lines waiting to be read, at most
MAX_NOTES, MAX_NOTE = 12, 300  # the model's task notes: how many, how long each
LEFTOVER_GRACE = 0.5  # seconds to wait for output after a command ends, before stopping what it left running
IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".idea", ".vscode", "dist", "build", ".next", "target", ".cache", ".gradle",
    "site-packages", ".eggs", ".DS_Store",
}  # fmt: skip


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


SCHEMAS = [
    _fn(
        "read_file",
        "Read a text file. Returns lines prefixed with line numbers (cat -n style). Reads up to `limit` lines "
        "starting at `offset` (1-based). Read a file before editing it.",
        {
            "path": {"type": "string", "description": "File path, relative to the working directory or absolute"},
            "offset": {"type": "integer", "description": "1-based line to start from (default 1)"},
            "limit": {"type": "integer", "description": "Maximum lines to read (default 2000)"},
        },
        ["path"],
    ),
    _fn(
        "write_file",
        "Create a new file or completely overwrite an existing one with `content`. Parent directories are "
        "created. For small changes to existing files prefer edit_file.",
        {"path": {"type": "string"}, "content": {"type": "string", "description": "Full file content"}},
        ["path", "content"],
    ),
    _fn(
        "edit_file",
        "Replace `old_string` with `new_string` in a file. `old_string` must match the file exactly (including "
        "indentation) and be unique unless replace_all is true: include enough surrounding lines to make it "
        "unique. Do NOT include the line-number prefixes from read_file output.",
        {
            "path": {"type": "string"},
            "old_string": {"type": "string", "description": "Exact text to replace"},
            "new_string": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
        },
        ["path", "old_string", "new_string"],
    ),
    _fn(
        "apply_patch",
        "Apply a unified diff to one or more files, as `git diff` writes it: `--- a/path` and `+++ b/path` "
        "headers, then `@@` hunks of context lines (starting with a space), removed lines (-) and added lines (+). "
        "Hunks are found by their text, so the @@ line numbers may be off; include 2-3 context lines around each "
        "change. Use `--- /dev/null` for a new file. For a single change, edit_file is simpler.",
        {"patch": {"type": "string", "description": "The unified diff"}},
        ["patch"],
    ),
    _fn(
        "list_dir",
        "Show a directory tree (skips .git, node_modules, virtualenvs and caches).",
        {
            "path": {"type": "string", "description": "Directory (default: working directory)"},
            "depth": {"type": "integer", "description": "Maximum depth (default 2)"},
        },
        [],
    ),
    _fn(
        "glob",
        "Find files by glob pattern, e.g. '**/*.py' or 'src/**/test_*.ts'. Newest first.",
        {"pattern": {"type": "string"}, "path": {"type": "string", "description": "Base directory"}},
        ["pattern"],
    ),
    _fn(
        "grep",
        "Search file contents with a regular expression. Returns path:line:text matches.",
        {
            "pattern": {"type": "string", "description": "Regular expression"},
            "path": {"type": "string", "description": "File or directory to search (default: working directory)"},
            "glob": {"type": "string", "description": "Only search files matching this glob, e.g. '*.py'"},
            "ignore_case": {"type": "boolean"},
            "context": {"type": "integer", "description": "Lines of context around each match"},
        },
        ["pattern"],
    ),
    _fn(
        "bash",
        "Run a shell command with bash in the working directory and return its output and exit code. The "
        "working directory persists between calls (cd works). Use it to run scripts and tests, use git, install "
        "packages, etc. Avoid interactive commands. For a process that keeps running (a dev server, a watcher), "
        "set background=true and give the plain command, without & or redirecting its output: lcode keeps it "
        "running and keeps its output, you get a job id back and can carry on.",
        {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "Seconds before the command is killed (default 180)"},
            "background": {
                "type": "boolean",
                "description": "Keep it running in the background; read its output with bash_output and stop it "
                "with bash_stop (not with kill)",
            },
        },
        ["command"],
    ),
    _fn(
        "bash_output",
        "Read what a background command (bash with background=true) printed since you last read it, and whether "
        "it's still running.",
        {"id": {"type": "string", "description": "The job id bash returned"}},
        ["id"],
    ),
    _fn("bash_stop", "Stop a background command.", {"id": {"type": "string"}}, ["id"]),
    _fn(
        "todo_write",
        "Create or update your task list for multi-step work. Pass the full list every time. Keep exactly one "
        "item in_progress while working. In `notes`, keep what you must not forget: decisions and why, findings, "
        "open questions. The list and the notes stay with you even when old messages are removed.",
        {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                    },
                    "required": ["content", "status"],
                },
            },
            "notes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short notes, the full list each time (omit to keep the current ones)",
            },
        },
        ["todos"],
    ),
    _fn(
        "screenshot",
        "See a desktop program or web page as the user would, without a screen: it runs the program (or opens the "
        "page), saves a screenshot in .lcode/screenshots/ and returns what it shows, with the sizes of windows and "
        "widgets (Qt) or of the page, and the browser's console errors. Use it to check GUI and layout work instead "
        "of guessing. Python Qt programs run offscreen; other desktop programs on a virtual display (Linux); web pages "
        "in a headless browser. For a web app, give the command that starts its server and the url (lcode starts and "
        "stops it); if a server is already running, or for an .html file, give only the url. For something that "
        "only shows after a click or a key (a Python Qt program), give `actions`: they run first, then the picture.",
        {
            "command": {"type": "string", "description": "Command that starts the program, or the web server"},
            "url": {"type": "string", "description": "Page to open: a URL, localhost:PORT/path or an .html file"},
            "question": {
                "type": "string",
                "description": "What to check, e.g. 'Are all six panels inside the window?'",
            },
            "screen": {"type": "string", "description": "Screen (or browser window) size, WxH (default 1920x1080)"},
            "wait": {"type": "integer", "description": "Seconds to let it start and draw first (default 3)"},
            "full_page": {"type": "boolean", "description": "Web pages: the whole page, not just the window"},
            "actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Python Qt programs: steps to take before the picture, as a user would: "
                '"click X,Y", "double_click X,Y", "right_click X,Y" (a point in the window, as the sizes show), '
                'or with the widget\'s text: "double_click Camera 1"; "key Escape", "key ctrl+s"; "type some text"; '
                '"wait 2". The result says what each step hit.',
            },
            "mode": {
                "type": "string",
                "enum": ["auto", "qt", "xvfb"],
                "description": "Desktop programs: auto (default), qt (Python Qt, offscreen), xvfb (virtual display)",
            },
        },
        [],
    ),
]
WEB_SEARCH_SCHEMA = _fn(
    "web_search",
    "Search the web for up-to-date information: latest versions and releases, documentation, API changes, "
    "error messages, security advisories. Returns titles, URLs and snippets; read promising results with "
    "web_fetch. Look in the repository first for questions about this codebase.",
    {
        "query": {"type": "string", "description": "Search query"},
        "max_results": {"type": "integer", "description": "Number of results, 1-10 (default 5)"},
    },
    ["query"],
)
WEB_FETCH_SCHEMA = _fn(
    "web_fetch",
    "Download a web page or text file by URL and return its main content as text (HTML is converted).",
    {
        "url": {"type": "string", "description": "http(s) URL"},
        "max_chars": {"type": "integer", "description": "Maximum characters to return (default 20000)"},
    },
    ["url"],
)
VIEW_IMAGE_SCHEMA = _fn(
    "view_image",
    "Look at an image file (screenshot, diagram, photo, mockup): returns a detailed description with all visible "
    "text transcribed. Ask a specific question to focus it.",
    {
        "path": {"type": "string", "description": "Image file: png, jpg, webp or gif"},
        "question": {"type": "string", "description": "What you need to know from the image (optional)"},
    },
    ["path"],
)
MEMORY_SCHEMA = _fn(
    "memory",
    "Your memory across sessions. save: keep a fact that a future session needs and that isn't in the code, git "
    "history or AGENTS.md (one fact per note; saving under an existing name updates that note). delete: remove a "
    "note that turned out wrong. read: a note's details, or the list of all notes when no name is given.",
    {
        "action": {"type": "string", "enum": ["save", "read", "delete"]},
        "name": {"type": "string", "description": "Short kebab-case name, e.g. use-pnpm"},
        "type": {
            "type": "string",
            "enum": ["feedback", "project", "reference", "user"],
            "description": "save: feedback = the user's corrections and preferences; project = decisions, "
            "constraints, ongoing work; reference = where things live outside the repository; user = who the "
            "user is",
        },
        "description": {"type": "string", "description": "save: the fact, in one line"},
        "details": {"type": "string", "description": "save (optional): why, and how to apply it"},
        "scope": {
            "type": "string",
            "enum": ["project", "user"],
            "description": "save: project (default) = this repository only; user = every repository",
        },
    },
    ["action"],
)
REPO_MAP_SCHEMA = _fn(
    "repo_map",
    "A map of the repository: the important files with their classes and functions, signatures and line numbers, "
    "ranked by how much of the code uses them. Use it to find your way around before reading files.",
    {"path": {"type": "string", "description": "Only this folder (to see more of it)"}},
    [],
)
SEARCH_CODE_SCHEMA = _fn(
    "search_code",
    "Find code by what it does, not by its exact words: e.g. 'where are failed uploads retried'. Returns the "
    "closest matches with file:line. Use grep for exact names.",
    {
        "query": {"type": "string", "description": "What you're looking for, in words"},
        "path": {"type": "string", "description": "Only in this folder (optional)"},
    },
    ["query"],
)
LSP_SCHEMA = _fn(
    "lsp",
    "Ask the language server about code: where a symbol is defined, where it's used, its type or signature, or "
    "the symbols in a file (or in the whole workspace, by name). More precise than grep, and cheaper on context.",
    {
        "action": {"type": "string", "enum": ["definition", "references", "hover", "symbols"]},
        "path": {"type": "string", "description": "The file (for symbols: leave it out to search the workspace)"},
        "line": {"type": "integer", "description": "The line (1-based) where the symbol appears"},
        "symbol": {"type": "string", "description": "The symbol's name on that line"},
        "query": {"type": "string", "description": "symbols without a path: the name to search for"},
    },
    ["action"],
)
PRESENT_PLAN_SCHEMA = _fn(
    "present_plan",
    "Plan mode: show the user your plan and ask for approval. Once approved, plan mode ends and you carry it out.",
    {
        "title": {"type": "string", "description": "A short name for the plan, e.g. 'Add retries to webhooks'"},
        "plan": {
            "type": "string",
            "description": "Markdown: numbered steps, the files you'll change and how, the risks, and how you'll "
            "verify the result",
        },
    },
    ["title", "plan"],
)
LCODE_HELP_SCHEMA = _fn(
    "lcode_help",
    "Look things up in the manual of lcode, the program you run in: its slash commands, its modes (code, image and "
    "video), settings, models and options. When the user asks how to do something in lcode, use it and tell them "
    "exactly what to type; don't answer such questions from memory.",
    {"question": {"type": "string", "description": "What the user wants to do in lcode, in words"}},
    ["question"],
)
OPTIONAL_SCHEMAS = [
    LCODE_HELP_SCHEMA,
    WEB_SEARCH_SCHEMA,
    WEB_FETCH_SCHEMA,
    VIEW_IMAGE_SCHEMA,
    MEMORY_SCHEMA,
    PRESENT_PLAN_SCHEMA,
    LSP_SCHEMA,
    REPO_MAP_SCHEMA,
    SEARCH_CODE_SCHEMA,
]
TOOL_NAMES = {s["function"]["name"] for s in [*SCHEMAS, *OPTIONAL_SCHEMAS]} | {
    "agent",
    "skill",
}  # those depend on the session


# ----------------------------------------------------------------------------- helpers


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


def parse_text_tool_calls(content: str) -> list[dict]:
    """Recover tool calls that a model printed as text instead of emitting structured calls."""
    calls = []
    for block in re.findall(r"<tool_call>(.*?)</tool_call>", content, re.S):
        block = block.strip()
        fn = re.search(r"<function=([\w.-]+)>(.*?)</function>", block, re.S)
        if fn:
            params = re.findall(r"<parameter=([\w.-]+)>(.*?)</parameter>", fn.group(2), re.S)
            calls.append({"function": {"name": fn.group(1), "arguments": {k: v.strip("\n") for k, v in params}}})
            continue
        try:
            obj = json.loads(block)
            calls.append({"function": {"name": obj["name"], "arguments": obj.get("arguments", {})}})
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    if calls:
        return calls
    # A message that is nothing but JSON calls such as {"name": "grep", "arguments": {...}}, possibly in a ```
    # fence. JSON inside an answer is an example, not a call: running it once ran a command the model was only
    # explaining.
    body = content.strip()
    lead = re.match(r"([^\n{`]{0,50})\n+(?=```|\{)", body)  # a short lead-in such as "Sure:" is fine
    if lead:
        body = body[lead.end() :]
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body)
    decoder = json.JSONDecoder()
    position = 0
    while position < len(body):
        while position < len(body) and body[position].isspace():
            position += 1
        if position == len(body):
            break
        try:
            obj, position = decoder.raw_decode(body, position)
        except json.JSONDecodeError:
            return []  # something else in the message: not a call
        if not (isinstance(obj, dict) and obj.get("name") in TOOL_NAMES and isinstance(obj.get("arguments", {}), dict)):
            return []
        calls.append({"function": {"name": obj["name"], "arguments": obj.get("arguments", {})}})
    return calls


class ToolError(Exception):
    pass


# ----------------------------------------------------------------------------- toolbox


class Toolbox:
    def __init__(self, agent: Agent):
        self.agent = agent
        self.read_mtimes: dict[str, float] = {}
        self.formats: dict[str, TextFormat] = {}  # encoding and line endings of the files read
        self.notes: list[str] = []  # the model's notes on the task (todo_write): decisions, findings, questions
        self.changed: dict[str, None] = {}  # files lcode wrote in this session, in order
        self.before: dict[str, str | None] = {}  # files changed in this request: their text before (None: new)
        self.todos: list[dict] = []
        # An editor's buffers (lcode acp): read unsaved changes and write through the editor.
        self.reader: Callable[[Path], str | None] | None = None
        self.writer: Callable[[Path, str], None] | None = None

    @property
    def console(self):
        return self.agent.console

    def resolve(self, path: str | None) -> Path:
        p = Path(os.path.expanduser(path or "."))
        return (p if p.is_absolute() else self.agent.cwd / p).resolve()

    def path(self, path: str | None, read: bool = False) -> Path:
        """Resolve a path from the model; with the sandbox on, it must be inside the project (or, for
        reading, inside a skill's folder)."""
        p = self.resolve(path)
        root = self.agent.sandbox_root()
        if read and root is not None:
            skills = self.agent.extensions().skills.values()
            if any(p == s.folder or s.folder in p.parents for s in skills):
                return p
        if read and root is not None:
            from lcode.context import outputs_dir

            if outputs_dir().resolve() in p.parents:  # full output that was cut short
                return p
        if root is not None and p != root and root not in p.parents:
            raise ToolError(f"{path} is outside the project ({root}); with the sandbox on, that's all you can use")
        return p

    def project_rel(self, p: Path) -> str:
        """A path relative to the repository's root, as permission rules and hooks see it."""
        from lcode.checkpoints import work_tree_for

        try:
            return p.resolve().relative_to(work_tree_for(self.agent.cwd)).as_posix()
        except ValueError:
            return str(p)

    def rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.agent.cwd)) or "."
        except ValueError:
            return str(p)

    def run(self, name: str, args: dict) -> str:
        hooks = self.agent.hooks
        if not hooks.hooks:
            return self._run(name, args)
        path = str(self.resolve(args["path"])) if isinstance(args.get("path"), str) and args.get("path") else ""
        payload = {"tool": name, "arguments": args, "path": path, "cwd": str(self.agent.cwd)}
        for outcome in hooks.run("before_tool", payload, self.agent.cwd, name, path):
            if outcome.code == 2:  # the hook blocks the call
                self.console.print(Text(f"  ⎿ blocked by a hook: {outcome.output[:200]}", style="yellow"))
                return f"Error: a hook blocked this {name} call: {outcome.output or 'no reason given'}"
            if outcome.code != 0:
                self.console.print(
                    Text(f"  ⎿ a before_tool hook failed ({outcome.code}): {outcome.output[:200]}", style="yellow")
                )
        result = self._run(name, args)
        notes = []
        for outcome in hooks.run("after_tool", {**payload, "result": result[:4000]}, self.agent.cwd, name, path):
            if outcome.code != 0 or outcome.hook.feedback:
                status = "" if outcome.code == 0 else f", exit code {outcome.code}"
                notes.append(
                    f"[A hook ran after this ({outcome.hook.command[:60]}{status}):\n{outcome.output or '(no output)'}]"
                )
            if outcome.code != 0:
                self.console.print(Text(f"  ⎿ hook exit code {outcome.code}: {outcome.output[:200]}", style="yellow"))
        return result + "".join("\n" + n for n in notes)

    def _run(self, name: str, args: dict) -> str:
        allowed = self.agent.allowed_tools
        if allowed is not None and name not in allowed:
            return f"Error: {name} isn't available to you. Available: {', '.join(sorted(allowed))}"
        if self.agent.planning() and name not in PLAN_MODE_TOOLS:
            return f"Error: {BLOCKED}"
        if self.agent.no_changes and name not in PLAN_MODE_TOOLS:
            return f"Error: {self.agent.no_changes}"
        if name == "present_plan" and not self.agent.planning():
            return "Error: plan mode is off, so there's nothing to present: carry on with the work."
        if self.agent.mcp and self.agent.mcp.owns(name):
            return self._mcp(name, args)
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return f"Error: unknown tool '{name}'. Available: {', '.join(sorted(TOOL_NAMES))}"
        problem = self._argument_problem(fn, args)
        if problem:
            return f"Error: bad arguments for {name}: {problem}"
        try:
            return fn(**self._typed(fn, args))
        except ToolError as e:
            return f"Error: {e}"
        except TypeError as e:
            return f"Error: bad arguments for {name}: {e}"
        except Exception as e:  # surface every failure to the model instead of crashing the session
            return f"Error: {type(e).__name__}: {e}"

    @staticmethod
    def _typed(fn, args: dict) -> dict:
        """Arguments with text turned into the declared type: some servers and the text fallback send every value
        as a string, and "false" is true in Python."""
        params = inspect.signature(fn).parameters
        typed = dict(args)
        for name, value in args.items():
            if not isinstance(value, str) or name not in params:
                continue
            kind = str(params[name].annotation).split("|")[0].strip()
            text = value.strip().lower()
            if kind == "bool" and text in ("true", "false", "1", "0", "yes", "no"):
                typed[name] = text in ("true", "1", "yes")
            elif kind == "int" and re.fullmatch(r"-?\d+", text):
                typed[name] = int(text)
        return typed

    @staticmethod
    def _argument_problem(fn, args: dict) -> str:
        """Explain unknown or missing arguments in terms the model can act on."""
        params = inspect.signature(fn).parameters
        unknown = [a for a in args if a not in params]
        missing = [p for p, spec in params.items() if spec.default is inspect.Parameter.empty and p not in args]
        if not unknown and not missing:
            return ""
        parts = []
        if unknown:
            parts.append(f"unknown argument(s) {', '.join(map(repr, unknown))}")
        if missing:
            parts.append(f"missing required argument(s) {', '.join(map(repr, missing))}")
        valid = ", ".join(p if params[p].default is inspect.Parameter.empty else f"{p} (optional)" for p in params)
        return f"{'; '.join(parts)}. Valid arguments: {valid}."

    # -- subagents
    def t_agent(self, type: str, task: str, description: str = "") -> str:
        if not self.agent.settings.subagents:
            raise ToolError("subagents are turned off")
        from lcode import subagents

        kind = self.agent.agent_types().get(type)
        if self.agent.planning() and kind and not kind.read_only:
            raise ToolError(f"plan mode is on: only read-only agents can run now ({BLOCKED})")
        if self.agent.no_changes and kind and not kind.read_only:
            raise ToolError(f"only read-only agents can run now: {self.agent.no_changes}")
        return subagents.run_one(self.agent, {"type": type, "task": task, "description": description})

    # -- plan mode
    def t_present_plan(self, title: str, plan: str) -> str:
        from lcode.planning import present

        return present(self.agent, title, plan)

    # -- finding one's way around
    def t_repo_map(self, path: str = "") -> str:
        repo = self.agent.repo_map()
        if repo is None:
            raise ToolError("the repository map is turned off")
        if path:
            self.path(path, read=True)  # with the sandbox on, only the project
        return repo.for_tool(self.rel(self.resolve(path)) if path else "")

    def t_search_code(self, query: str, path: str = "") -> str:
        from lcode import codesearch

        index = self.agent.code_index()
        if index is None:
            raise ToolError("semantic code search isn't set up for this repository: use grep")
        note = ""
        try:
            index.update(self.agent.ollama, limit=codesearch.SESSION_UPDATE_LIMIT)
        except codesearch.SearchError as e:
            note = f"\n[The index is out of date ({e}); run `lcode index` to refresh it.]"
        try:
            hits = index.search(self.agent.ollama, query, folder=self.rel(self.resolve(path)) if path else "")
        except codesearch.SearchError as e:
            raise ToolError(str(e)) from e
        return codesearch.format_hits(index.root, hits) + note

    # -- language servers
    def t_lsp(self, action: str, path: str = "", line: int = 0, symbol: str = "", query: str = "") -> str:
        from lcode.lsp import LspError

        if self.agent.lsp is None:
            raise ToolError("no language server is available")
        try:
            return self.agent.lsp.query(
                action, self.path(path, read=True) if path else None, int(line or 0), symbol, query
            )
        except LspError as e:
            raise ToolError(str(e)) from e

    def _check(self, p: Path, before: str | None, after: str) -> str:
        """Errors the language server finds that an edit introduced (lcode.lsp), for the tool result."""
        return self.agent.lsp.check_edit(p, before, after) if self.agent.lsp is not None else ""

    # -- lcode's own manual
    def t_lcode_help(self, question: str) -> str:
        from lcode import __version__, manual

        return manual.lookup(question, self.agent.settings.mode, __version__)

    # -- skills
    def t_skill(self, name: str) -> str:
        from lcode.extensions import activate

        result = activate(self.agent, name)
        if not result.startswith("Error:"):
            self.console.print(Text(f"  ⎿ loaded the {name} skill", style="dim"))
        return result

    # -- memory
    def t_memory(
        self, action: str, name: str = "", type: str = "", description: str = "", details: str = "", scope: str = ""
    ) -> str:
        if self.agent.settings.memory == "off":
            raise ToolError("memory is turned off")
        from lcode.memory import run_tool

        return run_tool(self.agent, action, name, type, description, details, scope)

    # -- images
    def t_screenshot(
        self,
        command: str = "",
        url: str = "",
        question: str = "",
        screen: str = "",
        wait: int = screenshots.DEFAULT_WAIT,
        full_page: bool = False,
        mode: str = "auto",
        actions: list | str | None = None,
    ) -> str:
        command, url = (command or "").strip(), (url or "").strip()
        if not command and not url:
            raise ToolError("give command (the program to start), url (the page), or both (a server and its page)")
        if mode not in ("auto", "qt", "xvfb"):
            raise ToolError("mode must be auto, qt or xvfb")
        try:
            size = screenshots.parse_screen(screen)
            if url:
                url = screenshots.normalize_url(url, self.agent.cwd)
            steps = screenshots.parse_actions(actions)
        except screenshots.ShotError as e:
            raise ToolError(str(e)) from e
        if command:
            if self.agent.sandbox:
                raise ToolError(
                    "with the sandbox on, programs don't run on this machine, so they can't be shown on a screen. Ask "
                    "the user to turn it off (/sandbox off) for this, or take a screenshot of a url only"
                )
            if self.agent.planning() or self.agent.no_changes or self.agent.read_only:
                raise ToolError("a screenshot runs the program, and only read-only commands run now")
            verdict = self.agent.perms.rule("bash", command)
            if verdict is not None and not verdict[0]:
                return verdict[1]
            body = Syntax(command, "bash", theme="monokai", word_wrap=True)
            title = f"Run for a screenshot (in {self.agent.cwd})"
            ok, feedback = self.agent.perms.request(bash_key(command), "bash", title, body, command)
            if not ok:
                return feedback
            self.agent.checkpoint()  # the program may change files, like any command
        if url and not screenshots.is_local(url):
            domain = web.urlparse(url).netloc or url
            ok, feedback = self._web_allowed(f"web:{domain}", "Open a web page for a screenshot", url)
            if not ok:
                return feedback

        def cancelled() -> bool:
            return self.agent.cancel is not None and self.agent.cancel.is_set()

        try:
            with self.agent.activity("Taking the screenshot"):
                shot = screenshots.take(
                    self.agent.cwd,
                    screenshots.shots_dir(self.agent.cwd),
                    command=command or None,
                    url=url or None,
                    screen=size,
                    wait=wait,
                    full_page=full_page,
                    mode=mode,
                    cancel=cancelled,
                    actions=steps,
                )
        except screenshots.ShotError as e:
            raise ToolError(f"no screenshot: {e}") from e
        w, h = shot.screen
        where = "window" if url else "screen"
        self.console.print(Text(f"  ⎿ {self.rel(shot.image)} ({shot.how}, {w}x{h} {where})", style="dim"))
        lines = [f"Screenshot ({shot.how}, {w}x{h} {where}): {self.rel(shot.image)}"]
        if shot.windows:
            lines.append("Each window on its own: " + ", ".join(self.rel(p) for p in shot.windows))
        lines += shot.report
        if shot.output:
            lines.append(f"What the {'server' if url else 'program'} printed:\n{shot.output}")
        if self.agent.vision_model():
            what = f"the page {url}" if url else f"the program `{command}` on a {w}x{h} screen"
            if steps:
                what += ", after these steps: " + "; ".join(s["raw"] for s in steps)
            ask = f"This is a screenshot of {what}." + (f" {question.strip()}" if question.strip() else "")
            try:
                seen = self.agent.look(shot.image, ask)
                lines.append(f"What it shows (described by {self.agent.vision_model()}):\n{seen}")
            except ToolError as e:
                lines.append(f"(Nobody could look at the picture: {e}. The sizes above are measured.)")
        else:
            lines.append(
                "(No model that can see images is installed, so only the measurements above are known. "
                "For descriptions too: ollama pull qwen3-vl:8b, then lcode config set vision_model qwen3-vl:8b)"
            )
        return "\n".join(lines)

    def t_view_image(self, path: str, question: str = "") -> str:
        p = self.path(path)
        return f"[{self.rel(p)}, as described by {self.agent.vision_model()}]\n" + self.agent.look(p, question)

    # -- MCP servers
    def _mcp(self, name: str, args: dict) -> str:
        mcp = self.agent.mcp
        assert mcp is not None
        if name == "mcp_find_tools":
            result = mcp.find(str(args.get("query", "")))
            found = sum(1 for line in result.splitlines() if line.startswith("mcp__"))
            self.console.print(Text(f"  ⎿ {found} tool(s) found", style="dim"))
            return result
        try:
            state, tool, arguments = mcp.resolve(name, args)
        except Exception as e:
            return f"Error: {e}"
        if not mcp.allowed(state, tool):
            body = Syntax(json.dumps(arguments, indent=2, ensure_ascii=False), "json", theme="monokai", word_wrap=True)
            title = f"Use {state.name} › {tool['name']}"
            key = f"mcp:{state.name}:{tool['name']}"
            ok, feedback = self.agent.perms.request(key, "mcp", title, body, f"{state.name}:{tool['name']}")
            if not ok:
                return feedback
        if mcp.needs_gpu(state, tool):
            # The tool must finish before lcode's model comes back: a job left running in the
            # background would compete with it for the GPU.
            properties = (tool.get("inputSchema") or {}).get("properties") or {}
            if (properties.get("wait") or {}).get("type") == "boolean" and arguments.get("wait") is not True:
                arguments = {**arguments, "wait": True}
            freed = self.agent.free_gpu()
            if freed:
                self.agent.made_room_for = state.name
                self.agent.say(f"Unloaded {', '.join(freed)} so {state.name} can use the GPU; it reloads afterwards.")
        result = mcp.call(state, tool, arguments, image_text=self.agent.describe_image_data)
        if result.startswith("Error:"):
            return result
        self.console.print(Text(f"  ⎿ {result.count(chr(10)) + 1} line(s) from {state.name}", style="dim"))
        return shorten(result, MAX_TOOL_OUTPUT, f"{state.name} result")

    # -- read-only
    def t_read_file(self, path: str, offset: int = 1, limit: int = 2000) -> str:
        p = self.path(path, read=True)
        if not p.exists():
            raise ToolError(f"{path} does not exist.{self._did_you_mean(path)}")
        if p.is_dir():
            listing = tree(p, depth=1)
            return f"{self.rel(p)} is a directory, so here's what's in it (read a file with read_file):\n{listing}"
        if is_image(p):
            raise ToolError(f"{path} is an image; look at it with view_image")
        if is_binary(p):
            raise ToolError(f"{path} is a binary file ({p.stat().st_size} bytes)")
        lines = self.read_text(p).splitlines()
        offset, limit = max(1, int(offset or 1)), max(1, int(limit or 2000))
        chunk = lines[offset - 1 : offset - 1 + limit]
        self.read_mtimes[str(p)] = p.stat().st_mtime
        if not lines:
            return "(empty file)"
        budget, shown, size = self.agent.result_budget() - 300, [], 0
        for i, line in enumerate(chunk, start=offset):
            numbered = f"{i:6}\t{line[:2000]}"
            if shown and size + len(numbered) + 1 > budget:
                break  # the rest wouldn't fit in the context at once
            shown.append(numbered)
            size += len(numbered) + 1
        out = "\n".join(shown)
        end = offset - 1 + len(shown)
        if end < offset - 1 + len(chunk):
            out += (
                f"\n\n[Showing lines {offset}-{end} of {len(lines)}: more doesn't fit in the context at once. "
                f"Read on with offset={end + 1}.]"
            )
        elif end < len(lines) or offset > 1:
            out += f"\n\n[Showing lines {offset}-{end} of {len(lines)}. Use offset/limit to read more.]"
        return truncate(out, 120_000)

    def t_list_dir(self, path: str = ".", depth: int = 2) -> str:
        p = self.path(path, read=True)
        if p.is_file():
            return f"{self.rel(p)} is a file, not a directory: read it with read_file."
        if not p.is_dir():
            raise ToolError(f"{path} does not exist.{self._did_you_mean(path)}")
        return tree(p, depth=max(1, min(int(depth or 2), 6)))

    def t_glob(self, pattern: str, path: str = ".") -> str:
        base = self.path(path)
        pattern = pattern.removeprefix("./")
        patterns = [pattern] + ([pattern[3:]] if pattern.startswith("**/") else [])
        hits = [
            p for p in walk_files(base) if any(fnmatch.fnmatch(p.relative_to(base).as_posix(), x) for x in patterns)
        ]
        hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if not hits:
            if not base.is_dir():
                return f"No files found: {path} does not exist.{self._did_you_mean(path)}"
            return "No files found."
        more = f"\n... and {len(hits) - 200} more" if len(hits) > 200 else ""
        return "\n".join(self.rel(p) for p in hits[:200]) + more

    def t_grep(
        self, pattern: str, path: str = ".", glob: str | None = None, ignore_case: bool = False, context: int = 0
    ) -> str:
        p = self.path(path)
        literal = False  # the pattern wasn't a valid regex, so it was searched as plain text
        if shutil.which("rg"):
            cmd = ["rg", "--line-number", "--no-heading", "--with-filename", "--hidden", "--color", "never"]
            cmd += ["--max-columns", "400"]
            if ignore_case:
                cmd.append("-i")
            if context:
                cmd += ["-C", str(int(context))]
            if glob:
                cmd += ["-g", glob]
            for d in IGNORE_DIRS:
                cmd += ["-g", f"!{d}/"]
            if not p.exists():
                raise ToolError(f"{path} does not exist.{self._did_you_mean(path)}")
            r = subprocess.run([*cmd, "-e", pattern, str(p)], capture_output=True, text=True, timeout=60)
            if r.returncode == 2 and "regex parse error" in r.stderr:
                # "def add(" is meant literally: search for the text instead of failing
                r = subprocess.run([*cmd, "-F", "-e", pattern, str(p)], capture_output=True, text=True, timeout=60)
                literal = True
            if r.returncode == 2:
                raise ToolError(r.stderr.strip())
            out = r.stdout
        else:
            if not p.exists():
                raise ToolError(f"{path} does not exist.{self._did_you_mean(path)}")
            try:
                rx = re.compile(pattern, re.I if ignore_case else 0)
            except re.error:
                rx = re.compile(re.escape(pattern), re.I if ignore_case else 0)  # meant literally
                literal = True
            results = []
            for f in [p] if p.is_file() else walk_files(p):
                if (glob and not fnmatch.fnmatch(f.name, glob)) or is_binary(f):
                    continue
                try:
                    for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                        if rx.search(line):
                            results.append(f"{f}:{n}:{line[:400]}")
                except OSError:
                    continue
            out = "\n".join(results)
        out = out.replace(str(self.agent.cwd) + os.sep, "")
        lines = out.splitlines()
        note = "[The pattern isn't a valid regular expression, so it was searched as plain text.]\n" if literal else ""
        if not lines:
            return note + "No matches."
        if len(lines) > 400:
            return note + "\n".join(lines[:400]) + f"\n... [{len(lines) - 400} more lines; narrow the search]"
        return note + out

    # -- file changes
    def _did_you_mean(self, path: str) -> str:
        """Existing paths close to one that doesn't exist: the same name elsewhere in the project, or a near name
        in the nearest folder that exists. Saves the model a search, or a guess."""
        wanted = self.resolve(path)
        found: list[Path] = []
        from lcode.checkpoints import work_tree_for

        for i, candidate in enumerate(walk_files(work_tree_for(self.agent.cwd))):
            if i > 20_000 or len(found) >= 3:
                break
            if candidate.name == wanted.name:
                found.append(candidate)
        folder = wanted.parent
        while not folder.is_dir() and folder != folder.parent:
            folder = folder.parent
        try:
            names = [e.name + ("/" if e.is_dir() else "") for e in folder.iterdir() if e.name not in IGNORE_DIRS]
        except OSError:
            names = []
        for name in difflib.get_close_matches(wanted.name, names, n=3, cutoff=0.6):
            found.append(folder / name.rstrip("/"))
        shown = list(dict.fromkeys(self.rel(p) + ("/" if p.is_dir() else "") for p in found))[:3]
        return f" Did you mean: {', '.join(shown)}?" if shown else ""

    def _check_fresh(self, p: Path) -> None:
        key = str(p)
        if key not in self.read_mtimes:
            raise ToolError(f"You must read_file {self.rel(p)} before modifying it.")
        if p.stat().st_mtime > self.read_mtimes[key] + 1e-6:
            raise ToolError(f"{self.rel(p)} changed on disk since you read it. Read it again first.")

    def _diff(self, p: Path, old: str, new: str) -> Syntax:
        diff = "".join(
            difflib.unified_diff(
                old.splitlines(True), new.splitlines(True), f"a/{self.rel(p)}", f"b/{self.rel(p)}", n=3
            )
        )
        if len(diff) > 12_000:
            diff = diff[:12_000] + "\n... (diff truncated for display)\n"
        return Syntax(diff or "(no changes)", "diff", theme="monokai", word_wrap=True)

    def read_text(self, p: Path) -> str:
        """A file's text: the editor's buffer when lcode runs inside one, else the file on disk."""
        if self.reader is not None:
            text = self.reader(p)
            if text is not None:
                return text
        text, self.formats[str(p)] = decode(p.read_bytes())
        return text

    def _write(self, p: Path, content: str) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        fmt = self.formats.get(str(p)) or (decode(p.read_bytes())[1] if p.exists() else TextFormat())
        data = encode(content, fmt)  # before anything is written: it may not be possible
        if self.rel(p) not in self.before:
            self.before[self.rel(p)] = decode(p.read_bytes())[0] if p.exists() else None
        if self.writer is not None:
            self.writer(p, content)  # the editor updates its buffer (and the file)
        if not p.exists() or p.read_bytes() != data:
            p.write_bytes(data)  # the file on disk must match, for checkpoints and the tools
        self.read_mtimes[str(p)] = p.stat().st_mtime
        self.changed[self.rel(p)] = None

    def t_write_file(self, path: str, content: str) -> str:
        p = self.path(path)
        exists = p.exists()
        if exists:
            if p.is_dir():
                raise ToolError(f"{path} is a directory")
            self._check_fresh(p)
            shown = self.read_text(p)
            body = self._diff(p, shown, content)
        else:
            shown = None
            lines = content.splitlines()
            preview = "\n".join(lines[:60]) + (f"\n... ({len(lines) - 60} more lines)" if len(lines) > 60 else "")
            body = Syntax(preview, Syntax.guess_lexer(str(p), content), theme="monokai", line_numbers=True)
        verb = "Overwrite" if exists else "Create"
        ok, feedback = self.agent.perms.request("edit", "edit", f"{verb} {self.rel(p)}", body, self.project_rel(p))
        if not ok:
            return feedback
        before = self.read_text(p) if p.exists() else None
        if before != shown:
            return self._changed_meanwhile(p)
        self.agent.checkpoint()
        self._write(p, content)
        n = len(content.splitlines())
        self.console.print(f"  [green]✓[/] {'Updated' if exists else 'Created'} {self.rel(p)} ({n} lines)")
        return f"{'Overwrote' if exists else 'Created'} {self.rel(p)} ({n} lines)." + self._check(p, before, content)

    def _changed_meanwhile(self, p: Path) -> str:
        """The file changed while the user was asked (they edited it): writing now would undo their change."""
        self.read_mtimes.pop(str(p), None)
        self.console.print(
            Text(f"  ⎿ {self.rel(p)} changed while you were asked; the edit wasn't applied", style="yellow")
        )
        return (
            f"Error: {self.rel(p)} changed while the user was being asked (they or another program edited it), so "
            "lcode didn't apply this change: it would have undone theirs. Read the file again and redo the change "
            "on its current content."
        )

    def t_apply_patch(self, patch: str) -> str:
        try:
            files = matching.parse_patch(patch)
        except ValueError as e:
            raise ToolError(str(e)) from e
        planned: list[tuple[Path, str | None, str, list[str]]] = []  # path, before, after, notes
        for fp in files:
            p = self.path(fp.path)
            if fp.deleted:
                raise ToolError(f"{fp.path}: deleting files isn't something apply_patch does; use bash (rm)")
            if fp.new_file:
                if p.exists():
                    raise ToolError(f"{fp.path} already exists; the patch creates it from /dev/null")
                planned.append((p, None, "\n".join(h.new for h in fp.hunks) + "\n", []))
                continue
            if not p.is_file():
                raise ToolError(f"{fp.path} does not exist (use --- /dev/null to create it)")
            self._check_fresh(p)
            before = self.read_text(p)
            try:
                after, notes = matching.apply_hunks(before, fp.hunks)
            except ValueError as e:
                raise ToolError(f"{fp.path}: {e}") from e
            planned.append((p, before, after, notes))
        done = []
        for p, before, after, notes in planned:
            body = self._diff(p, before or "", after)
            verb = "Create" if before is None else "Patch"
            ok, feedback = self.agent.perms.request("edit", "edit", f"{verb} {self.rel(p)}", body, self.project_rel(p))
            if not ok:
                return feedback + (f" (already patched: {', '.join(done)})" if done else "")
            if (self.read_text(p) if p.exists() else None) != before:
                return self._changed_meanwhile(p)
            self.agent.checkpoint()
            self._write(p, after)
            self.console.print(f"  [green]✓[/] {'Created' if before is None else 'Patched'} {self.rel(p)}")
            done.append(self.rel(p) + (f" ({'; '.join(notes)})" if notes else ""))
            problems = self._check(p, before, after)
            if problems:
                done[-1] += problems
        return "Patched " + ", ".join(done) + "."

    def _edit_miss(self, p: Path, miss: matching.Miss) -> str:
        """Why an edit can't be placed, with the closest lines so the model can copy them exactly."""
        if miss.reason == "ambiguous":
            return (
                f"old_string isn't in {self.rel(p)} exactly, and it's close to more than one place:\n{miss.hint}\n"
                "Add surrounding lines to old_string so it matches only one, copied exactly."
            )
        message = f"old_string not found in {self.rel(p)}. It must match the file exactly, including indentation."
        if miss.hint:
            return (
                message
                + f" The closest text is:\n{miss.hint}\nCopy what you mean from it exactly (without the line numbers)."
            )
        return message + " Re-read the file and copy the text precisely (without line-number prefixes)."

    def t_edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        p = self.path(path)
        if not p.exists():
            raise ToolError(f"{path} does not exist (use write_file to create it)")
        self._check_fresh(p)
        if old_string == new_string:
            raise ToolError("old_string and new_string are identical")
        if not old_string:
            raise ToolError("old_string is empty; use write_file to create or overwrite files")
        text = self.read_text(p)
        count = text.count(old_string)
        how, start_line = "", 0
        if count == 0:
            found = matching.locate(text, old_string, new_string)
            if isinstance(found, matching.Miss):
                raise ToolError(self._edit_miss(p, found))
            new_text, how, start_line, count = found.text, found.how, found.start_line, 1
        elif count > 1 and not replace_all:
            raise ToolError(
                f"old_string occurs {count} times in {self.rel(p)}. Add surrounding lines to make it unique, "
                "or set replace_all=true."
            )
        else:
            new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        diff = self._diff(p, text, new_text)
        ok, feedback = self.agent.perms.request("edit", "edit", f"Edit {self.rel(p)}", diff, self.project_rel(p))
        if not ok:
            return feedback
        if not p.exists() or self.read_text(p) != text:
            return self._changed_meanwhile(p)
        self.agent.checkpoint()
        self._write(p, new_text)
        self.console.print(f"  [green]✓[/] Edited {self.rel(p)}")
        problems = self._check(p, text, new_text)
        done = f"Edited {self.rel(p)} ({count} replacement(s)" + (f", {how}" if how else "") + ")."
        idx = new_text.find(new_string) if new_string and not how else -1
        if idx < 0 and not start_line:
            return done + problems
        # Show the model the edited region so it can verify the result.
        start = new_text.count("\n", 0, idx) + 1 if idx >= 0 else start_line
        lines = new_text.splitlines()
        lo, hi = max(1, start - 3), min(len(lines), start + new_string.count("\n") + 3)
        snippet = "\n".join(f"{i:6}\t{lines[i - 1]}" for i in range(lo, hi + 1))
        return f"{done} Result:\n{snippet}" + problems

    # -- shell
    def t_bash(self, command: str, timeout: int = 180, background: bool = False) -> str:
        sandbox = self.agent.sandbox
        if self.agent.planning() and not is_read_only(command):
            raise ToolError(f"{BLOCKED} Until then, only read-only commands run (ls, cat, grep, git log, …).")
        if self.agent.no_changes and not is_read_only(command):
            raise ToolError(f"{self.agent.no_changes} Only read-only commands run (ls, cat, grep, git diff, …).")
        if self.agent.read_only and not is_read_only(command):
            raise ToolError(
                "you can only run read-only commands (such as ls, cat, grep, find, git log, git diff), one at a "
                "time without pipes into other programs, redirection or chaining"
            )
        verdict = self.agent.perms.rule("bash", command)
        if verdict is not None and not verdict[0]:  # deny rules apply to read-only commands too
            return verdict[1]
        approved = False
        if self.agent.settings.secret_check and not is_read_only(command):
            action, found = secretscan.check(self.agent.cwd, command)
            if found:
                text = secretscan.report(action, found)
                if self.agent.perms.mode != "ask":  # nobody looks at each command: it doesn't run
                    self.console.print(Text(f"  ⎿ {text}", style="red"))
                    return f"Error: lcode's secret check stopped this command. {text}\n\n{secretscan.ADVICE}"
                body = Group(Text(text + "\n", style="red"), Syntax(command, "bash", theme="monokai", word_wrap=True))
                key = "secret:" + hashlib.sha256(text.encode()).hexdigest()[:16]
                ok, feedback = self.agent.perms.request(
                    key, "secret", f"This {action} looks like it holds a secret", body, command
                )
                if not ok:
                    return f"{feedback}\n\n{text}\n{secretscan.ADVICE}"
                approved = True
        if (
            not approved
            and not is_read_only(command)
            and not (sandbox and self.agent.perms.mode == "auto-edit")
            and verdict is None
        ):
            body = Syntax(command, "bash", theme="monokai", word_wrap=True)
            where = "in the sandbox" if sandbox else f"in {self.agent.cwd}"
            ok, feedback = self.agent.perms.request(bash_key(command), "bash", f"Run command ({where})", body, command)
            if not ok:
                return feedback
        if not is_read_only(command):
            self.agent.checkpoint()
        self.console.print(Text(f"  $ {command}" + ("  (in the background)" if background else ""), style="bold cyan"))
        if background is True or str(background).lower() == "true":
            return self._background(command)
        marker = f"__lcode_cwd_{secrets.token_hex(8)}__"
        script = f"{command}\n__lcode_ec=$?\nprintf '\\n{marker}%s\\n' \"$(pwd -P)\"\nexit $__lcode_ec\n"
        token = ""
        if sandbox:
            try:
                with self.console.status("Starting the sandbox…"):
                    sandbox.ensure(self.agent.cwd)
            except SandboxError as e:
                return f"Error: the sandbox can't start, so the command didn't run: {e}"
            argv, token = sandbox.exec_argv(self.agent.cwd, script)
        else:
            argv = ["bash", "-c", script]
        proc = subprocess.Popen(
            argv,
            cwd=self.agent.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
            start_new_session=True,
            bufsize=1,
        )
        # Bounded, so a command that prints faster than lcode reads waits instead of filling memory.
        lines: queue.Queue[str | None] = queue.Queue(maxsize=PENDING_LINES)
        reading = threading.Event()  # cleared when lcode stops reading
        reading.set()

        def pump() -> None:
            assert proc.stdout is not None
            for line in itertools.chain(proc.stdout, [None]):  # streamed: never the whole output
                while reading.is_set():
                    try:
                        lines.put(line, timeout=0.5)
                        break
                    except queue.Full:
                        continue
                else:
                    return

        def stop() -> None:
            if sandbox and token:
                sandbox.kill(token)  # the command runs in the container, not under our process
            os.killpg(proc.pid, signal.SIGKILL)

        threading.Thread(target=pump, daemon=True).start()
        out = OutputSpool(MAX_TOOL_OUTPUT, "output")
        last = ""  # the latest line, for the end-of-output checks below
        recent: deque[str] = deque(maxlen=40)
        shown, status, recorded = 0, "", ""
        deadline = time.time() + int(timeout or 180)
        finished = 0.0  # when the command itself ended (its marker arrived)
        held_open = False  # something it started still holds the output open
        spinner = self.agent.activity("Running the command")  # the elapsed time, and a note being typed
        spinner.start()
        try:
            while True:
                if self.agent.cancel is not None and self.agent.cancel.is_set():
                    raise KeyboardInterrupt
                if finished and time.time() - finished > LEFTOVER_GRACE:
                    held_open = True  # also when what it left running keeps printing
                    break
                if time.time() > deadline and not finished:  # also while it keeps printing
                    stop()
                    status = f"\n[Command timed out after {timeout}s and was killed]"
                    break
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    break
                if line.startswith(marker):
                    recorded = line[len(marker) :].strip()
                    finished = time.time()
                    continue
                if last:
                    out.add(last)
                    recent.append(last)
                last = line
                if shown <= 40:
                    msg = "    ... (more output hidden)" if shown == 40 else "    " + line.rstrip("\n")[:300]
                    self.console.print(Text(msg, style="dim"))
                    shown += 1
        except KeyboardInterrupt:
            reading.clear()
            stop()
            raise
        finally:
            spinner.stop()
        reading.clear()
        code = proc.wait()
        if held_open and sandbox and token:
            sandbox.kill(token)  # what it left running in the container
        if (not sandbox and stop_leftovers(proc.pid)) or held_open:
            status += (
                "\n[lcode stopped what this command left running (started with &). To keep a process running, "
                "start it with background=true: then bash_output reads its output and bash_stop stops it]"
            )
        if last and last != "\n":  # the blank line printed before the marker isn't output
            out.add(last)
            recent.append(last)
        if recorded and Path(recorded).is_dir() and Path(recorded).resolve() != self.agent.cwd:
            from lcode.checkpoints import work_tree_for

            moved = Path(recorded).resolve()
            if sandbox and not sandbox.contains(moved):
                status += "\n[That directory is outside the project; the working directory stays the same]"
            elif not sandbox and not moved.is_relative_to(work_tree_for(self.agent.workspace)):
                status += (
                    f"\n[{moved} is outside the project, so the working directory stays {self.agent.cwd}: the next "
                    "command starts there. To run something elsewhere, cd in the same command]"
                )
            else:
                self.agent.cwd = moved
                status += f"\n[Working directory is now {self.agent.cwd}]"
        if sandbox and code != 0 and not sandbox.network and NO_NETWORK.search("".join(recent)):
            status += (
                "\n[The sandbox has no network access. If this needs the network, ask the user to allow it "
                "with /sandbox network on]"
            )
        self.console.print(Text(f"  exit code {code}", style="green" if code == 0 else "red"))
        return out.text() + status + f"\n[exit code: {code}]"

    # -- web
    def _web_allowed(self, key: str, title: str, detail: str) -> tuple[bool, str]:
        settings = self.agent.settings
        if settings.web == "off":
            return False, "Web access is turned off (web = off)."
        if settings.web == "ask":
            return self.agent.perms.request(key, "web", title, Text(detail), key.removeprefix("web:"))
        return True, ""

    def t_web_search(self, query: str, max_results: int = 5) -> str:
        backend = self.agent.search_backend()
        if backend is None:
            raise ToolError("web search isn't configured on this machine; use web_fetch with a known URL instead")
        ok, feedback = self._web_allowed("web:search", "Search the web", query)
        if not ok:
            return feedback
        self.console.print(Text(f"  ⌕ {query}", style="cyan"))
        try:
            results = web.search(query, backend, max_results, self.agent.settings.searxng_url)
        except web.WebError as e:
            raise ToolError(str(e)) from e
        self.console.print(Text(f"  ⎿ {len(results)} result(s)", style="dim"))
        return web.format_results(query, results, backend)

    def t_web_fetch(self, url: str, max_chars: int = 20000) -> str:
        domain = web.urlparse(url).netloc or url
        ok, feedback = self._web_allowed(f"web:{domain}", "Fetch a web page", url)
        if not ok:
            return feedback
        self.console.print(Text(f"  ↓ {url}", style="cyan"))
        try:
            title, text = web.fetch(url)
        except web.WebError as e:
            raise ToolError(str(e)) from e
        return web.format_page(url, title, text, max(1000, min(int(max_chars or 20000), MAX_TOOL_OUTPUT)))

    def _background(self, command: str) -> str:
        sandbox, kill = self.agent.sandbox, None
        if sandbox:
            try:
                with self.console.status("Starting the sandbox…"):
                    sandbox.ensure(self.agent.cwd)
            except SandboxError as e:
                return f"Error: the sandbox can't start, so the command didn't run: {e}"
            argv, token = sandbox.exec_argv(self.agent.cwd, command)
            kill = lambda: sandbox.kill(token)  # noqa: E731
        else:
            argv = ["bash", "-c", command]
        job = self.agent.jobs.start(argv, self.agent.cwd, command, kill)
        first = job.new_output(MAX_TOOL_OUTPUT // 3).rstrip()
        for line in first.splitlines()[:10]:
            self.console.print(Text("    " + line[:300], style="dim"))
        if not job.running:
            return (
                f"The command ended right away ({job.status()}), so it isn't running in the background:\n"
                f"{first or '(no output)'}"
            )
        self.console.print(Text(f"  ⎿ background job {job.id}, /jobs to see it", style="dim"))
        return (
            f"Started background job {job.id}: it keeps running while you work. Read its new output with "
            f'bash_output(id="{job.id}") and stop it with bash_stop(id="{job.id}") when it\'s no longer needed.\n'
            f"Output so far:\n{first or '(none yet)'}"
        )

    def t_bash_output(self, id: str) -> str:
        try:
            job = self.agent.jobs.get(id)
        except JobError as e:
            raise ToolError(str(e)) from e
        text = job.new_output(MAX_TOOL_OUTPUT).rstrip()
        return f"[{describe(job)}]\n{text or '(no new output)'}"

    def t_bash_stop(self, id: str) -> str:
        try:
            job = self.agent.jobs.stop(id)
        except JobError as e:
            raise ToolError(str(e)) from e
        self.console.print(Text(f"  ⎿ {describe(job)}", style="dim"))
        last = job.new_output(4000).rstrip()
        return f"[{describe(job)}]" + (f"\nIts last output:\n{last}" if last else "")

    # -- planning
    def t_todo_write(self, todos: list | str, notes: list | str | None = None) -> str:
        if isinstance(todos, str):
            todos = json.loads(todos)
        self.todos = list(todos)
        # Small models often put a note inside an item instead of in `notes`: keep it either way.
        attached = [
            str(todo.get(key)).strip()
            for todo in self.todos
            if isinstance(todo, dict)
            for key in ("notes", "note", "details")
            if str(todo.get(key) or "").strip()
        ]
        if attached:
            notes = [*([] if notes is None else ([notes] if isinstance(notes, str) else list(notes))), *attached]
        if notes is not None:
            if isinstance(notes, str):
                try:
                    notes = json.loads(notes)
                except ValueError:
                    notes = [notes]
            self.notes = [str(n).strip()[:MAX_NOTE] for n in notes if str(n).strip()][:MAX_NOTES]
        self.show_todos()
        return "Todo list updated." + (f" {len(self.notes)} note(s) kept." if notes is not None else "")

    def show_todos(self) -> None:
        icons = {"completed": "[green]✔[/]", "in_progress": "[yellow]◐[/]", "pending": "[dim]○[/]"}
        rows = []
        for todo in self.todos:
            status, text = todo.get("status", "pending"), todo.get("content", "")
            if status == "completed":
                text = f"[strike dim]{text}[/]"
            elif status == "in_progress":
                text = f"[bold]{text}[/]"
            rows.append(f"{icons.get(status, '○')} {text}")
        if self.notes:
            rows += ["", *(f"[dim]•[/] {escape(note)}" for note in self.notes)]
        self.console.print(Panel("\n".join(rows) or "(empty)", title="Todos", title_align="left", border_style="blue"))
