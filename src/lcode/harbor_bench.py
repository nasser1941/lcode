"""Public benchmarks through Harbor: Terminal-Bench 2.x and SWE-bench Verified (`lcode bench --harbor`).

Harbor runs each task in its own container and checks the result with the task's tests. lcode is
installed in that container (`lcode.harbor.Lcode`) and solves the task as one request, talking to
the model server on your machine. This module has the parts that don't need Harbor installed: the
commands that run in the container, the task slices, running Harbor with uvx, and the report.

The slices were drawn once, at random with seed 197 and by difficulty, from the tasks a local
model can try overnight: Terminal-Bench 2.0 without model training and with at most 30 minutes per
task, and SWE-bench Verified's two common difficulties. The dev slice is for everyday checks; the
held-out set is never used for tuning, only to confirm a change helps beyond the tasks it was
tuned on.
"""

from __future__ import annotations

import datetime as dt
import json
import shlex
import statistics
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

HARBOR = "harbor==0.25.0"  # the Harbor these slices were made and checked with
HARBOR_PYTHON = "3.13"
AGENT = "lcode.harbor:Lcode"
DATASETS = {"terminal-bench": "terminal-bench@2.0", "swebench-verified": "swebench-verified"}
ATTEMPTS = 3
SLICES = {
    "dev": {
        "terminal-bench": [
            "cobol-modernization", "prove-plus-comm", "break-filter-js-from-html", "filter-js-from-html",
            "rstan-to-pystan", "crack-7z-hash", "build-cython-ext", "sqlite-with-gcov", "pypi-server",
            "openssl-selfsigned-cert", "constraints-scheduling", "financial-document-processor", "polyglot-rust-c",
            "sparql-university", "torch-tensor-parallelism",
        ],
        "swebench-verified": [
            "sympy__sympy-18189", "django__django-11555", "django__django-14792", "matplotlib__matplotlib-25311",
            "django__django-14787", "pydata__xarray-6721", "matplotlib__matplotlib-23299", "pytest-dev__pytest-10051",
            "sympy__sympy-23824", "django__django-12774",
        ],
    },
    "held-out": {
        "terminal-bench": [
            "overfull-hbox", "mteb-retrieve", "gcode-to-text", "largest-eigenval", "kv-store-grpc", "code-from-image",
            "chess-best-move", "hf-model-inference", "log-summary-date-ranges", "git-leak-recovery", "qemu-startup",
            "make-mips-interpreter", "write-compressor", "torch-pipeline-parallelism",
        ],
        "swebench-verified": [
            "pydata__xarray-4629", "pytest-dev__pytest-10081", "sphinx-doc__sphinx-9281", "django__django-16642",
            "django__django-13933", "django__django-15569", "scikit-learn__scikit-learn-10844",
            "scikit-learn__scikit-learn-11578", "matplotlib__matplotlib-24970", "scikit-learn__scikit-learn-14087",
            "mwaskom__seaborn-3187",
        ],
    },
}  # fmt: skip
# Puts the task on Docker's default network: for when Docker can't make networks ("all predefined address pools
# have been fully subnetted"), as with a VPN that routes all addresses.
BRIDGE_OVERLAY = "services:\n  main:\n    network_mode: bridge\n"

PYTHON = "3.12"  # lcode's own Python in the task containers, installed by uv
OLLAMA_PORT = 11434
HOME = "/logs/agent/lcode"  # lcode's sessions and traces: Harbor copies /logs/agent into the trial's folder
LOG = "/logs/agent/lcode.txt"
ON_PATH = 'export PATH="$HOME/.local/bin:$PATH";'  # where uv puts lcode and itself


def find_ollama(routes: str = "/proc/net/route") -> str:
    """Shell that sets OLLAMA_HOST, unless it's set, to Ollama on your machine as a task's container sees it:
    host.docker.internal where Docker provides it (Docker Desktop), else the container's default gateway, which is
    your machine (Linux). The gateway is in the routing table as little-endian hex: 010011AC is 172.17.0.1."""
    return (
        'if [ -z "${OLLAMA_HOST:-}" ]; then '
        "if getent hosts host.docker.internal >/dev/null 2>&1; then host=host.docker.internal; "
        f"else gw=$(awk '$2 == \"00000000\" {{ print $3; exit }}' {routes}); "
        'host=$(printf "%d.%d.%d.%d" "0x${gw:6:2}" "0x${gw:4:2}" "0x${gw:2:2}" "0x${gw:0:2}"); fi; '
        f'export OLLAMA_HOST="http://$host:{OLLAMA_PORT}"; fi;'
    )


