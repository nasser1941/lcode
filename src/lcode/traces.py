"""Run traces: what each request did, step by step, recorded on this machine; and replaying one without a model.

Every request in code mode is recorded while it runs (unless the `trace` setting is off), one JSON object per line
in ~/.local/state/lcode/traces/<session id>-<request number>.jsonl. The lines, by "type":

- start: the request as typed, the model and its settings, lcode's version, the folder;
- env: the git commit and the model's own sampling settings, looked up in the background;
- message: what lcode or the user added to the conversation for the model (the request, lcode's notes);
- model: one model call: its answer (text, thinking, tool calls), time to the first token, time and tokens;
- tool: one tool call: its arguments, the result the model got, how long it took;
- check: the project's checks, when lcode ran them; escalate: a stronger model's advice;
- compacted: the conversation was summarized; full: the trace reached its size limit;
- end: how the request ended, and its totals.

Events carry a "lane": the main agent's is "main", a subagent's "agent 1", "agent 2"…

Nothing is sent anywhere. Traces older than KEEP_DAYS are deleted, and the oldest go once all of them take MAX_TOTAL.

lcode.replay runs a trace again without a model.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import getpass
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.markup import escape

from lcode import __version__, config
from lcode.config import format_tokens
from lcode.ollama import OllamaError

if TYPE_CHECKING:
    from lcode.agent import Agent

SCHEMA = 1
KEEP_DAYS = 30
MAX_TRACE = 20_000_000  # bytes of one trace: past this, only how the request ended is added
MAX_TOTAL = 200_000_000  # bytes of all traces: the oldest go first
MAX_TEXT = 200_000  # characters of one text in a trace: a tool result, an answer, thinking
SHOWN_ARGUMENT = 2_000  # characters of one argument in a tool event (the whole call is in the model event)
GIT_TIMEOUT = 2
LIST_LIMIT = 20
SUMMARY_WIDTH = 70  # characters of a command, path or query in a one-line summary of a tool call
# The settings a request ran with that a replay of it uses too.
REPLAYED = (
    "mode", "web", "memory", "subagents", "max_parallel_agents", "trust_project", "skills", "prune", "repo_map",
    "lsp", "embed_model", "checks", "review", "escalate_model", "repeat_limit", "compact_at", "secret_check",
    "sandbox", "vision_model",
)  # fmt: skip
TERMINAL = ("COLUMNS", "LINES")  # pytest and others fit their output to these: a replay's commands get the same
ACTIONS = ("list", "show", "export", "replay", "path")


def traces_dir() -> Path:
    return config.STATE_DIR / "traces"


def digest(value: Any) -> str:
    """A short fingerprint of a text or a JSON value: equal fingerprints, equal values."""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def cut(text: str, limit: int = MAX_TEXT) -> str:
    """A long text's start and end, saying how much is left out."""
    if len(text) <= limit:
        return text
    head = limit * 3 // 4
    tail = text[len(text) - (limit - head) :]
    return f"{text[:head]}\n[… {len(text) - limit} characters left out of the trace …]\n{tail}"


def nanos(value: Any) -> float | None:
    return round(value / 1e9, 3) if isinstance(value, (int, float)) and value else None


# ----------------------------------------------------------------------------- recording


class Recorder:
    """Writes one request's trace as it runs, a line per event, so a crash keeps everything before it.
    Without a path it keeps the events in memory (a replay's own trace)."""

    def __init__(self, path: Path | None, trace_id: str) -> None:
        self.path, self.id = path, trace_id
        self.file = path.open("w", encoding="utf-8", errors="replace") if path else None
        self.events: list[dict] = []  # without a file
        self.started = time.monotonic()
        self.size = 0
        self.full = False
        self.closed = False
        self.spent = 0.0  # seconds the request spent writing its trace
        self.lock = threading.Lock()  # subagents running at the same time write too
        self.lanes = 0
        self.totals: dict[str, Any] = {
            "model_calls": 0,
            "tool_calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "model_seconds": 0.0,
            "tool_seconds": 0.0,
        }
        self.first: dict = {}  # the main agent's first step: what its prompt and tools were
        self.looked = threading.Event()  # the env event is written (lcode looks it up in the background)

    def write(self, event: dict) -> None:
        began = time.perf_counter()
        try:
            data = {"type": event["type"], "at": round(time.monotonic() - self.started, 3)}
            data.update((k, v) for k, v in event.items() if k != "type")
            with self.lock:
                if self.closed or (self.full and event["type"] != "end"):
                    return
                if self.file is None:
                    self.events.append(json.loads(json.dumps(data, default=str)))
                    return
                line = json.dumps(data, ensure_ascii=False, default=str)
                if self.size + len(line) > MAX_TRACE and event["type"] != "end":
                    self.full = True
                    line = json.dumps({"type": "full", "at": data["at"], "limit": MAX_TRACE})
                self.file.write(line + "\n")
                self.file.flush()
                self.size += len(line) + 1
        except (OSError, ValueError, TypeError):
            pass  # a trace never stops a request
        finally:
            self.spent += time.perf_counter() - began

    def add(self, **counts: float) -> None:
        with self.lock:
            for key, value in counts.items():
                self.totals[key] += value

    def end(self, lane: Lane, status: str, error: str = "", changed: list[str] | None = None) -> None:
        totals = {k: round(v, 3) if isinstance(v, float) else v for k, v in self.totals.items()}
        event: dict = {"type": "end", "status": status}
        if error:
            event["error"] = error[:2000]
        event.update(seconds=round(time.monotonic() - self.started, 3), steps=lane.step, **totals, **self.first)
        event.update(changed=changed or [], trace_seconds=round(self.spent, 4))
        if self.full:
            event["full"] = True
        self.write(event)
        with self.lock:
            self.closed = True
            if self.file is not None:
                with contextlib.suppress(OSError):
                    self.file.close()


