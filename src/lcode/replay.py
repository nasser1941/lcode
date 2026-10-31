"""Replaying a run trace (lcode.traces): the recorded answers stand in for the model, the tools run again in a
copy of the folder, and their results are compared with the recorded ones. No GPU or model server is needed.

Shell commands, the web, MCP tools and whatever else reaches outside the folder or asks the user get their
recorded results, unless `commands` lets shell commands and the project's checks run again too (in the copy).
The recorded folder's paths point into the copy, and file tools can't reach anything outside it.
"""

from __future__ import annotations

import contextlib
import difflib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.markup import escape

from lcode import config
from lcode.ollama import OllamaError
from lcode.traces import TERMINAL, Info, Lane, Recorder, Recording, call_summary, home, load

if TYPE_CHECKING:
    from lcode.agent import Agent

# Tools a replay runs again. The others reach outside the folder (the shell, the web, other models, MCP servers,
# language servers, your memory notes) or ask you something, so a replay gives them their recorded results.
RERUN = frozenset(
    {
        "read_file", "list_dir", "glob", "grep", "write_file", "edit_file", "apply_patch", "todo_write", "repo_map",
        "skill", "agent", "present_plan",
    }
)  # fmt: skip
COMMANDS = frozenset({"bash", "bash_output", "bash_stop"})  # run again only with `commands`
REFUSALS = ("A deny rule", "The user denied", "This needs the user's permission")  # answers the user gave
# Folders a replay doesn't copy: they're big, and rebuilt by the project's tools.
NOT_COPIED = (".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox")
MAX_COPY = 2_000_000_000  # bytes: a folder bigger than this isn't copied for a replay


class Recorded:
    """What a recorded request got from outside the folder, by where it happened: the model's answers, the
    results of the tools a replay doesn't run, the checks and a stronger model's advice."""

    def __init__(self, events: list[dict], commands: bool = False) -> None:
        self.commands = commands
        self.steps: dict[tuple[str, int], dict] = {}
        self.calls: dict[tuple[str, str, str], list[dict]] = {}
        self.tools: dict[tuple[str, int, int], dict] = {}
        self.checks: dict[str, list[dict]] = {}
        self.escalations: dict[str, list[dict]] = {}
        self.served: set[tuple[str, int, int]] = set()
        self.moves: list[tuple[str, str]] = []  # recorded folder -> the replay's copy, for paths in arguments
        self.root: Path | None = None  # the replay's copy: tools can't touch anything outside it
        self.full = False
        status, last = "", None
        for e in events:
            kind, lane = e.get("type"), e.get("lane", "main")
            if kind == "model" and e.get("kind") == "step":
                self.steps[(lane, int(e.get("step") or 0))] = e
                last = (lane, int(e.get("step") or 0))
            elif kind == "model" and not e.get("inside"):
                self.calls.setdefault((lane, e.get("purpose") or "", str(e.get("kind"))), []).append(e)
            elif kind == "tool":
                self.tools[(lane, int(e.get("step") or 0), int(e.get("index") or 0))] = e
            elif kind == "check":
                self.checks.setdefault(lane, []).append(e)
            elif kind == "escalate":
                self.escalations.setdefault(lane, []).append(e)
            elif kind == "full":
                self.full = True
            elif kind == "end":
                status = str(e.get("status") or "")
        self.interrupted = last if status == "interrupted" else None


def counters(event: dict) -> dict:
    found = {
        "prompt_eval_count": event.get("tokens_in") or 0,
        "eval_count": event.get("tokens_out") or 0,
        "prompt_eval_duration": int((event.get("prompt_seconds") or 0) * 1e9),
        "eval_duration": int((event.get("gen_seconds") or 0) * 1e9),
    }
    if event.get("done_reason"):
        found["done_reason"] = event["done_reason"]
    return found


