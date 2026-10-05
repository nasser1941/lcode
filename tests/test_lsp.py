import shutil
import sys
from pathlib import Path

import pytest

from conftest import reply
from lcode import lsp

FAKE = str(Path(__file__).parent / "fake_lsp_server.py")
CODE = "def add(a, b):\n    return a + b\n\n\ndef twice(x):\n    return add(x, x)\n"


def tool(tool_name, **arguments):
    return {"function": {"name": tool_name, "arguments": arguments}}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr("lcode.config.STATE_DIR", tmp_path / "state")
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "calc.py").write_text(CODE)
    (root / "src" / "main.py").write_text("from calc import add\n\nprint(add(1, 2))\n")
    return root


@pytest.fixture
def manager(project):
    m = lsp.Manager(project, commands={"python": [[sys.executable, FAKE]]})
    yield m
    m.close()


def test_navigation_through_the_language_server(manager, project):
    main, calc = project / "src/main.py", project / "src/calc.py"
    manager.query("symbols", calc, 0, "", "")  # opens calc.py so the fake server knows it
    assert manager.query("definition", main, 3, "add", "") == "src/calc.py:1: def add(a, b):"
    refs = manager.query("references", calc, 1, "add", "")
    assert "src/calc.py:1: def add(a, b):" in refs and "src/main.py:3: print(add(1, 2))" in refs
    assert manager.query("hover", calc, 5, "twice", "") == "```python\ntwice: int\n```"
    assert manager.query("symbols", calc, 0, "", "") == "1: function add\n5: function twice"  # locals left out
    assert manager.query("symbols", None, 0, "", "twi") == "twi (function) src/calc.py:5: def twice(x):"
    with pytest.raises(lsp.LspError, match="'nope' isn't on line 3"):
        manager.query("definition", main, 3, "nope", "")
    with pytest.raises(lsp.LspError, match=r"no language server handles \.md"):
        manager.query("definition", project / "README.md", 1, "", "")


def test_new_errors_after_an_edit(manager, project):
    calc = project / "src/calc.py"
    before = calc.read_text()
    after = before.replace("return a + b", "return a + b  # BROKEN")
    calc.write_text(after)
    note = manager.check_edit(calc, before, after)
    expected = "calc.py:2: broken thing: return a + b # BROKEN (fake)"
    assert note == f"\n[The language server found 1 new error after this change:\n  {expected}]"
    again = after + "\nx = 1\n"
    assert manager.check_edit(calc, after, again) == ""  # the error was already there
    worse = again + "y = BROKEN\n"
    assert "1 new error" in manager.check_edit(calc, again, worse)
    new_file = project / "src/new.py"
    new_file.write_text("BROKEN\nBROKEN\n")
    assert "2 new errors" in manager.check_edit(new_file, None, "BROKEN\nBROKEN\n")


def test_edits_report_new_errors_to_the_model(make_agent, repo, manager, monkeypatch):
    monkeypatch.setattr(manager, "root", repo)
    agent = make_agent(
        [
            reply(tool_calls=[tool("read_file", path="src/pkg/math.py")]),
            reply(
                tool_calls=[
                    tool("edit_file", path="src/pkg/math.py", old_string="return a - b", new_string="return BROKEN")
                ]
            ),
            reply(tool_calls=[tool("lsp", action="definition", path="src/pkg/math.py", line=1, symbol="add")]),
            reply("Done."),
        ]
    )
    agent.lsp = manager
    agent.messages[0]["content"] = agent.system_prompt()
    assert (
        "# Code intelligence" in agent.messages[0]["content"]
        and "the Python language server" in agent.messages[0]["content"]
    )
    agent.run_turn("change sub")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert (
        "[The language server found 1 new error after this change:\n  math.py:6: broken thing: return BROKEN (fake)]"
        in results[1]
    )
    assert results[2] == "src/pkg/math.py:1: def add(a, b):"
    assert "lsp" in {t["function"]["name"] for t in agent.ollama.payloads[0]["tools"]}


def test_without_a_language_server_nothing_changes(make_agent, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert lsp.available() == {}
    agent = make_agent()
    assert agent.lsp is None and "lsp" not in agent.tool_names()
    assert "# Code intelligence" not in agent.messages[0]["content"]
    assert agent.tools.run("lsp", {"action": "definition"}).startswith("Error:")


def test_lsp_json_picks_or_turns_off_servers(tmp_path, monkeypatch):
    monkeypatch.setattr("lcode.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}" if name in ("pylsp", "gopls") else None)
    assert lsp.available() == {"python": [["pylsp"]], "go": [["gopls"]]}
    (tmp_path / "lsp.json").write_text('{"go": {"disabled": true}, "python": {"command": ["pylsp", "--verbose"]}}')
    assert lsp.available() == {"python": [["pylsp", "--verbose"]]}


def test_a_projects_own_typescript_comes_first(tmp_path, monkeypatch):
    monkeypatch.setattr("lcode.config.CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(
        shutil, "which", lambda name: f"/usr/bin/{name}" if name == "typescript-language-server" else None
    )
    (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
    (tmp_path / "node_modules" / ".bin" / "tsc").write_text("")
    assert lsp.candidates("typescript", tmp_path) == [
        ["typescript-language-server", "--stdio"],
        [str(tmp_path / "node_modules/.bin/tsc"), "--lsp", "--stdio"],
    ]


def test_a_server_that_wont_start_is_reported(project):
    m = lsp.Manager(project, commands={"python": [["definitely-not-a-language-server-xyz"]]})
    with pytest.raises(lsp.LspError, match="the Python language server failed: can't start"):
        m.query("hover", project / "src/calc.py", 1, "add", "")
    assert m.languages() == [] and m.check_edit(project / "src/calc.py", "a", "b") == ""


def test_definitions_that_stop_at_an_import_are_recognized(tmp_path):
    (tmp_path / "main.ts").write_text('import { add } from "./calc";\nconst x = require("y");\nadd(1, 2);\n')
    location = {"uri": (tmp_path / "main.ts").as_uri()}
    at = lambda line: [{**location, "range": {"start": {"line": line, "character": 9}}}]  # noqa: E731
    assert lsp.import_target(at(0)) == (tmp_path / "main.ts", {"line": 0, "character": 9})
    assert lsp.import_target(at(1)) is not None
    assert lsp.import_target(at(2)) is None and lsp.import_target([]) is None