class Lane:
    """One agent's part of a trace: the main agent's ("main") or a subagent's ("agent 1", "agent 2"…)."""

    def __init__(self, recorder: Recorder, name: str, agent: Agent) -> None:
        self.recorder, self.name, self.agent = recorder, name, agent
        self.step = 0  # the agent's model calls so far
        self.index = 0  # its tool calls since the last one
        self.purpose = ""  # what a model call outside the agent's steps is for: "summary", "review"
        self.inside = ""  # a tool or an escalation running: model calls inside it aren't made again by a replay
        self.messages: list[dict] | None = None
        self.seen = len(agent.messages)

    def sub(self, agent: Agent) -> Lane:
        with self.recorder.lock:
            self.recorder.lanes += 1
            number = self.recorder.lanes
        return Lane(self.recorder, f"agent {number}", agent)

    def sync(self, messages: list[dict]) -> None:
        """Before a step: write down what was added to the conversation since the last one (the request, lcode's
        notes, the user's notes), and whether it was summarized meanwhile."""
        if self.messages is not None and messages is not self.messages:
            self.recorder.write(
                {"type": "compacted", "lane": self.name, "step": self.step + 1, "messages": len(messages)}
            )
            self.seen = len(messages)
        self.messages = messages
        for message in messages[self.seen :]:
            if message.get("role") == "user":
                content = str(message.get("content") or "")
                by = "lcode" if content.startswith("[lcode]") else "user"
                self.recorder.write(
                    {
                        "type": "message",
                        "lane": self.name,
                        "step": self.step + 1,
                        "by": by,
                        "chars": len(content),
                        "sha": digest(content),
                        "content": cut(content),
                    }
                )
        self.seen = len(messages)

    def model(
        self,
        kind: str,
        payload: dict,
        began: float,
        first: float | None,
        reply: dict,
        final: dict,
        error: str = "",
        finished: bool = True,
    ) -> None:
        seconds = time.monotonic() - began
        tokens_in, tokens_out = int(final.get("prompt_eval_count") or 0), int(final.get("eval_count") or 0)
        messages = payload.get("messages") or []
        event: dict = {"type": "model", "lane": self.name, "kind": kind}
        if kind == "step":
            event["step"] = self.step
        else:
            event["after"] = self.step
            if self.purpose:
                event["purpose"] = self.purpose
            if self.inside:
                event["inside"] = self.inside
        event.update(
            model=payload.get("model", ""),
            messages=len(messages),
            first_token=None if first is None else round(first, 3),
            seconds=round(seconds, 3),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            prompt_seconds=nanos(final.get("prompt_eval_duration")),
            gen_seconds=nanos(final.get("eval_duration")),
            load_seconds=nanos(final.get("load_duration")),
            done_reason=final.get("done_reason"),
        )
        if kind == "step":
            system = messages[0].get("content", "") if messages and messages[0].get("role") == "system" else ""
            tools = payload.get("tools") or []
            event["prompt_sha"], event["tools_sha"] = digest(str(system)), digest(tools)
            if self.name == "main" and not self.recorder.first:
                names = [(t.get("function") or {}).get("name", "") for t in tools if isinstance(t, dict)]
                self.recorder.first = {
                    "prompt_sha": event["prompt_sha"],
                    "tools_sha": event["tools_sha"],
                    "tools": names,
                }
        event["reply"] = {k: cut(v) if isinstance(v, str) else v for k, v in reply.items() if v}
        if error:
            event["error"] = error[:4000]
        elif not finished:
            event["stopped"] = True  # lcode closed the stream: it went round in circles, or was interrupted
        self.recorder.add(model_calls=1, tokens_in=tokens_in, tokens_out=tokens_out, model_seconds=seconds)
        self.recorder.write(event)

    def tool_begin(self) -> None:
        self.inside = f"tool {self.step}.{self.index + 1}"

    def tool(self, call_id: str, name: str, args: Any, result: str, seconds: float) -> None:
        self.index += 1
        self.inside = ""
        shown = (
            {k: cut(v, SHOWN_ARGUMENT) if isinstance(v, str) else v for k, v in args.items()}
            if isinstance(args, dict)
            else args
        )
        event = {
            "type": "tool",
            "lane": self.name,
            "step": self.step,
            "index": self.index,
            "id": call_id,
            "name": name,
            "arguments": shown,
            "arguments_sha": digest(args),
            "seconds": round(seconds, 3),
            "chars": len(result),
            "sha": digest(result),
            "result": cut(result),
        }
        if result.startswith("Error:"):
            event["error"] = True
        self.recorder.add(tool_calls=1, tool_seconds=seconds)
        self.recorder.write(event)

    def check(self, command: str, result: str, code: int | None) -> None:
        self.recorder.write(
            {
                "type": "check",
                "lane": self.name,
                "step": self.step,
                "command": command,
                "exit_code": code,
                "chars": len(result),
                "sha": digest(result),
                "result": cut(result),
            }
        )

    def escalation(self, why: str, advice: str | None) -> None:
        self.recorder.write({"type": "escalate", "lane": self.name, "step": self.step, "why": why, "advice": advice})


