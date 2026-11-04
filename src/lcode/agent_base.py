"""An agent's state: its settings, conversation, tools and the parts of lcode it uses, set up when it's made."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rich.console import Console
from rich.text import Text

from lcode import (
    checks,
    codesearch,
    extensions,
    repoid,
    repomap,
    residency,
    subagents,
    traces,
)
from lcode import memory as memory_notes
from lcode.checkpoints import Checkpoints
from lcode.jobs import Jobs
from lcode.mcp import McpManager
from lcode.ollama import Ollama
from lcode.permissions import Permissions
from lcode.repeats import Repeats
from lcode.sandbox import Sandbox
from lcode.steer import Steering
from lcode.tools import Toolbox
from lcode.unreachable import NOTE_AT, Sites

if TYPE_CHECKING:
    from lcode.agent import Agent
    from lcode.agent_model import Progress
    from lcode.hardware import Hardware
    from lcode.lsp import Manager as LspManager
    from lcode.replay import ReplayServer


MAX_STEPS_PER_TURN = 150


@dataclass
class Settings:
    model: str  # Ollama model name to call
    context: int
    mode: str = "code"  # what lcode makes: code, image or video (lcode.modes)
    comfyui_url: str = "http://127.0.0.1:8188"
    comfyui_path: str | None = None  # a ComfyUI install lcode doesn't find by itself (lcode.comfy)
    media_dir: str = ".lcode/media"  # where image and video mode save their files (lcode.media)
    image_model: str = "flux2-klein-4b"  # the media catalog's model image mode uses (lcode.media_catalog)
    video_model: str = "wan22-ti2v-5b"  # and video mode's
    prompt_assistant: str = "video"  # video, all, off or an Ollama model: writes fuller prompts (lcode.assistant)
    check_results: bool = False  # the prompt assistant checks each image against the request
    preview: str = "auto"  # images in the terminal: auto, kitty, iterm, blocks or off (lcode.preview)
    num_batch: int | None = None
    keep_alive: str = "30m"
    think: bool = True
    show_thinking: bool = False
    permission_mode: str = "ask"
    web: str = "on"  # on | ask | off
    search_backend: str = "auto"
    searxng_url: str | None = None
    checkpoints: bool = True  # snapshot files before the model changes them, for /undo
    sandbox: str = "off"  # off, docker or podman: where the model's shell commands run
    sandbox_image: str | None = None
    sandbox_network: bool = False
    vision_model: str = "auto"  # auto, off or an Ollama model that can see images
    memory: str = "off"  # off, ask or auto: notes that carry over to later sessions (lcode.memory)
    memory_check: bool = False  # ask the model what's worth remembering when a conversation ends
    subagents: bool = False  # the agent tool (lcode.subagents)
    trust_project: bool = False  # use the repository's own commands, skills and agents (lcode.extensions)
    skills: str = "off"  # all, lcode (only lcode's own skill folders) or off
    prune: bool = True  # remove old tool output before summarizing (lcode.context)
    lsp: str = "off"  # auto: use the installed language servers (lcode.lsp)
    repo_map: bool = False  # the repository map (lcode.repomap)
    embed_model: str = "off"  # semantic code search (lcode.codesearch): auto, off or a model
    notify: bool = False  # desktop notifications after long requests (lcode.notify)
    notify_after: int = 30  # seconds: a request this long notifies when it's done or waits for an answer
    max_parallel_agents: int = 1
    secret_check: bool = True  # scan what git commit and git push would add (lcode.secretscan)
    compact_at: int = 0  # tokens: make room at this size even when the window is larger (0: only at 85%)
    repeat_limit: int = 3  # identical calls with identical results before the model is told (lcode.repeats)
    checks: str = "off"  # the project's checks after a change (lcode.checks): auto, off or a command
    review: str = "off"  # check a change against the request before the answer (lcode.review): auto or off
    escalate_model: str | None = None  # a stronger model to ask when stuck (lcode.escalate): provider/model
    escalate_url: str | None = None  # its server, when not the provider's usual one
    escalate_price: str | None = None  # dollars per million tokens in/out, e.g. "3/15", for the cost estimate
    trace: bool = False  # record each request on this machine (lcode.traces)


class AgentBase:
    """What Agent and its groups of methods share: the state __init__ sets up."""

    if TYPE_CHECKING:  # the methods the groups call on each other, for the type checker only: Agent has them all

        @staticmethod
        def new_session_id() -> str: ...
        def reset(self) -> None: ...
        def system_prompt(self) -> str: ...
        def tool_schemas(self) -> list[dict]: ...
        def planning(self) -> bool: ...
        def checks_prompt(self) -> str: ...
        def vision_model(self) -> str | None: ...
        def chat(self, messages: list[dict], tools: list | None, think: bool) -> Generator[dict, None, None]: ...
        def next_request_tokens(self) -> int: ...
        def can_escalate(self) -> bool: ...
        def before_request(self, progress: Progress) -> residency.LoadWatch | None: ...
        def after_load(self, watch: residency.LoadWatch) -> None: ...

    def __init__(self, ollama: Ollama, settings: Settings, cwd: Path, console: Console | None = None):
        self.console = console or Console(highlight=False)
        self.ollama = ollama
        self.settings = settings
        self.cwd = cwd.resolve()
        # The folder lcode works in: where it started, or where /cd or a resumed session took it. A shell
        # command may move the working directory around its project (self.cwd), but not out of it.
        self.workspace = self.cwd
        self.perms = Permissions(self.console, settings.permission_mode)
        self.tools = Toolbox(cast("Agent", self))
        self.session_id = self.new_session_id()
        self.checkpoints = Checkpoints(self.console, settings.checkpoints)
        self.mcp: McpManager | None = None  # set by the CLI when MCP servers are configured
        self.lsp: LspManager | None = None  # set by the CLI when language servers are installed
        from lcode.hooks import HookSet

        self.hooks = HookSet()  # set by the CLI from the settings (lcode.hooks)
        self._repo_map: tuple[Path, repomap.RepoMap, str] | None = None
        self._code_index: tuple[Path, codesearch.Index | None] | None = None
        self.sandbox = (
            Sandbox(settings.sandbox, settings.sandbox_image, settings.sandbox_network, self.console)
            if settings.sandbox != "off"
            else None
        )
        self._mcp_prompt = ""  # the MCP part at the end of the system prompt
        self._vision: str | bool | None = False  # the model that looks at images; False = not decided yet
        self.session_name = ""
        self.session_title = ""
        self.interactive = True  # False for `lcode -p`: no end-of-session questions
        self.comfy_process: subprocess.Popen | None = None  # the ComfyUI lcode started, stopped when it exits
        self.no_coding_model = ""  # why there's no coding model, when lcode started in image or video mode without
        from lcode.imageopts import Options as ImageOptions

        self.image_options = ImageOptions()  # image mode's defaults for this session (/options)
        self.media_results: list[dict] = []  # the session's images, numbered (/gallery, /vary, /open)
        self.last_request: dict | None = None  # what /again repeats (lcode.media.redo)
        self.image_refs: list[str] = []  # images every next request follows (/use)
        self.cloud_spent = 0.0  # dollars, about, spent at cloud image and video models this session (media_cloud)
        self.reflected = 0  # messages before this index were already checked for notes to remember
        self._memory: memory_notes.Memory | None = None
        self._agent_types: tuple[Path, dict[str, subagents.AgentType], list[str]] | None = None
        self.agent_runs: list[subagents.Record] = []  # subagents run in this session, for /agents
        self.plan = ""  # the plan the user approved; kept through compaction
        self.pruned = self.compacted = 0  # how often the context was pruned or summarized automatically
        self.summarized_size = 0  # tokens right after the last summary (0: none in this conversation)
        self._extensions: tuple[Path, extensions.Extensions] | None = None
        self.skills_loaded: set[str] = set()  # skills whose instructions are in the conversation
        # Set on subagents (see lcode.subagents):
        self.allowed_tools: set[str] | None = None  # None: every tool
        self.read_only = False  # only read-only shell commands
        self.no_changes = ""  # set during a review: why nothing may change (read-only tools only)
        self.cancel: threading.Event | None = None  # set from another thread to stop
        self.steering: Steering | None = None  # notes typed and pauses asked for during a request (lcode.repl)
        self.turn_requests: list[str] = []  # the request, then the notes added to it while it ran
        self.unsent = ""  # typed during the last request but never sent to the model: back in the prompt (lcode.repl)
        self.on_tool: Callable[[str, dict], None] | None = None  # called before each tool runs
        self.jobs = Jobs()  # background commands (shared with subagents)
        self.turn_started = 0.0  # when the running request started (time.monotonic)
        self.on_event: Callable[[dict], None] | None = None  # each step and tool result, for --output stream-json
        self.on_delta: Callable[[str, str], None] | None = None  # ("text" or "thinking", piece) as it streams
        self.current_call = ""  # id of the tool call that's running, so a permission request can name it
        # Decides on a presented plan instead of asking in the terminal: "ask", "auto-edit", or why not.
        self.plan_review: Callable[[str, str], str] | None = None
        self.max_steps = MAX_STEPS_PER_TURN
        self.hardware: Hardware | None = None  # set by the CLI: for what fits on the GPU
        self.made_room_for = ""  # why the model was unloaded (the vision model, an MCP tool), for the reload line
        self.load_state = ""  # "loading" while the model loads in the background at startup (lcode.repl)
        self.autosave = False  # save the session after every step (sessions people resume; not the bench's)
        self._check: tuple[Path, checks.Check | None] | None = None
        self.check_results: list[dict] = []  # the checks of the last request: command, exit code, who ran them
        self.review_results: list[str] | None = None  # what the last request's review found (None: no review)
        self.escalations: list[dict] = []  # the stronger model asked in the last request (lcode.escalate)
        self.escalate_approved: bool | None = None  # decided in advance (lcode -p --escalate, the benchmark)
        self.sent_chars = 0  # characters of the conversation the model had already read
        self.counted_chars: int | None = None  # characters that ctx_used covers (None: all of them)
        self.turn_status = ""  # how the last request ended: success, max_steps, loop, blocked
        self.repeats = Repeats(settings.repeat_limit)  # notices when the model repeats itself
        self.sites = Sites(NOTE_AT if settings.repeat_limit else 0)  # notices a site this machine can't reach
        self.reply_trouble = ""  # what went wrong with the last reply: "circles", "cut_off" or ""
        self.response = None  # the streaming response, so another thread can abort it
        self.ctx_used = 0
        self._repo: tuple[Path, repoid.Repo | None] | None = None  # the workspace's repository, for saved sessions
        self.last_speed = 0.0
        self.reply_speeds = residency.SpeedWatch()  # to say why, when replies get far slower (lcode.residency)
        self.usage = {"requests": 0, "prompt_tokens": 0, "prompt_ns": 0, "output_tokens": 0, "output_ns": 0}
        self.trace: traces.Lane | None = None  # the request's trace while it's recorded (lcode.traces)
        self.replay: ReplayServer | None = None  # in a replay: the recorded results of tools and checks (lcode.replay)
        self.messages: list[dict] = []
        self.reset()

    def say(self, text: str, warning: bool = False) -> None:
        """One line about what happens under the hood (loading, memory), not about the work itself."""
        self.console.print(Text(f"{'⚠' if warning else '◆'} {text}", style="yellow" if warning else "dim cyan"))

    def emit(self, event: dict) -> None:
        if self.on_event is not None:
            self.on_event(event)
