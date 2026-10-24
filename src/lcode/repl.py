"""The interactive prompt: input handling, slash commands and the status bar."""

from __future__ import annotations

import html
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Group
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from lcode import (
    __version__,
    backends,
    catalog,
    clipboard,
    escalate,
    extensions,
    gitflow,
    limits,
    media,
    modes,
    residency,
    sessions,
    web,
)
from lcode import context as context_tools
from lcode import memory as memory_notes
from lcode.agent import AUTO_COMPACT_RATIO, INIT_PROMPT, Agent
from lcode.catalog import MIN_USEFUL_CONTEXT
from lcode.checkpoints import Checkpoint, CheckpointError, Restore
from lcode.cloud import is_cloud
from lcode.cloud import notice as cloud_notice
from lcode.config import PERMISSION_MODES, STATE_DIR, ConfigError, format_tokens, parse_context
from lcode.hardware import Hardware
from lcode.ollama import OllamaError
from lcode.steer import KeyReader, Steering
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
    "/agents": "Subagent types and the subagents this session ran: /agents, /agents <number> for a transcript",
    "/remember": "Save a note for later sessions, e.g. /remember use pnpm, not npm (-g: for every repository)",
    "/memory": "Notes lcode remembers: /memory, /memory show|edit|delete <number or name>, /memory path",
    "/sandbox": "Shell-command sandbox: status, or /sandbox network on|off",
    "/mcp": "MCP servers and their tools: /mcp, /mcp tools NAME, /mcp login NAME, /mcp restart NAME",
    "/compact": "Summarize the conversation to free context (optional: what to focus on)",
    "/context": "Show context usage and change the window size: pick from a list, or /context 128k",
    "/ctx": "Shortcut for /context",
    "/model": "Switch model: pick a number from the list, or /model 2, /model qwen3.5-9b",
    "/models": "List models and how they fit this machine; type a number to switch",
    "/think": "Toggle model reasoning on/off",
    "/verbose": "Toggle showing the model's reasoning text",
    "/plan": "Plan before changing anything: /plan <request>, or /plan to show the approved plan",
    "/mode": "What lcode makes: code, image or video, e.g. /mode image",
    "/permissions": "Permission mode: ask | plan | auto-edit | yolo (Shift+Tab cycles)",
    "/options": "Image mode's options for this session, e.g. /options n=4 ar=16:9 quality=best style=photo",
    "/again": "Image mode: the last request again, with new seeds (flags work: /again --n 4)",
    "/vary": "Image mode: variations of an image, new seeds, e.g. /vary 3 (default: the last; --n 2)",
    "/gallery": "Image mode: this session's images with their settings (/gallery all: every image here)",
    "/open": "Open an image in your viewer, e.g. /open 3 (default: the last)",
    "/edit": "Image mode: change an image by instruction, e.g. /edit 3 make the sky purple (@photo.png: references)",
    "/use": "Image mode: images the next requests follow, e.g. /use 3 or /use photo.png (/use off)",
    "/extend": "Outpaint an image (/extend 3 --ar 16:9), or continue a clip in video mode (/extend 4 --seconds 3)",
    "/upscale": "2 or 4 times the pixels of an image or a clip, e.g. /upscale 3 4",
    "/nobg": "Image mode: remove the background (a transparent PNG), e.g. /nobg 3",
    "/animate": "Image mode: make a video starting from an image, e.g. /animate 3 the waves roll in (video mode)",
    "/smooth": "Video mode: 2 or 4 times the frames (smoother motion), e.g. /smooth 4",
    "/sound": "Video mode: add sound to a clip, e.g. /sound 4 waves and seagulls",
    "/restyle": "Video mode: the same clip in a new look, e.g. /restyle 4 as a watercolor painting",
    "/play": "Play a video in your player, e.g. /play 4 (default: the last)",
    "/cd": "Change the working directory",
    "/todos": "Show the current todo list",
    "/status": "The task sheet: your requests, the plan and notes, files changed, the last check",
    "/escalate": "Ask the stronger model (escalate_model) for advice, with your OK: /escalate [what's wrong]",
    "/jobs": "Background commands the model started: /jobs, /jobs stop <id>",
    "/commit": "Commit the changes with a message in the repository's style, after you approve it",
    "/review": "Review the uncommitted changes, or the branch against a base: /review [base]",
    "/pr": "Push the branch and open a GitHub pull request, after you approve it: /pr [base] [notes]",
    "/exit": "Quit",
}
REPLACEABLE = {"/commit", "/review", "/pr"}  # a command of your own with the same name runs instead


class InputCompleter(Completer):
    """Completes slash commands and @file mentions."""

    def __init__(self, agent: Agent):
        self.agent = agent

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/") and " " not in text:
            for cmd, desc in {**COMMANDS, **custom_commands(self.agent)}.items():
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


def mention(path: Path) -> str:
    """How a file goes into the prompt: @path, with ~ for the home folder, in quotes if it has spaces."""
    home = Path.home()
    shown = f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)
    return f'@"{shown}"' if " " in shown else f"@{shown}"


