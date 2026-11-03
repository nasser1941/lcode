"""Saved conversations: listing them for /resume, choosing one, and resuming it with a recap."""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from lcode import context as context_tools
from lcode import (
    sessions,
)
from lcode.agent import Agent


def short_path(folder: str) -> str:
    """A folder for a list: under the home folder as ~/…"""
    home = str(Path.home())
    return "~" + folder[len(home) :] if folder == home or folder.startswith(home + "/") else folder


def session_table(agent: Agent, title: str, found: list[sessions.SessionInfo], first: int, folders: bool) -> Table:
    table = Table(title=title, title_justify="left", header_style="bold")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Session")
    table.add_column("Last used", no_wrap=True)
    table.add_column("Requests", justify="right")
    table.add_column("Context", no_wrap=True)
    if folders:
        table.add_column("Folder", overflow="fold")
    for i, info in enumerate(found, first):
        if info.name:
            label = f"[bold]{escape(info.name)}[/]\n[dim]{escape(info.title or '(no requests yet)')}[/]"
        else:
            label = escape(info.label)
        if info.id == agent.session_id:
            label += " [green](current)[/]"
        elif not folders and info.cwd != str(agent.workspace):  # in another folder or checkout of this repository
            saved = Path(info.cwd)
            where = saved.relative_to(agent.workspace) if agent.workspace in saved.parents else short_path(info.cwd)
            label += f"\n[dim]in {escape(str(where))}[/]"
        tokens = info.context(agent.settings.context)
        if info.tokens > agent.settings.context * 0.85:  # resuming summarizes it first
            tokens = f"[yellow]{tokens}[/]"
        row = [str(i), label, sessions.age(info.updated), str(info.turns), tokens]
        if folders:
            row.append(escape(short_path(info.cwd)))
        table.add_row(*row)
    return table


def print_sessions(agent: Agent, listing: sessions.Listing) -> None:
    """This folder's sessions, then the others with their folders, numbered across both."""
    c = agent.console
    here = short_path(str(agent.workspace))
    if listing.here:
        c.print(session_table(agent, f"This folder · {here}", listing.here, 1, folders=False))
    else:
        c.print(f"[dim]No saved sessions in this folder ({escape(here)}) yet.[/]")
    if listing.elsewhere:
        c.print(session_table(agent, "Elsewhere", listing.elsewhere, len(listing.here) + 1, folders=True))
    left_out = []
    if listing.more:
        left_out.append(f"{listing.more} older session{'' if listing.more == 1 else 's'} elsewhere")
    if listing.hidden:
        left_out.append(f"{listing.hidden} test run{'' if listing.hidden == 1 else 's'} from temporary folders")
    if left_out:
        c.print(f"[dim]Not shown: {' and '.join(left_out)}. /resume all lists them.[/]")


def choose_session(agent: Agent, query: str = "") -> sessions.SessionInfo | None:
    """Find a session by number/name/id, or list them and ask. `all` also lists older sessions elsewhere."""
    c = agent.console
    words = query.split()
    everything = bool(words) and words[0].lower() in ("all", "--all", "-a")
    query = " ".join(words[1:] if everything else words)
    listing = sessions.grouped(agent.workspace, everything=everything)
    found = listing.numbered
    if query:
        match = sessions.find(query, found)
        if match is None and not everything:
            match = sessions.find(query, sessions.grouped(agent.workspace, everything=True).numbered)
        if match:
            return match
        c.print(f"[yellow]No session matches '{escape(query)}'.[/]")
    if not found:
        c.print("No saved sessions yet. Sessions are saved after every request.")
        return None
    print_sessions(agent, listing)
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