class Recording:
    """The model client while a request is recorded: everything passes through, and each model call is written
    down in the lane, with its answer, its timing and its tokens."""

    def __init__(self, inner: Any, lane: Lane) -> None:
        self.inner, self.lane = inner, lane

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def chat_stream(self, payload: dict, **kwargs: Any) -> Iterator[dict]:
        lane = self.lane
        step = payload.get("messages") is lane.agent.messages  # the agent's own step, not a summary
        if step:
            lane.sync(lane.agent.messages)
            lane.step += 1
            lane.index = 0
        began = time.monotonic()
        first: float | None = None
        content: list[str] = []
        thinking: list[str] = []
        calls: list[dict] = []
        final: dict = {}
        error, finished = "", False
        try:
            for chunk in self.inner.chat_stream(payload, **kwargs):
                if first is None:
                    first = time.monotonic() - began
                message = chunk.get("message") or {}
                if message.get("content"):
                    content.append(message["content"])
                if message.get("thinking"):
                    thinking.append(message["thinking"])
                if message.get("tool_calls"):
                    calls.extend(json.loads(json.dumps(message["tool_calls"], default=str)))
                if chunk.get("done"):
                    final = chunk
                yield chunk
            finished = True
        except OllamaError as e:
            error = str(e)
            raise
        finally:
            with contextlib.suppress(Exception):
                reply = {"content": "".join(content), "thinking": "".join(thinking), "tool_calls": calls}
                lane.model("step" if step else "stream", payload, began, first, reply, final, error, finished)

    def chat(self, payload: dict, *args: Any, **kwargs: Any) -> dict:
        began = time.monotonic()
        reply: dict = {}
        error = ""
        try:
            reply = self.inner.chat(payload, *args, **kwargs)
            return reply
        except OllamaError as e:
            error = str(e)
            raise
        finally:
            with contextlib.suppress(Exception):
                message = (reply or {}).get("message") or {}
                answer = {k: message.get(k) for k in ("content", "thinking", "tool_calls")}
                self.lane.model("chat", payload, began, None, answer, reply or {}, error)


def next_number(folder: Path, session_id: str) -> int:
    numbers = [0]
    for f in folder.glob(f"{session_id}-*.jsonl"):
        tail = f.stem[len(session_id) + 1 :]
        if tail.isdigit():
            numbers.append(int(tail))
    return max(numbers) + 1


def read_files(agent: Agent) -> list[str]:
    """Files read earlier in the conversation, which the model may edit without reading them again."""
    found = []
    for name in agent.tools.read_mtimes:
        path = Path(name)
        try:
            found.append(str(path.relative_to(agent.cwd)))
        except ValueError:
            found.append(name)
    return sorted(found)


