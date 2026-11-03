"""lcode trace export: a copy of a trace for a bug report, with secrets, home-folder paths, user names, and email
and IP addresses masked."""

from __future__ import annotations

import contextlib
import datetime as dt
import getpass
import ipaddress
import json
import os
import re
import socket
import subprocess
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from rich.console import Console
from rich.markup import escape

from lcode import __version__
from lcode.trace_view import Info, load
from lcode.traces import GIT_TIMEOUT

if TYPE_CHECKING:
    pass


EMAIL = re.compile(r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")


PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)")


OTHER_HOME = re.compile(r"(?<![\w.-])(/home/|/Users/)[^/\s\"'<>:;,|]+")


WINDOWS_HOME = re.compile(r"(?i)\b([A-Z]:\\+Users\\+)[^\\\s\"'<>:;,|]+")


SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION", re.I)


class Masker:
    """Masks what a bug report shouldn't carry: secrets, your home folder, your user and host names, email and
    IP addresses. Errs on the side of masking too much."""

    def __init__(self) -> None:
        from lcode import secretscan

        self.secretscan = secretscan
        self.counts: Counter = Counter()
        self.home = str(Path.home())
        names: set[str] = set()
        with contextlib.suppress(Exception):
            names.add(getpass.getuser())
        with contextlib.suppress(Exception):
            import pwd

            names.add(pwd.getpwnam(getpass.getuser()).pw_gecos.split(",")[0])
        for key in ("user.name", "user.email"):
            with contextlib.suppress(Exception):
                value = subprocess.run(
                    ["git", "config", "--global", "--get", key], capture_output=True, text=True, timeout=GIT_TIMEOUT
                ).stdout.strip()
                names.add(value)
        self.names = sorted((n.strip() for n in names if len(n.strip()) >= 3), key=len, reverse=True)
        self.hosts = []
        with contextlib.suppress(Exception):
            host = socket.gethostname()
            self.hosts = sorted({host, host.split(".")[0]} - {"", "localhost"}, key=len, reverse=True)
        # Values of secret-looking environment variables: masked wherever they turn up, whatever they look like.
        self.values = sorted(
            {v for k, v in os.environ.items() if SECRET_NAME.search(k) and len(v) >= 8 and not v.startswith("/")},
            key=len,
            reverse=True,
        )

    def text(self, value: str) -> str:
        if not value:
            return value
        for secret in self.values:
            if secret in value:
                self.counts["secrets"] += value.count(secret)
                value = value.replace(secret, "<secret>")
        value, n = PRIVATE_KEY.subn("<private key>", value)
        self.counts["secrets"] += n
        for _, pattern in self.secretscan.STRONG:
            value, n = pattern.subn("<secret>", value)
            self.counts["secrets"] += n
        value = self.secretscan.URL_PASSWORD.sub(self._url_password, value)
        value = self.secretscan.ASSIGNED.sub(self._assigned, value)
        if self.home and self.home != "/":
            n = value.count(self.home)
            if n:
                value = value.replace(self.home, "~")
                self.counts["home folder paths"] += n
        value, n = OTHER_HOME.subn(r"\1<user>", value)
        self.counts["home folder paths"] += n
        value, n = WINDOWS_HOME.subn(r"\1<user>", value)
        self.counts["home folder paths"] += n
        value, n = EMAIL.subn("<email>", value)
        self.counts["email addresses"] += n
        for name in self.names:
            value, n = re.subn(rf"(?<![\w]){re.escape(name)}(?![\w])", "<user>", value, flags=re.I)
            self.counts["user names"] += n
        for host in self.hosts:
            value, n = re.subn(rf"(?<![\w.-]){re.escape(host)}(?![\w-])", "<host>", value, flags=re.I)
            self.counts["host names"] += n
        return IPV4.sub(self._address, value)

    def _url_password(self, m: re.Match) -> str:
        if self.secretscan.placeholder(m.group("value")):
            return m.group(0)
        self.counts["secrets"] += 1
        return m.group(0).replace(f":{m.group('value')}@", ":<secret>@")

    def _assigned(self, m: re.Match) -> str:
        value = m.group("value")
        if self.secretscan.placeholder(value) or self.secretscan.ABOUT.search(m.group("name")) or value.isdigit():
            return m.group(0)
        self.counts["secrets"] += 1
        return m.group(0).replace(value, "<secret>")

    def _address(self, m: re.Match) -> str:
        try:
            address = ipaddress.ip_address(m.group(0))
        except ValueError:
            return m.group(0)
        if address.is_loopback or address.is_unspecified:
            return m.group(0)
        self.counts["IP addresses"] += 1
        return "<ip>"

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        return value


def export(info: Info, out: TextIO | None = None, path: Path | None = None) -> Counter:
    """Write the trace with secrets and personal details masked; returns what was masked, by kind."""
    masker = Masker()
    lines = []
    for event in load(info.path):
        masked = masker.value(event)
        if masked.get("type") == "start":
            masked["exported"] = {
                "lcode": __version__,
                "time": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        lines.append(json.dumps(masked, ensure_ascii=False))
    text = "\n".join(lines) + "\n"
    if path is not None:
        path.write_text(text, encoding="utf-8")
    if out is not None:
        out.write(text)
    return masker.counts


def export_command(info: Info, output: str | None, console: Console) -> int:
    import sys

    if output == "-":
        export(info, out=sys.stdout)
        return 0
    path = Path(output).expanduser() if output else Path.cwd() / f"lcode-trace-{info.id}.jsonl"
    try:
        counts = export(info, path=path)
    except OSError as e:
        console.print(f"[red]Can't write {escape(str(path))}: {escape(str(e))}[/]")
        return 1
    masked = ", ".join(f"{n} {kind}" for kind, n in counts.most_common() if n) or "nothing needed it"
    console.print(f"Exported to {escape(str(path))}")
    console.print(f"[dim]Masked: {escape(masked)}. Read it before you share it: masking can miss things.[/]")
    return 0
