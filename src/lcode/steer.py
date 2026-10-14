"""Steer a request while it runs: type a note for the model, or press Esc to pause after the step.

With a local model a request can take minutes. Ctrl+C throws away the step in progress; a note doesn't
interrupt anything: a line typed while lcode works is kept and handed to the model before its next step, and the
spinner line shows what's being typed and what's waiting. Esc asks lcode to stop after the current step, so you
can look at what it did and then go on, with or without a note.

`Steering` holds the state and turns keystrokes into notes; it knows nothing about terminals, so it's easy to
test. `KeyReader` reads the keys from the terminal in the background, without echoing them (that would garble
the output), and hands the keyboard back whenever lcode asks a question (`Steering.ask`).
"""

from __future__ import annotations

import codecs
import contextlib
import os
import select
import sys
import threading
import time
from collections.abc import Callable, Iterator

from rich.text import Text

try:
    import termios
except ImportError:  # Windows: no steering, everything else works
    termios = None  # type: ignore[assignment]

MAX_NOTE = 4000  # characters in one note
SHOWN = 45  # characters of the line being typed shown under the spinner (the line fits 80 columns)
ESC_WAIT = 0.03  # an Esc followed by more bytes this soon is an arrow or function key, not the Esc key
TOO_QUICK = 0.4  # an Enter this soon after a question appears was meant for the note being typed
NOTE_INTRO = (
    "[lcode] The user wrote this while you were working. Take it into account from here on; it doesn't mean "
    "starting over unless it says so."
)


class Steering:
    """Notes typed during a request, the line being typed, and whether to pause after the step."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.typed = ""  # the line being typed
        self.notes: list[str] = []  # finished lines, waiting for the model's next step
        self.pause = False  # stop after the current step
        self.reader: KeyReader | None = None
        self.read_line: Callable[[str], str] | None = None  # input() when None (looked up when asking)

    # Input ------------------------------------------------------------------------------------------------

    def feed(self, keys: str) -> None:
        """Keystrokes as text: printable characters, Enter, Backspace, Ctrl+U/Ctrl+W, Esc and escape sequences.

        A lone "\\x1b" is the Esc key (KeyReader waits a moment for the rest of a sequence before it hands one
        over). Everything else that starts with it is skipped: the sequences arrow and function keys send,
        Alt+key, and a terminal's answers to queries (cursor position, colours).
        """
        with self.lock:
            i = 0
            while i < len(keys):
                ch = keys[i]
                i += 1
                if ch == "\x1b":
                    after = keys[i] if i < len(keys) else ""
                    if after == "[":  # CSI: parameters, then a final byte in @..~
                        i += 1
                        while i < len(keys) and not ("@" <= keys[i] <= "~"):
                            i += 1
                        i += 1
                    elif after == "O":  # SS3: one more character (F1-F4, keypad)
                        i += 2
                    elif after in ("]", "P", "_", "^"):  # a string (OSC, DCS…), ended by BEL or ESC \
                        end = min(
                            (j for j in (keys.find("\x07", i), keys.find("\x1b\\", i)) if j >= 0), default=len(keys)
                        )
                        i = end + (2 if keys.startswith("\x1b\\", end) else 1)
                    elif after in ("", "\x1b"):  # the Esc key (twice in a row: two presses)
                        self.pause = not self.pause
                    else:
                        i += 1  # Alt+key
                elif ch in "\r\n":
                    note = self.typed.strip()
                    self.typed = ""
                    if note:
                        self.notes.append(note[:MAX_NOTE])
                elif ch in "\x7f\x08":
                    self.typed = self.typed[:-1]
                elif ch == "\x15":  # Ctrl+U: clear the line
                    self.typed = ""
                elif ch == "\x17":  # Ctrl+W: delete the last word
                    self.typed = self.typed.rstrip()
                    self.typed = self.typed[: self.typed.rfind(" ") + 1] if " " in self.typed else ""
                elif ch == "\t":
                    self.typed += " "
                elif ch >= " " and len(self.typed) < MAX_NOTE:
                    self.typed += ch

    # For the agent ----------------------------------------------------------------------------------------

    def add(self, note: str) -> None:
        with self.lock:
            self.notes.append(note[:MAX_NOTE])

    def waiting(self) -> bool:
        with self.lock:
            return bool(self.notes)

    def take(self) -> list[str]:
        """The notes waiting for the model (and forget them)."""
        with self.lock:
            notes, self.notes = self.notes, []
            return notes

    def take_pause(self) -> bool:
        """Whether a pause was asked for (and clear the request)."""
        with self.lock:
            asked, self.pause = self.pause, False
            return asked

    def leftover(self) -> str:
        """What never reached the model: notes still waiting and the unfinished line, for the next prompt."""
        with self.lock:
            text = "\n".join([*self.notes, self.typed.strip()]).strip()
            self.notes, self.typed = [], ""
            return text

    def ask(self, question: str) -> str:
        """Ask the user a question while the keyboard is being read for notes (permission prompts, the pause).

        Keys typed before the question appeared are dropped, and an Enter pressed right as it appeared asks
        once more: it was meant to finish a note, not to answer (an empty answer often means yes).
        """
        hold = self.reader.suspended() if self.reader is not None else contextlib.nullcontext()
        with hold:
            asked = time.monotonic()
            answer = (self.read_line or input)(question)
            if not answer.strip() and time.monotonic() - asked < TOO_QUICK:
                answer = (self.read_line or input)(question)
            return answer

    def status(self) -> Text | None:
        """The line under the spinner: what's being typed, notes waiting, a pause coming."""
        with self.lock:
            typed, waiting, pause = self.typed, len(self.notes), self.pause
        parts: list[Text] = []
        if typed:
            shown = typed if len(typed) <= SHOWN else "…" + typed[-SHOWN:]
            parts.append(Text.assemble(("✎ ", "cyan"), shown, ("▏", "cyan"), ("  Enter adds it as a note", "dim")))
        if waiting:
            what = "a note" if waiting == 1 else f"{waiting} notes"
            parts.append(Text(f"✎ {what} waiting for the model's next step", style="cyan"))
        if pause:
            parts.append(Text("⏸ Pausing after this step (Esc again to go on without pausing)", style="yellow"))
        if not parts:
            return None
        return Text("\n").join(parts)


