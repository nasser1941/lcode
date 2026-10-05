"""Subagents: hand a task to a fresh agent with its own context; only its report comes back.

The main model calls the `agent` tool with an agent type and a self-contained task. The subagent is
an Agent of its own with an empty conversation, so exploring a large codebase or reading long logs
doesn't fill the main context: the main model only sees the final report.

Subagents use the session's model and context window. A different window would make Ollama reload
the model, and on a single GPU that costs more than it saves. They share the session's permissions
(every prompt is asked in the main thread, labelled with the agent), sandbox and checkpoints, so
/undo covers their changes too.

Several agent calls in one response run at the same time when `max_parallel_agents` allows it.
Ollama only runs them in parallel when its server is started with OLLAMA_NUM_PARALLEL > 1; with
one slot, interleaved requests would evict each other's cached prompt, so lcode runs them one after
another by default. Workers that edit files at the same time each get a git worktree, and their
changes come back as a diff to approve.

Built-in types are explore, plan and worker. Custom agents are markdown files with a frontmatter
(description, tools, model, max_steps, isolation) in `.lcode/agents/` of the repository or in
`~/.config/lcode/agents/`; the body is their instructions.
"""

from __future__ import annotations

import contextlib
import io
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text

from lcode import config
from lcode.checkpoints import _clean_env, work_tree_for
from lcode.frontmatter import split as frontmatter
from lcode.ollama import OllamaError

if TYPE_CHECKING:
    from lcode.agent import Agent
    from lcode.permissions import Permissions

READ_ONLY_TOOLS = frozenset(
    {
        "read_file",
        "list_dir",
        "glob",
        "grep",
        "bash",
        "web_search",
        "web_fetch",
        "view_image",
        "todo_write",
        "skill",
        "lsp",
        "repo_map",
        "search_code",
    }
)
EDIT_TOOLS = frozenset({"write_file", "edit_file"})
NOT_FOR_SUBAGENTS = frozenset({"agent", "memory"})  # no agents inside agents; only the main session remembers
MAX_STEPS = 200
MAX_UNTRACKED_BYTES = 10 * 1024 * 1024  # larger untracked files aren't copied into a worker's worktree
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

ROLE_PROMPT = """
# You are a subagent
The main agent gave you one task. You have your own context: the main agent can't see your work, only your final message, so make that message a complete report. Work on your own: don't ask questions, because nobody will answer them; if something is unclear, make a reasonable choice and say so in the report.
{instructions}"""

EXPLORE_PROMPT = """Your job: find what the task asks for in the codebase and explain it. Search broadly first (glob, grep), then read the relevant parts. Don't change anything; run only read-only commands.
Report: the answer first, then the evidence as `path:line` references, and anything you looked for but didn't find. Keep it short (under about 300 words) unless the task asks for more."""

PLAN_PROMPT = """Your job: work out how to make the change the task describes, without making it. Read the code it touches first. Don't change anything; run only read-only commands.
Report a numbered plan: which files and functions change and how, in what order, how to verify the result (which tests or commands), and the risks or open questions."""

WORKER_PROMPT = """Your job: make the change the task describes, completely, then verify it (run the tests, or at least the code you changed). Stay within the task: don't refactor or fix unrelated things.
Report: what you changed (files and a sentence each), how you verified it and the result, and anything left to do."""


class AgentDefError(ValueError):
    pass


@dataclass(frozen=True)
class AgentType:
    name: str
    description: str
    instructions: str
    tools: frozenset[str] | None = None  # None: every tool a subagent may use
    read_only: bool = False  # only read-only shell commands, no file changes
    max_steps: int = 60
    model: str | None = None  # None: the session's model
    isolation: str = "auto"  # auto: a worktree only when other editing agents run at the same time | worktree | none
    source: str = "built-in"

    @property
    def edits(self) -> bool:
        return not self.read_only and (self.tools is None or bool(self.tools & (EDIT_TOOLS | {"bash"})))


