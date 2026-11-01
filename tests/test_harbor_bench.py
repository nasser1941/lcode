import json
import os
import shutil
import subprocess

import pytest

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


def write_trial(job, name, task, reward, minute, tokens=(1000, 100)):
    folder = job / name
    folder.mkdir(parents=True)
    result = {
        "task_name": task,
        "started_at": f"2026-11-01T01:{minute:02d}:00Z",
        "agent_execution": {
            "started_at": f"2026-11-01T01:{minute:02d}:00Z",
            "finished_at": f"2026-11-01T01:{minute + 2:02d}:30Z",
        },
        "agent_result": {"n_input_tokens": tokens[0], "n_output_tokens": tokens[1]},
        "verifier_result": None if reward is None else {"rewards": {"reward": reward}},
    }
    if reward is None:
        result["exception_info"] = {
            "exception_type": "RuntimeError",
            "exception_message": "no network\nall predefined address pools have been fully subnetted",
        }
    (folder / "result.json").write_text(json.dumps(result))


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
