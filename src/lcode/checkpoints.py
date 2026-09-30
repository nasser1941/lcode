"""Automatic checkpoints, so /undo can take back what the model changed.

Before the first change in a request (a file edit, or a shell command that isn't read-only), lcode
snapshots the project into a private git repository in the state directory, and snapshots it again
when the request ends. /undo puts the files that request changed back the way they were.

The private repository is separate from the user's own: their index, HEAD, branches, stash and
objects are never touched. It covers every file that isn't ignored, including changes made by shell
commands, and works in folders that aren't git repositories.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import shutil
import stat
import subprocess
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from lcode import config

KEEP_DAYS = 14  # checkpoints of older sessions are deleted
MAX_FILES = 20_000  # larger folders aren't checkpointed
MAX_TOTAL_BYTES = 1024**3
MAX_FILE_BYTES = 10 * 1024**2  # larger files are left out of checkpoints
GIT_TIMEOUT = 120
LOCK_RETRIES = 20
TITLE_LENGTH = 70
DEFAULT_EXCLUDES = """\
# Generated folders, left out even where no .gitignore mentions them
node_modules/
.venv/
venv/
__pycache__/
*.pyc
.mypy_cache/
.pytest_cache/
.ruff_cache/
.tox/
.gradle/
.next/
.DS_Store
"""
GIT_CONFIG = (
    "core.autocrlf=false",
    "core.safecrlf=false",
    "core.quotepath=false",
    "core.fsmonitor=false",
    "commit.gpgsign=false",
    "advice.addEmbeddedRepo=false",
    "gc.auto=0",
)
IDENTITY = {
    "GIT_AUTHOR_NAME": "lcode",
    "GIT_AUTHOR_EMAIL": "lcode@localhost",
    "GIT_COMMITTER_NAME": "lcode",
    "GIT_COMMITTER_EMAIL": "lcode@localhost",
}


class CheckpointError(Exception):
    pass


@dataclass
class Checkpoint:
    n: int
    request: str  # the start of the request, for display and for finding it in the conversation
    worktree: str
    before: str  # commit with the files as they were before the request
    after: str = ""  # commit with the files after it; empty if lcode stopped before recording it
    message_index: int = 0  # where the request starts in the conversation
    created: float = 0.0
    changes: list[dict] = field(default_factory=list)  # [{"status": "A" | "M" | "D", "path": ...}]
    undone: bool = False

    @property
    def title(self) -> str:
        text = " ".join(self.request.split())
        return text if len(text) <= TITLE_LENGTH else text[: TITLE_LENGTH - 1].rstrip() + "…"

    @property
    def paths(self) -> list[str]:
        return [c["path"] for c in self.changes]

    @classmethod
    def from_dict(cls, data: dict) -> Checkpoint:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Restore:
    """What /undo or /rewind will do: which checkpoints it reverts and which files it touches."""

    target: Checkpoint  # files go back to how they were before this request
    undoing: list[Checkpoint]
    restore: list[str]  # files to put back (modified or deleted since)
    remove: list[str]  # files the requests created
    conflicts: list[str]  # files that changed again after lcode changed them
    current: str  # tree with the files as they are now


def stores_dir() -> Path:
    return config.STATE_DIR / "checkpoints"


def work_tree_for(cwd: Path) -> Path:
    """The folder a checkpoint covers: the enclosing git repository, or the folder itself."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            env=_clean_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return cwd.resolve()
    top = r.stdout.strip()
    return Path(top).resolve() if r.returncode == 0 and top else cwd.resolve()


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _exclude_pattern(path: str) -> str:
    """An exclude line matching exactly this path (relative to the work tree)."""
    escaped = "".join("\\" + ch if ch in "\\*?[" else ch for ch in path)
    if escaped.endswith(" "):
        escaped = escaped[:-1] + "\\ "
    return "/" + escaped


