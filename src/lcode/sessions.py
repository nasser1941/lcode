"""Saved conversations: listing, naming and finding sessions to resume."""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path

from lcode import config

TITLE_LENGTH = 70


@dataclass(frozen=True)
class SessionInfo:
    id: str
    path: Path
    cwd: str
    name: str  # set with /rename; empty if never named
    title: str  # the first request, shortened
    model: str
    updated: float  # modification time of the session file
    turns: int  # number of requests from the user

    @property
    def label(self) -> str:
        return self.name or self.title or "(empty)"


def sessions_dir() -> Path:
    return config.STATE_DIR / "sessions"


def title_from(messages: list[dict]) -> str:
    """A one-line title from the first request, without attached files."""
    for message in messages:
        if message.get("role") == "user":
            text = re.split(r"\n\n<(?:file|directory) path=", message.get("content", ""), maxsplit=1)[0]
            text = " ".join(text.split())
            return text if len(text) <= TITLE_LENGTH else text[: TITLE_LENGTH - 1].rstrip() + "…"
    return ""


def read_info(path: Path) -> SessionInfo | None:
    try:
        data = json.loads(path.read_text())
        updated = path.stat().st_mtime
    except (OSError, ValueError):
        return None
    messages = data.get("messages") or []
    return SessionInfo(
        id=path.stem,
        path=path,
        cwd=data.get("cwd", ""),
        name=data.get("name", ""),
        title=data.get("title") or title_from(messages),
        model=data.get("model", ""),
        updated=updated,
        turns=sum(1 for m in messages if m.get("role") == "user"),
    )


def list_sessions(cwd: Path | str | None = None, limit: int = 50) -> list[SessionInfo]:
    """Saved sessions, most recently used first; only those for `cwd` if given."""
    directory = sessions_dir()
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    found = []
    for f in files:
        info = read_info(f)
        if info and info.turns and (cwd is None or info.cwd == str(cwd)):
            found.append(info)
            if len(found) >= limit:
                break
    return found


def find(query: str, sessions: list[SessionInfo]) -> SessionInfo | None:
    """Match a list number (1-based), a name, a session id (or unique prefix) or a unique title fragment."""
    q = query.strip()
    if not q:
        return None
    if q.isdigit() and 1 <= int(q) <= len(sessions):
        return sessions[int(q) - 1]
    lowered = q.lower()
    for s in sessions:
        if s.name and s.name.lower() == lowered:
            return s
    for candidates in (
        [s for s in sessions if s.id == q or s.id.startswith(q)],
        [s for s in sessions if lowered in s.label.lower()],
    ):
        if len(candidates) == 1:
            return candidates[0]
    return None


def age(timestamp: float, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    then = dt.datetime.fromtimestamp(timestamp)
    seconds = (now - then).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if then.date() == now.date():
        return f"{int(seconds // 3600)} h ago"
    if then.date() == (now - dt.timedelta(days=1)).date():
        return "yesterday"
    return then.strftime("%b %d") if then.year == now.year else then.strftime("%Y-%m-%d")
