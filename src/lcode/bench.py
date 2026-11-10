"""lcode bench: score models on small, self-contained coding tasks on this machine.

Each task runs in a fresh temporary folder in `yolo` mode without web access, and ends with an
automatic check of the files or the answer. Some checks rerun the model's code on data it never
saw, so hard-coding the expected output doesn't pass.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import random
import shutil
import signal
import statistics
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from lcode import __version__, escalate, residency
from lcode.agent import Agent, Settings
from lcode.bench_checks import Check, Task
from lcode.bench_quick import TASKS
from lcode.config import format_tokens
from lcode.hardware import GIB, Hardware
from lcode.ollama import Ollama, OllamaError

SCHEMA_VERSION = 1
DEFAULT_CONTEXT = 32768  # the same everywhere, so results compare across machines
LONGER_TIMEOUT = 600  # for the longer tasks, which take a strong model up to five minutes
DEFAULT_ATTEMPTS = 3  # each task's attempts: one run is within the noise between runs
PROBE_WORDS = 3000  # about 4,000 tokens
PROBE_TEXT = (
    "file function value error test module class return import list string number change build user "
    "server request cache memory thread order parse check config option result folder branch commit"
)


class TaskTimeout(KeyboardInterrupt):
    """Raised when a task runs out of time. The agent stops running commands just like on Ctrl+C."""


# ----------------------------------------------------------------------------- helpers for checks


# ----------------------------------------------------------------------------- 1. fix a bug


# ----------------------------------------------------------------------------- 2. find code


# ----------------------------------------------------------------------------- 3. write a script


# ----------------------------------------------------------------------------- 4. rename across files


# ----------------------------------------------------------------------------- 5. precise edit in a long file


# ----------------------------------------------------------------------------- 6. recover from errors


# ----------------------------------------------------------------------------- 7. fix to a written spec


# ----------------------------------------------------------------------------- 8. a feature across files


# ----------------------------------------------------------------------------- the suite


def extra_tasks() -> list[Task]:
    """Tasks that only run when asked for with --tasks (for optional features such as --code-search)."""
    from lcode import bench_backend

    return [Task("find-concept", "Find code by what it does (40 files)", *parts(bench_backend))]


def work_tasks() -> list[Task]:
    """Tasks closer to real work, after the quick ones: longer, and where good models still slip."""
    from lcode import bench_budgets, bench_dedupe, bench_flaky, bench_logsum, bench_quotes, bench_runtime

    return [
        Task("cli-tool", "Build a tool from a spec", *parts(bench_logsum), LONGER_TIMEOUT),
        Task("flaky-test", "Find why a test is flaky", *parts(bench_flaky), LONGER_TIMEOUT),
        Task("repo-bug", "Fix a bug in a 47-file repo", *parts(bench_quotes), LONGER_TIMEOUT),
        Task("wide-feature", "Add a feature across modules", *parts(bench_budgets), LONGER_TIMEOUT),
        Task("fast-dedupe", "Make a slow job fast, same output", *parts(bench_dedupe), LONGER_TIMEOUT),
        Task("runtime-bug", "Fix a bug that shows only when it runs", *parts(bench_runtime), LONGER_TIMEOUT),
    ]


def parts(module) -> tuple[str, dict[str, str], Callable[[Path, str], Check]]:
    """A task module's prompt, files and check."""
    return module.PROMPT, module.FILES, module.check


def default_tasks() -> list[Task]:
    """What `lcode bench` runs: the quick tasks, then the longer ones."""
    return [*TASKS, *work_tasks()]


def select_tasks(ids: str | None) -> list[Task]:
    if not ids:
        return default_tasks()
    wanted = [i.strip() for i in ids.split(",") if i.strip()]
    known = {task.id: task for task in [*default_tasks(), *extra_tasks()]}
    unknown = [i for i in wanted if i not in known]
    if unknown:
        raise ValueError(f"unknown task(s) {', '.join(unknown)}; choose from {', '.join(known)}")
    return [known[i] for i in wanted]


# ----------------------------------------------------------------------------- running


