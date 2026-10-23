"""A stand-in ComfyUI for tests: its HTTP API and its websocket, with jobs that run, fail, wait or get cancelled."""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
FLUX = {
    "diffusion_models": ["flux-2-klein-4b-fp8.safetensors"],
    "text_encoders": ["qwen_3_4b.safetensors"],
    "vae": ["flux2-vae.safetensors"],
}


def tiny_png() -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x80\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


class StandIn:
    """Start with `with StandIn() as comfy:`; comfy.url is its address. Set the attributes to shape the next jobs."""

    def __init__(self, models: dict | None = None, websocket: bool = True, models_root=None):
        self.models = dict(FLUX if models is None else models)
        self.models_root = models_root  # a folder: its subfolders are the model folders, read on each request
        self.websocket = websocket  # False: no websocket, so lcode has to poll the history
        self.steps = 4
        self.ahead = 0  # jobs before the next one in the queue (they "finish" while it waits)
        self.fail = ""  # an exception message: the next job fails in its sampler
        self.hold = False  # the next job keeps running until it's cancelled
        self.reject = False  # /prompt refuses the next workflow
        self.prompts: list[dict] = []  # the workflows queued
        self.cancelled: list[str] = []
        self.freed = 0
        self.history: dict[str, dict] = {}
        self.queue: list[str] = []  # waiting jobs, ours included
        self.running: str | None = None
        self.sockets: dict[str, object] = {}  # client id -> the handler holding its websocket
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.closing = threading.Event()

    def __enter__(self) -> StandIn:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.closing.set()
        self.server.shutdown()
        self.server.server_close()

    # -- the job ----------------------------------------------------------------------------------------------

    def send(self, client_id: str, message: dict | bytes) -> None:
        handler = self.sockets.get(client_id)
        if handler is None:
            return
        try:
            if isinstance(message, bytes):
                handler.frame(2, message)
            else:
                handler.frame(1, json.dumps(message).encode())
        except OSError:  # lcode closed the websocket (cancelled, or done)
            self.sockets.pop(client_id, None)

    def run_job(self, prompt_id: str, client_id: str, graph: dict, hold: bool, fail: str, ahead: int) -> None:
        for _ in range(ahead):  # others' jobs finish first
            time.sleep(0.05)
            self.queue.pop(0)
            self.send(client_id, {"type": "status", "data": {"status": {"exec_info": {"queue_remaining": 1}}}})
        self.queue.remove(prompt_id)
        self.running = prompt_id
        sid = {"prompt_id": prompt_id}
        self.send(client_id, {"type": "execution_start", "data": sid})
        for node in ("1", "2", "3", "4", "11"):
            self.send(client_id, {"type": "executing", "data": {"node": node, **sid}})
        status = "success"
        messages = []
        for step in range(1, self.steps + 1):
            if prompt_id in self.cancelled:
                break
            self.send(client_id, {"type": "progress", "data": {"value": step, "max": self.steps, "node": "11", **sid}})
            self.send(client_id, struct.pack(">II", 1, 2) + tiny_png())
            time.sleep(0.02)
        while hold and prompt_id not in self.cancelled and not self.closing.is_set():
            time.sleep(0.02)
        if prompt_id in self.cancelled:
            status = "error"
            messages = [["execution_interrupted", sid]]
            self.send(client_id, {"type": "execution_interrupted", "data": sid})
        elif fail:
            status = "error"
            error = {"node_type": "SamplerCustomAdvanced", "exception_message": fail, **sid}
            messages = [["execution_error", error]]
            self.send(client_id, {"type": "execution_error", "data": error})
        outputs = {}
        if status == "success":
            for node in ("12", "13"):
                self.send(client_id, {"type": "executing", "data": {"node": node, **sid}})
            outputs = {"13": {"images": [{"filename": "lcode/image_00001_.png", "subfolder": "", "type": "output"}]}}
            for node_id, node in graph.items():  # previews: temporary images, like ComfyUI's PreviewImage
                if node.get("class_type") == "PreviewImage":
                    outputs[node_id] = {"images": [{"filename": "preview_00001_.png", "subfolder": "", "type": "temp"}]}
        self.history[prompt_id] = {
            "prompt": [0, prompt_id, graph, {}, ["13"]],
            "outputs": outputs,
            "status": {"status_str": status, "completed": status == "success", "messages": messages},
        }
        self.running = None
        if status == "success":
            self.send(client_id, {"type": "execution_success", "data": sid})
            self.send(client_id, {"type": "executing", "data": {"node": None, **sid}})

    # -- HTTP and the websocket ------------------------------------------------------------------------------

    def handler(self):
        standin = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, status: int, body: object, kind: str = "application/json") -> None:
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def frame(self, opcode: int, payload: bytes) -> None:
                n = len(payload)
                head = bytes([0x80 | opcode]) + (
                    bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n)
                    if n < 65536 else bytes([127]) + struct.pack(">Q", n)
                )  # fmt: skip
                with self.lock:
                    self.wfile.write(head + payload)
                    self.wfile.flush()

            def do_GET(self):
                parts = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                path = parts.path
                if path == "/ws":
                    if not standin.websocket:
                        return self.reply(404, {"error": "no websocket here"})
                    key = self.headers["Sec-WebSocket-Key"]
                    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
                    self.lock = threading.Lock()
                    standin.sockets[query["clientId"]] = self
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", accept)
                    self.end_headers()
                    self.wfile.flush()
                    self.frame(1, json.dumps({"type": "status", "data": {"sid": query["clientId"]}}).encode())
                    standin.closing.wait()  # hold the connection open until the stand-in stops
                    self.close_connection = True
                    return None
                if path == "/system_stats":
                    return self.reply(200, {"system": {"comfyui_version": "0.38.0"}, "devices": []})
                if path.startswith("/models/"):
                    folder = path.removeprefix("/models/")
                    if standin.models_root is not None:
                        where = standin.models_root / folder
                        names = sorted(p.name for p in where.iterdir() if p.is_file()) if where.is_dir() else []
                        return self.reply(200, [n for n in names if not n.endswith(".part")])
                    return self.reply(200, standin.models.get(folder, []))
                if path == "/internal/folder_paths":
                    root = standin.models_root
                    folders = ("diffusion_models", "text_encoders", "vae", "loras")
                    return self.reply(200, {f: [str(root / f)] for f in folders} if root else {})
                if path == "/queue":
                    running = [[0, standin.running, {}, {}, []]] if standin.running else []
                    pending = [[i + 1, pid, {}, {}, []] for i, pid in enumerate(standin.queue)]
                    return self.reply(200, {"queue_running": running, "queue_pending": pending})
                if path.startswith("/history/"):
                    prompt_id = path.removeprefix("/history/")
                    entry = standin.history.get(prompt_id)
                    return self.reply(200, {prompt_id: entry} if entry else {})
                if path == "/view":
                    return self.reply(200, tiny_png(), "image/png")
                return self.reply(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                path = urlsplit(self.path).path
                if path == "/prompt":
                    if standin.reject:
                        standin.reject = False
                        error = {
                            "type": "prompt_outputs_failed_validation",
                            "message": "Prompt outputs failed validation",
                        }
                        node_errors = {
                            "1": {
                                "class_type": "UNETLoader",
                                "errors": [{"message": "Value not in list", "details": "unet_name: 'x' not in []"}],
                            }
                        }
                        return self.reply(400, {"error": error, "node_errors": node_errors})
                    prompt_id = uuid.uuid4().hex
                    standin.prompts.append(body["prompt"])
                    standin.queue[:0] = [f"other-{i}" for i in range(standin.ahead)]
                    standin.queue.append(prompt_id)
                    hold, fail, ahead = standin.hold, standin.fail, standin.ahead
                    standin.hold, standin.fail, standin.ahead = False, "", 0
                    threading.Thread(
                        target=standin.run_job,
                        args=(prompt_id, body.get("client_id", ""), body["prompt"], hold, fail, ahead),
                        daemon=True,
                    ).start()
                    return self.reply(200, {"prompt_id": prompt_id, "number": 1, "node_errors": {}})
                if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                    standin.cancelled.append(path.split("/")[3])
                    return self.reply(200, {})
                if path == "/interrupt":
                    standin.cancelled.append(body.get("prompt_id", ""))
                    return self.reply(200, {})
                if path == "/free":
                    standin.freed += 1
                    return self.reply(200, {})
                return self.reply(404, {"error": "not found"})

        return Handler
