"""Code intelligence from language servers: definitions, references, types, symbols, and errors after edits.

grep finds text; a language server knows the code. With one installed (basedpyright or pyright for
Python, typescript-language-server for TypeScript and JavaScript, gopls, rust-analyzer, clangd),
the model gets an `lsp` tool for precise navigation, and every edit to a file in that language is
followed by the errors the edit introduced, so the model fixes them right away instead of finding
out when it runs the build. Servers start on demand, one per language, and live for the session.

Without any server installed nothing changes: there's no tool and nothing in the prompt.
`~/.config/lcode/lsp.json` can name another command for a language or turn one off:

    {"python": {"command": ["pylsp"]}, "rust": {"disabled": true}}
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from lcode import config

LANGUAGES = {
    ".py": ("python", "python"), ".pyi": ("python", "python"),
    ".ts": ("typescript", "typescript"), ".tsx": ("typescript", "typescriptreact"), ".mts": ("typescript", "typescript"),
    ".js": ("typescript", "javascript"), ".jsx": ("typescript", "javascriptreact"), ".mjs": ("typescript", "javascript"),
    ".cjs": ("typescript", "javascript"),
    ".go": ("go", "go"),
    ".rs": ("rust", "rust"),
    ".c": ("c", "c"), ".h": ("c", "c"), ".cc": ("c", "cpp"), ".cpp": ("c", "cpp"), ".hpp": ("c", "cpp"), ".cxx": ("c", "cpp"),
}  # fmt: skip  # extension -> (server, LSP language id)
SERVERS = {
    "python": [["basedpyright-langserver", "--stdio"], ["pyright-langserver", "--stdio"], ["pylsp"]],
    # typescript-language-server needs TypeScript 5 (tsserver.js); TypeScript 7 has its own server in tsc.
    "typescript": [["typescript-language-server", "--stdio"], ["tsc", "--lsp", "--stdio"]],
    "go": [["gopls"]],
    "rust": [["rust-analyzer"]],
    "c": [["clangd"]],
}
EXTENSIONS = {language: [e for e, (lang, _) in LANGUAGES.items() if lang == language] for language in SERVERS}
NAMES = {"python": "Python", "typescript": "TypeScript/JavaScript", "go": "Go", "rust": "Rust", "c": "C/C++"}
INSTALL = {
    "python": "uv tool install basedpyright (or npm install -g pyright)",
    "typescript": "npm install -g typescript-language-server typescript",
    "go": "go install golang.org/x/tools/gopls@latest",
    "rust": "rustup component add rust-analyzer",
    "c": "install clangd (apt install clangd, or brew install llvm)",
}
REQUEST_TIMEOUT = 30.0
FIRST_DIAGNOSTICS = 15.0  # seconds to wait for a file's first diagnostics (the server may still be indexing)
DIAGNOSTICS = 5.0
MAX_RESULTS = 100
SYMBOL_KINDS = {
    1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class", 6: "method", 7: "property", 8: "field",
    9: "constructor", 10: "enum", 11: "interface", 12: "function", 13: "variable", 14: "constant", 15: "string",
    16: "number", 17: "boolean", 18: "array", 19: "object", 20: "key", 21: "null", 22: "enum member", 23: "struct",
    24: "event", 25: "operator", 26: "type parameter",
}  # fmt: skip


class LspError(Exception):
    pass


def uri(path: Path) -> str:
    return path.resolve().as_uri()


def path_of(value: str) -> Path:
    return Path(unquote(urlparse(value).path))


def overrides() -> dict:
    try:
        data = json.loads((config.CONFIG_DIR / "lsp.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def candidates(language: str, root: Path | None = None) -> list[list[str]]:
    """The installed commands for a language's server, best first: lsp.json's, or the known ones, with a
    project's own (node_modules/.bin) before the ones on the PATH."""
    custom = overrides().get(language) or {}
    if custom.get("disabled"):
        return []
    known = [[str(part) for part in custom["command"]]] if custom.get("command") else SERVERS[language]
    found = []
    for command in known:
        local = root / "node_modules" / ".bin" / command[0] if root else None
        if local is not None and local.is_file():
            found.append([str(local), *command[1:]])
        elif shutil.which(command[0]):
            found.append(command)
    return found


