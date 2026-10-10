"""Run lcode from a program, and the structured results behind `lcode -p --output json`.

    from lcode import Session

    with Session("path/to/repo", permission_mode="auto-edit") as session:
        result = session.run("Fix the failing test in tests/test_parser.py")
        print(result.status, result.text)
        for change in result.files_changed:
            print(change["status"], change["path"])

A session is one conversation: each `run` continues it. `stream` yields the same events as
`--output stream-json` while the request runs. Nothing asks a person: permission requests go to
`approve` (by default they're refused), so pick a permission mode that fits the job.
"""

from __future__ import annotations

import io
import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console

from lcode import __version__, backends, config
from lcode.config import ConfigError, parse_context
from lcode.ollama import MIN_VERSION, OllamaError, version_tuple

if TYPE_CHECKING:
    from lcode.agent import Agent
    from lcode.hardware import Hardware

SCHEMA_VERSION = 1
STATUSES = ("success", "error", "max_steps", "loop", "interrupted")
EXIT_CODES = {"success": 0, "error": 1, "max_steps": 3, "loop": 4, "interrupted": 130}
OLLAMA_INSTALL = {
    "linux": "curl -fsSL https://ollama.com/install.sh | sh",
    "darwin": "brew install ollama   (or download it from https://ollama.com/download)",
}


class SetupError(Exception):
    """lcode can't start: no model server, a missing model, a sandbox that won't start…"""


@dataclass
class Options:
    """How to run. None means: what the user's config says."""

    model: str | None = None
    context: int | str | None = None
    permission_mode: str | None = None  # ask, plan, auto-edit or yolo
    allowed_tools: list[str] | None = None  # only these tools (default: all)
    max_steps: int | None = None  # model steps per request
    think: bool | None = None
    web: bool | None = None
    memory: bool | None = None
    mcp: bool = False
    sandbox: bool = False
    show_thinking: bool = False
    interactive: bool = False  # True only for lcode's own terminal session


@dataclass
class Result:
    """How a request went. `to_json()` is what `lcode -p --output json` prints."""

    status: str  # success, error, max_steps, loop (stopped: the model kept repeating itself) or interrupted
    text: str  # the model's final answer
    session_id: str
    model: str
    tool_calls: list[dict] = field(default_factory=list)  # {"id", "name", "arguments", "error", "output"}
    files_changed: list[dict] = field(default_factory=list)  # {"status": "A" | "M" | "D", "path"}
    usage: dict = field(default_factory=dict)  # requests, prompt_tokens, output_tokens
    seconds: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.status, 1)

    def to_json(self) -> dict:
        return {"type": "result", "schema": SCHEMA_VERSION, "lcode": __version__, **asdict(self)}


# ----------------------------------------------------------------------------- setting up


def check_server(ollama, hw: Hardware | None = None) -> str:
    """The server's version (or name); SetupError when it can't be used."""
    if backends.is_openai(ollama):
        try:
            return ollama.version()
        except OllamaError as e:
            raise SetupError(
                f"{e}\n\nIs {ollama.name} running, with its server started? lcode expects its OpenAI-compatible "
                "API there; change the address with: lcode config set base_url http://host:port/v1"
            ) from e
    try:
        version = ollama.version()
    except OllamaError as e:
        from lcode.hardware import detect

        hint = OLLAMA_INSTALL.get((hw or detect()).os, OLLAMA_INSTALL["linux"])
        raise SetupError(f"{e}\n\nIs Ollama installed and running? Install it with:\n  {hint}") from e
    if version_tuple(version) < MIN_VERSION:
        needed = ".".join(map(str, MIN_VERSION))
        raise SetupError(f"Ollama {version} is too old; lcode needs {needed} or newer. Update Ollama.")
    return version


def switch(wanted: bool | None, configured: str, default_on: str) -> str:
    """A setting that's "off" or some mode: None keeps the config's, False turns it off, True on."""
    if wanted is None:
        return configured
    if not wanted:
        return "off"
    return configured if configured != "off" else default_on


