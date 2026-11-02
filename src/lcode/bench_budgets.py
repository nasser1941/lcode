"""A feature across several modules, for `lcode bench --tasks feature-wide`.

An expense tracker gets monthly budgets: a new command with three subcommands, a change to the report in
text and JSON, budgets saved in the data file next to the expenses, with data files from before the change
still working. The check runs a scripted session from an old data file and compares every output; it also
adds an expense after setting budgets, which loses them if `add` saves only the expenses.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

MODELS = '''\
"""Expenses."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class Expense:
    date: str  # YYYY-MM-DD
    amount: Decimal
    currency: str  # EUR, USD, GBP or CHF
    category: str
    note: str = ""

    def to_json(self) -> dict:
        return {
            "date": self.date,
            "amount": str(self.amount),
            "currency": self.currency,
            "category": self.category,
            "note": self.note,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Expense":
        return cls(data["date"], Decimal(data["amount"]), data["currency"], data["category"], data.get("note", ""))
'''

STORAGE = '''\
"""The data file: expenses.json in the current folder, or the file EXPENSES_FILE names."""

import json
import os
from pathlib import Path

from expenses.models import Expense

VERSION = 1


def data_path() -> Path:
    return Path(os.environ.get("EXPENSES_FILE", "expenses.json"))


def load() -> list:
    path = data_path()
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [Expense.from_json(item) for item in data.get("expenses", [])]


def save(expenses: list) -> None:
    data = {"version": VERSION, "expenses": [expense.to_json() for expense in expenses]}
    data_path().write_text(json.dumps(data, indent=2) + "\\n")
'''

CURRENCY = '''\
"""Currencies, converted to euros."""

from decimal import Decimal

RATES = {"EUR": Decimal("1"), "USD": Decimal("0.92"), "GBP": Decimal("1.17"), "CHF": Decimal("1.05")}


def to_eur(amount: Decimal, currency: str) -> Decimal:
    """An amount in euros, to the cent."""
    if currency not in RATES:
        raise ValueError(f"unknown currency {currency}; use one of {', '.join(RATES)}")
    return (amount * RATES[currency]).quantize(Decimal("0.01"))
'''

FORMATTING = '''\
"""Money as text."""

from decimal import Decimal


def money(amount: Decimal) -> str:
    """Like €1,234.50."""
    return f"€{amount:,.2f}"
'''

REPORT = '''\
"""Monthly reports."""

from decimal import Decimal

from expenses.currency import to_eur
from expenses.formatting import money


def monthly(expenses: list, month: str) -> dict:
    """What was spent in each category in a month (YYYY-MM), in euros, by category name."""
    totals = {}
    for expense in expenses:
        if expense.date.startswith(month + "-"):
            totals[expense.category] = totals.get(expense.category, Decimal(0)) + to_eur(
                expense.amount, expense.currency
            )
    return dict(sorted(totals.items()))


def report_lines(totals: dict) -> list:
    lines = [f"{category}: {money(spent)}" for category, spent in totals.items()]
    lines.append(f"total: {money(sum(totals.values(), Decimal(0)))}")
    return lines


def report_json(totals: dict, month: str) -> dict:
    return {
        "month": month,
        "categories": [{"category": category, "spent": float(spent)} for category, spent in totals.items()],
        "total": float(sum(totals.values(), Decimal(0))),
    }
'''

CLI = '''\
"""The command line: python -m expenses add|list|report."""

import argparse
import datetime as dt
import json
import sys
from decimal import Decimal, InvalidOperation

from expenses import storage
from expenses.currency import RATES
from expenses.formatting import money
from expenses.models import Expense
from expenses.report import monthly, report_json, report_lines


def amount(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not an amount: {text}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("the amount must be more than 0")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="expenses")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="record an expense")
    add.add_argument("amount", type=amount)
    add.add_argument("category")
    add.add_argument("--currency", default="EUR", choices=sorted(RATES))
    add.add_argument("--date", default=dt.date.today().isoformat())
    add.add_argument("--note", default="")
    listing = commands.add_parser("list", help="list expenses")
    listing.add_argument("--month", help="YYYY-MM")
    report = commands.add_parser("report", help="spending per category in a month")
    report.add_argument("--month", required=True, help="YYYY-MM")
    report.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    expenses = storage.load()
    if args.command == "add":
        expenses.append(Expense(args.date, args.amount, args.currency, args.category.lower(), args.note))
        storage.save(expenses)
        print(f"Added {args.amount} {args.currency} for {args.category.lower()} on {args.date}")
    elif args.command == "list":
        for expense in expenses:
            if args.month is None or expense.date.startswith(args.month + "-"):
                note = f"  ({expense.note})" if expense.note else ""
                print(f"{expense.date}  {expense.amount} {expense.currency}  {expense.category}{note}")
    elif args.command == "report":
        totals = monthly(expenses, args.month)
        if args.json:
            print(json.dumps(report_json(totals, args.month)))
        else:
            print("\\n".join(report_lines(totals)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

README = """\
# expenses

Track what you spend, in any of four currencies, and see it per category each month.

    python -m expenses add 12.50 food --note lunch
    python -m expenses add 30 travel --currency USD --date 2026-10-02
    python -m expenses list --month 2026-10
    python -m expenses report --month 2026-10 [--json]

The data is kept in `expenses.json` in the current folder, or in the file `EXPENSES_FILE` names.
Amounts in other currencies count in euros at the rates in `expenses/currency.py`.
"""

TEST_CLI = """\
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from expenses.cli import main


def run(*argv):
    out = io.StringIO()
    with redirect_stdout(out):
        main(list(argv))
    return out.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        os.environ["EXPENSES_FILE"] = str(Path(self.folder.name) / "data.json")

    def tearDown(self):
        del os.environ["EXPENSES_FILE"]
        self.folder.cleanup()

    def test_add_and_report(self):
        run("add", "12.50", "Food", "--date", "2026-10-01")
        run("add", "10", "travel", "--currency", "USD", "--date", "2026-10-03")
        run("add", "99", "food", "--date", "2026-11-01")
        self.assertEqual(
            run("report", "--month", "2026-10").splitlines(), ["food: €12.50", "travel: €9.20", "total: €21.70"]
        )

    def test_report_json(self):
        run("add", "1000", "rent", "--date", "2026-10-01")
        data = json.loads(run("report", "--month", "2026-10", "--json"))
        self.assertEqual(data["categories"], [{"category": "rent", "spent": 1000.0}])
        self.assertEqual(data["total"], 1000.0)

    def test_list_a_month(self):
        run("add", "5", "fun", "--date", "2026-09-30", "--note", "cards")
        run("add", "7", "fun", "--date", "2026-10-01")
        self.assertEqual(run("list", "--month", "2026-09"), "2026-09-30  5 EUR  fun  (cards)\\n")
"""

TEST_STORAGE = """\
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from expenses import storage
from expenses.models import Expense


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        os.environ["EXPENSES_FILE"] = str(Path(self.folder.name) / "data.json")

    def tearDown(self):
        del os.environ["EXPENSES_FILE"]
        self.folder.cleanup()

    def test_nothing_saved_yet(self):
        self.assertEqual(storage.load(), [])

    def test_round_trip(self):
        expense = Expense("2026-10-01", Decimal("12.50"), "GBP", "food", "lunch")
        storage.save([expense])
        self.assertEqual(storage.load(), [expense])
"""

FILES = {
    "README.md": README,
    "expenses/__init__.py": '"""An expense tracker."""\n',
    "expenses/__main__.py": "import sys\n\nfrom expenses.cli import main\n\nsys.exit(main())\n",
    "expenses/cli.py": CLI,
    "expenses/models.py": MODELS,
    "expenses/storage.py": STORAGE,
    "expenses/currency.py": CURRENCY,
    "expenses/formatting.py": FORMATTING,
    "expenses/report.py": REPORT,
    "tests/__init__.py": "",
    "tests/test_cli.py": TEST_CLI,
    "tests/test_storage.py": TEST_STORAGE,
}

PROMPT = """\
Add monthly budgets per category to this expense tracker:
- `python -m expenses budget set CATEGORY AMOUNT` sets a category's monthly budget in euros, replacing \
any it had; `budget unset CATEGORY` removes it; `budget list` prints the budgets in alphabetical order, one \
per line, like `food: €300.00`.
- In `report --month YYYY-MM`, a category with a budget shows as `food: €320.50 of €300.00 (over by €20.50)`, \
or as `food: €120.00 of €300.00` when it isn't over. A category with a budget and nothing spent that month \
is shown too, as `€0.00 of €50.00`. Other categories and the total stay as they are.
- `report --json` gives each category `budget` (a number, or null without one) and `over` (how much over \
budget, 0 when it isn't), and includes the budgeted categories with nothing spent, with `spent` 0.
- Budgets are saved in the data file. Data files written before this change have no budgets and must keep \
working; files written after it have `"version": 2`.
Update the README and the tests, and run them."""

OLD_DATA = {
    "version": 1,
    "expenses": [
        {"date": "2026-09-28", "amount": "80.00", "currency": "EUR", "category": "food", "note": "market"},
        {"date": "2026-10-02", "amount": "120.00", "currency": "EUR", "category": "food", "note": "groceries"},
        {"date": "2026-10-05", "amount": "45.00", "currency": "USD", "category": "travel", "note": "taxi"},
        {"date": "2026-10-09", "amount": "100.00", "currency": "GBP", "category": "food", "note": "dinner"},
        {"date": "2026-10-15", "amount": "15.99", "currency": "EUR", "category": "fun", "note": "cinema"},
        {"date": "2026-10-21", "amount": "19.90", "currency": "CHF", "category": "food", "note": "chocolate"},
        {"date": "2026-11-01", "amount": "9.00", "currency": "EUR", "category": "food", "note": "bakery"},
    ],
}

# The session the check runs, from OLD_DATA: (what it checks, arguments, the expected output or None for any).
OCTOBER = [
    "books: €0.00 of €40.00",
    "food: €257.90 of €250.00 (over by €7.90)",
    "fun: €15.99",
    "travel: €41.40 of €50.50",
    "total: €315.29",
]
NO_BUDGETS: list[str] = []  # `budget list` without budgets: no budget lines, whatever else it says ("No budgets set.")
SESSION = [
    (
        "an old data file still reports",
        ["report", "--month", "2026-10"],
        ["food: €257.90", "fun: €15.99", "travel: €41.40", "total: €315.29"],
    ),
    ("no budgets yet", ["budget", "list"], NO_BUDGETS),
    ("budget set", ["budget", "set", "food", "300"], None),
    ("budget set", ["budget", "set", "travel", "50.5"], None),
    ("budget set replaces", ["budget", "set", "food", "250"], None),
    ("budget set", ["budget", "set", "books", "40"], None),
    ("budget list", ["budget", "list"], ["books: €40.00", "food: €250.00", "travel: €50.50"]),
    ("the report with budgets", ["report", "--month", "2026-10"], OCTOBER),
    ("adding an expense keeps the budgets", ["add", "12.50", "food", "--date", "2026-10-20"], None),
    (
        "the report after adding",
        ["report", "--month", "2026-10"],
        [*OCTOBER[:1], "food: €270.40 of €250.00 (over by €20.40)", *OCTOBER[2:4], "total: €327.79"],
    ),
    ("budget unset", ["budget", "unset", "travel"], None),
    ("budget list after unset", ["budget", "list"], ["books: €40.00", "food: €250.00"]),
    (
        "a month without spending",
        ["report", "--month", "2026-12"],
        ["books: €0.00 of €40.00", "food: €0.00 of €250.00", "total: €0.00"],
    ),
]
JSON_REPORT = {  # report --month 2026-10 --json at the end: category -> (spent, budget, over)
    "books": (0, 40, 0),
    "food": (270.40, 250, 20.40),
    "fun": (15.99, None, 0),
    "travel": (41.40, None, 0),
}


def run(folder: Path, data: Path, argv: list[str]) -> subprocess.CompletedProcess:
    env = {**os.environ, "EXPENSES_FILE": str(data), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", "expenses", *argv],
        cwd=folder,
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )


def lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.strip().splitlines()]


def same(got: list[str], want: list[str], argv: list[str]) -> bool:
    """Whether a command printed what it should. The task fixes the order of `budget list` (alphabetical), not of
    the report's categories, so those may come in any order before the total."""
    if want is NO_BUDGETS:
        return not any(re.match(r"\S+: €", line) for line in got)
    if argv[0] == "report":
        return bool(got) and got[-1] == want[-1] and sorted(got[:-1]) == sorted(want[:-1])
    return got == want


def json_problem(text: str) -> str | None:
    try:
        categories = {c["category"]: c for c in json.loads(text)["categories"]}
    except (ValueError, KeyError, TypeError):
        return "report --json isn't the report with a list of categories"
    for name, (spent, budget, over) in JSON_REPORT.items():
        got = categories.get(name)
        if got is None:
            return f"report --json leaves out {name}"
        want = {"spent": spent, "budget": budget, "over": over}
        for key, value in want.items():
            have = got.get(key, "missing")
            if value is None:
                if have is not None:
                    return f"report --json: {name}'s {key} is {have!r}, not null"
            elif not isinstance(have, (int, float)) or isinstance(have, bool) or abs(have - value) > 0.001:
                return f"report --json: {name}'s {key} is {have!r}, not {value}"
    return None


def check(folder: Path, answer: str):
    from lcode.bench_checks import Check, last_line, run_python

    if not (folder / "expenses" / "cli.py").is_file():
        return Check(False, "expenses/cli.py is missing")
    r = run_python(folder, "-m", "unittest", "-q")
    if r.returncode != 0:
        return Check(False, f"its tests fail: {last_line(r.stderr) or last_line(r.stdout)}")
    with tempfile.TemporaryDirectory(prefix="lcode-bench-budgets-") as tmp:
        data = Path(tmp) / "old.json"
        data.write_text(json.dumps(OLD_DATA, indent=2))
        for what, argv, want in SESSION:
            r = run(folder, data, argv)
            if r.returncode != 0:
                return Check(False, f"{what}: `{' '.join(argv)}` failed: {last_line(r.stderr)}")
            if want is not None and not same(lines(r.stdout), want, argv):
                got = " / ".join(lines(r.stdout)) or "nothing"
                return Check(False, f"{what}: `{' '.join(argv)}` printed {got[:150]}")
        r = run(folder, data, ["report", "--month", "2026-10", "--json"])
        problem = json_problem(r.stdout) if r.returncode == 0 else f"report --json failed: {last_line(r.stderr)}"
        if problem:
            return Check(False, problem)
        saved = json.loads(data.read_text())
        if saved.get("version") != 2:
            return Check(False, f'the data file has "version": {saved.get("version")!r}, not 2')
        if len(saved.get("expenses", [])) != len(OLD_DATA["expenses"]) + 1:
            return Check(False, "the data file lost expenses")
    return Check(True, "budgets work in the report, in JSON and with old data, and its tests pass")
