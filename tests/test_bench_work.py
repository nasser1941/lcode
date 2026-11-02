"""The longer `lcode bench` tasks: each check passes a correct solution and fails the likely shortcuts."""

import io
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console

from conftest import FakeOllama
from lcode import bench, bench_budgets, bench_dedupe, bench_flaky, bench_logsum, bench_quotes
from lcode.agent import Settings


def setup(task_id: str, folder: Path, **files: str) -> Path:
    task = bench.select_tasks(task_id)[0]
    for rel, content in {**task.files, **files}.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(content)
    return folder


def check(task_id: str, folder: Path):
    return bench.select_tasks(task_id)[0].check(folder, "Done.")


def patched(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new)


# ----------------------------------------------------------------------------- cli-tool


def test_cli_tool_spec_shows_what_the_sample_gives(tmp_path):
    setup("cli-tool", tmp_path, **{"logsum.py": bench_logsum.REFERENCE})
    r = subprocess.run([sys.executable, "logsum.py", "access.log"], cwd=tmp_path, capture_output=True, text=True)
    printed = "\n".join(line.strip() for line in r.stdout.strip().splitlines())
    shown = bench_logsum.SPEC.split("prints\n\n")[1].split("\n\nand")[0]
    assert printed == "\n".join(line.strip() for line in shown.splitlines())
    assert r.stderr == "logsum: skipped 1 malformed line(s)\n" and r.returncode == 0


def test_cli_tool_passes_the_reference(tmp_path):
    result = check("cli-tool", setup("cli-tool", tmp_path, **{"logsum.py": bench_logsum.REFERENCE}))
    assert result.passed, result.detail


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        (
            "if since is not None and when < since:",
            "if since is not None and when.replace(tzinfo=None) < since.replace(tzinfo=None):",
            "--since compares moments, not clock readings",
        ),
        ('for name in args.files or ["-"]:', "for name in args.files:", "standard input when there's no FILE"),
        ("            code = 1\n            continue", "            sys.exit(1)", "an unreadable file"),
        ("key=lambda item: (-item[1], item[0])", "key=lambda item: -item[1]", "the summary of a log"),
        ("        if since.tzinfo is None:\n", "        if False:\n", "--since needs an offset"),
    ],
)
def test_cli_tool_catches_a_missed_requirement(tmp_path, old, new, problem):
    script = patched(bench_logsum.REFERENCE, old, new)
    result = check("cli-tool", setup("cli-tool", tmp_path, **{"logsum.py": script}))
    assert not result.passed and problem in result.detail


def test_cli_tool_needs_the_script(tmp_path):
    assert check("cli-tool", setup("cli-tool", tmp_path)).detail == "logsum.py is missing"


# ----------------------------------------------------------------------------- flaky-test

ORDERED = "return list(dict.fromkeys(i.warehouse for i in self._items))"
SET = "return list({i.warehouse for i in self._items})"


def test_flaky_test_fails_now_and_then(tmp_path):
    setup("flaky-test", tmp_path)
    failed = sum(bench_flaky.unittest_with_seed(tmp_path, seed).returncode != 0 for seed in range(bench_flaky.SEEDS))
    assert 0 < failed < bench_flaky.SEEDS  # the tests pass under some hash seeds and fail under others
    assert "still flaky" in check("flaky-test", tmp_path).detail


def test_flaky_test_passes_a_fix_that_keeps_the_order(tmp_path):
    fixed = patched(bench_flaky.INVENTORY, SET, ORDERED)
    result = check("flaky-test", setup("flaky-test", tmp_path, **{"stockroom/inventory.py": fixed}))
    assert result.passed, result.detail


@pytest.mark.parametrize(
    ("order", "problem"),
    [
        ("return sorted({i.warehouse for i in self._items})", "fail every time"),
        ("return sorted({i.warehouse for i in self._items}, reverse=True)", "doesn't keep the order"),
    ],
)
def test_flaky_test_catches_a_fixed_order_that_isnt_the_right_one(tmp_path, order, problem):
    inventory = patched(bench_flaky.INVENTORY, SET, order)
    result = check("flaky-test", setup("flaky-test", tmp_path, **{"stockroom/inventory.py": inventory}))
    assert not result.passed and problem in result.detail


