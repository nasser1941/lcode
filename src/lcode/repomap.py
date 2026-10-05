"""A repository map: the important symbols of a codebase in a few hundred lines.

On an unfamiliar codebase the model spends many steps finding its way around. The map gives it the
lay of the land at once: for each important file, its classes and functions with their signatures
and line numbers, ranked by how much of the rest of the code refers to them (an idea from aider's
repo map), and cut to a token budget.

Python is read with its own parser (ast); TypeScript, JavaScript, Go, Rust and Java with careful
patterns for their declarations. Small repositories get the whole map in the system prompt; larger
ones have a `repo_map` tool, which can also zoom into a folder.
"""

from __future__ import annotations

import ast
import math
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from lcode.checkpoints import _clean_env

SOURCE = {".py", ".pyi", ".ts", ".tsx", ".mts", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build", "target", ".next",
             "vendor", "third_party", ".tox", ".mypy_cache", ".pytest_cache", "site-packages", "coverage"}  # fmt: skip
MAX_FILES = 5000
MAX_FILE_BYTES = 300_000
PROMPT_TOKENS = 1500  # a map this small goes into the system prompt
TOOL_TOKENS = 3000  # the repo_map tool's budget
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
COMMON = {"__init__", "main", "run", "get", "set", "init", "setup", "test", "self", "cls", "new", "default", "index"}

PATTERNS = {
    "ts": [
        (re.compile(r"^\s*export\s+(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*(\([^)]*\))"), "function"),
        (re.compile(r"^(?:async\s+)?function\s*\*?\s*(\w+)\s*(\([^)]*\))"), "function"),
        (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)()"), "class"),
        (re.compile(r"^\s*(?:export\s+)?(interface|type|enum)\s+(\w+)"), "kind"),
        (
            re.compile(r"^\s*export\s+const\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s*)?(\([^)]*\))\s*(?::[^=]+)?=>"),
            "function",
        ),
        (re.compile(r"^\s*export\s+const\s+(\w+)()"), "const"),
        (
            re.compile(
                r"^  (?:(?:public|private|protected|static|readonly|async|override)\s+)*(\w+)\s*(\([^)]*\))\s*(?::[^{]+)?\{\s*$"
            ),
            "method",
        ),
    ],
    "go": [
        (re.compile(r"^func\s+\(\s*\w+\s+\*?(\w+)\s*\)\s+(\w+)\s*(\([^)]*\))"), "method"),
        (re.compile(r"^func\s+(\w+)\s*(\([^)]*\))"), "function"),
        (re.compile(r"^type\s+(\w+)\s+(struct|interface)\b"), "type"),
    ],
    "rs": [
        (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)\s*(?:<[^>]*>)?\s*(\([^)]*\))"), "function"),
        (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(struct|enum|trait)\s+(\w+)"), "kind"),
        (re.compile(r"^impl(?:<[^>]*>)?\s+(?:(\w+)\s+for\s+)?(\w+)"), "impl"),
    ],
    "java": [
        (
            re.compile(
                r"^\s*(?:public|protected|private|abstract|final|static|\s)*(class|interface|enum|record)\s+(\w+)"
            ),
            "kind",
        ),
        (
            re.compile(
                r"^\s{2,8}(?:public|protected|private)\s+(?:static\s+)?(?:final\s+)?[\w<>\[\], ?]+\s+(\w+)\s*(\([^)]*\))"
            ),
            "function",
        ),
        (
            re.compile(
                r"^\s*(?:(?:private|public|internal|override|suspend)\s+)*fun\s+(?:<[^>]*>\s*)?(\w+)\s*(\([^)]*\))"
            ),
            "function",
        ),
    ],
}
FAMILY = {".ts": "ts", ".tsx": "ts", ".mts": "ts", ".js": "ts", ".jsx": "ts", ".mjs": "ts", ".cjs": "ts",
          ".go": "go", ".rs": "rs", ".java": "java", ".kt": "java"}  # fmt: skip


@dataclass
class Symbol:
    name: str
    line: int
    text: str  # how it reads in the map, e.g. "def add(a, b)" or "class Cart(Base)"
    top: bool = True  # top level, not a method
    score: float = 0.0
    constant: bool = False


