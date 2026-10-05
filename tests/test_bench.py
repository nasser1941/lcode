import io
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from rich.console import Console

from conftest import FakeOllama, call, reply
from lcode import bench
from lcode.agent import Settings
from lcode.hardware import Hardware

LAPTOP = Hardware("linux", "x", 31, "RTX 4080 Laptop", 12)
STATS = """\
import statistics

numbers = [int(line) for line in open("numbers.txt") if line.strip()]
print(f"count: {len(numbers)}")
print(f"sum: {sum(numbers)}")
print(f"mean: {statistics.mean(numbers):.2f}")
print(f"median: {statistics.median(numbers):.2f}")
"""


DURATIONS_FIXED = """\
import re

UNITS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    text = text.strip().lower()
    if text.isdigit():
        return int(text)
    match = re.fullmatch(r"(?:(\\d+)h)?\\s*(?:(\\d+)m)?\\s*(?:(\\d+)s)?", text)
    if not text or not match:
        raise ValueError(f"invalid duration: {text!r}")
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds
"""


def setup(task: bench.Task, folder: Path) -> Path:
    for rel, content in task.files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(content)
    return folder


def replace(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text().replace(old, new))


def solve(task_id: str, folder: Path) -> str:
    """A correct solution for each task; returns the final answer."""
    if task_id == "fix-bug":
        replace(folder / "cart.py", "sum(price for", "sum(price * quantity for")
    elif task_id == "find-code":
        line = bench.UPLOADER["uploader/backoff.py"].splitlines().index("def next_wait(attempt):") + 1
        return f"It's `next_wait` in uploader/backoff.py:{line}."
    elif task_id == "write-script":
        (folder / "stats.py").write_text(STATS)
    elif task_id == "rename":
        for path in folder.rglob("*.py"):
            replace(path, "get_user_name", "display_name")
    elif task_id == "precise-edit":
        lines = (folder / "services.py").read_text().split("\n")
        target = lines.index('    "service_087": {') + 3
        lines[target] = lines[target].replace("30", "45")
        (folder / "services.py").write_text("\n".join(lines))
    elif task_id == "spec-fix":
        (folder / "durations.py").write_text(DURATIONS_FIXED)
    elif task_id == "feature":
        replace(folder / "todo.py", '"done": False}', '"done": False, "priority": priority}')
        replace(folder / "todo.py", "def add(title):", "def add(title, priority):")
        replace(folder / "todo.py", "add(args.title)", "add(args.title, args.priority)")
        replace(
            folder / "todo.py",
            'add_command.add_argument("title")',
            'add_command.add_argument("title")\n    add_command.add_argument("--priority", '
            'choices=["high", "normal", "low"], default="normal")',
        )
        replace(
            folder / "todo.py",
            "{'x' if todo['done'] else ' '}] {todo['title']}",
            "{'x' if todo['done'] else ' '}] {'! ' if todo.get('priority') == 'high' else ''}{todo['title']}",
        )
        replace(
            folder / "todo.py",
            "for todo in storage.load():",
            "rank = {'high': 0, 'normal': 1, 'low': 2}\n"
            "    for todo in sorted(storage.load(), key=lambda t: rank[t.get('priority', 'normal')]):",
        )
    elif task_id == "recover":
        replace(folder / "data.txt", "dave, n/a\n", "")
        subprocess.run(
            [sys.executable, "tool.py", "--source", "data.txt", "--out", "report.txt"], cwd=folder, check=True
        )
    return "Done."


@pytest.mark.parametrize("task", bench.TASKS, ids=lambda t: t.id)
def test_checks_pass_a_correct_solution(task, tmp_path):
    answer = solve(task.id, setup(task, tmp_path))
    result = task.check(tmp_path, answer)
    assert result.passed, result.detail


@pytest.mark.parametrize("task", bench.TASKS, ids=lambda t: t.id)
def test_checks_fail_when_nothing_was_done(task, tmp_path):
    result = task.check(setup(task, tmp_path), "I'm not sure.")
    assert not result.passed and result.detail


def test_fix_bug_rejects_changed_tests(tmp_path):
    task = bench.select_tasks("fix-bug")[0]
    setup(task, tmp_path)
    replace(tmp_path / "test_cart.py", "4.25", "2.75")
    assert bench.check_fix_bug(tmp_path, "").detail == "test_cart.py was changed; the fix belongs in cart.py"


def test_find_code_needs_the_right_line(tmp_path):
    assert "cited backoff.py:3" in bench.check_find_code(tmp_path, "`next_wait` at uploader/backoff.py:3").detail
    assert bench.check_find_code(tmp_path, "It's `jitter`.").detail == "named jitter instead of next_wait"


