"""Custom slash commands and skills, and the approval of those a repository brings.

Commands are prompt templates: `.lcode/commands/<name>.md` in a repository or
`~/.config/lcode/commands/<name>.md`, run as `/name args`. `$ARGUMENTS` in the template becomes
everything after the name, `$1`…`$9` single words. An optional frontmatter gives a description,
an argument hint for /help and the tools the model may use for that request.

Skills follow the Agent Skills format (https://agentskills.io): a folder with a SKILL.md
(frontmatter with name and description, then instructions) and any scripts or reference files next
to it. lcode looks in `.lcode/skills/`, `.agents/skills/` and `.claude/skills/` of the repository and
the same places in the user's home (`~/.config/lcode/skills/` for lcode's own). Only the names and
descriptions are in the system prompt; the instructions load when the model calls the skill tool for
a task that matches, or when the user runs `/name`. Bundled files are listed, not read, until needed.

A repository's commands, skills and agents can steer the model and bring scripts it may run, so
lcode asks before using them the first time, and again whenever they change.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape

from lcode import config
from lcode.checkpoints import work_tree_for
from lcode.frontmatter import split

if TYPE_CHECKING:
    from rich.console import Console

    from lcode.agent import Agent

SKILL_FOLDERS = (".lcode/skills", ".agents/skills", ".claude/skills")  # in a repository, first found wins
PROJECT_FOLDERS = (".lcode/commands", ".lcode/agents", *SKILL_FOLDERS)  # what needs approval
MAX_SKILLS_PROMPT = 6000  # characters of skill descriptions in the system prompt (~2K tokens)
MAX_DESCRIPTION = 300  # characters of one skill's description in the system prompt
MIN_DESCRIPTION = 40  # with many skills, descriptions are cut down to this rather than skills left out
MAX_RESOURCES = 50
MAX_FILES = 2000  # files looked at when fingerprinting a repository's extensions
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}

SKILLS_PROMPT = """
# Skills
Skills are instructions for specific kinds of tasks. When a task matches a skill's description, call the skill tool with its name first, then follow the instructions it returns. Paths in a skill are relative to its folder; use the absolute paths it gives.
{catalog}
"""


class ExtensionError(ValueError):
    pass


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    template: str
    path: Path
    scope: str  # project | user
    argument_hint: str = ""
    tools: frozenset[str] | None = None

    def render(self, args: str) -> str:
        """The request: the template with $ARGUMENTS and $1…$9 filled in."""
        words = args.split()
        text = self.template
        used = "$ARGUMENTS" in text or re.search(r"\$[1-9]", text)
        text = text.replace("$ARGUMENTS", args)
        text = re.sub(r"\$([1-9])", lambda m: words[int(m.group(1)) - 1] if int(m.group(1)) <= len(words) else "", text)
        if args and not used:
            text = f"{text.rstrip()}\n\n{args}"
        return text.strip()


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path  # the SKILL.md
    scope: str

    @property
    def folder(self) -> Path:
        return self.path.parent

    def instructions(self) -> str:
        parts = split(self.path.read_text(errors="replace"))
        return (parts[1] if parts else self.path.read_text(errors="replace")).strip()

    def resources(self) -> list[str]:
        found = []
        for root, dirs, files in os.walk(self.folder):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(files):
                path = Path(root) / name
                if path != self.path:
                    found.append(path.relative_to(self.folder).as_posix())
                if len(found) > MAX_RESOURCES:
                    return found
        return found


# ----------------------------------------------------------------------------- loading


def user_skill_folders(which: str = "all") -> list[Path]:
    home = Path.home()
    other_agents = [home / ".agents" / "skills", home / ".claude" / "skills"] if which == "all" else []
    return [config.CONFIG_DIR / "skills", *other_agents]


def load_commands(cwd: Path, include_project: bool) -> tuple[dict[str, Command], list[str]]:
    folders = [(config.CONFIG_DIR / "commands", "user")]
    if include_project:
        folders.append((work_tree_for(cwd) / ".lcode" / "commands", "project"))
    commands: dict[str, Command] = {}
    problems = []
    for folder, scope in folders:  # the repository's override the user's
        for f in sorted(folder.glob("*.md")) if folder.is_dir() else []:
            try:
                commands[f.stem] = parse_command(f, scope)
            except (ExtensionError, OSError) as e:
                problems.append(f"{f}: {e}")
    return commands, problems


def parse_command(path: Path, scope: str) -> Command:
    from lcode.tools import TOOL_NAMES

    name = path.stem
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise ExtensionError("the file name must be lowercase letters, digits, - or _")
    text = path.read_text(errors="replace")
    fields, body = split(text) or ({}, text)
    if not body.strip():
        raise ExtensionError("the command has no prompt")
    tools = None
    listed = fields.get("allowed-tools") or fields.get("tools")
    if listed:
        tools = frozenset(t for t in re.split(r"[,\s]+", listed.strip("[]")) if t)
        unknown = tools - TOOL_NAMES
        if unknown:
            raise ExtensionError(
                f"unknown tools: {', '.join(sorted(unknown))} (lcode's tools: {', '.join(sorted(TOOL_NAMES))})"
            )
    first = next((line.strip() for line in body.strip().splitlines() if line.strip()), "")
    return Command(
        name=name,
        description=" ".join(fields.get("description", "").split()) or first[:80],
        template=body.strip(),
        path=path,
        scope=scope,
        argument_hint=fields.get("argument-hint", ""),
        tools=tools,
    )


def load_skills(cwd: Path, include_project: bool, which: str = "all") -> tuple[dict[str, Skill], list[str]]:
    """Skills from lcode's folders and, with `which` = all, the folders other agents share."""
    if which == "off":
        return {}, []
    places = [(folder, "user") for folder in user_skill_folders(which)]
    if include_project:
        root = work_tree_for(cwd)
        places += [(root / folder, "project") for folder in (SKILL_FOLDERS if which == "all" else SKILL_FOLDERS[:1])]
    skills: dict[str, Skill] = {}
    problems = []
    for folder, scope in places:
        for skill_file in sorted(folder.glob("*/SKILL.md")) if folder.is_dir() else []:
            try:
                skill = parse_skill(skill_file, scope)
            except (ExtensionError, OSError) as e:
                problems.append(f"{skill_file}: {e}")
                continue
            existing = skills.get(skill.name)
            if existing and existing.scope == scope:
                if not same_file(existing.path, skill_file):  # the same skill installed twice is fine
                    problems.append(f"{skill_file}: skipped, {existing.path} has the same name")
                continue
            skills[skill.name] = skill  # a repository's skill replaces the user's
    return skills, problems


