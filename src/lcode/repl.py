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
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from lcode import __version__, catalog, sessions
from lcode.agent import INIT_PROMPT, Agent
from lcode.catalog import MIN_USEFUL_CONTEXT
from lcode.config import PERMISSION_MODES, STATE_DIR, ConfigError, format_tokens, parse_context
from lcode.hardware import Hardware
from lcode.ollama import OllamaError
from lcode.tools import IGNORE_DIRS

COMMANDS = {
    "/help": "Show this help",
    "/init": "Analyze the repo and write an AGENTS.md guide (loaded at every start)",
    "/clear": "Start a fresh conversation",
    "/rename": "Name this session so you can find it later, e.g. /rename auth refactor",
    "/resume": "Resume a saved session: pick from a list, or /resume <number|name> (/resume all: every folder)",
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
            + (f" · <b>{html.escape(agent.session_name)}</b>" if agent.session_name else "")
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
        agent.new_session()
        c.print("[green]Started a new conversation.[/] The previous one is saved; /resume brings it back.")
    elif cmd == "/rename":
        if not arg:
            current = f"'{escape(agent.session_name)}'" if agent.session_name else "not named yet"
            c.print(f"This session is {current}. Name it with /rename <name>.")
        else:
            agent.rename(arg)
            c.print(f"[green]Session named '{escape(agent.session_name)}'.[/] Find it later with /resume.")
    elif cmd == "/resume":
        info = choose_session(agent, arg)
        if info:
            resume_session(agent, info)
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
            if spec:  # size the context for the new model: largest window that fits this machine
                s.context = spec.fit(hardware)[0] or MIN_USEFUL_CONTEXT
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


def print_sessions(agent: Agent, found: list[sessions.SessionInfo], all_dirs: bool) -> None:
    where = "all folders" if all_dirs else str(agent.cwd)
    table = Table(title=f"Saved sessions · {where}", title_justify="left", header_style="bold")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Session")
    table.add_column("Last used", no_wrap=True)
    table.add_column("Requests", justify="right")
    if all_dirs:
        table.add_column("Folder", overflow="fold")
    for i, info in enumerate(found, 1):
        if info.name:
            label = f"[bold]{escape(info.name)}[/]\n[dim]{escape(info.title or '(no requests yet)')}[/]"
        else:
            label = escape(info.label)
        if info.id == agent.session_id:
            label += " [green](current)[/]"
        row = [str(i), label, sessions.age(info.updated), str(info.turns)]
        if all_dirs:
            row.append(escape(info.cwd))
        table.add_row(*row)
    agent.console.print(table)


def choose_session(agent: Agent, query: str = "") -> sessions.SessionInfo | None:
    """Find a session by number/name/id, or list them and ask. `all` lists every folder."""
    c = agent.console
    words = query.split()
    all_dirs = bool(words) and words[0].lower() in ("all", "--all", "-a")
    query = " ".join(words[1:] if all_dirs else words)
    found = sessions.list_sessions(None if all_dirs else agent.cwd)
    if not found and not all_dirs:
        found, all_dirs = sessions.list_sessions(None), True
        if found and not query:
            c.print("[dim]No saved sessions in this folder; showing all folders.[/]")
    if not found:
        c.print("No saved sessions yet. Sessions are saved after every request.")
        return None
    if query:
        match = sessions.find(query, found)
        if match is None and not all_dirs:
            match = sessions.find(query, sessions.list_sessions(None))
        if match:
            return match
        c.print(f"[yellow]No session matches '{escape(query)}'.[/]")
    print_sessions(agent, found, all_dirs)
    try:
        answer = input("  Resume which session? (number or name, Enter to cancel): ").strip()
    except EOFError:
        return None
    if not answer:
        return None
    match = sessions.find(answer, found)
    if match is None:
        c.print(f"[yellow]No session matches '{escape(answer)}'.[/]")
    return match


def resume_session(agent: Agent, info: sessions.SessionInfo) -> None:
    c = agent.console
    if info.id == agent.session_id:
        c.print("That's the current session.")
        return
    agent.save()
    try:
        note = agent.load(info)
    except OSError as e:
        c.print(f"[red]{e}[/]")
        return
    c.print(
        f"[green]Resumed[/] [bold]{escape(info.label)}[/] "
        f"[dim]({info.turns} request{'' if info.turns == 1 else 's'}, last used {sessions.age(info.updated)})[/]"
    )
    if note:
        c.print(f"[yellow]{escape(note)}[/]")
    print_recap(agent)


def print_recap(agent: Agent) -> None:
    """Show the last request and the start of the last answer, so it's clear where things left off."""
    last_user = next((m for m in reversed(agent.messages) if m.get("role") == "user"), None)
    last_answer = next(
        (m for m in reversed(agent.messages) if m.get("role") == "assistant" and m.get("content", "").strip()), None
    )
    if not last_user:
        return
    lines = [f"[bold]You:[/] {escape(sessions.title_from([last_user]))}"]
    if last_answer:
        answer = " ".join(last_answer["content"].split())
        answer = answer if len(answer) <= 300 else answer[:299].rstrip() + "…"
        lines.append(f"[bold]lcode:[/] {escape(answer)}")
    agent.console.print(Panel("\n".join(lines), title="Where you left off", title_align="left", border_style="dim"))


def repl(agent: Agent, prompt: str | None, hardware: Hardware, cont: bool = False, resume: str | None = None) -> None:
    if cont:
        if agent.load_latest():
            agent.console.print(f"[green]Continuing[/] [bold]{escape(agent.session_name or agent.session_title)}[/]")
            print_recap(agent)
        else:
            agent.console.print("[dim]No saved session in this folder yet; starting a new one.[/]")
    elif resume is not None:
        info = choose_session(agent, resume)
        if info:
            resume_session(agent, info)
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
        if line.lower() in sessions.EXIT_WORDS:  # people type these expecting to quit, not to ask the model
            break
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
