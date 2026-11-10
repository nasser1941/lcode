"""The interactive prompt: input handling, slash commands and the status bar."""

from __future__ import annotations

import html
import json
import os
import re
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
    api,
    assistant,
    backends,
    clipboard,
    escalate,
    extensions,
    gitflow,
    manual,
    media,
    modes,
    residency,
    sessions,
    web,
)
from lcode import context as context_tools
from lcode import memory as memory_notes
from lcode.agent import INIT_PROMPT, Agent
from lcode.cloud import is_cloud
from lcode.cloud import notice as cloud_notice
from lcode.config import PERMISSION_MODES, STATE_DIR, format_tokens
from lcode.hardware import Hardware
from lcode.ollama import OllamaError
from lcode.repl_history import (
    memory_command,
    memory_status,
    print_checkpoints,
    remember_command,
    rewind_command,
    undo_command,
)
from lcode.repl_media import IMAGE_COMMANDS, film_command, image_command, make_image
from lcode.repl_models import context_command, media_model_command, mode_command, model_command
from lcode.repl_sessions import choose_session, print_recap, resume_session
from lcode.steer import KeyReader, Steering
from lcode.tool_helpers import IGNORE_DIRS

COMMANDS = {
    "/help": "Show this help, or ask how to do something: /help how do I make a clip from two pictures?",
    "/init": "Analyze the repo and write an AGENTS.md guide (loaded at every start)",
    "/clear": "Start a fresh conversation",
    "/rename": "Name this session so you can find it later, e.g. /rename auth refactor",
    "/resume": "Resume a saved session: this folder's, then others; /resume <number|name>; /resume all: older too",
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
    "/extend": "Outpaint an image (/extend 3 --ar 16:9), or continue a clip in video mode (/extend 4 --seconds 20)",
    "/upscale": "2 or 4 times the pixels of an image or a clip, e.g. /upscale 3 4",
    "/nobg": "Image mode: remove the background (a transparent PNG), e.g. /nobg 3",
    "/animate": "Image mode: make a video starting from an image, e.g. /animate 3 the waves roll in (video mode)",
    "/smooth": "Video mode: 2 or 4 times the frames (smoother motion), e.g. /smooth 4",
    "/sound": "Video mode: add sound to a clip, e.g. /sound 4 waves and seagulls",
    "/restyle": "Video mode: the same clip in a new look, e.g. /restyle 4 as a watercolor painting",
    "/play": "Play a video in your player, e.g. /play 4 (default: the last)",
    "/film": "A short film from an idea: /film a fox finds a lost kitten --shots 3 (plan, make, shot N, stitch, list)",
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
        c.print("[dim]/jobs stop <id> stops one; they stop when the session ends, except kept ones.[/]")


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


def run_safely(agent: Agent, text: str) -> str:
    """Run a request. An interrupt or a model error ends the request, not the session. Returns how it ended, as
    `lcode -p --output json` says it: success, error, max_steps, loop, blocked or interrupted."""
    started = time.monotonic()
    reader = start_steering(agent) if agent.interactive else None
    try:
        agent.run_turn(text)
        status = api.request_status(agent)
    except KeyboardInterrupt:
        status = "interrupted"
    except OllamaError as e:
        status = "error"
        agent.console.print(f"[red]{e}[/]")
    finally:
        if reader is not None:
            stop_steering(agent, reader)
        agent.save()
        agent.finished(text, time.monotonic() - started)
    return status


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
    if cmd == "/help" and arg:
        ask_manual(agent, arg)
    elif cmd == "/help":
        rows = "\n".join(f"  [cyan]{k:<13}[/] {v}" for k, v in COMMANDS.items())
        keys = (
            "Enter = send · Esc+Enter or trailing \\ = newline · Ctrl+C = interrupt · Ctrl+D = quit · "
            "@path = attach a file · Shift+Tab = cycle permission mode"
        )
        c.print(Panel(f"{rows}\n\n  [dim]{keys}[/]", title="lcode commands", border_style="cyan"))
        help_extensions(agent)
        c.print(
            "[dim]Ask how to do something: /help how do I make a 20-second clip from two pictures? In image and video "
            "mode, just ask.[/]"
        )
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
    elif cmd == "/film":
        film_command(agent, arg)
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


def repl(agent: Agent, prompt: str | None, hardware: Hardware, cont: bool = False, resume: str | None = None) -> int:
    """The interactive session, or with a prompt (`lcode -p`) that one request. Returns lcode's exit code: for a coding
    request, the one `--output json` gives (api.EXIT_CODES), so a script can tell a failed request from a done one;
    otherwise 0."""
    if cont:
        note = agent.load_latest()
        if note is None:
            agent.console.print("[dim]No saved session in this folder yet; starting a new one.[/]")
        else:
            agent.console.print(f"[green]Continuing[/] [bold]{escape(agent.session_name or agent.session_title)}[/]")
            if note:
                agent.console.print(f"[yellow]{escape(note)}[/]")
            print_recap(agent)
    elif resume is not None:
        info = choose_session(agent, resume)
        if info:
            resume_session(agent, info)
    session_start(agent)
    if prompt and agent.settings.mode in ("image", "video"):
        respond(agent, prompt)
        return 0
    if prompt:
        return api.EXIT_CODES.get(run_safely(agent, prompt), 1)
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
    return 0


def respond(agent: Agent, line: str) -> None:
    """What a typed request does: the coding agent works on it, or image and video mode take it (a question about
    lcode is answered from its manual instead)."""
    if agent.settings.mode in ("image", "video"):  # no coding model involved
        if manual.is_question(line) and not media.RAW_FLAG.search(line):
            ask_manual(agent, line, typed=True)
            return
        make_image(agent, line)
        return
    agent.console.print(Rule(style="dim"))
    run_safely(agent, line)


def manual_writer(agent: Agent):
    """Who writes answers from lcode's manual, as (model, chat): the coding model in code mode, the prompt assistant's
    small model in image and video mode (so the GPU stays with ComfyUI), or (None, None): the manual's own words."""
    if agent.settings.mode == "code":
        model = agent.settings.model

        def chat(messages: list[dict]) -> str:
            payload = {"model": model, "messages": messages, "think": False,
                       "keep_alive": agent.settings.keep_alive, "options": agent.options()}  # fmt: skip
            reply = agent.ollama.chat(payload, timeout=300)
            return (reply.get("message") or {}).get("content") or ""

        return model, chat
    setting = agent.settings.prompt_assistant  # its model, even when it's off for prompts
    model = assistant.pick(agent.ollama, "auto" if setting in assistant.SCOPES or not setting else setting)
    if model is None:
        return None, None
    helper = assistant.Assistant(agent.ollama, model)
    return model, lambda messages: helper.converse(messages, num_ctx=manual.HELP_CONTEXT)


def ask_manual(agent: Agent, question: str, typed: bool = False) -> None:
    """A question about lcode, answered from its manual (lcode.manual), with the first step put in the prompt."""
    c, mode = agent.console, agent.settings.mode
    model, chat = manual_writer(agent)
    book = manual.load(set(custom_commands(agent)))
    try:
        with c.status(f"Looking it up in lcode's manual{f' ({model})' if model else ''}…", spinner="dots"):
            found = manual.answer(question, mode, chat, model, book)
    except Exception as e:  # the model didn't answer: the manual's own words
        agent.say(f"{model} didn't answer ({e}). Here's what the manual says.", warning=True)
        found = manual.answer(question, mode, None, None, book)
    if not found.text:
        c.print(f"[yellow]Nothing in lcode's manual matches that.[/] The whole manual: {manual.DOCS_URL}")
        return
    title = "lcode help" if found.model else "From lcode's manual"
    c.print(Panel(Markdown(found.text), title=title, title_align="left", border_style="cyan"))
    for problem in found.problems:
        c.print(f"[yellow]⚠ This part won't work: {escape(problem)}[/]")
    for piece in found.sources():
        c.print(Text(f"  {piece.heading}: {piece.url}", style="dim"))
    if found.model is None and mode != "code":
        c.print("[dim]For a written answer, install a small model: ollama pull qwen3.5:4b[/]")
    first = manual.suggestion(found.text, mode) if found.model and not found.problems else ""
    if first and agent.interactive:
        agent.unsent = first
        c.print(Text("✎ The first step is in the prompt below: edit it, or press Enter to run it.", style="cyan"))
    if typed:
        what = "picture" if mode == "image" else "clip"
        c.print(f"[dim]That looked like a question, so lcode answered it. To make it as a {what}, add --raw.[/]")


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
            if agent.settings.mode != "code":  # switched to image or video mode while it loaded: give the GPU back
                residency.release(agent.ollama, agent.settings.model)
        except OllamaError:
            pass
        finally:
            agent.load_state = ""
            refresh()

    def refresh() -> None:
        if session is not None and session.app.is_running:
            session.app.invalidate()

    threading.Thread(target=load, daemon=True).start()