def status_command(agent: Agent) -> None:
    """What the session is doing, as lcode keeps it for the model (the task sheet)."""
    c = agent.console
    requests = context_tools.user_requests(agent.messages)
    if not requests and not agent.tools.todos and not agent.tools.changed:
        c.print("Nothing yet: no requests in this session.")
        return
    if requests:
        latest = requests[-3:]
        lines = [f"[dim]{len(requests) - len(latest) + i + 1}.[/] {escape(r[:300])}" for i, r in enumerate(latest)]
        c.print(Panel("\n".join(lines), title="Requests (latest)", title_align="left", border_style="cyan"))
    if agent.tools.todos or agent.tools.notes:
        agent.tools.show_todos()
    if agent.tools.changed:
        c.print(f"[bold]Files changed:[/] {escape(', '.join(agent.tools.changed))}")
    if agent.check_results:
        last = agent.check_results[-1]
        code = last.get("exit_code")
        state = (
            "[green]passed[/]" if code == 0 else "didn't run" if code is None else f"[red]failed[/] (exit code {code})"
        )
        c.print(f"[bold]Last check:[/] {escape(str(last.get('command')))} {state}")
    c.print("[dim]The model keeps this too, even when old messages are removed or summarized.[/]")


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

    @kb.add("c-v")
    def _paste(event):
        """Paste the clipboard: an image becomes @path (terminals can't paste images), anything else is text."""
        got = clipboard.paste()
        buf = event.current_buffer
        if got.image:
            before = buf.document.char_before_cursor
            buf.insert_text(("" if not before or before.isspace() else " ") + mention(got.image) + " ")
        elif got.text:
            buf.insert_text(got.text.replace("\r\n", "\n"))
        else:
            run_in_terminal(lambda: agent.console.print(Text(f"  {got.problem}", style="yellow")))

    @kb.add("s-tab")
    def _cycle_mode(event):
        agent.perms.cycle()
        event.app.invalidate()

    def toolbar():
        s = agent.settings
        pct = 100 * agent.ctx_used / s.context
        color = {"ask": "ansigreen", "plan": "ansicyan", "auto-edit": "ansiyellow", "yolo": "ansired"}[agent.perms.mode]
        return HTML(
            f" <b>{html.escape(s.model)}</b> · ctx {format_tokens(agent.ctx_used)}/{format_tokens(s.context)} "
            f"({pct:.0f}%) · <b>{s.mode}</b> · <{color}>{agent.perms.mode}</{color}> (shift+tab) · "
            f"think {'on' if s.think else 'off'} · {html.escape(agent.cwd.name)}/"
            + (" · <ansiyellow>loading the model into memory…</ansiyellow>" if agent.load_state == "loading" else "")
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


def model_lines(agent: Agent) -> str:
    """The banner's model lines: the coding model and its context, or the mode's own model."""
    s = agent.settings
    if s.mode == "image":
        return f"[dim]model[/]    {escape(media.current(agent).name)} [dim]· ComfyUI · {escape(s.media_dir)}[/]\n"
    if s.mode == "video":
        return f"[dim]model[/]    {escape(media.current(agent).name)} [dim]· ComfyUI · {escape(s.media_dir)}[/]\n"
    server = f" [dim]· {escape(agent.ollama.describe())}[/]" if backends.is_openai(agent.ollama) else ""
    return f"[dim]model[/]    {escape(s.model)}{server}\n[dim]context[/]  {format_tokens(s.context)} tokens\n"


def banner(agent: Agent) -> None:
    s = agent.settings
    agent.console.print(
        Panel(
            Group(
                Text.from_markup(f"[bold cyan]lcode[/] [dim]v{__version__}[/] — {modes.TITLES[s.mode]}\n"),
                Text.from_markup(
                    model_lines(agent) + f"[dim]cwd[/]      {agent.cwd}\n"
                    f"[dim]mode[/]     {s.mode} [dim](/mode code, image or video)[/]\n"
                    f"[dim]perms[/]    {agent.perms.mode} [dim](Shift+Tab to cycle)[/]\n"
                    f"[dim]web[/]      {web_status(agent)}\n"
                    + (f"[dim]mcp[/]      {escape(', '.join(agent.mcp.servers))} [dim](/mcp)[/]\n" if agent.mcp else "")
                    + (
                        f"[dim]sandbox[/]  {escape(agent.sandbox.describe())} [dim](/sandbox)[/]\n"
                        if agent.sandbox
                        else ""
                    )
                    + (f"[dim]memory[/]   {memory_status(agent)}\n" if s.memory != "off" else "")
                    + "\n"
                    "[dim]/help for commands · @file to attach · Esc+Enter for a newline[/]"
                ),
            ),
            border_style="cyan",
            expand=False,
        )
    )


CONTEXT_CHOICES = (16384, 32768, 65536, 131072, 262144, 524288, 1048576)


def model_limit(agent: Agent) -> int | None:
    """The largest context the model supports (what Ollama reports, else the catalog's), if known."""
    spec = catalog.find(agent.settings.model)
    try:
        return agent.ollama.max_context(agent.settings.model) or (spec.max_context if spec else None)
    except OllamaError:
        return spec.max_context if spec else None


def context_options(agent: Agent, hardware: Hardware) -> list[dict]:
    """The context sizes this model supports, with how each one fits this machine."""
    s = agent.settings
    spec = catalog.find(s.model)
    max_ctx = model_limit(agent)
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
        f"{'' if len(agent.messages) == 1 else 's'}; makes room at {format_tokens(agent.compact_limit())} "
        + ("(compact_at)." if agent.compact_limit() < AUTO_COMPACT_RATIO * s.context else "(85%).")
    )
    if agent.mcp and agent.mcp.ready():
        how = "found on demand" if agent.mcp.searching(s.context) else "sent with every request"
        c.print(f"MCP tools: ~{format_tokens(agent.mcp.cost())} tokens of definitions, {how}.")
    if not arg:
        print_breakdown(agent)
    if not arg and backends.is_openai(agent.ollama):
        c.print(
            f"[dim]{agent.ollama.name} sets the context window when it loads the model; lcode uses "
            f"{format_tokens(s.context)} of it. To summarize sooner: /ctx 16k[/]"
        )
        return
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
    limit = model_limit(agent)
    beyond = [size for size in CONTEXT_CHOICES if limit and size > limit]
    if beyond:
        sizes = " and ".join(format_tokens(size) for size in beyond[:2])
        c.print(
            f"[dim]Not offered: {sizes}{' and more' if len(beyond) > 2 else ''}. {s.model} was trained on up to "
            f"{format_tokens(limit)} tokens, and Ollama caps the window there whatever the memory, so a larger "
            "setting would only look larger.[/]"
        )
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


def print_breakdown(agent: Agent) -> None:
    """What uses the context, by category."""
    parts = [p for p in context_tools.breakdown(agent) if p.tokens]
    total = sum(p.tokens for p in parts) or 1
    table = Table(title="What uses the context (estimates)", title_justify="left", header_style="bold")
    table.add_column("Part")
    table.add_column("Tokens", justify="right")
    table.add_column("", no_wrap=True)
    table.add_column("", style="dim")
    for part in parts:
        share = part.tokens / total
        bar = "█" * max(1, round(share * 20)) + f" {share:.0%}"
        table.add_row(escape(part.name), format_tokens(part.tokens), bar, escape(part.detail))
    agent.console.print(table)
    if agent.pruned or agent.compacted:
        agent.console.print(
            f"[dim]This session: old tool output removed {agent.pruned} time(s), conversation summarized "
            f"{agent.compacted} time(s).[/]"
        )


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