def test_write_script_is_checked_on_unseen_data(tmp_path):
    task = bench.select_tasks("write-script")[0]
    setup(task, tmp_path)
    hard_coded = "".join(f"print({line!r})\n" for line in bench.expected_stats(bench.NUMBERS))
    (tmp_path / "stats.py").write_text(hard_coded)
    result = task.check(tmp_path, "")
    assert not result.passed and "wrong output on other data" in result.detail


def test_rename_catches_leftovers(tmp_path):
    task = bench.select_tasks("rename")[0]
    setup(task, tmp_path)
    for name in ("shop/users.py", "shop/orders.py", "test_shop.py"):
        replace(tmp_path / name, "get_user_name", "display_name")
    assert task.check(tmp_path, "").detail == "get_user_name is still used in shop/emails.py"


def test_precise_edit_rejects_extra_changes(tmp_path):
    task = bench.select_tasks("precise-edit")[0]
    setup(task, tmp_path)
    solve("precise-edit", tmp_path)
    replace(tmp_path / "services.py", '"retries": 5', '"retries": 6')
    result = task.check(tmp_path, "")
    assert not result.passed and "instead of 1" in result.detail


def probe() -> list[dict]:
    """The reply to the prompt-speed measurement: 4,000 tokens in 2 seconds."""
    done = {"done": True, "prompt_eval_count": 4000, "prompt_eval_duration": 2_000_000_000}
    return [{"message": {"role": "assistant", "content": "OK"}, **done}]


def fix_bug_script() -> list[list[dict]]:
    return [
        probe(),
        probe(),
        reply(tool_calls=[call("edit_file", path="cart.py", old_string="x", new_string="y")]),  # not read yet
        reply(tool_calls=[call("read_file", path="cart.py")]),
        reply(
            tool_calls=[
                call("edit_file", path="cart.py", old_string="sum(price for", new_string="sum(price * quantity for")
            ]
        ),
        reply(tool_calls=[call("bash", command=f"{sys.executable} -m unittest -q")]),
        reply("Fixed: `total` ignored quantities."),
    ]


def settings() -> Settings:
    return Settings(model="lcode-qwen3.6-35b", context=32768, permission_mode="yolo", web="off", checkpoints=False)


def quiet() -> Console:
    return Console(file=io.StringIO(), width=120, force_terminal=False)


def test_run_model_scores_tasks_and_measures_speed(monkeypatch, tmp_path):
    monkeypatch.setattr(bench.tempfile, "tempdir", str(tmp_path))
    console = quiet()
    tasks = bench.select_tasks("fix-bug,find-code")
    ollama = FakeOllama([*fix_bug_script(), reply("It is `jitter` in backoff.py:9.")])
    run = bench.run_model(ollama, "qwen3.6-35b", settings(), tasks, console)
    assert [(t.id, t.passed) for t in run.tasks] == [("fix-bug", True), ("find-code", False)]
    fix = run.tasks[0]
    assert (fix.steps, fix.tool_errors, fix.detail) == (5, 1, "the tests pass")
    assert run.generation_tps == 50.0 and run.prompt_tps == 2000.0 and run.passed == 1
    assert (run.memory_gb, run.gpu_percent) == (23.0, 50)
    text = console.file.getvalue()
    assert "✓ fix-bug" in text and "✗ find-code" in text and "named jitter instead of next_wait" in text
    assert not list(tmp_path.iterdir())  # the task folders are cleaned up


class SlowOllama(FakeOllama):
    def chat_stream(self, payload):
        time.sleep(10)
        yield from ()


def test_tasks_stop_at_the_time_limit(monkeypatch):
    monkeypatch.setattr(bench, "prompt_speed", lambda *args: None)
    started = time.monotonic()
    run = bench.run_model(SlowOllama(), "slow", settings(), bench.select_tasks("fix-bug"), quiet(), timeout=1)
    assert time.monotonic() - started < 5
    assert not run.tasks[0].passed and run.tasks[0].detail == "timed out after 1s"
    assert not run.interrupted


class BrokenOllama(FakeOllama):
    def chat_stream(self, payload):
        from lcode.ollama import OllamaError

        raise OllamaError("Ollama error 500: something broke")
        yield


def test_ollama_errors_fail_the_task_and_the_run_goes_on():
    run = bench.run_model(BrokenOllama(), "broken", settings(), bench.select_tasks("fix-bug,rename"), quiet())
    assert [t.detail for t in run.tasks] == ["Ollama error 500: something broke"] * 2