class ReplayServer:
    """Stands in for the model server in a replay: each model call gets the answer recorded at the same place,
    lane by lane. It also hands out the recorded results of the tools a replay doesn't run again."""

    kind = "ollama"
    host = "replay"
    name = "replay"

    def __init__(self, recorded: Recorded, lane: str = "main", agent: Agent | None = None) -> None:
        self.recorded, self.lane, self.agent = recorded, lane, agent
        self.step = 0
        self.index = 0
        self.taken: Counter = Counter()
        self.current: dict | None = None  # the recorded tool call for the tool that's running

    def at(self, lane: str, agent: Agent) -> ReplayServer:
        return ReplayServer(self.recorded, lane, agent)

    @property
    def root(self) -> Path | None:
        return self.recorded.root

    def moved(self, value: Any) -> Any:
        """Arguments with the recorded folder's paths moved into the replay's copy."""
        if isinstance(value, str):
            for old, new in self.recorded.moves:
                value = value.replace(old, new)
            return value
        if isinstance(value, list):
            return [self.moved(v) for v in value]
        if isinstance(value, dict):
            return {k: self.moved(v) for k, v in value.items()}
        return value

    # What the agent asks a model server besides chats.
    def describe(self) -> str:
        return "the recorded answers"

    def version(self) -> str:
        return "replay"

    def show(self, model: str) -> dict:
        return {"capabilities": ["completion", "tools"]}

    def running(self) -> list[dict]:
        model = self.agent.settings.model if self.agent is not None else ""
        return [{"name": model, "size": 1, "size_vram": 1, "context_length": 0}]

    def load(self, *args: Any, **kwargs: Any) -> None:
        return None

    def unload(self, model: str) -> None:
        return None

    def max_context(self, model: str) -> int | None:
        return None

    def installed_names(self) -> set[str]:
        return set()

    def list_models(self) -> list[dict]:
        return []

    def embed(self, *args: Any, **kwargs: Any) -> Any:
        raise OllamaError("replay: embeddings aren't recorded")

    def next_call(self, kind: str) -> dict:
        lane = self.agent.trace if self.agent is not None else None
        what = lane.purpose if lane is not None else ""
        found = self.recorded.calls.get((self.lane, what, kind)) or []
        taken = self.taken[(what, kind)]
        if taken >= len(found):
            raise OllamaError(f"replay: the recorded run made no such model call here ({what or kind}, {self.lane})")
        self.taken[(what, kind)] += 1
        return found[taken]

    def chat_stream(self, payload: dict, **kwargs: Any) -> Iterator[dict]:
        if self.agent is not None and payload.get("messages") is self.agent.messages:
            self.step += 1
            self.index = 0
            event = self.recorded.steps.get((self.lane, self.step))
            if event is None:
                raise OllamaError(f"replay: the recorded run had no model call {self.step} ({self.lane})")
        else:
            event = self.next_call("stream")
        reply = event.get("reply") or {}
        message: dict = {"role": "assistant", "content": reply.get("content") or ""}
        if reply.get("thinking"):
            message["thinking"] = reply["thinking"]
        if reply.get("tool_calls"):
            message["tool_calls"] = json.loads(json.dumps(reply["tool_calls"]))
        if event.get("error"):
            if message["content"] or message.get("thinking"):
                yield {"message": message, "done": False}
            raise OllamaError(str(event["error"]))
        yield {"message": message, "done": False}
        if event.get("kind") == "step" and self.recorded.interrupted == (self.lane, self.step):
            raise KeyboardInterrupt
        if event.get("stopped"):
            return
        yield {"message": {}, "done": True, **counters(event)}

    def chat(self, payload: dict, *args: Any, **kwargs: Any) -> dict:
        event = self.next_call("chat")
        if event.get("error"):
            raise OllamaError(str(event["error"]))
        reply = event.get("reply") or {}
        return {"message": {"role": "assistant", "content": reply.get("content") or ""}, **counters(event)}

    def tool_result(self, name: str, args: Any) -> str | None:
        """The recorded result for a call a replay doesn't run again; None: run it."""
        self.index += 1
        key = (self.lane, self.step, self.index)
        recorded = self.recorded.tools.get(key)
        self.current = recorded if recorded is not None and recorded.get("name") == name else None
        rerun = name in RERUN or (self.recorded.commands and name in COMMANDS)
        if self.current is None:
            if rerun:
                return None
            return f"Error: replay: the recorded run made no {name} call here, and a replay doesn't run {name}."
        result = str(self.current.get("result") or "")
        if not rerun or result.startswith(REFUSALS):
            self.recorded.served.add(key)
            return result
        return None

    def plan_decision(self, title: str, plan: str) -> str:
        """What the user decided about a presented plan, from the recorded result."""
        result = str((self.current or {}).get("result") or "")
        if result.startswith("The user approved the plan"):
            return "auto-edit" if "(auto-edit mode" in result else "ask"
        return result or "The user didn't approve the plan."

    def check_result(self, command: str) -> str | None:
        """The recorded output of the project's checks, unless commands run again."""
        if self.recorded.commands:
            return None
        found = self.recorded.checks.get(self.lane) or []
        taken = self.taken["check"]
        self.taken["check"] += 1
        if taken < len(found):
            return str(found[taken].get("result") or "")
        return "Error: replay: the recorded run didn't run the checks here, and a replay doesn't run commands."

    def escalation(self, why: str) -> str | None:
        found = self.recorded.escalations.get(self.lane) or []
        taken = self.taken["escalate"]
        self.taken["escalate"] += 1
        return found[taken].get("advice") if taken < len(found) else None


