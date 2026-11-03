"""Screenshots of desktop programs and web pages without a screen, for the screenshot tool.

GUI and layout bugs are invisible to a model that can only read code: a window bigger than the screen, panels
cut off, a page that scrolls sideways. This runs the program the way the user would, but without a screen, and
returns what the model can't otherwise see: a picture (described by the vision model) and the sizes behind it.

- A Python Qt program (PyQt5/6, PySide2/6) runs on Qt's "offscreen" platform, with a screen of the asked size. A
  small startup hook (a sitecustomize module on PYTHONPATH) waits for the program's event loop, then grabs its
  windows, measures them and their widgets, and quits it. First it can take `actions`, the way a user would: click
  or double-click a point or a widget by its text, press a key, type, wait. Bugs that only show after a click need
  that, and Qt's warnings (in what the program printed) often name the cause.
- Other desktop programs run on a virtual X display (Xvfb, Linux), captured with ImageMagick's `import` or `xwd`.
  The Qt hook still measures a Python Qt program there.
- A web page, or a local server started with `command`, opens in headless Chrome (or Chromium, Edge), driven
  over its DevTools pipe: the page's console errors and failed requests, its size against the window and what
  sticks out sideways come back too. Firefox, the fallback, only takes the picture.

Shots go to .lcode/screenshots/ in the project, which ignores itself in git, so the user can look at them too.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from lcode.qt_hook import QT_HOOK

DEFAULT_SCREEN = (1920, 1080)
DEFAULT_WAIT = 3  # seconds for a program or page to start and draw
MAX_WAIT = 60
MAX_ACTIONS = 20
ACTION_PAUSE = 0.4  # seconds after each action, for the program to react and redraw
MOUSE_ACTIONS = ("click", "double_click", "right_click")
KEY_NAMES = {"enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "space": "Space",
             "tab": "Tab", "backspace": "Backspace", "delete": "Delete", "del": "Delete", "home": "Home", "end": "End",
             "pageup": "PageUp", "pagedown": "PageDown", "up": "Up", "down": "Down", "left": "Left",
             "right": "Right"}  # fmt: skip
MODIFIERS = ("ctrl", "shift", "alt", "meta")
SERVER_TIMEOUT = 60  # seconds for a server started with `command` to answer at `url`
GRACE = 25  # seconds beyond `wait` before a program that never showed a window is stopped
QT_BINDINGS = ("PyQt6", "PyQt5", "PySide6", "PySide2")
QT_IMPORT = re.compile(r"^\s*(?:from|import)\s+(?:PyQt[56]|PySide[26])\b", re.M)
SHOWN_OUTPUT = 1500  # characters of what the program printed

INSTALL_XVFB = (
    "install Xvfb (Debian/Ubuntu: sudo apt install xvfb x11-apps; Fedora: sudo dnf install xorg-x11-server-Xvfb)"
)


class ShotError(Exception):
    """The screenshot couldn't be taken; the message says why and what to do."""


@dataclass
class Shot:
    image: Path  # the screenshot (the whole screen or page)
    how: str  # "Qt offscreen", "a virtual display (Xvfb)", "headless Chrome"…
    screen: tuple[int, int]
    report: list[str] = field(default_factory=list)  # windows, widgets, page size, console: what the picture can't say
    output: str = ""  # what the program printed
    windows: list[Path] = field(default_factory=list)  # each window on its own (Qt)


# --------------------------------------------------------------------------------------------- arguments


def parse_screen(text: str | None) -> tuple[int, int]:
    if not text:
        return DEFAULT_SCREEN
    m = re.fullmatch(r"\s*(\d{3,5})\s*[xX×*,]\s*(\d{3,5})\s*", str(text))
    if not m:
        raise ShotError(f"screen must be a size like 1920x1080, not {text!r}")
    w, h = int(m.group(1)), int(m.group(2))
    if not (200 <= w <= 7680 and 200 <= h <= 4320):
        raise ShotError(f"screen {w}x{h} is out of range (200x200 to 7680x4320)")
    return w, h


def shots_dir(root: Path) -> Path:
    folder = root / ".lcode" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# lcode's screenshots: not part of the project\n*\n")
    return folder