class KeyReader:
    """Reads the keyboard in the background while a request runs, and gives the keys to a Steering.

    The terminal stays in "cbreak" mode meanwhile: keys arrive one by one and aren't echoed, while Ctrl+C still
    interrupts. `suspended()` hands the keyboard back, in its usual mode, for a question.
    """

    def __init__(self, steering: Steering, fd: int | None = None) -> None:
        self.steering = steering
        self.fd = fd  # stdin when None
        self.saved: list | None = None
        self.thread: threading.Thread | None = None
        self.stopping = threading.Event()
        self.hold = threading.Event()  # asked to stop reading for a while
        self.held = threading.Event()  # ...and the thread has stopped
        self.resume = threading.Event()
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def start(self) -> bool:
        """Start reading; False when this isn't a terminal lcode can read keys from."""
        if termios is None:
            return False
        try:
            if self.fd is None:
                self.fd = sys.stdin.fileno()
            if not os.isatty(self.fd):
                return False
        except (OSError, ValueError):  # no real stdin (tests, some IDE consoles)
            return False
        try:
            self.saved = termios.tcgetattr(self.fd)
            self._cbreak()
        except termios.error:
            self.saved = None
            return False
        self.steering.reader = self
        self.thread = threading.Thread(target=self._run, name="lcode-keys", daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        self.stopping.set()
        self.resume.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
        self._restore()
        self.steering.reader = None

    def __enter__(self) -> KeyReader:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    @contextlib.contextmanager
    def suspended(self) -> Iterator[None]:
        """The keyboard back in its usual mode (echo, line editing) for a question, with typed-ahead keys dropped."""
        if self.thread is None or not self.thread.is_alive():
            yield
            return
        self.resume.clear()
        self.hold.set()
        self.held.wait(timeout=1)
        self._restore()
        with contextlib.suppress(termios.error):
            termios.tcflush(self.fd, termios.TCIFLUSH)
        try:
            yield
        finally:
            with contextlib.suppress(termios.error):
                self._cbreak()
            self.hold.clear()
            self.held.clear()
            self.resume.set()

    def _cbreak(self) -> None:
        mode = termios.tcgetattr(self.fd)
        mode[3] &= ~(termios.ECHO | termios.ICANON)  # lflags; ISIG stays, so Ctrl+C still interrupts
        mode[6][termios.VMIN] = 1
        mode[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, mode)

    def _restore(self) -> None:
        if self.saved is not None:
            with contextlib.suppress(termios.error):
                termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)

    def _run(self) -> None:
        while not self.stopping.is_set():
            if self.hold.is_set():
                self.held.set()
                self.resume.wait()
                continue
            try:
                ready, _, _ = select.select([self.fd], [], [], 0.1)
                if not ready or self.hold.is_set():
                    continue
                data = os.read(self.fd, 1024)
                if not data:
                    return  # the terminal is gone
                while incomplete_escape(data):
                    more, _, _ = select.select([self.fd], [], [], ESC_WAIT)  # the rest of an escape sequence?
                    if not more:
                        break  # a lone "\x1b" is then the Esc key
                    data += os.read(self.fd, 1024)
            except (OSError, ValueError):
                return
            self.steering.feed(self.decoder.decode(data))


def incomplete_escape(data: bytes) -> bool:
    """Whether the bytes end in the middle of an escape sequence (or with a "\\x1b" that may start one)."""
    start = data.rfind(b"\x1b")
    if start < 0:
        return False
    tail = data[start:]
    if tail == b"\x1b":
        return True
    if tail.startswith(b"\x1b["):
        return not any(0x40 <= b <= 0x7E for b in tail[2:])
    if tail.startswith(b"\x1bO"):
        return len(tail) < 3
    if tail[1:2] in (b"]", b"P", b"_", b"^"):
        return b"\x07" not in tail  # (one ended by ESC \ has its last ESC there: complete)
    return False
