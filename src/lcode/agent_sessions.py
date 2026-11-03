"""Saving and loading the conversation: session files, names, and continuing where it was left."""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from lcode import (
    repoid,
    residency,
    sessions,
)
from lcode.agent_base import AgentBase
from lcode.agent_context import conversation_chars

if TYPE_CHECKING:
    pass


class Sessions(AgentBase):
    """Agent's saved sessions."""

    # -- prompt and sessions
    @staticmethod
    def new_session_id() -> str:
        return dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]

    def reset(self) -> None:
        self.summarized_size = 0
        self.messages = [{"role": "system", "content": self.system_prompt()}]
        self._mcp_prompt = ""
        self.tools.read_mtimes.clear()
        self.reflected = len(self.messages)
        self.ctx_used = len(self.messages[0]["content"]) // 3
        self.counted_chars = None

    def session_file(self) -> Path:
        return sessions.sessions_dir() / f"{self.session_id}.json"

    def has_conversation(self) -> bool:
        return any(m.get("role") == "user" for m in self.messages)

    def save(self) -> None:
        if not (self.has_conversation() or self.session_name or self.media_results):
            return  # nothing worth resuming
        if not self.session_title and not self.has_conversation() and self.media_results:
            self.session_title = " ".join(self.media_results[0]["prompt"].split())[:60]  # an image session
        self.session_title = self.session_title or sessions.title_from(self.messages)
        f = self.session_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        repo = self.workspace_repo()
        data = {
            "cwd": str(self.workspace),  # where `lcode -c` there finds it, wherever the shell moved
            # /resume lists it under "this folder" in any checkout of the same repository, wherever it moves
            **({"repo": repo.to_json()} if repo else {}),
            "context": {"used": self.next_request_tokens(), "window": self.settings.context},
            "model": self.settings.model,
            "mode": self.settings.mode,
            "image_model": self.settings.image_model,
            "video_model": self.settings.video_model,
            "media": {"results": self.media_results, "options": self.image_options.to_json(), "refs": self.image_refs},
            "name": self.session_name,
            "title": self.session_title,
            "messages": self.messages,
            "checkpoints": self.checkpoints.to_json(),
            "todos": self.tools.todos,
            "notes": self.tools.notes,
            "changed": list(self.tools.changed),
        }
        # Written next to it and then renamed: a crash halfway leaves the previous version, not a broken file.
        partial = f.with_name(f".{f.name}.partial")
        partial.write_text(json.dumps(data))
        os.replace(partial, f)

    def workspace_repo(self) -> repoid.Repo | None:
        """The repository the workspace is in, looked up again when the workspace changes or gets its first commit."""
        if self._repo is None or self._repo[0] != self.workspace or (self._repo[1] and not self._repo[1].first):
            self._repo = (self.workspace, repoid.find(self.workspace))
        return self._repo[1]

    def new_session(self) -> None:
        self.reset()
        self.skills_loaded.clear()
        self.session_id = self.new_session_id()
        self.session_name = ""
        self.session_title = ""
        self.checkpoints.reset(self.session_id)
        self.tools.todos, self.tools.notes, self.tools.changed = [], [], {}
        self.check_results = []

    def rename(self, name: str) -> None:
        self.session_name = " ".join(name.split())
        self.save()

    def load(self, info: sessions.SessionInfo) -> str:
        """Resume a saved session. Returns a note about the working directory, if it changed."""
        try:
            data = json.loads(info.path.read_text())
        except (OSError, ValueError) as e:
            raise OSError(f"can't read session {info.id}: {e}") from e
        note = ""
        saved_cwd = Path(info.cwd) if info.cwd else self.cwd
        if saved_cwd != self.cwd:
            if saved_cwd.is_dir():
                self.cwd = self.workspace = saved_cwd.resolve()
                note = f"Working directory is now {self.cwd}"
            else:
                note = f"The session's directory {saved_cwd} no longer exists; staying in {self.cwd}"
        self.messages = [{"role": "system", "content": self.system_prompt()}, *data.get("messages", [])[1:]]
        self._mcp_prompt = ""
        self.session_id = info.id
        self.checkpoints.load(info.id, data.get("checkpoints") or [])
        self.session_name = data.get("name", "")
        if data.get("mode") in ("code", "image", "video"):
            self.settings.mode = data["mode"]  # a session belongs to the mode it was made in
        if isinstance(data.get("image_model"), str):
            self.settings.image_model = data["image_model"]
        if isinstance(data.get("video_model"), str):
            self.settings.video_model = data["video_model"]
        media = data.get("media") or {}
        self.media_results = [r for r in media.get("results") or [] if isinstance(r, dict) and "path" in r]
        self.image_options = type(self.image_options).from_json(media.get("options") or {})
        self.image_refs = [r for r in media.get("refs") or [] if isinstance(r, str)]
        self.session_title = data.get("title") or sessions.title_from(self.messages)
        self.tools.read_mtimes.clear()  # files may have changed since; the model must read them again
        self.tools.todos = list(data.get("todos") or [])
        self.tools.notes = [str(n) for n in data.get("notes") or []]
        self.tools.changed = dict.fromkeys(str(f) for f in data.get("changed") or [])
        self.reflected = len(self.messages)  # checked for notes to remember when that session ended
        saved = (data.get("context") or {}).get("used") if isinstance(data.get("context"), dict) else None
        if isinstance(saved, int) and saved > 0:  # what the model counted, plus what came after
            self.ctx_used, self.counted_chars = saved, conversation_chars(self.messages)
        else:  # saved by a version before 0.52
            self.ctx_used, self.counted_chars = sessions.estimate_tokens(self.messages), None
        return note

    def load_latest(self) -> str | None:
        """Continue the latest conversation in this folder (or its repository). Returns a note about the working
        directory ("" if it stayed), or None when there's nothing to continue."""
        latest = sessions.latest(self.workspace)
        return self.load(latest) if latest else None

    def after_load(self, watch: residency.LoadWatch) -> None:
        """The first token after a load: one line on how long it took and how the model fits."""
        seconds = watch.stop()
        self.made_room_for = ""
        self.load_state = ""
        where = residency.placement(self.ollama, self.settings.model)
        dense = residency.is_moe(self.ollama, self.settings.model) is False
        report = residency.load_report(
            self.settings.model, seconds, where, self.settings.context, self.hardware, dense, watch.reload
        )
        self.say(report, warning=report.endswith(("closed.", "slow.")))
