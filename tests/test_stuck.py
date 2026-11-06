"""When a request can't make progress: an unreachable site, a reply that goes round in circles or says
nothing, the shell leaving the project, a script written and never run. Each case comes from a real session
that spent an hour on an Android build whose downloads this machine couldn't reach."""

import os
import stat

from conftest import call, output, reply
from lcode import checks
from lcode.repeats import Circles
from lcode.unreachable import Sites, failure, outcomes, site_of

BUILD_LOG = """# Run 'git config --get remote.origin.url' ...
https://github.com/kivy/python-for-android.git
# Recommended android's NDK version by p4a is: 28c
# Android NDK is missing, downloading
# Downloading https://dl.google.com/android/repository/android-ndk-r26b-linux.zip
Traceback (most recent call last):
urllib.error.HTTPError: HTTP Error 404: Not Found

[exit code: 1]"""


def bash(command):
    return {"command": command}


# ----------------------------------------------------------------------------- unreachable sites


def test_a_failure_belongs_to_the_address_printed_before_it():
    assert outcomes("bash", bash("./build_android.sh"), BUILD_LOG) == ({"dl.google.com": "HTTP 404"}, set())


def test_a_command_naming_one_site_owns_bare_status_lines():
    head = bash('curl -sI "https://dl.google.com/android/repository/android-ndk-r30-linux.zip" | head -3')
    assert outcomes("bash", head, "HTTP/2 404 \ncontent-length: 1449\n[exit code: 0]") == (
        {"dl.google.com": "HTTP 404"},
        set(),
    )
    assert outcomes("bash", head, "HTTP/2 200 \n[exit code: 0]") == ({}, {"dl.google.com"})
    several = bash("curl -sI https://dl.google.com/a.zip; curl -sI https://github.com/b")
    assert outcomes("bash", several, "HTTP/2 404\n---\nHTTP/2 200\n[exit code: 0]") == ({}, set())


def test_a_page_from_one_site_counts_as_reached():
    page = bash("curl -s https://raw.githubusercontent.com/kivy/python-for-android/master/README.md")
    assert outcomes("bash", page, "x = 1\n" * 100 + "[exit code: 0]") == ({}, {"raw.githubusercontent.com"})
    assert outcomes("bash", page, "short\n[exit code: 0]") == ({}, set())


def test_network_errors_and_web_fetch():
    assert failure("curl: (6) Could not resolve host: dl.google.com") == "the name doesn't resolve"
    assert failure("curl: (7) Failed to connect to x port 443: Connection refused") == "connection refused"
    assert failure("Error 404 (Not Found)!!1") == "HTTP 404" and failure("all 404 tests passed") == ""
    assert failure("  Status: 404") == failure("http_code=404") == failure("HTTP status code 404") == "HTTP 404"
    loop = bash('for u in https://dl.google.com/a.zip https://dl.google.com/b.zip; do curl -o /dev/null "$u"; done')
    assert outcomes("bash", loop, "Trying: https://dl.google.com/a.zip\n  Status: 404\n") == (
        {"dl.google.com": "HTTP 404"},
        set(),
    )
    assert outcomes("bash", loop, "  Status: 200\n") == ({}, {"dl.google.com"})
    url = {"url": "https://developer.android.com/ndk/downloads"}
    assert outcomes("web_fetch", url, f"Error: {url['url']} returned HTTP 403") == (
        {"developer.android.com": "HTTP 403"},
        set(),
    )
    assert outcomes("web_fetch", url, "NDK Downloads\n...") == ({}, {"developer.android.com"})


def test_local_addresses_are_not_tracked():
    for url in (
        "http://localhost:8000/x",
        "http://127.0.0.1/x",
        "rtsp://10.0.0.5:554/cam",
        "http://192.168.1.2/",
        "http://nas/x",
        "http://printer.local/",
        "https://${HOST}/x",
    ):
        assert site_of(url) == "", url
    assert site_of("https://DL.Google.com./x") == "dl.google.com"


def test_a_site_that_never_answers_gets_a_note_then_stops():
    sites = Sites()
    verdicts = [
        sites.check("bash", bash(f'curl -sI "https://dl.google.com/android-ndk-{v}-linux.zip"'), "HTTP/2 404\n")
        for v in ("r26b", "r27", "r28c", "r29", "r30")
    ]
    assert [("stop" if v.stop else "note") if v.note else "" for v in verdicts] == ["", "", "note", "note", "stop"]
    assert "Every request to dl.google.com" in verdicts[2].note and "(3 different calls: HTTP 404)" in verdicts[2].note
    assert "HTTPS_PROXY" in verdicts[2].note and "such as CI" in verdicts[2].note