def start(agent: Agent, text: str) -> Lane | None:
    """Start recording a request, when the trace setting is on. Returns the main agent's lane."""
    if not agent.settings.trace or agent.trace is not None:
        return None
    folder = traces_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        number = next_number(folder, agent.session_id)
        trace_id = f"{agent.session_id}-{number}"
        recorder = Recorder(folder / f"{trace_id}.jsonl", trace_id)
    except OSError:
        return None
    settings = agent.settings
    lane = Lane(recorder, "main", agent)
    recorder.write(
        {
            "type": "start",
            "schema": SCHEMA,
            "lcode": __version__,
            "trace": trace_id,
            "session": agent.session_id,
            "request": number,
            "time": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "text": text,
            "cwd": str(agent.cwd),
            "model": settings.model,
            "server": getattr(agent.ollama, "kind", "ollama"),
            "context": settings.context,
            "options": agent.options(),
            "think": settings.think,
            "keep_alive": settings.keep_alive,
            "permission_mode": agent.perms.mode,
            "settings": {key: getattr(settings, key) for key in REPLAYED},
            "max_steps": agent.max_steps,
            "allowed_tools": sorted(agent.allowed_tools) if agent.allowed_tools is not None else None,
            "interactive": agent.interactive,
            "terminal": {name: os.environ.get(name) for name in TERMINAL},  # commands' output depends on it
            "history": len(agent.messages),
            "read": read_files(agent),
        }
    )
    inner = agent.ollama
    agent.trace = lane
    agent.ollama = Recording(inner, lane)
    threading.Thread(target=look_around, args=(recorder, inner, settings.model, agent.cwd, folder), daemon=True).start()
    return lane


def finish(agent: Agent, lane: Lane, status: str, error: str = "") -> None:
    """The request ended: write its totals and stop recording."""
    if isinstance(agent.ollama, Recording) and agent.ollama.lane is lane:
        agent.ollama = agent.ollama.inner
    if agent.trace is lane:
        agent.trace = None
    lane.recorder.looked.wait(1.0)  # a request quicker than git: its trace still gets the commit
    lane.recorder.end(lane, status, error, sorted(agent.tools.before))


def follow(parent: Agent, child: Agent) -> None:
    """A subagent's model calls and tools go in a lane of their own in the parent's trace; in a replay, it gets
    its own recorded answers too."""
    if parent.trace is None:
        return
    from lcode.replay import ReplayServer

    lane = parent.trace.sub(child)
    inner = parent.ollama.inner if isinstance(parent.ollama, Recording) else parent.ollama
    if isinstance(inner, ReplayServer):
        inner = inner.at(lane.name, child)
        child.replay = inner
    child.trace = lane
    child.ollama = Recording(inner, lane)


@contextlib.contextmanager
def purpose(agent: Agent, what: str) -> Iterator[None]:
    """Model calls in this block are for `what` ("summary", "review"): a replay answers them by that."""
    lane = agent.trace
    if lane is None:
        yield
        return
    before, lane.purpose = lane.purpose, what
    try:
        yield
    finally:
        lane.purpose = before


@contextlib.contextmanager
def inside(agent: Agent, what: str) -> Iterator[None]:
    """Model calls in this block belong to something a replay doesn't run again (it has the recorded outcome)."""
    lane = agent.trace
    if lane is None:
        yield
        return
    before, lane.inside = lane.inside, what
    try:
        yield
    finally:
        lane.inside = before


def look_around(recorder: Recorder, client: Any, model: str, cwd: Path, folder: Path) -> None:
    """What takes a moment to find out, off the request's path: the git commit and the model's sampling
    settings. Then old traces are cleaned up."""
    try:
        recorder.write({"type": "env", "git": git_state(cwd), "sampling": sampling(client, model)})
    except Exception:
        pass  # what can't be found out isn't recorded
    finally:
        recorder.looked.set()
    with contextlib.suppress(OSError):
        clean(folder, keep=recorder.path)


def git_state(cwd: Path) -> dict | None:
    """The commit the folder is at, and whether it has uncommitted changes (to tracked files)."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT
        )
        if head.returncode != 0:
            return None
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {
        "commit": head.stdout.strip(),
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
        "root": top.stdout.strip() if top.returncode == 0 else None,
    }


_SAMPLING: dict[tuple[str, str], dict] = {}


def sampling(client: Any, model: str) -> dict:
    """The model's own sampling settings (temperature, top_p…) from Ollama: lcode doesn't set them."""
    key = (str(getattr(client, "host", "")), model)
    if key not in _SAMPLING:
        found: dict = {}
        show = getattr(client, "show", None)
        if callable(show) and getattr(client, "kind", "ollama") == "ollama":
            with contextlib.suppress(Exception):
                found = parse_parameters(str((show(model) or {}).get("parameters") or ""))
        _SAMPLING[key] = found
    return _SAMPLING[key]


def parse_parameters(text: str) -> dict:
    """Ollama's `parameters` text ("temperature 0.6\\ntop_k 20\\nstop "<|im_end|>"") as a dictionary."""
    found: dict[str, Any] = {}
    for line in text.splitlines():
        key, _, value = line.strip().partition(" ")
        if not key:
            continue
        value = value.strip().strip('"')
        number: Any = value
        with contextlib.suppress(ValueError):
            number = float(value)
            number = int(number) if number.is_integer() and "." not in value else number
        if key in found:
            found[key] = [*found[key], number] if isinstance(found[key], list) else [found[key], number]
        else:
            found[key] = number
    return found


