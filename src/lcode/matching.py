"""Find where an edit goes when the model's text doesn't match the file exactly.

Small models copy text imperfectly: with the line numbers `read_file` shows, with spaces where the file has
tabs, a block at the wrong indentation, quotes still escaped, a blank line dropped, a letter wrong. An exact
match fails and the model often repeats the same failing edit. So when the exact text isn't there, lcode tries,
in order: removing line-number prefixes and stray escapes; comparing lines without trailing whitespace, without
indentation (and re-indenting the new text to fit), without blank lines; and, last, a very close match. It
only ever applies a match that is unique; anything ambiguous or weak is refused, with the closest lines and
their numbers, so the model can copy them exactly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

LINE_NUMBER = re.compile(r"^\s*\d+(?:\t|→| \| |: )")  # how read_file and other tools number lines
CLOSE_ENOUGH = 0.9  # similarity a loose match needs
CLEAR_LEAD = 0.05  # how much better than the next one it must be
MAX_SIMILARITY_LINES = 20_000  # bigger files: no similarity search
HINT_FLOOR = 0.5  # weaker candidates aren't worth showing


@dataclass
class Found:
    text: str  # the file with the edit applied
    how: str  # how the match was found, for the model ("ignoring indentation"); "" for an exact match
    start_line: int  # first matched line, 1-based


@dataclass
class Miss:
    reason: str  # "not found" or "ambiguous"
    hint: str  # the closest lines, numbered, or ""


def numbered(lines: list[str], start: int) -> str:
    return "\n".join(f"{start + i:6}\t{line.rstrip(chr(10))}" for i, line in enumerate(lines))


def strip_line_numbers(text: str) -> str | None:
    lines = text.split("\n")
    content = [line for line in lines if line.strip()]
    if not content or not all(LINE_NUMBER.match(line) for line in content):
        return None
    return "\n".join(LINE_NUMBER.sub("", line, count=1) if line.strip() else line for line in lines)


def unescape(text: str, file_text: str) -> str | None:
    """`\\"`, `\\n` and `\\t` written literally, when the file doesn't contain them literally."""
    changed = text
    for literal, real in (('\\"', '"'), ("\\'", "'"), ("\\t", "\t")):
        if literal in changed and literal not in file_text:
            changed = changed.replace(literal, real)
    if "\\n" in changed and "\n" not in changed and "\\n" not in file_text:
        changed = changed.replace("\\n", "\n")
    return changed if changed != text else None


def indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def width(indent: str) -> int:
    return len(indent.expandtabs(4))


def reindent(new: str, old_lines: list[str], file_lines: list[str]) -> str:
    """Move `new` from the old text's indentation to the file's, keeping its relative structure."""
    old_first = next((line for line in old_lines if line.strip()), "")
    file_first = next((line for line in file_lines if line.strip()), "")
    new_first = next((line for line in new.split("\n") if line.strip()), "")
    base_old, base_file = width(indent_of(old_first)), indent_of(file_first)
    same_kind = ("\t" in base_file) == ("\t" in indent_of(new_first))
    if base_old != width(base_file) and width(indent_of(new_first)) == width(base_file) and same_kind:
        return new  # the model got the old text's indentation wrong but wrote the new text in the file's
    old_steps = sorted({width(indent_of(line)) - base_old for line in old_lines if line.strip()} - {0})
    step_old = next((s for s in old_steps if s > 0), 4)
    tabs = "\t" in base_file or any(indent_of(line).startswith("\t") for line in file_lines if line.strip())
    file_steps = sorted({width(indent_of(line)) - width(base_file) for line in file_lines if line.strip()} - {0})
    unit = "\t" if tabs else " " * next((s for s in file_steps if s > 0), step_old)
    out = []
    for line in new.split("\n"):
        if not line.strip():
            out.append(line)
            continue
        levels = round((width(indent_of(line)) - base_old) / step_old)
        lead = base_file + unit * levels if levels >= 0 else base_file[: max(0, len(base_file) + levels * len(unit))]
        out.append(lead + line.lstrip(" \t"))
    return "\n".join(out)


def splice(lines: list[str], start: int, end: int, new: str) -> str:
    """Replace lines[start:end] with `new`, keeping the line break the region ended with."""
    ending = "\n" if lines[end - 1].endswith("\n") else ""
    body = new.rstrip("\n")
    return "".join(lines[:start]) + (body + ending if body else "") + "".join(lines[end:])