def install_command(spec: str) -> str:
    """Install uv if the image has none, then lcode (`lcode-cli==X` or a wheel's path) with its own Python."""
    return (
        "set -euo pipefail; "
        "if ! command -v uv >/dev/null 2>&1; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi; "
        f"{ON_PATH} uv tool install --force --python {PYTHON} {shlex.quote(spec)}; lcode --version"
    )


def run_command(instruction: str, log: str = LOG) -> str:
    """The task as one lcode request, without questions; its output goes to the trial's agent folder too.

    lcode starts in an interactive shell, so the image's own shell setup applies to its commands, as in
    a terminal there: SWE-bench's images activate the project's conda environment only in ~/.bashrc,
    and without it `python` is conda's base, without the project's packages. lcode's folder goes last
    on PATH, so it never hides the task's own tools.
    """
    inner = f'export PATH="$PATH:$HOME/.local/bin"; {find_ollama()} lcode -p {shlex.quote(instruction)} --yolo'
    return f"bash -ic {shlex.quote(inner)} 2>&1 | tee {shlex.quote(log)}"


def run_env(model: str | None) -> dict[str, str]:
    env = {"LCODE_HOME": HOME, "COLUMNS": "120"}
    if model:
        env["LCODE_MODEL"] = model.removeprefix("ollama/")
    return env


def trace_usage(home: Path) -> dict | None:
    """How the newest request traced in `home` (lcode's LCODE_HOME in the task) went: its end event."""
    traces = sorted((home / "traces").glob("*.jsonl"), key=lambda path: path.stat().st_mtime)
    for path in reversed(traces):
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "end":
                return event
    return None


# ----------------------------------------------------------------------------- running Harbor, and the report


def harbor_argv(
    dataset: str,
    tasks: list[str],
    model: str,
    jobs: Path,
    job_name: str,
    *,
    attempts: int = ATTEMPTS,
    context: int | None = None,
    lcode_with: list[str] | None = None,
    wheel: Path | None = None,
    overlay: Path | None = None,
) -> list[str]:
    """`harbor run` for some tasks of a dataset, with uvx, and lcode next to Harbor so it can import the agent."""
    argv = ["uvx", "--python", HARBOR_PYTHON, "--from", HARBOR, *(lcode_with or []), "harbor", "run"]
    argv += ["-d", DATASETS[dataset], "-a", AGENT, "-m", model, "-k", str(attempts), "-n", "1"]
    argv += ["-o", str(jobs), "--job-name", job_name, "-y"]
    for task in tasks:
        argv += ["-i", task, "-i", f"*/{task}"]  # the name alone, or with its organization in front
    if context:
        argv += ["--ak", f"context={context}"]
    if wheel:
        argv += ["--ak", f"wheel={wheel}"]
    if overlay:
        argv += ["--extra-docker-compose", str(overlay)]
    return argv


@dataclass
class Trial:
    """One attempt at a task, from Harbor's result.json."""

    dataset: str
    task: str
    reward: float | None  # None: the task's tests didn't run (its container didn't start, say)
    started: str = ""
    seconds: float | None = None  # lcode's time on the task
    tokens_in: int = 0
    tokens_out: int = 0
    error: str = ""


def seconds_between(timing: dict | None) -> float | None:
    try:
        start = dt.datetime.fromisoformat(str(timing["started_at"]).replace("Z", "+00:00"))  # type: ignore[index]
        end = dt.datetime.fromisoformat(str(timing["finished_at"]).replace("Z", "+00:00"))  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return None
    return (end - start).total_seconds()