@dataclass
class Difference:
    lane: str
    step: int
    what: str  # the call, e.g. "edit_file src/app.py"
    recorded: str
    now: str

    def where(self) -> str:
        return f"step {self.step}" + ("" if self.lane == "main" else f" of {self.lane}")


@dataclass
class Report:
    trace: str
    folder: Path
    status: tuple[str, str]  # recorded, now
    steps: tuple[int, int]  # the agents' model calls (subagents' too): recorded, now
    rerun: int = 0  # tool calls run again
    served: int = 0  # tool calls given their recorded results
    same: int = 0  # of those run again, with the same result
    differences: list[Difference] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.differences and self.status[0] == self.status[1]


@contextlib.contextmanager
def same_terminal(start_event: dict) -> Iterator[None]:
    """Commands in a replay see the terminal size the recorded ones saw (their output depends on it)."""
    recorded = start_event.get("terminal")
    if not isinstance(recorded, dict):
        yield
        return
    before = {name: os.environ.get(name) for name in TERMINAL}
    try:
        for name in TERMINAL:
            if recorded.get(name) is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = str(recorded[name])
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextlib.contextmanager
def private_state() -> Iterator[Path]:
    """A replay keeps its sessions, saved outputs and notes out of the user's own: in a folder of its own."""
    before = config.STATE_DIR
    folder = Path(tempfile.mkdtemp(prefix="lcode-replay-state-"))
    config.STATE_DIR = folder
    try:
        yield folder
    finally:
        config.STATE_DIR = before
        shutil.rmtree(folder, ignore_errors=True)


def replay_agent(start_event: dict, folder: Path, server: ReplayServer, console: Console) -> Agent:
    from lcode.agent import Agent, Settings

    recorded = start_event.get("settings") or {}
    allowed = Settings.__dataclass_fields__
    options = start_event.get("options") or {}
    settings = Settings(
        model=str(start_event.get("model") or "replay"),
        context=int(start_event.get("context") or 32768),
        **{k: v for k, v in recorded.items() if k in allowed},
    )
    settings.think = bool(start_event.get("think", True))
    settings.num_batch = options.get("num_batch")
    settings.max_parallel_agents = 1  # subagents one after another, in the order they were asked for
    settings.sandbox, settings.checkpoints, settings.notify = "off", False, False
    settings.memory, settings.vision_model, settings.embed_model, settings.lsp = "off", "off", "off", "off"
    settings.permission_mode = "plan" if start_event.get("permission_mode") == "plan" else "yolo"
    agent = Agent(server, settings, folder, console=console)
    server.agent = agent
    agent.replay = server
    agent.interactive = False
    agent.perms.approve = lambda request: True  # what the user refused gets its recorded refusal instead
    agent.plan_review = server.plan_decision
    agent.max_steps = int(start_event.get("max_steps") or agent.max_steps)
    if start_event.get("allowed_tools") is not None:
        agent.allowed_tools = set(start_event["allowed_tools"])
    for name in start_event.get("read") or []:
        path = (folder / name) if not Path(name).is_absolute() else Path(name)
        if path.is_file():
            agent.tools.read_mtimes[str(path)] = path.stat().st_mtime
    return agent