def test_a_site_that_answered_once_never_counts_as_out_of_reach():
    sites = Sites()
    sites.check("bash", bash("curl -sI https://dl.google.com/ok.zip"), "HTTP/2 200\n")
    for v in range(5):
        assert not sites.check("bash", bash(f"curl -sI https://dl.google.com/{v}.zip"), "HTTP/2 404\n").note


def test_the_same_failing_call_counts_once_and_a_new_request_starts_over():
    sites = Sites()
    same = ("bash", bash("curl -sI https://dl.google.com/a.zip"), "HTTP/2 404\n")
    assert not any(sites.check(*same).note for _ in range(5))
    sites.check("bash", bash("curl -sI https://dl.google.com/b.zip"), "HTTP/2 404\n")
    sites.reset()
    assert not sites.check("bash", bash("curl -sI https://dl.google.com/c.zip"), "HTTP/2 404\n").note


def test_the_agent_stops_a_request_whose_site_cannot_be_reached(make_agent):
    tries = [
        reply(tool_calls=[call("bash", command=f"echo https://dl.example.com/ndk-{v}.zip; echo 'HTTP Error 404'")])
        for v in range(5)
    ]
    agent = make_agent([*tries, reply("never asked")])
    agent.run_turn("Download the NDK")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert len(results) == 5 and "Every request to dl.example.com" in results[2]
    assert "so lcode stopped the request here" in results[4] and agent.turn_status == "blocked"
    text = " ".join(output(agent).split())
    assert "Stopped: every request to dl.example.com failed (5 calls: HTTP 404)" in text
    assert agent.ollama.scripts == [reply("never asked")]


# ----------------------------------------------------------------------------- replies that go nowhere

LOOP = (
    "Let me try to check the actual download page for the correct URL.\n\n"
    "Let me check if there's a way to get the NDK from the Android SDK Manager.\n\n"
    "Actually, let me try a completely different approach with the android.ndk_path option.\n\n"
)


def test_circles_are_noticed_and_varied_thinking_is_not():
    circles = Circles()
    assert not circles.feed("I need to read the build script first, then the spec.\n")
    assert any(circles.feed(LOOP) for _ in range(12))
    varied = Circles()
    text = "".join(f"Step {n}: read file number {n} and compare it with the spec.\n" for n in range(200))
    assert not any(varied.feed(text[i : i + 9]) for i in range(0, len(text), 9))
    short = Circles()
    assert not any(short.feed("Wait.\nHmm.\nOK.\n") for _ in range(100))


def streamed(chunks, closed):
    def stream():
        try:
            yield from chunks
        finally:
            closed.append(True)

    return stream()


def test_a_reply_going_round_in_circles_is_stopped_and_the_model_asked_again(make_agent):
    looping = [{"message": {"thinking": LOOP}, "done": False} for _ in range(200)]
    closed = []
    agent = make_agent([None, reply("The NDK downloads fail from this network: use a VPN.")])
    agent.ollama.scripts[0] = streamed(looping, closed)
    agent.run_turn("Build the APK")
    assert closed == [True] and agent.turn_status == "success"
    assert not any(m.get("thinking") for m in agent.messages)  # the circles don't stay in the context
    assert "went round in circles" in agent.messages[-2]["content"]
    assert "thinking is off for this reply" in agent.messages[-2]["content"]
    assert [p["think"] for p in agent.ollama.payloads] == [True, False]  # asked again, it can't deliberate
    assert agent.messages[-1]["content"].startswith("The NDK downloads fail")
    text = " ".join(output(agent).split())
    assert "the reply went round in circles, so lcode stopped it: asking for a short answer, with thinking off" in text


def test_an_empty_or_cut_off_reply_gets_one_more_chance_then_the_request_stops(make_agent):
    cut = [{"message": {"thinking": "Hmm, let me think about the URL."}, "done": False}]
    cut.append({"message": {}, "done": True, "done_reason": "length"})
    agent = make_agent([cut, reply("Done: the build works.")])
    agent.run_turn("Build it")
    assert "filled the rest of the context" in agent.messages[-2]["content"] and agent.turn_status == "success"
    agent = make_agent([reply(""), reply("  ")])
    agent.run_turn("Build it")
    assert agent.turn_status == "loop" and [p["think"] for p in agent.ollama.payloads] == [True, False]
    assert "ended without an answer, twice in a row" in " ".join(output(agent).split())


