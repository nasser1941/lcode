"""Screenshots of web pages: a headless Chrome over the DevTools protocol, or Firefox, and the local server
they load."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import re
import select
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from lcode.screenshots import Shot, ShotError, printed, stop

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


COMMAND_TIMEOUT = 30  # seconds for the browser to answer a command


# Seconds for the browser to start. The first start after a reboot reads it all from a cold disk: 6-12 s on GitHub's
# machines, and over 30 s while they were busy installing. Later starts take a fraction of a second.
STARTUP_TIMEOUT = 90


INSTALL_BROWSER = "install Google Chrome or Chromium (or Firefox)"


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
        self.log = tmp / "browser.txt"  # what the browser prints: an error quotes its last lines
        with open(self.log, "wb") as printed:
            self.proc = subprocess.Popen(
                ["bash", "-c", script, browser, *args],
                pass_fds=(to_chrome_r, from_chrome_w),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=printed,
                start_new_session=True,
            )
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        self.buffer = b""
        self.next_id = 0
        self.events: list[dict] = []
        self.session = ""
        self.answered = False  # it has answered something, so it's running
        self.closed = False  # it closed its end of the pipe: it stopped

    def send(
        self,
        method: str,
        params: dict | None = None,
        timeout: float | None = None,
        session: bool = True,
        waiting: str = "",
    ) -> dict:
        """Run a command and return its result. `waiting` names what an error says it didn't do in time."""
        timeout = COMMAND_TIMEOUT if timeout is None else timeout
        self.next_id += 1
        message = {"id": self.next_id, "method": method, "params": params or {}}
        if session and self.session:
            message["sessionId"] = self.session
        os.write(self.to_chrome, json.dumps(message).encode() + b"\0")
        deadline = time.time() + timeout
        while True:
            reply = self.read(deadline - time.time())
            if reply is None:
                waiting = waiting or f"answer {method}"
                if self.closed:
                    raise ShotError(f"the browser stopped before it could {waiting}{self.last_lines()}")
                raise ShotError(f"the browser didn't {waiting} within {timeout:g} seconds{self.last_lines()}")
            self.answered = True
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
                self.closed = True
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

    def last_lines(self) -> str:
        """The browser's last lines, without the D-Bus complaints it prints on any machine without a desktop."""
        try:
            lines = self.log.read_text(errors="replace").splitlines()
        except OSError:
            return ""
        # Chrome starts each line with [process:thread:date/time:LEVEL:source]
        lines = [re.sub(r"^\[[^\]]*\]\s*", "", line).strip() for line in lines if "dbus" not in line.lower()]
        lines = [line[:200] for line in lines if line][-3:]
        return f". It printed: {' | '.join(lines)}" if lines else ""

    def close(self) -> None:
        if self.answered and not self.closed:  # one that never answered won't answer this either
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
        # The first command, so its answer waits for the browser to start
        target = tools.send(
            "Target.createTarget", {"url": "about:blank"}, timeout=STARTUP_TIMEOUT, session=False, waiting="start"
        )["targetId"]
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
