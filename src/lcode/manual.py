"""lcode's own manual, for questions about lcode: "how do I make a 25-second clip from a start and an end picture?"

The docs (docs/*.md, the pages of https://nasser1941.github.io/lcode/) ship inside the package as lcode/docs, so
answers match the installed version and work offline. A question is answered in three steps:

1. **Search:** the pages are cut into small pieces (a paragraph, a list item, a few rows of a table), each under its
   headings, and ranked against the question with BM25. Both sides are reduced to word stems first, with a few
   synonyms folded together (picture = image, clip = video, first = start…).
2. **Answer:** a language model writes what to type in lcode from the best pieces: the coding model in code mode, the
   prompt assistant's small model in image and video mode (loaded only for the answer). Without a model, the pieces
   themselves are the answer.
3. **Check:** every slash command and --option in the answer must exist in lcode (a built-in command, or one the
   manual names). An answer with one that doesn't is asked for again once, then marked.

In code mode the coding model gets the search as a tool too (lcode_help).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DOCS_URL = "https://nasser1941.github.io/lcode/"
SKIPPED = {"changelog.md", "contributing.md"}  # the repository's own files, included: not how-to
PIECE_WORDS = 110  # a piece of the manual: a few list items or paragraphs
EXCERPT_CHARS = 7000  # what the answering model reads (~2K tokens)
MAX_PIECES = 8
HELP_CONTEXT = 8192  # the small model's context for an answer: the excerpts, the question and the answer

STOP = frozenset((
    "a", "an", "and", "any", "are", "as", "at", "be", "been", "but", "by", "can", "could", "do", "does", "did",
    "doing", "for", "from", "get", "got", "go", "going", "have", "has", "how", "i", "if", "in", "into", "is", "it",
    "its", "just", "me", "my", "need", "of", "on", "or", "out", "over", "so", "some", "than", "that", "the", "their",
    "them", "then", "there", "these", "this", "those", "to", "too", "up", "use", "using", "us", "want", "was", "way",
    "we", "what", "when", "where", "which", "who", "why", "will", "with", "would", "you", "your", "lcode", "please",
    "tell", "show", "help", "know", "possible", "thing", "something",
))  # fmt: skip
# Words that mean the same here, folded into one
SYNONYMS = {
    **dict.fromkeys(("picture", "photo", "pic", "img", "png", "jpg", "jpeg", "webp"), "image"),
    **dict.fromkeys(("clip", "mp4", "footage"), "video"),
    **dict.fromkeys(("movie", "storyboard"), "film"),
    **dict.fromkeys(("create", "generate", "produce", "render", "draw", "made"), "make"),
    **dict.fromkeys(("long", "longer", "length", "duration", "second", "sec"), "seconds"),
    **dict.fromkeys(("first", "begin", "beginning", "initial", "opening"), "start"),
    **dict.fromkeys(("last", "final", "ending", "finish", "closing"), "end"),
    **dict.fromkeys(("ref",), "reference"),
    **dict.fromkeys(("bigger", "larger", "enlarge", "resolution", "upscaling", "sharper"), "upscale"),
    **dict.fromkeys(("transparent", "cutout"), "nobg"),
    **dict.fromkeys(("audio", "music", "soundtrack"), "sound"),
    **dict.fromkeys(("smoother", "interpolate", "fps", "framerate"), "smooth"),
    **dict.fromkeys(("saved", "stored", "folder", "directory", "location"), "save"),
    **dict.fromkeys(("shape", "aspect", "landscape", "portrait"), "ratio"),
    **dict.fromkeys(("several", "multiple", "many", "count"), "number"),
}
CANONICAL = {**SYNONYMS, **{word: word for word in SYNONYMS.values()}}
WORD = re.compile(r"\d+:\d+|--?[a-z][a-z0-9-]*|/[a-z][a-z-]*|[a-z0-9]+")
# A slash command in an answer (/film, not a/b, ~/folder or a URL), and an --option
COMMAND = re.compile(r"(?<![\w/.~:@-])/([a-z][a-z-]*)\b(?![/\w]|\.\w)")
OPTION = re.compile(r"(?<![\w-])--([a-z](?:[a-z0-9-]*[a-z0-9])?)\b")
HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
LIST_ITEM = re.compile(r"^([-*+]|\d+\.)\s+")
ATTRIBUTES = re.compile(r"\{\s*[.#][^}]*\}")  # MkDocs' { .class } after a picture or a heading


def unsuffixed(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3 and not word.endswith(("ss", "us", "is")):
            return word[: -len(suffix)]
    return word


def stem(word: str) -> str:
    """A rough stem, the same for a word's forms: saves, saved, save -> sav."""
    word = unsuffixed(word)
    return word[:-1] if word.endswith("e") and len(word) > 3 else word


