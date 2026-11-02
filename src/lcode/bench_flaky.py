"""A test that fails now and then, for `lcode bench --tasks flaky-test`.

A small stockroom package whose restock plan lists warehouses in the order of a set, so it depends on
Python's string hashing, which changes from one run to the next: `tests/test_restock.py` fails in about half
the runs. Running the tests once often passes; the model has to run them several times (or read the code
for an order that isn't fixed), find the set in `inventory.py`, and keep the order the docstring promises.
Sorting the warehouses makes the test fail every time, and changing the tests doesn't count: the check runs
the original tests under 24 hash seeds, and a hidden test of the order.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

INIT = '"""Stock levels across warehouses, and what to reorder."""\n'

ITEMS = '''\
"""Stock items."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    name: str
    sku: str
    warehouse: str
    quantity: int

    def with_quantity(self, quantity):
        return Item(self.name, self.sku, self.warehouse, quantity)
'''

INVENTORY = '''\
"""What's in stock, where."""

from stockroom.items import Item


class Inventory:
    def __init__(self):
        self._items = []

    def add(self, item: Item) -> None:
        """Add an item, or replace the one with the same SKU in the same warehouse."""
        self._items = [i for i in self._items if (i.sku, i.warehouse) != (item.sku, item.warehouse)]
        self._items.append(item)

    def items_in(self, warehouse: str) -> list:
        """The items in a warehouse, in the order they were added."""
        return [i for i in self._items if i.warehouse == warehouse]

    def warehouses(self) -> list:
        """The warehouses, in the order their first item was added."""
        return list({i.warehouse for i in self._items})

    def skus(self) -> list:
        """Every SKU in stock, sorted."""
        return sorted({i.sku for i in self._items})

    def total(self, sku: str) -> int:
        """How many of a SKU there are, in all warehouses."""
        return sum(i.quantity for i in self._items if i.sku == sku)

    def take(self, sku: str, warehouse: str, quantity: int) -> None:
        """Take items out of a warehouse; raises ValueError if there aren't enough."""
        for item in self.items_in(warehouse):
            if item.sku == sku:
                if item.quantity < quantity:
                    raise ValueError(f"only {item.quantity} of {sku} in {warehouse}")
                self.add(item.with_quantity(item.quantity - quantity))
                return
        raise ValueError(f"no {sku} in {warehouse}")
'''

RESTOCK = '''\
"""What to reorder."""

from stockroom.formatting import names
from stockroom.inventory import Inventory


def low(inventory: Inventory, warehouse: str, threshold: int) -> list:
    """The items in a warehouse with fewer than `threshold` left."""
    return [i for i in inventory.items_in(warehouse) if i.quantity < threshold]


def restock_plan(inventory: Inventory, threshold: int = 5) -> list:
    """One line per warehouse that needs anything, like "north: bolts, nuts"."""
    plan = []
    for warehouse in inventory.warehouses():
        items = low(inventory, warehouse, threshold)
        if items:
            plan.append(f"{warehouse}: {names(items)}")
    return plan


def order_quantities(inventory: Inventory, target: int) -> dict:
    """How many of each SKU to order so every warehouse that stocks it has `target`."""
    order = {}
    for warehouse in inventory.warehouses():
        for item in inventory.items_in(warehouse):
            if item.quantity < target:
                order[item.sku] = order.get(item.sku, 0) + target - item.quantity
    return order
'''

FORMATTING = '''\
"""Text for people."""


def names(items) -> str:
    """The items' names, in the order given."""
    return ", ".join(item.name for item in items)


def quantity(n: int) -> str:
    return "none" if n == 0 else f"{n} left"
'''

PRICING = '''\
"""What a reorder costs."""

PRICES = {"B-1": 0.10, "N-1": 0.05, "W-1": 0.02, "S-4": 1.25}


def cost(order: dict) -> float:
    """The cost of an order, {sku: quantity}, in euros."""
    return round(sum(PRICES.get(sku, 0) * quantity for sku, quantity in order.items()), 2)