@dataclass
class TaskResult:
    id: str
    passed: bool
    seconds: float
    detail: str
    steps: int = 0  # model responses
    tool_errors: int = 0  # failed tool calls, including malformed ones
    error: str | None = None  # why the run stopped early: a timeout or an Ollama error
    errors_by_tool: dict[str, int] = field(default_factory=dict)  # which tools failed, "malformed" for unreadable calls
    first_passed: bool | None = None  # with --retry: whether the first try passed (None: no retry was needed)
    escalations: int = 0  # times the stronger model was asked (--escalate)
    attempt: int = 1  # which of the task's attempts this was (--attempts)


@dataclass
class PassRates:
    """How often the tasks passed, as `lcode bench --harbor` counts it (lcode.harbor_bench.summarize)."""

    tries: int  # k: each task's tries, its attempts (times the rounds of a --session)
    pass_at_1: float | None  # the share of tries that passed, averaged over the tasks
    pass_all: float | None  # pass^k: the share of tasks that passed every try (of those with all k)
    attempts: list[tuple[int, int]]  # (passed, tasks run) in each attempt, in order

    @property
    def runs(self) -> list[float]:
        """The pass rate of each attempt that ran every task."""
        full = max((ran for _, ran in self.attempts), default=0)
        return [passed / ran for passed, ran in self.attempts if ran == full and ran]

    @property
    def spread(self) -> float | None:
        runs = self.runs
        return max(runs) - min(runs) if len(runs) > 1 else None


def pass_rates(results: list[TaskResult], attempts: int) -> PassRates:
    tries: dict[str, list[bool]] = {}
    for result in results:
        tries.setdefault(result.id, []).append(result.passed)
    per_attempt = Counter((result.id, result.attempt) for result in results)
    k = max(1, attempts) * max(per_attempt.values(), default=1)
    full = [passed for passed in tries.values() if len(passed) >= k]
    by_attempt: dict[int, list[bool]] = {}
    for result in results:
        by_attempt.setdefault(result.attempt, []).append(result.passed)
    return PassRates(
        tries=k,
        pass_at_1=statistics.fmean(statistics.fmean(passed) for passed in tries.values()) if tries else None,
        pass_all=statistics.fmean(all(passed[:k]) for passed in full) if full else None,
        attempts=[(sum(passed), len(passed)) for _, passed in sorted(by_attempt.items())],
    )


@dataclass
class ModelRun:
    model: str  # as given: a catalog key or Ollama tag
    ollama_model: str
    context: int = 0
    num_batch: int | None = None
    think: bool = True
    load_seconds: float = 0.0
    prompt_tps: float | None = None  # measured with a fresh prompt; see prompt_speed()
    memory_gb: float | None = None  # as Ollama's /api/ps counts it, which can leave part of the model out
    gpu_percent: int | None = None  # of memory_gb, on the GPU
    estimated_memory_gib: float | None = None  # all of the model by the catalog's estimate, when Ollama leaves part out
    estimated_gpu_percent: int | None = None  # of estimated_memory_gib, on the GPU
    usage: dict = field(default_factory=dict)
    tasks: list[TaskResult] = field(default_factory=list)
    error: str | None = None  # the model couldn't run at all
    interrupted: bool = False
    session: bool = False  # all tasks in one conversation (--session)
    pruned: int = 0  # times old tool output was removed to make room
    compacted: int = 0  # times the conversation was summarized
    attempts: int = 1  # each task's attempts, each in a new folder and conversation (--attempts)

    def rates(self) -> PassRates:
        return pass_rates(self.tasks, self.attempts)

    @property
    def passed(self) -> int:
        return sum(t.passed for t in self.tasks)

    @property
    def seconds(self) -> float:
        return sum(t.seconds for t in self.tasks)

    @property
    def generation_tps(self) -> float | None:
        ns = self.usage.get("output_ns", 0)
        return round(self.usage["output_tokens"] / (ns / 1e9), 1) if ns else None

    @property
    def tool_errors(self) -> int:
        return sum(t.tool_errors for t in self.tasks)

    def note_memory(self, where: residency.Placement) -> None:
        """Ollama's count keeps the names it always had, so results compare with older ones. The estimate that the line
        after the load shows goes next to it."""
        self.memory_gb, self.gpu_percent = round(where.size / 1e9, 1), round(100 * where.vram / where.size)
        if where.estimate > where.size:
            self.estimated_memory_gib = round(where.estimate / GIB, 1)
            self.estimated_gpu_percent = round(100 * where.gpu_share)

    def to_json(self) -> dict:
        data = asdict(self)
        rates = self.rates()
        data.update(
            passed=self.passed,
            total=len(self.tasks),
            seconds=round(self.seconds, 1),
            generation_tps=self.generation_tps,
            tool_errors=self.tool_errors,
            summary={
                "pass_at_1": rates.pass_at_1,
                f"pass^{rates.tries}": rates.pass_all,
                "runs": rates.runs,
                "spread": rates.spread,
            },
        )
        return data


