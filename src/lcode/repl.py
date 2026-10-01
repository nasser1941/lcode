"""The interactive prompt: input handling, slash commands and the status bar."""

from __future__ import annotations

import html
import json
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

from lcode import __version__, catalog, limits, sessions, web
from lcode.agent import AUTO_COMPACT_RATIO, INIT_PROMPT, Agent
from lcode.catalog import MIN_USEFUL_CONTEXT
from lcode.checkpoints import Checkpoint, CheckpointError, Restore
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
    "/undo": "Undo the file changes of the last request (lcode saves a checkpoint before changing files)",
    "/rewind": "Go back to before an earlier request: its files, and optionally the conversation",
    "/checkpoints": "List the requests that changed files, and which files",
    "/sandbox": "Shell-command sandbox: status, or /sandbox network on|off",
    "/mcp": "MCP servers and their tools: /mcp, /mcp tools NAME, /mcp login NAME, /mcp restart NAME",
    "/compact": "Summarize the conversation to free context (optional: what to focus on)",
    "/context": "Show context usage and change the window size: pick from a list, or /context 128k",
    "/ctx": "Shortcut for /context",
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
                    f"[dim]mode[/]     {agent.perms.mode} [dim](Shift+Tab to cycle)[/]\n"
                    f"[dim]web[/]      {web_status(agent)}\n"
                    + (f"[dim]mcp[/]      {escape(', '.join(agent.mcp.servers))} [dim](/mcp)[/]\n" if agent.mcp else "")
                    + (
                        f"[dim]sandbox[/]  {escape(agent.sandbox.describe())} [dim](/sandbox)[/]\n"
                        if agent.sandbox
                        else ""
                    )
                    + "\n"
                    "[dim]/help for commands · @file to attach · Esc+Enter for a newline[/]"
                ),
            ),
            border_style="cyan",
            expand=False,
        )
    )


CONTEXT_CHOICES = (16384, 32768, 65536, 131072, 262144, 524288, 1048576)


def context_options(agent: Agent, hardware: Hardware) -> list[dict]:
    """The context sizes this model supports, with how each one fits this machine."""
    s = agent.settings
    spec = catalog.find(s.model)
    try:
        max_ctx = agent.ollama.max_context(s.model) or (spec.max_context if spec else None)
    except OllamaError:
        max_ctx = spec.max_context if spec else None
    sizes = {c for c in CONTEXT_CHOICES if not max_ctx or c <= max_ctx} | {s.context}
    if max_ctx:
        sizes.add(max_ctx)
    learned = limits.get(s.model)
    recommended = limits.cap(s.model, spec.fit(hardware)[0]) if spec and spec.fit(hardware)[0] else None
    options = []
    for size in sorted(sizes):
        if spec:
            memory = spec.memory_gib(size)
            if hardware.unified:
                fit = "fits" if memory <= hardware.budget_gib - catalog.HEADROOM_GIB else "too large"
            elif hardware.gpu and memory <= hardware.vram_gib:
                fit = "on GPU"
            elif memory <= hardware.budget_gib - catalog.HEADROOM_GIB:
                fit = "GPU + RAM" if spec.moe else "GPU + RAM, slow"
            else:
                fit = "too large"
            note = f"~{memory:.0f} GB · {fit}"
        else:
            note = "no memory estimate for this model"
        if learned and size > learned:
            note += " · ran out of memory here before"
        options.append({"size": size, "note": note, "current": size == s.context, "recommended": size == recommended})
    return options


