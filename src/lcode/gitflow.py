"""Git workflow: /commit, /review, /pr, and sessions in a worktree of their own (lcode --worktree).

/commit and /pr ask the model for a message (or a title and description) in one extra request
alongside the conversation, so it knows why the changes were made and Ollama reuses the cached
prompt; that request isn't kept in the conversation. Nothing is committed, pushed or opened
without the user's yes, also in yolo mode. /review runs as a normal request in which the model can
read but not change anything.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:
    from lcode.agent import Agent

# Files that often hold secrets: /commit leaves them out unless the user stages them.
SENSITIVE = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "id_ecdsa*",
             "*credentials*", "*secret*", "*.keystore", "*.jks", ".netrc", ".pgpass")  # fmt: skip
MAX_UNTRACKED_LINES = 200  # of a new file, in the diff the model sees
MIN_DIFF_CHARS = 6000
MAX_DIFF_CHARS = 120_000

COMMIT_SCHEMA = {
    "type": "object",
    "properties": {"files": {"type": "array", "items": {"type": "string"}}, "message": {"type": "string"}},
    "required": ["files", "message"],
}
PR_SCHEMA = {
    "type": "object",
    "properties": {"branch": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"}},
    "required": ["branch", "title", "body"],
}

COMMIT_PROMPT = """Write the git commit message for the changes below.{hint}

The repository's recent commit messages; match their style (length, tense, capitals, prefixes such as "fix:", trailers):
{style}

Changed files{which}:
{files}

{diff}

Reply with JSON. "message" is the whole commit message: a subject line of at most 72 characters and, when the change needs explaining, a blank line and a body wrapped at 72 characters that says why, not what the diff already shows. {files_rule} Use what you know from this conversation about why the changes were made. Don't mention yourself or lcode."""

FILES_CHOSEN = """"files" lists the files that belong in this commit, from the list above: leave out files unrelated to the work (scratch files, logs, local experiments). When in doubt, include a file."""
FILES_STAGED = """The user staged exactly what to commit, so "files" is an empty list."""

PR_PROMPT = """Write a pull request for the branch's changes below, into {base}.{hint}

Commits:
{commits}

{diff}
{template}
Reply with JSON. "title" is a short title (at most 70 characters) in the style of the commits. "body" is the description in Markdown: what changes and why, and how it was tested when you know. Keep it short and concrete; no headings for a small change. {branch_rule} Use what you know from this conversation. Don't mention yourself or lcode."""

REVIEW_PROMPT = """Review {what} before it's {next}.

{stat}
{diff}

Look for:
1. Bugs: wrong logic, missed edge cases, error handling, wrong assumptions about inputs, races.
2. Tests: changed behaviour without a test, tests that don't check what they claim.
3. Security: injection, secrets in the code, unchecked input, too-broad permissions.
4. Clarity: confusing names, dead code, duplication, comments that no longer match the code. Only what matters.

Read the surrounding code where you need it: a diff alone can mislead. Don't change any files: this is a review. If the project's instructions or a skill describe how to review here, follow them.

Report each finding as `path:line`, its kind (bug, test, security or clarity), what's wrong and how to fix it, most important first. If nothing needs changing, say so in a sentence; don't invent problems."""


class GitError(Exception):
    pass


def git(cwd: Path, *args: str, check: bool = True, stdin: str | None = None, timeout: float = 120) -> str:
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, input=stdin, capture_output=True, text=True, timeout=timeout
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as e:
        raise GitError(f"git {args[0]} failed: {e}") from e
    if r.returncode != 0 and check:
        raise GitError((r.stderr or r.stdout).strip() or f"git {args[0]} failed")
    return r.stdout


def repo_root(cwd: Path) -> Path:
    try:
        return Path(git(cwd, "rev-parse", "--show-toplevel").strip())
    except GitError as e:
        raise GitError("this isn't a git repository") from e


def has_commits(root: Path) -> bool:
    return bool(git(root, "rev-parse", "--verify", "-q", "HEAD", check=False).strip())


# ----------------------------------------------------------------------------- changes


