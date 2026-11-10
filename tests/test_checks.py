import json
import subprocess
import sys

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
        ({"tests/test_calc.py": "import unittest\n"}, "-m unittest discover -s tests"),
        ({"tests/__init__.py": "", "tests/test_calc.py": "import unittest\n"}, "-m unittest discover -s tests -t ."),
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


@pytest.mark.parametrize("package", [False, True])
def test_the_unittest_check_runs_with_or_without_init(tmp_path, package):
    """`discover -s tests -t .` stops with "Start directory is not importable" when tests/ isn't a package: a fine
    project's checks "failed", and the model was asked to fix that."""
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        "import unittest\n\nfrom calc import add\n\n\nclass CalcTest(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n"
    )
    if package:
        (tmp_path / "tests" / "__init__.py").write_text("")
    arguments = checks.detect(tmp_path).command.split()[1:]  # after the Python to use, which is this one here
    done = subprocess.run([sys.executable, *arguments], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "Ran 1 test" in done.stderr


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


@pytest.mark.parametrize(
    ("run", "command"),
    [
        (PASS, "cd . && {check} 2>&1"),  # the exit code is the check's
        ("python3 -c \"print('3 passed in 0.01s')\"", "cd . && {check} 2>&1 | tail -5"),  # the output says so
    ],
)
def test_checks_the_model_ran_after_its_change_arent_repeated(make_agent, run, command):
    agent = make_agent(
        [
            reply(tool_calls=[call("write_file", path="calc.py", content="x = 1\n")]),
            reply(tool_calls=[call("bash", command=command.format(check=run))]),
            reply("Done, the tests pass."),
        ],
        checks=run,
    )
    agent.run_turn("fix calc")
    assert "Checking the work" not in output(agent)
    assert agent.check_results == [{"command": run, "exit_code": 0, "by": "model"}]


def test_a_failing_run_piped_into_tail_doesnt_count_as_passed(make_agent):
    agent = make_agent(
        [
            reply(tool_calls=[call("write_file", path="calc.py", content="x = 1\n")]),
            reply(tool_calls=[call("bash", command=f"{FAIL} 2>&1 | tail -5")]),  # exits with tail's 0
            reply("Done, the tests pass."),
            reply("The test still fails: it expects 4."),
        ],
        checks=FAIL,
    )
    agent.run_turn("fix calc")
    assert "◆ Checking the work" in text(agent)
    assert agent.check_results == [{"command": FAIL, "exit_code": 1, "by": "lcode"}]
    notes = [m["content"] for m in agent.messages if m["role"] == "user" and m["content"].startswith("[lcode]")]
    assert len(notes) == 1 and "FAIL: test_average" in notes[0]


def test_a_passing_run_of_one_test_file_doesnt_count_as_the_checks(make_agent):
    # Passes when it's given a test file, fails on its own: the rest of the tests fail.
    check = "python3 -c \"import sys; print('FAIL: test_average' * (not sys.argv[1:])); sys.exit(not sys.argv[1:])\""
    agent = make_agent(
        [
            reply(tool_calls=[call("write_file", path="calc.py", content="x = 1\n")]),
            reply(tool_calls=[call("bash", command=f"{check} tests/test_calc.py -x")]),
            reply("Done, the tests pass."),
            reply("test_average fails, and it isn't about my change."),
        ],
        checks=check,
    )
    agent.run_turn("fix calc")
    assert "◆ Checking the work" in text(agent)
    assert agent.check_results == [{"command": check, "exit_code": 1, "by": "lcode"}]
    notes = [m["content"] for m in agent.messages if m["role"] == "user" and m["content"].startswith("[lcode]")]
    assert len(notes) == 1 and "FAIL: test_average" in notes[0]


@pytest.mark.parametrize(
    ("command", "shown", "passed"),
    [
        ("python -m pytest -q", "18 passed in 3.2s", True),
        ("cd x && python -m pytest -q 2>&1", "no summary in this output", True),
        ("python  -m pytest   -q > log.txt 2>&1", "", True),
        ("python -m pytest -q &> log.txt", "", True),
        ("python -m pytest -q && echo done", "done", True),
        ("python -m pytest -q 2>&1 | tail -5", "18 passed in 3.2s", True),
        ("python -m pytest -q | tail -3", "12 passed, 0 failed", True),
        ("python -m pytest -q | tee log.txt", "Ran 3 tests in 0.1s\n\nOK", True),
        ("python -m pytest -q 2>&1 | tail -5", "1 failed, 17 passed in 3.6s", False),
        ("python -m pytest -q 2>&1 | tail -5", "2 errors in 0.5s", False),
        ("python -m pytest -q 2>&1 | tail -5", "5 passed, 1 error in 0.5s", False),
        ("python -m pytest -q | tail -3", "FAILED tests/test_a.py::test_b\n3 passed", False),
        ("python -m pytest -q | tail -3", "ERROR tests/test_a.py\n3 passed", False),
        ("python -m pytest -q | tail -3", "Ran 3 tests in 0.1s\n\nFAILED (failures=1)", False),
        ("python -m pytest -q | head -5", "collecting ...", False),  # the summary was cut off
        ("python -m pytest -q 2>&1 | tail -5", "no tests ran in 0.1s", False),
        ("python -m pytest -q || true", "1 failed in 0.1s", False),
        ("python -m pytest -q; echo done", "1 failed in 0.1s\ndone", False),
        ("python -m pytest -q\necho done", "1 failed in 0.1s\ndone", False),
        ("python -m pytest -q & wait", "", False),
    ],
)
def test_a_run_passed_when_its_exit_code_or_output_says_so(command, shown, passed):
    check = checks.Check("python -m pytest -q", "tests/")
    assert checks.covers(check, command)
    assert checks.run_passed(check, command, f"{shown}\n[exit code: 0]") is passed


@pytest.mark.parametrize(
    ("check", "command", "whole"),
    [
        # Options alone, redirections and what comes after a shell operator leave the run whole.
        ("python -m pytest -q", "python -m pytest -q -x --tb=short 2>&1 | tail -40", True),
        ("python -m pytest -q", "python -m pytest -q --tb short -p no:cacheprovider -n auto -W error", True),
        ("python -m pytest -q", "python -m pytest -qx > log.txt 2>&1", True),
        ("python -m pytest -q", "python -m pytest -q  # all of them\necho tests/test_a.py", True),
        ("python -m pytest -q", "python -m pytest -q tests/test_a.py && python -m pytest -q", True),
        # A path, a test, or an option that picks tests: part of the checks.
        ("python -m pytest -q", "python -m pytest -q xarray/tests/test_dataset.py -x --tb=short 2>&1 | tail", False),
        ("python -m pytest -q", "python -m pytest -q tests/test_a.py::test_b", False),
        ("python -m pytest -q", "python -m pytest -q 2>&1 tests/test_a.py", False),
        ("python -m pytest -q", "python -m pytest -q -k chunk -x", False),
        ("python -m pytest -q", 'python -m pytest -q -m "not slow"', False),
        ("python -m pytest -q", "python -m pytest -q --ignore=tests/test_catalogue.py 2>&1", False),
        ("python -m pytest -q", "python -m pytest -q --lf", False),
        ("python -m pytest -q", "python -m pytest -q --collect-only", False),
        ("python -m pytest -q", 'echo "then run python -m pytest -q"', False),
        ("python -m unittest discover -s tests", "python -m unittest discover -s tests -t . -v 2>&1", True),
        ("python -m unittest discover -s tests", "python -m unittest discover -s tests/decorators -t .", False),
        ("python -m unittest", "python -m unittest discover -v", True),
        ("python -m unittest", "python -m unittest test_outputs -v 2>&1", False),
        ("python -m unittest", "python -m unittest discover -s tests", False),
        ("npm test", "npm test -- --coverage --reporter dot", True),
        ("npm test", "npm test -- --grep adds", False),
        ("npm test", "npm test -- --grep=adds", False),
        ("npm test", "npm test -- tests/calc.test.js", False),
        ("npm test", "npm test:unit", False),
        ("cargo test", "cargo test -- --nocapture --test-threads 1", True),
        ("cargo test", "cargo test parser", False),
        ("cargo test", "cargo test --lib", False),
        ("go test ./...", "go test ./... -count=1 -v", True),
        ("go test ./...", "go test ./... -run=TestAdd", False),
        ("make test", "make test -j 4", True),
        ("make test", "make test-fast", False),
        (" ", "python -m pytest -q", False),  # `checks = " "` isn't anything a command runs
    ],
)
def test_a_run_of_part_of_the_checks_isnt_the_checks(check, command, whole):
    assert checks.covers(checks.Check(check, "tests/"), command) is whole


def test_a_run_that_exits_with_an_error_never_passed():
    check = checks.Check("python -m pytest -q", "tests/")
    assert not checks.run_passed(check, "set -o pipefail; python -m pytest -q | tail", "3 passed\n[exit code: 1]")
    assert not checks.run_passed(check, "python -m pytest -q", "the command didn't run")


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


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"tests/test_wall.py": "import pytest\n\n@pytest.mark.parametrize('x', [1])\ndef test_x(x): ...\n"},
         "-m pytest -q"),
        ({"requirements.txt": "PyQt6\npytest>=8\n", "tests/test_wall.py": "def test_x(): ...\n"}, "-m pytest -q"),
        ({"tests/test_wall.py": "import unittest\n"}, "-m unittest discover -s tests"),
    ],
)  # fmt: skip
def test_tests_written_for_pytest_get_pytest_without_a_config(tmp_path, files, expected):
    for name, content in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(content)
    check = checks.detect(tmp_path)
    assert check.command.endswith(expected)