def context_command(agent: Agent, arg: str, hardware: Hardware) -> None:
    """Show context usage and change the window size, from a list or directly (/context 128k)."""
    c, s = agent.console, agent.settings
    c.print(
        f"In use: {format_tokens(agent.ctx_used)} of {format_tokens(s.context)} tokens "
        f"({100 * agent.ctx_used / s.context:.0f}%) in {len(agent.messages)} message"
        f"{'' if len(agent.messages) == 1 else 's'}; auto-compacts at 85%."
    )
    if agent.mcp and agent.mcp.ready():
        how = "found on demand" if agent.mcp.searching(s.context) else "sent with every request"
        c.print(f"MCP tools: ~{format_tokens(agent.mcp.cost())} tokens of definitions, {how}.")
    if arg:
        try:
            apply_context(agent, parse_context(arg), hardware)
        except ConfigError as e:
            c.print(f"[red]{e}[/]")
        return
    options = context_options(agent, hardware)
    table = Table(title=f"Context window for {s.model}", title_justify="left", header_style="bold")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Size", justify="right")
    table.add_column("Memory · fit")
    table.add_column("")
    for i, option in enumerate(options, 1):
        marks = []
        if option["current"]:
            marks.append("[bold]current[/]")
        if option["recommended"]:
            marks.append("[cyan]recommended[/]")
        table.add_row(str(i), format_tokens(option["size"]), option["note"], ", ".join(marks))
    c.print(table)
    try:
        answer = input(f"  Choose a number, a size like 96k, or press Enter to keep {format_tokens(s.context)}: ")
    except EOFError:
        return
    answer = answer.strip()
    if not answer:
        c.print(f"Keeping {format_tokens(s.context)}.")
        return
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        size = options[int(answer) - 1]["size"]
    else:
        try:
            size = parse_context(answer)
        except ConfigError as e:
            c.print(f"[red]{e}[/]")
            return
    apply_context(agent, size, hardware)


def sandbox_command(agent: Agent, arg: str) -> None:
    c, sandbox = agent.console, agent.sandbox
    if sandbox is None:
        c.print(
            "The sandbox is off: shell commands run directly on this machine. Start lcode with --sandbox, or "
            "turn it on for every session with: lcode config set sandbox docker"
        )
        return
    words = arg.split()
    if len(words) == 2 and words[0] == "network" and words[1] in ("on", "off"):
        sandbox.network = words[1] == "on"
        sandbox.stop()  # the next command starts a container with the new setting
        c.print(f"Network access in the sandbox is {words[1]} from the next command.")
        return
    if arg:
        c.print("[yellow]Use /sandbox or /sandbox network on|off.[/]")
        return
    where = f" · container {sandbox.container}, mounting {sandbox.root}" if sandbox.container else ""
    c.print(f"Sandbox: {escape(sandbox.describe())}{escape(where)}")
    c.print(
        "[dim]Shell commands run in the container and can only see this project; file tools are limited to it "
        "too. In auto-edit mode, commands run without asking.[/]"
    )


def mcp_command(agent: Agent, arg: str) -> None:
    from lcode.mcp.commands import status_table
    from lcode.mcp.protocol import McpError

    c, mcp = agent.console, agent.mcp
    if mcp is None:
        c.print(
            "No MCP servers in this session. Add one with [bold]lcode mcp add[/] (see [bold]lcode mcp catalog[/]) "
            "and start a new session."
        )
        return
    action, _, name = arg.partition(" ")
    name = name.strip()
    if action in ("tools", "login", "restart") and name not in mcp.servers:
        c.print(f"[yellow]Which server? One of: {escape(', '.join(mcp.servers))}[/]")
        return
    if mcp.pending:
        with c.status("Waiting for MCP servers to start…"):
            mcp.wait()
    if action == "tools":
        state = mcp.servers[name]
        if state.status != "ready":
            c.print(f"{escape(name)}: {escape(state.error or state.status)}")
        for tool in state.tools:
            first = (tool.get("description") or "").strip().split("\n")[0]
            c.print(f"  [cyan]{escape(tool['name'])}[/] [dim]{escape(first[:100])}[/]")
        return
    try:
        if action == "login":
            mcp.login(name, notify=lambda text: c.print(escape(text)))
        elif action == "restart":
            with c.status(f"Restarting {name}…"):
                mcp.restart(name)
        elif action:
            c.print("[yellow]Use /mcp, /mcp tools NAME, /mcp login NAME or /mcp restart NAME.[/]")
            return
    except McpError as e:
        c.print(f"[red]{escape(str(e))}[/]")
    c.print(status_table(mcp, agent.settings.context))
    if action in ("login", "restart"):
        agent.prepare_mcp()  # the model sees the server's tools from the next request


