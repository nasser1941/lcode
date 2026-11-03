"""Reading recorded traces: listing them, finding one, and showing it as a timeline (lcode trace, lcode trace show)."""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.markup import escape

from lcode import config
from lcode.config import format_tokens
from lcode.traces import KEEP_DAYS, LIST_LIMIT, SUMMARY_WIDTH, stopped_estimate, traces_dir

if TYPE_CHECKING:
    pass


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
            if "estimated_out" in e:  # stopped before the server counted it
                numbers = (
                    f"about {format_tokens(e.get('estimated_in') or 0)} in · "
                    f"{format_tokens(e.get('estimated_out') or 0)} out"
                )
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
            f"{format_tokens(end.get('tokens_in') or 0)} tokens in, {format_tokens(end.get('tokens_out') or 0)} out"
            f"{stopped_estimate(end)} · "
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
