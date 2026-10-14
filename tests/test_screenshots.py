"""Visual checks (#116): screenshots of desktop programs and web pages without a screen."""

import json
import os
import shutil
import struct
import subprocess
import sys
import textwrap
import zlib

import pytest

from conftest import call, output, reply
from lcode import screenshots as sh

# --------------------------------------------------------------------------------------------- arguments


def test_screen_sizes():
    assert sh.parse_screen("") == sh.DEFAULT_SCREEN
    assert sh.parse_screen("1366x768") == (1366, 768)
    assert sh.parse_screen(" 800 × 600 ") == (800, 600)
    for bad in ("big", "10x10", "99999x100"):
        with pytest.raises(sh.ShotError):
            sh.parse_screen(bad)


def test_shots_go_to_a_folder_git_ignores(tmp_path):
    folder = sh.shots_dir(tmp_path)
    assert folder == tmp_path / ".lcode" / "screenshots"
    assert (folder / ".gitignore").read_text().splitlines()[-1] == "*"
    first = sh.new_path(folder, "Camera wall!")
    first.write_bytes(b"x")
    second = sh.new_path(folder, "Camera wall!")
    assert first.name.endswith("-camera-wall.png") and second != first


def test_shot_names():
    assert sh.label_for("/venv/bin/python wall.py --debug", None) == "wall"
    assert sh.label_for("xmessage -center 'Build finished'", None) == "xmessage"
    assert sh.label_for(None, "http://localhost:8000/shop?x=1") == "localhost:8000/shop"


def test_qt_programs_are_recognized(tmp_path):
    (tmp_path / "wall.py").write_text("from PyQt6.QtWidgets import QApplication\n")
    (tmp_path / "cli.py").write_text("import argparse\n")
    assert sh.looks_like_qt("python wall.py", tmp_path)
    assert sh.looks_like_qt("uv run python cli.py", tmp_path)  # the project's other modules use Qt
    assert not sh.looks_like_qt("npm run dev", tmp_path)
    plain = tmp_path / "plain"
    (plain / ".venv" / "lib").mkdir(parents=True)
    (plain / ".venv" / "lib" / "qt.py").write_text("import PySide6\n")  # a dependency, not the project
    (plain / "app.py").write_text("import tkinter\n")
    assert not sh.looks_like_qt("python app.py", plain)


def test_urls(tmp_path):
    (tmp_path / "index.html").write_text("<p>hi</p>")
    assert sh.normalize_url("index.html", tmp_path) == (tmp_path / "index.html").resolve().as_uri()
    assert sh.normalize_url("localhost:5173/app", tmp_path) == "http://localhost:5173/app"
    assert sh.normalize_url("example.com", tmp_path) == "https://example.com"
    for missing in ("missing.html", "./build/app"):
        with pytest.raises(sh.ShotError, match="doesn't exist here"):
            sh.normalize_url(missing, tmp_path)
    assert sh.is_local("http://127.0.0.1:8000/") and sh.is_local("file:///x.html") and sh.is_local("http://a.localhost")
    assert not sh.is_local("https://example.com/")


# --------------------------------------------------------------------------------------------- reports


def test_what_the_qt_measurements_say():
    panel = {"class": "Panel", "name": "panel-3", "text": "", "width": 640, "height": 480, "depth": 1}
    data = {
        "screen": [1366, 768],
        "windows": [
            {
                "title": "Camera Wall",
                "class": "Wall",
                "x": 0,
                "y": 0,
                "width": 1950,
                "height": 984,
                "min_width": 1950,
                "min_height": 984,
                "outside": [{**panel, "x": 909, "y": 9}],
                "overlaps": [[{**panel, "x": 9, "y": 9}, {**panel, "name": "panel-2", "x": 459, "y": 9}, 190, 480]],
                "widgets": [{**panel, "x": 909, "y": 9}],
            }
        ],
    }
    report = "\n".join(sh.qt_report(data))
    assert '- Window "Camera Wall" (Wall): 1950x984 at (0, 0) on a 1366x768 screen: 584x216 pixels' in report
    assert "Its layout needs at least 1950x984, more than the screen has" in report
    assert "Sticks out of the window: Panel #panel-3: 640x480 at (909, 9)" in report
    assert "Overlap (190x480): Panel #panel-3: 640x480 at (9, 9) and Panel #panel-2" in report
    assert sh.qt_report({"screen": [800, 600], "windows": []}) == ["No visible Qt window."]


