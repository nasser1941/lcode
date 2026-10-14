"""Steer while it works (#115): notes typed during a request reach the model at its next step; Esc pauses."""

import os
import select
import sys
import time

import pytest

from conftest import call, output, reply
from lcode import context, review, steer
from lcode.agent import Progress
from lcode.steer import NOTE_INTRO, KeyReader, Steering, incomplete_escape


def text(agent):
    return " ".join(output(agent).split())


def during(agent, step, action):
    """Run `action` while the model streams its `step`-th response (1-based), as if the user typed then.

    Returns the messages sent at each step as they were then (the agent's own list keeps growing).
    """
    stream, sent = agent.ollama.chat_stream, []

    def chat_stream(payload, **kwargs):
        sent.append([dict(m) for m in payload["messages"]])
        if len(sent) == step:
            action()
        yield from stream(payload, **kwargs)

    agent.ollama.chat_stream = chat_stream
    return sent


def steered(agent):
    agent.steering = Steering()
    return agent.steering


# Keystrokes -----------------------------------------------------------------------------------------------


def test_a_typed_line_becomes_a_note():
    s = Steering()
    s.feed("use tabs")
    assert s.typed == "use tabs" and not s.notes
    s.feed("\r")
    assert s.notes == ["use tabs"] and s.typed == ""
    s.feed("   \r\n")  # nothing typed: no note
    assert s.notes == ["use tabs"]


def test_editing_keys():
    s = Steering()
    s.feed("helo\x7f\x7fllo there")
    assert s.typed == "hello there"
    s.feed("\x17")  # Ctrl+W: the last word
    assert s.typed == "hello "
    s.feed("\x15")  # Ctrl+U: the whole line
    assert s.typed == ""
    s.feed("a\tb\x01\x04c")  # a tab is a space; other control keys do nothing
    assert s.typed == "a bc"


def test_esc_pauses_and_arrow_keys_are_ignored():
    s = Steering()
    s.feed("ab\x1b[D\x1b[1;5C\x1bOPc")  # left, Ctrl+right, F1
    assert s.typed == "abc" and not s.pause
    s.feed("\x1b")
    assert s.pause
    s.feed("\x1b")  # again: no pause after all
    assert not s.pause
    s.feed("\x1b")
    assert s.take_pause() and not s.pause and not s.take_pause()


def test_alt_keys_and_terminal_replies_are_not_esc():
    s = Steering()
    s.feed("a\x1bbc")  # Alt+B (a word back, in many shells)
    assert s.typed == "ac" and not s.pause
    s.feed("\x1b]11;rgb:0000/0000/0000\x07d")  # a terminal's answer to a colour query (OSC)
    s.feed("\x1bP1$r0m\x1b\\e")  # a DCS answer, ended by ST
    s.feed("\x1b[12;1R")  # a cursor position report
    assert s.typed == "acde" and not s.pause
    s.feed("\x1b\x1b")  # Esc pressed twice, quickly: no pause after all
    assert not s.pause


def test_a_long_note_is_capped():
    s = Steering()
    s.feed("x" * (steer.MAX_NOTE + 50) + "\r")
    assert len(s.notes[0]) == steer.MAX_NOTE


def test_incomplete_escape():
    assert incomplete_escape(b"abc\x1b")
    assert incomplete_escape(b"\x1b[1;5")
    assert incomplete_escape(b"\x1bO")
    assert not incomplete_escape(b"\x1b[A")
    assert not incomplete_escape(b"\x1bOP")
    assert not incomplete_escape(b"plain")
    assert incomplete_escape(b"\x1b]11;rgb:00")
    assert not incomplete_escape(b"\x1b]11;rgb:0000/0000/0000\x07")
    assert not incomplete_escape(b"\x1bP1$r0m\x1b\\")


def test_status_line():
    s = Steering()
    assert s.status() is None
    s.feed("fix the te")
    assert "✎ fix the te▏" in s.status().plain and "Enter adds it as a note" in s.status().plain
    s.feed("sts\r")
    assert "a note waiting for the model's next step" in s.status().plain
    s.feed("and the docs\r\x1b")
    assert "2 notes waiting" in s.status().plain and "Pausing after this step" in s.status().plain
    s.take()
    s.feed("y" * 200)
    assert s.status().plain.count("y") == steer.SHOWN and "…" in s.status().plain


def test_leftover_goes_back_to_the_prompt():
    s = Steering()
    s.feed("first\rsecond, unfinished")
    assert s.leftover() == "first\nsecond, unfinished"
    assert s.leftover() == "" and not s.notes and not s.typed


