"""Model Context Protocol basics shared by the transports: versions, errors and results.

lcode speaks both eras of MCP. Legacy servers (protocol 2025-11-25 and earlier) expect an
`initialize` handshake; modern servers (2026-07-28 and later) are stateless and expect the protocol
version and client capabilities in every request's `_meta`.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable

from lcode import __version__

MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
CLIENT_INFO = {"name": "lcode", "title": "lcode", "version": __version__}

# JSON-RPC error codes the modern spec defines; any of them means the server speaks modern MCP.
HEADER_MISMATCH = -32020
MISSING_CLIENT_CAPABILITY = -32021
UNSUPPORTED_VERSION = -32022
MODERN_ERROR_CODES = {HEADER_MISMATCH, MISSING_CLIENT_CAPABILITY, UNSUPPORTED_VERSION}
METHOD_NOT_FOUND = -32601


class McpError(Exception):
    """Something went wrong talking to an MCP server; the message is meant for the user."""


class RpcError(McpError):
    """The server answered with a JSON-RPC error."""

    def __init__(self, code: int, message: str, data=None, http_status: int | None = None):
        super().__init__(f"{message} (error {code})")
        self.code, self.message, self.data, self.http_status = code, message, data, http_status


class TransportError(McpError):
    """The server couldn't be reached, exited, or answered with something that isn't MCP."""

    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


class ServerTimeout(McpError):
    pass


class AuthRequired(McpError):
    """The server wants credentials that lcode doesn't have (or that were rejected)."""


def modern_meta(version: str) -> dict:
    return {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def header_value(value: str) -> str:
    """An HTTP header value, Base64-wrapped when it isn't plain printable ASCII (modern MCP rule)."""
    plain = bool(re.fullmatch(r"[\x21-\x7e]([\x20-\x7e]*[\x21-\x7e])?", value))
    if plain and not (value.startswith("=?base64?") and value.endswith("?=")):
        return value
    return f"=?base64?{base64.b64encode(value.encode()).decode()}?="


def param_headers(schema: dict, arguments: dict) -> dict[str, str] | None:
    """Mcp-Param-* headers for tool arguments marked with `x-mcp-header`; None if the schema is invalid."""
    headers: dict[str, str] = {}
    seen: set[str] = set()

    def walk(node: dict, values) -> bool:
        for key, prop in (node.get("properties") or {}).items():
            if not isinstance(prop, dict):
                continue
            name = prop.get("x-mcp-header")
            value = values.get(key) if isinstance(values, dict) else None
            if name is not None:
                if (
                    not isinstance(name, str)
                    or not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name)
                    or name.lower() in seen
                    or prop.get("type") not in ("string", "integer", "boolean")
                ):
                    return False
                seen.add(name.lower())
                if value is not None:
                    text = str(value).lower() if isinstance(value, bool) else str(value)
                    headers[f"Mcp-Param-{name}"] = header_value(text)
            if prop.get("type") == "object" and not walk(prop, value):
                return False
        return True

    return headers if walk(schema, arguments) else None


def result_text(result: dict, image_text: Callable[[str, str], str] | None = None) -> str:
    """Turn a tools/call result into text for the model. `image_text(data, mime)` describes images."""
    parts = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "image" and image_text and item.get("data"):
            parts.append(image_text(item["data"], item.get("mimeType", "image/png")))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} ({item.get('mimeType', 'unknown type')}) not shown]")
        elif kind == "resource_link":
            parts.append(f"[resource: {item.get('name') or ''} {item.get('uri', '')}]".replace("  ", " "))
        elif kind == "resource":
            resource = item.get("resource") or {}
            if "text" in resource:
                parts.append(f"[{resource.get('uri', 'resource')}]\n{resource['text']}")
            else:
                parts.append(f"[binary resource {resource.get('uri', '')} not shown]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], indent=2, ensure_ascii=False))
    text = "\n".join(parts) if parts else "(no output)"
    return f"Error: {text}" if result.get("isError") else text
