"""A second look at a change before the answer goes to the user.

Models claim more than they did: "users can now shrink the window" without having checked, half of a
two-part request done, a debug print left behind. So when a request changed files, lcode asks the model to
step back and check its change against the request (what was asked, what the diff shows, what the answer
claims) before the answer counts. Real problems go back to the model once; the user sees a line about it.

The review is asked on top of the conversation, not in a fresh one: the model server keeps the processed
conversation cached, and a separate review prompt would push it out, so the next step would have to read the
whole conversation again, tens of seconds on a small GPU. The question tells the model to judge only from the
request and the diff.
"""

from __future__ import annotations

import difflib
from typing import TYPE_CHECKING

from lcode.gitflow import parse_json

if TYPE_CHECKING:
    from lcode.agent import Agent

MIN_CHANGED_LINES = 1  # even one line: a one-line bug fix is where a wrong fix hides
MAX_PROBLEMS = 5
SCHEMA = {
    "type": "object",
    "properties": {"problems": {"type": "array", "items": {"type": "string"}}},
    "required": ["problems"],
}
PROMPT = """[lcode] Before your answer goes to the user, review your change as a strict reviewer would: judge it only by what the user asked and what the diff shows, not by what you meant to do.

The user's request:
<request>
{request}
</request>

Your answer to them:
<answer>
{answer}
</answer>

{checks}The change (unified diff of the files changed in this request):
<diff>
{diff}
</diff>

Find real problems only, each one you can point to in the request or the diff:
1. Something the user asked for that the change doesn't do, or only partly does.
2. A claim in your answer that the diff doesn't support, or something called tested or working that nothing shows was run.
3. A mistake in the diff: a bug, a broken edge case the request mentions, leftover debugging (prints, commented-out code), an unfinished TODO.
No style preferences, no suggestions beyond the request. Answer in JSON: {{"problems": ["…"]}}, with an empty list when the change does what was asked."""

FEEDBACK = """[lcode] A review of your change against the user's request found:
{problems}

Fix what's real; if a point is wrong, say why in your answer. Then finish."""


def diff_of(before: dict[str, str | None], read, budget: int) -> tuple[str, int]:
    """The unified diff of the files changed in the request, and how many lines it changes."""
    parts, changed = [], 0
    for path, old in before.items():
        new = read(path)
        if new is None or new == old:
            continue
        lines = list(
            difflib.unified_diff((old or "").splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}", n=2)
        )
        changed += sum(1 for line in lines if line[:1] in "+-" and not line.startswith(("+++", "---")))
        parts.append("".join(lines))
    text = "".join(parts)
    if len(text) > budget:
        text = text[:budget] + f"\n[… the rest of the diff ({len(text) - budget} characters) is cut]"
    return text, changed


def run(agent: Agent, request: str, answer: str, diff: str) -> list[str]:
    """Ask for the review; the problems it found (empty when none, or when the answer can't be read)."""
    checks = ""
    if agent.check_results:
        last = agent.check_results[-1]
        code = last.get("exit_code")
        state = "passed" if code == 0 else "didn't run" if code is None else f"failed (exit code {code})"
        checks = f"The project's checks (`{last.get('command')}`) {state} after the change.\n\n"
    prompt = PROMPT.format(request=request[:4000], answer=answer[:3000] or "(none)", checks=checks, diff=diff)
    payload = {
        "model": agent.settings.model,
        "messages": [*agent.messages, {"role": "user", "content": prompt}],
        "tools": agent.tool_schemas(),  # as in the session, so the cached conversation is reused
        "think": agent.settings.think,  # without reasoning it missed every gap it caught with it, and wasn't faster
        "format": SCHEMA,
        "keep_alive": agent.settings.keep_alive,
        "options": agent.options(),
    }
    reply = agent.ollama.chat(payload)
    data = parse_json((reply.get("message") or {}).get("content") or "")
    problems = data.get("problems") if isinstance(data.get("problems"), list) else []
    return [str(p).strip()[:500] for p in problems if str(p).strip()][:MAX_PROBLEMS]


def feedback(problems: list[str]) -> str:
    return FEEDBACK.format(problems="\n".join(f"- {p}" for p in problems))
