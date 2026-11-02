"""A bug in a larger repository, for `lcode bench --tasks repo-bug`.

About forty files of a shop's quoting service, with its tests. Support reports that a second quote gets the
first one's shipping cost. The cause is three modules away from the quote: the cache decorator builds its
key from the keyword arguments' names, not their values, so `rate("NO", weight=2)` and `rate("NO",
weight=30)` share an entry. The same decorator caches VAT rates, which another module looks up the same way.
Removing the cache from the shipping rate, or passing the weight positionally, fixes the report but not the
cause: the check also asks for VAT rates the same way, and checks that lookups stay cached.
"""

from __future__ import annotations

from pathlib import Path

CACHE = '''\
"""Caching for lookups that are slow (the rate tables live in a database in production)."""

import functools


def memoize(fn):
    """Remember fn's results by its arguments, so each lookup is made once."""
    results = {}

    @functools.wraps(fn)
    def cached(*args, **kwargs):
        key = args + tuple(kwargs)
        if key not in results:
            results[key] = fn(*args, **kwargs)
        return results[key]

    cached.cache_clear = results.clear
    return cached
'''

RATES = '''\
"""Shipping rates by zone and weight."""

from quotes.shipping.zones import zone_for
from quotes.util.cache import memoize
from quotes.util.money import cents

# Euros for a parcel up to each weight in kg, by zone.
TABLE = {
    "domestic": [(2, 4.90), (10, 7.90), (31.5, 12.90)],
    "eu": [(2, 9.90), (10, 15.90), (31.5, 24.90)],
    "world": [(2, 19.90), (10, 39.90), (31.5, 69.90)],
}


class TooHeavy(ValueError):
    pass


@memoize
def rate(country: str, weight: float) -> int:
    """The shipping rate in cents for a parcel of `weight` kg to `country`."""
    for limit, price in TABLE[zone_for(country)]:
        if weight <= limit:
            return cents(price)
    raise TooHeavy(f"{weight} kg is over the 31.5 kg limit")
'''

VAT = '''\
"""VAT rates."""

from quotes.util.cache import memoize

STANDARD = {"DE": 19, "FR": 20, "NL": 21, "AT": 20, "IT": 22, "ES": 21}
REDUCED = {"DE": 7, "FR": 5.5, "NL": 9, "AT": 10, "IT": 4, "ES": 4}
REDUCED_CATEGORIES = {"books", "food"}


@memoize
def vat_rate(country: str, category: str) -> float:
    """The VAT rate in percent for a product category sold to `country` (0 outside the EU)."""
    if country not in STANDARD:
        return 0
    return REDUCED[country] if category in REDUCED_CATEGORIES else STANDARD[country]
'''

QUOTE = '''\
"""Quotes: what an order will cost, before it's placed."""

from dataclasses import dataclass

from quotes.customers.loyalty import discount_percent
from quotes.orders.totals import line_total
from quotes.shipping.packaging import parcel_weight
from quotes.shipping.rates import rate
from quotes.tax.vat import vat_rate


@dataclass
class Quote:
    items: int  # cents, VAT included
    shipping: int  # cents
    discount: int  # cents
    weight: float  # kg, with packaging

    @property
    def total(self) -> int:
        return self.items + self.shipping - self.discount


def build_quote(cart, customer) -> Quote:
    country = customer.address.country
    weight = parcel_weight(cart)
    items = sum(line_total(line, vat_rate(country, line.product.category)) for line in cart.lines)
    discount = items * discount_percent(customer) // 100
    return Quote(items=items, shipping=rate(country, weight=weight), discount=discount, weight=weight)
'''

HANDLERS = '''\
"""Request handlers of the quote API."""

from quotes.api.serializers import quote_json
from quotes.api.validation import ValidationError, check_country
from quotes.orders.quote import build_quote
from quotes.tax.vat import vat_rate


def quote(cart, customer) -> dict:
    check_country(customer.address.country)
    return quote_json(build_quote(cart, customer))


def vat_info(country: str, category: str) -> dict:
    """What the product pages show next to a price."""
    try:
        check_country(country)
    except ValidationError as e:
        return {"error": str(e)}
    return {"country": country, "category": category, "vat": vat_rate(country, category=category)}
'''

