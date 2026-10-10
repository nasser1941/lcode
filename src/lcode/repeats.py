"""Notice when the model goes round in circles.

Small models sometimes fall into a loop: they make a tool call, get a result, and make the very same call
again, and again, until the user steps in. Within one request lcode counts the calls that repeat an earlier
one exactly: the same tool, the same arguments and the same result. At the `limit`-th one (a call that
failed: one sooner) the result gets a note that tells the model so, and if it still goes on, lcode ends the
request.

Results that change aren't repeats: a test run again after a fix, a background job read while it runs. And
a call that may have changed something (a file edit, a shell command that isn't read-only) starts the count
again for every other call, so checking a build after each edit never adds up. Its own repeats still count:
running the same failing command over and over is a loop.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from lcode.permissions import is_read_only

STOP_AFTER_NOTE = 2  # repeats after the note before the request ends
FAILED = ("Error:", "A deny rule", "The user denied", "This needs the user's permission")
UNCHANGING = frozenset(
    {
        "read_file",
        "list_dir",
        "glob",
        "grep",
        "view_image",
        "repo_map",
        "search_code",
        "lsp",
        "web_search",
        "web_fetch",
        "bash_output",
        "todo_write",
        "skill",
        "mcp_find_tools",
    }
)  # tools that don't change files; anything else might


@dataclass
class Verdict:
    count: int  # how often this exact call got this exact result in the request
    note: str = ""  # to add to the tool result
    stop: bool = False  # end the request


class Repeats:
    def __init__(self, limit: int = 3) -> None:
        self.limit = limit  # 0: off
        self.seen: dict[str, int] = {}

    def reset(self) -> None:
        """A new request."""
        self.seen.clear()

    def check(self, name: str, args: dict, result: str) -> Verdict:
        """Count this call and its result."""
        if self.limit <= 0:
            return Verdict(1)
        key = hashlib.sha256(json.dumps([name, args, result], sort_keys=True, default=str).encode()).hexdigest()
        count = self.seen.get(key, 0) + 1
        if changes_things(name, args, result):
            self.seen.clear()
        self.seen[key] = count
        failed = result.startswith(FAILED)
        note_at = max(2, self.limit - 1) if failed else max(2, self.limit)
        if count < note_at:
            return Verdict(count)
        same = "the same error" if failed else "the same result"
        if count >= note_at + STOP_AFTER_NOTE:
            return Verdict(
                count,
                f"\n\n[lcode] You made this exact call {count} times in this request and got {same} each "
                "time, so lcode stopped the request here. Next time, don't repeat a call that didn't help: "
                "change your approach, or tell the user what is blocking you.",
                stop=True,
            )
        hint = (
            "If it's an edit, read the file again and copy the text exactly. "
            if failed and name in ("edit_file", "write_file")
            else ""
        )
        return Verdict(
            count,
            f"\n\n[lcode] You made this exact call {count} times in this request and got {same} each time. "
            f"Making it again won't change that. Use what it returned, or try something different. {hint}"
            "If you're stuck, stop and tell the user what is blocking you.",
        )


def changes_things(name: str, args: dict, result: str) -> bool:
    """Whether the call may have changed files (or anything else a later call could see)."""
    if name in UNCHANGING:
        return False
    if name in ("edit_file", "write_file"):
        return not result.startswith(FAILED)
    if name == "bash":
        return not is_read_only(str(args.get("command", "")))
    return True  # other tools (MCP, subagents…) might