def same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def parse_skill(path: Path, scope: str) -> Skill:
    parts = split(path.read_text(errors="replace"))
    if not parts:
        raise ExtensionError("SKILL.md needs a frontmatter with a name and a description")
    fields = parts[0]
    description = " ".join(fields.get("description", "").split())
    if not description:
        raise ExtensionError("the frontmatter needs a description")
    # Lenient, like other clients: a name that breaks the format rules still works, as the folder name.
    name = fields.get("name", "").strip() or path.parent.name
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
        name = path.parent.name
    return Skill(name=name, description=description, path=path, scope=scope)


# ----------------------------------------------------------------------------- using skills


def skills_prompt(skills: dict[str, Skill], context: int = 262144) -> str:
    """The skill catalog for the system prompt: within ~5% of the context window, every skill listed
    (descriptions are shortened first), and only then cut off."""
    if not skills:
        return ""
    budget = min(MAX_SKILLS_PROMPT, context * 3 // 20)
    room = budget // len(skills) - 6
    lines, used = [], 0
    for skill in skills.values():
        limit = max(MIN_DESCRIPTION, min(MAX_DESCRIPTION, room - len(skill.name)))
        description = skill.description
        if len(description) > limit:
            description = description[: limit - 1].rstrip() + "…"
        line = f"- {skill.name}: {description}"
        if used + len(line) > budget:
            lines.append(f"- … and {len(skills) - len(lines)} more (the skill tool lists them all)")
            break
        lines.append(line)
        used += len(line) + 1
    return SKILLS_PROMPT.format(catalog="\n".join(lines))


def schema(skills: dict[str, Skill]) -> dict:
    from lcode.tools import _fn

    return _fn(
        "skill",
        "Load a skill's instructions when the task matches its description (see Skills). Returns the "
        "instructions, the skill's folder and the files in it.",
        {"name": {"type": "string", "enum": sorted(skills)}},
        ["name"],
    )


def content(skill: Skill) -> str:
    """What the model gets when a skill is activated: instructions, folder and bundled files."""
    files = skill.resources()
    listing = "\n".join(f"  {f}" for f in files[:MAX_RESOURCES])
    if len(files) > MAX_RESOURCES:
        listing += "\n  … (more files; list the folder to see them)"
    return (
        f'<skill_content name="{skill.name}">\n{skill.instructions()}\n\n'
        f"Skill folder: {skill.folder}\nRelative paths in this skill are relative to that folder: use "
        "absolute paths in tool calls.\n"
        + (f"<skill_resources>\n{listing}\n</skill_resources>\n" if files else "")
        + "</skill_content>"
    )


def activate(agent: Agent, name: str) -> str:
    skills = agent.extensions().skills
    if name not in skills:
        return f"Error: no skill {name!r}. Skills: {', '.join(sorted(skills)) or 'none'}"
    if name in agent.skills_loaded:
        return f"The {name} skill's instructions are already loaded above; follow them."
    try:
        text = content(skills[name])
    except OSError as e:
        return f"Error: can't read the {name} skill: {e}"
    agent.skills_loaded.add(name)
    return text


# ----------------------------------------------------------------------------- approving a repository's extensions


def approvals_path() -> Path:
    return config.STATE_DIR / "extensions-approved.json"


def project_files(cwd: Path) -> tuple[Path, list[Path]]:
    """The repository's root and the files of its commands, skills and agents."""
    root = work_tree_for(cwd)
    files: list[Path] = []
    for folder in PROJECT_FOLDERS:
        base = root / folder
        if not base.is_dir():
            continue
        for current, dirs, names in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            files += [Path(current) / n for n in sorted(names)]
            if len(files) >= MAX_FILES:
                return root, files[:MAX_FILES]
    return root, files


def fingerprint(root: Path, files: list[Path]) -> str:
    digest = hashlib.sha256()
    for f in files:
        digest.update(f.relative_to(root).as_posix().encode() + b"\0")
        try:
            digest.update(hashlib.sha256(f.read_bytes()).digest())
        except OSError:
            digest.update(b"?")
    return digest.hexdigest()


def _approved() -> dict:
    try:
        data = json.loads(approvals_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def status(cwd: Path) -> str:
    """none (the repository brings nothing), approved, new or changed."""
    root, files = project_files(cwd)
    if not files:
        return "none"
    seen = _approved().get(str(root))
    if seen is None:
        return "new"
    return "approved" if seen == fingerprint(root, files) else "changed"


def approve(cwd: Path) -> None:
    root, files = project_files(cwd)
    data = _approved()
    data[str(root)] = fingerprint(root, files)
    approvals_path().parent.mkdir(parents=True, exist_ok=True)
    approvals_path().write_text(json.dumps(data, indent=2))


def describe(cwd: Path) -> list[str]:
    """What the repository brings, one line per kind."""
    from lcode.subagents import parse_agent

    root = work_tree_for(cwd)
    commands, _ = load_commands(cwd, include_project=True)
    skills, _ = load_skills(cwd, include_project=True)
    agents = []
    for f in sorted((root / ".lcode" / "agents").glob("*.md")):
        try:
            agents.append(parse_agent(f.read_text(errors="replace"), f.stem, str(f)).name)
        except (ValueError, OSError):
            continue
    lines = []
    project_commands = [f"/{c.name}" for c in commands.values() if c.scope == "project"]
    project_skills = [s.name for s in skills.values() if s.scope == "project"]
    if project_commands:
        lines.append(f"commands: {', '.join(project_commands)}")
    if project_skills:
        lines.append(f"skills: {', '.join(project_skills)}")
    if agents:
        lines.append(f"agents: {', '.join(agents)}")
    return lines


def trust_project(cwd: Path, console: Console, interactive: bool) -> bool:
    """Whether to use the repository's commands, skills and agents; asks the first time."""
    state = status(cwd)
    if state in ("none", "approved"):
        return state == "approved"
    root = work_tree_for(cwd)
    if not interactive:
        console.print(
            f"[dim]Not using the commands, skills and agents in {escape(str(root))} until you approve them in a session.[/]"
        )
        return False
    changed = " [yellow](changed since you approved them)[/]" if state == "changed" else ""
    console.print(f"This repository brings its own commands, skills or agents{changed}:")
    for line in describe(cwd) or ["(files in " + ", ".join(PROJECT_FOLDERS) + ")"]:
        console.print(f"  {escape(line)}")
    console.print(
        "[dim]They can steer the model and include scripts it may run (commands still ask first, unless "
        "you allow them).[/]"
    )
    try:
        answer = input("  Use them? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer in ("y", "yes"):
        approve(cwd)
        return True
    console.print("[dim]Not using them in this session. You'll be asked again next time.[/]")
    return False


@dataclass
class Extensions:
    commands: dict[str, Command]
    skills: dict[str, Skill]
    problems: list[str]


def load(cwd: Path, include_project: bool, skills_from: str = "all") -> Extensions:
    commands, command_problems = load_commands(cwd, include_project)
    skills, skill_problems = load_skills(cwd, include_project, skills_from)
    return Extensions(commands, skills, command_problems + skill_problems)