def term(word: str) -> str:
    """A word as a search term: its synonyms' word (picture, pictures, making -> image, image, make), or its stem."""
    base = unsuffixed(word)
    for form in (word, base, base + "e", word[:-1] if word.endswith("s") else word):
        if form in CANONICAL:
            return CANONICAL[form]
    return stem(word)


def words(text: str) -> list[str]:
    """The words of a question or a piece, as search terms: stems, synonyms folded, without numbers or stop
    words. /commands and --options count as themselves and as their word."""
    found = []
    for token in WORD.findall(text.lower()):
        if token.isdigit():
            continue
        if ":" in token:  # 16:9
            found.append("ratio")
            continue
        if token[0] in "/-":
            found.append(token)
            token = token.lstrip("/-")
        if token in STOP or len(token) < 2:
            continue
        found.append(term(token))
    return found


def slug(title: str) -> str:
    """The anchor MkDocs gives a heading (Python-Markdown's toc slugify)."""
    value = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(r"[-\s]+", "-", value)


def plain(markdown: str) -> str:
    """A heading's text without Markdown: links, code marks, emphasis and attribute lists."""
    text = ATTRIBUTES.sub("", markdown)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"[`*_]", "", text).strip()


@dataclass
class Piece:
    """A few paragraphs, list items or table rows of the manual, under their headings."""

    page: str  # the file, e.g. modes.md
    path: tuple[str, ...]  # the headings, from the page's title down
    anchor: str
    text: str
    order: int = 0
    terms: Counter = field(default_factory=Counter, repr=False)

    @property
    def heading(self) -> str:
        return " › ".join(self.path)

    @property
    def url(self) -> str:
        page = self.page[:-3] if self.page.endswith(".md") else self.page
        base = DOCS_URL if page in ("index", "") else f"{DOCS_URL}{page}/"
        return base + (f"#{self.anchor}" if self.anchor else "")


