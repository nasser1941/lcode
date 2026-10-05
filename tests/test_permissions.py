import io

import pytest
from rich.console import Console

from lcode.permissions import Permissions, bash_key, is_read_only


@pytest.mark.parametrize(
    ("command", "safe"),
    [
        ("ls -la", True),
        ("git status", True),
        ("cat a.py | grep def", True),
        ("git push", False),
        ("rm -rf build", False),
        ("ls > out.txt", False),
        ("ls; rm x", False),
        ("find . -delete", False),
        ("echo $(whoami)", False),
        ("python3 script.py", False),
        ("cd /repo/src && grep -n def x.py", True),
        ("cd 'my dir' && cd sub && git log -3", True),
        ("cd /repo && ./check.sh", False),
        ("cd /repo && ls && rm x", False),
        ("cd $(rm -rf ~) && ls", False),
        ("cd `whoami` && ls", False),
        ('cd "$HOME" && ls', False),
        ("cd /repo; rm x", False),
    ],
)
def test_is_read_only(command, safe):
    assert is_read_only(command) is safe


def test_bash_key():
    assert bash_key("pytest -q") == "bash:pytest"
    assert bash_key("git commit -m x") == "bash:git commit"
    assert bash_key("python3 x.py") == "bash:python3 x.py"
    assert bash_key("cd /tmp/app && python3 -m unittest -v") == "bash:python3 -m unittest"
    assert bash_key("cd a && cd b && make test") == "bash:make"


@pytest.mark.parametrize(
    ("answer", "allowed", "feedback"),
    [
        ("y", True, ""),
        ("", True, ""),
        ("n", False, "The user denied this action. Ask the user how to proceed or choose a different approach."),
        ("no, use git mv", False, "The user denied this action. User says: use git mv"),
        ("try the other file", False, "The user denied this action. User says: try the other file"),
    ],
)
def test_request_answers(monkeypatch, answer, allowed, feedback):
    perms = Permissions(Console(file=io.StringIO()), "ask")
    monkeypatch.setattr("builtins.input", lambda _: answer)
    assert perms.request("bash:rm", "bash", "Run", "rm -rf build") == (allowed, feedback)


def test_always_and_modes(monkeypatch):
    perms = Permissions(Console(file=io.StringIO()), "ask")
    monkeypatch.setattr("builtins.input", lambda _: "a")
    assert perms.request("bash:pytest", "bash", "Run", "pytest")[0]
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("should not ask again"))
    assert perms.request("bash:pytest", "bash", "Run", "pytest")[0]
    perms.mode = "auto-edit"
    assert perms.request("edit", "edit", "Edit", "diff")[0]
    perms.cycle()
    assert perms.mode == "yolo"


@pytest.mark.parametrize(("kind", "key"), [("edit", "edit"), ("bash", "bash:rm"), ("web", "web:search")])
def test_every_kind_can_ask_and_remember(monkeypatch, kind, key):
    perms = Permissions(Console(file=io.StringIO()), "ask")
    monkeypatch.setattr("builtins.input", lambda _: "a")
    assert perms.request(key, kind, "Title", "body") == (True, "")
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("should not ask again"))
    assert perms.request(key, kind, "Title", "body") == (True, "")
