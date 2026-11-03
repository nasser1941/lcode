"""The hook lcode puts into a Qt program it screenshots: Python's sitecustomize, which offscreen Qt runs at startup."""

from __future__ import annotations

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
    actions = json.loads(_os.environ.get("LCODE_SHOT_ACTIONS") or "[]")
    pause_ms = int(float(_os.environ.get("LCODE_SHOT_PAUSE", "0.4")) * 1000)
    bindings = ("PyQt6", "PyQt5", "PySide6", "PySide2")
    state = {"scheduled": False}

    def capture(binding, steps=None):
        QtCore = _sys.modules[binding + ".QtCore"]
        QtGui = _sys.modules[binding + ".QtGui"]
        QtWidgets = _sys.modules.get(binding + ".QtWidgets")
        app = QtGui.QGuiApplication.instance()
        result = {"binding": binding, "windows": [], "screen": None, "platform": app.platformName()}
        if steps is not None:
            result["actions"] = steps
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

    def text_of(widget):
        for getter in ("text", "windowTitle", "title", "placeholderText"):
            fn = getattr(widget, getter, None)
            if callable(fn):
                try:
                    text = str(fn() or "")
                except Exception:
                    text = ""
                if text:
                    return text
        return ""

    def act(binding, then):
        """Take the steps one by one, pausing after each so the program reacts, then call then(log)."""
        QtCore = _sys.modules[binding + ".QtCore"]
        QtWidgets = _sys.modules[binding + ".QtWidgets"]
        Qt = QtCore.Qt
        try:
            QTest = importlib.import_module(binding + ".QtTest").QTest
        except Exception as e:
            then([{"step": s["raw"], "error": "Qt's QtTest module isn't available (%s)" % e} for s in actions])
            return
        app = QtWidgets.QApplication.instance()

        def enum(group, name):  # scoped enums (PyQt6, PySide6), or flat ones (PyQt5, PySide2)
            scope = getattr(Qt, group, None)
            if scope is not None and hasattr(scope, name):
                return getattr(scope, name)
            return getattr(Qt, name)

        def window():
            tops = [w for w in app.topLevelWidgets() if w.isVisible() and w.width() > 1 and w.height() > 1]
            active = app.activeWindow()
            return active if active in tops else (tops[0] if tops else None)

        def describe(widget):
            text = text_of(widget)[:40]
            return type(widget).__name__ + (' "%s"' % text if text else "")

        def by_text(top, wanted):
            wanted = wanted.casefold()
            found = [c for c in top.findChildren(QtWidgets.QWidget)
                     if c.isVisible() and wanted in text_of(c).casefold()]  # fmt: skip
            return min(found, key=lambda c: c.width() * c.height()) if found else None

        log = []

        def step(i):
            if i >= len(actions):
                then(log)
                return
            s, note, pause = actions[i], {"step": actions[i]["raw"]}, pause_ms
            try:
                top = window()
                if s["do"] == "wait":
                    pause = int(s["seconds"] * 1000)
                elif top is None:
                    note["error"] = "no window to act on"
                elif s["do"] in ("click", "double_click", "right_click"):
                    if "text" in s:
                        target = by_text(top, s["text"])
                        pos = target.rect().center() if target is not None else None
                    else:
                        point = QtCore.QPoint(s["x"], s["y"])
                        target = top.childAt(point) or top
                        pos = target.mapFrom(top, point)
                    if target is None:
                        note["error"] = 'no visible widget shows "%s"' % s["text"]
                    else:
                        button = enum("MouseButton", "RightButton" if s["do"] == "right_click" else "LeftButton")
                        none = enum("KeyboardModifier", "NoModifier")
                        at = target.mapTo(top, pos)  # before the click moves things around
                        note["target"] = "%s at (%d, %d)" % (describe(target), at.x(), at.y())
                        click = QTest.mouseDClick if s["do"] == "double_click" else QTest.mouseClick
                        click(target, button, none, pos)
                else:
                    target = app.focusWidget() or top
                    if s["do"] == "key":
                        mods = enum("KeyboardModifier", "NoModifier")
                        names = {"ctrl": "ControlModifier", "shift": "ShiftModifier", "alt": "AltModifier",
                                 "meta": "MetaModifier"}  # fmt: skip
                        for m in s["mods"]:
                            mods = mods | enum("KeyboardModifier", names[m])
                        QTest.keyClick(target, enum("Key", "Key_" + s["key"]), mods)
                    else:
                        QTest.keyClicks(target, s["text"])
                    note["target"] = describe(target)
            except Exception as e:  # one step failing doesn't stop the picture
                note["error"] = "%s: %s" % (type(e).__name__, e)
            log.append(note)
            QtCore.QTimer.singleShot(pause, lambda: step(i + 1))

        step(0)

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

                def run(steps=None):
                    try:
                        capture(binding, steps)
                    except Exception as e:  # the program goes on; lcode reports what went wrong
                        with open(_os.path.join(out, "qt.json"), "w") as f:
                            json.dump({"error": "%s: %s" % (type(e).__name__, e)}, f)

                def start():
                    if not actions:
                        run()
                        return
                    try:
                        act(binding, run)
                    except Exception as e:
                        run([{"step": "actions", "error": "%s: %s" % (type(e).__name__, e)}])

                QtCore.QTimer.singleShot(wait_ms, start)

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