def available(root: Path | None = None) -> dict[str, list[list[str]]]:
    """The languages with a server installed, and the commands to try."""
    return {language: found for language in SERVERS if (found := candidates(language, root))}


# ----------------------------------------------------------------------------- one server


@dataclass
class Document:
    version: int
    text: str


class Server:
    """A language server process, spoken to over stdin/stdout with JSON-RPC (Content-Length framing)."""

    def __init__(self, language: str, command: list[str], root: Path, log: Path | None = None):
        self.language, self.command, self.root = language, command, root
        log_file = open(log, "ab") if log else subprocess.DEVNULL  # noqa: SIM115 - lives as long as the process
        try:
            self.process = subprocess.Popen(
                command, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log_file
            )
        except OSError as e:
            raise LspError(f"can't start {command[0]}: {e}") from e
        self.write_lock = threading.Lock()
        self.next_id = 0
        self.pending: dict[int, dict] = {}
        self.changed = threading.Condition()
        self.diagnostics: dict[str, list[dict]] = {}
        self.published: dict[str, int] = {}  # times diagnostics were published, per document
        self.published_version: dict[str, int | None] = {}  # the document version they were for, if the server says
        self.documents: dict[str, Document] = {}
        self.capabilities: dict = {}
        threading.Thread(target=self._read, daemon=True).start()
        result = self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": uri(root),
                "workspaceFolders": [{"uri": uri(root), "name": root.name}],
                "capabilities": {
                    "textDocument": {
                        "synchronization": {"didSave": True},
                        "definition": {"linkSupport": True},
                        "references": {},
                        "hover": {"contentFormat": ["plaintext", "markdown"]},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "publishDiagnostics": {"versionSupport": True},
                    },
                    "workspace": {"symbol": {}, "configuration": True, "workspaceFolders": True},
                },
                "clientInfo": {"name": "lcode"},
            },
            timeout=60,
        )
        self.capabilities = result.get("capabilities") or {}
        self.notify("initialized", {})

    # -- transport
    def _send(self, message: dict) -> None:
        body = json.dumps(message).encode()
        with self.write_lock:
            if self.process.stdin is None or self.process.poll() is not None:
                raise LspError(f"{self.command[0]} has stopped")
            try:
                self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
                self.process.stdin.flush()
            except OSError as e:
                raise LspError(f"{self.command[0]} has stopped: {e}") from e

    def _read(self) -> None:
        stream = self.process.stdout
        assert stream is not None
        while True:
            length = None
            while True:
                line = stream.readline()
                if not line:
                    with self.changed:
                        self.changed.notify_all()
                    return
                line = line.strip()
                if not line:
                    break
                name, _, value = line.decode(errors="replace").partition(":")
                if name.lower() == "content-length":
                    length = int(value.strip())
            if length is None:
                continue
            try:
                message = json.loads(stream.read(length))
            except ValueError:
                continue
            self._dispatch(message)

    def _dispatch(self, message: dict) -> None:
        method = message.get("method")
        if method and "id" in message:  # a request from the server
            answer = (
                [None] * len((message.get("params") or {}).get("items") or [])
                if method == "workspace/configuration"
                else None
            )
            try:
                self._send({"jsonrpc": "2.0", "id": message["id"], "result": answer})
            except LspError:
                pass
            return
        with self.changed:
            if method == "textDocument/publishDiagnostics":
                params = message.get("params") or {}
                key = params.get("uri", "")
                self.diagnostics[key] = params.get("diagnostics") or []
                self.published[key] = self.published.get(key, 0) + 1
                self.published_version[key] = params.get("version")
            elif "id" in message:
                self.pending[message["id"]] = message
            self.changed.notify_all()

    def request(self, method: str, params: dict, timeout: float = REQUEST_TIMEOUT):
        with self.changed:
            self.next_id += 1
            ident = self.next_id
        self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        with self.changed:
            while ident not in self.pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.process.poll() is not None:
                    raise LspError(
                        f"{self.command[0]} didn't answer {method}"
                        + (" (it stopped)" if self.process.poll() is not None else "")
                    )
                self.changed.wait(remaining)
            reply = self.pending.pop(ident)
        if "error" in reply:
            raise LspError(f"{self.command[0]}: {(reply['error'] or {}).get('message', 'error')}")
        return reply.get("result")

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # -- documents
    def sync(self, path: Path, text: str | None = None) -> tuple[str, bool]:
        """Make the server see the file as it is now (or as `text`). Returns (uri, whether it changed)."""
        key = uri(path)
        if text is None:
            text = path.read_text(errors="replace") if path.is_file() else ""
        document = self.documents.get(key)
        if document is None:
            language_id = LANGUAGES.get(path.suffix.lower(), (self.language, self.language))[1]
            self.documents[key] = Document(1, text)
            self.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": key, "languageId": language_id, "version": 1, "text": text}},
            )
            return key, True
        if document.text == text:
            return key, False
        document.version += 1
        document.text = text
        self.notify(
            "textDocument/didChange",
            {"textDocument": {"uri": key, "version": document.version}, "contentChanges": [{"text": text}]},
        )
        return key, True

    def wait_diagnostics(self, key: str, seen: int, timeout: float) -> list[dict] | None:
        """The diagnostics for the document as it is now: published after `seen` publications and, if the
        server says which version they're for, for the current version. None if they don't come in time."""
        deadline = time.monotonic() + timeout
        document = self.documents.get(key)

        def ready() -> bool:
            if self.published.get(key, 0) <= seen:
                return False
            version = self.published_version.get(key)
            return version is None or document is None or version >= document.version

        with self.changed:
            while not ready():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.process.poll() is not None:
                    return None
                self.changed.wait(remaining)
            return list(self.diagnostics.get(key, []))

    def pull_diagnostics(self, key: str) -> list[dict]:
        result = self.request("textDocument/diagnostic", {"textDocument": {"uri": key}}) or {}
        if result.get("kind") == "unchanged":
            return list(self.diagnostics.get(key, []))
        self.diagnostics[key] = result.get("items") or []
        return list(self.diagnostics[key])

    def close(self) -> None:
        try:
            self.request("shutdown", {}, timeout=3)
            self.notify("exit", {})
        except LspError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()