def open_agent(cwd: Path, options: Options, console: Console, hw: Hardware | None = None) -> Agent:
    """An agent set up the way `lcode` sets up a session: model, context, hooks, language servers, MCP."""
    from lcode import catalog, cli, extensions, hooks
    from lcode.agent import Agent, Settings
    from lcode.hardware import detect

    try:
        cfg = config.load()
        requested = parse_context(options.context) if options.context else cfg["context"]
        if options.model and not options.context:
            same = (catalog.find(options.model) or options.model) == (catalog.find(cfg["model"]) or cfg["model"])
            if not same:
                requested = None  # the saved context was sized for the saved model; fit this one instead
    except (ConfigError, ValueError) as e:
        raise SetupError(str(e)) from e
    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise SetupError(f"not a directory: {cwd}")
    try:
        ollama = cli.Ollama(cfg["ollama_host"]) if cfg["backend"] == "ollama" else backends.connect(cfg)
    except OllamaError as e:
        raise SetupError(str(e)) from e
    hw = hw or detect()
    check_server(ollama, hw)
    try:
        model, spec = cli.resolve_model(ollama, options.model or cfg["model"])
    except cli.NotInstalled as e:
        first_run = not options.model and cfg["model"] == config.DEFAULT_MODEL and not config.CONFIG_PATH.exists()
        if first_run and not backends.is_openai(ollama):
            raise SetupError("lcode isn't set up yet. Run:  lcode setup") from e
        raise SetupError(str(e)) from e
    context, note = cli.choose_context(ollama, model, spec, requested, hw)
    if note:
        console.print(f"[yellow]Context {config.format_tokens(context)}: {note}[/]")
    num_batch = cfg["num_batch"]
    if num_batch is None and spec and spec.num_batch:
        if model == spec.local_name:
            num_batch = spec.num_batch
        else:  # the tuned batch size assumes the text-only variant; the vision projector needs that VRAM
            console.print(f"[dim]Tip: run `lcode setup {spec.key}` once to create the faster text-only variant.[/]")
    trust_project = extensions.trust_project(cwd, console, interactive=options.interactive)
    sandbox = (cfg["sandbox"] if cfg["sandbox"] != "off" else "docker") if options.sandbox else cfg["sandbox"]
    settings = Settings(
        model=model,
        context=context,
        num_batch=num_batch,
        keep_alive=cfg["keep_alive"],
        think=cfg["think"] if options.think is None else options.think,
        show_thinking=options.show_thinking,
        permission_mode=options.permission_mode or cfg["permission_mode"],
        web=switch(options.web, cfg["web"], "on"),
        search_backend=cfg["search_backend"],
        searxng_url=cfg["searxng_url"],
        checkpoints=cfg["checkpoints"],
        sandbox=sandbox,
        sandbox_image=cfg["sandbox_image"],
        sandbox_network=cfg["sandbox_network"],
        vision_model=cfg["vision_model"],
        memory=switch(options.memory, cfg["memory"], "ask"),
        subagents=cfg["subagents"],
        max_parallel_agents=cfg["max_parallel_agents"],
        trust_project=trust_project,
        skills=cfg["skills"],
        prune=cfg["prune"],
        repo_map=cfg["repo_map"],
        embed_model=cfg["embed_model"],
        notify=cfg["notify"],
        notify_after=cfg["notify_after"],
        repeat_limit=cfg["repeat_limit"],
    )
    agent = Agent(ollama, settings, cwd, console=console)
    agent.interactive = options.interactive
    if options.max_steps:
        agent.max_steps = options.max_steps
    if options.allowed_tools is not None:
        known = agent.tool_names()
        unknown = sorted(set(options.allowed_tools) - known)
        if unknown:
            raise SetupError(f"unknown tool(s) {', '.join(unknown)}; the tools are: {', '.join(sorted(known))}")
        agent.allowed_tools = set(options.allowed_tools)
    agent.hooks, agent.perms.rules = hooks.load(cwd, trust_project)
    for problem in agent.hooks.problems:
        console.print(f"[yellow]Settings: {problem}[/]")
    agent.perms.on_prompt = agent.waiting  # notification hooks and desktop notifications
    if cfg["lsp"] == "auto":
        from lcode import lsp
        from lcode.checkpoints import work_tree_for

        if lsp.available():
            agent.lsp = lsp.Manager(work_tree_for(cwd))
            agent.messages[0]["content"] = agent.system_prompt()  # now it mentions the language servers
    if agent.sandbox:
        # Never run commands unsandboxed when the user asked for a sandbox: stop here instead.
        from lcode.sandbox import SandboxError

        try:
            with console.status("Starting the sandbox…"):
                agent.sandbox.ensure(cwd)
        except SandboxError as e:
            close_agent(agent)
            raise SetupError(f"the sandbox can't start: {e}\nTurn it off with: lcode config set sandbox off") from e
    if options.mcp:
        from lcode.mcp.commands import start_session

        agent.mcp = start_session(cwd, cfg["mcp_tools"], console, interactive=options.interactive)
    return agent


