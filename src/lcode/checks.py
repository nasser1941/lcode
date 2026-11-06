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
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from lcode.permissions import is_read_only

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


VENVS = (".venv", "venv", "env")
PYTEST_USE = re.compile(r"^\s*(?:import pytest\b|from pytest\b)|^\s*@pytest\.", re.M)


def python(root: Path | None = None) -> str:
    """The project's own Python (its .venv, venv or env), where its dependencies are; else the one on PATH."""
    if root is not None:
        for env in VENVS:
            for exe in ("bin/python", "Scripts/python.exe"):
                if (root / env / exe).is_file():
                    return f"{env}/{exe}"  # the checks run in the project's root
    return "python" if shutil.which("python") else "python3"


def uses_pytest(root: Path) -> bool:
    """Whether the tests are written for pytest (they import it), or the requirements list it, without a config."""
    for name in ("requirements.txt", "requirements-dev.txt", "requirements_dev.txt", "dev-requirements.txt"):
        if re.search(r"^\s*pytest\b", text(root / name), re.M | re.I):
            return True
    return any(PYTEST_USE.search(text(path)[:20000]) for path in test_files(root)[:50])


def text(path: Path) -> str:
    try:
        return path.read_text(errors="replace") if path.is_file() else ""
    except OSError:
        return ""


def test_files(root: Path, limit: int = 200) -> list[Path]:
    """The project's Python test files, a few levels deep, outside environments and caches."""
    from lcode.tool_helpers import IGNORE_DIRS

    found: list[Path] = []

    def walk(folder: Path, depth: int) -> None:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return
        found.extend(e for e in entries if e.is_file() and re.fullmatch(r"test_.*\.py|.*_test\.py", e.name))
        if depth > 0:
            for e in entries:
                if len(found) >= limit:
                    return
                if e.is_dir() and e.name not in IGNORE_DIRS and e.name not in VENVS and not e.name.startswith("."):
                    walk(e, depth - 1)

    walk(root, 3)
    return found[:limit]


def python_tests(root: Path) -> bool:
    """Whether the project has Python test files (a few levels deep, outside environments and caches)."""
    return bool(test_files(root, limit=1))


def detect(root: Path) -> Check | None:
    """A check command from what the project looks like. Python checks need test files: pytest configured in a
    project without tests "fails" with nothing to run."""
    if python_tests(root):
        py = python(root)
        configs = text(root / "pyproject.toml") + text(root / "setup.cfg") + text(root / "tox.ini")
        if "pytest" in configs or any((root / name).is_file() for name in ("pytest.ini", "conftest.py")):
            runner = "uv run pytest -q" if (root / "uv.lock").is_file() else f"{py} -m pytest -q"
            return Check(runner, "pytest in the project's configuration")
        if uses_pytest(root):
            return Check(f"{py} -m pytest -q", "tests written for pytest")
        if list(root.glob("test_*.py")) or list(root.glob("*_test.py")):
            return Check(f"{py} -m unittest", "test files")
        if (root / "tests").is_dir():
            # `-t .` lets a package of tests use relative imports. Without __init__.py, it doesn't start at all.
            top = " -t ." if (root / "tests" / "__init__.py").is_file() else ""
            return Check(f"{py} -m unittest discover -s tests{top}", "tests/")
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


KEEPS_EXIT_CODE = re.compile(r"\d*>&\d+|&>>?|&&")  # redirections and `&&`, after the check
HIDES_EXIT_CODE = re.compile(r"[|;&\n]")  # a pipe, `||`, `;`, `&` or another line: the exit code isn't the check's
RUN_FAILED = re.compile(r"\b[1-9]\d* (?:failed|errors?)\b|^(?:FAILED|ERROR)\b", re.M)
RUN_PASSED = re.compile(r"\b[1-9]\d* passed\b|^OK\b", re.M)


def run_passed(check: Check, command: str, result: str) -> bool:
    """Whether the model's own run of the check passed. `pytest -q | tail` exits with tail's 0 when tests fail, so when
    the exit code isn't the check's, only the output can say: a test runner's summary, with nothing failing."""
    if exit_code(result) != 0:
        return False
    found = re.search(r"\s+".join(map(re.escape, check.command.split())), command)
    after = KEEPS_EXIT_CODE.sub(" ", command[found.end() :]).rstrip() if found else ""
    if not HIDES_EXIT_CODE.search(after):
        return True
    return RUN_PASSED.search(result) is not None and RUN_FAILED.search(result) is None


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


# ----------------------------------------------------------------------------- code written, never run