def test_flaky_test_runs_the_tests_as_they_were(tmp_path):
    test = patched(
        bench_flaky.TEST_RESTOCK,
        'self.assertEqual(restock_plan(stocked()), ["south: washers", "north: bolts, nuts"])',
        'self.assertEqual(sorted(restock_plan(stocked())), ["north: bolts, nuts", "south: washers"])',
    )
    setup("flaky-test", tmp_path, **{"tests/test_restock.py": test})
    assert "still flaky" in check("flaky-test", tmp_path).detail


# ----------------------------------------------------------------------------- repo-bug

KEY = "key = args + tuple(kwargs)"


def test_repo_bug_is_a_larger_repository_whose_tests_pass(tmp_path):
    setup("repo-bug", tmp_path)
    assert sum(1 for path in tmp_path.rglob("*.py")) >= 40
    r = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "first one's shipping cost" in check("repo-bug", tmp_path).detail


@pytest.mark.parametrize(
    "cache",
    [
        patched(bench_quotes.CACHE, KEY, "key = args + tuple(sorted(kwargs.items()))"),
        "import functools\n\n\ndef memoize(fn):\n    return functools.lru_cache(maxsize=None)(fn)\n",
    ],
)
def test_repo_bug_passes_a_fix_of_the_cause(tmp_path, cache):
    result = check("repo-bug", setup("repo-bug", tmp_path, **{"quotes/util/cache.py": cache}))
    assert result.passed, result.detail


@pytest.mark.parametrize(
    ("files", "problem"),
    [
        (
            {
                "quotes/orders/quote.py": patched(
                    bench_quotes.QUOTE, "rate(country, weight=weight)", "rate(country, weight)"
                )
            },
            "wrong VAT rates",
        ),
        ({"quotes/shipping/rates.py": patched(bench_quotes.RATES, "@memoize\ndef rate", "def rate")}, "the tests fail"),
        (
            {"quotes/util/cache.py": "def memoize(fn):\n    fn.cache_clear = lambda: None\n    return fn\n"},
            "the tests fail",
        ),
    ],
)
def test_repo_bug_catches_band_aids(tmp_path, files, problem):
    result = check("repo-bug", setup("repo-bug", tmp_path, **files))
    assert not result.passed and problem in result.detail


# ----------------------------------------------------------------------------- wide-feature


BUDGETS_STORAGE = '''\
"""The data file: expenses.json in the current folder, or the file EXPENSES_FILE names."""

import json
import os
from decimal import Decimal
from pathlib import Path

from expenses.models import Expense

VERSION = 2


def data_path() -> Path:
    return Path(os.environ.get("EXPENSES_FILE", "expenses.json"))


def _read() -> dict:
    path = data_path()
    return json.loads(path.read_text()) if path.exists() else {}


def load() -> list:
    return [Expense.from_json(item) for item in _read().get("expenses", [])]


def load_budgets() -> dict:
    return {category: Decimal(value) for category, value in _read().get("budgets", {}).items()}


def save(expenses: list, budgets: dict | None = None) -> None:
    if budgets is None:
        budgets = load_budgets()
    data = {
        "version": VERSION,
        "expenses": [expense.to_json() for expense in expenses],
        "budgets": {category: str(value) for category, value in sorted(budgets.items())},
    }
    data_path().write_text(json.dumps(data, indent=2) + "\\n")
'''