def folder() -> Path | None:
    """Where the manual is: inside the installed package, or the docs of a checkout (an editable install)."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "docs", here.parent.parent / "docs"):
        if candidate.is_dir() and any(candidate.glob("*.md")):
            return candidate
    return None


def blocks(lines: list[str]) -> list[str]:
    """A section's lines as blocks: a paragraph, a list item with its continuation, a code block, or one table row
    (with the table's header in front)."""
    out: list[str] = []
    current: list[str] = []
    header: list[str] = []
    in_code = False
    in_item = False

    def close() -> None:
        nonlocal current, in_item
        if current and any(line.strip() for line in current):
            out.append("\n".join(current).strip("\n"))
        current, in_item = [], False

    for line in lines:
        if in_code:
            current.append(line)
            if FENCE.match(line):
                in_code = False
                if not in_item:
                    close()
            continue
        if FENCE.match(line):
            if not in_item or not line.startswith((" ", "\t")):
                close()
            current.append(line)
            in_code = True
            continue
        stripped = line.strip()
        if stripped.startswith("|"):  # a table: each row a block, under the table's header
            if not header:
                close()
                header = [stripped]
            elif len(header) == 1 and re.match(r"^\|[\s:|-]+\|$", stripped):
                header.append(stripped)
            else:
                out.append("\n".join([*header, stripped]))
            continue
        header = []
        if not stripped:
            if not in_item:
                close()
            else:
                current.append("")
            continue
        if stripped.startswith(("![", "<", "--8<--")) and not stripped.startswith("<kbd"):
            continue  # pictures and page layout
        if LIST_ITEM.match(line):  # a top-level item starts a block of its own
            close()
            in_item = True
            current.append(line)
            continue
        if in_item and current and current[-1] == "" and not line.startswith((" ", "\t")):
            close()  # a paragraph after the list
        if stripped.startswith(("!!!", "???")):  # an admonition: its title, then its indented text
            close()
            title = re.findall(r'"([^"]*)"', stripped)
            current.append(f"**{title[0]}:**" if title else "")
            in_item = True
            continue
        current.append(line)
    close()
    return [ATTRIBUTES.sub("", b) for b in out]


def pieces(page: str, text: str, start: int = 0) -> list[Piece]:
    """A page cut into pieces of about PIECE_WORDS words, each within one section."""
    found: list[Piece] = []
    path: list[str] = []
    anchor = ""
    section: list[str] = []
    in_code = False

    def flush() -> None:
        group: list[str] = []
        size = 0
        for block in blocks(section):
            n = len(block.split())
            if group and size + n > PIECE_WORDS:
                found.append(Piece(page, tuple(path), anchor, "\n\n".join(group)))
                group, size = [], 0
            group.append(block)
            size += n
        if group:
            found.append(Piece(page, tuple(path), anchor, "\n\n".join(group)))

    for line in text.splitlines():
        if FENCE.match(line):
            in_code = not in_code
        m = None if in_code else HEADING.match(line)
        if m is None:
            section.append(line)
            continue
        flush()
        section = []
        level, title = len(m.group(1)), plain(m.group(2))
        path = [*path[: level - 1], title][:level]
        anchor = "" if level == 1 else slug(title)
    flush()
    for i, piece in enumerate(found, start):
        piece.order = i
        terms = Counter(words(piece.text))
        for term in words(" ".join(piece.path[1:] or piece.path)):
            terms[term] += 2  # a heading says what the piece is about
        piece.terms = terms
    return found


@dataclass
class Manual:
    pieces: list[Piece]
    commands: set[str]  # slash commands lcode has, without the slash
    options: set[str]  # --options the manual names, without the dashes
    idf: dict[str, float] = field(default_factory=dict, repr=False)
    average: float = 1.0

    def __post_init__(self) -> None:
        n = len(self.pieces) or 1
        seen: Counter = Counter()
        for piece in self.pieces:
            seen.update(set(piece.terms))
        self.idf = {term: math.log(1 + (n - k + 0.5) / (k + 0.5)) for term, k in seen.items()}
        self.average = sum(sum(p.terms.values()) for p in self.pieces) / n or 1.0

    def search(self, question: str, mode: str = "", limit: int = MAX_PIECES) -> list[Piece]:
        """The pieces that best answer the question (BM25), best first. In image or video mode, the mode counts a
        little, as if it were part of the question."""
        query: Counter = Counter(words(question))
        if mode in ("image", "video"):
            query[mode] += 0.5
        k1, b = 1.2, 0.75
        scored = []
        for piece in self.pieces:
            length = sum(piece.terms.values())
            score = 0.0
            for term, weight in query.items():
                tf = piece.terms.get(term, 0)
                if tf:
                    score += weight * self.idf[term] * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / self.average))
            if score > 0:
                scored.append((score, piece))
        scored.sort(key=lambda s: -s[0])
        return [piece for _, piece in scored[:limit]]

    def excerpts(self, found: list[Piece], budget: int = EXCERPT_CHARS) -> str:
        """The pieces as text for a model: in the manual's order, each under its headings, within the budget."""
        chosen, used = [], 0
        for piece in found:
            if used + len(piece.text) > budget and chosen:
                continue
            chosen.append(piece)
            used += len(piece.text)
        out, last = [], None
        for piece in sorted(chosen, key=lambda p: p.order):
            if piece.heading != last:
                out.append(f"## {piece.heading}")
                last = piece.heading
            out.append(piece.text)
        return "\n\n".join(out)

    def unknown(self, text: str) -> list[str]:
        """The slash commands and --options in an answer that lcode doesn't have."""
        missing = [f"/{c}" for c in dict.fromkeys(COMMAND.findall(text)) if c not in self.commands]
        missing += [f"--{o}" for o in dict.fromkeys(OPTION.findall(text)) if o not in self.options]
        return missing


def builtin_commands() -> dict[str, str]:
    from lcode.repl import COMMANDS

    return COMMANDS


@lru_cache(maxsize=1)
def _load(place: str) -> Manual:
    root = Path(place) if place else None
    found: list[Piece] = []
    text_all = []
    for path in sorted(root.glob("*.md")) if root else []:
        if path.name in SKIPPED:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        text_all.append(text)
        found += pieces(path.name, text, len(found))
    commands = builtin_commands()
    listing = "\n".join(f"- `{name}`: {description}" for name, description in commands.items())
    before = len(found)
    found += pieces("usage.md", f"# Slash commands (built in)\n\n{listing}\n", before)  # always as installed
    for piece in found[before:]:
        piece.anchor = "slash-commands"
    everything = "\n".join(text_all)
    known_commands = {c.lstrip("/") for c in commands} | {"exit", "quit", "q"}  # the docs name made-up ones too
    known_options = set(OPTION.findall(everything)) | {"raw"}
    return Manual(found, known_commands, known_options)


