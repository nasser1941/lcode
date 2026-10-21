"""Looking at a window before calling it done: the screenshot tool's actions (click, double-click, key, type, wait
before the picture), the check that sends back window code the model never looked at, and repeats that differ only
in their timings. From a real session: a double-click feature 'fixed' four times without one look at the window."""

import os
import shutil
import subprocess
import textwrap

import pytest

from conftest import call, output, reply
from lcode import checks
from lcode import screenshots as sh
from lcode.repeats import Repeats

QT_PYTHON = os.environ.get("LCODE_TEST_QT_PYTHON")  # a Python with PyQt6, for the real run below


# ----------------------------------------------------------------------------- the screenshot tool's actions


def test_actions_are_parsed_the_way_a_user_would_say_them():
    steps = sh.parse_actions(
        ["double_click 300,200", 'click "Save"', "double-click Camera 1", "key ctrl+s", "key esc", "type hello", "wait"]
    )
    assert steps[0] == {"do": "double_click", "raw": "double_click 300,200", "x": 300, "y": 200}
    assert steps[1]["text"] == "Save" and steps[2]["do"] == "double_click" and steps[2]["text"] == "Camera 1"
    assert (steps[3]["key"], steps[3]["mods"]) == ("S", ["ctrl"]) and steps[4]["key"] == "Escape"
    assert steps[5]["text"] == "hello" and steps[6]["seconds"] == 1
    assert [s["raw"] for s in sh.parse_actions("click 10,20; wait 2\nkey Return")] == [
        "click 10,20",
        "wait 2",
        "key Return",
    ]
    assert sh.parse_actions(None) == [] and sh.actions_time(sh.parse_actions(["wait 2", "click 1,1"])) == 2.8


@pytest.mark.parametrize(
    ("action", "says"),
    [
        ("hover 10,10", "actions are click"),
        ("click", "say where"),
        ("key", "name the key"),
        ("key hyper+x", "modifiers are"),
        ("key PrintScreen", "unknown key"),
        ("type", "give the text"),
        ("wait soon", "wait takes seconds"),
    ],
)
def test_a_wrong_action_says_how_to_write_it(action, says):
    with pytest.raises(sh.ShotError, match=says):
        sh.parse_actions([action])


def test_actions_only_for_qt_programs(tmp_path):
    with pytest.raises(sh.ShotError, match="not web pages"):
        sh.take(tmp_path, sh.shots_dir(tmp_path), url="https://example.com", actions=sh.parse_actions(["click 1,1"]))
    with pytest.raises(sh.ShotError, match="Python Qt programs"):
        sh.take(tmp_path, sh.shots_dir(tmp_path), command="xterm", mode="xvfb", actions=sh.parse_actions(["wait 1"]))


def test_the_report_says_what_each_step_hit():
    data = {
        "screen": [1366, 768],
        "windows": [{"title": "Wall", "class": "Wall", "x": 0, "y": 0, "width": 800, "height": 600}],
        "actions": [
            {"step": "double_click Camera 1", "target": 'QLabel "Camera 1 — Connected" at (207, 317)'},
            {"step": 'click "Gone"', "error": 'no visible widget shows "Gone"'},
            {"step": "wait 1"},
        ],
    }
    lines = sh.qt_report(data)
    assert lines[:4] == [
        "Steps, before the picture:",
        '- double_click Camera 1: on QLabel "Camera 1 — Connected" at (207, 317)',
        '- click "Gone": couldn\'t: no visible widget shows "Gone"',
        "- wait 1: done",
    ]


def test_the_tool_passes_the_actions_on(make_agent, fake_take):
    agent = make_agent(
        [
            reply(tool_calls=[call("screenshot", command="python wall.py", actions=["double_click Camera 1"])]),
            reply(tool_calls=[call("screenshot", command="python wall.py", actions=["hover 1,1"])]),
            reply("Seen."),
        ]
    )
    agent.run_turn("Does double-clicking a camera fill the window?")
    assert [s["raw"] for s in fake_take[0]["actions"]] == ["double_click Camera 1"]
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert results[1].startswith("Error:") and "actions are click" in results[1] and len(fake_take) == 1


@pytest.fixture
def fake_take(monkeypatch):
    calls = []

    def take(cwd, folder, **kwargs):
        calls.append(kwargs)
        image = folder / "20261021-120000-wall.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        return sh.Shot(image=image, how="Qt offscreen (PyQt6)", screen=(1366, 768), report=[], output="")

    monkeypatch.setattr(sh, "take", take)
    return calls


