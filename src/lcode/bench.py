"""lcode bench: score models on small, self-contained coding tasks on this machine.

Each task runs in a fresh temporary folder in `yolo` mode without web access, and ends with an
automatic check of the files or the answer. Some checks rerun the model's code on data it never
saw, so hard-coding the expected output doesn't pass.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from lcode import __version__
from lcode.agent import Agent, Settings
from lcode.config import format_tokens
from lcode.hardware import Hardware
from lcode.ollama import Ollama, OllamaError

SCHEMA_VERSION = 1
DEFAULT_CONTEXT = 32768  # the same everywhere, so results compare across machines
DEFAULT_TIMEOUT = 300  # seconds per task
CHECK_TIMEOUT = 60
PROBE_WORDS = 3000  # about 4,000 tokens
PROBE_TEXT = (
    "file function value error test module class return import list string number change build user "
    "server request cache memory thread order parse check config option result folder branch commit"
)


class TaskTimeout(KeyboardInterrupt):
    """Raised when a task runs out of time. The agent stops running commands just like on Ctrl+C."""


@dataclass
class Check:
    passed: bool
    detail: str


@dataclass
class Task:
    id: str
    title: str
    prompt: str
    files: dict[str, str]
    check: Callable[[Path, str], Check]  # (task folder, the model's final answer) -> result


# ----------------------------------------------------------------------------- helpers for checks


def run_python(folder: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=folder,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=CHECK_TIMEOUT,
    )


def read(folder: Path, name: str) -> str | None:
    try:
        return (folder / name).read_text()
    except (OSError, UnicodeDecodeError):
        return None


def last_line(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def unittest_check(folder: Path, what: str) -> Check:
    r = run_python(folder, "-m", "unittest", "-q")
    if r.returncode == 0:
        return Check(True, f"{what} pass")
    return Check(False, f"{what} fail: {last_line(r.stderr) or last_line(r.stdout)}")


# ----------------------------------------------------------------------------- 1. fix a bug

CART = '''\
"""A tiny shopping cart."""


class Cart:
    def __init__(self):
        self.items = []

    def add(self, name, price, quantity=1):
        self.items.append((name, price, quantity))

    def total(self, discount=0.0):
        """Total price after an optional discount between 0 and 1."""
        subtotal = sum(price for _, price, quantity in self.items)
        return round(subtotal * (1 - discount), 2)
'''

CART_TESTS = """\
import unittest

from cart import Cart


class CartTest(unittest.TestCase):
    def test_total_counts_quantities(self):
        cart = Cart()
        cart.add("apple", 0.5, quantity=4)
        cart.add("bread", 2.25)
        self.assertEqual(cart.total(), 4.25)

    def test_discount(self):
        cart = Cart()
        cart.add("coffee", 10.0, quantity=2)
        self.assertEqual(cart.total(discount=0.1), 18.0)


if __name__ == "__main__":
    unittest.main()
"""


def check_fix_bug(folder: Path, answer: str) -> Check:
    if read(folder, "test_cart.py") != CART_TESTS:
        return Check(False, "test_cart.py was changed; the fix belongs in cart.py")
    return unittest_check(folder, "the tests")


# ----------------------------------------------------------------------------- 2. find code

UPLOADER = {
    "uploader/__init__.py": '"""Upload files to the storage service."""\n',
    "uploader/config.py": '''\
"""Settings for the uploader."""

BASE_DELAY = 0.5  # seconds
MAX_DELAY = 30.0
MAX_ATTEMPTS = 6
POLL_INTERVAL = 2.0
CHUNK_SIZE = 4 * 1024 * 1024
''',
    "uploader/client.py": '''\
"""Talking to the storage service."""

import time

from uploader.config import POLL_INTERVAL


class StorageClient:
    def __init__(self, session):
        self.session = session

    def put_chunk(self, url, data):
        response = self.session.put(url, data=data)
        response.raise_for_status()
        return response

    def wait_until_ready(self, job_id):
        """Poll the service until a processing job has finished."""
        while self.session.get(f"/jobs/{job_id}").json()["state"] != "done":
            time.sleep(POLL_INTERVAL)
''',
    "uploader/backoff.py": '''\
"""Spacing out attempts."""

import random

from uploader.config import BASE_DELAY, MAX_DELAY


def jitter(delay):
    """Spread attempts out so clients don't all hit the service at once."""
    return delay * random.uniform(0.8, 1.2)


def next_wait(attempt):
    """Seconds to pause after `attempt` failed tries of the same chunk."""
    delay = min(MAX_DELAY, BASE_DELAY * 2**attempt)
    return jitter(delay)
