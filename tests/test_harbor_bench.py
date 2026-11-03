import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from rich.console import Console

from lcode import harbor_bench

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="the commands run in bash in the task's container")


def bash(command: str, env: dict, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", command], env=env, cwd=cwd, capture_output=True, text=True, timeout=30)


@needs_bash
def test_the_task_reaches_lcode_word_for_word(tmp_path):
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    fake = bin_dir / "lcode"  # stands in for lcode: writes what it was asked, its model server and its shell setup
    fake.write_text(
        '#!/bin/sh\nprintf "%s" "$2" > "$HOME/asked"\necho "$OLLAMA_HOST $3 $FROM_BASHRC" > "$HOME/how"\necho done\n'
    )
    fake.chmod(0o755)
    # The image's interactive shell setup, as SWE-bench's images activate the project's conda environment.
    (tmp_path / ".bashrc").write_text("export FROM_BASHRC=yes\n")
    task = "Fix it: don't break `make`, keep $HOME and 'quotes' as they are.\nThen run the tests."
    log = tmp_path / "lcode.txt"
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "OLLAMA_HOST": "http://models:11434"}
    result = bash(harbor_bench.run_command(task, str(log)), env)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "asked").read_text() == task
    assert (tmp_path / "how").read_text().split() == ["http://models:11434", "--yolo", "yes"]
    assert log.read_text().endswith("done\n")  # the output is kept in the trial's agent folder too


@needs_bash
def test_ollama_is_found_on_the_containers_gateway(tmp_path):
    routes = tmp_path / "route"
    routes.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\n"
        "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\n"
    )
    command = harbor_bench.find_ollama(str(routes)) + ' echo "$OLLAMA_HOST"'
    env = {"PATH": os.environ["PATH"]}
    found = bash(command, env).stdout.strip()
    # Where host.docker.internal resolves (Docker Desktop), that name is used instead.
    assert found in ("http://172.18.0.1:11434", "http://host.docker.internal:11434")
    assert bash(command, {**env, "OLLAMA_HOST": "http://gpu-box:11434"}).stdout.strip() == "http://gpu-box:11434"


@needs_bash
def test_the_install_command_is_valid_shell():
    for spec in ("lcode-cli==0.53.0", "/installed-agent/lcode_cli-0.53.0-py3-none-any.whl"):
        command = harbor_bench.install_command(spec)
        assert f"uv tool install --force --python {harbor_bench.PYTHON} {spec};" in command
        assert subprocess.run(["bash", "-n", "-c", command]).returncode == 0


def test_the_model_and_lcodes_folder_for_the_task():
    env = harbor_bench.run_env("ollama/qwen3.6-35b")
    assert env["LCODE_MODEL"] == "qwen3.6-35b" and env["LCODE_HOME"] == harbor_bench.HOME
    assert "LCODE_MODEL" not in harbor_bench.run_env(None)


def test_the_newest_traces_end_tells_how_the_request_went(tmp_path):
    traces = tmp_path / "traces"
    traces.mkdir()
    assert harbor_bench.trace_usage(tmp_path) is None
    old, new = traces / "a-1.jsonl", traces / "a-2.jsonl"
    old.write_text(json.dumps({"type": "end", "tokens_in": 1}) + "\n")
    new.write_text(
        json.dumps({"type": "start"}) + "\n" + json.dumps({"type": "end", "tokens_in": 900, "tokens_out": 40}) + "\n"
    )
    os.utime(old, (1, 1))
    assert harbor_bench.trace_usage(tmp_path) == {"type": "end", "tokens_in": 900, "tokens_out": 40}
    new.write_text(json.dumps({"type": "start"}) + "\n")  # a request that didn't end (killed at the time limit)
    assert harbor_bench.trace_usage(tmp_path) == {"type": "end", "tokens_in": 1}


