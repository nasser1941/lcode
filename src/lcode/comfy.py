"""Image and video generation through ComfyUI, driven by lcode itself: no language model in between.

- **Finding it:** a ComfyUI server already answering at `comfyui_url` is used as it is. Otherwise lcode looks for an
  install (the `comfyui_path` setting, the `comfy` CLI's workspace, the usual folders, lcode's own) and starts it in
  the background with that install's own Python, and stops it again when lcode exits. With none, lcode offers to
  install one with comfy-cli.
- **A job:** lcode fills one of its built-in workflows (`lcode/workflows/*.json`, ComfyUI's API format with
  `$name` placeholders) and queues it, then follows it on ComfyUI's websocket: the step it's on, the node that's
  running, its place in ComfyUI's queue, preview pictures, and the end. Esc or Ctrl+C cancels it in ComfyUI too.
- **Outputs** are fetched from ComfyUI and saved in the project (`media_dir`), each with its settings in a JSON
  file next to it (and, for PNGs, in the file's own text chunk). Jobs are written down while they run, so results
  that finish after a crash or a closed terminal are collected the next time.

The websocket client is a small one for a local server (no TLS, no extensions), so lcode needs no new dependency.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import requests

from lcode import config

if TYPE_CHECKING:
    from lcode.hardware import Hardware

START_TIMEOUT = 180  # seconds for ComfyUI to answer after it's started (it loads its custom nodes first)
CANDIDATES = ("~/comfy/ComfyUI", "~/ComfyUI", "~/comfyui", "~/Documents/ComfyUI")  # comfy-cli's default first
OUTPUT_KINDS = ("images", "gifs", "videos", "audio")  # what ComfyUI's history lists for a node's files


class ComfyError(Exception):
    pass


class Cancelled(ComfyError):
    pass


# ----------------------------------------------------------------------------- finding and starting ComfyUI


@dataclass
class Install:
    path: Path  # the folder with main.py
    python: str  # the Python that runs it (its own environment, if it has one)


def find_install(setting: str | None = None) -> Install | None:
    """A ComfyUI install: `comfyui_path`, or the comfy CLI's workspace, one of the usual folders, lcode's own."""
    folders = [setting] if setting else [cli_workspace(), *CANDIDATES, str(own_folder())]
    for folder in folders:
        if not folder:
            continue
        path = Path(folder).expanduser()
        if is_install(path):
            return Install(path.resolve(), python_for(path))
    return None


def is_install(path: Path) -> bool:
    return (path / "main.py").is_file() and (path / "comfy").is_dir()


def own_folder() -> Path:
    """Where lcode installs ComfyUI when you have none."""
    return config.STATE_DIR / "ComfyUI"


def cli_workspace() -> str | None:
    """The ComfyUI the comfy CLI (comfy-cli) uses, if it's installed."""
    exe = shutil.which("comfy")
    if not exe:
        return None
    try:
        done = subprocess.run(
            [exe, "--skip-prompt", "--json", "which"],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
        return (json.loads(done.stdout.strip().splitlines()[-1]).get("data") or {}).get("workspace_path")
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, AttributeError):
        return None


def python_for(path: Path) -> str:
    for env in (path / ".venv", path / "venv", path.parent / ".venv", path.parent / "venv"):
        for exe in ("bin/python", "Scripts/python.exe"):
            if (env / exe).is_file():
                return str(env / exe)
    return shutil.which("python3") or "python3"


def install_hint() -> str:
    return (
        "ComfyUI runs the image and video models: lcode installs it when you make your first image (or run "
        "`uvx --from comfy-cli comfy install` yourself), or point lcode at yours: "
        "lcode config set comfyui_path /path/to/ComfyUI"
    )


def install_command(folder: Path, hardware: Hardware) -> list[str]:
    """comfy-cli's install of ComfyUI into `folder`, with the PyTorch build for this machine's GPU."""
    if hardware.unified:
        gpu = "--m-series"
    elif hardware.vram_gib or "nvidia" in (hardware.gpu or "").lower():  # lcode measures VRAM on NVIDIA cards
        gpu = "--nvidia"
    else:
        gpu = "--cpu"
    comfy = [shutil.which("comfy")] if shutil.which("comfy") else ["uvx", "--from", "comfy-cli", "comfy"]
    return [*comfy, "--skip-prompt", "--workspace", str(folder), "install", gpu, "--fast-deps"]