def agents_command(agent: Agent, arg: str) -> None:
    c = agent.console
    if not agent.settings.subagents:
        c.print("Subagents are off. Turn them on with: lcode config set subagents true")
        return
    runs = agent.agent_runs
    if arg:
        if not (arg.isdigit() and 1 <= int(arg) <= len(runs)):
            c.print(f"[yellow]No subagent {escape(arg)}. /agents lists them.[/]")
            return
        run = runs[int(arg) - 1]
        c.print(Rule(f"{run.kind} agent: {run.label}", style="dim"))
        for message in run.messages:
            role, content = message.get("role"), str(message.get("content") or "")
            if role == "user":
                c.print(Text(f"task: {content}", style="bold"))
            elif role == "tool":
                first = content.strip().splitlines()[0] if content.strip() else ""
                c.print(Text(f"  ⎿ {message.get('tool_name', '')}: {first[:150]}", style="dim"))
            elif role == "assistant":
                for call in message.get("tool_calls") or []:
                    fn = call.get("function", {})
                    c.print(
                        Text(f"● {fn.get('name', '')} {json.dumps(fn.get('arguments', {}))[:150]}", style="magenta")
                    )
                if content.strip():
                    c.print(Text(content.strip()))
        return
    table = Table(title="Agent types", title_justify="left", header_style="bold")
    table.add_column("Type", style="cyan")
    table.add_column("Does")
    table.add_column("From")
    for kind in agent.agent_types().values():
        source = kind.source if kind.source == "built-in" else agent.tools.rel(Path(kind.source))
        table.add_row(kind.name, escape(kind.description), escape(source))
    c.print(table)
    for problem in agent.agent_type_problems():
        c.print(f"[yellow]Skipped {escape(problem)}[/]")
    if runs:
        table = Table(title="Subagents in this session", title_justify="left", header_style="bold")
        table.add_column("#", justify="right", style="cyan")
        table.add_column("Agent")
        table.add_column("Task")
        table.add_column("Tools", justify="right")
        table.add_column("Time", justify="right")
        for i, run in enumerate(runs, 1):
            mark = {"done": "", "stopped": " [yellow](stopped)[/]"}.get(run.outcome, " [red](failed)[/]")
            table.add_row(str(i), run.kind, escape(run.label) + mark, str(run.tools), f"{run.seconds:.0f}s")
        c.print(table)
        c.print("[dim]/agents <number> shows what a subagent did.[/]")
    n = agent.settings.max_parallel_agents
    c.print(
        f"[dim]Up to {n} subagents run at the same time.[/]"
        if n > 1
        else "[dim]Subagents run one at a time (max_parallel_agents = 1).[/]"
    )


def custom_commands(agent: Agent) -> dict[str, str]:
    """The user's and the repository's commands and skills, as /name -> description (built-ins win)."""
    ext = agent.extensions()
    found = {f"/{name}": skill.description for name, skill in ext.skills.items()}
    found.update({f"/{name}": command.description for name, command in ext.commands.items()})
    return {
        name: description
        for name, description in found.items()
        if name not in COMMANDS or (name in REPLACEABLE and name[1:] in ext.commands)
    }


def help_extensions(agent: Agent) -> None:
    c, ext = agent.console, agent.extensions()
    if ext.commands:
        table = Table(title="Your commands", title_justify="left", header_style="bold")
        for column in ("Command", "Does", "From"):
            table.add_column(column)
        for command in ext.commands.values():
            hidden = ""
            if f"/{command.name}" in REPLACEABLE:
                hidden = " [dim](replaces the built-in command)[/]"
            elif f"/{command.name}" in COMMANDS:
                hidden = " [yellow](hidden by the built-in command)[/]"
            usage = f"/{command.name}" + (f" {command.argument_hint}" if command.argument_hint else "")
            table.add_row(escape(usage), escape(command.description) + hidden, escape(agent.tools.rel(command.path)))
        c.print(table)
    if ext.skills:
        table = Table(
            title="Skills (the model loads them when a task matches; /name runs one)",
            title_justify="left",
            header_style="bold",
        )
        for column in ("Skill", "Does", "From"):
            table.add_column(column)
        for skill in ext.skills.values():
            description = skill.description if len(skill.description) <= 140 else skill.description[:139] + "…"
            if f"/{skill.name}" in COMMANDS or skill.name in ext.commands:
                description += f" [yellow](/{skill.name} runs the command; the model can still load the skill)[/]"
            table.add_row(f"/{escape(skill.name)}", escape(description), escape(agent.tools.rel(skill.folder)))
        c.print(table)
    for problem in ext.problems:
        c.print(f"[yellow]Skipped {escape(problem)}[/]")
    if not agent.settings.trust_project and extensions.status(agent.cwd) in ("new", "changed"):
        c.print(
            "[dim]This repository's own commands, skills and agents aren't in use: you didn't approve them "
            "(restart lcode here to be asked again).[/]"
        )


def memory_status(agent: Agent) -> str:
    project, user = agent.memory().counts()
    if not (project or user):
        return f"{agent.settings.memory} · no notes yet [dim](/remember)[/]"
    parts = [f"{project} for this repository"] if project else []
    parts += [f"{user} for every repository"] if user else []
    total = project + user
    return f"{agent.settings.memory} · {total} note{'' if total == 1 else 's'}: {', '.join(parts)} [dim](/memory)[/]"


def memory_off(agent: Agent) -> bool:
    if agent.settings.memory != "off":
        return False
    agent.console.print(
        "Memory is off in this session. Turn it on with: lcode config set memory ask (or drop --no-memory)"
    )
    return True


def remember_command(agent: Agent, arg: str) -> None:
    """/remember [-g] [type:] text — save a note the user writes, without asking the model."""
    c = agent.console
    if memory_off(agent):
        return
    words = arg.split()
    scope = "project"
    if words and words[0] in ("-g", "--global", "--user"):
        scope, words = "user", words[1:]
    text = " ".join(words)
    if not text:
        c.print(
            "Usage: /remember <fact>, e.g. /remember the staging database needs the VPN. "
            "Add -g for a note for every repository, and start with feedback:, reference: or user: to set its type."
        )
        return
    kind = "user" if scope == "user" else "project"
    m = re.match(r"(feedback|project|reference|user):\s*(.*)", text, re.I | re.S)
    if m:
        kind, text = m.group(1).lower(), m.group(2)
    description, details = text, ""
    if len(text) > memory_notes.MAX_DESCRIPTION:
        description, details = text[: memory_notes.MAX_DESCRIPTION - 1].rstrip() + "…", text
    try:
        note = memory_notes.make_note(memory_notes.slugify(description), kind, description, details, scope)
    except memory_notes.NoteError as e:
        c.print(f"[red]{escape(str(e))}[/]")
        return
    saved, updated = agent.memory().save(note)
    where = "every repository" if scope == "user" else "this repository"
    c.print(f"[green]{'Updated' if updated else 'Remembered'} for {where}:[/] {escape(saved.description)}")
    agent.messages.append(
        {"role": "user", "content": f"[lcode] The user saved a note that later sessions will see: {saved.description}"}
    )
    agent.save()


