"""Keeping the conversation in the context window: how full it is, removing old output, and summaries."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from rich.markdown import Markdown
from rich.panel import Panel

from lcode import (
    catalog,
    sessions,
    traces,
)
from lcode import context as context_tools
from lcode import memory as memory_notes
from lcode.agent_base import AgentBase
from lcode.config import format_tokens
from lcode.ollama import OllamaError

if TYPE_CHECKING:
    from lcode.agent import Agent

    pass


RESULT_SHARE = 0.25  # of the context window, at most, for one tool result


MIN_RESULT_CHARS = 8000


AUTO_COMPACT_RATIO = 0.85  # summarize the history when the context is this full


PRUNED_ENOUGH = 0.6  # after pruning old tool output, summarize only if the context is still this full


def conversation_chars(messages: list[dict]) -> int:
    return sum(len(str(m.get("content") or "")) + len(str(m.get("thinking") or "")) for m in messages)


class ContextWindow(AgentBase):
    """Agent's handling of the context window."""

    # -- context management
    def compact_limit(self) -> int:
        """The tokens in use at which lcode makes room: 85% of the window, or `compact_at` when that's lower.
        Small models lose track in long contexts well before a large window is full."""
        limit = int(AUTO_COMPACT_RATIO * self.settings.context)
        if self.settings.compact_at > 0:
            limit = min(limit, self.settings.compact_at)
        return limit

    def next_request_tokens(self) -> int:
        """The size of the next request: what the model last counted, plus what was added since (tool output,
        attachments). Deciding on the last count alone let one large tool result overflow the window."""
        if self.counted_chars is None:
            return self.ctx_used
        return self.ctx_used + max(0, conversation_chars(self.messages) - self.counted_chars) // 3

    def result_budget(self) -> int:
        """Characters one tool result may take: a share of the window, so one large result can't fill it."""
        return max(MIN_RESULT_CHARS, int(self.settings.context * 3 * RESULT_SHARE))

    def maybe_compact(self) -> None:
        limit = self.compact_limit()
        if self.next_request_tokens() > limit:
            self.ctx_used, self.counted_chars = self.next_request_tokens(), None
        if self.ctx_used <= limit:
            return
        enough = int(limit * PRUNED_ENOUGH / AUTO_COMPACT_RATIO)  # after pruning, a summary isn't needed below this
        if self.settings.prune:
            removed: list[str] = []  # files and pages whose only copy goes
            saved = context_tools.prune(self.messages, removed=removed)  # tool output from before the last two requests
            if self.ctx_used - saved // 3 > enough:
                saved += context_tools.prune(
                    self.messages, keep=1, removed=removed
                )  # not enough: before the last request
            context_tools.note_removed(self.messages, removed)
            if saved:
                context_tools.pin(self.messages, context_tools.task_sheet(cast("Agent", self)))
            if saved:
                self.sent_chars = 0  # the conversation changed: the model reads it again
                self.pruned += 1
                self.ctx_used = max(len(self.messages[0]["content"]) // 3, self.ctx_used - saved // 3)
                self.console.print(
                    f"[dim]Removed old tool output from the conversation (~{format_tokens(saved // 3)} tokens) to "
                    "make room.[/]"
                )
                if self.ctx_used <= enough:
                    return
        window_full = self.ctx_used > AUTO_COMPACT_RATIO * self.settings.context
        if not window_full and self.summarized_size and self.ctx_used - self.summarized_size < limit // 4:
            return  # summarized not long ago: summarizing again so soon would only lose more
        if limit < AUTO_COMPACT_RATIO * self.settings.context:
            self.console.print(
                f"[yellow]The conversation reached {format_tokens(limit)} tokens (compact_at) — summarizing it…[/]"
            )
        else:
            self.console.print("[yellow]Context is nearly full — compacting the conversation…[/]")
        self.compacted += 1
        self.compact()

    def compact(self, focus: str = "") -> None:
        if len(self.messages) <= 2:
            return
        if self.interactive:
            with traces.inside(cast("Agent", self), "memory"):
                memory_notes.reflect(cast("Agent", self))  # what the summary leaves out is gone for good
        instructions = (
            "Summarize this conversation so the work can continue in a fresh context. Include: the user's goals "
            "and requests, key facts learned about the codebase (files, functions, paths with line numbers), "
            "decisions made, every file created or modified and how, commands run and their outcomes, the current "
            "state, and the remaining next steps. Be thorough but compact. Do not call tools."
        )
        if focus:
            instructions += f" Focus especially on: {focus}"
        summary = ""
        with self.console.status("[cyan]Compacting…[/]"), traces.purpose(cast("Agent", self), "summary"):
            for chunk in self.chat([*self.messages, {"role": "user", "content": instructions}], None, False):
                summary += chunk.get("message", {}).get("content", "")
        requests = context_tools.user_requests(self.messages)
        self.messages = [
            {"role": "system", "content": self.system_prompt()},
            {
                "role": "user",
                "content": f"{sessions.SUMMARY_PREFIX}\n\n{summary}"
                + context_tools.requests_block(requests)
                + (f"\n\n{sheet}" if (sheet := context_tools.task_sheet(cast("Agent", self))) else ""),
            },
            {"role": "assistant", "content": "Got it — I have the context from the summary and will continue."},
        ]
        if self.plan:  # the approved plan stays word for word
            self.messages[1]["content"] += f"\n\n[The plan the user approved; keep following it]\n\n{self.plan}"
        if self.skills_loaded:  # their instructions were in the old messages: load them again when needed
            names = ", ".join(sorted(self.skills_loaded))
            self.messages[1]["content"] += (
                f"\n\n[Skills in use before the summary: {names}. Load them again with the skill tool if you still need them.]"
            )
            self.skills_loaded.clear()
        self.ctx_used = sum(len(m["content"]) for m in self.messages) // 3
        self.counted_chars = None
        self.summarized_size = self.ctx_used
        self.sent_chars = 0
        self.reflected = len(self.messages)
        self.console.print(Panel(Markdown(summary), title="Compacted summary", border_style="blue"))

    # -- settings changes
    def set_context(self, tokens: int) -> str:
        spec = catalog.find(self.settings.model)
        limit = None
        try:
            limit = self.ollama.max_context(self.settings.model)
        except OllamaError:
            limit = spec.max_context if spec else None
        if limit and tokens > limit:
            tokens = limit
            note = f" (capped at the model's maximum, {format_tokens(limit)})"
        else:
            note = ""
        self.settings.context = tokens
        if getattr(self.ollama, "kind", "ollama") != "ollama":  # the server keeps its own window
            return f"lcode now uses {format_tokens(tokens)} tokens of the context window{note}, and summarizes sooner."
        return f"Context window set to {format_tokens(tokens)} tokens{note}. The model reloads on the next request."
