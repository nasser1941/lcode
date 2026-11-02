"""The agent loop: stream a model turn, run the tools it calls, repeat until it answers."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape
from rich.text import Text

from lcode import (
    checks,
    escalate,
    notify,
    planning,
    residency,
    review,
    sessions,
    subagents,
    traces,
    vision,
)
from lcode import context as context_tools
from lcode.agent_base import Settings as Settings  # where lcode and its tests have always imported it from
from lcode.agent_checks import Checks
from lcode.agent_context import ContextWindow, conversation_chars
from lcode.agent_model import ModelCalls, Progress
from lcode.agent_prompt import PromptParts
from lcode.agent_sessions import Sessions
from lcode.cloud import is_cloud
from lcode.config import format_tokens
from lcode.ollama import OllamaError
from lcode.permissions import LEADING_CD
from lcode.sandbox import SandboxError, project_root
from lcode.steer import NOTE_INTRO
from lcode.tool_helpers import ToolError, is_binary, tree

if TYPE_CHECKING:
    pass

MAX_STALLS = 2  # replies that go round in circles or say nothing, per request, before lcode stops it
STALLED = {
    "circles": "the reply went round in circles, so lcode stopped it",
    "cut_off": "the reply filled the context before it said anything",
    "": "the reply was empty",
}


def stall_note(trouble: str) -> str:
    """What the model is told after a reply that went round in circles or ended without a word."""
    first = {
        "circles": "Your last reply went round in circles (the same thoughts over and over), so lcode stopped it.",
        "cut_off": "Your last reply filled the rest of the context before you said anything.",
        "": "Your last reply was empty.",
    }[trouble]
    return (
        f"[lcode] {first} Don't deliberate at length now. Either make your next tool call, or, if you're stuck, "
        "tell the user in a few lines what you did, what fails, and what they could do. If you were about to check "
        "something by running it, run it now instead of reasoning about it: for a window, take a screenshot (with "
        "actions for clicks or keys)."
    )


EVENT_OUTPUT = 4000  # characters of a tool's output in an event
# How a tool result starts when the call failed or wasn't allowed (an "error" in events and results).
REFUSED = ("Error:", "A deny rule", "The user denied", "This needs the user's permission")
MAX_MALFORMED_CALL_RETRIES = 2  # Ollama rejects tool calls whose arguments aren't valid JSON
# How Ollama reports a tool call it couldn't parse (Qwen3.6's calls are XML)
MALFORMED_CALL = (
    "error parsing tool call",
    # Go's XML decoder, for models that write calls as XML (Qwen3.6): a cut-off or misordered call
    "XML syntax error",
    "expected element type",
    "closed by </",
)
READING_NOTE_TOKENS = 4000  # new input this large gets "Reading … new tokens" in the status line
# @path, @"path with spaces" or @'path'
MENTION = re.compile(r"""(?<!\S)@(?:"([^"\n]+)"|'([^'\n]+)'|([\w./~\-]+))""")
# An image path written in a message: in quotes, or without spaces (a space escaped as "\ " is fine), or as a
# file:// address, the way terminals paste a dragged file.
IMAGE_PATH = re.compile(
    r"""(['"])((?:file://)?[^'"\n]+?\.(?:png|jpe?g|webp|gif))\1"""
    r"""|(?<![^\s(])((?:[^\s\\'"(]|\\ )+?\.(?:png|jpe?g|webp|gif))(?=[\s,;:!?)\]]|\.(?:\s|$)|$)""",
    re.IGNORECASE,
)


INIT_PROMPT = """Analyze this repository and create (or improve, if it exists) an AGENTS.md file at its root that will be given to you in future sessions. Explore the codebase first (layout, README, config/build files, entry points, main modules, tests). AGENTS.md should contain:
1. A short overview of what the project does.
2. How to set up, build, run, and test it (exact commands).
3. The architecture: the important directories/modules and how they fit together, with key file paths.
4. Code conventions and any gotchas you noticed.
Keep it concise (under ~150 lines) and factual — only include what you verified in the code."""