def line_matches(file_lines: list[str], old_lines: list[str], key) -> list[int]:
    n = len(old_lines)
    wanted = [key(line) for line in old_lines]
    return [i for i in range(len(file_lines) - n + 1) if [key(line) for line in file_lines[i : i + n]] == wanted]


def locate(text: str, old: str, new: str) -> Found | Miss:
    """Where `old` goes in `text`, when it isn't there exactly; with `new` in its place."""
    # 1. Copying mistakes that leave the text itself right.
    for fix, how in ((strip_line_numbers, "without the line numbers"), (unescape, "with the escapes undone")):
        fixed = fix(old) if fix is strip_line_numbers else unescape(old, text)
        if fixed is None:
            continue
        fixed_new = (fix(new) if fix is strip_line_numbers else unescape(new, text)) or new
        if text.count(fixed) == 1:
            start = text.index(fixed)
            return Found(text.replace(fixed, fixed_new, 1), how, text.count("\n", 0, start) + 1)
        old, new = fixed, fixed_new  # the looser steps below work on the fixed text
    file_lines = text.splitlines(keepends=True)
    old_lines = old.strip("\n").split("\n")
    if not old_lines or not any(line.strip() for line in old_lines):
        return Miss("not found", "")
    plain = [line.rstrip("\r\n") for line in file_lines]
    # 2. Line by line, looser each time.
    for key, how, indent in (
        (lambda s: s.rstrip(), "ignoring trailing spaces", False),
        (lambda s: s.strip(), "ignoring indentation", True),
    ):
        hits = line_matches(plain, old_lines, key)
        if len(hits) == 1:
            i = hits[0]
            region = plain[i : i + len(old_lines)]
            body = reindent(new.strip("\n"), old_lines, region) if indent else new.strip("\n")
            return Found(splice(file_lines, i, i + len(old_lines), body), how, i + 1)
        if len(hits) > 1:
            return Miss("ambiguous", "\n\n".join(numbered(plain[i : i + len(old_lines)], i + 1) for i in hits[:3]))
    # Without blank lines: map the non-blank lines back to the file.
    solid = [line for line in old_lines if line.strip()]
    content = [(i, line) for i, line in enumerate(plain) if line.strip()]
    keys = [line.strip() for _, line in content]
    wanted = [line.strip() for line in solid]
    hits = [j for j in range(len(keys) - len(wanted) + 1) if keys[j : j + len(wanted)] == wanted]
    first, last = (content[hits[0]][0], content[hits[0] + len(wanted) - 1][0]) if len(hits) == 1 else (0, -1)
    if len(hits) == 1 and last - first + 1 != len(old_lines):
        body = reindent(new.strip("\n"), old_lines, plain[first : last + 1])
        return Found(splice(file_lines, first, last + 1, body), "ignoring blank lines", first + 1)
    # 3. A very close match of the same number of lines, clearly better than any other. Only the lines the model
    #    changed change: the rest stays as the file has it now, so an earlier edit the model's copy doesn't show
    #    survives. Where a changed line differs in the file by more than a typo, the edit is refused.
    if len(plain) > MAX_SIMILARITY_LINES:
        return Miss("not found", "")
    n = len(old_lines)
    target = "\n".join(line.strip() for line in old_lines)
    scores = []
    for j in range(len(plain) - n + 1):
        if (old_lines[0].strip() and not plain[j].strip()) or (old_lines[-1].strip() and not plain[j + n - 1].strip()):
            continue  # a window that starts or ends on a blank line where the model's text doesn't
        window = "\n".join(line.strip() for line in plain[j : j + n])
        matcher = SequenceMatcher(None, target, window, autojunk=False)
        if matcher.real_quick_ratio() < HINT_FLOOR or matcher.quick_ratio() < HINT_FLOOR:
            continue
        scores.append((matcher.ratio(), j))
    scores.sort(reverse=True)
    if not scores or scores[0][0] < HINT_FLOOR:
        return Miss("not found", "")
    best, j = scores[0]
    runner_up = scores[1][0] if len(scores) > 1 else 0.0
    if best >= CLOSE_ENOUGH and best - runner_up >= CLEAR_LEAD:
        merged = merge(old_lines, new.strip("\n").split("\n"), plain[j : j + n])
        if merged is not None:
            body = "\n".join(merged)
            return Found(splice(file_lines, j, j + n, body), f"at a close match ({best:.0%} alike)", j + 1)
    # To show the model where to look, compare line by line: one long line doesn't outweigh the rest.
    hint = max(scores[:20], key=lambda score: line_by_line(old_lines, plain[score[1] : score[1] + n]))[1]
    return Miss("not found", numbered(plain[hint : hint + n], hint + 1))