FILES = {
    "README.md": (
        "# quotes\n\nThe quoting service of a small web shop in Germany: what an order will cost, with VAT,\n"
        "shipping and loyalty discounts. Run the tests with `python -m unittest`.\n"
    ),
    "quotes/__init__.py": "",
    "quotes/config.py": '''\
"""Settings."""

SHOP_COUNTRY = "DE"
CURRENCY = "EUR"
MAX_LINES = 50
QUOTE_VALID_DAYS = 14
''',
    "quotes/util/__init__.py": "",
    "quotes/util/cache.py": CACHE,
    "quotes/util/money.py": '''\
"""Money is kept in whole cents."""


def cents(euros: float) -> int:
    return round(euros * 100)


def euros(amount: int) -> str:
    """How an amount in cents is shown, like 12.90 €."""
    return f"{amount // 100}.{amount % 100:02d} €"
''',
    "quotes/util/units.py": '''\
"""Weights."""

POUND = 0.45359237


def kg_from_pounds(pounds: float) -> float:
    return round(pounds * POUND, 3)


def grams(kg: float) -> int:
    return round(kg * 1000)
''',
    "quotes/util/dates.py": '''\
"""Dates on quotes."""

import datetime as dt

from quotes.config import QUOTE_VALID_DAYS


def valid_until(today: dt.date) -> dt.date:
    return today + dt.timedelta(days=QUOTE_VALID_DAYS)
''',
    "quotes/util/text.py": '''\
"""Text helpers."""


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")
''',
    "quotes/catalog/__init__.py": "",
    "quotes/catalog/products.py": '''\
"""The products for sale."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price: int  # cents, before VAT
    weight: float  # kg
    category: str


CATALOG = {
    p.sku: p
    for p in [
        Product("BK-1", "Field guide to mushrooms", 2400, 0.9, "books"),
        Product("BK-2", "Atlas of the Alps", 3900, 1.6, "books"),
        Product("TL-1", "Cast iron pan", 4500, 3.2, "kitchen"),
        Product("TL-2", "Kettle", 3500, 1.1, "kitchen"),
        Product("FD-1", "Coffee beans, 1 kg", 1800, 1.0, "food"),
        Product("EL-1", "Headlamp", 2900, 0.2, "electronics"),
        Product("GD-1", "Garden bench", 18900, 24.0, "garden"),
    ]
}


def find(sku: str) -> Product:
    return CATALOG[sku]
''',
    "quotes/catalog/bundles.py": '''\
"""Products sold together."""

from quotes.catalog.products import find

BUNDLES = {"camp-kitchen": ["TL-1", "TL-2", "FD-1"]}


def bundle(name: str) -> list:
    return [find(sku) for sku in BUNDLES[name]]
''',
    "quotes/catalog/stock.py": '''\
"""Stock levels (a stand-in for the warehouse API)."""

STOCK = {"BK-1": 12, "BK-2": 3, "TL-1": 0, "TL-2": 8, "FD-1": 40, "EL-1": 25, "GD-1": 2}


def in_stock(sku: str, quantity: int = 1) -> bool:
    return STOCK.get(sku, 0) >= quantity
''',
    "quotes/customers/__init__.py": "",
    "quotes/customers/addresses.py": '''\
"""Delivery addresses."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Address:
    name: str
    street: str
    city: str
    postcode: str
    country: str  # ISO code, like DE
''',
    "quotes/customers/accounts.py": '''\
"""Customer accounts."""

from dataclasses import dataclass

from quotes.customers.addresses import Address


@dataclass
class Customer:
    id: str
    email: str
    address: Address
    orders: int = 0  # orders placed so far
''',
    "quotes/customers/loyalty.py": '''\
"""Discounts for customers who come back."""

TIERS = [(20, 10), (10, 5), (3, 2)]  # (orders placed, discount in percent)


def discount_percent(customer) -> int:
    for orders, percent in TIERS:
        if customer.orders >= orders:
            return percent
    return 0
''',
    "quotes/shipping/__init__.py": "",
    "quotes/shipping/zones.py": '''\
"""Shipping zones."""

from quotes.config import SHOP_COUNTRY

EU = {"AT", "BE", "DK", "ES", "FI", "FR", "IE", "IT", "NL", "PL", "PT", "SE"}


def zone_for(country: str) -> str:
    if country == SHOP_COUNTRY:
        return "domestic"
    return "eu" if country in EU else "world"
''',
    "quotes/shipping/rates.py": RATES,
    "quotes/shipping/packaging.py": '''\
"""Boxes and their weight."""

BOXES = [(1, 0.15), (5, 0.4), (15, 0.9), (31.5, 1.5)]  # (contents up to kg, box weight in kg)


def box_weight(contents: float) -> float:
    for limit, weight in BOXES:
        if contents <= limit:
            return weight
    return BOXES[-1][1]


def parcel_weight(cart) -> float:
    """The weight of a cart's parcel, box included, in kg."""
    contents = sum(line.product.weight * line.quantity for line in cart.lines)
    return round(contents + box_weight(contents), 2)
''',
    "quotes/shipping/carriers.py": '''\
"""Which carrier takes a parcel."""

from quotes.shipping.zones import zone_for

CARRIERS = {"domestic": "DHL", "eu": "DPD", "world": "UPS"}


def carrier(country: str) -> str:
    return CARRIERS[zone_for(country)]
''',
    "quotes/shipping/delivery.py": '''\
"""Delivery times."""

from quotes.shipping.zones import zone_for

DAYS = {"domestic": (1, 2), "eu": (2, 5), "world": (5, 12)}


def delivery_days(country: str) -> str:
    low, high = DAYS[zone_for(country)]
    return f"{low}-{high} working days"
''',
    "quotes/tax/__init__.py": "",
    "quotes/tax/vat.py": VAT,
    "quotes/tax/exemptions.py": '''\
"""Business customers in other EU countries pay no VAT (reverse charge)."""

import re

VAT_ID = re.compile(r"[A-Z]{2}[0-9A-Z]{8,12}")


def reverse_charge(vat_id: str | None, country: str) -> bool:
    return bool(vat_id and VAT_ID.fullmatch(vat_id) and not vat_id.startswith("DE") and country != "DE")
''',
    "quotes/orders/__init__.py": "",
    "quotes/orders/cart.py": '''\
"""Shopping carts."""

from dataclasses import dataclass, field

from quotes.catalog.products import Product, find
from quotes.config import MAX_LINES


@dataclass
class Line:
    product: Product
    quantity: int


@dataclass
class Cart:
    lines: list = field(default_factory=list)

    def add(self, sku: str, quantity: int = 1) -> None:
        for line in self.lines:
            if line.product.sku == sku:
                line.quantity += quantity
                return
        if len(self.lines) >= MAX_LINES:
            raise ValueError("the cart is full")
        self.lines.append(Line(find(sku), quantity))

    def remove(self, sku: str) -> None:
        self.lines = [line for line in self.lines if line.product.sku != sku]
''',
    "quotes/orders/totals.py": '''\
"""Line totals."""


def line_total(line, vat_percent: float) -> int:
    """A cart line's price in cents, VAT included."""
    net = line.product.price * line.quantity
    return round(net * (1 + vat_percent / 100))
''',
    "quotes/orders/quote.py": QUOTE,
    "quotes/api/__init__.py": "",
    "quotes/api/validation.py": '''\
"""Checks on what the API is sent."""

import re


class ValidationError(ValueError):
    pass


def check_country(country: str) -> None:
    if not re.fullmatch(r"[A-Z]{2}", country or ""):
        raise ValidationError(f"not a country code: {country!r}")
''',
    "quotes/api/serializers.py": '''\
"""Quotes as JSON."""

from quotes.shipping.packaging import parcel_weight  # noqa: F401  (kept for older callers)
from quotes.util.money import euros


def quote_json(quote) -> dict:
    return {
        "items": euros(quote.items),
        "shipping": euros(quote.shipping),
        "discount": euros(quote.discount),
        "total": euros(quote.total),
        "weight_kg": quote.weight,
    }
''',
    "quotes/api/handlers.py": HANDLERS,
    "quotes/storage/__init__.py": "",
    "quotes/storage/memory.py": '''\
"""Saved quotes, in memory (a database in production)."""

import itertools

_quotes = {}
_ids = itertools.count(1)


def save(quote) -> int:
    quote_id = next(_ids)
    _quotes[quote_id] = quote
    return quote_id


def load(quote_id: int):
    return _quotes[quote_id]
''',
    "quotes/notify/__init__.py": "",
    "quotes/notify/email.py": '''\
"""The email with a quote."""

from quotes.shipping.carriers import carrier
from quotes.shipping.delivery import delivery_days
from quotes.util.money import euros


def quote_email(quote, customer) -> str:
    country = customer.address.country
    return (
        f"Your quote: {euros(quote.total)} in all, shipping {euros(quote.shipping)} with "
        f"{carrier(country)} in {delivery_days(country)}."
    )
''',
    "tests/__init__.py": "",
    "tests/helpers.py": '''\
from quotes.customers.accounts import Customer
from quotes.customers.addresses import Address
from quotes.orders.cart import Cart
from quotes.shipping.rates import rate
from quotes.tax.vat import vat_rate


def customer(country="DE", orders=0):
    return Customer("c1", "ada@example.com", Address("Ada", "Main St 1", "Town", "12345", country), orders)


def cart(*skus):
    c = Cart()
    for sku in skus:
        c.add(sku)
    return c


def fresh():
    """Forget cached lookups, so each test starts clean."""
    rate.cache_clear()
    vat_rate.cache_clear()
''',
    "tests/test_cache.py": """\
import unittest

from quotes.util.cache import memoize


class MemoizeTest(unittest.TestCase):
    def test_each_lookup_is_made_once(self):
        calls = []

        @memoize
        def square(n):
            calls.append(n)
            return n * n

        self.assertEqual([square(3), square(3), square(4)], [9, 9, 16])
        self.assertEqual(calls, [3, 4])

    def test_cache_clear(self):
        calls = []

        @memoize
        def double(n):
            calls.append(n)
            return 2 * n

        double(1)
        double.cache_clear()
        double(1)
        self.assertEqual(calls, [1, 1])
""",
    "tests/test_rates.py": """\
import unittest

from quotes.shipping.rates import TooHeavy, rate
from tests.helpers import fresh


class RatesTest(unittest.TestCase):
    def setUp(self):
        fresh()

    def test_domestic(self):
        self.assertEqual(rate("DE", 1.5), 490)
        self.assertEqual(rate("DE", 12), 1290)

    def test_eu(self):
        self.assertEqual(rate("FR", 0.5), 990)

    def test_world(self):
        self.assertEqual(rate("US", weight=9), 3990)

    def test_too_heavy(self):
        with self.assertRaises(TooHeavy):
            rate("DE", 40)
""",
    "tests/test_vat.py": """\
import unittest

from quotes.tax.exemptions import reverse_charge
from quotes.tax.vat import vat_rate
from tests.helpers import fresh


class VatTest(unittest.TestCase):
    def setUp(self):
        fresh()

    def test_standard_and_reduced(self):
        self.assertEqual(vat_rate("DE", "kitchen"), 19)
        self.assertEqual(vat_rate("DE", "books"), 7)
        self.assertEqual(vat_rate("FR", "food"), 5.5)

    def test_outside_the_eu(self):
        self.assertEqual(vat_rate("NO", "books"), 0)

    def test_reverse_charge(self):
        self.assertTrue(reverse_charge("FR12345678901", "FR"))
        self.assertFalse(reverse_charge("DE123456789", "DE"))
        self.assertFalse(reverse_charge(None, "FR"))
""",
    "tests/test_quote.py": """\
import unittest

from quotes.orders.quote import build_quote
from tests.helpers import cart, customer, fresh


class QuoteTest(unittest.TestCase):
    def setUp(self):
        fresh()

    def test_domestic_books(self):
        quote = build_quote(cart("BK-1", "BK-2"), customer("DE"))
        self.assertEqual((quote.items, quote.shipping, quote.discount), (6741, 790, 0))
        self.assertEqual(quote.weight, 2.9)

    def test_loyal_customer_in_france(self):
        quote = build_quote(cart("TL-1"), customer("FR", orders=12))
        self.assertEqual((quote.items, quote.shipping, quote.discount), (5400, 1590, 270))

    def test_heavy_parcel_to_norway(self):
        quote = build_quote(cart("GD-1"), customer("NO"))
        self.assertEqual((quote.items, quote.shipping, quote.total), (18900, 6990, 25890))
""",
    "tests/test_cart.py": """\
import unittest

from quotes.orders.cart import Cart
from quotes.shipping.packaging import parcel_weight


class CartTest(unittest.TestCase):
    def test_adding_twice_adds_up(self):
        cart = Cart()
        cart.add("BK-1")
        cart.add("BK-1", 2)
        self.assertEqual([(line.product.sku, line.quantity) for line in cart.lines], [("BK-1", 3)])

    def test_remove(self):
        cart = Cart()
        cart.add("BK-1")
        cart.add("EL-1")
        cart.remove("BK-1")
        self.assertEqual([line.product.sku for line in cart.lines], ["EL-1"])

    def test_parcel_weight_includes_the_box(self):
        cart = Cart()
        cart.add("EL-1", 2)
        self.assertEqual(parcel_weight(cart), 0.55)
""",
    "tests/test_api.py": """\
import unittest

from quotes.api.handlers import quote, vat_info
from tests.helpers import cart, customer, fresh


class ApiTest(unittest.TestCase):
    def setUp(self):
        fresh()

    def test_quote(self):
        self.assertEqual(
            quote(cart("EL-1"), customer("AT")),
            {"items": "34.80 €", "shipping": "9.90 €", "discount": "0.00 €", "total": "44.70 €", "weight_kg": 0.35},
        )

    def test_vat_info(self):
        self.assertEqual(vat_info("IT", "books"), {"country": "IT", "category": "books", "vat": 4})

    def test_bad_country(self):
        self.assertIn("error", vat_info("Germany", "books"))
""",
    "tests/test_misc.py": """\
import datetime as dt
import unittest

from quotes.catalog.bundles import bundle
from quotes.catalog.stock import in_stock
from quotes.notify.email import quote_email
from quotes.orders.quote import build_quote
from quotes.util.dates import valid_until
from quotes.util.money import euros
from quotes.util.units import kg_from_pounds
from tests.helpers import cart, customer, fresh


class MiscTest(unittest.TestCase):
    def setUp(self):
        fresh()

    def test_money_and_units(self):
        self.assertEqual(euros(1290), "12.90 €")
        self.assertEqual(kg_from_pounds(10), 4.536)

    def test_dates_and_stock(self):
        self.assertEqual(valid_until(dt.date(2026, 10, 30)), dt.date(2026, 11, 13))
        self.assertFalse(in_stock("TL-1"))
        self.assertEqual([p.sku for p in bundle("camp-kitchen")], ["TL-1", "TL-2", "FD-1"])

    def test_email(self):
        quote = build_quote(cart("FD-1"), customer("NL"))
        self.assertEqual(
            quote_email(quote, customer("NL")),
            "Your quote: 29.52 € in all, shipping 9.90 € with DPD in 2-5 working days.",
        )
""",
}

