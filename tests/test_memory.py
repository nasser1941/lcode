import json
import shutil
import subprocess

import pytest
from rich.console import Console

from conftest import output, reply
from lcode import config
from lcode import memory as memory_notes
from lcode.hardware import Hardware
from lcode.memory import Memory, NoteError, make_note, project_dir
from lcode.permissions import Permissions
from lcode.repl import handle_command

HW = Hardware("linux", "x", 31, "GPU", 12)


@pytest.fixture(autouse=True)
def state(tmp_path_factory, monkeypatch):
    monkeypatch.setattr("lcode.config.STATE_DIR", tmp_path_factory.mktemp("state"))


def mem(**arguments):
    return {"function": {"name": "memory", "arguments": arguments}}


def answers(monkeypatch, *replies):
    queue = list(replies)
    monkeypatch.setattr("builtins.input", lambda prompt="": queue.pop(0))


def two_turns(make_agent, **settings):
    agent = make_agent([reply("Use pnpm here."), reply("Done.")], **settings)
    agent.run_turn("how do I install deps?")
    agent.run_turn("no, we use pnpm, not npm")
    return agent


# ----------------------------------------------------------------------------- notes on disk


def test_notes_are_markdown_files_with_an_index(repo):
    memory = Memory(repo)
    saved, updated = memory.save(make_note("test-command", "project", "Run tests with `uv run pytest -q`", "Why: deps"))
    assert not updated
    text = saved.path.read_text()
    assert text.startswith("---\nname: test-command\ntype: project\ndescription: Run tests with `uv run pytest -q`\n")
    assert text.endswith("---\nWhy: deps\n")
    [note] = memory.notes()
    assert (note.name, note.type, note.details, note.scope) == ("test-command", "project", "Why: deps", "project")
    assert "test-command" in (memory.project / "MEMORY.md").read_text()


def test_saving_under_the_same_name_updates_the_note(repo):
    memory = Memory(repo)
    memory.save(make_note("use-pnpm", "feedback", "Use pnpm, not npm", "Why: lockfile"))
    _, updated = memory.save(make_note("use-pnpm", "feedback", "Use pnpm (not npm) for everything"))
    assert updated
    [note] = memory.notes()
    assert note.description == "Use pnpm (not npm) for everything" and note.details == ""
    memory.save(make_note("fact-2", "project", "Fact number 2 that a later session should know"))
    memory.save(make_note("fact-3", "project", "Fact number 3 that a later session should know"))
    assert len(memory.notes()) == 3  # similar wording, different facts
    assert memory.similar(make_note("again", "feedback", "Use pnpm (not npm) for everything!")).name == "use-pnpm"


def test_user_notes_are_shared_by_every_repository(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    Memory(tmp_path / "a").save(make_note("short-answers", "user", "Prefers short answers", scope="user"))
    Memory(tmp_path / "a").save(make_note("only-a", "project", "Only in a"))
    notes = Memory(tmp_path / "b").notes()
    assert [n.name for n in notes] == ["short-answers"]


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_git_worktrees_share_the_repository_notes(tmp_path):
    main = tmp_path / "main"
    main.mkdir()

    def git(*args, cwd=main):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    git("init", "-q")
    (main / "f").write_text("x")
    git("add", "f")
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")
    git("worktree", "add", "-q", str(tmp_path / "wt"))
    (main / "sub").mkdir()
    assert project_dir(main) == project_dir(tmp_path / "wt") == project_dir(main / "sub")
    assert project_dir(main).name.startswith("main-")
    other = tmp_path / "other"
    other.mkdir()
    assert project_dir(other) != project_dir(main)


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "deploy key AKIAIOSFODNN7EXAMPLE",
        "token ghp_" + "a1" * 18,
        "use sk-ant-" + "x" * 30,
        "db at postgresql://app:hunter22@db.internal:5432/app",
        "password: hunter22",
        "API_KEY" + "=" + "abc" * 4,  # fake credentials are built in pieces so secret scanners skip this file
        ".".join(["eyJ" + "a" * 12, "eyJ" + "b" * 12, "c" * 12]),
    ],
)
def test_secrets_are_never_saved(repo, text):
    with pytest.raises(NoteError, match="secret"):
        make_note("x", "reference", text)
    with pytest.raises(NoteError, match="secret"):
        make_note("x", "reference", "Staging credentials", details=text)


@pytest.mark.parametrize(
    "text", ["API keys live in 1Password under 'staging'", "The token is stored in Vault", "Use the password manager"]
)
def test_talking_about_secrets_is_fine(text):
    make_note("where-secrets-live", "reference", text)


def test_notes_are_checked():
    with pytest.raises(NoteError, match="type"):
        make_note("x", "fact", "something")
    with pytest.raises(NoteError, match="one line"):
        make_note("x", "project", "y" * 250)
    with pytest.raises(NoteError, match="empty"):
        make_note("x", "project", "   ")
    assert make_note("", "project", "The Staging DB needs the VPN!").name == "the-staging-db-needs-the-vpn"