# ----------------------------------------------------------------------------- the session's servers


@dataclass
class Manager:
    """The language servers of a session: started on demand, one per language."""

    root: Path
    commands: dict[str, list[list[str]]] | None = None  # language -> commands to try; None: what's installed
    servers: dict[str, Server] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        if self.commands is None:
            self.commands = available(self.root)

    def languages(self) -> list[str]:
        return [NAMES[language] for language in self.commands if language not in self.failed]

    def server_for(self, path: Path) -> Server | None:
        language = LANGUAGES.get(path.suffix.lower(), (None,))[0]
        if language is None or language not in self.commands or language in self.failed:
            return None
        server = self.servers.get(language)
        if server is not None and server.process.poll() is None:
            return server
        log = config.STATE_DIR / "lsp-logs" / f"{language}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        problems = []
        for command in self.commands[language]:  # e.g. typescript-language-server fails with TypeScript 7
            try:
                server = Server(language, command, self.root, log)
            except LspError as e:
                problems.append(str(e))
                continue
            self.servers[language] = server
            return server
        self.failed[language] = "; ".join(problems)
        return None

    def require(self, path: Path) -> Server:
        server = self.server_for(path)
        if server is None:
            language = LANGUAGES.get(path.suffix.lower(), (None,))[0]
            if language is None:
                raise LspError(f"no language server handles {path.suffix or 'files without an extension'}; use grep")
            if language in self.failed:
                raise LspError(f"the {NAMES[language]} language server failed: {self.failed[language]}")
            raise LspError(f"no {NAMES[language]} language server is installed ({INSTALL[language]}); use grep")
        return server

    def close(self) -> None:
        for server in self.servers.values():
            server.close()
        self.servers.clear()

    # -- after an edit
    def check_edit(self, path: Path, before: str | None, after: str) -> str:
        """The errors an edit introduced, as a note for the tool result ("" if none or no server)."""
        with self.lock:
            server = self.server_for(path)
            if server is None:
                return ""
            try:
                if server.capabilities.get("diagnosticProvider"):  # the client asks (LSP 3.17 pull diagnostics)
                    key, _ = server.sync(path, before or "")
                    baseline = server.pull_diagnostics(key) if before else []
                    server.sync(path, after)
                    return describe_new_errors(path, baseline, server.pull_diagnostics(key))
                key = uri(path)
                first = key not in server.documents
                wait = FIRST_DIAGNOSTICS if first else DIAGNOSTICS
                seen = server.published.get(key, 0)
                _, changed = server.sync(path, before or "")  # the file as it was before the edit
                baseline = (server.wait_diagnostics(key, seen, wait) if changed else None) or []
                if not changed:
                    baseline = list(server.diagnostics.get(key, []))
                if not before:
                    baseline = []  # a new file: everything is new
                seen = server.published.get(key, 0)
                _, changed = server.sync(path, after)
                result = server.wait_diagnostics(key, seen, wait) if changed else baseline
            except LspError:
                return ""
        if result is None:
            return ""
        return describe_new_errors(path, baseline, result)

    # -- the lsp tool
    def query(self, action: str, path: Path | None, line: int, symbol: str, query: str) -> str:
        with self.lock:
            if action == "symbols" and path is None:
                return self.workspace_symbols(query)
            if path is None:
                raise LspError("path is needed")
            server = self.require(path)
            key, _ = server.sync(path)
            if action == "symbols":
                return format_outline(
                    server.request("textDocument/documentSymbol", {"textDocument": {"uri": key}}), self.root
                )
            position = locate(path, line, symbol)
            params = {"textDocument": {"uri": key}, "position": position}
            if action == "definition":
                result = server.request("textDocument/definition", params)
                # Right after it starts, a server may only know the open file and stop at the import
                # (TypeScript 5 does): ask again while it loads the rest of the project.
                for _ in range(8):
                    if import_target(result) is None:
                        break
                    time.sleep(0.5)
                    result = server.request("textDocument/definition", params) or result
                return format_locations(result, self.root, "No definition found.")
            if action == "references":
                params["context"] = {"includeDeclaration": True}
                return format_locations(
                    server.request("textDocument/references", params), self.root, "No references found."
                )
            if action == "hover":
                return format_hover(server.request("textDocument/hover", params))
            raise LspError("action must be definition, references, hover or symbols")

    def workspace_symbols(self, query: str) -> str:
        if not query:
            raise LspError("give a path for a file's outline, or a query to search the workspace")
        lines: list[str] = []
        running = [s for s in self.servers.values() if s.process.poll() is None]
        if not running:  # start the servers for the languages this repository has
            for language in self.commands:
                if any(next(self.root.glob(f"**/*{ext}"), None) for ext in EXTENSIONS[language][:3]):
                    server = self.server_for(self.root / f"x{EXTENSIONS[language][0]}")
                    if server is not None:
                        running.append(server)
        for server in running:
            try:
                found = server.request("workspace/symbol", {"query": query}) or []
            except LspError:
                continue
            for item in found[:MAX_RESULTS]:
                location = item.get("location") or {}
                where = (
                    format_location(location, self.root)
                    if location.get("range")
                    else relative(path_of(location.get("uri", "")), self.root)
                )
                lines.append(f"{item.get('name')} ({SYMBOL_KINDS.get(item.get('kind'), 'symbol')}) {where}")
        return "\n".join(lines) if lines else f"No symbols match {query!r}."


