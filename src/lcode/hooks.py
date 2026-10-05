"""Hooks and permission rules: what should always happen, whatever the model does.

Hooks are shell commands that run on lcode's events, set in `~/.config/lcode/config.toml` or, per
repository, in `.lcode/settings.toml` (used once you approve it, like the repository's commands):

    [[hooks]]
    event = "after_tool"              # before_tool | after_tool | after_request | session_start | notification
    tools = ["edit_file", "write_file"]
    paths = ["*.py"]
    command = "ruff format {path} && ruff check --fix {path}"

The event comes as JSON on stdin (and `{path}`, `{tool}` in the command are replaced, quoted). A
`before_tool` hook that exits with code 2 blocks the call, and what it printed tells the model why.
An `after_tool` hook's output reaches the model when it fails, or always with `feedback = true`.

Permission rules are checked before lcode asks, and deny always wins, even in yolo mode:

    [permissions]
    allow = ["bash:pytest *", "bash:npm test", "edit:src/**"]
    deny = ["bash:git push --force*", "edit:.env"]
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from lcode import config
from lcode.checkpoints import work_tree_for

EVENTS = ("before_tool", "after_tool", "after_request", "session_start", "notification")
BLOCK = 2  # exit code of a before_tool hook that blocks the call
MAX_OUTPUT = 4000
FILE_TOOLS = {"read_file", "write_file", "edit_file", "view_image", "list_dir"}


class HookError(ValueError):
    pass


def project_settings(cwd: Path) -> Path:
    return work_tree_for(cwd) / ".lcode" / "settings.toml"


def read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except OSError:
        return {}
    except tomllib.TOMLDecodeError as e:
        raise HookError(f"{path} isn't valid TOML: {e}") from e


# ----------------------------------------------------------------------------- permission rules


def strip_cd(command: str) -> str:
    return re.sub(r"^\s*(cd\s+\S+\s*&&\s*)+", "", command).strip()


def matches_path(pattern: str, path: str) -> bool:
    """gitignore-like: a pattern without a slash matches the file name anywhere."""
    if "/" not in pattern.rstrip("/"):
        return fnmatch.fnmatch(Path(path).name, pattern) or fnmatch.fnmatch(path, pattern)
    pattern = pattern.removeprefix("./").removeprefix("/")
    return fnmatch.fnmatch(path, pattern) or (pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]))


@dataclass
class Rules:
    allow: list[tuple[str, str]] = field(default_factory=list)  # (rule, where it's from)
    deny: list[tuple[str, str]] = field(default_factory=list)

    def add(self, data: dict, source: str) -> None:
        section = data.get("permissions") or {}
        for kind in ("allow", "deny"):
            values = section.get(kind) or []
            if not isinstance(values, list) or not all(isinstance(v, str) and ":" in v for v in values):
                raise HookError(f'{source}: permissions.{kind} must be a list of rules like "bash:pytest *"')
            getattr(self, kind).extend((v, source) for v in values)

    def _matches(self, rule: str, kind: str, target: str) -> bool:
        rule_kind, _, pattern = rule.partition(":")
        if rule_kind != kind or not target:
            return False
        if kind == "bash":
            return fnmatch.fnmatch(strip_cd(target), pattern)
        if kind == "edit":
            return matches_path(pattern, target)
        return fnmatch.fnmatch(target, pattern)

    def check(self, kind: str, target: str) -> tuple[str | None, str]:
        """("deny" or "allow", the rule and where it's from), or (None, "")."""
        if kind == "bash":  # a denied command stays denied inside a chain: git add . && git push --force
            parts = [p for p in re.split(r"\s*(?:&&|\|\||;|\|)\s*", strip_cd(target)) if p]
            for rule, source in self.deny:
                if any(self._matches(rule, kind, part) for part in [target, *parts]):
                    return "deny", f"{rule} ({source})"
            for rule, source in self.allow:
                if self._matches(rule, kind, target):
                    return "allow", f"{rule} ({source})"
            if parts and all(any(self._matches(r, kind, part) for r, _ in self.allow) for part in parts):
                return "allow", "every part of the command is allowed"
            return None, ""
        for rule, source in self.deny:
            if self._matches(rule, kind, target):
                return "deny", f"{rule} ({source})"
        for rule, source in self.allow:
            if self._matches(rule, kind, target):
                return "allow", f"{rule} ({source})"
        return None, ""

    def __bool__(self) -> bool:
        return bool(self.allow or self.deny)


# ----------------------------------------------------------------------------- hooks


@dataclass
class Hook:
    event: str
    command: str
    source: str
    tools: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    timeout: float = 60.0
    feedback: bool = False

    def applies(self, tool: str = "", path: str = "") -> bool:
        if self.tools and not any(fnmatch.fnmatch(tool, t) for t in self.tools):
            return False
        return not self.paths or bool(path and any(matches_path(p, path) for p in self.paths))


@dataclass
class Outcome:
    hook: Hook
    code: int
    output: str


@dataclass
class HookSet:
    hooks: list[Hook] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def add(self, data: dict, source: str) -> None:
        entries = data.get("hooks") or []
        if not isinstance(entries, list):
            raise HookError(f"{source}: hooks must be [[hooks]] tables")
        for entry in entries:
            event, command = entry.get("event"), entry.get("command")
            if event not in EVENTS or not isinstance(command, str) or not command.strip():
                self.problems.append(f"{source}: a hook needs an event ({', '.join(EVENTS)}) and a command")
                continue
            self.hooks.append(
                Hook(
                    event=event,
                    command=command,
                    source=source,
                    tools=list(entry.get("tools") or []),
                    paths=list(entry.get("paths") or []),
                    timeout=float(entry.get("timeout") or 60),
                    feedback=bool(entry.get("feedback", False)),
                )
            )

    def for_event(self, event: str, tool: str = "", path: str = "") -> list[Hook]:
        return [h for h in self.hooks if h.event == event and h.applies(tool, path)]

    def run(self, event: str, payload: dict, cwd: Path, tool: str = "", path: str = "") -> list[Outcome]:
        outcomes = []
        for hook in self.for_event(event, tool, path):
            outcomes.append(run_hook(hook, {"event": event, **payload}, cwd, tool, path))
        return outcomes

    def notify(self, message: str, cwd: Path) -> None:
        """Notification hooks, in the background: lcode doesn't wait for them."""
        hooks = self.for_event("notification")
        if hooks:

            def go() -> None:
                for hook in hooks:
                    run_hook(hook, {"event": "notification", "message": message}, cwd, "", "")

            threading.Thread(target=go, daemon=True).start()


def run_hook(hook: Hook, payload: dict, cwd: Path, tool: str, path: str) -> Outcome:
    command = hook.command.replace("{path}", shlex.quote(path) if path else "").replace("{tool}", shlex.quote(tool))
    env = {**os.environ, "LCODE_EVENT": payload.get("event", ""), "LCODE_TOOL": tool, "LCODE_PATH": path}
    try:
        r = subprocess.run(
            ["bash", "-c", command],
            input=json.dumps(payload, default=str),
            capture_output=True,
            text=True,
            timeout=hook.timeout,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return Outcome(hook, 124, f"the hook took longer than {hook.timeout:.0f}s and was stopped")
    except OSError as e:
        return Outcome(hook, 127, str(e))
    output = (r.stdout + ("\n" if r.stdout and r.stderr else "") + r.stderr).strip()
    return Outcome(hook, r.returncode, output[:MAX_OUTPUT])


# ----------------------------------------------------------------------------- loading


def load(cwd: Path, trust_project: bool) -> tuple[HookSet, Rules]:
    """The user's hooks and rules, plus the repository's when its settings are approved."""
    hooks, rules = HookSet(), Rules()
    sources = [(config.CONFIG_PATH, str(config.CONFIG_PATH))]
    if trust_project:
        project = project_settings(cwd)
        sources.append((project, str(project)))
    for path, label in sources:
        try:
            data = read_toml(path)
            hooks.add(data, label)
            rules.add(data, label)
        except HookError as e:
            hooks.problems.append(str(e))
    return hooks, rules
