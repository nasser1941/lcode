# Memory

Every session used to start from zero. Whatever you and the model worked out was lost unless
someone wrote it into `AGENTS.md`: the test command that actually works, "we use pnpm, not npm",
that the staging database needs the VPN, a correction you had to give twice.

lcode now keeps short **notes** that later sessions load: only key knowledge, not whole
conversations. A note is one fact, in one line, with optional details.

```text
❯ /memory
Memory
 #  For          Type       Note                                                  Updated
 1  every repo   feedback   Prefers short answers without a summary at the end  2026-10-03
 2  this repo    project    Run the tests with `uv run pytest -q`; plain pyte…  2026-10-05
 3  this repo    reference  Staging logs are in Grafana, dashboard "api-prod"   2026-10-04
```

## What gets saved

Only what a future session needs and can't find in the code, the git history or `AGENTS.md`. There
are four kinds:

| Type | For | Example |
|---|---|---|
| `feedback` | Your corrections and preferences | "Use pnpm, not npm" |
| `project` | Decisions, constraints, ongoing work | "The staging database needs the VPN" |
| `reference` | Where things live outside the repository | "Bugs are tracked in Linear, project API" |
| `user` | Who you are and how you like to work | "Prefers short answers" |

Never saved: code structure, file paths, what changed in a session (git has that), temporary
state, and **secrets**. lcode refuses any note that looks like it contains a password, API key,
token, private key or a URL with a password in it, whoever writes it. Write where the secret lives
instead ("staging credentials are in 1Password, vault Dev").

## How notes get saved

- **The model** saves a note with its `memory` tool when it learns something worth keeping, for
  example after you correct it. It can also update a note (same name) or delete one that turned out
  wrong.
- **You** save one with `/remember`:

    ```text
    ❯ /remember the staging database needs the VPN
    ❯ /remember feedback: don't add docstrings to private functions
    ❯ /remember -g user: I'm a backend developer; explain frontend code in more detail
    ```

    `-g` saves it for every repository. Start with `feedback:`, `reference:` or `user:` to set the
    type (the default is `project`).

- **At the end of a session**, because local models rarely save anything on their own, lcode
  asks the model one short question: is anything from this session worth remembering? It happens
  when you quit, on `/clear`, and before the conversation is compacted (the summary leaves details
  out). The model proposes at most three notes:

    ```text
    ╭─ Worth remembering? ────────────────────────────────────────────────────────╮
    │ 1 [feedback] Use pnpm, not npm (this repository)                              │
    │ 2 [project] Integration tests need `docker compose up db` first (this         │
    │   repository)                                                                 │
    ╰───────────────────────────────────────────────────────────────────────────────╯
      Save them? [y]es / numbers to keep, e.g. 1,3 / [n]o: 1
    ```

    The question is quick: it reuses the conversation Ollama already has in memory, so the model
    only writes a few lines. Short sessions (one request and little work) are skipped, and Ctrl+C
    skips it too.

## Asking first: the `memory` setting

| `memory` | |
|---|---|
| `ask` (default) | Every note the model saves, and the end-of-session proposals, need your OK. Answer `a` once to allow the model's notes for the rest of the session. This holds in `yolo` mode too. |
| `auto` | Notes are saved without asking; lcode prints each one. |
| `off` | No notes are loaded or saved, and the model has no `memory` tool. |

```bash
lcode config set memory auto
```

`lcode --no-memory` turns memory off for one session. `lcode -p` runs never ask the end-of-session
question.

## Managing notes

| Command | |
|---|---|
| `/memory` | List the notes for this repository and for every repository |
| `/memory show 2` | Show a note with its details (by number or name) |
| `/memory edit 2` | Open it in your editor (`$VISUAL`, `$EDITOR`, nano or vi) |
| `/memory delete 2` | Delete it |
| `/memory path` | Where the notes are stored |

Changes you make with `/memory` apply from the next session. Notes added with `/remember` are
also mentioned to the model right away.

## How it works

Notes are plain markdown files with a small header, one fact per file:

```markdown
---
name: test-command
type: project
description: Run the tests with `uv run pytest -q`; plain pytest misses the dev dependencies
modified: 2026-10-05
---
The dev dependencies are in a uv dependency group, which plain pytest doesn't see.
```

| Folder | Notes |
|---|---|
| `~/.local/state/lcode/memory/projects/<repository>/` | For one repository, shared by all its git worktrees and subfolders |
| `~/.local/state/lcode/memory/user/` | For every repository |

Each folder also has a `MEMORY.md` listing its notes, for reading; lcode regenerates it, so edit
the notes rather than that file. A folder that isn't in a git repository gets its own notes.

At the start of a session, the model gets a one-line index of the notes (description, name, date)
in its instructions. The index is capped at about 2,000 tokens, or 5% of a small context window;
when there are more notes, the newest are shown and the model can list the rest with its `memory`
tool, which also reads a note's details. The dates let stale facts stand out, and the model is told
to trust what it finds in the code over an old note, and to fix the note.

Saving under an existing name replaces that note. A new note that says nearly the same as another
one is not saved twice: the model is told to update the existing note instead, and
end-of-session proposals that only restate a note are dropped.
