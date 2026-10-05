"""Permission prompts for file edits and shell commands."""

from __future__ import annotations

import re
import shlex

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


def is_read_only(command: str) -> bool:
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
        self.console = console
        self.mode = mode
        self.always: set[str] = set()

    def cycle(self) -> None:
        self.mode = PERMISSION_MODES[(PERMISSION_MODES.index(self.mode) + 1) % len(PERMISSION_MODES)]

    def needs_prompt(self, key: str, kind: str) -> bool:
        if key in self.always or (self.mode == "yolo" and kind != "memory"):
            return False
        return not (kind == "edit" and self.mode == "auto-edit")

    def request(self, key: str, kind: str, title: str, body: RenderableType) -> tuple[bool, str]:
        """Ask the user. kind is 'edit', 'bash', 'web', 'mcp' or 'memory'. Returns (allowed, message for the model).

        Memory notes follow the memory setting rather than the mode: with `memory = ask`, even yolo asks.
        """
        if not self.needs_prompt(key, kind):
            return True, ""
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
