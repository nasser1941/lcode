"""The state every tool works with: the agent, the files read and changed in the session, and the paths the
model gives, resolved."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from lcode.tool_helpers import (
    TextFormat,
    ToolError,
)

if TYPE_CHECKING:
    from lcode.agent import Agent


class ToolboxBase:
    """What Toolbox and its groups of tools (lcode.tools_edit, lcode.tools_shell) share: state, paths."""

    def __init__(self, agent: Agent):
        self.agent = agent
        self.read_mtimes: dict[str, float] = {}
        self.formats: dict[str, TextFormat] = {}  # encoding and line endings of the files read
        self.notes: list[str] = []  # the model's notes on the task (todo_write): decisions, findings, questions
        self.changed: dict[str, None] = {}  # files lcode wrote in this session, in order
        self.before: dict[str, str | None] = {}  # files changed in this request: their text before (None: new)
        self.todos: list[dict] = []
        # An editor's buffers (lcode acp): read unsaved changes and write through the editor.
        self.reader: Callable[[Path], str | None] | None = None
        self.writer: Callable[[Path, str], None] | None = None

    @property
    def console(self):
        return self.agent.console

    def resolve(self, path: str | None) -> Path:
        p = Path(os.path.expanduser(path or "."))
        return (p if p.is_absolute() else self.agent.cwd / p).resolve()

    def path(self, path: str | None, read: bool = False) -> Path:
        """Resolve a path from the model; with the sandbox on, it must be inside the project (or, for
        reading, inside a skill's folder)."""
        p = self.resolve(path)
        root = self.agent.sandbox_root()
        if read and root is not None:
            skills = self.agent.extensions().skills.values()
            if any(p == s.folder or s.folder in p.parents for s in skills):
                return p
        if read and root is not None:
            from lcode.context import outputs_dir

            if outputs_dir().resolve() in p.parents:  # full output that was cut short
                return p
        if root is not None and p != root and root not in p.parents:
            raise ToolError(f"{path} is outside the project ({root}); with the sandbox on, that's all you can use")
        return p

    def project_rel(self, p: Path) -> str:
        """A path relative to the repository's root, as permission rules and hooks see it."""
        from lcode.checkpoints import work_tree_for

        try:
            return p.resolve().relative_to(work_tree_for(self.agent.cwd)).as_posix()
        except ValueError:
            return str(p)

    def rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.agent.cwd)) or "."
        except ValueError:
            return str(p)

    def _check(self, p: Path, before: str | None, after: str) -> str:
        """Errors the language server finds that an edit introduced (lcode.lsp), for the tool result."""
        return self.agent.lsp.check_edit(p, before, after) if self.agent.lsp is not None else ""