def install(hardware: Hardware, run=subprocess.run) -> Install:
    """Install ComfyUI into lcode's own folder (several GB: PyTorch). Its output goes to the terminal."""
    folder = own_folder()
    folder.parent.mkdir(parents=True, exist_ok=True)
    command = install_command(folder, hardware)
    if command[0] == "uvx" and not shutil.which("uvx"):
        raise ComfyError("installing ComfyUI needs uv (https://docs.astral.sh/uv/) or comfy-cli")
    try:
        done = run(command, stdin=subprocess.DEVNULL)
    except OSError as e:
        raise ComfyError(f"couldn't run comfy-cli: {e}") from e
    if done.returncode != 0 or not is_install(folder):
        raise ComfyError(f"comfy-cli couldn't install ComfyUI into {folder} (exit {done.returncode})")
    return Install(folder.resolve(), python_for(folder))


def local_models(install: Install, folder: str) -> list[str]:
    """The model files in one of the install's model folders, as ComfyUI names them (for when it isn't running)."""
    root = install.path / "models" / folder
    if not root.is_dir():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and not p.name.startswith("."))


def log_path() -> Path:
    return config.STATE_DIR / "comfyui.log"


def launch(install: Install, url: str) -> subprocess.Popen:
    """Start ComfyUI in the background on `url`'s port, logging to lcode's state folder."""
    parts = urlsplit(url)
    host, port = parts.hostname or "127.0.0.1", str(parts.port or 8188)
    log_path().parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path(), "ab")  # noqa: SIM115 - the process keeps writing to it
    return subprocess.Popen(
        [
            install.python,
            "main.py",
            "--listen",
            host,
            "--port",
            port,
            "--disable-auto-launch",
            "--preview-method",
            "auto",
        ],
        cwd=install.path,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # its own process group: stopped together, and Ctrl+C in lcode doesn't reach it
        preexec_fn=death_signal(),
    )