def new_path(folder: Path, label: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "shot"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = folder / f"{stamp}-{slug}.png"
    n = 2
    while path.exists():
        path = folder / f"{stamp}-{slug}-{n}.png"
        n += 1
    return path


def label_for(command: str | None, url: str | None) -> str:
    if url:
        return re.sub(r"^\w+://", "", url).split("?")[0]
    try:
        words = [w for w in shlex.split(command or "") if not w.startswith("-")]
    except ValueError:
        words = (command or "").split()
    scripts = [Path(w).stem for w in words if Path(w).suffix in (".py", ".js", ".sh", ".html")]
    return (scripts or [Path(w).name for w in words[:1]] or ["program"])[0]


def looks_like_qt(command: str, cwd: Path) -> bool:
    """Whether the command runs a Python Qt program: the script it runs, or the project's Python, imports Qt."""
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    if not any("python" in Path(w).name or w.endswith(".py") or w in ("uv", "poetry", "pipenv") for w in words):
        return False
    files = [cwd / w for w in words if w.endswith(".py")]
    if "-m" in words and words.index("-m") + 1 < len(words):
        module = cwd / Path(*words[words.index("-m") + 1].split("."))
        files += (
            [module.with_suffix(".py"), *sorted(module.glob("*.py"))[:50]]
            if module.is_dir()
            else [module.with_suffix(".py")]
        )
    for f in files:
        with contextlib.suppress(OSError):
            if QT_IMPORT.search(f.read_text(errors="replace")):
                return True
    # The script itself doesn't import Qt, or isn't a file: the project's other modules might.
    skip = {".venv", "venv", "env", "node_modules", ".git", "build", "dist", "site-packages", "__pycache__", ".tox"}
    seen = 0
    for folder, dirs, names in os.walk(cwd):
        dirs[:] = sorted(d for d in dirs if d not in skip and not d.startswith("."))
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            seen += 1
            if seen > 300:
                return False
            with contextlib.suppress(OSError):
                if QT_IMPORT.search(Path(folder, name).read_text(errors="replace")):
                    return True
    return False


# --------------------------------------------------------------------------------------------- the Qt hook


def hook_dir(tmp: Path) -> Path:
    folder = tmp / "hook"
    folder.mkdir(exist_ok=True)
    (folder / "sitecustomize.py").write_text(QT_HOOK)
    return folder


# --------------------------------------------------------------------------------------------- running a program


def parse_actions(actions) -> list[dict]:
    """The steps to take before the picture, from the tool's `actions` (a list, or one string split on ; and lines).

    `click X,Y`, `double_click X,Y`, `right_click X,Y` (a point in the window), or the same with a widget's text
    (`double_click "Camera 1"`); `key Escape` or `key ctrl+s`; `type some text`; `wait 2` (seconds).
    """
    if not actions:
        return []
    if isinstance(actions, str):
        actions = [part for line in actions.splitlines() for part in line.split(";")]
    steps = []
    for raw in actions:
        raw = str(raw).strip()
        if not raw:
            continue
        verb, _, rest = raw.partition(" ")
        verb, rest = verb.lower().replace("-", "_"), rest.strip()
        if verb == "doubleclick":
            verb = "double_click"
        step: dict = {"do": verb, "raw": raw}
        if verb in MOUSE_ACTIONS:
            point = re.fullmatch(r"\(?\s*(-?\d+)\s*[, ]\s*(-?\d+)\s*\)?", rest)
            if point:
                step["x"], step["y"] = int(point.group(1)), int(point.group(2))
            elif rest.strip("\"'"):
                step["text"] = rest.strip("\"'")
            else:
                raise ShotError(f"{raw!r}: say where, as X,Y in the window or the widget's text, e.g. {verb} 200,150")
        elif verb == "key":
            parts = [p for p in re.split(r"[+\s]+", rest.lower()) if p]
            if not parts:
                raise ShotError(f"{raw!r}: name the key, e.g. key Escape or key ctrl+s")
            *mods, key = parts
            if any(m not in MODIFIERS for m in mods):
                raise ShotError(f"{raw!r}: modifiers are {', '.join(MODIFIERS)}")
            if key in KEY_NAMES:
                key = KEY_NAMES[key]
            elif re.fullmatch(r"f([1-9]|1[0-2])", key) or (len(key) == 1 and key.isalnum()):
                key = key.upper()
            else:
                raise ShotError(f"{raw!r}: unknown key {key!r} (Escape, Return, Tab, Space, arrows, F1-F12, a letter)")
            step["key"], step["mods"] = key, mods
        elif verb == "type":
            if not rest:
                raise ShotError(f"{raw!r}: give the text to type")
            step["text"] = rest
        elif verb == "wait":
            try:
                step["seconds"] = max(0.0, min(float(rest or 1), 10.0))
            except ValueError as e:
                raise ShotError(f"{raw!r}: wait takes seconds, e.g. wait 2") from e
        else:
            raise ShotError(f"{raw!r}: actions are click, double_click, right_click, key, type and wait")
        steps.append(step)
    if len(steps) > MAX_ACTIONS:
        raise ShotError(f"at most {MAX_ACTIONS} actions in one screenshot")
    return steps


def actions_time(steps: list[dict]) -> float:
    return sum(s.get("seconds", 0) for s in steps) + ACTION_PAUSE * len(steps)


def run_program(command: str, cwd: Path, env: dict[str, str], log: Path) -> subprocess.Popen:
    out = log.open("wb")
    try:
        return subprocess.Popen(
            ["bash", "-c", command],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # its own process group: stopped with everything it started
        )
    finally:
        out.close()


def stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        if proc is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)  # what it left running
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=3)


