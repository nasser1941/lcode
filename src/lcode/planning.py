"""Plan mode: agree on an approach before anything changes.

In the `plan` permission mode the model can read, search, run read-only commands, use the web and
read-only subagents, but can't change anything. It ends by presenting a plan with the
present_plan tool. The user approves it (lcode switches to `ask` or `auto-edit`, fills the todo
list from the plan's steps and keeps the plan through compaction), edits it, saves it to
`.lcode/plans/`, or sends it back with feedback.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel

from lcode.checkpoints import work_tree_for
from lcode.memory import slugify

if TYPE_CHECKING:
    from lcode.agent import Agent

# Tools that can't change anything (bash is limited to read-only commands, agent to read-only types).
PLAN_MODE_TOOLS = frozenset(
    {
        "read_file", "list_dir", "glob", "grep", "bash", "web_search", "web_fetch", "view_image", "todo_write",
        "agent", "memory", "present_plan", "mcp_find_tools", "skill", "lsp", "repo_map", "search_code",
    }
)  # fmt: skip

NOTE = """

[lcode] Plan mode is on: you can read, search, run read-only commands, use the web and read-only subagents, but you can't change anything yet. Work out how to do this, then call present_plan with: numbered steps, the files you'll change and how, the risks, and how you'll verify the result. Ask the user only about decisions that are genuinely theirs."""

BLOCKED = (
    "Plan mode is on, so nothing can be changed yet. Finish working out the plan and present it with "
    "present_plan; once the user approves it, you can make the changes."
)

CHOICES = (
    "  [bold]\\[y][/] run it, asking before changes · [bold]\\[a][/] run it, edits without asking (auto-edit)\n"
    "  [bold]\\[e][/] edit the plan · [bold]\\[s][/] save it to .lcode/plans/ · [bold]\\[n][/] keep planning (+ what to change)"
)


def steps(plan: str, limit: int = 20) -> list[str]:
    """The plan's numbered steps (top level only), for the todo list."""
    found = []
    for line in plan.splitlines():
        m = re.match(r"^ {0,3}\d+[.)]\s+(.+)", line)
        if m:
            # Plain text for the todo list: no bold, code or emphasis marks (underscores in names stay).
            text = m.group(1).replace("**", "").replace("__", "").replace("`", "")
            text = re.sub(r"(?<!\w)[*_]([^*_]+)[*_](?!\w)", r"\1", text).strip().rstrip(":").strip()
            found.append(text if len(text) <= 120 else text[:119].rstrip() + "…")
    return found[:limit]


def edit_text(text: str) -> str:
    """Let the user edit `text` in their editor; returns the result."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("nano" if shutil.which("nano") else "vi")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(text)
        path = Path(f.name)
    try:
        subprocess.call([*shlex.split(editor), str(path)])
        return path.read_text()
    finally:
        path.unlink(missing_ok=True)


def save(agent: Agent, title: str, plan: str) -> Path:
    folder = work_tree_for(agent.cwd) / ".lcode" / "plans"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{slugify(title) or 'plan'}.md"
    path.write_text(f"# {title}\n\n{plan.strip()}\n")
    return path


def present(agent: Agent, title: str, plan: str) -> str:
    """Show the plan and let the user decide. Returns the tool result for the model."""
    c = agent.console
    title = " ".join(title.split()) or "Plan"
    plan = plan.strip()
    if not plan:
        return "Error: the plan is empty. Write the steps, the files, the risks and how you'll verify the work."
    if agent.plan_review is not None:  # an editor shows the plan (lcode acp)
        choice = agent.plan_review(title, plan)
        if choice in ("ask", "auto-edit"):
            return approve(agent, title, plan, choice, False)
        return choice or "The user didn't approve the plan. Ask them what they'd like to change."
    edited = False
    while True:
        c.print(Panel(Markdown(plan), title=f"Plan: {escape(title)}", title_align="left", border_style="cyan"))
        if not agent.interactive:
            return "The plan was shown to the user, who will review it later. Stop here and don't change anything."
        c.print(CHOICES)
        try:
            answer = input("  > ").strip()
        except EOFError:
            return "The plan was shown to the user, who will review it later. Stop here and don't change anything."
        word = answer.lower()
        if word in ("", "y", "yes"):
            return approve(agent, title, plan, "ask", edited)
        if word in ("a", "auto", "auto-edit"):
            return approve(agent, title, plan, "auto-edit", edited)
        if word in ("e", "edit"):
            new = edit_text(plan).strip()
            if new and new != plan:
                plan, edited = new, True
            continue
        if word in ("s", "save"):
            path = save(agent, title, plan)
            c.print(f"[green]Saved to {escape(agent.tools.rel(path))}[/]")
            continue
        feedback = re.sub(r"^(no|n)\b[\s,:-]*", "", answer, flags=re.I).strip()
        if feedback:
            return f"The user wants changes to the plan: {feedback}\nStay in plan mode: revise the plan and present it again."
        return "The user didn't approve the plan. Ask them what they'd like to change."


def approve(agent: Agent, title: str, plan: str, mode: str, edited: bool) -> str:
    agent.perms.mode = mode
    agent.plan = f"# {title}\n\n{plan}"
    todo = steps(plan)
    if todo:
        agent.tools.todos = [{"content": step, "status": "pending"} for step in todo]
        agent.tools.show_todos()
    how = "file edits no longer ask (auto-edit)" if mode == "auto-edit" else "changes ask for approval as usual"
    agent.console.print(f"[green]Plan approved.[/] Mode: {mode} — {how}.")
    result = f"The user approved the plan, and plan mode is off ({mode} mode: {how}). Carry it out now, step by step"
    result += ", keeping the todo list up to date." if todo else "."
    if edited:
        result += f"\nThe user edited the plan first. This is the approved version:\n\n{plan}"
    return result
