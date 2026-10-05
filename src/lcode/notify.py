"""Desktop notifications: when a long request is done, or lcode waits for an answer during one.

Local models are slow, so people switch to something else while a request runs. lcode shows a
notification with `notify-send` on Linux and `osascript` on macOS, and rings the terminal bell
where neither is available.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import threading


def command(title: str, message: str) -> list[str] | None:
    """The command that shows a notification on this machine, or None."""
    if platform.system() == "Darwin" and shutil.which("osascript"):
        quoted = message.replace("\\", "\\\\").replace('"', '\\"')
        heading = title.replace("\\", "\\\\").replace('"', '\\"')
        return ["osascript", "-e", f'display notification "{quoted}" with title "{heading}"']
    if shutil.which("notify-send"):
        return ["notify-send", "--app-name=lcode", title, message]
    return None


def desktop(title: str, message: str) -> None:
    """Show the notification without waiting for it; the terminal bell if there's no way to."""
    argv = command(title, message[:200])
    if argv is None:
        sys.stderr.write("\a")
        sys.stderr.flush()
        return

    def run() -> None:
        try:
            subprocess.run(argv, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            sys.stderr.write("\a")
            sys.stderr.flush()

    threading.Thread(target=run, daemon=True).start()