QT_WARNING = re.compile(r"^(?:Q[A-Za-z0-9]+::\w+|qt\.[\w.]+:|QML |Qt[: ])")
NOT_A_PROBLEM = re.compile(
    r"propagateSizeHints|QFont::|This plugin does not support|qt\.qpa\.fonts"
)  # offscreen platform noise
MAX_WARNINGS = 6


def qt_warnings(log: Path) -> list[str]:
    """Qt's own warnings in everything the program printed (not just the end that's shown), without repeats."""
    try:
        text = log.read_bytes().decode(errors="replace")
    except OSError:
        return []
    found: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if QT_WARNING.match(line) and not NOT_A_PROBLEM.search(line) and line[:300] not in found:
            found.append(line[:300])
    return found[:MAX_WARNINGS]


def printed(log: Path) -> str:
    try:
        text = log.read_bytes().decode(errors="replace")
    except OSError:
        return ""
    text = text.strip()
    return text if len(text) <= SHOWN_OUTPUT else "…" + text[-SHOWN_OUTPUT:]


def base_env(hook: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(hook), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])])
    env["PYTHONUNBUFFERED"] = "1"
    for name in ("WAYLAND_DISPLAY", "QT_QPA_PLATFORMTHEME"):
        env.pop(name, None)
    return env


def qt_offscreen(
    command: str, cwd: Path, screen: tuple[int, int], wait: int, tmp: Path, cancel=None, actions: list | None = None
) -> Shot:
    """Run a Python Qt program on Qt's offscreen platform and capture its windows."""
    config = tmp / "screens.json"
    w, h = screen
    config.write_text(
        json.dumps(
            {
                "screens": [
                    {
                        "name": "lcode",
                        "x": 0,
                        "y": 0,
                        "width": w,
                        "height": h,
                        "logicalDpi": 96,
                        "logicalBaseDpi": 96,
                        "dpr": 1,
                    }
                ]
            }
        )
    )
    out = tmp / "qt"
    out.mkdir()
    env = base_env(hook_dir(tmp))
    env.update(
        QT_QPA_PLATFORM=f"offscreen:configfile={config}",
        LCODE_SHOT_OUT=str(out),
        LCODE_SHOT_WAIT=str(wait),
        LCODE_SHOT_QUIT="1",
        LCODE_SHOT_ACTIONS=json.dumps(actions or []),
        LCODE_SHOT_PAUSE=str(ACTION_PAUSE),
    )
    env.pop("DISPLAY", None)
    log = tmp / "output.txt"
    proc = run_program(command, cwd, env, log)
    try:
        deadline = time.time() + wait + actions_time(actions or []) + GRACE
        while time.time() < deadline and proc.poll() is None and not (out / "qt.json").exists():
            if cancel is not None and cancel():
                raise KeyboardInterrupt
            time.sleep(0.1)
        time.sleep(0.2)
    finally:
        stop(proc)
    data = read_qt(out)
    if data is None:
        why = "it exited before showing a window" if proc.returncode not in (None, -15, -9) else "no Qt window appeared"
        raise ShotError(
            f"{why} (exit code {proc.returncode}). The offscreen capture works for Python programs that use PyQt or "
            "PySide and start Qt's event loop (app.exec())."
            + (f"\nWhat it printed:\n{printed(log)}" if printed(log) else "")
        )
    if data.get("error"):
        raise ShotError(f"capturing the Qt windows failed: {data['error']}")
    shot_screen = (int((data.get("screen") or screen)[0]), int((data.get("screen") or screen)[1]))
    image = Path(data["screen_image"]) if data.get("screen_image") else None
    windows = [Path(win["image"]) for win in data.get("windows", []) if win.get("image")]
    if image is None and not windows:
        raise ShotError("the program started Qt but showed no window")
    report = qt_report(data)
    warnings = qt_warnings(log)
    if warnings:
        report[:0] = ["Qt printed warnings (they often name the cause):", *[f"- {line}" for line in warnings]]
    if shot_screen != screen:  # Qt 5's offscreen platform has a fixed screen
        report.append(
            f"(Qt used a {shot_screen[0]}x{shot_screen[1]} screen offscreen, not the {screen[0]}x{screen[1]} asked "
            "for: Qt 5 can't change it there. A virtual display (mode xvfb, Linux) takes any size.)"
        )
    return Shot(
        image=image or windows[0],
        how=f"Qt offscreen ({data.get('binding', 'Qt')})",
        screen=shot_screen,
        report=report,
        output=printed(log),
        windows=windows,
    )