@contextmanager
def time_limit(seconds: float) -> Iterator[None]:
    def expire(signum, frame):
        raise TaskTimeout

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class Elapsed:
    def __init__(self, label: str):
        self.label, self.start = label, time.monotonic()

    def __rich__(self) -> Text:
        return Text(f"{self.label}  {time.monotonic() - self.start:.0f}s", style="dim")


def errors_by_tool(messages: list[dict]) -> dict[str, int]:
    """Failed tool calls by tool, and "malformed" for calls the model wrote unreadably."""
    counts: dict[str, int] = {}
    for m in messages:
        if m.get("role") == "tool" and m.get("content", "").startswith("Error:"):
            name = m.get("tool_name") or "?"
            counts[name] = counts.get(name, 0) + 1
        elif m.get("role") == "user" and m.get("content", "").startswith("[lcode] Your last tool call"):
            counts["malformed"] = counts.get("malformed", 0) + 1
    return counts


def count_tool_errors(messages: list[dict]) -> int:
    return sum(errors_by_tool(messages).values())


def run_task(
    ollama: Ollama,
    settings: Settings,
    task: Task,
    timeout: float,
    log: Console,
    keep: bool,
    usage: dict,
    agent: Agent | None = None,
    retry: bool = False,
) -> TaskResult:
    """Run one task in a new folder: in a new conversation, or continuing `agent`'s (--session).

    With `retry`, a failed task gets one more try, as a user would ask: "That didn't fix it. Look again." With a
    stronger model set (--escalate), its advice goes along, as with /escalate.
    """
    folder = Path(tempfile.mkdtemp(prefix=f"lcode-bench-{task.id}-"))
    try:
        for rel, content in task.files.items():
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_text(content)
        if agent is None:
            agent = Agent(ollama, settings, folder, console=log)
        else:  # the same conversation moves on to this task's folder, as with /cd
            agent.cwd = agent.workspace = folder.resolve()
            agent.messages[0]["content"] = agent.system_prompt()
        if settings.embed_model != "off":  # --code-search: index the task's folder first, outside the timer
            from lcode import codesearch

            model = codesearch.pick_model(ollama, settings.embed_model)
            if model:
                codesearch.Index(folder, model).update(ollama)  # on the CPU: lcode's model stays loaded
            agent._code_index = None
        agent.escalate_approved = True if settings.escalate_model else None  # the --escalate flag is the OK
        before = dict(agent.usage)
        first = len(agent.messages)
        started = time.monotonic()
        escalations = 0
        error = attempt(agent, task.prompt, timeout)
        escalations += len(agent.escalations)
        check = checked(task, folder, agent)
        first_passed = None
        if retry and not (check.passed and error is None):
            first_passed = False
            text = RETRY
            if error is None and agent.can_escalate():
                advice = escalate.run(agent, "the user says the fix didn't work")
                escalations += 1 if advice else 0
                text = f"{RETRY}\n\n{advice}" if advice else RETRY
            error = attempt(agent, text, timeout)
            escalations += len(agent.escalations)
            check = checked(task, folder, agent)
        seconds = time.monotonic() - started
        for key, value in agent.usage.items():
            usage[key] = usage.get(key, 0) + value - before.get(key, 0)
    finally:  # also when Ctrl+C stops the task
        from lcode.codesearch import index_dir

        shutil.rmtree(index_dir(folder), ignore_errors=True)
        if keep:
            (folder / "lcode-bench.log").write_text(log.file.getvalue() if isinstance(log.file, io.StringIO) else "")
        else:
            shutil.rmtree(folder, ignore_errors=True)
    return TaskResult(
        id=task.id,
        passed=check.passed and error is None,
        seconds=round(seconds, 1),
        detail=error or check.detail,
        steps=agent.usage["requests"] - before.get("requests", 0),
        tool_errors=count_tool_errors(agent.messages[first:] if len(agent.messages) > first else agent.messages),
        errors_by_tool=errors_by_tool(agent.messages[first:] if len(agent.messages) > first else agent.messages),
        error=error,
        first_passed=first_passed,
        escalations=escalations,
    )


