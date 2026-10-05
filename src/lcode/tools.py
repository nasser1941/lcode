"""The tools the model can call: file reading/editing, search, shell and a todo list."""

from __future__ import annotations

import difflib
import fnmatch
import inspect
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
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from lcode import web
from lcode.context import shorten
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
        "packages, etc. Avoid interactive commands.",
        {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "Seconds before the command is killed (default 180)"},
        },
        ["command"],
    ),
    _fn(
        "todo_write",
        "Create or update your task list for multi-step work. Pass the full list every time. Keep exactly one "
        "item in_progress while working.",
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
            }
        },
        ["todos"],
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
OPTIONAL_SCHEMAS = [
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


def fuzzy_replace(text: str, old: str, new: str) -> str | None:
    """Replace `old` with `new` matching lines while ignoring trailing whitespace. None unless exactly one match."""
    t_lines = text.splitlines(keepends=True)
    o_lines = [line.rstrip() for line in old.strip("\n").splitlines()]
    if not o_lines:
        return None
    n = len(o_lines)
    matches = [i for i in range(len(t_lines) - n + 1) if [ln.rstrip() for ln in t_lines[i : i + n]] == o_lines]
    if len(matches) != 1:
        return None
    i = matches[0]
    tail = "\n" if t_lines[i + n - 1].endswith("\n") else ""
    replacement = new.strip("\n") + tail if new.strip("\n") else ""
    return "".join(t_lines[:i]) + replacement + "".join(t_lines[i + n :])


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
    # Bare JSON objects such as {"name": "grep", "arguments": {...}}, possibly inside ``` fences.
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", content):
        try:
            obj, _ = decoder.raw_decode(content, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("name") in TOOL_NAMES and isinstance(obj.get("arguments", {}), dict):
            calls.append({"function": {"name": obj["name"], "arguments": obj.get("arguments", {})}})
    return calls


class ToolError(Exception):
    pass


# ----------------------------------------------------------------------------- toolbox


class Toolbox:
    def __init__(self, agent: Agent):
        self.agent = agent
        self.read_mtimes: dict[str, float] = {}
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
            return fn(**args)
        except ToolError as e:
            return f"Error: {e}"
        except TypeError as e:
            return f"Error: bad arguments for {name}: {e}"
        except Exception as e:  # surface every failure to the model instead of crashing the session
            return f"Error: {type(e).__name__}: {e}"

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
                self.console.print(
                    Text(f"  ⎿ freed the GPU for {state.name} ({', '.join(freed)} reloads afterwards)", style="dim")
                )
        result = mcp.call(state, tool, arguments, image_text=self.agent.describe_image_data)
        if result.startswith("Error:"):
            return result
        self.console.print(Text(f"  ⎿ {result.count(chr(10)) + 1} line(s) from {state.name}", style="dim"))
        return shorten(result, MAX_TOOL_OUTPUT, f"{state.name} result")

    # -- read-only
    def t_read_file(self, path: str, offset: int = 1, limit: int = 2000) -> str:
        p = self.path(path, read=True)
        if not p.exists():
            raise ToolError(f"{path} does not exist")
        if p.is_dir():
            raise ToolError(f"{path} is a directory; use list_dir")
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
        out = "\n".join(f"{i:6}\t{line[:2000]}" for i, line in enumerate(chunk, start=offset))
        end = offset - 1 + len(chunk)
        if end < len(lines) or offset > 1:
            out += f"\n\n[Showing lines {offset}-{end} of {len(lines)}. Use offset/limit to read more.]"
        return truncate(out, 120_000)

    def t_list_dir(self, path: str = ".", depth: int = 2) -> str:
        p = self.path(path, read=True)
        if not p.is_dir():
            raise ToolError(f"{path} is not a directory")
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
            return "No files found."
        more = f"\n... and {len(hits) - 200} more" if len(hits) > 200 else ""
        return "\n".join(self.rel(p) for p in hits[:200]) + more

    def t_grep(
        self, pattern: str, path: str = ".", glob: str | None = None, ignore_case: bool = False, context: int = 0
    ) -> str:
        p = self.path(path)
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
            cmd += ["-e", pattern, str(p)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode == 2:
                raise ToolError(r.stderr.strip())
            out = r.stdout
        else:
            try:
                rx = re.compile(pattern, re.I if ignore_case else 0)
            except re.error as e:
                raise ToolError(f"invalid regex: {e}") from e
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
        if not lines:
            return "No matches."
        if len(lines) > 400:
            return "\n".join(lines[:400]) + f"\n... [{len(lines) - 400} more lines; narrow the search]"
        return out

    # -- file changes
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
        return p.read_text(errors="replace")

    def _write(self, p: Path, content: str) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        if self.writer is not None:
            self.writer(p, content)  # the editor updates its buffer (and the file)
        if not p.exists() or p.read_text(errors="replace") != content:
            p.write_text(content)  # the file on disk must match, for checkpoints and the tools
        self.read_mtimes[str(p)] = p.stat().st_mtime

    def t_write_file(self, path: str, content: str) -> str:
        p = self.path(path)
        exists = p.exists()
        if exists:
            if p.is_dir():
                raise ToolError(f"{path} is a directory")
            self._check_fresh(p)
            body = self._diff(p, self.read_text(p), content)
        else:
            lines = content.splitlines()
            preview = "\n".join(lines[:60]) + (f"\n... ({len(lines) - 60} more lines)" if len(lines) > 60 else "")
            body = Syntax(preview, Syntax.guess_lexer(str(p), content), theme="monokai", line_numbers=True)
        verb = "Overwrite" if exists else "Create"
        ok, feedback = self.agent.perms.request("edit", "edit", f"{verb} {self.rel(p)}", body, self.project_rel(p))
        if not ok:
            return feedback
        before = self.read_text(p) if exists else None
        self.agent.checkpoint()
        self._write(p, content)
        n = len(content.splitlines())
        self.console.print(f"  [green]✓[/] {'Updated' if exists else 'Created'} {self.rel(p)} ({n} lines)")
        return f"{'Overwrote' if exists else 'Created'} {self.rel(p)} ({n} lines)." + self._check(p, before, content)

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
        if count == 0:
            new_text = fuzzy_replace(text, old_string, new_string)
            if new_text is None:
                raise ToolError(
                    f"old_string not found in {self.rel(p)}. It must match exactly, including indentation. "
                    "Re-read the file and copy the text precisely (without line-number prefixes)."
                )
            count = 1
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
        self.agent.checkpoint()
        self._write(p, new_text)
        self.console.print(f"  [green]✓[/] Edited {self.rel(p)}")
        problems = self._check(p, text, new_text)
        idx = new_text.find(new_string) if new_string else -1
        if idx < 0:
            return f"Edited {self.rel(p)} ({count} replacement(s))." + problems
        # Show the model the edited region so it can verify the result.
        start = new_text.count("\n", 0, idx) + 1
        lines = new_text.splitlines()
        lo, hi = max(1, start - 3), min(len(lines), start + new_string.count("\n") + 3)
        snippet = "\n".join(f"{i:6}\t{lines[i - 1]}" for i in range(lo, hi + 1))
        return f"Edited {self.rel(p)} ({count} replacement(s)). Result:\n{snippet}" + problems

    # -- shell
    def t_bash(self, command: str, timeout: int = 180) -> str:
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
        if not is_read_only(command) and not (sandbox and self.agent.perms.mode == "auto-edit") and verdict is None:
            body = Syntax(command, "bash", theme="monokai", word_wrap=True)
            where = "in the sandbox" if sandbox else f"in {self.agent.cwd}"
            ok, feedback = self.agent.perms.request(bash_key(command), "bash", f"Run command ({where})", body, command)
            if not ok:
                return feedback
        if not is_read_only(command):
            self.agent.checkpoint()
        self.console.print(Text(f"  $ {command}", style="bold cyan"))
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
        lines: queue.Queue[str | None] = queue.Queue()

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        def stop() -> None:
            if sandbox and token:
                sandbox.kill(token)  # the command runs in the container, not under our process
            os.killpg(proc.pid, signal.SIGKILL)

        threading.Thread(target=pump, daemon=True).start()
        out: list[str] = []
        shown, status, recorded = 0, "", ""
        deadline = time.time() + int(timeout or 180)
        try:
            while True:
                if self.agent.cancel is not None and self.agent.cancel.is_set():
                    raise KeyboardInterrupt
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    if time.time() > deadline:
                        stop()
                        status = f"\n[Command timed out after {timeout}s and was killed]"
                        break
                    continue
                if line is None:
                    break
                if line.startswith(marker):
                    recorded = line[len(marker) :].strip()
                    continue
                out.append(line)
                if shown <= 40:
                    msg = "    ... (more output hidden)" if shown == 40 else "    " + line.rstrip("\n")[:300]
                    self.console.print(Text(msg, style="dim"))
                    shown += 1
        except KeyboardInterrupt:
            stop()
            raise
        code = proc.wait()
        if out and out[-1] == "\n":
            out.pop()  # the blank line printed before the marker
        if recorded and Path(recorded).is_dir() and Path(recorded).resolve() != self.agent.cwd:
            if sandbox and not sandbox.contains(Path(recorded).resolve()):
                status += "\n[That directory is outside the project; the working directory stays the same]"
            else:
                self.agent.cwd = Path(recorded).resolve()
                status += f"\n[Working directory is now {self.agent.cwd}]"
        if sandbox and code != 0 and not sandbox.network and NO_NETWORK.search("".join(out[-40:])):
            status += (
                "\n[The sandbox has no network access. If this needs the network, ask the user to allow it "
                "with /sandbox network on]"
            )
        self.console.print(Text(f"  exit code {code}", style="green" if code == 0 else "red"))
        return shorten("".join(out), MAX_TOOL_OUTPUT, "output") + status + f"\n[exit code: {code}]"

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

    # -- planning
    def t_todo_write(self, todos: list | str) -> str:
        if isinstance(todos, str):
            todos = json.loads(todos)
        self.todos = list(todos)
        self.show_todos()
        return "Todo list updated."

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
        self.console.print(Panel("\n".join(rows) or "(empty)", title="Todos", title_align="left", border_style="blue"))
