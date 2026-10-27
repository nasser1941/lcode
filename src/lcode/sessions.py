"""Saved conversations: listing, naming and finding sessions to resume."""

from __future__ import annotations

import contextlib
import datetime as dt
import itertools
import json
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from lcode import config, repoid
from lcode.config import format_tokens

TITLE_LENGTH = 70
ELSEWHERE_LIMIT = 20  # sessions from other folders /resume lists; /resume all lists them all
SUMMARY_PREFIX = "[Summary of our conversation so far]"  # first message of a compacted conversation
EXIT_WORDS = {"exit", "quit", ":q", ":quit"}


@dataclass(frozen=True)
class SessionInfo:
    id: str
    path: Path
    cwd: str
    name: str  # set with /rename; empty if never named
    title: str  # the first request, shortened
    model: str
    updated: float  # modification time of the session file
    turns: int  # number of requests from the user (and images made, in image mode)
    tokens: int = 0  # the context the conversation uses
    estimated: bool = True  # tokens estimated from the text: saved by a version before 0.52
    repo: repoid.Repo | None = field(default=None, compare=False)  # the repository it was saved in, if saved

    @property
    def label(self) -> str:
        return self.name or self.title or "(no requests yet)"

    def context(self, window: int) -> str:
        """The context it uses against a window, e.g. `18.2K / 32K (57%)`."""
        if not self.tokens:
            return ""
        used = f"{'~' if self.estimated else ''}{format_tokens(self.tokens)}"
        return f"{used} / {format_tokens(window)} ({100 * self.tokens / window:.0f}%)" if window else used


@dataclass
class Listing:
    """Saved sessions in two sections, numbered across both: this folder's first."""

    here: list[SessionInfo]  # saved in this folder, or anywhere in its repository (another checkout too)
    elsewhere: list[SessionInfo]
    more: int = 0  # older sessions elsewhere not listed
    hidden: int = 0  # from temporary folders that are gone (test runs)

    @property
    def numbered(self) -> list[SessionInfo]:
        return [*self.here, *self.elsewhere]


def sessions_dir() -> Path:
    return config.STATE_DIR / "sessions"


def title_from(messages: list[dict]) -> str:
    """A one-line title from the first request, without attached files or compaction summaries."""
    compacted = False
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if content.startswith(SUMMARY_PREFIX):
            compacted = True
            continue
        if content.startswith("[lcode]"):
            continue  # lcode's own message to the model (a failed check, a review), not the user's words
        text = re.split(r"\n\n(?:<(?:file|directory|image) path=|\[lcode[:\]] )", content, maxsplit=1)[0]
        text = " ".join(text.split())
        if text:
            return text if len(text) <= TITLE_LENGTH else text[: TITLE_LENGTH - 1].rstrip() + "…"
    return "Compacted conversation" if compacted else ""


def estimate_tokens(messages: list[dict]) -> int:
    """About how many tokens a conversation takes: a token is about three characters of its JSON."""
    return sum(len(json.dumps(m)) for m in messages) // 3


def read_info(path: Path) -> SessionInfo | None:
    try:
        data = json.loads(path.read_text())
        updated = path.stat().st_mtime
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    messages = [m for m in data.get("messages") or [] if isinstance(m, dict)]
    saved = data.get("context") if isinstance(data.get("context"), dict) else {}
    tokens = saved.get("used") if isinstance(saved.get("used"), int) and saved.get("used") > 0 else 0
    return SessionInfo(
        id=path.stem,
        path=path,
        cwd=data.get("cwd", ""),
        name=data.get("name", ""),
        title=data.get("title") or title_from(messages),
        model=data.get("model", ""),
        updated=updated,
        turns=sum(1 for m in messages if m.get("role") == "user") + len((data.get("media") or {}).get("results") or []),
        tokens=tokens or estimate_tokens(messages),
        estimated=not tokens,
        repo=repoid.Repo.from_json(data.get("repo")),
    )


def _each() -> Iterator[SessionInfo]:
    """Every saved session worth listing, most recently used first, read as they're needed."""
    directory = sessions_dir()
    if not directory.is_dir():
        return
    stamped = []
    for f in directory.glob("*.json"):
        with contextlib.suppress(OSError):  # deleted while listing
            stamped.append((f.stat().st_mtime, f))
    for _, f in sorted(stamped, reverse=True):
        info = read_info(f)
        if info and (info.turns or info.name):
            yield info


def list_sessions(cwd: Path | str | None = None, limit: int = 50) -> list[SessionInfo]:
    """Saved sessions, most recently used first; only those saved in exactly `cwd` if given."""
    found = (info for info in _each() if cwd is None or info.cwd == str(cwd))
    return list(itertools.islice(found, limit))


def _gone_test_run(folder: str) -> bool:
    """Saved in a temporary folder that no longer exists: a scripted test run, not a conversation to go back to."""
    path = Path(folder)
    return bool(folder) and repoid.within(path, Path(tempfile.gettempdir()).resolve()) and not path.exists()


class _Matcher:
    """Whether a session belongs to this folder: saved here, or in any checkout of the same repository."""

    def __init__(self, cwd: Path):
        self.cwd = cwd.resolve()
        self.repo = repoid.find(self.cwd)
        self.found: dict[str, repoid.Repo | None] = {}

    def __call__(self, info: SessionInfo) -> bool:
        if not info.cwd:
            return False
        saved = Path(info.cwd)
        if saved == self.cwd:
            return True
        if self.repo is None:
            return False
        if info.repo is None and saved.is_dir():  # saved by an older version: look at the folder as it is now
            if info.cwd not in self.found:
                self.found[info.cwd] = repoid.find(saved)
            other = self.found[info.cwd]
            return other is not None and self.repo.same(other)
        if info.repo is None:  # its folder is gone: whether it was inside this repository
            return repoid.within(saved, self.repo.root)
        return self.repo.same(info.repo)


def grouped(cwd: Path, everything: bool = False, elsewhere_limit: int | None = None) -> Listing:
    """This folder's sessions and the others, newest first. Unless `everything`, the others are cut to the
    newest few, and test runs from temporary folders that are gone are left out."""
    elsewhere_limit = ELSEWHERE_LIMIT if elsewhere_limit is None else elsewhere_limit
    belongs = _Matcher(cwd)
    listing = Listing([], [])
    for info in _each():
        if belongs(info):
            listing.here.append(info)
        elif not everything and _gone_test_run(info.cwd):
            listing.hidden += 1
        elif not everything and len(listing.elsewhere) >= elsewhere_limit:
            listing.more += 1
        else:
            listing.elsewhere.append(info)
    return listing


def latest(cwd: Path) -> SessionInfo | None:
    """The conversation `lcode -c` continues: the latest saved in this folder, else in its repository."""
    exact = list_sessions(cwd, limit=1)
    if exact:
        return exact[0]
    here = grouped(cwd, elsewhere_limit=0).here
    return here[0] if here else None


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
