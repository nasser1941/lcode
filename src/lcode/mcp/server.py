"""A small MCP server over stdio, for the servers lcode ships itself (lcode.servers).

It speaks both eras of MCP, like lcode's client: the legacy `initialize` handshake and the modern
stateless `server/discover`. Tools are plain functions that return text; raising ToolFailure turns
into an error result the model can read. Tools that change something are only listed when the
server is started with --allow-writes.
"""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO

from lcode.mcp.protocol import LEGACY_VERSIONS, METHOD_NOT_FOUND, MODERN_VERSIONS

INVALID_PARAMS = -32602
MAX_RESULT = 60_000  # characters per tool result; long listings are cut with a note


class ToolFailure(Exception):
    """A tool couldn't do its job; the message goes to the model."""


@dataclass
class Tool:
    name: str
    description: str
    properties: dict
    required: list[str]
    fn: Callable[..., str]
    writes: bool = False

    def definition(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {"type": "object", "properties": self.properties, "required": self.required},
            "annotations": {"readOnlyHint": not self.writes, "destructiveHint": self.writes},
        }


@dataclass
class Server:
    name: str
    version: str
    instructions: str = ""
    allow_writes: bool = False
    tools: dict[str, Tool] = field(default_factory=dict)

    def tool(self, name: str, description: str, properties: dict | None = None, required=(), writes: bool = False):
        """Register a function as a tool (a decorator)."""

        def register(fn: Callable[..., str]) -> Callable[..., str]:
            self.tools[name] = Tool(name, description, properties or {}, list(required), fn, writes)
            return fn

        return register

    def listed(self) -> list[Tool]:
        return [t for t in self.tools.values() if self.allow_writes or not t.writes]

    # -- JSON-RPC
    def handle(self, message: dict) -> dict | None:
        """The response to one message (None for notifications)."""
        method, params, ident = message.get("method"), message.get("params") or {}, message.get("id")
        if ident is None:
            return None  # a notification, such as notifications/initialized
        try:
            result = self.dispatch(str(method), params if isinstance(params, dict) else {})
        except _RpcError as e:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": e.code, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def dispatch(self, method: str, params: dict) -> dict:
        info = {"name": self.name, "version": self.version}
        if method == "initialize":
            asked = str(params.get("protocolVersion") or "")
            return {
                "protocolVersion": asked if asked in LEGACY_VERSIONS else LEGACY_VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": info,
                "instructions": self.instructions,
            }
        if method == "server/discover":
            return {
                "supportedVersions": list(MODERN_VERSIONS),
                "capabilities": {"tools": {}},
                "instructions": self.instructions,
                "_meta": {"io.modelcontextprotocol/serverInfo": info},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [t.definition() for t in self.listed()]}
        if method == "tools/call":
            return self.call(str(params.get("name", "")), params.get("arguments") or {})
        raise _RpcError(METHOD_NOT_FOUND, f"unknown method {method}")

    def call(self, name: str, arguments: dict) -> dict:
        tool = self.tools.get(name)
        if tool is None or tool not in self.listed():
            hint = " (start the server with --allow-writes to enable it)" if tool else ""
            return _text(f"Unknown tool {name}{hint}", error=True)
        if not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS, "arguments must be an object")
        unknown = set(arguments) - set(tool.properties)
        missing = [r for r in tool.required if arguments.get(r) in (None, "")]
        if unknown or missing:
            problems = [f"unknown argument(s): {', '.join(sorted(unknown))}"] if unknown else []
            problems += [f"missing: {', '.join(missing)}"] if missing else []
            return _text(f"Bad arguments for {name}: {'; '.join(problems)}", error=True)
        try:
            text = tool.fn(**arguments)
        except ToolFailure as e:
            return _text(str(e), error=True)
        except Exception as e:  # report, don't crash the server
            traceback.print_exc(file=sys.stderr)
            return _text(f"{type(e).__name__}: {e}", error=True)
        if len(text) > MAX_RESULT:
            text = text[:MAX_RESULT] + "\n… (cut off: ask for less, e.g. with a smaller limit or a filter)"
        return _text(text)

    def serve(self, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
        """Answer newline-delimited JSON-RPC on stdin/stdout until stdin closes."""
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            else:
                response = self.handle(message) if isinstance(message, dict) else None
            if response is not None:
                stdout.write(json.dumps(response) + "\n")
                stdout.flush()


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}
