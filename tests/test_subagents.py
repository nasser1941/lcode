import shutil
import subprocess
import threading
import time

import pytest

from conftest import FakeOllama, output, reply
from lcode import config, subagents
from lcode.hardware import Hardware
from lcode.repl import handle_command

HW = Hardware("linux", "x", 31, "GPU", 12)
needs_git = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def tool(name, **arguments):
    return {"function": {"name": name, "arguments": arguments}}


def delegate(kind="explore", task="Find where add is defined", description="find add"):
    return tool("agent", type=kind, task=task, description=description)


def answers(monkeypatch, *replies):
    queue = list(replies)
    monkeypatch.setattr("builtins.input", lambda prompt="": queue.pop(0))


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def commit_all(repo):
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init")


class RoutingOllama(FakeOllama):
    """Answers each conversation from its own script, picked by a word in its first request, and records
    how many requests were in flight at once."""

    def __init__(self, routes: dict[str, list[list[dict]]], delay: float = 0.0):
        super().__init__()
        self.routes = routes
        self.delay = delay
        self.lock = threading.Lock()
        self.in_flight = 0
        self.most_in_flight = 0

    def chat_stream(self, payload, on_open=None):
        first = next(m["content"] for m in payload["messages"] if m["role"] == "user")
        with self.lock:
            self.payloads.append(payload)
            script = next(scripts for word, scripts in self.routes.items() if word in first).pop(0)
            self.in_flight += 1
            self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            time.sleep(self.delay)
            yield from script
        finally:
            with self.lock:
                self.in_flight -= 1


# ----------------------------------------------------------------------------- one subagent


def test_an_explore_agent_returns_only_its_report(make_agent):
    agent = make_agent(
        [
            reply(tool_calls=[delegate()]),
            reply(tool_calls=[tool("grep", pattern="def add")]),
            reply("`add` is defined in src/pkg/math.py:1."),
            reply("It's in src/pkg/math.py."),
        ],
        subagents=True,
    )
    agent.run_turn("where is add?")
    roles = [m["role"] for m in agent.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]  # the subagent's grep stays out
    result = agent.messages[3]["content"]
    assert result.startswith("`add` is defined in src/pkg/math.py:1.")
    assert "[explore agent · 1 tool calls ·" in result
    sub = agent.ollama.payloads[1]
    assert sub["messages"][0]["content"].count("# You are a subagent") == 1
    assert "find what the task asks for" in sub["messages"][0]["content"]
    assert sub["messages"][1] == {"role": "user", "content": "Find where add is defined"}
    names = {t["function"]["name"] for t in sub["tools"]}
    assert "grep" in names and not names & {"edit_file", "write_file", "agent", "memory"}
    assert sub["options"] == agent.ollama.payloads[0]["options"]  # same model and window: no reload
    [run] = agent.agent_runs
    assert (run.kind, run.label, run.outcome, run.tools) == ("explore", "find add", "done", 1)
    assert "↳ explore" in output(agent)


def test_read_only_agents_cant_change_anything(make_agent, repo):
    agent = make_agent(
        [
            reply(tool_calls=[delegate()]),
            reply(
                tool_calls=[
                    tool("edit_file", path="README.md", old_string="Demo", new_string="X"),
                    tool("bash", command="rm README.md"),
                    tool("bash", command="ls"),
                ]
            ),
            reply("Nothing changed."),
            reply("ok"),
        ],
        subagents=True,
    )
    agent.run_turn("go")
    results = [m["content"] for m in agent.agent_runs[0].messages if m["role"] == "tool"]
    assert results[0].startswith("Error: edit_file isn't available to you")
    assert results[1].startswith("Error: you can only run read-only commands")
    assert "README.md" in results[2]
    assert (repo / "README.md").read_text() == "# Demo\n"