def load(extra_commands: set[str] = frozenset()) -> Manual:
    """The manual, read once."""
    place = folder()
    manual = _load(str(place) if place else "")
    if extra_commands:  # the user's own commands and skills
        manual = Manual(manual.pieces, manual.commands | {c.lstrip("/") for c in extra_commands}, manual.options)
    return manual


# -- telling questions from prompts (image and video mode)

QUESTION = re.compile(
    r"^(how (do|can|could|would|should|to|does)\b|is (there|it possible)\b|can (i|you|lcode)\b|could (i|you)\b"
    r"|(what|which)('s| is| are)? (the )?(command|option|flag|setting|way)s?\b|what does [/-]|where (do|does|are|is) "
    r"|why (does|did|is|do|won't|can't)\b|help me\b|tell me how\b|explain how\b|what can (i|lcode|you)\b)",
    re.I,
)
QUESTION_WORD = re.compile(
    r"^(how|what|which|where|why|when|can|could|is|are|does|do|should|will|would|may)\b(?! if\b)", re.I
)


def is_question(text: str) -> bool:
    """Whether text typed in image or video mode asks something of lcode rather than describing a picture: "how do I
    make a longer clip?" (but not "what if cats ruled the world")."""
    text = text.strip()
    if not text or text.lower().startswith(("what if", "imagine")):
        return False
    return bool(QUESTION.match(text) or (text.endswith("?") and QUESTION_WORD.match(text)))


# -- answers

# What a model needs to know to read the manual right: how requests work in each mode (tests check that every
# command and option here exists).
BASICS = """- Image mode (/mode image): what the user types at the prompt is a description, and lcode makes pictures from it. There is no command for that (no /image, /imagine or /generate). Options go after the description, for example: a red fox in the snow --n 4 --ar 16:9 (--n: 1 to 8 pictures; --ar: the shape; also --seed, --quality fast|balanced|best, --style, --no). @photo.png in a request uses the user's picture, to edit or as a reference. Results are numbered in the session's gallery (#1, #2…), and follow-ups take the number: /upscale 3, /nobg 3, /edit 3 make the sky purple, /vary 3, /use 3, /animate 3. Files go to .lcode/media/ in the project.
- Video mode (/mode video): what the user types is a description of a clip, and lcode makes it. There is no /video command. A clip is 1 to 8 seconds (--seconds). @start.png in the request starts the clip from that picture; @start.png @end.png goes from the first picture to the second. Other options: --camera zoom-in (and other moves), --quality, --n, --ar, --seed, --style, --no. Follow-ups take the clip's number: /extend 4 --seconds 3 (continues it), /sound 4, /smooth 4, /upscale 4, /play 4. /film makes a short film of several shots, with the same characters, from one idea.
- Code mode (/mode code, the default): the user asks a coding agent to work on their project. Slash commands control lcode itself (/model, /context, /resume, /undo…), and lcode config set NAME VALUE, in a shell, changes a setting for good."""
GUIDE = """You are the built-in help of lcode, a terminal program with three modes: code (a coding agent), image and video (they make pictures and clips on the user's GPU). The user is in {mode} mode now. Answer their question about lcode from what's below, and from nothing else.

How lcode works:
{basics}

Answer in Markdown:
1. First, exactly what to type in lcode, in one ```text block: one command or request per line, in order, without what lcode prints back. Fill in the user's own details (numbers, lengths, file names, descriptions). Where they gave no file name, use a clear one like start.png. When another mode is needed, the first line switches to it (for example /mode video).
2. Then one to three short sentences: what it does, and the limits, time or setup to know.

The limits in the manual are real. When the user asks for more than lcode can do (a longer clip, more pictures), say plainly that it can't, then give the closest way that works. Use only commands and options that appear below: never make one up. At most 120 words.

Manual excerpts:

{excerpts}"""
RETRY = (
    "These don't work in lcode:\n{problems}\nAnswer again: fix them with what's in the manual. If lcode can't do what "
    "the user asked, say so and give the closest way that works."
)
SHELL = ("lcode", "uv", "uvx", "pip", "pipx", "brew", "ollama", "export", "ls", "cd", "git", "curl", "cat", "sudo")
# Commands that read a request's options after the result's number (/vary 3 --n 2), and those that read none
OPTION_COMMANDS = {"/again", "/vary", "/animate", "/edit", "/extend", "/restyle"}
NO_OPTIONS = {"/sound", "/nobg", "/upscale", "/smooth", "/play", "/open", "/use", "/gallery", "/mode", "/model"}
FILM_OPTIONS = ("shots", "seconds", "quality", "transition", "sound")
# Commands that take only a result's number (or nothing: the last result), and the ones only video mode has
NUMBERED = {"/open", "/play", "/upscale", "/nobg", "/smooth"}
VIDEO_ONLY = {"/smooth", "/sound", "/restyle"}
REQUEST_EXTRA = {"raw", "ref", "refs", "mask"}
EXTEND_PADS = {"left", "right", "top", "bottom"}
PICTURE = re.compile(r'(?<!\S)@(?:"[^"]+"|\S+)')
NUMBER = re.compile(r"^#?\d+$")
OUTPUT_MARKS = ("◆", "✎", "⚠", "⎿", "●", "▀", "╭", "│", "╰", "…")
PROMPT_MARK = re.compile(r"^\s*(?:(?:\w+\s*)?❯\s*|(?:code|image|video)\s+(?=/))")  # a prompt shown before the line
NO_SLASH = re.compile(
    r"^(mode (code|image|video)|models?\b|(upscale|nobg|vary|play|open|animate|extend|sound|smooth|edit|restyle) #?\d+)\b"
)  # a command without its slash


