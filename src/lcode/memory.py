"""Memory across sessions: short notes about what a future session should know.

Each note is a markdown file holding one fact, with a small frontmatter:

    ---
    name: test-command
    type: project
    description: Run the tests with `uv run pytest -q`; plain pytest misses the dev dependencies
    modified: 2026-10-05
    ---
    Optional details: why, and how to apply it.

Notes live in lcode's state folder: `memory/user/` holds the user's own preferences, for every
repository, and `memory/projects/<repo>/` holds one repository's notes, shared by all its git
worktrees. The system prompt gets a one-line index of both, capped so it costs little context;
the model reads a note's details with the memory tool.

Notes come from three places: the model's memory tool, the user's /remember, and a short
reflection when a session ends ("what from this session is worth remembering?"), because local
models rarely save anything on their own.
"""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from lcode import config

if TYPE_CHECKING:
    from lcode.agent import Agent

TYPES = ("feedback", "project", "reference", "user")
SCOPES = ("project", "user")
MAX_DESCRIPTION = 200  # characters: a note's description is one line
MAX_DETAILS = 1000
MAX_INDEX_CHARS = 6000  # ~2K tokens of the system prompt at most
SIMILAR = 0.9  # descriptions this alike say the same thing
MAX_REFLECTION_NOTES = 3
# A session is worth a reflection when it had a few requests, or one request with real work in it.
REFLECT_MIN_REQUESTS = 2
REFLECT_MIN_TOOL_CALLS = 4

# Never saved: text that looks like a credential. The label tells the model what to leave out.
SECRET_PATTERNS = [
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("a GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("an API key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("a JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("a password in a URL", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:[^\s/@]+@", re.I)),
    (
        "a password or key",
        re.compile(
            r"\b(?:password|passwd|pwd|secret|token|api[_ -]?key|access[_ -]?key|client[_ -]?secret)\b"
            r"\s*[:=]\s*['\"]?[^\s'\"]{6,}",
            re.I,
        ),
    ),
]

PROMPT = """
# Memory
Notes saved in earlier sessions. They can be out of date: if one conflicts with what you find, trust what you find and update or delete the note.
{index}
- Save a note with the memory tool when you learn something a future session needs that isn't in the code, git history or AGENTS.md: a correction or preference from the user (feedback), a decision or constraint (project), where something lives outside the repository (reference), or who the user is (user). One fact per note; to change a note, save it again under the same name.
- Never save code structure, file paths, what you changed in this session, temporary state, or secrets.
"""

REFLECT_PROMPT = """[lcode] The session is ending. Is there anything from this conversation that a future session in this repository should know, and that isn't in the code, the git history or AGENTS.md? Worth saving:
- feedback: a correction or preference the user gave ("use pnpm, not npm", "don't add docstrings")
- project: a decision, constraint or ongoing work in this repository ("the staging database needs the VPN")
- reference: where something lives outside the repository (a dashboard, tracker or document)
- user: who the user is (role, expertise) and how they like to work in general
Not worth saving: code structure, file paths, what changed in this session, anything git records, temporary state, secrets. Most sessions have nothing worth saving: then answer with an empty list. At most {limit} notes. To update an existing note, reuse its name.

Existing notes:
{existing}

Scope: "project" for anything about this repository (almost always); "user" only for something about the user that holds in every repository.

Answer with JSON only: {{"notes": [{{"type": "feedback", "name": "short-kebab-case-name", "description": "the fact, in one line", "details": "optional: why, and how to apply it", "scope": "project"}}]}}"""

REFLECT_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(TYPES)},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "details": {"type": "string"},
                    "scope": {"type": "string", "enum": list(SCOPES)},
                },
                "required": ["type", "name", "description"],
            },
        }
    },
    "required": ["notes"],
}


class NoteError(ValueError):
    """A note can't be saved; the message is meant for the model or the user."""


@dataclass(frozen=True)
class Note:
    name: str
    type: str
    description: str
    details: str = ""
    modified: str = ""  # YYYY-MM-DD
    scope: str = "project"
    path: Path | None = None

    def line(self) -> str:
        more = ", has details" if self.details else ""
        return f"- [{self.type}] {self.description} ({self.name}, {self.modified}{more})"

    def render(self) -> str:
        head = (
            f"---\nname: {self.name}\ntype: {self.type}\ndescription: {self.description}\n"
            f"modified: {self.modified}\n---\n"
        )
        return head + (f"{self.details.strip()}\n" if self.details.strip() else "")