def read_qt(out: Path) -> dict | None:
    try:
        return json.loads((out / "qt.json").read_text())
    except (OSError, ValueError):
        return None


def qt_report(data: dict) -> list[str]:
    lines = []
    screen = data.get("screen") or [0, 0]
    sw, sh = screen
    windows = data.get("windows") or []
    if data.get("actions"):
        lines.append("Steps, before the picture:")
        for note in data["actions"]:
            done = (
                f"couldn't: {note['error']}"
                if note.get("error")
                else (f"on {note['target']}" if note.get("target") else "done")
            )
            lines.append(f"- {note['step']}: {done}")
    if not windows:
        return [*lines, "No visible Qt window."]
    for win in windows:
        title = f'"{win["title"]}" ' if win.get("title") else ""
        x, y, w, h = win["x"], win["y"], win["width"], win["height"]
        line = f"- Window {title}({win['class']}): {w}x{h} at ({x}, {y})"
        off_w = max(0, x + w - sw) + max(0, -x)
        off_h = max(0, y + h - sh) + max(0, -y)
        if sw and (off_w or off_h):
            line += f" on a {sw}x{sh} screen: {off_w}x{off_h} pixels of it are off screen"
        lines.append(line)
        if win.get("min_width") and (win["min_width"] > sw or win["min_height"] > sh):
            lines.append(
                f"  Its layout needs at least {win['min_width']}x{win['min_height']}, more than the screen has"
            )
        for item in win.get("outside", [])[:8]:
            lines.append(f"  Sticks out of the window: {describe_widget(item)}")
        for a, b, ow, oh in win.get("overlaps", [])[:6]:
            lines.append(f"  Overlap ({ow}x{oh}): {describe_widget(a)} and {describe_widget(b)}")
        widgets = win.get("widgets", [])
        if widgets:
            lines.append("  Widgets (position in the window):")
            lines += [f"  {'  ' * item['depth']}- {describe_widget(item)}" for item in widgets[:30]]
    return lines


def describe_widget(item: dict) -> str:
    name = f" #{item['name']}" if item.get("name") else ""
    text = f' "{item["text"]}"' if item.get("text") else ""
    return f"{item['class']}{name}{text}: {item['width']}x{item['height']} at ({item['x']}, {item['y']})"


# --------------------------------------------------------------------------------------------- a virtual display


def xvfb_available() -> bool:
    return sys.platform.startswith("linux") and shutil.which("Xvfb") is not None


