"""Background commands: dev servers, watchers and other processes that keep running.

The model starts one with `bash(command, background=true)` and gets an id back, reads what it
printed since last time with `bash_output(id)` and stops it with `bash_stop(id)`. `/jobs` lists
them. They're stopped when the session ends, except kept ones (`keep=true`): a service the user
asked to leave running. A kept job writes its output to a log file, not to lcode, so it outlives
lcode's exit.
"""

from __future__ import annotations

import codecs
import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

KEEP = 1_000_000  # characters of a job's output kept in memory
STARTUP_WAIT = 3.0  # seconds to wait for a new job's first output (and quick failures)


class JobError(Exception):
    pass


@dataclass
class Job:
    id: str
    command: str
    proc: subprocess.Popen
    kill: Callable[[], None] | None = None  # extra cleanup, e.g. the process in the sandbox
    started: float = field(default_factory=time.monotonic)
    output: str = ""
    read: int = 0  # how much of `output` the model has seen
    dropped: int = 0  # characters dropped from the front to stay under KEEP
    stopped: bool = False
    ended: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    log: Path | None = None  # a kept job's output: it keeps running after lcode exits

    @property
    def kept(self) -> bool:
        return self.log is not None

    def add(self, text: str) -> None:
        with self.lock:
            self.output += text
            if len(self.output) > KEEP:
                cut = len(self.output) - KEEP
                self.output = self.output[cut:]
                self.dropped += cut
                self.read = max(0, self.read - cut)

    def pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.add(line)
        self.proc.wait()
        self.ended = time.monotonic()

    def follow(self) -> None:
        """Read a kept job's output from its log as it grows, until the job ends (or lcode does)."""
        assert self.log is not None
        decoder = codecs.getincrementaldecoder("utf-8")("replace")  # a character written in two parts stays whole
        with open(self.log, "rb") as log:
            while True:
                ended = self.proc.poll() is not None
                data = log.read()  # read after the check, so what it printed last isn't missed
                if data:
                    self.add(decoder.decode(data))
                elif ended:
                    self.add(decoder.decode(b"", final=True))
                    break
                else:
                    time.sleep(0.1)
        self.ended = time.monotonic()

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def status(self) -> str:
        if self.running:
            return "running"
        if self.stopped:
            return "stopped"
        return f"exited with code {self.proc.returncode}"

    def runtime(self) -> float:
        return (self.ended or time.monotonic()) - self.started

    def new_output(self, limit: int) -> str:
        with self.lock:
            text, self.read = self.output[self.read :], len(self.output)
        if len(text) > limit:
            text = f"[… {len(text) - limit:,} earlier characters not shown]\n" + text[-limit:]
        return text


class Jobs:
    """The session's background commands (shared with its subagents)."""

    def __init__(self) -> None:
        self.items: dict[str, Job] = {}
        self.counter = 0

    def start(
        self, argv: list[str], cwd: Path, command: str, kill: Callable[[], None] | None = None, log: Path | None = None
    ) -> Job:
        """Start a background command. With `log`, it's a kept job: its output goes to that file instead of a pipe
        to lcode, so it can keep running after lcode exits (writing to a pipe nobody reads would stop it)."""
        sink = None
        if log is not None:
            log.parent.mkdir(parents=True, exist_ok=True)
            sink = open(log, "ab")  # noqa: SIM115 (the job keeps its own copy; ours is closed below)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                stdout=subprocess.PIPE if sink is None else sink,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                errors="replace",
                start_new_session=True,  # its own process group, so stopping it stops its children too
                bufsize=1,
            )
        finally:
            if sink is not None:
                sink.close()
        self.counter += 1
        job = Job(str(self.counter), command, proc, kill, log=log)
        self.items[job.id] = job
        threading.Thread(target=job.pump if log is None else job.follow, daemon=True).start()
        deadline = time.monotonic() + STARTUP_WAIT
        while time.monotonic() < deadline and job.running and not job.output:
            time.sleep(0.05)
        if job.running:
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))  # a little more of its first output
        else:
            time.sleep(0.1)  # let the pump read the rest
        return job

    def get(self, job_id: str) -> Job:
        job = self.items.get(str(job_id).strip().lstrip("#"))
        if job is None:
            known = ", ".join(self.items) or "none"
            raise JobError(f"there's no background job {job_id} (jobs: {known})")
        return job

    def stop(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.running:
            job.stopped = True
            terminate(job)
        return job

    def stop_all(self, kept: bool = False) -> int:
        """Stop the running jobs, except kept ones unless `kept`; how many were stopped."""
        running = [j for j in self.items.values() if j.running and (kept or not j.kept)]
        for job in running:
            job.stopped = True
            terminate(job)
        return len(running)

    def running(self) -> list[Job]:
        return [j for j in self.items.values() if j.running]

    def kept(self) -> list[Job]:
        """The running jobs to leave running when the session ends."""
        return [j for j in self.items.values() if j.running and j.kept]


def stop_leftovers(group: int) -> bool:
    """Stop what a finished command left running in its process group; whether there was anything."""
    try:
        os.killpg(group, 0)  # raises when nothing is left
    except (ProcessLookupError, PermissionError):
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(group, sig)
            time.sleep(0.3)
    return True


def terminate(job: Job, grace: float = 3.0) -> None:
    """SIGTERM the job's process group, then SIGKILL whatever is left."""
    if job.kill is not None:
        with contextlib.suppress(Exception):
            job.kill()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(job.proc.pid, sig)
        try:
            job.proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def describe(job: Job) -> str:
    minutes, seconds = divmod(int(job.runtime()), 60)
    took = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
    return f"job {job.id} ({job.status()}, {took}{', kept' if job.kept else ''}): {job.command}"
