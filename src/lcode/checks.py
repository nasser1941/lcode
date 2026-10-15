"""Run the project's checks before calling work done.

Small models call work done after a syntax check, and telling them to verify isn't enough. So after a request
that changed files, lcode runs the project's check command itself (its tests, or a linter) before the answer
counts as final. When it fails, the output goes back to the model, which keeps working, at most `MAX_ROUNDS`
times; then the answer says plainly that the checks still fail.

The command is, in order: the repository's `.lcode/settings.toml` (`[checks] command = "…"`, used once the
repository is trusted, like its hooks), the user's `checks` setting, or what the project looks like: pytest,
unittest, npm test, cargo test, go test, make test. `checks = "off"` turns it off.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

MAX_ROUNDS = 2  # times the model gets failing checks back before lcode stops asking
TIMEOUT = 600  # seconds a check may run
OUTPUT_TAIL = 6000  # characters of a failing check's output the model gets back
EXIT = re.compile(r"\[exit code: (-?\d+)\]\s*$")


@dataclass
class Check:
    command: str
    source: str  # where it comes from, for the user: "pyproject.toml", "your configuration"…


def find(root: Path, setting: str, trusted: bool) -> Check | None:
    """The project's check command, or None when there's none or checks are off."""
    setting = (setting or "auto").strip()
    if setting == "off":
        return None
    if trusted:
        from lcode.hooks import HookError, read_toml

        try:
            data = read_toml(root / ".lcode" / "settings.toml")
        except HookError:
            data = {}
        command = str((data.get("checks") or {}).get("command") or "").strip()
        if command:
            return Check(command, ".lcode/settings.toml")
    if setting != "auto":
        return Check(setting, "your configuration (checks)")
    return detect(root)


def python() -> str:
    return "python" if shutil.which("python") else "python3"


def text(path: Path) -> str:
    try:
        return path.read_text(errors="replace") if path.is_file() else ""
    except OSError:
        return ""


def python_tests(root: Path) -> bool:
    """Whether the project has Python test files (a few levels deep, outside environments and caches)."""
    from lcode.tools import IGNORE_DIRS

    def walk(folder: Path, depth: int) -> bool:
        try:
            entries = list(folder.iterdir())
        except OSError:
            return False
        if any(e.is_file() and re.fullmatch(r"test_.*\.py|.*_test\.py", e.name) for e in entries):
            return True
        return depth > 0 and any(
            walk(e, depth - 1)
            for e in entries
            if e.is_dir() and e.name not in IGNORE_DIRS and not e.name.startswith(".")
        )

    return walk(root, 3)


def detect(root: Path) -> Check | None:
    """A check command from what the project looks like. Python checks need test files: pytest configured in a
    project without tests "fails" with nothing to run."""
    if python_tests(root):
        configs = text(root / "pyproject.toml") + text(root / "setup.cfg") + text(root / "tox.ini")
        if "pytest" in configs or any((root / name).is_file() for name in ("pytest.ini", "conftest.py")):
            runner = "uv run pytest -q" if (root / "uv.lock").is_file() else f"{python()} -m pytest -q"
            return Check(runner, "pytest in the project's configuration")
        if list(root.glob("test_*.py")) or list(root.glob("*_test.py")):
            return Check(f"{python()} -m unittest", "test files")
        if (root / "tests").is_dir():
            return Check(f"{python()} -m unittest discover -s tests -t .", "tests/")
    package = text(root / "package.json")
    if package:
        try:
            test = (json.loads(package).get("scripts") or {}).get("test") or ""
        except ValueError:
            test = ""
        if test and "no test specified" not in test:
            for lock, tool in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun")):
                if (root / lock).is_file():
                    return Check(f"{tool} test", "package.json")
            return Check("npm test", "package.json")
    if (root / "Cargo.toml").is_file():
        return Check("cargo test", "Cargo.toml")
    if (root / "go.mod").is_file():
        return Check("go test ./...", "go.mod")
    if re.search(r"^test:", text(root / "Makefile"), re.M):
        return Check("make test", "Makefile")
    return None


NO_TESTS = 5  # pytest's and unittest's exit code when no test ran: not a failure


def no_tests(check: Check, result: str) -> bool:
    return exit_code(result) == NO_TESTS and bool(re.search(r"pytest|unittest", check.command))


def exit_code(result: str) -> int | None:
    """The exit code at the end of a bash tool result (None: it didn't run, e.g. not allowed)."""
    found = EXIT.search(result)
    return int(found.group(1)) if found else None


def covers(check: Check, command: str) -> bool:
    """Whether a command the model ran is this check (`cd x && uv run pytest -q 2>&1 | tail` counts)."""
    return " ".join(check.command.split()) in " ".join(command.split())


def failure_note(check: Check, result: str, round_: int) -> str:
    output = result if len(result) <= OUTPUT_TAIL else "…" + result[-OUTPUT_TAIL:]
    last = round_ >= MAX_ROUNDS
    return (
        f"[lcode] Your changes are done, but the project's checks fail: `{check.command}` "
        f"(exit code {exit_code(result)}).\n\n{output}\n\n"
        + (
            "This was the last check lcode runs for this request. Don't change anything more: tell the user what "
            "still fails and why."
            if last
            else "Fix what your changes broke, then finish. If a failure isn't caused by your changes (it failed "
            "before), don't chase it: say so in your answer."
        )
    )


# The check command itself is missing (not the code under test): nothing the model can fix.
MISSING = re.compile(r"No module named '?pytest'?\s*$|command not found|^\S+: not found$", re.I | re.M)


def missing(check: Check, result: str) -> bool:
    """Whether the check couldn't run at all: its program isn't installed."""
    code = exit_code(result)
    if code == 127:
        return True
    head = result.splitlines()[:5]  # a missing program says so first; test failures look different
    return code not in (0, None) and any(MISSING.search(line) for line in head)


# ----------------------------------------------------------------------------- scripts written, never run

SCRIPT_SUFFIXES = (".sh", ".bash", ".zsh")
SHELL_SHEBANG = re.compile(r"^#!\S*?(?:/|env\s+)(?:ba|z|da|k)?sh\b")
RUNNERS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "source", ".", "exec"})
PREFIXES = frozenset({"sudo", "time", "nohup", "env", "command", "nice"})
SEGMENTS = re.compile(r"&&|\|\||[;|\n&()]")


