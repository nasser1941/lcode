"""The agent loop: stream a model turn, run the tools it calls, repeat until it answers."""

from __future__ import annotations

import datetime as dt
import json
import platform
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from lcode import catalog, sessions
from lcode.config import format_tokens
from lcode.ollama import Ollama, OllamaError
from lcode.permissions import Permissions
from lcode.render import MarkdownStreamer
from lcode.tools import SCHEMAS, Toolbox, is_binary, parse_text_tool_calls, tree, truncate

MAX_STEPS_PER_TURN = 150  # safety cap on tool-call iterations for one request
AUTO_COMPACT_RATIO = 0.85  # summarize the history when the context is this full
PROJECT_FILES = ("AGENTS.md", "LCODE.md", "CLAUDE.md")

SYSTEM_PROMPT = """You are lcode, an autonomous software-engineering agent running in the user's terminal on their own machine, powered by the local model {model}. You help the user understand codebases, answer questions about code, write scripts (Python by default), fix bugs, refactor, and run commands.

# How to work
- Use your tools to gather facts. Never guess file contents, APIs, or command output — read, search, or run them.
- To understand a repository: look at the layout, README/docs, entry points and config, then grep and read the relevant files. Cite code as `path:line`.
- Read a file before editing it. Use edit_file for targeted changes and write_file for new files or full rewrites. old_string must be copied exactly from the file (without the line-number prefixes).
- After writing or changing code, verify it: run the script, the tests, or at least a syntax check (e.g. `{python} -m py_compile file.py`). Fix what fails.
- Match the existing code style, naming and structure. Don't add unrequested features.
- For multi-step tasks, plan with todo_write and keep it updated.
- Work autonomously until the task is done. Only stop to ask the user when you are genuinely blocked or the decision is theirs.
- Don't run destructive or irreversible commands (rm -rf, git reset --hard, git push --force, dropping data) unless the user explicitly asked.
- Never claim something works without having run it. If something fails, say so and show the relevant error.
- If the user denies a tool call, don't retry the same thing; adjust or ask.

# Communication
- Be concise and direct; no filler. Use GitHub-flavored markdown.
- When you finish, give a short summary of what you found or changed and anything the user needs to do.

# Environment
- Working directory: {cwd}
- OS: {os}; shell: bash; Python command: `{python}`
- Date: {date}
- Git: {git}

# Top-level layout of the working directory
{tree}
{memory}"""

INIT_PROMPT = """Analyze this repository and create (or improve, if it exists) an AGENTS.md file at its root that will be given to you in future sessions. Explore the codebase first (layout, README, config/build files, entry points, main modules, tests). AGENTS.md should contain:
1. A short overview of what the project does.
2. How to set up, build, run, and test it (exact commands).
3. The architecture: the important directories/modules and how they fit together, with key file paths.
4. Code conventions and any gotchas you noticed.
Keep it concise (under ~150 lines) and factual — only include what you verified in the code."""


def git_info(cwd: Path) -> str:
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)

    try:
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if branch.returncode != 0:
            return "not a git repository"
        changed = git("status", "--short").stdout.strip().splitlines()
        log = git("log", "--oneline", "-5").stdout.strip() or "(no commits)"
        return f"branch {branch.stdout.strip()}, {len(changed)} changed file(s)\nRecent commits:\n{log}"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


@dataclass
class Settings:
    model: str  # Ollama model name to call
    context: int
    num_batch: int | None = None
    keep_alive: str = "30m"
    think: bool = True
    show_thinking: bool = False
    permission_mode: str = "ask"


