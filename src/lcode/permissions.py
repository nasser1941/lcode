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


# `cd <plain path> &&` in front of a command: harmless, and models write it all the time.
NOT_ASKED = (
    "This needs the user's permission, and nobody can be asked in this run, so it was not allowed. Do without it "
    "if you can; otherwise finish and say what permission you need."
)
LEADING_CD = re.compile(r"""^\s*(?:cd\s+(?:[\w./~@+,=:-]+|'[^']*'|"[^"$`\\]*")\s*&&\s*)+""")


# Redirections that write nothing anyone keeps: stderr into stdout, or output thrown away.
HARMLESS_REDIRECT = re.compile(r"(?<![\w>&])(?:[12]?>&[12]|[12]?>\s*/dev/null)(?=\s|$)")
OPERATORS = {"|", "||", "&&", ";", "&", ">", ">>", "<", "<<", "<<<", ">&", "<&", ">|", "(", ")", ";;"}


def split_command(command: str) -> tuple[list[list[str]], list[str]] | None:
    """The command's segments (as words) and the operators between them, quotes respected. None when it can't be
    read: command substitution, process substitution or unbalanced quotes."""
    if any(tok in command for tok in ("`", "$(", "<(", ">(")) or "\n" in command.strip():
        return None
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = [[]]
    operators: list[str] = []
    for token in tokens:
        if token in OPERATORS or (token and set(token) <= set("|&;<>()")):
            operators.append(token)
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s], operators


def segment_key(words: list[str]) -> str:
    """The program, plus the subcommand for tools like git (`git commit`, `python -m pytest`)."""
    while words and re.fullmatch(r"\w+=\S*", words[0]):
        words = words[1:]  # VAR=value in front
    if not words:
        return ""
    if words[0] in MULTI_WORD and len(words) > 1:
        if words[1] in ("-m", "-c") and len(words) > 2:  # python -m <module>
            return " ".join(words[:3])
        return f"{words[0]} {words[1]}"
    return words[0]


def bash_key(command: str) -> str:
    """What "always allow" remembers. For one program, the program (plus subcommand for git and the like); for a
    chain such as `npm test && rm -rf build`, every program in it, so allowing `npm test` doesn't allow the chain.
    `cd dir &&` in front doesn't count."""
    command = HARMLESS_REDIRECT.sub(" ", LEADING_CD.sub("", command)).strip()
    parsed = split_command(command)
    if parsed is None:
        return "bash:" + " ".join(command.split())  # can't be read: only this exact command
    segments, operators = parsed
    keys, redirected = [], False
    for i, words in enumerate(segments):
        before = operators[i - 1] if i else ""
        if before and before not in ("|", "&&", "||", ";", "&"):
            redirected = True  # a file name after > or <, not a program
            continue
        keys.append(segment_key(words))
    key = " + ".join(k for k in keys if k) + (" (redirected)" if redirected else "")
    return f"bash:{key}"


def option_values(words: list[str], *options: str) -> list[str]:
    """The values of options such as `-o FILE`, `-oFILE` or `--output=FILE`."""
    found = []
    for i, word in enumerate(words):
        for option in options:
            if word == option and i + 1 < len(words):
                found.append(words[i + 1])
            elif option.startswith("--") and word.startswith(option + "="):
                found.append(word.split("=", 1)[1])
            elif not option.startswith("--") and word.startswith(option) and len(word) > len(option):
                found.append(word[len(option) :])
    return found


def flags(words: list[str]) -> list[str]:
    return [w for w in words[1:] if w.startswith("-")]


def positional(words: list[str]) -> list[str]:
    return [w for w in words[1:] if not w.startswith("-")]


# Branch and remote listing; anything else creates, deletes, renames or reconfigures.
BRANCH_LISTING = {
    "-a", "--all", "-r", "--remotes", "-l", "--list", "-v", "-vv", "--verbose", "--show-current", "--color",
    "--no-color", "--column", "--no-column", "--abbrev", "--no-abbrev", "-i", "--ignore-case", "--omit-empty",
}  # fmt: skip
BRANCH_WITH_VALUE = {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format"}
NVIDIA_QUERY = ("-q", "--query", "-L", "--list-gpus", "--format", "-i", "--id", "-d", "--display", "-x", "--xml-format",
                "-h", "--help", "--query-gpu", "--query-compute-apps")  # fmt: skip


def safe_git(words: list[str]) -> bool:
    if len(words) < 2 or words[1] not in SAFE_GIT:
        return False  # also `git -c …` and `git -C …`, which can run anything
    sub, args = words[1], words[2:]
    if sub in ("log", "show", "diff", "shortlog") and option_values(words, "--output"):
        return False  # writes the output to a file
    if sub == "branch":
        listing = any(a in ("-l", "--list") for a in args)
        i = 0
        while i < len(args):
            arg = args[i]
            name = arg.split("=", 1)[0]
            if name in BRANCH_WITH_VALUE:
                i += 1 if "=" in arg else 2
                continue
            if arg.startswith("-") and name not in BRANCH_LISTING:
                return False  # -d, -D, -m, -c, -f, -u, --set-upstream-to, --edit-description, …
            if not arg.startswith("-") and not listing:
                return False  # `git branch name` creates a branch
            i += 1
    if sub == "remote":
        return not args or all(a in ("-v", "--verbose") for a in args) or args[0] in ("show", "get-url")
    return True


def safe_segment(words: list[str]) -> bool:
    program = words[0]
    if program == "git":
        return safe_git(words)
    if program not in SAFE_COMMANDS:
        return False
    if program == "sort" and (
        option_values(words, "-o", "--output") or any(f.startswith("--compress-program") for f in flags(words))
    ):
        return False
    if program == "uniq" and len(positional(words)) > 1:
        return False  # `uniq in out` writes out
    if program == "tree" and option_values(words, "-o"):
        return False
    if program == "rg" and any(f.split("=")[0] in ("--pre", "--pre-glob") for f in flags(words)):
        return False  # runs a program on each file
    if program == "find" and any(
        w in ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls") for w in words
    ):
        return False
    if program == "date" and (option_values(words, "-s", "--set") or "-s" in words):
        return False
    if program == "file" and any(f in ("-C", "--compile") for f in flags(words)):
        return False
    if program == "nvidia-smi":
        return all(f.split("=")[0] in NVIDIA_QUERY for f in flags(words))
    return True


def is_read_only(command: str) -> bool:
    """Whether a command only reads: known read-only programs with arguments that don't write, joined only by
    pipes. `cd dir &&` in front, `2>&1` and `>/dev/null` are fine."""
    command = HARMLESS_REDIRECT.sub(" ", LEADING_CD.sub("", command)).strip()
    parsed = split_command(command)
    if parsed is None:
        return False
    segments, operators = parsed
    if not segments or any(op != "|" for op in operators):
        return False
    return all(safe_segment(words) for words in segments)


class Permissions:
    def __init__(self, console: Console, mode: str = "ask"):
        from lcode.hooks import Rules

        self.console = console
        self.mode = mode
        self.always: set[str] = set()
        self.rules = Rules()  # allow and deny lists from the settings (lcode.hooks)
        self.on_prompt = None  # called with the title before lcode asks the user (notification hooks)
        # Decides instead of asking (lcode.api, lcode acp): True, False, or a message saying why not.
        self.approve: Callable[[dict], bool | str] | None = None

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
            verdict = self.approve({"kind": kind, "title": title, "target": target, "key": key})
            if verdict is True:
                return True, ""
            return False, verdict if isinstance(verdict, str) and verdict else NOT_ASKED
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