BUDGETS_REPORT = '''\
"""Monthly reports."""

from decimal import Decimal

from expenses.currency import to_eur
from expenses.formatting import money


def monthly(expenses: list, month: str, budgets: dict | None = None) -> dict:
    totals = {category: Decimal(0) for category in budgets or {}}
    for expense in expenses:
        if expense.date.startswith(month + "-"):
            spent = to_eur(expense.amount, expense.currency)
            totals[expense.category] = totals.get(expense.category, Decimal(0)) + spent
    return dict(sorted(totals.items()))


def line(category, spent, budget):
    if budget is None:
        return f"{category}: {money(spent)}"
    over = f" (over by {money(spent - budget)})" if spent > budget else ""
    return f"{category}: {money(spent)} of {money(budget)}{over}"


def report_lines(totals: dict, budgets: dict | None = None) -> list:
    budgets = budgets or {}
    lines = [line(category, spent, budgets.get(category)) for category, spent in totals.items()]
    lines.append(f"total: {money(sum(totals.values(), Decimal(0)))}")
    return lines


def report_json(totals: dict, month: str, budgets: dict | None = None) -> dict:
    budgets = budgets or {}
    return {
        "month": month,
        "categories": [
            {
                "category": category,
                "spent": float(spent),
                "budget": float(budgets[category]) if category in budgets else None,
                "over": float(max(spent - budgets[category], Decimal(0))) if category in budgets else 0,
            }
            for category, spent in totals.items()
        ],
        "total": float(sum(totals.values(), Decimal(0))),
    }
'''


def budgets_cli(original: str) -> str:
    text = original.replace(
        "    args = parser.parse_args(argv)\n",
        '    budget = commands.add_parser("budget", help="monthly budgets")\n'
        '    actions = budget.add_subparsers(dest="action", required=True)\n'
        '    setting = actions.add_parser("set")\n'
        '    setting.add_argument("category")\n'
        '    setting.add_argument("amount", type=amount)\n'
        '    unsetting = actions.add_parser("unset")\n'
        '    unsetting.add_argument("category")\n'
        '    actions.add_parser("list")\n'
        "    args = parser.parse_args(argv)\n",
    )
    text = text.replace(
        "    expenses = storage.load()\n",
        "    expenses = storage.load()\n    budgets = storage.load_budgets()\n",
    )
    text = text.replace(
        '    elif args.command == "report":\n        totals = monthly(expenses, args.month)\n'
        "        if args.json:\n            print(json.dumps(report_json(totals, args.month)))\n"
        '        else:\n            print("\\n".join(report_lines(totals)))\n',
        '    elif args.command == "report":\n        totals = monthly(expenses, args.month, budgets)\n'
        "        if args.json:\n            print(json.dumps(report_json(totals, args.month, budgets)))\n"
        '        else:\n            print("\\n".join(report_lines(totals, budgets)))\n'
        '    elif args.command == "budget":\n'
        '        if args.action == "set":\n'
        "            budgets[args.category.lower()] = args.amount\n"
        "            storage.save(expenses, budgets)\n"
        '        elif args.action == "unset":\n'
        "            budgets.pop(args.category.lower(), None)\n"
        "            storage.save(expenses, budgets)\n"
        "        else:\n"
        "            for category, value in sorted(budgets.items()):\n"
        '                print(f"{category}: {money(value)}")\n',
    )
    assert text.count("budget") > 10, "patch didn't apply"
    return text


def budgets_tests(original: str) -> str:
    old = '[{"category": "rent", "spent": 1000.0}]'
    assert old in original
    return original.replace(old, '[{"category": "rent", "spent": 1000.0, "budget": None, "over": 0}]')


def budgets() -> dict[str, str]:
    """A complete solution: budgets in the data file, the report and the command line, and the tests updated."""
    return {
        "expenses/storage.py": BUDGETS_STORAGE,
        "expenses/report.py": BUDGETS_REPORT,
        "expenses/cli.py": budgets_cli(bench_budgets.CLI),
        "tests/test_cli.py": budgets_tests(bench_budgets.TEST_CLI),
    }


def test_wide_feature_passes_a_complete_solution(tmp_path):
    result = check("wide-feature", setup("wide-feature", tmp_path, **budgets()))
    assert result.passed, result.detail


def test_wide_feature_needs_the_budget_command(tmp_path):
    result = check("wide-feature", setup("wide-feature", tmp_path))
    assert not result.passed and "budget list" in result.detail


