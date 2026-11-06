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

Other agents run on the same slices and the same model for comparison (`--agent`): Claude Code,
OpenCode and Codex, each set up the way `ollama launch` sets it up.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import shlex
import statistics
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from lcode.ollama import OllamaError

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


# ----------------------------------------------------------------------------- other agents on the same model

# The agents compared with lcode (#198), by Harbor's names. Each is set up the way `ollama launch` sets it up (in
# Ollama's cmd/launch), with your machine's Ollama as a task's container reaches it, approvals off (Harbor's).
OTHER_AGENTS = ("claude-code", "opencode", "codex")
OLLAMA_FROM_TASKS = f"http://host.docker.internal:{OLLAMA_PORT}"  # with the host-gateway entry in the overlay
CODEX_CATALOG = "/opt/lcode-bench/codex-models.json"  # in the task's container
CLAUDE_CODE_ENV = {  # what `ollama launch claude` sets besides the address, the key and each tier's model (Harbor's)
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
    "CLAUDE_CODE_TOTAL_TOKENS_REMINDER": "off",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_FEEDBACK_COMMAND": "1",
    "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
    "CLAUDE_CODE_AUTO_MODE_SERVER": "0",
}
CODEX_LEVELS = {  # Codex's reasoning efforts for Ollama's thinking values, with Ollama's descriptions
    "none": "Turn thinking off",
    "minimal": "Thinking effort",
    "low": "Fast responses with lighter thinking",
    "medium": "Balances speed and thinking depth for everyday tasks",
    "high": "Greater thinking depth for complex tasks",
    "xhigh": "Thinking effort",
    "max": "Maximum thinking depth for the hardest tasks",
    "ultra": "Thinking effort",
}


def variant_name(model: str, context: int) -> str:
    """The Ollama model the other agents get: `model` with its context window set (lcode-qwen3.6-35b-64k), since
    the APIs they use (OpenAI's, Anthropic's) can't set it per request and Ollama's default is small."""
    from lcode.config import format_tokens

    return f"{model.removesuffix(':latest')}-{format_tokens(context).lower()}"


def agent_overlay(bridge: bool, catalog: Path | None = None) -> str:
    """The Docker Compose override for another agent's tasks: host.docker.internal is your machine (Docker Desktop
    has it; on Linux it's added), and Codex's model catalog is in the container."""
    lines = ["services:", "  main:"]
    if bridge:
        lines.append("    network_mode: bridge")
    lines += ["    extra_hosts:", '      - "host.docker.internal:host-gateway"']
    if catalog is not None:
        lines += ["    volumes:", f"      - {json.dumps(f'{catalog}:{CODEX_CATALOG}:ro')}"]
    return "\n".join(lines) + "\n"