def typed_lines(text: str) -> list[str]:
    """The lines of an answer's code blocks: what it says to type."""
    found = []
    for block in re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.S):
        for line in block.splitlines():
            if line.strip().startswith(OUTPUT_MARKS):  # what lcode prints back, copied from the manual's examples
                continue
            found.append(PROMPT_MARK.sub("", line).strip())
    return [line for line in found if line]


def request_problem(text: str, extra: set[str] = frozenset()) -> str:
    """What's wrong with a request's options (image or video mode), by lcode's own parser; empty when nothing."""
    from lcode import imageopts

    text = re.sub(r'--mask\s+("[^"]+"|\S+)', " ", PICTURE.sub(" ", text))
    allowed = set(imageopts.ALIASES) | REQUEST_EXTRA | extra
    bad = [name for name in OPTION.findall(text) if name not in allowed]
    if bad:
        return f"there's no --{bad[0]} here (a request's options: --n, --ar, --seed, --quality, --style, --no, --seconds, --camera)"
    try:
        imageopts.parse(re.sub(r"(?<!\S)--(raw|refs?)\b", " ", text), imageopts.Options())
    except imageopts.OptionError as e:
        return str(e)
    return ""


def mode_problem(command: str, mode: str) -> str:
    """When a command doesn't work in the mode it would be typed in: /animate in video mode."""
    from lcode.repl import IMAGE_COMMANDS, VIDEO_TOO

    if mode == "video" and command in IMAGE_COMMANDS and command not in VIDEO_TOO:
        return f"{command} works in image mode, not in video mode"
    if mode == "image" and command in VIDEO_ONLY:
        return f"{command} works in video mode, not in image mode"
    return ""


def problems(text: str, mode: str, manual: Manual) -> list[str]:
    """What in an answer wouldn't work in lcode: commands or options it doesn't have (anywhere in the text), and lines
    to type that its own parsers reject (--seconds 25: "seconds is how long a video is: 1 to 8")."""
    found = [f"lcode has no {name}" for name in manual.unknown(text)]
    current = mode
    for line in typed_lines(text):
        first = line.split()[0]
        if line.startswith(("#", "$")) or first in SHELL:
            continue
        if line.startswith("/"):
            command, _, rest = line.partition(" ")
            options = OPTION.findall(rest)
            if command[1:] not in manual.commands:
                continue  # said above
            first = rest.split()[0] if rest.split() else ""
            elsewhere = mode_problem(command, current)
            if elsewhere:
                found.append(f"{line}: {elsewhere}")
            if command == "/mode":
                if first not in ("code", "image", "video") or len(rest.split()) > 1:
                    found.append(f"{line}: /mode takes one word: code, image or video")
                current = first if first in ("code", "image", "video") else current
            elif command in NUMBERED and first and not NUMBER.match(first) and not first.startswith("--"):
                found.append(f"{line}: {command} takes a result's number from the gallery ({command} 3), not {first}")
            elif command == "/film":
                bad = [name for name in options if name not in FILM_OPTIONS]
                if bad:
                    found.append(f"{line}: /film has no --{bad[0]} (it has --{', --'.join(FILM_OPTIONS)})")
            elif command in OPTION_COMMANDS:
                number, _, after = rest.strip().partition(" ")
                problem = request_problem(
                    after if NUMBER.match(number) else rest, EXTEND_PADS if command == "/extend" else set()
                )
                if problem:
                    found.append(f"{line}: {problem}")
            elif command in NO_OPTIONS and options:
                found.append(f"{line}: {command} takes no --options (--{options[0]} would be read as words)")
            continue
        if NO_SLASH.match(line):
            found.append(f"{line}: a command starts with a slash (/{line})")
            continue
        if current in ("image", "video"):
            problem = request_problem(line)
            if problem:
                found.append(f"{line}: {problem}")
    return list(dict.fromkeys(found))