def ask_yes(question: str) -> bool:
    try:
        return input(f"  {question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def checkpoints_off(agent: Agent) -> bool:
    if agent.checkpoints.enabled:
        return False
    agent.console.print("Checkpoints are off. Turn them on with: lcode config set checkpoints true")
    return True


def print_checkpoints(agent: Agent) -> None:
    c = agent.console
    if checkpoints_off(agent):
        return
    items = agent.checkpoints.items
    if not items:
        c.print(
            "No checkpoints yet. Before the model first changes files in a request, lcode saves a checkpoint; "
            "/undo restores it."
        )
        return
    table = Table(title="Checkpoints", title_justify="left", header_style="bold")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("When")
    table.add_column("Request")
    table.add_column("Files changed")
    for cp in items:
        files = ", ".join(escape(p) for p in cp.paths[:3]) + (f", +{len(cp.paths) - 3}" if len(cp.paths) > 3 else "")
        style = "dim strike" if cp.undone else ""
        table.add_row(
            str(cp.n),
            sessions.age(cp.created),
            escape(cp.title),
            files + (" [dim](undone)[/]" if cp.undone else ""),
            style=style,
        )
    c.print(table)
    c.print("[dim]/undo reverts the latest request; /rewind <number> goes back to before that request.[/]")


def undo_command(agent: Agent) -> None:
    if checkpoints_off(agent):
        return
    active = agent.checkpoints.active()
    if not active:
        agent.console.print("Nothing to undo: no request in this session has changed files (or they're all undone).")
        return
    restore_checkpoint(agent, active[-1], rewind=False)


def rewind_command(agent: Agent, arg: str) -> None:
    c = agent.console
    if checkpoints_off(agent):
        return
    if not arg:
        print_checkpoints(agent)
        if not agent.checkpoints.active():
            return
        try:
            arg = input("  Go back to before which request? (number, Enter to cancel): ").strip()
        except EOFError:
            return
        if not arg:
            return
    target = agent.checkpoints.find(int(arg)) if arg.isdigit() else None
    if target is None:
        c.print(f"[yellow]No checkpoint {escape(arg)}. /checkpoints lists them.[/]")
    elif target.undone:
        c.print(f"Checkpoint {target.n} is already undone.")
    else:
        restore_checkpoint(agent, target, rewind=True)


def file_summary(plan: Restore, limit: int = 30) -> str:
    parts = []
    for verb, paths in (("restored", plan.restore), ("removed", plan.remove)):
        if paths:
            shown = ", ".join(paths[:limit]) + (f" and {len(paths) - limit} more" if len(paths) > limit else "")
            parts.append(f"{verb} {shown}")
    return "; ".join(parts)


def restore_checkpoint(agent: Agent, target: Checkpoint, rewind: bool) -> None:
    """Put the files back to how they were before `target`'s request (and every later one)."""
    c, cps = agent.console, agent.checkpoints
    try:
        plan = cps.plan(target)
    except CheckpointError as e:
        c.print(f"[red]Can't read checkpoint {target.n}: {escape(str(e))}[/]")
        return
    later = len(plan.undoing) - 1
    also = f" and {later} later request{'' if later == 1 else 's'}" if later > 0 else ""
    action = "Going back to before" if rewind else "Undoing"
    c.print(f"{action} request {target.n}{also}: [bold]{escape(target.title)}[/]")
    for path in plan.restore[:20]:
        c.print(f"  [green]restore[/] {escape(path)}")
    for path in plan.remove[:20]:
        c.print(f"  [red]remove[/]  {escape(path)}")
    hidden = max(0, len(plan.restore) - 20) + max(0, len(plan.remove) - 20)
    if hidden:
        c.print(f"  … and {hidden} more")
    if plan.conflicts:
        c.print("[yellow]These files changed again after lcode changed them; going back loses those later edits:[/]")
        for path in plan.conflicts[:20]:
            c.print(f"  [yellow]{escape(path)}[/]")
        if not ask_yes("Go back anyway?"):
            c.print("Nothing changed.")
            return
    elif rewind and (plan.restore or plan.remove) and not ask_yes("Restore these files?"):
        c.print("Nothing changed.")
        return
    index = target.message_index
    truncate = (
        rewind
        and 0 < index < len(agent.messages)
        and agent.messages[index].get("role") == "user"
        and agent.messages[index].get("content", "").startswith(target.request)
        and ask_yes("Also remove those requests and answers from the conversation?")
    )
    try:
        cps.apply(plan)
    except CheckpointError as e:
        c.print(f"[red]Couldn't restore the files: {escape(str(e))}[/]")
        return
    for path in cps.absolute_paths(plan):
        agent.tools.read_mtimes.pop(path, None)  # the model must read restored files again before editing
    if not (plan.restore or plan.remove):
        c.print("The files were already back to how they were before that request.")
    else:
        c.print(f"[green]Done:[/] {escape(file_summary(plan, limit=8))}.")
    if truncate:
        agent.messages = agent.messages[:index]
        agent.ctx_used = sum(len(json.dumps(m)) for m in agent.messages) // 3
        c.print(f"The conversation is back to before request {target.n} as well.")
    elif plan.restore or plan.remove:
        what = "undid the file changes from" if not rewind else "rolled the files back to before"
        agent.messages.append(
            {
                "role": "user",
                "content": f'[lcode] The user {what} their request "{target.title}": {file_summary(plan)}. '
                "Those files are back to how they were before that request, so read them again before "
                "changing them.",
            }
        )
    agent.save()


def apply_context(agent: Agent, size: int, hardware: Hardware) -> None:
    c, s = agent.console, agent.settings
    if size == s.context:
        c.print(f"The context window is already {format_tokens(size)}.")
        return
    if agent.has_conversation() and agent.ctx_used > AUTO_COMPACT_RATIO * size:
        c.print(
            f"This conversation uses {format_tokens(agent.ctx_used)} tokens, too much for {format_tokens(size)}; "
            "summarizing it first."
        )
        try:
            agent.compact()
        except OllamaError as e:
            c.print(f"[red]Couldn't summarize the conversation, so the context stays the same: {e}[/]")
            return
    c.print(agent.set_context(size))
    spec = catalog.find(s.model)
    if spec and spec.memory_gib(s.context) > hardware.budget_gib:
        c.print(
            f"[yellow]~{spec.memory_gib(s.context):.0f} GB needed but ~{hardware.budget_gib:.0f} GB available; "
            "this may run out of memory, in which case lcode falls back to a smaller size.[/]"
        )


def web_status(agent: Agent) -> str:
    if agent.settings.web == "off":
        return "off"
    backend = agent.search_backend()
    search = f"search via {web.BACKEND_NAMES[backend]}" if backend else "no search provider (see lcode doctor)"
    return f"{agent.settings.web} · {search}"


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
        rows = "\n".join(f"  [cyan]{k:<13}[/] {v}" for k, v in COMMANDS.items())
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
    elif cmd == "/undo":
        undo_command(agent)
    elif cmd == "/rewind":
        rewind_command(agent, arg)
    elif cmd == "/checkpoints":
        print_checkpoints(agent)
    elif cmd == "/mcp":
        mcp_command(agent, arg)
    elif cmd == "/sandbox":
        sandbox_command(agent, arg)
    elif cmd == "/compact":
        agent.compact(arg)
    elif cmd in ("/context", "/ctx"):
        context_command(agent, arg, hardware)
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
                s.context = limits.cap(name, spec.fit(hardware)[0] or MIN_USEFUL_CONTEXT)
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