def opencode_config(model: str, context: int, thinking: bool) -> dict:
    """`ollama launch opencode`'s provider: Ollama's OpenAI-compatible API, and the model with the limits of the
    context it runs at (a quarter of it for output, at most 8K)."""
    entry: dict = {"name": model}
    if thinking:  # a model that can think without levels: on, or "none"
        entry["reasoning"] = True
        entry["variants"] = {
            "none": {"reasoningEffort": "none"},
            "low": {"disabled": True},
            "medium": {"disabled": True},
            "high": {"disabled": True},
        }
    entry["limit"] = {"context": context, "input": context, "output": min(8192, context // 4)}
    provider = {"npm": "@ai-sdk/openai-compatible", "name": "Ollama", "options": {"baseURL": f"{OLLAMA_FROM_TASKS}/v1"}}
    return {"provider": {"ollama": {**provider, "models": {model: entry}}}}


def codex_level(value: object) -> str | None:
    if isinstance(value, bool):
        return "high" if value else "none"
    return value if isinstance(value, str) and value in CODEX_LEVELS and value != "none" else None


def codex_catalog(model: str, context: int, thinking: dict | None) -> dict:
    """`ollama launch codex`'s model catalog entry, so Codex knows the model (otherwise it warns that it falls back
    to defaults that "can degrade performance"). `thinking`: Ollama's, as `/api/show` gives it."""
    values = list((thinking or {}).get("values") or [])
    levels: list[str] = []
    for value in values:
        if value is True and "high" in values:
            continue
        level = codex_level(value)
        if level is not None and level not in levels:
            levels.append(level)
    default = codex_level((thinking or {}).get("default"))
    if default not in levels or (len(values) == 1 and False in values):
        default = None
    entry = {
        "slug": model,
        "display_name": model,
        "context_window": context,
        "shell_type": "default",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 0,
        "truncation_policy": {"mode": "bytes", "limit": 10000},
        "input_modalities": ["text"],
        "base_instructions": "",
        "support_verbosity": True,
        "default_verbosity": "low",
        "supports_parallel_tool_calls": False,
        "supports_reasoning_summaries": False,
        "supported_reasoning_levels": [{"effort": level, "description": CODEX_LEVELS[level]} for level in levels],
        "default_reasoning_level": default,
        "experimental_supported_tools": [],
    }
    return {"models": [entry]}


def codex_config() -> dict:
    """`ollama launch codex`'s profile: Ollama's Responses API, and the model catalog."""
    provider = {"name": "Ollama", "base_url": f"{OLLAMA_FROM_TASKS}/v1/", "wire_api": "responses"}
    return {
        "model_provider": "ollama-launch",
        "model_providers": {"ollama-launch": provider},
        "model_catalog_json": CODEX_CATALOG,
    }


def agent_setup(agent: str, model: str, context: int, show: dict, folder: Path) -> tuple[list[str], Path | None]:
    """Harbor's arguments for another agent on `model` (the Ollama model at `context`), and Codex's model catalog,
    written in `folder`. `show`: Ollama's `/api/show` for the model."""
    thinking = "thinking" in (show.get("capabilities") or [])
    if agent == "claude-code":
        return [arg for name, value in CLAUDE_CODE_ENV.items() for arg in ("--ae", f"{name}={value}")], None
    if agent == "opencode":
        return ["--ak", "opencode_config=" + json.dumps(opencode_config(model, context, thinking))], None
    if agent == "codex":
        catalog = folder / "codex-models.json"
        catalog.write_text(json.dumps(codex_catalog(model, context, show.get("thinking") if thinking else None)))
        return ["--ak", "config=" + json.dumps(codex_config())], catalog
    raise ValueError(f"unknown agent {agent!r}")


def harbor_env() -> dict[str, str]:
    """For Harbor itself: other agents reach Ollama through its `ollama/` provider, which needs a key (any)."""
    return {"OLLAMA_BASE_URL": OLLAMA_FROM_TASKS, "OLLAMA_API_KEY": "ollama"}


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
    """How the newest request traced in `home` (lcode's LCODE_HOME in the task) went: its end event. When lcode
    was stopped before the request ended (at the task's time limit), the totals of what its trace recorded."""
    traces = sorted((home / "traces").glob("*.jsonl"), key=lambda path: path.stat().st_mtime)
    for path in reversed(traces):
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        events = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
        end = next((event for event in reversed(events) if event.get("type") == "end"), None)
        if end is not None:
            return end
        if any(event.get("type") == "model" for event in events):
            return unfinished_usage(events)
    return None


def unfinished_usage(events: list[dict]) -> dict:
    """An end event's totals for a request that never ended, from the events its trace has."""
    usage: dict = {"type": "end", "status": "unfinished", "steps": 0, "model_calls": 0, "tool_calls": 0}
    usage.update(dict.fromkeys(("tokens_in", "tokens_out", "estimated_in", "estimated_out"), 0))
    usage.update(seconds=0.0, model_seconds=0.0, tool_seconds=0.0)
    for event in events:
        usage["seconds"] = max(usage["seconds"], number(event.get("at")))
        if event.get("type") == "model":
            usage["model_calls"] += 1
            for key in ("tokens_in", "tokens_out", "estimated_in", "estimated_out"):
                usage[key] += int(number(event.get(key)))
            usage["model_seconds"] += number(event.get("seconds"))
            if event.get("lane", "main") == "main" and event.get("kind") == "step":
                usage["steps"] = max(usage["steps"], int(number(event.get("step"))))
        elif event.get("type") == "tool":
            usage["tool_calls"] += 1
            usage["tool_seconds"] += number(event.get("seconds"))
    return usage


def number(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def usage_tokens(usage: dict) -> tuple[int, int]:
    """Tokens in and out of a traced request, with the estimates for replies lcode stopped before the server
    counted them."""
    return (
        int(number(usage.get("tokens_in")) + number(usage.get("estimated_in"))),
        int(number(usage.get("tokens_out")) + number(usage.get("estimated_out"))),
    )


def generation_speed(home: Path) -> float | None:
    """How fast the model generated in the requests traced in `home`, in tokens per second: None without a trace.
    It tells how fast the machine was, since a slower one runs into the tasks' time limits more often."""
    tokens = seconds = 0.0
    for path in (home / "traces").glob("*.jsonl"):
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "model":
                tokens += number(event.get("tokens_out"))
                seconds += number(event.get("gen_seconds"))
    return tokens / seconds if tokens and seconds else None


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
    agent: str = "lcode",
    agent_args: list[str] | None = None,
) -> list[str]:
    """`harbor run` for some tasks of a dataset, with uvx, and lcode next to Harbor so it can import the agent.
    Another `agent` (Harbor's name) gets `model` as Harbor's `ollama/` model and `agent_args` (agent_setup)."""
    argv = ["uvx", "--python", HARBOR_PYTHON, "--from", HARBOR, *(lcode_with or []), "harbor", "run"]
    harbor_agent, harbor_model = (AGENT, model) if agent == "lcode" else (agent, f"ollama/{model}")
    argv += ["-d", DATASETS[dataset], "-a", harbor_agent, "-m", harbor_model, "-k", str(attempts), "-n", "1"]
    argv += ["-o", str(jobs), "--job-name", job_name, "-y"]
    for task in tasks:
        argv += ["-i", task, "-i", f"*/{task}"]  # the name alone, or with its organization in front
    if context and agent == "lcode":  # the others get a model with the context set (variant_name)
        argv += ["--ak", f"context={context}"]
    if wheel:
        argv += ["--ak", f"wheel={wheel}"]
    argv += agent_args or []
    if overlay:
        argv += ["--extra-docker-compose", str(overlay)]
    return argv


# Errors of attempts whose tests ran but didn't finish or give a result. Harbor grades these attempts, as failed
# (VERIFIER_FAILED in its error_policy.py), and lcode counts them the same way: a change that makes the tests hang
# mustn't drop out of a comparison.
TESTS_UNFINISHED = (
    "VerifierTimeoutError",
    "RewardFileNotFoundError",
    "RewardFileEmptyError",
    "VerifierOutputParseError",
)


@dataclass
class Trial:
    """One attempt at a task, from Harbor's result.json."""

    dataset: str
    task: str
    reward: float | None  # None: the task's tests didn't run (its container didn't start, say)
    started: str = ""
    seconds: float | None = None  # lcode's time on the task
    tokens_in: int | None = None  # None: not known (no trace)
    tokens_out: int | None = None
    error: str = ""
    version: str = ""  # the agent's, in the task's container
    unfinished_tests: bool = False  # its tests ran but didn't finish or give a result: counted as failed
    tokens_per_second: float | None = None  # how fast the model generated, from lcode's trace


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
        error = kind = str(problem.get("exception_type") or "")
        if error and problem.get("exception_message"):
            error += ": " + str(problem["exception_message"]).strip().splitlines()[-1][:300]
        reward = float(reward) if isinstance(reward, (int, float)) else None
        unfinished = reward is None and kind in TESTS_UNFINISHED
        name = str(result.get("task_name") or path.parent.name.rsplit("__", 1)[0])
        tokens_in, tokens_out = agent.get("n_input_tokens"), agent.get("n_output_tokens")
        if tokens_in is None and tokens_out is None:  # lcode 0.54.0 gave none for an attempt stopped at its limit
            usage = trace_usage(path.parent / "agent" / "lcode")
            if usage is not None:
                tokens_in, tokens_out = usage_tokens(usage)
        trials.append(
            Trial(
                dataset=dataset,
                task=name.rsplit("/", 1)[-1],
                reward=0.0 if unfinished else reward,
                started=str(result.get("started_at") or ""),
                seconds=seconds_between(result.get("agent_execution")),
                tokens_in=None if tokens_in is None else int(number(tokens_in)),
                tokens_out=None if tokens_out is None else int(number(tokens_out)),
                error=error,
                version=str((result.get("agent_info") or {}).get("version") or ""),
                unfinished_tests=unfinished,
                tokens_per_second=generation_speed(path.parent / "agent" / "lcode"),
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
    tokens_in: int | None = None  # per attempt, on average, of those whose tokens are known
    tokens_out: int | None = None
    unfinished_tests: int = 0  # attempts whose tests didn't finish or give a result, in `passed` as failed


@dataclass
class Summary:
    tasks: list[TaskResult]
    attempts: int
    pass_at_1: float | None  # the share of attempts that passed, averaged over the tasks
    pass_all: float | None  # pass^k: the share of tasks whose every attempt passed (of those with all k)
    runs: list[float]  # the pass rate of each task's first attempt, of its second...
    not_run: int
    unfinished_tests: int = 0
    tokens_per_second: float | None = None  # the median attempt's, of those with a trace

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
        counted_in = [t.tokens_in for t in ran if t.tokens_in is not None]
        counted_out = [t.tokens_out for t in ran if t.tokens_out is not None]
        tasks.append(
            TaskResult(
                dataset,
                task,
                [(t.reward or 0) >= 1 for t in ran],
                len(group) - len(ran),
                statistics.fmean(timed) if timed else None,
                round(statistics.fmean(counted_in)) if counted_in else None,
                round(statistics.fmean(counted_out)) if counted_out else None,
                sum(t.unfinished_tests for t in ran),
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
    speeds = [t.tokens_per_second for t in trials if t.tokens_per_second]
    return Summary(
        tasks,
        attempts,
        pass_at_1,
        pass_all,
        runs,
        sum(t.not_run for t in tasks),
        sum(t.unfinished_tests for t in tasks),
        statistics.median(speeds) if speeds else None,
    )


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
        tokens_in = "–" if t.tokens_in is None else f"{t.tokens_in:,}"
        tokens_out = "–" if t.tokens_out is None else f"{t.tokens_out:,}"
        table.add_row(t.task, t.dataset, passed, took, tokens_in, tokens_out)
    console.print(table)
    k = summary.attempts
    line = f"pass@1 {percent(summary.pass_at_1)} · pass^{k} {percent(summary.pass_all)}"
    if summary.spread is not None:
        line += f" · runs {', '.join(percent(r) for r in summary.runs)} (spread {summary.spread * 100:.0f} points)"
    console.print(f"[bold]{line}[/]")
    if summary.tokens_per_second:
        console.print(f"The model generated {summary.tokens_per_second:.0f} tokens/s (the median attempt).")
    if summary.unfinished_tests:
        console.print(
            f"[yellow]{summary.unfinished_tests} attempt(s) count as failed: their tests didn't finish (a time limit, "
            "say) or give a result. Harbor's own mean counts them the same way.[/]"
        )
    if summary.not_run:
        console.print(f"[yellow]{summary.not_run} attempt(s) didn't run (their container didn't start, say).[/]")


# ----------------------------------------------------------------------------- comparing two runs (the release gate)

SAME = ("slice", "agent", "model", "context", "attempts")  # results compare fairly only when these match
SPEED_GAP = 0.1  # model speeds further apart than this make time limits bite differently


@dataclass
class Saved:
    """A saved `lcode bench --harbor` result: lcode-bench.json, or the folder it's in."""

    path: Path
    data: dict
    passed: dict[str, list[bool]]  # "benchmark/task" -> each attempt that ran, passed or not
    tokens_per_second: float | None = None  # the median attempt's model speed

    @classmethod
    def load(cls, path: Path) -> Saved:
        file = path / "lcode-bench.json" if path.is_dir() else path
        try:
            data = json.loads(file.read_text())
            passed = {f"{r['dataset']}/{r['task']}": [bool(p) for p in r.get("passed") or []] for r in data["results"]}
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise ValueError(f"{file} isn't a result of lcode bench --harbor: {e}") from None
        summary = data.get("summary")
        speed = summary.get("tokens_per_second") if isinstance(summary, dict) else None
        trials = harbor_trials(file.parent, data)
        if trials:  # read again with this version's rules, so that both sides of a comparison count alike
            again = summarize(trials, int(number(data.get("attempts"))) or ATTEMPTS)
            passed = {f"{t.dataset}/{t.task}": t.passed for t in again.tasks}
            speed = again.tokens_per_second
        return cls(file, data, passed, number(speed) or None)

    def describe(self) -> str:
        d = self.data
        context = f"{d['context'] // 1024}K" if isinstance(d.get("context"), int) else "?"
        agent = "" if d.get("agent", "lcode") == "lcode" else f"{d.get('agent')} {d.get('agent_version') or ''} on "
        return f"{agent}lcode {d.get('lcode', '?')} · {d.get('model', '?')} at {context} · {d.get('date', '?')[:10]}"


def harbor_trials(folder: Path, data: dict) -> list[Trial]:
    """A saved run's attempts, read again from Harbor's results while they're still on this machine: in the folder
    of its lcode-bench.json, or where the run left them."""
    places = [folder] + ([Path(str(data["jobs"]))] if data.get("jobs") else [])
    for jobs in places:
        trials = [t for name in DATASETS if (jobs / name).is_dir() for t in read_trials(jobs / name, name)]
        if trials:
            return trials
    return []


def pass_at_1(passed: list[list[bool]]) -> float:
    return statistics.fmean(statistics.fmean(p) for p in passed)


def standard_error(passed: list[list[bool]], other: list[list[bool]]) -> float:
    """How much pass@1 would move between runs, from how much each task's attempts disagree: a task that always
    passes or always fails adds nothing, one that passes half the time the most. One attempt can't disagree with
    itself, so that task's pass rate comes from both runs (`other` is the other run's attempts, task by task)."""
    variance = 0.0
    for attempts, others in zip(passed, other, strict=True):
        rate = statistics.fmean(attempts if len(attempts) > 1 else attempts + others)
        variance += rate * (1 - rate) / max(len(attempts) - 1, 1)
    return math.sqrt(variance) / len(passed)


def compare(old: Saved, new: Saved) -> tuple[list[str], bool]:
    """Lines to print, and whether the new result is no worse than the old one beyond the noise."""
    lines = [f"Before: {old.describe()}", f"After:  {new.describe()}"]
    for key in SAME:
        if old.data.get(key, "lcode" if key == "agent" else None) != new.data.get(
            key, "lcode" if key == "agent" else None
        ):
            lines.append(f"[yellow]Not the same {key}: {old.data.get(key)} and {new.data.get(key)}.[/]")
    if old.tokens_per_second and new.tokens_per_second:
        lines.append(
            f"Model speed: {old.tokens_per_second:.0f} tokens/s before, {new.tokens_per_second:.0f} after "
            "(the median attempt's)."
        )
        ratio = new.tokens_per_second / old.tokens_per_second - 1
        if abs(ratio) > SPEED_GAP:
            lines.append(
                f"[yellow]The model ran {abs(ratio) * 100:.0f}% {'slower' if ratio < 0 else 'faster'} after. "
                "Attempts run into their time limits more often on a slower machine, so part of a difference can "
                "come from the machine (free memory, other programs) rather than from lcode.[/]"
            )
    both = sorted(task for task in old.passed if old.passed[task] and new.passed.get(task))
    for task in sorted(set(old.passed) | set(new.passed)):
        before, after = old.passed.get(task) or [], new.passed.get(task) or []
        if task not in both:
            lines.append(f"  {task}: only {'after' if after else 'before'}, left out")
        elif sum(before) * len(after) != sum(after) * len(before):
            lines.append(f"  {task}: {sum(before)}/{len(before)} → {sum(after)}/{len(after)}")
    if not both:
        return [*lines, "[red]No task ran in both, so there's nothing to compare.[/]"], False
    runs_before, runs_after = [old.passed[t] for t in both], [new.passed[t] for t in both]
    ran_before, ran_after = sum(map(len, runs_before)), sum(map(len, runs_after))
    if ran_before != ran_after:
        lines.append(
            f"[yellow]Not the same number of attempts ran: {ran_before} before and {ran_after} after, over the tasks "
            "in both. With fewer attempts, a result says less.[/]"
        )
    a, b = pass_at_1(runs_before), pass_at_1(runs_after)
    noise = 2 * math.hypot(standard_error(runs_before, runs_after), standard_error(runs_after, runs_before))
    change = f"{(b - a) * 100:+.0f} points; the noise is ±{noise * 100:.0f}"
    if a - b > noise + 1e-9:
        verdict = f"[red]a drop beyond the noise ({change})[/]"
    elif b - a > noise + 1e-9:
        verdict = f"[green]better, beyond the noise ({change})[/]"
    else:
        verdict = f"within the noise ({change})"
    lines.append(f"pass@1 {a * 100:.0f}% → {b * 100:.0f}% over the {len(both)} tasks in both: {verdict}")
    return lines, a - b <= noise + 1e-9


def compare_command(paths: list[str], console) -> int:
    """`lcode bench --compare BEFORE AFTER`: 0 when AFTER is no worse beyond the noise, 1 when it is, 2 on an error."""
    try:
        old, new = (Saved.load(Path(p).expanduser()) for p in paths)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        return 2
    lines, ok = compare(old, new)
    for line in lines:
        console.print(line)
    return 0 if ok else 1


def took(seconds: float) -> str:
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def context_variant(settings: dict, model: str, context: int, console) -> tuple[str, dict]:
    """The Ollama model for another agent (variant_name), made when it's missing, and Ollama's `/api/show` for it.
    Making it copies nothing: it's the same files with one more parameter."""
    from rich.markup import escape

    from lcode.cli import NotInstalled, resolve_model
    from lcode.config import format_tokens
    from lcode.ollama import Ollama

    if settings.get("backend", "ollama") != "ollama":
        raise OllamaError("--agent runs the other agents on Ollama's models: lcode's backend here isn't Ollama.")
    ollama = Ollama(settings["ollama_host"])
    try:
        tag, _ = resolve_model(ollama, model)
    except NotInstalled as e:
        raise OllamaError(str(e)) from e
    name = variant_name(tag, context)
    if name not in ollama.installed_names():
        ollama.create_variant(name, tag, {"num_ctx": context})
        console.print(
            f"Made [bold]{escape(name)}[/]: {escape(tag)} with a {format_tokens(context)} context window, for the "
            f"other agents, which can't set it per request. `ollama rm {escape(name)}` removes it."
        )
    return name, ollama.show(name)


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
    agent = getattr(args, "agent", None) or "lcode"
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
    ollama_model, show, agent_args, env = "", {}, None, None
    if agent != "lcode":  # on Ollama's model at the context, set up like `ollama launch` does
        try:
            ollama_model, show = context_variant(settings, model, context, console)
        except OllamaError as e:
            console.print(f"[red]{escape(str(e))}[/]")
            return 1
    named = args.harbor if agent == "lcode" else f"{args.harbor}-{agent}"
    jobs = config.STATE_DIR / "bench" / f"{named}-{dt.datetime.now():%Y%m%d-%H%M%S}"
    jobs.mkdir(parents=True, exist_ok=True)
    install = update.lcode_install()
    wheel = None
    overlay: Path | None = None
    if agent != "lcode":
        agent_args, catalog = agent_setup(agent, ollama_model, context, show, jobs)
        overlay = jobs / "overlay.yaml"
        overlay.write_text(agent_overlay(bool(args.docker_bridge), catalog))
        lcode_with = None
        env = {**os.environ, **harbor_env()}
    elif install.kind == "checkout" and install.path:  # this checkout, in Harbor and in the containers
        lcode_with = ["--with-editable", str(install.path)]
        build = ["uv", "build", "--wheel", "-q", "-o", str(jobs / "wheel"), str(install.path)]
        if subprocess.run(build, capture_output=True, text=True).returncode != 0:
            console.print(f"[red]Couldn't build a wheel of {install.path}:[/] {' '.join(build)}")
            return 1
        wheel = next((jobs / "wheel").glob("*.whl"), None)
    else:
        lcode_with = ["--with", f"lcode-cli=={__version__}"]
    if agent == "lcode" and args.docker_bridge:
        overlay = jobs / "bridge.yaml"
        overlay.write_text(BRIDGE_OVERLAY)
    trials: list[Trial] = []
    started, began = dt.datetime.now().astimezone(), time.monotonic()
    for dataset, tasks in picked.items():
        if not tasks:
            continue
        argv = harbor_argv(
            dataset, tasks, ollama_model or model, jobs, dataset, attempts=attempts, context=context,
            lcode_with=lcode_with, wheel=wheel, overlay=overlay, agent=agent, agent_args=agent_args,
        )  # fmt: skip
        console.print(f"[bold]{DATASETS[dataset]}[/]: {len(tasks)} task(s), {attempts} attempt(s) each")
        console.print(f"[dim]{escape(shlex.join(argv))}[/]")
        if env is None:
            subprocess.call(argv)
        else:
            subprocess.call(argv, env=env)
        trials += read_trials(jobs / dataset, dataset)
    summary = summarize(trials, attempts)
    seconds = time.monotonic() - began
    version = next((trial.version for trial in trials if trial.version), "")
    if agent != "lcode":
        console.print(f"[bold]{agent}[/] {escape(version)} on {escape(ollama_model)}")
    print_summary(console, summary)
    console.print(f"Took {took(seconds)}.")
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
        "agent": agent,
        "agent_version": version,  # in the tasks' containers
        "model": model,
        "ollama_model": ollama_model or None,  # the other agents': the model with the context set
        "context": context,
        "attempts": attempts,
        "docker_bridge": bool(args.docker_bridge),
        "machine": detect().describe(),
        "date": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "started": started.isoformat(timespec="seconds"),
        "seconds": round(seconds),  # the whole run, every task's attempts
        "command": "lcode " + shlex.join(sys.argv[1:]),
        "tasks": picked,
        "summary": {
            "pass_at_1": summary.pass_at_1,
            f"pass^{attempts}": summary.pass_all,
            "runs": summary.runs,
            "spread": summary.spread,
            "not_run": summary.not_run,
            "unfinished_tests": summary.unfinished_tests,
            "tokens_per_second": None if summary.tokens_per_second is None else round(summary.tokens_per_second, 1),
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