PROMPT = (
    "Support says shipping costs on quotes are sometimes wrong. A customer in Norway asked for a quote for a "
    "headlamp, then for a garden bench, and both quotes had the headlamp's shipping cost; after the service "
    "restarted, the bench's quote was right. Find the cause and fix it. The rate lookups are slow, so they "
    "should stay cached, and the tests must keep passing."
)

HIDDEN_TEST = """\
import unittest

from quotes.api.handlers import vat_info
from quotes.orders.quote import build_quote
from quotes.util.cache import memoize
from tests.helpers import cart, customer, fresh


class HiddenTest(unittest.TestCase):
    def setUp(self):
        fresh()

    def test_shipping_follows_each_quote(self):
        norway = customer("NO")
        self.assertEqual(build_quote(cart("EL-1"), norway).shipping, 1990)
        self.assertEqual(build_quote(cart("GD-1"), norway).shipping, 6990)
        self.assertEqual(build_quote(cart("TL-2", "TL-2"), norway).shipping, 3990)
        self.assertEqual(build_quote(cart("EL-1"), norway).shipping, 1990)

    def test_vat_follows_each_lookup(self):
        self.assertEqual(vat_info("DE", "books")["vat"], 7)
        self.assertEqual(vat_info("DE", "electronics")["vat"], 19)
        self.assertEqual(vat_info("DE", "food")["vat"], 7)

    def test_lookups_stay_cached(self):
        calls = []

        @memoize
        def lookup(country, weight=0, express=False):
            calls.append((country, weight, express))
            return (country, weight, express)

        for _ in range(2):
            self.assertEqual(lookup("NO", weight=2), ("NO", 2, False))
            self.assertEqual(lookup("NO", weight=30), ("NO", 30, False))
            self.assertEqual(lookup("NO", weight=30, express=True), ("NO", 30, True))
        self.assertEqual(calls, [("NO", 2, False), ("NO", 30, False), ("NO", 30, True)])


if __name__ == "__main__":
    unittest.main()
"""