def test_report_and_markdown(tmp_path):
    tasks = bench.select_tasks("fix-bug,find-code")
    run = bench.run_model(FakeOllama([*fix_bug_script(), reply("`jitter`")]), "qwen3.6-35b", settings(), tasks, quiet())
    skipped = bench.ModelRun("qwen3.5-9b", "qwen3.5-9b", error="not installed")
    document = bench.report([run, skipped], LAPTOP, "0.32.15")
    bench.write_json(tmp_path / "r.json", document)
    data = json.loads((tmp_path / "r.json").read_text())
    assert data["schema"] == 1 and data["ollama"] == "0.32.15"
    assert data["hardware"]["gpu"] == "RTX 4080 Laptop"
    first = data["runs"][0]
    assert (first["passed"], first["total"], first["generation_tps"], first["context"]) == (1, 2, 50.0, 32768)
    assert first["prompt_tps"] == 2000.0
    assert set(first["tasks"][0]) == {"id", "passed", "seconds", "detail", "steps", "tool_errors", "error"}
    assert data["runs"][1]["error"] == "not installed"

    md = bench.markdown([run, skipped], tasks, LAPTOP, "0.32.15")
    assert "| | `qwen3.6-35b` | `qwen3.5-9b` |" in md
    assert "| **Passed** | **1/2** | **—** |" in md
    assert "| fix-bug: Fix a bug so the tests pass | ✓ " in md


def test_select_tasks():
    assert [t.id for t in bench.select_tasks(None)] == [t.id for t in bench.TASKS]
    assert [t.id for t in bench.select_tasks("rename, fix-bug")] == ["rename", "fix-bug"]
    with pytest.raises(ValueError, match="unknown task"):
        bench.select_tasks("fix-bug,nope")


def test_spec_fix_names_the_cases_still_wrong(tmp_path):
    task = bench.select_tasks("spec-fix")[0]
    setup(task, tmp_path)
    replace(tmp_path / "durations.py", "total = int", "total += int")  # only the reported symptom
    result = task.check(tmp_path, "")
    assert not result.passed and result.detail.startswith("wrong for ") and "'1H 30m'" in result.detail


def test_feature_needs_old_todos_to_keep_working(tmp_path):
    task = bench.select_tasks("feature")[0]
    setup(task, tmp_path)
    solve("feature", tmp_path)
    replace(tmp_path / "todo.py", "t.get('priority', 'normal')", "t['priority']")  # old todos have none
    result = task.check(tmp_path, "")
    assert not result.passed and "todo.py list" in result.detail


class InterruptedOllama(FakeOllama):
    def chat_stream(self, payload):
        raise KeyboardInterrupt
        yield


def test_ctrl_c_stops_the_run_and_cleans_up(monkeypatch, tmp_path):
    monkeypatch.setattr(bench.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(bench, "prompt_speed", lambda *args: None)
    run = bench.run_model(InterruptedOllama(), "m", settings(), bench.select_tasks("fix-bug,rename"), quiet())
    assert run.interrupted and run.tasks == []
    assert not list(tmp_path.iterdir())  # the stopped task's folder is gone too
    assert dict(bench.summary_rows([run], bench.select_tasks("fix-bug,rename")))["Passed"] == ["0/0 (stopped)"]


def test_session_runs_all_tasks_in_one_conversation(monkeypatch, tmp_path):
    monkeypatch.setattr(bench.tempfile, "tempdir", str(tmp_path))
    tasks = bench.select_tasks("fix-bug,find-code")
    ollama = FakeOllama([*fix_bug_script(), reply("It is `next_wait` in backoff.py:13.")])
    run = bench.run_model(ollama, "qwen3.6-35b", settings(), tasks, quiet(), session=True)
    assert [t.passed for t in run.tasks] == [True, True]
    assert (run.tasks[0].steps, run.tasks[1].steps) == (5, 1)  # counted per task, not cumulatively
    last = ollama.payloads[-1]["messages"]
    assert any("Fix the bug" in str(m.get("content")) or "cart.py" in str(m.get("content")) for m in last[1:4])
    assert "lcode-bench-find-code-" in last[0]["content"]  # the system prompt moved on to the second folder
    assert run.session and (run.pruned, run.compacted) == (0, 0)
    rows = dict(bench.summary_rows([run], tasks))
    assert rows["One conversation"] == ["pruned 0×, summarized 0×"]


def test_rounds_get_one_row_per_task_with_a_mark_per_run(monkeypatch, tmp_path):
    monkeypatch.setattr(bench.tempfile, "tempdir", str(tmp_path))
    tasks = bench.select_tasks("find-code") * 2
    ollama = FakeOllama([probe(), probe(), reply("`next_wait` in backoff.py:13"), reply("no idea")])
    run = bench.run_model(ollama, "m", settings(), tasks, quiet(), session=True)
    rows = dict(bench.summary_rows([run], tasks))
    assert rows["find-code: Answer with file:line"][0].startswith("✓✗ ")
    assert rows["Passed"] == ["1/2"]