RETRY = "That didn't fix it: it still doesn't work right. Look again."


def attempt(agent: Agent, text: str, timeout: float) -> str | None:
    """One request of a task; why it stopped early, if it did."""
    try:
        with time_limit(timeout):
            agent.run_turn(text)
    except TaskTimeout:
        return f"timed out after {timeout:.0f}s"
    except OllamaError as e:
        return str(e).splitlines()[0][:200]
    return None


def checked(task: Task, folder: Path, agent: Agent) -> Check:
    answer = next((m.get("content", "") for m in reversed(agent.messages) if m.get("role") == "assistant"), "")
    try:
        return task.check(folder, answer)
    except Exception as e:  # a check must never crash the benchmark
        return Check(False, f"check failed: {type(e).__name__}: {e}")


def prompt_speed(ollama: Ollama, settings: Settings, options: dict) -> float | None:
    """Prompt tokens per second for prompts the model hasn't seen: the faster of two readings.

    Measured separately: the tasks' requests mostly repeat the conversation so far, which Ollama
    serves from its prompt cache, and counting those tokens would overstate the speed. The first
    reading after loading can be much slower while the model warms up, hence two.
    """
    readings = [_read_prompt(ollama, settings, options) for _ in range(2)]
    return max((r for r in readings if r), default=None)


def _read_prompt(ollama: Ollama, settings: Settings, options: dict) -> float | None:
    rng = random.Random()
    vocabulary = PROBE_TEXT.split()
    words = " ".join(rng.choice(vocabulary) for _ in range(PROBE_WORDS))
    payload = {
        "model": settings.model,
        "messages": [{"role": "user", "content": f"{rng.random()}\n{words}\n\nReply with OK."}],
        "think": False,
        "keep_alive": settings.keep_alive,
        "options": {**options, "num_predict": 1},
    }
    final: dict = {}
    try:
        with time_limit(120):
            for chunk in ollama.chat_stream(payload):
                if chunk.get("done"):
                    final = chunk
    except (OllamaError, TaskTimeout):
        return None
    count, ns = final.get("prompt_eval_count", 0), final.get("prompt_eval_duration", 0)
    return round(count / (ns / 1e9), 1) if ns and count >= 1000 else None


def memory_use(ollama: Ollama, model: str, context: int) -> residency.Placement | None:
    """How the loaded model sits in memory, counted as the line after a load counts it: Ollama 0.40 leaves out the
    part of a Mixture-of-Experts model that stays mapped from its file in RAM (see residency.with_estimate)."""
    where = residency.placement(ollama, model)  # Ollama lists lcode-qwen3.6-35b as lcode-qwen3.6-35b:latest
    return residency.with_estimate(where, model, context) if where and where.size else None