PROBLEMS = {
    "test_shipping_follows_each_quote": "a second quote still gets the first one's shipping cost",
    "test_vat_follows_each_lookup": "the same cause still gives wrong VAT rates (api.handlers.vat_info)",
    "test_lookups_stay_cached": "lookups aren't cached any more, or the cache mixes up keyword arguments",
}


def check(folder: Path, answer: str):
    import re

    from lcode.bench import Check, last_line, run_python

    if not (folder / "quotes" / "orders" / "quote.py").is_file():
        return Check(False, "quotes/orders/quote.py is missing")
    for name, content in FILES.items():  # the tests as they were
        if name.startswith("tests/"):
            (folder / name).write_text(content)
    r = run_python(folder, "-m", "unittest", "-q")
    if r.returncode != 0:
        return Check(False, f"the tests fail: {last_line(r.stderr) or last_line(r.stdout)}")
    (folder / "tests" / "test_hidden_quotes.py").write_text(HIDDEN_TEST)
    r = run_python(folder, "-m", "unittest", "-q", "tests.test_hidden_quotes")
    if r.returncode == 0:
        return Check(True, "quotes, VAT and the cache are right, and the tests pass")
    failed = [text for name, text in PROBLEMS.items() if re.search(rf"\b{name}\b", r.stderr)]
    return Check(False, "; ".join(failed) or last_line(r.stderr) or last_line(r.stdout))
