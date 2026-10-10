"""A bug that only shows when the code runs, for `lcode bench --tasks runtime-bug`.

A camera wall lays out video panels in a window. While video plays, the window grows taller frame after
frame, past the screen, and can't be made smaller again: each panel's minimum size is taken from the
frame it shows, and the frame is scaled to the whole cell, status bar included. Reading the code, the
obvious fixes are band-aids (ignore the minimum sizes, or only fix the scaling) that fail part of the
hidden test. Running it, the window sizes show the ratchet. It's the kind of bug a GUI has, without
needing one, and rewards reproducing a problem before fixing it.
"""

from __future__ import annotations

import re
from pathlib import Path

WALL = '''\
"""A camera wall: a window with a grid of panels, each showing its camera's latest video frame."""

SPACING = 8  # pixels between and around the panels
STATUS_BAR = 20  # under its video, each panel shows the camera's name and status


class Panel:
    def __init__(self, name):
        self.name = name
        self.min_width = self.min_height = 1  # the panel can't get smaller than this
        self.video = (0, 0)  # the size of the frame on screen

    def show(self, frame_width, frame_height, cell_width, cell_height):
        """Show a frame scaled to fit the panel's cell, keeping its aspect ratio."""
        scale = min(cell_width / frame_width, cell_height / frame_height)
        self.video = (int(frame_width * scale), int(frame_height * scale))
        # The video and the status bar under it must stay visible.
        self.min_width, self.min_height = self.video[0], self.video[1] + STATUS_BAR


class Wall:
    def __init__(self, names, rows, cols):
        self.rows, self.cols = rows, cols
        self.panels = [Panel(name) for name in names]
        self.width = self.height = 0

    def resize(self, width, height):
        """Ask for a window size. The window can't be smaller than its panels need."""
        need_width = max(p.min_width for p in self.panels) * self.cols + SPACING * (self.cols + 1)
        need_height = max(p.min_height for p in self.panels) * self.rows + SPACING * (self.rows + 1)
        self.width, self.height = max(width, need_width), max(height, need_height)

    def cell(self):
        """The size of one panel's cell."""
        return (
            (self.width - SPACING * (self.cols + 1)) // self.cols,
            (self.height - SPACING * (self.rows + 1)) // self.rows,
        )

    def new_frame(self, frame_width, frame_height):
        """Every camera delivered a frame: show it in each panel, then let the layout settle."""
        cell_width, cell_height = self.cell()
        for panel in self.panels:
            panel.show(frame_width, frame_height, cell_width, cell_height)
        self.resize(self.width, self.height)
'''

SIMULATE = '''\
"""Play the camera wall without a screen: python simulate.py [WIDTH HEIGHT [ROWSxCOLS]]"""

import sys

from wall import Wall


def main():
    width, height = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (2560, 1080)
    rows, cols = map(int, sys.argv[3].split("x")) if len(sys.argv) > 3 else (3, 3)
    wall = Wall([f"camera {n + 1}" for n in range(rows * cols)], rows, cols)
    wall.resize(width * 9 // 10, height * 9 // 10)
    print(f"{rows}x{cols} wall on a {width}x{height} screen, window {wall.width}x{wall.height}")
    for n in range(1, 6):
        wall.new_frame(1920, 1080)
        print(f"after frame {n}: window {wall.width}x{wall.height}, video {wall.panels[0].video}")


if __name__ == "__main__":
    main()
'''

FILES = {"wall.py": WALL, "simulate.py": SIMULATE}

PROMPT = (
    "Users say the camera wall in wall.py keeps growing taller while video plays, until it's bigger than "
    "their screen, and after that it can't be made smaller again. Fix it."
)

HIDDEN_TEST = """\
import unittest

from wall import SPACING, STATUS_BAR, Wall

SCREENS = [(2560, 1080, 3, 3), (1920, 1080, 2, 3), (1366, 768, 3, 3), (3440, 1440, 2, 4), (1280, 1024, 2, 2)]


def play(width, height, rows, cols, frames=30, frame=(1920, 1080)):
    wall = Wall([f"camera {n}" for n in range(rows * cols)], rows, cols)
    wall.resize(width, height)
    for _ in range(frames):
        wall.new_frame(*frame)
    return wall


class HiddenTest(unittest.TestCase):
    def test_keeps_its_size(self):
        for width, height, rows, cols in SCREENS:
            with self.subTest(screen=(width, height), grid=(rows, cols)):
                wall = play(width * 9 // 10, height * 9 // 10, rows, cols)
                self.assertEqual((wall.width, wall.height), (width * 9 // 10, height * 9 // 10))

    def test_can_be_made_smaller(self):
        wall = play(2304, 972, 3, 3)
        wall.resize(1280, 720)
        wall.new_frame(1920, 1080)
        self.assertEqual((wall.width, wall.height), (1280, 720))

    def test_video_fits_and_fills_its_cell(self):
        for width, height, rows, cols in SCREENS:
            for frame in [(1920, 1080), (1280, 960), (720, 1280)]:
                with self.subTest(screen=(width, height), grid=(rows, cols), frame=frame):
                    wall = play(width * 9 // 10, height * 9 // 10, rows, cols, frames=3, frame=frame)
                    cell_width = (wall.width - SPACING * (cols + 1)) // cols
                    cell_height = (wall.height - SPACING * (rows + 1)) // rows
                    for panel in wall.panels:
                        video_width, video_height = panel.video
                        self.assertLessEqual(video_width, cell_width)
                        self.assertLessEqual(video_height + STATUS_BAR, cell_height)
                        fills = video_width >= cell_width - 2 or video_height + STATUS_BAR >= cell_height - 2
                        self.assertTrue(fills, "the video doesn't use the room it has")
                        self.assertAlmostEqual(video_width / video_height, frame[0] / frame[1], delta=0.02)
"""

PROBLEMS = {
    "test_keeps_its_size": "the window still grows",
    "test_can_be_made_smaller": "the window can't be made smaller",
    "test_video_fits_and_fills_its_cell": "the video doesn't fit its cell above the status bar (or doesn't fill it)",
}


def check(folder: Path, answer: str):
    from lcode.bench import Check, last_line, run_python

    if not (folder / "wall.py").is_file():
        return Check(False, "wall.py is missing")
    (folder / "test_hidden_wall.py").write_text(HIDDEN_TEST)
    r = run_python(folder, "-m", "unittest", "-q", "test_hidden_wall")
    if r.returncode == 0:
        return Check(True, "keeps its size, can be made smaller, and the video fits")
    failed = [text for name, text in PROBLEMS.items() if re.search(rf"\b{name}\b", r.stderr)]
    return Check(False, "; ".join(failed) or last_line(r.stderr) or last_line(r.stdout))
