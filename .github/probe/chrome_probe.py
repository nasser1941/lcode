"""Temporary CI probe (removed before merge): start headless Chrome like lcode does, many times, and time its
answer to the first DevTools command. Slow starts print Chrome's stderr; after a hang, a second start is timed."""

import fcntl
import json
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BROWSER = sys.argv[1]
RUNS = int(sys.argv[2])
LIMIT = 40.0


def high_fd(fd):
    moved = fcntl.fcntl(fd, fcntl.F_DUPFD, 10)
    os.close(fd)
    return moved


def once(i):
    tmp = Path(tempfile.mkdtemp())
    log = tmp / "chrome.log"
    to_r, to_w = os.pipe()
    from_r, from_w = os.pipe()
    to_r, from_w = high_fd(to_r), high_fd(from_w)
    script = f'exec "$0" "$@" 3<&{to_r} 4>&{from_w} {to_r}<&- {from_w}>&-'
    args = [
        "--headless=new", "--remote-debugging-pipe", "--no-first-run", "--no-default-browser-check", "--disable-gpu",
        "--disable-extensions", "--disable-sync", "--mute-audio", "--hide-scrollbars",
        f"--user-data-dir={tmp / 'profile'}", "--enable-logging=stderr", "--v=0", "about:blank",
    ]
    err = open(log, "wb")
    start = time.time()
    p = subprocess.Popen(["bash", "-c", script, BROWSER, *args], pass_fds=(to_r, from_w), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=err, start_new_session=True)
    os.close(to_r)
    os.close(from_w)
    os.write(to_w, json.dumps({"id": 1, "method": "Target.createTarget", "params": {"url": "about:blank"}}).encode() + b"\0")
    buf, answer, marks = b"", None, []
    while time.time() - start < LIMIT:
        r, _, _ = select.select([from_r], [], [], 0.5)
        if r:
            c = os.read(from_r, 1 << 20)
            if not c:
                answer = "EOF"
                break
            buf += c
            if b'"id":1' in buf:
                answer = "ok"
                break
    took = time.time() - start
    alive = p.poll() is None
    try:
        os.write(to_w, json.dumps({"id": 2, "method": "Browser.close", "params": {}}).encode() + b"\0")
    except OSError:
        pass
    try:
        p.wait(5)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, 9)
        p.wait()
    os.close(to_w)
    os.close(from_r)
    err.close()
    answer = answer or "timeout"
    slow = took > 3 or answer != "ok"
    print(f"{i:4d} {answer:7s} {took:6.2f}s alive={alive}{'  <-- SLOW' if slow else ''}", flush=True)
    if slow:
        print("---- chrome stderr ----")
        print(log.read_text(errors="replace")[-6000:])
        print("---- end ----", flush=True)
    return slow


print("chrome:", subprocess.run([BROWSER, "--version"], capture_output=True, text=True).stdout.strip())
print("DBUS_SESSION_BUS_ADDRESS =", os.environ.get("DBUS_SESSION_BUS_ADDRESS"), "| DISPLAY =", os.environ.get("DISPLAY"))
slow_count = 0
for i in range(RUNS):
    if once(i):
        slow_count += 1
        print("retrying straight after the slow start:")
        once(i + 10000)
print(f"slow starts: {slow_count} of {RUNS}")