def test_an_enter_right_as_a_question_appears_is_not_an_answer(monkeypatch):
    monkeypatch.setattr(steer, "TOO_QUICK", 0.05)
    answers = iter([("", 0), ("", 0.08)])  # an instant Enter (meant for a note), then a considered one
    asked = []

    def read_line(question):
        asked.append(question)
        answer, wait = next(answers)
        time.sleep(wait)
        return answer

    s = Steering()
    s.read_line = read_line
    assert s.ask("Allow? ") == "" and len(asked) == 2
    s.read_line = lambda q: "n, not that file"
    assert s.ask("Allow? ") == "n, not that file"  # a typed answer counts however fast it comes


def test_the_status_line_under_the_spinner():
    plain = Progress().__rich__().plain
    assert "Ctrl+C to stop" in plain and "Esc" not in plain
    s = Steering()
    progress = Progress("Running the command", s, tokens=False)
    assert "type a note · Esc pauses · Ctrl+C stops" in progress.__rich__().plain
    assert "tokens" not in progress.__rich__().plain
    s.feed("wait")
    assert progress.__rich__().plain.endswith("✎ wait▏  Enter adds it as a note")


# The agent -------------------------------------------------------------------------------------------------


def test_a_note_reaches_the_model_at_its_next_step(make_agent):
    agent = make_agent([reply(tool_calls=[call("read_file", path="README.md")]), reply("Done.")])
    s = steered(agent)
    sent = during(agent, 1, lambda: s.feed("also check the license\r"))
    agent.run_turn("look at the readme")
    assert sent[1][-1] == {"role": "user", "content": "also check the license\n\n" + NOTE_INTRO}
    assert sent[1][-2]["role"] == "tool"  # after the step's results
    assert "✎ Note for the model: also check the license" in text(agent)
    assert agent.turn_requests == ["look at the readme", "also check the license"]
    # a note is the user's own words: it's kept with the requests when old messages are summarized
    assert context.user_requests(agent.messages) == ["look at the readme", "also check the license"]


def test_a_note_typed_during_the_answer_is_read_before_the_request_ends(make_agent):
    agent = make_agent([reply("All done."), reply("Also renamed it, as you asked.")])
    s = steered(agent)
    sent = during(agent, 1, lambda: s.feed("and rename it\r"))
    agent.run_turn("fix it")
    assert len(sent) == 2
    assert sent[1][-2] == {"role": "assistant", "content": "All done."}
    assert sent[1][-1]["content"].startswith("and rename it\n\n")
    assert agent.messages[-1]["content"] == "Also renamed it, as you asked." and agent.turn_status == "success"


def test_several_notes_go_together(make_agent):
    agent = make_agent([reply(tool_calls=[call("read_file", path="README.md")]), reply("Done.")])
    s = steered(agent)
    sent = during(agent, 1, lambda: s.feed("one\rtwo\r"))
    agent.run_turn("go")
    assert sent[1][-1]["content"] == "one\n\ntwo\n\n" + NOTE_INTRO


def test_without_steering_nothing_changes(make_agent):
    agent = make_agent([reply(tool_calls=[call("read_file", path="README.md")]), reply("Done.")])
    sent = during(agent, 1, lambda: None)
    agent.run_turn("go")
    assert sent[1][-1]["role"] == "tool"


def test_esc_pauses_after_the_step(make_agent):
    agent = make_agent([reply(tool_calls=[call("read_file", path="README.md")]), reply("Done, with tabs.")])
    s = steered(agent)
    questions = []
    s.read_line = lambda q: questions.append(q) or "use tabs"
    sent = during(agent, 1, lambda: s.feed("\x1b"))
    agent.run_turn("go")
    assert questions == ["  ⏸ "]
    assert "⏸ Paused. Press Enter to go on" in text(agent)
    assert sent[1][-1]["content"].startswith("use tabs\n\n")
    assert sent[1][-2]["role"] == "tool"  # the step finished first


def test_enter_at_the_pause_just_goes_on(make_agent):
    agent = make_agent([reply(tool_calls=[call("read_file", path="README.md")]), reply("Done.")])
    s = steered(agent)
    s.read_line = lambda q: ""
    sent = during(agent, 1, lambda: s.feed("\x1b"))
    agent.run_turn("go")
    assert sent[1][-1]["role"] == "tool"
    assert agent.messages[-1]["content"] == "Done."


def test_ctrl_c_at_the_pause_stops_the_request(make_agent):
    agent = make_agent([reply(tool_calls=[call("read_file", path="README.md")]), reply("never")])
    s = steered(agent)

    def interrupt(question):
        raise KeyboardInterrupt

    s.read_line = interrupt
    during(agent, 1, lambda: s.feed("\x1b"))
    with pytest.raises(KeyboardInterrupt):
        agent.run_turn("go")
    assert len(agent.ollama.payloads) == 1


