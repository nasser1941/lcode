"""Forgiving edits (#112): close matches applied safely, and apply_patch."""

import pytest

from lcode.matching import Found, Miss, locate, parse_patch

SOURCE = """import os


class Config:
    def __init__(self, path):
        self.path = path
        self.values = {}

    def load(self):
        with open(self.path) as f:
            for line in f:
                key, value = line.split("=", 1)
                self.values[key.strip()] = value.strip()
        return self.values

    def get(self, key, default=None):
        return self.values.get(key, default)
"""
TABS = SOURCE.replace("    ", "\t")
GET = "    def get(self, key, default=None):\n        return self.values.get(key, default)"
GET_LOWER = "    def get(self, key, default=None):\n        return self.values.get(key.lower(), default)"


@pytest.mark.parametrize(
    ("text", "old", "new", "expected", "how"),
    [
        (
            SOURCE,
            "    16\t" + GET.replace("\n", "\n    17\t"),
            GET_LOWER,
            SOURCE.replace(GET, GET_LOWER),
            "line numbers",
        ),
        (TABS, GET, GET_LOWER, TABS.replace(GET.replace("    ", "\t"), GET_LOWER.replace("    ", "\t")), "indentation"),
        (
            SOURCE,
            "def get(self, key, default=None):\n    return self.values.get(key, default)",
            "def get(self, key, default=None):\n    value = self.values.get(key, default)\n    return value",
            SOURCE.replace(
                GET,
                "    def get(self, key, default=None):\n        value = self.values.get(key, default)\n        return value",
            ),
            "indentation",
        ),
        (
            SOURCE,
            '                key, value = line.split(\\"=\\", 1)',
            '                key, value = line.split(\\"=\\", 1)  # KEY=VALUE',
            SOURCE.replace('line.split("=", 1)', 'line.split("=", 1)  # KEY=VALUE'),
            "escapes",
        ),
        (
            SOURCE,
            "        return self.values\n    def get(self, key, default=None):",
            "        return dict(self.values)\n\n    def get(self, key, default=None):",
            SOURCE.replace(
                "        return self.values\n\n    def get", "        return dict(self.values)\n\n    def get"
            ),
            "blank lines",
        ),
        (
            SOURCE,
            "                self.value[key.strip()] = value.strip()",
            "                self.values[key.strip().lower()] = value.strip()",
            SOURCE.replace("self.values[key.strip()]", "self.values[key.strip().lower()]"),
            "close match",
        ),
    ],
)
def test_imperfect_copies_land_in_the_right_place(text, old, new, expected, how):
    found = locate(text, old, new)
    assert isinstance(found, Found) and found.text == expected and how in found.how


def test_weak_or_ambiguous_matches_are_refused_with_the_closest_lines():
    twins = "def get_user(db, id):\n    return db.find_one({'id': id})\n\n\ndef get_users(db, ids):\n    return db.find_many({'id': ids})\n"
    miss = locate(twins, "def get_usr(db, id):\n    return db.find({'id': id})", "x")
    assert isinstance(miss, Miss) and "     1\tdef get_user(db, id):" in miss.hint
    assert isinstance(locate("x = 1\ny = 2\n", "q = 9", "q = 10"), Miss)
    two = "a = 1\n    b = 2\nc = 3\n    b = 2\n"
    assert locate(two, "b = 2", "b = 3").reason == "ambiguous"  # same text, two indentations


def test_a_stale_copy_doesnt_undo_an_earlier_edit():
    block = "def total(items):\n    result = 0\n    for item in items:\n        result += item.price * item.quantity\n    return round(result, 2)\n"
    edited = block.replace("item.quantity", "item.quantity * (1 - item.discount)")
    stale = block.strip("\n")
    found = locate(edited, stale, stale.replace("return round(result, 2)", "return round(result, 2) if items else 0"))
    assert isinstance(found, Found) and "item.discount" in found.text and "if items else 0" in found.text
    clash = locate(edited, stale, stale.replace("item.price * item.quantity", "item.price * item.qty"))
    assert isinstance(clash, Miss)  # it would change the line the earlier edit changed


def test_edit_file_says_how_it_matched_and_where_to_look(make_agent, repo):
    (repo / "config.py").write_text(SOURCE)
    agent = make_agent()
    agent.tools.run("read_file", {"path": "config.py"})
    result = agent.tools.run(
        "edit_file", {"path": "config.py", "old_string": GET.replace("    ", "  "), "new_string": GET_LOWER}
    )
    assert result.startswith("Edited config.py (1 replacement(s), ignoring indentation). Result:")
    assert "key.lower()" in (repo / "config.py").read_text()
    miss = agent.tools.run(
        "edit_file", {"path": "config.py", "old_string": "def get(self, k):\n    return None", "new_string": "x"}
    )
    assert "The closest text is:\n    16\t    def get(self, key, default=None):\n    17\t        return" in miss


PATCH = """--- a/calc.py
+++ b/calc.py
@@ -40,2 +40,2 @@
 def add(a, b):
-    return a + b
+    return b + a
--- /dev/null
+++ b/mul.py
@@ -0,0 +1,2 @@
+def mul(a, b):
+    return a * b
"""


def test_a_patch_with_wrong_line_numbers_still_applies(make_agent, repo):
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n\n    return a - b\n")
    agent = make_agent()
    agent.tools.run("read_file", {"path": "calc.py"})
    result = agent.tools.run("apply_patch", {"patch": PATCH})
    assert result == "Patched calc.py, mul.py."
    assert (
        "return b + a" in (repo / "calc.py").read_text()
        and (repo / "mul.py").read_text() == "def mul(a, b):\n    return a * b\n"
    )


def test_a_patch_that_doesnt_fit_changes_nothing(make_agent, repo):
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    agent = make_agent()
    agent.tools.run("read_file", {"path": "calc.py"})
    bad = PATCH.replace(" def add(a, b):\n-    return a + b", " def multiply(a, b):\n-    return a * b")
    result = agent.tools.run("apply_patch", {"patch": bad})
    assert result.startswith("Error: calc.py: hunk 1 (@@ -40,2 +40,2 @@) doesn't match the file.")
    assert (repo / "calc.py").read_text() == "def add(a, b):\n    return a + b\n" and not (repo / "mul.py").exists()
    assert agent.tools.run("apply_patch", {"patch": "just text"}).startswith("Error: no ---/+++ file headers")


def test_blank_context_lines_without_their_space_are_read():
    files = parse_patch("--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a = 1\n\n-b = 2\n+b = 3\n")
    assert files[0].hunks[0].old == "a = 1\n\nb = 2" and files[0].hunks[0].new == "a = 1\n\nb = 3"


def test_common_tool_slips_get_a_useful_answer(make_agent, repo):
    agent = make_agent()
    run = agent.tools.run
    found = run("grep", {"pattern": "def add("})
    assert found.startswith("[The pattern isn't a valid regular expression, so it was searched as plain text.]")
    assert "src/pkg/math.py:1:def add(a, b):" in found
    assert run("read_file", {"path": "math.py"}) == "Error: math.py does not exist. Did you mean: src/pkg/math.py?"
    assert run("list_dir", {"path": "src/pkgs"}) == "Error: src/pkgs does not exist. Did you mean: src/pkg/?"
    assert run("list_dir", {"path": "src/pkg/math.py"}).startswith("src/pkg/math.py is a file, not a directory")
    assert "math.py" in run("read_file", {"path": "src/pkg"})  # a directory: here's what's in it
    assert run("glob", {"pattern": "*.py", "path": "nowhere"}).startswith("No files found: nowhere does not exist.")
