import os
import subprocess
import time
from pathlib import Path

import pytest

from conftest import call, output, reply
from lcode import checkpoints, sessions
from lcode.hardware import Hardware
from lcode.repl import handle_command

LAPTOP = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)
ORIGINAL_MATH = "def add(a, b):\n    return a + b   \n\n\ndef sub(a, b):\n    return a - b\n"


def answers(monkeypatch, *replies):
    queue = list(replies)
    monkeypatch.setattr("builtins.input", lambda _: queue.pop(0))


def refactor_turn() -> list[list[dict]]:
    """One request that edits a file, creates one and deletes one with a shell command."""
    return [
        reply(tool_calls=[call("read_file", path="src/pkg/math.py")]),
        reply(tool_calls=[call("edit_file", path="src/pkg/math.py", old_string="a + b", new_string="b + a")]),
        reply(tool_calls=[call("write_file", path="src/pkg/extra/new.py", content="x = 1\n")]),
        reply(tool_calls=[call("bash", command="rm README.md")]),
        reply("Done."),
    ]


def write_turn(path: str, content: str = "x\n") -> list[list[dict]]:
    return [reply(tool_calls=[call("write_file", path=path, content=content)]), reply("Written.")]


def git(repo: Path, *args: str) -> str:
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
    return subprocess.run(["git", *identity, *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def test_undo_restores_edits_new_files_and_shell_deletions(make_agent, repo):
    agent = make_agent(refactor_turn())
    agent.run_turn("tidy up the math module")
    assert not (repo / "README.md").exists()
    assert [c["path"] for c in agent.checkpoints.items[0].changes] == [
        "README.md",
        "src/pkg/extra/new.py",
        "src/pkg/math.py",
    ]
    assert "Checkpoint 1: 3 files changed" in output(agent)

    handle_command(agent, "/undo", LAPTOP)
    assert (repo / "src/pkg/math.py").read_text() == ORIGINAL_MATH
    assert (repo / "README.md").read_text() == "# Demo\n"
    assert not (repo / "src/pkg/extra").exists()  # the new file and its new folder are gone
    assert agent.checkpoints.items[0].undone
    note = agent.messages[-1]["content"]
    assert note.startswith('[lcode] The user undid the file changes from their request "tidy up the math module"')
    assert "restored README.md, src/pkg/math.py; removed src/pkg/extra/new.py" in note
    assert str(repo / "src/pkg/math.py") not in agent.tools.read_mtimes  # must be read again before editing


def test_git_repository_is_left_alone(make_agent, repo):
    (repo / ".gitignore").write_text("node_modules/\n*.log\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    (repo / "README.md").write_text("# Demo\nwork in progress\n")
    git(repo, "stash", "-q")
    (repo / "notes.txt").write_text("untracked, not ignored\n")
    (repo / "debug.log").write_text("ignored\n")
    git(repo, "add", "notes.txt")  # something staged

    def state():
        index = (repo / ".git/index").read_bytes()
        return git(repo, "rev-parse", "HEAD"), git(repo, "for-each-ref"), git(repo, "stash", "list"), index

    before = state()
    agent = make_agent(refactor_turn())
    agent.run_turn("tidy up")
    assert state() == before
    handle_command(agent, "/undo", LAPTOP)
    assert state() == before
    assert git(repo, "status", "--porcelain") == "A  notes.txt\n"
    assert (repo / "debug.log").read_text() == "ignored\n"
    assert "refs/lcode" not in git(repo, "for-each-ref")


def test_undo_steps_back_one_request_at_a_time(make_agent, repo):
    agent = make_agent([*write_turn("one.txt"), reply("Just answering."), *write_turn("two.txt")])
    agent.run_turn("write one")
    agent.run_turn("explain something")  # no changes, no checkpoint
    agent.run_turn("write two")
    assert [cp.n for cp in agent.checkpoints.items] == [1, 2]

    handle_command(agent, "/undo", LAPTOP)
    assert (repo / "one.txt").exists() and not (repo / "two.txt").exists()
    handle_command(agent, "/undo", LAPTOP)
    assert not (repo / "one.txt").exists()
    handle_command(agent, "/undo", LAPTOP)
    assert "Nothing to undo" in output(agent)


def test_commands_that_change_nothing_leave_no_checkpoint(make_agent, repo):
    agent = make_agent([reply(tool_calls=[call("bash", command="echo hi > /dev/null")]), reply("ok")])
    agent.run_turn("run something")
    assert agent.checkpoints.items == []
    assert "refs/lcode" not in "".join(p.name for p in checkpoints.stores_dir().rglob("*"))


def test_later_edits_are_only_lost_after_confirming(make_agent, repo, monkeypatch):
    agent = make_agent([reply(tool_calls=[call("read_file", path="README.md")]), *write_turn("README.md", "# New\n")])
    agent.run_turn("rewrite the readme")
    (repo / "README.md").write_text("# Edited by me afterwards\n")

    answers(monkeypatch, "n")
    handle_command(agent, "/undo", LAPTOP)
    assert "going back loses those later edits" in output(agent)
    assert (repo / "README.md").read_text() == "# Edited by me afterwards\n"
    assert not agent.checkpoints.items[0].undone

    answers(monkeypatch, "y")
    handle_command(agent, "/undo", LAPTOP)
    assert (repo / "README.md").read_text() == "# Demo\n"


def test_rewind_restores_files_and_conversation(make_agent, repo, monkeypatch):
    agent = make_agent([*write_turn("one.txt"), *write_turn("two.txt"), *write_turn("three.txt")])
    for n in ("one", "two", "three"):
        agent.run_turn(f"write {n}")

    answers(monkeypatch, "y", "y")  # restore the files; also rewind the conversation
    handle_command(agent, "/rewind 2", LAPTOP)
    assert (repo / "one.txt").exists()
    assert not (repo / "two.txt").exists() and not (repo / "three.txt").exists()
    assert [m["content"] for m in agent.messages if m["role"] == "user"] == ["write one"]
    assert [cp.undone for cp in agent.checkpoints.items] == [False, True, True]
    assert "Going back to before request 2 and 1 later request: write two" in output(agent)


def test_rewind_can_keep_the_conversation(make_agent, repo, monkeypatch):
    agent = make_agent([*write_turn("one.txt"), *write_turn("two.txt")])
    agent.run_turn("write one")
    agent.run_turn("write two")
    answers(monkeypatch, "1", "y", "n")  # pick from the list, restore, keep the conversation
    handle_command(agent, "/rewind", LAPTOP)
    assert not (repo / "one.txt").exists() and not (repo / "two.txt").exists()
    assert agent.messages[-1]["content"].startswith("[lcode] The user rolled the files back to before their request")


def test_checkpoints_survive_resume(make_agent, repo):
    first = make_agent(write_turn("one.txt"))
    first.run_turn("write one")
    first.save()
    second = make_agent()
    second.load(sessions.list_sessions(repo)[0])
    handle_command(second, "/checkpoints", LAPTOP)
    assert "write one" in output(second) and "one.txt" in output(second)
    handle_command(second, "/undo", LAPTOP)
    assert not (repo / "one.txt").exists()


def test_checkpoints_can_be_turned_off(make_agent, repo):
    agent = make_agent(write_turn("one.txt"), checkpoints=False)
    agent.run_turn("write one")
    assert agent.checkpoints.items == []
    handle_command(agent, "/undo", LAPTOP)
    assert "Checkpoints are off" in output(agent)
    assert (repo / "one.txt").exists()


def test_large_files_are_left_out(make_agent, repo, monkeypatch):
    monkeypatch.setattr(checkpoints, "MAX_FILE_BYTES", 1000)
    (repo / "data.bin").write_text("x" * 5000)
    agent = make_agent(
        [reply(tool_calls=[call("bash", command="echo more >> data.bin; touch small.txt")]), reply("ok")]
    )
    agent.run_turn("append")
    assert [c["path"] for c in agent.checkpoints.items[0].changes] == ["small.txt"]


def test_folders_that_are_too_big_turn_checkpoints_off(make_agent, repo, monkeypatch):
    monkeypatch.setattr(checkpoints, "MAX_FILES", 1)
    agent = make_agent([*write_turn("one.txt"), *write_turn("two.txt")])
    agent.run_turn("write one")
    agent.run_turn("write two")
    assert (repo / "one.txt").exists() and (repo / "two.txt").exists()  # the work still happens
    assert agent.checkpoints.items == []
    assert output(agent).count("Checkpoints are off") == 1  # said once, not every request
    assert "more than 1 files" in output(agent)


def test_home_folder_is_not_checkpointed(make_agent, repo, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: repo)
    agent = make_agent(write_turn("one.txt"))
    agent.run_turn("write one")
    assert agent.checkpoints.items == []
    assert "home folder" in output(agent)


def test_old_checkpoints_are_deleted(make_agent, repo):
    agent = make_agent(write_turn("one.txt"))
    agent.run_turn("write one")
    store = checkpoints.Store(repo)
    commit = agent.checkpoints.items[0].after
    store.set_ref("refs/lcode/20200101-000000-abcdef/1", commit)
    store.prune()
    refs = store.git("for-each-ref", "--format=%(refname)").split()
    assert refs == [agent.checkpoints.ref(1)]

    stale = checkpoints.stores_dir() / "0123456789abcdef.git"
    stale.mkdir()
    (stale / "lcode-last-used").touch()
    old = time.time() - 30 * 86400
    os.utime(stale / "lcode-last-used", (old, old))
    checkpoints.prune_stores()
    assert not stale.exists() and store.git_dir.exists()


@pytest.mark.parametrize(
    ("path", "pattern"),
    [("a.bin", "/a.bin"), ("data/[x]*.csv", "/data/\\[x]\\*.csv"), ("trailing ", "/trailing\\ ")],
)
def test_exclude_patterns_match_one_path(path, pattern):
    assert checkpoints._exclude_pattern(path) == pattern