def death_signal():
    """On Linux, what makes ComfyUI stop when lcode goes, however it goes (a closed terminal, a crash, kill -9).

    It runs in the child before ComfyUI starts, so it's looked up here first: after the fork, only a plain call.
    The parent it watches is the thread that started it, which is lcode's main thread.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes

        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return None
    return lambda: prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


def stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)


# ----------------------------------------------------------------------------- the HTTP API


class Server:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def stats(self) -> dict | None:
        """ComfyUI's system stats (its version, the GPU), or None when nothing answers."""
        try:
            r = requests.get(self.url + "/system_stats", timeout=3)
            return r.json() if r.status_code == 200 else None
        except (requests.RequestException, ValueError):
            return None

    def wait(self, proc: subprocess.Popen | None, timeout: float = START_TIMEOUT, cancelled=None) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            stats = self.stats()
            if stats:
                return stats
            if proc is not None and proc.poll() is not None:
                raise ComfyError(f"ComfyUI stopped while starting: see {log_path()}")
            if cancelled is not None and cancelled():
                raise Cancelled("cancelled while ComfyUI was starting")
            time.sleep(0.5)
        raise ComfyError(f"ComfyUI didn't answer at {self.url} within {timeout:.0f} s: see {log_path()}")

    def models(self, folder: str) -> list[str]:
        """The model files ComfyUI has in one of its folders (diffusion_models, text_encoders, vae…)."""
        try:
            r = requests.get(f"{self.url}/models/{folder}", timeout=10)
            return list(r.json()) if r.status_code == 200 else []
        except (requests.RequestException, ValueError):
            return []

    def folder_paths(self) -> dict[str, list[str]]:
        """Where ComfyUI keeps each kind of model (its own folders and extra_model_paths.yaml's)."""
        try:
            r = requests.get(self.url + "/internal/folder_paths", timeout=10)
            return r.json() if r.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            return {}

    def upload(self, path: Path) -> str:
        """Upload an input image; the name to use in a workflow."""
        with path.open("rb") as f:
            r = requests.post(
                self.url + "/upload/image", files={"image": (path.name, f)}, data={"overwrite": "true"}, timeout=60
            )
        if r.status_code != 200:
            raise ComfyError(f"ComfyUI didn't take {path.name}: {r.text[:200]}")
        data = r.json()
        return f"{data['subfolder']}/{data['name']}" if data.get("subfolder") else data["name"]

    def queue(self, workflow: dict, client_id: str) -> str:
        try:
            r = requests.post(self.url + "/prompt", json={"prompt": workflow, "client_id": client_id}, timeout=30)
        except requests.RequestException as e:
            raise ComfyError(f"can't reach ComfyUI at {self.url}: {e}") from e
        if r.status_code != 200:
            raise ComfyError(describe_rejection(r))
        return r.json()["prompt_id"]

    def history(self, prompt_id: str) -> dict | None:
        try:
            r = requests.get(f"{self.url}/history/{prompt_id}", timeout=10)
            return r.json().get(prompt_id) if r.status_code == 200 else None
        except (requests.RequestException, ValueError):
            return None

    def fetch(self, item: dict) -> bytes:
        params = {
            "filename": item["filename"],
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        }
        r = requests.get(self.url + "/view", params=params, timeout=120)
        if r.status_code != 200:
            raise ComfyError(f"ComfyUI didn't hand over {item['filename']}: {r.status_code}")
        return r.content

    def ahead(self, prompt_id: str) -> int | None:
        """How many jobs run or wait before this one in ComfyUI's queue; None once it's running or done."""
        try:
            data = requests.get(self.url + "/queue", timeout=5).json()
        except (requests.RequestException, ValueError):
            return None
        running = data.get("queue_running") or []
        pending = sorted(data.get("queue_pending") or [], key=lambda job: job[0])
        if any(len(job) > 1 and job[1] == prompt_id for job in running):
            return None
        for i, job in enumerate(pending):
            if len(job) > 1 and job[1] == prompt_id:
                return len(running) + i
        return None

    def cancel(self, prompt_id: str) -> None:
        with contextlib.suppress(requests.RequestException):
            r = requests.post(f"{self.url}/api/jobs/{prompt_id}/cancel", timeout=5)
            if r.status_code == 200:
                return
            requests.post(self.url + "/interrupt", json={"prompt_id": prompt_id}, timeout=5)

    def ws_address(self, client_id: str) -> tuple[str, int, str]:
        parts = urlsplit(self.url)
        return parts.hostname or "127.0.0.1", parts.port or 80, f"/ws?clientId={client_id}"


def describe_rejection(r: requests.Response) -> str:
    """ComfyUI's reasons for refusing a workflow, readably: which node, and what's wrong."""
    try:
        data = r.json()
    except ValueError:
        return f"ComfyUI refused the workflow ({r.status_code}): {r.text[:300]}"
    lines = [str((data.get("error") or {}).get("message") or "ComfyUI refused the workflow")]
    for node, info in (data.get("node_errors") or {}).items():
        for err in info.get("errors", [])[:3]:
            message, detail = err.get("message") or "", err.get("details") or ""
            why = f"{message}: {detail}" if message and detail and detail not in message else (detail or message)
            lines.append(f"{info.get('class_type', 'node')} ({node}): {why}")
    return "; ".join(lines)


# ----------------------------------------------------------------------------- events (a small websocket client)