''',
    "uploader/transfer.py": '''\
"""Uploading a file in chunks."""

import time

from uploader.backoff import next_wait
from uploader.config import CHUNK_SIZE, MAX_ATTEMPTS


def chunks(path):
    with open(path, "rb") as f:
        while block := f.read(CHUNK_SIZE):
            yield block


def upload(client, url, path):
    for index, block in enumerate(chunks(path)):
        for attempt in range(MAX_ATTEMPTS):
            try:
                client.put_chunk(f"{url}?part={index}", block)
                break
            except OSError:
                time.sleep(next_wait(attempt))
        else:
            raise RuntimeError(f"chunk {index} failed {MAX_ATTEMPTS} times")
''',
}


def check_find_code(folder: Path, answer: str) -> Check:
    lines = UPLOADER["uploader/backoff.py"].splitlines()
    start = next(i for i, line in enumerate(lines, 1) if line.startswith("def next_wait"))
    end = start + 3  # def, docstring and two lines of body
    cited = [int(n) for n in re.findall(r"backoff\.py:(\d+)", answer)]
    if "next_wait" not in answer:
        named = re.findall(r"`([A-Za-z_]\w*)(?:\(\))?`", answer)
        return Check(False, f"named {named[0]} instead of next_wait" if named else "didn't name next_wait")
    if not cited:
        return Check(False, "named next_wait but didn't cite backoff.py:<line>")
    if not any(start <= n <= end for n in cited):
        return Check(False, f"cited backoff.py:{cited[0]}, but next_wait is at lines {start}-{end}")
    return Check(True, f"next_wait at backoff.py:{cited[0]}")


# ----------------------------------------------------------------------------- 3. write a script

NUMBERS = "12\n7\n\n3\n25\n8\n19\n"
HIDDEN_NUMBERS = "5\n-2\n\n10\n4\n4\n"  # checked with data the model never saw


def expected_stats(data: str) -> list[str]:
    numbers = [int(line) for line in data.splitlines() if line.strip()]
    return [
        f"count: {len(numbers)}",
        f"sum: {sum(numbers)}",
        f"mean: {statistics.mean(numbers):.2f}",
        f"median: {statistics.median(numbers):.2f}",
    ]


def check_write_script(folder: Path, answer: str) -> Check:
    if not (folder / "stats.py").is_file():
        return Check(False, "stats.py wasn't created")
    for label, data in (("numbers.txt", NUMBERS), ("other data", HIDDEN_NUMBERS)):
        (folder / "numbers.txt").write_text(data)
        r = run_python(folder, "stats.py")
        got = [line.strip() for line in r.stdout.strip().splitlines()]
        want = expected_stats(data)
        if r.returncode != 0:
            return Check(False, f"stats.py failed on {label}: {last_line(r.stderr)}")
        if got != want:
            return Check(False, f"wrong output on {label}: {' | '.join(got)[:120]} (expected {' | '.join(want)})")
    return Check(True, "correct output, also on data it hadn't seen")


# ----------------------------------------------------------------------------- 4. rename across files

SHOP = {
    "shop/__init__.py": "",
    "shop/users.py": '''\
"""Users of the shop."""


def get_user_name(user):
    """The name to show for a user: their nickname if they have one."""
    return user.get("nickname") or f"{user['first']} {user['last']}"