@dataclass
class Change:
    path: str
    code: str  # git's two status letters, e.g. " M", "A ", "??"

    @property
    def staged(self) -> bool:
        return self.code[0] not in " ?"

    @property
    def untracked(self) -> bool:
        return self.code == "??"


def changes(root: Path) -> list[Change]:
    out = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    found, parts, i = [], out.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            i += 1  # the old name follows
        found.append(Change(path, code))
    return found


def sensitive(path: str) -> bool:
    name = Path(path).name.lower()
    return any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE)


def diff_budget(agent: Agent) -> int:
    """How much diff (in characters) fits next to the conversation."""
    room = int(agent.settings.context * 0.8) - agent.ctx_used - 3000
    return max(MIN_DIFF_CHARS, min(MAX_DIFF_CHARS, room * 3))


def shorten(text: str, limit: int, what: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[… {len(text) - limit:,} more characters of the {what} not shown]"


def new_files(root: Path, paths: list[str]) -> str:
    parts = []
    for rel in paths:
        path = root / rel
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
                parts.append(f"new file: {rel} (not shown)")
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            parts.append(f"new file: {rel} (binary)")
            continue
        lines = data.decode(errors="replace").splitlines()
        more = f"\n[… {len(lines) - MAX_UNTRACKED_LINES} more lines]" if len(lines) > MAX_UNTRACKED_LINES else ""
        parts.append(f"new file: {rel}\n" + "\n".join(lines[:MAX_UNTRACKED_LINES]) + more)
    return "\n\n".join(parts)


def changes_diff(root: Path, files: list[Change], staged_only: bool, limit: int) -> str:
    """The diff of these changes, new files included, cut to `limit` characters."""
    tracked = [c.path for c in files if not c.untracked]
    text = ""
    if staged_only:
        text = git(root, "diff", "--cached", "--no-color")
    elif tracked:
        against = ["HEAD"] if has_commits(root) else ["--cached"]
        text = git(root, "diff", "--no-color", *against, "--", *tracked)
    untracked = new_files(root, [c.path for c in files if c.untracked])
    return shorten("\n\n".join(p for p in (text.strip(), untracked) if p), limit, "diff")


def style(root: Path) -> str:
    if not has_commits(root):
        return "(no commits yet)"
    subjects = git(root, "log", "-n", "15", "--no-merges", "--format=%s").strip()
    bodies = git(root, "log", "-n", "3", "--no-merges", "--format=--- %B").strip()
    return f"{subjects}\n\nThe latest three in full:\n{bodies}"


# ----------------------------------------------------------------------------- asking the model


def parse_json(content: str) -> dict:
    for candidate in (content, *re.findall(r"\{.*\}", content, re.S)):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def ask(agent: Agent, request: str, schema: dict) -> dict:
    """One structured answer, with the conversation as context; the request isn't kept in it."""
    history = agent.messages
    if agent.ctx_used + len(request) // 3 > agent.settings.context * 0.85:
        history = agent.messages[:1]  # too long: the instructions only
    payload = {
        "model": agent.settings.model,
        "messages": [*history, {"role": "user", "content": request}],
        "tools": agent.tool_schemas(),  # the same as the session's, so the cached prompt is reused
        "think": False,
        "format": schema,
        "keep_alive": agent.settings.keep_alive,
        "options": agent.options(),
    }
    reply = agent.ollama.chat(payload)
    return parse_json((reply.get("message") or {}).get("content") or "")


def clean_message(text: str) -> str:
    text = re.sub(r"^```\w*\n|\n```$", "", text.strip()).strip()
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def confirm(agent: Agent, question: str, editable: str | None = None) -> str:
    """'y', 'e' (when there's something to edit) or 'n'."""
    try:
        answer = input(question).strip().lower()
    except (EOFError, KeyboardInterrupt):
        agent.console.print()
        return "n"
    if answer in ("y", "yes"):
        return "y"
    if answer in ("e", "edit") and editable is not None:
        return "e"
    return "n"


# ----------------------------------------------------------------------------- /commit


def commit_command(agent: Agent, hint: str = "") -> None:
    c = agent.console
    try:
        root = repo_root(agent.cwd)
        found = changes(root)
    except GitError as e:
        c.print(f"[red]{escape(str(e))}[/]")
        return
    if not found:
        c.print("Nothing to commit: the working tree is clean.")
        return
    staged = [ch for ch in found if ch.staged]
    risky: list[str] = []
    if staged:
        candidates = staged
        unstaged = len([ch for ch in found if not ch.staged])
        c.print(
            f"[dim]Committing the {len(staged)} staged file(s)"
            + (f"; {unstaged} other changed file(s) stay out of it." if unstaged else ".")
            + "[/]"
        )
    else:
        risky = [ch.path for ch in found if sensitive(ch.path)]
        candidates = [ch for ch in found if not sensitive(ch.path)]
        if not candidates:
            c.print(
                f"Only files that may hold secrets changed ({escape(', '.join(risky))}). "
                "If they belong in a commit, stage them yourself (git add) and run /commit again."
            )
            return
    paths = [ch.path for ch in candidates]
    request = COMMIT_PROMPT.format(
        hint=f" The user adds: {hint}" if hint else "",
        style=style(root),
        which=" (staged)" if staged else "",
        files="\n".join(f"{ch.code.strip() or 'M'} {ch.path}" for ch in candidates),
        diff=changes_diff(root, candidates, bool(staged), diff_budget(agent)),
        files_rule=FILES_STAGED if staged else FILES_CHOSEN,
    )
    from lcode.ollama import OllamaError

    try:
        with c.status("[cyan]Writing the commit message…[/]", spinner="dots"):
            answer = ask(agent, request, COMMIT_SCHEMA)
    except KeyboardInterrupt:
        c.print("[dim]Cancelled.[/]")
        return
    except OllamaError as e:
        c.print(f"[red]Couldn't write a commit message: {escape(str(e))}[/]")
        return
    message = clean_message(str(answer.get("message") or ""))
    if not message:
        c.print("[red]The model didn't write a commit message.[/] Try again, or commit yourself.")
        return
    files = paths
    if not staged:
        chosen = [f for f in answer.get("files") or [] if f in paths]
        files = chosen or paths
    left_out = [p for p in paths if p not in files]
    from lcode import secretscan

    secrets_found = secretscan.in_files(root, files, bool(staged)) if agent.settings.secret_check else []
    while True:
        c.print(Panel(escape(message), title="Commit message", title_align="left", border_style="cyan"))
        c.print(f"  [bold]Files[/] {escape(', '.join(files))}")
        if left_out:
            c.print(f"  [dim]Left out (unrelated, the model thinks): {escape(', '.join(left_out))}[/]")
        if risky:
            c.print(f"  [dim]Left out (may hold secrets; stage them yourself if needed): {escape(', '.join(risky))}[/]")
        if secrets_found:
            c.print(Text(secretscan.report("commit", secrets_found), style="red"))
            c.print("  [dim]Commit them only if they aren't secrets; otherwise move them out of the code first.[/]")
        choice = confirm(agent, "  [y] commit · [e] edit the message · [n] cancel: ", message)
        if choice == "e":
            from lcode.planning import edit_text

            message = clean_message(edit_text(message)) or message
            continue
        break
    if choice != "y":
        c.print("Not committed.")
        return
    try:
        if not staged:
            git(root, "add", "-A", "--", *files)
        git(root, "commit", "-q", "-F", "-", stdin=message + "\n", timeout=600)  # commit hooks may run tests
        summary = git(root, "log", "-1", "--format=%h %s").strip()
    except GitError as e:
        c.print(f"[red]git commit failed:[/] {escape(str(e))}")
        return
    c.print(f"[green]✓ Committed[/] {escape(summary)}")


# ----------------------------------------------------------------------------- /review


def default_branch(root: Path) -> str:
    head = git(root, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", check=False).strip()
    if head:
        return head.removeprefix("origin/")
    for name in ("main", "master", "trunk", "develop"):
        if git(root, "rev-parse", "--verify", "-q", name, check=False).strip():
            return name
    return "main"


def base_ref(root: Path, base: str, remote: str = "origin") -> str:
    """<remote>/<base> when there is one (the local branch may be behind), else <base>."""
    for ref in (f"{remote}/{base}", base):
        if git(root, "rev-parse", "--verify", "-q", ref, check=False).strip():
            return ref
    raise GitError(f"there's no branch called {base}")


def is_branch(root: Path, name: str, remote: str = "origin") -> bool:
    refs = (f"refs/heads/{name}", f"refs/remotes/{remote}/{name}")
    return any(git(root, "rev-parse", "--verify", "-q", ref, check=False).strip() for ref in refs)


def review_prompt(agent: Agent, base: str = "") -> str:
    """The request for a review of the uncommitted changes, or of the branch against `base`.

    Uncommitted changes to tracked files are the work in progress, so they come first; otherwise
    (or with a base) the branch's changes since it left the base. New files are shown either way.
    """
    root = repo_root(agent.cwd)
    limit = diff_budget(agent)
    found = changes(root)
    untracked = [ch.path for ch in found if ch.untracked]
    work_in_progress = any(not ch.untracked for ch in found)
    commits = fork = ""
    explicit = bool(base)
    if base or (has_commits(root) and not work_in_progress):
        if not has_commits(root):
            raise GitError("nothing to compare with yet: the repository has no commits")
        base = base or default_branch(root)
        if is_branch(root, base):
            fork = git(root, "merge-base", base_ref(root, base), "HEAD").strip()
        elif explicit and git(root, "rev-parse", "--verify", "-q", base, check=False).strip():
            fork = git(root, "merge-base", base, "HEAD").strip()
        elif explicit:
            raise GitError(f"there's no branch or commit called {base}")
        if fork:
            commits = git(root, "log", "--format=- %s", f"{fork}..HEAD").strip()
    if fork and (commits or explicit or work_in_progress):
        branch = git(root, "branch", "--show-current").strip() or "HEAD"
        tracked = git(root, "diff", "--no-color", fork)  # the branch's commits and any uncommitted changes
        what, next_step = f"the changes on {branch} compared with {base}", "merged"
        stat = (
            f"Commits:\n{commits or '(none: uncommitted changes only)'}\n\n{git(root, 'diff', '--stat', fork).strip()}"
        )
    elif found:
        tracked = git(root, "diff", "--no-color", "HEAD") if has_commits(root) else ""
        what, next_step = "the uncommitted changes", "committed"
        stat = git(root, "diff", "--stat", "HEAD").strip() if has_commits(root) else ""
    else:
        where = f" and nothing on this branch that {base} doesn't have" if base else ""
        raise GitError(f"nothing to review: no uncommitted changes{where}")
    if untracked:
        stat += ("\n" if stat else "") + "New files, not yet added to git: " + ", ".join(untracked)
    diff = "\n\n".join(p for p in (tracked.strip(), new_files(root, untracked)) if p)
    if not diff:
        raise GitError(f"nothing to review: {what} are empty")
    return REVIEW_PROMPT.format(
        what=what, next=next_step, stat=stat, diff=f"```diff\n{shorten(diff, limit, 'diff')}\n```"
    )


# ----------------------------------------------------------------------------- /pr


def pr_command(agent: Agent, arg: str = "") -> None:
    c = agent.console
    if not shutil.which("gh"):
        c.print("[red]/pr needs the GitHub CLI:[/] install it from https://cli.github.com and run `gh auth login`.")
        return
    try:
        root = repo_root(agent.cwd)
        if not git(root, "remote").split():
            raise GitError("this repository has no remote to push to")
        branch = git(root, "branch", "--show-current").strip()
        if not branch:
            raise GitError("HEAD isn't on a branch (detached); switch to one first")
        uncommitted = [ch for ch in changes(root) if not ch.untracked]
        if uncommitted:
            c.print(
                f"There are uncommitted changes ({len(uncommitted)} file(s)). Commit them first with /commit, "
                "then run /pr again."
            )
            return
        remotes = git(root, "remote").split()
        remote = "origin" if "origin" in remotes else remotes[0]
        first, _, rest = arg.strip().partition(" ")
        if first and is_branch(root, first, remote):  # /pr develop, or /pr develop <what to say>
            base, hint = first, rest.strip()
        else:
            base, hint = default_branch(root), arg.strip()
        ref = base_ref(root, base, remote)
        commits = git(root, "log", "--format=- %s%n%b", f"{ref}..HEAD").strip()
    except GitError as e:
        c.print(f"[red]{escape(str(e))}[/]")
        return
    if not commits:
        c.print(f"No commits to open a pull request with: {escape(branch)} has nothing that {escape(ref)} doesn't.")
        return
    existing = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "url,state", "-q", 'select(.state == "OPEN") | .url'],
        cwd=root, capture_output=True, text=True, timeout=60,
    ).stdout.strip() if branch != base else ""  # fmt: skip
    if existing:
        c.print(f"A pull request for {escape(branch)} is already open: {existing}")
        if confirm(agent, f"  Push the new commits to {remote}/{branch}? [y] push · [n] no: ") == "y":
            push(agent, root, remote, branch)
        return
    new_branch = branch == base  # the commits are on the base branch: they go to a new branch
    template = next(
        (p for p in (root / ".github" / "pull_request_template.md", root / "docs" / "pull_request_template.md",
                     root / "PULL_REQUEST_TEMPLATE.md") if p.is_file()),
        None,
    )  # fmt: skip
    request = PR_PROMPT.format(
        base=base,
        hint=f" The user adds: {hint}" if hint else "",
        commits=commits,
        diff=f"```diff\n{shorten(git(root, 'diff', '--no-color', f'{ref}...HEAD'), diff_budget(agent), 'diff')}\n```",
        template=f"\nFill in the repository's pull request template:\n{template.read_text()[:4000]}\n"
        if template
        else "",
        branch_rule=(
            '"branch" is a short name for a new branch for these commits, such as fix-login-timeout.'
            if new_branch
            else '"branch" is an empty string.'
        ),
    )
    from lcode.ollama import OllamaError

    try:
        with c.status("[cyan]Writing the pull request…[/]", spinner="dots"):
            answer = ask(agent, request, PR_SCHEMA)
    except KeyboardInterrupt:
        c.print("[dim]Cancelled.[/]")
        return
    except OllamaError as e:
        c.print(f"[red]Couldn't write the pull request: {escape(str(e))}[/]")
        return
    title = " ".join(str(answer.get("title") or "").split())
    body = clean_message(str(answer.get("body") or ""))
    if not title:
        c.print("[red]The model didn't write a title.[/] Try again, or use gh pr create yourself.")
        return
    head = branch
    if new_branch:
        head = branch_name(str(answer.get("branch") or "")) or f"lcode-{time.strftime('%m%d')}-{secrets.token_hex(2)}"
    while True:
        c.print(
            Panel(escape(f"{title}\n\n{body}".strip()), title="Pull request", title_align="left", border_style="cyan")
        )
        if new_branch:
            c.print(f"  [dim]The commits are on {escape(base)}, so they go to a new branch: {escape(head)}[/]")
        choice = confirm(
            agent,
            f"  Push {head} to {remote} and open the pull request into {base}? [y] yes · [e] edit · [n] no: ",
            body,
        )
        if choice == "e":
            from lcode.planning import edit_text

            edited = edit_text(f"{title}\n\n{body}").strip()
            if edited:
                title, _, body = edited.partition("\n")
                title, body = title.strip(), body.strip()
            continue
        break
    if choice != "y":
        c.print("No pull request opened.")
        return
    try:
        if new_branch:
            git(root, "switch", "-q", "-c", head)
            c.print(f"[dim]Switched to the new branch {escape(head)}; {escape(base)} still has the commits locally.[/]")
        if not push(agent, root, remote, head):
            return
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(body + "\n")
        try:
            r = subprocess.run(
                ["gh", "pr", "create", "--base", base, "--head", head, "--title", title, "--body-file", f.name],
                cwd=root, capture_output=True, text=True, timeout=120,
            )  # fmt: skip
        finally:
            Path(f.name).unlink(missing_ok=True)
    except (GitError, OSError, subprocess.SubprocessError) as e:
        c.print(f"[red]{escape(str(e))}[/]")
        return
    if r.returncode != 0:
        c.print(f"[red]gh pr create failed:[/] {escape((r.stderr or r.stdout).strip())}")
        return
    c.print(f"[green]✓ Opened[/] {r.stdout.strip().splitlines()[-1] if r.stdout.strip() else 'the pull request'}")