def run(
    events: list[dict],
    folder: Path,
    commands: bool = False,
    console: Console | None = None,
    root: Path | None = None,
) -> Report:
    """Run a recorded request again in `folder` (a copy: it changes), the recorded answers standing in for the
    model, and compare the results with the recorded ones. Tools can't change anything outside `root` (the
    copy's top folder; by default `folder`)."""
    start_event = next((e for e in events if e.get("type") == "start"), None)
    if start_event is None:
        raise ValueError("not a trace: it has no start")
    recorded = Recorded(events, commands)
    console = console or Console(file=io.StringIO(), width=120)
    folder = folder.resolve()
    recorded.root = (root or folder).resolve()
    env = next((e for e in events if e.get("type") == "env"), {})
    git_root = (env.get("git") or {}).get("root")
    if git_root and root is not None:
        recorded.moves.append((str(git_root), str(recorded.root)))
    if start_event.get("cwd"):
        recorded.moves.append((str(start_event["cwd"]), str(folder)))
    recorded.moves.sort(key=lambda move: len(move[0]), reverse=True)  # the deeper folder first
    server = ReplayServer(recorded)
    with private_state(), same_terminal(start_event):
        agent = replay_agent(start_event, folder, server, console)
        lane = Lane(Recorder(None, "replay"), "main", agent)
        agent.trace = lane
        agent.ollama = Recording(server, lane)
        status, error = "", ""
        try:
            agent.run_turn(str(start_event.get("text") or ""))
            status = agent.turn_status
        except KeyboardInterrupt:
            status = "interrupted"
        except OllamaError as e:
            status, error = "error", str(e)
        finally:
            lane.recorder.end(lane, status or "error", error)
            with contextlib.suppress(Exception):
                agent.jobs.stop_all()  # background commands it started (with commands)
    end = next((e for e in reversed(events) if e.get("type") == "end"), {})
    cwd = Path(str(start_event.get("cwd") or ""))
    report = compare(events, lane.recorder.events, cwd, folder, recorded.served)
    report.trace = str(start_event.get("trace") or "")
    report.status = (str(end.get("status") or "unfinished"), status or "error")
    report.served = len(recorded.served)
    report.error = error
    if recorded.full:
        report.error = report.error or "the trace reached its size limit, so the replay stops where it does"
    return report


def normalized(text: str, *folders: Path) -> str:
    """A result as it compares between machines and folders: the folder's path and saved-output names don't
    count, nor how long things took."""
    for folder in folders:
        if str(folder) not in ("", "."):
            text = text.replace(str(folder), "<folder>")
    text = re.sub(r"/[^\s'\"]*?outputs/\d{8}-\d{6}-", "<outputs>/", text)
    text = re.sub(r"lcode-replay-state-\w+", "<state>", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|seconds)\b", "<time>", text)
    # Lines drawn to the terminal's width (pytest's ===== and _ _ _), and the padding before [100%]
    text = re.sub(r"(?:_ ){3,}_?|([=_\-~*#])\1{3,}", "<line>", text)
    return "\n".join(" ".join(line.split()) for line in text.splitlines())


