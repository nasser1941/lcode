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
    assert agent.messages[-1]["content"].startswith("The NDK downloads fail")
    assert "the reply went round in circles, so lcode stopped it" in output(agent)


def test_an_empty_or_cut_off_reply_gets_one_more_chance_then_the_request_stops(make_agent):
    cut = [{"message": {"thinking": "Hmm, let me think about the URL."}, "done": False}]
    cut.append({"message": {}, "done": True, "done_reason": "length"})
    agent = make_agent([cut, reply("Done: the build works.")])
    agent.run_turn("Build it")
    assert "filled the rest of the context" in agent.messages[-2]["content"] and agent.turn_status == "success"
    agent = make_agent([reply(""), reply("  ")])
    agent.run_turn("Build it")
    assert agent.turn_status == "loop"
    assert "ended without an answer, twice" in " ".join(output(agent).split())


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
