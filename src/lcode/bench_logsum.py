"""A command-line tool built from a description, for `lcode bench --tasks cli-tool`.

SPEC.md describes `logsum.py`, which summarizes web server access logs: options, output formats, standard
input, malformed lines, unreadable files and exit codes. Each part is easy; the task is getting all of them
right, as with a real tool built from a written spec. The check runs the tool on logs the model never saw
and compares it with a reference implementation, so hard-coding the sample's output doesn't pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from functools import cache
from pathlib import Path

SPEC = """\
# logsum

`logsum.py` summarizes web server access logs in the Common Log Format, one request per line:

    203.0.113.9 - - [10/Oct/2026:13:55:36 +0200] "GET /docs/index.html?lang=en HTTP/1.1" 200 2326

The fields are the client's address, two fields that logsum doesn't use, the time in brackets, the
request in quotes (method, path and protocol), the status code, and the size of the response in
bytes (`-` when there was none). A line is valid when it has all of these, its time reads as
`day/Mon/year:hour:minute:second ±hhmm`, and its status code is from 200 to 599.

## Usage

    python logsum.py [--top N] [--status CODE] [--since TIME] [--json] [FILE ...]

- logsum reads each FILE in turn, or standard input when there's none. A FILE named `-` also means
  standard input.
- `--top N`: how many paths to list (default 3). N is a whole number, at least 1.
- `--status CODE`: count only the responses with this status, given as a code (`404`) or a class
  (`4xx`), from 200 to 599.
- `--since TIME`: count only the requests made at or after TIME, given as `2026-10-10T14:00:00+02:00`.
  The offset is required, and `Z` means UTC. Log lines carry their own offsets: compare moments in
  time, not clock readings.
- `--json`: print the summary as JSON instead of text.

## Output

For access.log, `python logsum.py access.log` prints

    requests: 6
    bytes: 6794
    status: 2xx=3 3xx=1 4xx=1 5xx=1
    top paths:
      2 /api/items
      2 /docs/index.html
      1 /docs/missing.html

and `logsum: skipped 1 malformed line(s)` on standard error.

- `requests` is how many requests were counted, and `bytes` their sizes added up (`-` counts as 0).
- `status` counts the requests in each status class, always all four classes, in this order.
- `top paths` lists the paths requested most often, without their query string (`?lang=en`): most
  requested first, paths with the same count in alphabetical order, at most N of them. Each line is
  two spaces, the count, a space and the path.
- With `--json`, the same as one JSON object:
  `{"requests": 6, "bytes": 6794, "status": {"2xx": 3, "3xx": 1, "4xx": 1, "5xx": 1},
  "top_paths": [{"path": "/api/items", "count": 2}, ...]}`

## Problems

