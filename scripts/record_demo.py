"""Record docs/assets/demo.svg from a real lcode session.

    uv run python scripts/record_demo.py [--model MODEL] [--context 64k]

Needs a running Ollama with the model installed. The session runs in a throwaway project under
/tmp and approves every permission prompt automatically (the prompts are still recorded).
"""

from __future__ import annotations

import argparse
import builtins
import shutil
import tempfile
from pathlib import Path

from rich.console import Console
from rich.text import Text

from lcode import config
from lcode.agent import Agent, Settings
from lcode.cli import choose_context, resolve_model
from lcode.config import parse_context
from lcode.hardware import detect
from lcode.ollama import Ollama

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

TESTS = """\
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

AGENTS = "# Shop\n\nRun the tests with: python3 -m unittest -v\n"

PROMPT = "The tests in test_cart.py fail. Find the bug, fix it, and run the tests."


class NoSpinner:
    """Spinners are transient on a real terminal but would be recorded frame by frame."""

    def start(self): ...
    def stop(self): ...
    def update(self, *args, **kwargs): ...
    def __enter__(self):
        return self

    def __exit__(self, *exc): ...


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=config.load()["model"])
    parser.add_argument("--context", default="64k")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "docs/assets/demo.svg"))
    args = parser.parse_args()

    cfg = config.load()
    ollama = Ollama(cfg["ollama_host"])
    model, spec = resolve_model(ollama, args.model)
    context, _ = choose_context(ollama, model, spec, parse_context(args.context), detect())

    project = (Path("/tmp") if Path("/tmp").is_dir() else Path(tempfile.gettempdir())) / "lcode-demo" / "shop"
    shutil.rmtree(project.parent, ignore_errors=True)
    project.mkdir(parents=True)
    (project / "cart.py").write_text(CART)
    (project / "test_cart.py").write_text(TESTS)
    (project / "AGENTS.md").write_text(AGENTS)

    console = Console(record=True, width=92, force_terminal=True, color_system="truecolor", highlight=False)

    def approve(prompt: str) -> str:
        console.print(Text(prompt, style="default") + Text("y", style="bold green"))
        return "y"

    builtins.input = approve
    console.status = lambda *args, **kwargs: NoSpinner()
    agent = Agent(ollama, Settings(model=model, context=context, permission_mode="ask"), project, console=console)
    console.print(Text("❯ ", style="bold cyan") + Text(PROMPT))
    agent.run_turn(PROMPT)
    console.save_svg(args.out, title="lcode")
    shutil.rmtree(project.parent)
    print(f"Saved {args.out}")


if __name__ == "__main__":
    main()