def memory_command(agent: Agent, arg: str) -> None:
    c = agent.console
    if memory_off(agent):
        return
    memory = agent.memory()
    notes = memory.notes()
    action, _, target = arg.partition(" ")
    target = target.strip()
    if action == "path":
        c.print(f"For this repository: {memory.project}\nFor every repository: {memory.user}")
        return
    if action in ("", "list"):
        if not notes:
            c.print(
                "No notes yet. lcode saves what later sessions should know: the model with its memory tool, you "
                "with /remember, and a short check when a session ends."
            )
            return
        table = Table(title="Memory", title_justify="left", header_style="bold")
        table.add_column("#", justify="right", style="cyan")
        table.add_column("For")
        table.add_column("Type")
        table.add_column("Note")
        table.add_column("Updated", no_wrap=True)
        for i, note in enumerate(notes, 1):
            table.add_row(
                str(i),
                "every repo" if note.scope == "user" else "this repo",
                note.type,
                f"{escape(note.description)} [dim]{escape(note.name)}[/]",
                note.modified,
            )
        c.print(table)
        c.print("[dim]/memory show|edit|delete <number or name> · the model sees these at the start of a session[/]")
        return
    if action not in ("show", "edit", "delete", "forget"):
        c.print("[yellow]Use /memory, /memory show|edit|delete <number or name> or /memory path.[/]")
        return
    note = notes[int(target) - 1] if target.isdigit() and 1 <= int(target) <= len(notes) else None
    note = note or (memory.find(target) if target else None)
    if note is None or note.path is None:
        c.print(f"[yellow]No note {escape(target)!r}. /memory lists them.[/]" if target else "[yellow]Which note?[/]")
        return
    if action == "show":
        where = "every repository" if note.scope == "user" else "this repository"
        c.print(
            Panel(
                escape(note.render()), title=f"{escape(note.name)} · {where}", title_align="left", border_style="blue"
            )
        )
        c.print(f"[dim]{escape(str(note.path))}[/]")
    elif action == "edit":
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("nano" if shutil.which("nano") else "vi")
        try:
            subprocess.call([*shlex.split(editor), str(note.path)])
        except OSError as e:
            c.print(f"[red]Can't start the editor {escape(editor)}: {escape(str(e))}[/]")
            return
        if memory_notes.parse(note.path.read_text(errors="replace"), note.scope, note.path) is None:
            c.print(f"[yellow]{escape(str(note.path))} no longer has a name and description, so lcode ignores it.[/]")
        memory.write_index(note.scope)
        c.print("[dim]Saved. The model sees the change from the next session.[/]")
    elif ask_yes(f"Delete the note '{note.description}'?"):
        memory.delete(note)
        c.print(f"Deleted {escape(note.name)}. [dim]The model sees the change from the next session.[/]")


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
        agent.reflected = min(agent.reflected, index)
        agent.ctx_used = sum(len(json.dumps(m)) for m in agent.messages) // 3
        agent.counted_chars = None
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


def mode_command(agent: Agent, arg: str) -> None:
    """/mode: show the mode, or switch to code, image or video (handing the GPU over)."""
    c = agent.console
    if not arg:
        c.print(
            f"Mode: [bold]{agent.settings.mode}[/] ({modes.TITLES[agent.settings.mode]}). Switch with /mode code, "
            "/mode image or /mode video. The permission mode is /permissions."
        )
        return
    if arg in PERMISSION_MODES:  # what /mode used to set
        agent.perms.mode = arg
        c.print(f"Permission mode: {arg}. [dim](/permissions sets it now; /mode switches code, image and video.)[/]")
        return
    if arg not in modes.MODES:
        c.print(f"[red]Unknown mode {escape(arg)!r}: choose code, image or video.[/]")
        return
    lines = modes.switch(agent, arg)
    c.print(f"[green]{escape(lines[0])}[/]")
    for line in lines[1:]:
        c.print(f"[dim]{escape(line)}[/]")
    if arg == "code":
        preload(agent)  # the coding model loads in the background, as at the start


def model_command(agent: Agent, arg: str, hardware: Hardware) -> None:
    """/models and /model: the numbered list to pick from, or straight to a number or a name (/model 2)."""
    from lcode.cli import model_choices, print_models

    c, s = agent.console, agent.settings
    if arg and not arg.isdigit():
        switch_model(agent, arg, hardware)
        return
    if arg:
        choices = model_choices(agent.ollama)
    else:
        choices = print_models(agent.ollama, hardware, s.model, numbered=True, out=c)
        if not choices:
            return
        try:
            answer = input(f"  Switch to which model? (number or name, Enter to keep {s.model}): ").strip()
        except EOFError:
            return
        if not answer:
            c.print(f"Keeping {s.model}.")
            return
        arg = answer
    if arg.isdigit():
        if not 1 <= int(arg) <= len(choices):
            c.print(f"[red]There's no model {arg} in the list: choose 1 to {len(choices)}.[/]")
            return
        arg = choices[int(arg) - 1]
    switch_model(agent, arg, hardware)


def media_model_command(agent: Agent, arg: str, hardware: Hardware) -> None:
    """/models and /model in image or video mode: the catalog's models for the mode, numbered; pick one."""
    from lcode import media_catalog as catalog

    c, s = agent.console, agent.settings
    choices = catalog.load().for_mode(s.mode)
    if not choices:
        c.print(f"[yellow]{escape(media.SOON.get(s.mode, modes.ready(agent)))}[/]")
        return
    current = media.current(agent)
    installed = media.installed_lister(agent)
    if not arg:
        for i, m in enumerate(choices, 1):
            mark = "[green]●[/]" if m.key == current.key else " "
            c.print(
                f"  {i}. {mark} [bold]{escape(m.name)}[/] [dim]({m.key})[/]  "
                f"{escape(catalog.describe(m, hardware, installed))}"
            )
            if m.measured:
                c.print(f"        [dim]{escape(m.measured)}[/]")
        try:
            arg = input(f"  Switch to which model? (number or name, Enter to keep {current.name}): ").strip()
        except EOFError:
            return
        if not arg:
            return
    model = catalog.load().find(arg, s.mode)
    if model is None:
        c.print(f"[red]There's no {s.mode} model {escape(arg)!r}: choose 1 to {len(choices)}, or a name.[/]")
        return
    setattr(s, f"{s.mode}_model", model.key)
    c.print(f"[green]{s.mode.capitalize()} model: {escape(model.name)}.[/]")
    if installed is not None:
        _, missing = catalog.chosen_files(model, installed)
        if missing:
            size = sum(f.gb for f in missing)
            c.print(f"[dim]Not installed yet: {size:.1f} GB download with your next {s.mode} (you're asked first).[/]")


