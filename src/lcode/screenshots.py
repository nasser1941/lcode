"""Screenshots of desktop programs and web pages without a screen, for the screenshot tool.

GUI and layout bugs are invisible to a model that can only read code: a window bigger than the screen, panels
cut off, a page that scrolls sideways. This runs the program the way the user would, but without a screen, and
returns what the model can't otherwise see: a picture (described by the vision model) and the sizes behind it.

- A Python Qt program (PyQt5/6, PySide2/6) runs on Qt's "offscreen" platform, with a screen of the asked size. A
  small startup hook (a sitecustomize module on PYTHONPATH) waits for the program's event loop, then grabs its
  windows, measures them and their widgets, and quits it.
- Other desktop programs run on a virtual X display (Xvfb, Linux), captured with ImageMagick's `import` or `xwd`.
  The Qt hook still measures a Python Qt program there.
- A web page, or a local server started with `command`, opens in headless Chrome (or Chromium, Edge), driven
  over its DevTools pipe: the page's console errors and failed requests, its size against the window and what
  sticks out sideways come back too. Firefox, the fallback, only takes the picture.

Shots go to .lcode/screenshots/ in the project, which ignores itself in git, so the user can look at them too.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
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
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SCREEN = (1920, 1080)
DEFAULT_WAIT = 3  # seconds for a program or page to start and draw
MAX_WAIT = 60
SERVER_TIMEOUT = 60  # seconds for a server started with `command` to answer at `url`
GRACE = 25  # seconds beyond `wait` before a program that never showed a window is stopped
QT_BINDINGS = ("PyQt6", "PyQt5", "PySide6", "PySide2")
QT_IMPORT = re.compile(r"^\s*(?:from|import)\s+(?:PyQt[56]|PySide[26])\b", re.M)
SHOWN_OUTPUT = 1500  # characters of what the program printed

INSTALL_XVFB = (
    "install Xvfb (Debian/Ubuntu: sudo apt install xvfb x11-apps; Fedora: sudo dnf install xorg-x11-server-Xvfb)"
)
INSTALL_BROWSER = "install Google Chrome or Chromium (or Firefox)"


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

QT_HOOK = r'''
# Written by lcode's screenshot tool (lcode.screenshots). When this program's Qt event loop starts, its windows are
# grabbed and measured, the results written to $LCODE_SHOT_OUT, and (unless LCODE_SHOT_QUIT=0) the program quits.
import os as _os
import sys as _sys


def _lcode_shot():
    import importlib.abc
    import importlib.machinery
    import json

    out = _os.environ.get("LCODE_SHOT_OUT")
    wait_ms = int(float(_os.environ.get("LCODE_SHOT_WAIT", "3")) * 1000)
    quit_after = _os.environ.get("LCODE_SHOT_QUIT", "1") == "1"
    bindings = ("PyQt6", "PyQt5", "PySide6", "PySide2")
    state = {"scheduled": False}

    def capture(binding):
        QtCore = _sys.modules[binding + ".QtCore"]
        QtGui = _sys.modules[binding + ".QtGui"]
        QtWidgets = _sys.modules.get(binding + ".QtWidgets")
        app = QtGui.QGuiApplication.instance()
        result = {"binding": binding, "windows": [], "screen": None, "platform": app.platformName()}
        screen = app.primaryScreen()
        if screen is not None:
            g, a = screen.geometry(), screen.availableGeometry()
            result["screen"] = [g.width(), g.height()]
            result["available"] = [a.x(), a.y(), a.width(), a.height()]
        tops = []
        if QtWidgets is not None:
            tops = [w for w in app.topLevelWidgets() if w.isVisible() and w.width() > 1 and w.height() > 1]
        canvas = None
        if screen is not None:
            rgb32 = getattr(QtGui.QImage, "Format_RGB32", None) or QtGui.QImage.Format.Format_RGB32
            canvas = QtGui.QImage(result["screen"][0], result["screen"][1], rgb32)
            canvas.fill(QtGui.QColor(40, 44, 52))
            painter = QtGui.QPainter(canvas)
        for i, w in enumerate(tops):
            pixmap = w.grab()
            path = _os.path.join(out, "window-%d.png" % (i + 1))
            pixmap.save(path)
            g, f = w.geometry(), w.frameGeometry()
            hint = w.minimumSizeHint()
            info = {
                "title": w.windowTitle(), "class": type(w).__name__, "name": w.objectName(),
                "x": f.x(), "y": f.y(), "width": g.width(), "height": g.height(),
                "min_width": hint.width(), "min_height": hint.height(), "image": path,
                "widgets": [], "outside": [],
            }
            if canvas is not None:
                painter.drawPixmap(g.x(), g.y(), pixmap)
            reported = []
            siblings = {}  # parent -> [(widget, item)]: overlapping siblings are a squeezed layout
            for c in w.findChildren(QtWidgets.QWidget):
                if not c.isVisible() or c.width() < 2 or c.height() < 2:
                    continue
                depth, p = 0, c.parentWidget()
                while p is not None and p is not w:
                    depth, p = depth + 1, p.parentWidget()
                pos = c.mapTo(w, QtCore.QPoint(0, 0))
                text = ""
                for getter in ("text", "windowTitle", "title", "placeholderText"):
                    fn = getattr(c, getter, None)
                    if callable(fn):
                        try:
                            text = str(fn() or "")
                        except Exception:
                            text = ""
                        if text:
                            break
                item = {"class": type(c).__name__, "name": c.objectName(), "text": text[:40], "x": pos.x(),
                        "y": pos.y(), "width": c.width(), "height": c.height(), "depth": depth}
                out_x = pos.x() < 0 or pos.x() + c.width() > g.width()
                out_y = pos.y() < 0 or pos.y() + c.height() > g.height()
                if (out_x or out_y) and not any(c is r or r.isAncestorOf(c) for r in reported):
                    reported.append(c)
                    info["outside"].append(item)
                if depth <= 2 and len(info["widgets"]) < 40:
                    info["widgets"].append(item)
                if not c.isWindow():
                    siblings.setdefault(id(c.parentWidget()), []).append((c, item))
            overlaps = []
            for group in siblings.values():
                for i, (a, ia) in enumerate(group):
                    for b, ib in group[i + 1 :]:
                        ow = min(ia["x"] + ia["width"], ib["x"] + ib["width"]) - max(ia["x"], ib["x"])
                        oh = min(ia["y"] + ia["height"], ib["y"] + ib["height"]) - max(ia["y"], ib["y"])
                        if ow > 2 and oh > 2 and len(overlaps) < 8:
                            overlaps.append([ia, ib, ow, oh])
            info["overlaps"] = overlaps
            result["windows"].append(info)
        if canvas is not None:
            painter.end()
            path = _os.path.join(out, "screen.png")
            canvas.save(path)
            result["screen_image"] = path
        with open(_os.path.join(out, "qt.json"), "w") as f:
            json.dump(result, f)
        if quit_after:
            app.quit()
            QtCore.QTimer.singleShot(3000, lambda: _os._exit(0))

    def patch(binding):
        """Wrap exec() of the application classes loaded so far (QtWidgets may come after QtGui)."""
        QtCore = _sys.modules.get(binding + ".QtCore")
        QtGui = _sys.modules.get(binding + ".QtGui")
        if QtCore is None or QtGui is None:
            return
        # A module can still be loading (QtWidgets imports QtGui): take only the classes that are there.
        classes = [getattr(QtCore, "QCoreApplication", None), getattr(QtGui, "QGuiApplication", None)]
        widgets = _sys.modules.get(binding + ".QtWidgets")
        if widgets is not None:
            classes.append(getattr(widgets, "QApplication", None))
        classes = [cls for cls in classes if cls is not None]

        def schedule():
            if not state["scheduled"]:
                state["scheduled"] = True
                open(_os.path.join(out, "started"), "w").close()  # the event loop runs: a capture is coming

                def run():
                    try:
                        capture(binding)
                    except Exception as e:  # the program goes on; lcode reports what went wrong
                        with open(_os.path.join(out, "qt.json"), "w") as f:
                            json.dump({"error": "%s: %s" % (type(e).__name__, e)}, f)

                QtCore.QTimer.singleShot(wait_ms, run)

        class Wrapped:
            """In place of exec(): schedules the capture, then runs the original, static or not."""

            def __init__(self, original):
                self.original = original

            def __get__(self, obj, objtype=None):
                target = self.original
                if hasattr(target, "__get__") and not isinstance(target, staticmethod):
                    target = target.__get__(obj, objtype)
                elif isinstance(target, staticmethod):
                    target = target.__func__

                def call(*args, **kwargs):
                    schedule()
                    return target(*args, **kwargs)

                return call

        for cls in classes:
            for name in ("exec", "exec_"):
                original = cls.__dict__.get(name)
                if original is None or isinstance(original, Wrapped):
                    continue
                try:
                    setattr(cls, name, Wrapped(original))
                except (TypeError, AttributeError):
                    pass

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            top, _, sub = name.partition(".")
            if top not in bindings or sub not in ("QtGui", "QtWidgets"):
                return None
            for finder in _sys.meta_path:
                if finder is self or not hasattr(finder, "find_spec"):
                    continue
                spec = finder.find_spec(name, path, target)
                if spec is not None and spec.loader is not None:
                    loader = spec.loader
                    run = loader.exec_module

                    def exec_module(module, _run=run, _top=top):
                        _run(module)
                        try:
                            patch(_top)
                        except Exception:
                            pass  # never break the program: without the hook there's just no screenshot

                    try:
                        loader.exec_module = exec_module
                    except (AttributeError, TypeError):
                        return None
                    return spec
            return None

    if out:
        _sys.meta_path.insert(0, Finder())


_lcode_shot()
del _lcode_shot

# A sitecustomize of the program's own environment, if it has one, still runs.
try:
    import importlib.machinery as _machinery

    _here = _os.path.dirname(_os.path.abspath(__file__))
    _rest = [p for p in _sys.path if _os.path.abspath(p or ".") != _here]
    _spec = _machinery.PathFinder.find_spec("sitecustomize", _rest)
    if _spec is not None and _spec.loader is not None:
        _module = type(_sys)("_lcode_original_sitecustomize")
        _spec.loader.exec_module(_module)
except Exception:
    pass
'''


def hook_dir(tmp: Path) -> Path:
    folder = tmp / "hook"
    folder.mkdir(exist_ok=True)
    (folder / "sitecustomize.py").write_text(QT_HOOK)
    return folder


# --------------------------------------------------------------------------------------------- running a program


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


def qt_offscreen(command: str, cwd: Path, screen: tuple[int, int], wait: int, tmp: Path, cancel=None) -> Shot:
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
    )
    env.pop("DISPLAY", None)
    log = tmp / "output.txt"
    proc = run_program(command, cwd, env, log)
    try:
        deadline = time.time() + wait + GRACE
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
    if not windows:
        return ["No visible Qt window."]
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

BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
    "microsoft-edge",
    "brave-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
FIREFOX = ("firefox", "/Applications/Firefox.app/Contents/MacOS/firefox")

MEASURE = r"""(() => {
  const vw = window.innerWidth, vh = window.innerHeight, d = document.documentElement;
  const name = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
    (el.classList && el.classList.length ? '.' + [...el.classList].slice(0, 2).join('.') : '');
  const text = el => ((el.innerText || el.value || el.alt || '') + '').trim().replace(/\s+/g, ' ').slice(0, 40);
  const out = [], seen = [];
  if (d.scrollWidth > vw + 1 && document.body) {
    for (const el of document.body.querySelectorAll('*')) {
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1 || (r.right <= vw + 1 && r.left >= -1)) continue;
      if (seen.some(s => s.contains(el))) continue;
      const st = getComputedStyle(el);
      if (st.visibility === 'hidden' || st.display === 'none') continue;
      seen.push(el);
      out.push({el: name(el), text: text(el), x: Math.round(r.left), y: Math.round(r.top + scrollY),
                w: Math.round(r.width), h: Math.round(r.height)});
      if (out.length >= 8) break;
    }
  }
  return {title: document.title, viewport: [vw, vh], page: [d.scrollWidth, d.scrollHeight], sideways: out,
          empty: !(document.body && document.body.innerText.trim()) && !document.querySelector('img,canvas,svg,video')};
})()"""


def find_browser() -> tuple[str, str] | None:
    """("chrome", path) for a browser lcode can drive, else ("firefox", path), else None."""
    for name in BROWSERS:
        path = shutil.which(name) or (name if name.startswith("/") and os.path.exists(name) else None)
        if path:
            return "chrome", path
    for name in FIREFOX:
        path = shutil.which(name) or (name if name.startswith("/") and os.path.exists(name) else None)
        if path:
            return "firefox", path
    return None


def normalize_url(url: str, cwd: Path) -> str:
    if re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        return url
    if re.match(r"^(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0)(:\d+)?(/|$)", url):
        return "http://" + url
    path = (cwd / os.path.expanduser(url)).resolve()
    if path.exists():
        return path.as_uri()
    if path.suffix.lower() in (".html", ".htm", ".xhtml", ".svg") or url.startswith(("/", "./", "../", "~")):
        raise ShotError(f"{url} doesn't exist here ({path})")
    if re.match(r"^[\w.-]+\.[a-z]{2,}(:\d+)?(/|$)", url, re.I):
        return "https://" + url
    raise ShotError(f"{url} is neither a URL nor a file here")


def is_local(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return (
        url.startswith("file:") or host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost")
    )


def answers(url: str) -> bool:
    """Whether something answers at the URL (an HTTP error is an answer: the page shows it)."""
    if url.startswith("file:"):
        return True
    try:
        with urllib.request.urlopen(url, timeout=2):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_for_server(url: str, proc: subprocess.Popen, timeout: int, log: Path, cancel=None) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cancel is not None and cancel():
            raise KeyboardInterrupt
        if answers(url):  # first: a command that hands off to a server and exits is fine too
            return
        if proc.poll() is not None:
            raise ShotError(
                f"the server command exited (exit code {proc.returncode}) before {url} answered"
                + (f"\nWhat it printed:\n{printed(log)}" if printed(log) else "")
            )
        time.sleep(0.5)
    raise ShotError(
        f"{url} didn't answer within {timeout}s of starting the server"
        + (f"\nWhat it printed:\n{printed(log)}" if printed(log) else "")
    )


def high_fd(fd: int) -> int:
    """The same file at a descriptor of 10 or more (the original is closed)."""
    if fd >= 10:
        return fd
    moved = fcntl.fcntl(fd, fcntl.F_DUPFD, 10)
    os.close(fd)
    return moved


class DevTools:
    """Chrome's DevTools protocol over a pipe (--remote-debugging-pipe): no websocket, no extra package."""

    def __init__(self, browser: str, tmp: Path) -> None:
        to_chrome_r, self.to_chrome = os.pipe()
        self.from_chrome, from_chrome_w = os.pipe()
        # Above 9, so they can't clash with the 3 and 4 they're moved to.
        to_chrome_r, from_chrome_w = high_fd(to_chrome_r), high_fd(from_chrome_w)
        # Chrome reads commands on fd 3 and writes replies on fd 4: the shell sets those up.
        script = f'exec "$0" "$@" 3<&{to_chrome_r} 4>&{from_chrome_w} {to_chrome_r}<&- {from_chrome_w}>&-'
        args = [
            "--headless=new",
            "--remote-debugging-pipe",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-sync",
            "--mute-audio",
            "--hide-scrollbars",
            f"--user-data-dir={tmp / 'profile'}",
            "about:blank",
        ]
        self.proc = subprocess.Popen(
            ["bash", "-c", script, browser, *args],
            pass_fds=(to_chrome_r, from_chrome_w),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        self.buffer = b""
        self.next_id = 0
        self.events: list[dict] = []
        self.session = ""

    def send(self, method: str, params: dict | None = None, timeout: float = 30, session: bool = True) -> dict:
        self.next_id += 1
        message = {"id": self.next_id, "method": method, "params": params or {}}
        if session and self.session:
            message["sessionId"] = self.session
        os.write(self.to_chrome, json.dumps(message).encode() + b"\0")
        deadline = time.time() + timeout
        while True:
            reply = self.read(deadline - time.time())
            if reply is None:
                raise ShotError(f"the browser didn't answer ({method})")
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise ShotError(f"the browser refused {method}: {reply['error'].get('message')}")
                return reply.get("result", {})
            if "method" in reply:
                self.events.append(reply)

    def read(self, timeout: float) -> dict | None:
        while b"\0" not in self.buffer:
            if timeout <= 0:
                return None
            start = time.time()
            ready, _, _ = select.select([self.from_chrome], [], [], timeout)
            if not ready:
                return None
            chunk = os.read(self.from_chrome, 1 << 20)
            if not chunk:
                return None
            self.buffer += chunk
            timeout -= time.time() - start
        message, _, self.buffer = self.buffer.partition(b"\0")
        try:
            return json.loads(message)
        except ValueError:
            return {}

    def pump(self, seconds: float, until: str | None = None) -> bool:
        """Collect events for a while; True as soon as the event `until` arrives."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            event = self.read(deadline - time.time())
            if event is None:
                return False
            if "method" in event:
                self.events.append(event)
                if until and event["method"] == until:
                    return True
        return False

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.send("Browser.close", session=False, timeout=3)
        for fd in (self.to_chrome, self.from_chrome):
            with contextlib.suppress(OSError):
                os.close(fd)
        stop(self.proc)


def chrome_shot(
    browser: str, url: str, screen: tuple[int, int], wait: int, full_page: bool, path: Path, tmp: Path
) -> Shot:
    w, h = screen
    tools = DevTools(browser, tmp)
    try:
        target = tools.send("Target.createTarget", {"url": "about:blank"}, session=False)["targetId"]
        tools.session = tools.send("Target.attachToTarget", {"targetId": target, "flatten": True}, session=False)[
            "sessionId"
        ]
        for method in ("Page.enable", "Runtime.enable", "Log.enable"):
            tools.send(method)
        tools.send(
            "Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False}
        )
        navigated = tools.send("Page.navigate", {"url": url}, timeout=60)
        if navigated.get("errorText"):
            raise ShotError(f"the browser couldn't open {url}: {navigated['errorText']}")
        loaded = any(e.get("method") == "Page.loadEventFired" for e in tools.events) or tools.pump(
            30, until="Page.loadEventFired"
        )
        tools.pump(wait)  # scripts draw, fonts and images arrive
        measured = tools.send("Runtime.evaluate", {"expression": MEASURE, "returnByValue": True})
        page = (measured.get("result") or {}).get("value") or {}
        params: dict = {"format": "png"}
        if full_page and page.get("page"):
            pw, ph = page["page"]
            params.update(
                captureBeyondViewport=True,
                clip={"x": 0, "y": 0, "width": max(w, pw), "height": min(max(h, ph), 16000), "scale": 1},
            )
        data = tools.send("Page.captureScreenshot", params, timeout=60)["data"]
        path.write_bytes(base64.b64decode(data))
        report = web_report(page, tools.events, screen, loaded)
        return Shot(image=path, how="headless Chrome", screen=screen, report=report)
    finally:
        tools.close()


def web_report(page: dict, events: list[dict], screen: tuple[int, int], loaded: bool) -> list[str]:
    lines = []
    if page:
        title = f' "{page["title"]}"' if page.get("title") else ""
        pw, ph = page.get("page") or [0, 0]
        vw, vh = page.get("viewport") or screen
        lines.append(f"- Page{title}: {pw}x{ph} in a {vw}x{vh} window")
        if pw > vw + 1:
            lines.append(f"  It's {pw - vw} pixels wider than the window, so it scrolls sideways. What sticks out:")
            for item in page.get("sideways", []):
                text = f' "{item["text"]}"' if item.get("text") else ""
                lines.append(f"  - {item['el']}{text}: {item['w']}x{item['h']} at ({item['x']}, {item['y']})")
        if page.get("empty"):
            lines.append("  The page shows no text or images.")
    if not loaded:
        lines.append("- The page hadn't finished loading (no load event within 30 seconds).")
    console = []
    for event in events:
        method, params = event.get("method"), event.get("params") or {}
        if method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            text = (details.get("exception") or {}).get("description") or details.get("text") or "an exception"
            source = str(details.get("url") or "").rsplit("/", 1)[-1]
            where = f" ({source}:{int(details.get('lineNumber') or 0) + 1})" if source else ""
            console.append(f"  error: {text.splitlines()[0][:300]}{where}")
        elif method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "warning", "assert"):
            parts = [str(a.get("value", a.get("description", ""))) for a in params.get("args", [])]
            console.append(f"  {params['type']}: {' '.join(parts)[:300]}")
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            if str(entry.get("url", "")).endswith("/favicon.ico"):
                continue  # the browser asks for it by itself: not the page's problem
            if entry.get("level") in ("error", "warning"):
                where = f" ({entry['url']})" if entry.get("url") else ""
                console.append(f"  {entry['level']}: {str(entry.get('text', ''))[:300]}{where}")
    if console:
        lines.append("- The browser console:")
        lines += list(dict.fromkeys(console))[:15]
    else:
        lines.append("- No errors in the browser console.")
    return lines


def firefox_shot(browser: str, url: str, screen: tuple[int, int], wait: int, path: Path, tmp: Path) -> Shot:
    w, h = screen
    profile = tmp / "firefox"
    profile.mkdir()
    done = subprocess.run(
        [
            browser,
            "--headless",
            "--no-remote",
            "--profile",
            str(profile),
            f"--window-size={w},{h}",
            "--screenshot",
            str(path),
            url,
        ],
        capture_output=True,
        timeout=60 + wait,
        start_new_session=True,
    )
    if done.returncode != 0 or not path.exists():
        raise ShotError(f"Firefox couldn't take the screenshot: {done.stderr.decode(errors='replace')[-300:]}")
    return Shot(
        image=path,
        how="headless Firefox",
        screen=screen,
        report=["- (Firefox takes the picture only: no console messages or sizes. Chrome or Chromium gives both.)"],
    )


def web(url: str, screen: tuple[int, int], wait: int, full_page: bool, path: Path, tmp: Path) -> Shot:
    found = find_browser()
    if found is None:
        raise ShotError(f"no browser to open the page with: {INSTALL_BROWSER}")
    kind, browser = found
    if kind == "chrome":
        return chrome_shot(browser, url, screen, wait, full_page, path, tmp)
    return firefox_shot(browser, url, screen, wait, path, tmp)


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
) -> Shot:
    """Take the screenshot. `cancel` is checked while waiting (return True to stop)."""
    wait = max(0, min(int(wait), MAX_WAIT))
    path = new_path(folder, label_for(command, url))
    with tempfile.TemporaryDirectory(prefix="lcode-shot-") as name:
        tmp = Path(name)
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
        shot = (qt_offscreen if mode == "qt" else virtual_display)(command, cwd, screen, wait, tmp, cancel)
        shutil.copyfile(shot.image, path)
        kept = []
        for i, window in enumerate(shot.windows):
            if window != shot.image and len(shot.windows) > 1:
                extra = path.with_name(f"{path.stem}-window-{i + 1}.png")
                shutil.copyfile(window, extra)
                kept.append(extra)
        shot.image, shot.windows = path, kept
        return shot