@pytest.mark.parametrize(
    ("file", "old", "new", "problem"),
    [
        (
            "expenses/storage.py",
            "    if budgets is None:\n        budgets = load_budgets()\n",
            "    budgets = budgets or {}\n",
            "the report after adding",
        ),
        ("expenses/storage.py", "VERSION = 2", "VERSION = 1", '"version": 1, not 2'),
        (
            "expenses/report.py",
            "    totals = {category: Decimal(0) for category in budgets or {}}\n",
            "    totals = {}\n",
            "the report with budgets",
        ),
    ],
)
def test_wide_feature_catches_a_missed_requirement(tmp_path, file, old, new, problem):
    files = budgets()
    files[file] = patched(files[file], old, new)
    result = check("wide-feature", setup("wide-feature", tmp_path, **files))
    assert not result.passed and problem in result.detail


def test_wide_feature_runs_its_tests(tmp_path):
    files = budgets()
    files["tests/test_cli.py"] = bench_budgets.TEST_CLI  # not updated for the new JSON fields
    result = check("wide-feature", setup("wide-feature", tmp_path, **files))
    assert not result.passed and result.detail.startswith("its tests fail")


# ----------------------------------------------------------------------------- fast-dedupe


def dedupe_output(script: str, records: str, folder: Path) -> str:
    folder.mkdir()
    (folder / "job.py").write_text(script)
    (folder / "in.csv").write_text(records)
    subprocess.run([sys.executable, "job.py", "in.csv", "out.csv"], cwd=folder, check=True, timeout=120)
    return (folder / "out.csv").read_text()


def test_fast_dedupe_reference_gives_the_slow_jobs_output(tmp_path):
    records = bench_dedupe.customers_csv(1500, seed=5)
    slow = dedupe_output(bench_dedupe.DEDUPE, records, tmp_path / "slow")
    assert slow == dedupe_output(bench_dedupe.REFERENCE, records, tmp_path / "fast")
    customers = len(slow.splitlines()) - 1
    assert 900 < customers < 1300  # many records merge, and many don't


def test_fast_dedupe_passes_a_fast_job_with_the_same_output(tmp_path):
    result = check("fast-dedupe", setup("fast-dedupe", tmp_path, **{"dedupe.py": bench_dedupe.REFERENCE}))
    assert result.passed, result.detail


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        ("            if key[1]:\n", "            if True:\n", "customers, not"),  # empty fields match
        (
            'newest = max(rows, key=lambda r: r["updated"])',
            'newest = max(rows[::-1], key=lambda r: r["updated"])',
            "from row",
        ),
        ('"id": str(min(int(r["id"]) for r in rows)),', '"id": min(r["id"] for r in rows),', "from row"),
        (
            'for key in (("e", normal_email(record["email"])), ("p", normal_phone(record["phone"]))):',
            'for key in (("e", normal_email(record["email"])),):',
            "customers, not",
        ),
    ],
)
def test_fast_dedupe_catches_a_changed_output(tmp_path, old, new, problem):
    script = patched(bench_dedupe.REFERENCE, old, new)
    result = check("fast-dedupe", setup("fast-dedupe", tmp_path, **{"dedupe.py": script}))
    assert not result.passed and problem in result.detail


def test_fast_dedupe_needs_it_fast(tmp_path, monkeypatch):
    monkeypatch.setattr(bench_dedupe, "CASES", [(50, 1, 1)])
    result = check("fast-dedupe", setup("fast-dedupe", tmp_path, **{"dedupe.py": "import time\ntime.sleep(5)\n"}))
    assert result.detail == "still too slow: 50 records took over 1 s"


# ----------------------------------------------------------------------------- time limits


def test_longer_tasks_get_more_time_unless_timeout_says_otherwise(monkeypatch):
    limits = []

    def run_task(ollama, settings, task, limit, *args):
        limits.append(limit)
        return bench.TaskResult(task.id, True, 1.0, "")

    monkeypatch.setattr(bench, "prompt_speed", lambda *args: None)
    monkeypatch.setattr(bench, "run_task", run_task)
    tasks = bench.select_tasks("fix-bug,repo-bug")
    settings = Settings(model="m", context=32768, permission_mode="yolo", web="off", checkpoints=False)
    bench.run_model(FakeOllama(), "m", settings, tasks, Console(file=io.StringIO()))
    bench.run_model(FakeOllama(), "m", settings, tasks, Console(file=io.StringIO()), timeout=42)
    assert limits == [300, 600, 42, 42]