def close_agent(agent: Agent) -> None:
    stopped = agent.jobs.stop_all()
    if stopped:
        agent.console.print(f"[dim]Stopped {stopped} background job(s).[/]")
    if agent.lsp is not None:
        agent.lsp.close()
    if agent.mcp:
        agent.mcp.close()
    if agent.sandbox:
        agent.sandbox.stop()


# ----------------------------------------------------------------------------- running


def run_request(agent: Agent, prompt: str) -> Result:
    """Run one request to its end and say how it went; errors end up in the result, not raised."""
    calls: dict[str, dict] = {}
    previous = agent.on_event

    def collect(event: dict) -> None:
        if event["type"] == "assistant":
            for call in event["tool_calls"]:
                calls[call["id"]] = {**call, "error": False, "output": ""}
        elif event["type"] == "tool_result" and event["id"] in calls:
            calls[event["id"]].update(error=event["error"], output=event["output"])
        if previous is not None:
            previous(event)

    usage_before = dict(agent.usage)
    checkpoints_before = len(agent.checkpoints.items)
    start = len(agent.messages)
    status, error = "success", ""
    started = time.monotonic()
    agent.on_event = collect
    from lcode.ollama import OllamaError as ModelError

    try:
        agent.run_turn(prompt)
        status = agent.turn_status if agent.turn_status in ("max_steps", "loop") else "success"
    except KeyboardInterrupt:
        status, error = "interrupted", "interrupted before the request was done"
    except ModelError as e:
        status, error = "error", str(e)
    finally:
        agent.on_event = previous
        agent.save()
    text = next((m.get("content") or "" for m in reversed(agent.messages[start:]) if m.get("role") == "assistant"), "")
    if len(agent.checkpoints.items) > checkpoints_before:
        files = list(agent.checkpoints.items[-1].changes)
    else:  # checkpoints are off: the files the model wrote or edited
        paths = dict.fromkeys(
            c["arguments"].get("path", "") for c in calls.values()
            if c["name"] in ("write_file", "edit_file") and not c["error"]
        )  # fmt: skip
        files = [{"status": "M", "path": p} for p in paths if p]
    usage = {k: agent.usage[k] - usage_before.get(k, 0) for k in ("requests", "prompt_tokens", "output_tokens")}
    return Result(
        status=status,
        text=text,
        session_id=agent.session_id,
        model=agent.settings.model,
        tool_calls=list(calls.values()),
        files_changed=files,
        usage=usage,
        seconds=round(time.monotonic() - started, 2),
        error=error,
    )


def start_event(agent: Agent) -> dict:
    return {
        "type": "start",
        "schema": SCHEMA_VERSION,
        "lcode": __version__,
        "session_id": agent.session_id,
        "model": agent.settings.model,
        "cwd": str(agent.cwd),
    }


# ----------------------------------------------------------------------------- the Python API


class Session:
    """A conversation with lcode in a repository, for programs. See the module docstring."""

    def __init__(
        self,
        repo: str | Path = ".",
        *,
        approve: Callable[[dict], bool] | None = None,
        on_event: Callable[[dict], None] | None = None,
        verbose: bool = False,
        **options: Any,
    ):
        """`options` are the fields of `Options`: model, context, permission_mode, allowed_tools,
        max_steps, think, web, memory, mcp, sandbox. `approve(request)` decides permission requests
        ({"kind", "title", "target"}); without it they're refused. `verbose` shows lcode's usual
        output on stderr."""
        self.console = Console(stderr=True) if verbose else Console(file=io.StringIO(), width=120)
        self.agent = open_agent(Path(repo), Options(**options), self.console)
        self.agent.perms.approve = approve or (lambda request: False)
        self.agent.on_event = on_event

    @property
    def session_id(self) -> str:
        return self.agent.session_id

    @property
    def messages(self) -> list[dict]:
        return self.agent.messages

    def run(self, prompt: str) -> Result:
        return run_request(self.agent, prompt)

    def stream(self, prompt: str) -> Iterator[dict]:
        """The request's events as they happen, ending with {"type": "result", …}."""
        events: queue.Queue = queue.Queue()
        previous = self.agent.on_event

        def forward(event: dict) -> None:
            events.put(event)
            if previous is not None:
                previous(event)

        def work() -> None:
            self.agent.on_event = forward
            try:
                events.put(run_request(self.agent, prompt).to_json())
            except Exception as e:  # never leave the reader waiting
                events.put({"type": "result", "status": "error", "error": f"{type(e).__name__}: {e}"})
            finally:
                self.agent.on_event = previous

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        while True:
            event = events.get()
            yield event
            if event["type"] == "result":
                break
        thread.join()

    def close(self) -> None:
        close_agent(self.agent)

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