@dataclass
class FileMap:
    path: str
    symbols: list[Symbol] = field(default_factory=list)
    score: float = 0.0


# ----------------------------------------------------------------------------- reading files


def source_files(root: Path) -> list[Path]:
    """The repository's source files: git's list when it's a repository (it respects .gitignore)."""
    try:
        r = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root,
                           capture_output=True, text=True, timeout=30, env=_clean_env())  # fmt: skip
        names = [n for n in r.stdout.split("\0") if n] if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        names = None
    if names is None:
        names = []
        for path in sorted(root.rglob("*")):
            if len(names) > MAX_FILES * 4:
                break
            if path.is_file() and not set(path.relative_to(root).parts) & SKIP_DIRS:
                names.append(str(path.relative_to(root)))
    found = []
    for name in names:
        path = root / name
        if path.suffix.lower() in SOURCE and not set(Path(name).parts) & SKIP_DIRS:
            try:
                if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES:
                    found.append(path)
            except OSError:
                continue
        if len(found) >= MAX_FILES:
            break
    return found


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = ast.unparse(node.args)
    except Exception:  # very old or odd syntax
        args = "..."
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    text = f"{prefix} {node.name}({args}){returns}"
    return text if len(text) <= 160 else text[:159] + "…"


def python_symbols(text: str) -> list[Symbol]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    found = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append(Symbol(node.name, node.lineno, _signature(node)))
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            found.append(Symbol(node.name, node.lineno, f"class {node.name}" + (f"({bases})" if bases else "")))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and not (
                    item.name.startswith("_") and item.name != "__init__"
                ):
                    found.append(Symbol(item.name, item.lineno, _signature(item), top=False))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id.isupper() and len(target.id) > 2:
                    found.append(Symbol(target.id, node.lineno, target.id, constant=True))
    return found


def pattern_symbols(text: str, family: str) -> list[Symbol]:
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if len(line) > 400:
            continue
        for pattern, kind in PATTERNS[family]:
            m = pattern.match(line)
            if not m:
                continue
            groups = m.groups()
            if kind == "kind":  # (keyword, name)
                name, text_ = groups[1], f"{groups[0]} {groups[1]}"
            elif kind == "impl":
                name = groups[1]
                text_ = f"impl {groups[0]} for {groups[1]}" if groups[0] else f"impl {groups[1]}"
            elif kind == "type":
                name, text_ = groups[0], f"type {groups[0]} {groups[1]}"
            elif kind == "method" and family == "go":
                name, text_ = groups[1], f"func ({groups[0]}) {groups[1]}{' '.join(groups[2].split())}"
            elif kind in ("function", "method"):
                name = groups[0]
                args = " ".join(groups[1].split()) if len(groups) > 1 and groups[1] else ""
                keyword = {"go": "func ", "rs": "fn ", "ts": "function " if kind == "function" else ""}.get(family, "")
                text_ = f"{keyword}{name}{args}"
            else:
                name, text_ = groups[0], f"{kind} {groups[0]}"
            if name in ("if", "for", "while", "switch", "catch", "return", "constructor") and kind == "method":
                break
            text_ = text_ if len(text_) <= 160 else text_[:159] + "…"
            found.append(Symbol(name, number, text_, top=kind not in ("method",) and not line[:1].isspace()))
            break
    return found


def symbols_of(path: Path, text: str) -> list[Symbol]:
    suffix = path.suffix.lower()
    if suffix in (".py", ".pyi"):
        return python_symbols(text)
    family = FAMILY.get(suffix)
    return pattern_symbols(text, family) if family else []


# ----------------------------------------------------------------------------- ranking and rendering