def switch_model(agent: Agent, wanted: str, hardware: Hardware) -> None:
    """Switch to a catalog key or an installed model, and size the context for it."""
    from lcode.cli import NotInstalled, resolve_model

    c, s = agent.console, agent.settings
    try:
        name, spec = resolve_model(agent.ollama, wanted)
    except (NotInstalled, OllamaError) as e:
        c.print(f"[red]{e}[/]")
        return
    if name == s.model:
        c.print(f"Already using {name} (context {format_tokens(s.context)}).")
        return
    s.model = name
    s.num_batch = spec.num_batch if spec and name == spec.local_name else None
    if spec:  # size the context for the new model: largest window that fits this machine
        s.context = limits.cap(name, spec.fit(hardware)[0] or MIN_USEFUL_CONTEXT)
    elif backends.is_openai(agent.ollama):
        s.context = backends.context_for(agent.ollama, name, None)[0]  # the server's window
    agent._vision = False  # the model that looks at images goes with the model: decide again
    agent.messages[0]["content"] = agent.system_prompt()
    c.print(f"[green]Switched to {name}[/] (context {format_tokens(s.context)}).")
    if is_cloud(name):
        agent.say(cloud_notice(name))
    warning = residency.dense_warning(agent.ollama, name, s.context, hardware)
    if warning:
        agent.say(warning, warning=True)


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


def jobs_command(agent: Agent, arg: str) -> None:
    """List the background commands, or stop one: /jobs stop <id>."""
    from lcode.jobs import JobError, describe

    c = agent.console
    word, _, job_id = arg.partition(" ")
    if word == "stop":
        try:
            job = agent.jobs.stop(job_id or "?")
        except JobError as e:
            c.print(f"[red]{escape(str(e))}[/]")
            return
        c.print(f"Stopped {escape(describe(job))}")
        return
    if not agent.jobs.items:
        c.print("No background commands. The model starts them with bash(background=true), e.g. a dev server.")
        return
    for job in agent.jobs.items.values():
        style = "green" if job.running else "dim"
        c.print(f"[{style}]{escape(describe(job))}[/]")
    if agent.jobs.running():
        c.print("[dim]/jobs stop <id> stops one; they all stop when the session ends.[/]")


def git_command(agent: Agent, cmd: str, arg: str) -> None:
    """/commit, /review and /pr (a command of your own with the same name replaces them)."""
    c = agent.console
    if cmd == "/commit":
        gitflow.commit_command(agent, arg)
    elif cmd == "/pr":
        gitflow.pr_command(agent, arg)
    else:
        try:
            with c.status("Collecting the changes…", spinner="dots"):
                prompt = gitflow.review_prompt(agent, arg)
        except gitflow.GitError as e:
            c.print(f"[yellow]{escape(str(e))}[/]")
            return
        agent.no_changes = "This is a review, so nothing can be changed: report what should change instead."
        try:
            run_safely(agent, prompt)
        finally:
            agent.no_changes = ""


def run_safely(agent: Agent, text: str) -> None:
    started = time.monotonic()
    reader = start_steering(agent) if agent.interactive else None
    try:
        agent.run_turn(text)
    except KeyboardInterrupt:
        pass
    except OllamaError as e:
        agent.console.print(f"[red]{e}[/]")
    finally:
        if reader is not None:
            stop_steering(agent, reader)
        agent.save()
        agent.finished(text, time.monotonic() - started)


def start_steering(agent: Agent) -> KeyReader | None:
    """Read the keyboard while the request runs: a typed line becomes a note for the model, Esc pauses."""
    steering = Steering()
    reader = KeyReader(steering)
    if not reader.start():
        return None  # not a terminal lcode can read keys from (or Windows): the request runs as before
    agent.steering = steering
    agent.perms.read_line = steering.ask  # questions take the keyboard back meanwhile
    return reader


def stop_steering(agent: Agent, reader: KeyReader) -> None:
    reader.stop()
    agent.steering = None
    agent.perms.read_line = None
    unsent = reader.steering.leftover()
    if unsent:
        agent.unsent = unsent
        agent.console.print(Text("✎ What you typed didn't reach the model; it's in the prompt below.", style="cyan"))


def escalate_command(agent: Agent, arg: str) -> None:
    """Ask the stronger model about the work so far, then let the model carry on with its advice."""
    if agent.planning():
        agent.console.print("[yellow]Leave plan mode first (Shift+Tab): the advice is for changing the code.[/]")
        return
    if not agent.can_escalate():
        agent.console.print(
            "[yellow]No stronger model is set.[/] Set one, for example:\n"
            "  lcode config set escalate_model anthropic/<model>   (with ANTHROPIC_API_KEY in your environment)\n"
            "  lcode config set escalate_model ollama/qwen3.8-27b  (a bigger local model)\n"
            "See https://nasser1941.github.io/lcode/usage/#a-stronger-model-when-stuck"
        )
        return
    if not context_tools.user_requests(agent.messages):
        agent.console.print("[dim]Nothing to ask about yet: make a request first.[/]")
        return
    why = f"the user says: {arg}" if arg else "the user asked for a second opinion on the work so far"
    advice = escalate.run(agent, why)
    if advice:
        run_safely(agent, f"{arg}\n\n{advice}" if arg else advice)


def session_start(agent: Agent) -> None:
    """session_start hooks: their output is shown, and with feedback = true also given to the model."""
    for outcome in agent.hooks.run("session_start", {"cwd": str(agent.cwd), "session": agent.session_id}, agent.cwd):
        if outcome.output:
            agent.console.print(Text(f"session_start hook: {outcome.output[:1000]}", style="dim"))
        if outcome.hook.feedback and outcome.output:
            agent.messages[0]["content"] += f"\n# From a session_start hook\n{outcome.output}\n"


