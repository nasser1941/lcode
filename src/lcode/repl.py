"""The interactive prompt: input handling, slash commands and the status bar."""

from __future__ import annotations

import html
import os
import re
import threading
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Group
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from lcode import __version__, catalog
from lcode.agent import INIT_PROMPT, Agent
from lcode.config import PERMISSION_MODES, STATE_DIR, ConfigError, format_tokens, parse_context
from lcode.hardware import Hardware
from lcode.ollama import OllamaError
from lcode.tools import IGNORE_DIRS

COMMANDS = {
    "/help": "Show this help",
    "/init": "Analyze the repo and write an AGENTS.md guide (loaded at every start)",
    "/clear": "Start a fresh conversation",
    "/compact": "Summarize the conversation to free context (optional: what to focus on)",
    "/context": "Show context-window usage",
    "/ctx": "Show or change the context window, e.g. /ctx 128k",
    "/model": "Show or switch model, e.g. /model qwen3.5-9b",
    "/models": "List models and how they fit this machine",
    "/think": "Toggle model reasoning on/off",
    "/verbose": "Toggle showing the model's reasoning text",
    "/mode": "Permission mode: ask | auto-edit | yolo (Shift+Tab cycles)",
    "/cd": "Change the working directory",
    "/todos": "Show the current todo list",
    "/exit": "Quit",
}


class InputCompleter(Completer):
    """Completes slash commands and @file mentions."""

    def __init__(self, agent: Agent):
        self.agent = agent

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/") and " " not in text:
            for cmd, desc in COMMANDS.items():
                if cmd.startswith(text):
                    yield Completion(cmd, start_position=-len(text), display_meta=desc)
            return
        m = re.search(r"(?:^|\s)@([\w./~\-]*)$", text)
        if not m:
            return
        fragment = m.group(1)
        base = self.agent.cwd / os.path.expanduser(os.path.dirname(fragment) or ".")
        prefix = os.path.basename(fragment)
        try:
            entries = sorted(base.iterdir())
        except OSError:
            return
        for entry in entries:
            hidden = entry.name.startswith(".") and not prefix.startswith(".")
            if entry.name.startswith(prefix) and entry.name not in IGNORE_DIRS and not hidden:
                completion = os.path.join(os.path.dirname(fragment), entry.name) + ("/" if entry.is_dir() else "")
                yield Completion(completion, start_position=-len(fragment))


def build_session(agent: Agent) -> PromptSession:
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        buf = event.current_buffer
        if buf.complete_state and buf.complete_state.current_completion:
            buf.apply_completion(buf.complete_state.current_completion)
        elif buf.text.endswith("\\"):
            buf.delete_before_cursor(1)
            buf.insert_text("\n")
        else:
            buf.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    @kb.add("s-tab")
    def _cycle_mode(event):
        agent.perms.cycle()
        event.app.invalidate()

    def toolbar():
        s = agent.settings
        pct = 100 * agent.ctx_used / s.context
        color = {"ask": "ansigreen", "auto-edit": "ansiyellow", "yolo": "ansired"}[agent.perms.mode]
        return HTML(
            f" <b>{html.escape(s.model)}</b> · ctx {format_tokens(agent.ctx_used)}/{format_tokens(s.context)} "
            f"({pct:.0f}%) · mode <{color}>{agent.perms.mode}</{color}> (shift+tab) · "
            f"think {'on' if s.think else 'off'} · {html.escape(agent.cwd.name)}/"
        )

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return PromptSession(
        history=FileHistory(str(STATE_DIR / "history")),
        key_bindings=kb,
        multiline=True,
        completer=InputCompleter(agent),
        complete_while_typing=True,
        bottom_toolbar=toolbar,
        prompt_continuation="  ",
    )


def banner(agent: Agent) -> None:
    s = agent.settings
    agent.console.print(
        Panel(
            Group(
                Text.from_markup(f"[bold cyan]lcode[/] [dim]v{__version__}[/] — local coding agent\n"),
                Text.from_markup(
                    f"[dim]model[/]    {s.model}\n"
                    f"[dim]context[/]  {format_tokens(s.context)} tokens\n"
                    f"[dim]cwd[/]      {agent.cwd}\n"
                    f"[dim]mode[/]     {agent.perms.mode} [dim](Shift+Tab to cycle)[/]\n\n"
                    "[dim]/help for commands · @file to attach · Esc+Enter for a newline[/]"
                ),
            ),
            border_style="cyan",
            expand=False,
        )
    )


def run_safely(agent: Agent, text: str) -> None:
    try:
        agent.run_turn(text)
    except KeyboardInterrupt:
        pass
    except OllamaError as e:
        agent.console.print(f"[red]{e}[/]")
    finally:
        agent.save()