def test_what_the_page_measurements_say():
    page = {
        "title": "Shop",
        "viewport": [1024, 700],
        "page": [1380, 700],
        "sideways": [{"el": "div.card", "text": "Table", "x": 936, "y": 66, "w": 444, "h": 224}],
    }
    events = [
        {
            "method": "Runtime.exceptionThrown",
            "params": {
                "exceptionDetails": {
                    "exception": {"description": "ReferenceError: renderPrices is not defined\n    at x"},
                    "url": "http://localhost:8000/app.js",
                    "lineNumber": 10,
                }
            },
        },
        {
            "method": "Log.entryAdded",
            "params": {"entry": {"level": "error", "text": "404", "url": "http://x/logo.png"}},
        },
        {
            "method": "Log.entryAdded",
            "params": {"entry": {"level": "error", "text": "404", "url": "http://x/favicon.ico"}},
        },
        {"method": "Runtime.consoleAPICalled", "params": {"type": "log", "args": [{"value": "chatter"}]}},
    ]
    report = "\n".join(sh.web_report(page, events, (1024, 700), True))
    assert '- Page "Shop": 1380x700 in a 1024x700 window' in report
    assert "356 pixels wider than the window" in report and 'div.card "Table": 444x224 at (936, 66)' in report
    assert "error: ReferenceError: renderPrices is not defined (app.js:11)" in report
    assert "logo.png" in report and "favicon" not in report and "chatter" not in report
    clean = sh.web_report({"page": [800, 600], "viewport": [800, 600]}, [], (800, 600), False)
    assert "No errors in the browser console." in clean[-1] and any("hadn't finished loading" in x for x in clean)


