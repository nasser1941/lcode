"""Run traces: what each request did, step by step, recorded on this machine; and replaying one without a model.

Every request in code mode is recorded while it runs (unless the `trace` setting is off), one JSON object per line
in ~/.local/state/lcode/traces/<session id>-<request number>.jsonl. The lines, by "type":

- start: the request as typed, the model and its settings, lcode's version, the folder;
- env: the git commit and the model's own sampling settings, looked up in the background;
- message: what lcode or the user added to the conversation for the model (the request, lcode's notes);
- model: one model call: its answer (text, thinking, tool calls), time to the first token, time and tokens. The
  server counts a reply's tokens only when it ends, so a reply lcode stopped has 0 there and an estimate in
  estimated_in and estimated_out (a token is about three characters);
- tool: one tool call: its arguments, the result the model got, how long it took;
- check: the project's checks, when lcode ran them; escalate: a stronger model's advice;
- compacted: the conversation was summarized; full: the trace reached its size limit;
- end: how the request ended, and its totals (estimated_in and estimated_out: the stopped replies' estimates).

Events carry a "lane": the main agent's is "main", a subagent's "agent 1", "agent 2"…

Nothing is sent anywhere. Traces older than KEEP_DAYS are deleted, and the oldest go once all of them take MAX_TOTAL.

lcode.replay runs a trace again without a model.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from rich.console import Console
from rich.markup import escape

from lcode import __version__, config
from lcode.config import format_tokens
from lcode.ollama import Ollama, OllamaError

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
    "lsp", "embed_model", "checks", "review", "escalate_model", "repeat_limit", "think_budget", "compact_at",
    "secret_check", "sandbox", "vision_model",
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


def estimate(payload: dict, reply: dict) -> tuple[int, int]:
    """About how many tokens a model call took, in and out, for a reply the server never counted: a token is about
    three characters, as lcode.sessions.estimate_tokens counts. For qwen3.6, Ollama counted 2.7 to 3.4 characters
    a token in replies and 3.2 in tool results (the JSON), and about 3.9 in lcode's system prompt and tools. The
    prompt is its messages' and tools' JSON, images left out."""
    try:
        messages = [
            {k: v for k, v in m.items() if k != "images"} if isinstance(m, dict) else m
            for m in payload.get("messages") or []
        ]
        prompt = len(json.dumps(messages, default=str)) + len(json.dumps(payload.get("tools") or [], default=str))
        calls = reply.get("tool_calls") or []
        answer = len(str(reply.get("content") or "")) + len(str(reply.get("thinking") or ""))
        answer += len(json.dumps(calls, default=str)) if calls else 0
        return prompt // 3, answer // 3
    except (TypeError, ValueError, RecursionError):  # an estimate never costs the model event
        return 0, 0


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
            "estimated_in": 0,  # replies lcode stopped, which the server never counted
            "estimated_out": 0,
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
            if payload.get("think") is False:
                event["think"] = False  # asked without thinking: the setting is off, or the reply before went nowhere
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
            event["stopped"] = True  # lcode closed the stream: circles, thinking too long, or interrupted
            if not final and first is not None:  # the server counts a reply only at its end: about what this took
                event["estimated_in"], event["estimated_out"] = estimate(payload, reply)
                self.recorder.add(estimated_in=event["estimated_in"], estimated_out=event["estimated_out"])
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


def stopped_estimate(end: dict) -> str:
    """The end line's note on replies lcode stopped, which the server never counted."""
    if not (end.get("estimated_in") or end.get("estimated_out")):
        return ""
    return (
        f" (and about {format_tokens(end.get('estimated_in') or 0)} in, "
        f"{format_tokens(end.get('estimated_out') or 0)} out in stopped replies)"
    )


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
    agent.ollama = cast(Ollama, Recording(inner, lane))  # records each call, and passes it on
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
    child.ollama = cast(Ollama, Recording(inner, lane))


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
                shown: Any = show(model) or {}
                found = parse_parameters(str(shown.get("parameters") or ""))
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


# ----------------------------------------------------------------------------- showing


# ----------------------------------------------------------------------------- exporting


# ----------------------------------------------------------------------------- lcode trace


def command(args: Any, console: Console) -> int:
    """`lcode trace`: list, show, export or replay traces."""
    from lcode.trace_export import export_command
    from lcode.trace_view import find, list_traces, print_list, show

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
