"""Permission prompts for file edits and shell commands."""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable

from rich.console import Console, RenderableType
from rich.panel import Panel

from lcode.config import PERMISSION_MODES

# Commands that only read state run without asking (when used without redirection or chaining).
SAFE_COMMANDS = {
    "ls", "cat", "head", "tail", "wc", "pwd", "grep", "rg", "find", "tree", "file", "stat", "du", "df",
    "echo", "which", "whoami", "uname", "date", "sort", "uniq", "cut", "nl", "diff", "basename", "dirname",
    "realpath", "readlink", "nvidia-smi", "sw_vers",
}  # fmt: skip
SAFE_GIT = {"status", "log", "diff", "show", "branch", "ls-files", "rev-parse", "remote", "blame", "shortlog"}
# For these, "always allow" is remembered per subcommand (e.g. `git commit`, not all of `git`).
MULTI_WORD = {"git", "npm", "pnpm", "yarn", "pip", "uv", "docker", "cargo", "go", "kubectl", "python", "python3", "brew"}  # fmt: skip  # noqa: E501


def bash_key(command: str) -> str:
    """The key "always allow" remembers: the program (plus subcommand for tools like git), ignoring `cd dir &&`."""
    command = re.sub(r"^\s*(cd\s+\S+\s*&&\s*)+", "", command)
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    if not words:
        return "bash:"
    if words[0] in MULTI_WORD and len(words) > 1:
        if words[1] in ("-m", "-c") and len(words) > 2:  # python -m <module>
            return f"bash:{' '.join(words[:3])}"
        return f"bash:{words[0]} {words[1]}"
    return f"bash:{words[0]}"


# `cd <plain path> &&` in front of a command: harmless, and models write it all the time.
NOT_ASKED = (
    "This needs the user's permission, and nobody can be asked in this run, so it was not allowed. Do without it "
    "if you can; otherwise finish and say what permission you need."
)
LEADING_CD = re.compile(r"""^\s*(?:cd\s+(?:[\w./~@+,=:-]+|'[^']*'|"[^"$`\\]*")\s*&&\s*)+""")


def is_read_only(command: str) -> bool:
    command = LEADING_CD.sub("", command)
    if any(tok in command for tok in (";", "&", ">", "`", "$(", "<(")) or "\n" in command:
        return False
    if re.search(r"-exec|-delete|-ok\b|-fprint", command):
        return False
    for segment in command.split("|"):
        try:
            words = shlex.split(segment)
        except ValueError:
            return False
        if not words:
            return False
        if words[0] == "git":
            if len(words) < 2 or words[1] not in SAFE_GIT:
                return False
        elif words[0] not in SAFE_COMMANDS:
            return False
    return True


class Permissions:
    def __init__(self, console: Console, mode: str = "ask"):
        from lcode.hooks import Rules

        self.console = console
        self.mode = mode
        self.always: set[str] = set()
        self.rules = Rules()  # allow and deny lists from the settings (lcode.hooks)
        self.on_prompt = None  # called with the title before lcode asks the user (notification hooks)
        self.approve: Callable[[dict], bool] | None = None  # decides instead of asking (see lcode.api)

    def rule(self, kind: str, target: str) -> tuple[bool, str] | None:
        """A rule's verdict on an action: (allowed, message for the model), or None when no rule matches."""
        verdict, which = self.rules.check(kind, target) if target else (None, "")
        if verdict == "deny":
            return False, f"A deny rule blocks this: {which}. Don't try it another way; tell the user if it's needed."
        if verdict == "allow":
            return True, ""
        return None

    def cycle(self) -> None:
        self.mode = PERMISSION_MODES[(PERMISSION_MODES.index(self.mode) + 1) % len(PERMISSION_MODES)]

    def needs_prompt(self, key: str, kind: str) -> bool:
        if key in self.always or (self.mode == "yolo" and kind != "memory"):
            return False
        return not (kind == "edit" and self.mode == "auto-edit")

    def request(self, key: str, kind: str, title: str, body: RenderableType, target: str = "") -> tuple[bool, str]:
        """Ask the user. kind is 'edit', 'bash', 'web', 'mcp' or 'memory'. Returns (allowed, message for the model).

        Memory notes follow the memory setting rather than the mode: with `memory = ask`, even yolo asks.
        """
        verdict = self.rule(kind, target)
        if verdict is not None:
            return verdict
        if not self.needs_prompt(key, kind):
            return True, ""
        if self.approve is not None:  # a program or a non-interactive run decides
            target = target or key.split(":", 1)[-1]
            if kind == "bash":
                target = LEADING_CD.sub("", target)  # the command itself, without `cd <folder> &&`
            if self.approve({"kind": kind, "title": title, "target": target}):
                return True, ""
            return False, NOT_ASKED
        if self.on_prompt:
            self.on_prompt(title)
        self.console.print(Panel(body, title=title, title_align="left", border_style="yellow"))
        scope = {"edit": "file edits", "memory": "memory notes"}.get(kind) or key.split(":", 1)[-1]
        if kind == "mcp":
            scope = scope.replace(":", " › ", 1)
        try:
            answer = input(f"  Allow? [y]es / [a]lways for '{scope}' this session / [n]o (+ optional reason): ")
        except EOFError:
            return False, "The user denied this action (no input available)."
        answer = answer.strip()
        if answer.lower() in ("y", "yes", ""):
            return True, ""
        if answer.lower() in ("a", "always"):
            self.always.add("edit" if kind == "edit" else key)
            return True, ""
        # "n", "no", "n <reason>" or any other text, which is taken as the reason.
        reason = re.sub(r"^(no|n)\b[\s,:-]*", "", answer, flags=re.I).strip()
        message = "The user denied this action."
        if reason:
            return False, f"{message} User says: {reason}"
        return False, f"{message} Ask the user how to proceed or choose a different approach."