def test_the_index_stays_within_its_budget(repo):
    memory = Memory(repo)
    for i in range(300):
        note = make_note(f"note-{i}", "project", f"Fact number {i} that a later session should know about")
        memory.save(memory_notes.replace(note, modified=f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}"))
    prompt = memory.prompt(262144)
    index = prompt.split("For this repository:\n", 1)[1].split("\n- Save a note", 1)[0]
    assert len(index) <= memory_notes.MAX_INDEX_CHARS + 100
    assert "older notes not shown" in index
    assert "note-299" in index and "note-0," not in index  # the newest are kept
    assert len(memory.prompt(16384)) < len(prompt)  # smaller windows get a smaller index


# ----------------------------------------------------------------------------- in a session


def test_notes_go_into_the_system_prompt(make_agent, repo):
    Memory(repo).save(make_note("use-pnpm", "feedback", "Use pnpm, not npm"))
    agent = make_agent(memory="ask")
    assert "# Memory" in agent.messages[0]["content"]
    assert "[feedback] Use pnpm, not npm (use-pnpm," in agent.messages[0]["content"]
    assert "memory" in {s["function"]["name"] for s in agent.tool_schemas()}
    off = make_agent(memory="off")
    assert "# Memory" not in off.messages[0]["content"]
    assert "memory" not in {s["function"]["name"] for s in off.tool_schemas()}


def test_the_model_saves_a_note(make_agent):
    save = mem(action="save", name="use-pnpm", type="feedback", description="Use pnpm, not npm")
    agent = make_agent([reply(tool_calls=[save]), reply("Noted.")], memory="auto")
    agent.run_turn("we use pnpm here")
    assert agent.messages[3]["content"] == "Saved the project note 'use-pnpm'."
    assert [n.name for n in agent.memory().notes()] == ["use-pnpm"]
    assert "remembered use-pnpm" in output(agent)


def test_with_memory_ask_even_yolo_asks(make_agent, monkeypatch):
    save = mem(action="save", name="use-pnpm", type="feedback", description="Use pnpm, not npm")
    agent = make_agent([reply(tool_calls=[save]), reply("OK.")], mode="yolo", memory="ask")
    answers(monkeypatch, "n")
    agent.run_turn("we use pnpm here")
    assert "didn't want this saved" in agent.messages[3]["content"]
    assert agent.memory().notes() == []


def test_the_model_is_told_about_a_similar_note(make_agent, repo):
    Memory(repo).save(make_note("use-pnpm", "feedback", "Use pnpm, not npm"))
    save = mem(action="save", name="package-manager", type="feedback", description="Use pnpm, not npm!")
    agent = make_agent([reply(tool_calls=[save]), reply("OK.")], memory="auto")
    agent.run_turn("we use pnpm")
    assert "save under the name 'use-pnpm'" in agent.messages[3]["content"]
    assert len(agent.memory().notes()) == 1


def test_the_model_cant_save_secrets(make_agent):
    save = mem(action="save", name="db", type="reference", description="DB password: hunter22!")
    agent = make_agent([reply(tool_calls=[save]), reply("OK.")], memory="auto")
    agent.run_turn("remember the db password")
    assert agent.messages[3]["content"].startswith("Error: not saved: it seems to contain a password or key")
    assert agent.memory().notes() == []


def test_the_model_reads_and_deletes_notes(make_agent, repo):
    Memory(repo).save(make_note("vpn", "project", "Staging needs the VPN", "Connect with `vpn up` first"))
    agent = make_agent(
        [
            reply(tool_calls=[mem(action="read")]),
            reply(tool_calls=[mem(action="read", name="vpn")]),
            reply(tool_calls=[mem(action="delete", name="vpn")]),
            reply("Done."),
        ],
        memory="auto",
    )
    agent.run_turn("the vpn note is wrong")
    tool_results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "Staging needs the VPN" in tool_results[0]
    assert "Connect with `vpn up` first" in tool_results[1]
    assert tool_results[2] == "Deleted the note 'vpn'."
    assert agent.memory().notes() == []


# ----------------------------------------------------------------------------- reflection


def proposal(*notes):
    return json.dumps({"notes": list(notes)})


PNPM = {"type": "feedback", "name": "use-pnpm", "description": "Use pnpm, not npm", "scope": "project"}
VPN = {"type": "project", "name": "vpn", "description": "Staging needs the VPN", "details": "vpn up"}


def test_reflection_saves_proposed_notes_with_memory_auto(make_agent):
    agent = two_turns(make_agent, memory="auto")
    agent.ollama.chat_replies = [proposal(PNPM, VPN)]
    memory_notes.reflect(agent)
    assert sorted(n.name for n in agent.memory().notes()) == ["use-pnpm", "vpn"]
    payload = agent.ollama.chats[0]
    # The session's messages, tools and options, so Ollama reuses the cached prompt.
    assert payload["messages"][:-1] == agent.messages
    assert payload["tools"] == agent.tool_schemas()
    assert payload["options"] == agent.options()
    assert payload["format"] == memory_notes.REFLECT_SCHEMA and payload["think"] is False
    assert "Remembered: Use pnpm, not npm" in output(agent)
    memory_notes.reflect(agent)  # nothing new since
    assert len(agent.ollama.chats) == 1


def test_reflection_with_memory_ask_saves_only_what_the_user_picks(make_agent, monkeypatch):
    agent = two_turns(make_agent, memory="ask")
    agent.ollama.chat_replies = [proposal(PNPM, VPN)]
    answers(monkeypatch, "2")
    memory_notes.reflect(agent)
    assert [n.name for n in agent.memory().notes()] == ["vpn"]


def test_reflection_with_memory_ask_saves_nothing_without_approval(make_agent, monkeypatch):
    agent = two_turns(make_agent, memory="ask")
    agent.ollama.chat_replies = [proposal(PNPM)]

    def no_input(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_input)
    memory_notes.reflect(agent)
    assert agent.memory().notes() == []


def test_short_sessions_and_memory_off_skip_the_reflection(make_agent):
    agent = make_agent([reply("Hi!")], memory="auto")
    agent.run_turn("hi")
    memory_notes.reflect(agent)
    off = two_turns(make_agent, memory="off")
    memory_notes.reflect(off)
    assert agent.ollama.chats == [] and off.ollama.chats == []


def test_reflection_drops_secrets_junk_and_restatements(make_agent, repo):
    Memory(repo).save(make_note("vpn-note", "project", "Staging needs the VPN!"))
    agent = two_turns(make_agent, memory="auto")
    secret = {"type": "reference", "name": "db", "description": "DB at postgres://app:hunter22@db:5432/app"}
    items = [json.dumps(secret), '{"type": "x"}', json.dumps(PNPM), json.dumps(VPN)]
    agent.ollama.chat_replies = ['Sure! {"notes": [' + ", ".join(items) + "]}"]
    memory_notes.reflect(agent)
    assert sorted(n.name for n in agent.memory().notes()) == ["use-pnpm", "vpn-note"]


def test_compaction_reflects_first(make_agent):
    agent = two_turns(make_agent, memory="auto")
    agent.ollama.chat_replies = [proposal(PNPM)]
    agent.ollama.scripts.append(reply("Summary: pnpm."))
    agent.compact()
    assert [n.name for n in agent.memory().notes()] == ["use-pnpm"]
    assert agent.reflected == len(agent.messages)


def test_clear_reflects_before_starting_over(make_agent):
    agent = two_turns(make_agent, memory="auto")
    agent.ollama.chat_replies = [proposal(PNPM)]
    handle_command(agent, "/clear", HW)
    assert [n.name for n in agent.memory().notes()] == ["use-pnpm"]


# ----------------------------------------------------------------------------- commands and settings


def test_remember_and_memory_commands(make_agent, monkeypatch):
    agent = make_agent(memory="ask")
    handle_command(agent, "/remember the staging database needs the VPN", HW)
    handle_command(agent, "/remember -g feedback: keep answers short", HW)
    notes = agent.memory().notes()
    assert [(n.scope, n.type, n.description) for n in notes] == [
        ("user", "feedback", "keep answers short"),
        ("project", "project", "the staging database needs the VPN"),
    ]
    assert agent.messages[-1]["content"].startswith("[lcode] The user saved a note")
    handle_command(agent, "/memory", HW)
    assert "keep answers short" in output(agent) and "every repo" in output(agent)
    handle_command(agent, "/remember password: hunter22", HW)
    assert "secret" in output(agent)
    answers(monkeypatch, "y")
    handle_command(agent, "/memory delete 1", HW)
    assert [n.scope for n in agent.memory().notes()] == ["project"]
    handle_command(agent, "/memory show the-staging-database-needs-the-vpn", HW)
    assert "name: the-staging-database-needs-the-vpn" in output(agent)


def test_memory_commands_when_memory_is_off(make_agent):
    agent = make_agent(memory="off")
    handle_command(agent, "/remember something", HW)
    assert "Memory is off" in output(agent)
    assert agent.memory().notes() == []


def test_memory_setting():
    assert config.coerce("memory", "AUTO") == "auto"
    assert config.coerce("memory", "on") == "ask"
    assert config.coerce("memory", "false") == "off"
    with pytest.raises(config.ConfigError):
        config.coerce("memory", "sometimes")


def test_always_allow_memory_notes(monkeypatch):
    perms = Permissions(Console(file=None, quiet=True), "yolo")
    answers(monkeypatch, "a")
    assert perms.request("memory", "memory", "Remember this?", "x") == (True, "")
    assert perms.request("memory", "memory", "Remember this?", "x") == (True, "")  # no second question
