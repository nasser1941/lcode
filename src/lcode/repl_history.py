"""What lcode remembers and how to go back: /remember, /memory, /checkpoints, /undo and /rewind."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess

from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from lcode import memory as memory_notes
from lcode import (
    sessions,
)
from lcode.agent import Agent
from lcode.checkpoints import Checkpoint, CheckpointError, Restore


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