def handle_command(agent: Agent, line: str, hardware: Hardware) -> bool:
    """Run a slash command. Returns False when the REPL should exit."""
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
        help_extensions(agent)
    elif cmd == "/clear":
        memory_notes.reflect(agent)
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
    elif cmd == "/agents":
        agents_command(agent, arg)
    elif cmd == "/remember":
        remember_command(agent, arg)
    elif cmd == "/memory":
        memory_command(agent, arg)
    elif cmd == "/mcp":
        mcp_command(agent, arg)
    elif cmd == "/sandbox":
        sandbox_command(agent, arg)
    elif cmd == "/compact":
        agent.compact(arg)
    elif cmd in ("/context", "/ctx"):
        context_command(agent, arg, hardware)
    elif cmd in ("/models", "/model"):
        if s.mode != "code":
            media_model_command(agent, arg, hardware)
        else:
            model_command(agent, arg, hardware)
    elif cmd == "/think":
        s.think = (arg == "on") if arg in ("on", "off") else not s.think
        c.print(f"Reasoning {'on' if s.think else 'off'}.")
    elif cmd == "/verbose":
        s.show_thinking = not s.show_thinking
        c.print(f"Showing reasoning text: {'on' if s.show_thinking else 'off'}.")
    elif cmd == "/plan":
        if not arg:
            if agent.plan:
                c.print(Panel(Markdown(agent.plan), title="Approved plan", title_align="left", border_style="cyan"))
            else:
                c.print(
                    "No approved plan in this session. [bold]/plan <request>[/] works out a plan first, without "
                    "changing anything (or Shift+Tab to plan mode)."
                )
            return True
        agent.perms.mode = "plan"
        c.print("[cyan]Plan mode:[/] the model can look around but not change anything until you approve its plan.")
        c.print(Rule(style="dim"))
        run_safely(agent, arg)
    elif cmd == "/mode":
        mode_command(agent, arg.strip().lower())
    elif cmd in IMAGE_COMMANDS:
        image_command(agent, cmd, arg)
    elif cmd == "/permissions":
        if arg in PERMISSION_MODES:
            agent.perms.mode = arg
        elif arg:
            c.print(f"[red]Unknown permission mode; choose from {', '.join(PERMISSION_MODES)}[/]")
        c.print(f"Permission mode: {agent.perms.mode}")
    elif cmd == "/cd":
        p = Path(os.path.expanduser(arg or "~"))
        p = (p if p.is_absolute() else agent.cwd / p).resolve()
        if p.is_dir():
            agent.cwd = agent.workspace = p
            agent.messages[0]["content"] = agent.system_prompt()
            c.print(f"Working directory: {p}")
        else:
            c.print(f"[red]Not a directory: {p}[/]")
    elif cmd == "/status":
        status_command(agent)
    elif cmd == "/todos":
        agent.tools.show_todos()
    elif cmd == "/jobs":
        jobs_command(agent, arg)
    elif cmd == "/init":
        run_safely(agent, INIT_PROMPT)
    elif cmd == "/escalate":
        escalate_command(agent, arg)
    elif cmd in REPLACEABLE and cmd[1:] not in agent.extensions().commands:
        git_command(agent, cmd, arg)
    elif cmd[1:] in agent.extensions().commands:
        command = agent.extensions().commands[cmd[1:]]
        c.print(f"[dim]/{escape(command.name)} from {escape(agent.tools.rel(command.path))}[/]")
        c.print(Rule(style="dim"))
        previous = agent.allowed_tools
        if command.tools is not None:
            agent.allowed_tools = set(command.tools)
        try:
            run_safely(agent, command.render(arg))
        finally:
            agent.allowed_tools = previous
    elif cmd[1:] in agent.extensions().skills:
        skill = agent.extensions().skills[cmd[1:]]
        agent.skills_loaded.add(skill.name)
        c.print(f"[dim]Using the {escape(skill.name)} skill from {escape(agent.tools.rel(skill.folder))}[/]")
        c.print(Rule(style="dim"))
        run_safely(agent, f"{arg or f'Use the {skill.name} skill.'}\n\n{extensions.content(skill)}")
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
    requests = context_tools.user_requests(agent.messages)  # the user's own words, not lcode's notes to the model
    last_answer = next(
        (m for m in reversed(agent.messages) if m.get("role") == "assistant" and m.get("content", "").strip()), None
    )
    if not requests:
        return
    lines = [f"[bold]You:[/] {escape(sessions.title_from([{'role': 'user', 'content': requests[-1]}]))}"]
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
    session_start(agent)
    if prompt and agent.settings.mode in ("image", "video"):
        respond(agent, prompt)
        return
    if prompt:
        run_safely(agent, prompt)
        return
    banner(agent)
    if agent.settings.mode == "code" and is_cloud(agent.settings.model):
        agent.say(cloud_notice(agent.settings.model))
    if agent.settings.mode == "code":
        warning = residency.dense_warning(agent.ollama, agent.settings.model, agent.settings.context, hardware)
        if warning:
            agent.say(warning, warning=True)
    session = build_session(agent)
    if agent.settings.mode == "code":
        preload(agent, session)
    else:
        warming = media.warm(agent)
        if warming:
            agent.say(warming)
        agent.console.print(f"[yellow]{escape(modes.ready(agent))}[/]")
    while True:
        try:
            agent.console.print()
            unsent, agent.unsent = agent.unsent, ""
            marker = "❯ " if agent.settings.mode == "code" else f"{agent.settings.mode} ❯ "
            line = session.prompt(HTML(f"<ansicyan><b>{marker}</b></ansicyan>"), default=unsent).strip()
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
        respond(agent, line)
    memory_notes.reflect(agent)
    agent.console.print("[dim]Bye.[/]")


def respond(agent: Agent, line: str) -> None:
    """What a typed request does: the coding agent works on it, or image and video mode take it."""
    if agent.settings.mode in ("image", "video"):  # no coding model involved
        make_image(agent, line)
        return
    agent.console.print(Rule(style="dim"))
    run_safely(agent, line)


IMAGE_COMMANDS = (
    "/options", "/again", "/vary", "/gallery", "/open", "/play", "/edit", "/use", "/extend", "/upscale", "/nobg",
    "/animate", "/smooth", "/sound", "/restyle",
)  # fmt: skip
VIDEO_TOO = (
    "/options", "/again", "/vary", "/gallery", "/open", "/play", "/use", "/extend", "/upscale", "/smooth", "/sound",
    "/restyle",
)  # what video mode has  # fmt: skip
NUMBER = re.compile(r"^#?(\d+)$")