class Agent:
    def __init__(self, ollama: Ollama, settings: Settings, cwd: Path, console: Console | None = None):
        self.console = console or Console(highlight=False)
        self.ollama = ollama
        self.settings = settings
        self.cwd = cwd.resolve()
        self.perms = Permissions(self.console, settings.permission_mode)
        self.tools = Toolbox(self)
        self.session_id = self.new_session_id()
        self.session_name = ""
        self.session_title = ""
        self.ctx_used = 0
        self.last_speed = 0.0
        self.messages: list[dict] = []
        self.reset()

    # -- prompt and sessions
    @staticmethod
    def new_session_id() -> str:
        return dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]

    def system_prompt(self) -> str:
        memory = ""
        for name in PROJECT_FILES:
            f = self.cwd / name
            if f.is_file():
                memory = f"\n# Project instructions ({name})\n{truncate(f.read_text(errors='replace'), 20_000)}\n"
                break
        return SYSTEM_PROMPT.format(
            model=self.settings.model,
            cwd=self.cwd,
            os=f"{platform.system()} {platform.release()} ({platform.machine()})",
            python="python" if shutil.which("python") else "python3",
            date=dt.date.today().isoformat(),
            git=git_info(self.cwd),
            tree=tree(self.cwd, depth=1, limit=120),
            memory=memory,
        )

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self.system_prompt()}]
        self.tools.read_mtimes.clear()
        self.ctx_used = len(self.messages[0]["content"]) // 3

    def session_file(self) -> Path:
        return sessions.sessions_dir() / f"{self.session_id}.json"

    def has_conversation(self) -> bool:
        return any(m.get("role") == "user" for m in self.messages)

    def save(self) -> None:
        if not self.has_conversation():
            return
        self.session_title = self.session_title or sessions.title_from(self.messages)
        f = self.session_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cwd": str(self.cwd),
            "model": self.settings.model,
            "name": self.session_name,
            "title": self.session_title,
            "messages": self.messages,
        }
        f.write_text(json.dumps(data))

    def new_session(self) -> None:
        self.reset()
        self.session_id = self.new_session_id()
        self.session_name = ""
        self.session_title = ""

    def rename(self, name: str) -> None:
        self.session_name = " ".join(name.split())
        self.save()

    def load(self, info: sessions.SessionInfo) -> str:
        """Resume a saved session. Returns a note about the working directory, if it changed."""
        try:
            data = json.loads(info.path.read_text())
        except (OSError, ValueError) as e:
            raise OSError(f"can't read session {info.id}: {e}") from e
        note = ""
        saved_cwd = Path(info.cwd) if info.cwd else self.cwd
        if saved_cwd != self.cwd:
            if saved_cwd.is_dir():
                self.cwd = saved_cwd.resolve()
                note = f"Working directory is now {self.cwd}"
            else:
                note = f"The session's directory {saved_cwd} no longer exists; staying in {self.cwd}"
        self.messages = [{"role": "system", "content": self.system_prompt()}, *data.get("messages", [])[1:]]
        self.session_id = info.id
        self.session_name = data.get("name", "")
        self.session_title = data.get("title") or sessions.title_from(self.messages)
        self.tools.read_mtimes.clear()  # files may have changed since; the model must read them again
        self.ctx_used = sum(len(json.dumps(m)) for m in self.messages) // 3
        return note

    def load_latest(self) -> bool:
        latest = sessions.list_sessions(self.cwd, limit=1)
        if latest:
            self.load(latest[0])
        return bool(latest)

    # -- model calls
    def options(self) -> dict:
        opts: dict = {"num_ctx": self.settings.context}
        if self.settings.num_batch:
            opts["num_batch"] = self.settings.num_batch
        return opts

    def chat(self, messages: list[dict], tools: list | None, think: bool):
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "think": think,
            "keep_alive": self.settings.keep_alive,
            "options": self.options(),
        }
        if tools:
            payload["tools"] = tools
        try:
            yield from self.ollama.chat_stream(payload)
        except OllamaError as e:
            if think and "think" in str(e) and "support" in str(e):
                self.settings.think = False
                self.console.print(f"[dim]{self.settings.model} does not support reasoning; continuing without.[/]")
                yield from self.ollama.chat_stream({**payload, "think": False})
                return
            if "out of memory" in str(e).lower():
                raise OllamaError(
                    f"{e}\nThe model ran out of GPU memory. Try a smaller context (/ctx 128k) or a smaller "
                    "prompt batch (`lcode config set num_batch 512`)."
                ) from e
            raise

    def assistant_step(self) -> dict:
        """Stream one model response, rendering thinking/content live. Returns the assistant message."""
        content, thinking, tool_calls, final = "", "", [], {}
        md = MarkdownStreamer(self.console)
        status = self.console.status("[cyan]Thinking…[/]", spinner="dots")
        status.start()
        spinning, printed_thinking, t0 = True, False, time.time()

        def stop_spinner() -> None:
            nonlocal spinning
            if spinning:
                status.stop()
                spinning = False

        try:
            for chunk in self.chat(self.messages, SCHEMAS, self.settings.think):
                msg = chunk.get("message", {})
                if msg.get("thinking"):
                    thinking += msg["thinking"]
                    if self.settings.show_thinking:
                        stop_spinner()
                        self.console.print(Text(msg["thinking"], style="dim italic"), end="")
                        printed_thinking = True
                    else:
                        status.update(
                            f"[cyan]Thinking…[/] [dim]({len(thinking) // 4} tokens, {time.time() - t0:.0f}s)[/]"
                        )
                if msg.get("content"):
                    stop_spinner()
                    if printed_thinking:
                        self.console.print("\n")
                        printed_thinking = False
                    content += msg["content"]
                    md.feed(msg["content"])
                if msg.get("tool_calls"):
                    tool_calls.extend(msg["tool_calls"])
                if chunk.get("done"):
                    final = chunk
        except KeyboardInterrupt:
            stop_spinner()
            md.flush()
            self.console.print("[yellow]⏹ Interrupted[/]")
            self.messages.append({"role": "assistant", "content": content + "\n[interrupted by user]"})
            raise
        finally:
            stop_spinner()
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
            self.ctx_used = final.get("prompt_eval_count", 0) + final.get("eval_count", 0)
            seconds = final.get("eval_duration", 0) / 1e9
            self.last_speed = final.get("eval_count", 0) / seconds if seconds else 0.0
        return message

    def describe_call(self, name: str, args: dict) -> str:
        if name in ("read_file", "write_file", "edit_file"):
            path = self.tools.rel(self.tools.resolve(args.get("path", "")))
            offset = args.get("offset")
            return f"{name}({path})" + (
                f" from line {offset}" if name == "read_file" and offset not in (None, 1) else ""
            )
        if name == "grep":
            where = args.get("path", ".") + (f" {args['glob']}" if args.get("glob") else "")
            return f"grep({args.get('pattern', '')!r} in {where})"
        if name == "glob":
            return f"glob({args.get('pattern', '')})"
        if name == "list_dir":
            return f"list_dir({args.get('path', '.')})"
        return name

    def run_turn(self, user_text: str) -> None:
        self.messages.append({"role": "user", "content": self.expand_mentions(user_text)})
        for _ in range(MAX_STEPS_PER_TURN):
            self.maybe_compact()
            calls = self.assistant_step().get("tool_calls") or []
            if not calls:
                break
            for i, call in enumerate(calls):
                fn = call.get("function", {})
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if name not in ("bash", "todo_write"):  # those print their own header
                    self.console.print(Text(f"● {self.describe_call(name, args)}", style="bold magenta"))
                try:
                    result = self.tools.run(name, args)
                except KeyboardInterrupt:
                    for rest in calls[i:]:
                        self.messages.append(
                            {
                                "role": "tool",
                                "tool_name": rest.get("function", {}).get("name", ""),
                                "content": "Interrupted by the user before completion.",
                            }
                        )
                    self.console.print("[yellow]⏹ Interrupted[/]")
                    raise
                if result.startswith("Error:"):
                    self.console.print(Text(f"  {result[:300]}", style="red"))
                elif name in ("read_file", "grep", "glob", "list_dir"):
                    self.console.print(Text(f"  ⎿ {result.count(chr(10)) + 1} line(s)", style="dim"))
                tool_msg = {"role": "tool", "tool_name": name, "content": result}
                if call.get("id"):
                    tool_msg["tool_call_id"] = call["id"]
                self.messages.append(tool_msg)
        else:
            self.console.print(f"[yellow]Stopped after {MAX_STEPS_PER_TURN} steps.[/]")
        self.print_stats()

    def print_stats(self) -> None:
        pct = 100 * self.ctx_used / self.settings.context
        self.console.print(
            Text(
                f"  ctx {format_tokens(self.ctx_used)}/{format_tokens(self.settings.context)} ({pct:.0f}%) · "
                f"{self.last_speed:.1f} tok/s",
                style="dim",
            )
        )

    def expand_mentions(self, text: str) -> str:
        """Inline files referenced as @path in the user's message."""
        attached = []
        for ref in re.findall(r"(?<!\S)@([\w./~\-]+)", text):
            p = self.tools.resolve(ref)
            if p.is_file() and not is_binary(p) and p.stat().st_size < 200_000:
                attached.append(f'<file path="{self.tools.rel(p)}">\n{p.read_text(errors="replace")}\n</file>')
                self.tools.read_mtimes[str(p)] = p.stat().st_mtime
                self.console.print(Text(f"  ⎿ attached {self.tools.rel(p)}", style="dim"))
            elif p.is_dir():
                attached.append(f'<directory path="{self.tools.rel(p)}">\n{tree(p, 2)}\n</directory>')
        return text + ("\n\n" + "\n\n".join(attached) if attached else "")

    # -- context management
    def maybe_compact(self) -> None:
        if self.ctx_used > AUTO_COMPACT_RATIO * self.settings.context:
            self.console.print("[yellow]Context is nearly full — compacting the conversation…[/]")
            self.compact()

    def compact(self, focus: str = "") -> None:
        if len(self.messages) <= 2:
            return
        instructions = (
            "Summarize this conversation so the work can continue in a fresh context. Include: the user's goals "
            "and requests, key facts learned about the codebase (files, functions, paths with line numbers), "
            "decisions made, every file created or modified and how, commands run and their outcomes, the current "
            "state, and the remaining next steps. Be thorough but compact. Do not call tools."
        )
        if focus:
            instructions += f" Focus especially on: {focus}"
        summary = ""
        with self.console.status("[cyan]Compacting…[/]"):
            for chunk in self.chat([*self.messages, {"role": "user", "content": instructions}], None, False):
                summary += chunk.get("message", {}).get("content", "")
        self.messages = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": f"[Summary of our conversation so far]\n\n{summary}"},
            {"role": "assistant", "content": "Got it — I have the context from the summary and will continue."},
        ]
        self.ctx_used = sum(len(m["content"]) for m in self.messages) // 3
        self.console.print(Panel(Markdown(summary), title="Compacted summary", border_style="blue"))

    # -- settings changes
    def set_context(self, tokens: int) -> str:
        spec = catalog.find(self.settings.model)
        limit = None
        try:
            limit = self.ollama.max_context(self.settings.model)
        except OllamaError:
            limit = spec.max_context if spec else None
        if limit and tokens > limit:
            tokens = limit
            note = f" (capped at the model's maximum, {format_tokens(limit)})"
        else:
            note = ""
        self.settings.context = tokens
        return f"Context window set to {format_tokens(tokens)} tokens{note}. The model reloads on the next request."