def test_the_review_judges_the_request_with_its_notes(make_agent, monkeypatch):
    agent = make_agent(
        [reply(tool_calls=[call("write_file", path="app.py", content="print('hi')\n")]), reply("Done.")],
        review="auto",
    )
    s = steered(agent)
    during(agent, 1, lambda: s.feed("make it say hi\r"))
    seen = []
    monkeypatch.setattr(review, "run", lambda agent, request, answer, diff: seen.append(request) or [])
    agent.run_turn("write app.py")
    assert seen == ["write app.py\n\nWhat the user added while you worked:\n- make it say hi"]


def test_permission_questions_use_the_steering_reader(make_agent):
    agent = make_agent([reply(tool_calls=[call("write_file", path="x.txt", content="x")]), reply("ok")], mode="ask")
    asked = []
    agent.perms.read_line = lambda q: asked.append(q) or "y"
    agent.run_turn("write x.txt")
    assert len(asked) == 1 and "Allow?" in asked[0]
    assert (agent.cwd / "x.txt").read_text() == "x"


# The REPL ---------------------------------------------------------------------------------------------------


def test_without_a_terminal_requests_run_as_before(make_agent):
    from lcode import repl

    agent = make_agent([reply("hi")])
    repl.run_safely(agent, "hello")  # stdin isn't a terminal under pytest
    assert agent.steering is None and agent.perms.read_line is None


def test_what_never_reached_the_model_goes_back_to_the_prompt(make_agent):
    from lcode import repl

    agent = make_agent()
    reader = KeyReader(Steering())
    reader.steering.feed("note one\rhalf typed")
    agent.steering = reader.steering
    repl.stop_steering(agent, reader)
    assert agent.unsent == "note one\nhalf typed" and agent.steering is None
    assert "didn't reach the model; it's in the prompt below" in text(agent)


# A real terminal ---------------------------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="no termios")
def test_keys_from_a_real_terminal():
    import termios

    master, slave = os.openpty()
    before = termios.tcgetattr(slave)
    s = Steering()
    reader = KeyReader(s, fd=slave)
    try:
        assert reader.start()
        assert not termios.tcgetattr(slave)[3] & termios.ECHO  # what's typed isn't echoed over the output
        assert termios.tcgetattr(slave)[3] & termios.ISIG  # Ctrl+C still interrupts
        os.write(master, "fix the tést\r".encode())
        wait_for(lambda: s.notes == ["fix the tést"])
        os.write(master, b"\x1b[A")  # an arrow key isn't Esc
        time.sleep(0.2)
        assert not s.pause
        os.write(master, b"\x1b")
        wait_for(lambda: s.pause)

        with reader.suspended():  # a question: the keyboard is back in its usual mode
            mode = termios.tcgetattr(slave)
            assert mode[3] & termios.ECHO and mode[3] & termios.ICANON
            os.write(master, b"y\n")
            assert select.select([slave], [], [], 3)[0]
            assert os.read(slave, 100) == b"y\n"  # the answer goes to the question, not into a note
        assert s.typed == "" and s.notes == ["fix the tést"]

        os.write(master, b"more\r")  # and reading goes on afterwards
        wait_for(lambda: s.notes == ["fix the tést", "more"])
        assert not termios.tcgetattr(slave)[3] & termios.ECHO
        reader.stop()
        assert same_mode(termios.tcgetattr(slave), before)  # the terminal is as it was
        assert not reader.thread.is_alive()
    finally:
        reader.stop()  # stopping twice is harmless
        os.close(master)
        os.close(slave)


def same_mode(a, b):
    """Terminal modes, ignoring PENDIN: macOS sets it itself when line editing comes back with input pending."""
    import termios

    pendin = getattr(termios, "PENDIN", 0)
    return [*a[:3], a[3] & ~pendin, *a[4:]] == [*b[:3], b[3] & ~pendin, *b[4:]]


def test_not_a_terminal():
    r, w = os.pipe()
    try:
        assert not KeyReader(Steering(), fd=r).start()
    finally:
        os.close(r)
        os.close(w)


def wait_for(condition, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_spinner_with_a_command_running(make_agent):
    agent = make_agent([reply(tool_calls=[call("bash", command="echo one; echo two")]), reply("ok")])
    agent.steering = Steering()
    agent.run_turn("run it")
    assert "one" in output(agent) and "two" in output(agent)


def test_the_recap_shows_the_users_words(make_agent):
    from lcode import repl

    agent = make_agent()
    agent.messages += [
        {"role": "user", "content": "add a --limit option"},
        {"role": "assistant", "content": "", "tool_calls": [call("read_file", path="README.md")]},
        {"role": "user", "content": "also add --min-count\n\n" + NOTE_INTRO},
        {"role": "user", "content": "[lcode] The project's checks (`pytest`) failed after your change: …"},
        {"role": "assistant", "content": "Both options are in, and the tests pass."},
    ]
    repl.print_recap(agent)
    shown = text(agent)
    assert "You: also add --min-count │" in shown and "[lcode]" not in shown