def compare(
    before: list[dict], after: list[dict], recorded_cwd: Path, folder: Path, served: set[tuple] | None = None
) -> Report:
    """What the replay did differently: tool calls and their results, lcode's notes, the checks, the steps.
    `served`: the places of the tool calls that got their recorded results."""
    report = Report("", folder, ("", ""), (0, 0))
    served = served or set()
    cwd = str(recorded_cwd)

    def by_place(events: list[dict], kind: str) -> dict[tuple, dict]:
        return {
            (e.get("lane", "main"), e.get("step", 0), e.get("index", 0)): e for e in events if e.get("type") == kind
        }

    def in_order(events: list[dict], kind: str, keep=lambda e: True) -> dict[str, list[dict]]:
        found: dict[str, list[dict]] = {}
        for e in events:
            if e.get("type") == kind and keep(e):
                found.setdefault(e.get("lane", "main"), []).append(e)
        return found

    was, now = by_place(before, "tool"), by_place(after, "tool")
    for key in sorted(set(was) | set(now), key=lambda k: (k[0] != "main", k)):
        old, new = was.get(key), now.get(key)
        lane, step = key[0], int(key[1])
        if old is None or new is None:
            e = old or new or {}
            what = call_summary(str(e.get("name")), e.get("arguments"), cwd)
            report.differences.append(
                Difference(lane, step, what, "made" if old else "not made", "not made" if old else "made")
            )
            continue
        what = call_summary(str(old.get("name")), old.get("arguments"), cwd)
        if old.get("name") != new.get("name") or old.get("arguments_sha") != new.get("arguments_sha"):
            now_call = call_summary(str(new.get("name")), new.get("arguments"), cwd)
            report.differences.append(Difference(lane, step, what, what, now_call))
            continue
        if key in served:
            continue  # it got the recorded result
        report.rerun += 1
        if old.get("sha") == new.get("sha"):
            same = True
        else:
            same = normalized(str(old.get("result")), recorded_cwd) == normalized(str(new.get("result")), folder)
        if same:
            report.same += 1
        else:
            report.differences.append(Difference(lane, step, what, str(old.get("result")), str(new.get("result"))))

    def lcode_notes(e: dict) -> bool:
        return e.get("by") == "lcode"

    notes_was, notes_now = in_order(before, "message", lcode_notes), in_order(after, "message", lcode_notes)
    for lane in sorted(set(notes_was) | set(notes_now), key=lambda name: (name != "main", name)):
        old_notes, new_notes = notes_was.get(lane, []), notes_now.get(lane, [])
        for i in range(max(len(old_notes), len(new_notes))):
            old = old_notes[i] if i < len(old_notes) else None
            new = new_notes[i] if i < len(new_notes) else None
            if (
                old
                and new
                and normalized(str(old.get("content")), recorded_cwd) == normalized(str(new.get("content")), folder)
            ):
                continue
            step = int((old or new or {}).get("step") or 0)
            report.differences.append(
                Difference(
                    lane,
                    step,
                    "lcode's note to the model",
                    str((old or {}).get("content") or "(none)"),
                    str((new or {}).get("content") or "(none)"),
                )
            )
    checks_was, checks_now = in_order(before, "check"), in_order(after, "check")
    for lane in sorted(set(checks_was) | set(checks_now)):
        old_checks, new_checks = checks_was.get(lane, []), checks_now.get(lane, [])
        for i in range(max(len(old_checks), len(new_checks))):
            old = old_checks[i] if i < len(old_checks) else {}
            new = new_checks[i] if i < len(new_checks) else {}
            if old.get("exit_code") != new.get("exit_code") or (not old) != (not new):
                report.differences.append(
                    Difference(
                        lane,
                        int((old or new).get("step") or 0),
                        f"checks: {(old or new).get('command')}",
                        f"exit code {old.get('exit_code')}" if old else "not run",
                        f"exit code {new.get('exit_code')}" if new else "not run",
                    )
                )

    def steps(events: list[dict]) -> int:
        return sum(1 for e in events if e.get("type") == "model" and e.get("kind") == "step")

    report.steps = (steps(before), steps(after))
    report.differences.sort(key=lambda d: (d.lane != "main", d.lane, d.step))
    return report


def describe_difference(d: Difference, width: int = 6) -> list[str]:
    """A difference in a few lines: a short one side by side, a long one as a diff."""
    if "\n" not in d.recorded and "\n" not in d.now and len(d.recorded) < 200 and len(d.now) < 200:
        return [f"    recorded: {d.recorded}", f"    now:      {d.now}"]
    lines = list(difflib.unified_diff(d.recorded.splitlines(), d.now.splitlines(), "recorded", "now", n=1, lineterm=""))
    shown = [f"    {line[:200]}" for line in lines[2 : 2 + width * 4]]
    if len(lines) > 2 + width * 4:
        shown.append(f"    … {len(lines) - 2 - width * 4} more line(s)")
    return shown


# ----------------------------------------------------------------------------- a folder to replay in


def replay_folder(start_event: dict, env: dict, repo: Path | None) -> tuple[Path, Path, Path, str]:
    """A scratch copy to replay in: (the folder to work in, the copy's top folder, the temporary folder to
    delete, what it is)."""
    scratch = Path(tempfile.mkdtemp(prefix="lcode-replay-"))
    recorded = Path(str(start_event.get("cwd") or ""))
    name = recorded.name or "work"
    git = env.get("git") or {}
    if repo is not None:
        target = scratch / (repo.name or "work")
        copy_folder(repo, target)
        return target, target, scratch, f"a copy of {home(str(repo))}"
    root = Path(git["root"]) if git.get("root") else None
    if git.get("commit") and root is not None and (root / ".git").exists():
        target = scratch / (root.name or "work")
        try:
            run_git(["clone", "--quiet", "--shared", "--no-checkout", str(root), str(target)])
            run_git(["-C", str(target), "checkout", "--quiet", "--detach", git["commit"]])
            run_git(["-C", str(target), "remote", "remove", "origin"])  # a replayed push goes nowhere
        except (OSError, subprocess.SubprocessError) as e:
            shutil.rmtree(scratch, ignore_errors=True)
            raise OSError(
                f"can't check out {git['commit'][:7]} of {home(str(root))}: {e} (--repo replays in a copy of another "
                "folder)"
            ) from e
        try:
            sub = recorded.relative_to(root)
        except ValueError:
            sub = Path()
        what = f"{home(str(root))} at the recorded commit {git['commit'][:7]}"
        if git.get("dirty"):
            what += " (the recording had uncommitted changes, which the copy doesn't have)"
        return target / sub, target, scratch, what
    target = scratch / name
    if recorded.is_dir() and str(recorded) not in ("", "."):
        copy_folder(recorded, target)
        return (
            target,
            target,
            scratch,
            f"a copy of {home(str(recorded))} as it is now (not a git repository, so the run's own changes are in it)",
        )
    target.mkdir()
    return target, target, scratch, f"an empty folder ({home(str(recorded))} is gone)"


