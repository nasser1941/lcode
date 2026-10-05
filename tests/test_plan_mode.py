import io

from rich.console import Console

from conftest import output, reply
from lcode import config, planning
from lcode.hardware import Hardware
from lcode.permissions import Permissions
from lcode.repl import handle_command

HW = Hardware("linux", "x", 31, "GPU", 12)
PLAN = (
    "1. Add `mul` to **src/pkg/math.py**\n2. Test it\n   - with negative numbers\n\n"
    "Risks: none.\nVerify: run the tests."
)


def tool(name, **arguments):
    return {"function": {"name": name, "arguments": arguments}}


def answers(monkeypatch, *replies):
    queue = list(replies)
    monkeypatch.setattr("builtins.input", lambda prompt="": queue.pop(0))


def present(title="Add mul", plan=PLAN):
    return tool("present_plan", title=title, plan=plan)


def test_plan_mode_blocks_every_change(make_agent, repo):
    agent = make_agent(
        [
            reply(
                tool_calls=[
                    tool("read_file", path="README.md"),
                    tool("bash", command="cd src && ls"),
                    tool("edit_file", path="README.md", old_string="Demo", new_string="X"),
                    tool("write_file", path="new.txt", content="x"),
                    tool("bash", command="touch made"),
                    tool("agent", type="worker", task="change things"),
                ]
            ),
            reply("Here's what I found."),
        ],
        mode="plan",
        subagents=True,
    )
    agent.run_turn("add mul")
    assert agent.messages[1]["content"].endswith(planning.NOTE)
    assert "present_plan" in {t["function"]["name"] for t in agent.ollama.payloads[0]["tools"]}
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert "# Demo" in results[0] and "pkg" in results[1]
    assert all("Plan mode is on" in r for r in results[2:]), results[2:]
    assert (repo / "README.md").read_text() == "# Demo\n"
    assert not (repo / "new.txt").exists() and not (repo / "made").exists()


def test_approving_a_plan_switches_mode_and_fills_the_todo_list(make_agent, repo, monkeypatch):
    agent = make_agent(
        [
            reply(tool_calls=[present()]),
            reply(tool_calls=[tool("write_file", path="new.txt", content="x\n")]),
            reply("Done."),
        ],
        mode="plan",
    )
    answers(monkeypatch, "a")
    agent.run_turn("add mul")
    assert agent.perms.mode == "auto-edit"
    assert "[y] run it, asking before changes · [a] run it, edits without asking" in output(agent)
    assert agent.plan.startswith("# Add mul\n\n1. Add `mul`")
    assert agent.tools.todos == [
        {"content": "Add mul to src/pkg/math.py", "status": "pending"},
        {"content": "Test it", "status": "pending"},
    ]
    assert agent.messages[3]["content"].startswith("The user approved the plan, and plan mode is off (auto-edit mode")
    assert (repo / "new.txt").read_text() == "x\n"  # carried out in the same request
    assert "present_plan" not in {t["function"]["name"] for t in agent.ollama.payloads[1]["tools"]}


def test_y_approves_in_ask_mode(make_agent, monkeypatch):
    agent = make_agent([reply(tool_calls=[present()]), reply("Starting.")], mode="plan")
    answers(monkeypatch, "")
    agent.run_turn("add mul")
    assert agent.perms.mode == "ask"
    assert "(ask mode: changes ask for approval as usual)" in agent.messages[3]["content"]


def test_a_plan_sent_back_with_feedback_stays_in_plan_mode(make_agent, monkeypatch):
    agent = make_agent([reply(tool_calls=[present()]), reply("I'll revise it.")], mode="plan")
    answers(monkeypatch, "add tests for overflow too")  # starts with "a", but isn't an approval
    agent.run_turn("add mul")
    assert agent.perms.mode == "plan" and agent.plan == ""
    assert agent.messages[3]["content"].startswith("The user wants changes to the plan: add tests for overflow too")


def test_edit_and_save_a_plan_before_approving(make_agent, repo, monkeypatch):
    agent = make_agent([reply(tool_calls=[present()]), reply("Starting.")], mode="plan")
    monkeypatch.setattr(planning, "edit_text", lambda text: text.replace("Test it", "Test it twice"))
    answers(monkeypatch, "e", "s", "y")
    agent.run_turn("add mul")
    saved = repo / ".lcode" / "plans" / "add-mul.md"
    assert "Test it twice" in saved.read_text()
    result = agent.messages[3]["content"]
    assert "The user edited the plan first" in result and "2. Test it twice" in result
    assert agent.tools.todos[1]["content"] == "Test it twice"


def test_without_a_terminal_the_plan_waits_for_review(make_agent):
    agent = make_agent([reply(tool_calls=[present()]), reply("Waiting.")], mode="plan")
    agent.interactive = False
    agent.run_turn("add mul")
    assert agent.perms.mode == "plan"
    assert "review it later" in agent.messages[3]["content"]


def test_present_plan_outside_plan_mode_is_refused(make_agent):
    agent = make_agent([reply(tool_calls=[present()]), reply("ok")], mode="ask")
    agent.run_turn("go")
    assert agent.messages[3]["content"].startswith("Error: plan mode is off")
    assert "present_plan" not in {t["function"]["name"] for t in agent.ollama.payloads[0]["tools"]}


def test_the_approved_plan_survives_compaction(make_agent):
    agent = make_agent([reply("ok"), reply("Summary.")])
    agent.run_turn("hi")
    agent.plan = "# Add mul\n\n1. Add it"
    agent.compact()
    assert agent.messages[1]["content"].endswith(
        "[The plan the user approved; keep following it]\n\n# Add mul\n\n1. Add it"
    )


def test_plan_command(make_agent, monkeypatch):
    agent = make_agent([reply(tool_calls=[present()]), reply("Starting.")], mode="ask")
    handle_command(agent, "/plan", HW)
    assert "No approved plan" in output(agent)
    answers(monkeypatch, "y")
    handle_command(agent, "/plan add mul", HW)
    assert agent.messages[1]["content"].startswith("add mul") and planning.NOTE in agent.messages[1]["content"]
    assert agent.perms.mode == "ask"
    handle_command(agent, "/plan", HW)
    assert "Approved plan" in output(agent)


def test_shift_tab_reaches_plan_mode_first():
    perms = Permissions(Console(file=io.StringIO()), "ask")
    seen = []
    for _ in range(4):
        perms.cycle()
        seen.append(perms.mode)
    assert seen == ["plan", "auto-edit", "yolo", "ask"]
    assert config.coerce("permission_mode", "plan") == "plan"


def test_steps_come_from_the_numbered_lines():
    assert planning.steps(PLAN) == ["Add mul to src/pkg/math.py", "Test it"]
    assert planning.steps("No numbers here") == []
