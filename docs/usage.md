# Using lcode

## Command line

```bash
lcode [options]              # interactive session in the current directory
lcode setup [model]          # pick, download and configure a model
lcode models                 # the model catalog and what fits this machine
lcode doctor                 # check the installation (include this in bug reports)
lcode media [install MODEL]  # image and video models: what fits here, what's installed; install one
lcode config [set|unset]     # show or change settings
lcode bench [models]         # score models on small coding tasks on this machine
lcode mcp [catalog|add|list] # connect MCP servers: Jira, GitHub, AWS, databases… (see MCP servers)
lcode acp                    # run inside an editor over the Agent Client Protocol (see Editors)
lcode update [--check]       # update lcode, and Ollama when it runs in Docker (see Installation)
```

| Option | Meaning |
|---|---|
| `-p, --prompt TEXT` | Run one request non-interactively, print the result and exit |
| `--output json\|stream-json` | With `-p`: print a JSON result, or a JSON event per line ([Automation](automation.md)) |
| `--max-steps N` | Stop a request after N model steps |
| `--allowed-tools LIST` | Only these tools, e.g. `"read_file,grep,glob,bash"` |
| `-m, --model NAME` | Catalog key (see `lcode models`) or any installed Ollama model |
| `--context SIZE` | Context window, e.g. `65536`, `128k`, `1m` |
| `-r, --repo DIR` | Work in another directory |
| `-c, --continue` | Continue the most recently used session in this directory |
| `--resume [SESSION]` | Resume a saved session: pick from a list, or give its number, name or id |
| `-w, --worktree [NAME]` | Work in a [git worktree](git.md#parallel-sessions-lcode-worktree) of its own, on branch NAME; removed at the end if nothing changed |
| `--plan` | Start in [plan mode](#plan-mode): agree on a plan before anything changes |
| `--auto-edit` | Apply file edits without asking (commands still ask) |
| `--yolo` | Never ask for permission |
| `--no-think` | Turn off the model's reasoning: faster, less accurate |
| `--no-web` | No web search or page fetching in this session |
| `--no-mcp` | Don't start [MCP servers](mcp.md) in this session |
| `--no-memory` | Don't load or save [memory notes](memory.md) in this session |
| `--sandbox` | Run shell commands in a container that only sees the project ([Sandbox](sandbox.md)) |
| `--show-thinking` | Print the model's reasoning as it streams |
| `-V, --version` | Print the version |

## In the session

| Key | Action |
|---|---|
| ++enter++ | Send |
| ++esc++ ++enter++ or a trailing `\` | New line |
| ++ctrl+c++ | Interrupt the model or a running command |
| Typing while it works | A note for the model, read at its next step ([Steer while it works](#steer-while-it-works)) |
| ++esc++ while it works | Pause after the current step |
| ++ctrl+d++ | Quit |
| ++shift+tab++ | Cycle permission mode: ask → plan → auto-edit → yolo |
| ++tab++ | Complete slash commands and `@` paths |
| `@path` | Attach a file (or a directory listing) to your message |
| `@name` | Ask for a [subagent](agents.md) of that type, e.g. `@plan` |

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
| `/name [args]` | Run one of your [commands or skills](commands.md); `/help` lists them |
| `/agents [N]` | [Subagent](agents.md) types, and what this session's subagents did |
| `/remember [-g] TEXT` | Save a [note](memory.md) for later sessions (`-g`: for every repository) |
| `/memory [show\|edit\|delete N]` | The [notes](memory.md) lcode remembers |
| `/sandbox [network on\|off]` | The [sandbox](sandbox.md) for shell commands |
| `/mcp [tools\|login\|restart NAME]` | [MCP servers](mcp.md), their status and tools |
| `/plan [request]` | Work out a [plan](#plan-mode) before changing anything, or show the approved plan |
| `/compact [focus]` | Summarize the conversation to free context |
| `/context [size]` | Show how full the context window is, what's using it (by category), and change its size: pick from a list with memory estimates, or give a size like `/context 128k` (`/ctx` is a shortcut) |
| `/models`, `/model [number or name]` | List the models and how they fit this machine, numbered: type a number to switch, or `/model 2`, `/model qwen3.5-9b` |
| `/think [on\|off]` | Toggle reasoning |
| `/verbose` | Toggle showing the reasoning text |
| `/mode [code\|image\|video]` | Switch what lcode makes: [code, images or videos](modes.md) |
| `/options`, `/again`, `/vary N`, `/gallery`, `/open N` | Image mode: [options for the session, follow-ups on results](modes.md#results) |
| `/edit N`, `/use N`, `/extend N`, `/upscale N`, `/nobg N` | Image mode: [edit, follow, outpaint, upscale, cut out](modes.md#editing) |
| `/animate N`, `/play N` | [Video mode](modes.md#video-mode): animate image N; play a clip |
| `/film <idea>` (`plan`, `make`, `shot N`, `stitch`, `list`) | [A short film](modes.md#films) with the same characters in every shot |
| `/restyle N`, `/smooth N`, `/sound N`, `/upscale N` | [Video mode](modes.md#video-mode): a new look, more frames, sound, more pixels |
| `/permissions [ask\|plan\|auto-edit\|yolo]` | Set the permission mode |
| `/cd DIR` | Change the working directory |
| `/todos` | Show the model's task list |
| `/status` | The task sheet: your latest requests, the plan and the model's notes, files changed, the last check |
| `/escalate` | Ask the [stronger model](#a-stronger-model-when-stuck) for advice on the work, with your OK; `/escalate <what's wrong>` says what to look at |
| `/jobs [stop ID]` | The [background commands](#background-commands) the model started; stop one |
| `/commit [notes]` | [Commit](git.md#commit) the changes with a message in the repository's style, once you approve it |
| `/review [base]` | [Review](git.md#review) the uncommitted changes, or the branch against a base |
| `/pr [base] [notes]` | Push the branch and open a GitHub [pull request](git.md#pr), once you approve it |
| `/exit` | Quit |

## What the model can do

| Tool | Purpose |
|---|---|
| `read_file` | Read a file with line numbers (paged for large files) |
| `write_file` | Create or overwrite a file (shows a preview or diff) |
| `edit_file` | Replace a snippet in a file (shows a diff); [forgiving](#imperfect-edits) when the model copies it imperfectly |
| `apply_patch` | Apply a unified diff (as `git diff` writes it) to one or more files; hunks are found by their text |
| `list_dir` | Directory tree, skipping `.git`, `node_modules`, virtualenvs and caches |
| `glob` | Find files by pattern, newest first |
| `grep` | Regex search with ripgrep (falls back to Python if ripgrep is missing) |
| `bash` | Run a shell command with live output, a timeout and a persistent working directory (a `cd` within the project is kept, one out of it isn't); with `background`, keep it running ([details](#background-commands)) |
| `bash_output`, `bash_stop` | Read what a background command printed since last time; stop it |
| `todo_write` | Keep a visible task list for multi-step work, with notes (decisions, findings) that survive when old messages are removed; `/status` shows them |
| `web_search` | Search the web for current information (needs a [search provider](#web-search)) |
| `web_fetch` | Read a web page or text file by URL as clean text |
| `view_image` | Look at an image: a model that can see describes it ([Images](#images)) |
| `screenshot` | Run a desktop program or open a web page without a screen, and see it with its sizes ([Screenshots](#screenshots)) |
| `agent` | Hand a task to a [subagent](agents.md) with its own context; only its report comes back |
| `memory` | Save, update or delete [notes](memory.md) that later sessions load |
| `repo_map` | The [repository map](search.md): important files, classes and functions, ranked (larger repositories; small ones get it in the instructions) |
| `search_code` | Find code by meaning with a local [embedding model](search.md#semantic-code-search), once the repository is indexed |
| `lsp` | Ask a [language server](lsp.md) for a symbol's definition, references, type, or a file's outline |
| `skill` | Load a [skill](commands.md#skills)'s instructions when a task matches it |
| `present_plan` | In [plan mode](#plan-mode): show the plan and ask for approval |

lcode refuses to edit a file the model hasn't read in the session, or one that changed on disk since
it was read, so the model always edits the current version.

### Imperfect edits

Small models copy text imperfectly, and an edit that doesn't match exactly used to fail, often again
and again. When the text isn't in the file exactly, lcode tries, in order:

- **Copying slips:** removing the line numbers `read_file` shows, and undoing stray escapes such
  as `\"`.
- **Looser line matches:** comparing lines without trailing spaces, then without indentation (a block
  at the wrong level, or spaces where the file has tabs). The new text is re-indented to fit.
- **Blank lines:** ignoring blank lines the model dropped or added.
- **A very close match:** at least 90% alike, and clearly better than any other place. Only the lines
  the model meant to change are changed, so an earlier edit that the model's copy doesn't show is
  kept. If a changed line differs in the file by more than a typo, the edit is refused.

The result says how it matched (`Edited config.py (1 replacement(s), ignoring indentation)`), and the
diff shows the change as usual. A match that's ambiguous or too weak is never applied. Instead, the
model gets the closest lines with their numbers, so it can copy them exactly. `apply_patch` places
each hunk the same way, so wrong `@@` line numbers don't matter, and it changes a file only if every
hunk fits.

Other common slips get a useful answer instead of a bare error:

- **A pattern that isn't a valid regex** (`grep "def add("`) is searched as plain text, and the result
  says so.
- **A path that doesn't exist** comes back with the closest existing ones: the same file name
  elsewhere in the project, or a near name in the folder (`Did you mean: src/pkg/math_utils.py?`).
- **`read_file` on a folder** lists it, and **`list_dir` on a file** says to read it.

## Checks before done

Small models like to call work done after a syntax check. So after a request that changed files, lcode
runs the project's checks itself before the answer is final:

```text
◆ Checking the work: python -m unittest (from test files)
⚠ Checks fail (python -m unittest, exit code 1): the model gets the output to fix it.
◆ Checking the work: python -m unittest (from test files)
◆ Checks pass: python -m unittest
```

- **When they fail,** the model gets the output and keeps working, twice at most. After that its answer
  says what still fails instead of claiming success. A failure that the change didn't cause is
  reported, not chased, and the checks run again only if the model changes something more.
- **When nothing could run** (no tests collected, or the test runner isn't installed), lcode says so
  and doesn't send it to the model.
- **When the model already ran them** after its last change and they passed, they aren't run again.
- **Permission:** they run like any other command, so in `ask` mode the first run asks (++a++ allows
  them for the session).
- **Programs** using `lcode -p --output json` see the runs in the result's `checks`.

**Which checks.** lcode looks at the project: pytest when the configuration mentions it (`uv run
pytest -q` with a `uv.lock`), when the tests import it, or when a requirements file lists it;
otherwise `python -m unittest` for `test_*.py` files. Python checks run with the project's own
environment when it has one (`.venv/bin/python`, `venv`, `env`), where its dependencies are. Then
`npm test` (or pnpm, yarn, bun) when `package.json` has a test script, `cargo test`, `go test ./...`
and `make test`. To choose the command yourself, set it for the repository in
`.lcode/settings.toml`. That file is used once you trust the repository, like its hooks:

```toml
[checks]
command = "uv run pytest -q && uv run ruff check ."
```

Or set it for yourself with `lcode config set checks "make check"`; `lcode config set checks off`
turns checks off.

**Windows it never looked at.** A model that changes a window checks it with an import and the
project's tests, which don't show what's on the screen, and calls it done: you find a blank
window. When a request changed a Python file that draws one (it imports PyQt, PySide, tkinter,
Kivy, wx or GTK) and the model never took a [screenshot](#screenshots) or looked at an image,
lcode sends it back once: look at it, with clicks first if that's where the change shows, or say
plainly that it's unchecked. `checks = "off"` turns this off.

**Scripts it never ran.** A model that writes a build or install script tends to call it done
without running it once, and the first thing you see is its first error. When a request wrote a
shell script (a `.sh` file, or a file with a shell shebang) and no command in it ran the script,
lcode sends it back once before the answer counts: run it and fix what fails, or say plainly that
it's untested and why, when running it would install, deploy or delete things. Making it executable
or reading it doesn't count as running it. `checks = "off"` turns this off too.

## A review before the answer

Models claim more than they did: half of a two-part request done, "it works now" without having
checked, a debug print left behind. So after a request that changed files (and after the
[checks](#checks-before-done)), lcode asks the model to step back and review its change against your
request. It's told to judge only from what you asked, the diff, and its own answer:

```text
◆ Review: the change does what was asked.
⚠ Review: 1 point(s) sent back to the model. First: README.md isn't changed, though …
```

Problems it can point to go back to the model once; it fixes them, or says why a point is wrong.
`lcode -p --output json` reports what the review found in `review`.

**It's off by default,** because it's an extra model answer after every change. On the 12 GB laptop
with qwen3.6-35b, the 8 `lcode bench` tasks took 416 s with the review against 240 s without
(8/8 against 7/8, within run-to-run noise). It found a real gap in one run of `runtime-bug` ("it can't
be made smaller" left undone), which the model then argued away. Without reasoning, the review was
no faster and missed that gap every time, so it reasons like the model does. Turn it on with
`lcode config set review auto` when a stronger or faster model makes the extra look worth it.

The review is asked on top of the conversation rather than in a fresh one. The model server keeps
the processed conversation cached, and a separate review would push it out, so the next request
would have to read the whole conversation again. On a small GPU that's tens of seconds.

## A stronger model when stuck

Some problems are beyond a local model. When it's stuck, lcode can ask a stronger one for
advice: a cloud model with your API key, one of [Ollama's cloud models](models.md#cloud-models-through-ollama)
(less than a cent a question for some), or a bigger local model. The local model gets the advice
and carries on with its own tools. It's off until you choose the model:

```bash
lcode config set escalate_model anthropic/<model>   # with ANTHROPIC_API_KEY in your environment
lcode config set escalate_model openai/<model>      # OPENAI_API_KEY
lcode config set escalate_model xai/<model>         # XAI_API_KEY
lcode config set escalate_model ollama/qwen3.8-27b  # a bigger local model: free, and nothing leaves
lcode config set escalate_model ollama/glm-5.3-flash:cloud  # Ollama's cloud, after `ollama signin`
lcode config set escalate_price 3/15                # dollars per million tokens in/out, for the cost
```

**When.** lcode offers it when the [loop breaker](how-it-works.md#when-the-model-repeats-itself)
would stop the request, when the model's replies [go nowhere](how-it-works.md#when-a-reply-goes-nowhere)
twice, and when the project's [checks](#checks-before-done) fail twice in a row after the model's
fixes, at most once per request. Not for a [site this machine can't reach](how-it-works.md#when-a-site-cant-be-reached):
a stronger model can't reach it either. You can also ask at any time: `/escalate`, or
`/escalate the window still grows` to say what's wrong.

**Nothing is sent without asking**, in any permission mode:

```text
╭─ Ask claude-… for help? ──────────────────────────────────────────────────╮
│ Why: the project's checks (`pytest -q`) failed 2 times in a row          │
│ What goes: your requests, the task sheet, the failure, the last steps     │
│ and these files: wall.py, simulate.py (3.1K tokens)                       │
│ Where: claude-… (Anthropic), at api.anthropic.com                         │
│ Cost: about $0.009 to $0.071 (3.1K tokens in at $3/M, up to 4.1K out …)  │
╰───────────────────────────────────────────────────────────────────────────╯
  Send it? [y]es / [n]o / [s]how what's sent:
```

- **What goes:** your last requests, the [task sheet](how-it-works.md#context-management), why
  it's stuck (the failing check's output, the repeated call), the last few steps, and the files the
  request changed or read: never the whole session or the rest of the repository. Press `s` to read
  all of it before you decide.
- **Keys** come from environment variables, are read when needed, and go only to their own
  provider. lcode never stores them.
- **Cost.** The token count is always shown; the price only when you set `escalate_price`, since
  prices change. A local model is free; an Ollama cloud model isn't, and the panel says it goes to
  ollama.com.
- **Another server:** `escalate_url` points at another Ollama (`ollama/<model>`) or any OpenAI
  API (`custom/<model>`, key in `LCODE_ESCALATE_API_KEY`).
- `lcode -p` can't ask, so it doesn't escalate unless you add `--escalate`, which is your OK for
  that run. `--output json` lists what was asked in `escalations`.

## Background commands

Some commands keep running: a dev server, a test watcher, a build in watch mode. The model starts
one with `bash` and `background: true` and carries on working. It reads what the command printed
since last time with `bash_output`, and stops it with `bash_stop`.

```text
  $ npm run dev  (in the background)
    > vite
    VITE v6.0.0  ready in 312 ms
  ⎿ background job 1, /jobs to see it
```

- `/jobs` lists the background commands; `/jobs stop 1` stops one.
- They all stop when the session ends.
- Anything else a command starts with `&` is stopped when that command ends. Only background
  commands keep running, so no process outlives the session unnoticed.
- Background commands ask for permission like any other command, and run in the
  [sandbox](sandbox.md) when it's on.

## What's happening under the hood

Much of the waiting with a local model isn't the model thinking: it's loading the model, or reading
a long conversation before the first word. lcode says which, in the status line under a request:

```text
⠋ Loading qwen3.6-35b into memory… (0 tokens, 9s · type a note · Esc pauses · Ctrl+C stops)
⠋ Reading the conversation (18K tokens)… (0 tokens, 21s · type a note · Esc pauses · Ctrl+C stops)
⠋ Thinking… (312 tokens, 27s · type a note · Esc pauses · Ctrl+C stops)
⠋ Running the command… (48s · type a note · Esc pauses · Ctrl+C stops)
```

It prints a line only when something worth knowing happens, and nothing while the model simply
stays loaded:

| When | What it says |
|---|---|
| The model was loaded | `◆ Loaded qwen3.6-35b in 15s: 22.3 GB, 45% on the GPU and the rest in system RAM.`, and a warning when less of it is on the GPU than usual, because another program or model is using GPU memory |
| A dense model doesn't fit on the GPU (at startup and on `/model`) | `⚠ qwen3.8-27b is a dense model: all 27B parameters work on every token. It's capable, but slow here. Only about 61% of it fits in the 12 GB of GPU memory…`, with what to do instead |
| The model that looks at images needs the memory | `Loading the vision model … to look at bug.png; qwen3.6-35b makes room and reloads afterwards`, then `◆ qwen3.6-35b was unloaded to make room…`, and the next step says `Reloading qwen3.6-35b into memory (it made room for the vision model …)` |
| A tool needs the GPU to itself (an MCP server with `free_gpu`) | `◆ Unloaded qwen3.6-35b so the server can use the GPU; it reloads afterwards.` |
| Ollama ran out of memory | `⚠` what ran out (the prompt batch, or the context next to the model), what lcode retries with, and what it remembers for next time |

While the model loads in the background at startup, the status bar says
`loading the model into memory…`. Mixture-of-Experts models, such as qwen3.6-35b, run well with
part of them in system RAM. Dense ones slow down a lot, to a few tokens a second, which is why they
get the warning.

## Steer while it works

A request can take minutes with a local model. You don't have to stop it to change course: type
while it works, and press ++enter++. The line goes to the model at its next step, after the step it's
on finishes, as a note from you. Nothing is interrupted or thrown away.

```text
● edit_file(tally.py)
  ✓ Edited tally.py
✎ Note for the model: also add a --min-count N option that skips rare words
```

What you type shows under the spinner, since the terminal doesn't echo it while lcode works:

```text
⠋ Thinking… (212 tokens, 14s · type a note · Esc pauses · Ctrl+C stops)
  ✎ also add a --min-count N opt▏  Enter adds it as a note
```

- A note typed while the model writes its final answer still counts: the model reads it before the
  request ends.
- Press ++esc++ to pause after the current step, to look at what it did. Then press ++enter++ to go
  on, type a note to go on with it, or press ++ctrl+c++ to stop the request. Press ++esc++ again
  before the step ends if you change your mind.
- Notes count as your requests: they're kept when old messages are
  [summarized](how-it-works.md#context-management), and the [review](#a-review-before-the-answer)
  checks the change against them too.
- When lcode asks you something, such as a permission, the keyboard is yours again for the answer.
  Keys you typed just before the question appeared are dropped, so a note you were finishing can't
  answer it by accident.
- What never reached the model, such as a note typed as the request ended or after ++ctrl+c++, is
  put back in the prompt.

Steering works in the interactive session on macOS and Linux. `lcode -p` and the editor
integration don't read the keyboard during a request.

## Notifications

Local models take a while, so you'll often switch to something else during a request. lcode shows
a desktop notification:
- when a request that ran longer than 30 seconds is done;
- when, during such a request, lcode waits for your answer (a permission question or a plan).

It uses `notify-send` on Linux and `osascript` on macOS, and rings the terminal bell where neither
works. Change the time with `lcode config set notify_after 60`, or turn notifications off with
`lcode config set notify false`. For something else, such as a sound or a phone message, use a
[notification hook](hooks.md#hooks).

## Images

lcode can look at screenshots, mockups, diagrams and photos:

```text
❯ the layout breaks on mobile, see @screenshots/mobile.png
❯ make the settings page match @design/settings.png
```

- **Paste a screenshot with ++ctrl+v++.** A terminal can only paste text, so lcode reads the
  clipboard itself, saves the image in its state folder (`~/.local/state/lcode/pasted/`, the last
  50) and puts `@path` in the prompt. Anything else on the clipboard is pasted as text. It needs
  `wl-clipboard` on Wayland or `xclip` on X11 (`sudo apt install wl-clipboard`); on macOS it works
  as is, and faster with `brew install pngpaste`. `lcode doctor` shows whether it works.
- Attach an image with `@path`, like a file, or just write its path: `/tmp/shot.png`,
  `~/Pictures/bug.png`, `'./shots/My Shot.png'` and `file://` paths dragged into the terminal are
  attached too. lcode describes the image in detail, with all visible text transcribed and with your
  question in mind, and the model works from that description.
- **A path to an image that doesn't exist** is reported right away
  (`/tmp/pasted-image-.png doesn't exist, so the model can't see it`), and the model is told it
  can't see the image, so it asks instead of guessing what it shows. A bare name such as `logo.png`
  may be a file you want created, so it's left alone.
- The model can open images itself with the `view_image` tool, for example a screenshot a test wrote.
- Screenshots that [MCP](mcp.md) tools return, such as the Playwright browser's, are described too.

**Which model looks.** lcode's text-only model variants leave out the vision part to save GPU
memory, so lcode borrows the original model, which `lcode setup` already downloaded (for example
`qwen3.6:35b-a3b-coding` for `qwen3.6-35b`). If your model can see, it looks itself. `lcode doctor`
shows which one is used. With another model, install one that can see and point lcode to it:

```bash
ollama pull qwen3-vl:8b
lcode config set vision_model qwen3-vl:8b
```

Loading a second model takes a moment: on a 12 GB GPU, describing an image with the original
`qwen3.6:35b-a3b-coding` takes about a minute, and the next request reloads the main model. Turn
image support off with `lcode config set vision_model off`.

## Screenshots

Layout bugs are invisible in the code: a window bigger than the screen, panels cut off, a page that
scrolls sideways. With the `screenshot` tool the model runs the program, or opens the page, without
a screen and sees what you would, measured:

```text
● screenshot(python camera_wall.py)
  ⎿ .lcode/screenshots/20261014-120211-camera-wall.png (Qt offscreen (PyQt6), 1366x768 screen)
```

```text
- Window "Camera Wall" (CameraWall): 1362x764 at (0, 0)
  Its layout needs at least 1950x984, more than the screen has
  Sticks out of the window: Panel #panel-4: 640x480 at (9, 385)
  Overlap (190x480): Panel #panel-1: 640x480 at (9, 9) and Panel #panel-2: 640x480 at (459, 9)
```

| What | How | Besides the picture |
|---|---|---|
| A Python Qt program (PyQt5/6, PySide2/6) | Qt's offscreen platform, on a screen of the size asked for (Qt 6) | Each window's size and position, the size its layout needs, widgets that stick out or overlap |
| Any other desktop program (Linux) | A virtual display, Xvfb | The size and position of each window |
| A web page, an `.html` file or a local server | Headless Chrome, Chromium or Edge (Firefox takes the picture only) | The page's size against the window and what sticks out sideways, console errors, failed requests |

- For a web app, the model gives the command that starts the server and the page's URL: lcode
  starts the server, waits for the page to answer, takes the shot and stops the server.
- **Clicks and keys first.** For something that only shows after a click, a Python Qt program can
  take `actions` before the picture, the way you would: `double_click 300,200` (a point in the
  window), `double_click Camera 1` (the widget showing that text), `click Save`, `right_click …`,
  `key Escape`, `key ctrl+s`, `type some text`, `wait 2`. The result says what each step hit.
- **Qt's warnings come first.** Qt names many layout mistakes in what the program prints, often
  among the program's own logs. lcode lists them at the top of the report:

    ```text
    ● screenshot(python camera_wall.py)  actions: double_click Camera 1
    Qt printed warnings (they often name the cause):
    - QWidget::setLayout: Attempting to set QLayout "" on FocusedCamera "", which already has a layout
    Steps, before the picture:
    - double_click Camera 1: on QLabel "Camera 1 — Connected" at (207, 317)
    ```
- A model that can see describes the picture with the model's question in mind (see
  [Which model looks](#images)). Without one, the measurements still come back.
- Shots are saved in `.lcode/screenshots/` in the project, which keeps itself out of git, so you
  can look too.
- Running a program asks for permission like any command. A web page that isn't local follows the
  `web` setting. With the [sandbox](sandbox.md) on, programs can't be shown, since they don't run on
  your machine.
- Needs: Xvfb for other desktop programs (`sudo apt install xvfb x11-apps`), and Chrome or
  Chromium for web pages. On macOS only Qt programs and web pages work, since there's no virtual
  display.

## Permissions

| Mode | File edits | Shell commands |
|---|---|---|
| `ask` (default) | ask | ask, except read-only commands |
| `plan` | refused | only read-only commands ([Plan mode](#plan-mode)) |
| `auto-edit` | automatic | ask, except read-only commands |
| `yolo` | automatic | automatic |

++shift+tab++ cycles through them in this order, `/permissions` sets one, and `--plan`, `--auto-edit` and
`--yolo` start in one.

Read-only commands run without asking: `ls`, `cat`, `head`, `tail`, `grep`, `rg`, `find`, `tree`,
`wc`, `diff`, `git status/log/diff/show/branch/blame` and similar. Their arguments count too:
`sort -o file`, `git branch -D`, `git log --output=file`, `rg --pre` or `find -delete` change
things, so they ask. Pipes between read-only commands are fine, and so are `cd <folder> &&` in front
and `2>&1` or `>/dev/null`; anything else (`&&`, `;`, redirecting to a file, `$(…)`) asks.

When asked, answer ++y++ (once), ++a++ (always, for this command or for all edits, until you quit)
or ++n++. "Always" covers the same program, or for a chain the same programs in the same order:
allowing `npm test` doesn't allow `npm test && rm -rf build`. For lasting answers, write [permission rules](hooks.md#permission-rules): allow lists and
deny lists, per user or per repository; deny always wins, even in `yolo`. Text after ++n++ goes to the model as instructions: `n run the tests with -x first`.

!!! warning "yolo mode"

    In `yolo` mode the model can run any command as your user. Use it with the
    [sandbox](sandbox.md), in a VM or in a throwaway clone, not on a machine with data you care
    about.

With the [sandbox](sandbox.md) on, `auto-edit` mode runs shell commands without asking too: they can
only reach the project, and `/undo` can take their changes back.

### Secrets in commits

Before the model runs `git commit` or `git push`, lcode checks what would go into the repository's
history: the lines a commit adds (including files a `git add` in the same command stages), or every
commit a push would send. It looks for:

- passwords, keys and tokens written as values in code or configuration, like
  `"password": "…"` or `API_KEY=…`;
- credentials in addresses, like `rtsp://admin:…@192.168.1.10`;
- private keys and tokens with a known form (GitHub, GitLab, AWS, Slack, OpenAI, Anthropic, xAI,
  Google, Hugging Face, Stripe);
- `.env` files.

Placeholders such as `CHANGE_ME`, `your_password`, `${PASSWORD}` or `os.environ["PASSWORD"]` don't
count.

```text
This commit would add what looks like a secret to the repository's history:
- camera_wall.py:42: a password or key: "password": "Xk…"
```

- **In `ask` mode,** you decide: lcode shows the findings and the command.
- **In `auto-edit` and `yolo`,** the command doesn't run. The model is told to read the secret from
  an environment variable or a configuration file that git ignores, and to ask you if it isn't one.
- **`/commit` and `/pr`** show the same findings before you confirm.
- **The [GitHub Action](automation.md#github-action)** doesn't commit or push at all when it finds
  one.

If a line only looks like a secret, add `lcode: allow-secret` (or `gitleaks:allow`) in a comment
on it. `lcode config set secret_check false` turns the check off. It's a safety net, not a full
scanner: for a public repository, also run a tool such as [gitleaks](https://github.com/gitleaks/gitleaks)
before you push.

## Plan mode

Local models are slower than cloud models and make more mistakes, so on a bigger change it pays to
agree on the approach before the model starts editing. In **plan mode** the model can read, search,
run read-only commands, use the web and [explore and plan subagents](agents.md), but it can't
change anything. It ends by presenting a plan: numbered steps, the files it will change, the risks
and how it will verify the work.

```text
❯ /plan add retries to the webhook handlers
  …
╭─ Plan: Retries for webhook handlers ───────────────────────────────────────────╮
│ 1. Wrap the HTTP call in payments/webhooks.py:88 in a retry loop (3 tries, …)  │
│ 2. Add tests for a timeout and a 500 in tests/test_webhooks.py                 │
│ Risks: duplicate deliveries if the endpoint isn't idempotent …                 │
│ Verify: pytest tests/test_webhooks.py                                          │
╰────────────────────────────────────────────────────────────────────────────────╯
  [y] run it, asking before changes · [a] run it, edits without asking (auto-edit)
  [e] edit the plan · [s] save it to .lcode/plans/ · [n] keep planning (+ what to change)
  >
```

| Answer | |
|---|---|
| ++y++ or ++enter++ | Approve: lcode switches to `ask` mode and the model starts on the plan right away |
| ++a++ | Approve and switch to `auto-edit`, so file edits don't ask |
| ++e++ | Edit the plan in your editor first; the model follows your version |
| ++s++ | Save it as `.lcode/plans/<title>.md`, for example to share it or commit it |
| ++n++ + text | Send it back: `n keep the old API working` |

After approval, the todo list is filled from the plan's numbered steps, and the plan is kept word
for word when the conversation is compacted. `/plan` shows the approved plan.

Start planning with `/plan <request>` (switches to plan mode and sends the request), with
++shift+tab++ (one step from `ask`), or with `lcode --plan`.

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

Instructions that are only needed for some tasks fit better in a [command or skill](commands.md),
which costs no context until it's used.

What doesn't belong in `AGENTS.md` but should carry over, such as your corrections and preferences,
goes into lcode's [memory](memory.md): short notes the model, you (`/remember`) or a short check at
the end of a session save, and that every later session in the repository loads.

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
