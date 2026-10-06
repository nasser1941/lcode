# Quickstart

This walks through a first session. It assumes you [installed lcode](installation.md) and ran
`lcode setup`.

## Start a session

```bash
cd ~/code/my-project
lcode
```

```text
╭──────────────────────────────────────────────╮
│ lcode v0.20.0 — local coding agent           │
│                                              │
│ model    lcode-qwen3.6-35b                   │
│ context  256K tokens                         │
│ cwd      /home/you/code/my-project           │
│ mode     ask (Shift+Tab to cycle)            │
│ memory   ask · no notes yet (/remember)      │
╰──────────────────────────────────────────────╯
❯
```

The model loads in the background while you type your first request (10–45 seconds the first
time).

## Teach it your project

```text
❯ /init
```

lcode explores the repository and writes an `AGENTS.md` file: an overview, how to build and test,
the architecture and the conventions it found. It reads this file at the start of every session,
so later requests start with context. Edit it like any other file; commit it if it's useful to
your team.

## Ask about the code

```text
❯ How does a request flow from the HTTP handler to the database? Cite files.
```

lcode searches and reads the relevant files and answers with `path:line` references. Tool calls
appear as they happen:

```text
● grep('def handle_' in src)
  ⎿ 14 line(s)
● read_file(src/api/handlers.py)
  ⎿ 212 line(s)
```

Attach files directly with `@` (Tab completes paths):

```text
❯ Explain the retry logic in @src/client.py
```

## Let it change things

```text
❯ Write scripts/dedupe.py that removes duplicate rows from data/users.csv by email, then run it on the sample file.
```

Before editing a file or running a command, lcode shows the diff or command and asks:

```text
╭─ Create scripts/dedupe.py ─────────────────────────╮
│   1 import csv                                     │
│   2 import sys                                     │
│   …                                                │
╰────────────────────────────────────────────────────╯
  Allow? [y]es / [a]lways for 'file edits' this session / [n]o (+ optional reason):
```

- ++y++ allows this once, ++a++ allows this kind of action for the rest of the session.
- ++n++ refuses. Anything you type after it is passed to the model, for example
  `n use pandas instead`.
- ++shift+tab++ switches between `ask`, `auto-edit` (edits don't ask) and `yolo` (nothing asks).

Read-only commands such as `ls`, `cat`, `grep` and `git status` run without asking.

## Keep going

- ++ctrl+c++ interrupts the model or a running command; you keep the conversation.
- `/compact` summarizes a long conversation to free context (this also happens automatically at 85%).
- `lcode -c` continues the last session in this folder; `/rename` names a session and `/resume`
  picks one from a list.
- `/help` lists everything else.

Next: [everything lcode can do](usage.md), or [choosing models and context windows](models.md).
