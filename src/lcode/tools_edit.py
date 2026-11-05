"""The tools that change files: write_file, edit_file and apply_patch, with the checks that keep them safe."""

from __future__ import annotations

import difflib
from pathlib import Path

from rich.syntax import Syntax
from rich.text import Text

from lcode import matching
from lcode.tool_helpers import IGNORE_DIRS, TextFormat, ToolError, decode, encode, walk_files
from lcode.tools_base import ToolboxBase


class FileChanges(ToolboxBase):
    """Toolbox's tools that change files."""

    # -- file changes
    def _did_you_mean(self, path: str) -> str:
        """Existing paths close to one that doesn't exist: the same name elsewhere in the project, or a near name
        in the nearest folder that exists. Saves the model a search, or a guess."""
        wanted = self.resolve(path)
        found: list[Path] = []
        from lcode.checkpoints import work_tree_for

        for i, candidate in enumerate(walk_files(work_tree_for(self.agent.cwd))):
            if i > 20_000 or len(found) >= 3:
                break
            if candidate.name == wanted.name:
                found.append(candidate)
        folder = wanted.parent
        while not folder.is_dir() and folder != folder.parent:
            folder = folder.parent
        try:
            names = [e.name + ("/" if e.is_dir() else "") for e in folder.iterdir() if e.name not in IGNORE_DIRS]
        except OSError:
            names = []
        for name in difflib.get_close_matches(wanted.name, names, n=3, cutoff=0.6):
            found.append(folder / name.rstrip("/"))
        shown = list(dict.fromkeys(self.rel(p) + ("/" if p.is_dir() else "") for p in found))[:3]
        return f" Did you mean: {', '.join(shown)}?" if shown else ""

    def _check_fresh(self, p: Path) -> None:
        key = str(p)
        if key not in self.read_mtimes:
            raise ToolError(f"You must read_file {self.rel(p)} before modifying it.")
        if p.stat().st_mtime > self.read_mtimes[key] + 1e-6:
            raise ToolError(f"{self.rel(p)} changed on disk since you read it. Read it again first.")

    def _diff(self, p: Path, old: str, new: str) -> Syntax:
        diff = "".join(
            difflib.unified_diff(
                old.splitlines(True), new.splitlines(True), f"a/{self.rel(p)}", f"b/{self.rel(p)}", n=3
            )
        )
        if len(diff) > 12_000:
            diff = diff[:12_000] + "\n... (diff truncated for display)\n"
        return Syntax(diff or "(no changes)", "diff", theme="monokai", word_wrap=True)

    def read_text(self, p: Path) -> str:
        """A file's text: the editor's buffer when lcode runs inside one, else the file on disk."""
        if self.reader is not None:
            text = self.reader(p)
            if text is not None:
                return text
        text, self.formats[str(p)] = decode(p.read_bytes())
        return text

    def _write(self, p: Path, content: str) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        fmt = self.formats.get(str(p)) or (decode(p.read_bytes())[1] if p.exists() else TextFormat())
        data = encode(content, fmt)  # before anything is written: it may not be possible
        if self.rel(p) not in self.before:
            self.before[self.rel(p)] = decode(p.read_bytes())[0] if p.exists() else None
        if self.writer is not None:
            self.writer(p, content)  # the editor updates its buffer (and the file)
        if not p.exists() or p.read_bytes() != data:
            p.write_bytes(data)  # the file on disk must match, for checkpoints and the tools
        self.read_mtimes[str(p)] = p.stat().st_mtime
        self.changed[self.rel(p)] = None
        self.wrote.append(self.rel(p))

    def t_write_file(self, path: str, content: str) -> str:
        p = self.path(path)
        exists = p.exists()
        if exists:
            if p.is_dir():
                raise ToolError(f"{path} is a directory")
            self._check_fresh(p)
            shown = self.read_text(p)
            body = self._diff(p, shown, content)
        else:
            shown = None
            lines = content.splitlines()
            preview = "\n".join(lines[:60]) + (f"\n... ({len(lines) - 60} more lines)" if len(lines) > 60 else "")
            body = Syntax(preview, Syntax.guess_lexer(str(p), content), theme="monokai", line_numbers=True)
        verb = "Overwrite" if exists else "Create"
        ok, feedback = self.agent.perms.request("edit", "edit", f"{verb} {self.rel(p)}", body, self.project_rel(p))
        if not ok:
            return feedback
        before = self.read_text(p) if p.exists() else None
        if before != shown:
            return self._changed_meanwhile(p)
        self.agent.checkpoint()
        self._write(p, content)
        n = len(content.splitlines())
        self.console.print(f"  [green]✓[/] {'Updated' if exists else 'Created'} {self.rel(p)} ({n} lines)")
        return f"{'Overwrote' if exists else 'Created'} {self.rel(p)} ({n} lines)." + self._check(p, before, content)

    def _changed_meanwhile(self, p: Path) -> str:
        """The file changed while the user was asked (they edited it): writing now would undo their change."""
        self.read_mtimes.pop(str(p), None)
        self.console.print(
            Text(f"  ⎿ {self.rel(p)} changed while you were asked; the edit wasn't applied", style="yellow")
        )
        return (
            f"Error: {self.rel(p)} changed while the user was being asked (they or another program edited it), so "
            "lcode didn't apply this change: it would have undone theirs. Read the file again and redo the change "
            "on its current content."
        )

    def t_apply_patch(self, patch: str) -> str:
        try:
            files = matching.parse_patch(patch)
        except ValueError as e:
            raise ToolError(str(e)) from e
        planned: list[tuple[Path, str | None, str, list[str]]] = []  # path, before, after, notes
        for fp in files:
            p = self.path(fp.path)
            if fp.deleted:
                raise ToolError(f"{fp.path}: deleting files isn't something apply_patch does; use bash (rm)")
            if fp.new_file:
                if p.exists():
                    raise ToolError(f"{fp.path} already exists; the patch creates it from /dev/null")
                planned.append((p, None, "\n".join(h.new for h in fp.hunks) + "\n", []))
                continue
            if not p.is_file():
                raise ToolError(f"{fp.path} does not exist (use --- /dev/null to create it)")
            self._check_fresh(p)
            before = self.read_text(p)
            try:
                after, notes = matching.apply_hunks(before, fp.hunks)
            except ValueError as e:
                raise ToolError(f"{fp.path}: {e}") from e
            planned.append((p, before, after, notes))
        done = []
        for p, before, after, notes in planned:
            body = self._diff(p, before or "", after)
            verb = "Create" if before is None else "Patch"
            ok, feedback = self.agent.perms.request("edit", "edit", f"{verb} {self.rel(p)}", body, self.project_rel(p))
            if not ok:
                return feedback + (f" (already patched: {', '.join(done)})" if done else "")
            if (self.read_text(p) if p.exists() else None) != before:
                return self._changed_meanwhile(p)
            self.agent.checkpoint()
            self._write(p, after)
            self.console.print(f"  [green]✓[/] {'Created' if before is None else 'Patched'} {self.rel(p)}")
            done.append(self.rel(p) + (f" ({'; '.join(notes)})" if notes else ""))
            problems = self._check(p, before, after)
            if problems:
                done[-1] += problems
        return "Patched " + ", ".join(done) + "."

    def _edit_miss(self, p: Path, miss: matching.Miss) -> str:
        """Why an edit can't be placed, with the closest lines so the model can copy them exactly."""
        if miss.reason == "ambiguous":
            return (
                f"old_string isn't in {self.rel(p)} exactly, and it's close to more than one place:\n{miss.hint}\n"
                "Add surrounding lines to old_string so it matches only one, copied exactly."
            )
        message = f"old_string not found in {self.rel(p)}. It must match the file exactly, including indentation."
        if miss.hint:
            return (
                message
                + f" The closest text is:\n{miss.hint}\nCopy what you mean from it exactly (without the line numbers)."
            )
        return message + " Re-read the file and copy the text precisely (without line-number prefixes)."

    def t_edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        p = self.path(path)
        if not p.exists():
            raise ToolError(f"{path} does not exist (use write_file to create it)")
        self._check_fresh(p)
        if old_string == new_string:
            raise ToolError("old_string and new_string are identical")
        if not old_string:
            raise ToolError("old_string is empty; use write_file to create or overwrite files")
        text = self.read_text(p)
        count = text.count(old_string)
        how, start_line = "", 0
        if count == 0:
            found = matching.locate(text, old_string, new_string)
            if isinstance(found, matching.Miss):
                raise ToolError(self._edit_miss(p, found))
            new_text, how, start_line, count = found.text, found.how, found.start_line, 1
        elif count > 1 and not replace_all:
            raise ToolError(
                f"old_string occurs {count} times in {self.rel(p)}. Add surrounding lines to make it unique, "
                "or set replace_all=true."
            )
        else:
            new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        diff = self._diff(p, text, new_text)
        ok, feedback = self.agent.perms.request("edit", "edit", f"Edit {self.rel(p)}", diff, self.project_rel(p))
        if not ok:
            return feedback
        if not p.exists() or self.read_text(p) != text:
            return self._changed_meanwhile(p)
        self.agent.checkpoint()
        self._write(p, new_text)
        self.console.print(f"  [green]✓[/] Edited {self.rel(p)}")
        problems = self._check(p, text, new_text)
        done = f"Edited {self.rel(p)} ({count} replacement(s)" + (f", {how}" if how else "") + ")."
        idx = new_text.find(new_string) if new_string and not how else -1
        if idx < 0 and not start_line:
            return done + problems
        # Show the model the edited region so it can verify the result.
        start = new_text.count("\n", 0, idx) + 1 if idx >= 0 else start_line
        lines = new_text.splitlines()
        lo, hi = max(1, start - 3), min(len(lines), start + new_string.count("\n") + 3)
        snippet = "\n".join(f"{i:6}\t{lines[i - 1]}" for i in range(lo, hi + 1))
        return f"{done} Result:\n{snippet}" + problems