# Not .sql: one can be a migration, and asking the model to run that could change a real database.
CODE_SUFFIXES = frozenset(
    {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".rs", ".go", ".c", ".cc", ".cpp", ".java", ".kt", ".rb",
     ".php", ".pl", ".lua", ".jl", ".r", ".scala", ".swift", ".cs", ".sparql", ".rq"}
)  # fmt: skip
SPARQL_SUFFIXES = (".sparql", ".rq")
# Checks that read the code without running it (syntax, lint, types): they don't show that it works.
STATIC_ONLY = re.compile(
    r"\bast\.parse\b|\bpy_compile\b|\bcompileall\b|(?:^|\s)-fsyntax-only\b|\b(?:node|deno)\s+(?:-c|--check)\b"
    r"|\bphp\s+-l\b|\b(?:ruby|perl)\s+-c\b"
    r"|\b(?:pyflakes|flake8|pylint|mypy|pyright|basedpyright|ruff|eslint|tsc)\b|\bcargo\s+check\b|\bgo\s+vet\b"
)
# A test runner runs the code it tests, whether or not the command names a file.
TEST_RUNNER = re.compile(
    r"\b(?:pytest|py\.test|unittest|tox|nox|jest|vitest|mocha|ctest|phpunit|rspec)\b"
    r"|\b(?:cargo|go|npm|pnpm|yarn|bun|deno|mvn|gradle|dotnet|swift|mix|make)\s+(?:run\s+)?test\b"
)
# The program, or a module the code needs, isn't installed: the code didn't run.
NOT_INSTALLED = re.compile(r"command not found|^\S+: not found$|No module named|Cannot find module", re.I | re.M)
NOT_FOUND = re.compile(r"(?:^|:\s)([\w.+-]+): (?:command )?not found\s*$", re.M)


def is_code(rel: str) -> bool:
    """Whether a file is a program, module or query in a language that runs (shell scripts have their own check)."""
    return Path(rel).suffix.lower() in CODE_SUFFIXES


def exercised(command: str, result: str, files: Iterable[str]) -> list[str]:
    """The files among `files` that a shell command ran: it names them, or their module, or runs tests, and its
    program was there. Syntax, lint and type checks, and commands that only look, don't count."""
    if is_read_only(command) or STATIC_ONLY.search(command):
        return []
    if exit_code(result) == 127 or any(NOT_INSTALLED.search(line) for line in result.splitlines()[:20]):
        return []
    if TEST_RUNNER.search(command):
        return list(files)
    return [rel for rel in files if re.search(rf"(?<![\w-]){re.escape(Path(rel).stem)}(?![\w-])", command)]


def not_found(result: str) -> list[str]:
    """The programs a shell command's output says aren't installed (`bash: line 1: python3: command not found`)."""
    return list(dict.fromkeys(NOT_FOUND.findall(result)))


def uv_available() -> bool:
    return shutil.which("uv") is not None


def untested_note(files: list[str], missing: list[str], uv: bool) -> str:
    names = ", ".join(files)
    it = "it" if len(files) == 1 else "them"
    note = (
        f"[lcode] You wrote {names} in this request but haven't run {it} since your last change. Run {it} now and fix "
        f"what fails, so you know {it} works before you say so: for a module, import it and call what you wrote; "
        "for a query, run it on its data; for a server or anything else that keeps running, start it in the "
        "background and try it. A syntax check doesn't show that it works."
    )
    if missing:
        note += f" {', '.join(f'`{m}`' for m in missing)} isn't installed here."
    sparql = any(f.lower().endswith(SPARQL_SUFFIXES) for f in files)
    if uv and (sparql or any(f.endswith(".py") for f in files)):
        note += (
            " To run Python code without changing this machine's Python, `uv run --with PACKAGE python FILE` fetches "
            "Python and the packages it needs into a throwaway environment."
        )
        if sparql:
            note += " For a SPARQL query, the package is `rdflib`."
    return note + f" If you can't run {it}, say plainly in your answer that {it} is untested, and why."


# ----------------------------------------------------------------------------- windows it never looked at

GUI_IMPORT = re.compile(r"^\s*(?:from|import)\s+(PyQt[56]|PySide[26]|tkinter|kivy|wx|gi\.repository)\b", re.M)
LOOKED = frozenset({"screenshot", "view_image"})


def unseen_windows(root: Path, changed: list[str], tools_used: set[str]) -> dict[str, str]:
    """Python files that draw a window (they import a GUI toolkit), changed in this request when the model never
    looked at one: file -> toolkit.

    A small model checks a window change by importing the module and running unrelated tests, then calls it done;
    the user finds a blank window. Tests and imports don't show what's on the screen; a screenshot does.
    """
    if tools_used & LOOKED:
        return {}
    found = {}
    for rel in changed:
        path = root / rel
        if path.suffix != ".py" or not path.is_file():
            continue
        try:
            text = path.read_text(errors="replace")[:200_000]
        except OSError:
            continue
        match = GUI_IMPORT.search(text)
        if match:
            found[rel] = match.group(1)
    return found


def unseen_note(found: dict[str, str]) -> str:
    names = ", ".join(found)
    kits = sorted(set(found.values()))
    qt = any(kit.startswith(("PyQt", "PySide")) for kit in kits)
    how = (
        "take a screenshot with the screenshot tool: it runs the program without a screen and shows what it draws, "
        "and what the program printed (Qt names layout mistakes there). For something that only shows after a click "
        'or a key, give actions, e.g. actions=["double_click Camera 1"]'
        if qt
        else "take a screenshot with the screenshot tool: it runs the program on a virtual display and shows what it "
        "draws"
    )
    return (
        f"[lcode] You changed {names}, which draws a window ({', '.join(kits)}), but never looked at it: tests and "
        f"imports don't show what's on the screen. Before you say it works, {how}. Check the picture against what the "
        "user asked for. If the program can't run here, tell the user plainly that you haven't seen it working."
    )
