# Git workflow

The everyday git steps, without spelling them out each time:
- **`/commit`** writes a message in your repository's style;
- **`/review`** checks the changes before you commit or merge;
- **`/pr`** opens a pull request;
- **`lcode --worktree`** runs several sessions on one repository without them getting in each other's way.

lcode never commits, pushes or opens a pull request until you say yes, also in `yolo` mode.

## /commit

```text
❯ /commit
╭─ Commit message ───────────────────────────────────────────────╮
│ feat: add median()                                             │
│                                                                │
│ Add a median() function to stats.py and its tests. It raises   │
│ ValueError on empty input, consistent with mean().             │
╰────────────────────────────────────────────────────────────────╯
  Files stats.py, test_stats.py
  Left out (unrelated, the model thinks): debug.log
  [y] commit · [e] edit the message · [n] cancel: y
✓ Committed fecd803 feat: add median()
```

- **What goes in.**
  - If you staged files (`git add`), exactly those.
  - Otherwise every changed and new file, except:
    - files the model thinks are unrelated to the work, listed so you can see them;
    - files that may hold secrets (`.env`, `*.pem`, keys, credentials and the like). These only go in when you stage them yourself.
- **The message.** It's written from the diff, from what the conversation knows about why you made the change, and from the repository's recent commits: their prefixes, tense, length and trailers. `/commit mention ticket ABC-12` adds your own notes.
- **Editing.** `e` opens the message in your editor (`$EDITOR`).
- **Hooks.** Your git hooks run as usual: a failing pre-commit hook stops the commit and shows why.

## /review

| | Reviews |
|---|---|
| `/review` | The uncommitted changes if any tracked file changed; otherwise the branch's changes compared with the default branch (`main`) |
| `/review develop` | The branch, and any uncommitted changes, compared with `develop` |

The model reads the code around the changes where it needs to. During a review it can't change
files or run commands that change anything. It reports findings with `path:line`, the kind of
problem (bug, test, security or clarity), what's wrong and how to fix it, most important first:

```text
stats.py:18 — bug
The even-length median is broken: for median([1, 3, 2, 4]) the old code returns 2.5, the new
code returns 3. Revert line 18 to (s[mid - 1] + s[mid]) / 2.

test_stats.py:13 — test
test_median() asserts median([1, 3, 2, 4]) == 2.5, so it fails with this change.
```

Then ask it to fix what you agree with, in the same conversation.

**Your own checklist:** put review guidelines in `AGENTS.md` or in a [skill](commands.md#skills),
and the review follows them. To replace `/review` entirely, write a
[command](commands.md#commands) of your own called `review`. The same goes for `/commit` and `/pr`.

## /pr

Pushes the branch and opens a GitHub pull request. It needs the [GitHub CLI](https://cli.github.com),
signed in with `gh auth login`.

- **Into which branch.** The default branch, or the one you name: `/pr develop`. Anything else you
  type is a note for the description: `/pr develop link it to #12`.
- **Before you start.** Commit first: `/pr` stops while there are uncommitted changes.
- **Commits made on `main` itself** go to a new branch named after the change. Your local `main`
  still has them, so reset it once the pull request is merged.
- **The title and description** are written from the commits, the diff and the conversation, and
  follow the repository's pull request template if it has one. You see them before anything is
  pushed: `y` to push and open, `e` to edit, `n` to stop.
- **A branch with an open pull request:** `/pr` offers to push the new commits instead.

## Parallel sessions: lcode --worktree

```bash
lcode --worktree fix-login     # a session on a new branch fix-login, in a folder of its own
lcode -w                       # the same, with a generated name
```

A [git worktree](https://git-scm.com/docs/git-worktree) is a second working folder of the same
repository, on its own branch. Sessions in different worktrees can edit, test and commit at the
same time without touching each other's files, or yours.

- **Where it starts.** The worktree starts from your current commit. Uncommitted changes in your
  own folder don't come along.
- **Where it lives.** lcode keeps worktree folders in `~/.local/state/lcode/worktrees/` and prints
  the path. They're outside the repository so the model can't mistake your folder for its own.
- **When the session ends.**
  - If nothing changed (no commits and no uncommitted changes), the worktree and its branch are
    removed.
  - Otherwise it stays, and lcode tells you how to continue (`lcode --worktree fix-login` again),
    merge it (`git merge fix-login`) or remove it.
- **What's separate.** Sessions (`/resume`, `lcode -c`) and [checkpoints](usage.md#undo-and-checkpoints)
  belong to each worktree. [Memory notes](memory.md) are shared with the repository.