def test_thinking_comes_back_once_the_model_acts_and_so_does_its_second_chance(make_agent):
    """After a reply that goes nowhere, the next one runs without thinking. Once the model acts, thinking is back
    on, and a later reply that goes nowhere gets its own second chance: on the dev slice, 7 attempts that had
    recovered and worked on were stopped by a second stall much later (#241)."""
    looping = [{"message": {"thinking": LOOP}, "done": False} for _ in range(200)]
    agent = make_agent(
        [
            None,  # goes round in circles
            reply(tool_calls=[call("bash", command="echo built")]),  # asked without thinking: it acts
            reply(""),  # later, an empty reply
            reply(tool_calls=[call("bash", command="echo tested")]),
            reply("Built and tested."),
        ]
    )
    agent.ollama.scripts[0] = streamed(looping, [])
    agent.run_turn("Build and test it")
    assert agent.turn_status == "success" and agent.messages[-1]["content"] == "Built and tested."
    assert [p["think"] for p in agent.ollama.payloads] == [True, False, True, False, True]


def test_four_replies_that_go_nowhere_stop_the_request_even_with_work_in_between(make_agent):
    work = [reply(tool_calls=[call("bash", command=f"echo step {n}")]) for n in range(3)]
    agent = make_agent([reply(""), work[0], reply(""), work[1], reply(""), work[2], reply(""), reply("never asked")])
    agent.run_turn("Build it")
    assert agent.turn_status == "loop" and agent.ollama.scripts == [reply("never asked")]
    assert "ended without an answer, 4 times in this request" in " ".join(output(agent).split())


def test_other_servers_keep_thinking_on_for_the_second_chance(make_agent):
    """Only Ollama's API has a switch for one reply without thinking. Other servers take a chat template argument
    instead, which hosted APIs refuse, so lcode doesn't send one there."""
    agent = make_agent([reply(""), reply("Done.")])
    agent.ollama.kind = "openai"
    agent.run_turn("Build it")
    assert agent.turn_status == "success" and [p["think"] for p in agent.ollama.payloads] == [True, True]
    assert "thinking is off" not in agent.messages[-2]["content"] and "with thinking off" not in output(agent)


def test_with_thinking_off_the_second_chance_says_nothing_about_it(make_agent):
    agent = make_agent([reply(""), reply("Done.")], think=False)
    agent.run_turn("Build it")
    assert agent.turn_status == "success" and [p["think"] for p in agent.ollama.payloads] == [False, False]
    assert "thinking is off" not in agent.messages[-2]["content"] and "with thinking off" not in output(agent)


def test_an_answer_cut_off_by_the_context_says_so(make_agent):
    cut = [{"message": {"content": "The build fails because"}, "done": False}]
    cut.append({"message": {}, "done": True, "done_reason": "length"})
    agent = make_agent([cut])
    agent.run_turn("Why does it fail?")
    assert "The answer was cut off: the context window is full" in " ".join(output(agent).split())


# ----------------------------------------------------------------------------- the shell leaving the project