def build(root: Path, files: list[Path] | None = None) -> list[FileMap]:
    """Every file's symbols, ranked: a symbol scores by how many other files mention it."""
    files = files if files is not None else source_files(root)
    maps: list[FileMap] = []
    mentions: dict[str, Counter] = {}
    for path in files:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        mentions[rel] = Counter(IDENTIFIER.findall(text))
        maps.append(FileMap(rel, symbols_of(path, text)))
    files_mentioning: Counter = Counter()
    for counts in mentions.values():
        files_mentioning.update(counts.keys())
    defined = Counter(s.name for m in maps for s in m.symbols)
    for m in maps:
        own = mentions.get(m.path, Counter())
        for s in m.symbols:
            elsewhere = files_mentioning[s.name] - (1 if own[s.name] else 0)
            score = elsewhere + 0.1 * math.log1p(own[s.name])
            if s.name in COMMON or s.name.startswith("_") or len(s.name) < 3:
                score *= 0.2
            elif re.fullmatch(r"[a-z]+", s.name):
                score *= 0.35  # a plain word (load, status) is mentioned for other reasons too
            score /= defined[s.name]  # a name defined in many places says little about any of them
            s.score = score * (0.4 if s.constant else 1.0 if s.top else 0.6)
        m.score = sum(sorted((s.score for s in m.symbols), reverse=True)[:8]) + 0.01 * len(m.symbols)
        if is_test(m.path):
            m.score *= 0.2  # tests matter less for finding one's way around
    maps.sort(key=lambda m: (-m.score, m.path))
    return maps


def is_test(path: str) -> bool:
    parts = path.lower().split("/")
    name = parts[-1]
    return (
        bool({"test", "tests", "__tests__", "spec"} & set(parts[:-1]))
        or name.startswith("test_")
        or bool(re.search(r"(_test\.(py|go)|\.(test|spec)\.[jt]sx?)$", name))
    )


def worth_showing(symbols: list[Symbol], most: int) -> list[Symbol]:
    """A file's most important symbols, in line order: unreferenced constants and methods are left out."""
    kept = [s for s in symbols if s.score > 0 or (s.top and not s.constant)]
    best = sorted(kept, key=lambda s: -s.score)[:most]
    return sorted(best, key=lambda s: s.line)


def render(maps: list[FileMap], tokens: int, prefix: str = "") -> tuple[str, bool]:
    """The map within a token budget (~3 characters a token). Returns (text, whether it's complete)."""
    budget = tokens * 3
    out: list[str] = []
    used, complete = 0, True
    for m in maps:
        if prefix and not m.path.startswith(prefix.rstrip("/") + "/") and m.path != prefix:
            continue
        if not m.symbols:
            continue
        shown = worth_showing(m.symbols, 25)
        if not shown:
            continue
        if len(shown) < len(m.symbols):
            complete = False
        block = "\n".join([f"{m.path}:"] + [f"  {s.line}: {'  ' if not s.top else ''}{s.text}" for s in shown])
        if used + len(block) > budget:
            # Try the file with only its few most important symbols, then give up on the rest.
            short = worth_showing(m.symbols, 4)
            block = "\n".join([f"{m.path}:"] + [f"  {s.line}: {'  ' if not s.top else ''}{s.text}" for s in short])
            complete = False
            if used + len(block) > budget:
                break
        out.append(block)
        used += len(block) + 1
    return "\n".join(out), complete


class RepoMap:
    """A repository's map, built once per session (and again on request)."""

    def __init__(self, root: Path):
        self.root = root
        self._maps: list[FileMap] | None = None

    def maps(self) -> list[FileMap]:
        if self._maps is None:
            self._maps = build(self.root)
        return self._maps

    def for_prompt(self) -> str:
        """The whole map, if the repository is small enough for it to go into the system prompt."""
        files = source_files(self.root)
        if not files or len(files) > 300:
            return ""
        self._maps = build(self.root, files)
        text, complete = render(self._maps, PROMPT_TOKENS)
        return text if complete and text else ""

    def for_tool(self, folder: str = "", tokens: int = TOOL_TOKENS) -> str:
        self._maps = None  # files may have changed since
        text, complete = render(self.maps(), tokens, folder.strip().strip("/").removeprefix("./"))
        if not text:
            return f"No source files with symbols{' under ' + folder if folder else ''}."
        note = "" if complete else "\n(cut to fit: give a folder to see more of it)"
        return text + note