def call_arguments(call: dict) -> dict:
    """A tool call's arguments as a dict (some models send them as a JSON string)."""
    args = call.get("function", {}).get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    return args if isinstance(args, dict) else {}


class Agent(PromptParts, Sessions, Checks, ContextWindow, ModelCalls):
    _assistant_model: str  # image and video mode's prompt assistant, "" for none: set when first needed (lcode.media)

    def prepare_mcp(self) -> None:
        """Before a request: wait for MCP servers still starting and describe them in the system prompt."""
        if not self.mcp:
            return
        if self.mcp.pending:
            with self.console.status(f"Starting MCP servers: {', '.join(self.mcp.pending)}", spinner="dots"):
                self.mcp.wait()
            for state in self.mcp.servers.values():
                if state.status in ("failed", "login") and not state.reported:
                    state.reported = True
                    self.console.print(f"[yellow]MCP server {state.name}: {escape(state.error)}[/] [dim](/mcp)[/]")
        self.mcp.refresh_changed()
        section = self.mcp.prompt_section(self.settings.context)
        if section != self._mcp_prompt:
            content = self.messages[0]["content"]
            if self._mcp_prompt and content.endswith(self._mcp_prompt):
                content = content[: -len(self._mcp_prompt)]
            self.messages[0]["content"] = content + section
            self._mcp_prompt = section

    def before_request(self, progress: Progress) -> residency.LoadWatch | None:
        """Set the status line for what comes before the first token: loading the model if it isn't in memory
        (watched until it is), then reading the prompt. Returns the watch while the model loads."""
        if getattr(self.ollama, "kind", "ollama") != "ollama" or is_cloud(self.settings.model):
            return None  # another server loads models itself; an Ollama cloud model isn't loaded here at all
        model, context = self.settings.model, self.settings.context
        fresh = max(0, conversation_chars(self.messages) - self.sent_chars) // 3
        where = residency.placement(self.ollama, model)
        if where is not None and (not where.context or where.context == context):
            if fresh >= READING_NOTE_TOKENS:
                progress.set_phase(f"Reading {format_tokens(fresh)} new tokens")
            return None
        total = conversation_chars(self.messages) // 3
        if where is not None:
            phase = f"Reloading {model} with a {format_tokens(context)} context"
        elif self.made_room_for:
            phase = f"Reloading {model} into memory (it made room for {self.made_room_for})"
        else:
            phase = f"Loading {model} into memory"
        progress.set_phase(phase)
        watch = residency.LoadWatch(
            self.ollama, model, context, progress, f"Reading the conversation ({format_tokens(total)} tokens)"
        )
        watch.reload = where is not None or bool(self.made_room_for)
        return watch

    def describe_call(self, name: str, args: dict) -> str:
        if name in ("read_file", "write_file", "edit_file"):
            path = self.tools.rel(self.tools.resolve(args.get("path", "")))
            offset = args.get("offset")
            return f"{name}({path})" + (
                f" from line {offset}" if name == "read_file" and offset not in (None, 1) else ""
            )
        if name == "grep":
            where = args.get("path", ".") + (f" {args['glob']}" if args.get("glob") else "")
            return f"grep({args.get('pattern', '')!r} in {where})"
        if name == "glob":
            return f"glob({args.get('pattern', '')})"
        if name == "list_dir":
            return f"list_dir({args.get('path', '.')})"
        if name == "memory":
            return f"memory({' '.join(str(args.get(k) or '') for k in ('action', 'name')).strip()})"
        if name == "skill":
            return f"skill({args.get('name', '')})"
        if name == "lsp":
            where = f"{args.get('path', '')}:{args.get('line', '')}" if args.get("path") else args.get("query", "")
            return f"lsp({args.get('action', '')} {args.get('symbol', '')} {where})".replace("  ", " ")
        if name == "agent":
            return f"agent({args.get('type', '')}: {args.get('description') or str(args.get('task', ''))[:50]})"
        if name == "bash":
            return f"$ {str(args.get('command', ''))[:60]}"
        if name == "screenshot":
            what = args.get("url") or LEADING_CD.sub("", str(args.get("command") or ""))
            return f"screenshot({what[:60]})"
        if name in ("web_search", "web_fetch", "view_image"):
            return f"{name}({args.get('query') or args.get('url') or args.get('path') or ''})"
        if self.mcp and self.mcp.owns(name):
            if name == "mcp_find_tools":
                return f"mcp_find_tools({args.get('query', '')!r})"
            try:
                state, tool, arguments = self.mcp.resolve(name, args)
            except Exception:
                return name
            preview = json.dumps(arguments, ensure_ascii=False)
            return f"{state.name} › {tool['name']}({preview if len(preview) <= 80 else preview[:79] + '…'})"
        return name

    def run_turn(self, user_text: str) -> None:
        self.turn_started = time.monotonic()
        self.checkpoints.begin_turn(self.session_id, user_text, len(self.messages))
        lane = traces.start(self, user_text)
        status, error = "error", ""
        try:
            self._run_turn(user_text)
            status = self.turn_status
        except KeyboardInterrupt:
            status = "interrupted"
            raise
        except Exception as e:
            error = str(e) if isinstance(e, OllamaError) else f"{type(e).__name__}: {e}"
            raise
        finally:
            if lane is not None:
                traces.finish(self, lane, status, error)
            self.checkpoints.end_turn()
            if self.hooks.hooks and self.allowed_tools is None:
                payload = {"request": user_text[:2000], "cwd": str(self.cwd), "session": self.session_id}
                for outcome in self.hooks.run("after_request", payload, self.cwd):
                    style = "dim" if outcome.code == 0 else "yellow"
                    if outcome.output or outcome.code:
                        self.console.print(
                            Text(f"  ⎿ after_request hook ({outcome.code}): {outcome.output[:500]}", style=style)
                        )

    def waiting(self, title: str) -> None:
        """lcode waits for the user's answer: tell them, if they've likely walked away (lcode.notify)."""
        if self.hooks.for_event("notification"):
            self.hooks.notify(f"lcode needs you: {title}", self.cwd)
        if (
            self.settings.notify
            and self.interactive
            and time.monotonic() - self.turn_started >= self.settings.notify_after
        ):
            notify.desktop("lcode needs you", title)

    def finished(self, request: str, seconds: float) -> None:
        """A request is done: after a long one, tell the user."""
        if seconds < self.settings.notify_after:
            return
        title = sessions.title_from([{"role": "user", "content": request}])
        if self.hooks.for_event("notification"):
            self.hooks.notify(f"lcode finished: {title}", self.cwd)
        if self.settings.notify and self.interactive:
            notify.desktop("lcode is done", f"{title} ({seconds:.0f}s)")

    def sandbox_root(self) -> Path | None:
        """The folder the model is limited to while the sandbox is on (None when it's off)."""
        if self.replay is not None:
            return self.replay.root  # a replay changes nothing outside its copy of the folder (lcode.traces)
        if not self.sandbox:
            return None
        try:
            return project_root(self.cwd)
        except SandboxError:
            return self.cwd

    def checkpoint(self) -> None:
        """Called before the model changes files: snapshot them once per request, for /undo."""
        self.checkpoints.before_change(self.cwd)

    def _run_turn(self, user_text: str) -> None:
        self.prepare_mcp()
        self.turn_status = "running"
        note = planning.NOTE if self.planning() and self.allowed_tools is None else ""
        self.messages.append({"role": "user", "content": self.expand_mentions(user_text) + note})
        self.turn_requests = [user_text.strip()]
        self.repeats.reset()
        self.sites.reset()
        self.check_results = []
        self.review_results = None
        self.escalations = []
        escalated = False  # the stronger model is asked at most once per request
        self.tools.before = {}
        reviewed = False
        changed = verified = False  # files changed in this request; the checks passed since
        check_rounds = 0
        malformed = 0
        looping = ""  # the call the model kept repeating, when lcode stopped the request for it
        blocked = None  # the site that couldn't be reached, when lcode stopped the request for it
        stalls = 0  # replies that went round in circles or ended without a word
        commands: list[str] = []  # shell commands run in this request
        scripts_noted = False  # the model was told about scripts it wrote and never ran
        tools_used: set[str] = set()  # tools called in this request
        windows_noted = False  # the model was told about windows it changed and never looked at
        for step in range(self.max_steps):
            if self.cancel is not None and self.cancel.is_set():
                raise KeyboardInterrupt
            if step and self.steering is not None:
                self.steer()
            if step and self.autosave:
                self.save()  # a crash or a closed terminal loses at most this step
            self.maybe_compact()
            try:
                reply = self.assistant_step()
                calls = reply.get("tool_calls") or []
                for i, call in enumerate(calls):
                    call.setdefault("id", f"call_{step}_{i}")  # so a tool result can name its call
                self.emit(
                    {
                        "type": "assistant",
                        "text": reply.get("content") or "",
                        "tool_calls": [
                            {
                                "id": c["id"],
                                "name": c.get("function", {}).get("name", ""),
                                "arguments": call_arguments(c),
                            }
                            for c in calls
                        ],
                    }
                )
            except OllamaError as e:
                if not any(m in str(e) for m in MALFORMED_CALL) or malformed >= MAX_MALFORMED_CALL_RETRIES:
                    raise
                malformed += 1
                if "err=" in str(e):
                    reason = str(e).rsplit("err=", 1)[-1].strip()
                elif "XML syntax error" in str(e):
                    reason = str(e).split("XML syntax error", 1)[1].strip(" :") or "invalid XML"
                elif "expected element type" in str(e) or "closed by </" in str(e):
                    reason = str(e).split("Ollama error:", 1)[-1].strip() or "invalid XML"
                else:
                    reason = "invalid JSON"
                self.console.print("[yellow]The model wrote a malformed tool call; asking it to try again.[/]")
                self.messages.append(
                    {
                        "role": "user",
                        "content": f"[lcode] Your last tool call could not be parsed ({reason}): it must be "
                        "complete and well-formed, with every argument closed. Make the call again. If it writes "
                        "a file, make sure the whole content is included and properly escaped.",
                    }
                )
                continue
            if not calls and (self.reply_trouble == "circles" or not (reply.get("content") or "").strip()):
                stalls += 1
                dropped = self.messages.pop()  # no answer, and circles would only fill the context
                kept = self.keep_dropped(dropped)
                if stalls < MAX_STALLS:
                    self.console.print(
                        Text(f"  ⎿ {STALLED[self.reply_trouble]}: asking for a short answer{kept}", style="yellow")
                    )
                    self.messages.append({"role": "user", "content": stall_note(self.reply_trouble)})
                    continue
                why = "its replies went round in circles or ended without an answer, twice in this request"
                if not escalated and self.can_escalate():
                    escalated = True
                    advice = self.ask_stronger(why)
                    if advice:
                        self.messages.append({"role": "user", "content": advice})
                        stalls = 0
                        continue
                self.turn_status = "loop"
                self.console.print(
                    Text(
                        "Stopped: the model's replies went round in circles or ended without an answer, twice in "
                        f"this request{kept}. Try again with a smaller step, or tell it what to do differently.",
                        style="yellow",
                    )
                )
                return
            if not calls:
                if self.reply_trouble == "cut_off":
                    self.say(
                        "The answer was cut off: the context window is full. /compact or a larger /context helps.",
                        warning=True,
                    )
                if self.steering is not None and self.steering.waiting():
                    continue  # the user added a note while the model wrote its answer: it reads the note first
                unrun = [] if scripts_noted else self.unrun_scripts(commands)
                if unrun:
                    scripts_noted = True
                    self.console.print(Text(f"  ⎿ never ran {', '.join(unrun)}: asking the model to", style="yellow"))
                    self.messages.append({"role": "user", "content": checks.unrun_note(unrun)})
                    continue
                unseen = {} if windows_noted else self.unseen_windows(tools_used)
                if unseen:
                    windows_noted = True
                    self.console.print(
                        Text(f"  ⎿ never looked at {', '.join(unseen)}: asking the model to", style="yellow")
                    )
                    self.messages.append({"role": "user", "content": checks.unseen_note(unseen)})
                    continue
                check = self.project_check() if changed and not verified else None
                if check is not None and check_rounds < checks.MAX_ROUNDS:
                    check_rounds += 1
                    feedback = self.run_check(check, check_rounds)
                    if feedback:
                        if check_rounds >= checks.MAX_ROUNDS and not escalated and self.can_escalate():
                            escalated = True
                            why = f"the project's checks (`{check.command}`) failed {check_rounds} times in a row"
                            advice = self.ask_stronger(why, failure=feedback)
                            if advice:
                                self.messages.append({"role": "user", "content": advice})
                                check_rounds -= 1  # the checks run once more after the advice
                                changed = False
                                continue
                        self.messages.append({"role": "user", "content": feedback})
                        changed = False  # run them again only if the model changes something more
                        continue
                    verified = True
                if not reviewed and self.tools.before and self.reviewing():
                    reviewed = True
                    problems = self.self_review(reply.get("content") or "")
                    if problems:
                        self.messages.append({"role": "user", "content": review.feedback(problems)})
                        continue
                break
            done: dict[int, str] = {}  # results of subagents that ran together
            together = [
                (i, call_arguments(c)) for i, c in enumerate(calls) if c.get("function", {}).get("name") == "agent"
            ]
            if len(together) > 1 and self.settings.subagents and self.settings.max_parallel_agents > 1:
                try:
                    done = subagents.run_many(self, together)
                except KeyboardInterrupt:
                    for call in calls:
                        self.messages.append(
                            {
                                "role": "tool",
                                "tool_name": call.get("function", {}).get("name", ""),
                                "content": "Interrupted by the user before completion.",
                            }
                        )
                    self.console.print("[yellow]⏹ Interrupted[/]")
                    raise
            for i, call in enumerate(calls):
                name, args = call.get("function", {}).get("name", ""), call_arguments(call)
                self.current_call = call.get("id", "")
                if self.on_tool:
                    self.on_tool(name, args)
                if name not in ("bash", "todo_write", "web_search", "web_fetch", "agent"):  # those print their own
                    self.console.print(Text(f"● {self.describe_call(name, args)}", style="bold magenta"))
                began = time.monotonic()
                if self.trace is not None:
                    self.trace.tool_begin()
                try:
                    result = done[i] if i in done else self.run_tool(name, args)
                except KeyboardInterrupt:
                    for rest in calls[i:]:
                        self.messages.append(
                            {
                                "role": "tool",
                                "tool_name": rest.get("function", {}).get("name", ""),
                                "content": "Interrupted by the user before completion.",
                            }
                        )
                    self.console.print("[yellow]⏹ Interrupted[/]")
                    raise
                if result.startswith("Error:"):
                    self.console.print(Text(f"  {result[:300]}", style="red"))
                elif name in ("read_file", "grep", "glob", "list_dir"):
                    self.console.print(Text(f"  ⎿ {result.count(chr(10)) + 1} line(s)", style="dim"))
                budget = self.result_budget()
                if len(result) > budget and name not in ("agent", "skill"):
                    result = context_tools.shorten(result, budget, f"{name} result")
                verdict = self.repeats.check(name, args, result)
                if verdict.note:
                    result += verdict.note
                    what = "stopping the request" if verdict.stop else "told the model"
                    self.console.print(
                        Text(f"  ⎿ same call, same result, {verdict.count} times: {what}", style="yellow")
                    )
                    if verdict.stop:
                        looping = self.describe_call(name, args)
                out_of_reach = self.sites.check(name, args, result)
                if out_of_reach.note and not looping:
                    result += out_of_reach.note
                    what = "stopping the request" if out_of_reach.stop else "told the model"
                    self.console.print(
                        Text(
                            f"  ⎿ {out_of_reach.calls} calls to {out_of_reach.site} failed "
                            f"({out_of_reach.what}), none worked: {what}",
                            style="yellow",
                        )
                    )
                    if out_of_reach.stop:
                        blocked = out_of_reach
                tools_used.add(name)
                if name == "bash":
                    commands.append(str(args.get("command", "")))
                if name in ("edit_file", "write_file", "apply_patch") and not result.startswith(REFUSED):
                    changed, verified = True, False
                elif name == "bash" and changed and self.model_ran_checks(str(args.get("command", "")), result):
                    verified = True  # the model ran the checks itself, after its last change
                if self.trace is not None:
                    self.trace.tool(call.get("id", ""), name, args, result, time.monotonic() - began)
                tool_msg = {"role": "tool", "tool_name": name, "content": result}
                if call.get("id"):
                    tool_msg["tool_call_id"] = call["id"]
                self.messages.append(tool_msg)
                self.emit(
                    {
                        "type": "tool_result",
                        "id": call.get("id", ""),
                        "name": name,
                        "error": result.startswith(REFUSED),
                        "output": result[:EVENT_OUTPUT] + ("…" if len(result) > EVENT_OUTPUT else ""),
                    }
                )
                if looping or blocked:
                    for rest in calls[i + 1 :]:
                        skipped = {
                            "role": "tool",
                            "tool_name": rest.get("function", {}).get("name", ""),
                            "content": "Not run: lcode stopped the request before this call.",
                        }
                        if rest.get("id"):
                            skipped["tool_call_id"] = rest["id"]
                        self.messages.append(skipped)
                    break
            if looping and not escalated and self.can_escalate():
                escalated = True
                advice = self.ask_stronger(f"it kept making the same call with the same result: {looping}")
                if advice:
                    self.messages.append({"role": "user", "content": advice})
                    self.repeats.reset()
                    looping = ""
                    continue
            if blocked:
                self.turn_status = "blocked"
                self.console.print(
                    Text(
                        f"Stopped: every request to {blocked.site} failed ({blocked.calls} calls: {blocked.what}) and "
                        "none worked, so this machine probably can't reach it: a firewall, a proxy, a regional block "
                        "or no internet. Check the connection (another network or a VPN, a proxy setting), or tell "
                        "the model another way to get what it needs.",
                        style="yellow",
                    )
                )
                return
            if looping:
                self.turn_status = "loop"
                self.console.print(
                    Text(
                        f"Stopped: the model kept making the same call with the same result: {looping}. Tell it "
                        "what to do differently, or try again.",
                        style="yellow",
                    )
                )
                return
        else:
            self.turn_status = "max_steps"
            self.console.print(f"[yellow]Stopped after {self.max_steps} steps.[/]")
            return
        self.turn_status = "success"
        self.print_stats()

    def run_tool(self, name: str, args: dict) -> str:
        if self.replay is not None:  # a replay gives some tools their recorded results (lcode.traces)
            served = self.replay.tool_result(name, args)
            if served is not None:
                return served
            args = self.replay.moved(args)  # the recorded folder's paths point into the replay's copy
        return self.tools.run(name, args)

    def ask_stronger(self, why: str, failure: str = "") -> str | None:
        """The stronger model's advice for the local model, if one is set and the user agrees (lcode.escalate)."""
        if self.replay is not None:
            return self.replay.escalation(why)
        with traces.inside(self, "escalate"):
            advice = escalate.run(self, why, failure)
        if self.trace is not None:
            self.trace.escalation(why, advice)
        return advice

    def keep_dropped(self, reply: dict) -> str:
        """Save a reply lcode dropped (its thinking too), so the user can see what went round in circles."""
        text = "\n\n".join(part for part in (reply.get("thinking"), reply.get("content")) if part)
        if not text.strip():
            return ""
        path = context_tools.save_output(text, "dropped reply")
        return f" (the dropped reply is in {path})" if path else ""

    def can_escalate(self) -> bool:
        """Whether a stronger model may be asked here: one is set, and this isn't a subagent or plan mode."""
        model = (self.settings.escalate_model or "").strip().lower()
        return bool(model) and model != "off" and self.allowed_tools is None and not self.planning()

    def steer(self) -> None:
        """Between two steps: pause if the user pressed Esc, then hand the model the notes typed meanwhile."""
        steering = self.steering
        if steering is None:
            return
        if steering.take_pause():
            self.console.print(
                Text(
                    "⏸ Paused. Press Enter to go on, type a note to go on with it, or press Ctrl+C to stop the request.",
                    style="yellow",
                )
            )
            note = steering.ask("  ⏸ ").strip()
            if note:
                steering.add(note)
        notes = steering.take()
        if not notes:
            return
        self.turn_requests += notes
        self.messages.append({"role": "user", "content": "\n\n".join(notes) + "\n\n" + NOTE_INTRO})
        for note in notes:
            shown = note if len(note) <= 200 else note[:200] + "…"
            self.console.print(Text(f"✎ Note for the model: {shown}", style="cyan"))

    def activity(self, phase: str):
        """A spinner with the elapsed time while something runs (a command), with the note being typed under it."""
        return self.console.status(Progress(phase, self.steering, tokens=False), spinner="dots")

    def print_stats(self) -> None:
        pct = 100 * self.ctx_used / self.settings.context
        self.console.print(
            Text(
                f"  ctx {format_tokens(self.ctx_used)}/{format_tokens(self.settings.context)} ({pct:.0f}%) · "
                f"{self.last_speed:.1f} tok/s",
                style="dim",
            )
        )

    def expand_mentions(self, text: str) -> str:
        """Attach what the user's message points to: files and folders written as @path, image paths (with or
        without @), and @name of an agent asks for that agent. An image path that doesn't exist is reported to
        the user and the model, so the model doesn't guess what the image shows."""
        attached: list[str] = []
        seen: set[Path] = set()
        question = MENTION.sub("", text)

        def attach_image(p: Path) -> None:
            try:
                description = self.look(p, question)
                who = f' described_by="{self.vision_model()}"'
            except ToolError as e:
                description, who = f"(lcode couldn't look at this image: {e})", ""
                self.console.print(Text(f"  ⎿ {e}", style="yellow"))
            else:
                self.console.print(Text(f"  ⎿ looked at {self.tools.rel(p)}", style="dim"))
            attached.append(f'<image path="{self.tools.rel(p)}"{who}>\n{description}\n</image>')

        def missing_image(shown: str) -> None:
            self.console.print(
                Text(f"  ⎿ {shown} doesn't exist, so the model can't see it. Paste images with Ctrl+V.", style="yellow")
            )
            attached.append(
                f"[lcode: the user's message mentions the image {shown}, but there's no such file, so it wasn't "
                "attached and you can't see it. Don't guess what it shows: ask the user to paste it again (Ctrl+V "
                "in lcode's prompt) or to describe it.]"
            )

        for match in MENTION.finditer(text):
            ref = next(g for g in match.groups() if g)
            p = self.tools.resolve(ref)
            seen.add(p)
            name = ref.removeprefix("agent-")
            if not p.exists() and self.settings.subagents and name in self.agent_types():
                attached.append(f'[The user wants the {name} agent for this: call the agent tool with type "{name}".]')
                continue
            if p.is_file() and vision.is_image(p):
                attach_image(p)
            elif p.is_file() and not is_binary(p) and p.stat().st_size < 200_000:
                attached.append(f'<file path="{self.tools.rel(p)}">\n{p.read_text(errors="replace")}\n</file>')
                self.tools.read_mtimes[str(p)] = p.stat().st_mtime
                self.console.print(Text(f"  ⎿ attached {self.tools.rel(p)}", style="dim"))
            elif p.is_dir():
                attached.append(f'<directory path="{self.tools.rel(p)}">\n{tree(p, 2)}\n</directory>')
            elif not p.exists() and vision.is_image(p):
                missing_image(ref)
        for match in IMAGE_PATH.finditer(text):
            quoted, raw = bool(match.group(2)), match.group(2) or match.group(3)
            if raw.startswith("@") or "://" in raw.removeprefix("file://"):
                continue  # attached above, or a web address
            if raw.startswith("file://"):
                shown = urllib.parse.unquote(raw.removeprefix("file://"))
            else:
                shown = raw.replace("\\ ", " ")
            p = self.tools.resolve(shown)
            if p in seen:
                continue
            seen.add(p)
            if p.is_file():
                attach_image(p)
            elif quoted or raw.startswith(("/", "~", "./", "../", "file://")):
                missing_image(shown)  # a name without a folder may be a file to create: no warning
        return text + ("\n\n" + "\n\n".join(attached) if attached else "")