@dataclass
class Answer:
    text: str
    pieces: list[Piece]
    model: str | None = None  # who wrote it; None: the manual's own words
    problems: list[str] = field(default_factory=list)  # what in it wouldn't work in lcode

    def sources(self, limit: int = 2) -> list[Piece]:
        """The sections the answer came from, one piece each."""
        seen: dict[str, Piece] = {}
        for piece in self.pieces:
            seen.setdefault(piece.heading, piece)
        return list(seen.values())[:limit]


def clean(text: str) -> str:
    """The answer without reasoning tags, and its code blocks without what lcode prints back or its prompt."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()

    def typed(block: re.Match) -> str:
        lines = [PROMPT_MARK.sub("", line) for line in block.group(2).splitlines()
                 if not line.strip().startswith(OUTPUT_MARKS)]  # fmt: skip
        return block.group(1) + "".join(f"{line}\n" for line in lines) + "```"

    return re.sub(r"(```[^\n]*\n)(.*?)```", typed, text, flags=re.S)


def answer(
    question: str,
    mode: str,
    chat: Callable[[list[dict]], str] | None,
    model: str | None = None,
    manual: Manual | None = None,
) -> Answer:
    """The answer to a question about lcode: written by `chat` (messages in, text out) from the manual's best
    pieces, or the pieces themselves when there's no model. An answer with something that wouldn't work in lcode
    is asked for again once; what still wouldn't is listed in its `problems`."""
    manual = manual or load()
    found = manual.search(question, mode)
    if not found:
        return Answer("", [], None)
    if chat is None:
        return Answer(manual.excerpts(found[:2], budget=2500), found[:2], None)
    system = GUIDE.format(mode=mode or "code", basics=BASICS, excerpts=manual.excerpts(found))
    messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    text = clean(chat(messages))
    wrong = problems(text, mode, manual)
    if wrong:
        listed = "\n".join(f"- {problem}" for problem in wrong)
        retry = clean(chat([*messages, {"role": "assistant", "content": text},
                            {"role": "user", "content": RETRY.format(problems=listed)}]))  # fmt: skip
        if retry and len(problems(retry, mode, manual)) < len(wrong):
            text = retry
            wrong = problems(text, mode, manual)
    return Answer(text, found, model, wrong)


def suggestion(text: str, mode: str) -> str:
    """The first thing to type from an answer: the first line of its first code block, unless it's a shell command
    or a switch to the mode the user is in already."""
    for line in typed_lines(text):
        if line.startswith(("#", "$")) or line.split()[0] in SHELL:
            continue
        if line.lower() == f"/mode {mode}".lower():
            continue
        if " " not in line and not line.startswith("/"):
            continue  # a folder or a file (.lcode/media/), or a menu's choice: nothing to send
        return line
    return ""


def lookup(question: str, mode: str = "", version: str = "") -> str:
    """The lcode_help tool: how lcode works, and the manual's best pieces for a question, for the coding model to
    answer from."""
    manual = load()
    found = manual.search(question, mode)
    if not found:
        return "Nothing in lcode's manual matches that. Try other words, or see " + DOCS_URL
    sources = "\n".join(f"- {p.heading}: {p.url}" for p in Answer("", found).sources(3))
    return (
        f"How lcode{f' {version}' if version else ''} works:\n{BASICS}\n\nFrom its manual:\n\n"
        f"{manual.excerpts(found)}\n\nSources:\n{sources}\n\nTell the user exactly what to type in lcode. Only the "
        "commands and options above exist, and their limits are real: when lcode can't do what was asked, say so "
        "and give the closest way."
    )
