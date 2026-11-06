"""Checking the work before a request counts as done: the project's checks, and the optional self-review."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, cast

from lcode import (
    checks,
    review,
    traces,
)
from lcode.agent_base import AgentBase
from lcode.agent_prompt import project_root_or_cwd
from lcode.ollama import OllamaError

if TYPE_CHECKING:
    from lcode.agent import Agent

    pass


class Checks(AgentBase):
    """Agent's checks of its own work."""

    def reviewing(self) -> bool:
        return (
            self.settings.review != "off" and self.allowed_tools is None and not self.planning() and not self.no_changes
        )

    def self_review(self, answer: str) -> list[str]:
        """Check the request's change against the request before the answer counts; the problems found."""
        budget = int(self.settings.context * 3 * 0.25)
        diff, changed = review.diff_of(self.tools.before, self.current_text, budget)
        if changed < review.MIN_CHANGED_LINES:
            return []
        request = self.turn_requests[0] if self.turn_requests else ""
        if len(self.turn_requests) > 1:
            added = "\n".join(f"- {note}" for note in self.turn_requests[1:])
            request += f"\n\nWhat the user added while you worked:\n{added}"
        try:
            with (
                self.console.status("Reviewing the change against your request…", spinner="dots"),
                traces.purpose(cast("Agent", self), "review"),
            ):
                problems = review.run(cast("Agent", self), request, answer, diff)
        except OllamaError as e:
            self.say(f"The review didn't run: {e}", warning=True)
            return []
        self.review_results = problems
        if problems:
            self.say(
                f"Review: {len(problems)} point(s) sent back to the model. First: {problems[0][:160]}", warning=True
            )
        else:
            self.say("Review: the change does what was asked.")
        return problems

    def current_text(self, path: str) -> str | None:
        p = self.tools.resolve(path)
        try:
            return self.tools.read_text(p) if p.is_file() else None
        except OSError:
            return None

    def checks_prompt(self) -> str:
        check = self.project_check()
        if check is None:
            return ""
        return (
            f"\n- When you've changed files, lcode runs the project's checks (`{check.command}`) before your answer "
            "is final, and gives you the failures to fix. Run them yourself while you work, too."
        )

    def unrun_scripts(self, commands: list[str]) -> list[str]:
        """Shell scripts the model wrote in this request and never ran (only where the checks would run)."""
        if self.allowed_tools is not None or self.planning() or self.no_changes or self.settings.checks == "off":
            return []
        return checks.unrun_scripts(project_root_or_cwd(self.cwd), list(self.tools.before), commands)

    def untested_code(self, written: Iterable[str]) -> list[str]:
        """Code files the model wrote in this request and hasn't run since their last change (#242): only where the
        checks would run, and when the project has no checks of its own (lcode runs those instead). Window code has
        its own check: a screenshot shows it."""
        if self.allowed_tools is not None or self.planning() or self.no_changes or self.settings.checks == "off":
            return []
        if self.project_check() is not None:
            return []
        root = project_root_or_cwd(self.cwd)
        found = []
        for rel in written:
            path = root / rel
            try:
                if path.is_file() and not checks.GUI_IMPORT.search(path.read_text(errors="replace")):
                    found.append(rel)
            except OSError:
                continue
        return found

    def unseen_windows(self, tools_used: set[str]) -> dict[str, str]:
        """Window code the model changed in this request and never looked at (only where the checks would run)."""
        if self.allowed_tools is not None or self.planning() or self.no_changes or self.settings.checks == "off":
            return {}
        return checks.unseen_windows(project_root_or_cwd(self.cwd), list(self.tools.before), tools_used)

    def project_check(self) -> checks.Check | None:
        """The project's check command, for the main agent outside plan mode and reviews (None otherwise)."""
        if self.allowed_tools is not None or self.planning() or self.no_changes or self.settings.checks == "off":
            return None
        root = project_root_or_cwd(self.cwd)
        if self._check is None or self._check[0] != root or self._check[1] is None:  # tests may appear meanwhile
            self._check = (root, checks.find(root, self.settings.checks, self.settings.trust_project))
        return self._check[1]

    def model_ran_checks(self, command: str, result: str) -> bool:
        """Whether a command the model ran was the project's checks, and they passed."""
        check = self.project_check()
        if check is None or not checks.covers(check, command) or not checks.run_passed(check, command, result):
            return False
        self.check_results.append({"command": check.command, "exit_code": 0, "by": "model"})
        return True

    def check_folder(self, check: checks.Check) -> Path:
        """Where the checks run: the project's root, wherever the model's last `cd` left its shell. A command from the
        user's own setting runs where they started lcode."""
        return self.workspace if check.source == checks.YOUR_SETTING else project_root_or_cwd(self.cwd)

    def files_changed(self) -> list[str]:
        """The files this request changed, with the model's file tools or with its shell commands."""
        return [*self.tools.before, *self.checkpoints.changed_so_far()]

    def run_check(self, check: checks.Check, round_: int) -> str | None:
        """Run the checks after a change. Returns what to tell the model when they fail, else None."""
        self.say(f"Checking the work: {check.command} (from {check.source})")
        result = self.replay.check_result(check.command) if self.replay is not None else None
        if result is None:
            here, self.cwd = self.cwd, self.check_folder(check)
            try:
                result = self.tools.run("bash", {"command": check.command, "timeout": checks.TIMEOUT})
            finally:
                self.cwd = here
        code = checks.exit_code(result)
        if self.trace is not None:
            self.trace.check(check.command, result, code)
        self.check_results.append({"command": check.command, "exit_code": code, "by": "lcode"})
        if code is None:
            self.say(f"The checks didn't run: {result.splitlines()[0][:200]}", warning=True)
            return None
        if code == 0:
            self.say(f"Checks pass: {check.command}")
            return None
        if checks.no_tests(check, result):
            self.say(f"The checks found no tests to run ({check.command}).")
        elif checks.missing(check, result):
            self.say(
                f"The checks couldn't run ({check.command}): {result.splitlines()[0][:160]}. Install what they need, "
                'or set the command: lcode config set checks "…" (or checks off).',
                warning=True,
            )
        elif checks.no_test_ran(check, result) and not checks.names_any(result, self.files_changed()):
            self.say(
                f"The checks stopped before running a test ({check.command}): {checks.stop_reason(result)}. It doesn't "
                "name a file this request changed, so the model isn't asked to fix it. To get checks that run, fix "
                'that or set the command: lcode config set checks "…" (or checks off).',
                warning=True,
            )
        else:
            return self.failed_check(check, result, code, round_)
        self.check_results[-1]["exit_code"] = None  # no test ran: whoever reads this later shouldn't read a failure
        return None

    def failed_check(self, check: checks.Check, result: str, code: int, round_: int) -> str:
        """Say that the checks failed, or didn't finish; what to tell the model."""
        if checks.timed_out(result):
            self.check_results[-1]["exit_code"] = None
            self.say(
                f"The checks didn't finish in {checks.TIMEOUT} seconds and were stopped ({check.command}). To use a "
                'faster command: lcode config set checks "…" (or checks off).',
                warning=True,
            )
        elif round_ >= checks.MAX_ROUNDS:
            then = "" if self.can_escalate() else ": the model will say what's left"
            self.say(f"Checks still fail ({check.command}, exit code {code}){then}.", warning=True)
        else:
            self.say(
                f"Checks fail ({check.command}, exit code {code}): the model gets the output to fix it.", warning=True
            )
        return checks.failure_note(check, result, round_)