# ----------------------------------------------------------------------------- notes on disk


def memory_dir() -> Path:
    return config.STATE_DIR / "memory"


def slugify(text: str, limit: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > limit:
        slug = slug[:limit].rsplit("-", 1)[0] or slug[:limit]
    return slug


def _git_common_dir(cwd: Path) -> Path | None:
    """The repository's git directory, the same for every worktree of it (None outside git)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=cwd, capture_output=True, text=True, timeout=10, env=env
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        return None
    return (cwd / out).resolve()


def project_dir(cwd: Path) -> Path:
    """Where the notes for the repository around `cwd` live: shared by its worktrees."""
    common = _git_common_dir(cwd)
    if common:
        root = common.parent if common.name == ".git" else common
        name, ident = root.name.removesuffix(".git"), common
    else:
        name, ident = cwd.name, cwd.resolve()
    key = hashlib.sha256(str(ident).encode()).hexdigest()[:8]
    return memory_dir() / "projects" / f"{slugify(name, 40) or 'folder'}-{key}"


def frontmatter(text: str) -> tuple[dict[str, str], str] | None:
    """The `key: value` header between --- lines, and the body after it (None without a header)."""
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n?(.*)", text, re.S)
    if not m:
        return None
    fields = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields, m.group(2)


def parse(text: str, scope: str, path: Path | None = None) -> Note | None:
    split = frontmatter(text)
    if not split:
        return None
    fields, body = split
    name = path.stem if path else fields.get("name", "")  # the file name, in case an edit changed the header
    if not name or not fields.get("description"):
        return None
    kind = fields.get("type", "project")
    return Note(
        name=name,
        type=kind if kind in TYPES else "project",
        description=fields["description"],
        details=body.strip(),
        modified=fields.get("modified", ""),
        scope=scope,
        path=path,
    )


def find_secret(text: str) -> str | None:
    """What kind of credential the text seems to contain, if any."""
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


def make_note(name: str, type: str, description: str, details: str = "", scope: str = "project") -> Note:
    """A checked note, ready to save. Raises NoteError explaining what's wrong."""
    description = " ".join(str(description or "").split())
    details = str(details or "").strip()
    if type not in TYPES:
        raise NoteError(f"type must be one of {', '.join(TYPES)}")
    if scope not in SCOPES:
        raise NoteError(f"scope must be one of {', '.join(SCOPES)}")
    if not description:
        raise NoteError("description is empty: write the fact in one line")
    if len(description) > MAX_DESCRIPTION:
        raise NoteError(
            f"description is {len(description)} characters; keep it to one line under {MAX_DESCRIPTION} and put "
            "the rest in details"
        )
    if len(details) > MAX_DETAILS:
        raise NoteError(f"details are {len(details)} characters; keep them under {MAX_DETAILS}")
    secret = find_secret(f"{name}\n{description}\n{details}")
    if secret:
        raise NoteError(f"not saved: it seems to contain {secret}. Never save secrets; describe where they live")
    slug = slugify(name or description)
    if not slug:
        raise NoteError("name is empty: use a short kebab-case name such as use-pnpm")
    return Note(slug, type, description, details, dt.date.today().isoformat(), scope)


class Memory:
    """The notes for one working directory: the user's own, and its repository's."""

    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.project = project_dir(cwd)
        self.user = memory_dir() / "user"

    def folder(self, scope: str) -> Path:
        return self.user if scope == "user" else self.project

    def notes(self, scope: str | None = None) -> list[Note]:
        """User notes first, then the repository's; newest first within each."""
        found = []
        for s in [scope] if scope else ["user", "project"]:
            folder = self.folder(s)
            if not folder.is_dir():
                continue
            mine = []
            for f in folder.glob("*.md"):
                if f.name == "MEMORY.md":
                    continue
                try:
                    note = parse(f.read_text(errors="replace"), s, f)
                except OSError:
                    continue
                if note:
                    mine.append(note)
            found += sorted(mine, key=lambda n: (n.modified, n.name), reverse=True)
        return found

    def find(self, name: str, scope: str | None = None) -> Note | None:
        slug = slugify(name)
        matches = [n for n in self.notes(scope) if n.name == slug]
        # A repository note wins over a user note with the same name.
        return next((n for n in matches if n.scope == "project"), matches[0] if matches else None)

    def similar(self, note: Note) -> Note | None:
        """A note under another name in the same scope that says nearly the same thing."""
        wanted = note.description.lower()
        for other in self.notes(note.scope):
            ratio = difflib.SequenceMatcher(None, wanted, other.description.lower()).ratio()
            if other.name != note.name and ratio >= SIMILAR:
                return other
        return None

    def save(self, note: Note) -> tuple[Note, bool]:
        """Save a note, replacing the one with the same name. Returns (note, updated)."""
        existing = self.find(note.name, note.scope)
        if existing and not note.details and existing.description == note.description:
            note = replace(note, details=existing.details)
        folder = self.folder(note.scope)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{note.name}.md"
        _write_atomic(path, note.render())
        self.write_index(note.scope)
        return replace(note, path=path), existing is not None

    def delete(self, note: Note) -> None:
        if note.path:
            note.path.unlink(missing_ok=True)
        self.write_index(note.scope)

    def write_index(self, scope: str) -> None:
        """MEMORY.md: a readable list of the notes in a folder. lcode reads the notes themselves."""
        folder = self.folder(scope)
        notes = self.notes(scope)
        if not notes:
            (folder / "MEMORY.md").unlink(missing_ok=True)
            return
        lines = ["# lcode memory", "", "Generated by lcode: edit or delete the notes, not this file.", ""]
        lines += [n.line() for n in notes]
        _write_atomic(folder / "MEMORY.md", "\n".join(lines) + "\n")

    def prompt(self, context: int) -> str:
        """The memory section of the system prompt: the index, within its budget."""
        budget = min(MAX_INDEX_CHARS, context * 3 // 20)  # at most ~5% of the context window
        notes = self.notes()
        shown: list[Note] = []
        used = 0
        for note in sorted(notes, key=lambda n: n.modified, reverse=True):
            cost = len(note.line()) + 1
            if used + cost > budget:
                continue
            shown.append(note)
            used += cost
        parts = []
        for scope, title in (("user", "For every repository:"), ("project", "For this repository:")):
            lines = [n.line() for n in notes if n in shown and n.scope == scope]
            if lines:
                parts.append(title + "\n" + "\n".join(lines))
        hidden = len(notes) - len(shown)
        if hidden:
            parts.append(f'({hidden} older note{"" if hidden == 1 else "s"} not shown: memory action "read" lists all)')
        return PROMPT.format(index="\n".join(parts) if parts else "(none yet)")

    def counts(self) -> tuple[int, int]:
        return len(self.notes("project")), len(self.notes("user"))


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".partial")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ----------------------------------------------------------------------------- the memory tool


def run_tool(agent: Agent, action: str, name: str, type: str, description: str, details: str, scope: str) -> str:
    memory = agent.memory()
    if action == "read":
        if not name:
            notes = memory.notes()
            return "\n".join(f"{n.line()} [{n.scope}]" for n in notes) if notes else "No notes yet."
        note = memory.find(name)
        if not note:
            return f"Error: no note named {name!r}. Read without a name to list them."
        return f"{note.line()} [{note.scope}]\n{note.details or '(no details)'}"
    if action == "delete":
        note = memory.find(name)
        if not note:
            return f"Error: no note named {name!r}."
        if not approved(agent, f"Forget this {note.scope} note?", note):
            return "The user kept the note."
        memory.delete(note)
        agent.console.print(Text(f"  ⎿ forgot {note.name}", style="dim"))
        return f"Deleted the note {note.name!r}."
    if action != "save":
        return 'Error: action must be "save", "read" or "delete"'
    if not scope:
        scope = "user" if type == "user" else "project"
    try:
        note = make_note(name, type, description, details, scope)
    except NoteError as e:
        return f"Error: {e}"
    twin = memory.similar(note)
    if twin:
        return (
            f"Error: the note {twin.name!r} already says nearly the same: {twin.description!r}. To change it, save "
            f"under the name {twin.name!r}; if this is a different fact, make the description say how it differs."
        )
    if not approved(
        agent, f"Remember this {'for every repository' if scope == 'user' else 'for this repository'}?", note
    ):
        return "The user didn't want this saved. Don't try to save it again."
    saved, updated = memory.save(note)
    agent.console.print(Text(f"  ⎿ {'updated' if updated else 'remembered'} {saved.name}", style="dim"))
    return f"{'Updated' if updated else 'Saved'} the {saved.scope} note {saved.name!r}."


def approved(agent: Agent, title: str, note: Note) -> bool:
    if agent.settings.memory == "auto":
        return True
    body = Text(f"[{note.type}] {note.description}" + (f"\n{note.details}" if note.details else ""))
    ok, _ = agent.perms.request("memory", "memory", title, body)
    return ok


# ----------------------------------------------------------------------------- end-of-session reflection


def worth_reflecting(messages: list[dict], start: int) -> bool:
    from lcode.sessions import SUMMARY_PREFIX

    new = messages[start:]
    requests = sum(
        1
        for m in new
        if m.get("role") == "user" and not str(m.get("content", "")).startswith(("[lcode]", SUMMARY_PREFIX))
    )
    tool_calls = sum(1 for m in new if m.get("role") == "tool")
    return requests >= REFLECT_MIN_REQUESTS or (requests >= 1 and tool_calls >= REFLECT_MIN_TOOL_CALLS)


def parse_reflection(content: str) -> list[dict]:
    """The notes in the model's answer: {"notes": [...]}, a bare list, or JSON inside other text."""
    data = None
    for candidate in (content, *re.findall(r"\{.*\}|\[.*\]", content, re.S)):
        try:
            data = json.loads(candidate)
            break
        except (json.JSONDecodeError, TypeError):
            continue
    if isinstance(data, dict):
        data = data.get("notes")
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def propose(agent: Agent) -> list[Note]:
    """Ask the model which facts from this session a future session should know."""
    memory = agent.memory()
    existing = "\n".join(f"{n.line()} [{n.scope}]" for n in memory.notes()) or "(none)"
    payload = {
        "model": agent.settings.model,
        # The same messages and tools as the session, so Ollama reuses the cached prompt.
        "messages": [
            *agent.messages,
            {"role": "user", "content": REFLECT_PROMPT.format(limit=MAX_REFLECTION_NOTES, existing=existing)},
        ],
        "tools": agent.tool_schemas(),
        "think": False,
        "format": REFLECT_SCHEMA,
        "keep_alive": agent.settings.keep_alive,
        "options": agent.options(),
    }
    reply = agent.ollama.chat(payload)
    content = (reply.get("message") or {}).get("content") or ""
    notes = []
    for item in parse_reflection(content)[:MAX_REFLECTION_NOTES]:
        kind = str(item.get("type", "project"))
        scope = str(item.get("scope") or ("user" if kind == "user" else "project"))
        description = " ".join(str(item.get("description", "")).split())
        details = str(item.get("details") or "")
        if len(description) > MAX_DESCRIPTION:
            description, details = description[: MAX_DESCRIPTION - 1].rstrip() + "…", (details or description)
        try:
            notes.append(make_note(str(item.get("name", "")), kind, description, details[:MAX_DETAILS], scope))
        except NoteError:
            continue  # secrets and malformed notes are dropped
    return [n for n in notes if not memory.similar(n)]  # restatements of notes it already has


def reflect(agent: Agent) -> None:
    """At the end of a session (or before compaction): propose notes, then save the ones the user accepts."""
    if agent.settings.memory == "off" or not worth_reflecting(agent.messages, agent.reflected):
        return
    agent.reflected = len(agent.messages)
    c = agent.console
    from lcode.ollama import OllamaError

    try:
        with c.status("[cyan]Looking for anything worth remembering…[/] [dim](Ctrl+C to skip)[/]", spinner="dots"):
            notes = propose(agent)
    except KeyboardInterrupt:
        c.print("[dim]Skipped the memory check.[/]")
        return
    except OllamaError as e:
        c.print(f"[dim]Couldn't check for anything worth remembering: {escape(str(e))}[/]")
        return
    if not notes:
        return
    memory = agent.memory()
    if agent.settings.memory == "ask":
        rows = []
        for i, note in enumerate(notes, 1):
            where = "every repository" if note.scope == "user" else "this repository"
            update = memory.find(note.name, note.scope)
            rows.append(
                f"[cyan]{i}[/] \\[{note.type}] {escape(note.description)} [dim]({where}"
                f"{', updates ' + escape(update.name) if update else ''})[/]"
                + (f"\n    [dim]{escape(note.details)}[/]" if note.details else "")
            )
        c.print(Panel("\n".join(rows), title="Worth remembering?", title_align="left", border_style="blue"))
        try:
            answer = input("  Save them? [y]es / numbers to keep, e.g. 1,3 / [n]o: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("n", "no", "none"):
            return
        if answer not in ("", "y", "yes", "a", "all"):
            keep = {int(x) for x in re.findall(r"\d+", answer)}
            notes = [n for i, n in enumerate(notes, 1) if i in keep]
    for note in notes:
        saved, updated = memory.save(note)
        c.print(Text(f"  {'Updated' if updated else 'Remembered'}: {saved.description}", style="dim"))