def test_the_projects_own_python_runs_the_checks(tmp_path):
    """Its dependencies are in its environment: the Python on PATH fails on their imports."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_wall.py").write_text("import pytest\n")
    assert not checks.detect(tmp_path).command.startswith(".venv")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    (tmp_path / ".venv" / "lib" / "test_inside_the_venv.py").parent.mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "test_inside_the_venv.py").write_text("import pytest\n")
    assert checks.detect(tmp_path).command == ".venv/bin/python -m pytest -q"
    assert checks.test_files(tmp_path) == [tmp_path / "tests" / "test_wall.py"]  # not the environment's own


# ----------------------------------------------------------------------------- runs that say nothing about the change

PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider"
NOTE = "[lcode] Your changes are done, but the project's checks fail"
WRITE_NOTES = [reply(tool_calls=[call("write_file", path="notes.md", content="hi\n")]), reply("Done.")]


def notes_to_the_model(agent):
    return [m["content"] for m in agent.messages if m["role"] == "user" and m["content"].startswith("[lcode]")]


def test_a_run_that_stops_before_its_first_test_isnt_sent_to_the_model(make_agent, repo):
    """Django's `tests/` can't be discovered by unittest, before or after any change: on the dev slice the model was
    told to fix what its changes broke, and it ran `git stash` or kept editing to find out that they broke nothing."""
    command = f"{sys.executable} -m unittest discover -s no_such_folder"
    agent = make_agent(list(WRITE_NOTES), checks=command)
    agent.run_turn("write notes")
    assert "stopped before running a test" in text(agent) and "not importable" in text(agent)
    assert "lcode config set checks" in text(agent)
    assert notes_to_the_model(agent) == [] and agent.messages[-1]["content"] == "Done."
    assert agent.check_results == [{"command": command, "exit_code": None, "by": "lcode"}]


def test_pytest_stopped_by_a_missing_dependency_isnt_sent_to_the_model(make_agent, repo):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_old.py").write_text("import no_such_dependency\n\n\ndef test_it(): ...\n")
    agent = make_agent(list(WRITE_NOTES), checks=PYTEST)
    agent.run_turn("write notes")
    assert "stopped before running a test" in text(agent) and "no_such_dependency" in text(agent)
    assert notes_to_the_model(agent) == [] and agent.check_results[0]["exit_code"] is None


@pytest.mark.parametrize(
    "change",
    [
        call("write_file", path="calc.py", content="def add(:\n"),  # a syntax error
        call("write_file", path="calc.py", content="import no_such_dependency\n"),  # an import that isn't there
        call("write_file", path="calc.py", content="def plus(a, b):\n    return a + b\n"),  # a name the test needs
        call("write_file", path="tests/test_new.py", content="def test_it(:\n"),  # its own new test
        call("write_file", path="conftest.py", content="import no_such_dependency\n"),
        call("bash", command="printf 'def add(:\\n' > calc.py"),  # the same, with a shell command
        call("bash", command="rm calc.py"),  # the test can't find the module any more
    ],
)
def test_a_run_stopped_by_the_change_still_goes_to_the_model(make_agent, repo, change):
    """This is what the checks are for: the same early stop, but its error names what this request changed."""
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    agent = make_agent(
        [
            reply(tool_calls=[call("read_file", path="calc.py")]),
            reply(tool_calls=[change]),
            reply(tool_calls=[call("write_file", path="notes.md", content="hi\n")]),  # a shell command alone: no checks
            reply("Done."),
            reply("I broke it, sorry."),
        ],
        checks=PYTEST,
    )
    agent.run_turn("change calc")
    notes = notes_to_the_model(agent)
    assert len(notes) == 1 and notes[0].startswith(NOTE) and "Fix what your changes broke" in notes[0]
    assert agent.check_results[0]["exit_code"] != 0 and agent.check_results[0]["exit_code"] is not None


def test_tests_that_ran_and_failed_still_go_to_the_model(make_agent, repo):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_old.py").write_text("def test_old():\n    assert False\n")
    agent = make_agent([*WRITE_NOTES, reply("That test has nothing to do with my change.")], checks=PYTEST)
    agent.run_turn("write notes")
    assert notes_to_the_model(agent)[0].startswith(NOTE) and agent.check_results[0]["exit_code"] == 1


TRACEBACK = "Traceback (most recent call last):\n"
DJANGO_STOP = (
    "django.core.exceptions.ImproperlyConfigured: Requested settings, but settings are not configured. You must "
    "either define the environment variable DJANGO_SETTINGS_MODULE or call settings.configure() before accessing "
    "settings."
)


@pytest.mark.parametrize(
    ("command", "output", "stopped"),
    [
        ("python -m unittest discover -s tests", f"{TRACEBACK}ImportError: x\n[exit code: 1]", True),
        ("python -m unittest", f"ERROR: x\n{TRACEBACK}Ran 1 test in 0.0s\nFAILED\n[exit code: 1]", False),
        ("python -m unittest", "Ran 12 tests in 0.1s\n\nFAILED (failures=1)\n[exit code: 1]", False),
        ("python -m unittest", "Fatal Python error: Segmentation fault\n[exit code: -11]", False),
        ("python -m pytest -q", "Interrupted: 1 error during collection\n[exit code: 2]", True),
        ("uv run pytest -q", "ERROR: usage: pytest [options]\n[exit code: 4]", True),
        ("python -m pytest -q", "INTERNALERROR> Traceback\n[exit code: 3]", True),
        ("python -m pytest -q", "36 failed, 7993 passed, 898 skipped in 330.00s\n[exit code: 1]", False),
        ("python -m pytest -q", "3 errors in 0.5s\n[exit code: 1]", False),  # errors in a test's fixtures: tests ran
        ("python -m pytest -q", "1 failed, 3 passed in 1.0s (interrupted)\n[exit code: 2]", False),
        ("uv run pytest -q && uv run ruff check .", "3 passed in 0.1s\nerror: bad rule\n[exit code: 2]", False),
        ("make test", "make: *** [Makefile:2: test] Error 1\n[exit code: 2]", False),
        ("npm test", f"{TRACEBACK}[exit code: 1]", False),
    ],
)
def test_which_runs_stopped_before_their_first_test(command, output, stopped):
    assert checks.no_test_ran(checks.Check(command, "test"), output) == stopped


@pytest.mark.parametrize(
    ("output", "files", "named"),
    [
        ('E     File "/work/pkg/calc.py", line 3', ["pkg/calc.py"], True),
        ("pkg/calc.py:1: in <module>", ["calc.py"], True),  # as the model's shell saw it, from inside pkg/
        ("tests/test_calc.py:1: in <module>", ["calc.py"], False),
        ("E   ModuleNotFoundError: No module named 'pkg.calc'", ["src/pkg/calc.py"], True),
        ("E   ModuleNotFoundError: No module named 'pkg'", ["src/pkg/calc.py"], True),
        ("E   ModuleNotFoundError: No module named 'hypothesis'", ["src/pkg/calc.py"], False),
        ("E   ModuleNotFoundError: No module named 'pkg.make.walks'", ["pkg/make/torus.py"], False),
        ("anything", [], False),
    ],
)
def test_whether_an_error_names_a_changed_file(output, files, named):
    assert checks.names_any(output, files) == named


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        (f"{TRACEBACK}  File x\nImportError: not importable\n\n[exit code: 1]", "ImportError: not importable."),
        ("ERROR x\nE   OSError: no data\nE   more\n2 errors in 0.60s\n\n[exit code: 2]", "OSError: no data."),
        ("[exit code: 2]", "no output."),
        (f"{TRACEBACK}{DJANGO_STOP}\n\n[exit code: 1]", DJANGO_STOP),  # one full stop, and it says what to set
        (f"{TRACEBACK}{'x' * 500}\n[exit code: 1]", "x" * 239 + "…"),
    ],
)
def test_the_user_is_told_why_the_run_stopped(output, reason):
    assert checks.stop_reason(output) == reason


def test_the_checks_run_in_the_projects_root_wherever_the_model_went(make_agent, repo):
    """`discover -s tests` from inside tests/ doesn't find tests/: a fine project's checks would stop there."""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "import unittest\n\nfrom calc import add\n\n\nclass CalcTest(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n"
    )
    agent = make_agent(
        [
            reply(tool_calls=[call("bash", command="cd tests && ls")]),
            reply(tool_calls=[call("write_file", path="notes.md", content="hi\n")]),
            reply("Done."),
        ],
        checks="auto",
    )
    agent.run_turn("write notes")
    assert "Checks pass" in text(agent) and agent.check_results[0]["exit_code"] == 0
    assert agent.cwd == repo / "tests"  # the model's shell stays where it went


def test_the_users_own_command_runs_where_they_started_lcode(make_agent, repo):
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "service").mkdir()
    (repo / "service" / "only_here").write_text("")
    agent = make_agent(
        [
            reply(tool_calls=[call("bash", command="cd .. && ls")]),
            reply(tool_calls=[call("write_file", path="notes.md", content="hi\n")]),
            reply("Done."),
        ],
        checks="test -f only_here",
    )
    agent.cwd = agent.workspace = repo / "service"
    agent.run_turn("write notes")
    assert "Checks pass" in text(agent) and agent.cwd == repo


def test_checks_that_dont_finish_arent_called_a_failure(make_agent, monkeypatch):
    """On the dev slice, sympy's whole suite ran into the time limit, and the model was told to fix what it broke."""
    monkeypatch.setattr(checks, "TIMEOUT", 1)
    agent = make_agent([*WRITE_NOTES, reply("The full checks didn't finish.")], checks="echo 1 passed; sleep 30")
    agent.run_turn("write notes")
    notes = notes_to_the_model(agent)
    assert len(notes) == 1 and "didn't finish in 1 seconds" in notes[0] and "hang" in notes[0]
    assert "Fix what your changes broke" not in notes[0] and "checks fail" not in notes[0]
    assert "The checks didn't finish" in text(agent) and agent.check_results[0]["exit_code"] is None


def test_what_lcode_remembers_about_a_run_without_tests_isnt_a_failure(make_agent, repo):
    from lcode import context

    agent = make_agent(list(WRITE_NOTES), checks=f"{sys.executable} -m unittest discover -s no_such_folder")
    agent.run_turn("write notes")
    assert "didn't run" in context.task_sheet(agent) and "failed" not in context.task_sheet(agent)