def test_a_worker_edits_the_tree_under_the_sessions_checkpoint(make_agent, repo):
    agent = make_agent(
        [
            reply(tool_calls=[delegate("worker", "Rename sub to subtract in src/pkg/math.py", "rename sub")]),
            reply(tool_calls=[tool("read_file", path="src/pkg/math.py")]),
            reply(
                tool_calls=[tool("edit_file", path="src/pkg/math.py", old_string="def sub", new_string="def subtract")]
            ),
            reply("Renamed sub to subtract."),
            reply("Done."),
        ],
        subagents=True,
    )
    agent.run_turn("rename sub")
    assert "def subtract" in (repo / "src/pkg/math.py").read_text()
    assert [cp.paths for cp in agent.checkpoints.items] == [["src/pkg/math.py"]]  # /undo covers it


def test_permission_prompts_name_the_agent(make_agent, repo, monkeypatch):
    agent = make_agent(
        [
            reply(tool_calls=[delegate("worker", "Create a file x", "make x")]),
            reply(tool_calls=[tool("bash", command="touch x")]),
            reply("Created x."),
            reply("Done."),
        ],
        mode="ask",
        subagents=True,
    )
    answers(monkeypatch, "y")
    agent.run_turn("make x")
    assert "worker agent › Run command" in output(agent)
    assert (repo / "x").exists()


def test_running_out_of_steps_still_gives_a_report(make_agent, repo):
    (repo / ".lcode" / "agents").mkdir(parents=True)
    (repo / ".lcode" / "agents" / "quick.md").write_text(
        "---\ndescription: one step only\nmax_steps: 1\n---\nBe quick."
    )
    agent = make_agent(
        [
            reply(tool_calls=[delegate("quick")]),
            reply(tool_calls=[tool("list_dir")]),
            reply("I only got as far as the top-level listing."),
            reply("ok"),
        ],
        subagents=True,
    )
    agent.run_turn("go")
    assert agent.messages[3]["content"].startswith("I only got as far as the top-level listing.")
    last = agent.ollama.payloads[2]
    assert "You've used all your steps" in last["messages"][-2]["content"] and "tools" not in last


def test_ctrl_c_stops_a_subagent_and_the_request(make_agent):
    agent = make_agent([reply(tool_calls=[delegate()])], subagents=True)
    original = agent.ollama.chat_stream

    def chat_stream(payload, on_open=None):
        if len(agent.ollama.payloads) == 1:
            agent.ollama.payloads.append(payload)
            raise KeyboardInterrupt
        yield from original(payload)

    agent.ollama.chat_stream = chat_stream
    with pytest.raises(KeyboardInterrupt):
        agent.run_turn("go")
    assert agent.agent_runs[0].outcome == "stopped"
    assert agent.messages[-1]["content"] == "Interrupted by the user before completion."


def test_unknown_agent_types_are_explained(make_agent):
    agent = make_agent([reply(tool_calls=[delegate("wizard")]), reply("ok")], subagents=True)
    agent.run_turn("go")
    assert agent.messages[3]["content"].startswith("Error: unknown agent type 'wizard'; available: explore, plan")


# ----------------------------------------------------------------------------- several at once


def two_explores(**words):
    return [
        delegate("explore", "ALPHA: find add", "find add"),
        delegate("explore", "BETA: find sub", "find sub"),
    ]


@pytest.mark.parametrize(("parallel", "expected"), [(2, 2), (1, 1)])
def test_agents_run_at_the_same_time_when_allowed(make_agent, parallel, expected):
    agent = make_agent(subagents=True, max_parallel_agents=parallel)
    agent.ollama = RoutingOllama(
        {
            "ALPHA": [reply("add is in math.py:1")],
            "BETA": [reply("sub is in math.py:5")],
            "where": [reply(tool_calls=two_explores()), reply("Both found.")],
        },
        delay=0.3,
    )
    agent.run_turn("where are add and sub?")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert results[0].startswith("add is in math.py:1") and results[1].startswith("sub is in math.py:5")
    assert agent.ollama.most_in_flight == expected
    assert [r.label for r in agent.agent_runs] == ["find add", "find sub"]


