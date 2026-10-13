import json

import pytest

from conftest import call, output, reply
from lcode import api, checks

PASS = "python3 -c \"print('3 tests OK')\""
FAIL = "python3 -c \"import sys; print('FAIL: test_average (expected 4, got 3)'); sys.exit(1)\""


def text(agent):
    return " ".join(output(agent).split())


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"pyproject.toml": "[tool.pytest.ini_options]\n", "uv.lock": "", "tests/test_x.py": ""}, "uv run pytest -q"),
        ({"pyproject.toml": "[project]\ndependencies = ['pytest']\n", "src/pkg/test_x.py": ""}, "-m pytest -q"),
        ({"test_calc.py": "import unittest\n"}, "-m unittest"),
        ({"tests/test_calc.py": "import unittest\n"}, "-m unittest discover -s tests -t ."),
        ({"package.json": json.dumps({"scripts": {"test": "vitest run"}})}, "npm test"),
        ({"package.json": json.dumps({"scripts": {"test": "jest"}}), "pnpm-lock.yaml": ""}, "pnpm test"),
        ({"Cargo.toml": "[package]\n"}, "cargo test"),
        ({"go.mod": "module x\n"}, "go test ./..."),
        ({"Makefile": "build:\n\tcc x.c\ntest:\n\t./run\n"}, "make test"),
    ],
)
def test_the_check_is_found_from_the_project(tmp_path, files, expected):
    for name, content in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(content)
    assert checks.detect(tmp_path).command.endswith(expected)


