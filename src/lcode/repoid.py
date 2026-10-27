"""Which git repository a folder belongs to, so saved conversations and notes follow a repository that was
moved, renamed or cloned again.

A repository is known by its first commit, which every clone and worktree of it shares wherever it lives,
plus its git directory (shared by its worktrees) and the top of its working tree.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Repo:
    root: Path  # the top of the working tree
    common: Path  # the git directory, the same for every worktree of the repository
    first: str = ""  # the first commit's hash; empty in a repository without commits

    def same(self, other: Repo) -> bool:
        """Two checkouts of one repository: the same first commit, git directory or working tree."""
        return bool(self.first and self.first == other.first) or self.common == other.common or self.root == other.root

    def to_json(self) -> dict:
        return {"root": str(self.root), "common": str(self.common), "first": self.first}

    @classmethod
    def from_json(cls, data: object) -> Repo | None:
        if not isinstance(data, dict) or not data.get("root") or not data.get("common"):
            return None
        return cls(Path(str(data["root"])), Path(str(data["common"])), str(data.get("first") or ""))


def _git(folder: Path, *args: str) -> str | None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # a hook's GIT_DIR would point elsewhere
    try:
        r = subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def first_commit(folder: Path) -> str:
    """The repository's first commit (the oldest root, for one with several); empty without commits."""
    out = _git(folder, "rev-list", "--max-parents=0", "HEAD")
    return sorted(out.split())[0] if out else ""


def find(folder: Path | str) -> Repo | None:
    """The repository around `folder`, or None outside git (or when the folder no longer exists)."""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    out = _git(folder, "rev-parse", "--show-toplevel", "--git-common-dir")
    lines = out.splitlines() if out else []
    if len(lines) != 2:
        return None
    return Repo(Path(lines[0]).resolve(), (folder / lines[1]).resolve(), first_commit(folder))


def within(path: Path, folder: Path) -> bool:
    """`path` is `folder` or somewhere inside it."""
    return path == folder or folder in path.parents