class Events:
    """ComfyUI's websocket for one client: JSON messages, and binary preview pictures."""

    def __init__(self, host: str, port: int, path: str, timeout: float = 10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ComfyError("ComfyUI closed the websocket while it opened")
            head += chunk
        head, _, self.buffer = head.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ComfyError("ComfyUI didn't open a websocket: " + head.split(b"\r\n", 1)[0].decode(errors="replace"))
        self.closed = False

    def _read(self, n: int) -> bytes:
        while len(self.buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                self.closed = True
                raise ComfyError("ComfyUI closed the websocket")
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def _send(self, opcode: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        header = bytes([0x80 | opcode])
        n = len(payload)
        header += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def ready(self, timeout: float) -> bool:
        if self.buffer:
            return True
        readable, _, _ = select.select([self.sock], [], [], timeout)
        return bool(readable)

    def next(self) -> tuple[str, object]:
        """The next message: ("json", dict) or ("binary", bytes). Answers pings on the way."""
        message, kind = b"", None
        while True:
            b0, b1 = self._read(2)
            opcode, length = b0 & 0x0F, b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else b""
            payload = self._read(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 9:  # ping
                self._send(10, payload)
                continue
            if opcode == 8:  # close
                self.closed = True
                raise ComfyError("ComfyUI closed the websocket")
            if opcode in (1, 2):
                kind = opcode
            message += payload
            if b0 & 0x80:  # the last fragment
                if kind == 1:
                    return "json", json.loads(message.decode())
                return "binary", message

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._send(8)
            self.sock.close()


# ----------------------------------------------------------------------------- workflows


@dataclass
class Workflow:
    """A graph in ComfyUI's API format with `$name` placeholders: the model's files (by role, from media.toml),
    the prompt, the seed and the settings in `defaults`."""

    key: str
    graph: dict
    defaults: dict = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)  # node id -> what it does, for progress


def load_workflow(key: str) -> Workflow:
    data = json.loads(resources.files("lcode.workflows").joinpath(f"{key}.json").read_text())
    return Workflow(key, data["graph"], data.get("defaults", {}), data.get("labels", {}))


PLACEHOLDER = re.compile(r"^\$([a-z_]+)$")


def fill(graph: dict, values: dict) -> dict:
    """The workflow with each `"$name"` value replaced by values[name] (keeping its type)."""

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            m = PLACEHOLDER.match(node)
            if m:
                if m.group(1) not in values:
                    raise ComfyError(f"the workflow needs a value for {m.group(1)}")
                return values[m.group(1)]
        return node

    return walk(json.loads(json.dumps(graph)))


# ----------------------------------------------------------------------------- running a job


@dataclass
class Event:
    kind: str  # "queued", "waiting" (value: jobs ahead), "node", "progress" (value of total), "preview" (data)
    value: int = 0
    total: int = 0
    node: str = ""
    data: bytes = b""
    prompt_id: str = ""


def run(
    server: Server, graph: dict, on_event: Callable[[Event], None], cancelled: Callable[[], bool]
) -> tuple[str, dict]:
    """Queue a workflow and follow it to the end. (prompt id, the outputs ComfyUI's history lists per node)."""
    client_id = uuid.uuid4().hex
    try:
        events = Events(*server.ws_address(client_id))
    except (OSError, ComfyError):
        events = None  # still works: lcode polls the history instead
    prompt_id = server.queue(graph, client_id)
    on_event(Event("queued", prompt_id=prompt_id))
    ahead = server.ahead(prompt_id)
    if ahead:
        on_event(Event("waiting", ahead, prompt_id=prompt_id))
    started = False  # whether ComfyUI is on this job yet (or still on others' before it)
    last_poll = 0.0
    try:
        while True:
            if cancelled():
                server.cancel(prompt_id)
                raise Cancelled("cancelled")
            if events is not None and not events.closed and events.ready(0.25):
                kind, message = events.next()
                if kind == "binary":
                    if len(message) > 8 and struct.unpack(">I", message[:4])[0] == 1:  # a preview picture
                        on_event(Event("preview", data=message[8:]))
                    continue
                data = message.get("data") or {}
                if data.get("prompt_id") not in (None, prompt_id):
                    continue
                kind = message.get("type")
                if kind == "status" and not started:  # the queue moved
                    ahead = server.ahead(prompt_id)
                    if ahead:
                        on_event(Event("waiting", ahead, prompt_id=prompt_id))
                    continue
                if kind in ("execution_start", "executing", "progress"):
                    started = True
                if kind == "progress":
                    on_event(
                        Event(
                            "progress", int(data.get("value", 0)), int(data.get("max", 0)), str(data.get("node") or "")
                        )
                    )
                elif kind == "executing" and data.get("node"):
                    on_event(Event("node", node=str(data["node"])))
                elif kind == "execution_error":
                    why = str(data.get("exception_message", "")).strip()[:400]
                    raise ComfyError(f"{data.get('node_type', 'a node')} failed: {why}")
                elif kind == "execution_interrupted":
                    raise Cancelled("cancelled in ComfyUI")
                elif kind == "execution_success" or (kind == "executing" and data.get("node") is None):
                    break
            elif time.time() - last_poll > (2 if events is None or events.closed else 10):
                last_poll = time.time()
                entry = server.history(prompt_id)
                if entry and (entry.get("status") or {}).get("completed", True):
                    status = (entry.get("status") or {}).get("status_str")
                    if status == "error":
                        raise ComfyError("ComfyUI reported an error: " + describe_history_error(entry))
                    break
                if events is None or events.closed:
                    time.sleep(0.25)
    finally:
        if events is not None:
            events.close()
    entry = None
    for _ in range(40):  # the history can lag the last message by a moment
        entry = server.history(prompt_id)
        if entry and entry.get("outputs"):
            break
        time.sleep(0.25)
    return prompt_id, (entry or {}).get("outputs") or {}


def describe_history_error(entry: dict) -> str:
    for kind, data in (entry.get("status") or {}).get("messages") or []:
        if kind == "execution_error":
            return f"{data.get('node_type', 'a node')}: {str(data.get('exception_message', '')).strip()[:300]}"
    return "unknown"


def save(server: Server, outputs: dict, folder: Path, stem: str, meta: dict) -> list[Path]:
    """Fetch the job's files into `folder`: stem.png (stem-2.png if that's taken), or stem-1.png, … for several,
    each with its settings in a .json next to it."""
    folder.mkdir(parents=True, exist_ok=True)
    items = [item for node in outputs.values() for kind in OUTPUT_KINDS for item in node.get(kind, [])]
    items = [it for it in items if it.get("type", "output") == "output"]
    saved = []
    for i, item in enumerate(items, 1):
        ext = Path(item["filename"]).suffix or ".png"
        path = unique(folder / (f"{stem}{ext}" if len(items) == 1 else f"{stem}-{i}{ext}"))
        data = server.fetch(item)
        if ext.lower() == ".png":
            data = with_text(data, "lcode", json.dumps(meta, ensure_ascii=False))
        path.write_bytes(data)
        path.with_suffix(".json").write_text(
            json.dumps({**meta, "file": path.name}, indent=2, ensure_ascii=False) + "\n"
        )
        saved.append(path)
    return saved


def with_text(png: bytes, key: str, text: str) -> bytes:
    """The PNG with a text chunk added (key: text), before its end; anything that isn't a PNG is left as it is."""
    end = png.rfind(b"IEND")
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or end < 4:
        return png
    body = b"iTXt" + key.encode("latin-1") + b"\0\0\0\0\0" + text.encode()  # uncompressed, no language
    chunk = struct.pack(">I", len(body) - 4) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    return png[: end - 4] + chunk + png[end - 4 :]


def unique(path: Path) -> Path:
    n = 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    return candidate


def slug(text: str, limit: int = 40) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return words[:limit].rstrip("-") or "media"


# ----------------------------------------------------------------------------- jobs, kept across a crash


def jobs_path() -> Path:
    return config.STATE_DIR / "media-jobs.json"


def _read_jobs() -> dict:
    try:
        return json.loads(jobs_path().read_text())
    except (OSError, ValueError):
        return {}


def _write_jobs(jobs: dict) -> None:
    with contextlib.suppress(OSError):
        jobs_path().parent.mkdir(parents=True, exist_ok=True)
        jobs_path().write_text(json.dumps(jobs, indent=2) + "\n")


def remember(prompt_id: str, url: str, folder: Path, stem: str, meta: dict) -> None:
    jobs = _read_jobs()
    jobs[prompt_id] = {"url": url, "folder": str(folder), "stem": stem, "meta": meta, "started": time.time()}
    _write_jobs(jobs)


def forget(prompt_id: str) -> None:
    jobs = _read_jobs()
    if jobs.pop(prompt_id, None) is not None:
        _write_jobs(jobs)


def collect(server: Server) -> list[Path]:
    """Results of jobs that finished while lcode wasn't watching (a crash, a closed terminal)."""
    saved = []
    for prompt_id, job in list(_read_jobs().items()):
        if job.get("url", "").rstrip("/") != server.url:
            continue
        entry = server.history(prompt_id)
        if entry is None:
            if time.time() - job.get("started", 0) > 86400:
                forget(prompt_id)  # ComfyUI restarted since: its history is gone
            continue
        if entry.get("outputs"):
            with contextlib.suppress(ComfyError, OSError, requests.RequestException):
                saved += save(server, entry["outputs"], Path(job["folder"]), job["stem"], job.get("meta", {}))
        forget(prompt_id)
    return saved