BUILT_IN = {
    "explore": AgentType(
        "explore",
        "finds code and answers questions about the codebase (read-only)",
        EXPLORE_PROMPT,
        READ_ONLY_TOOLS,
        read_only=True,
        max_steps=40,
    ),
    "plan": AgentType(
        "plan",
        "works out a step-by-step plan for a change, without making it (read-only)",
        PLAN_PROMPT,
        READ_ONLY_TOOLS,
        read_only=True,
        max_steps=40,
    ),
    "worker": AgentType(
        "worker", "makes a self-contained change and verifies it (edits files, runs commands)", WORKER_PROMPT
    ),
}


# ----------------------------------------------------------------------------- agent types


def agent_dirs(cwd: Path, include_project: bool = True) -> list[Path]:
    """Where custom agents live, lowest priority first: the user's, then the repository's (once approved)."""
    return [config.CONFIG_DIR / "agents", *([work_tree_for(cwd) / ".lcode" / "agents"] if include_project else [])]


def parse_agent(text: str, name: str, source: str) -> AgentType:
    from lcode.tools import TOOL_NAMES

    split = frontmatter(text)
    if not split:
        raise AgentDefError("needs a frontmatter with a description (--- description: … ---)")
    fields, body = split
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise AgentDefError("the file name must be lowercase letters, digits, - or _")
    description = " ".join(fields.get("description", "").split())
    if not description:
        raise AgentDefError("the frontmatter needs a description")
    preset = fields.get("tools", "all").strip().lower()
    read_only = preset == "read-only"
    if preset == "all":
        tools = None
    elif read_only:
        tools = READ_ONLY_TOOLS
    else:
        tools = frozenset(t.strip() for t in re.split(r"[,\s]+", preset.strip("[]")) if t.strip())
        unknown = tools - (TOOL_NAMES - NOT_FOR_SUBAGENTS)
        if unknown:
            raise AgentDefError(f"unknown tools: {', '.join(sorted(unknown))}")
    if "read_only" in fields:
        read_only = fields["read_only"].lower() in ("true", "yes", "1")
    try:
        max_steps = max(1, min(int(fields.get("max_steps", 60)), MAX_STEPS))
    except ValueError as e:
        raise AgentDefError("max_steps must be a number") from e
    isolation = fields.get("isolation", "auto").lower()
    if isolation not in ("auto", "worktree", "none"):
        raise AgentDefError("isolation must be auto, worktree or none")
    return AgentType(
        name=name,
        description=description,
        instructions=body.strip() or WORKER_PROMPT,
        tools=tools,
        read_only=read_only,
        max_steps=max_steps,
        model=fields.get("model") or None,
        isolation=isolation,
        source=source,
    )


def load_types(cwd: Path, include_project: bool = True) -> tuple[dict[str, AgentType], list[str]]:
    """The agent types for `cwd` (custom ones override built-in ones), and problems with custom ones."""
    types = dict(BUILT_IN)
    problems = []
    for folder in agent_dirs(cwd, include_project):
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.md")):
            try:
                types[f.stem] = parse_agent(f.read_text(errors="replace"), f.stem, str(f))
            except (AgentDefError, OSError) as e:
                problems.append(f"{f}: {e}")
    return types, problems


def schema(types: dict[str, AgentType], parallel: int) -> dict:
    from lcode.tools import _fn

    listing = "\n".join(f"- {t.name}: {t.description}" for t in types.values())
    together = (
        f" Several agent calls in one response run at the same time (up to {parallel})."
        if parallel > 1
        else " Agent calls run one after another."
    )
    return _fn(
        "agent",
        "Hand a task to a subagent with its own fresh context. It works on its own and returns only its final "
        "report, so your context stays small: use it for broad searches, reading lots of code or logs, planning, "
        "and self-contained changes. It can't see this conversation, so describe the task completely."
        f"{together} Types:\n{listing}",
        {
            "type": {"type": "string", "enum": list(types)},
            "task": {
                "type": "string",
                "description": "The complete task: what to do or find, why, and everything the subagent needs to know",
            },
            "description": {"type": "string", "description": "3 to 6 words for the progress line"},
        },
        ["type", "task"],
    )