def image_command(agent: Agent, cmd: str, arg: str) -> None:
    """Image mode's commands: /options, follow-ups on results (/again, /vary, /edit, /extend, /upscale, /nobg),
    references (/use), /gallery and /open."""
    from dataclasses import replace

    from lcode import edits, imageopts

    c = agent.console
    video = agent.settings.mode == "video"
    if cmd == "/options":
        if not arg.strip():
            c.print(f"Options: [bold]{escape(agent.image_options.describe(video))}[/]")
            c.print(
                '[dim]Set them with /options n=4 ar=16:9 seed=42 quality=best style=photo no="text"; '
                f"styles: {', '.join(imageopts.STYLES)}. Flags in a request (--n 4) win for that request.[/]"
            )
            return
        try:
            agent.image_options = imageopts.set_values(agent.image_options, imageopts.parse_assignments(arg))
        except imageopts.OptionError as e:
            c.print(f"[red]{escape(str(e))}[/]")
            return
        c.print(f"Options: [bold]{escape(agent.image_options.describe(video))}[/]")
        return
    if cmd == "/gallery":
        gallery(agent, everything=arg.strip() == "all")
        return
    if cmd not in ("/open", "/play") and not (agent.settings.mode == "image" or (video and cmd in VIDEO_TOO)):
        where = "image or video mode" if cmd in VIDEO_TOO else "image mode"
        c.print(f"[yellow]{cmd} works in {where}: /mode {'image' if where == 'image mode' else 'video'}.[/]")
        return
    first, _, rest = arg.strip().partition(" ")
    number = first if NUMBER.match(first) else ""
    rest = rest if number else arg.strip()
    try:
        if cmd == "/use":
            use_references(agent, arg.strip())
            return
        if cmd == "/again":
            if agent.last_request is None:
                c.print("[yellow]Nothing to do again yet: describe an image first.[/]")
                return
            job = dict(agent.last_request)
            _, job["options"] = imageopts.parse(arg, imageopts.replace_seed(job["options"]))
            make_image(agent, run=lambda cancel, ask: media.redo(agent, job, cancel, ask))
            return
        entry = media.result(agent, f"#{NUMBER.match(number).group(1)}" if number else "")
        source = Path(entry["path"])
        if cmd in ("/open", "/play"):
            problem = media.open_file(source)
            c.print(f"[yellow]{escape(problem)}[/]" if problem else f"[dim]Opened #{entry['n']}.[/]")
            return
        if cmd == "/vary":
            job = dict(entry.get("job") or {"kind": "image", "sources": [], "mask": None, "pads": None})
            tools = ("upscale", "remove-background", "interpolate", "sound")
            if entry.get("kind") in tools or job["kind"] not in ("image", "edit", "video"):
                c.print(f"[yellow]#{entry['n']} is {escape(entry['prompt'])}: /vary the image it came from.[/]")
                return
            job["prompt"] = entry["prompt"] if job["kind"] == "image" else job.get("prompt", entry["prompt"])
            options = imageopts.replace_seed(imageopts.Options.from_json(entry.get("options") or {}), n=4)
            _, job["options"] = imageopts.parse(rest, options)
            c.print(f"[dim]Variations of #{entry['n']}: {escape(entry['prompt'])}[/]")
            make_image(agent, run=lambda cancel, ask: media.redo(agent, job, cancel, ask))
            return
        if cmd == "/animate":
            if not edits.is_image(source):
                c.print(f"[yellow]#{entry['n']} isn't a picture: /animate takes an image.[/]")
                return
            for line in modes.switch(agent, "video"):
                c.print(f"[dim]{escape(line)}[/]")
            text, options = imageopts.parse(rest, imageopts.replace_seed(agent.image_options, n=1))
            job = {"kind": "video", "prompt": text or entry["prompt"], "options": options, "sources": [str(source)],
                   "mask": None, "pads": None, "keep_shape": "--ar" not in rest and "--size" not in rest}  # fmt: skip
            make_image(agent, run=lambda cancel, ask: media.redo(agent, job, cancel, ask))
            return
        if cmd == "/edit":
            text, refs = edits.split_references(rest)
            instruction, options = imageopts.parse(text, imageopts.replace_seed(agent.image_options))
            if not instruction:
                c.print("[yellow]Say what to change: /edit 3 make the sky purple[/]")
                return
            sources = [str(source)] + [str(media.resolve(agent, r)) for r in refs]
            job = {"kind": "edit", "prompt": instruction, "options": options, "sources": sources, "mask": None,
                   "pads": None, "keep_shape": "--ar" not in rest and "--size" not in rest}  # fmt: skip
            make_image(agent, run=lambda cancel, ask: media.redo(agent, job, cancel, ask))
            return
        if cmd == "/extend" and video:
            if edits.is_image(source):
                c.print(f"[yellow]#{entry['n']} is a picture: /animate it, or /extend a clip.[/]")
                return
            text, options = imageopts.parse(rest, imageopts.replace_seed(agent.image_options, n=1))
            job = {"kind": "video", "prompt": text or entry["prompt"], "options": options, "sources": [],
                   "mask": None, "pads": None, "keep_shape": True, "extend": str(source),
                   "size": [entry["width"], entry["height"]]}  # fmt: skip
            c.print(f"[dim]Continuing #{entry['n']} from its last frame, {options.seconds:g} s more.[/]")
            make_image(agent, run=lambda cancel, ask: media.redo(agent, job, cancel, ask))
            return
        if cmd == "/extend":
            width, height = edits.image_size(source)
            pads, text = edits.parse_extend(rest, width, height)
            text, options = imageopts.parse(text, imageopts.replace_seed(agent.image_options, n=1))
            job = {"kind": "edit", "prompt": text, "options": replace(options, size="1:1"), "sources": [str(source)],
                   "mask": None, "pads": pads, "keep_shape": True}  # fmt: skip
            grown = f"{width + pads[0] + pads[2]}×{height + pads[1] + pads[3]}"
            c.print(f"[dim]Extending #{entry['n']} from {width}×{height} to {grown} before scaling.[/]")
            make_image(agent, run=lambda cancel, ask: media.redo(agent, job, cancel, ask))
            return
        if cmd == "/upscale":
            scale = rest.strip().lower().rstrip("x×") or "2"
            if scale not in ("2", "4"):
                c.print("[yellow]/upscale makes 2 or 4 times the size: /upscale 3 4[/]")
                return
            make_image(
                agent, run=lambda cancel, ask: media.apply_tool(agent, "upscale", source, int(scale), cancel, ask)
            )
            return
        if cmd == "/smooth":
            times = int(rest.strip().lower().rstrip("x×") or "2")
            if times not in (2, 4):
                c.print("[yellow]/smooth makes 2 or 4 times the frames: /smooth 4 2[/]")
                return
            make_image(
                agent, run=lambda cancel, ask: media.apply_tool(agent, "interpolate", source, times, cancel, ask)
            )
            return
        if cmd == "/sound":
            make_image(agent, run=lambda cancel, ask: media.apply_tool(agent, "sound", source, 1, cancel, ask, rest))
            return
        if cmd == "/restyle":
            if not media.is_video(source):
                c.print("[yellow]/restyle works on clips: /restyle 4 as a watercolor painting.[/]")
                return
            text, options = imageopts.parse(rest, imageopts.replace_seed(agent.image_options, n=1))
            if not text:
                c.print("[yellow]Say the new look: /restyle 4 as a watercolor painting[/]")
                return
            job = {"kind": "video", "prompt": text, "options": options, "sources": [], "mask": None, "pads": None,
                   "keep_shape": True, "restyle": str(source), "seconds_given": "--seconds" in rest}  # fmt: skip
            make_image(agent, run=lambda cancel, ask: media.redo(agent, job, cancel, ask))
            return
        if cmd == "/nobg":
            make_image(
                agent, run=lambda cancel, ask: media.apply_tool(agent, "remove-background", source, 2, cancel, ask)
            )
            return
    except (media.MediaError, edits.EditError, imageopts.OptionError) as e:
        c.print(f"[yellow]{escape(str(e))}[/]")