def test_the_shell_can_move_around_the_project_but_not_out_of_it(agent, repo, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    out = agent.tools.run("bash", {"command": f"cd {elsewhere} && pwd"})
    assert agent.cwd == repo and f"the working directory stays {repo}" in out
    assert agent.tools.run("bash", {"command": "pwd"}).startswith(str(repo))
    agent.tools.run("bash", {"command": "cd src && pwd"})
    assert agent.cwd == repo / "src"
    agent.messages.append({"role": "user", "content": "hi"})
    agent.save()
    from lcode import sessions

    assert [s.id for s in sessions.list_sessions(repo)] == [agent.session_id]


# ----------------------------------------------------------------------------- scripts written, never run


def test_runs_tells_running_a_script_from_touching_it():
    for command in (
        "./build.sh",
        "cd x && bash -n build.sh",
        "FOO=1 sudo scripts/build.sh release",
        "timeout 60 sh build.sh",
    ):
        assert checks.runs(command, "build.sh"), command
    for command in ("chmod +x build.sh", "cat build.sh | head", "git add build.sh", "ls -la build.sh; echo ok"):
        assert not checks.runs(command, "build.sh"), command


def test_only_shell_scripts_count(repo):
    (repo / "build.sh").write_text("echo hi\n")
    (repo / "deploy").write_text("#!/usr/bin/env bash\necho hi\n")
    (repo / "main.py").write_text("#!/usr/bin/env python3\nprint(1)\n")
    changed = ["build.sh", "deploy", "main.py", "gone.sh"]
    assert checks.unrun_scripts(repo, changed, ["chmod +x build.sh deploy"]) == ["build.sh", "deploy"]
    assert checks.unrun_scripts(repo, changed, ["./build.sh", "bash deploy"]) == []


def test_a_script_written_and_never_run_is_sent_back_once(make_agent, repo):
    write = call("write_file", path="build_android.sh", content="#!/usr/bin/env bash\necho built\n")
    run = call("bash", command="chmod +x build_android.sh && ./build_android.sh")
    agent = make_agent(
        [
            reply(tool_calls=[write, call("bash", command="chmod +x build_android.sh")]),
            reply("All done: run ./build_android.sh."),
            reply(tool_calls=[run]),
            reply("It runs: it printed 'built'."),
        ],
        checks="auto",
    )
    agent.run_turn("Write a build script")
    notes = [m["content"] for m in agent.messages if m["role"] == "user" and "never ran it" in m["content"]]
    assert len(notes) == 1 and "build_android.sh" in notes[0]
    assert agent.messages[-1]["content"] == "It runs: it printed 'built'." and not agent.ollama.scripts
    assert os.stat(repo / "build_android.sh").st_mode & stat.S_IXUSR
    agent = make_agent([reply(tool_calls=[write]), reply("Done.")], checks="off")
    agent.run_turn("Write a build script")
    assert agent.messages[-1]["content"] == "Done."


# ----------------------------------------------------------------------------- code written, never run (#242)

GEOMETRY = "def area(w, h):\n    return w * h\n"
SYNTAX = "python3 -c \"import ast; ast.parse(open('geometry.py').read()); print('Syntax OK')\""


def untested_notes(agent) -> list[str]:
    return [m["content"] for m in agent.messages if m["role"] == "user" and "haven't run " in m["content"]]


def test_what_counts_as_running_new_code():
    files = ["geometry.py", "pkg/shapes.py"]
    ran = "6\n\n[exit code: 0]"
    assert checks.exercised("python3 -c 'import geometry; print(geometry.area(2, 3))'", ran, files) == ["geometry.py"]
    assert checks.exercised("cd /app && python3 -m pkg.shapes", ran, files) == ["pkg/shapes.py"]
    assert checks.exercised("cd /app && python3 -m pytest -q", ran, files) == files  # tests run what they test
    assert checks.exercised(SYNTAX, "Syntax OK\n\n[exit code: 0]", files) == []  # the syntax alone
    assert checks.exercised("ruff check geometry.py && mypy geometry.py", ran, files) == []  # lint and types
    assert checks.exercised("cat geometry.py", ran, files) == []  # it only looks
    assert checks.exercised("python3 other.py", ran, files) == []
    missing = "bash: line 1: python3: command not found\n\n[exit code: 127]"
    assert checks.exercised("python3 geometry.py", missing, files) == []
    hidden = "bash: line 1: python3: command not found\nno python\n\n[exit code: 0]"
    assert checks.exercised("python3 geometry.py || echo no python", hidden, files) == []
    no_torch = "ModuleNotFoundError: No module named 'torch'\n\n[exit code: 1]"
    assert checks.exercised("python3 -c 'import geometry'", no_torch, files) == []
    assert checks.not_found(missing + "\n/bin/sh: 1: node: not found\n") == ["python3", "node"]
    assert all(checks.is_code(f) for f in ("geometry.py", "src/Main.JAVA", "query.sparql", "ask.rq"))
    assert not any(checks.is_code(f) for f in ("build.sh", "notes.md", "meeting.ics", "schema.sql"))


def test_a_query_written_and_never_run_is_sent_back_once(make_agent, repo, monkeypatch):
    """2 of 3 sparql-university attempts on 0.61.0 wrote solution.sparql and said they were done without running it."""
    monkeypatch.setattr(checks, "uv_available", lambda: True)
    agent = make_agent(
        [
            reply(tool_calls=[call("write_file", path="solution.sparql", content="SELECT ?s WHERE { ?s ?p ?o }\n")]),
            reply("Done: solution.sparql has the query."),
            reply(tool_calls=[call("bash", command="uv run --with rdflib python -c 'open(\"solution.sparql\")'")]),
            reply("It runs, and returns 3 rows."),
        ],
        checks="auto",
    )
    agent.run_turn("write the query")
    notes = untested_notes(agent)
    assert len(notes) == 1 and "You wrote solution.sparql" in notes[0] and "for a query, run it on its data" in notes[0]
    assert "the package is `rdflib`" in notes[0]
    assert agent.messages[-1]["content"] == "It runs, and returns 3 rows."


def test_code_written_and_never_run_is_sent_back_once(make_agent, repo, monkeypatch):
    """On the dev slice, 5 of 6 torch-tensor-parallelism attempts wrote the module, found no python3 or checked only
    its syntax, and said they were done: each had a runtime error that one run would have shown (#242)."""
    monkeypatch.setattr(checks, "uv_available", lambda: True)
    agent = make_agent(
        [
            reply(tool_calls=[call("write_file", path="geometry.py", content=GEOMETRY)]),
            reply(tool_calls=[call("bash", command=SYNTAX)]),
            reply("Done: geometry.py has area()."),
            reply(tool_calls=[call("bash", command="python3 -c 'import geometry; print(geometry.area(2, 3))'")]),
            reply("It works: area(2, 3) is 6."),
        ],
        checks="auto",
    )
    agent.run_turn("Write a geometry module")
    [note] = untested_notes(agent)
    assert note.startswith("[lcode] You wrote geometry.py in this request but haven't run it since your last change")
    assert "A syntax check doesn't show that it works" in note and "uv run --with PACKAGE python FILE" in note
    assert agent.messages[-1]["content"] == "It works: area(2, 3) is 6." and not agent.ollama.scripts
    assert "never ran geometry.py after writing it: asking the model to" in " ".join(output(agent).split())


def test_a_program_that_isnt_installed_doesnt_count_as_a_run(make_agent, repo, monkeypatch):
    monkeypatch.setattr(checks, "uv_available", lambda: False)
    agent = make_agent(
        [
            reply(tool_calls=[call("write_file", path="geometry.py", content=GEOMETRY)]),
            reply(tool_calls=[call("bash", command="no-such-python -c 'import geometry'")]),
            reply("Done."),
            reply("It's untested: there's no Python here."),
        ],
        checks="auto",
    )
    agent.run_turn("Write a geometry module")
    [note] = untested_notes(agent)
    assert "`no-such-python` isn't installed here" in note and "uv run" not in note
    assert agent.messages[-1]["content"] == "It's untested: there's no Python here." and not agent.ollama.scripts


def test_a_command_that_was_denied_didnt_run_the_code(make_agent, repo):
    from lcode.hooks import Rules

    agent = make_agent(
        [
            reply(tool_calls=[call("write_file", path="geometry.py", content=GEOMETRY)]),
            reply(tool_calls=[call("bash", command="python3 geometry.py")]),
            reply("Done."),
            reply("It's untested: running Python isn't allowed here."),
        ],
        checks="auto",
    )
    agent.perms.rules = Rules(deny=[("bash:python3*", "a test")])
    agent.run_turn("Write a geometry module")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert results[1].startswith("A deny rule blocks this") and len(untested_notes(agent)) == 1
    assert agent.messages[-1]["content"] == "It's untested: running Python isn't allowed here."


def test_edits_files_that_dont_run_windows_and_project_checks_get_no_such_note(make_agent, repo):
    read = call("read_file", path="src/pkg/math.py")
    edit = call("edit_file", path="src/pkg/math.py", old_string="return a - b", new_string="return a - b  # sub")
    notes = call("write_file", path="NOTES.md", content="# Notes\n")
    agent = make_agent([reply(tool_calls=[read, edit, notes]), reply("Done.")], checks="auto")
    agent.run_turn("Comment sub")
    assert (repo / "src/pkg/math.py").read_text().count("# sub") == 1  # the edit happened
    assert agent.messages[-1]["content"] == "Done." and not untested_notes(agent)
    window = call("write_file", path="viewer.py", content="import tkinter\n\ntkinter.Tk().mainloop()\n")
    agent = make_agent([reply(tool_calls=[window]), reply("Done."), reply("Done, really.")], checks="auto")
    agent.run_turn("Write a viewer")
    assert not untested_notes(agent) and agent.messages[-1]["content"] == "Done, really."  # the window check asked
    new = call("write_file", path="geometry.py", content=GEOMETRY)
    for setting in ("true", "off"):  # the project's checks run instead, or the checks are off
        (repo / "geometry.py").unlink(missing_ok=True)
        agent = make_agent([reply(tool_calls=[new]), reply("Done.")], checks=setting)
        agent.run_turn("Write a geometry module")
        assert agent.messages[-1]["content"] == "Done." and not untested_notes(agent)


def test_a_dropped_reply_is_kept_where_the_user_can_read_it(make_agent):
    from lcode import context

    looping = [{"message": {"thinking": LOOP}, "done": False} for _ in range(200)]
    agent = make_agent([None, reply("Done.")])
    agent.ollama.scripts[0] = streamed(looping, [])
    agent.run_turn("Build the APK")
    [saved] = context.outputs_dir().glob("*dropped-reply.txt")
    assert "Let me try to check the actual download page" in saved.read_text()
    shown = "".join(output(agent).split())  # a long path may be wrapped on screen
    assert "thedroppedreplyisin" in shown and saved.name in shown
