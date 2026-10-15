"""Notice when the network, not the model, is in the way.

A download that fails however its address is written usually means this machine can't reach the site: a
firewall, a proxy, a regional block, or no internet. Nothing the model writes fixes that, yet a small model
will try address after address for an hour (and lcode's repeat check never fires: every address differs).
So within one request lcode keeps track, per site, of the calls that failed to reach it (an HTTP error, a
name that doesn't resolve, a refused or timed-out connection…) and of the ones that worked. When `NOTE_AT`
different calls failed for a site and none worked, the result gets a note: check one address that must
exist, and if that fails too, tell the user. If `STOP_AFTER_NOTE` more fail, lcode ends the request.

Which site a failure belongs to is read from the output: the address on the failing line, else the last
address printed before it, else the command's address when the command names only one site. A call that
names several sites and prints a bare status line counts for none of them. A site counts as reached when a
call got an answer from it: a success status, a page from web_fetch, or a call naming only that site that
printed plenty without an error. Local addresses (localhost, private networks, names without a dot) aren't
tracked: they aren't "the internet".
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

NOTE_AT = 3  # different calls that failed for a site, and none worked: the note
STOP_AFTER_NOTE = 2  # more such calls after the note: the request ends
PLENTY = 400  # characters of error-free output from a call naming one site: it was reached
URL = re.compile(r"\b(?:https?|ftp)://[^\s'\"<>`|]+", re.I)
OK_STATUS = re.compile(
    r"\bHTTP/\d(?:\.\d)?\s+[23]\d\d\b|awaiting response\.\.\. [23]\d\d\b"
    r"|\b(?:status(?:[ _]code)?|http_code)[:= ]+[23]\d\d\b",
    re.I,
)
FAILED_STATUS = re.compile(
    r"\bHTTP/\d(?:\.\d)?\s+([45]\d\d)\b"  # curl -I / -v, wget -S
    r"|\bHTTP Error ([45]\d\d)\b"  # Python's urllib
    r"|\breturned HTTP ([45]\d\d)\b"  # lcode's web_fetch
    r"|\breturned error: ([45]\d\d)\b"  # curl --fail
    r"|\bERROR ([45]\d\d):"  # wget
    r"|\b([45]\d\d) (?:Client|Server) Error\b"  # requests
    r"|\b(?:status(?:[ _]code)?|http_code)[:= ]+([45]\d\d)\b"  # what a model's own loop prints
    r"|\b([45]\d\d):? \(?(?:Not Found|Forbidden|Unauthorized|Bad Gateway|Service Unavailable|Gateway Time-?out|"
    r"Too Many Requests)\b",  # npm, wget, error pages
    re.I,
)
NETWORK_ERRORS = (
    (
        "the name doesn't resolve",
        re.compile(
            r"Could not resolve host|Temporary failure in name resolution|Name or service not known|"
            r"nodename nor servname|getaddrinfo|ENOTFOUND|EAI_AGAIN|NameResolutionError|Failed to resolve",
            re.I,
        ),
    ),
    ("connection refused", re.compile(r"Connection refused|ECONNREFUSED", re.I)),
    ("timed out", re.compile(r"Connection timed out|ETIMEDOUT|Operation timed out|Read timed out", re.I)),
    ("connection reset", re.compile(r"Connection reset|ECONNRESET", re.I)),
    ("no route to it", re.compile(r"Network is unreachable|No route to host|ENETUNREACH", re.I)),
    ("a TLS/SSL error", re.compile(r"SSL certificate problem|certificate verify failed|SSLError", re.I)),
    ("couldn't connect", re.compile(r"Failed to connect to|Max retries exceeded", re.I)),
)
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localhost", ".test", ".example", ".invalid")


@dataclass
class Verdict:
    site: str = ""
    calls: int = 0  # different calls that failed for the site
    what: str = ""  # how they failed, e.g. "HTTP 404"
    note: str = ""  # to add to the tool result
    stop: bool = False  # end the request


class Sites:
    def __init__(self, note_at: int = NOTE_AT) -> None:
        self.note_at = note_at  # 0: off
        self.failed: dict[str, dict[str, str]] = {}  # site -> {call key: how it failed}
        self.reached: set[str] = set()

    def reset(self) -> None:
        """A new request."""
        self.failed.clear()
        self.reached.clear()

    def check(self, name: str, args: dict, result: str) -> Verdict:
        """Count what this call reached and failed to reach; a note when a site looks out of reach."""
        if self.note_at <= 0:
            return Verdict()
        failed, reached = outcomes(name, args, result)
        self.reached |= reached
        key = hashlib.sha256(json.dumps([name, args], sort_keys=True, default=str).encode()).hexdigest()
        verdict = Verdict()
        for site, how in failed.items():
            if site in self.reached:
                continue
            calls = self.failed.setdefault(site, {})
            calls[key] = how
            n = len(calls)
            if n < self.note_at or n < verdict.calls:
                continue
            what = ", ".join(sorted(set(calls.values())))
            if n >= self.note_at + STOP_AFTER_NOTE:
                note = (
                    f"\n\n[lcode] {n} different calls to {site} failed ({what}) and none worked, so lcode stopped "
                    "the request here: this machine can't reach that site."
                )
                verdict = Verdict(site, n, what, note, stop=True)
            elif not verdict.stop:
                verdict = Verdict(site, n, what, advice(site, n, what))
        return verdict


def advice(site: str, n: int, what: str) -> str:
    return (
        f"\n\n[lcode] Every request to {site} in this request has failed ({n} different calls: {what}) and none "
        "worked. When a site fails however the address is written, the usual cause is the network, not the "
        "address: a firewall, a proxy, a regional block, or no internet on this machine. Check that once: request "
        f"an address on {site} that must exist (one the site's own pages link to). If that fails too, stop trying "
        "addresses and tell the user what fails and what they could do: use another network or a VPN, set a "
        f"proxy (HTTPS_PROXY), use a mirror, or run this step on a machine that can reach {site}, such as CI."
    )


def site_of(url: str) -> str:
    """The host to track for a URL, or "" for local and private addresses (and anything unparseable)."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    if not host or "$" in host or "{" in host or host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        return ""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host if "." in host else ""
    return "" if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified else host


def failure(line: str) -> str:
    """How a line of output says a request failed ("HTTP 404", "the name doesn't resolve"…), or ""."""
    m = FAILED_STATUS.search(line)
    if m:
        return f"HTTP {next(g for g in m.groups() if g)}"
    for what, pattern in NETWORK_ERRORS:
        if pattern.search(line):
            return what
    return ""


def outcomes(name: str, args: dict, result: str) -> tuple[dict[str, str], set[str]]:
    """The sites a call failed to reach (with how) and the ones it reached."""
    if name == "web_fetch":
        site = site_of(str(args.get("url", "")))
        if not site:
            return {}, set()
        if result.startswith("Error:"):
            how = failure(result)
            return ({site: how} if how else {}), set()
        return {}, {site}
    if name != "bash":
        return {}, set()
    named = {site_of(url) for url in URL.findall(str(args.get("command", "")))}
    current = next(iter(named)) if len(named) == 1 else ""
    failed: dict[str, str] = {}
    reached: set[str] = set()
    for line in result.splitlines():
        if line.startswith("[lcode") or line.startswith("[exit code"):
            continue
        urls = URL.findall(line)
        if urls:
            current = site_of(urls[-1])
        if not current:
            continue
        how = failure(line)
        if how:
            failed.setdefault(current, how)
        elif OK_STATUS.search(line):
            reached.add(current)
    if len(named) == 1 and (site := next(iter(named))) and not failed and site not in reached:
        printed = "".join(line for line in result.splitlines() if not line.startswith(("[lcode", "[exit code")))
        if result.rstrip().endswith("[exit code: 0]") and len(printed) >= PLENTY:
            reached.add(site)
    return failed, reached