def copy_folder(source: Path, target: Path) -> None:
    """A copy to replay in, without the folders tools rebuild, and without git remotes: a replayed push goes
    nowhere."""
    size = 0
    for folder, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if d not in NOT_COPIED]
        for name in files:
            with contextlib.suppress(OSError):
                size += os.lstat(os.path.join(folder, name)).st_size
        if size > MAX_COPY:
            raise OSError(
                f"{home(str(source))} takes over {MAX_COPY // 1_000_000_000} GB: replay in a smaller copy of it with "
                "--repo"
            )
    shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns(*NOT_COPIED))
    if (target / ".git").exists():
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            remotes = subprocess.run(
                ["git", "-C", str(target), "remote"], capture_output=True, text=True, timeout=30
            ).stdout.split()
            for remote in remotes:
                run_git(["-C", str(target), "remote", "remove", remote])


def run_git(args: list[str]) -> None:
    done = subprocess.run(["git", *args], capture_output=True, text=True, timeout=300)
    if done.returncode != 0:
        raise subprocess.SubprocessError(done.stderr.strip() or f"git {args[0]} failed")


def replay_command(info: Info, args: Any, console: Console) -> int:
    events = load(info.path)
    start_event = next((e for e in events if e.get("type") == "start"), {})
    env = next((e for e in events if e.get("type") == "env"), {})
    repo = Path(args.repo).expanduser().resolve() if args.repo else None
    if repo is not None and not repo.is_dir():
        console.print(f"[red]Not a folder: {escape(str(repo))}[/]")
        return 1
    try:
        folder, root, scratch, what = replay_folder(start_event, env, repo)
    except OSError as e:
        console.print(f"[red]Can't make a copy to replay in: {escape(str(e))}[/]")
        return 1
    steps = sum(1 for e in events if e.get("type") == "model" and e.get("kind") == "step")
    console.print(f"Replaying {escape(info.id)} ({steps} model answer(s)) in {escape(what)}.")
    if not args.commands and any(e.get("type") == "tool" and e.get("name") in COMMANDS for e in events):
        console.print("[dim]Shell commands get their recorded results; --commands runs them again in the copy.[/]")
    try:
        with console.status("Replaying…", spinner="dots"):
            report = run(events, folder, commands=args.commands, root=root)
    finally:
        if args.keep:
            console.print(f"[dim]The copy stays in {escape(str(folder))}.[/]")
        else:
            shutil.rmtree(scratch, ignore_errors=True)
    print_report(report, console)
    return 0 if report.ok else 1


def print_report(report: Report, console: Console) -> None:
    ran = report.rerun
    console.print(
        f"Tool calls run again: {ran}, with the same result: {report.same}. Given their recorded result: "
        f"{report.served}. Model calls: {report.steps[0]} recorded, {report.steps[1]} in the replay."
    )
    old, new = report.status
    style = "green" if old == new else "yellow"
    console.print(f"Status: recorded {escape(old)}, now [{style}]{escape(new)}[/]")
    if report.error:
        console.print(f"[yellow]{escape(report.error)}[/]")
    if not report.differences:
        console.print("[green]No differences.[/]")
        return
    console.print(f"[yellow]{len(report.differences)} difference(s):[/]")
    for d in report.differences[:20]:
        console.print(f"  {escape(d.where())} · {escape(d.what)}")
        for line in describe_difference(d):
            console.print(f"[dim]{escape(line)}[/]")
    if len(report.differences) > 20:
        console.print(f"  … and {len(report.differences) - 20} more")