def parallel_slots(spec, context: int, hw, most: int = 4) -> int:
    """How many requests Ollama could run at once with this model: each one needs its own context cache."""
    from lcode.catalog import HEADROOM_GIB

    fits = 1
    for n in range(2, most + 1):
        if spec.memory_gib(context * n) <= hw.budget_gib - HEADROOM_GIB:
            fits = n
    return fits


# ----------------------------------------------------------------------------- worktrees for workers


class WorktreeError(Exception):
    pass


class Worktree:
    """A temporary git worktree with the working tree's current state (uncommitted changes included)."""

    def __init__(self, cwd: Path):
        self.root = work_tree_for(cwd)
        if self._git("rev-parse", "--verify", "-q", "HEAD", check=False) == "":
            raise WorktreeError("isolating a worker needs a git repository with at least one commit")
        common = Path(self._git("rev-parse", "--git-common-dir").strip())
        common = (self.root / common).resolve()
        self.path = common / "lcode-worktrees" / uuid.uuid4().hex[:10]
        base = self._git("stash", "create").strip() or self._git("rev-parse", "HEAD").strip()
        self._git("worktree", "add", "--detach", "-q", str(self.path), base)
        try:
            untracked = [p for p in self._git("ls-files", "--others", "--exclude-standard", "-z").split("\0") if p]
            copied = False
            for rel in untracked:
                source = self.root / rel
                if source.is_file() and not source.is_symlink() and source.stat().st_size <= MAX_UNTRACKED_BYTES:
                    (self.path / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, self.path / rel)
                    copied = True
            if copied:
                self._git("add", "-A", cwd=self.path)
                self._git("commit", "-q", "--no-verify", "-m", "lcode: starting point", cwd=self.path)
            self.base = self._git("rev-parse", "HEAD", cwd=self.path).strip()
        except (WorktreeError, OSError):
            self.remove()
            raise
        self.cwd = self.path / cwd.resolve().relative_to(self.root)

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True, stdin: str | None = None) -> str:
        identity = ["-c", "user.name=lcode", "-c", "user.email=lcode@localhost", "-c", "commit.gpgsign=false"]
        try:
            r = subprocess.run(
                ["git", *identity, *args],
                cwd=cwd or self.root,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=120,
                env=_clean_env(),
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise WorktreeError(f"git {args[0]} failed: {e}") from e
        if r.returncode != 0:
            if not check:
                return ""
            raise WorktreeError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")
        return r.stdout

    def diff(self) -> str:
        """Everything the worker changed, as a patch against its starting point."""
        self._git("add", "-A", cwd=self.path)
        return self._git("diff", "--cached", "--binary", self.base, cwd=self.path)

    def apply(self, patch: str) -> None:
        """Apply the worker's patch to the real working tree."""
        self._git("apply", "--whitespace=nowarn", "--binary", "-", stdin=patch)

    def remove(self) -> None:
        self._git("worktree", "remove", "--force", str(self.path), check=False)
        if self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)
        self._git("worktree", "prune", check=False)