def clean(folder: Path, keep: Path | None = None) -> None:
    """Delete traces older than KEEP_DAYS, then the oldest until they take at most MAX_TOTAL."""
    cutoff = time.time() - KEEP_DAYS * 86400
    files = []
    for f in folder.glob("*.jsonl"):
        try:
            info = f.stat()
        except OSError:
            continue
        if f == keep:
            continue
        if info.st_mtime < cutoff:
            f.unlink(missing_ok=True)
        else:
            files.append((info.st_mtime, info.st_size, f))
    total = sum(size for _, size, _ in files)
    for _, size, f in sorted(files):
        if total <= MAX_TOTAL:
            break
        f.unlink(missing_ok=True)
        total -= size


# ----------------------------------------------------------------------------- reading


@dataclass
class Info:
    """A trace in the list: its start and end lines."""

    id: str
    path: Path
    start: dict
    end: dict | None
    size: int
    updated: float

    @property
    def status(self) -> str:
        return (self.end or {}).get("status") or "unfinished"

    @property
    def text(self) -> str:
        return " ".join(str(self.start.get("text") or "").split())


def read_info(path: Path) -> Info | None:
    try:
        stat = path.stat()
        with path.open("rb") as f:
            first = f.readline()
            f.seek(max(0, stat.st_size - 65536))
            tail = f.read().splitlines()
        start_event = json.loads(first)
    except (OSError, ValueError):
        return None
    if not isinstance(start_event, dict) or start_event.get("type") != "start":
        return None
    end = None
    if tail:
        with contextlib.suppress(ValueError):
            last = json.loads(tail[-1])
            end = last if isinstance(last, dict) and last.get("type") == "end" else None
    trace_id = str(start_event.get("trace") or path.stem)
    return Info(trace_id, path, start_event, end, stat.st_size, stat.st_mtime)


def list_traces() -> list[Info]:
    """Every trace on this machine, newest first."""
    folder = traces_dir()
    if not folder.is_dir():
        return []
    stamped = []
    for f in folder.glob("*.jsonl"):
        with contextlib.suppress(OSError):
            stamped.append((f.stat().st_mtime, f))
    found = []
    for _, f in sorted(stamped, reverse=True):
        info = read_info(f)
        if info is not None:
            found.append(info)
    return found


def find(query: str | None, infos: list[Info]) -> Info | None:
    """A trace by its number in the list (1: the newest), its id or a unique start of it, or a trace file."""
    q = (query or "1").strip()
    if q.isdigit() and 1 <= int(q) <= len(infos):
        return infos[int(q) - 1]
    for info in infos:
        if info.id == q:
            return info
    starting = [info for info in infos if info.id.startswith(q)]
    if len(starting) == 1:
        return starting[0]
    path = Path(q).expanduser()
    if path.suffix == ".jsonl" and path.is_file():
        return read_info(path)
    return None


def load(path: Path) -> list[dict]:
    events = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            with contextlib.suppress(ValueError):
                event = json.loads(line)
                if isinstance(event, dict) and "type" in event:
                    events.append(event)
    return events


# ----------------------------------------------------------------------------- showing


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 10:
        return f"{seconds:.1f} s"
    if seconds < 60:
        return f"{seconds:.0f} s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} min {rest} s" if rest else f"{minutes} min"


def home(path: str) -> str:
    folder = str(Path.home())
    return "~" + path[len(folder) :] if path == folder or path.startswith(folder + os.sep) else path


