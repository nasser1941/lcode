"""`lcode acp`: lcode as the agent inside an editor, over the Agent Client Protocol (ACP, version 1).

The editor starts `lcode acp` and talks JSON-RPC 2.0 over its stdin and stdout, one message per
line. The editor shows what happens:
- the model's answers and thinking stream as message chunks;
- each tool call is a tool call with its kind, files and, for edits, a diff;
- permission requests and plan approvals use the editor's own dialog;
- the todo list becomes the editor's plan;
- lcode's permission modes are the session's modes.

Files are read from and written through the editor's buffers when it supports that, so the model
sees unsaved changes and the editor tracks the edits. Everything else (tools, checkpoints, MCP
servers, memory) works as in the terminal.

Nothing but protocol messages may reach stdout: lcode's own output is discarded (or written to
the file in LCODE_ACP_LOG), and stdin is the protocol channel only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from rich.console import Console

from lcode import __version__

if TYPE_CHECKING:
    from lcode.agent import Agent

PROTOCOL_VERSION = 1
MODES = [
    ("ask", "Ask", "Ask before changing files or running commands"),
    ("auto-edit", "Auto-edit", "Change files without asking; ask before commands"),
    ("plan", "Plan", "Look around and plan; change nothing until you approve the plan"),
    ("yolo", "Yolo", "Never ask"),
]
KINDS = {
    "read_file": "read", "view_image": "read", "list_dir": "search", "glob": "search", "grep": "search",
    "repo_map": "search", "search_code": "search", "lsp": "search", "edit_file": "edit", "write_file": "edit",
    "bash": "execute", "web_search": "fetch", "web_fetch": "fetch", "agent": "think", "todo_write": "think",
    "present_plan": "switch_mode", "bash_output": "read", "bash_stop": "execute",
}  # fmt: skip
STOP = {
    "success": "end_turn",
    "max_steps": "max_turn_requests",
    "loop": "max_turn_requests",
    "interrupted": "cancelled",
}
MAX_INLINE = 200_000  # bytes of a file the editor attaches that go into the prompt
METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR, NOT_FOUND = -32601, -32602, -32603, -32002


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------------------- JSON-RPC over stdio


class Connection:
    """Newline-delimited JSON-RPC 2.0, both ways: the editor's requests and lcode's own."""

    def __init__(self, inp: IO[bytes], out: IO[bytes]):
        self.inp, self.out = inp, out
        self.lock = threading.Lock()
        self.pending: dict[int, tuple[threading.Event, dict]] = {}
        self.next_id = 0
        self.closed = False

    def send(self, message: dict) -> None:
        data = (json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False) + "\n").encode()
        with self.lock:
            self.out.write(data)
            self.out.flush()

    def notify(self, method: str, params: dict) -> None:
        self.send({"method": method, "params": params})

    def request(self, method: str, params: dict) -> dict:
        """Ask the editor and wait for its answer."""
        with self.lock:
            self.next_id += 1
            request_id = self.next_id
            done, slot = threading.Event(), {}
            self.pending[request_id] = (done, slot)
        self.send({"id": request_id, "method": method, "params": params})
        done.wait()
        if "error" in slot:
            error = slot["error"] or {}
            raise RpcError(int(error.get("code", INTERNAL_ERROR)), str(error.get("message", "the editor failed")))
        return slot.get("result") or {}

    def serve(self, handle) -> None:
        """Read messages until the editor closes stdin; requests are handled in threads of their own."""
        for raw in self.inp:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.send({"id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            if "method" in message:
                threading.Thread(target=self._dispatch, args=(handle, message), daemon=True).start()
            elif message.get("id") in self.pending:
                done, slot = self.pending.pop(message["id"])
                slot.update({k: message[k] for k in ("result", "error") if k in message})
                done.set()
        self.closed = True
        for done, slot in list(self.pending.values()):  # nobody will answer now
            slot["error"] = {"code": INTERNAL_ERROR, "message": "the editor closed the connection"}
            done.set()

    def _dispatch(self, handle, message: dict) -> None:
        request_id = message.get("id")
        try:
            result = handle(message["method"], message.get("params") or {})
            if request_id is not None:
                self.send({"id": request_id, "result": result if result is not None else {}})
        except RpcError as e:
            if request_id is not None:
                self.send({"id": request_id, "error": {"code": e.code, "message": str(e)}})
        except Exception as e:  # never leave the editor waiting
            if request_id is not None:
                self.send({"id": request_id, "error": {"code": INTERNAL_ERROR, "message": f"{type(e).__name__}: {e}"}})


# ----------------------------------------------------------------------------- one editor session


def file_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme in ("", "file"):
        return Path(unquote(parsed.path)) if parsed.path else None
    return None


def call_title(agent: Agent, name: str, args: dict) -> str:
    path = args.get("path")
    rel = agent.tools.rel(agent.tools.resolve(path)) if isinstance(path, str) and path else ""
    titles = {
        "read_file": f"Read {rel}",
        "list_dir": f"List {rel or '.'}",
        "glob": f"Find {args.get('pattern', '')}",
        "grep": f"Search for {args.get('pattern', '')}",
        "edit_file": f"Edit {rel}",
        "write_file": f"Write {rel}",
        "bash": str(args.get("command", "")).strip()[:200],
        "web_search": f"Search the web for {args.get('query', '')}",
        "web_fetch": f"Fetch {args.get('url', '')}",
        "todo_write": "Update the plan",
        "present_plan": f"Plan: {args.get('title', '')}",
        "agent": f"Subagent: {args.get('description') or args.get('type', '')}",
    }
    return titles.get(name) or agent.describe_call(name, args)


class Session:
    """An lcode agent wired to the editor: its output becomes session/update notifications."""

    def __init__(self, server: Server, agent: Agent):
        self.server, self.agent, self.id = server, agent, agent.session_id
        self.conn = server.conn
        self.before: dict[str, str | None] = {}  # file contents before an edit, by tool call id
        self.mode = agent.perms.mode
        agent.cancel = threading.Event()
        agent.on_delta = self.delta
        agent.on_event = self.event
        agent.on_tool = self.started
        agent.perms.approve = self.permission
        agent.plan_review = self.review_plan
        caps = server.client_caps.get("fs") or {}
        if caps.get("readTextFile"):
            agent.tools.reader = self.read_buffer
        if caps.get("writeTextFile"):
            agent.tools.writer = self.write_buffer

    def update(self, update: dict) -> None:
        self.conn.notify("session/update", {"sessionId": self.id, "update": update})

    # -- the model's output
    def delta(self, kind: str, text: str) -> None:
        chunk = "agent_message_chunk" if kind == "text" else "agent_thought_chunk"
        self.update({"sessionUpdate": chunk, "content": {"type": "text", "text": text}})

    def call(self, call_id: str, name: str, args: dict, status: str) -> dict:
        update: dict[str, Any] = {
            "toolCallId": call_id,
            "title": call_title(self.agent, name, args),
            "kind": KINDS.get(name, "other"),
            "status": status,
            "rawInput": args,
        }
        path = args.get("path")
        if isinstance(path, str) and path:
            location: dict[str, Any] = {"path": str(self.agent.tools.resolve(path))}
            if isinstance(args.get("offset"), int):
                location["line"] = args["offset"]
            update["locations"] = [location]
        diff = self.proposed_diff(call_id, name, args)
        if diff:
            update["content"] = [diff]
        return update

    def proposed_diff(self, call_id: str, name: str, args: dict) -> dict | None:
        """What an edit will change, as the editor's diff: the whole file before and after."""
        if name not in ("edit_file", "write_file") or not isinstance(args.get("path"), str):
            return None
        path = self.agent.tools.resolve(args["path"])
        try:
            old = self.agent.tools.read_text(path) if path.is_file() else None
        except OSError:
            old = None
        if name == "write_file":
            new = str(args.get("content", ""))
        else:
            find, replace = str(args.get("old_string", "")), str(args.get("new_string", ""))
            if old is None or not find or find not in old:
                return {"type": "diff", "path": str(path), "oldText": find, "newText": replace}
            new = old.replace(find, replace) if args.get("replace_all") else old.replace(find, replace, 1)
        self.before[call_id] = old
        return {"type": "diff", "path": str(path), "oldText": old, "newText": new}

    def event(self, event: dict) -> None:
        if event["type"] == "assistant":
            for call in event["tool_calls"]:
                self.update(
                    {"sessionUpdate": "tool_call", **self.call(call["id"], call["name"], call["arguments"], "pending")}
                )
        elif event["type"] == "tool_result":
            update: dict[str, Any] = {
                "sessionUpdate": "tool_call_update",
                "toolCallId": event["id"],
                "status": "failed" if event["error"] else "completed",
                "rawOutput": {"output": event["output"]},
            }
            content = [{"type": "content", "content": {"type": "text", "text": event["output"]}}]
            if event["name"] in ("edit_file", "write_file") and not event["error"]:
                path = next((c for c in self.pending_paths(event["id"])), None)
                if path is not None:
                    try:
                        after = path.read_text(errors="replace")
                        content = [{"type": "diff", "path": str(path), "oldText": self.before.pop(event["id"], None),
                                    "newText": after}]  # fmt: skip
                    except OSError:
                        pass
            update["content"] = content
            self.update(update)
            if event["name"] == "todo_write":
                self.send_plan()
            if self.agent.perms.mode != self.mode:  # an approved plan switched the mode
                self.mode = self.agent.perms.mode
                self.update({"sessionUpdate": "current_mode_update", "currentModeId": self.mode})

    def pending_paths(self, call_id: str):
        for message in reversed(self.agent.messages):
            for call in message.get("tool_calls") or []:
                if call.get("id") == call_id:
                    args = call.get("function", {}).get("arguments") or {}
                    if isinstance(args, dict) and isinstance(args.get("path"), str):
                        yield self.agent.tools.resolve(args["path"])
                    return

    def started(self, name: str, args: dict) -> None:
        self.update(
            {"sessionUpdate": "tool_call_update", "toolCallId": self.agent.current_call, "status": "in_progress"}
        )

    def send_plan(self) -> None:
        entries = [
            {"content": str(t.get("content", "")), "priority": "medium",
             "status": t.get("status") if t.get("status") in ("pending", "in_progress", "completed") else "pending"}
            for t in self.agent.tools.todos
        ]  # fmt: skip
        self.update({"sessionUpdate": "plan", "entries": entries})

    # -- asking the user, in the editor
    def ask(self, tool_call: dict, options: list[dict]) -> str | None:
        """The option the user picked, or None if the request was cancelled."""
        try:
            reply = self.conn.request(
                "session/request_permission", {"sessionId": self.id, "toolCall": tool_call, "options": options}
            )
        except RpcError:
            return None
        outcome = reply.get("outcome") or {}
        return outcome.get("optionId") if outcome.get("outcome") == "selected" else None

    def permission(self, request: dict) -> bool | str:
        call_id = self.agent.current_call
        name = next(
            (c.get("function", {}).get("name", "") for m in reversed(self.agent.messages)
             for c in m.get("tool_calls") or [] if c.get("id") == call_id), "",
        )  # fmt: skip
        args = next(
            (c.get("function", {}).get("arguments") or {} for m in reversed(self.agent.messages)
             for c in m.get("tool_calls") or [] if c.get("id") == call_id), {},
        )  # fmt: skip
        tool_call = self.call(call_id, name, args if isinstance(args, dict) else {}, "pending") if name else {
            "toolCallId": call_id or "permission", "title": request["title"], "kind": "other", "status": "pending"
        }  # fmt: skip
        tool_call["title"] = request["title"]
        scope = "file edits" if request["kind"] == "edit" else request["key"].split(":", 1)[-1]
        choice = self.ask(
            tool_call,
            [
                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                {"optionId": "always", "name": f"Always allow {scope} in this session", "kind": "allow_always"},
                {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
            ],
        )
        if choice == "always":
            self.agent.perms.always.add("edit" if request["kind"] == "edit" else request["key"])
        if choice in ("allow", "always"):
            return True
        if choice is None and self.agent.cancel is not None and self.agent.cancel.is_set():
            return "The user denied this: they cancelled the request."
        return "The user denied this in the editor. Ask how to proceed, or choose a different approach."

    def review_plan(self, title: str, plan: str) -> str:
        tool_call = {
            "toolCallId": self.agent.current_call or "plan",
            "title": f"Plan: {title}",
            "kind": "switch_mode",
            "status": "pending",
            "content": [{"type": "content", "content": {"type": "text", "text": plan}}],
        }
        choice = self.ask(
            tool_call,
            [
                {"optionId": "ask", "name": "Approve, and ask before changes", "kind": "allow_once"},
                {"optionId": "auto-edit", "name": "Approve, and change files without asking", "kind": "allow_always"},
                {"optionId": "keep", "name": "Keep planning", "kind": "reject_once"},
            ],
        )
        if choice in ("ask", "auto-edit"):
            return choice
        return "The user wants to keep planning. Ask what they'd like to change in the plan; don't change anything yet."

    # -- the editor's buffers
    def read_buffer(self, path: Path) -> str | None:
        try:
            return self.conn.request("fs/read_text_file", {"sessionId": self.id, "path": str(path)}).get("content")
        except RpcError:
            return None  # not open in the editor, or not readable there: lcode reads the file itself

    def write_buffer(self, path: Path, content: str) -> None:
        try:
            self.conn.request("fs/write_text_file", {"sessionId": self.id, "path": str(path), "content": content})
        except RpcError:
            pass  # lcode writes the file itself

    # -- requests
    def prompt(self, blocks: list[dict]) -> dict:
        from lcode import api

        text = self.render(blocks)
        if not text.strip():
            return {"stopReason": "end_turn"}
        command = self.command(text)
        if command == "":
            return {"stopReason": "end_turn"}
        if command is not None:
            text = command
        self.agent.cancel.clear()
        result = api.run_request(self.agent, text)
        self.update({"sessionUpdate": "usage_update", "used": self.agent.ctx_used, "size": self.agent.settings.context})
        self.update({"sessionUpdate": "session_info_update", "title": self.agent.session_title or None})
        if result.status == "error":
            raise RpcError(INTERNAL_ERROR, result.error)
        return {"stopReason": STOP.get(result.status, "end_turn")}

    def command(self, text: str) -> str | None:
        """A /command from the editor: the user's own commands, and /review."""
        if not text.startswith("/"):
            return None
        name, _, arg = text[1:].partition(" ")
        commands = self.agent.extensions().commands
        if name in commands:
            return commands[name].render(arg.strip())
        if name == "review":
            from lcode import gitflow

            try:
                prompt = gitflow.review_prompt(self.agent, arg.strip())
            except gitflow.GitError as e:
                self.delta("text", f"Nothing to review: {e}")
                return ""
            self.agent.no_changes = "This is a review, so nothing can be changed: report what should change instead."
            return prompt
        return None

    def render(self, blocks: list[dict]) -> str:
        """The editor's prompt (text, attached files, images) as one request for lcode."""
        parts = []
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                parts.append(str(block.get("text", "")))
            elif kind == "resource_link":
                parts.append(self.attached(block.get("uri", ""), None))
            elif kind == "resource":
                resource = block.get("resource") or {}
                parts.append(self.attached(resource.get("uri", ""), resource.get("text")))
            elif kind == "image" and block.get("data"):
                parts.append(self.image(block["data"], block.get("mimeType", "image/png")))
        return "\n\n".join(p for p in parts if p)

    def attached(self, uri: str, text: str | None) -> str:
        path = file_path(uri)
        where = self.agent.tools.rel(path) if path else uri
        if text is None and path is not None and path.is_file() and path.stat().st_size <= MAX_INLINE:
            text = self.agent.tools.read_text(path)
        if text is None:
            return f"[The user points at {where}]"
        return f"[The user attached {where}]\n```\n{text[:MAX_INLINE].rstrip(chr(10))}\n```"

    def image(self, data: str, mime: str) -> str:
        from lcode import config

        raw = base64.b64decode(data)
        folder = config.STATE_DIR / "acp-images"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{hashlib.sha256(raw).hexdigest()[:16]}.{mime.split('/')[-1].split('+')[0] or 'png'}"
        path.write_bytes(raw)
        return f"@{path}"

    def replay(self) -> None:
        """A loaded session's conversation, sent to the editor as it happened."""
        results: dict[str, str] = {}
        names: dict[str, str] = {}
        for message in self.agent.messages[1:]:
            role, content = message.get("role"), message.get("content") or ""
            if role == "user" and content:
                self.update({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": content}})
            elif role == "assistant":
                if content:
                    self.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": content}})
                for i, call in enumerate(message.get("tool_calls") or []):
                    call.setdefault("id", f"call_loaded_{len(names)}_{i}")
                    fn = call.get("function", {})
                    args = fn.get("arguments") if isinstance(fn.get("arguments"), dict) else {}
                    names[call["id"]] = fn.get("name", "")
                    update = {"sessionUpdate": "tool_call", "toolCallId": call["id"],
                              "title": call_title(self.agent, fn.get("name", ""), args),
                              "kind": KINDS.get(fn.get("name", ""), "other"), "status": "completed", "rawInput": args}  # fmt: skip
                    self.update(update)
            elif role == "tool":
                call_id = message.get("tool_call_id") or next((i for i in reversed(names) if i not in results), "")
                if call_id:
                    results[call_id] = content
                    self.update({"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": "completed",
                                 "content": [{"type": "content", "content": {"type": "text", "text": content[:4000]}}]})  # fmt: skip


# ----------------------------------------------------------------------------- the agent side


class Server:
    def __init__(self, conn: Connection, console: Console):
        self.conn, self.console = conn, console
        self.client_caps: dict = {}
        self.sessions: dict[str, Session] = {}

    def handle(self, method: str, params: dict) -> dict | None:
        handlers = {
            "initialize": self.initialize,
            "authenticate": lambda p: {},
            "session/new": self.new_session,
            "session/load": self.load_session,
            "session/prompt": self.prompt,
            "session/cancel": self.cancel,
            "session/set_mode": self.set_mode,
        }
        if method not in handlers:
            raise RpcError(METHOD_NOT_FOUND, f"lcode doesn't support {method}")
        return handlers[method](params)

    def initialize(self, params: dict) -> dict:
        self.client_caps = params.get("clientCapabilities") or {}
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
                "mcpCapabilities": {"http": True, "sse": False},
            },
            "agentInfo": {"name": "lcode", "title": "lcode", "version": __version__},
            "authMethods": [],
        }

    def open(self, params: dict) -> Session:
        from lcode import api

        cwd = Path(str(params.get("cwd") or ""))
        if not cwd.is_absolute():
            raise RpcError(INVALID_PARAMS, "cwd must be an absolute path")
        try:
            agent = api.open_agent(cwd, api.Options(interactive=False), self.console)
        except api.SetupError as e:
            raise RpcError(INTERNAL_ERROR, str(e)) from e
        agent.mcp = self.mcp(cwd, params.get("mcpServers") or [], agent)
        session = Session(self, agent)
        self.sessions[session.id] = session
        return session

    def mcp(self, cwd: Path, editor_servers: list[dict], agent: Agent):
        """lcode's MCP servers, plus the ones the editor passes."""
        from lcode import config
        from lcode.mcp import config as mcp_config
        from lcode.mcp.manager import McpManager

        project = mcp_config.project_file(cwd)
        servers, problems = mcp_config.load_all(cwd, include_project=bool(project and mcp_config.is_approved(project)))
        names = {s.name for s in servers}
        for entry in editor_servers:
            name = str(entry.get("name") or "editor")
            if name in names:
                continue
            if entry.get("url"):
                raw = {
                    "type": "http",
                    "url": entry["url"],
                    "headers": {h["name"]: h["value"] for h in entry.get("headers") or []},
                }
            else:
                raw = {"command": entry.get("command", ""), "args": entry.get("args") or [],
                       "env": {e["name"]: e["value"] for e in entry.get("env") or []}}  # fmt: skip
            server = mcp_config.parse(name, raw, source="editor")
            if server.error:
                problems.append(f"{name}: {server.error}")
            else:
                servers.append(server)
        for problem in problems:
            self.console.print(f"MCP: {problem}")
        if not servers:
            return None
        manager = McpManager(cwd, servers, config.load()["mcp_tools"])
        manager.start()
        return manager

    def modes(self, session: Session) -> dict:
        return {
            "currentModeId": session.agent.perms.mode,
            "availableModes": [{"id": i, "name": n, "description": d} for i, n, d in MODES],
        }

    def new_session(self, params: dict) -> dict:
        session = self.open(params)
        self.commands(session)
        return {"sessionId": session.id, "modes": self.modes(session)}

    def load_session(self, params: dict) -> dict:
        from lcode import sessions

        session = self.open(params)
        wanted = str(params.get("sessionId", ""))
        found = next((s for s in sessions.list_sessions(session.agent.cwd, limit=1000) if s.id == wanted), None)
        if found is None:
            self.sessions.pop(session.id, None)
            raise RpcError(NOT_FOUND, f"no saved lcode session {wanted} in {session.agent.cwd}")
        self.sessions.pop(session.id, None)
        session.agent.load(found)
        session.id = session.agent.session_id
        self.sessions[session.id] = session
        session.replay()
        self.commands(session)
        return {"modes": self.modes(session)}

    def commands(self, session: Session) -> None:
        available = [{"name": "review", "description": "Review the uncommitted changes, or the branch against a base",
                      "input": {"hint": "base branch (optional)"}}]  # fmt: skip
        for name, command in session.agent.extensions().commands.items():
            if name != "review":
                entry: dict = {"name": name, "description": command.description}
                if command.argument_hint:
                    entry["input"] = {"hint": command.argument_hint}
                available.append(entry)
        session.update({"sessionUpdate": "available_commands_update", "availableCommands": available})

    def session(self, params: dict) -> Session:
        session = self.sessions.get(str(params.get("sessionId", "")))
        if session is None:
            raise RpcError(NOT_FOUND, f"no session {params.get('sessionId')}")
        return session

    def prompt(self, params: dict) -> dict:
        session = self.session(params)
        try:
            return session.prompt(params.get("prompt") or [])
        finally:
            session.agent.no_changes = ""

    def cancel(self, params: dict) -> None:
        session = self.sessions.get(str(params.get("sessionId", "")))
        if session is not None and session.agent.cancel is not None:
            session.agent.cancel.set()
            from lcode import subagents

            if session.agent.response is not None:
                subagents.abort(session.agent.response)  # stop the model's answer now
        return None

    def set_mode(self, params: dict) -> dict:
        session = self.session(params)
        mode = str(params.get("modeId", ""))
        if mode not in {m[0] for m in MODES}:
            raise RpcError(INVALID_PARAMS, f"unknown mode {mode}")
        session.agent.perms.mode = session.mode = mode
        return {}


def main() -> int:
    """Serve one editor over stdin and stdout until it disconnects."""
    inp, out = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr  # a stray print must not corrupt the protocol
    # Open for the life of the process:
    sys.stdin = open(os.devnull)  # noqa: SIM115  # nothing may wait for typed input: stdin is the protocol
    log = os.environ.get("LCODE_ACP_LOG")
    output = open(log, "a") if log else open(os.devnull, "w")  # noqa: SIM115  # lcode's own output
    console = Console(file=output, width=120, force_terminal=False)
    conn = Connection(inp, out)
    server = Server(conn, console)
    conn.serve(server.handle)
    for session in server.sessions.values():
        from lcode import api

        api.close_agent(session.agent)
    return 0