def read_trials(job: Path, dataset: str) -> list[Trial]:
    trials = []
    for path in sorted(job.glob("*/result.json")):
        try:
            result = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        rewards = (result.get("verifier_result") or {}).get("rewards")
        reward = rewards.get("reward") if isinstance(rewards, dict) else None
        agent = result.get("agent_result") or {}
        problem = result.get("exception_info") or {}
        error = str(problem.get("exception_type") or "")
        if error and problem.get("exception_message"):
            error += ": " + str(problem["exception_message"]).strip().splitlines()[-1][:300]
        name = str(result.get("task_name") or path.parent.name.rsplit("__", 1)[0])
        trials.append(
            Trial(
                dataset=dataset,
                task=name.rsplit("/", 1)[-1],
                reward=float(reward) if isinstance(reward, (int, float)) else None,
                started=str(result.get("started_at") or ""),
                seconds=seconds_between(result.get("agent_execution")),
                tokens_in=int(agent.get("n_input_tokens") or 0),
                tokens_out=int(agent.get("n_output_tokens") or 0),
                error=error,
            )
        )
    return trials


@dataclass
class TaskResult:
    dataset: str
    task: str
    passed: list[bool] = field(default_factory=list)  # each attempt whose tests ran, in the order they ran
    not_run: int = 0
    seconds: float | None = None  # per attempt, on average
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class Summary:
    tasks: list[TaskResult]
    attempts: int
    pass_at_1: float | None  # the share of attempts that passed, averaged over the tasks
    pass_all: float | None  # pass^k: the share of tasks whose every attempt passed (of those with all k)
    runs: list[float]  # the pass rate of each task's first attempt, of its second...
    not_run: int

    @property
    def spread(self) -> float | None:
        return max(self.runs) - min(self.runs) if len(self.runs) > 1 else None


def summarize(trials: list[Trial], attempts: int = ATTEMPTS) -> Summary:
    groups: dict[tuple[str, str], list[Trial]] = {}
    for trial in sorted(trials, key=lambda t: t.started):
        groups.setdefault((trial.dataset, trial.task), []).append(trial)
    tasks = []
    for (dataset, task), group in groups.items():
        ran = [t for t in group if t.reward is not None]
        timed = [t.seconds for t in ran if t.seconds is not None]
        tasks.append(
            TaskResult(
                dataset,
                task,
                [(t.reward or 0) >= 1 for t in ran],
                len(group) - len(ran),
                statistics.fmean(timed) if timed else None,
                round(statistics.fmean(t.tokens_in for t in ran)) if ran else 0,
                round(statistics.fmean(t.tokens_out for t in ran)) if ran else 0,
            )
        )
    scored = [t for t in tasks if t.passed]
    pass_at_1 = statistics.fmean(statistics.fmean(t.passed) for t in scored) if scored else None
    full = [t for t in scored if len(t.passed) >= attempts]
    pass_all = statistics.fmean(all(t.passed[:attempts]) for t in full) if full else None
    runs = []
    for i in range(attempts):
        nth = [t.passed[i] for t in scored if len(t.passed) > i]
        if nth:
            runs.append(statistics.fmean(nth))
    return Summary(tasks, attempts, pass_at_1, pass_all, runs, sum(t.not_run for t in tasks))


def percent(share: float | None) -> str:
    return "–" if share is None else f"{share * 100:.0f}%"


def print_summary(console, summary: Summary) -> None:
    from rich.table import Table

    table = Table(box=None, pad_edge=False)
    for column in ("Task", "Benchmark", "Passed", "Time", "Tokens in", "Tokens out"):
        table.add_column(column, justify="left" if column in ("Task", "Benchmark") else "right")
    for t in summary.tasks:
        passed = f"{sum(t.passed)}/{len(t.passed)}" + (f" ({t.not_run} didn't run)" if t.not_run else "")
        took = "–" if t.seconds is None else f"{t.seconds / 60:.1f} min"
        table.add_row(t.task, t.dataset, passed, took, f"{t.tokens_in:,}", f"{t.tokens_out:,}")
    console.print(table)
    k = summary.attempts
    line = f"pass@1 {percent(summary.pass_at_1)} · pass^{k} {percent(summary.pass_all)}"
    if summary.spread is not None:
        line += f" · runs {', '.join(percent(r) for r in summary.runs)} (spread {summary.spread * 100:.0f} points)"
    console.print(f"[bold]{line}[/]")
    if summary.not_run:
        console.print(f"[yellow]{summary.not_run} attempt(s) didn't run (their container didn't start, say).[/]")


def print_slice(console, name: str) -> None:
    for dataset, tasks in SLICES[name].items():
        console.print(f"[bold]{DATASETS[dataset]}[/] ({len(tasks)}): {', '.join(tasks)}")


