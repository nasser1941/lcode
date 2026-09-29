from pathlib import Path

import pytest

from lcode.tools import fuzzy_replace, truncate


def test_read_file_numbers_lines_and_pages(agent):
    out = agent.tools.run("read_file", {"path": "src/pkg/math.py", "limit": 2})
    assert out.startswith("     1\tdef add(a, b):")
    assert "Showing lines 1-2 of 6" in out
    out = agent.tools.run("read_file", {"path": "src/pkg/math.py", "offset": 5})
    assert "     5\tdef sub(a, b):" in out


def test_read_file_errors(agent, repo: Path):
    assert agent.tools.run("read_file", {"path": "missing.py"}).startswith("Error: missing.py does not exist")
    assert "is a directory" in agent.tools.run("read_file", {"path": "src"})
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    assert "binary file" in agent.tools.run("read_file", {"path": "blob.bin"})


def test_edit_requires_reading_first(agent):
    out = agent.tools.run("edit_file", {"path": "src/pkg/math.py", "old_string": "a - b", "new_string": "b - a"})
    assert out == "Error: You must read_file src/pkg/math.py before modifying it."


def test_edit_exact_and_ambiguous(agent, repo: Path):
    agent.tools.run("read_file", {"path": "src/pkg/math.py"})
    out = agent.tools.run("edit_file", {"path": "src/pkg/math.py", "old_string": "a - b", "new_string": "b - a"})
    assert out.startswith("Edited src/pkg/math.py (1 replacement(s))")
    assert "return b - a" in (repo / "src/pkg/math.py").read_text()
    out = agent.tools.run("edit_file", {"path": "src/pkg/math.py", "old_string": "(a, b)", "new_string": "(x, y)"})
    assert "occurs 2 times" in out
    out = agent.tools.run(
        "edit_file", {"path": "src/pkg/math.py", "old_string": "(a, b)", "new_string": "(x, y)", "replace_all": True}
    )
    assert "2 replacement(s)" in out


def test_edit_detects_external_changes(agent, repo: Path):
    agent.tools.run("read_file", {"path": "src/pkg/math.py"})
    target = repo / "src/pkg/math.py"
    target.write_text(target.read_text() + "# changed\n")
    import os

    os.utime(target, (target.stat().st_atime, target.stat().st_mtime + 5))
    out = agent.tools.run("edit_file", {"path": "src/pkg/math.py", "old_string": "a - b", "new_string": "b - a"})
    assert "changed on disk" in out


def test_fuzzy_replace_ignores_trailing_whitespace():
    text = "class A:\n    def f(self):   \n        return 1\n"
    assert fuzzy_replace(text, "    def f(self):\n        return 1", "    def f(self):\n        return 2") == (
        "class A:\n    def f(self):\n        return 2\n"
    )
    assert fuzzy_replace(text, "not there", "x") is None


def test_write_file_create_and_overwrite(agent, repo: Path):
    assert agent.tools.run("write_file", {"path": "scripts/new.py", "content": "print(1)\n"}).startswith("Created")
    assert (repo / "scripts/new.py").read_text() == "print(1)\n"
    # Overwriting a file the model never read is refused.
    assert "must read_file" in agent.tools.run("write_file", {"path": "README.md", "content": "x"})


def test_glob_skips_ignored_dirs(agent):
    out = agent.tools.run("glob", {"pattern": "**/*.py"})
    assert out.splitlines() == ["src/pkg/math.py"]
    assert agent.tools.run("glob", {"pattern": "*.nothing"}) == "No files found."


@pytest.mark.parametrize("ripgrep", [True, False])
def test_grep(agent, monkeypatch, ripgrep):
    if not ripgrep:
        monkeypatch.setattr("lcode.tools.shutil.which", lambda name: None)
    elif not __import__("shutil").which("rg"):
        pytest.skip("ripgrep not installed")
    out = agent.tools.run("grep", {"pattern": r"def add", "glob": "*.py"})
    assert out.strip() == "src/pkg/math.py:1:def add(a, b):"
    assert agent.tools.run("grep", {"pattern": "zzz_nothing"}) == "No matches."


def test_list_dir(agent):
    out = agent.tools.run("list_dir", {"depth": 3})
    assert "math.py" in out
    assert "node_modules" not in out


def test_bash_output_exit_code_and_cwd(agent, repo: Path):
    out = agent.tools.run("bash", {"command": "echo hello && exit 3"})
    assert "hello" in out and "[exit code: 3]" in out
    out = agent.tools.run("bash", {"command": "cd src && pwd"})
    assert "Working directory is now" in out
    assert agent.cwd == (repo / "src").resolve()


def test_bash_timeout_keeps_cwd(agent, repo: Path):
    out = agent.tools.run("bash", {"command": "sleep 5", "timeout": 1})
    assert "timed out" in out
    assert agent.cwd == repo


def test_bash_denied(make_agent, monkeypatch):
    agent = make_agent(mode="ask")
    monkeypatch.setattr("builtins.input", lambda _: "n use pathlib instead")
    out = agent.tools.run("bash", {"command": "rm -rf build"})
    assert out == "The user denied this action. User says: use pathlib instead"


def test_todo_write_accepts_json_string(agent):
    assert agent.tools.run("todo_write", {"todos": '[{"content": "a", "status": "pending"}]'}) == "Todo list updated."
    assert agent.tools.todos == [{"content": "a", "status": "pending"}]


def test_unknown_tool_and_bad_args(agent):
    assert agent.tools.run("nope", {}).startswith("Error: unknown tool 'nope'")
    out = agent.tools.run("read_file", {"path": "README.md", "line_start": 3})
    assert out == (
        "Error: bad arguments for read_file: unknown argument(s) 'line_start'. "
        "Valid arguments: path, offset (optional), limit (optional)."
    )
    assert "missing required argument(s) 'path'" in agent.tools.run("read_file", {})


def test_truncate_keeps_head_and_tail():
    text = "a" * 50 + "b" * 50
    out = truncate(text, limit=30)
    assert out.startswith("a" * 20) and out.endswith("b" * 10) and "truncated" in out


def test_edits_in_ask_mode(make_agent, repo: Path, monkeypatch):
    """Regression: file edits used to crash with IndexError whenever lcode asked for permission."""
    agent = make_agent(mode="ask")
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert agent.tools.run("write_file", {"path": "NOTES.md", "content": "# Notes\n"}).startswith("Created")
    agent.tools.run("read_file", {"path": "src/pkg/math.py"})
    out = agent.tools.run("edit_file", {"path": "src/pkg/math.py", "old_string": "a - b", "new_string": "b - a"})
    assert out.startswith("Edited src/pkg/math.py")
    assert "return b - a" in (repo / "src/pkg/math.py").read_text()