def changed_files(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/(.+?) b/", patch, re.M)


# ----------------------------------------------------------------------------- permissions from subagents


class Broker:
    """Permission prompts from subagent threads, asked one at a time in the main thread."""

    def __init__(self, perms: Permissions, cancel: threading.Event):
        self.perms = perms
        self.cancel = cancel
        self.waiting: queue.Queue = queue.Queue()

    def ask(self, *args) -> tuple[bool, str]:
        """Called in a subagent's thread: wait until the main thread has asked the user."""
        box: dict = {}
        done = threading.Event()
        self.waiting.put((args, box, done))
        while not done.wait(0.1):
            if self.cancel.is_set():
                return False, "Stopped by the user."
        return box["answer"]

    def serve(self, pause: Callable[[], contextlib.AbstractContextManager]) -> None:
        """Called in the main thread: ask the questions that are waiting."""
        while True:
            try:
                args, box, done = self.waiting.get_nowait()
            except queue.Empty:
                return
            try:
                with pause():
                    box["answer"] = self.perms.request(*args)
            except (EOFError, KeyboardInterrupt):
                box["answer"] = (False, "The user denied this action.")
                done.set()
                raise
            done.set()


class Relay:
    """A subagent's permissions: the main session's, labelled with the agent and asked in the main thread."""

    def __init__(self, perms: Permissions, label: str, pause: Callable[[], contextlib.AbstractContextManager]):
        self.perms = perms
        self.label = label
        self.pause = pause
        self.broker: Broker | None = None

    @property
    def mode(self) -> str:
        return self.perms.mode

    @property
    def rules(self):
        return self.perms.rules

    def rule(self, kind: str, target: str):
        return self.perms.rule(kind, target)

    def request(self, key: str, kind: str, title: str, body, target: str = "") -> tuple[bool, str]:
        verdict = self.perms.rule(kind, target)
        if verdict is not None:
            return verdict
        if not self.perms.needs_prompt(key, kind):
            return True, ""
        title = f"{self.label} › {title}"
        if self.broker and threading.current_thread() is not threading.main_thread():
            return self.broker.ask(key, kind, title, body, target)
        with self.pause():
            return self.perms.request(key, kind, title, body, target)


# ----------------------------------------------------------------------------- one subagent


class Line:
    """The progress line of one subagent: ↳ explore: find the auth code · grep('login') · 3 tools · 12s"""

    def __init__(self, kind: str, label: str):
        self.kind, self.label = kind, label
        self.action = "starting"
        self.tools = 0
        self.started = time.time()
        self.ended: float | None = None
        self.outcome = ""  # done | stopped | failed

    def tool(self, description: str) -> None:
        self.tools += 1
        self.action = description

    def finish(self, outcome: str) -> None:
        self.ended, self.outcome = time.time(), outcome

    @property
    def seconds(self) -> float:
        return (self.ended or time.time()) - self.started

    def __rich__(self) -> Text:
        if self.outcome:
            mark = {"done": "[green]✓[/]", "stopped": "[yellow]⏹[/]"}.get(self.outcome, "[red]✗[/]")
        else:
            mark = f"[cyan]{SPINNER[int(time.time() * 10) % len(SPINNER)]}[/]"
        action = "" if self.outcome else f" · {escape(self.action[:70])}"
        count = f"{self.tools} tool{'' if self.tools == 1 else 's'}"
        return Text.from_markup(
            f"  {mark} [bold magenta]↳ {escape(self.kind)}[/]: {escape(self.label)}{action} "
            f"[dim]· {count} · {self.seconds:.0f}s[/]"
        )


@dataclass
class Record:
    """What /agents shows about a finished run."""

    kind: str
    label: str
    task: str
    outcome: str
    tools: int
    seconds: float
    report: str
    messages: list[dict] = field(default_factory=list)


class Run:
    def __init__(self, parent: Agent, kind: AgentType, task: str, label: str, isolate: bool, pause):
        from lcode.agent import Agent

        self.parent, self.kind, self.task = parent, kind, task
        self.line = Line(kind.name, label)
        self.relay = Relay(parent.perms, f"{kind.name} agent", pause)
        self.cancel = threading.Event()
        self.report = ""
        self.error = ""
        self.worktree = Worktree(parent.cwd) if isolate else None
        cwd = self.worktree.cwd if self.worktree else parent.cwd
        settings = replace(
            parent.settings,
            model=kind.model or parent.settings.model,
            sandbox="off",  # the session's sandbox is shared below; this would start another one
            checkpoints=False,
            subagents=False,
            show_thinking=False,
        )
        child = Agent(parent.ollama, settings, cwd, console=Console(file=io.StringIO(), width=120))
        child.perms = self.relay  # type: ignore[assignment]
        child.sandbox = parent.sandbox
        child.jobs = parent.jobs  # its background commands belong to the session
        if not self.worktree:
            child.checkpoints = parent.checkpoints  # changes in the shared tree are part of the running request
        child.mcp = None
        child.lsp = parent.lsp
        child._vision = parent._vision
        child.interactive = False
        child.allowed_tools = (kind.tools or parent.tool_names()) - NOT_FOR_SUBAGENTS
        child.read_only = kind.read_only
        child.cancel = self.cancel
        child.on_tool = lambda name, args: self.line.tool(child.describe_call(name, args))
        child.messages[0]["content"] += ROLE_PROMPT.format(instructions=kind.instructions)
        child.ctx_used = len(child.messages[0]["content"]) // 3
        self.child = child

    def execute(self) -> None:
        """Run the subagent to its report. Safe to call in a thread: errors end up in self.error."""
        child = self.child
        try:
            child.max_steps = self.kind.max_steps
            child._run_turn(self.task)
            last = child.messages[-1]
            if last.get("role") != "assistant" or last.get("tool_calls") or not last.get("content", "").strip():
                # It ran out of steps: ask for what it has so far, without tools.
                child.messages.append(
                    {
                        "role": "user",
                        "content": "[lcode] You've used all your steps. Stop here and write your report: what "
                        "you found or did so far, and what's left.",
                    }
                )
                child.allowed_tools = set()
                child.assistant_step()
            self.report = child.messages[-1].get("content", "").strip() or "(the subagent wrote no report)"
            self.line.finish("done")
        except KeyboardInterrupt:
            self.line.finish("stopped")
            self.error = "Stopped by the user before it finished."
        except OllamaError as e:
            self.line.finish("failed")
            self.error = f"Failed: {e}"
        except Exception as e:  # a subagent's crash must not take the session down
            self.line.finish("failed")
            self.error = f"Failed: {type(e).__name__}: {e}"

    def stop(self) -> None:
        self.cancel.set()
        response = self.child.response
        if response is not None:
            abort(response)

    def finish(self, bring_back: bool = True) -> str:
        """In the main thread: the tool result for the main model; applies an isolated worker's changes."""
        parent, child = self.parent, self.child
        for key, value in child.usage.items():
            parent.usage[key] += value
        footer = f"[{self.kind.name} agent · {self.line.tools} tool calls · {self.line.seconds:.0f}s]"
        result = self.error or self.report
        try:
            if self.worktree and not self.error and bring_back:
                result += "\n\n" + self.bring_back()
        finally:
            if self.worktree:
                self.worktree.remove()
        parent.agent_runs.append(
            Record(
                self.kind.name,
                self.line.label,
                self.task,
                self.line.outcome,
                self.line.tools,
                self.line.seconds,
                result,
                child.messages[1:],
            )
        )
        return f"{result}\n\n{footer}"

    def bring_back(self) -> str:
        """Show the worker's changes and apply them to the working tree if the user agrees."""
        assert self.worktree is not None
        parent = self.parent
        try:
            patch = self.worktree.diff()
        except WorktreeError as e:
            return f"[Its changes couldn't be read: {e}]"
        if not patch.strip():
            return "[It changed no files.]"
        files = changed_files(patch)
        shown = patch if len(patch) <= 12_000 else patch[:12_000] + "\n... (diff truncated for display)\n"
        body = Syntax(shown, "diff", theme="monokai", word_wrap=True)
        title = f"Apply the {self.kind.name} agent's changes to {len(files)} file{'' if len(files) == 1 else 's'}"
        targets = " ".join(files)
        ok, feedback = parent.perms.request("edit", "edit", title, body, targets)
        if not ok:
            return f"[Its changes were NOT applied. {feedback}]"
        parent.checkpoint()
        try:
            self.worktree.apply(patch)
        except WorktreeError as e:
            saved = config.STATE_DIR / "agents" / f"{parent.session_id}-{uuid.uuid4().hex[:6]}.patch"
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_text(patch)
            return f"[Its changes didn't apply cleanly ({e}); the patch is saved in {saved}.]"
        for name in files:
            parent.tools.read_mtimes.pop(str((self.worktree.root / name).resolve()), None)
        parent.console.print(
            f"  [green]✓[/] Applied the {escape(self.kind.name)} agent's changes: {escape(', '.join(files))}"
        )
        return f"[Its changes were applied to the working tree: {', '.join(files)}. Read those files again before editing them.]"


def abort(response) -> None:
    """Stop a streaming Ollama response from another thread (Ollama then cancels the request)."""
    with contextlib.suppress(Exception):
        connection = getattr(response.raw, "_connection", None) or getattr(response.raw, "connection", None)
        sock = getattr(connection, "sock", None)
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(Exception):
        response.close()


# ----------------------------------------------------------------------------- running them


def start(parent: Agent, args: dict, isolate: bool, pause) -> Run:
    types = parent.agent_types()
    name = str(args.get("type", "")).strip()
    task = str(args.get("task", "")).strip()
    if name not in types:
        raise AgentDefError(f"unknown agent type {name!r}; available: {', '.join(types)}")
    if not task:
        raise AgentDefError("the task is empty: describe what the subagent should do")
    label = " ".join(str(args.get("description") or "").split()) or " ".join(task.split())[:50]
    kind = types[name]
    return Run(parent, kind, task, label, isolate or kind.isolation == "worktree", pause)


def run_one(parent: Agent, args: dict) -> str:
    """Run one subagent in the main thread, with a progress line."""
    holder: dict = {}

    @contextlib.contextmanager
    def pause() -> Iterator[None]:
        live = holder.get("live")
        if live:
            live.stop()
        try:
            yield
        finally:
            if live:
                live.start()

    try:
        run = start(parent, args, False, pause)
    except (AgentDefError, WorktreeError) as e:
        return f"Error: {e}"
    live = Live(run.line, console=parent.console, refresh_per_second=8, transient=True)
    holder["live"] = live
    live.start()
    try:
        run.execute()
    finally:
        live.stop()
    parent.console.print(run.line)
    if run.error and run.line.outcome == "stopped":
        run.finish()
        raise KeyboardInterrupt
    return run.finish()


def run_many(parent: Agent, calls: list[tuple[int, dict]]) -> dict[int, str]:
    """Run several subagents at once (up to max_parallel_agents); returns each call's result by its index."""
    results: dict[int, str] = {}
    types = parent.agent_types()
    editors = [i for i, args in calls if (t := types.get(str(args.get("type", "")))) and t.edits]
    cancel = threading.Event()
    broker = Broker(parent.perms, cancel)
    lines: list[Line] = []
    live = Live(Group(*lines), console=parent.console, refresh_per_second=8, transient=True)

    @contextlib.contextmanager
    def pause() -> Iterator[None]:
        live.stop()
        try:
            yield
        finally:
            live.start()

    runs: list[tuple[int, Run]] = []
    interrupted = False
    try:
        for index, args in calls:
            try:
                run = start(parent, args, len(editors) > 1 and index in editors, pause)
            except WorktreeError as e:
                # Without a worktree, editing agents mustn't run at the same time: run this one on its own later.
                results[index] = ""
                parent.console.print(f"[yellow]Running an agent on its own: {escape(str(e))}[/]")
                continue
            except AgentDefError as e:
                results[index] = f"Error: {e}"
                continue
            run.relay.broker = broker
            runs.append((index, run))
            lines.append(run.line)
        live.update(Group(*lines))
        slots = threading.Semaphore(max(1, parent.settings.max_parallel_agents))

        def work(run: Run) -> None:
            with slots:
                if cancel.is_set():
                    run.line.finish("stopped")
                    run.error = "Stopped by the user before it started."
                    return
                run.execute()

        threads = [threading.Thread(target=work, args=(run,), daemon=True) for _, run in runs]
        live.start()
        try:
            for thread in threads:
                thread.start()
            while any(thread.is_alive() for thread in threads):
                broker.serve(pause)
                time.sleep(0.05)
        except KeyboardInterrupt:
            interrupted = True
            cancel.set()
            for _, run in runs:
                run.stop()
            for thread in threads:
                thread.join(timeout=5)
            raise
        finally:
            live.stop()
            for line in lines:
                parent.console.print(line)
    finally:
        finished = []
        for index, run in runs:
            if run.line.outcome == "":
                run.line.finish("stopped")
                run.error = run.error or "Stopped by the user before it finished."
            finished.append((index, run))
        for index, run in finished:
            results[index] = run.finish(bring_back=not interrupted)
    for index, args in calls:
        if results.get(index) == "":
            results[index] = run_one(parent, args)
    return results