def use_references(agent: Agent, arg: str) -> None:
    """/use 3 @photo.png: images every next request follows (as references) until /use off."""
    c = agent.console
    if arg in ("off", "none", "clear"):
        agent.image_refs = []
        c.print("[dim]No reference images now.[/]")
        return
    if arg:
        refs = [r.lstrip("@") for r in arg.split()]
        paths = [media.resolve(agent, f"#{NUMBER.match(r).group(1)}" if NUMBER.match(r) else r) for r in refs]
        agent.image_refs = [str(p) for p in paths]
    if not agent.image_refs:
        c.print("[dim]No reference images. /use 3 (or /use photo.png) makes the next requests follow one.[/]")
        return
    shown = ", ".join(media.shown(agent, Path(p)) for p in agent.image_refs)
    c.print(f"References for the next requests: [bold]{escape(shown)}[/] [dim](/use off stops)[/]")


def gallery(agent: Agent, everything: bool = False) -> None:
    """The session's images (or every image in the media folder), with what made them."""
    import json as json_module

    c = agent.console
    if everything:
        folder = media.folder(agent)
        entries = []
        for sidecar in sorted(folder.glob("*.json"), reverse=True)[:50]:
            try:
                meta = json_module.loads(sidecar.read_text())
            except (OSError, ValueError):
                continue
            entries.append({"path": str(sidecar.with_name(meta.get("file", sidecar.stem + ".png"))), **meta})
        if not entries:
            c.print(f"[dim]No images in {media.shown(agent, folder)} yet.[/]")
            return
        c.print(f"[bold]{len(entries)} latest image(s) in {escape(media.shown(agent, folder))}[/]")
    else:
        entries = agent.media_results
        if not entries:
            c.print("[dim]No images in this session yet: describe one. /gallery all lists every image here.[/]")
            return
    for entry in entries:
        number = f"#{entry['n']:<3}" if "n" in entry else "    "
        c.print(
            f"  [bold green]{number}[/] {escape(media.shown(agent, Path(entry['path'])))}\n"
            f"       [dim]{escape(entry.get('prompt', ''))} · {entry.get('width')}×{entry.get('height')} · "
            f"{escape(str(entry.get('model', '')))} · seed {entry.get('seed')}[/]"
        )


def make_image(agent: Agent, prompt: str = "", options=None, run=None) -> None:
    """Image mode: ComfyUI makes the images (a request, or `run(cancelled, ask)`); Esc or Ctrl+C cancels them
    there too."""
    steering = Steering()
    reader = KeyReader(steering)
    reading = agent.interactive and reader.start()  # Esc sets steering.pause: here it cancels

    def ask(question: str) -> bool:
        answer = steering.ask(f"  {question} [y/N] ") if reading else input(f"  {question} [y/N] ")
        return answer.strip().lower() in ("y", "yes")

    started = time.monotonic()
    try:
        asking = ask if agent.interactive else None
        if run is not None:
            run(lambda: steering.pause, asking)
        elif options is None:
            media.request(agent, prompt, cancelled=lambda: steering.pause, ask=asking)
        else:
            media.make(agent, prompt, options, cancelled=lambda: steering.pause, ask=asking)
    except media.MediaError as e:
        agent.console.print(f"[yellow]{escape(str(e))}[/]")
    except KeyboardInterrupt:  # while ComfyUI was starting: nothing queued yet
        agent.console.print("[yellow]⏹ Cancelled.[/]")
    except EOFError:
        pass
    finally:
        if reading:
            reader.stop()
        typed = steering.leftover()  # the next prompt, typed while this one was being made
        if typed:
            agent.unsent = typed
            agent.console.print(Text("✎ What you typed is in the prompt below.", style="cyan"))
        agent.save()  # the gallery and options carry over when the session is resumed
        agent.finished(prompt or "image or video", time.monotonic() - started)  # a long render: notify


def preload(agent: Agent, session: PromptSession | None = None) -> None:
    """Start loading the model in the background so the first answer comes sooner; the status bar says so."""
    if is_cloud(agent.settings.model):
        return  # it runs on ollama.com: there's nothing to load here
    where = residency.placement(agent.ollama, agent.settings.model)
    if where is not None and (not where.context or where.context == agent.settings.context):
        return  # already in memory

    def load() -> None:
        agent.load_state = "loading"
        refresh()
        try:
            agent.ollama.load(agent.settings.model, agent.options(), agent.settings.keep_alive)
        except OllamaError:
            pass
        finally:
            agent.load_state = ""
            refresh()

    def refresh() -> None:
        if session is not None and session.app.is_running:
            session.app.invalidate()

    threading.Thread(target=load, daemon=True).start()