def write_unfinished_trace(home):
    """A request killed at its time limit: no end event, and its last reply was one lcode stopped."""
    (home / "traces").mkdir(parents=True)
    events = [
        {"type": "start", "at": 0.0},
        {"type": "model", "at": 30.0, "lane": "main", "kind": "step", "step": 1, "seconds": 30.0,
         "tokens_in": 9000, "tokens_out": 300},
        {"type": "tool", "at": 32.5, "lane": "main", "step": 1, "seconds": 2.5},
        {"type": "model", "at": 40.0, "lane": "agent 1", "kind": "step", "step": 1, "seconds": 5.0,
         "tokens_in": 2000, "tokens_out": 100},
        {"type": "model", "at": 90.0, "lane": "main", "kind": "step", "step": 2, "seconds": 57.5, "tokens_in": 0,
         "tokens_out": 0, "stopped": True, "estimated_in": 9500, "estimated_out": 4000},
    ]  # fmt: skip
    (home / "traces" / "a-1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events) + '{"type": "mod')


def test_a_request_stopped_at_its_time_limit_counts_what_its_trace_recorded(tmp_path):
    write_unfinished_trace(tmp_path)
    usage = harbor_bench.trace_usage(tmp_path)
    assert usage is not None and (usage["status"], usage["steps"], usage["seconds"]) == ("unfinished", 2, 90.0)
    assert (usage["model_calls"], usage["tool_calls"], usage["tool_seconds"]) == (3, 1, 2.5)
    assert (usage["tokens_in"], usage["tokens_out"]) == (11000, 400)
    assert harbor_bench.usage_tokens(usage) == (20500, 4400)  # with the estimate for the reply lcode stopped
    assert harbor_bench.usage_tokens({"type": "end", "tokens_in": 900, "tokens_out": 40}) == (900, 40)


def test_harbor_runs_the_tasks_with_lcode_next_to_it(tmp_path):
    argv = harbor_bench.harbor_argv(
        "terminal-bench",
        ["fix-git", "prove-plus-comm"],
        "qwen3.6-35b",
        tmp_path,
        "terminal-bench",
        context=32768,
        lcode_with=["--with", "lcode-cli==0.53.0"],
        wheel=tmp_path / "lcode.whl",
        overlay=tmp_path / "bridge.yaml",
    )
    line = " ".join(argv)
    assert line.startswith(f"uvx --python 3.13 --from {harbor_bench.HARBOR} --with lcode-cli==0.53.0 harbor run")
    assert "-d terminal-bench@2.0 -a lcode.harbor:Lcode -m qwen3.6-35b -k 3 -n 1" in line
    assert "-i fix-git -i */fix-git -i prove-plus-comm -i */prove-plus-comm" in line
    assert f"--ak context=32768 --ak wheel={tmp_path / 'lcode.whl'}" in line
    assert line.endswith(f"--extra-docker-compose {tmp_path / 'bridge.yaml'}")


def write_trial(job, name, task, reward, minute, tokens=(1000, 100), error="RuntimeError", speed=None):
    """An attempt as Harbor saves it, with lcode's trace of the model generating at `speed` tokens/s, if given."""
    folder = job / name
    folder.mkdir(parents=True)
    result = {
        "task_name": task,
        "started_at": f"2026-11-01T01:{minute:02d}:00Z",
        "agent_execution": {
            "started_at": f"2026-11-01T01:{minute:02d}:00Z",
            "finished_at": f"2026-11-01T01:{minute + 2:02d}:30Z",
        },
        "agent_result": {"n_input_tokens": tokens and tokens[0], "n_output_tokens": tokens and tokens[1]},
        "verifier_result": None if reward is None else {"rewards": {"reward": reward}},
    }
    if reward is None:
        result["exception_info"] = {
            "exception_type": error,
            "exception_message": "no network\nall predefined address pools have been fully subnetted",
        }
    (folder / "result.json").write_text(json.dumps(result))
    if speed:
        traces = folder / "agent" / "lcode" / "traces"
        traces.mkdir(parents=True)
        steps = [{"type": "model", "tokens_out": 2 * speed, "gen_seconds": 2.0}, {"type": "model", "tokens_out": 0}]
        (traces / "a-1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in steps))


def test_pass_rates_spread_and_cost_from_harbors_results(tmp_path):
    job = tmp_path / "terminal-bench"
    for attempt, reward in enumerate([1.0, 1.0, 1.0]):
        write_trial(job, f"a__{attempt}", "terminal-bench/always", reward, 10 * attempt)
    for attempt, reward in enumerate([1.0, 0.0, 1.0]):
        write_trial(job, f"b__{attempt}", "terminal-bench/sometimes", reward, 10 * attempt + 1, (3000, 300))
    for attempt, reward in enumerate([0.0, None, 0.0]):  # the second attempt's container didn't start
        write_trial(job, f"c__{attempt}", "terminal-bench/never", reward, 10 * attempt + 2)
    trials = harbor_bench.read_trials(job, "terminal-bench")
    assert {t.task for t in trials} == {"always", "sometimes", "never"}
    assert any("fully subnetted" in t.error for t in trials)
    summary = harbor_bench.summarize(trials, attempts=3)
    tasks = {t.task: t for t in summary.tasks}
    assert tasks["sometimes"].passed == [True, False, True] and tasks["sometimes"].tokens_in == 3000
    assert tasks["never"].passed == [False, False] and tasks["never"].not_run == 1
    assert tasks["always"].seconds == 150.0
    assert summary.pass_at_1 == pytest.approx((1 + 2 / 3 + 0) / 3)
    assert summary.pass_all == 0.5  # of the tasks with three attempts that ran, one passed all three
    # A task's second run is its second attempt that ran: for "never", that's its third attempt.
    assert summary.runs == pytest.approx([2 / 3, 1 / 3, 1.0])
    assert summary.spread == pytest.approx(2 / 3) and summary.not_run == 1


def test_tokens_of_attempts_stopped_at_their_time_limit(tmp_path):
    """lcode 0.54.0 gave Harbor no tokens for an attempt stopped at its time limit: they come from its trace, and
    an attempt without one counts as unknown, not as 0."""
    job = tmp_path / "terminal-bench"
    write_trial(job, "slow__0", "terminal-bench/slow", 0.0, 0, None)
    write_unfinished_trace(job / "slow__0" / "agent" / "lcode")
    write_trial(job, "slow__1", "terminal-bench/slow", 1.0, 10, (1500, 600))
    write_trial(job, "slow__2", "terminal-bench/slow", 0.0, 20, None)  # no trace either
    write_trial(job, "lost__0", "terminal-bench/lost", 0.0, 30, None)
    trials = harbor_bench.read_trials(job, "terminal-bench")
    slow = [(t.tokens_in, t.tokens_out) for t in trials if t.task == "slow"]
    assert slow == [(20500, 4400), (1500, 600), (None, None)]
    summary = harbor_bench.summarize(trials, attempts=3)
    tasks = {t.task: t for t in summary.tasks}
    assert (tasks["slow"].tokens_in, tasks["slow"].tokens_out) == (11000, 2500)  # of the two attempts known
    assert (tasks["lost"].tokens_in, tasks["lost"].tokens_out) == (None, None)
    console = Console(file=io.StringIO(), width=200)
    harbor_bench.print_summary(console, summary)
    [lost] = [line for line in console.file.getvalue().splitlines() if line.startswith("lost")]
    assert lost.split()[-2:] == ["–", "–"]


def test_attempts_whose_tests_didnt_finish_count_as_failed(tmp_path):
    """Harbor's mean counts an attempt whose tests ran out of time as failed, and so does lcode. One whose container
    didn't start counts neither way."""
    job = tmp_path / "terminal-bench"
    write_trial(job, "hangs__0", "terminal-bench/hangs", None, 0, error="VerifierTimeoutError")
    write_trial(job, "hangs__1", "terminal-bench/hangs", 1.0, 10)
    write_trial(job, "hangs__2", "terminal-bench/hangs", None, 20, error="EnvironmentStartTimeoutError")
    trials = harbor_bench.read_trials(job, "terminal-bench")
    assert [(t.reward, t.unfinished_tests) for t in trials] == [(0.0, True), (1.0, False), (None, False)]
    summary = harbor_bench.summarize(trials, attempts=3)
    [hangs] = summary.tasks
    assert (hangs.passed, hangs.not_run, hangs.unfinished_tests) == ([False, True], 1, 1)
    assert (summary.unfinished_tests, summary.not_run, summary.pass_at_1) == (1, 1, 0.5)
    console = Console(file=io.StringIO(), width=200)
    harbor_bench.print_summary(console, summary)
    text = console.file.getvalue()
    assert "1 attempt(s) count as failed: their tests didn't finish" in text and "1 attempt(s) didn't run" in text


def test_the_model_speed_comes_from_lcodes_traces(tmp_path):
    job = tmp_path / "terminal-bench"
    for attempt, speed in enumerate([60, 80, 70]):
        write_trial(job, f"a__{attempt}", "terminal-bench/a", 1.0, 10 * attempt, speed=speed)
    write_trial(job, "b__0", "terminal-bench/b", 1.0, 40)  # no trace
    trials = harbor_bench.read_trials(job, "terminal-bench")
    assert [t.tokens_per_second for t in trials] == [60.0, 80.0, 70.0, None]
    summary = harbor_bench.summarize(trials, attempts=3)
    assert summary.tokens_per_second == 70.0
    console = Console(file=io.StringIO(), width=200)
    harbor_bench.print_summary(console, summary)
    assert "The model generated 70 tokens/s (the median attempt)." in console.file.getvalue()


def test_lcode_bench_harbor_runs_harbor_then_reports_and_saves(capsys, monkeypatch):
    from lcode import cli, config, hardware, update

    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    monkeypatch.setattr(update, "docker_available", lambda: True)
    monkeypatch.setattr(update, "lcode_install", lambda *a, **k: update.Install("uvx"))  # lcode from PyPI
    monkeypatch.setattr(hardware, "detect", lambda: hardware.Hardware("linux", "test CPU", 32.0))
    runs = []

    def harbor(argv):
        runs.append(argv)
        job = Path(argv[argv.index("-o") + 1]) / argv[argv.index("--job-name") + 1]
        write_trial(job, "prove-plus-comm__a", "terminal-bench/prove-plus-comm", 1.0, 0, (2000, 200))
        return 0

    monkeypatch.setattr(subprocess, "call", harbor)
    with pytest.raises(SystemExit) as done:
        cli.main(
            ["bench", "--harbor", "dev", "qwen3.6-35b", "--tasks", "prove-plus-comm", "--attempts", "1", "--ctx", "32k"]
        )
    out = " ".join(capsys.readouterr().out.split())
    assert done.value.code == 0 and len(runs) == 1 and "--with lcode-cli==" in " ".join(runs[0])
    assert "pass@1 100%" in out and "Took 0 min." in out
    [saved] = (config.STATE_DIR / "bench").glob("dev-*/lcode-bench.json")
    report = json.loads(saved.read_text())
    assert (report["model"], report["context"], report["attempts"]) == ("qwen3.6-35b", 32768, 1)
    assert report["started"] <= report["date"] and 0 <= report["seconds"] < 60
    assert report["results"][0]["tokens_in"] == 2000 and report["summary"]["pass_at_1"] == 1.0
    assert (report["summary"]["unfinished_tests"], report["summary"]["tokens_per_second"]) == (0, None)


def test_other_agents_are_set_up_like_ollama_launch_sets_them_up(tmp_path):
    model = harbor_bench.variant_name("lcode-qwen3.6-35b", 65536)
    assert model == "lcode-qwen3.6-35b-64k" and harbor_bench.variant_name("qwen3.6:35b-a3b:latest", 32768).endswith(
        "-32k"
    )
    show = {"capabilities": ["completion", "tools", "thinking"], "thinking": {"values": [False, True], "default": True}}
    claude, no_catalog = harbor_bench.agent_setup("claude-code", model, 65536, show, tmp_path)
    assert no_catalog is None and claude[:2] == ["--ae", "ENABLE_CLAUDEAI_MCP_SERVERS=false"]
    assert "DISABLE_ERROR_REPORTING=1" in claude and claude.count("--ae") == len(harbor_bench.CLAUDE_CODE_ENV)
    opencode, _ = harbor_bench.agent_setup("opencode", model, 65536, show, tmp_path)
    provider = json.loads(opencode[1].removeprefix("opencode_config="))["provider"]["ollama"]
    assert provider["options"]["baseURL"] == "http://host.docker.internal:11434/v1"
    entry = provider["models"][model]
    assert entry["reasoning"] and entry["limit"] == {"context": 65536, "input": 65536, "output": 8192}
    codex, catalog = harbor_bench.agent_setup("codex", model, 65536, show, tmp_path)
    config = json.loads(codex[1].removeprefix("config="))
    assert config["model_providers"]["ollama-launch"]["wire_api"] == "responses"
    assert config["model_catalog_json"] == harbor_bench.CODEX_CATALOG
    [known] = json.loads(catalog.read_text())["models"]
    assert (known["slug"], known["context_window"], known["default_reasoning_level"]) == (model, 65536, "high")
    assert [level["effort"] for level in known["supported_reasoning_levels"]] == ["none", "high"]
    overlay = harbor_bench.agent_overlay(True, catalog)
    assert "network_mode: bridge" in overlay and "host.docker.internal:host-gateway" in overlay
    assert f"{catalog}:{harbor_bench.CODEX_CATALOG}:ro" in overlay
    assert "volumes" not in harbor_bench.agent_overlay(False) and "network_mode" not in harbor_bench.agent_overlay(
        False
    )
    argv = harbor_bench.harbor_argv(
        "terminal-bench", ["fix-git"], model, tmp_path, "terminal-bench", context=65536, agent="codex",
        agent_args=codex, overlay=tmp_path / "overlay.yaml",
    )  # fmt: skip
    line = " ".join(argv)
    assert "-a codex -m ollama/lcode-qwen3.6-35b-64k" in line and "context=65536" not in line
    assert argv[argv.index("--extra-docker-compose") - 2 :][:2] == codex  # the agent's options, then the overlay
    assert "lcode" not in argv[: argv.index("run")]  # Harbor alone: lcode's agent isn't needed


def test_lcode_bench_harbor_runs_another_agent_on_the_model_at_the_context(capsys, monkeypatch):
    from lcode import cli, config, hardware, ollama, update

    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    monkeypatch.setattr(update, "docker_available", lambda: True)
    monkeypatch.setattr(update, "lcode_install", lambda *a, **k: update.Install("uvx"))
    monkeypatch.setattr(hardware, "detect", lambda: hardware.Hardware("linux", "test CPU", 32.0))
    installed = [{"name": "lcode-qwen3.6-35b:latest"}]
    created = []

    def create_variant(self, name, base, parameters):
        created.append((name, base, parameters))
        installed.append({"name": f"{name}:latest"})

    monkeypatch.setattr(ollama.Ollama, "list_models", lambda self: installed)
    monkeypatch.setattr(ollama.Ollama, "create_variant", create_variant)
    show = {"capabilities": ["completion", "tools", "thinking"], "thinking": {"values": [False, True], "default": True}}
    monkeypatch.setattr(ollama.Ollama, "show", lambda self, model: show)
    runs = []

    def harbor(argv, env=None):
        runs.append((argv, env))
        job = Path(argv[argv.index("-o") + 1]) / argv[argv.index("--job-name") + 1]
        write_trial(job, "prove-plus-comm__a", "terminal-bench/prove-plus-comm", 1.0, 0, (9000, 300))
        result = job / "prove-plus-comm__a" / "result.json"
        result.write_text(json.dumps({**json.loads(result.read_text()), "agent_info": {"version": "0.166.0"}}))
        return 0

    monkeypatch.setattr(subprocess, "call", harbor)
    bench = ["bench", "--harbor", "dev", "qwen3.6-35b", "--tasks", "prove-plus-comm", "--attempts", "1", "--ctx", "64k"]
    with pytest.raises(SystemExit) as done:
        cli.main([*bench, "--agent", "codex"])
    assert done.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert created == [("lcode-qwen3.6-35b-64k", "lcode-qwen3.6-35b", {"num_ctx": 65536})]
    assert "Made lcode-qwen3.6-35b-64k" in out and "codex 0.166.0 on lcode-qwen3.6-35b-64k" in out
    again = harbor_bench.context_variant(config.load(), "qwen3.6-35b", 65536, Console(file=io.StringIO()))
    assert again == ("lcode-qwen3.6-35b-64k", show) and len(created) == 1  # made once
    argv, env = runs[0]
    assert "-a codex -m ollama/lcode-qwen3.6-35b-64k" in " ".join(argv)
    assert env["OLLAMA_BASE_URL"] == "http://host.docker.internal:11434" and env["OLLAMA_API_KEY"]
    overlay = Path(argv[argv.index("--extra-docker-compose") + 1])
    assert "codex-models.json" in overlay.read_text() and (overlay.parent / "codex-models.json").is_file()
    saved = sorted((config.STATE_DIR / "bench").glob("dev-codex-*/lcode-bench.json"))
    report = json.loads(saved[-1].read_text())
    assert (report["agent"], report["agent_version"], report["ollama_model"]) == (
        "codex",
        "0.166.0",
        "lcode-qwen3.6-35b-64k",
    )
    assert report["results"][0]["tokens_in"] == 9000


def test_another_agent_needs_the_model_installed(capsys, monkeypatch):
    from lcode import cli, config, ollama, update

    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    monkeypatch.setattr(update, "docker_available", lambda: True)
    monkeypatch.setattr(ollama.Ollama, "list_models", lambda self: [])
    with pytest.raises(SystemExit) as done:
        cli.main(["bench", "--harbor", "dev", "qwen3.6-35b", "--agent", "opencode"])
    out = " ".join(capsys.readouterr().out.split())
    assert done.value.code == 1 and "is not installed yet. Run: lcode setup qwen3.6-35b" in out
    assert not list((config.STATE_DIR / "bench").glob("dev-opencode-*"))  # no empty results folder


def test_lcode_bench_harbor_lists_its_slice_and_refuses_other_tasks(capsys, monkeypatch):
    from lcode import cli, update

    with pytest.raises(SystemExit) as done:
        cli.main(["bench", "--harbor", "dev", "--list"])
    out = " ".join(capsys.readouterr().out.split())
    assert done.value.code == 0 and "terminal-bench@2.0 (15)" in out and "swebench-verified (10)" in out
    assert "django__django-12774" in out
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    monkeypatch.setattr(update, "docker_available", lambda: True)
    with pytest.raises(SystemExit) as done:
        cli.main(["bench", "--harbor", "dev", "--tasks", "fix-git"])  # in Terminal-Bench, not in the dev slice
    assert done.value.code == 2 and "Not in the dev slice: fix-git" in " ".join(capsys.readouterr().out.split())


# ----------------------------------------------------------------------------- comparing two runs


def saved(folder: Path, passed: dict[str, list[bool]], **fields) -> Path:
    """A saved lcode-bench.json with these attempts, in a folder of its own like a real run's."""
    folder.mkdir(parents=True)
    data = {"lcode": "0.57.0", "slice": "dev", "model": "qwen3.6-35b", "context": 32768, "attempts": 3}
    data |= fields
    data["results"] = [{"dataset": "terminal-bench", "task": task, "passed": runs} for task, runs in passed.items()]
    (folder / "lcode-bench.json").write_text(json.dumps(data))
    return folder


def compared(tmp_path, before: dict, after: dict, **after_fields) -> tuple[int, str]:
    console = Console(file=io.StringIO(), width=200)
    old = saved(tmp_path / "before", before)
    new = saved(tmp_path / "after", after, **after_fields)
    code = harbor_bench.compare_command([str(old), str(new / "lcode-bench.json")], console)
    return code, console.file.getvalue()


SOMETIMES = [True, True, False]


def test_compare_calls_a_small_change_noise(tmp_path):
    before = {f"t{n}": SOMETIMES for n in range(6)}
    code, text = compared(tmp_path, before, {**before, "t0": [True, False, False]})
    assert code == 0 and "t0: 2/3 → 1/3" in text
    assert "pass@1 67% → 61% over the 6 tasks in both: within the noise (-6 points; the noise is ±38)" in text


def test_compare_fails_a_drop_beyond_the_noise(tmp_path):
    before = {f"t{n}": [True] * 3 for n in range(10)}
    after = {**before, **{f"t{n}": [False] * 3 for n in range(5)}}
    code, text = compared(tmp_path, before, after)
    assert code == 1 and "pass@1 100% → 50%" in text and "a drop beyond the noise (-50 points" in text


def test_compare_says_when_its_better_and_what_differs(tmp_path):
    before = {"t0": [False] * 3, "t1": [False] * 3, "gone": [True] * 3}
    code, text = compared(tmp_path, before, {"t0": [True] * 3, "t1": [True] * 3, "new": [True] * 3}, context=65536)
    assert code == 0 and "better, beyond the noise (+100 points" in text
    assert "Not the same context: 32768 and 65536." in text
    assert "terminal-bench/gone: only before, left out" in text and "terminal-bench/new: only after, left out" in text
    assert "Before: lcode 0.57.0 · qwen3.6-35b at 32K" in text and "at 64K" in text


def test_compare_reads_both_runs_again_from_harbors_results(tmp_path):
    """Runs are read again from Harbor's results while they're there, so a run saved by an older lcode counts the
    same way as a new one: here, attempts whose tests didn't finish. A slower model gets a warning."""
    old, new = tmp_path / "before", tmp_path / "after"
    for folder, speed, rewards in ((old, 80, [0.0, 0.0, 0.0]), (new, 60, [None, None, None])):
        saved(folder, {"hangs": []})  # what lcode 0.59.0 saved when the tests ran out of time: no attempt
        for attempt, reward in enumerate(rewards):
            write_trial(
                folder / "terminal-bench", f"hangs__{attempt}", "terminal-bench/hangs", reward, 10 * attempt,
                error="VerifierTimeoutError", speed=speed,
            )  # fmt: skip
    console = Console(file=io.StringIO(), width=200)
    assert harbor_bench.compare_command([str(old), str(new)], console) == 0
    text = console.file.getvalue()
    assert "Model speed: 80 tokens/s before, 60 after (the median attempt's)." in text
    assert "The model ran 25% slower after." in text and "pass@1 0% → 0% over the 1 tasks in both" in text
    copy = tmp_path / "copy" / "lcode-bench.json"  # a copy elsewhere: Harbor's results are where the run left them
    copy.parent.mkdir()
    copy.write_text(json.dumps({**json.loads((new / "lcode-bench.json").read_text()), "jobs": str(new)}))
    assert harbor_bench.Saved.load(copy).passed == {"terminal-bench/hangs": [False, False, False]}
    near = harbor_bench.compare(
        harbor_bench.Saved(old, {}, {"t": [True]}, 80.0), harbor_bench.Saved(new, {}, {"t": [True]}, 76.0)
    )
    assert any("Model speed" in line for line in near[0]) and not any("slower" in line for line in near[0])


def test_compare_needs_two_results(tmp_path):
    console = Console(file=io.StringIO(), width=200)
    (tmp_path / "other.json").write_text('{"schema": 1, "runs": []}')
    code = harbor_bench.compare_command([str(tmp_path / "missing"), str(tmp_path / "other.json")], console)
    assert code == 2 and "isn't a result of lcode bench --harbor" in console.file.getvalue()


def test_lcode_bench_compare_exits_with_the_verdict(tmp_path, monkeypatch):
    from lcode import cli

    monkeypatch.setattr(cli, "console", Console(file=io.StringIO(), width=200))
    old = saved(tmp_path / "before", {"t0": [True] * 3})
    new = saved(tmp_path / "after", {"t0": [False] * 3})
    with pytest.raises(SystemExit) as stopped:
        cli.main(["bench", "--compare", str(old), str(new)])
    assert stopped.value.code == 1