- A line that isn't valid is skipped. If any were, logsum prints `logsum: skipped N malformed
  line(s)` to standard error after the summary.
- If a FILE can't be read, logsum prints `logsum: cannot read FILE` to standard error, goes on with
  the other files, and exits with status 1 at the end.
- For an invalid option or value (`--top 0`, `--status 6xx`, `--since yesterday`), logsum prints a
  message to standard error and exits with status 2 without reading anything.
- Otherwise it exits with status 0.
"""

ACCESS_LOG = """\
203.0.113.9 - - [10/Oct/2026:13:55:36 +0200] "GET /docs/index.html?lang=en HTTP/1.1" 200 2326
198.51.100.4 - - [10/Oct/2026:13:56:02 +0200] "GET /api/items HTTP/1.1" 200 1543
203.0.113.9 - - [10/Oct/2026:13:57:11 +0200] "POST /login HTTP/1.1" 302 -
192.0.2.33 - - [10/Oct/2026:14:01:45 +0200] "GET /docs/index.html HTTP/1.1" 200 2326
198.51.100.4 - - [10/Oct/2026:14:03:09 +0200] "GET /api/items?page=2 HTTP/1.1" 500 87
this line is not a log line
192.0.2.33 - - [10/Oct/2026:14:05:30 +0200] "GET /docs/missing.html HTTP/1.1" 404 512
"""

FILES = {"SPEC.md": SPEC, "access.log": ACCESS_LOG}

PROMPT = "Write logsum.py, the command-line tool that SPEC.md describes, and try it on access.log."

# Logs the model never sees. Times are spread over offsets so that comparing clock readings instead of
# moments gives other answers for --since, and some paths tie.
NORTH = """\
198.51.100.7 - - [10/Oct/2026:11:58:00 +0000] "GET /shop/cart?id=1 HTTP/1.1" 200 410
198.51.100.7 - - [10/Oct/2026:13:59:30 +0200] "GET /shop/cart HTTP/1.1" 200 410
203.0.113.20 - - [10/Oct/2026:07:59:59 -0400] "GET /shop/item/7 HTTP/1.1" 200 1200
203.0.113.20 - - [10/Oct/2026:08:00:00 -0400] "POST /shop/checkout HTTP/1.1" 303 -
192.0.2.8 - - [10/Oct/2026:12:30:00 +0000] "GET /shop/item/7?ref=mail HTTP/1.1" 200 1200
192.0.2.8 - - [10/Oct/2026:14:31:00 +0200] "GET /help HTTP/1.1" 404 98
192.0.2.8 - - [10/Oct/2026:09:15:00 -0400] "GET /shop/cart HTTP/1.1" 503 0
198.51.100.9 - - [10/Oct/2026:21:00:00 +0900] "GET /about HTTP/1.1" 200 3000
198.51.100.9 - - [10/Oct/2026:12:05:00 +0000] "DELETE /shop/cart HTTP/1.1" 405 120
203.0.113.20 - - [10/Oct/2026:12:59:00 +0100] "GET /help HTTP/1.1" 200 640
"""

SOUTH = """\
192.0.2.50 - - [11/Oct/2026:09:00:00 +0000] "GET /about HTTP/1.1" 200 3000
192.0.2.50 - - [11/Oct/2026:09:00:05 +0000] "GET /shop/item/9 HTTP/1.1" 404 98
192.0.2.51 - - [11/Oct/2026:10:00:00 +0200] "GET /shop/cart HTTP/1.1" 200 410
"""

BROKEN = """\
192.0.2.50 - - [11/Oct/2026:09:00:00 +0000] "GET /about HTTP/1.1" 200 3000
192.0.2.50 - - [31/Foo/2026:09:00:05 +0000] "GET /about HTTP/1.1" 200 3000
192.0.2.51 - - [11/Oct/2026:10:00:00 +0200] "GET /shop/cart HTTP/1.1" 200 410
192.0.2.51 - - [11/Oct/2026:10:00:01 +0200] "GET /shop/cart HTTP/1.1" 999 410
GET /shop/cart HTTP/1.1
192.0.2.52 - - [11/Oct/2026:10:02:00 +0200] "GET /shop/cart?x=1 HTTP/1.1" 500 7
"""

# (what it checks, arguments, the log on standard input or None)
CASES = [
    ("the summary of a log", ["north.log"], None),
    ("--top 5 and --json", ["--top", "5", "--json", "north.log"], None),
    ("--status 4xx", ["--status", "4xx", "north.log"], None),
    ("--status with a code", ["--status", "200", "--top", "1", "north.log"], None),
    ("--since compares moments, not clock readings", ["--since", "2026-10-10T12:00:00Z", "north.log"], None),
    ("--since with an offset, and --json", ["--json", "--since", "2026-10-10T08:30:00-04:00", "north.log"], None),
    ("standard input when there's no FILE", [], "north"),
    ("`-` reads standard input, after the other files", ["south.log", "-"], "north"),
    ("malformed lines are skipped and counted", ["broken.log"], None),
    ("an unreadable file", ["south.log", "missing.log", "north.log"], None),
    ("an empty log", [], ""),
    ("--top 0 is invalid", ["--top", "0", "north.log"], None),
    ("--status 6xx is invalid", ["--status", "6xx", "north.log"], None),
    ("--since needs an offset", ["--since", "2026-10-10T14:00:00", "north.log"], None),
]
LOGS = {"north.log": NORTH, "south.log": SOUTH, "broken.log": BROKEN}

REFERENCE = """\
import argparse
import datetime as dt
import json
import re
import sys

LINE = re.compile(r'(\\S+) (\\S+) (\\S+) \\[([^\\]]+)\\] "(\\S+) (\\S+) (\\S+)" (\\d{3}) (\\d+|-)')
CLASSES = ["2xx", "3xx", "4xx", "5xx"]


def fail(message):
    print(f"logsum: {message}", file=sys.stderr)
    sys.exit(2)


def parse(line):
    match = LINE.fullmatch(line.rstrip("\\r\\n"))
    if not match:
        return None
    try:
        when = dt.datetime.strptime(match[4], "%d/%b/%Y:%H:%M:%S %z")
    except ValueError:
        return None
    status = int(match[8])
    if not 200 <= status <= 599:
        return None
    return when, match[6].split("?", 1)[0], status, 0 if match[9] == "-" else int(match[9])


def main():
    parser = argparse.ArgumentParser(prog="logsum")
    parser.add_argument("--top", default="3")
    parser.add_argument("--status")
    parser.add_argument("--since")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args()
    if not args.top.isdigit() or int(args.top) < 1:
        fail(f"--top must be a whole number of at least 1, not {args.top}")
    status = args.status
    if status is not None and not re.fullmatch(r"[2-5](xx|\\d\\d)", status):
        fail(f"--status must be a code or a class from 200 to 599, not {status}")
    since = None
    if args.since is not None:
        try:
            since = dt.datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        except ValueError:
            fail(f"--since must be a time like 2026-10-10T14:00:00+02:00, not {args.since}")
        if since.tzinfo is None:
            fail("--since needs an offset, like +02:00 or Z")
    requests = size = skipped = 0
    classes = dict.fromkeys(CLASSES, 0)
    paths = {}
    code = 0
    for name in args.files or ["-"]:
        try:
            lines = sys.stdin.readlines() if name == "-" else open(name).readlines()
        except OSError:
            print(f"logsum: cannot read {name}", file=sys.stderr)
            code = 1
            continue
        for line in lines:
            entry = parse(line)
            if entry is None:
                skipped += 1
                continue
            when, path, code_seen, length = entry
            if status and not (status == str(code_seen) or status == f"{str(code_seen)[0]}xx"):
                continue
            if since is not None and when < since:
                continue
            requests += 1
            size += length
            classes[f"{str(code_seen)[0]}xx"] += 1
            paths[path] = paths.get(path, 0) + 1
    top = sorted(paths.items(), key=lambda item: (-item[1], item[0]))[: int(args.top)]
    if args.json:
        print(json.dumps({"requests": requests, "bytes": size, "status": classes,
                          "top_paths": [{"path": p, "count": c} for p, c in top]}))
    else:
        print(f"requests: {requests}")
        print(f"bytes: {size}")
        print("status: " + " ".join(f"{k}={v}" for k, v in classes.items()))
        print("top paths:")
        for path, count in top:
            print(f"  {count} {path}")
    if skipped:
        print(f"logsum: skipped {skipped} malformed line(s)", file=sys.stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
"""


def run(script: Path, folder: Path, argv: list[str], stdin: str | None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *argv],
        cwd=folder,
        input=stdin if stdin is not None else "",
        capture_output=True,
        text=True,
        timeout=30,
    )


def logs(folder: Path) -> None:
    for name, text in LOGS.items():
        (folder / name).write_text(text)


@cache
def expected() -> tuple[tuple[str, str, int], ...]:
    """What the reference implementation prints for each case: stdout, stderr and the exit status."""
    with tempfile.TemporaryDirectory(prefix="lcode-bench-logsum-") as tmp:
        folder = Path(tmp)
        logs(folder)
        (folder / "reference.py").write_text(REFERENCE)
        results = []
        for _, argv, stdin in CASES:
            r = run(folder / "reference.py", folder, argv, LOGS.get(f"{stdin}.log", stdin))
            results.append((r.stdout, r.stderr, r.returncode))
        return tuple(results)


def lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.strip().splitlines()]


def same_output(got: str, want: str, as_json: bool) -> bool:
    if as_json:
        try:
            return json.loads(got) == json.loads(want)
        except ValueError:
            return False
    return lines(got) == lines(want)


def problem(r: subprocess.CompletedProcess, want: tuple[str, str, int], as_json: bool) -> str | None:
    """What's wrong with one case's result, or None."""
    out, err, code = want
    if r.returncode != code:
        return f"exit status {r.returncode} instead of {code}"
    if code == 2:
        return None if not r.stdout.strip() else "printed a summary for an invalid option"
    if not same_output(r.stdout, out, as_json):
        return "wrong summary"
    for message in err.strip().splitlines():
        if message.removeprefix("logsum: ") not in r.stderr:
            return f"no `{message}` on standard error"
    return None


def check(folder: Path, answer: str):
    from lcode.bench_checks import Check

    script = folder / "logsum.py"
    if not script.is_file():
        return Check(False, "logsum.py is missing")
    with tempfile.TemporaryDirectory(prefix="lcode-bench-logsum-") as tmp:
        work = Path(tmp)
        logs(work)
        wrong = []
        for (what, argv, stdin), want in zip(CASES, expected(), strict=True):
            try:
                r = run(script, work, argv, LOGS.get(f"{stdin}.log", stdin))
            except subprocess.TimeoutExpired:
                wrong.append(f"{what}: timed out")
                continue
            issue = problem(r, want, "--json" in argv)
            if issue:
                wrong.append(f"{what}: {issue}")
    if not wrong:
        return Check(True, f"all {len(CASES)} cases right, on logs it hadn't seen")
    return Check(False, f"{len(CASES) - len(wrong)} of {len(CASES)} cases right; {wrong[0]}")
