"""A connection to one MCP server: works out which protocol era it speaks, lists and calls its tools."""

from __future__ import annotations

from lcode.mcp.http import HttpTransport
from lcode.mcp.protocol import (
    CLIENT_INFO,
    LEGACY_VERSIONS,
    MODERN_ERROR_CODES,
    MODERN_VERSIONS,
    UNSUPPORTED_VERSION,
    McpError,
    RpcError,
    TransportError,
    header_value,
    modern_meta,
    param_headers,
)
from lcode.mcp.stdio import StdioTransport

Transport = StdioTransport | HttpTransport
MAX_TOOL_PAGES = 50


class Connection:
    """Speaks legacy MCP (an `initialize` handshake) or modern MCP (per-request metadata).

    lcode tries `initialize` first: almost every server deployed today supports it, and modern-only
    servers reject it with an error, which is the cue to switch. Probing modern-first would make
    some older stdio servers exit, since they don't expect any request before `initialize`.
    """

    def __init__(self, transport: Transport, timeout: float = 60):
        self.transport = transport
        self.timeout = timeout
        self.era = ""  # "legacy" or "modern"
        self.version = ""
        self.server_info: dict = {}
        self.instructions = ""
        self.capabilities: dict = {}
        self.tools_changed = False
        transport.on_notification = self._notified

    @property
    def is_http(self) -> bool:
        return isinstance(self.transport, HttpTransport)

    def open(self, prefer: str = "") -> None:
        """Connect. `prefer` is the era remembered from last time, if any."""
        if prefer == "modern":
            try:
                return self._open_modern()
            except McpError:
                pass
        try:
            self._open_legacy()
        except RpcError as e:
            if e.code in MODERN_ERROR_CODES or e.http_status == 400 or _mentions_modern(e):
                self._open_modern()
            else:
                try:
                    self._open_modern()
                except McpError:
                    raise e from None
        except TransportError as e:
            if not (self.is_http and e.http_status in (400, 404, 405)):
                raise
            self._open_modern()

    def _open_legacy(self) -> None:
        if isinstance(self.transport, HttpTransport):
            self.transport.protocol_version = None
            self.transport.session_id = None
        result = self.transport.request(
            "initialize",
            {"protocolVersion": LEGACY_VERSIONS[0], "capabilities": {}, "clientInfo": CLIENT_INFO},
            self.timeout,
        )
        self.era, self.version = "legacy", str(result.get("protocolVersion") or LEGACY_VERSIONS[-1])
        self.server_info = result.get("serverInfo") or {}
        self.instructions = result.get("instructions") or ""
        self.capabilities = result.get("capabilities") or {}
        if isinstance(self.transport, HttpTransport) and self.version >= "2025-06-18":
            self.transport.protocol_version = self.version
        self.transport.notify("notifications/initialized")

    def _open_modern(self, version: str = MODERN_VERSIONS[0]) -> None:
        if isinstance(self.transport, HttpTransport):
            self.transport.session_id = None
            self.transport.protocol_version = version
        self.era, self.version = "modern", version
        try:
            result = self._request("server/discover", {})
        except RpcError as e:
            supported = (e.data or {}).get("supported") if isinstance(e.data, dict) else None
            usable = [v for v in MODERN_VERSIONS if v in (supported or [])]
            if e.code == UNSUPPORTED_VERSION and usable and usable[0] != version:
                return self._open_modern(usable[0])
            raise McpError(
                f"the server speaks MCP {', '.join(supported)}, which lcode doesn't support yet"
                if supported
                else str(e)
            ) from e
        versions = result.get("supportedVersions") or []
        if version not in versions:
            usable = [v for v in MODERN_VERSIONS if v in versions]
            if not usable:
                raise McpError(f"the server speaks MCP {', '.join(versions)}, which lcode doesn't support yet")
            self.version = usable[0]
            if isinstance(self.transport, HttpTransport):
                self.transport.protocol_version = self.version
        self.server_info = (result.get("_meta") or {}).get("io.modelcontextprotocol/serverInfo") or {}
        self.instructions = result.get("instructions") or ""
        self.capabilities = result.get("capabilities") or {}

    # -- requests
    def _request(self, method: str, params: dict, timeout: float | None = None, headers: dict | None = None) -> dict:
        headers = dict(headers or {})
        if self.era == "modern":
            params = {**params, "_meta": {**params.get("_meta", {}), **modern_meta(self.version)}}
            if self.is_http:
                headers["Mcp-Method"] = method
                if "name" in params:
                    headers["Mcp-Name"] = header_value(str(params["name"]))
        result = self.transport.request(method, params, timeout or self.timeout, headers)
        if result.get("resultType", "complete") != "complete":
            raise McpError(
                "the server asked for more input (such as a confirmation or a sign-in), which lcode can't provide yet"
                if result.get("resultType") == "input_required"
                else f"unexpected result type {result.get('resultType')!r}"
            )
        return result

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        for _ in range(MAX_TOOL_PAGES):
            result = self._request("tools/list", {"cursor": cursor} if cursor else {})
            tools += [t for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.tools_changed = False
        if self.era == "modern" and self.is_http:
            # Tools with malformed `x-mcp-header` annotations must be left out.
            tools = [t for t in tools if param_headers(t.get("inputSchema") or {}, {}) is not None]
        return tools

    def call_tool(self, tool: dict, arguments: dict, timeout: float) -> dict:
        headers = {}
        if self.era == "modern" and self.is_http:
            headers = param_headers(tool.get("inputSchema") or {}, arguments) or {}
        return self._request("tools/call", {"name": tool["name"], "arguments": arguments}, timeout, headers)

    def _notified(self, message: dict) -> None:
        if message.get("method") == "notifications/tools/list_changed":
            self.tools_changed = True

    def close(self) -> None:
        self.transport.close()


def _mentions_modern(error: RpcError) -> bool:
    """Modern-only servers should name their versions when they reject `initialize`."""
    return any(v in f"{error.message} {error.data}" for v in MODERN_VERSIONS)
