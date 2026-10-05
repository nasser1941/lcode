"""Background commands: dev servers, watchers and other processes that keep running.

The model starts one with `bash(command, background=true)` and gets an id back, reads what it
printed since last time with `bash_output(id)` and stops it with `bash_stop(id)`. `/jobs` lists
them. They're all stopped when the session ends.
"""

from __future__ import annotations

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

    def pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with self.lock:
                self.output += line
                if len(self.output) > KEEP:
                    cut = len(self.output) - KEEP
                    self.output = self.output[cut:]
                    self.dropped += cut
                    self.read = max(0, self.read - cut)
        self.proc.wait()
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

    def start(self, argv: list[str], cwd: Path, command: str, kill: Callable[[], None] | None = None) -> Job:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
            start_new_session=True,  # its own process group, so stopping it stops its children too
            bufsize=1,
        )
        self.counter += 1
        job = Job(str(self.counter), command, proc, kill)
        self.items[job.id] = job
        threading.Thread(target=job.pump, daemon=True).start()
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

    def stop_all(self) -> int:
        running = [j for j in self.items.values() if j.running]
        for job in running:
            job.stopped = True
            terminate(job)
        return len(running)

    def running(self) -> list[Job]:
        return [j for j in self.items.values() if j.running]


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
    return f"job {job.id} ({job.status()}, {took}): {job.command}"
