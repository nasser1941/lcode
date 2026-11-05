"""The shell tools: bash in the foreground or the background, and reading or stopping what runs in the background."""

from __future__ import annotations

import hashlib
import itertools
import os
import queue
import re
import secrets
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from rich.console import Group
from rich.syntax import Syntax
from rich.text import Text

from lcode import config, secretscan
from lcode.context import OutputSpool
from lcode.jobs import JobError, describe, stop_leftovers
from lcode.permissions import bash_key, is_read_only
from lcode.planning import BLOCKED
from lcode.sandbox import SandboxError
from lcode.tool_helpers import LEFTOVER_GRACE, MAX_TOOL_OUTPUT, NO_NETWORK, PENDING_LINES, ToolError
from lcode.tools_base import ToolboxBase

# A last `&` on a background command: the shell would end at once, and leave the command where lcode can't see it.
TRAILING_AMPERSAND = re.compile(r"\s*(?<![&|>\\])&\s*$")


class ShellTools(ToolboxBase):
    """Toolbox's shell tools."""

    # -- shell
    def t_bash(self, command: str, timeout: int = 180, background: bool = False, keep: bool = False) -> str:
        sandbox = self.agent.sandbox
        keep = keep is True or str(keep).lower() == "true"  # a kept job is a background job
        background = keep or background is True or str(background).lower() == "true"
        only_reads = is_read_only(command) and not keep  # what's left running after lcode exits does more than read
        if self.agent.planning() and not only_reads:
            raise ToolError(f"{BLOCKED} Until then, only read-only commands run (ls, cat, grep, git log, …).")
        if self.agent.no_changes and not only_reads:
            raise ToolError(f"{self.agent.no_changes} Only read-only commands run (ls, cat, grep, git diff, …).")
        if self.agent.read_only and not only_reads:
            raise ToolError(
                "you can only run read-only commands (such as ls, cat, grep, find, git log, git diff), one at a "
                "time without pipes into other programs, redirection or chaining"
            )
        verdict = self.agent.perms.rule("bash", command)
        if verdict is not None and not verdict[0]:  # deny rules apply to read-only commands too
            return verdict[1]
        approved = False
        if self.agent.settings.secret_check and not only_reads:
            action, found = secretscan.check(self.agent.cwd, command)
            if found:
                text = secretscan.report(action, found)
                if self.agent.perms.mode != "ask":  # nobody looks at each command: it doesn't run
                    self.console.print(Text(f"  ⎿ {text}", style="red"))
                    return f"Error: lcode's secret check stopped this command. {text}\n\n{secretscan.ADVICE}"
                body = Group(Text(text + "\n", style="red"), Syntax(command, "bash", theme="monokai", word_wrap=True))
                key = "secret:" + hashlib.sha256(text.encode()).hexdigest()[:16]
                ok, feedback = self.agent.perms.request(
                    key, "secret", f"This {action} looks like it holds a secret", body, command
                )
                if not ok:
                    return f"{feedback}\n\n{text}\n{secretscan.ADVICE}"
                approved = True
        if (
            not approved
            and not only_reads
            and not (sandbox and self.agent.perms.mode == "auto-edit")
            and verdict is None
        ):
            body = Syntax(command, "bash", theme="monokai", word_wrap=True)
            where = "in the sandbox" if sandbox else f"in {self.agent.cwd}"
            key, title = bash_key(command), f"Run command ({where})"
            if keep and not sandbox:  # the user should see that it stays, and "always" for the program doesn't cover it
                key, title = f"{key} (kept)", f"Run command and leave it running after lcode exits ({where})"
            ok, feedback = self.agent.perms.request(key, "bash", title, body, command)
            if not ok:
                return feedback
        if not only_reads:
            self.agent.checkpoint()
        where = "  (in the background, kept after lcode exits)" if keep else "  (in the background)"
        self.console.print(Text(f"  $ {command}" + (where if background else ""), style="bold cyan"))
        if background:
            return self._background(command, keep)
        marker = f"__lcode_cwd_{secrets.token_hex(8)}__"
        script = f"{command}\n__lcode_ec=$?\nprintf '\\n{marker}%s\\n' \"$(pwd -P)\"\nexit $__lcode_ec\n"
        token = ""
        if sandbox:
            try:
                with self.console.status("Starting the sandbox…"):
                    sandbox.ensure(self.agent.cwd)
            except SandboxError as e:
                return f"Error: the sandbox can't start, so the command didn't run: {e}"
            argv, token = sandbox.exec_argv(self.agent.cwd, script)
        else:
            argv = ["bash", "-c", script]
        proc = subprocess.Popen(
            argv,
            cwd=self.agent.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
            start_new_session=True,
            bufsize=1,
        )
        # Bounded, so a command that prints faster than lcode reads waits instead of filling memory.
        lines: queue.Queue[str | None] = queue.Queue(maxsize=PENDING_LINES)
        reading = threading.Event()  # cleared when lcode stops reading
        reading.set()

        def pump() -> None:
            assert proc.stdout is not None
            for line in itertools.chain(proc.stdout, [None]):  # streamed: never the whole output
                while reading.is_set():
                    try:
                        lines.put(line, timeout=0.5)
                        break
                    except queue.Full:
                        continue
                else:
                    return

        def stop() -> None:
            if sandbox and token:
                sandbox.kill(token)  # the command runs in the container, not under our process
            os.killpg(proc.pid, signal.SIGKILL)

        threading.Thread(target=pump, daemon=True).start()
        out = OutputSpool(MAX_TOOL_OUTPUT, "output")
        last = ""  # the latest line, for the end-of-output checks below
        recent: deque[str] = deque(maxlen=40)
        shown, status, recorded = 0, "", ""
        deadline = time.time() + int(timeout or 180)
        finished = 0.0  # when the command itself ended (its marker arrived)
        held_open = False  # something it started still holds the output open
        spinner = self.agent.activity("Running the command")  # the elapsed time, and a note being typed
        spinner.start()
        try:
            while True:
                if self.agent.cancel is not None and self.agent.cancel.is_set():
                    raise KeyboardInterrupt
                if finished and time.time() - finished > LEFTOVER_GRACE:
                    held_open = True  # also when what it left running keeps printing
                    break
                if time.time() > deadline and not finished:  # also while it keeps printing
                    stop()
                    status = f"\n[Command timed out after {timeout}s and was killed]"
                    break
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    break
                if line.startswith(marker):
                    recorded = line[len(marker) :].strip()
                    finished = time.time()
                    continue
                if last:
                    out.add(last)
                    recent.append(last)
                last = line
                if shown <= 40:
                    msg = "    ... (more output hidden)" if shown == 40 else "    " + line.rstrip("\n")[:300]
                    self.console.print(Text(msg, style="dim"))
                    shown += 1
        except KeyboardInterrupt:
            reading.clear()
            stop()
            raise
        finally:
            spinner.stop()
        reading.clear()
        code = proc.wait()
        if held_open and sandbox and token:
            sandbox.kill(token)  # what it left running in the container
        if (not sandbox and stop_leftovers(proc.pid)) or held_open:
            status += (
                "\n[lcode stopped what this command left running (started with &). To keep a process running, "
                "start it with background=true: then bash_output reads its output and bash_stop stops it. Add "
                "keep=true for a service the user wants left running after you finish]"
            )
        if last and last != "\n":  # the blank line printed before the marker isn't output
            out.add(last)
            recent.append(last)
        if recorded and Path(recorded).is_dir() and Path(recorded).resolve() != self.agent.cwd:
            from lcode.checkpoints import work_tree_for

            moved = Path(recorded).resolve()
            if sandbox and not sandbox.contains(moved):
                status += "\n[That directory is outside the project; the working directory stays the same]"
            elif not sandbox and not moved.is_relative_to(work_tree_for(self.agent.workspace)):
                status += (
                    f"\n[{moved} is outside the project, so the working directory stays {self.agent.cwd}: the next "
                    "command starts there. To run something elsewhere, cd in the same command]"
                )
            else:
                self.agent.cwd = moved
                status += f"\n[Working directory is now {self.agent.cwd}]"
        if sandbox and code != 0 and not sandbox.network and NO_NETWORK.search("".join(recent)):
            status += (
                "\n[The sandbox has no network access. If this needs the network, ask the user to allow it "
                "with /sandbox network on]"
            )
        self.console.print(Text(f"  exit code {code}", style="green" if code == 0 else "red"))
        return out.text() + status + f"\n[exit code: {code}]"

    def _background(self, command: str, keep: bool = False) -> str:
        sandbox, kill, log, note = self.agent.sandbox, None, None, ""
        command = TRAILING_AMPERSAND.sub("", command)
        if sandbox:
            try:
                with self.console.status("Starting the sandbox…"):
                    sandbox.ensure(self.agent.cwd)
            except SandboxError as e:
                return f"Error: the sandbox can't start, so the command didn't run: {e}"
            argv, token = sandbox.exec_argv(self.agent.cwd, command)
            kill = lambda: sandbox.kill(token)  # noqa: E731
            if keep:
                note = (
                    "\n[It can't be kept: it runs in the sandbox, which stops when lcode exits, and the job with it. "
                    "If the user needs it to stay up, tell them]"
                )
        else:
            argv = ["bash", "-c", command]
            if keep:  # its own log, so it doesn't depend on lcode reading its output
                log = config.STATE_DIR / "jobs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.log"
        job = self.agent.jobs.start(argv, self.agent.cwd, command, kill, log=log)
        first = job.new_output(MAX_TOOL_OUTPUT // 3).rstrip()
        for line in first.splitlines()[:10]:
            self.console.print(Text("    " + line[:300], style="dim"))
        if not job.running:
            if not sandbox and stop_leftovers(job.proc.pid):
                note = (
                    "\n[lcode stopped what this command left running (started with & or nohup). Give the plain "
                    "command, without & or nohup: then lcode keeps it running]"
                )
            return (
                f"The command ended right away ({job.status()}), so it isn't running in the background:\n"
                f"{first or '(no output)'}{note}"
            )
        self.console.print(Text(f"  ⎿ background job {job.id}, /jobs to see it", style="dim"))
        lasts = (
            f"it keeps running while you work, and after lcode exits (its output also goes to {log})"
            if log
            else "it keeps running while you work"
        )
        return (
            f"Started background job {job.id}: {lasts}. Read its new output with "
            f'bash_output(id="{job.id}") and stop it with bash_stop(id="{job.id}") when it\'s no longer needed.\n'
            f"Output so far:\n{first or '(none yet)'}{note}"
        )

    def t_bash_output(self, id: str) -> str:
        try:
            job = self.agent.jobs.get(id)
        except JobError as e:
            raise ToolError(str(e)) from e
        text = job.new_output(MAX_TOOL_OUTPUT).rstrip()
        return f"[{describe(job)}]\n{text or '(no new output)'}"

    def t_bash_stop(self, id: str) -> str:
        try:
            job = self.agent.jobs.stop(id)
        except JobError as e:
            raise ToolError(str(e)) from e
        self.console.print(Text(f"  ⎿ {describe(job)}", style="dim"))
        last = job.new_output(4000).rstrip()
        return f"[{describe(job)}]" + (f"\nIts last output:\n{last}" if last else "")