def run_model(
    ollama: Ollama,
    name: str,
    settings: Settings,
    tasks: list[Task],
    console: Console,
    timeout: float | None = None,
    keep: bool = False,
    verbose: bool = False,
    session: bool = False,
    retry: bool = False,
    attempts: int = 1,
) -> ModelRun:
    """Run the tasks `attempts` times: every task once, then every task again, each time in a new folder and
    conversation (with `session`, each attempt is one conversation for all the tasks)."""
    attempts = max(1, attempts)
    run = ModelRun(
        name, settings.model, settings.context, settings.num_batch, settings.think, session=session, attempts=attempts
    )
    shared: Agent | None = None  # with --session, one conversation for all tasks
    console.print(
        f"\n[bold]{escape(settings.model)}[/] · {format_tokens(settings.context)} context · "
        f"reasoning {'on' if settings.think else 'off'} · {len(tasks)} task{'' if len(tasks) == 1 else 's'}"
        + (f" · {attempts} attempts each" if attempts > 1 else "")
    )
    options = {"num_ctx": settings.context, **({"num_batch": settings.num_batch} if settings.num_batch else {})}
    started = time.monotonic()
    try:
        with console.status(Elapsed("  Loading the model")):
            ollama.load(settings.model, options, settings.keep_alive)
    except OllamaError as e:
        console.print(f"  [yellow]Loading failed ({escape(str(e).splitlines()[0][:150])}); the tasks will retry.[/]")
    except KeyboardInterrupt:
        run.interrupted = True
        return run
    run.load_seconds = round(time.monotonic() - started, 1)
    if placed := memory_use(ollama, settings.model, settings.context):
        run.note_memory(placed)
    try:
        with console.status(Elapsed("  Measuring prompt speed")):
            run.prompt_tps = prompt_speed(ollama, settings, options)
    except KeyboardInterrupt:
        run.interrupted = True
        return run
    where = f" · {placed.describe()}" if placed else ""
    reads = f" · reads prompts at {run.prompt_tps:,.0f} tok/s" if run.prompt_tps else ""
    if getattr(ollama, "kind", "ollama") == "ollama":
        console.print(f"  [dim]Loaded in {run.load_seconds:.0f}s{where}{reads}[/]")
    elif reads:  # the server loads the model itself
        console.print(f"  [dim]{reads.removeprefix(' · ').capitalize()}[/]")
    pruned = compacted = 0  # in the conversations of the earlier attempts (--session)
    for attempt in range(1, attempts + 1):
        if attempts > 1:
            console.print(f"  [dim]Attempt {attempt} of {attempts}[/]")
        if shared is not None:  # with --session, each attempt is a conversation of its own
            pruned, compacted = pruned + shared.pruned, compacted + shared.compacted
            shared = None
        for task in tasks:
            limit = timeout or task.timeout  # --timeout, or the task's own
            log = console if verbose else Console(file=io.StringIO(), width=120, force_terminal=False)
            if session and shared is None:
                shared = Agent(ollama, settings, Path(tempfile.gettempdir()), console=log)
            if shared is not None:
                shared.console = log
            try:
                if verbose:
                    console.rule(f"{task.id}: {task.title}")
                    result = run_task(ollama, settings, task, limit, log, keep, run.usage, shared, retry)
                else:
                    with console.status(Elapsed(f"  {task.id:<13} {task.title}")):
                        result = run_task(ollama, settings, task, limit, log, keep, run.usage, shared, retry)
            except KeyboardInterrupt:
                console.print("  [yellow]Stopped.[/]")
                run.interrupted = True
                break
            result.attempt = attempt
            run.tasks.append(result)
            mark = "[green]✓[/]" if result.passed else "[red]✗[/]"
            console.print(
                f"  {mark} {task.id:<13} {task.title:<32} {result.seconds:>5.0f}s  [dim]{escape(result.detail)}[/]"
            )
            if shared is not None:
                run.pruned, run.compacted = pruned + shared.pruned, compacted + shared.compacted
        if run.interrupted:
            break
    if run.memory_gb is None and (placed := memory_use(ollama, settings.model, settings.context)):
        run.note_memory(placed)
    # lcode lowers the batch size or context if the GPU runs out of memory; report what was used.
    run.context, run.num_batch, run.think = settings.context, settings.num_batch, settings.think
    return run


# ----------------------------------------------------------------------------- reporting


def duration(seconds: float) -> str:
    return f"{int(seconds // 60)}m {seconds % 60:02.0f}s" if seconds >= 60 else f"{seconds:.0f}s"


def percent(share: float | None) -> str:
    return "–" if share is None else f"{share * 100:.0f}%"


def memory(run: ModelRun) -> str:
    """'~23 GB, 42% on GPU' by lcode's estimate, as the line after the load counts it, or else what Ollama says."""
    if run.estimated_memory_gib:
        return f"~{run.estimated_memory_gib:.0f} GB, {run.estimated_gpu_percent}% on GPU"
    return f"{run.memory_gb} GB, {run.gpu_percent}% on GPU" if run.memory_gb else "?"