def virtual_display(command: str, cwd: Path, screen: tuple[int, int], wait: int, tmp: Path, cancel=None) -> Shot:
    """Run any desktop program on a virtual X display and capture the screen."""
    if not xvfb_available():
        if sys.platform == "darwin":
            raise ShotError(
                "there's no virtual display on macOS, so only Python Qt programs (offscreen) and web pages (url) can "
                "be captured"
            )
        raise ShotError(f"no virtual display: {INSTALL_XVFB}")
    if not (shutil.which("import") or shutil.which("xwd")):
        raise ShotError(f"nothing to capture the virtual display with: {INSTALL_XVFB} (xwd), or install ImageMagick")
    w, h = screen
    xvfb = subprocess.Popen(
        ["Xvfb", "-displayfd", "1", "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    proc = None
    try:
        number = read_display(xvfb)
        display = f":{number}"
        out = tmp / "qt"
        out.mkdir()
        env = base_env(hook_dir(tmp))
        env.update(
            DISPLAY=display,
            QT_QPA_PLATFORM="xcb",
            GDK_BACKEND="x11",
            SDL_VIDEODRIVER="x11",
            LCODE_SHOT_OUT=str(out),
            LCODE_SHOT_WAIT=str(wait),
            LCODE_SHOT_QUIT="0",  # the screen is captured from outside, then the program is stopped
        )
        log = tmp / "output.txt"
        proc = run_program(command, cwd, env, log)
        deadline = time.time() + wait
        while time.time() < deadline and proc.poll() is None:
            if cancel is not None and cancel():
                raise KeyboardInterrupt
            time.sleep(0.1)
        if (out / "started").exists():
            # A Python Qt program measures itself `wait` seconds after its event loop starts: give it the time.
            end = time.time() + wait + 5
            while time.time() < end and not (out / "qt.json").exists() and proc.poll() is None:
                time.sleep(0.1)
        image = tmp / "screen.png"
        capture_x(display, image)
        windows = x_windows(display)
        report = []
        data = read_qt(out)
        if data and not data.get("error") and data.get("windows"):
            report = qt_report(data)
        elif windows:
            report = [
                f"- Window {name}: {ww}x{wh} at ({x}, {y})" + off_screen(x, y, ww, wh, w, h)
                for name, ww, wh, x, y in windows
            ]
        else:
            report = ["No window appeared on the screen."]
        if proc.poll() is not None:
            report.append(f"The program had already exited (exit code {proc.returncode}).")
        return Shot(image=image, how="a virtual display (Xvfb)", screen=screen, report=report, output=printed(log))
    finally:
        stop(proc)
        stop(xvfb)


def off_screen(x: int, y: int, w: int, h: int, sw: int, sh: int) -> str:
    off_w = max(0, x + w - sw) + max(0, -x)
    off_h = max(0, y + h - sh) + max(0, -y)
    return f" on a {sw}x{sh} screen: {off_w}x{off_h} pixels of it are off screen" if off_w or off_h else ""


def read_display(xvfb: subprocess.Popen) -> int:
    assert xvfb.stdout is not None
    ready, _, _ = select.select([xvfb.stdout], [], [], 10)
    line = xvfb.stdout.readline().decode().strip() if ready else ""
    if not line.isdigit():
        raise ShotError("the virtual display (Xvfb) didn't start")
    return int(line)


def capture_x(display: str, path: Path) -> None:
    if shutil.which("import"):
        done = subprocess.run(
            ["import", "-display", display, "-window", "root", str(path)], capture_output=True, timeout=30
        )
        if done.returncode == 0 and path.exists():
            return
    if shutil.which("xwd"):
        done = subprocess.run(["xwd", "-display", display, "-root", "-silent"], capture_output=True, timeout=30)
        if done.returncode == 0 and done.stdout:
            path.write_bytes(xwd_to_png(done.stdout))
            return
    raise ShotError("couldn't capture the virtual display (ImageMagick's import and xwd both failed)")


TREE_LINE = re.compile(
    r'^\s*0x[0-9a-f]+ "(?P<name>[^"]*)":.*?\s(?P<w>\d+)x(?P<h>\d+)\+(?P<x>-?\d+)\+(?P<y>-?\d+)', re.M
)


def x_windows(display: str) -> list[tuple[str, int, int, int, int]]:
    """The named windows on the display: (name, width, height, x, y)."""
    if not shutil.which("xwininfo"):
        return []
    try:
        done = subprocess.run(
            ["xwininfo", "-display", display, "-root", "-tree"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    found, seen = [], set()
    for m in TREE_LINE.finditer(done.stdout):
        name, w, h = m.group("name"), int(m.group("w")), int(m.group("h"))
        if not name or w < 2 or h < 2 or name in seen:
            continue
        seen.add(name)
        found.append((f'"{name}"', w, h, int(m.group("x")), int(m.group("y"))))
    return found[:10]


def xwd_to_png(data: bytes) -> bytes:
    """An X window dump (ZPixmap, 24/32 bits per pixel, as Xvfb makes them) as a PNG."""
    header = struct.unpack(">25I", data[:100])
    header_size, _, fmt, _depth, width, height = header[:6]
    byte_order, bits_per_pixel, bytes_per_line = header[7], header[11], header[12]
    red_mask, green_mask, blue_mask, ncolors = header[14], header[15], header[16], header[19]
    if fmt != 2 or bits_per_pixel not in (24, 32):
        raise ShotError(f"can't read this screen dump (format {fmt}, {bits_per_pixel} bits per pixel)")
    start = header_size + ncolors * 12
    step = bits_per_pixel // 8
    rows = []
    shifts = [(mask, (mask & -mask).bit_length() - 1) for mask in (red_mask, green_mask, blue_mask)]
    standard = (red_mask, green_mask, blue_mask) == (0xFF0000, 0xFF00, 0xFF)
    for y in range(height):
        line = data[start + y * bytes_per_line : start + y * bytes_per_line + width * step]
        rgb = bytearray(width * 3)
        if standard and byte_order == 0:  # LSBFirst: B, G, R(, X)
            rgb[0::3], rgb[1::3], rgb[2::3] = line[2::step], line[1::step], line[0::step]
        elif standard:  # MSBFirst: (X,) R, G, B
            o = step - 3
            rgb[0::3], rgb[1::3], rgb[2::3] = line[o::step], line[o + 1 :: step], line[o + 2 :: step]
        else:
            for x in range(width):
                chunk = line[x * step : x * step + step]
                value = int.from_bytes(chunk, "little" if byte_order == 0 else "big")
                for i, (mask, shift) in enumerate(shifts):
                    rgb[x * 3 + i] = ((value & mask) >> shift) & 0xFF
        rows.append(b"\x00" + bytes(rgb))
    return png(width, height, b"".join(rows))


def png(width: int, height: int, raw: bytes) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    head = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


# --------------------------------------------------------------------------------------------- web pages


# --------------------------------------------------------------------------------------------- the entry point


def take(
    cwd: Path,
    folder: Path,
    command: str | None = None,
    url: str | None = None,
    screen: tuple[int, int] = DEFAULT_SCREEN,
    wait: int = DEFAULT_WAIT,
    full_page: bool = False,
    mode: str = "auto",
    cancel=None,
    actions: list | None = None,
) -> Shot:
    """Take the screenshot. `cancel` is checked while waiting (return True to stop); `actions` come first."""
    from lcode.screenshots_web import answers, normalize_url, wait_for_server, web

    wait = max(0, min(int(wait), MAX_WAIT))
    path = new_path(folder, label_for(command, url))
    with tempfile.TemporaryDirectory(prefix="lcode-shot-") as name:
        tmp = Path(name)
        if actions and url:
            raise ShotError("actions work for Python Qt programs (PyQt, PySide), not web pages yet")
        if url:
            url = normalize_url(url, cwd)
            server = None
            log = tmp / "server.txt"
            running = bool(command) and answers(url)  # a server is up already (one the model started, say)
            try:
                if command and not running:
                    server = run_program(command, cwd, dict(os.environ), log)
                    wait_for_server(url, server, SERVER_TIMEOUT, log, cancel)
                shot = web(url, screen, wait, full_page, path, tmp)
            finally:
                stop(server)
            if running:
                shot.report.append(f"({url} already answered, so `{command}` wasn't started: a server is running.)")
            elif command:
                shot.output = printed(log)
            return shot
        if not command:
            raise ShotError("give a command that starts the program, or a url")
        if mode == "auto":
            mode = "qt" if looks_like_qt(command, cwd) or not xvfb_available() else "xvfb"
        if actions and mode != "qt":
            raise ShotError(
                "actions work for Python Qt programs (PyQt, PySide) only: take this one's screenshot without them"
            )
        if mode == "qt":
            shot = qt_offscreen(command, cwd, screen, wait, tmp, cancel, actions)
        else:
            shot = virtual_display(command, cwd, screen, wait, tmp, cancel)
        shutil.copyfile(shot.image, path)
        kept = []
        for i, window in enumerate(shot.windows):
            if window != shot.image and len(shot.windows) > 1:
                extra = path.with_name(f"{path.stem}-window-{i + 1}.png")
                shutil.copyfile(window, extra)
                kept.append(extra)
        shot.image, shot.windows = path, kept
        return shot
