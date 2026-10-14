"""Ask a stronger model when the local one is stuck (opt-in, asked each time, with the cost shown).

Some problems are beyond a small local model: it loops, or the same check keeps failing. With `escalate_model`
set, lcode can then ask a stronger model, in the cloud (OpenAI, Anthropic, xAI, with the user's key) or a
bigger local one (Ollama, here or on another server), for one piece of advice. The local model gets the advice
and carries on with its own tools.

Nothing is sent without asking: the user sees why, what goes (the requests, the task sheet, the failure, the
files in question) and its size and price, and can read all of it first. Only that goes, never the whole
session, and API keys come from environment variables, read when needed and sent only to their own provider.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from lcode import context as context_tools
from lcode.ollama import Ollama, OllamaError, redact

if TYPE_CHECKING:
    from lcode.agent import Agent

# provider -> (API address, the environment variable with its key, its name)
PROVIDERS = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "OpenAI"),
    "anthropic": ("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY", "Anthropic"),
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY", "xAI"),
    "ollama": ("", "", "Ollama"),  # lcode's own Ollama, or another one at escalate_url
    "custom": ("", "LCODE_ESCALATE_API_KEY", "OpenAI-compatible server"),  # any OpenAI API at escalate_url
}
BUDGET = 60_000  # characters sent at most (about 15K tokens)
FILE_CHARS = 16_000  # of one file
MAX_OUTPUT = 4096  # tokens the advice may take (thinking included, for local models)
RECENT_STEPS = 8  # the last tool calls and results that go along
TIMEOUT = 1800  # seconds: a big local model is slow

ADVISOR = """You are a senior engineer helping a smaller coding model that is stuck on a task in the user's repository. You can't run anything: you get the user's request, the model's notes, why it's stuck, the files in question and its last steps. Find the real cause and say exactly what to change.

Answer in this order:
1. What's wrong: the root cause in a few sentences, pointing at file:line.
2. The fix: unified diffs, or edits precise enough to apply (file, the exact code to replace, the new code). Fix the cause, not the symptom: no special cases for a test.
3. How to check it: the command to run and what it should show.
Be brief. If something you need is missing, say what to look at."""

ADVICE = """[lcode] You were stuck ({why}), so lcode asked a stronger model, {model}, for advice. It saw your notes, the failure, the files involved and your last steps, but couldn't run anything:

{advice}

Check its advice against the code, apply what's right with your tools, run the checks, and carry on."""


class EscalateError(Exception):
    """The stronger model can't be asked; the message says why and what to do."""