def read_png(data: bytes) -> tuple[int, int, bytes]:
    """Width, height and the raw rows of a PNG (all of its IDAT chunks)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    pos, compressed = 8, b""
    while pos < len(data):
        size, kind = struct.unpack(">I4s", data[pos : pos + 8])
        if kind == b"IDAT":
            compressed += data[pos + 8 : pos + 8 + size]
        pos += 12 + size
    return width, height, zlib.decompress(compressed)


def test_screen_dumps_become_pngs():
    # A 2x2 dump as Xvfb writes it: 32 bits per pixel, least significant byte first (B, G, R, X).
    header = [100, 7, 2, 24, 2, 2, 0, 0, 32, 0, 32, 32, 8, 4, 0xFF0000, 0xFF00, 0xFF, 8, 256, 0, 2, 2, 0, 0, 0]
    pixels = bytes([0, 0, 255, 0, 0, 255, 0, 0]) + bytes([255, 0, 0, 0, 255, 255, 255, 0])
    width, height, raw = read_png(sh.xwd_to_png(struct.pack(">25I", *header) + pixels))
    assert (width, height) == (2, 2)
    assert raw == b"\x00" + bytes([255, 0, 0, 0, 255, 0]) + b"\x00" + bytes([0, 0, 255, 255, 255, 255])
    with pytest.raises(sh.ShotError):
        sh.xwd_to_png(struct.pack(">25I", *[100, 7, 1, *header[3:]]) + pixels)  # not ZPixmap


# --------------------------------------------------------------------------------------------- the Qt hook

FAKE_QT = {
    "QtCore.py": """
        class QTimer:
            pending = []
            @staticmethod
            def singleShot(ms, fn):
                QTimer.pending.append(fn)
        class QCoreApplication:
            @staticmethod
            def exec():
                for fn in QTimer.pending:
                    fn()
                return 0
    """,
    "QtGui.py": """
        from FakeQt.QtCore import QCoreApplication
        class QGuiApplication(QCoreApplication):
            pass
    """,
    "QtWidgets.py": """
        from FakeQt.QtGui import QGuiApplication
        class QApplication(QGuiApplication):
            def __init__(self, argv):
                self.argv = argv
            def exec_(self):  # an instance method, as some bindings have
                return QGuiApplication.exec()
    """,
}


@pytest.mark.parametrize("call_style", ["app.exec()", "app.exec_()", "QApplication.exec()"])
def test_the_hook_runs_when_the_event_loop_starts(tmp_path, call_style):
    """The hook wraps exec() (static or not) and captures when the loop runs: shown with a stand-in binding."""
    package = tmp_path / "lib" / "FakeQt"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    for name, code in FAKE_QT.items():
        (package / name).write_text(textwrap.dedent(code))
    hook = sh.hook_dir(tmp_path)
    (hook / "sitecustomize.py").write_text(
        sh.QT_HOOK.replace('("PyQt6", "PyQt5", "PySide6", "PySide2")', '("FakeQt",)')
    )
    out = tmp_path / "out"
    out.mkdir()
    app = tmp_path / "app.py"
    app.write_text(f"from FakeQt.QtWidgets import QApplication\napp = QApplication([])\nprint('code', {call_style})\n")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(hook), str(tmp_path / "lib")]),
        "LCODE_SHOT_OUT": str(out),
        "LCODE_SHOT_WAIT": "0",
    }
    done = subprocess.run([sys.executable, str(app)], env=env, capture_output=True, text=True, timeout=60)
    assert "code 0" in done.stdout, done.stderr
    assert (out / "started").exists()
    data = json.loads((out / "qt.json").read_text())
    assert "error" in data  # the stand-in can't be grabbed: the hook reports that instead of crashing the program


def test_the_hook_does_nothing_unasked(tmp_path):
    done = subprocess.run(
        [sys.executable, "-c", "import sys; print('ok', [type(f).__name__ for f in sys.meta_path])"],
        env={**os.environ, "PYTHONPATH": str(sh.hook_dir(tmp_path))},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.stdout.startswith("ok") and "'Finder'" not in done.stdout  # lcode's finder isn't installed


# --------------------------------------------------------------------------------------------- the tool


@pytest.fixture
def fake_take(monkeypatch, tmp_path):
    calls = []

    def take(cwd, folder, **kwargs):
        calls.append(kwargs)
        image = folder / "20261014-120000-wall.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        return sh.Shot(
            image=image,
            how="Qt offscreen (PyQt6)",
            screen=(1366, 768),
            report=['- Window "Camera Wall" (Wall): 1950x984 at (0, 0) on a 1366x768 screen: 584x216 pixels'],
            output="started",
        )

    monkeypatch.setattr(sh, "take", take)
    return calls


def test_the_tool_runs_the_program_after_asking(make_agent, fake_take, monkeypatch):
    agent = make_agent(
        [reply(tool_calls=[call("screenshot", command="python wall.py", screen="1366x768")]), reply("Too big.")],
        mode="ask",
    )
    questions = []
    monkeypatch.setattr("builtins.input", lambda q: questions.append(q) or "y")
    agent.run_turn("does the wall fit a laptop screen?")
    result = agent.messages[-2]["content"]
    assert len(questions) == 1 and "python wall.py" in output(agent)
    assert fake_take[0]["command"] == "python wall.py" and fake_take[0]["screen"] == (1366, 768)
    assert result.startswith("Screenshot (Qt offscreen (PyQt6), 1366x768 screen): .lcode/screenshots/")
    assert "584x216 pixels" in result and "What the program printed:\nstarted" in result
    assert "No model that can see images is installed" in result
    assert "● screenshot(python wall.py)" in output(agent)
    assert agent.describe_call("screenshot", {"command": "cd /tmp/proj && .venv/bin/python wall.py"}) == (
        "screenshot(.venv/bin/python wall.py)"
    )


def test_a_denied_screenshot_doesnt_run(make_agent, fake_take, monkeypatch):
    agent = make_agent([reply(tool_calls=[call("screenshot", command="python wall.py")]), reply("ok")], mode="ask")
    monkeypatch.setattr("builtins.input", lambda q: "n")
    agent.run_turn("look at it")
    assert not fake_take and "denied" in agent.messages[-2]["content"]


def test_what_the_vision_model_sees_is_added(make_agent, fake_take):
    agent = make_agent([reply(tool_calls=[call("screenshot", command="python wall.py", question="Panels cut?")]),
                        reply("ok")])  # fmt: skip
    agent.ollama.capabilities["lcode-qwen3.6-35b"] = ["completion", "vision", "tools"]
    agent._vision = False
    agent.ollama.description = "Six panels; the right column is cut off."
    agent.run_turn("look at it")
    result = agent.messages[-2]["content"]
    assert "What it shows (described by lcode-qwen3.6-35b):\nSix panels; the right column is cut off." in result
    asked = agent.ollama.chats[-1]["messages"][0]["content"]
    assert "screenshot of the program `python wall.py` on a 1366x768 screen. Panels cut?" in asked


def test_what_the_tool_refuses(make_agent, fake_take):
    agent = make_agent()
    assert agent.tools.run("screenshot", {}).startswith("Error: give command")
    assert "1920x1080" in agent.tools.run("screenshot", {"url": "index.html", "screen": "big"})
    assert "nowhere.html doesn't exist here" in agent.tools.run("screenshot", {"url": "nowhere.html"})
    agent.sandbox = object()  # the sandbox is on
    assert "sandbox" in agent.tools.run("screenshot", {"command": "python wall.py"})
    agent.sandbox = None
    agent.settings.web = "off"
    assert "Web access is turned off" in agent.tools.run("screenshot", {"url": "https://example.com"})
    (agent.cwd / "index.html").write_text("<p>hi</p>")
    assert agent.tools.run("screenshot", {"url": "index.html"}).startswith("Screenshot")  # local: no web needed
    assert not fake_take[-1]["command"] and fake_take[-1]["url"].startswith("file://")


def test_a_failed_screenshot_says_why(make_agent, monkeypatch):
    def take(*args, **kwargs):
        raise sh.ShotError("no browser to open the page with: install Google Chrome")

    monkeypatch.setattr(sh, "take", take)
    agent = make_agent()
    (agent.cwd / "index.html").write_text("<p>hi</p>")
    result = agent.tools.run("screenshot", {"url": "index.html"})
    assert result == "Error: no screenshot: no browser to open the page with: install Google Chrome"


# --------------------------------------------------------------------------------------------- real programs


@pytest.mark.skipif(sh.find_browser() is None or sh.find_browser()[0] != "chrome", reason="no Chrome or Chromium")
def test_a_real_page_in_headless_chrome(tmp_path):
    (tmp_path / "index.html").write_text(
        "<title>Shop</title><body style='margin:0'><div style='width:1500px;height:50px'>wide</div>"
        "<script>missingFunction()</script></body>"
    )
    shot = sh.take(tmp_path, sh.shots_dir(tmp_path), url="index.html", screen=(1024, 600), wait=0)
    report = "\n".join(shot.report)
    assert shot.image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and read_png(shot.image.read_bytes())[:2] == (1024, 600)
    assert "1500x600 in a 1024x600 window" in report and "missingFunction is not defined" in report


@pytest.mark.skipif(not sh.xvfb_available() or not shutil.which("xmessage"), reason="no Xvfb or xmessage")
def test_a_real_program_on_a_virtual_display(tmp_path):
    shot = sh.take(tmp_path, sh.shots_dir(tmp_path), command="xmessage -center hello", screen=(800, 600), wait=1)
    assert shot.how == "a virtual display (Xvfb)" and read_png(shot.image.read_bytes())[:2] == (800, 600)
    assert any('"xmessage"' in line for line in shot.report)


def test_a_program_that_fails_to_start(tmp_path):
    (tmp_path / "app.py").write_text("raise SystemExit('no camera')\n")
    with pytest.raises(sh.ShotError) as e:
        sh.take(tmp_path, sh.shots_dir(tmp_path), command=f"{sys.executable} app.py", wait=0, mode="qt")
    assert "exited before showing a window" in str(e.value) and "no camera" in str(e.value)


@pytest.fixture
def local_site(tmp_path):
    """A web server answering on a free local port, as if one were running already."""
    import http.server
    import threading

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


def test_a_running_server_isnt_started_again(tmp_path, local_site, monkeypatch):
    def web(url, screen, wait, full_page, path, tmp):
        path.write_bytes(b"png")
        return sh.Shot(image=path, how="headless Chrome", screen=screen, report=["- Page: fine"])

    monkeypatch.setattr(sh, "web", web)
    marker = tmp_path / "started"
    shot = sh.take(tmp_path, sh.shots_dir(tmp_path), command=f"touch {marker}; sleep 30", url=local_site, wait=0)
    assert not marker.exists()  # the command never ran: the port was taken by the running server anyway
    assert (
        shot.report[-1]
        == f"({local_site} already answered, so `touch {marker}; sleep 30` wasn't started: a server is running.)"
    )


def test_a_server_that_answers_counts_even_if_its_command_ended(tmp_path, local_site):
    log = tmp_path / "server.txt"
    log.write_text("handed off to a daemon\n")
    proc = subprocess.Popen(["true"])
    proc.wait()
    sh.wait_for_server(local_site, proc, 5, log)  # no error
    with pytest.raises(sh.ShotError, match="exited \\(exit code 0\\) before"):
        sh.wait_for_server("http://127.0.0.1:9/", proc, 5, log)
