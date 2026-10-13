"""Self-review (#114): a change is checked against the request before the answer counts."""

import json

from conftest import call, output, reply
from lcode import api, review
from lcode.ollama import OllamaError

NEW = (
    "import sys\n\n\ndef main(argv):\n    verbose = '--verbose' in argv\n"
    "    print(sum(int(a) for a in argv if a != '--verbose'))\n"
)


def text(agent):
    return " ".join(output(agent).split())


def changing(make_agent, *after, **settings):
    return make_agent(
        [
            reply(tool_calls=[call("write_file", path="app.py", content=NEW)]),
            reply("Added --verbose and documented it."),
            *after,
        ],
        **{"review": "auto", **settings},
    )


def test_problems_go_back_to_the_model_once(make_agent):
    agent = changing(make_agent, reply("Fixed: the flag now prints each number, and README.md documents it."))
    agent.ollama.chat_replies = [
        json.dumps({"problems": ["README.md isn't changed, though the user asked to document the flag."]})
    ]
    agent.run_turn("add --verbose and document it in README.md")
    note = next(
        m["content"] for m in agent.messages if m["role"] == "user" and m["content"].startswith("[lcode] A review")
    )
    assert "- README.md isn't changed" in note and "Fix what's real" in note
    assert agent.messages[-1]["content"].startswith("Fixed:")
    assert "Review: 1 point(s) sent back to the model" in text(agent)
    assert agent.review_results == ["README.md isn't changed, though the user asked to document the flag."]
    asked = agent.ollama.chats[0]
    assert "add --verbose and document it in README.md" in asked["messages"][-1]["content"]
    assert "+    verbose = '--verbose' in argv" in asked["messages"][-1]["content"]  # the diff
    assert asked["format"] == review.SCHEMA


def test_a_clean_review_adds_no_step(make_agent):
    agent = changing(make_agent)
    agent.ollama.chat_replies = [json.dumps({"problems": []})]
    agent.run_turn("add --verbose")
    assert agent.messages[-1]["content"] == "Added --verbose and documented it."
    assert "Review: the change does what was asked." in text(agent) and agent.review_results == []


def test_no_review_without_a_change_or_when_off(make_agent):
    reading = make_agent(
        [reply(tool_calls=[call("read_file", path="README.md")]), reply("It's a demo.")], review="auto"
    )
    reading.run_turn("what is this?")
    assert reading.ollama.chats == [] and reading.review_results is None
    off = changing(make_agent, review="off")
    off.run_turn("add --verbose")
    assert off.ollama.chats == []
    planner = make_agent(mode="plan", review="auto")
    assert not planner.reviewing()


def test_a_review_that_fails_doesnt_stop_the_answer(make_agent):
    agent = changing(make_agent)

    def broken(payload):
        raise OllamaError("model crashed")

    agent.ollama.chat = broken
    agent.run_turn("add --verbose")
    assert agent.messages[-1]["content"] == "Added --verbose and documented it."
    assert "The review didn't run: model crashed" in text(agent)


def test_the_diff_covers_only_this_request(tmp_path):
    files = {"a.py": "x = 1\ny = 2\nz = 3\n", "new.py": "print('hi')\n"}
    diff, changed = review.diff_of(
        {"a.py": "x = 1\ny = 0\nz = 3\n", "new.py": None, "same.py": "q\n"}, files.get, 10_000
    )
    assert "-y = 0\n+y = 2" in diff and "+print('hi')" in diff and "same.py" not in diff and changed == 3
    cut, _ = review.diff_of({"a.py": ""}, lambda p: "x\n" * 5000, 100)
    assert cut.endswith("is cut]")


def test_programs_see_the_review(make_agent):
    result = api.Result(status="success", text="", session_id="s", model="m", review=["README.md not updated"])
    assert result.to_json()["review"] == ["README.md not updated"]
