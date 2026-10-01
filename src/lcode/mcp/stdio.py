"""MCP over stdio: the server is a subprocess, messages are one JSON object per line."""

from __future__ import annotations

import itertools
import json
import os
import queue
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from lcode.mcp.protocol import METHOD_NOT_FOUND, RpcError, ServerTimeout, TransportError

INSTALL_HINTS = {
    "npx": "install Node.js 18 or newer (https://nodejs.org)",
    "node": "install Node.js 18 or newer (https://nodejs.org)",
    "uvx": "install uv (https://docs.astral.sh/uv/getting-started/installation/)",
    "docker": "install Docker (https://docs.docker.com/get-docker/)",
}


class StdioTransport:
    def __init__(self, command: list[str], env: dict[str, str], cwd: Path, log_path: Path):
        exe = shutil.which(command[0])
        if not exe:
            hint = INSTALL_HINTS.get(command[0])
            raise TransportError(f"`{command[0]}` isn't installed or isn't on PATH" + (f"; {hint}" if hint else ""))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path = log_path
        self._log = log_path.open("wb")
        try:
            self.proc = subprocess.Popen(
                [exe, *command[1:]],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._log,
                cwd=cwd,
                env={**os.environ, **env},
                start_new_session=True,  # Ctrl+C in lcode mustn't kill the server mid-request
            )
        except OSError as e:
            raise TransportError(f"couldn't start {command[0]}: {e}") from e
        self._ids = itertools.count(1)
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self.on_notification: Callable[[dict], None] | None = None
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()

    # -- messages
    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            try:
                message = json.loads(raw)
            except ValueError:
                continue  # not MCP; some servers print banners
            if not isinstance(message, dict):
                continue
            if "method" in message:
                if "id" in message:
                    self._answer(message)
                elif self.on_notification:
                    self.on_notification(message)
            elif "id" in message:
                waiting = self._pending.pop(message["id"], None)
                if waiting:
                    waiting.put(message)
        self.closed = True
        for waiting in list(self._pending.values()):
            waiting.put(None)

    def _answer(self, request: dict) -> None:
        """Legacy servers may ask the client things; lcode offers no client features besides ping."""
        if request["method"] == "ping":
            self._write({"jsonrpc": "2.0", "id": request["id"], "result": {}})
        else:
            error = {"code": METHOD_NOT_FOUND, "message": f"lcode doesn't support {request['method']}"}
            self._write({"jsonrpc": "2.0", "id": request["id"], "error": error})

    def _write(self, message: dict) -> None:
        data = (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        with self._lock:
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                raise TransportError(f"the server has exited{self.log_tail()}") from e

    def request(self, method: str, params: dict, timeout: float, headers: dict | None = None) -> dict:
        request_id = next(self._ids)
        waiting: queue.Queue = queue.Queue()
        self._pending[request_id] = waiting
        if self.closed:
            raise TransportError(f"the server has exited{self.log_tail()}")
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            message = waiting.get(timeout=timeout)
        except queue.Empty:
            self._pending.pop(request_id, None)
            try:
                self.notify("notifications/cancelled", {"requestId": request_id, "reason": "timed out"})
            except TransportError:
                pass
            raise ServerTimeout(f"no answer to {method} within {timeout:.0f}s") from None
        except KeyboardInterrupt:
            self._pending.pop(request_id, None)
            try:
                self.notify("notifications/cancelled", {"requestId": request_id, "reason": "interrupted"})
            except TransportError:
                pass
            raise
        if message is None:
            raise TransportError(f"the server has exited{self.log_tail()}")
        if "error" in message:
            error = message["error"] or {}
            raise RpcError(error.get("code", 0), str(error.get("message", "unknown error")), error.get("data"))
        return message.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    # -- lifecycle
    def log_tail(self, lines: int = 6) -> str:
        """The end of the server's stderr, to explain why it stopped."""
        try:
            self._log.flush()
            text = self.log_path.read_text(errors="replace").strip()
        except OSError:
            return ""
        tail = "\n".join(text.splitlines()[-lines:])
        return f":\n{tail}" if tail else ""

    def close(self) -> None:
        self.closed = True
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.proc.pid, sig)
                    self.proc.wait(timeout=2)
                    break
                except (OSError, subprocess.TimeoutExpired):
                    continue
        self._log.close()
