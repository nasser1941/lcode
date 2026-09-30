"""MCP over Streamable HTTP: each message is a POST; answers come back as JSON or as an SSE stream."""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterator
from typing import Protocol

import requests

from lcode.mcp.protocol import AuthRequired, RpcError, ServerTimeout, TransportError

ACCEPT = "application/json, text/event-stream"


class TokenSource(Protocol):
    def token(self) -> str | None: ...

    def renew(self, challenge: str, status: int) -> bool: ...


def sse_events(response: requests.Response) -> Iterator[str]:
    """The `data` of each server-sent event, as it arrives."""
    buffer, data = "", []
    for chunk in response.iter_content(chunk_size=None, decode_unicode=True):
        buffer += chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                if data:
                    yield "\n".join(data)
                    data = []
            elif line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
            # comments (":") and the event/id/retry fields don't matter here
    if data:
        yield "\n".join(data)


class HttpTransport:
    def __init__(self, url: str, headers: dict[str, str], auth: TokenSource | None = None):
        self.url = url
        self.headers = headers
        self.auth = auth
        self.http = requests.Session()
        self.protocol_version: str | None = None  # sent as MCP-Protocol-Version once known
        self.session_id: str | None = None  # legacy servers only
        self.on_notification: Callable[[dict], None] | None = None
        self._ids = itertools.count(1)

    def request(self, method: str, params: dict, timeout: float, headers: dict | None = None) -> dict:
        message = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        return self._send(message, timeout, headers or {}) or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        self._send(message, 30, {})

    def _headers(self, extra: dict) -> dict:
        headers = {"Accept": ACCEPT, "Content-Type": "application/json", **self.headers, **extra}
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        token = self.auth.token() if self.auth else None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _send(self, message: dict, timeout: float, extra: dict, retry_auth: bool = True) -> dict | None:
        try:
            response = self.http.post(
                self.url, json=message, headers=self._headers(extra), stream=True, timeout=(15, timeout)
            )
        except requests.Timeout as e:
            raise ServerTimeout(f"no answer from {self.url} within {timeout:.0f}s") from e
        except requests.RequestException as e:
            raise TransportError(f"can't reach {self.url}: {e}") from e
        with response:
            status = response.status_code
            challenge = response.headers.get("WWW-Authenticate", "")
            if status == 401 or (status == 403 and "insufficient_scope" in challenge):
                if self.auth is None:  # credentials from the settings (a token header) were refused
                    raise TransportError(
                        f"the server refused the credentials (HTTP {status}): check the token in mcp.json", status
                    )
                if retry_auth and self.auth.renew(challenge, status):
                    return self._send(message, timeout, extra, retry_auth=False)
                raise AuthRequired("the server needs you to sign in" if status == 401 else "more access is needed")
            if "id" not in message or "method" not in message:  # a notification or a reply: nothing comes back
                return None
            session = response.headers.get("Mcp-Session-Id")
            if session:
                self.session_id = session
            if status >= 400:
                self._raise_error(response)
            kind = response.headers.get("Content-Type", "")
            if "text/event-stream" in kind:
                return self._from_stream(response, message["id"])
            try:
                body = response.json()
            except ValueError as e:
                raise TransportError(f"{self.url} didn't answer with MCP (HTTP {status})", status) from e
            for reply in body if isinstance(body, list) else [body]:
                if isinstance(reply, dict) and reply.get("id") == message["id"]:
                    return self._unwrap(reply, status)
            raise TransportError("the server's answer didn't match the request", status)

    def _raise_error(self, response: requests.Response):
        text = response.text[:2000]
        try:
            body = json.loads(text)
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and "code" in error:
            raise RpcError(error["code"], str(error.get("message", "")), error.get("data"), response.status_code)
        detail = " ".join(text.split())[:200]
        raise TransportError(f"HTTP {response.status_code}" + (f": {detail}" if detail else ""), response.status_code)

    def _from_stream(self, response: requests.Response, request_id) -> dict:
        for data in sse_events(response):
            try:
                message = json.loads(data)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message:
                if "id" in message:  # a legacy server asking the client something
                    reply = {"jsonrpc": "2.0", "id": message["id"]}
                    if message["method"] == "ping":
                        reply["result"] = {}
                    else:
                        reply["error"] = {"code": -32601, "message": f"lcode doesn't support {message['method']}"}
                    try:
                        self._send(reply, 30, {})
                    except Exception:
                        pass
                elif self.on_notification:
                    self.on_notification(message)
            elif message.get("id") == request_id:
                return self._unwrap(message, response.status_code)
        raise TransportError("the server closed the connection without answering")

    @staticmethod
    def _unwrap(reply: dict, status: int) -> dict:
        if "error" in reply:
            error = reply["error"] or {}
            raise RpcError(error.get("code", 0), str(error.get("message", "unknown error")), error.get("data"), status)
        return reply.get("result") or {}

    def close(self) -> None:
        if self.session_id:
            try:
                self.http.delete(self.url, headers=self._headers({}), timeout=5)
            except requests.RequestException:
                pass
        self.http.close()