'''

TEST_INVENTORY = """\
import unittest

from stockroom.inventory import Inventory
from stockroom.items import Item


def stocked():
    inventory = Inventory()
    inventory.add(Item("bolts", "B-1", "east", 40))
    inventory.add(Item("nuts", "N-1", "east", 3))
    inventory.add(Item("bolts", "B-1", "west", 2))
    return inventory


class InventoryTest(unittest.TestCase):
    def test_total_counts_every_warehouse(self):
        self.assertEqual(stocked().total("B-1"), 42)

    def test_adding_the_same_sku_replaces_it(self):
        inventory = stocked()
        inventory.add(Item("bolts", "B-1", "east", 10))
        self.assertEqual(inventory.total("B-1"), 12)

    def test_take(self):
        inventory = stocked()
        inventory.take("N-1", "east", 2)
        self.assertEqual(inventory.total("N-1"), 1)
        with self.assertRaises(ValueError):
            inventory.take("N-1", "east", 5)

    def test_skus_are_sorted(self):
        self.assertEqual(stocked().skus(), ["B-1", "N-1"])


if __name__ == "__main__":
    unittest.main()
"""

TEST_RESTOCK = """\
import unittest

from stockroom.inventory import Inventory
from stockroom.items import Item
from stockroom.pricing import cost
from stockroom.restock import order_quantities, restock_plan


def stocked():
    inventory = Inventory()
    inventory.add(Item("washers", "W-1", "south", 2))
    inventory.add(Item("bolts", "B-1", "north", 1))
    inventory.add(Item("screws", "S-4", "south", 80))
    inventory.add(Item("nuts", "N-1", "north", 4))
    return inventory


class RestockTest(unittest.TestCase):
    def test_plan(self):
        self.assertEqual(restock_plan(stocked()), ["south: washers", "north: bolts, nuts"])

    def test_nothing_to_restock(self):
        inventory = Inventory()
        inventory.add(Item("screws", "S-4", "south", 80))
        self.assertEqual(restock_plan(inventory), [])

    def test_order(self):
        order = order_quantities(stocked(), target=10)
        self.assertEqual(order, {"W-1": 8, "B-1": 9, "N-1": 6})
        self.assertEqual(cost(order), 1.36)


if __name__ == "__main__":
    unittest.main()
"""

FILES = {
    "README.md": "# stockroom\n\nStock levels across warehouses. Run the tests with `python -m unittest`.\n",
    "stockroom/__init__.py": INIT,
    "stockroom/items.py": ITEMS,
    "stockroom/inventory.py": INVENTORY,
    "stockroom/restock.py": RESTOCK,
    "stockroom/formatting.py": FORMATTING,
    "stockroom/pricing.py": PRICING,
    "tests/__init__.py": "",
    "tests/test_inventory.py": TEST_INVENTORY,
    "tests/test_restock.py": TEST_RESTOCK,
}

PROMPT = (
    "tests/test_restock.py fails now and then: in about half of the CI runs, with the same code. Find out why "
    "and fix it so the tests pass every time. The tests describe what we want, so don't change them."
)

HIDDEN_TEST = """\
import unittest

from stockroom.inventory import Inventory
from stockroom.items import Item
from stockroom.restock import restock_plan


class HiddenTest(unittest.TestCase):
    def test_warehouses_keep_the_order_they_were_stocked_in(self):
        inventory = Inventory()
        for n, warehouse in enumerate(["harbor", "airport", "depot", "harbor", "yard", "airport", "mill"]):
            inventory.add(Item(f"part {n}", f"P-{n}", warehouse, n))
        self.assertEqual(inventory.warehouses(), ["harbor", "airport", "depot", "yard", "mill"])
        self.assertEqual(
            restock_plan(inventory, threshold=6),
            ["harbor: part 0, part 3", "airport: part 1, part 5", "depot: part 2", "yard: part 4"],
        )


if __name__ == "__main__":
    unittest.main()
"""

SEEDS = 24


def unittest_with_seed(folder: Path, seed: int, *names: str) -> subprocess.CompletedProcess:
    from lcode.bench import CHECK_TIMEOUT

    env = {**os.environ, "PYTHONHASHSEED": str(seed), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-m", "unittest", "-q", *names],
        cwd=folder,
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=CHECK_TIMEOUT,
    )


def check(folder: Path, answer: str):
    from lcode.bench import Check, last_line

    if not (folder / "stockroom" / "inventory.py").is_file():
        return Check(False, "stockroom/inventory.py is missing")
    for name in ("tests/__init__.py", "tests/test_inventory.py", "tests/test_restock.py"):  # the tests as they were
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(FILES[name])
    failed = [(seed, r) for seed in range(SEEDS) if (r := unittest_with_seed(folder, seed)).returncode != 0]
    if failed:
        seed, r = failed[0]
        if len(failed) == SEEDS:
            return Check(False, f"the tests fail every time: {last_line(r.stderr) or last_line(r.stdout)}")
        return Check(
            False, f"still flaky: the tests failed under {len(failed)} of {SEEDS} hash seeds (PYTHONHASHSEED={seed})"
        )
    (folder / "tests" / "test_hidden_order.py").write_text(HIDDEN_TEST)
    for seed in range(4):
        if unittest_with_seed(folder, seed, "tests.test_hidden_order").returncode != 0:
            return Check(False, "the tests pass, but warehouses() doesn't keep the order they were stocked in")
    return Check(True, f"the tests pass under all {SEEDS} hash seeds, and warehouses keep their order")