def command(args, console) -> int:
    """`lcode bench --harbor SLICE`: run the slice's tasks with Harbor, then report and save the results."""
    import shutil
    import sys

    from rich.markup import escape

    from lcode import __version__, config, update
    from lcode.hardware import detect

    if args.list:
        print_slice(console, args.harbor)
        return 0
    if shutil.which("uvx") is None:
        console.print("[red]lcode bench --harbor runs Harbor with uvx. Install uv: https://docs.astral.sh/uv/[/]")
        return 1
    if not update.docker_available():
        console.print("[red]Harbor runs each task in a Docker container, and Docker isn't running. Start it first.[/]")
        return 1
    settings = config.load()
    model = args.models[0] if args.models else settings["model"]
    # Always given: in a task's container lcode sees no GPU and would pick a context for the CPU.
    context = config.parse_context(args.context or settings.get("context") or 32768)
    attempts = max(1, args.attempts)
    wanted = {name.strip() for name in args.tasks.split(",")} if args.tasks else None
    picked = {
        dataset: [t for t in tasks if wanted is None or t in wanted] for dataset, tasks in SLICES[args.harbor].items()
    }
    unknown = sorted((wanted or set()) - {t for tasks in picked.values() for t in tasks})
    if unknown:
        console.print(
            f"[red]Not in the {args.harbor} slice: {', '.join(unknown)}[/] (lcode bench --harbor {args.harbor} --list)"
        )
        return 2
    jobs = config.STATE_DIR / "bench" / f"{args.harbor}-{dt.datetime.now():%Y%m%d-%H%M%S}"
    jobs.mkdir(parents=True, exist_ok=True)
    install = update.lcode_install()
    wheel = None
    if install.kind == "checkout" and install.path:  # this checkout, in Harbor and in the containers
        lcode_with = ["--with-editable", str(install.path)]
        build = ["uv", "build", "--wheel", "-q", "-o", str(jobs / "wheel"), str(install.path)]
        if subprocess.run(build, capture_output=True, text=True).returncode != 0:
            console.print(f"[red]Couldn't build a wheel of {install.path}:[/] {' '.join(build)}")
            return 1
        wheel = next((jobs / "wheel").glob("*.whl"), None)
    else:
        lcode_with = ["--with", f"lcode-cli=={__version__}"]
    overlay = None
    if args.docker_bridge:
        overlay = jobs / "bridge.yaml"
        overlay.write_text(BRIDGE_OVERLAY)
    trials: list[Trial] = []
    for dataset, tasks in picked.items():
        if not tasks:
            continue
        argv = harbor_argv(
            dataset, tasks, model, jobs, dataset, attempts=attempts, context=context, lcode_with=lcode_with,
            wheel=wheel, overlay=overlay,
        )  # fmt: skip
        console.print(f"[bold]{DATASETS[dataset]}[/]: {len(tasks)} task(s), {attempts} attempt(s) each")
        console.print(f"[dim]{escape(shlex.join(argv))}[/]")
        subprocess.call(argv)
        trials += read_trials(jobs / dataset, dataset)
    summary = summarize(trials, attempts)
    print_summary(console, summary)
    if any("fully subnetted" in trial.error for trial in trials):
        console.print(
            "Docker couldn't make a network for the tasks: all its address ranges overlap your routes (a VPN that "
            "routes everything does that). Run again with [bold]--docker-bridge[/] to use Docker's default network."
        )
    report = {
        "schema": 1,
        "lcode": __version__,
        "harbor": HARBOR,
        "slice": args.harbor,
        "model": model,
        "context": context,
        "attempts": attempts,
        "docker_bridge": bool(args.docker_bridge),
        "machine": detect().describe(),
        "date": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "command": "lcode " + shlex.join(sys.argv[1:]),
        "tasks": picked,
        "summary": {
            "pass_at_1": summary.pass_at_1,
            f"pass^{attempts}": summary.pass_all,
            "runs": summary.runs,
            "spread": summary.spread,
            "not_run": summary.not_run,
        },
        "results": [asdict(task) for task in summary.tasks],
        "jobs": str(jobs),
    }
    text = json.dumps(report, indent=2) + "\n"
    (jobs / "lcode-bench.json").write_text(text)
    if args.json:
        Path(args.json).write_text(text)
    console.print(f"[dim]Results: {jobs / 'lcode-bench.json'} · Harbor's: {jobs}[/]")
    return 0 if trials else 1