def line_by_line(old: list[str], region: list[str]) -> float:
    pairs = list(zip(old, region, strict=False))  # the region may end early, at the end of the file
    return sum(SequenceMatcher(None, a.strip(), b.strip(), autojunk=False).ratio() for a, b in pairs) / len(pairs)


def merge(old: list[str], new: list[str], region: list[str]) -> list[str] | None:
    """Apply the model's change (old -> new) to the file's lines (`region`, aligned with `old`). None when a line
    it changes differs in the file by more than a typo: the model's copy is out of date there."""
    new = reindent("\n".join(new), old, region).split("\n")
    out: list[str] = []
    ops = SequenceMatcher(None, [line.strip() for line in old], [line.strip() for line in new], autojunk=False)
    for tag, i1, i2, j1, j2 in ops.get_opcodes():
        if tag == "equal":
            out += region[i1:i2]  # as the file has them
            continue
        for k in range(i1, i2):
            alike = SequenceMatcher(None, old[k].strip(), region[k].strip(), autojunk=False).ratio()
            if old[k].strip() != region[k].strip() and alike < CLOSE_ENOUGH:
                return None
        out += new[j1:j2]
    return out


# ----------------------------------------------------------------------------- unified diffs


@dataclass
class Hunk:
    header: str  # the @@ line, for messages
    old: str  # its context and removed lines
    new: str  # its context and added lines


@dataclass
class FilePatch:
    path: str  # without a/ and b/
    hunks: list[Hunk]
    new_file: bool = False
    deleted: bool = False


def strip_prefix(path: str) -> str:
    path = path.strip().split("\t")[0]
    return path[2:] if path[:2] in ("a/", "b/") else path


def parse_patch(patch: str) -> list[FilePatch]:
    """A unified diff (as git diff writes it) as files and hunks. The @@ line numbers aren't trusted: hunks are
    found by their text. Raises ValueError when it isn't a diff."""
    files: list[FilePatch] = []
    hunk: list[str] | None = None
    current: FilePatch | None = None

    def close() -> None:
        nonlocal hunk
        if hunk is not None and current is not None:
            old = [line[1:] for line in hunk[1:] if line[:1] in (" ", "-")]
            new = [line[1:] for line in hunk[1:] if line[:1] in (" ", "+")]
            current.hunks.append(Hunk(hunk[0], "\n".join(old), "\n".join(new)))
        hunk = None

    lines = patch.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            close()
            old_path, new_path = line[4:], lines[i + 1][4:]
            gone, born = old_path.strip() == "/dev/null", new_path.strip() == "/dev/null"
            current = FilePatch(strip_prefix(old_path if born else new_path), [], new_file=gone, deleted=born)
            files.append(current)
            i += 2
            continue
        if line.startswith("@@") and current is not None:
            close()
            hunk = [line]
        elif hunk is not None:
            if line.startswith(("\\ No newline", "diff --git", "index ")):
                pass
            elif line == "" and i == len(lines) - 1:
                pass  # the patch's own final newline
            else:
                hunk.append(line if line[:1] in (" ", "-", "+") else " " + line)  # a context line that lost its space
        i += 1
    close()
    if not files or not any(f.hunks for f in files):
        raise ValueError("no ---/+++ file headers with @@ hunks: write a unified diff, as git diff does")
    return files


def apply_hunks(text: str, hunks: list[Hunk]) -> tuple[str, list[str]]:
    """Apply hunks one after another; returns the text and how each loose match was made. Raises ValueError
    naming the hunk that can't be placed."""
    notes = []
    for n, hunk in enumerate(hunks, 1):
        if not hunk.old.strip():
            raise ValueError(f"hunk {n} ({hunk.header}) has no context lines to place it by")
        count = text.count(hunk.old)
        if count == 1:
            text = text.replace(hunk.old, hunk.new, 1)
            continue
        if count > 1:
            raise ValueError(f"hunk {n} ({hunk.header}) matches {count} places: add more context lines")
        found = locate(text, hunk.old, hunk.new)
        if isinstance(found, Miss):
            where = f" The closest text is:\n{found.hint}" if found.hint else ""
            raise ValueError(f"hunk {n} ({hunk.header}) doesn't match the file.{where}")
        text = found.text
        notes.append(f"hunk {n} {found.how}")
    return text, notes