''',
    "shop/orders.py": """\
from shop.users import get_user_name


def order_summary(order):
    return f"Order {order['id']} for {get_user_name(order['user'])}: {len(order['items'])} item(s)"
""",
    "shop/emails.py": """\
from shop import users


def greeting(user):
    return f"Hello {users.get_user_name(user)},"
""",
    "test_shop.py": """\
import unittest

from shop.emails import greeting
from shop.orders import order_summary
from shop.users import get_user_name

ADA = {"first": "Ada", "last": "Lovelace"}
BOB = {"first": "Robert", "last": "Smith", "nickname": "Bob"}


class ShopTest(unittest.TestCase):
    def test_name(self):
        self.assertEqual(get_user_name(ADA), "Ada Lovelace")
        self.assertEqual(get_user_name(BOB), "Bob")

    def test_callers(self):
        self.assertEqual(greeting(BOB), "Hello Bob,")
        self.assertEqual(order_summary({"id": 7, "user": ADA, "items": [1, 2]}), "Order 7 for Ada Lovelace: 2 item(s)")


if __name__ == "__main__":
    unittest.main()
""",
}

HIDDEN_SHOP_TEST = """\
import unittest

from shop import users
from shop.emails import greeting
from shop.orders import order_summary

ADA = {"first": "Ada", "last": "Lovelace"}
BOB = {"first": "Robert", "last": "Smith", "nickname": "Bob"}


class HiddenTest(unittest.TestCase):
    def test_renamed(self):
        self.assertFalse(hasattr(users, "get_user_name"))
        self.assertEqual(users.display_name(ADA), "Ada Lovelace")
        self.assertEqual(users.display_name(BOB), "Bob")
        self.assertEqual(greeting(ADA), "Hello Ada Lovelace,")
        self.assertEqual(order_summary({"id": 1, "user": BOB, "items": []}), "Order 1 for Bob: 0 item(s)")
"""


def check_rename(folder: Path, answer: str) -> Check:
    left = sorted(
        str(p.relative_to(folder))
        for p in folder.rglob("*.py")
        if "__pycache__" not in p.parts and "get_user_name" in (read(folder, str(p.relative_to(folder))) or "")
    )
    if left:
        return Check(False, f"get_user_name is still used in {', '.join(left)}")
    tests = folder / "test_shop.py"
    if not tests.is_file() or "display_name" not in tests.read_text():
        return Check(False, "test_shop.py wasn't updated")
    (folder / "test_hidden_rename.py").write_text(HIDDEN_SHOP_TEST)
    return unittest_check(folder, "its tests and extra checks")


# ----------------------------------------------------------------------------- 5. precise edit in a long file


def services_file() -> str:
    lines = ['"""Connection settings for internal services."""', "", "SERVICES = {"]
    for i in range(1, 151):
        names = [f"service_{i:03d}"] + ([f"service_{i:03d}_replica"] if i % 29 == 0 else [])
        for name in names:
            lines += [
                f'    "{name}": {{',
                f'        "host": "{name.replace("_", "-")}.internal",',
                f'        "port": {8000 + i},',
                f'        "timeout": {60 if i % 10 == 0 else 30},',
                f'        "retries": {5 if i % 7 == 0 else 3},',
                "    },",
            ]
    lines += ["}", ""]
    return "\n".join(lines)


SERVICES = services_file()
TARGET_SERVICE = "service_087"


def check_precise_edit(folder: Path, answer: str) -> Check:
    new = read(folder, "services.py")
    if new is None:
        return Check(False, "services.py is missing")
    before, after = SERVICES.splitlines(), new.splitlines()
    target = before.index(f'    "{TARGET_SERVICE}": {{') + 3  # its timeout line
    expected = before.copy()
    expected[target] = expected[target].replace("30", "45")
    if after == expected:
        return Check(True, f"changed only {TARGET_SERVICE}'s timeout")
    if len(after) != len(before):
        return Check(False, f"the file went from {len(before)} to {len(after)} lines")
    changed = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    if target not in changed:
        return Check(False, f"{TARGET_SERVICE}'s timeout wasn't changed to 45")
    return Check(False, f"changed {len(changed)} lines instead of 1")


# ----------------------------------------------------------------------------- 6. recover from errors

TOOL = '''\
"""Build a ranked report from a data file.

Usage: python tool.py --source DATA --out REPORT
"""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description="Build a ranked report from a data file.")
    parser.add_argument("--source", help="data file with one 'name,score' per line")
    parser.add_argument("--out", help="where to write the report")
    parser.add_argument("--input", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.input:
        sys.exit("error: --input was removed in version 2; use --source instead")
    if not args.source or not args.out:
        parser.error("--source and --out are required")
    rows = []
    with open(args.source) as f:
        for number, line in enumerate(f, 1):
            if not line.strip():
                continue
            name, _, score = line.partition(",")
            if not score.strip().isdigit():
                sys.exit(f"error: {args.source} line {number}: score must be a whole number, got {score.strip()!r}")
            rows.append((name.strip(), int(score)))
    rows.sort(key=lambda row: (-row[1], row[0]))
    with open(args.out, "w") as f:
        for rank, (name, score) in enumerate(rows, 1):
            f.write(f"{rank}. {name} {score}\\n")
        f.write(f"total {sum(score for _, score in rows)}\\n")


if __name__ == "__main__":
    main()
'''

SCORES = "alice, 42\nbob, 17\ncarol, 42\ndave, n/a\nerin, 8\n"
GOOD_ROWS = [("alice", 42), ("carol", 42), ("bob", 17), ("erin", 8)]


def check_recover(folder: Path, answer: str) -> Check:
    report = read(folder, "report.txt")
    expected = "".join(f"{rank}. {name} {score}\n" for rank, (name, score) in enumerate(GOOD_ROWS, 1))
    expected += f"total {sum(score for _, score in GOOD_ROWS)}\n"
    if report is None:
        return Check(False, "report.txt wasn't created")
    data = read(folder, "data.txt") or ""
    if "dave" in data or any(name not in data for name, _ in GOOD_ROWS):
        return Check(False, "data.txt should lose only the invalid line")
    if report != expected:
        return Check(False, f"report.txt is wrong: {' | '.join(report.splitlines())[:120]}")
    return Check(True, "used --source and dropped the invalid line")


# ----------------------------------------------------------------------------- 7. fix to a written spec

DURATIONS = '''\
import re

UNITS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    """Parse a duration such as "1h30m", "45s" or "2h" into seconds.

    - The units are h, m and s, in that order, each at most once: "1h0m5s" is fine, "30m1h" is not.
    - Units are case-insensitive, and spaces between the parts are allowed: "1H 30m".
    - A plain number means seconds: "90".
    - Anything else raises ValueError, including an empty string and unknown units like "1x".
    """
    total = 0
    for number, unit in re.findall(r"(\\d+)([hms])", text):
        total = int(number) * UNITS[unit]
    return total
'''

HIDDEN_DURATION_TEST = """\
import unittest

from durations import parse_duration


class HiddenTest(unittest.TestCase):
    def test_valid(self):
        cases = {"1h30m": 5400, "45s": 45, "2h": 7200, "1h0m5s": 3605, "90m": 5400, "1H 30m": 5400,
                 "90": 90, "0s": 0, "2h 5s": 7205, "1h 2m 3s": 3723}
        for text, seconds in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_duration(text), seconds)

    def test_invalid(self):
        for text in ["", "1x", "30m1h", "h", "1h1h", "abc", "5m 3m"]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_duration(text)
"""


def check_spec_fix(folder: Path, answer: str) -> Check:
    if not (folder / "durations.py").is_file():
        return Check(False, "durations.py is missing")
    (folder / "test_hidden_durations.py").write_text(HIDDEN_DURATION_TEST)
    r = run_python(folder, "-m", "unittest", "-q", "test_hidden_durations")
    if r.returncode == 0:
        return Check(True, "follows the whole docstring")
    failed = sorted(set(re.findall(r"\(text='([^']*)'\)", r.stderr)))
    if failed:
        return Check(False, "wrong for " + ", ".join(repr(t) for t in failed[:6]))
    return Check(False, f"hidden tests fail: {last_line(r.stderr)}")


# ----------------------------------------------------------------------------- 8. a feature across files

TODO_APP = {
    "storage.py": '''\
"""Todos live in todos.json in the current folder."""

import json
from pathlib import Path

PATH = Path("todos.json")


def load():
    if not PATH.exists():
        return []
    return json.loads(PATH.read_text())


def save(todos):
    PATH.write_text(json.dumps(todos, indent=2))
''',
    "todo.py": '''\
"""A tiny todo list: python todo.py add TITLE | list | done ID"""

import argparse

import storage


def add(title):
    todos = storage.load()
    next_id = max((t["id"] for t in todos), default=0) + 1
    todos.append({"id": next_id, "title": title, "done": False})
    storage.save(todos)
    print(f"Added #{next_id}")


def done(todo_id):
    todos = storage.load()
    for todo in todos:
        if todo["id"] == todo_id:
            todo["done"] = True
            storage.save(todos)
            print(f"Done #{todo_id}")
            return
    raise SystemExit(f"No todo #{todo_id}")


def format_todo(todo):
    return f"#{todo['id']} [{'x' if todo['done'] else ' '}] {todo['title']}"


def list_todos():
    for todo in storage.load():
        print(format_todo(todo))


def main(argv=None):
    parser = argparse.ArgumentParser(description="A tiny todo list")
    commands = parser.add_subparsers(dest="command", required=True)
    add_command = commands.add_parser("add")
    add_command.add_argument("title")
    done_command = commands.add_parser("done")
    done_command.add_argument("id", type=int)
    commands.add_parser("list")
    args = parser.parse_args(argv)
    if args.command == "add":
        add(args.title)
    elif args.command == "done":
        done(args.id)
    else:
        list_todos()


if __name__ == "__main__":
    main()
''',
    "test_todo.py": """\
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import storage
import todo


def run(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        todo.main(list(argv))
    return out.getvalue().splitlines()


class TodoTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        storage.PATH = Path(self.folder.name) / "todos.json"

    def tearDown(self):
        self.folder.cleanup()

    def test_add_and_list(self):
        self.assertEqual(run("add", "buy milk"), ["Added #1"])
        self.assertEqual(run("add", "call mom"), ["Added #2"])
        self.assertEqual(run("list"), ["#1 [ ] buy milk", "#2 [ ] call mom"])

    def test_done(self):
        run("add", "buy milk")
        self.assertEqual(run("done", "1"), ["Done #1"])
        self.assertEqual(run("list"), ["#1 [x] buy milk"])
""",
}

TODO_STEPS = [
    (["add", "low thing", "--priority", "low"], ["Added #2"]),
    (["add", "urgent", "--priority", "high"], ["Added #3"]),
    (["add", "normal thing"], ["Added #4"]),
    (["add", "urgent two", "--priority", "high"], ["Added #5"]),
    (["done", "4"], ["Done #4"]),
    (
        ["list"],
        ["#3 [ ] ! urgent", "#5 [ ] ! urgent two", "#1 [ ] old task", "#4 [x] normal thing", "#2 [ ] low thing"],
    ),
]


def check_feature(folder: Path, answer: str) -> Check:
    tests = unittest_check(folder, "its tests")
    if not tests.passed:
        return tests
    trial = folder / "hidden-check"
    trial.mkdir()
    for source in folder.glob("*.py"):
        shutil.copy(source, trial / source.name)
    (trial / "todos.json").write_text('[{"id": 1, "title": "old task", "done": false}]')  # saved before the change
    for argv, want in TODO_STEPS:
        r = run_python(trial, "todo.py", *argv)
        got = [line.rstrip() for line in r.stdout.strip().splitlines()]
        if r.returncode != 0 or got != want:
            shown = " | ".join(got)[:120] or last_line(r.stderr)
            return Check(False, f"`todo.py {' '.join(argv)}` printed {shown!r}, expected {' | '.join(want)!r}")
    r = run_python(trial, "todo.py", "add", "x", "--priority", "urgent")
    if r.returncode == 0:
        return Check(False, "accepted an unknown priority")
    return Check(True, "priorities work, including old todos")


# ----------------------------------------------------------------------------- the suite

TASKS = [
    Task(
        "fix-bug",
        "Fix a bug so the tests pass",
        "The tests in test_cart.py fail. Find the bug, fix it, and run the tests.",
        {"cart.py": CART, "test_cart.py": CART_TESTS},
        check_fix_bug,
    ),
    Task(
        "find-code",
        "Answer with file:line",
        "Which function computes how long to wait before retrying a failed upload in this project? Reply with "
        "the function name and where it is defined, as path:line (for example `pkg/module.py:12`).",
        UPLOADER,
        check_find_code,
    ),
    Task(
        "write-script",
        "Write a script from a spec",
        "Write a Python script stats.py that reads numbers.txt (one integer per line; ignore blank lines) and "
        "prints exactly these four lines:\ncount: <how many numbers>\nsum: <their sum>\n"
        "mean: <the mean, with 2 decimals>\nmedian: <the median, with 2 decimals>\nThen run it to check the output.",
        {"numbers.txt": NUMBERS},
        check_write_script,
    ),
    Task(
        "rename",
        "Rename a function across files",
        "Rename the function get_user_name to display_name everywhere in this project: the definition, every "
        "caller and the tests. Then run the tests with `python -m unittest`.",
        SHOP,
        check_rename,
    ),
    Task(
        "precise-edit",
        "One-line edit in a long file",
        f"In services.py, change the timeout of {TARGET_SERVICE} from 30 to 45. Change nothing else.",
        {"services.py": SERVICES},
        check_precise_edit,
    ),
    Task(
        "recover",
        "Recover from failing commands",
        "Build report.txt from data.txt by running: python tool.py --input data.txt --out report.txt\n"
        "If a line in data.txt is invalid, delete that line from data.txt and run the tool again.",
        {"tool.py": TOOL, "data.txt": SCORES},
        check_recover,
    ),
    Task(
        "spec-fix",
        "Fix a function to match its spec",
        "Users report that parse_duration in durations.py is wrong: parse_duration('1h30m') returns 1800 instead "
        "of 5400. Fix it so it does everything its docstring says. Add tests for the cases in the docstring to "
        "test_durations.py and run them.",
        {"durations.py": DURATIONS},
        check_spec_fix,
    ),
    Task(
        "feature",
        "Add a feature across files",
        "Add priorities to this todo app. `python todo.py add TITLE --priority high|normal|low` sets one "
        "(the default is normal). `list` shows high-priority todos first, then normal, then low, keeping the order "
        'they were added within each priority, and marks high-priority todos with "!" after the checkbox, like '
        "`#3 [ ] ! call the bank`. Todos saved before this change have no priority and count as normal. Update "
        "the tests and run them.",
        TODO_APP,
        check_feature,
    ),
]


def select_tasks(ids: str | None) -> list[Task]:
    if not ids:
        return list(TASKS)
    wanted = [i.strip() for i in ids.split(",") if i.strip()]
    known = {task.id: task for task in TASKS}
    unknown = [i for i in wanted if i not in known]
    if unknown:
        raise ValueError(f"unknown task(s) {', '.join(unknown)}; choose from {', '.join(known)}")
    return [known[i] for i in wanted]


# ----------------------------------------------------------------------------- running


@dataclass
class TaskResult:
    id: str
    passed: bool
    seconds: float
    detail: str
    steps: int = 0  # model responses
    tool_errors: int = 0  # failed tool calls, including malformed ones
    error: str | None = None  # why the run stopped early: a timeout or an Ollama error


@dataclass
class ModelRun:
    model: str  # as given: a catalog key or Ollama tag
    ollama_model: str
    context: int = 0
    num_batch: int | None = None
    think: bool = True
    load_seconds: float = 0.0
    prompt_tps: float | None = None  # measured with a fresh prompt; see prompt_speed()
    memory_gb: float | None = None
    gpu_percent: int | None = None
    usage: dict = field(default_factory=dict)
    tasks: list[TaskResult] = field(default_factory=list)
    error: str | None = None  # the model couldn't run at all
    interrupted: bool = False

    @property
    def passed(self) -> int:
        return sum(t.passed for t in self.tasks)

    @property
    def seconds(self) -> float:
        return sum(t.seconds for t in self.tasks)

    @property
    def generation_tps(self) -> float | None:
        ns = self.usage.get("output_ns", 0)
        return round(self.usage["output_tokens"] / (ns / 1e9), 1) if ns else None

    @property
    def tool_errors(self) -> int:
        return sum(t.tool_errors for t in self.tasks)

    def to_json(self) -> dict:
        data = asdict(self)
        data.update(
            passed=self.passed,
            total=len(self.tasks),
            seconds=round(self.seconds, 1),
            generation_tps=self.generation_tps,
            tool_errors=self.tool_errors,
        )
        return data


@contextmanager
def time_limit(seconds: float) -> Iterator[None]:
    def expire(signum, frame):
        raise TaskTimeout

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class Elapsed:
    def __init__(self, label: str):
        self.label, self.start = label, time.monotonic()

    def __rich__(self) -> Text:
        return Text(f"{self.label}  {time.monotonic() - self.start:.0f}s", style="dim")


def count_tool_errors(messages: list[dict]) -> int:
    failed = sum(1 for m in messages if m.get("role") == "tool" and m.get("content", "").startswith("Error:"))
    malformed = sum(
        1
        for m in messages
        if m.get("role") == "user" and m.get("content", "").startswith("[lcode] Your last tool call")
    )
    return failed + malformed


def run_task(
    ollama: Ollama, settings: Settings, task: Task, timeout: float, log: Console, keep: bool, usage: dict
) -> TaskResult:
    folder = Path(tempfile.mkdtemp(prefix=f"lcode-bench-{task.id}-"))
    try:
        for rel, content in task.files.items():
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_text(content)
        agent = Agent(ollama, settings, folder, console=log)
        error = None
        started = time.monotonic()
        try:
            with time_limit(timeout):
                agent.run_turn(task.prompt)
        except TaskTimeout:
            error = f"timed out after {timeout:.0f}s"
        except OllamaError as e:
            error = str(e).splitlines()[0][:200]
        seconds = time.monotonic() - started
        for key, value in agent.usage.items():
            usage[key] = usage.get(key, 0) + value
        answer = next((m.get("content", "") for m in reversed(agent.messages) if m.get("role") == "assistant"), "")
        try:
            check = task.check(folder, answer)
        except Exception as e:  # a check must never crash the benchmark
            check = Check(False, f"check failed: {type(e).__name__}: {e}")
    finally:  # also when Ctrl+C stops the task
        if keep:
            (folder / "lcode-bench.log").write_text(log.file.getvalue() if isinstance(log.file, io.StringIO) else "")
        else:
            shutil.rmtree(folder, ignore_errors=True)
    return TaskResult(
        id=task.id,
        passed=check.passed and error is None,
        seconds=round(seconds, 1),
        detail=error or check.detail,
        steps=sum(1 for m in agent.messages if m.get("role") == "assistant"),
        tool_errors=count_tool_errors(agent.messages),
        error=error,
    )


def prompt_speed(ollama: Ollama, settings: Settings, options: dict) -> float | None:
    """Prompt tokens per second for prompts the model hasn't seen: the faster of two readings.

    Measured separately: the tasks' requests mostly repeat the conversation so far, which Ollama
    serves from its prompt cache, and counting those tokens would overstate the speed. The first
    reading after loading can be much slower while the model warms up, hence two.
    """
    readings = [_read_prompt(ollama, settings, options) for _ in range(2)]
    return max((r for r in readings if r), default=None)


def _read_prompt(ollama: Ollama, settings: Settings, options: dict) -> float | None:
    rng = random.Random()
    vocabulary = PROBE_TEXT.split()
    words = " ".join(rng.choice(vocabulary) for _ in range(PROBE_WORDS))
    payload = {
        "model": settings.model,
        "messages": [{"role": "user", "content": f"{rng.random()}\n{words}\n\nReply with OK."}],
        "think": False,
        "keep_alive": settings.keep_alive,
        "options": {**options, "num_predict": 1},
    }
    final: dict = {}
    try:
        with time_limit(120):
            for chunk in ollama.chat_stream(payload):
                if chunk.get("done"):
                    final = chunk
    except (OllamaError, TaskTimeout):
        return None
    count, ns = final.get("prompt_eval_count", 0), final.get("prompt_eval_duration", 0)
    return round(count / (ns / 1e9), 1) if ns and count >= 1000 else None


def memory_use(ollama: Ollama, model: str) -> tuple[float | None, int | None]:
    for entry in ollama.running():
        if entry.get("name") in (model, f"{model}:latest") or entry.get("model") in (model, f"{model}:latest"):
            size, vram = entry.get("size") or 0, entry.get("size_vram") or 0
            if size:
                return round(size / 1e9, 1), round(100 * vram / size)
    return None, None


def run_model(
    ollama: Ollama,
    name: str,
    settings: Settings,
    tasks: list[Task],
    console: Console,
    timeout: float = DEFAULT_TIMEOUT,
    keep: bool = False,
    verbose: bool = False,
) -> ModelRun:
    run = ModelRun(name, settings.model, settings.context, settings.num_batch, settings.think)
    console.print(
        f"\n[bold]{escape(settings.model)}[/] · {format_tokens(settings.context)} context · "
        f"reasoning {'on' if settings.think else 'off'} · {len(tasks)} task{'' if len(tasks) == 1 else 's'}"
    )
    options = {"num_ctx": settings.context, **({"num_batch": settings.num_batch} if settings.num_batch else {})}
    started = time.monotonic()
    try:
        with console.status(Elapsed("  Loading the model")):
            ollama.load(settings.model, options, settings.keep_alive)
    except OllamaError as e:
        console.print(f"  [yellow]Loading failed ({escape(str(e).splitlines()[0][:150])}); the tasks will retry.[/]")
    except KeyboardInterrupt:
        run.interrupted = True
        return run
    run.load_seconds = round(time.monotonic() - started, 1)
    run.memory_gb, run.gpu_percent = memory_use(ollama, settings.model)
    try:
        with console.status(Elapsed("  Measuring prompt speed")):
            run.prompt_tps = prompt_speed(ollama, settings, options)
    except KeyboardInterrupt:
        run.interrupted = True
        return run
    where = f" · {run.memory_gb} GB, {run.gpu_percent}% on the GPU" if run.memory_gb else ""
    reads = f" · reads prompts at {run.prompt_tps:,.0f} tok/s" if run.prompt_tps else ""
    console.print(f"  [dim]Loaded in {run.load_seconds:.0f}s{where}{reads}[/]")
    for task in tasks:
        log = console if verbose else Console(file=io.StringIO(), width=120, force_terminal=False)
        try:
            if verbose:
                console.rule(f"{task.id}: {task.title}")
                result = run_task(ollama, settings, task, timeout, log, keep, run.usage)
            else:
                with console.status(Elapsed(f"  {task.id:<13} {task.title}")):
                    result = run_task(ollama, settings, task, timeout, log, keep, run.usage)
        except KeyboardInterrupt:
            console.print("  [yellow]Stopped.[/]")
            run.interrupted = True
            break
        run.tasks.append(result)
        mark = "[green]✓[/]" if result.passed else "[red]✗[/]"
        console.print(
            f"  {mark} {task.id:<13} {task.title:<32} {result.seconds:>5.0f}s  [dim]{escape(result.detail)}[/]"
        )
    if run.memory_gb is None:
        run.memory_gb, run.gpu_percent = memory_use(ollama, settings.model)
    # lcode lowers the batch size or context if the GPU runs out of memory; report what was used.
    run.context, run.num_batch, run.think = settings.context, settings.num_batch, settings.think
    return run


# ----------------------------------------------------------------------------- reporting


def duration(seconds: float) -> str:
    return f"{int(seconds // 60)}m {seconds % 60:02.0f}s" if seconds >= 60 else f"{seconds:.0f}s"


def summary_rows(runs: list[ModelRun], tasks: list[Task]) -> list[tuple[str, list[str]]]:
    rows = []
    for task in tasks:
        cells = []
        for run in runs:
            result = next((t for t in run.tasks if t.id == task.id), None)
            cells.append("" if result is None else f"{'✓' if result.passed else '✗'} {duration(result.seconds)}")
        rows.append((f"{task.id}: {task.title}", cells))

    def cell(run: ModelRun, value: str) -> str:
        return "—" if run.error else value

    rows += [
        ("Passed", [cell(r, f"{r.passed}/{len(r.tasks)}" + (" (stopped)" if r.interrupted else "")) for r in runs]),
        ("Time", [cell(r, duration(r.seconds)) for r in runs]),
        ("Generation", [cell(r, f"{r.generation_tps:.1f} tok/s" if r.generation_tps else "?") for r in runs]),
        ("Prompt reading", [cell(r, f"{r.prompt_tps:,.0f} tok/s" if r.prompt_tps else "?") for r in runs]),
        ("Tool-call errors", [cell(r, str(r.tool_errors)) for r in runs]),
        (
            "Memory",
            [cell(r, f"{r.memory_gb} GB, {r.gpu_percent}% on GPU" if r.memory_gb else "?") for r in runs],
        ),
        ("Context", [cell(r, format_tokens(r.context)) for r in runs]),
    ]
    return rows


def print_summary(console: Console, runs: list[ModelRun], tasks: list[Task]) -> None:
    table = Table(title="lcode bench", title_justify="left", header_style="bold")
    table.add_column("")
    for run in runs:
        table.add_column(escape(run.model), justify="right")
    for label, cells in summary_rows(runs, tasks):
        style = "bold" if label == "Passed" else ""
        table.add_row(escape(label), *[escape(c) for c in cells], style=style)
    console.print()
    console.print(table)
    for run in runs:
        if run.error:
            console.print(f"[yellow]{escape(run.model)}: {escape(run.error)}[/]")


def report(runs: list[ModelRun], hardware: Hardware, ollama_version: str) -> dict:
    """The JSON document written by --json (schema version 1)."""
    return {
        "schema": SCHEMA_VERSION,
        "lcode": __version__,
        "ollama": ollama_version,
        "date": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "hardware": {**asdict(hardware), "description": hardware.describe()},
        "tasks": [{"id": t.id, "title": t.title} for t in TASKS],
        "runs": [run.to_json() for run in runs],
    }


def markdown(runs: list[ModelRun], tasks: list[Task], hardware: Hardware, ollama_version: str) -> str:
    """A table to paste into a model test report."""
    date = dt.date.today().isoformat()
    lines = [
        f"**lcode bench** · lcode {__version__} · Ollama {ollama_version} · {hardware.describe()} · {date}",
        "",
        "| | " + " | ".join(f"`{r.model}`" for r in runs) + " |",
        "|---|" + "---|" * len(runs),
    ]
    for label, cells in summary_rows(runs, tasks):
        bold = label == "Passed"
        label_md = f"**{label}**" if bold else label
        lines.append(f"| {label_md} | " + " | ".join(f"**{c}**" if bold and c else c for c in cells) + " |")
    return "\n".join(lines) + "\n"


def write_json(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, indent=2) + "\n")


def print_tasks(console: Console) -> None:
    table = Table(title="lcode bench tasks", title_justify="left", header_style="bold")
    table.add_column("Task", style="cyan")
    table.add_column("What it tests")
    for task in TASKS:
        table.add_row(task.id, task.title)
    console.print(table)
    console.print("[dim]Run a subset with --tasks, e.g. lcode bench --tasks fix-bug,rename[/]")
