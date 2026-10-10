from conftest import call, output, reply
from lcode.repeats import Repeats

GREP = {"pattern": "size_panels", "path": "."}


def counts(repeats, *calls):
    """(note given, stop) for each (name, args, result)."""
    out = []
    for name, args, result in calls:
        verdict = repeats.check(name, args, result)
        out.append(("stop" if verdict.stop else "note") if verdict.note else "")
    return out


def test_the_same_call_and_result_gets_a_note_then_stops():
    repeats = Repeats()
    same = ("grep", GREP, "camera_wall.py:350: def size_panels")
    assert counts(repeats, *[same] * 5) == ["", "", "note", "note", "stop"]
    verdict = Repeats().check(*same)
    assert verdict.count == 1 and not verdict.note


def test_a_failed_call_is_noted_sooner():
    repeats = Repeats()
    edit = ("edit_file", {"path": "a.py", "old_string": "x", "new_string": "y"}, "Error: old_string not found")
    assert counts(repeats, *[edit] * 4) == ["", "note", "note", "stop"]
    repeats = Repeats()
    repeats.check(*edit)
    assert "read the file again and copy the text exactly" in repeats.check(*edit).note


def test_results_that_change_are_not_repeats():
    repeats = Repeats()
    runs = [("bash", {"command": "pytest -q"}, f"{n} failed\n[exit code: 1]") for n in (3, 2, 1, 1)]
    assert counts(repeats, *runs) == ["", "", "", ""]


def test_a_change_starts_the_count_again_for_other_calls():
    repeats = Repeats()
    check = ("bash", {"command": "python -m py_compile app.py && echo OK"}, "OK\n[exit code: 0]")
    for n in range(6):
        edit = ("edit_file", {"path": "app.py", "old_string": f"v{n}", "new_string": f"v{n + 1}"}, "Edited app.py")
        assert counts(repeats, edit, check) == ["", ""]
    # A read-only look in between changes nothing: the repeats still add up.
    look = ("bash", {"command": "git diff app.py | head -80"}, "diff --git a/app.py b/app.py")
    failed = ("edit_file", {"path": "app.py", "old_string": "gone", "new_string": "x"}, "Error: old_string not found")
    assert counts(repeats, failed, look, failed, look, failed, look) == ["", "", "note", "", "note", "note"]


def test_a_command_that_may_change_things_still_counts_its_own_repeats():
    repeats = Repeats()
    run = ("bash", {"command": "cd /src && python app.py"}, "Traceback: KeyError\n[exit code: 1]")
    assert counts(repeats, run, run, run) == ["", "", "note"]


def test_a_new_request_and_limit_zero():
    repeats = Repeats()
    same = ("read_file", {"path": "a.py"}, "1\tx")
    counts(repeats, same, same)
    repeats.reset()
    assert counts(repeats, same, same) == ["", ""]
    assert counts(Repeats(0), *[same] * 10) == [""] * 10
    assert counts(Repeats(5), *[same] * 7) == ["", "", "", "", "note", "note", "stop"]


def test_the_agent_tells_the_model_and_then_stops(make_agent):
    look = call("bash", command="git diff README.md | grep -c Demo")
    agent = make_agent([reply(tool_calls=[dict(look)]) for _ in range(5)] + [reply("never reached")])
    agent.run_turn("is it clean?")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert len(results) == 5 and agent.turn_status == "loop"
    assert "[lcode] You made this exact call 3 times" in results[2] and "lcode stopped" not in results[2]
    assert "lcode stopped the request here" in results[4]
    assert len(agent.ollama.scripts) == 1  # the model wasn't asked again
    text = output(agent)
    assert "same call, same result, 3 times: told the model" in text
    assert "Stopped: the model kept making the same call with the same result: $ git diff README.md" in text


def test_calls_after_the_stop_are_not_run(make_agent, repo):
    look = call("read_file", path="README.md")
    write = {"id": "w1", **call("write_file", path="later.txt", content="x")}
    agent = make_agent([reply(tool_calls=[dict(look)]) for _ in range(4)] + [reply(tool_calls=[dict(look), write])])
    agent.run_turn("go")
    assert agent.turn_status == "loop" and not (repo / "later.txt").exists()
    skipped = agent.messages[-1]
    assert skipped["tool_call_id"] == "w1" and skipped["content"].startswith("Not run: lcode stopped")
