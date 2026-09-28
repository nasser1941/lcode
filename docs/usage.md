# Using lcode

## Command line

```bash
lcode [options]              # interactive session in the current directory
lcode setup [model]          # pick, download and configure a model
lcode models                 # the model catalog and what fits this machine
lcode doctor                 # check the installation (include this in bug reports)
lcode config [set|unset]     # show or change settings
```

| Option | Meaning |
|---|---|
| `-p, --prompt TEXT` | Run one request non-interactively, print the result and exit |
| `-m, --model NAME` | Catalog key (see `lcode models`) or any installed Ollama model |
| `--context SIZE` | Context window, e.g. `65536`, `128k`, `1m` |
| `-r, --repo DIR` | Work in another directory |
| `-c, --continue` | Continue the most recently used session in this directory |
| `--resume [SESSION]` | Resume a saved session: pick from a list, or give its number, name or id |
| `--auto-edit` | Apply file edits without asking (commands still ask) |
| `--yolo` | Never ask for permission |
| `--no-think` | Turn off the model's reasoning: faster, less accurate |
| `--show-thinking` | Print the model's reasoning as it streams |
| `-V, --version` | Print the version |

## In the session

| Key | Action |
|---|---|
| ++enter++ | Send |
| ++esc++ ++enter++ or a trailing `\` | New line |
| ++ctrl+c++ | Interrupt the model or a running command |
| ++ctrl+d++ | Quit |
| ++shift+tab++ | Cycle permission mode: ask → auto-edit → yolo |
| ++tab++ | Complete slash commands and `@` paths |
| `@path` | Attach a file (or a directory listing) to your message |

The status bar shows the model, how full the context window is, the permission mode and whether
reasoning is on.

### Slash commands

| Command | What it does |
|---|---|
| `/help` | List commands and keys |
| `/init` | Analyze the repository and write `AGENTS.md` (read at every start) |
| `/clear` | Start a new conversation (the current one stays saved) |
| `/rename NAME` | Name the current session so it's easy to find later |
| `/resume [SESSION]` | Resume a saved session: pick from a list, or give its number or name; `/resume all` lists every folder |
| `/compact [focus]` | Summarize the conversation to free context |
| `/context` | Show how full the context window is |
| `/ctx [size]` | Show or change the context window |
| `/model [name]`, `/models` | Switch model, list models |
| `/think [on\|off]` | Toggle reasoning |
| `/verbose` | Toggle showing the reasoning text |
| `/mode [ask\|auto-edit\|yolo]` | Set the permission mode |
| `/cd DIR` | Change the working directory |
| `/todos` | Show the model's task list |
| `/exit` | Quit |

## What the model can do

| Tool | Purpose |
|---|---|
| `read_file` | Read a file with line numbers (paged for large files) |
| `write_file` | Create or overwrite a file (shows a preview or diff) |
| `edit_file` | Replace an exact snippet in a file (shows a diff); tolerates trailing-whitespace differences |
| `list_dir` | Directory tree, skipping `.git`, `node_modules`, virtualenvs and caches |
| `glob` | Find files by pattern, newest first |
| `grep` | Regex search with ripgrep (falls back to Python if ripgrep is missing) |
| `bash` | Run a shell command with live output, a timeout and a persistent working directory |
| `todo_write` | Keep a visible task list for multi-step work |

lcode refuses to edit a file the model hasn't read in the session, or one that changed on disk since
it was read, so the model always edits the current version.

## Permissions

| Mode | File edits | Shell commands |
|---|---|---|
| `ask` (default) | ask | ask, except read-only commands |
| `auto-edit` | automatic | ask, except read-only commands |
| `yolo` | automatic | automatic |

Read-only commands run without asking: `ls`, `cat`, `head`, `tail`, `grep`, `rg`, `find`, `tree`,
`wc`, `diff`, `git status/log/diff/show/branch/blame` and similar, as long as they don't redirect
output, chain commands or run subshells.

When asked, answer ++y++ (once), ++a++ (always, for this command or for all edits, until you quit)
or ++n++. Text after ++n++ goes to the model as instructions: `n run the tests with -x first`.

!!! warning "yolo mode"

    In `yolo` mode the model can run any command as your user. Use it in a container, a VM or a
    throwaway clone, not on a machine with data you care about.

## Project instructions: AGENTS.md

At startup lcode reads `AGENTS.md` (or `LCODE.md`, or `CLAUDE.md`) from the working directory and
adds it to the model's instructions. Use it for build and test commands, architecture notes and
conventions. `/init` writes a first version for you.

## Sessions

Every conversation is saved after each request to `~/.local/state/lcode/sessions/`, so you can
leave and pick up where you stopped.

```text
❯ /rename auth refactor        # name the current session
❯ /resume                      # list this folder's sessions and pick one
```

```text
Saved sessions · /home/you/code/my-project
 #  Session                                  Last used   Requests
 1  auth refactor                            2 h ago           14
    Explain how login tokens are validated
 2  The tests in tests/test_parser.py fail…  yesterday          6
  Resume which session? (number or name, Enter to cancel): 1
```

Unnamed sessions are listed by their first request. After resuming, lcode shows your last request
and the start of its last answer so you know where you left off.

| From the shell | From a session | Resumes |
|---|---|---|
| `lcode -c` | | the most recently used session in this folder |
| `lcode --resume` | `/resume` | one you pick from a list |
| `lcode --resume "auth refactor"` | `/resume auth refactor` | a session by name, list number, id or a unique part of its title |
| | `/resume all` | a session from any folder (lcode switches to that folder) |

`/clear` starts a new conversation and keeps the old one saved. Files may have changed since a
session was saved, so after resuming the model has to read a file again before editing it.

## Scripting

`-p` runs one request and exits, which is handy in scripts and git hooks:

```bash
lcode -p "Summarize the changes in the last 5 commits" --no-think
lcode -p "Review the uncommitted changes (git diff) for bugs"
lcode -p "Run the tests and fix any failures" --auto-edit
```

Combine with `--yolo` only in disposable environments.