@dataclass
class Target:
    provider: str
    model: str
    url: str  # where the request goes ("" for lcode's own Ollama)
    key_env: str  # the environment variable with the key ("" when none is needed)
    label: str  # e.g. "claude-sonnet-4-5 (Anthropic)"

    @property
    def local(self) -> bool:
        return not self.url or bool(re.match(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:|/|$)", self.url))

    @property
    def cloud(self) -> bool:
        """Paid per token: a cloud provider, or an OpenAI-compatible API elsewhere."""
        return self.provider in ("openai", "anthropic", "xai") or (self.provider == "custom" and not self.local)

    @property
    def where(self) -> str:
        if self.url:
            return re.sub(r"^https?://", "", redact(self.url)).split("/")[0]
        return "this machine"


def target(setting: str | None, url: str | None = None) -> Target | None:
    """The stronger model from the settings (None: escalation is off). Raises EscalateError for a bad setting."""
    setting = (setting or "").strip()
    if not setting or setting.lower() == "off":
        return None
    provider, sep, model = setting.partition("/")
    if not sep or provider.lower() not in PROVIDERS:
        if url:  # a model name with its own server
            provider, model = ("ollama" if ":11434" in url else "custom"), setting
        else:
            raise EscalateError(
                f"escalate_model {setting!r} needs a provider: openai/…, anthropic/…, xai/…, ollama/… or custom/… "
                "(with escalate_url)"
            )
    provider = provider.lower()
    base, key_env, name = PROVIDERS[provider]
    if not model.strip():
        raise EscalateError(f"escalate_model {setting!r} names no model")
    if provider == "custom" and not url:
        raise EscalateError("a custom/… escalate_model needs escalate_url, the server's OpenAI-compatible address")
    return Target(provider, model.strip(), (url or base).rstrip("/"), key_env, f"{model.strip()} ({name})")


def client(t: Target, ollama):
    """The client that talks to the stronger model. The key is read now and only goes to its provider."""
    if t.provider == "ollama":
        if t.url:
            return Ollama(t.url)
        # lcode's own Ollama, unless lcode talks to another kind of server: then the usual address
        return ollama if getattr(ollama, "kind", "ollama") == "ollama" else Ollama("http://localhost:11434")
    from lcode.backends import OpenAICompatible

    key = os.environ.get(t.key_env, "") if t.key_env else ""
    if t.provider != "custom" and not key:
        raise EscalateError(f"{t.label} needs an API key: set {t.key_env} in your environment (lcode never stores it)")
    return OpenAICompatible(t.url, api_key=key, name=t.label)


def parse_price(text: str | None) -> tuple[float, float] | None:
    """`escalate_price` "3/15": dollars per million input and output tokens."""
    if not text:
        return None
    m = re.fullmatch(r"\s*\$?(\d+(?:\.\d+)?)\s*/\s*\$?(\d+(?:\.\d+)?)\s*", str(text))
    if not m:
        raise EscalateError(f"escalate_price must look like 3/15 (dollars per million tokens in/out), not {text!r}")
    return float(m.group(1)), float(m.group(2))


def tokens(text: str) -> int:
    return max(1, len(text) // 4)


def cost_line(t: Target, sent: int, price: tuple[float, float] | None) -> str:
    if not t.cloud:
        return "free: it runs on " + ("this machine" if not t.url else t.where)
    if price is None:
        return "unknown: set escalate_price to see it (dollars per million tokens in/out, e.g. lcode config set escalate_price 3/15)"
    low = sent * price[0] / 1e6
    high = low + MAX_OUTPUT * price[1] / 1e6
    return f"about ${low:.3f} to ${high:.3f} ({format_k(sent)} tokens in at ${price[0]:g}/M, up to {format_k(MAX_OUTPUT)} out at ${price[1]:g}/M)"


def format_k(n: int) -> str:
    return f"{n / 1000:.1f}K" if n >= 1000 else str(n)


# --------------------------------------------------------------------------------------------- what's sent


def brief(agent: Agent, why: str, failure: str = "") -> tuple[str, list[str]]:
    """What the stronger model gets, and the names of the files in it."""
    parts = []
    requests = context_tools.user_requests(agent.messages)[-3:]
    parts.append("# What the user asked (latest last)\n" + "\n---\n".join(r[:3000] for r in requests))
    sheet = context_tools.task_sheet(agent)
    if sheet:
        parts.append("# The model's task sheet\n" + sheet)
    parts.append(f"# Why it's stuck\n{why}" + (f"\n\n{failure[-6000:]}" if failure else ""))
    steps = recent_steps(agent.messages)
    if steps:
        parts.append("# Its last steps\n" + steps)
    used = sum(len(p) for p in parts)
    files, names = [], []
    for path in relevant_files(agent):
        text = agent.current_text(path)
        if text is None:
            continue
        if len(text) > FILE_CHARS:
            text = text[:FILE_CHARS] + f"\n[… {len(text) - FILE_CHARS} more characters]"
        block = f"## {agent.tools.rel(agent.tools.resolve(path))}\n```\n{text}\n```"
        if used + len(block) > BUDGET:
            break
        used += len(block)
        files.append(block)
        names.append(agent.tools.rel(agent.tools.resolve(path)))
    if files:
        parts.append("# The files involved (as they are now)\n" + "\n\n".join(files))
    return "\n\n".join(parts), names


def relevant_files(agent: Agent) -> list[str]:
    """The files changed in this request, then the ones read most recently."""
    seen: dict[str, None] = dict.fromkeys(agent.tools.before)
    for message in reversed(agent.messages):
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = fn.get("arguments") or {}
            if (
                isinstance(args, dict)
                and fn.get("name") in ("read_file", "edit_file", "write_file")
                and args.get("path")
            ):
                seen.setdefault(str(args["path"]), None)
        if len(seen) >= 12:
            break
    return list(seen)


def recent_steps(messages: list[dict]) -> str:
    lines = []
    for message in messages[-40:]:
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = fn.get("arguments") or {}
            shown = ", ".join(f"{k}={str(v)[:120]!r}" for k, v in args.items()) if isinstance(args, dict) else ""
            lines.append(f"- {fn.get('name', '?')}({shown})")
        if message.get("role") == "tool":
            result = str(message.get("content") or "").strip()
            lines.append("  → " + (result[:400] + " …" if len(result) > 400 else result).replace("\n", "\n    "))
    return "\n".join(lines[-RECENT_STEPS * 2 :])


# --------------------------------------------------------------------------------------------- asking


def ask_user(agent: Agent, t: Target, why: str, text: str, names: list[str], price) -> bool:
    """Show what would go where, at what cost, and ask. Always asked, whatever the permission mode."""
    if agent.escalate_approved is not None:  # a program decides (lcode -p --escalate, the benchmark)
        return agent.escalate_approved
    if not agent.interactive:
        agent.say("A stronger model could help, but nobody can be asked in this run (lcode -p --escalate allows it).")
        return False
    sent = tokens(ADVISOR) + tokens(text)
    files = ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "") if names else "none"
    body = Text.from_markup(
        f"[bold]Why:[/] {escape(why)}\n"
        f"[bold]What goes:[/] your requests, the task sheet, the failure, the last steps and these files: "
        f"{escape(files)} ({format_k(sent)} tokens)\n"
        f"[bold]Where:[/] {escape(t.label)}, {'on' if t.where == 'this machine' else 'at'} {escape(t.where)}\n"
        f"[bold]Cost:[/] {escape(cost_line(t, sent, price))}"
    )
    agent.console.print(Panel(body, title=f"Ask {t.model} for help?", title_align="left", border_style="magenta"))
    agent.waiting(f"ask {t.model} for help?")
    read = agent.perms.read_line or input
    while True:
        try:
            answer = read("  Send it? [y]es / [n]o / [s]how what's sent: ").strip().lower()
        except EOFError:
            return False
        if answer in ("s", "show"):
            agent.console.print(Panel(Text(text), title="What would be sent", title_align="left", border_style="dim"))
            continue
        return answer in ("y", "yes")


def advise(agent: Agent, t: Target, text: str) -> tuple[str, dict]:
    """The stronger model's advice, and its token counts."""
    talk = client(t, agent.ollama)
    payload: dict = {
        "model": t.model,
        "messages": [{"role": "system", "content": ADVISOR}, {"role": "user", "content": text}],
        "options": {"num_predict": MAX_OUTPUT},
    }
    if t.provider == "ollama":
        from lcode.cli import NotInstalled, resolve_model

        try:
            payload["model"], _ = resolve_model(talk, t.model)  # a catalog key (qwen3.8-27b) or an Ollama tag
        except (NotInstalled, OllamaError) as e:
            raise EscalateError(str(e)) from e
        payload["think"] = agent.settings.think
        payload["keep_alive"] = "5m"  # it gives way to lcode's model soon after
        payload["options"]["num_ctx"] = max(16384, min(65536, (tokens(text) + MAX_OUTPUT) * 5 // 4))
    try:
        reply = talk.chat(payload, timeout=TIMEOUT)
    except OllamaError as e:
        raise EscalateError(str(e).splitlines()[0][:300]) from e
    advice = ((reply.get("message") or {}).get("content") or "").strip()
    if not advice:
        raise EscalateError(f"{t.model} gave no answer")
    usage = {"in": reply.get("prompt_eval_count") or tokens(text), "out": reply.get("eval_count") or tokens(advice)}
    return advice, usage


def run(agent: Agent, why: str, failure: str = "") -> str | None:
    """Ask the stronger model, if one is set and the user agrees; the message for the local model, or None."""
    try:
        t = target(agent.settings.escalate_model, agent.settings.escalate_url)
        price = parse_price(agent.settings.escalate_price)
    except EscalateError as e:
        agent.say(str(e), warning=True)
        return None
    if t is None:
        return None
    text, names = brief(agent, why, failure)
    if not ask_user(agent, t, why, text, names, price):
        return None
    started = time.monotonic()
    try:
        with agent.console.status(f"Asking {t.label} for help…", spinner="dots"):
            advice, usage = advise(agent, t, text)
    except EscalateError as e:
        agent.say(f"Asking {t.model} failed: {e}", warning=True)
        return None
    seconds = time.monotonic() - started
    cost = ""
    if t.cloud and price:
        cost = f", ${usage['in'] * price[0] / 1e6 + usage['out'] * price[1] / 1e6:.3f}"
    agent.escalations.append(
        {"model": t.label, "why": why, "tokens_in": usage["in"], "tokens_out": usage["out"], "seconds": round(seconds)}
    )
    agent.say(
        f"{t.model} answered in {seconds:.0f}s ({format_k(usage['in'])} tokens in, {format_k(usage['out'])} out"
        f"{cost}). Its advice goes to the model."
    )
    first = next((line.strip() for line in advice.splitlines() if line.strip()), "")
    agent.console.print(Text(f"  ⎿ {first[:200]}", style="dim"))
    return ADVICE.format(why=why, model=t.label, advice=advice)