def summary_rows(runs: list[ModelRun], tasks: list[Task]) -> list[tuple[str, list[str]]]:
    rows = []
    for task in {t.id: t for t in tasks}.values():  # a task that ran several times: one row, a mark each
        cells = []
        for run in runs:
            results = [t for t in run.tasks if t.id == task.id]
            marks = "".join("✓" if r.passed else "✗" for r in results)
            each = sum(r.seconds for r in results) / len(results) if results else 0.0  # one try's time, on average
            cells.append(f"{marks} {duration(each)}" if results else "")
        rows.append((f"{task.id}: {task.title}", cells))

    def cell(run: ModelRun, value: str) -> str:
        return "—" if run.error else value

    rows.append(
        ("Passed", [cell(r, f"{r.passed}/{len(r.tasks)}" + (" (stopped)" if r.interrupted else "")) for r in runs])
    )
    rates = [(run, run.rates()) for run in runs]
    tries = max((rate.tries for _, rate in rates), default=1)
    if tries > 1:
        rows += [
            ("pass@1", [cell(run, percent(rate.pass_at_1)) for run, rate in rates]),
            (f"pass^{tries}", [cell(run, percent(rate.pass_all)) for run, rate in rates]),
        ]
    if any(len(rate.attempts) > 1 for _, rate in rates):
        rows.append(
            ("Each attempt", [cell(run, ", ".join(f"{p}/{n}" for p, n in rate.attempts)) for run, rate in rates])
        )
    rows += [
        ("Time", [cell(r, duration(r.seconds)) for r in runs]),
        ("Generation", [cell(r, f"{r.generation_tps:.1f} tok/s" if r.generation_tps else "?") for r in runs]),
        ("Prompt reading", [cell(r, f"{r.prompt_tps:,.0f} tok/s" if r.prompt_tps else "?") for r in runs]),
        ("Tool-call errors", [cell(r, str(r.tool_errors)) for r in runs]),
        ("Memory", [cell(r, memory(r)) for r in runs]),
        ("Context", [cell(r, format_tokens(r.context)) for r in runs]),
    ]
    if any(r.session for r in runs):
        rows.append(
            (
                "One conversation",
                [cell(r, f"pruned {r.pruned}×, summarized {r.compacted}×" if r.session else "no") for r in runs],
            )
        )
    return rows


def print_summary(console: Console, runs: list[ModelRun], tasks: list[Task]) -> None:
    table = Table(title="lcode bench", title_justify="left", header_style="bold")
    table.add_column("")
    for run in runs:
        table.add_column(escape(run.model), justify="right")
    for label, cells in summary_rows(runs, tasks):
        style = "bold" if label == "Passed" else ""
        table.add_row(escape(label), *[escape(c) for c in cells], style=style)
    console.print()
    console.print(table)
    for run in runs:
        if run.error:
            console.print(f"[yellow]{escape(run.model)}: {escape(run.error)}[/]")


def report(runs: list[ModelRun], hardware: Hardware, ollama_version: str, server: str = "") -> dict:
    """The JSON document written by --json (schema version 1)."""
    return {
        "schema": SCHEMA_VERSION,
        "lcode": __version__,
        "ollama": None if server else ollama_version,
        "server": server or f"Ollama {ollama_version}",
        "date": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "hardware": {**asdict(hardware), "description": hardware.describe()},
        "tasks": [{"id": t.id, "title": t.title} for t in default_tasks()],
        "runs": [run.to_json() for run in runs],
    }


def markdown(runs: list[ModelRun], tasks: list[Task], hardware: Hardware, ollama_version: str, server: str = "") -> str:
    """A table to paste into a model test report."""
    date = dt.date.today().isoformat()
    server = server or f"Ollama {ollama_version}"
    lines = [
        f"**lcode bench** · lcode {__version__} · {server} · {hardware.describe()} · {date}",
        "",
        "| | " + " | ".join(f"`{r.model}`" for r in runs) + " |",
        "|---|" + "---|" * len(runs),
    ]
    for label, cells in summary_rows(runs, tasks):
        bold = label == "Passed"
        label_md = f"**{label}**" if bold else label
        lines.append(f"| {label_md} | " + " | ".join(f"**{c}**" if bold and c else c for c in cells) + " |")
    return "\n".join(lines) + "\n"


def write_json(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, indent=2) + "\n")


def print_tasks(console: Console) -> None:
    table = Table(title="lcode bench tasks", title_justify="left", header_style="bold")
    table.add_column("Task", style="cyan")
    table.add_column("What it tests")
    table.add_column("Runs", style="dim")
    for tasks, when in [(TASKS, "always"), (work_tasks(), "not with --quick"), (extra_tasks(), "only with --tasks")]:
        for task in tasks:
            table.add_row(task.id, task.title, when)
    console.print(table)
    console.print(
        "[dim]--quick runs the quick ones once; --tasks picks some, e.g. lcode bench --tasks fix-bug,rename[/]"
    )