def push(agent: Agent, root: Path, remote: str, branch: str) -> bool:
    from lcode import secretscan

    found = secretscan.outgoing(root, branch) if agent.settings.secret_check else []
    if found:
        agent.console.print(Text(secretscan.report("push", found), style="red"))
        if confirm(agent, "  Push anyway? [y]es / [n]o: ") != "y":
            agent.console.print("Not pushed.")
            return False
    try:
        with agent.console.status(f"Pushing {escape(branch)}…", spinner="dots"):
            git(root, "push", "-q", "-u", remote, branch, timeout=300)
    except GitError as e:
        agent.console.print(f"[red]git push failed:[/] {escape(str(e))}")
        return False
    agent.console.print(f"[green]✓ Pushed[/] {escape(branch)}")
    return True


def branch_name(text: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._/-]+", "-", text.strip()).strip("-/.")
    return re.sub(r"-{2,}", "-", name)[:60]


# ----------------------------------------------------------------------------- lcode --worktree


@dataclass
class WorktreeSession:
    root: Path  # the repository's main working tree
    path: Path
    branch: str
    base: str  # the commit the main working tree was on when the session started
    created_branch: bool
    cwd: Path

    def finish(self) -> str:
        """Remove the worktree when nothing changed in it; otherwise say where the work is."""
        try:
            dirty = git(self.path, "status", "--porcelain").strip()
            ahead = int(git(self.root, "rev-list", "--count", f"{self.base}..{self.branch}").strip() or 0)
        except (GitError, ValueError):
            return f"The worktree stays at {self.path}."
        if not dirty and not ahead:
            git(self.root, "worktree", "remove", str(self.path), check=False)
            if self.created_branch:
                git(self.root, "branch", "-D", self.branch, check=False)
            return f"Nothing changed in the worktree, so it was removed{' with its branch' if self.created_branch else ''}."
        parts = [f"{ahead} commit(s)" if ahead else "", "uncommitted changes" if dirty else ""]
        return (
            f"The worktree stays at {self.path} (branch {self.branch}: {' and '.join(p for p in parts if p)}). "
            f"Continue there with `lcode --worktree {self.branch}`; merge with `git merge {self.branch}`; "
            f"remove it with `git worktree remove {self.path}`."
        )