def test_no_check_when_theres_nothing_to_run(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": 'echo "Error: no test specified"'}}))
    (tmp_path / "README.md").write_text("# x\n")
    assert checks.detect(tmp_path) is None
    assert checks.find(tmp_path, "off", trusted=True) is None


def test_configured_checks_win(tmp_path):
    (tmp_path / "test_x.py").write_text("")
    (tmp_path / ".lcode").mkdir()
    (tmp_path / ".lcode" / "settings.toml").write_text('[checks]\ncommand = "make check"\n')
    assert checks.find(tmp_path, "auto", trusted=True).command == "make check"
    assert checks.find(tmp_path, "auto", trusted=False).command.endswith("-m unittest")  # not trusted: ignored
    assert checks.find(tmp_path, "tox -q", trusted=False).command == "tox -q"


def test_checks_run_after_a_change_and_pass(make_agent):
    agent = make_agent(
        [reply(tool_calls=[call("write_file", path="notes.md", content="hi\n")]), reply("Done.")], checks=PASS
    )
    agent.run_turn("write notes")
    assert "◆ Checking the work: python3 -c" in text(agent) and "◆ Checks pass" in text(agent)
    assert agent.check_results == [{"command": PASS, "exit_code": 0, "by": "lcode"}]
    assert agent.messages[-1]["content"] == "Done."  # no extra step


def test_failing_checks_go_back_to_the_model_twice_at_most(make_agent):
    agent = make_agent(
        [
            reply(tool_calls=[call("write_file", path="calc.py", content="x = 1\n")]),
            reply("Done."),
            reply(tool_calls=[call("write_file", path="calc.py", content="x = 2\n")]),
            reply("Fixed."),
            reply("The test still fails: it expects 4."),
        ],
        checks=FAIL,
    )
    agent.run_turn("fix calc")
    notes = [m["content"] for m in agent.messages if m["role"] == "user" and m["content"].startswith("[lcode]")]
    assert len(notes) == 2 and "FAIL: test_average" in notes[0] and "Fix what your changes broke" in notes[0]
    assert "This was the last check" in notes[1]
    assert agent.messages[-1]["content"] == "The test still fails: it expects 4."
    assert "Checks still fail" in text(agent) and [r["exit_code"] for r in agent.check_results] == [1, 1]


def test_checks_the_model_ran_after_its_change_arent_repeated(make_agent):
    agent = make_agent(
        [
            reply(tool_calls=[call("write_file", path="calc.py", content="x = 1\n")]),
            reply(tool_calls=[call("bash", command=f"cd . && {PASS} 2>&1 | tail -5")]),
            reply("Done, the tests pass."),
        ],
        checks=PASS,
    )
    agent.run_turn("fix calc")
    assert "Checking the work" not in output(agent)
    assert agent.check_results == [{"command": PASS, "exit_code": 0, "by": "model"}]


def test_no_checks_without_changes_or_in_plan_mode(make_agent):
    agent = make_agent([reply(tool_calls=[call("read_file", path="README.md")]), reply("It's a demo.")], checks=PASS)
    agent.run_turn("what is this?")
    assert agent.check_results == [] and "Checking" not in output(agent)
    planner = make_agent(mode="plan", checks=PASS)
    assert planner.project_check() is None


def test_checks_that_may_not_run_dont_loop(make_agent):
    agent = make_agent(
        [reply(tool_calls=[call("write_file", path="notes.md", content="hi\n")]), reply("Done.")],
        mode="auto-edit",
        checks=PASS,
    )
    agent.perms.approve = lambda request: False  # nobody to ask
    agent.run_turn("write notes")
    assert "The checks didn't run" in text(agent) and agent.check_results[0]["exit_code"] is None
    assert agent.messages[-1]["content"] == "Done."


def test_the_model_is_told_about_the_checks(make_agent):
    assert f"lcode runs the project's checks (`{PASS}`)" in make_agent(checks=PASS).system_prompt()
    assert "runs the project's checks" not in make_agent(checks="off").system_prompt()


def test_programs_see_which_checks_ran(make_agent):
    result = api.Result(status="success", text="", session_id="s", model="m", checks=[{"command": "x", "exit_code": 0}])
    assert result.to_json()["checks"] == [{"command": "x", "exit_code": 0}]


def test_misordered_xml_tool_calls_are_retried(make_agent):
    from lcode.ollama import OllamaError

    agent = make_agent([reply("fixed it")])
    calls = {"n": 0}
    real = agent.ollama.chat_stream

    def chat_stream(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OllamaError("Ollama error: expected element type <function> but have <parameter>")
        yield from real(payload)

    agent.ollama.chat_stream = chat_stream
    agent.run_turn("add a feature")
    assert agent.messages[-1]["content"] == "fixed it"
    assert "(expected element type <function> but have <parameter>)" in agent.messages[-2]["content"]


def test_a_check_that_cant_run_isnt_sent_to_the_model(make_agent):
    agent = make_agent(
        [reply(tool_calls=[call("write_file", path="notes.md", content="hi\n")]), reply("Done.")],
        checks="no-such-test-runner --all",
    )
    agent.run_turn("write notes")
    assert "The checks couldn't run" in text(agent) and agent.messages[-1]["content"] == "Done."
    assert not any(
        m["content"].startswith("[lcode] Your changes are done") for m in agent.messages if m["role"] == "user"
    )


def test_a_failing_test_about_a_missing_file_still_counts(make_agent):
    check = checks.Check("x", "test")
    failure = "E   FileNotFoundError: [Errno 2] No such file or directory: 'data.csv'\nFAILED test_load\n[exit code: 1]"
    assert not checks.missing(check, failure)
    assert checks.missing(check, "/usr/bin/python3: No module named pytest\n\n[exit code: 1]")


def test_pytest_without_tests_isnt_a_check(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    assert checks.detect(tmp_path) is None
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "test_installed.py").write_text("")  # inside an environment: not the project's
    assert checks.detect(tmp_path) is None


def test_no_tests_collected_isnt_a_failure(make_agent):
    agent = make_agent(
        [reply(tool_calls=[call("write_file", path="notes.md", content="hi\n")]), reply("Done.")],
        checks="python3 -m pytest -q --co -p no:cacheprovider no_such_dir_xyz || exit 5",
    )
    agent.run_turn("write notes")
    assert "found no tests to run" in text(agent) and agent.messages[-1]["content"] == "Done."


def test_a_failure_the_model_doesnt_touch_isnt_checked_again(make_agent):
    agent = make_agent(
        [
            reply(tool_calls=[call("write_file", path="notes.md", content="hi\n")]),
            reply("Done."),
            reply("That failure is in test_average, which my change didn't touch: it failed before."),
        ],
        checks=FAIL,
    )
    agent.run_turn("write notes")
    assert len(agent.check_results) == 1 and agent.messages[-1]["content"].startswith("That failure")