class Store:
    """A private git repository with the snapshots of one work tree."""

    def __init__(self, worktree: Path):
        self.worktree = worktree
        key = hashlib.sha256(str(worktree).encode()).hexdigest()[:16]
        self.git_dir = stores_dir() / f"{key}.git"

    def git(self, *args: str, stdin: bytes | None = None, check: bool = True, timeout: int = GIT_TIMEOUT) -> str:
        env = _clean_env()
        env.update(IDENTITY, GIT_DIR=str(self.git_dir), GIT_WORK_TREE=str(self.worktree), GIT_LITERAL_PATHSPECS="1")
        cmd = ["git"]
        for option in GIT_CONFIG:
            cmd += ["-c", option]
        cmd += args
        for attempt in range(LOCK_RETRIES):
            try:
                r = subprocess.run(cmd, cwd=self.worktree, env=env, input=stdin, capture_output=True, timeout=timeout)
            except FileNotFoundError as e:
                raise CheckpointError("git isn't installed") from e
            except subprocess.TimeoutExpired as e:
                raise CheckpointError(f"git {args[0]} took longer than {timeout}s") from e
            except OSError as e:
                raise CheckpointError(str(e)) from e
            err = r.stderr.decode(errors="replace").strip()
            if r.returncode != 0 and ".lock" in err:
                if attempt < LOCK_RETRIES - 1:
                    time.sleep(0.25)  # another lcode session in the same folder is saving a checkpoint
                    continue
                raise CheckpointError(err.splitlines()[-1])
            if check and r.returncode != 0:
                raise CheckpointError(err.splitlines()[-1] if err else f"git {args[0]} failed")
            return r.stdout.decode("utf-8", "surrogateescape")
        raise AssertionError("unreachable")

    def ensure(self) -> None:
        if (self.git_dir / "HEAD").exists():
            return
        home = Path.home().resolve()
        if self.worktree == home or self.worktree in home.parents:
            raise CheckpointError(f"{self.worktree} is your home folder or above it; start lcode in a project folder")
        self.git_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            r = subprocess.run(
                ["git", "init", "-q", "--bare", str(self.git_dir)], capture_output=True, env=_clean_env(), timeout=30
            )
        except FileNotFoundError as e:
            raise CheckpointError("git isn't installed") from e
        if r.returncode != 0:
            raise CheckpointError(r.stderr.decode(errors="replace").strip() or "git init failed")
        excludes = DEFAULT_EXCLUDES
        own = self.worktree / ".git" / "info" / "exclude"  # the project's private ignore rules
        if own.is_file():
            excludes += "\n" + own.read_text(errors="replace")
        (self.git_dir / "info").mkdir(exist_ok=True)
        (self.git_dir / "info" / "exclude").write_text(excludes)
        (self.git_dir / "lcode-worktree").write_text(str(self.worktree) + "\n")

    def _candidates(self, limit: int | None = None) -> list[str]:
        """Files that are new or changed since the last snapshot (every file, for the first one)."""
        out = self.git("ls-files", "-z", "--others", "--modified", "--exclude-standard", timeout=60)
        paths = [p for p in out.split("\0") if p]
        if limit is not None and len(paths) > limit:
            raise CheckpointError(f"{self.worktree} has more than {limit:,} files")
        return paths

    def _skip_large_files(self, first: bool) -> None:
        large, total = [], 0
        for rel in self._candidates(MAX_FILES if first else None):
            try:
                st = (self.worktree / rel).lstat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if st.st_size > MAX_FILE_BYTES:
                large.append(rel)
            else:
                total += st.st_size
        if first and total > MAX_TOTAL_BYTES:
            raise CheckpointError(f"{self.worktree} has more than {MAX_TOTAL_BYTES // 1024**3} GB of files")
        if large:
            with (self.git_dir / "info" / "exclude").open("a") as f:
                f.write("".join(_exclude_pattern(rel) + "\n" for rel in large))
            self.git("rm", "-q", "-r", "--cached", "--ignore-unmatch", "--", *large)

    def tree(self) -> str:
        """Record the files as they are now; returns the tree id."""
        self.ensure()
        first = not (self.git_dir / "index").exists()
        self._skip_large_files(first)
        self.git("add", "-A", "--ignore-errors", check=False)  # unreadable files are skipped
        (self.git_dir / "lcode-last-used").touch()
        return self.git("write-tree").strip()

    def commit(self, tree: str, message: str, parent: str = "") -> str:
        return self.git("commit-tree", tree, "-m", message, *(["-p", parent] if parent else [])).strip()

    def tree_of(self, commit: str) -> str:
        return self.git("rev-parse", f"{commit}^{{tree}}").strip()

    def changes(self, a: str, b: str, paths: list[str] | None = None) -> list[dict]:
        """Files that differ between two commits or trees: status A (only in b), D (only in a) or M."""
        args = ["diff", "--name-status", "--no-renames", "--no-ext-diff", "-z", a, b]
        if paths is not None and not paths:
            return []
        if paths is not None and len(paths) <= 500:
            args += ["--", *paths]
        parts = self.git(*args).split("\0")
        wanted = set(paths) if paths is not None else None
        out = []
        for status, path in zip(parts[0::2], parts[1::2], strict=False):
            if status and path and (wanted is None or path in wanted):
                out.append({"status": status[0] if status[0] in "AD" else "M", "path": path})
        return out

    def restore_files(self, commit: str, paths: list[str]) -> None:
        for i in range(0, len(paths), 200):  # works with any git version, unlike `git restore`
            self.git("checkout", commit, "--", *paths[i : i + 200])

    def remove_files(self, paths: list[str]) -> None:
        for rel in paths:
            p = self.worktree / rel
            if p.is_symlink() or p.is_file():
                p.unlink()
            parent = p.parent
            while parent != self.worktree and self.worktree in parent.parents:
                try:
                    parent.rmdir()  # only succeeds while empty
                except OSError:
                    break
                parent = parent.parent

    def set_ref(self, name: str, commit: str) -> None:
        self.git("update-ref", name, commit)

    def delete_ref(self, name: str) -> None:
        self.git("update-ref", "-d", name, check=False)

    def prune(self, keep_days: int = KEEP_DAYS) -> None:
        """Delete the checkpoints of sessions older than keep_days."""
        if not (self.git_dir / "HEAD").exists():
            return
        cutoff = dt.datetime.now() - dt.timedelta(days=keep_days)
        refs = self.git("for-each-ref", "--format=%(refname)", "refs/lcode/").split()
        old = [ref for ref in refs if _session_time(ref.split("/")[2]) < cutoff]
        for ref in old:
            self.delete_ref(ref)
        if old:
            self.git("gc", "-q", "--prune=now", check=False, timeout=600)