# ----------------------------------------------------------------------------- formatting


def relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def line_text(path: Path, number: int) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return lines[number].strip() if 0 <= number < len(lines) else ""


def locate(path: Path, line: int, symbol: str) -> dict:
    """The LSP position of `symbol` on a 1-based line (its first whole-word occurrence)."""
    lines = path.read_text(errors="replace").splitlines()
    if not 1 <= int(line) <= len(lines):
        raise LspError(f"{path.name} has {len(lines)} lines; line {line} doesn't exist")
    text = lines[int(line) - 1]
    if symbol:
        m = re.search(rf"(?<![\w$]){re.escape(symbol)}(?![\w$])", text) or re.search(re.escape(symbol), text)
        if not m:
            raise LspError(f"{symbol!r} isn't on line {line} of {path.name}: {text.strip()[:120]}")
        column = m.start()
    else:
        column = len(text) - len(text.lstrip())
    return {"line": int(line) - 1, "character": len(text[:column].encode("utf-16-le")) // 2}


def import_target(result) -> tuple[Path, dict] | None:
    """If a definition is just an import statement, where to ask again."""
    items = result if isinstance(result, list) else [result] if result else []
    if len(items) != 1:
        return None
    target = items[0].get("targetUri") or items[0].get("uri", "")
    span = items[0].get("targetSelectionRange") or items[0].get("range") or {}
    start = span.get("start") or {}
    path = path_of(target)
    statement = line_text(path, start.get("line", -1))
    if not re.match(r"(import|from|export\s*\{)\b|(const|let|var)\s.*=\s*require\(", statement):
        return None
    return path, {"line": start.get("line", 0), "character": start.get("character", 0)}


def format_location(location: dict, root: Path) -> str:
    target = location.get("targetUri") or location.get("uri", "")
    span = location.get("targetSelectionRange") or location.get("range") or {}
    number = (span.get("start") or {}).get("line", 0)
    path = path_of(target)
    return f"{relative(path, root)}:{number + 1}: {line_text(path, number)}"


def format_locations(result, root: Path, empty: str) -> str:
    if not result:
        return empty
    items = result if isinstance(result, list) else [result]
    lines = list(dict.fromkeys(format_location(item, root) for item in items[:MAX_RESULTS]))  # one per line
    more = f"\n… and {len(items) - MAX_RESULTS} more" if len(items) > MAX_RESULTS else ""
    return "\n".join(lines) + more


def format_hover(result) -> str:
    if not result:
        return "Nothing known about that position."
    contents = result.get("contents")
    parts = contents if isinstance(contents, list) else [contents]
    texts = []
    for part in parts:
        if isinstance(part, dict):
            texts.append(str(part.get("value", "")))
        elif part:
            texts.append(str(part))
    return "\n\n".join(t.strip() for t in texts if t.strip()) or "Nothing known about that position."


def format_outline(result, root: Path) -> str:
    if not result:
        return "No symbols."
    lines: list[str] = []

    def walk(items: list, depth: int, parent_kind: int = 0) -> None:
        for item in items:
            if parent_kind in (6, 9, 12) and item.get("kind") in (13, 14):  # a function's parameters and locals
                continue
            span = item.get("selectionRange") or item.get("range") or (item.get("location") or {}).get("range") or {}
            number = (span.get("start") or {}).get("line", 0) + 1
            detail = f" {item['detail']}" if item.get("detail") else ""
            lines.append(
                f"{'  ' * depth}{number}: {SYMBOL_KINDS.get(item.get('kind'), 'symbol')} {item.get('name')}{detail}"
            )
            walk(item.get("children") or [], depth + 1, item.get("kind", 0))

    walk(result, 0)
    return "\n".join(lines[: MAX_RESULTS * 3])


def describe_new_errors(path: Path, before: list[dict], after: list[dict], limit: int = 10) -> str:
    """Errors in `after` that weren't in `before` (compared by message, since lines move)."""
    remaining: dict[str, int] = {}
    for d in before:
        if d.get("severity", 1) == 1:
            remaining[d.get("message", "")] = remaining.get(d.get("message", ""), 0) + 1
    new = []
    for d in after:
        if d.get("severity", 1) != 1:
            continue
        message = d.get("message", "")
        if remaining.get(message):
            remaining[message] -= 1
            continue
        new.append(d)
    if not new:
        return ""
    lines = []
    for d in new[:limit]:
        number = ((d.get("range") or {}).get("start") or {}).get("line", 0) + 1
        source = f" ({d['source']})" if d.get("source") else ""
        lines.append(f"  {path.name}:{number}: {' '.join(str(d.get('message', '')).split())}{source}")
    more = f"\n  … and {len(new) - limit} more" if len(new) > limit else ""
    noun = "error" if len(new) == 1 else "errors"
    return f"\n[The language server found {len(new)} new {noun} after this change:\n" + "\n".join(lines) + more + "]"