def call_summary(name: str, args: Any, cwd: str = "") -> str:
    """read_file src/app.py, bash pytest -q…: a tool call in a few words (paths relative to `cwd`)."""
    if name == "agent" and isinstance(args, dict):
        about = " ".join(str(args.get("description") or args.get("task") or "").split())
        return f"agent {args.get('type', '')}: {about[:60] + '…' if len(about) > 60 else about}".strip()
    if isinstance(args, dict):
        for key in ("path", "command", "pattern", "query", "url", "type", "name", "question", "title", "id"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                value = " ".join(value.split())
                if cwd and cwd not in ("/", "."):
                    value = value.replace(cwd.rstrip("/") + "/", "")
                return f"{name} {path_end(value) if key == 'path' else shortened(value)}"
    return name


def shortened(text: str, width: int = SUMMARY_WIDTH) -> str:
    return text[:width] + "…" if len(text) > width else text


def path_end(path: str, width: int = SUMMARY_WIDTH) -> str:
    """The end of a long path, where the file name is: …/tests/test_app.py."""
    if len(path) <= width:
        return path
    tail = path[-width:]
    cut = min((i for i in (tail.find("/"), tail.find("\\")) if i >= 0), default=0)
    return "…" + tail[cut:]


def result_summary(result: str) -> str:
    first = next((line.strip() for line in result.splitlines() if line.strip()), "")
    if result.startswith("Error:"):
        return first[:100]
    lines = result.count("\n") + 1 if result else 0
    return f"{lines} lines" if lines > 1 else first[:80]


def show(info: Info, console: Console, verbose: bool = False) -> None:
    """The trace as a timeline: each model call and tool call, with times and tokens."""
    events = load(info.path)
    s = info.start
    env = next((e for e in events if e.get("type") == "env"), {})
    git = env.get("git") or {}
    when = str(s.get("time", ""))[:16].replace("T", " ")
    end = info.end or {}
    took = f" in {duration(end.get('seconds'))}" if end else ""
    console.print(f"[bold]Trace {escape(info.id)}[/] · {when} · {escape(info.status)}{took}")
    console.print(f"Request: {escape(info.text[:300])}")
    where = home(str(s.get("cwd", "")))
    if git.get("commit"):
        where += f" at {git['commit'][:7]}" + (" (with uncommitted changes)" if git.get("dirty") else "")
    think = "thinking on" if s.get("think") else "thinking off"
    context = format_tokens(int(s.get("context") or 0)) if s.get("context") else "?"
    console.print(
        f"[dim]{escape(str(s.get('model')))} · {context} context · {think} · {escape(str(s.get('permission_mode')))} "
        f"mode · lcode {escape(str(s.get('lcode')))} · {escape(where)}[/]"
    )
    if env.get("sampling"):
        shown = ", ".join(
            f"{k} {v}" for k, v in env["sampling"].items() if k in ("temperature", "top_p", "top_k", "min_p")
        )
        if shown:
            console.print(f"[dim]Sampling (the model's own): {escape(shown)}[/]")
    console.print()
    cwd = str(s.get("cwd") or "")

    def began(e: dict) -> float:  # events are written when they end; the timeline shows when they started
        return max(0.0, (e.get("at") or 0) - ((e.get("seconds") or 0) if e.get("type") in ("model", "tool") else 0))

    for e in sorted(events, key=began):
        kind = e.get("type")
        at = f"{began(e):7.1f} s"
        main = e.get("lane", "main") == "main"
        lane = "" if main else f"[cyan]{escape(e['lane'])}[/] "
        if kind == "message":
            first = e.get("step") == 1 and e.get("by") == "user"
            if main and first:
                continue  # the request, shown above
            label = "lcode" if e.get("by") == "lcode" else ("task" if first else "you")
            text = " ".join(str(e.get("content", "")).split())
            console.print(f"[dim]{at}[/]  {lane}[yellow]{label}:[/] {escape(text[:160])}")
        elif kind == "model":
            reply = e.get("reply") or {}
            calls = [(c.get("function") or {}).get("name", "?") for c in reply.get("tool_calls") or []]
            if e.get("kind") == "step":
                head = f"model {e.get('step')}"
            else:
                head = f"model ({e.get('purpose') or e.get('inside') or e.get('kind')})"
            numbers = f"{format_tokens(e.get('tokens_in') or 0)} in · {format_tokens(e.get('tokens_out') or 0)} out"
            if e.get("first_token") is not None:
                numbers += f" · first token {duration(e['first_token'])}"
                if (e.get("load_seconds") or 0) >= 1:  # what the wait was: loading the model, reading the prompt
                    numbers += f" (loading {duration(e['load_seconds'])}"
                    numbers += f", reading {duration(e['prompt_seconds'])})" if e.get("prompt_seconds") else ")"
            numbers += f" · {duration(e.get('seconds'))}"
            if e.get("error"):
                outcome = f"[red]error: {escape(str(e['error']).splitlines()[0][:120])}[/]"
            elif calls:
                outcome = "→ " + escape(", ".join(calls))
            elif e.get("stopped"):
                outcome = "[yellow]stopped[/]"
            else:
                outcome = "→ answer" + (
                    " [yellow](cut off: context full)[/]" if e.get("done_reason") == "length" else ""
                )
            console.print(f"[dim]{at}[/]  {lane}[bold]{head}[/]  [dim]{numbers}[/]  {outcome}")
            if verbose and reply.get("content"):
                console.print(f"            [dim]{escape(str(reply['content']).strip()[:600])}[/]")
        elif kind == "tool":
            result = str(e.get("result", ""))
            style = "red" if e.get("error") else "dim"
            console.print(
                f"[dim]{at}[/]  {lane}  {escape(call_summary(str(e.get('name')), e.get('arguments'), cwd))}  "
                f"[{style}]{escape(result_summary(result))}[/]  [dim]{duration(e.get('seconds'))}[/]"
            )
            if verbose and result and not e.get("error"):
                for line in result.splitlines()[:3]:
                    console.print(f"              [dim]{escape(line[:160])}[/]")
        elif kind == "check":
            code = e.get("exit_code")
            style = "green" if code == 0 else "yellow"
            console.print(
                f"[dim]{at}[/]  {lane}  checks: {escape(str(e.get('command')))}  [{style}]exit code {code}[/]"
            )
        elif kind == "escalate":
            answered = "answered" if e.get("advice") else "no advice"
            console.print(f"[dim]{at}[/]  {lane}  stronger model asked ({escape(str(e.get('why'))[:100])}): {answered}")
        elif kind == "compacted":
            console.print(f"[dim]{at}[/]  {lane}  [yellow]conversation summarized[/]")
        elif kind == "full":
            console.print(f"[dim]{at}[/]  [yellow]The trace reached its size limit here: the rest isn't recorded.[/]")
    if end:
        console.print()
        line = (
            f"{escape(info.status)} · {end.get('model_calls', 0)} model call(s) · "
            f"{format_tokens(end.get('tokens_in') or 0)} tokens in, {format_tokens(end.get('tokens_out') or 0)} out · "
            f"model {duration(end.get('model_seconds'))} · tools {duration(end.get('tool_seconds'))} "
            f"({end.get('tool_calls', 0)} calls) · recording {end.get('trace_seconds', 0) * 1000:.0f} ms"
        )
        console.print(line)
        if end.get("error"):
            console.print(f"[red]{escape(str(end['error'])[:500])}[/]")
        if end.get("changed"):
            console.print(f"[dim]Changed: {escape(', '.join(end['changed'][:20]))}[/]")
    else:
        console.print("\n[yellow]No end: lcode stopped before the request finished (closed, or crashed).[/]")


def print_list(infos: list[Info], console: Console, limit: int = LIST_LIMIT) -> None:
    from lcode.sessions import age

    if not infos:
        try:
            on = bool(config.load().get("trace", True))
        except config.ConfigError:
            on = True
        if on:
            console.print(f"No traces yet. Each request in code mode is recorded in {home(str(traces_dir()))}.")
        else:
            console.print("No traces: recording is off. Turn it on with: lcode config set trace true")
        return
    console.print(f"[bold] #  {'When':<11} {'Request':<44} {'Model':<20} {'Steps':>5} {'Time':>10}  Status[/]")
    for number, info in enumerate(infos[:limit], 1):
        end = info.end or {}
        text = info.text
        text = text if len(text) <= 44 else text[:43] + "…"
        model = str(info.start.get("model") or "")
        model = model if len(model) <= 20 else model[:19] + "…"
        steps = str(end.get("steps", "")) if end else ""
        took = duration(end.get("seconds")) if end else ""
        style = "green" if info.status == "success" else "yellow"
        console.print(
            f"{number:>2}  {age(info.updated):<11} {escape(text):<44} {escape(model):<20} {steps:>5} {took:>10}  "
            f"[{style}]{escape(info.status)}[/]"
        )
    if len(infos) > limit:
        console.print(f"[dim]… and {len(infos) - limit} older (lcode trace list -n {len(infos)})[/]")
    total = sum(i.size for i in infos)
    size = f"{total / 1e6:.1f} MB" if total >= 1e6 else f"{max(1, round(total / 1e3))} KB"
    console.print(
        f"[dim]{len(infos)} trace(s), {size}, in {escape(home(str(traces_dir())))} (kept {KEEP_DAYS} "
        "days). lcode trace show 1 · export 1 · replay 1[/]"
    )


# ----------------------------------------------------------------------------- exporting


EMAIL = re.compile(r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)")
OTHER_HOME = re.compile(r"(?<![\w.-])(/home/|/Users/)[^/\s\"'<>:;,|]+")
WINDOWS_HOME = re.compile(r"(?i)\b([A-Z]:\\+Users\\+)[^\\\s\"'<>:;,|]+")
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION", re.I)


class Masker:
    """Masks what a bug report shouldn't carry: secrets, your home folder, your user and host names, email and
    IP addresses. Errs on the side of masking too much."""

    def __init__(self) -> None:
        from lcode import secretscan

        self.secretscan = secretscan
        self.counts: Counter = Counter()
        self.home = str(Path.home())
        names: set[str] = set()
        with contextlib.suppress(Exception):
            names.add(getpass.getuser())
        with contextlib.suppress(Exception):
            import pwd

            names.add(pwd.getpwnam(getpass.getuser()).pw_gecos.split(",")[0])
        for key in ("user.name", "user.email"):
            with contextlib.suppress(Exception):
                value = subprocess.run(
                    ["git", "config", "--global", "--get", key], capture_output=True, text=True, timeout=GIT_TIMEOUT
                ).stdout.strip()
                names.add(value)
        self.names = sorted((n.strip() for n in names if len(n.strip()) >= 3), key=len, reverse=True)
        self.hosts = []
        with contextlib.suppress(Exception):
            host = socket.gethostname()
            self.hosts = sorted({host, host.split(".")[0]} - {"", "localhost"}, key=len, reverse=True)
        # Values of secret-looking environment variables: masked wherever they turn up, whatever they look like.
        self.values = sorted(
            {v for k, v in os.environ.items() if SECRET_NAME.search(k) and len(v) >= 8 and not v.startswith("/")},
            key=len,
            reverse=True,
        )

    def text(self, value: str) -> str:
        if not value:
            return value
        for secret in self.values:
            if secret in value:
                self.counts["secrets"] += value.count(secret)
                value = value.replace(secret, "<secret>")
        value, n = PRIVATE_KEY.subn("<private key>", value)
        self.counts["secrets"] += n
        for _, pattern in self.secretscan.STRONG:
            value, n = pattern.subn("<secret>", value)
            self.counts["secrets"] += n
        value = self.secretscan.URL_PASSWORD.sub(self._url_password, value)
        value = self.secretscan.ASSIGNED.sub(self._assigned, value)
        if self.home and self.home != "/":
            n = value.count(self.home)
            if n:
                value = value.replace(self.home, "~")
                self.counts["home folder paths"] += n
        value, n = OTHER_HOME.subn(r"\1<user>", value)
        self.counts["home folder paths"] += n
        value, n = WINDOWS_HOME.subn(r"\1<user>", value)
        self.counts["home folder paths"] += n
        value, n = EMAIL.subn("<email>", value)
        self.counts["email addresses"] += n
        for name in self.names:
            value, n = re.subn(rf"(?<![\w]){re.escape(name)}(?![\w])", "<user>", value, flags=re.I)
            self.counts["user names"] += n
        for host in self.hosts:
            value, n = re.subn(rf"(?<![\w.-]){re.escape(host)}(?![\w-])", "<host>", value, flags=re.I)
            self.counts["host names"] += n
        return IPV4.sub(self._address, value)

    def _url_password(self, m: re.Match) -> str:
        if self.secretscan.placeholder(m.group("value")):
            return m.group(0)
        self.counts["secrets"] += 1
        return m.group(0).replace(f":{m.group('value')}@", ":<secret>@")

    def _assigned(self, m: re.Match) -> str:
        value = m.group("value")
        if self.secretscan.placeholder(value) or self.secretscan.ABOUT.search(m.group("name")) or value.isdigit():
            return m.group(0)
        self.counts["secrets"] += 1
        return m.group(0).replace(value, "<secret>")

    def _address(self, m: re.Match) -> str:
        try:
            address = ipaddress.ip_address(m.group(0))
        except ValueError:
            return m.group(0)
        if address.is_loopback or address.is_unspecified:
            return m.group(0)
        self.counts["IP addresses"] += 1
        return "<ip>"

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        return value


def export(info: Info, out: io.TextIOBase | None = None, path: Path | None = None) -> Counter:
    """Write the trace with secrets and personal details masked; returns what was masked, by kind."""
    masker = Masker()
    lines = []
    for event in load(info.path):
        masked = masker.value(event)
        if masked.get("type") == "start":
            masked["exported"] = {
                "lcode": __version__,
                "time": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        lines.append(json.dumps(masked, ensure_ascii=False))
    text = "\n".join(lines) + "\n"
    if path is not None:
        path.write_text(text, encoding="utf-8")
    if out is not None:
        out.write(text)
    return masker.counts


# ----------------------------------------------------------------------------- lcode trace


def command(args: Any, console: Console) -> int:
    """`lcode trace`: list, show, export or replay traces."""
    action, query = args.action, args.trace
    if action not in ACTIONS:
        if query is not None:
            console.print(f"[red]Unknown action {escape(action)}: use one of {', '.join(ACTIONS)}.[/]")
            return 2
        action, query = "show", action
    if action == "path":
        print(traces_dir())
        return 0
    infos = list_traces()
    if action == "list":
        print_list(infos, console, args.limit)
        return 0
    info = find(query, infos)
    if info is None:
        if not infos and not query:
            print_list(infos, console)
        else:
            console.print(
                f"[red]No trace {escape(query or '')}.[/] Give a number from the list (lcode trace), a trace id or a "
                "trace file."
            )
        return 1
    if action == "show":
        show(info, console, args.verbose)
        return 0
    if action == "export":
        return export_command(info, args.output, console)
    from lcode.replay import replay_command

    return replay_command(info, args, console)


def export_command(info: Info, output: str | None, console: Console) -> int:
    import sys

    if output == "-":
        export(info, out=sys.stdout)
        return 0
    path = Path(output).expanduser() if output else Path.cwd() / f"lcode-trace-{info.id}.jsonl"
    try:
        counts = export(info, path=path)
    except OSError as e:
        console.print(f"[red]Can't write {escape(str(path))}: {escape(str(e))}[/]")
        return 1
    masked = ", ".join(f"{n} {kind}" for kind, n in counts.most_common() if n) or "nothing needed it"
    console.print(f"Exported to {escape(str(path))}")
    console.print(f"[dim]Masked: {escape(masked)}. Read it before you share it: masking can miss things.[/]")
    return 0