def test_prompts_from_parallel_agents_are_asked_in_the_main_thread(make_agent, repo, monkeypatch):
    agent = make_agent(mode="ask", subagents=True, max_parallel_agents=2)
    agent.ollama = RoutingOllama(
        {
            "ALPHA": [reply("add is in math.py:1")],
            "BETA": [reply(tool_calls=[tool("bash", command="touch made-by-beta")]), reply("Made it.")],
            "go": [
                reply(
                    tool_calls=[
                        delegate("explore", "ALPHA: find add", "find add"),
                        delegate("worker", "BETA: create made-by-beta", "make a file"),
                    ]
                ),
                reply("Done."),
            ],
        }
    )
    asked = []

    def answer(prompt=""):
        asked.append(threading.current_thread() is threading.main_thread())
        return "y"

    monkeypatch.setattr("builtins.input", answer)
    agent.run_turn("go")
    assert asked == [True]
    assert (repo / "made-by-beta").exists()  # one editing agent: it works in the real tree


@needs_git
def test_parallel_workers_get_worktrees_and_bring_back_diffs(make_agent, repo):
    commit_all(repo)
    (repo / "notes.txt").write_text("uncommitted\n")  # the workers start from the current state
    agent = make_agent(mode="auto-edit", subagents=True, max_parallel_agents=2)
    agent.ollama = RoutingOllama(
        {
            "ALPHA": [
                reply(tool_calls=[tool("bash", command="cat notes.txt")]),
                reply(tool_calls=[tool("write_file", path="alpha.txt", content="a\n")]),
                reply("Wrote alpha.txt."),
            ],
            "BETA": [reply(tool_calls=[tool("write_file", path="beta.txt", content="b\n")]), reply("Wrote beta.txt.")],
            "go": [
                reply(
                    tool_calls=[
                        delegate("worker", "ALPHA: write alpha.txt", "alpha"),
                        delegate("worker", "BETA: write beta.txt", "beta"),
                    ]
                ),
                reply("Done."),
            ],
        }
    )
    agent.run_turn("go")
    assert (repo / "alpha.txt").read_text() == "a\n" and (repo / "beta.txt").read_text() == "b\n"
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "[Its changes were applied to the working tree: alpha.txt." in results[0]
    assert "[Its changes were applied to the working tree: beta.txt." in results[1]
    alpha_bash = next(m for m in agent.agent_runs[0].messages if m["role"] == "tool")["content"]
    assert "uncommitted" in alpha_bash
    assert not list((repo / ".git" / "lcode-worktrees").glob("*"))  # cleaned up
    assert [cp.paths for cp in agent.checkpoints.items] == [
        ["alpha.txt", "beta.txt"]
    ]  # one checkpoint, so /undo takes back both


@needs_git
def test_a_workers_diff_is_not_applied_when_the_user_says_no(make_agent, repo, monkeypatch):
    commit_all(repo)
    (repo / ".lcode" / "agents").mkdir(parents=True)
    (repo / ".lcode" / "agents" / "isolated.md").write_text("---\ndescription: works apart\nisolation: worktree\n---\n")
    agent = make_agent(
        [
            reply(tool_calls=[delegate("isolated", "write x.txt", "x")]),
            reply(tool_calls=[tool("write_file", path="x.txt", content="x\n")]),
            reply("Wrote x.txt."),
            reply("ok"),
        ],
        mode="ask",
        subagents=True,
    )
    answers(monkeypatch, "y", "n not now")  # the write inside the worktree, then applying it
    agent.run_turn("go")
    assert not (repo / "x.txt").exists()
    assert (
        "[Its changes were NOT applied. The user denied this action. User says: not now]"
        in agent.messages[3]["content"]
    )
    assert "Apply the isolated agent's changes to 1 file" in output(agent)