def handle_command(agent: Agent, line: str, hardware: Hardware) -> bool:
    """Run a slash command. Returns False when the REPL should exit."""
    from lcode.cli import NotInstalled, print_models, resolve_model

    cmd, _, arg = line.partition(" ")
    arg = arg.strip()
    c, s = agent.console, agent.settings
    if cmd in ("/exit", "/quit", "/q"):
        return False
    if cmd == "/help":
        rows = "\n".join(f"  [cyan]{k:<10}[/] {v}" for k, v in COMMANDS.items())
        keys = (
            "Enter = send · Esc+Enter or trailing \\ = newline · Ctrl+C = interrupt · Ctrl+D = quit · "
            "@path = attach a file · Shift+Tab = cycle permission mode"
        )
        c.print(Panel(f"{rows}\n\n  [dim]{keys}[/]", title="lcode commands", border_style="cyan"))
    elif cmd == "/clear":
        agent.reset()
        agent.session_id = agent.new_session_id()
        c.print("[green]Conversation cleared.[/]")
    elif cmd == "/compact":
        agent.compact(arg)
    elif cmd == "/context":
        c.print(
            f"Context: {format_tokens(agent.ctx_used)} of {format_tokens(s.context)} tokens "
            f"({100 * agent.ctx_used / s.context:.1f}%) in {len(agent.messages)} messages; "
            "auto-compacts at 85%."
        )
    elif cmd == "/ctx":
        if not arg:
            spec = catalog.find(s.model)
            extra = f" · model maximum {format_tokens(spec.max_context)}" if spec else ""
            c.print(f"Context window: {format_tokens(s.context)} tokens{extra}. Change it with /ctx 128k")
        else:
            try:
                c.print(agent.set_context(parse_context(arg)))
            except ConfigError as e:
                c.print(f"[red]{e}[/]")
            spec = catalog.find(s.model)
            if spec and spec.memory_gib(s.context) > hardware.budget_gib:
                c.print(
                    f"[yellow]~{spec.memory_gib(s.context):.0f} GB needed but ~{hardware.budget_gib:.0f} GB "
                    "available; this may run out of memory.[/]"
                )
    elif cmd == "/models":
        print_models(agent.ollama, hardware, s.model)
    elif cmd == "/model":
        if not arg:
            c.print(f"Model: {s.model}. Switch with /model <key or Ollama tag>; list with /models.")
        else:
            try:
                name, spec = resolve_model(agent.ollama, arg)
            except (NotInstalled, OllamaError) as e:
                c.print(f"[red]{e}[/]")
                return True
            s.model = name
            s.num_batch = spec.num_batch if spec and name == spec.local_name else None
            limit = spec.max_context if spec else None
            if limit and s.context > limit:
                s.context = limit
            agent.messages[0]["content"] = agent.system_prompt()
            c.print(f"[green]Switched to {name}[/] (context {format_tokens(s.context)}).")
    elif cmd == "/think":
        s.think = (arg == "on") if arg in ("on", "off") else not s.think
        c.print(f"Reasoning {'on' if s.think else 'off'}.")
    elif cmd == "/verbose":
        s.show_thinking = not s.show_thinking
        c.print(f"Showing reasoning text: {'on' if s.show_thinking else 'off'}.")
    elif cmd == "/mode":
        if arg in PERMISSION_MODES:
            agent.perms.mode = arg
        elif arg:
            c.print(f"[red]Unknown mode; choose from {', '.join(PERMISSION_MODES)}[/]")
        c.print(f"Permission mode: {agent.perms.mode}")
    elif cmd == "/cd":
        p = Path(os.path.expanduser(arg or "~"))
        p = (p if p.is_absolute() else agent.cwd / p).resolve()
        if p.is_dir():
            agent.cwd = p
            agent.messages[0]["content"] = agent.system_prompt()
            c.print(f"Working directory: {p}")
        else:
            c.print(f"[red]Not a directory: {p}[/]")
    elif cmd == "/todos":
        agent.tools.show_todos()
    elif cmd == "/init":
        run_safely(agent, INIT_PROMPT)
    else:
        c.print(f"[red]Unknown command {cmd}.[/] Type /help.")
    return True


def repl(agent: Agent, prompt: str | None, resume: bool, hardware: Hardware) -> None:
    if resume and agent.load_latest():
        agent.console.print(f"[green]Resumed session {agent.session_id} ({len(agent.messages)} messages).[/]")
    if prompt:
        run_safely(agent, prompt)
        return
    preload(agent)
    banner(agent)
    session = build_session(agent)
    while True:
        try:
            agent.console.print()
            line = session.prompt(HTML("<ansicyan><b>❯ </b></ansicyan>")).strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        if not line:
            continue
        if line.startswith("/") and not line.startswith("//"):
            if not handle_command(agent, line, hardware):
                break
            continue
        agent.console.print(Rule(style="dim"))
        run_safely(agent, line)
    agent.console.print("[dim]Bye.[/]")


def preload(agent: Agent) -> None:
    """Start loading the model in the background so the first answer comes sooner."""

    def load() -> None:
        try:
            agent.ollama.load(agent.settings.model, agent.options(), agent.settings.keep_alive)
        except OllamaError:
            pass

    threading.Thread(target=load, daemon=True).start()
