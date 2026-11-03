"""The eight quick tasks of lcode bench: their files, prompts and checks.

Some checks rerun the model's code on data it never saw, so hard-coding the expected output doesn't pass.
"""

from __future__ import annotations

import re
import shutil
import statistics
from pathlib import Path

from lcode.bench_checks import Check, Task, last_line, read, run_python, unittest_check

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
