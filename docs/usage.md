# Using lcode

## Command line

```bash
lcode [options]              # interactive session in the current directory
lcode setup [model]          # pick, download and configure a model
lcode models                 # the model catalog and what fits this machine
lcode doctor                 # check the installation (include this in bug reports)
lcode config [set|unset]     # show or change settings
lcode bench [models]         # score models on small coding tasks on this machine
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
| `--no-web` | No web search or page fetching in this session |
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
| `/undo` | Undo the file changes of the last request ([details](#undo-and-checkpoints)) |
| `/rewind [N]` | Go back to before request N: its files, and optionally the conversation |
| `/checkpoints` | List the requests that changed files, and which files |
| `/compact [focus]` | Summarize the conversation to free context |
| `/context [size]` | Show how full the context window is and change its size: pick from a list with memory estimates, or give a size like `/context 128k` (`/ctx` is a shortcut) |
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
| `web_search` | Search the web for current information (needs a [search provider](#web-search)) |
| `web_fetch` | Read a web page or text file by URL as clean text |

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

## Undo and checkpoints

Before the model first changes anything in a request (an edit, a new file, or a shell command that
isn't read-only), lcode saves a checkpoint of your project. If the result isn't what you wanted,
`/undo` puts the files back:

```text
❯ /undo
Undoing request 3: switch the parser to the new tokenizer
  restore src/parser.py
  restore tests/test_parser.py
  remove  src/tokenizer_v2.py
Done: restored src/parser.py, tests/test_parser.py; removed src/tokenizer_v2.py.
```

- `/undo` again goes one more request back. The model is told which files were restored, so it
  reads them again instead of assuming its changes are still there.
- `/checkpoints` lists the requests that changed files. `/rewind 2` goes back to before request 2
  in one step (undoing every request after it too) and offers to remove those requests from the
  conversation as well.
- Changes made by shell commands are covered too (`sed -i`, generated files, `rm`), in any folder,
  whether or not it's a git repository.
- If you edited one of those files yourself after the model did, lcode lists it and asks before
  going back, since that would lose your edit. Files the model didn't touch are never changed.

Checkpoints are stored in a separate git repository under `~/.local/state/lcode/checkpoints/`. Your
own repository (its index, branches, stash and history) is never touched. Ignored files (per
`.gitignore`), `node_modules`, virtual environments and files over 10 MB aren't included. Folders
with more than 20,000 files, or your home folder itself, aren't checkpointed; lcode says so once and
carries on. Checkpoints are deleted after 14 days. To turn them off:
`lcode config set checkpoints false`.

## Web search

When the answer depends on information newer than the model's training data, such as the latest
version of a library, a changed API, an error message or current documentation, lcode lets the model
search the web and read pages by itself.

- **`web_fetch`** works out of the box: lcode downloads the page and converts it to clean text.
- **`web_search`** needs a search provider. Set one of these up once and lcode picks it automatically:

| Provider | Setup | Notes |
|---|---|---|
| [Ollama web search](https://docs.ollama.com/capabilities/web-search) | `export OLLAMA_API_KEY=…` (free key at [ollama.com/settings/keys](https://ollama.com/settings/keys)) | Easiest; also renders pages lcode can't read directly |
| [Brave Search API](https://brave.com/search/api/) | `export BRAVE_API_KEY=…` | |
| [Tavily](https://tavily.com) | `export TAVILY_API_KEY=…` | Built for AI agents |
| [SearXNG](https://docs.searxng.org/) (self-hosted) | `lcode config set searxng_url http://localhost:8888` | No account; enable the `json` format in its `settings.yml` |

Put the `export` line in your `~/.bashrc` or `~/.zshrc`. API keys are only read from the environment,
never stored in lcode's config. `lcode doctor` shows which provider is active, and the model is told
to mention the URLs it relied on.

??? note "Running SearXNG locally"

    ```bash
    mkdir -p ~/searxng && cat > ~/searxng/settings.yml <<'YAML'
    use_default_settings: true
    server:
      secret_key: "change-me-to-something-random"
      limiter: false
    search:
      formats: [html, json]
    YAML
    docker run -d --restart unless-stopped --name searxng -p 127.0.0.1:8888:8080 \
      -v ~/searxng:/etc/searxng searxng/searxng
    lcode config set searxng_url http://localhost:8888
    ```

**What leaves your machine:** search queries go to the provider, and fetched pages are downloaded
from their websites. lcode never uploads your files, but the model writes the queries, so a query can
contain names or snippets from your code. Choose how much web access the model gets:

| Setting | Effect |
|---|---|
| `lcode config set web on` | Default: the model searches and fetches pages when it needs to |
| `lcode config set web ask` | Ask before every search (and before fetching from each website) |
| `lcode config set web off` | No web access at all; `lcode --no-web` does the same for one session |

Web content is passed to the model marked as untrusted data, and the model is told never to follow
instructions found in it. Command and edit approvals still apply.

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
