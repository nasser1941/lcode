"""Saved conversations and notes across folders, checkouts and upgrades (#141)."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import output, reply
from lcode import cli, repoid, sessions
from lcode import memory as memory_notes
from lcode.hardware import Hardware
from lcode.memory import Memory, make_note
from lcode.repl import choose_session, handle_command, repl

HW = Hardware("linux", "x", 31, "GPU", 12)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(folder: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    r = subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=folder,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return r.stdout.strip()


def new_repo(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    git(folder, "init", "-q")
    (folder / "README.md").write_text(f"# {folder}\n")  # repositories made in the same second differ
    git(folder, "add", "README.md")
    git(folder, "commit", "-qm", "first")
    return folder.resolve()


def talk(make_agent, folder: Path, text: str):
    """A conversation of one request, saved in `folder`."""
    agent = make_agent([reply(f"answer to {text}")])
    agent.cwd = agent.workspace = folder.resolve()
    agent.run_turn(text)
    agent.save()
    return agent


def write_session(folder: Path, session_id: str, data: dict, age: float = 0) -> Path:
    path = sessions.sessions_dir() / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    if age:
        t = path.stat().st_mtime - age
        os.utime(path, (t, t))
    return path


def titles(found):
    return [s.title for s in found]


# ----------------------------------------------------------------------------- two sections


def test_this_folder_holds_its_repository_wherever_it_was_checked_out(make_agent, tmp_path):
    main = new_repo(tmp_path / "project")
    (main / "src").mkdir()
    git(main, "worktree", "add", "-q", str(tmp_path / "feature"))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(main), str(clone))
    other = new_repo(tmp_path / "other")
    plain = tmp_path / "notes"
    plain.mkdir()
    (plain / "inside").mkdir()

    talk(make_agent, plain / "inside", "in a plain subfolder")
    talk(make_agent, plain, "in a plain folder")
    talk(make_agent, other, "in another repository")
    talk(make_agent, clone, "in a clone")
    talk(make_agent, tmp_path / "feature", "in a worktree")
    talk(make_agent, main / "src", "in a subfolder")
    talk(make_agent, main, "in the repository")

    listing = sessions.grouped(main)
    assert titles(listing.here) == ["in the repository", "in a subfolder", "in a worktree", "in a clone"]
    assert titles(listing.elsewhere) == ["in another repository", "in a plain folder", "in a plain subfolder"]
    # A folder outside git has only its own conversations, not its subfolders'.
    assert titles(sessions.grouped(plain).here) == ["in a plain folder"]
    # Each checkout sees the others' conversations in its own section.
    assert titles(sessions.grouped(clone).here)[0] == "in the repository"


def test_the_list_numbers_both_sections_and_shows_where_and_how_much(make_agent, tmp_path, monkeypatch):
    main = new_repo(tmp_path / "project")
    (main / "src").mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    talk(make_agent, other, "fix the other thing")
    talk(make_agent, main / "src", "refactor the parser")
    talk(make_agent, main, "add a cache")
    agent = make_agent(context=32768)
    agent.cwd = agent.workspace = main
    monkeypatch.setattr("builtins.input", lambda prompt="": "3")
    picked = choose_session(agent)
    assert picked is not None and picked.title == "fix the other thing"
    shown = " ".join(output(agent).split())
    assert "This folder ·" in shown and "Elsewhere" in shown
    assert shown.index("add a cache") < shown.index("refactor the parser") < shown.index("Elsewhere")
    assert shown.index("Elsewhere") < shown.index("fix the other thing")
    assert "/ 32K (" in shown  # the context each one uses
    # The subfolder's row says where it was; numbers and names work without the list too.
    assert "│ in src │" in shown.split("refactor the parser", 1)[1].split("Elsewhere")[0]
    assert choose_session(agent, "3").title == "fix the other thing"
    assert choose_session(agent, "parser").title == "refactor the parser"


def test_when_this_folder_has_none_the_others_are_still_listed(make_agent, tmp_path, monkeypatch):
    far = tmp_path / "far"
    far.mkdir()
    talk(make_agent, far, "far away")
    here = tmp_path / "empty"
    here.mkdir()
    agent = make_agent()
    agent.cwd = agent.workspace = here.resolve()
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert choose_session(agent) is None
    shown = " ".join(output(agent).split())
    assert "No saved sessions in this folder" in shown and "far away" in shown


def test_test_runs_in_gone_temporary_folders_and_old_ones_wait_for_resume_all(make_agent, tmp_path, monkeypatch):
    scratch = tmp_path / "scratch-run"
    scratch.mkdir()
    talk(make_agent, scratch, "a scripted test run")
    shutil.rmtree(scratch)
    for i in range(3):
        folder = tmp_path / f"f{i}"
        folder.mkdir()
        talk(make_agent, folder, f"other {i}")
    here = new_repo(tmp_path / "here")
    listing = sessions.grouped(here, elsewhere_limit=2)
    assert titles(listing.elsewhere) == ["other 2", "other 1"]
    assert (listing.more, listing.hidden) == (1, 1)
    assert len(sessions.grouped(here, everything=True).elsewhere) == 4
    agent = make_agent()
    agent.cwd = agent.workspace = here
    monkeypatch.setattr("lcode.sessions.ELSEWHERE_LIMIT", 2)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    choose_session(agent)
    shown = " ".join(output(agent).split())
    assert "a scripted test run" not in shown
    assert (
        "Not shown: 1 older session elsewhere and 1 test run from temporary folders. /resume all lists them." in shown
    )
    # By name it's still found.
    assert choose_session(agent, "scripted").title == "a scripted test run"


# ----------------------------------------------------------------------------- lcode -c


def test_continue_takes_this_folder_first_then_its_repository(make_agent, tmp_path):
    main = new_repo(tmp_path / "project")
    (main / "src").mkdir()
    talk(make_agent, main / "src", "work in src")
    agent = make_agent()
    agent.cwd = agent.workspace = main
    assert agent.load_latest() == f"Working directory is now {main / 'src'}"
    assert agent.session_title == "work in src"
    talk(make_agent, main, "work at the top")
    talk(make_agent, main / "src", "more in src")
    top = make_agent()
    top.cwd = top.workspace = main
    assert top.load_latest() == "" and top.session_title == "work at the top"
    nowhere = make_agent()
    nowhere.cwd = nowhere.workspace = (tmp_path / "project-2").resolve()
    nowhere.workspace.mkdir()
    assert nowhere.load_latest() is None


def test_continue_says_when_it_moved_to_another_folder(make_agent, tmp_path):
    main = new_repo(tmp_path / "project")
    (main / "src").mkdir()
    talk(make_agent, main / "src", "work in src")
    agent = make_agent([reply("carried on")])
    agent.cwd = agent.workspace = main
    repl(agent, prompt="carry on", hardware=HW, cont=True)
    shown = "".join(output(agent).split())  # a long temporary path (macOS) wraps across lines
    assert "Continuingworkinsrc" in shown and f"Workingdirectoryisnow{main / 'src'}" in shown
    assert agent.messages[-1]["content"] == "carried on"


# ----------------------------------------------------------------------------- context per conversation


def test_sessions_save_the_context_they_use_and_resume_shows_it(make_agent, tmp_path):
    agent = make_agent([reply("hi")], context=32768)
    agent.run_turn("hello")  # FakeOllama counts 1000 prompt + 50 output tokens
    agent.save()
    data = json.loads(agent.session_file().read_text())
    assert data["context"] == {"used": 1050, "window": 32768}
    info = sessions.list_sessions(agent.workspace)[0]
    assert (info.tokens, info.estimated) == (1050, False)
    assert info.context(32768) == "1.1K / 32K (3%)"
    other = make_agent(context=32768)
    other.load(info)
    assert other.ctx_used == 1050 and other.next_request_tokens() == 1050  # the status bar, right away
    other.messages.append({"role": "user", "content": "z" * 300})
    assert other.next_request_tokens() == 1150  # and what's added after it


def test_older_sessions_get_an_estimate(make_agent, repo):
    messages = [{"role": "system", "content": "x" * 3000}, {"role": "user", "content": "y" * 3000}]
    write_session(repo, "old", {"cwd": str(repo), "messages": messages, "model": "m"})
    info = sessions.list_sessions(repo)[0]
    assert info.estimated and info.tokens == sessions.estimate_tokens(messages)
    assert info.context(8192).startswith("~2.0K / 8K (")
    agent = make_agent()
    agent.load(info)
    assert agent.ctx_used == sessions.estimate_tokens(agent.messages)  # with today's system prompt


# ----------------------------------------------------------------------------- saved by older versions

OLD_FORMATS = {
    # The keys each earlier version wrote, oldest first (seen in real state folders).
    "0.1": {"cwd": "{cwd}", "messages": "{messages}", "model": "m"},
    "0.5": {"cwd": "{cwd}", "messages": "{messages}", "model": "m", "name": "", "title": "t"},
    "0.10": {"cwd": "{cwd}", "messages": "{messages}", "model": "m", "name": "", "title": "t", "checkpoints": []},
    "0.31": {
        "cwd": "{cwd}",
        "messages": "{messages}",
        "model": "m",
        "name": "named",
        "title": "t",
        "checkpoints": [],
        "todos": [{"content": "step", "status": "pending"}],
        "notes": ["a note"],
        "changed": ["README.md"],
    },
    "0.51": {
        "cwd": "{cwd}",
        "messages": "{messages}",
        "model": "m",
        "mode": "code",
        "image_model": "flux2-klein-4b",
        "video_model": "wan22-ti2v-5b",
        "media": {"results": [], "options": {}, "refs": []},
        "name": "",
        "title": "t",
        "checkpoints": [],
        "todos": [],
        "notes": [],
        "changed": [],
    },
}


@pytest.mark.parametrize("version", list(OLD_FORMATS))
def test_sessions_saved_by_older_versions_list_and_resume(make_agent, tmp_path, version):
    main = new_repo(tmp_path / "project")
    (main / "src").mkdir()
    messages = [
        {"role": "system", "content": "old system prompt"},
        {"role": "user", "content": f"saved by {version}"},
        {"role": "assistant", "content": "ok"},
    ]
    raw = json.dumps(OLD_FORMATS[version]).replace('"{cwd}"', json.dumps(str(main / "src")))
    write_session(main, f"s{version}", json.loads(raw.replace('"{messages}"', json.dumps(messages))))
    # In the repository, from another folder in it: listed under this folder.
    found = sessions.grouped(main).here
    assert [s.id for s in found] == [f"s{version}"] and found[0].turns == 1 and found[0].estimated
    agent = make_agent()
    agent.cwd = agent.workspace = main
    agent.load(found[0])
    assert agent.messages[1]["content"] == f"saved by {version}" and agent.cwd == main / "src"
    agent.ollama.scripts.append(reply("sure"))
    agent.run_turn("go on")
    agent.save()  # saved again in today's format
    data = json.loads(found[0].path.read_text())
    assert data["repo"]["first"] == repoid.first_commit(main) and data["context"]["used"] > 0


def test_old_sessions_whose_folder_is_gone_still_belong_to_its_repository(tmp_path):
    main = new_repo(tmp_path / "project")
    gone = main / "build"  # deleted since
    write_session(main, "old", {"cwd": str(gone), "messages": [{"role": "user", "content": "x"}], "model": "m"})
    assert [s.id for s in sessions.grouped(main).here] == ["old"]


def test_a_broken_or_odd_session_file_is_skipped(tmp_path, repo):
    sessions.sessions_dir().mkdir(parents=True, exist_ok=True)
    (sessions.sessions_dir() / "broken.json").write_text("{not json")
    (sessions.sessions_dir() / "list.json").write_text("[1, 2]")
    write_session(repo, "fine", {"cwd": str(repo), "messages": [{"role": "user", "content": "x"}, "junk"]})
    assert [s.id for s in sessions.grouped(repo).here] == ["fine"]


# ----------------------------------------------------------------------------- notes follow the repository


def test_notes_follow_a_repository_that_moved_or_was_cloned_again(tmp_path):
    main = new_repo(tmp_path / "project")
    Memory(main).save(make_note("vpn", "project", "Staging needs the VPN"))
    folder = memory_notes.project_dir(main)
    assert json.loads((folder / memory_notes.IDENTITY).read_text())["first"] == repoid.first_commit(main)
    moved = tmp_path / "renamed"
    main.rename(moved)
    assert [n.name for n in Memory(moved).notes()] == ["vpn"]
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(moved), str(clone))
    assert [n.name for n in Memory(clone).notes()] == ["vpn"]
    unrelated = new_repo(tmp_path / "unrelated")
    assert Memory(unrelated).notes() == []


def test_notes_saved_by_older_versions_load_and_then_follow_the_repository(tmp_path):
    main = new_repo(tmp_path / "project")
    old = memory_notes.path_dir(main)  # where 0.51 and earlier saved them, without repo.json
    old.mkdir(parents=True)
    (old / "test-command.md").write_text(
        "---\nname: test-command\ntype: project\ndescription: Run uv run pytest\nmodified: 2026-10-01\n---\n"
    )
    assert [n.name for n in Memory(main).notes()] == ["test-command"]
    assert (old / memory_notes.IDENTITY).exists()  # now it says which repository it's for
    moved = tmp_path / "moved"
    main.rename(moved)
    assert [n.name for n in Memory(moved).notes()] == ["test-command"]


def test_a_folder_outside_git_keeps_its_own_notes(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    Memory(plain).save(make_note("x", "project", "Plain folder fact"))
    assert not (memory_notes.project_dir(plain) / memory_notes.IDENTITY).exists()
    assert [n.name for n in Memory(plain).notes()] == ["x"]


# ----------------------------------------------------------------------------- quitting


def test_quitting_saves_without_a_memory_check(make_agent, monkeypatch):
    agent = make_agent([reply("one"), reply("two")], memory="ask")
    lines = iter(["first request", "second request"])

    class Session:
        def prompt(self, *args, **kwargs):
            try:
                return next(lines)
            except StopIteration:
                raise EOFError from None

    monkeypatch.setattr("lcode.repl.build_session", lambda agent: Session())
    monkeypatch.setattr("lcode.repl.preload", lambda agent, session: None)
    monkeypatch.setattr("lcode.repl.start_steering", lambda agent: None)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail(f"asked {prompt!r} on the way out"))
    repl(agent, prompt=None, hardware=HW)
    assert len(agent.ollama.chats) == 0  # no end-of-session model call
    assert "worth remembering" not in output(agent) and "Bye." in output(agent)
    assert sessions.list_sessions(agent.workspace)[0].turns == 2


def test_resume_command_lists_this_folder_first(make_agent, tmp_path, monkeypatch):
    main = new_repo(tmp_path / "project")
    talk(make_agent, main, "the one here")
    agent = make_agent()
    agent.cwd = agent.workspace = main
    monkeypatch.setattr("builtins.input", lambda prompt="": "1")
    handle_command(agent, "/resume", HW)
    assert agent.session_title == "the one here"


def test_doctor_says_how_many_sessions_and_where(make_agent, tmp_path):
    main = new_repo(tmp_path / "project")
    talk(make_agent, main, "here")
    other = tmp_path / "other"
    other.mkdir()
    talk(make_agent, other, "there")
    assert cli.sessions_status(main) == f"2 saved, 1 for this folder · in {sessions.sessions_dir()}"