def unrun_scripts(root: Path, changed: list[str], commands: list[str]) -> list[str]:
    """Shell scripts written in this request that no command in it ran.

    Small models write a build or install script and call it done without running it once; the first thing
    the user then sees is its first error. Only shell scripts count (a .sh file, or a shell shebang): a Python
    file with a shebang is as likely an app or a server that never ends.
    """
    unrun = []
    for rel in changed:
        path = root / rel
        if not is_shell_script(path):
            continue
        if not any(runs(command, path.name) for command in commands):
            unrun.append(rel)
    return unrun


def is_shell_script(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix in SCRIPT_SUFFIXES:
        return True
    if path.suffix:
        return False
    try:
        with path.open("rb") as f:
            first = f.readline(200).decode("utf-8", "replace")
    except OSError:
        return False
    return bool(SHELL_SHEBANG.match(first))


def runs(command: str, name: str) -> bool:
    """Whether a shell command runs the script called `name` (not just chmods, cats or commits it)."""
    for segment in SEGMENTS.split(command):
        words = segment.split()
        while words and (("=" in words[0] and not words[0].startswith("=")) or words[0] in PREFIXES):
            words.pop(0)
        if words and words[0] == "timeout":
            words = words[2:] if len(words) > 2 else []
        if not words:
            continue
        program = words[0].strip("'\"")
        if program in RUNNERS:
            rest = [w.strip("'\"") for w in words[1:] if not w.startswith("-")]
            program = rest[0] if rest else ""
        if Path(program).name == name:
            return True
    return False


def unrun_note(scripts: list[str]) -> str:
    names = ", ".join(scripts)
    it = "it" if len(scripts) == 1 else "them"
    return (
        f"[lcode] You wrote {names} in this request but never ran {it}. Run {it} now and fix what fails, so you know "
        f"{it} works before you say so. If running {it} would change things outside the project (install packages, "
        f"deploy, delete data) or needs something you don't have, don't: tell the user plainly that {it} is untested "
        "and why."
    )