@pytest.mark.skipif(not QT_PYTHON, reason="set LCODE_TEST_QT_PYTHON to a Python with PyQt6")
def test_a_real_qt_program_is_clicked_before_the_picture(tmp_path):
    (tmp_path / "app.py").write_text(
        textwrap.dedent(
            """
            from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

            class Big(QLabel):
                def mouseDoubleClickEvent(self, event):
                    self.setText("zoomed in")

            app = QApplication([])
            window = QWidget()
            window.setWindowTitle("Demo")
            layout = QVBoxLayout(window)
            label = Big("Camera 1")
            count = QLabel("clicks 0")
            button = QPushButton("Add")
            button.clicked.connect(lambda: count.setText("clicks %d" % (int(count.text().split()[1]) + 1)))
            field = QLineEdit()
            for widget in (label, count, button, field):
                layout.addWidget(widget)
            window.resize(400, 300)
            window.show()
            field.setFocus()
            app.exec()
            """
        )
    )
    # typing goes where the keyboard focus is (the field, until a click moves it to the button)
    steps = sh.parse_actions(["double_click Camera 1", "type hello", "click Add", "click Add", 'click "Nowhere"'])
    shot = sh.take(tmp_path, sh.shots_dir(tmp_path), command=f"{QT_PYTHON} app.py", wait=1, mode="qt", actions=steps)
    report = "\n".join(shot.report)
    assert '- double_click Camera 1: on Big "Camera 1" at' in report
    assert '- click Add: on QPushButton "Add"' in report
    assert "- type hello: on QLineEdit" in report
    assert '- click "Nowhere": couldn\'t: no visible widget shows "Nowhere"' in report
    assert '"zoomed in"' in report and '"clicks 2"' in report and '"hello"' in report


# ----------------------------------------------------------------------------- windows it never looked at

WINDOW = "from PyQt6.QtWidgets import QApplication, QLabel\n\napp = QApplication([])\nQLabel('hi').show()\napp.exec()\n"


def test_window_code_is_recognized_and_a_look_counts(tmp_path):
    (tmp_path / "wall.py").write_text(WINDOW)
    (tmp_path / "tk.py").write_text("import tkinter as tk\n")
    (tmp_path / "logic.py").write_text("def add(a, b):\n    return a + b\n")
    changed = ["wall.py", "tk.py", "logic.py", "gone.py"]
    assert checks.unseen_windows(tmp_path, changed, {"bash"}) == {"wall.py": "PyQt6", "tk.py": "tkinter"}
    assert checks.unseen_windows(tmp_path, changed, {"screenshot"}) == {}
    assert checks.unseen_windows(tmp_path, changed, {"view_image"}) == {}
    note = checks.unseen_note({"wall.py": "PyQt6"})
    assert "never looked at it" in note and 'actions=["double_click' in note


def test_a_window_change_without_a_look_is_sent_back_once(make_agent, repo, fake_take):
    write = call("write_file", path="wall.py", content=WINDOW)
    agent = make_agent(
        [
            reply(tool_calls=[write, call("bash", command="python -c 'import ast'")]),
            reply("Done: double-click works."),
            reply(tool_calls=[call("screenshot", command="python wall.py", actions=["double_click hi"])]),
            reply("It shows the label."),
        ],
        checks="auto",
    )
    agent.run_turn("Make the label zoom on double-click")
    notes = [m["content"] for m in agent.messages if m["role"] == "user" and "never looked at it" in m["content"]]
    assert len(notes) == 1 and "wall.py" in notes[0] and "(PyQt6)" in notes[0]
    assert agent.messages[-1]["content"] == "It shows the label." and not agent.ollama.scripts
    assert "never looked at wall.py" in output(agent)


def test_no_check_when_checks_are_off_or_nothing_draws(make_agent, repo):
    agent = make_agent([reply(tool_calls=[call("write_file", path="wall.py", content=WINDOW)]), reply("Done.")])
    agent.run_turn("Change the window")  # checks off (the test default)
    assert agent.messages[-1]["content"] == "Done."
    logic = call("write_file", path="logic.py", content="X = 1\n")
    agent = make_agent([reply(tool_calls=[logic]), reply("Done.")], checks="auto")
    agent.run_turn("Change the logic")
    assert agent.messages[-1]["content"] == "Done."


# ----------------------------------------------------------------------------- repeats that differ only in timing


def test_a_rerun_that_only_took_a_different_time_is_a_repeat():
    repeats = Repeats()
    command = {"command": ".venv/bin/python -m pytest -q"}
    verdicts = [repeats.check("bash", command, f"75 passed in 0.{n}s\n[exit code: 0]") for n in (14, 12, 15)]
    assert [bool(v.note) for v in verdicts] == [False, False, True]
    changed = Repeats()
    runs = [changed.check("bash", command, f"{n} failed in 0.1s\n[exit code: 1]") for n in (3, 2, 1)]
    assert not any(v.note for v in runs)  # the results changed: not a repeat


def test_the_real_qt_test_can_find_a_python(tmp_path):
    """Documents how to run the real Qt test: any Python with PyQt6 will do."""
    if not QT_PYTHON:
        pytest.skip("set LCODE_TEST_QT_PYTHON to a Python with PyQt6")
    done = subprocess.run([QT_PYTHON, "-c", "import PyQt6.QtTest"], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert shutil.which(QT_PYTHON) or os.path.exists(QT_PYTHON)


def test_qt_warnings_are_pulled_out_of_what_the_program_printed(tmp_path):
    log = tmp_path / "output.txt"
    lines = ["2026-10-21 [INFO] Camera 1: connecting"] * 40
    lines[3] = 'QWidget::setLayout: Attempting to set QLayout "" on FocusedCamera "", which already has a layout'
    lines[5] = lines[3]  # said twice, listed once
    lines[7] = "This plugin does not support propagateSizeHints()"  # offscreen noise
    lines[9] = "qt.qpa.fonts: Populating font family aliases took 50 ms"
    lines[39] = "QPainter::begin: Paint device returned engine == 0, type: 2"
    log.write_text("\n".join(lines))
    assert sh.qt_warnings(log) == [lines[3], lines[39]]
    assert sh.qt_warnings(tmp_path / "missing.txt") == []
