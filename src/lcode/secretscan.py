"""Stop secrets before they're committed or pushed.

Before the model runs `git commit` or `git push`, lcode scans what that would add to the repository's
history: the lines a commit adds (including files a `git add` in the same command stages), or every commit
a push would send. Passwords and tokens in code, private keys, credentials in URLs (`rtsp://user:pass@…`)
and `.env` files are reported with their file and line. In `ask` mode the user decides; in the other modes
the command doesn't run, and the model is told to move the secret out of the code.

A line that holds a value which only looks like a secret can carry `lcode: allow-secret` (or
`gitleaks:allow`) in a comment, and is skipped.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lcode.memory import SECRET_PATTERNS

ALLOW = ("lcode: allow-secret", "lcode:allow-secret", "gitleaks:allow")
MAX_FINDINGS = 10
GIT_TIMEOUT = 20
MAX_FILE = 1_000_000  # bytes of an untracked file that are scanned

# Values that only stand in for a secret.
PLACEHOLDER = re.compile(
    r"^(?:change[_-]?me\w*|your[_-]?\w*|x+|\*+|\.+|…+|<[^>]*>"
    r"|(?:dummy|example|placeholder|redacted|secret|password|passwd|pass|pwd|token|test|changeit|none|null"
    r"|true|false|required|optional|hidden|masked)(?:[_-]?here)?)$",
    re.I,
)
# Words that only appear in made-up values: "change-me-in-production", "test-token", "example-key"…
FAKE_WORDS = (
    "example",
    "sample",
    "dummy",
    "fake",
    "changeme",
    "change-me",
    "change_me",
    "placeholder",
    "redacted",
    "test",
)
# A password, key or token written in code or configuration as a literal value.
ASSIGNED = re.compile(
    r"""(?P<name>\b[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key"""
    r"""|client[_-]?secret)[\w.-]*)['"]?\s*(?::|=|:=|=>)\s*(?P<quote>['"]?)(?P<value>[^\s'"`,;)}\]]{6,})"""
    r"""(?P=quote)""",
    re.I,
)
# Names that hold something about a secret (where it is, how it looks), not the secret itself.
ABOUT = re.compile(
    r"(?:url|uri|endpoint|file|path|dir|env|var|variable|name|field|header|type|label|prompt|hint|length|len|min|"
    r"max|policy|pattern|regex|format|count|expires?|expiry|ttl|timeout|limit|required|enabled|hash|salt_rounds)$",
    re.I,
)
EXTRA = [
    ("an xAI API key", re.compile(r"\bxai-[A-Za-z0-9]{20,}")),
    ("a Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("a Stripe key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}")),
]
CONFIG_FILES = re.compile(r"(?:\.(?:env|ini|cfg|conf|config|properties|ya?ml|toml)|(?:^|/)\.env[\w.-]*)$", re.I)
URL_PASSWORD = re.compile(r"\b[a-z][a-z0-9+.\-]*://(?P<user>[^\s/:@'\"]+):(?P<value>[^\s/@'\"]+)@", re.I)
STRONG = [p for p in SECRET_PATTERNS if p[0] not in ("a password in a URL", "a password or key")] + EXTRA
ENV_FILE = re.compile(r"(?:^|/)\.env(?:\.[\w-]+)?$")
ENV_EXAMPLES = re.compile(r"\.(?:example|sample|template|dist|defaults?)$")


@dataclass
class Finding:
    path: str
    line: int  # 0: the whole file
    label: str  # what it looks like, e.g. "a password"
    excerpt: str  # the line, with the secret masked
    commit: str = ""  # for a push: the commit that adds it

    def describe(self, excerpt: bool = True) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        if self.commit:
            where += f" (commit {self.commit})"
        return f"{where}: {self.label}" + (f": {self.excerpt}" if self.excerpt and excerpt else "")


def mask(value: str) -> str:
    return value[:2] + "…" if len(value) > 4 else "…"


def placeholder(value: str) -> bool:
    """A value that stands in for a secret: CHANGE_ME, <token>, ${PASSWORD}, a path, a web address…"""
    if PLACEHOLDER.match(value) or any(c in value for c in "{}$%<>"):
        return True
    if any(word in value.lower() for word in FAKE_WORDS):
        return True
    return value.startswith(("/", "./", "~/", "http:", "https:", "os.", "process.env", "env."))


def scan_line(text: str, config: bool = False) -> tuple[str, str] | None:
    """What kind of secret the line holds, and the line with it masked. In configuration files (`config`) a
    value counts without quotes too, as in `PASSWORD=hunter22` or `password: hunter22`."""
    if any(marker in text for marker in ALLOW):
        return None
    shown = text.strip()[:160]
    for label, pattern in STRONG:
        found = pattern.search(text)
        if found:
            return label, shown.replace(found.group(0), mask(found.group(0)))
    found = URL_PASSWORD.search(text)
    if found and not placeholder(found.group("value")) and not placeholder(found.group("user")):
        return "a password in a URL", shown.replace(found.group("value"), mask(found.group("value")))
    for found in ASSIGNED.finditer(text):
        value = found.group("value")
        if placeholder(value) or ABOUT.search(found.group("name")):
            continue
        if not found.group("quote") and not (config and re.match(r"^\s*(?:export\s+)?[\w.-]+\s*[:=]", text)):
            continue  # in code, only a quoted literal is a value: `password = args.password` isn't one
        return "a password or key", shown.replace(value, mask(value))
    return None


def scan_file(path: str, lines: list[tuple[int, str]], commit: str = "") -> list[Finding]:
    if ENV_FILE.search(path) and not ENV_EXAMPLES.search(path):
        return [Finding(path, 0, "an environment file (.env), which usually holds secrets", "", commit)]
    findings = []
    config = bool(CONFIG_FILES.search(path))
    for number, text in lines:
        hit = scan_line(text, config)
        if hit:
            findings.append(Finding(path, number, hit[0], hit[1], commit))
    return findings


def scan_diff(diff: str) -> list[Finding]:
    """The secrets in the lines a unified diff adds (`git diff`, or `git log -p` with commit: lines)."""
    findings: list[Finding] = []
    path, number, commit = "", 0, ""
    added: list[tuple[int, str]] = []

    def flush() -> None:
        if path and (added or ENV_FILE.search(path)):
            findings.extend(scan_file(path, added, commit))
        added.clear()

    for line in diff.splitlines():
        if line.startswith("commit:"):
            flush()
            commit, path = line.split(":", 1)[1].strip(), ""
        elif line.startswith("+++ "):
            flush()
            target = line[4:].strip()
            path = "" if target == "/dev/null" else target.removeprefix("b/")
        elif line.startswith("@@"):
            hunk = re.search(r"\+(\d+)", line)
            number = int(hunk.group(1)) if hunk else 0
        elif line.startswith("+") and path:
            added.append((number, line[1:]))
            number += 1
        elif line.startswith(" "):
            number += 1
    flush()
    return findings


def git(cwd: Path, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, errors="replace", timeout=GIT_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


GIT_VERB = re.compile(r"\bgit\b(?:\s+-[-\w=.]+(?:\s+[^\s'\"-]\S*)?)*\s+(commit|push)\b")


@dataclass
class GitSteps:
    adds: list[list[str]]  # the arguments of each `git add`
    commit: list[str] | None  # the arguments of `git commit`
    push: list[str] | None  # the arguments of `git push`


def git_steps(command: str) -> GitSteps | None:
    """The git add, commit and push a shell command runs, or None when it commits and pushes nothing."""
    steps = GitSteps([], None, None)
    for segment in re.split(r"&&|\|\||;|\n|\|", command):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        while words and re.fullmatch(r"\w+=\S*", words[0]):
            words = words[1:]  # VAR=value in front
        if len(words) < 2 or words[0] != "git":
            continue
        i = 1
        while i < len(words) and words[i].startswith("-"):
            i += 2 if words[i] in ("-C", "-c") else 1  # git -C dir / -c key=value
        if i >= len(words):
            continue
        verb, rest = words[i].strip("'\""), words[i + 1 :]
        if verb == "add":
            steps.adds.append(rest)
        elif verb == "commit":
            steps.commit = rest
        elif verb == "push":
            steps.push = rest
    # Inside quotes, as in bash -c "git add . && git commit …", sh -c or eval, what gets staged can't be told
    # from the words: assume everything.
    nested = [verb for m in re.finditer(r"(['\"])(.*?)\1", command, re.S) for verb in GIT_VERB.findall(m.group(2))]
    if steps.commit is None and steps.push is None:
        nested += GIT_VERB.findall(command)
    if "commit" in nested:
        steps.commit, steps.adds = ["--all"], [["."]]
    if "push" in nested and steps.push is None:
        steps.push = []
    return steps if steps.commit is not None or steps.push is not None else None


def untracked(cwd: Path, paths: list[str]) -> list[Finding]:
    findings = []
    for name in git(cwd, "ls-files", "--others", "--exclude-standard", "--", *paths).splitlines():
        file = cwd / name
        try:
            if file.stat().st_size > MAX_FILE:
                continue
            text = file.read_text(errors="strict")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable
        findings.extend(scan_file(name, list(enumerate(text.splitlines(), 1))))
    return findings


def to_commit(cwd: Path, steps: GitSteps) -> list[Finding]:
    """What `git add` and `git commit` in the command would commit: the staged changes, plus what it stages."""
    args = steps.commit or []
    everything = any(a in ("-a", "--all") or re.fullmatch(r"-[a-zA-Z]*a[a-zA-Z]*", a) for a in args if a != "--amend")
    findings = scan_diff(git(cwd, "diff", "--cached", "--no-color", "--no-ext-diff", "-U0"))
    if everything:
        findings += scan_diff(git(cwd, "diff", "--no-color", "--no-ext-diff", "-U0"))
    for add in steps.adds:
        options = [a for a in add if a.startswith("-")]
        paths = [a for a in add if not a.startswith("-")] or (["."] if {"-A", "--all"} & set(options) else [])
        if not paths and "-u" not in options and "--update" not in options:
            continue
        paths = paths or ["."]
        findings += scan_diff(git(cwd, "diff", "--no-color", "--no-ext-diff", "-U0", "--", *paths))
        if "-u" not in options and "--update" not in options:
            findings += untracked(cwd, paths)
    return findings


def to_push(cwd: Path, steps: GitSteps) -> list[Finding]:
    """The secrets in the commits a push would send that no remote has yet."""
    refs = [a for a in (steps.push or []) if not a.startswith("-")]
    branches = [r.split(":", 1)[0].lstrip("+") for r in refs[1:]] or ["HEAD"]
    findings: list[Finding] = []
    for branch in branches:
        if not branch or not git(cwd, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}").strip():
            continue
        log = git(
            cwd, "log", "-p", "--no-color", "--no-ext-diff", "-U0", "--format=commit:%h", branch, "--not", "--remotes"
        )
        findings += scan_diff(log)
    return findings


def check(cwd: Path, command: str) -> tuple[str, list[Finding]]:
    """('commit' or 'push', the secrets it would add to the history) for a shell command."""
    steps = git_steps(command)
    if steps is None or not git(cwd, "rev-parse", "--git-dir").strip():
        return "", []
    findings: list[Finding] = []
    if steps.commit is not None:
        findings += to_commit(cwd, steps)
    if steps.push is not None:
        findings += to_push(cwd, steps)
    unique = list({(f.path, f.line, f.label): f for f in findings}.values())
    return ("push" if steps.push is not None and steps.commit is None else "commit"), unique


def in_files(cwd: Path, paths: list[str], staged: bool) -> list[Finding]:
    """For lcode's /commit: the secrets in the staged changes, or in the changes to these files."""
    if staged:
        return scan_diff(git(cwd, "diff", "--cached", "--no-color", "--no-ext-diff", "-U0"))
    if not paths:
        return []
    diffs = [
        git(cwd, "diff", *staged_flag, "--no-color", "--no-ext-diff", "-U0", "--", *paths)
        for staged_flag in ([], ["--cached"])
    ]
    return scan_diff("\n".join(diffs)) + untracked(cwd, paths)


def outgoing(cwd: Path, branch: str = "HEAD") -> list[Finding]:
    """For lcode's /pr: the secrets in the commits of `branch` that no remote has yet."""
    return to_push(cwd, GitSteps([], None, ["origin", branch]))


def report(action: str, findings: list[Finding], excerpts: bool = True) -> str:
    """The findings, one per line; without `excerpts` for places others read, like a public comment."""
    lines = [f"- {f.describe(excerpts)}" for f in findings[:MAX_FINDINGS]]
    if len(findings) > MAX_FINDINGS:
        lines.append(f"- … and {len(findings) - MAX_FINDINGS} more")
    return f"This {action} would add what looks like a secret to the repository's history:\n" + "\n".join(lines)


ADVICE = (
    "lcode didn't run the command. Don't commit secrets: read them from an environment variable or from a "
    "configuration file that git ignores (and commit an example with placeholders instead). If one of these "
    "isn't a secret, tell the user: they decide."
)
