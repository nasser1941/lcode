import json
import subprocess

import pytest

from conftest import output, reply
from lcode import gitflow
from lcode.agent import git_info
from lcode.cli import build_parser
from lcode.hardware import Hardware
from lcode.repl import handle_command

HW = Hardware("linux", "x", 31, "GPU", 12)


def run(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def git_repo(repo, tmp_path_factory, monkeypatch):
    """The demo repository under git, with one commit on main and git isolated from the user's settings."""
    home = tmp_path_factory.mktemp("home")
    (home / "gitconfig").write_text("[user]\n\tname = Test\n\temail = test@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (repo / ".gitignore").write_text("node_modules/\n")
    run(repo, "init", "-q", "-b", "main")
    run(repo, "add", "-A")
    run(repo, "commit", "-q", "-m", "fix: start the demo")
    return repo


def answers(monkeypatch, *values):
    queue = list(values)
    monkeypatch.setattr("builtins.input", lambda prompt="": queue.pop(0))


def model_answers(agent, *answers):
    agent.ollama.chat_replies = [json.dumps(a) for a in answers]


# ----------------------------------------------------------------------------- /commit


def test_commit_writes_a_message_in_the_repositorys_style(make_agent, git_repo, monkeypatch):
    agent = make_agent()
    (git_repo / "src/pkg/math.py").write_text("def add(a, b):\n    return a + b\n")
    (git_repo / "src/pkg/mul.py").write_text("def mul(a, b):\n    return a * b\n")
    (git_repo / "scratch.txt").write_text("notes to self\n")
    (git_repo / ".env").write_text("TOKEN=abc\n")
    model_answers(agent, {"files": ["src/pkg/math.py", "src/pkg/mul.py", "nope.py"], "message": "feat: add mul"})
    answers(monkeypatch, "y")
    before = list(agent.messages)
    handle_command(agent, "/commit", HW)
    assert run(git_repo, "log", "-1", "--format=%s").strip() == "feat: add mul"
    committed = run(git_repo, "show", "--name-only", "--format=").split()
    assert sorted(committed) == ["src/pkg/math.py", "src/pkg/mul.py"]
    status = run(git_repo, "status", "--porcelain")
    assert "?? .env" in status and "?? scratch.txt" in status  # left out
    text = output(agent)
    assert "Left out (unrelated, the model thinks): scratch.txt" in text and "may hold secrets" in text
    [payload] = agent.ollama.chats
    request = payload["messages"][-1]["content"]
    assert payload["format"] == gitflow.COMMIT_SCHEMA and payload["messages"][:-1] == before
    assert "fix: start the demo" in request and "new file: src/pkg/mul.py" in request and ".env" not in request
    assert agent.messages == before  # the request isn't kept in the conversation


def test_commit_uses_what_is_staged_and_can_be_edited(make_agent, git_repo, monkeypatch):
    agent = make_agent()
    (git_repo / "README.md").write_text("# Demo\n\nNow documented.\n")
    (git_repo / "src/pkg/math.py").write_text("def add(a, b):\n    return a + b\n")
    run(git_repo, "add", "README.md")
    model_answers(agent, {"files": [], "message": "```\nDocument the demo\n```"})
    monkeypatch.setattr("lcode.planning.edit_text", lambda text: text.replace("Document", "Describe"))
    answers(monkeypatch, "e", "y")
    handle_command(agent, "/commit", HW)
    assert run(git_repo, "log", "-1", "--format=%s").strip() == "Describe the demo"
    assert run(git_repo, "show", "--name-only", "--format=").split() == ["README.md"]
    assert " M src/pkg/math.py" in run(git_repo, "status", "--porcelain")  # not staged, not committed
    assert "Committing the 1 staged file(s); 1 other changed file(s) stay out of it." in output(agent)


def test_commit_asks_first_and_explains_when_there_is_nothing(make_agent, git_repo, monkeypatch):
    agent = make_agent()
    handle_command(agent, "/commit", HW)
    assert "Nothing to commit: the working tree is clean." in output(agent)
    (git_repo / "README.md").write_text("changed\n")
    model_answers(agent, {"files": ["README.md"], "message": "Change the readme"})
    answers(monkeypatch, "n")
    handle_command(agent, "/commit", HW)
    assert "Not committed." in output(agent) and run(git_repo, "rev-list", "--count", "HEAD").strip() == "1"
    (git_repo / "README.md").write_text("# Demo\n")
    (git_repo / ".env.local").write_text("KEY=1\n")
    handle_command(agent, "/commit", HW)
    assert "Only files that may hold secrets changed (.env.local)" in output(agent)


# ----------------------------------------------------------------------------- /review


def test_review_covers_uncommitted_changes_or_the_branch(make_agent, git_repo):
    agent = make_agent()
    with pytest.raises(gitflow.GitError, match="nothing to review"):
        gitflow.review_prompt(agent)
    (git_repo / "src/pkg/math.py").write_text("def add(a, b):\n    return a * b\n")
    (git_repo / "src/pkg/new.py").write_text("X = 1\n")
    prompt = gitflow.review_prompt(agent)
    assert prompt.startswith("Review the uncommitted changes before it's committed.")
    assert "-    return a + b" in prompt and "new file: src/pkg/new.py" in prompt
    assert "New files, not yet added to git: src/pkg/new.py" in prompt
    run(git_repo, "switch", "-q", "-c", "feature")
    run(git_repo, "add", "-A")
    run(git_repo, "commit", "-q", "-m", "Multiply instead")
    prompt = gitflow.review_prompt(agent)  # clean: the branch against main
    assert "the changes on feature compared with main before it's merged" in prompt
    assert "- Multiply instead" in prompt and "+    return a * b" in prompt
    with pytest.raises(gitflow.GitError, match="no branch or commit called nope"):
        gitflow.review_prompt(agent, "nope")
    (git_repo / "debug.log").write_text("junk\n")  # only a new file: the branch is still what's reviewed
    prompt = gitflow.review_prompt(agent)
    assert "compared with main" in prompt and "New files, not yet added to git: debug.log" in prompt
    (git_repo / "README.md").write_text("# Demo\n\nWork in progress.\n")  # tracked changes come first
    prompt = gitflow.review_prompt(agent)
    assert prompt.startswith("Review the uncommitted changes") and "+Work in progress." in prompt
    assert "Multiply instead" not in prompt
    prompt = gitflow.review_prompt(agent, "main")  # with a base: the branch and the work in progress
    assert "- Multiply instead" in prompt and "+Work in progress." in prompt


def test_a_review_cannot_change_anything(make_agent, git_repo):
    (git_repo / "README.md").write_text("# Demo\n\nTypo heer.\n")
    agent = make_agent(
        [
            reply(tool_calls=[{"function": {"name": "edit_file", "arguments": {
                "path": "README.md", "old_string": "heer", "new_string": "here"}}}]),
            reply(tool_calls=[{"function": {"name": "bash", "arguments": {"command": "rm README.md"}}}]),
            reply(tool_calls=[{"function": {"name": "bash", "arguments": {"command": "git diff"}}}]),
            reply("README.md:3 — clarity — 'heer' should be 'here'."),
        ]
    )  # fmt: skip
    handle_command(agent, "/review", HW)
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert results[0].startswith("Error: This is a review, so nothing can be changed")
    assert "Only read-only commands run" in results[1]
    assert "Typo heer" in results[2]  # git diff ran
    assert (git_repo / "README.md").read_text() == "# Demo\n\nTypo heer.\n" and agent.no_changes == ""


def test_a_command_of_your_own_replaces_review(make_agent, git_repo):
    (git_repo / ".lcode/commands").mkdir(parents=True)
    (git_repo / ".lcode/commands/review.md").write_text("---\ndescription: our review\n---\nCheck $ARGUMENTS our way")
    agent = make_agent([reply("ok")])
    handle_command(agent, "/review src", HW)
    assert agent.messages[1]["content"] == "Check src our way"
    handle_command(agent, "/help", HW)
    assert "(replaces the built-in command)" in output(agent)


# ----------------------------------------------------------------------------- /pr


@pytest.fixture
def github(git_repo, tmp_path_factory, monkeypatch):
    """A bare repository as origin, and a stand-in gh that logs its arguments."""
    remote = tmp_path_factory.mktemp("remote") / "demo.git"
    run(remote.parent, "init", "-q", "--bare", "-b", "main", str(remote))
    run(git_repo, "remote", "add", "origin", str(remote))
    run(git_repo, "push", "-q", "-u", "origin", "main")
    bin_dir = tmp_path_factory.mktemp("bin")
    log = bin_dir / "gh.log"
    gh = bin_dir / "gh"
    gh.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {log}\n'
        'if [ "$1 $2" = "pr view" ]; then exit 1; fi\n'
        'if [ "$1 $2" = "pr create" ]; then echo https://github.com/acme/demo/pull/7; fi\n'
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    return remote, log


def test_pr_moves_commits_on_main_to_a_new_branch_and_opens_it(make_agent, git_repo, github, monkeypatch):
    remote, log = github
    agent = make_agent()
    (git_repo / "src/pkg/math.py").write_text("def add(a, b):\n    return a + b\n")
    handle_command(agent, "/pr", HW)
    assert "Commit them first with /commit" in output(agent)
    run(git_repo, "commit", "-qam", "Tidy add")
    model_answers(agent, {"branch": "tidy add!", "title": "Tidy add", "body": "Removes trailing spaces."})
    answers(monkeypatch, "y")
    handle_command(agent, "/pr", HW)
    assert run(git_repo, "branch", "--show-current").strip() == "tidy-add"
    assert run(remote, "log", "-1", "--format=%s", "tidy-add").strip() == "Tidy add"
    create = next(line for line in log.read_text().splitlines() if line.startswith("pr create"))
    assert "--base main --head tidy-add --title Tidy add --body-file" in create
    text = output(agent)
    assert "✓ Opened https://github.com/acme/demo/pull/7" in text and "go to a new branch: tidy-add" in text
    request = agent.ollama.chats[0]["messages"][-1]["content"]
    assert "into main" in request and "- Tidy add" in request


def test_pr_asks_before_pushing(make_agent, git_repo, github, monkeypatch):
    remote, log = github
    agent = make_agent()
    run(git_repo, "switch", "-q", "-c", "docs")
    (git_repo / "README.md").write_text("# Demo\n\nMore.\n")
    run(git_repo, "commit", "-qam", "Say more")
    model_answers(agent, {"branch": "", "title": "Say more", "body": "Docs."})
    answers(monkeypatch, "n")
    handle_command(agent, "/pr main explain the docs", HW)
    assert "No pull request opened." in output(agent)
    assert "docs" not in run(remote, "branch") and "pr create" not in log.read_text()
    assert "The user adds: explain the docs" in agent.ollama.chats[0]["messages"][-1]["content"]


# ----------------------------------------------------------------------------- lcode --worktree


def test_a_worktree_is_removed_when_nothing_changed(git_repo, tmp_path_factory, monkeypatch):
    state = tmp_path_factory.mktemp("state")
    monkeypatch.setattr("lcode.config.STATE_DIR", state)
    session = gitflow.start_worktree(git_repo / "src", "try-it")
    assert session.path.parent.parent == state / "worktrees" and session.path.name == "try-it"
    assert not session.path.is_relative_to(git_repo)  # the main checkout's path would mislead the model
    assert session.cwd == session.path / "src" and session.created_branch
    assert run(session.path, "branch", "--show-current").strip() == "try-it"
    assert run(git_repo, "status", "--porcelain") == ""
    assert session.finish() == "Nothing changed in the worktree, so it was removed with its branch."
    assert not session.path.exists() and "try-it" not in run(git_repo, "branch")


def test_a_worktree_with_work_stays_and_can_be_continued(git_repo, tmp_path_factory, monkeypatch):
    monkeypatch.setattr("lcode.config.STATE_DIR", tmp_path_factory.mktemp("state"))
    session = gitflow.start_worktree(git_repo, "feature/x")
    (session.path / "README.md").write_text("# Demo\n\nFrom the worktree.\n")
    run(session.path, "commit", "-qam", "Work in the worktree")
    (session.path / "notes.txt").write_text("wip\n")
    message = session.finish()
    assert "stays at" in message and "1 commit(s) and uncommitted changes" in message
    assert "lcode --worktree feature/x" in message
    again = gitflow.start_worktree(session.path, "feature/x")  # also from inside the worktree
    assert again.path == session.path and not again.created_branch and again.root == git_repo
    assert "This folder is a git worktree, separate from the repository's main checkout" in git_info(again.cwd)
    assert "worktree" not in git_info(git_repo)
    assert (git_repo / "README.md").read_text() == "# Demo\n"  # the main working tree is untouched
    with pytest.raises(gitflow.GitError, match="isn't a valid branch name"):
        gitflow.start_worktree(git_repo, "bad..name")
    assert build_parser().parse_args(["-w"]).worktree == ""
    assert build_parser().parse_args(["--worktree", "fix-it"]).worktree == "fix-it"


def test_outside_a_repository(make_agent, repo):
    agent = make_agent()
    handle_command(agent, "/commit", HW)
    handle_command(agent, "/review", HW)
    assert output(agent).count("this isn't a git repository") == 2
    with pytest.raises(gitflow.GitError, match="this isn't a git repository"):
        gitflow.start_worktree(repo, "x")