def main_checkout(cwd: Path) -> Path:
    """The repository's main working tree, also from inside one of its worktrees."""
    root = repo_root(cwd)
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    return common.parent if common.name == ".git" else root


def worktree_path(root: Path, name: str) -> Path:
    """Outside the repository: inside it, the main checkout's path would lead models astray."""
    from lcode import config
    from lcode.memory import slugify

    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:8]
    return config.STATE_DIR / "worktrees" / f"{slugify(root.name, 40) or 'repo'}-{key}" / name.replace("/", "-")


def start_worktree(cwd: Path, name: str) -> WorktreeSession:
    """A worktree on its own branch, for a session that works in parallel with others."""
    root = main_checkout(cwd)
    if not has_commits(root):
        raise GitError("a worktree needs a repository with at least one commit")
    name = name or f"lcode-{time.strftime('%m%d')}-{secrets.token_hex(2)}"
    if git(root, "check-ref-format", "--branch", name, check=False).strip() != name:
        raise GitError(f"{name!r} isn't a valid branch name")
    path = worktree_path(root, name)
    base = git(root, "rev-parse", "HEAD").strip()
    created = False
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        git(root, "worktree", "prune")  # forget worktrees whose folders were deleted
        if git(root, "rev-parse", "--verify", "-q", f"refs/heads/{name}", check=False).strip():
            git(root, "worktree", "add", "-q", str(path), name)
        else:
            git(root, "worktree", "add", "-q", "-b", name, str(path), "HEAD")
            created = True
    here = cwd.resolve()
    sub = here.relative_to(root.resolve()) if here.is_relative_to(root.resolve()) else Path()
    inside = path / sub
    return WorktreeSession(root, path, name, base, created, inside if inside.is_dir() else path)