def _session_time(session_id: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(session_id[:15], "%Y%m%d-%H%M%S")
    except ValueError:
        return dt.datetime.min


def prune_stores(keep_days: int = KEEP_DAYS) -> None:
    """Delete the checkpoint stores of folders lcode hasn't worked in for keep_days."""
    cutoff = time.time() - keep_days * 86400
    root = stores_dir()
    if not root.is_dir():
        return
    for store in root.glob("*.git"):
        marker = store / "lcode-last-used"
        try:
            last = marker.stat().st_mtime if marker.exists() else store.stat().st_mtime
        except OSError:
            continue
        if last < cutoff:
            shutil.rmtree(store, ignore_errors=True)


class Checkpoints:
    """The checkpoints of one session."""

    def __init__(self, console: Console, enabled: bool = True):
        self.console = console
        self.enabled = enabled
        self.items: list[Checkpoint] = []
        self.session_id = ""
        self._pending: tuple[str, int] | None = None  # (request, message index) of the running request
        self._current: Checkpoint | None = None
        self._before_tree = ""
        self._disabled: dict[str, str] = {}  # work tree -> why checkpoints are off there
        self._pruned: set[str] = set()

    # -- recording
    def begin_turn(self, session_id: str, request: str, message_index: int) -> None:
        self.session_id = session_id
        self._pending = (request[:500], message_index)
        self._current = None

    def before_change(self, cwd: Path) -> None:
        """Snapshot the files before the first change of the running request."""
        if not self.enabled or self._pending is None or self._current is not None:
            return
        worktree = work_tree_for(cwd)
        if str(worktree) in self._disabled:
            return
        store = Store(worktree)
        request, index = self._pending
        n = (self.items[-1].n if self.items else 0) + 1
        try:
            if str(worktree) not in self._pruned:
                self._pruned.add(str(worktree))
                prune_stores()
                store.prune()
            self._before_tree = store.tree()
            before = store.commit(self._before_tree, f"lcode checkpoint {n} (before): {request[:200]}")
            store.set_ref(self.ref(n), before)
        except CheckpointError as e:
            self._disabled[str(worktree)] = str(e)
            self.console.print(f"[yellow]Checkpoints are off in {escape(str(worktree))}: {escape(str(e))}.[/]")
            self.console.print("[yellow]/undo can't take back changes made there.[/]")
            return
        self._current = Checkpoint(
            n=n, request=request, worktree=str(worktree), before=before, message_index=index, created=time.time()
        )

    def end_turn(self) -> None:
        """Snapshot the files after the request and keep the checkpoint if anything changed."""
        cp, self._current, self._pending = self._current, None, None
        if cp is None:
            return
        store = Store(Path(cp.worktree))
        try:
            after_tree = store.tree()
            if after_tree == self._before_tree:
                store.delete_ref(self.ref(cp.n))  # nothing changed; nothing to undo
                return
            cp.after = store.commit(after_tree, f"lcode checkpoint {cp.n} (after): {cp.request[:200]}", cp.before)
            store.set_ref(self.ref(cp.n), cp.after)
            cp.changes = store.changes(cp.before, cp.after)
        except CheckpointError as e:
            self.console.print(f"[yellow]Couldn't record the files after this request: {escape(str(e))}[/]")
        self.items.append(cp)
        if cp.changes:
            count = len(cp.changes)
            self.console.print(
                f"[dim]↶ Checkpoint {cp.n}: {count} file{'' if count == 1 else 's'} changed · /undo reverts them[/]"
            )

    def ref(self, n: int) -> str:
        return f"refs/lcode/{self.session_id}/{n}"

    # -- sessions
    def to_json(self) -> list[dict]:
        return [asdict(cp) for cp in self.items]

    def load(self, session_id: str, data: list[dict]) -> None:
        self.session_id = session_id
        self.items = [Checkpoint.from_dict(d) for d in data if isinstance(d, dict)]

    def reset(self, session_id: str) -> None:
        self.session_id = session_id
        self.items = []

    # -- going back
    def active(self) -> list[Checkpoint]:
        return [cp for cp in self.items if not cp.undone]

    def find(self, n: int) -> Checkpoint | None:
        return next((cp for cp in self.items if cp.n == n), None)

    def plan(self, target: Checkpoint) -> Restore:
        """Work out how to put the files back to how they were before `target`'s request."""
        undoing = [cp for cp in self.active() if cp.n >= target.n and cp.worktree == target.worktree]
        store = Store(Path(target.worktree))
        current = store.tree()
        latest: dict[str, str] = {}  # path -> commit with lcode's last version of it
        for cp in undoing:
            if not cp.after:  # lcode stopped before recording the result; compare with the files now
                cp.changes = store.changes(cp.before, current)
            for path in cp.paths:
                latest[path] = cp.after or current
        paths = sorted(latest)
        conflicts: list[str] = []
        for commit in set(latest.values()):
            group = [p for p in paths if latest[p] == commit]
            conflicts += [c["path"] for c in store.changes(commit, current, group)]
        diff = store.changes(target.before, current, paths)
        return Restore(
            target=target,
            undoing=undoing,
            restore=[c["path"] for c in diff if c["status"] != "A"],
            remove=[c["path"] for c in diff if c["status"] == "A"],
            conflicts=sorted(conflicts),
            current=current,
        )

    def apply(self, plan: Restore) -> None:
        store = Store(Path(plan.target.worktree))
        n = plan.target.n
        # Keep the files as they are now too, in case the undo itself needs undoing by hand.
        store.set_ref(f"{self.ref(n)}-undo", store.commit(plan.current, f"lcode: files before undoing {n}"))
        store.restore_files(plan.target.before, plan.restore)
        store.remove_files(plan.remove)
        for cp in plan.undoing:
            cp.undone = True

    def absolute_paths(self, plan: Restore) -> list[str]:
        root = Path(plan.target.worktree)
        return [str((root / p).resolve()) for p in [*plan.restore, *plan.remove]]