# ----------------------------------------------------------------------------- custom agents and settings


def test_custom_agents_from_the_repository_and_the_user(repo, monkeypatch, tmp_path):
    monkeypatch.setattr("lcode.config.CONFIG_DIR", tmp_path / "config")
    user = tmp_path / "config" / "agents"
    user.mkdir(parents=True)
    (user / "reviewer.md").write_text("---\ndescription: user reviewer\n---\nReview.")
    (user / "tester.md").write_text("---\ndescription: runs tests\ntools: read_file, bash\nmax_steps: 500\n---\nTest.")
    project = repo / ".lcode" / "agents"
    project.mkdir(parents=True)
    (project / "reviewer.md").write_text(
        "---\ndescription: reviews changes\ntools: read-only\nmodel: qwen3.5:9b\n---\nReview carefully."
    )
    (project / "broken.md").write_text("no frontmatter")
    (project / "bad-tools.md").write_text("---\ndescription: x\ntools: read_file, teleport\n---\n")
    types, problems = subagents.load_types(repo)
    reviewer, tester = types["reviewer"], types["tester"]
    assert reviewer.description == "reviews changes" and reviewer.read_only and reviewer.model == "qwen3.5:9b"
    assert reviewer.instructions == "Review carefully."
    assert tester.tools == {"read_file", "bash"} and not tester.read_only and tester.max_steps == 200
    assert set(types) >= {"explore", "plan", "worker", "reviewer", "tester"}
    assert len(problems) == 2 and any("teleport" in p for p in problems)


def test_at_mentions_ask_for_an_agent(make_agent, repo):
    (repo / ".lcode" / "agents").mkdir(parents=True)
    (repo / ".lcode" / "agents" / "reviewer.md").write_text("---\ndescription: reviews\n---\n")
    agent = make_agent(subagents=True)
    text = agent.expand_mentions("@reviewer check @README.md")
    assert '[The user wants the reviewer agent for this: call the agent tool with type "reviewer".]' in text
    assert '<file path="README.md">' in text


def test_subagents_can_be_turned_off(make_agent):
    agent = make_agent(subagents=False)
    assert "agent" not in agent.tool_names()
    assert "# Subagents" not in agent.messages[0]["content"]
    on = make_agent(subagents=True, max_parallel_agents=3)
    assert "agent" in on.tool_names()
    assert "up to 3 run at once" in on.messages[0]["content"]


def test_max_parallel_agents_setting():
    assert config.coerce("max_parallel_agents", "2") == 2
    with pytest.raises(config.ConfigError):
        config.coerce("max_parallel_agents", "0")
    with pytest.raises(config.ConfigError):
        config.coerce("max_parallel_agents", "20")


def test_agents_command_lists_types_and_runs(make_agent):
    agent = make_agent(
        [
            reply(tool_calls=[delegate()]),
            reply(tool_calls=[tool("glob", pattern="*.md")]),
            reply("README.md"),
            reply("ok"),
        ],
        subagents=True,
    )
    agent.run_turn("go")
    handle_command(agent, "/agents", HW)
    text = output(agent)
    assert "explore" in text and "worker" in text and "find add" in text
    handle_command(agent, "/agents 1", HW)
    assert "task: Find where add is defined" in output(agent)
    assert "● glob" in output(agent)


def test_a_subagent_that_repeats_itself_still_gives_a_report(make_agent):
    agent = make_agent(
        [reply(tool_calls=[delegate()])]
        + [reply(tool_calls=[tool("list_dir")]) for _ in range(5)]
        + [reply("I kept listing the top level and found nothing."), reply("ok")],
        subagents=True,
    )
    agent.run_turn("go")
    report = agent.ollama.payloads[6]
    assert "You kept making the same call, so lcode stopped you." in report["messages"][-2]["content"]
    assert "tools" not in report
    assert "I kept listing the top level" in agent.messages[3]["content"] and agent.turn_status == "success"
